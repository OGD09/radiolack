"""Test doubles shared by the engine, server and source tests, and a wide
signal for the tests of the noise estimates."""

import asyncio
import itertools
import math
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import radiolack
from radiolack.engine import FixedSource
from radiolack.updates import UpdateResult

FAKE_CENTRE_HZ = 145_000_000.0
FAKE_RATE_HZ = 245_760.0     # 8192 samples per frame at 30 frames per second
FAKE_TONE_BIN = 1000         # with 8192-point FFTs
LOOPBACK = "127.0.0.1"
# Runs tests/fake_rtl_tcp.py, the stand-in for the rtl_tcp program.
FAKE_RTL_TCP = (sys.executable, str(Path(__file__).with_name("fake_rtl_tcp.py")))


class FakeSource(FixedSource):
    """A fast source: one tone, delivered every few milliseconds, after
    ``empty_reads`` reads that find no samples, plus ``dc_offset``, a
    constant that shows on the centre frequency."""

    kind = "test"
    centre_hz = FAKE_CENTRE_HZ
    sample_rate_hz = FAKE_RATE_HZ

    def __init__(self, fail_after: int | None = None, empty_reads: int = 0,
                 dc_offset: complex = 0) -> None:
        self.reads = 0
        self.closed = False
        self._fail_after = fail_after
        self._empty_reads = empty_reads
        self._dc_offset = dc_offset

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.005)
        if self._empty_reads:
            self._empty_reads -= 1
            return None
        if self._fail_after is not None and self.reads >= self._fail_after:
            raise RuntimeError("the fake receiver went away")
        self.reads += 1
        offset_hz = (FAKE_TONE_BIN - 4096) * FAKE_RATE_HZ / 8192
        t = np.arange(count) / FAKE_RATE_HZ
        return (0.1 * np.exp(2j * np.pi * offset_hz * t) + self._dc_offset).astype(np.complex64)

    def close(self) -> None:
        self.closed = True


def wide_signal(count: int, rate_hz: float, offset_hz: float, density: float,
                rng: np.random.Generator) -> np.ndarray:
    """``count`` samples of a signal ``offset_hz`` from the centre frequency
    that fills the 150 kHz around it, as broadcast FM fills its channel:
    white noise of ``density``, full-scale power per hertz, there."""
    spectrum = np.fft.fft(rng.standard_normal(count) + 1j * rng.standard_normal(count))
    spectrum[np.abs(np.fft.fftfreq(count, 1 / rate_hz) - offset_hz) > 75_000.0] = 0
    return (np.fft.ifft(spectrum) * math.sqrt(density * rate_hz / 2)).astype(np.complex64)


