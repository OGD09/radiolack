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

The address of an ``RtlTcpSource`` is one of its settings: the way to the
receiver, which changes with the network the computers are on. A new address
hangs up and connects there, with the same settings.

After a change of centre frequency, sample rate or frequency correction,
samples of the previous setting still arrive for a moment: the source drops
what arrives during the next 0.4 s, then labels the samples with the new
setting.

The frequency correction takes hundredths of ppm (correction.py): rtl_tcp's
command 0x05 gives the receiver its nearest whole number, as librtlsdr
takes no other, and only when that changes, since the command retunes; the
source corrects the rest by scaling the frequency it asks for, after the
converter's conversion, on the tuner and in direct sampling alike. The
samples keep the frequency asked as their label.

An R820T or R828D tuner is placed on its grid (coverage-and-precision.md,
pull request 14a, and r82xx.py): its driver programs the tuner's PLL
exactly, or one constant off, only where the tuner's oscillator, the
frequency asked plus the intermediate frequency of the sample rate, is a
multiple of 112.5 kHz. On its tuner, the source asks for the frequency
nearest the one it would ask whose oscillator is such a multiple, 56.25 kHz
away at most (``r82xx.on_grid``), and labels its samples with the centre
in use: where that puts the receiver on the air, and, once the receiver's
``driver_pll`` setting names the driver's computation, where the tuner
truly sits by that computation (``r82xx.tuned_hz``). The driver's own
correction stays at 0 with such a tuner, since a corrected crystal moves
the grid: the source corrects the whole of it, by the frequency it asks
for, by the label, and by ``rate_scale``, the factor by which the receiver
then samples faster than its nominal rate, which the engine applies to the
VFO's distance from the centre. Direct sampling, a frequency below the
tuner's range, an R828D below 28.8 MHz, where an RTL-SDR Blog V4's driver
upconverts, and ``tuner_grid`` turned off, as a measure does, ask the
frequency as before, labelled by the computation where it applies.

Shortwave comes two ways, chosen per receiver by its ``hf_input``
setting. Direct sampling (``q`` or ``i``) makes the RTL2832 sample one of
its inputs at 28.8 MS/s without the tuner: the source switches to it by
itself for a centre frequency below the tuner's range, and back to the
tuner above it, as rtl_tcp's command 0x09 allows. The tuner sleeps
meanwhile: the gain is kept, and sent again with the tuner, which forgot
it. ``tuner`` is for a dongle whose tuner covers shortwave by itself, such
as an RTL-SDR Blog V4: the centre frequency simply goes lower. No tuner
says when it cannot reach a frequency: rtl_tcp checks no command's result.

