"""A stand-in for SoapySDR's Python module, for the tests: no library, no
device, no network.

The tests put this folder first on the import path (``tests.fakes.
stand_in_soapysdr``), so that ``import SoapySDR`` finds it whether the real
module is installed or not. It keeps the real module's names and values for
what Radiolack uses (SoapySDR 0.8's Python binding), and its devices are the
ones a test declares with ``add``: each found by ``Device.enumerate`` when
the search's arguments match, made by ``Device(args)`` or ``Device.make``,
and giving samples in its native format at its sample rate, paced by the
wall clock, as a receiver does, with the overflows, time-outs and stream
errors the test scripts.

Receive only: every call that takes a direction refuses any but
``SOAPY_SDR_RX``, and ``writeStream`` refuses whatever it is given. A
refusal raises ``TransmitRequested``, an AssertionError, and is kept in
``transmit_requests``, which the tests check at the end, so that a refusal
caught by the code under test still fails the test.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SOAPY_SDR_TX = 0
SOAPY_SDR_RX = 1

SOAPY_SDR_END_BURST = 1 << 1
SOAPY_SDR_HAS_TIME = 1 << 2
SOAPY_SDR_END_ABRUPT = 1 << 3
SOAPY_SDR_ONE_PACKET = 1 << 4
SOAPY_SDR_MORE_FRAGMENTS = 1 << 5

SOAPY_SDR_TIMEOUT = -1
SOAPY_SDR_STREAM_ERROR = -2
SOAPY_SDR_CORRUPTION = -3
SOAPY_SDR_OVERFLOW = -4
SOAPY_SDR_NOT_SUPPORTED = -5
SOAPY_SDR_TIME_ERROR = -6
SOAPY_SDR_UNDERFLOW = -7

SOAPY_SDR_CS8 = "CS8"
SOAPY_SDR_CS16 = "CS16"
SOAPY_SDR_CF32 = "CF32"

_ERRORS = {SOAPY_SDR_TIMEOUT: "TIMEOUT", SOAPY_SDR_STREAM_ERROR: "STREAM_ERROR", SOAPY_SDR_CORRUPTION: "CORRUPTION",
           SOAPY_SDR_OVERFLOW: "OVERFLOW", SOAPY_SDR_NOT_SUPPORTED: "NOT_SUPPORTED",
           SOAPY_SDR_TIME_ERROR: "TIME_ERROR", SOAPY_SDR_UNDERFLOW: "UNDERFLOW"}
# Each format's type of I and Q, and its full scale.
_FORMATS = {SOAPY_SDR_CS8: (np.int8, 128.0), SOAPY_SDR_CS16: (np.int16, 32768.0), SOAPY_SDR_CF32: (np.float32, 1.0)}
TONE_CYCLES = 1 / 64        # the samples: a tone at 1/64 of the sample rate, at half full scale

# What the tests declare and read.
devices: list[DeviceSpec] = []
searches: list[dict[str, str]] = []        # every Device.enumerate's arguments
opened: list[Device] = []                  # every device made, closed or not
transmit_requests: list[str] = []


class TransmitRequested(AssertionError):
    """Something asked the stand-in for a transmit direction."""


@dataclass
class DeviceSpec:
    """A device that a test declares: what it lists, what it can do, and
    the events of its stream, keyed by the number of the stream's read,
    counted from 0: ``"overflow"`` (it lost ``drop`` samples first, which
    its timestamps then skip), ``"timeout"`` and ``"error"``."""

    driver: str
    serial: str = "0001"
    label: str = ""
    native: str = SOAPY_SDR_CS8
    formats: tuple[str, ...] = ()           # those it converts to, the native first; the native alone if empty
    rates_hz: tuple[float, ...] = (2_400_000.0,)
    frequency_range_hz: tuple[float, float] = (1e6, 6e9)
    mtu: int = 16_384                       # samples a read gives at most
    timestamps: bool = True
    capacity_s: float = 10.0                # samples it keeps for a reader that falls behind
    events: dict[int, str] = field(default_factory=dict)
    drop: int = 1_000                       # samples an overflow loses
    fail_open: bool = False                 # making it fails, as an unplugged device

    def listing(self) -> dict[str, str]:
        return {"driver": self.driver, "serial": self.serial, "label": self.label or f"{self.driver} {self.serial}"}


def reset() -> None:
    """Forget every device, search and request: before each test."""
    for device in opened:
        device.close()
    devices.clear()
    searches.clear()
    opened.clear()
    transmit_requests.clear()


def add(driver: str, **settings: Any) -> DeviceSpec:
    spec = DeviceSpec(driver, **settings)
    devices.append(spec)
    return spec


def errToStr(code: int) -> str:  # noqa: N802 - SoapySDR's name
    return _ERRORS.get(code, "UNKNOWN")


def getAPIVersion() -> str:  # noqa: N802
    return "0.8.0"


def _receive_only(what: str, direction: int) -> None:
    if direction != SOAPY_SDR_RX:
        transmit_requests.append(f"{what}({direction!r})")
        raise TransmitRequested(f"{what} asked for direction {direction!r}: the stand-in receives only")


def _kwargs(args: Mapping[str, str] | str | None) -> dict[str, str]:
    if args is None:
        return {}
    if isinstance(args, str):
        pairs = (part.split("=", 1) for part in args.split(",") if part.strip())
        return {key.strip(): value.strip() for key, value in pairs}
    return {str(key): str(value) for key, value in dict(args).items()}


def _matches(spec: DeviceSpec, args: Mapping[str, str]) -> bool:
    listing = spec.listing()
    return all(listing.get(key) == value for key, value in args.items() if key in ("driver", "serial"))


class StreamResult:
    def __init__(self, ret: int, flags: int = 0, time_ns: int = 0) -> None:
        self.ret = ret
        self.flags = flags
        self.timeNs = time_ns  # noqa: N815 - SoapySDR's name
        self.chanMask = 0  # noqa: N815

    def __str__(self) -> str:
        return f"ret={self.ret}, flags={self.flags}, timeNs={self.timeNs}"


class _Stream:
    def __init__(self, sample_format: str, channels: Sequence[int]) -> None:
        self.format = sample_format
        self.channels = tuple(channels)
        self.active = False
        self.closed = False
        self.started = 0.0          # when activated, on the wall clock
        self.position = 0           # the next sample's index since activation
        self.reads = 0


class Device:
    """A stand-in device, made from the first declared one that matches."""

    def __new__(cls, args: Mapping[str, str] | str | None = None) -> Device:
        return cls.make(args)

    @staticmethod
    def enumerate(args: Mapping[str, str] | str | None = None) -> list[dict[str, str]]:
        wanted = _kwargs(args)
        searches.append(wanted)
        return [spec.listing() for spec in devices if _matches(spec, wanted)]

    @staticmethod
    def make(args: Mapping[str, str] | str | None = None) -> Device:
        wanted = _kwargs(args)
        for spec in devices:
            if _matches(spec, wanted):
                if spec.fail_open:
                    raise RuntimeError("SoapySDR::Device::make() device not found")
                device = object.__new__(Device)
                device._setup(spec)
                opened.append(device)
                return device
        raise RuntimeError("SoapySDR::Device::make() no match")

    @staticmethod
    def unmake(device: Device) -> None:
        device.close()

    def _setup(self, spec: DeviceSpec) -> None:
        self.spec = spec
        self.rate_hz = spec.rates_hz[0]
        self.frequency_hz = spec.frequency_range_hz[0]
        self.streams: list[_Stream] = []
        self.closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        self.closed = True

    def getDriverKey(self) -> str:  # noqa: N802
        return self.spec.driver

    def getHardwareKey(self) -> str:  # noqa: N802
        return f"{self.spec.driver}-stand-in"

    def getNumChannels(self, direction: int) -> int:  # noqa: N802
        _receive_only("getNumChannels", direction)
        return 1

    def getStreamFormats(self, direction: int, channel: int) -> list[str]:  # noqa: N802
        _receive_only("getStreamFormats", direction)
        return list(self.spec.formats or (self.spec.native,))

    def getNativeStreamFormat(self, direction: int, channel: int, fullScale: Any) -> str:  # noqa: N802,N803
        # SoapySDR 0.8's binding has no output for the full scale, which it
        # takes as a C++ reference: from Python it cannot be called. The
        # stand-in keeps that signature, and refuses any value for it.
        _receive_only("getNativeStreamFormat", direction)
        raise TypeError("in method 'Device_getNativeStreamFormat', argument 4 of type 'double &'")

    def listSampleRates(self, direction: int, channel: int) -> list[float]:  # noqa: N802
        _receive_only("listSampleRates", direction)
        return list(self.spec.rates_hz)

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:  # noqa: N802
        _receive_only("setSampleRate", direction)
        if rate not in self.spec.rates_hz:
            raise RuntimeError(f"setSampleRate({rate}) not supported")
        self.rate_hz = float(rate)

    def getSampleRate(self, direction: int, channel: int) -> float:  # noqa: N802
        _receive_only("getSampleRate", direction)
        return self.rate_hz

    def setFrequency(self, direction: int, channel: int, frequency: float,  # noqa: N802
                     args: Mapping[str, str] | None = None) -> None:
        _receive_only("setFrequency", direction)
        low, high = self.spec.frequency_range_hz
        if not low <= frequency <= high:
            raise RuntimeError(f"setFrequency({frequency}) out of range")
        self.frequency_hz = float(frequency)

    def getFrequency(self, direction: int, channel: int) -> float:  # noqa: N802
        _receive_only("getFrequency", direction)
        return self.frequency_hz

    def setupStream(self, direction: int, sample_format: str, channels: Sequence[int] = (0,),  # noqa: N802
                    args: Mapping[str, str] | None = None) -> _Stream:
        _receive_only("setupStream", direction)
        if sample_format not in self.getStreamFormats(direction, 0):
            raise RuntimeError(f"setupStream: format {sample_format} not supported")
        if sample_format not in _FORMATS:
            raise RuntimeError(f"the stand-in has no samples in {sample_format}")
        stream = _Stream(sample_format, channels)
        self.streams.append(stream)
        return stream

    def getStreamMTU(self, stream: _Stream) -> int:  # noqa: N802
        return self.spec.mtu

    def activateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0, numElems: int = 0) -> int:  # noqa: N802,N803
        stream.active = True
        stream.started = time.monotonic()
        stream.position = 0
        return 0

    def deactivateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0) -> int:  # noqa: N802,N803
        stream.active = False
        return 0

    def closeStream(self, stream: _Stream) -> None:  # noqa: N802
        stream.active = False
        stream.closed = True

    def readStream(self, stream: _Stream, buffs: Sequence[np.ndarray], numElems: int, flags: int = 0,  # noqa: N802,N803
                   timeoutUs: int = 100_000) -> StreamResult:  # noqa: N803
        """Up to ``numElems`` samples into ``buffs[0]``, those the device
        made since the previous read, waiting up to ``timeoutUs`` for a few;
        a scripted event instead, at its read."""
        if self.closed or stream.closed or not stream.active:
            return StreamResult(SOAPY_SDR_STREAM_ERROR)
        number = stream.reads
        stream.reads += 1
        event = self.spec.events.get(number)
        if event == "timeout":
            return StreamResult(SOAPY_SDR_TIMEOUT)
        if event == "error":
            return StreamResult(SOAPY_SDR_STREAM_ERROR)
        if event == "overflow":
            stream.position += self.spec.drop
            stream.started -= self.spec.drop / self.rate_hz     # the samples lost were made meanwhile
            return StreamResult(SOAPY_SDR_OVERFLOW, SOAPY_SDR_END_ABRUPT)
        wanted = min(int(numElems), self.spec.mtu)
        deadline = time.monotonic() + timeoutUs / 1e6
        while True:
            made = int((time.monotonic() - stream.started) * self.rate_hz)
            if made - stream.position > self.spec.capacity_s * self.rate_hz:
                lost = made - stream.position - int(self.spec.capacity_s * self.rate_hz)
                stream.position += lost
                return StreamResult(SOAPY_SDR_OVERFLOW, SOAPY_SDR_END_ABRUPT)
            ready = made - stream.position
            if ready >= min(wanted, self.spec.mtu // 4) or (ready > 0 and time.monotonic() >= deadline):
                break
            if time.monotonic() >= deadline:
                return StreamResult(SOAPY_SDR_TIMEOUT)
            time.sleep(min(0.002, max(0.0, deadline - time.monotonic())))
        count = min(wanted, ready)
        start = stream.position
        _fill(buffs[0], stream.format, start, count)
        stream.position += count
        result_flags = 0
        time_ns = 0
        if self.spec.timestamps:
            result_flags |= SOAPY_SDR_HAS_TIME
            time_ns = round(start * 1e9 / self.rate_hz)
        return StreamResult(count, result_flags, time_ns)

    def readStreamStatus(self, stream: _Stream, timeoutUs: int = 100_000) -> StreamResult:  # noqa: N802,N803
        return StreamResult(SOAPY_SDR_TIMEOUT)

    def writeStream(self, stream: Any, buffs: Any, numElems: int, flags: int = 0, timeNs: int = 0,  # noqa: N802,N803
                    timeoutUs: int = 100_000) -> StreamResult:  # noqa: N803
        transmit_requests.append("writeStream")
        raise TransmitRequested("writeStream: the stand-in receives only")


def samples(start: int, count: int) -> np.ndarray:
    """The samples a stand-in device gives from index ``start``, as
    complex64 at full scale 1: a test compares what it read with these."""
    index = np.arange(start, start + count, dtype=np.float64)
    return (0.5 * np.exp(2j * np.pi * TONE_CYCLES * index)).astype(np.complex64)


def _fill(buffer: np.ndarray, sample_format: str, start: int, count: int) -> None:
    component, full_scale = _FORMATS[sample_format]
    values = samples(start, count)
    if component is np.float32:
        native = values.view(np.float32)
    else:
        native = np.rint(values.view(np.float32) * full_scale).astype(component)
    target = buffer.reshape(-1).view(component)
    if len(target) < 2 * count:
        raise ValueError(f"a buffer of {len(target)} components cannot take {count} samples")
    target[:2 * count] = native
