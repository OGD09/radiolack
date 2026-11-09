"""rtl_tcp sources: RTL-SDR receivers served by rtl_tcp.

rtl_tcp (part of librtlsdr) serves one receiver to one client at a time over
TCP: a 12-byte header, then interleaved unsigned 8-bit I/Q samples, while the
client sends 5-byte commands. It has no access control, so it belongs on the
loopback interface or a trusted network. ``RtlTcpSource`` reads an rtl_tcp
that the user runs; ``RtlSdrSource`` runs rtl_tcp itself, on the loopback
interface, for a receiver plugged into this computer.

The source connects when the engine first reads, and sends all its settings
at each connection, since rtl_tcp keeps those of its previous client. It reads
everything rtl_tcp sends and keeps only half a second of samples, dropping
the oldest when the engine falls behind, so that the display stays current:
rtl_tcp itself queues up to about 130 MB for a client that stops reading. For
the same reason it hangs up once the engine stops reading, and connects again
at the next read. A refused, silent or lost connection is a waiting state,
retried with a growing delay, never an error.

After a change of centre frequency, sample rate or frequency correction,
samples of the previous setting still arrive for a moment: the source drops
what arrives during the next 0.4 s, then labels the samples with the new
setting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import shutil
import signal
import socket
import struct
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

DEFAULT_CENTRE_HZ = 145_000_000.0      # the 2 m band, until the user tunes elsewhere
DEFAULT_SAMPLE_RATE_HZ = 2_400_000.0
# librtlsdr's upper range of sample rates; its lower one, 225 to 300 kHz, is
# not offered.
SAMPLE_RATES_HZ = (900_001.0, 3_200_000.0)
PPM_RANGE = (-100, 100)     # frequency corrections offered: whole ppm, as librtlsdr takes them
SETTLE_S = 0.4          # samples dropped after a change of frequency, rate or correction
BUFFER_S = 0.5          # samples kept for a reader that falls behind
IDLE_S = 2.0            # hang up once nobody has read for this long
READ_WAIT_S = 0.5       # a read without samples gives up after this
TIMEOUT_S = 5.0         # limit for connecting, for the header and for silences
RETRY_S = (0.5, 5.0)    # first and longest delay before connecting again
RTL_TCP_PROGRAM = "rtl_tcp"     # what RtlSdrSource runs, found on the PATH
STOP_S = 3.0            # rtl_tcp stops within about a second; killed after this

_CHECK_S = 0.25         # how often a connection checks its time limits
_START_CHECK_S = 0.1    # how often RtlSdrSource tries to connect while rtl_tcp starts
_OUTPUT_CHARS = 4096    # end of rtl_tcp's messages kept, to tell why it stopped
# Buffers of 256 KiB that a started rtl_tcp queues at most for a reader that
# falls behind, dropping the oldest: about half a second at 2.4 MS/s.
_QUEUED_BUFFERS = 10
_LOOPBACK = "127.0.0.1"
_HEADER = struct.Struct(">4sII")    # b"RTL0", tuner type, number of gains
_COMMAND = struct.Struct(">BI")     # command, value
_MAGIC = b"RTL0"
_SET_FREQUENCY = 0x01   # Hz
_SET_SAMPLE_RATE = 0x02  # Hz
_SET_GAIN_MODE = 0x03   # 0: automatic, 1: manual
_SET_GAIN = 0x04        # tenths of a dB
_SET_FREQUENCY_CORRECTION = 0x05    # ppm
_SETTINGS = ("centre_hz", "sample_rate_hz", "gain_db", "ppm")
# Full-scale value of each unsigned 8-bit sample.
_LEVELS = (np.arange(256, dtype=np.float32) - np.float32(127.5)) / np.float32(127.5)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tuner:
    """A tuner, with what librtlsdr lets it do."""

    name: str
    range_hz: tuple[float, float]   # centre frequencies it tunes
    gains_db: tuple[float, ...]     # manual gains, lowest first; none: automatic gain only


_R82XX_GAINS_DB = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4,
                   28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6)
# By the tuner type rtl_tcp announces, with the gains librtlsdr lists.
TUNERS = {
    1: Tuner("Elonics E4000", (52e6, 2200e6),
             (-1.0, 1.5, 4.0, 6.5, 9.0, 11.5, 14.0, 16.5, 19.0, 21.5, 24.0, 29.0, 34.0, 42.0)),
    2: Tuner("Fitipower FC0012", (22e6, 948.6e6), (-9.9, -4.0, 7.1, 17.9, 19.2)),
    3: Tuner("Fitipower FC0013", (22e6, 1100e6),
             (-9.9, -7.3, -6.5, -6.3, -6.0, -5.8, -5.4, 5.8, 6.1, 6.3, 6.5, 6.7, 6.8, 7.0, 7.1,
              17.9, 18.1, 18.2, 18.4, 18.6, 18.8, 19.1, 19.7)),
    4: Tuner("FCI FC2580", (146e6, 924e6), ()),
    5: Tuner("Rafael Micro R820T", (24e6, 1766e6), _R82XX_GAINS_DB),
    6: Tuner("Rafael Micro R828D", (24e6, 1766e6), _R82XX_GAINS_DB),
}
# A command carries the frequency in 32 bits: the only limits for an unknown tuner.
_ANY_FREQUENCY_HZ = (1.0, float(0xFFFF_FFFF))


def tuner_for(tuner_type: int, gain_count: int) -> Tuner:
    """The tuner an rtl_tcp header announces. Its gains apply only when the
    header counts as many: another librtlsdr may offer other ones."""
    known = TUNERS.get(tuner_type)
    if known is None:
        return Tuner(f"Unknown tuner (type {tuner_type})", _ANY_FREQUENCY_HZ, ())
    if len(known.gains_db) != gain_count:
        return replace(known, gains_db=())
    return known


@dataclass(frozen=True)
class Settings:
    """What the receiver is asked to do."""

    centre_hz: float
    sample_rate_hz: float
    gain_db: float | None           # None: the tuner's automatic gain
    ppm: int                        # frequency correction, in parts per million


class RtlTcpSource:
    """An RTL-SDR receiver served by rtl_tcp, read like any source.

    ``centre_hz`` and ``sample_rate_hz`` describe the samples ``read`` last
    returned. ``describe`` gives the settings asked of the receiver and the
    connection state, and ``on_change`` is called whenever they change.
    """

    kind = "rtl_tcp"

    def __init__(self, host: str, port: int, *, centre_hz: float = DEFAULT_CENTRE_HZ,
                 sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ, gain_db: float | None = None,
                 ppm: int = 0, settle_s: float = SETTLE_S, buffer_s: float = BUFFER_S, idle_s: float = IDLE_S,
                 read_wait_s: float = READ_WAIT_S, timeout_s: float = TIMEOUT_S,
                 retry_s: tuple[float, float] = RETRY_S) -> None:
        self.host = host
        self.port = port
        self._settle_s = settle_s
        self._buffer_s = buffer_s
        self._idle_s = idle_s
        self._read_wait_s = read_wait_s
        self._timeout_s = timeout_s
        self._retry_s = retry_s
        start = Settings(DEFAULT_CENTRE_HZ, DEFAULT_SAMPLE_RATE_HZ, None, 0)
        self._settings = _changed(start, {"centre_hz": centre_hz, "sample_rate_hz": sample_rate_hz,
                                          "gain_db": gain_db, "ppm": ppm}, None)
        self.centre_hz = self._settings.centre_hz
        self.sample_rate_hz = self._settings.sample_rate_hz
        self.on_change: Callable[[], None] | None = None
        self.status = "idle"        # "connecting", "streaming", "waiting" or "idle"
        self.detail = ""            # why the source waits
        self.tuner: Tuner | None = None         # announced by the latest connection
        self._connection: _Connection | None = None
        self._task: asyncio.Task | None = None
        self._buffer = bytearray()  # bytes of the current setting, starting with an I byte
        self._wanted = 0            # bytes the latest read needs
        self._arrived = asyncio.Event()
        self._settle_until = 0.0
        self._last_read = -math.inf     # when the latest read ended
        self._reading = False
        self._closed = False

    @property
    def address(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def settings_key(self) -> str:
        """The engine saves settings per rtl_tcp address."""
        return f"{self.kind} {self.address}"

    async def read(self, count: int) -> np.ndarray | None:
        """The next ``count`` samples, or ``None`` when they did not arrive
        within a short wait: the source is connecting, or waiting to retry."""
        if count < 1:
            raise ValueError(f"cannot read {count} samples")
        if self._closed:
            return None
        size = 2 * count
        self._wanted = size
        self._reading = True
        try:
            if self._task is None or self._task.done():
                self._task = asyncio.ensure_future(self._run())
            deadline = time.monotonic() + self._read_wait_s
            while len(self._buffer) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    return None
                self._arrived.clear()
                try:
                    await asyncio.wait_for(self._arrived.wait(), remaining)
                except asyncio.TimeoutError:
                    return None
        finally:
            self._reading = False
            self._last_read = time.monotonic()
        data = self._buffer[:size]
        del self._buffer[:size]
        # The buffer only ever holds samples of the current setting.
        self.centre_hz = self._settings.centre_hz
        self.sample_rate_hz = self._settings.sample_rate_hz
        return _LEVELS[np.frombuffer(data, np.uint8)].view(np.complex64)

    def describe(self) -> dict[str, Any]:
        """The receiver's settings and the connection state, as JSON values."""
        settings, tuner = self._settings, self.tuner
        return {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "address": self.address,
            "tuner": tuner.name if tuner else None,
            "centre_hz": settings.centre_hz,
            "sample_rate_hz": settings.sample_rate_hz,
            "gain_db": settings.gain_db,
            "ppm": settings.ppm,
            "gains_db": list(tuner.gains_db) if tuner else [],
            "range_hz": list(tuner.range_hz if tuner else _ANY_FREQUENCY_HZ),
            "settable": list(_SETTINGS),
        }

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change ``centre_hz``, ``sample_rate_hz``, ``gain_db`` (``None``
        for automatic gain) and ``ppm``, then call ``on_change``.

        Raises ValueError, changing nothing, when a setting is unknown or
        invalid. A gain becomes the nearest the tuner offers. Changes made
        while disconnected apply at the next connection.
        """
        old = self._settings
        new = _changed(old, changes, self.tuner)
        self._settings = new
        retuned = (new.centre_hz, new.sample_rate_hz, new.ppm) != (old.centre_hz, old.sample_rate_hz,
                                                                   old.ppm)
        connection = self._connection
        if connection is not None and connection.tuner is not None:
            if new.gain_db != old.gain_db:
                _send_gain(connection, new.gain_db)
            if new.ppm != old.ppm:
                connection.send(_SET_FREQUENCY_CORRECTION, new.ppm)
            if new.sample_rate_hz != old.sample_rate_hz:
                connection.send(_SET_SAMPLE_RATE, int(new.sample_rate_hz))
            if retuned:
                # Also after a change of rate or correction, so that the
                # tuner applies it whatever the librtlsdr version.
                connection.send(_SET_FREQUENCY, int(new.centre_hz))
        if retuned:
            self._new_setting()
        self._notify()

    def close(self) -> None:
        """Hang up and stop for good. Safe to repeat: a repeat changes
        nothing, and lets a stop in progress finish."""
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close("closed")
        if self._task is not None:
            self._task.cancel()
        self._arrived.set()

    async def wait_closed(self) -> None:
        """Return once ``close`` has taken effect: the source has hung up,
        and an RtlSdrSource has stopped its rtl_tcp."""
        if self._task is not None:
            await asyncio.wait({self._task})

    # ------------------------------------------------------------ connection

    async def _run(self) -> None:
        """Stay connected while the engine reads, retrying after failures."""
        delay = self._retry_s[0]
        try:
            while not self._closed and not self._idle():
                try:
                    await self._session()
                except _Waiting as waiting:
                    if waiting.streamed:
                        delay = self._retry_s[0]
                    self._set_status("waiting", waiting.detail)
                    await asyncio.sleep(delay)
                    delay = min(2 * delay, self._retry_s[1])
        except Exception:
            log.exception("the rtl_tcp connection stopped")
        finally:
            if not self._closed:
                self._set_status("idle", "")

    async def _session(self) -> None:
        """Connect and stream until nobody reads; raises _Waiting when the
        connection fails or ends otherwise."""
        if self.status == "idle":
            self._set_status("connecting", "")
        connection = self._connection = await self._connect()
        try:
            while not connection.ended.done():
                await asyncio.wait({connection.ended}, timeout=_CHECK_S)
                if self._closed or self._idle():
                    return
                if time.monotonic() - connection.last_data >= self._timeout_s:
                    connection.close("no samples" if connection.tuner
                                     else "no answer (rtl_tcp may be serving another client)")
            raise _Waiting(connection.ended.result(), streamed=connection.tuner is not None)
        finally:
            connection.close("closed")
            self._connection = None
            self._buffer.clear()    # by the next connection, these samples are old

    async def _connect(self) -> _Connection:
        """A new connection to rtl_tcp; raises _Waiting when none opens."""
        loop = asyncio.get_running_loop()
        try:
            _, connection = await asyncio.wait_for(
                loop.create_connection(lambda: _Connection(self._header_received, self._store),
                                       self.host, self.port),
                self._timeout_s)
        except asyncio.TimeoutError:
            raise _Waiting("no answer") from None
        except (OSError, ValueError) as error:     # ValueError: a host name that cannot be encoded
            raise _Waiting(_reason(error)) from None
        return connection

    def _idle(self) -> bool:
        """Nobody has read for a while: the engine has no client."""
        return not self._reading and time.monotonic() - self._last_read >= self._idle_s

    def _header_received(self, connection: _Connection) -> None:
        """rtl_tcp answered: fit the settings to its tuner and send them all."""
        assert connection.tuner is not None
        self.tuner = connection.tuner
        settings = self._settings = _fitted(self._settings, connection.tuner)
        _send_gain(connection, settings.gain_db)
        connection.send(_SET_FREQUENCY_CORRECTION, settings.ppm)
        connection.send(_SET_SAMPLE_RATE, int(settings.sample_rate_hz))
        connection.send(_SET_FREQUENCY, int(settings.centre_hz))
        self._new_setting()
        self._set_status("streaming", "")

    def _store(self, offset: int, data: bytes) -> None:
        """Keep I/Q bytes that start ``offset`` bytes into rtl_tcp's stream."""
        if time.monotonic() < self._settle_until:
            return
        if not self._buffer and offset % 2:
            data = data[1:]         # a Q byte: start with the next pair
        self._buffer += data
        capacity = max(2 * round(self._buffer_s * self._settings.sample_rate_hz), self._wanted)
        excess = len(self._buffer) - capacity
        if excess > 0:
            del self._buffer[:excess + excess % 2]
        if len(self._buffer) >= self._wanted:
            self._arrived.set()

    def _new_setting(self) -> None:
        """Drop the samples of the previous setting, with those still on their way."""
        self._buffer.clear()
        self._settle_until = time.monotonic() + self._settle_s

    def _set_status(self, status: str, detail: str) -> None:
        if (status, detail) != (self.status, self.detail):
            self.status, self.detail = status, detail
            self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()