Command 0x09 with 0, the tuner, starts the tuner again in librtlsdr,
whether direct sampling was on or not (``rtlsdr_set_direct_sampling``
calls the tuner's init): an R820T or R828D forgets its gain, back to
automatic, and the bandwidth and the intermediate frequency that the
sample rate gave it, back to DVB-T's widest filter and 3.57 MHz. Whenever
the source sends it, at each connection and on leaving direct sampling,
the frequency follows, then the sample rate, which gives the tuner the
bandwidth and the intermediate frequency of the rate again, then the
gain. The frequency goes first for the RTL-SDR Blog fork: there, 0x09
with 0 means automatic direct sampling below 24 MHz, so that it may go
back to direct sampling on the frequency it was left on, and the next
frequency on the tuner starts the tuner again.

A converter in front of the receiver (``converter`` and
``converter_lo_hz``, converter.py) moves the frequencies: the source's
centre frequency is the one on the air, and only the command it sends is
converted. The converter applies on top of the tuner and of direct sampling
alike. One that mirrors the spectrum has I and Q swapped as the samples
arrive, before the reader and the tap.

Antenna power (``bias_tee``, command 0x0e) puts 4.5 V on the antenna
socket of an RTL-SDR Blog V3 or V4. It starts off, is sent at each
connection, since rtl_tcp keeps it from its previous client, and is never
saved. A source on which it is on turns it off before it closes, and before
it leaves a server for another address; an RtlSdrSource gives its rtl_tcp a
moment to apply it before stopping it.

For a recording of the raw I/Q, the source also hands every sample that
arrives to a tap, as rtl_tcp sends it, labelled the same way: whether the
engine reads it or not, so that a recording loses nothing when the engine
falls behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
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

from .converter import DEFAULT_LO_HZ, HIGHEST_HZ, Conversion, checked_converter, checked_lo, mhz
from . import r82xx
from .correction import checked_ppm, corrected_hz, whole_ppm

DEFAULT_ADDRESS = ("127.0.0.1", 1234)  # where rtl_tcp listens unless told otherwise
DEFAULT_CENTRE_HZ = 145_000_000.0      # the 2 m band, until the user tunes elsewhere
DEFAULT_SAMPLE_RATE_HZ = 2_400_000.0
# librtlsdr's two ranges of sample rates, lowest first.
SAMPLE_RATES_HZ = ((225_001.0, 300_000.0), (900_001.0, 3_200_000.0))
SETTLE_S = 0.4          # samples dropped after a change of frequency, rate or correction
BUFFER_S = 0.5          # samples kept for a reader that falls behind
IDLE_S = 2.0            # hang up once nobody has read for this long
READ_WAIT_S = 0.5       # a read without samples gives up after this
TIMEOUT_S = 5.0         # limit for connecting, for the header and for silences
RETRY_S = (0.5, 5.0)    # first and longest delay before connecting again
RTL_TCP_PROGRAM = "rtl_tcp"     # what RtlSdrSource runs, found on the PATH
STOP_S = 3.0            # rtl_tcp stops within about a second; killed after this

_MAX_HOST = 253         # characters in the longest host name
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
_SET_FREQUENCY_CORRECTION = 0x05    # whole ppm
_SET_RTL_AGC = 0x08     # the RTL2832's own AGC: 0 off, 1 on
_SET_DIRECT_SAMPLING = 0x09     # 0: the tuner, 1: the I input, 2: the Q input
_SET_BIAS_TEE = 0x0e    # antenna power: 0 off, 1 on (rtl_tcp 0.5.4 and later)
POWER_OFF_S = 0.3       # an rtl_tcp that RtlSdrSource stops first applies the power's end
# What a receiver may use for shortwave (hf_input): nothing, direct sampling
# on the Q or the I input, or a tuner that covers it.
HF_INPUTS = ("none", "q", "i", "tuner")
_DIRECT_INPUTS = {"q": 2, "i": 1}   # the value of command 0x09 for each input
DIRECT_SAMPLING_HZ = 28_800_000.0   # the RTL2832's own rate: what direct sampling covers
# What a receiver knows of its driver's computation of an R820T's or R828D's
# PLL (r82xx.py): nothing, rtl-sdr up to 2.0.1 and the RTL-SDR Blog fork's,
# or rtl-sdr 2.0.2 and later's.
DRIVER_PLLS = ("unknown", "truncating", "rounding")
# In the order the engine restores them: the input, the converter and the
# rate before the centre frequency, whose reach depends on them; the
# converter's oscillator before the converter, which keeps the receiver
# where it is tuned.
_SETTINGS = ("hf_input", "converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db", "ppm",
             "driver_pll", "tuner_grid", "rtl_agc", "bias_tee")
# Full-scale value of each unsigned 8-bit sample.
_LEVELS = (np.arange(256, dtype=np.float32) - np.float32(127.5)) / np.float32(127.5)

log = logging.getLogger(__name__)

# Receives raw samples (RtlTcpSource.tap): whole I/Q pairs of unsigned bytes,
# I first, the centre frequency and the sample rate they were taken with, and
# whether they follow those it received before without a gap.
SampleTap = Callable[[bytes, float, float, bool], None]


@dataclass(frozen=True)
class Tuner:
    """A tuner, with what librtlsdr lets it do."""

    name: str
    range_hz: tuple[float, float]   # centre frequencies it tunes
    gains_db: tuple[float, ...]     # manual gains, lowest first; none: automatic gain only
    # Whether a dongle with this tuner may cover shortwave by itself, as the
    # RTL-SDR Blog V4 does: rtl_tcp announces a V4 as any R828D.
    shortwave: bool = False
    # An R820T or R828D, whose PLL r82xx.py computes: placed on its grid.
    pll: bool = False
    # Below this, a dongle with this tuner may upconvert (an RTL-SDR Blog V4's R828D).
    upconversion_hz: float = 0.0


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
    5: Tuner("Rafael Micro R820T", (24e6, 1766e6), _R82XX_GAINS_DB, shortwave=True, pll=True),
    6: Tuner("Rafael Micro R828D", (24e6, 1766e6), _R82XX_GAINS_DB, shortwave=True, pll=True,
             upconversion_hz=float(r82xx.UPCONVERSION_HZ)),
}
# A command carries the frequency in 32 bits: the only limits for an unknown tuner.
_ANY_FREQUENCY_HZ = (1.0, float(0xFFFF_FFFF))


def parse_address(text: str) -> tuple[str, int]:
    """The host and port of an rtl_tcp server written as HOST, PORT or
    HOST:PORT, an IPv6 host in brackets as in [::1]:1234; rtl_tcp's default
    address gives the missing part. Raises ValueError, saying why, for
    anything else."""
    text = text.strip()
    host, port = DEFAULT_ADDRESS
    if not text:
        raise ValueError("no address given")
    if any(char.isspace() for char in text):
        raise ValueError("the address must be HOST, PORT or HOST:PORT")
    if text.isascii() and text.isdigit():
        return host, _parse_port(text)
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1:]
        if end < 0 or (rest and not rest.startswith(":")):
            raise ValueError("an IPv6 address goes in brackets, as in [::1]:1234")
        host = text[1:end]
        if rest:
            port = _parse_port(rest[1:])
    elif text.count(":") > 1:
        raise ValueError("an IPv6 address goes in brackets, as in [::1]:1234")
    else:
        host, colon, port_text = text.partition(":")
        if colon:
            port = _parse_port(port_text)
    if not host:
        raise ValueError("the address must be HOST, PORT or HOST:PORT")
    if len(host) > _MAX_HOST:
        raise ValueError("the host name is too long")
    return host, port


