"""A stand-in for SoapySDR's Python module, for the tests: no library, no
device, no network.

The tests put this folder first on the import path (``tests.fakes.
stand_in_soapysdr``), so that ``import SoapySDR`` finds it whether the real
module is installed or not. It keeps the real module's names and values for
what Radiolack uses (SoapySDR 0.8's Python binding), and its devices are the
ones a test declares with ``add``: each found by ``Device.enumerate`` when
the search's arguments match, made by ``Device(args)`` or ``Device.make``,
and giving samples in its native format at its sample rate, paced by the
wall clock, as a receiver does, with the overflows, silent losses,
time-outs and stream errors the test scripts. A test may make a driver's
search fail (``search_failures``) or wait (``search_gate``). A test that must not wait
replaces ``clock`` and ``sleep``, the module's wall clock and its wait,
which ``reset`` puts back.

A device also has what a SoapySDR source reads and sets: its sample rates
as a list or as ranges, its frequency range, its analogue bandwidths, its
gain stages, each with its range and step and its gain at opening, and the
overall gain that SoapySDR spreads across them (by default in the order
listed, or as the device's module does, such as SoapyHackRF's), a gain
mode, its antenna sockets, its settings, the device's and its receive
channel's, each of a type with its default, options or range, a frequency
correction of its own (a ``CORR`` frequency component, which SoapySDR's
overall frequency spreads its remainder into unless the call gives its
value, or a correction of the module's own, or none), a crystal that runs
off, a tuner that moves in steps, a carrier on the air, which its samples
then hold where the device's tuning and sampling clock put it, and a
device that is unplugged and
plugged again, whose stream cannot be set up or whose stop waits for the
test. A device may refuse to write a gain stage, its antenna or a setting
(``refuses``). ``hackrf_one``, ``rspdx_r2`` and ``rtl_sdr`` declare devices
shaped as their modules (SoapyHackRF, SoapySDRPlay3 and SoapyRTLSDR)
declare them: the RTL-SDR's as SoapyRTLSDR 0.3.3 declared one read on a
receiver (its keys, names, types, values and options), the others as
their modules' code was remembered, their real declarations to be read on
the devices.

Receive only: every call that takes a direction refuses any but
``SOAPY_SDR_RX``, and ``writeStream`` refuses whatever it is given. A
refusal raises ``TransmitRequested``, an AssertionError, and is kept in
``transmit_requests``, which the tests check at the end, so that a refusal
caught by the code under test still fails the test.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
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
_PERIOD = round(1 / TONE_CYCLES)
_TONE = (0.5 * np.exp(2j * np.pi * TONE_CYCLES * np.arange(_PERIOD))).astype(np.complex64)     # one period

# What the tests declare and read.
devices: list[DeviceSpec] = []
searches: list[dict[str, str]] = []        # every Device.enumerate's arguments
opened: list[Device] = []                  # every device made, closed or not
transmit_requests: list[str] = []
search_failures: dict[str, str] = {}       # a driver's search raises a RuntimeError with this message
search_gate: threading.Event | None = None  # each search waits for it, up to 10 s, as a slow search

# The wall clock that paces the devices, and their wait: a test may replace them.
clock = time.monotonic
sleep = time.sleep


class TransmitRequested(AssertionError):
    """Something asked the stand-in for a transmit direction."""


class ArgInfo:
    """SoapySDR's description of a setting."""

    BOOL, INT, FLOAT, STRING = 0, 1, 2, 3

    def __init__(self, key: str, value: str, name: str, description: str, kind: int,
                 span: tuple[float, float, float] | None, options: Sequence[str], option_names: Sequence[str]) -> None:
        self.key = key
        self.value = value
        self.name = name
        self.description = description
        self.units = ""
        self.type = kind
        self.range = Range(*span) if span is not None else Range(0.0, 0.0)
        self.options = list(options)
        self.optionNames = list(option_names)  # noqa: N815 - SoapySDR's name


