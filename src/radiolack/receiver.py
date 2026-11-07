"""Receive-only I/Q sharing: rtl_tcp child process, local bridge and frame capture.

Flow: RTL-SDR -> rtl_tcp -> local bridge -> Gqrx
                                   `-> FFT capture (same bytes, never altered)

The bridge relays the I/Q stream to one Gqrx client and forwards Gqrx's
5-byte rtl_tcp commands upstream. It observes those commands to know the
actual hardware center, sample rate and gain controls, and invalidates
captured samples after every change so that stale I/Q is never analysed.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .detection import FRAME_BYTES
from .errors import ReceiverError, StoreWriteError

# rtl_tcp command codes (first byte of each 5-byte command).
CMD_SET_FREQUENCY = 1
CMD_SET_SAMPLE_RATE = 2
# Samples arriving during PLL settling after a change are discarded.
SETTLE_SECONDS = 0.4
# Bounded relay buffers: TCP backpressure handles a slow or starting Gqrx.
MAX_TO_CLIENT_BYTES = 1048576
MAX_TO_SOURCE_BYTES = 65536
SUPPORTED_SAMPLE_RATES = (1024000, 1800000, 2048000, 2400000)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    def __str__(self):
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


def parse_endpoint(text: str) -> Endpoint:
    """Parse ``HOST:PORT`` or ``[IPv6]:PORT``."""
    host, separator, port = text.rpartition(":")
    if not separator or not host:
        raise ValueError(f"Expected HOST:PORT, got {text!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        number = int(port)
    except ValueError:
        raise ValueError(f"Invalid port in {text!r}") from None
    if not 1 <= number <= 65535:
        raise ValueError(f"Port out of range in {text!r}")
    return Endpoint(host, number)


@dataclass(frozen=True)
class Frame:
    sequence: int
    epoch: int
    center: int
    sample_rate: int
    received_at: float
    raw: bytes
    receiver_controls: tuple = ()


class Capture:
    """Bounded latest-frame mailbox, invalidated after receiver changes."""

    def __init__(self, center, sample_rate, *, frame_bytes=FRAME_BYTES, clock=time.monotonic):
        self.lock = threading.Lock()
        self.center = center
        self.sample_rate = sample_rate
        self.controls = {CMD_SET_FREQUENCY: center, CMD_SET_SAMPLE_RATE: sample_rate}
        self.frame_bytes = frame_bytes
        self.clock = clock
        self.epoch = 0
        self.sequence = 0
        self.frame = None
        self.buffer = bytearray()
        self.valid_after = 0.0
        self.connected = False

    def reset(self, connected):
        with self.lock:
            self.connected = connected
            self._invalidate()

    def _invalidate(self):
        """Caller holds the lock; discard queued samples during PLL settling."""
        self.epoch += 1
        self.frame = None
        tail = self.buffer[-1:] if len(self.buffer) % 2 else b""
        self.buffer[:] = tail
        self.valid_after = self.clock() + SETTLE_SECONDS

    def commands(self, data):
        """Observe complete 5-byte rtl_tcp commands forwarded to the receiver."""
        with self.lock:
            for index in range(0, len(data), 5):
                command, value = struct.unpack("!BI", data[index:index + 5])
                if self.controls.get(command) == value:
                    continue
                self.controls[command] = value
                if command == CMD_SET_FREQUENCY:
                    self.center = value
                elif command == CMD_SET_SAMPLE_RATE:
                    self.sample_rate = value
                self._invalidate()

    def feed(self, data, now):
        with self.lock:
            if now < self.valid_after:
                # Discard only complete I/Q pairs; preserve byte alignment if
                # TCP happens to split an I/Q sample between two receives.
                self.buffer.extend(data)
                if len(self.buffer) >= 2:
                    del self.buffer[:len(self.buffer) // 2 * 2]
                return
            self.buffer.extend(data)
            while len(self.buffer) >= self.frame_bytes:
                raw = bytes(self.buffer[:self.frame_bytes])
                del self.buffer[:self.frame_bytes]
                self.sequence += 1
                self.frame = Frame(self.sequence, self.epoch, self.center, self.sample_rate, now, raw,
                                   tuple(sorted((key, value) for key, value in self.controls.items()
                                                if key not in (CMD_SET_FREQUENCY, CMD_SET_SAMPLE_RATE))))

    def snapshot(self):
        with self.lock:
            return self.frame, self.epoch, self.connected


def receive_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise ConnectionError("rtl_tcp closed before sending its header")
        result.extend(chunk)
    return bytes(result)


class Bridge(threading.Thread):
    """One local Gqrx client at a time; relay I/Q unchanged and observe its commands."""

    def __init__(self, capture: Capture, listen: Endpoint, source: Endpoint):
        super().__init__(daemon=True, name="radiolack-bridge")
        if not listen.is_loopback:
            raise ReceiverError(f"The I/Q bridge only listens on a loopback address (got {listen})")
        self.capture = capture
        self.listen = listen
        self.source = source
        self.stop_event = threading.Event()
        self.failure = None
        family = socket.AF_INET6 if ":" in listen.host else socket.AF_INET
        self.server = socket.socket(family, socket.SOCK_STREAM)
        try:
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((listen.host, listen.port))
            self.server.listen(1)
        except OSError as error:
            self.server.close()
            raise ReceiverError(f"Cannot listen on {listen} for Gqrx: {error.strerror or error}. "
                                f"Another bridge or program may already use this port.") from None

    def relay(self, client):
        try:
            upstream = socket.create_connection((self.source.host, self.source.port), timeout=3)
        except OSError as error:
            raise ReceiverError(f"Cannot reach rtl_tcp at {self.source}: {error.strerror or error}") from None
        with upstream as source:
            client.settimeout(2)
            header = receive_exact(source, 12)
            if header[:4] != b"RTL0":
                raise ReceiverError(f"{self.source} is not an rtl_tcp I/Q source")
            client.sendall(header)
            client.setblocking(False)
            source.setblocking(False)
            self.capture.reset(True)
            commands = bytearray()
            to_client = bytearray()
            to_source = bytearray()
            while not self.stop_event.is_set():
                # Gqrx opens its source before starting DSP. Bounded buffers and
                # TCP backpressure tolerate that pause without timing out,
                # dropping I/Q bytes, or allocating an unbounded sample queue.
                readers = [client] if len(to_source) < MAX_TO_SOURCE_BYTES else []
                if len(to_client) < MAX_TO_CLIENT_BYTES:
                    readers.append(source)
                writers = ([client] if to_client else []) + ([source] if to_source else [])
                readable, writable, _ = select.select(readers, writers, [], 0.2)
                if client in writable:
                    sent = client.send(to_client)
                    if not sent:
                        return
                    del to_client[:sent]
                if source in writable:
                    sent = source.send(to_source)
                    if not sent:
                        raise ConnectionError("rtl_tcp command connection closed")
                    del to_source[:sent]
                if client in readable:
                    data = client.recv(4096)
                    if not data:
                        return
                    commands.extend(data)
                    length = len(commands) // 5 * 5
                    if length:
                        batch = bytes(commands[:length])
                        del commands[:length]
                        to_source.extend(batch)
                        self.capture.commands(batch)
                if source in readable:
                    data = source.recv(65536)
                    if not data:
                        raise ConnectionError("rtl_tcp stopped sending samples")
                    to_client.extend(data)
                    self.capture.feed(data, time.monotonic())

    def run(self):
        try:
            while not self.stop_event.is_set():
                if not select.select([self.server], [], [], 0.2)[0]:
                    continue
                client, _ = self.server.accept()
                try:
                    with client:
                        self.relay(client)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Gqrx may reconnect when its input configuration changes.
                finally:
                    self.capture.reset(False)
        except Exception as error:  # Surface any relay failure to the monitor thread.
            self.failure = error

    def close(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.server.close()


def port_in_use(endpoint: Endpoint) -> bool:
    """True if something already listens on the endpoint (binding test, no connection)."""
    family = socket.AF_INET6 if ":" in endpoint.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        # SO_REUSEADDR ignores TIME_WAIT leftovers but still detects listeners.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((endpoint.host, endpoint.port))
        except OSError:
            return True
    return False


_LOG_HINTS = (
    ("No supported devices found", "no RTL-SDR device was found (unplugged?)"),
    ("usb_claim_interface error", "the RTL-SDR is in use by another program "
                                  "(for example Gqrx with a direct USB input)"),
    ("Failed to open rtlsdr device", "the RTL-SDR could not be opened"),
    ("Address already in use", "the rtl_tcp port is already in use"),
)


class RtlTcpProcess:
    """rtl_tcp child owned by this command; only this process is ever stopped."""

    def __init__(self, endpoint: Endpoint, *, center_hz, sample_rate, gain_db, device_index,
                 log_path: Path, executable="rtl_tcp", startup_wait_s=0.5):
        if not endpoint.is_loopback:
            raise ReceiverError(f"A managed rtl_tcp only listens on a loopback address (got {endpoint}); "
                                "use --external-source for a remote rtl_tcp")
        self.endpoint = endpoint
        self.arguments = ["-a", endpoint.host, "-p", str(endpoint.port), "-f", str(center_hz),
                          "-s", str(sample_rate), "-g", str(gain_db), "-d", str(device_index),
                          "-n", "10"]
        self.log_path = Path(log_path)
        self.executable = executable
        self.startup_wait_s = startup_wait_s
        self.process = None
        self.log = None
        self.log_start = 0

    def start(self):
        program = shutil.which(self.executable)
        if program is None:
            raise ReceiverError("rtl_tcp is not installed (Debian: sudo apt install rtl-sdr)")
        if port_in_use(self.endpoint):
            raise ReceiverError(f"Port {self.endpoint} is already in use (another rtl_tcp?). "
                                "Stop it, choose another --rtl-tcp port, or reuse it with --external-source.")
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = self.log_path.open("a", encoding="utf-8")
            self.log_start = self.log.tell()
        except OSError as error:
            raise StoreWriteError(f"Cannot write the rtl_tcp log {self.log_path}: "
                                  f"{error.strerror or error}") from None
        self.process = subprocess.Popen([program, *self.arguments], stdout=self.log,
                                        stderr=subprocess.STDOUT)
        time.sleep(self.startup_wait_s)
        self.check()

    def hint(self) -> str:
        try:
            with self.log_path.open("rb") as stream:
                stream.seek(self.log_start)
                text = stream.read(65536).decode("utf-8", "replace")
        except OSError:
            return "see the rtl_tcp log"
        for marker, meaning in _LOG_HINTS:
            if marker in text:
                return meaning
        return "see the rtl_tcp log"

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise ReceiverError(f"rtl_tcp exited with code {self.process.returncode}: {self.hint()}. "
                                f"Log: {self.log_path}")

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None