def _parse_port(text: str) -> int:
    # At most five digits: int() refuses very long numbers with its own message.
    port = int(text) if text.isascii() and text.isdigit() and len(text) <= 5 else 0
    if not 1 <= port <= 65535:
        raise ValueError("the port must be a whole number from 1 to 65535")
    return port


def tuner_for(tuner_type: int, gain_count: int) -> Tuner:
    """The tuner an rtl_tcp header announces. Its gains apply only when the
    header counts as many: another librtlsdr may offer other ones."""
    known = TUNERS.get(tuner_type)
    if known is None:
        return Tuner(f"Unknown tuner (type {tuner_type})", _ANY_FREQUENCY_HZ, ())
    if len(known.gains_db) != gain_count:
        return replace(known, gains_db=())
    return known


def hf_inputs(tuner: Tuner | None) -> tuple[str, ...]:
    """The shortwave inputs offered with ``tuner``: all of them until it is
    known, ``tuner`` only with a tuner that may cover shortwave."""
    if tuner is None or tuner.shortwave:
        return HF_INPUTS
    return tuple(name for name in HF_INPUTS if name != "tuner")


@dataclass(frozen=True)
class Settings:
    """What the receiver is asked to do."""

    centre_hz: float
    sample_rate_hz: float
    gain_db: float | None           # None: the tuner's automatic gain
    ppm: float                      # frequency correction, in parts per million to the hundredth
    hf_input: str = "none"          # one of HF_INPUTS
    rtl_agc: bool = False           # the RTL2832's AGC, while sampling directly
    bias_tee: bool = False          # antenna power, never saved
    converter: str = "none"         # one of converter.CONVERTERS
    converter_lo_hz: float = DEFAULT_LO_HZ  # the converter's oscillator, in whole hertz
    driver_pll: str = "unknown"     # one of DRIVER_PLLS
    tuner_grid: bool = True         # an R820T or R828D on its grid; never saved