class RtlSdrSource(RtlTcpSource):
    """An RTL-SDR receiver on this computer, through an rtl_tcp that the
    source runs itself on the loopback interface.

    rtl_tcp holds the receiver for as long as it runs: the source starts it on
    a free port for each connection and stops it when the connection ends, so
    that the receiver stays free for other programs while no interface
    listens. When rtl_tcp cannot open the receiver, the source waits and tries
    again, with librtlsdr's reason as its detail. ``device`` is the receiver's
    index or serial number, as rtl_tcp's ``-d`` option takes it; ``program``
    runs rtl_tcp, and the source adds rtl_tcp's options. The other options
    are those of RtlTcpSource.
    """

    kind = "rtl_sdr"

    def __init__(self, device: str = "0", *, program: Sequence[str] = (RTL_TCP_PROGRAM,),
                 stop_s: float = STOP_S, **options: Any) -> None:
        if not device:
            raise ValueError("no receiver given")
        if not program:
            raise ValueError("no program given to run rtl_tcp")
        super().__init__(_LOOPBACK, 0, **options)
        self.device = device
        self._program = tuple(program)
        self._stop_s = stop_s
        self._child: _Child | None = None      # the rtl_tcp of the current connection

    @property
    def settings_key(self) -> str:
        """The engine saves settings per receiver, as given: rtl_tcp's port
        changes at each start."""
        return f"{self.kind} {self.device}"

    def describe(self) -> dict[str, Any]:
        # rtl_tcp listens on another port at each start: of no use to an interface.
        return {**super().describe(), "address": None}

    def close(self) -> None:
        if self._child is not None and not self._closed:
            self._child.terminate()
        super().close()

    async def _session(self) -> None:
        try:
            await super()._session()
        finally:
            child, self._child = self._child, None
            if child is not None:
                await child.stop(self._stop_s)

    async def _connect(self) -> _Connection:
        """Start rtl_tcp, then connect as soon as it listens."""
        try:
            self.port = _free_port(self.host)
        except OSError as error:
            raise _Waiting(f"no free port: {_error_text(error)}") from None
        child = self._child = await _Child.start(self._command())
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                return await super()._connect()
            except _Waiting:
                if child.exited.done():
                    raise _Waiting(await child.reason(self.device)) from None
                if time.monotonic() >= deadline:
                    raise _Waiting("rtl_tcp did not start") from None
            await asyncio.wait({child.exited}, timeout=_START_CHECK_S)

    def _command(self) -> list[str]:
        """The command line that runs rtl_tcp for this receiver."""
        program = shutil.which(self._program[0])
        if program is None:
            raise _Waiting("rtl_tcp is not installed")
        command = [program, *self._program[1:], "-a", self.host, "-p", str(self.port),
                   "-n", str(_QUEUED_BUFFERS), "-d", self.device]
        setpriv = shutil.which("setpriv") if sys.platform.startswith("linux") else None
        if setpriv is not None:
            # The kernel stops rtl_tcp if Radiolack ends without stopping it,
            # as when killed: the receiver does not stay busy.
            command = [setpriv, "--pdeathsig", "TERM", "--", *command]
        return command