@dataclass(frozen=True)
class SettingSpec:
    """A setting that a device declares: the device's own, or its receive
    channel's (``channel``), with its default value as SoapySDR writes it."""

    key: str
    value: str
    name: str = ""
    description: str = ""
    type: int = ArgInfo.BOOL
    options: tuple[str, ...] = ()
    option_names: tuple[str, ...] = ()
    range: tuple[float, float, float] | None = None
    channel: bool = False

    def info(self) -> ArgInfo:
        return ArgInfo(self.key, self.value, self.name, self.description, self.type, self.range, self.options,
                       self.option_names)


@dataclass
class DeviceSpec:
    """A device that a test declares: what it lists, what it can do, and
    the events of its stream, keyed by the number of the stream's read,
    counted from 0: ``"overflow"`` (it lost ``drop`` samples first, which
    its timestamps then skip), ``"skip"`` (it loses the next ``drop``
    samples it makes without saying so, as the wall clock and its
    timestamps both see), ``"timeout"`` and ``"error"``. Its timestamps
    advance by ``timestamp_factor`` times the samples read: 2 for SoapySDR's
    rtlsdr module 0.3.3, which counts the I and Q bytes."""

    driver: str
    serial: str = "0001"
    label: str = ""
    native: str = SOAPY_SDR_CS8
    formats: tuple[str, ...] = ()           # those it converts to, the native first; the native alone if empty
    rates_hz: tuple[float, ...] = (2_400_000.0,)
    frequency_range_hz: tuple[float, float] = (1e6, 6e9)
    mtu: int = 16_384                       # samples a read gives at most
    timestamps: bool = True
    timestamp_factor: float = 1.0           # the timestamps' samples per sample read
    capacity_s: float = 10.0                # samples it keeps for a reader that falls behind
    events: dict[int, str] = field(default_factory=dict)
    drop: int = 1_000                       # samples an overflow loses
    fail_open: bool = False                 # making it fails, as an unplugged device
    rate_ranges: tuple[tuple[float, float, float], ...] = ()     # lowest, highest, step
    full_scale: float | None = None         # its native integer format's full scale, if not the nominal one
    hardware: str = ""                      # its hardware key
    driver_key: str = ""                    # its driver key, if written otherwise than the driver, as "RTLSDR"
    bandwidths_hz: tuple[float, ...] = ()
    bandwidth_ranges: tuple[tuple[float, float, float], ...] = ()
    gains: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # each stage's range
    gain_values: dict[str, float] = field(default_factory=dict)     # at opening; otherwise each stage's lowest
    # How its module spreads an overall gain: each stage's gain; otherwise
    # SoapySDR's default, in the order listed.
    overall_gain: Callable[[float], dict[str, float]] | None = None
    gain_mode: bool = False                 # whether it offers automatic gain
    antennas: tuple[str, ...] = ()          # its sockets, the first in use at opening
    settings: tuple[SettingSpec, ...] = ()
    refuses: tuple[str, ...] = ()           # stages, setting keys or "antenna" whose writes it refuses
    correction: str = "none"                # "corr" (a CORR component), "native" (the module's own) or "none"
    crystal_ppm: float = 0.0                # how fast its crystal runs: its tuning and sampling clock with it
    tuning_step_hz: float = 0.0             # its tuner's step, truncating, as an R820T's with rtl-sdr's driver
    carrier_hz: float | None = None         # a carrier on the air at half full scale; otherwise the tone
    present: bool = True                    # False: unplugged
    unplugged_reads: str = "error"          # what reads give once unplugged: "error" or "timeout"
    fail_stream: bool = False               # its stream cannot be set up
    stop_gate: threading.Event | None = None  # deactivateStream waits for it, as a slow stop

    def listing(self) -> dict[str, str]:
        return {"driver": self.driver, "serial": self.serial, "label": self.label or f"{self.driver} {self.serial}"}


def reset() -> None:
    """Forget every device, search and request, and put the wall clock
    back: before each test."""
    global clock, sleep, search_gate
    for device in opened:
        device.close()
    devices.clear()
    searches.clear()
    opened.clear()
    transmit_requests.clear()
    search_failures.clear()
    if search_gate is not None:
        search_gate.set()       # a search still waiting ends
    search_gate = None
    clock, sleep = time.monotonic, time.sleep


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
    return spec.present and all(listing.get(key) == value for key, value in args.items()
                                if key in ("driver", "serial"))