class RtlTcpSource:
    """An RTL-SDR receiver served by rtl_tcp, read like any source.

    ``centre_hz`` and ``sample_rate_hz`` describe the samples ``read`` last
    returned, with ``exact_centre_hz``, the centre in use that labels them,
    and ``rate_scale`` (module docstring). ``describe`` gives the settings
    asked of the receiver and the connection state, and ``on_change`` is
    called whenever they change.

    Without ``host`` and ``port``, the source reads rtl_tcp's default
    address until the engine gives it the saved one; with either of them,
    the address is ``given``, and the saved one does not replace it.
    """

    kind = "rtl_tcp"

    def __init__(self, host: str | None = None, port: int | None = None, *,
                 centre_hz: float = DEFAULT_CENTRE_HZ, sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
                 gain_db: float | None = None, ppm: float = 0, settle_s: float = SETTLE_S,
                 buffer_s: float = BUFFER_S, idle_s: float = IDLE_S, read_wait_s: float = READ_WAIT_S,
                 timeout_s: float = TIMEOUT_S, retry_s: tuple[float, float] = RETRY_S) -> None:
        default_host, default_port = DEFAULT_ADDRESS
        self.given = frozenset() if host is None and port is None else frozenset({"address"})
        self.host = default_host if host is None else host
        self.port = default_port if port is None else port
        self._settle_s = settle_s
        self._buffer_s = buffer_s
        self._idle_s = idle_s
        self._read_wait_s = read_wait_s
        self._timeout_s = timeout_s
        self._retry_s = retry_s
        start = Settings(DEFAULT_CENTRE_HZ, DEFAULT_SAMPLE_RATE_HZ, None, 0.0)
        self._settings = _changed(start, {"centre_hz": centre_hz, "sample_rate_hz": sample_rate_hz,
                                          "gain_db": gain_db, "ppm": ppm}, None)
        self.centre_hz = self._settings.centre_hz
        self.sample_rate_hz = self._settings.sample_rate_hz
        self.exact_centre_hz = self.centre_hz
        self.rate_scale = 1.0
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
        self._powered_off = False   # close() asked rtl_tcp to turn the antenna power off
        # The tap of a recording: where the samples it received end in
        # rtl_tcp's stream, None after a gap; an I byte that waits for its Q
        # byte; and whether the next samples follow those it received.
        self._tap: SampleTap | None = None
        self._tap_next: int | None = None
        self._tap_carry = b""
        self._tap_joined = False

    @property
    def address(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def settings_key(self) -> str:
        """One set of settings whatever the address, which is one of them:
        the address is only the way to the receiver."""
        return self.kind

    @property
    def reopen(self) -> dict[str, Any]:
        """What opens this receiver again (sources.py): the address comes
        from the saved settings."""
        return {"kind": self.kind}

    def _settable(self) -> list[str]:
        """The settings an interface may change."""
        return ["address", *_SETTINGS]

    @property
    def hardware(self) -> str:
        """The receiver, for a recording's metadata: with its tuner once
        rtl_tcp named it, such as "RTL-SDR, Rafael Micro R820T", and the
        converter in front of it."""
        name = "RTL-SDR" if self.tuner is None else f"RTL-SDR, {self.tuner.name}"
        converter = _conversion(self._settings).text()
        return f"{name}, behind {converter}" if converter else name

    def tap(self, receiver: SampleTap | None) -> None:
        """Hand ``receiver`` every sample that arrives from now on, as rtl_tcp
        sends them: whole I/Q pairs of unsigned bytes, I first (SigMF's
        "cu8"), with the centre frequency and the sample rate of the setting
        they were taken with, and whether they follow those it received
        before without a gap. They come in the event loop as they arrive,
        whether the engine reads them or not: those the source drops for a
        reader that falls behind included. The centre frequency is the one
        on the air, and I and Q come swapped behind a converter that
        mirrors the spectrum. A new connection, and a change of frequency,
        rate, correction or converter, whose samples are dropped while the
        receiver settles, leave a gap. None stops."""
        self._tap = receiver
        self._tap_next = None

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
        settings = self._settings
        tuning = _tuning(settings, self.tuner)
        self.centre_hz = settings.centre_hz
        self.sample_rate_hz = settings.sample_rate_hz
        self.exact_centre_hz = tuning.centre_hz
        self.rate_scale = tuning.scale
        pairs = _LEVELS[np.frombuffer(data, np.uint8)].reshape(-1, 2)
        if _conversion(settings).inverted:
            pairs = np.ascontiguousarray(pairs[:, ::-1])     # Q, I: the band as it is on the air
        return pairs.ravel().view(np.complex64)

    def describe(self) -> dict[str, Any]:
        """The receiver's settings and the connection state, as JSON values."""
        settings, tuner = self._settings, self.tuner
        ranges = _reach(settings, tuner)
        return {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "address": self.address,
            "tuner": tuner.name if tuner else None,
            "centre_hz": settings.centre_hz,
            "centre_in_use_hz": _tuning(settings, tuner).centre_hz,
            "sample_rate_hz": settings.sample_rate_hz,
            "gain_db": settings.gain_db,
            "ppm": settings.ppm,
            "gains_db": list(tuner.gains_db) if tuner else [],
            "range_hz": [ranges[0][0], ranges[-1][1]],
            "hf_input": settings.hf_input,
            "hf_inputs": list(hf_inputs(tuner)),
            "direct": _direct_input(settings, tuner) != 0,
            "rtl_agc": settings.rtl_agc,
            "bias_tee": settings.bias_tee,
            "converter": settings.converter,
            "converter_lo_hz": settings.converter_lo_hz,
            "driver_pll": settings.driver_pll,
            "tuner_grid": settings.tuner_grid,
            "settable": self._settable(),
        }

    def centre_in_use(self, centre_hz: float) -> float:
        """The centre in use on the air that asking for ``centre_hz`` would
        give with the other settings as they are, without checking that the
        receiver reaches it: ``centre_hz`` itself until the tuner is known,
        and within half a hertz of it wherever the receiver is not placed on
        the grid."""
        settings = replace(self._settings, centre_hz=float(round(centre_hz)))
        return _tuning(settings, self.tuner).centre_hz

    @property
    def grid_hz(self) -> float:
        """How far apart, on the air, the centres in use lie where the
        receiver is placed on the grid: 0 with a receiver that is never
        placed (module docstring)."""
        tuner, settings = self.tuner, self._settings
        if tuner is None or not tuner.pll or not settings.tuner_grid:
            return 0.0
        return r82xx.GRID_HZ * (1 + settings.ppm / 1e6)

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change ``address`` (as ``parse_address`` reads it), ``centre_hz``,
        ``sample_rate_hz``, ``gain_db`` (``None`` for automatic gain),
        ``ppm``, ``hf_input`` (one of ``HF_INPUTS``), ``rtl_agc``,
        ``bias_tee``, ``converter`` (one of ``converter.CONVERTERS``),
        ``converter_lo_hz``, ``driver_pll`` (one of ``DRIVER_PLLS``) and
        ``tuner_grid``, then call ``on_change``.

        Raises ValueError, changing nothing, when a setting is unknown or
        invalid. A gain becomes the nearest the tuner offers. Another input
        or sample rate brings the centre frequency within the new reach.
        Another converter or oscillator, without a centre frequency in the
        same change, keeps the receiver where it is tuned: the centre
        frequency on the air moves, and is refused beyond the new reach.
        Changes made while disconnected apply at the next connection.
        Another address hangs up, and the next read connects there with the
        same settings; its tuner is unknown until it answers.
        """
        changes = dict(changes)
        address = None
        if "address" in changes and "address" in self._settable():
            value = changes.pop("address")
            if not isinstance(value, str):
                raise ValueError("the address must be text")
            address = parse_address(value)
            if address == (self.host, self.port):
                address = None
        old = self._settings
        tuner = self.tuner if address is None else None
        new = _changed(old, changes, tuner)
        self._settings = new
        old_input, new_input = _direct_input(old, tuner), _direct_input(new, tuner)
        retuned = _tuning_key(new, tuner) != _tuning_key(old, tuner)
        if address is not None:
            self._power_off(old)    # the receiver left behind
            self.host, self.port = address
            self.tuner = None
            if self._task is not None and not self._task.done():
                # The task ends, then the next read starts one for the new address.
                self._hang_up()
                self.status, self.detail = "connecting", ""
            self._notify()
            return
        connection = self._connection
        if connection is not None and connection.tuner is not None:
            # Leaving direct sampling starts the tuner again (module docstring).
            restarted = bool(old_input) and not new_input
            # The tuner sleeps while sampling directly: its gain waits.
            if new.gain_db != old.gain_db and not old_input and not new_input:
                _send_gain(connection, new.gain_db)
            # The receiver retunes on 0x05: it goes only when the whole part changes.
            whole = _whole(new, connection.tuner)
            if whole != _whole(old, connection.tuner):
                connection.send(_SET_FREQUENCY_CORRECTION, whole)
            if new.sample_rate_hz != old.sample_rate_hz and not restarted:
                connection.send(_SET_SAMPLE_RATE, int(new.sample_rate_hz))
            if new_input != old_input:
                connection.send(_SET_DIRECT_SAMPLING, new_input)
                if not old_input and new.rtl_agc:
                    connection.send(_SET_RTL_AGC, 1)
                if restarted and old.rtl_agc:
                    connection.send(_SET_RTL_AGC, 0)
            elif new_input and new.rtl_agc != old.rtl_agc:
                connection.send(_SET_RTL_AGC, int(new.rtl_agc))
            if new.bias_tee != old.bias_tee:
                connection.send(_SET_BIAS_TEE, int(new.bias_tee))
            if retuned:
                # Also after a change of rate, correction, input or
                # converter, so that the receiver applies it whatever the
                # librtlsdr version; after 0x05, which retuned to the
                # previous request with the new whole part.
                connection.send(_SET_FREQUENCY, _tuning(new, connection.tuner).request_hz)
            if restarted:
                _send_forgotten(connection, new)
        if retuned:
            self._new_setting()
        self._notify()

    def close(self) -> None:
        """Hang up and stop for good. Safe to repeat: a repeat changes
        nothing, and lets a stop in progress finish."""
        if self._closed:
            return
        self._closed = True
        self._powered_off = self._power_off(self._settings)
        self._hang_up()
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

    def _power_off(self, settings: Settings) -> bool:
        """Turn the antenna power off on a receiver about to be left, when
        ``settings`` have it on and rtl_tcp answered: True once asked. The
        command goes out before the connection closes."""
        connection = self._connection
        if not settings.bias_tee or connection is None or connection.tuner is None:
            return False
        connection.send(_SET_BIAS_TEE, 0)
        return True

    def _hang_up(self) -> None:
        """End the connection, or the attempts to make one, with the samples
        it brought."""
        if self._connection is not None:
            self._connection.close("closed")
        if self._task is not None:
            self._task.cancel()
        self._buffer.clear()

    def _idle(self) -> bool:
        """Nobody has read for a while: the engine has no client."""
        return not self._reading and time.monotonic() - self._last_read >= self._idle_s

    def _header_received(self, connection: _Connection) -> None:
        """rtl_tcp answered: fit the settings to its tuner and send them all,
        direct sampling and the RTL2832's AGC included, which rtl_tcp keeps
        from its previous client. The frequency, the sample rate and the
        gain follow direct sampling: 0x09 with 0 starts the tuner again
        without them (module docstring)."""
        tuner = connection.tuner
        assert tuner is not None
        self.tuner = tuner
        settings = self._settings = _fitted(self._settings, tuner)
        direct = _direct_input(settings, tuner)
        connection.send(_SET_FREQUENCY_CORRECTION, _whole(settings, tuner))
        connection.send(_SET_DIRECT_SAMPLING, direct)
        connection.send(_SET_RTL_AGC, int(bool(direct) and settings.rtl_agc))
        connection.send(_SET_FREQUENCY, _tuning(settings, tuner).request_hz)
        if direct:
            connection.send(_SET_SAMPLE_RATE, int(settings.sample_rate_hz))
        else:
            _send_forgotten(connection, settings)
        connection.send(_SET_BIAS_TEE, int(settings.bias_tee))
        self._new_setting()
        self._set_status("streaming", "")

    def _store(self, offset: int, data: bytes) -> None:
        """Keep I/Q bytes that start ``offset`` bytes into rtl_tcp's stream."""
        if time.monotonic() < self._settle_until:
            return
        if self._tap is not None:
            self._hand(offset, data)
        if not self._buffer and offset % 2:
            data = data[1:]         # a Q byte: start with the next pair
        self._buffer += data
        capacity = max(2 * round(self._buffer_s * self._settings.sample_rate_hz), self._wanted)
        excess = len(self._buffer) - capacity
        if excess > 0:
            del self._buffer[:excess + excess % 2]
        if len(self._buffer) >= self._wanted:
            self._arrived.set()

    def _hand(self, offset: int, data: bytes) -> None:
        """Hand the tap the whole I/Q pairs of ``data``, which starts
        ``offset`` bytes into rtl_tcp's stream, keeping a last I byte for the
        Q byte that follows it. A tap that fails is left, so that the
        connection goes on."""
        end = offset + len(data)
        if offset != self._tap_next:            # after a gap
            self._tap_carry, self._tap_joined = b"", False
            if offset % 2:
                data = data[1:]     # a Q byte: start with the next pair
        elif self._tap_carry:
            data = self._tap_carry + data
        whole = len(data) - len(data) % 2
        self._tap_carry, self._tap_next = data[whole:], end
        if not whole:
            return
        if whole < len(data):
            data = data[:whole]
        settings, joined = self._settings, self._tap_joined
        self._tap_joined = True
        if _conversion(settings).inverted:
            swapped = bytearray(len(data))      # Q, I: the band as it is on the air
            swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
            data = bytes(swapped)
        try:
            self._tap(data, _tuning(settings, self.tuner).centre_hz, settings.sample_rate_hz, joined)
        except Exception:
            log.exception("the samples' tap failed: it is left")
            self._tap = None

    def _new_setting(self) -> None:
        """Drop the samples of the previous setting, with those still on their way."""
        self._buffer.clear()
        self._settle_until = time.monotonic() + self._settle_s
        self._tap_next = None       # a gap for the tap

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

    @property
    def reopen(self) -> dict[str, Any]:
        return {"kind": self.kind, "device": self.device}

    def _settable(self) -> list[str]:
        return list(_SETTINGS)      # the source chooses where its rtl_tcp listens

    def describe(self) -> dict[str, Any]:
        # rtl_tcp listens on another port at each start: of no use to an interface.
        return {**super().describe(), "address": None}

    def close(self) -> None:
        if self._closed:
            return
        super().close()
        # rtl_tcp stops at once, unless it has the antenna power to turn off first.
        if self._child is not None and not self._powered_off:
            self._child.terminate()

    async def _session(self) -> None:
        try:
            await super()._session()
        finally:
            child, self._child = self._child, None
            if child is not None:
                if self._powered_off:
                    await asyncio.sleep(POWER_OFF_S)
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


def _send_forgotten(connection: _Connection, settings: Settings) -> None:
    """Send again what the tuner forgot when command 0x09 with 0 started it
    again, after the frequency (module docstring): the sample rate, which
    gives it the bandwidth and the intermediate frequency of the rate, then
    the gain."""
    connection.send(_SET_SAMPLE_RATE, int(settings.sample_rate_hz))
    _send_gain(connection, settings.gain_db)


def _changed(settings: Settings, changes: Mapping[str, object], tuner: Tuner | None) -> Settings:
    """``settings`` with ``changes``; raises ValueError for any invalid change.
    Without a known tuner, any frequency, gain and input pass until it is
    known. A new input or sample rate without a new centre frequency brings
    the centre within the new reach; a new converter or oscillator keeps
    the receiver where it is tuned, and is refused when that is out of the
    new reach on the air."""
    unknown = sorted(set(changes) - set(_SETTINGS))
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(unknown)}")
    before = settings
    if "converter_lo_hz" in changes:
        settings = replace(settings, converter_lo_hz=checked_lo(changes["converter_lo_hz"]))
    if "converter" in changes:
        settings = replace(settings, converter=checked_converter(changes["converter"]))
    if "hf_input" in changes:
        value = changes["hf_input"]
        offered = hf_inputs(tuner)
        if not isinstance(value, str) or value not in offered:
            raise ValueError(f"the shortwave input must be one of: {', '.join(offered)}")
        settings = replace(settings, hf_input=value)
    if "rtl_agc" in changes:
        value = changes["rtl_agc"]
        if not isinstance(value, bool):
            raise ValueError("the RTL2832's AGC must be true or false")
        settings = replace(settings, rtl_agc=value)
    if "bias_tee" in changes:
        value = changes["bias_tee"]
        if not isinstance(value, bool):
            raise ValueError("the antenna power must be true or false")
        settings = replace(settings, bias_tee=value)
    if "sample_rate_hz" in changes:
        rate = float(round(_number(changes["sample_rate_hz"], "the sample rate")))
        if not any(low <= rate <= high for low, high in SAMPLE_RATES_HZ):
            ranges = " or ".join(f"from {low / 1e6:g} to {high / 1e6:g}" for low, high in SAMPLE_RATES_HZ)
            raise ValueError(f"the sample rate must be {ranges} MS/s")
        settings = replace(settings, sample_rate_hz=rate)
    reach = _reach(settings, tuner)
    if "centre_hz" in changes:
        centre = float(round(_number(changes["centre_hz"], "the centre frequency")))
        if not _within(centre, reach):
            raise ValueError(_reach_text(reach, settings))
        settings = replace(settings, centre_hz=centre)
    elif _conversion(settings).key != _conversion(before).key:
        # The receiver stays where it is tuned: the signals stay, their frequencies change.
        centre = _conversion(settings).air(_tuned_hz(before))
        if not _within(centre, reach):
            raise ValueError(_conversion_text(centre, reach, settings))
        settings = replace(settings, centre_hz=centre)
    elif "hf_input" in changes or "sample_rate_hz" in changes:
        settings = _within_reach(settings, tuner)
    if "gain_db" in changes:
        value = changes["gain_db"]
        gain = None if value is None else _number(value, "the gain")
        if gain is not None and tuner is not None:
            if not tuner.gains_db:
                raise ValueError("this tuner offers automatic gain only")
            gain = _nearest(tuner.gains_db, gain)
        settings = replace(settings, gain_db=gain)
    if "ppm" in changes:
        settings = replace(settings, ppm=checked_ppm(changes["ppm"]))
    if "driver_pll" in changes:
        value = changes["driver_pll"]
        if not isinstance(value, str) or value not in DRIVER_PLLS:
            raise ValueError(f"the driver's computation must be one of: {', '.join(DRIVER_PLLS)}")
        settings = replace(settings, driver_pll=value)
    if "tuner_grid" in changes:
        value = changes["tuner_grid"]
        if not isinstance(value, bool):
            raise ValueError("the tuner's grid must be true or false")
        settings = replace(settings, tuner_grid=value)
    return settings