class FakeUpdates:
    """Stands in for updates.UpdateCheck, without apt or a network: each
    check counts in ``checks``, waits until ``release`` is set (it is at
    first), notes in ``cancelled`` whether it was cancelled, then gives
    ``result``, or raises it when it is an exception. ``reason`` is why
    checks are unavailable, or None."""

    def __init__(self, result: UpdateResult | Exception = UpdateResult("current"),
                 reason: str | None = None) -> None:
        self.result = result
        self.reason = reason
        self.checks = 0
        self.cancelled = False
        self.release = asyncio.Event()
        self.release.set()

    def unavailable_reason(self) -> str | None:
        return self.reason

    async def check(self) -> UpdateResult:
        self.checks += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def free_port() -> int:
    """A port nothing listens on: bound by the system, then released."""
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def fake_codes(frequency_hz: float, rate_hz: float) -> tuple[int, int]:
    """The I and Q bytes FakeRtlTcp sends for a setting: they tell which
    setting a sample was taken with."""
    return int(frequency_hz // 1_000_000) % 256, int(rate_hz // 100_000) % 256


def sample_codes(samples: np.ndarray) -> tuple[set[int], set[int]]:
    """The I bytes and the Q bytes that ``samples`` were read from."""
    codes = np.rint(samples.view(np.float32) * 127.5 + 127.5).astype(int)
    return set(codes[0::2].tolist()), set(codes[1::2].tolist())


class FakeRtlTcp:
    """An rtl_tcp stand-in on an ephemeral loopback port.

    It sends its 12-byte header in two parts, the second one together with
    the first samples, then a chunk of samples every few milliseconds. The
    first chunk has an odd size and the others an even one, so that every
    later chunk starts with a Q byte. Each I/Q pair is ``fake_codes`` of the
    current setting; with ``carriers``, a frequency in Hz for each carrier's
    amplitude in full-scale units, the samples are instead those carriers,
    the ones within the current setting's band, over white noise of
    ``noise`` (full-scale units, in I and in Q). It records the commands it
    receives, and applies a frequency or sample rate only after
    ``apply_delay_s``, as samples of the old setting still arrive for a while
    from a real receiver.
    """

    def __init__(self, *, port: int = 0, magic: bytes = b"RTL0", tuner_type: int = 5,
                 gain_count: int = 29, header: bool = True, chunk_bytes: int = 1000,
                 interval_s: float = 0.002, apply_delay_s: float = 0.05,
                 carriers: dict[float, float] | None = None, noise: float = 0.02) -> None:
        self.port = port
        self.magic = magic
        self.tuner_type = tuner_type
        self.gain_count = gain_count
        self.header = header            # False: never answer, like a busy rtl_tcp
        self.chunk_bytes = chunk_bytes
        self.interval_s = interval_s
        self.apply_delay_s = apply_delay_s
        self.carriers = carriers
        self.noise = noise
        self.sending = True             # False: send no samples
        self.frequency_hz = 100_000_000     # left by a previous client
        self.sample_rate_hz = 2_048_000
        self.commands: list[tuple[int, int]] = []
        self.connections = 0            # accepted so far
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self._rng = np.random.default_rng(7)
        self._sent = 0                  # carrier samples made so far, for their phases

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    async def __aenter__(self) -> "FakeRtlTcp":
        self._server = await asyncio.start_server(self._serve, LOOPBACK, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        self.drop()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    def drop(self) -> None:
        """Close every connection, as a crashed rtl_tcp would."""
        for writer in list(self._writers):
            writer.transport.abort()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        self._handlers.add(handler)
        self._writers.add(writer)
        self.connections += 1
        sender = asyncio.ensure_future(self._send(writer))
        loop = asyncio.get_running_loop()
        pending = b""
        try:
            while data := await reader.read(4096):
                pending += data
                while len(pending) >= 5:
                    command, value = struct.unpack_from(">BI", pending)
                    pending = pending[5:]
                    self.commands.append((command, value))
                    loop.call_later(self.apply_delay_s, self._apply, command, value)
        except ConnectionError:
            pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            self._writers.discard(writer)
            writer.close()
            self._handlers.discard(handler)

    def _apply(self, command: int, value: int) -> None:
        if command == 0x01:
            self.frequency_hz = value
        elif command == 0x02:
            self.sample_rate_hz = value

    async def _send(self, writer: asyncio.StreamWriter) -> None:
        if not self.header:
            return
        header = struct.pack(">4sII", self.magic, self.tuner_type, self.gain_count)
        writer.write(header[:5])
        await writer.drain()
        await asyncio.sleep(0.01)
        rest, offset = header[5:], 0
        stream = b""        # carrier samples made and not sent yet
        for size in itertools.chain([self.chunk_bytes + 1], itertools.repeat(self.chunk_bytes)):
            data = b""
            if self.sending and self.carriers is not None:
                if len(stream) < size:
                    stream += self._carrier_samples(size // 2 + 1)
                data, stream = stream[:size], stream[size:]
            elif self.sending:
                pair = bytes(fake_codes(self.frequency_hz, self.sample_rate_hz))
                start = offset % 2
                data = (pair * (size // 2 + 2))[start:start + size]
                offset += size
            if rest or data:
                writer.write(rest + data)
                rest = b""
                await writer.drain()
            await asyncio.sleep(self.interval_s)

    def _carrier_samples(self, count: int) -> bytes:
        """``count`` I/Q pairs of the carriers within the current setting's
        band over white noise, as rtl_tcp sends them: unsigned bytes."""
        rate = float(self.sample_rate_hz)
        t = (self._sent + np.arange(count)) / rate
        self._sent += count
        samples = self._rng.normal(0.0, self.noise, count) + 1j * self._rng.normal(0.0, self.noise, count)
        for frequency_hz, amplitude in self.carriers.items():
            offset_hz = frequency_hz - self.frequency_hz
            if abs(offset_hz) < rate / 2:
                samples += amplitude * np.exp(2j * np.pi * offset_hz * t)
        pairs = np.column_stack((samples.real, samples.imag))
        return np.clip(np.rint(pairs * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


class FakeRtlTcpRuns:
    """What the fake rtl_tcp program did during a test, from its log.

    It sets the environment that the program inherits for the rest of the
    test: where to write its log, in a temporary directory, and where to
    import the tests and Radiolack from.
    """

    def __init__(self, test: unittest.TestCase) -> None:
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "rtl_tcp.log"
        import_paths = (str(Path(radiolack.__file__).resolve().parents[1]),
                        str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH"))
        environment = mock.patch.dict(os.environ, {
            "FAKE_RTL_TCP_LOG": str(self.path),
            "PYTHONPATH": os.pathsep.join(filter(None, import_paths)),
        })
        environment.start()
        test.addCleanup(environment.stop)

    def started(self) -> list[tuple[int, str]]:
        """The process id and the arguments of each run, in order."""
        return [(pid, rest[len("started "):]) for pid, rest in self._events()
                if rest.startswith("started ")]

    def stopping(self) -> set[int]:
        """The runs that SIGTERM stopped."""
        return {pid for pid, rest in self._events() if rest == "stopping"}

    def _events(self) -> list[tuple[int, str]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [(int(pid), rest) for pid, _, rest in (line.partition(" ") for line in lines)]