class Range:
    """SoapySDR's range: lowest, highest and step, 0 for any value between."""

    def __init__(self, minimum: float, maximum: float, step: float = 0.0) -> None:
        self._range = (float(minimum), float(maximum), float(step))

    def minimum(self) -> float:
        return self._range[0]

    def maximum(self) -> float:
        return self._range[1]

    def step(self) -> float:
        return self._range[2]


def _within(value: float, span: tuple[float, float, float]) -> bool:
    low, high, step = span
    if not low <= value <= high:
        return False
    return step <= 0 or abs((value - low) / step - round((value - low) / step)) * step < 0.5


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
        gate = search_gate
        if gate is not None:
            gate.wait(10.0)
        failure = search_failures.get(wanted.get("driver", ""))
        if failure is not None:
            raise RuntimeError(failure)
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
        self.rate_hz = spec.rates_hz[0] if spec.rates_hz else spec.rate_ranges[0][0]
        self.frequency_hz = spec.frequency_range_hz[0]
        self.correction_ppm = 0.0               # its CORR component, or its own correction
        self.bandwidth_hz: float | None = None
        self.gain_db = {name: spec.gain_values.get(name, span[0]) for name, span in spec.gains.items()}
        self.gain_writes: list[tuple[str, float]] = []      # every stage's gain set, in order, overall's included
        self.automatic_gain = False
        self.antenna = spec.antennas[0] if spec.antennas else ""
        self.setting_values = {setting.key: setting.value for setting in spec.settings}
        self.setting_writes: list[tuple[str, str]] = []     # every setting written, in order
        # Settings written and the stream's and device's ends, in order, as
        # "key=value", "setupStream", "closeStream" and "close".
        self.events: list[str] = []
        self.tunings: list[tuple[str, float]] = []      # every frequency component set, in order
        self.rate_tunings: list[int] = []   # how many components were set when each sample rate was
        self.streams: list[_Stream] = []
        self.closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        self.closed = True
        self.events.append("close")

    def getDriverKey(self) -> str:  # noqa: N802
        return self.spec.driver_key or self.spec.driver

    def getHardwareKey(self) -> str:  # noqa: N802
        return self.spec.hardware or f"{self.spec.driver}-stand-in"

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

    def getSampleRateRange(self, direction: int, channel: int) -> list[Range]:  # noqa: N802
        _receive_only("getSampleRateRange", direction)
        return [Range(*span) for span in self.spec.rate_ranges]

    def setSampleRate(self, direction: int, channel: int, rate: float) -> None:  # noqa: N802
        _receive_only("setSampleRate", direction)
        if rate not in self.spec.rates_hz and not any(_within(rate, span) for span in self.spec.rate_ranges):
            raise RuntimeError(f"setSampleRate({rate}) not supported")
        self.rate_hz = float(rate)
        self.rate_tunings.append(len(self.tunings))

    def getSampleRate(self, direction: int, channel: int) -> float:  # noqa: N802
        _receive_only("getSampleRate", direction)
        return self.rate_hz

    def listBandwidths(self, direction: int, channel: int) -> list[float]:  # noqa: N802
        _receive_only("listBandwidths", direction)
        return list(self.spec.bandwidths_hz)

    def getBandwidthRange(self, direction: int, channel: int) -> list[Range]:  # noqa: N802
        _receive_only("getBandwidthRange", direction)
        return [Range(*span) for span in self.spec.bandwidth_ranges]

    def setBandwidth(self, direction: int, channel: int, bandwidth: float) -> None:  # noqa: N802
        _receive_only("setBandwidth", direction)
        if bandwidth not in self.spec.bandwidths_hz and not any(_within(bandwidth, span)
                                                                for span in self.spec.bandwidth_ranges):
            raise RuntimeError(f"setBandwidth({bandwidth}) not supported")
        self.bandwidth_hz = float(bandwidth)

    def getBandwidth(self, direction: int, channel: int) -> float:  # noqa: N802
        _receive_only("getBandwidth", direction)
        return self.bandwidth_hz or 0.0

    def listGains(self, direction: int, channel: int) -> list[str]:  # noqa: N802
        _receive_only("listGains", direction)
        return list(self.spec.gains)

    def hasGainMode(self, direction: int, channel: int) -> bool:  # noqa: N802
        _receive_only("hasGainMode", direction)
        return self.spec.gain_mode

    def setGainMode(self, direction: int, channel: int, automatic: bool) -> None:  # noqa: N802
        _receive_only("setGainMode", direction)
        if not self.spec.gain_mode:
            raise RuntimeError("setGainMode: no automatic gain")
        self.automatic_gain = bool(automatic)

    def getGainRange(self, direction: int, channel: int, name: str | None = None) -> Range:  # noqa: N802
        """A stage's range, or the overall gain's: SoapySDR's sum of the stages'."""
        _receive_only("getGainRange", direction)
        if name is None:
            return Range(sum(span[0] for span in self.spec.gains.values()),
                         sum(span[1] for span in self.spec.gains.values()))
        return Range(*self.spec.gains[name])

    def setGain(self, direction: int, channel: int, *args: Any) -> None:  # noqa: N802
        """A stage's gain, ``(name, value)``, within its range and on its
        steps, rounded down as libhackrf does; or the overall gain,
        ``(value,)``, spread across the stages by the module's own rule or
        SoapySDR's: each stage in turn takes what the next ones leave."""
        _receive_only("setGain", direction)
        if len(args) == 2:
            self._set_stage(str(args[0]), float(args[1]))
            return
        if len(args) != 1:
            raise RuntimeError("setGain takes a value, or a stage's name and a value")
        value = float(args[0])
        if self.spec.overall_gain is not None:
            for name, gain in self.spec.overall_gain(value).items():
                self._set_stage(name, gain)
            return
        names = list(self.spec.gains)
        remaining = value
        for index, name in enumerate(names):
            low, high, _ = self.spec.gains[name]
            later = sum(self.spec.gains[other][0] for other in names[index + 1:])
            self._set_stage(name, min(max(remaining - later, low), high))
            remaining -= self.gain_db[name]

    def _set_stage(self, name: str, value: float) -> None:
        if name in self.spec.refuses:
            raise RuntimeError(f"setGain({name}) refused")
        low, high, step = self.spec.gains[name]
        value = min(max(value, low), high)
        if step > 0:
            value = low + math.floor((value - low) / step + 1e-9) * step
        self.gain_db[name] = value
        self.gain_writes.append((name, value))

    def getGain(self, direction: int, channel: int, name: str | None = None) -> float:  # noqa: N802
        """A stage's gain, or the overall gain: the stages' sum."""
        _receive_only("getGain", direction)
        if name is None:
            return sum(self.gain_db.values())
        return self.gain_db[name]

    def listAntennas(self, direction: int, channel: int) -> list[str]:  # noqa: N802
        _receive_only("listAntennas", direction)
        return list(self.spec.antennas)

    def getAntenna(self, direction: int, channel: int) -> str:  # noqa: N802
        _receive_only("getAntenna", direction)
        return self.antenna

    def setAntenna(self, direction: int, channel: int, name: str) -> None:  # noqa: N802
        _receive_only("setAntenna", direction)
        if "antenna" in self.spec.refuses or name not in self.spec.antennas:
            raise RuntimeError(f"setAntenna({name}) refused")
        self.antenna = name

    def _settings(self, channel: bool) -> dict[str, SettingSpec]:
        return {setting.key: setting for setting in self.spec.settings if setting.channel == channel}

    def getSettingInfo(self, *args: Any) -> list[ArgInfo]:  # noqa: N802
        """The device's settings, ``()``, or a channel's, ``(direction, channel)``."""
        if args:
            _receive_only("getSettingInfo", args[0])
        return [setting.info() for setting in self._settings(bool(args)).values()]

    def readSetting(self, *args: Any) -> str:  # noqa: N802
        """``(key)`` for the device's, ``(direction, channel, key)`` for a channel's."""
        if len(args) == 3:
            _receive_only("readSetting", args[0])
        key = str(args[-1])
        if key not in self._settings(len(args) == 3):
            return ""       # as SoapySDR's default
        return self.setting_values[key]

    def writeSetting(self, *args: Any) -> None:  # noqa: N802
        """``(key, value)`` for the device's, ``(direction, channel, key,
        value)`` for a channel's: a value of the setting's type, among its
        options or within its range, as text."""
        if len(args) == 4:
            _receive_only("writeSetting", args[0])
        key, value = str(args[-2]), args[-1]
        setting = self._settings(len(args) == 4).get(key)
        if not isinstance(value, str):
            raise TypeError("writeSetting takes the value as text")
        if setting is None or key in self.spec.refuses:
            raise RuntimeError(f"writeSetting({key}) refused")
        if setting.type == ArgInfo.BOOL and value not in ("true", "false"):
            raise RuntimeError(f"writeSetting({key}, {value}): not a boolean")
        if setting.options and value not in setting.options:
            raise RuntimeError(f"writeSetting({key}, {value}): not an option")
        if setting.range is not None and not setting.range[0] <= float(value) <= setting.range[1]:
            raise RuntimeError(f"writeSetting({key}, {value}): out of range")
        self.setting_values[key] = value
        self.setting_writes.append((key, value))
        self.events.append(f"{key}={value}")

    def listFrequencies(self, direction: int, channel: int) -> list[str]:  # noqa: N802
        _receive_only("listFrequencies", direction)
        return ["RF", "CORR"] if self.spec.correction == "corr" else ["RF"]

    def hasFrequencyCorrection(self, direction: int, channel: int) -> bool:  # noqa: N802
        _receive_only("hasFrequencyCorrection", direction)
        return self.spec.correction != "none"

    def setFrequencyCorrection(self, direction: int, channel: int, value: float) -> None:  # noqa: N802
        _receive_only("setFrequencyCorrection", direction)
        if self.spec.correction == "none":
            raise RuntimeError("setFrequencyCorrection: no correction")
        self._set_component("CORR", float(value))

    def getFrequencyCorrection(self, direction: int, channel: int) -> float:  # noqa: N802
        _receive_only("getFrequencyCorrection", direction)
        return self.correction_ppm

    def setFrequency(self, direction: int, channel: int, *call: Any) -> None:  # noqa: N802
        """The overall frequency, ``(frequency[, args])``, spread across the
        components as SoapySDR 0.8 does: the first takes it, each next one
        the remainder, unless ``args`` gives its value; or one component,
        ``(name, frequency[, args])``."""
        _receive_only("setFrequency", direction)
        if call and isinstance(call[0], str):
            self._set_component(call[0], float(call[1]))
            return
        remainder = float(call[0])
        args = _kwargs(call[1] if len(call) > 1 else None)
        for name in self.listFrequencies(direction, channel):
            given = args.get(name)
            self._set_component(name, float(given) if given not in (None, "DEFAULT") else remainder)
            remainder -= self.getFrequency(direction, channel, name)

    def _set_component(self, name: str, value: float) -> None:
        if name == "RF":
            low, high = self.spec.frequency_range_hz
            if not low <= value <= high:
                raise RuntimeError(f"setFrequency({value}) out of range")
            self.frequency_hz = value
        elif name == "CORR" and self.spec.correction != "none":
            self.correction_ppm = value
        else:
            raise RuntimeError(f"no frequency component {name}")
        self.tunings.append((name, value))

    def getFrequency(self, direction: int, channel: int, name: str | None = None) -> float:  # noqa: N802
        _receive_only("getFrequency", direction)
        if name == "CORR":
            return self.correction_ppm
        return self.frequency_hz

    def getFrequencyRange(self, direction: int, channel: int, name: str | None = None) -> list[Range]:  # noqa: N802
        _receive_only("getFrequencyRange", direction)
        return [Range(*self.spec.frequency_range_hz)]

    def setupStream(self, direction: int, sample_format: str, channels: Sequence[int] = (0,),  # noqa: N802
                    args: Mapping[str, str] | None = None) -> _Stream:
        _receive_only("setupStream", direction)
        if sample_format not in self.getStreamFormats(direction, 0):
            raise RuntimeError(f"setupStream: format {sample_format} not supported")
        if sample_format not in _FORMATS:
            raise RuntimeError(f"the stand-in has no samples in {sample_format}")
        if self.spec.fail_stream:
            raise RuntimeError("setupStream: the device is busy")
        stream = _Stream(sample_format, channels)
        self.streams.append(stream)
        self.events.append("setupStream")
        return stream

    def getStreamMTU(self, stream: _Stream) -> int:  # noqa: N802
        return self.spec.mtu

    def activateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0, numElems: int = 0) -> int:  # noqa: N802,N803
        stream.active = True
        stream.started = clock()
        stream.position = 0
        return 0

    def deactivateStream(self, stream: _Stream, flags: int = 0, timeNs: int = 0) -> int:  # noqa: N802,N803
        if self.spec.stop_gate is not None:
            self.spec.stop_gate.wait(10.0)
        stream.active = False
        return 0

    def closeStream(self, stream: _Stream) -> None:  # noqa: N802
        stream.active = False
        stream.closed = True
        self.events.append("closeStream")

    def readStream(self, stream: _Stream, buffs: Sequence[np.ndarray], numElems: int, flags: int = 0,  # noqa: N802,N803
                   timeoutUs: int = 100_000) -> StreamResult:  # noqa: N803
        """Up to ``numElems`` samples into ``buffs[0]``, those the device
        made since the previous read, waiting up to ``timeoutUs`` for a few;
        a scripted event instead, at its read."""
        if self.closed or stream.closed or not stream.active:
            return StreamResult(SOAPY_SDR_STREAM_ERROR)
        if not self.spec.present:
            if self.spec.unplugged_reads == "timeout":
                sleep(timeoutUs / 1e6)
                return StreamResult(SOAPY_SDR_TIMEOUT)
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
        if event == "skip":
            stream.position += self.spec.drop       # the next samples it makes, lost: the reads wait for them
        wanted = min(int(numElems), self.spec.mtu)
        deadline = clock() + timeoutUs / 1e6
        while True:
            made = int((clock() - stream.started) * self.rate_hz)
            if made - stream.position > self.spec.capacity_s * self.rate_hz:
                lost = made - stream.position - int(self.spec.capacity_s * self.rate_hz)
                stream.position += lost
                return StreamResult(SOAPY_SDR_OVERFLOW, SOAPY_SDR_END_ABRUPT)
            ready = made - stream.position
            if ready >= min(wanted, self.spec.mtu // 4) or (ready > 0 and clock() >= deadline):
                break
            if clock() >= deadline:
                return StreamResult(SOAPY_SDR_TIMEOUT)
            sleep(min(0.002, max(0.0, deadline - clock())))
        count = min(wanted, ready)
        start = stream.position
        _fill(buffs[0], stream.format, self._samples(start, count), self.spec.full_scale)
        stream.position += count
        result_flags = 0
        time_ns = 0
        if self.spec.timestamps:
            result_flags |= SOAPY_SDR_HAS_TIME
            time_ns = round(start * self.spec.timestamp_factor * 1e9 / self.rate_hz)
        return StreamResult(count, result_flags, time_ns)

    def _samples(self, start: int, count: int) -> np.ndarray:
        """What the device gives from index ``start``: the tone, or the
        carrier where its tuning, in steps of its tuner's, and its sampling
        clock, both off by its crystal and corrected by its own correction,
        put it."""
        if self.spec.carrier_hz is None:
            return samples(start, count)
        step = self.spec.tuning_step_hz
        tuned = math.floor(self.frequency_hz / step) * step if step else self.frequency_hz
        scale = (1 + self.spec.crystal_ppm / 1e6) / (1 + self.correction_ppm / 1e6)
        cycles = (self.spec.carrier_hz - tuned * scale) / (self.rate_hz * scale)
        phase = 2 * np.pi * ((cycles * (start + np.arange(count, dtype=np.float64))) % 1.0)
        return (0.5 * np.exp(1j * phase)).astype(np.complex64)

    def readStreamStatus(self, stream: _Stream, timeoutUs: int = 100_000) -> StreamResult:  # noqa: N802,N803
        return StreamResult(SOAPY_SDR_TIMEOUT)

    def writeStream(self, stream: Any, buffs: Any, numElems: int, flags: int = 0, timeNs: int = 0,  # noqa: N802,N803
                    timeoutUs: int = 100_000) -> StreamResult:  # noqa: N803
        transmit_requests.append("writeStream")
        raise TransmitRequested("writeStream: the stand-in receives only")


def _hackrf_overall(value: float) -> dict[str, float]:
    """SoapyHackRF's spread of an overall gain, as remembered: LNA and VGA
    up to 51 dB, then the amplifier on, the rest shared."""
    gain = int(value)
    if gain <= 0:
        return {"LNA": 0.0, "AMP": 0.0, "VGA": 0.0}
    amp = 0 if gain <= 20 + 31 else 14
    rest = gain - amp
    vga = ((rest // 3) & ~1) if gain <= 20 + 31 + 14 else int(rest * 40 / 62)
    return {"LNA": float(rest - vga), "AMP": float(amp), "VGA": float(vga)}


def hackrf_one(**changes: Any) -> DeviceSpec:
    """A HackRF One as SoapyHackRF declares one: its stages LNA, AMP and
    VGA, its amplifier on at opening here (whatever the default, Radiolack
    starts it off), its overall gain spread as SoapyHackRF does, one
    antenna socket and its antenna power, ``bias_tx``."""
    values = dict(serial="0000000000000000a06063c8234e925f", label="HackRF One #0 a06063c8", hardware="HackRF One",
                  native=SOAPY_SDR_CS8, formats=(SOAPY_SDR_CS8, SOAPY_SDR_CS16, SOAPY_SDR_CF32),
                  rates_hz=(), rate_ranges=((1e6, 20e6, 0.0),), frequency_range_hz=(1e6, 6e9),
                  bandwidths_hz=(1.75e6, 2.5e6, 3.5e6, 5e6, 5.5e6, 6e6, 7e6, 8e6),
                  gains={"LNA": (0.0, 40.0, 8.0), "AMP": (0.0, 14.0, 14.0), "VGA": (0.0, 62.0, 2.0)},
                  gain_values={"LNA": 16.0, "AMP": 14.0, "VGA": 16.0}, overall_gain=_hackrf_overall,
                  antennas=("TX/RX",),
                  settings=(SettingSpec("bias_tx", "false", "Antenna Bias", "Antenna port power control."),))
    return add("hackrf", **{**values, **changes})


def rspdx_r2(**changes: Any) -> DeviceSpec:
    """An RSPdx-R2 as SoapySDRPlay3 declares one: gain reductions IFGR and
    RFGR, automatic gain, antennas A, B and C, its notch filters, its high
    dynamic range mode, its IQ correction, its AGC set point and its antenna
    power, ``biasT_ctrl``."""
    values = dict(serial="2305012345", label="SDRplay Dev0 RSPdx-R2 2305012345", hardware="RSPdx-R2",
                  native=SOAPY_SDR_CS16, formats=(SOAPY_SDR_CS16, SOAPY_SDR_CF32), full_scale=32767.0,
                  rates_hz=(62.5e3, 125e3, 250e3, 500e3, 1e6, 2e6, 2.048e6, 3e6, 4e6, 5e6, 6e6, 7e6, 8e6, 9e6, 10e6),
                  frequency_range_hz=(1e3, 2e9), bandwidths_hz=(200e3, 300e3, 600e3, 1.536e6, 5e6, 6e6, 7e6, 8e6),
                  gains={"IFGR": (20.0, 59.0, 1.0), "RFGR": (0.0, 27.0, 1.0)},
                  gain_values={"IFGR": 40.0, "RFGR": 4.0}, gain_mode=True,
                  antennas=("Antenna A", "Antenna B", "Antenna C"),
                  settings=(SettingSpec("rfnotch_ctrl", "false", "RF Notch Filter", "RF Notch Filter Control"),
                            SettingSpec("dabnotch_ctrl", "false", "DAB Notch Filter", "DAB Notch Filter Control"),
                            SettingSpec("hdr_ctrl", "false", "HDR Mode", "High Dynamic Range Mode Control"),
                            SettingSpec("biasT_ctrl", "false", "Bias-T Enable", "BiasT Control"),
                            SettingSpec("iqcorr_ctrl", "true", "IQ Correction", "IQ Correction Control"),
                            SettingSpec("agc_setpoint", "-30", "AGC Setpoint", "AGC Setpoint (dBfs)",
                                        type=ArgInfo.INT, range=(-60.0, 0.0, 1.0))))
    return add("sdrplay", **{**values, **changes})


def rtl_sdr(**changes: Any) -> DeviceSpec:
    """An RTL-SDR as SoapyRTLSDR 0.3.3 declares one: driver key RTLSDR, one
    gain stage, TUNER, with automatic gain, one antenna socket, RX, a CORR
    frequency component, direct sampling as a text setting with options,
    its other settings and its antenna power, ``biastee``. The options'
    names and the descriptions are the module's code's, as remembered."""
    values = dict(serial="00000001", label="Generic RTL2832U OEM :: 00000001", hardware="R820T", driver_key="RTLSDR",
                  native=SOAPY_SDR_CS8, formats=(SOAPY_SDR_CS8, SOAPY_SDR_CS16, SOAPY_SDR_CF32),
                  rates_hz=(250e3, 1.024e6, 1.536e6, 1.792e6, 1.92e6, 2.048e6, 2.16e6, 2.56e6, 2.88e6, 3.2e6),
                  frequency_range_hz=(24e6, 1764e6), gains={"TUNER": (0.0, 49.6, 0.0)}, gain_mode=True,
                  correction="corr", timestamp_factor=2.0, antennas=("RX",),
                  settings=(SettingSpec("direct_samp", "0", "Direct Sampling", "RTL-SDR Direct Sampling Mode",
                                        type=ArgInfo.STRING, options=("0", "1", "2"),
                                        option_names=("Off", "I-ADC", "Q-ADC")),
                            SettingSpec("offset_tune", "false", "Offset Tune", "RTL-SDR Offset Tuning Mode"),
                            SettingSpec("iq_swap", "false", "I/Q Swap", "RTL-SDR I/Q Swap Mode"),
                            SettingSpec("digital_agc", "false", "Digital AGC", "RTL-SDR digital AGC Mode"),
                            SettingSpec("biastee", "false", "Bias Tee", "RTL-SDR Blog V.3 Bias-Tee Mode")))
    return add("rtlsdr", **{**values, **changes})


def samples(start: int, count: int) -> np.ndarray:
    """The samples a stand-in device gives from index ``start``, as
    complex64 at full scale 1: a test compares what it read with these.
    The tone's period repeated, which costs little for long reads."""
    return np.resize(np.roll(_TONE, -(start % _PERIOD)), count)


def _fill(buffer: np.ndarray, sample_format: str, values: np.ndarray, native_scale: float | None = None) -> None:
    """``values`` into ``buffer`` in ``sample_format``, an integer format at
    ``native_scale`` if given, as a module whose integers have another full
    scale gives them."""
    component, full_scale = _FORMATS[sample_format]
    count = len(values)
    if native_scale is not None and component is not np.float32:
        full_scale = native_scale
    if component is np.float32:
        native = values.view(np.float32)
    else:
        native = np.rint(values.view(np.float32) * full_scale).astype(component)
    target = buffer.reshape(-1).view(component)
    if len(target) < 2 * count:
        raise ValueError(f"a buffer of {len(target)} components cannot take {count} samples")
    target[:2 * count] = native