def _fitted(settings: Settings, tuner: Tuner) -> Settings:
    """``settings`` brought within what ``tuner`` can do: an input it does
    not offer becomes ``none``, as does a converter through which it would
    reach nothing on the air."""
    if settings.hf_input not in hf_inputs(tuner):
        settings = replace(settings, hf_input="none")
    if not _reach(settings, tuner):
        log.warning("the converter is not used: through it, the %s reaches nothing on the air", tuner.name)
        settings = replace(settings, converter="none")
    gain = settings.gain_db
    if gain is not None:
        gain = _nearest(tuner.gains_db, gain) if tuner.gains_db else None
    return replace(_within_reach(settings, tuner), gain_db=gain)


def _conversion(settings: Settings) -> Conversion:
    return Conversion(settings.converter, settings.converter_lo_hz)


def _tuned_hz(settings: Settings) -> int:
    """The frequency the receiver tunes for ``settings``' centre frequency
    on the air: within 32 bits, as the centre is within reach."""
    return int(_conversion(settings).tuned(settings.centre_hz))


def _requested_hz(settings: Settings, tuner: Tuner, whole: int) -> int:
    """The frequency command's value: the frequency the receiver tunes,
    scaled for the part of the correction that it does not take itself
    (``whole`` being the part it takes), within 32 bits.

    The scale moves it by at most half a ppm. With the tuner, it stays
    within the tuner's range, so that at the range's very edges the
    request does not leave it: below the R820T's 24 MHz, the RTL-SDR Blog
    driver would switch to direct sampling by itself, and above its top the
    tuner's PLL may not lock. There, within half a ppm of an edge, the
    samples' label may be off by as much: 12 Hz at 24 MHz, 883 Hz at
    1766 MHz. A tuner that covers shortwave (``hf_input`` ``tuner``) goes
    below its range's start by itself. Direct sampling, which command 0x09
    chose already, keeps the scaled request."""
    low, high = _ANY_FREQUENCY_HZ
    if not _direct_input(settings, tuner):
        if settings.hf_input != "tuner":
            low = max(low, tuner.range_hz[0])
        high = min(high, tuner.range_hz[1])
    return int(min(max(corrected_hz(_tuned_hz(settings), settings.ppm, whole), low), high))