class _Waiting(Exception):
    """The connection failed or ended: the source waits, then tries again."""

    def __init__(self, detail: str, *, streamed: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.streamed = streamed    # rtl_tcp had answered: retry soon


class _Connection(asyncio.Protocol):
    """One connection to rtl_tcp: reads its header, then hands on the samples."""

    def __init__(self, on_header: Callable[[_Connection], None],
                 on_samples: Callable[[int, bytes], None]) -> None:
        self._on_header = on_header
        self._on_samples = on_samples
        self._header = bytearray()
        self._offset = 0            # I/Q bytes received so far
        self.transport: asyncio.BaseTransport | None = None
        self.tuner: Tuner | None = None         # known once the header arrived
        self.last_data = time.monotonic()
        self.ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        if self.ended.done():
            return
        self.last_data = time.monotonic()
        if self.tuner is None:
            self._header += data
            if len(self._header) < _HEADER.size:
                return
            magic, tuner_type, gain_count = _HEADER.unpack_from(self._header)
            if magic != _MAGIC:
                self.close("not an rtl_tcp server")
                return
            data = bytes(self._header[_HEADER.size:])
            self._header.clear()
            self.tuner = tuner_for(tuner_type, gain_count)
            self._on_header(self)
        if data:
            self._on_samples(self._offset, data)
            self._offset += len(data)

    def connection_lost(self, error: Exception | None) -> None:
        self.close("connection lost")

    def send(self, command: int, value: int) -> None:
        """Send a command; a negative value goes in two's complement."""
        if not self.ended.done() and isinstance(self.transport, asyncio.WriteTransport):
            self.transport.write(_COMMAND.pack(command, value & 0xFFFF_FFFF))

    def close(self, detail: str) -> None:
        """Hang up; ``detail`` says why, unless the connection had already ended."""
        if not self.ended.done():
            self.ended.set_result(detail)
        if self.transport is not None:
            self.transport.close()


class _Child:
    """One run of rtl_tcp by RtlSdrSource, with the end of its messages."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._output = ""       # the end of what rtl_tcp wrote on stderr
        self._reader = asyncio.ensure_future(self._read())
        self.exited: asyncio.Future[int] = asyncio.ensure_future(process.wait())

    @classmethod
    async def start(cls, command: list[str]) -> _Child:
        """Run ``command``; raises _Waiting when it cannot run."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except OSError as error:
            raise _Waiting(f"cannot run rtl_tcp: {_error_text(error)}") from None
        return cls(process)

    def terminate(self) -> None:
        """Ask rtl_tcp to stop, which it does within about a second."""
        if not self.exited.done():
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()

    async def stop(self, timeout_s: float) -> None:
        """Stop rtl_tcp, killing it if it has not stopped after ``timeout_s``."""
        self.terminate()
        _, running = await asyncio.wait({self.exited}, timeout=timeout_s)
        if running:
            log.warning("rtl_tcp did not stop within %g s: killing it", timeout_s)
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            _, running = await asyncio.wait({self.exited}, timeout=timeout_s)
            if running:
                log.warning("rtl_tcp (process %d) is still running", self._process.pid)
        await self._read_to_end()

    async def reason(self, device: str) -> str:
        """Why rtl_tcp stopped by itself, in a few words."""
        await asyncio.wait({self.exited})
        await self._read_to_end()
        known = _known_reason(self._output, device)
        if known is not None:
            return known
        text = _exit_text(self.exited.result())
        last_lines = " | ".join(self._output.strip().splitlines()[-3:])
        log.warning("%s: %s", text, last_lines or "no message")
        return text

    async def _read(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        with contextlib.suppress(OSError):
            while chunk := await stream.read(4096):
                self._output = (self._output + chunk.decode("ascii", "replace"))[-_OUTPUT_CHARS:]

    async def _read_to_end(self) -> None:
        """Let the reader take rtl_tcp's last messages: the end of its stderr
        follows its exit."""
        _, reading = await asyncio.wait({self._reader}, timeout=1.0)
        for task in reading:
            task.cancel()


def _send_gain(connection: _Connection, gain_db: float | None) -> None:
    if gain_db is None:
        connection.send(_SET_GAIN_MODE, 0)
    else:
        connection.send(_SET_GAIN_MODE, 1)
        connection.send(_SET_GAIN, round(gain_db * 10))


def _changed(settings: Settings, changes: Mapping[str, object], tuner: Tuner | None) -> Settings:
    """``settings`` with ``changes``; raises ValueError for any invalid change.
    Without a known tuner, any frequency and any gain pass until it is known."""
    unknown = sorted(set(changes) - set(_SETTINGS))
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(unknown)}")
    if "centre_hz" in changes:
        centre = float(round(_number(changes["centre_hz"], "the centre frequency")))
        low, high = tuner.range_hz if tuner else _ANY_FREQUENCY_HZ
        if not low <= centre <= high:
            raise ValueError(f"the receiver tunes from {low / 1e6:g} to {high / 1e6:g} MHz")
        settings = replace(settings, centre_hz=centre)
    if "sample_rate_hz" in changes:
        rate = float(round(_number(changes["sample_rate_hz"], "the sample rate")))
        low, high = SAMPLE_RATES_HZ
        if not low <= rate <= high:
            raise ValueError(f"the sample rate must be from {low / 1e6:g} to {high / 1e6:g} MS/s")
        settings = replace(settings, sample_rate_hz=rate)
    if "gain_db" in changes:
        value = changes["gain_db"]
        gain = None if value is None else _number(value, "the gain")
        if gain is not None and tuner is not None:
            if not tuner.gains_db:
                raise ValueError("this tuner offers automatic gain only")
            gain = _nearest(tuner.gains_db, gain)
        settings = replace(settings, gain_db=gain)
    if "ppm" in changes:
        ppm = round(_number(changes["ppm"], "the frequency correction"))
        low, high = PPM_RANGE
        if not low <= ppm <= high:
            raise ValueError(f"the frequency correction must be from {low} to {high} ppm")
        settings = replace(settings, ppm=ppm)
    return settings


def _fitted(settings: Settings, tuner: Tuner) -> Settings:
    """``settings`` brought within what ``tuner`` can do."""
    low, high = tuner.range_hz
    gain = settings.gain_db
    if gain is not None:
        gain = _nearest(tuner.gains_db, gain) if tuner.gains_db else None
    return replace(settings, centre_hz=min(max(settings.centre_hz, low), high), gain_db=gain)


def _nearest(gains_db: tuple[float, ...], gain_db: float) -> float:
    return min(gains_db, key=lambda gain: abs(gain - gain_db))


def _number(value: object, what: str) -> float:
    """``value`` as a finite float; ValueError for anything else, booleans included."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            pass
        else:
            if math.isfinite(number):
                return number
    raise ValueError(f"{what} must be a number")


def _reason(error: BaseException) -> str:
    """Why a connection could not open, in a few words for the interface."""
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    if isinstance(error, socket.gaierror):
        return "unknown host"
    if isinstance(error, OSError) and error.errno:
        return _error_text(error)
    return "cannot connect"


def _error_text(error: OSError) -> str:
    """The system's words for ``error``, starting in lower case."""
    if not error.errno:
        return "unknown error"
    text = os.strerror(error.errno)
    return text[:1].lower() + text[1:]


def _free_port(host: str) -> int:
    """A port nothing listens on now, for rtl_tcp. Should another program
    take it first, the source finds no rtl_tcp there, stops it and starts it
    again on another port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


# What rtl_tcp and librtlsdr write on stderr when rtl_tcp cannot start, most
# specific first, and what it means.
_STOP_REASONS = (
    ("No supported devices found", "no receiver found"),
    ("No matching devices found", "receiver {device} not found"),
    ("Please fix the device permissions", "no permission to open the receiver"),
    ("Kernel driver is active", "the DVB-T driver holds the receiver"),
    ("Detaching kernel driver failed", "the DVB-T driver holds the receiver"),
    ("usb_claim_interface error", "the receiver is in use by another program"),
    ("Failed to open rtlsdr device", "the receiver cannot be opened"),
)


def _known_reason(output: str, device: str) -> str | None:
    """Why rtl_tcp stopped, when its messages tell."""
    for message, reason in _STOP_REASONS:
        if message in output:
            return reason.format(device=device)
    return None


def _exit_text(code: int) -> str:
    """How rtl_tcp stopped, from its exit code."""
    if code < 0:
        with contextlib.suppress(ValueError):
            return f"rtl_tcp stopped ({signal.Signals(-code).name})"
    return f"rtl_tcp stopped (exit code {code})"