def _whole(settings: Settings, tuner: Tuner | None) -> int:
    """The part of the correction that the receiver takes itself (command
    0x05): none with an R820T or R828D, whose grid a corrected crystal
    would move, and otherwise its nearest whole number."""
    return whole_ppm(settings.ppm, tuner is None or not tuner.pll)


@dataclass(frozen=True)
class _Tuning:
    """How the receiver tunes for a setting: the frequency command's value,
    the centre in use on the air that labels the samples, and how much
    faster than its nominal rate the receiver samples, which Radiolack
    alone corrects."""

    request_hz: int
    centre_hz: float
    scale: float = 1.0


@functools.lru_cache(maxsize=64)
def _tuning(settings: Settings, tuner: Tuner | None) -> _Tuning:
    """How the receiver tunes for ``settings`` with ``tuner`` (module
    docstring). A tuner other than an R820T or R828D takes the request of
    section 9, labelled with the centre asked. With one, the whole of the
    correction is Radiolack's: the request goes on the grid where it
    applies, and the label is where that puts the receiver on the air, by
    the driver's computation once ``driver_pll`` names it, the crystal
    taken as corrected. Until the tuner is known nothing is asked, and the
    samples, of no receiver yet, keep the centre asked."""
    if tuner is None:
        return _Tuning(0, settings.centre_hz)
    request = _requested_hz(settings, tuner, _whole(settings, tuner))
    if not tuner.pll:
        return _Tuning(request, settings.centre_hz)
    scale = 1 + settings.ppm / 1e6
    tuned = float(request)
    if not _direct_input(settings, tuner) and _computed(request, settings, tuner):
        if settings.tuner_grid:
            try:
                placed = r82xx.on_grid(request, r82xx.rate_if_hz(int(settings.sample_rate_hz)),
                                       max(tuner.range_hz[0], tuner.upconversion_hz), tuner.range_hz[1])
            except ValueError:      # no oscillator on the grid within the range: unplaced
                placed = request
            if _computed(placed, settings, tuner):
                request = placed
                tuned = float(placed)
        if settings.driver_pll != "unknown":
            tuned = r82xx.tuned_hz(settings.driver_pll, request, int(settings.sample_rate_hz))
    return _Tuning(request, _conversion(settings).air(tuned * scale), scale)


def _tuning_key(settings: Settings, tuner: Tuner | None) -> tuple[Any, ...]:
    """What the receiver is asked for ``settings``: a change retunes it,
    and its samples settle (module docstring). Until the tuner is known,
    the settings that make it."""
    if tuner is None:
        return (settings.centre_hz, settings.sample_rate_hz, settings.ppm, settings.hf_input,
                _conversion(settings).key)
    return (_tuning(settings, tuner).request_hz, settings.sample_rate_hz, _whole(settings, tuner),
            _direct_input(settings, tuner), _conversion(settings).key)


def _computed(request_hz: int, settings: Settings, tuner: Tuner) -> bool:
    """Whether the tuner, asked for ``request_hz``, sets what r82xx.py
    computes: from its range's start, which a tuner that covers shortwave
    may go below, from where a V4's driver stops upconverting, and with an
    oscillator that its PLL can make."""
    if request_hz < max(tuner.range_hz[0], tuner.upconversion_hz):
        return False
    try:
        r82xx.mix_divider(r82xx.oscillator_hz(request_hz, int(settings.sample_rate_hz)))
    except ValueError:
        return False
    return True


def _centre_ranges(settings: Settings, tuner: Tuner | None) -> list[tuple[float, float, str]]:
    """The centre frequencies the receiver tunes with ``settings``' input
    and sample rate, as one or two ranges, lowest first, each with how it
    tunes them when there are two. Direct sampling's span stays within 0 to
    28.8 MHz, and a tuner that covers shortwave goes down to a span that
    starts at 0 Hz; with a tuner whose range starts above what direct
    sampling reaches, the frequencies in between are out of reach."""
    if tuner is None:
        return [(*_ANY_FREQUENCY_HZ, "")]
    low, high = tuner.range_hz
    half = float(math.ceil(settings.sample_rate_hz / 2))    # whole hertz, as the centre is
    if settings.hf_input == "tuner":
        return [(min(half, low), high, "")]
    if settings.hf_input in _DIRECT_INPUTS:
        top = DIRECT_SAMPLING_HZ - half
        if top >= low:
            return [(min(half, low), high, "")]
        return [(half, top, "by direct sampling"), (low, high, "with its tuner")]
    return [(low, high, "")]


def _reach(settings: Settings, tuner: Tuner | None) -> list[tuple[float, float, str]]:
    """The centre frequencies on the air within reach, through the converter
    if there is one: none when it leaves nothing."""
    return _conversion(settings).reach(_centre_ranges(settings, tuner), settings.sample_rate_hz)


def _within(centre_hz: float, reach: list[tuple[float, float, str]]) -> bool:
    return any(low <= centre_hz <= high for low, high, _ in reach)


def _reach_text(reach: list[tuple[float, float, str]], settings: Settings) -> str:
    """Why a centre frequency outside ``reach`` is refused."""
    through = " through its converter" if _conversion(settings).active else ""
    if not reach:
        return (f"through this converter the receiver reaches nothing on the air from "
                f"{mhz(math.ceil(settings.sample_rate_hz / 2))} MHz, half the sample rate, "
                f"to {HIGHEST_HZ / 1e9:g} GHz")
    ranges = " and ".join(f"from {mhz(low)} to {mhz(high)} MHz" + (f" {label}" if label else "")
                          for low, high, label in reach)
    return f"the receiver tunes {ranges}{through}"


def _conversion_text(centre_hz: float, reach: list[tuple[float, float, str]], settings: Settings) -> str:
    """Why a converter that would put the centre frequency at ``centre_hz``
    on the air is refused."""
    if not reach:
        return _reach_text(reach, settings)
    return (f"through this converter the receiver's centre frequency would be {mhz(centre_hz)} MHz "
            f"on the air, out of reach: {_reach_text(reach, settings)}")


def _within_reach(settings: Settings, tuner: Tuner | None) -> Settings:
    """``settings`` with the centre frequency brought to the nearest one the
    receiver tunes; raises ValueError when it reaches none."""
    centre = settings.centre_hz
    reach = _reach(settings, tuner)
    if not reach:
        raise ValueError(_reach_text(reach, settings))
    nearest = min((min(max(centre, low), high) for low, high, _ in reach),
                  key=lambda candidate: abs(candidate - centre))
    return replace(settings, centre_hz=nearest)


def _direct_input(settings: Settings, tuner: Tuner | None) -> int:
    """Command 0x09's value for ``settings``: the direct sampling input in
    use below the tuner's range, 0 for the tuner, and while the tuner is
    unknown."""
    if tuner is None or _tuned_hz(settings) >= tuner.range_hz[0]:
        return 0
    return _DIRECT_INPUTS.get(settings.hf_input, 0)


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
