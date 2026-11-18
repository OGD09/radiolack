"""SoapySDR sources: receivers that SoapySDR's modules read, such as an
SDRplay RSPdx-R2, a HackRF One or an RTL-SDR (later-milestones.md,
section 1, Any receiver).

The ``SoapySDR`` Python module (Debian's and Ubuntu's python3-soapysdr) is
imported only when a SoapySDR source first opens its device: Radiolack
runs without it, and a source that finds it missing waits, saying which
package brings it, and tries again.

A device is named by SoapySDR device arguments, ``KEY=VALUE`` separated by
commas, which must name a driver (``parse_args``): a search without one
runs every module's, SoapyRemote's included, which asks avahi-daemon to
search the network. The source opens its device when the engine first
reads, searches with the arguments, opens the first device found and
learns its serial number: from then on its settings are saved under its
driver and serial (``settings_key``), and it opens again by them
(``reopen``), not by a position on the bus that changes. Like the rtl_tcp
sources, it releases the device once nobody has read for a while, so that
the device stays free for other programs while no interface listens.

Receive only: the source sets up one receive stream on channel 0, and
nothing else. Its range, its sample rates and, where it has one, its gain
come from the device, and the source state describes them as the other
sources' do. Rates are those the device lists, or lies within the ranges
it gives, up to 8 MS/s (spike 7): a rate between two measured ones costs
no more than the higher. An analogue bandwidth, where the device has one,
is the nearest at or below 0.8 times the sample rate. A device with one
gain stage takes ``gain_db`` as that stage's gain, or automatic gain where
it offers it; a device with several, such as a HackRF's three, keeps its
own gains until the gains per stage come (pull request 4).

Samples come in the device's native format among CS8, CS16 and CF32: the
first of those it lists, as SoapySDR 0.8's binding cannot ask for the
native one, with the full scale that ``FULL_SCALES`` gives for the drivers
whose native format was read in their code; any other device reads CF32,
which its module scales itself. A thread of the source's own reads the
stream into a ``NativeBuffer``, which keeps half a second and drops the
oldest when the engine falls behind, as the rtl_tcp client does; the
engine takes its blocks from it and converts them to complex64. The
thread alone calls the device: it applies the settings between its reads.
After a change of frequency, sample rate or correction it drops what
arrives during the next 0.4 s, then labels the samples with the new
setting.

A device that cannot be found or opened, that is unplugged, whose stream
fails or that stops answering is a waiting state with the reason in its
detail, tried again with a growing delay. However the source stops, by its
close or because nobody reads, the thread deactivates and closes the
stream and releases the device itself, in its own ``finally``: a close
that comes while it stops does not cut that short.

What the source reports: the overflows that the device reports, the
samples that the buffer dropped for an engine behind, and the blocks that
took the engine longer than their period, counted between the end of one
read and the next, are logged at most once a minute, with their counts
since the previous line. The device's timestamps are not used: not every
module counts samples with them (SoapySDR's rtlsdr module 0.3.3 counts the
I and Q bytes, spike 7), and the source needs them for nothing that the
overflows and its own buffer do not tell; ``tools/measure_reader.py``
reads them, with that check, where a loss must be measured.

The frequency correction is the scaling of Coverage and precision
(correction.py): a device that declares a correction of its own
(``hasFrequencyCorrection``, or a ``CORR`` frequency component, as
SoapySDR's own default declares it) takes the nearest whole ppm, as
librtlsdr does, and Radiolack the rest, by the frequency it asks for, by
the samples' label, and by ``rate_scale``, taking the device's correction
to correct its sampling clock too, as librtlsdr's does; a device without
one takes none, and Radiolack the whole of it. No tuner grid and no
driver's computation here: those belong to the rtl_tcp sources' R820T and
R828D.

A converter in front of the receiver (converter.py) moves the
frequencies as with the rtl_tcp sources. Not here yet: antenna power,
antenna sockets, the device's other settings and shortwave inputs
(pull request 4), and I/Q recordings, which ``tap`` refuses until they
take the device's own format (pull request 5).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any

import numpy as np

from .converter import DEFAULT_LO_HZ, HIGHEST_HZ, Conversion, checked_converter, checked_lo, mhz
from .correction import checked_ppm, corrected_hz, whole_ppm
from .rtltcp import (BUFFER_S, DEFAULT_CENTRE_HZ, DEFAULT_SAMPLE_RATE_HZ, IDLE_S, READ_WAIT_S, RETRY_S, SETTLE_S,
                     TIMEOUT_S, finite_number)

MODULE = "SoapySDR"
PACKAGE = "python3-soapysdr"    # what brings the module on Debian and Ubuntu
MISSING = f"the SoapySDR Python module is not installed (on Debian and Ubuntu: {PACKAGE})"
MAX_RATE_HZ = 8_000_000.0       # the highest rate offered (spike 7)
BANDWIDTH_SHARE = 0.8           # the analogue bandwidth: at most this share of the sample rate
TIMEOUT_US = 100_000            # a read's wait for samples, which bounds how soon the thread sees a change
FAILED_READS = 10               # stream errors in a row that end a session
REPORT_S = 60.0                 # the losses are logged at most this often
STOP_S = 5.0                    # the reading thread stops within a read and a stop; a warning after this
MAX_ARGS = 256                  # characters in the device arguments
MAX_GAINS = 128                 # gains offered at most, the steps widened beyond
# The full scale of the native format of the drivers whose modules' code
# was read (later-milestones.md, section 1, As built): bladeRF's CS16 has
# 12 bits. Another driver, or another format, reads CF32.
FULL_SCALES = {"rtlsdr": ("CS8", 128.0), "hackrf": ("CS8", 128.0), "sdrplay": ("CS16", 32767.0),
               "airspy": ("CS16", 32767.0), "bladerf": ("CS16", 2048.0)}
# Rates offered by a device that gives ranges without a list, where they fit.
COMMON_RATES_HZ = (250_000.0, 1_024_000.0, 2_048_000.0, 2_400_000.0, 3_200_000.0, 4_000_000.0, 5_000_000.0,
                   6_000_000.0, 8_000_000.0)
# In the order the engine restores them, as for the rtl_tcp sources.
_SETTINGS = ("converter_lo_hz", "converter", "sample_rate_hz", "centre_hz", "gain_db", "ppm")
_ANY_HZ = ((1.0, HIGHEST_HZ),)  # until the device is known
_CHECK_S = 0.1                  # how often the source checks whether to stop its thread
_TEXT_CHARS = 200               # of a module's message, in a detail

log = logging.getLogger(__name__)


# ------------------------------------------------------------- samples


@dataclass(frozen=True)
class Format:
    """A device's sample format, as SoapySDR names it."""

    name: str
    component: type[np.generic]     # the type of each of I and Q
    full_scale: float               # the value of a full-scale component, nominally

    @property
    def sample_bytes(self) -> int:
        return 2 * np.dtype(self.component).itemsize


FORMATS = {kind.name: kind for kind in (
    Format("CS8", np.int8, 128.0), Format("CS16", np.int16, 32768.0), Format("CF32", np.float32, 1.0))}


def to_complex(data: bytes | bytearray | memoryview, sample_format: Format, full_scale: float | None = None
               ) -> np.ndarray:
    """Samples in ``sample_format``, I then Q, as complex64 scaled to full
    scale 1, by ``full_scale`` if given, otherwise the format's."""
    components = np.frombuffer(data, sample_format.component)
    if sample_format.component is np.float32:
        samples = components.copy()
    else:
        samples = components.astype(np.float32)
    scale = sample_format.full_scale if full_scale is None else full_scale
    if scale != 1.0:
        samples *= np.float32(1 / scale)
    return samples.view(np.complex64)


class NativeBuffer:
    """The samples a reading thread brings in for the engine, kept in the
    device's format as bytes: at most ``capacity`` samples, or what the
    engine waits for at once if more; past that the oldest are dropped, and
    counted, so that the engine stays current when it falls behind, as the
    rtl_tcp client's buffer does (rtltcp.py)."""

    def __init__(self, sample_bytes: int, capacity: int) -> None:
        if sample_bytes < 1 or capacity < 1:
            raise ValueError("a buffer holds at least one sample of at least one byte")
        self.sample_bytes = sample_bytes
        self.capacity = capacity
        self.dropped = 0            # samples dropped, the oldest
        self._data = bytearray()
        self._wanted = 0            # samples the engine waits for
        self._ready = threading.Condition()

    def __len__(self) -> int:
        with self._ready:
            return len(self._data) // self.sample_bytes

    def put(self, data: bytes | bytearray | memoryview) -> None:
        """Keep whole samples: ``data`` holds a whole number of them."""
        if len(data) % self.sample_bytes:
            raise ValueError(f"{len(data)} bytes are not whole samples of {self.sample_bytes} bytes")
        with self._ready:
            self._data += data
            limit = max(self.capacity, self._wanted) * self.sample_bytes
            excess = len(self._data) - limit
            if excess > 0:
                del self._data[:excess]
                self.dropped += excess // self.sample_bytes
            if len(self._data) >= self._wanted * self.sample_bytes:
                self._ready.notify_all()

    def take(self, count: int, timeout_s: float) -> bytes | None:
        """The oldest ``count`` samples, or None when they did not come
        within ``timeout_s``."""
        if count < 1:
            raise ValueError(f"cannot take {count} samples")
        size = count * self.sample_bytes
        with self._ready:
            self._wanted = count
            try:
                if not self._ready.wait_for(lambda: len(self._data) >= size, timeout_s):
                    return None
            finally:
                self._wanted = 0
            return self._take(size)

    def take_now(self, count: int) -> bytes | None:
        """The oldest ``count`` samples if they are there, without waiting;
        otherwise None, and the buffer keeps at least ``count`` samples
        until they are taken, for a reader that waits for them elsewhere."""
        if count < 1:
            raise ValueError(f"cannot take {count} samples")
        size = count * self.sample_bytes
        with self._ready:
            if len(self._data) < size:
                self._wanted = count
                return None
            self._wanted = 0
            return self._take(size)

    def clear(self) -> None:
        with self._ready:
            self._data.clear()

    def _take(self, size: int) -> bytes:
        data = bytes(self._data[:size])
        del self._data[:size]
        return data


def native_format(soapy: ModuleType, device: Any) -> str | None:
    """The device's native format among CS8, CS16 and CF32: as
    ``getNativeStreamFormat`` gives it where the binding can call it, or
    else the first of those the device lists; None when it lists none."""
    receive = soapy.SOAPY_SDR_RX
    try:
        native = device.getNativeStreamFormat(receive, 0)
    except TypeError:
        # SoapySDR 0.8's binding takes the full scale as a C++ reference,
        # which Python cannot pass: the call cannot be made.
        native = None
    if isinstance(native, (tuple, list)) and native:
        native = native[0]
    if isinstance(native, str) and native in FORMATS:
        return native
    for listed in device.getStreamFormats(receive, 0):
        if listed in FORMATS:
            return str(listed)
    return None


def stream_format(soapy: ModuleType, device: Any, driver: str) -> tuple[Format, float]:
    """The format to read the device in, and its full scale: the native
    one with the full scale of ``FULL_SCALES`` for the drivers there, CF32
    otherwise; a native integer format at its nominal full scale for a
    device that offers neither. Raises ValueError for a device that gives
    none of CS8, CS16 and CF32."""
    native = native_format(soapy, device)
    known = FULL_SCALES.get(driver)
    if known is not None and known[0] == native:
        return FORMATS[native], known[1]
    if "CF32" in device.getStreamFormats(soapy.SOAPY_SDR_RX, 0):
        return FORMATS["CF32"], 1.0
    if native is not None:
        log.warning("the %s device's full scale is not known: its levels in dBFS take %s's nominal %g",
                    driver, native, FORMATS[native].full_scale)
        return FORMATS[native], FORMATS[native].full_scale
    raise ValueError("the device gives none of the formats CS8, CS16 and CF32")


def load_module() -> tuple[ModuleType | None, str]:
    """The SoapySDR module, or None and why it cannot be used."""
    try:
        return importlib.import_module(MODULE), ""
    except ImportError as error:
        log.debug("cannot import %s: %s", MODULE, error)
        return None, MISSING
    except Exception as error:      # its library failed to load, for instance
        return None, f"the SoapySDR Python module cannot be loaded: {_text(error)}"


def module_problem() -> str | None:
    """Why SoapySDR sources cannot open, or None when its module loads."""
    module, problem = load_module()
    return None if module is not None else problem


# ------------------------------------------------------------- arguments


def parse_args(text: str) -> dict[str, str]:
    """SoapySDR device arguments, ``KEY=VALUE`` separated by commas, such
    as ``driver=rtlsdr`` or ``driver=sdrplay,serial=1234``. Raises
    ValueError, saying why, unless they name a driver: a search without one
    runs every module's, which may search the network."""
    if not isinstance(text, str):
        raise ValueError("the device arguments must be text")
    text = text.strip()
    if not text:
        raise ValueError("no device arguments given")
    if len(text) > MAX_ARGS:
        raise ValueError(f"the device arguments are longer than {MAX_ARGS} characters")
    if not text.isprintable():
        raise ValueError("the device arguments must be printable text")
    pairs: dict[str, str] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, equals, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not equals or not key:
            raise ValueError(f"device arguments are KEY=VALUE separated by commas, such as driver=rtlsdr, "
                             f"not {part.strip()!r}")
        if key in pairs:
            raise ValueError(f"{key} is given twice")
        pairs[key] = value
    if not pairs.get("driver"):
        raise ValueError("the device arguments must name a driver, such as driver=rtlsdr: without one, "
                         "SoapySDR searches with every module, the network included")
    return pairs


def args_text(pairs: Mapping[str, str]) -> str:
    """Device arguments as SoapySDR writes them, the driver first."""
    ordered = {"driver": pairs["driver"], **pairs}
    return ",".join(f"{key}={value}" for key, value in ordered.items())


# ------------------------------------------------------------- the device


Range = tuple[float, float, float]      # lowest, highest and step (0: any value between)


@dataclass(frozen=True)
class Capabilities:
    """What a device offers, read once it opens."""

    driver: str
    serial: str
    hardware: str                   # such as "R820T" or "HackRF One": its hardware key
    frequency_ranges: tuple[tuple[float, float], ...]
    rates_hz: tuple[float, ...]     # those it lists, lowest first
    rate_ranges: tuple[Range, ...]
    bandwidths_hz: tuple[float, ...]
    bandwidth_ranges: tuple[Range, ...]
    gain_stage: str | None          # its one gain stage; None without one, or with several
    gain_range: Range | None
    gain_mode: bool                 # whether it offers automatic gain
    gain_db: float | None           # its stage's gain when it opened
    correction: bool                # whether it takes a correction of its own
    corr_component: bool            # whether that is a CORR frequency component
    sample_format: Format
    full_scale: float

    def offers(self, rate_hz: float) -> bool:
        """Whether ``rate_hz`` is one of the rates offered."""
        if not 0 < rate_hz <= MAX_RATE_HZ:
            return False
        return (any(abs(rate_hz - listed) < 0.5 for listed in self.rates_hz)
                or any(_within_range(rate_hz, span) for span in self.rate_ranges))

    def menu_rates(self) -> list[float]:
        """The rates an interface lists: those the device lists, or the
        common ones within its ranges, up to MAX_RATE_HZ."""
        listed = sorted({rate for rate in self.rates_hz if self.offers(rate)})
        return listed or [rate for rate in COMMON_RATES_HZ if self.offers(rate)]

    def nearest_rate(self, rate_hz: float) -> float:
        """``rate_hz`` if offered, otherwise the nearest rate offered."""
        if self.offers(rate_hz):
            return rate_hz
        candidates = [*self.menu_rates()]
        for low, high, step in self.rate_ranges:
            within = min(max(rate_hz, low), min(high, MAX_RATE_HZ))
            if step > 0:        # the steps on either side
                steps = (within - low) / step
                around = (low + math.floor(steps) * step, low + math.ceil(steps) * step)
            else:
                around = (within,)
            candidates += [rate for rate in (low, min(high, MAX_RATE_HZ), *around) if self.offers(rate)]
        if not candidates:
            raise ValueError(f"the device offers no sample rate up to {MAX_RATE_HZ / 1e6:g} MS/s")
        return min(candidates, key=lambda rate: (abs(rate - rate_hz), rate))

    def rates_text(self) -> str:
        """The rates offered, in a few words."""
        parts = [f"{rate / 1e6:g}" for rate in sorted(set(self.rates_hz)) if self.offers(rate)]
        parts += [f"from {low / 1e6:g} to {min(high, MAX_RATE_HZ) / 1e6:g}" for low, high, _ in self.rate_ranges
                  if low <= MAX_RATE_HZ]
        return f"the sample rate must be one this device offers, up to {MAX_RATE_HZ / 1e6:g} MS/s: " \
               f"{', '.join(parts) or 'none'} MS/s"

    def bandwidth_for(self, rate_hz: float) -> float | None:
        """The analogue bandwidth for ``rate_hz``: the nearest at or below
        BANDWIDTH_SHARE of it, or the narrowest if all are wider; None for a
        device without one."""
        target = BANDWIDTH_SHARE * rate_hz
        values = list(self.bandwidths_hz)
        for low, high, step in self.bandwidth_ranges:
            value = min(max(target, low), high)
            if step > 0:
                value = low + math.floor((value - low) / step + 1e-9) * step
            values += [low, value]
        if not values:
            return None
        below = [value for value in values if value <= target + 0.5]
        return max(below) if below else min(values)

    def gains_db(self) -> tuple[float, ...]:
        """The gains offered for the one stage, lowest first, in its steps,
        or 1 dB without one; empty without such a stage."""
        if self.gain_stage is None or self.gain_range is None:
            return ()
        low, high, step = self.gain_range
        if not (math.isfinite(low) and math.isfinite(high)) or high < low:
            return ()
        step = step if step > 0 else 1.0
        count = math.floor((high - low) / step + 1e-9) + 1
        if count > MAX_GAINS:
            step = (high - low) / (MAX_GAINS - 1)
            count = MAX_GAINS
        values = [round(low + index * step, 2) for index in range(count)]
        if values[-1] < round(high, 2):
            values.append(round(high, 2))
        return tuple(values)

    @property
    def gain_settable(self) -> bool:
        return bool(self.gains_db())


def _within_range(value: float, span: Range) -> bool:
    low, high, step = span
    if not low - 0.5 <= value <= high + 0.5:
        return False
    if step <= 0:
        return True
    steps = (value - low) / step
    return abs(steps - round(steps)) * step < 0.5


def _ranges(values: Any) -> tuple[Range, ...]:
    """SoapySDR ranges as tuples, lowest first, those that are not finite
    or empty left out."""
    found = []
    for span in values or ():
        low, high, step = float(span.minimum()), float(span.maximum()), float(span.step())
        if math.isfinite(low) and math.isfinite(high) and low <= high and math.isfinite(step):
            found.append((low, high, max(step, 0.0)))
    return tuple(sorted(found))


def _optional(call: Callable[[], Any], default: Any) -> Any:
    """What a device's call gives, or ``default`` when the module, or its
    binding, does not have it."""
    try:
        return call()
    except (AttributeError, NotImplementedError):
        return default


def read_capabilities(soapy: ModuleType, device: Any, listing: Mapping[str, str]) -> Capabilities:
    """What ``device``, opened from ``listing``, offers on channel 0 in
    reception. Raises ValueError, or the module's own error, for a device
    that cannot be read."""
    receive = soapy.SOAPY_SDR_RX
    driver = str(_optional(device.getDriverKey, "") or listing.get("driver", ""))
    frequencies = tuple((low, high) for low, high, _ in _ranges(device.getFrequencyRange(receive, 0))
                        if high >= 1.0)
    if not frequencies:
        raise ValueError("the device gives no frequency range")
    rates = tuple(sorted({float(rate) for rate in _optional(lambda: device.listSampleRates(receive, 0), ())
                          if math.isfinite(float(rate)) and float(rate) > 0}))
    rate_ranges = _ranges(_optional(lambda: device.getSampleRateRange(receive, 0), ()))
    bandwidths = tuple(sorted({float(width) for width in _optional(lambda: device.listBandwidths(receive, 0), ())
                               if math.isfinite(float(width)) and float(width) > 0}))
    bandwidth_ranges = tuple(span for span in _ranges(_optional(lambda: device.getBandwidthRange(receive, 0), ()))
                             if span[1] > 0)
    stages = list(_optional(lambda: device.listGains(receive, 0), ()))
    stage = str(stages[0]) if len(stages) == 1 else None
    gain_range = gain_db = None
    gain_mode = False
    if stage is not None:
        spans = _ranges([device.getGainRange(receive, 0, stage)])
        gain_range = spans[0] if spans else None
        gain_mode = bool(_optional(lambda: device.hasGainMode(receive, 0), False))
        gain_db = float(device.getGain(receive, 0, stage))
    components = [str(name) for name in _optional(lambda: device.listFrequencies(receive, 0), ())]
    corr = "CORR" in components
    correction = bool(_optional(lambda: device.hasFrequencyCorrection(receive, 0), corr))
    sample_format, full_scale = stream_format(soapy, device, driver)
    capabilities = Capabilities(
        driver=driver, serial=str(listing.get("serial", "")), hardware=str(_optional(device.getHardwareKey, "")),
        frequency_ranges=frequencies, rates_hz=rates, rate_ranges=rate_ranges, bandwidths_hz=bandwidths,
        bandwidth_ranges=bandwidth_ranges, gain_stage=stage, gain_range=gain_range, gain_mode=gain_mode,
        gain_db=gain_db if gain_db is not None and math.isfinite(gain_db) else None, correction=correction,
        corr_component=corr, sample_format=sample_format, full_scale=full_scale)
    capabilities.nearest_rate(DEFAULT_SAMPLE_RATE_HZ)      # raises for a device without a rate offered
    return capabilities


# ------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    """What the device is asked to do."""

    centre_hz: float
    sample_rate_hz: float
    gain_db: float | None           # None: automatic gain, or the device's own
    ppm: float                      # frequency correction, in parts per million to the hundredth
    converter: str = "none"         # one of converter.CONVERTERS
    converter_lo_hz: float = DEFAULT_LO_HZ


@dataclass(frozen=True)
class Tuning:
    """How the device tunes for a setting: the frequency asked of it, the
    whole ppm it takes itself, where it then sits on the air, which labels
    the samples, and how much faster than its nominal rate it samples."""

    request_hz: float
    whole_ppm: int
    centre_hz: float
    scale: float


def _conversion(settings: Settings) -> Conversion:
    return Conversion(settings.converter, settings.converter_lo_hz)


def _device_ranges(capabilities: Capabilities | None) -> tuple[tuple[float, float], ...]:
    return _ANY_HZ if capabilities is None else capabilities.frequency_ranges


def _reach(settings: Settings, capabilities: Capabilities | None) -> list[tuple[float, float, str]]:
    """The centre frequencies on the air within reach: the device's, from
    half the sample rate at the lowest so that the span never goes below
    0 Hz, through the converter if there is one."""
    ranges = [(low, high, "") for low, high in _device_ranges(capabilities)]
    conversion = _conversion(settings)
    if not conversion.active:
        half = float(math.ceil(settings.sample_rate_hz / 2))
        ranges = [(max(low, half), high, label) for low, high, label in ranges if max(low, half) <= high]
    return conversion.reach(ranges, settings.sample_rate_hz)


def _within(centre_hz: float, reach: list[tuple[float, float, str]]) -> bool:
    return any(low <= centre_hz <= high for low, high, _ in reach)


def _reach_text(reach: list[tuple[float, float, str]], settings: Settings) -> str:
    """Why a centre frequency outside ``reach`` is refused."""
    if not reach:
        return "through this converter the device reaches nothing on the air"
    through = " through its converter" if _conversion(settings).active else ""
    ranges = " and ".join(f"from {mhz(low)} to {mhz(high)} MHz" for low, high, _ in reach)
    return f"the device tunes {ranges}{through}"


def _within_reach(settings: Settings, capabilities: Capabilities | None) -> Settings:
    """``settings`` with the centre frequency brought to the nearest one
    within reach; raises ValueError when there is none."""
    reach = _reach(settings, capabilities)
    if not reach:
        raise ValueError(_reach_text(reach, settings))
    centre = settings.centre_hz
    nearest = min((min(max(centre, low), high) for low, high, _ in reach), key=lambda value: abs(value - centre))
    if _within(round(nearest), reach):
        nearest = round(nearest)    # whole hertz, as the centre asked
    return replace(settings, centre_hz=float(nearest))


def changed(settings: Settings, changes: Mapping[str, object], capabilities: Capabilities | None) -> Settings:
    """``settings`` with ``changes``; raises ValueError for any invalid
    change. Until the device is known, any rate up to MAX_RATE_HZ, any
    frequency and any gain pass, which the device's first opening fits. A
    new sample rate without a new centre frequency brings the centre within
    the new reach; a new converter or oscillator keeps the device where it
    is tuned, and is refused when that is out of the new reach."""
    unknown = sorted(set(changes) - set(settable(capabilities)))
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(unknown)}")
    before = settings
    if "converter_lo_hz" in changes:
        settings = replace(settings, converter_lo_hz=checked_lo(changes["converter_lo_hz"]))
    if "converter" in changes:
        settings = replace(settings, converter=checked_converter(changes["converter"]))
    if "sample_rate_hz" in changes:
        rate = float(round(finite_number(changes["sample_rate_hz"], "the sample rate")))
        if capabilities is not None and not capabilities.offers(rate):
            raise ValueError(capabilities.rates_text())
        if not 0 < rate <= MAX_RATE_HZ:
            raise ValueError(f"the sample rate must be above 0 and at most {MAX_RATE_HZ / 1e6:g} MS/s")
        settings = replace(settings, sample_rate_hz=rate)
    reach = _reach(settings, capabilities)
    if "centre_hz" in changes:
        centre = float(round(finite_number(changes["centre_hz"], "the centre frequency")))
        if not _within(centre, reach):
            raise ValueError(_reach_text(reach, settings))
        settings = replace(settings, centre_hz=centre)
    elif _conversion(settings).key != _conversion(before).key:
        # The device stays where it is tuned: the signals stay, their frequencies change.
        centre = _conversion(settings).air(_conversion(before).tuned(before.centre_hz))
        if not _within(centre, reach):
            raise ValueError(f"through this converter the device's centre frequency would be {mhz(centre)} MHz "
                             f"on the air, out of reach: {_reach_text(reach, settings)}")
        settings = replace(settings, centre_hz=centre)
    elif "sample_rate_hz" in changes:
        settings = _within_reach(settings, capabilities)
    if "gain_db" in changes:
        value = changes["gain_db"]
        gain = None if value is None else finite_number(value, "the gain")
        if capabilities is not None:
            if gain is None and not capabilities.gain_mode:
                raise ValueError("this device has no automatic gain")
            if gain is not None:
                gain = _nearest(capabilities.gains_db(), gain)
        settings = replace(settings, gain_db=gain)
    if "ppm" in changes:
        settings = replace(settings, ppm=checked_ppm(changes["ppm"]))
    return settings


def fitted(settings: Settings, capabilities: Capabilities) -> Settings:
    """``settings`` brought within what the device offers: the nearest rate
    offered, a centre frequency within reach, a gain it offers, or its own
    without automatic gain, and no converter through which it would reach
    nothing."""
    settings = replace(settings, sample_rate_hz=capabilities.nearest_rate(settings.sample_rate_hz))
    if not _reach(settings, capabilities):
        log.warning("the converter is not used: through it, the %s device reaches nothing on the air",
                    capabilities.driver)
        settings = replace(settings, converter="none")
    gain = settings.gain_db
    gains = capabilities.gains_db()
    if not gains:
        gain = None
    elif gain is None and not capabilities.gain_mode:
        gain = _nearest(gains, capabilities.gain_db if capabilities.gain_db is not None else gains[0])
    elif gain is not None:
        gain = _nearest(gains, gain)
    return replace(_within_reach(settings, capabilities), gain_db=gain)


def settable(capabilities: Capabilities | None) -> list[str]:
    """The settings an interface may change: the gain until the device is
    known, then only for a device with one gain stage."""
    return [name for name in _SETTINGS
            if name != "gain_db" or capabilities is None or capabilities.gain_settable]


def tuning(settings: Settings, capabilities: Capabilities | None) -> Tuning:
    """How the device tunes for ``settings`` (module docstring): the
    frequency it tunes, through the converter, scaled for the part of the
    correction it does not take itself, within the device's range that
    holds it; the label, where that puts it on the air."""
    conversion = _conversion(settings)
    tuned = conversion.tuned(settings.centre_hz)
    whole = whole_ppm(settings.ppm, capabilities is not None and capabilities.correction)
    request = float(corrected_hz(tuned, settings.ppm, whole))
    for low, high in _device_ranges(capabilities):
        if low <= tuned <= high:
            request = min(max(request, low), high)
            break
    scale = (1 + settings.ppm / 1e6) / (1 + whole / 1e6)
    return Tuning(request, whole, conversion.air(request * scale), scale)


def _nearest(values: Sequence[float], value: float) -> float:
    return min(values, key=lambda candidate: abs(candidate - value))


def _text(error: BaseException) -> str:
    """A module's message, on one line and not too long."""
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS - 1] + "…"


# ------------------------------------------------------------- reporting


class Losses:
    """What the source reports as it runs: the device's overflows, the
    samples the buffer dropped for an engine behind, and the blocks that
    took the engine longer than their period; logged at most every
    ``interval_s``, with their counts since the previous line."""

    def __init__(self, name: str, interval_s: float = REPORT_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self.overflows = 0          # since the previous line, as the counts below
        self.dropped = 0
        self.late_blocks = 0
        self._since = clock()
        self._logged = -math.inf

    def add(self, *, overflows: int = 0, dropped: int = 0, late_blocks: int = 0) -> None:
        with self._lock:
            self.overflows += overflows
            self.dropped += dropped
            self.late_blocks += late_blocks

    def check(self, flush: bool = False) -> None:
        """Log the counts, if any, unless a line went out less than the
        interval ago; with ``flush``, whatever the interval."""
        with self._lock:
            now = self._clock()
            if not (self.overflows or self.dropped or self.late_blocks):
                return
            if not flush and now - self._logged < self._interval_s:
                return
            parts = []
            if self.overflows:
                parts.append(f"{self.overflows} overflows reported by the device")
            if self.dropped:
                parts.append(f"{self.dropped} samples dropped, the engine behind")
            if self.late_blocks:
                parts.append(f"{self.late_blocks} blocks took the engine longer than their period")
            log.warning("%s: %s, in the last %.0f s", self.name, ", ".join(parts), now - self._since)
            self.overflows = self.dropped = self.late_blocks = 0
            self._since = self._logged = now


# ------------------------------------------------------------- the source


@dataclass(frozen=True)
class _Label:
    """What the samples in the buffer were taken with."""

    centre_hz: float            # asked
    sample_rate_hz: float
    exact_centre_hz: float      # in use, which labels them
    rate_scale: float
    inverted: bool
    sample_format: Format
    full_scale: float


class SoapySource:
    """A receiver read through SoapySDR, read like any source (module
    docstring).

    ``args`` are the SoapySDR device arguments, which name a driver
    (``parse_args``). ``load`` gives the SoapySDR module, or None and why
    not; the tests give a stand-in. The other options set the source's
    timings: as the rtl_tcp sources' (rtltcp.py), with ``silence_s``, how
    long a stream may give no samples before the source waits and opens the
    device again, ``stop_s``, how long ``wait_closed`` waits for the reading
    thread, and ``report_s``, how often the losses are logged at most.
    """

    kind = "soapy"

    def __init__(self, args: str, *, settle_s: float = SETTLE_S, buffer_s: float = BUFFER_S,
                 idle_s: float = IDLE_S, read_wait_s: float = READ_WAIT_S, silence_s: float = TIMEOUT_S,
                 retry_s: tuple[float, float] = RETRY_S, stop_s: float = STOP_S, report_s: float = REPORT_S,
                 load: Callable[[], tuple[ModuleType | None, str]] = load_module,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._args = parse_args(args)
        self._settle_s = settle_s
        self._buffer_s = buffer_s
        self._idle_s = idle_s
        self._read_wait_s = read_wait_s
        self._silence_s = silence_s
        self._retry_s = retry_s
        self._stop_s = stop_s
        self._load = load
        self._clock = clock
        self.given: frozenset[str] = frozenset()
        self._serial = self._args.get("serial", "")
        self._listed_label = ""         # the device's label, as its module's search gave it
        # Shared with the reading thread, under the lock: the settings
        # asked, counted by their changes, and what the device offers.
        self._lock = threading.Lock()
        self._settings = Settings(DEFAULT_CENTRE_HZ, DEFAULT_SAMPLE_RATE_HZ, None, 0.0)
        self._generation = 0
        self._capabilities: Capabilities | None = None
        self.centre_hz = self._settings.centre_hz
        self.sample_rate_hz = self._settings.sample_rate_hz
        self.exact_centre_hz = self.centre_hz
        self.rate_scale = 1.0
        self.on_change: Callable[[], None] | None = None
        self.status = "idle"        # "connecting", "streaming", "waiting" or "idle"
        self.detail = ""            # why the source waits
        self._losses = Losses(f"SoapySDR {self._args['driver']}", report_s, clock)
        # The buffer of the session that runs and what its samples were
        # taken with, set by the thread under ``_buffer_lock``; the samples
        # a read waits for.
        self._buffer_lock = threading.Lock()
        self._buffer: NativeBuffer | None = None
        self._label: _Label | None = None
        self._dropped = 0           # the buffer's drops already reported
        self._waiting = 0
        self._arrived = asyncio.Event()
        self._wake = asyncio.Event()        # set by close: a pause between sessions ends
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._last_read = -math.inf     # when the latest read ended
        self._reading = False
        self._closed = False
        # The latest read that gave samples: when it returned, and the
        # period of its block, for the blocks that take longer.
        self._returned: tuple[float, float] | None = None

    # ------------------------------------------------------------ identity

    @property
    def args(self) -> str:
        """The device arguments: those given, with the serial number once
        the device is found."""
        pairs = dict(self._args)
        if self._serial:
            pairs.pop("index", None)    # a position on the bus, which the serial number replaces
            pairs["serial"] = self._serial
        return args_text(pairs)

    @property
    def settings_key(self) -> str:
        """Such as "soapy sdrplay 1234": its driver and serial number once
        known; until then the arguments given."""
        if self._serial:
            return f"{self.kind} {self._args['driver']} {self._serial}"
        return f"{self.kind} {args_text(self._args)}"

    @property
    def reopen(self) -> dict[str, Any]:
        """What opens this device again (sources.py): by its driver and
        serial number once found."""
        return {"kind": self.kind, "args": self.args}

    @property
    def identity(self) -> tuple[str, str, str]:
        """The device's driver, serial number and label, the last two as
        its module's search gave them once found, "" until then: what the
        Receiver menu lists for the device in use (soapysearch.py)."""
        return self._args["driver"], self._serial, self._listed_label

    @property
    def hardware(self) -> str:
        """The device, such as "SoapySDR rtlsdr, R820T", and the converter
        in front of it."""
        capabilities = self._capabilities
        name = f"SoapySDR {self._args['driver']}"
        if capabilities is not None and capabilities.hardware:
            name = f"{name}, {capabilities.hardware}"
        converter = _conversion(self._settings).text()
        return f"{name}, behind {converter}" if converter else name

    # ------------------------------------------------------------ what the engine asks

    @property
    def grid_hz(self) -> float:
        """No grid: that is the rtl_tcp sources' R820T and R828D."""
        return 0.0

    def pll_tuning(self) -> tuple[int, int] | str:
        return "receiver"

    def centre_off_grid(self, request_hz: int) -> float | None:
        return None

    def centre_in_use(self, centre_hz: float) -> float:
        """Where the device sits on the air for ``centre_hz``, with the
        other settings as they are: within half a hertz of it."""
        settings = replace(self._settings, centre_hz=float(round(centre_hz)))
        return tuning(settings, self._capabilities).centre_hz

    def tap(self, receiver: Callable[[bytes, float, float, bool], None] | None) -> None:
        """I/Q recordings take the device's own format in a later version:
        refused, so that no file says the wrong one."""
        if receiver is not None:
            raise ValueError("I/Q recordings of a SoapySDR device are not available yet")

    def describe(self) -> dict[str, Any]:
        """The device's settings and the source's state, as JSON values."""
        settings, capabilities = self._settings, self._capabilities
        reach = _reach(settings, capabilities)
        rates = capabilities.menu_rates() if capabilities is not None else \
            [rate for rate in COMMON_RATES_HZ if rate <= MAX_RATE_HZ]
        return {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "address": None,
            "tuner": capabilities.hardware or None if capabilities is not None else None,
            "driver": self._args["driver"],
            "centre_hz": settings.centre_hz,
            "centre_in_use_hz": tuning(settings, capabilities).centre_hz,
            "sample_rate_hz": settings.sample_rate_hz,
            "sample_rates_hz": sorted({*rates, settings.sample_rate_hz}),
            "gain_db": settings.gain_db,
            "ppm": settings.ppm,
            "gains_db": list(capabilities.gains_db()) if capabilities is not None else [],
            "range_hz": [reach[0][0], reach[-1][1]] if reach else [settings.centre_hz, settings.centre_hz],
            "hf_input": None,
            "hf_inputs": [],
            "direct": False,
            "rtl_agc": None,
            "bias_tee": None,
            "converter": settings.converter,
            "converter_lo_hz": settings.converter_lo_hz,
            "driver_pll": None,
            "tuner_grid": None,
            "settable": settable(capabilities),
        }

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change ``centre_hz``, ``sample_rate_hz`` (one the device offers,
        up to MAX_RATE_HZ), ``gain_db`` (``None`` for automatic gain; with
        one gain stage), ``ppm``, ``converter`` and ``converter_lo_hz``, then
        call ``on_change``. Raises ValueError, changing nothing, when a
        setting is unknown or invalid (``changed``). The reading thread
        applies them between its reads; until the device is first found,
        they wait for it."""
        with self._lock:
            new = changed(self._settings, changes, self._capabilities)
            if new != self._settings:
                self._settings = new
                self._generation += 1
        self._notify()

    async def read(self, count: int) -> np.ndarray | None:
        """The next ``count`` samples, or ``None`` when they did not arrive
        within a short wait: the source is opening its device, or waiting
        to try again."""
        if count < 1:
            raise ValueError(f"cannot read {count} samples")
        if self._closed:
            return None
        self._loop = asyncio.get_running_loop()
        self._note_period()
        self._reading = True
        taken = None
        try:
            if self._task is None or self._task.done():
                self._task = asyncio.ensure_future(self._run())
            deadline = time.monotonic() + self._read_wait_s
            while not self._closed:
                taken = self._take(count)
                if taken is not None:
                    break
                self._arrived.clear()
                self._waiting = count
                taken = self._take(count)       # what came meanwhile
                if taken is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._arrived.wait(), remaining)
                except asyncio.TimeoutError:
                    break
        finally:
            self._waiting = 0
            self._reading = False
            self._last_read = time.monotonic()
        self._report()
        if taken is None or self._closed:
            self._returned = None
            return None
        data, label = taken
        self.centre_hz, self.sample_rate_hz = label.centre_hz, label.sample_rate_hz
        self.exact_centre_hz, self.rate_scale = label.exact_centre_hz, label.rate_scale
        samples = to_complex(data, label.sample_format, label.full_scale)
        if label.inverted:      # Q, I: the band as it is on the air
            samples = np.ascontiguousarray(samples.view(np.float32).reshape(-1, 2)[:, ::-1]).view(np.complex64).ravel()
        self._returned = (self._clock(), count / label.sample_rate_hz)
        return samples

    def close(self) -> None:
        """Stop for good: the reading thread releases the device. Safe to
        repeat: a repeat changes nothing, and lets a stop in progress
        finish."""
        if self._closed:
            return
        self._closed = True
        if self._stop is not None:
            self._stop.set()
        self._wake.set()
        self._arrived.set()

    async def wait_closed(self) -> None:
        """Return once ``close`` has taken effect: the stream and the
        device are closed and the reading thread has ended, or ``stop_s``
        has passed for it, which the log then says."""
        if self._task is not None:
            await asyncio.wait({self._task})
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, self._stop_s)
            if thread.is_alive():
                log.warning("the SoapySDR %s device did not stop within %g s", self._args["driver"], self._stop_s)
        self._losses.check(flush=True)

    # ------------------------------------------------------------ sessions

    async def _run(self) -> None:
        """Keep a session open while the engine reads, trying again after
        failures."""
        delay = self._retry_s[0]
        try:
            while not self._closed and not self._idle():
                if self.status == "idle":
                    self._set_status("connecting", "")
                outcome = await self._session()
                if outcome is None:
                    break
                detail, streamed = outcome
                if streamed:
                    delay = self._retry_s[0]
                self._set_status("waiting", detail)
                self._wake.clear()
                if self._closed:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
                delay = min(2 * delay, self._retry_s[1])
        except Exception:
            log.exception("the SoapySDR source stopped")
        finally:
            self._losses.check(flush=True)
            if not self._closed:
                self._set_status("idle", "")

    async def _session(self) -> tuple[str, bool] | None:
        """One session in the reading thread, until it fails or is asked to
        stop: why it failed and whether samples came, or None once stopped."""
        loop = asyncio.get_running_loop()
        done: asyncio.Future[tuple[str, bool] | None] = loop.create_future()
        stop = threading.Event()

        def finish(outcome: tuple[str, bool] | None) -> None:
            if not done.done():
                done.set_result(outcome)

        def target() -> None:
            outcome: tuple[str, bool] | None = None
            try:
                outcome = self._stream(stop)
            except Exception as error:
                log.exception("the SoapySDR session failed")
                outcome = (f"the device failed: {_text(error)}", False)
            finally:
                try:
                    loop.call_soon_threadsafe(finish, outcome)
                except RuntimeError:        # the event loop has ended: nobody waits
                    pass

        self._stop = stop
        self._thread = threading.Thread(target=target, name="radiolack-soapy", daemon=True)
        self._thread.start()
        while not done.done():
            await asyncio.wait({done}, timeout=_CHECK_S)
            if self._closed or self._idle():
                stop.set()
        return None if stop.is_set() else done.result()

    def _stream(self, stop: threading.Event) -> tuple[str, bool] | None:
        """The reading thread's session (module docstring): find and open
        the device, set it up and read it until ``stop``; why it failed and
        whether samples came, or None once stopped. The stream and the
        device are closed whatever ends it."""
        soapy, problem = self._load()
        if soapy is None:
            return problem, False
        receive = soapy.SOAPY_SDR_RX
        # Once found, the device is sought again by its serial number: the
        # source reads one receiver, whose settings it saves.
        wanted = parse_args(self.args)
        try:
            found = soapy.Device.enumerate(wanted)
        except Exception as error:
            return f"the device search failed: {_text(error)}", False
        if not found:
            # Neither its serial number nor the other arguments: the detail goes into problem reports.
            given = "with its serial number" if "serial" in wanted else "with the arguments given"
            return f"no {wanted['driver']} device found {given}", False
        listing = {**{str(key): str(value) for key, value in dict(found[0]).items()}, **wanted}
        self._listed_label = listing.get("label", "")
        if stop.is_set():
            return None
        try:
            device = soapy.Device(listing)
        except Exception as error:
            return f"the device cannot be opened: {_text(error)}", False
        stream = None
        streamed = False
        try:
            try:
                capabilities = read_capabilities(soapy, device, listing)
            except Exception as error:
                return f"the device cannot be read: {_text(error)}", False
            with self._lock:
                self._capabilities = capabilities
                if capabilities.serial:
                    self._serial = capabilities.serial
                self._settings = fitted(self._settings, capabilities)
                self._generation += 1
            self._post(self._notify)
            log.info("SoapySDR %s device opened: %s, in %s", capabilities.driver, capabilities.hardware or "no name",
                     capabilities.sample_format.name)
            sample_format = capabilities.sample_format
            try:
                stream = device.setupStream(receive, sample_format.name, [0])
                mtu = int(device.getStreamMTU(stream))
            except Exception as error:
                return f"the stream cannot be opened: {_text(error)}", False
            if mtu < 1:
                return f"the stream's MTU is {mtu}", False
            array = np.empty(2 * mtu, sample_format.component)
            applied: Settings | None = None
            generation = -1
            settle_until = 0.0
            status = device.activateStream(stream)
            if status != 0:
                return f"the stream cannot start: {soapy.errToStr(status)}", False
            failed = 0
            last_samples = time.monotonic()
            while not stop.is_set():
                if generation != self._generation:
                    with self._lock:
                        settings, generation = self._settings, self._generation
                    if self._apply(soapy, device, applied, settings, capabilities):
                        settle_until = time.monotonic() + self._settle_s
                    applied = settings
                result = device.readStream(stream, [array], mtu, 0, TIMEOUT_US)
                count = int(result.ret)
                now = time.monotonic()
                if count > 0:
                    failed = 0
                    last_samples = now
                    if not streamed:
                        streamed = True
                        self._post(self._set_status, "streaming", "")
                    if now >= settle_until:
                        self._keep(memoryview(array)[:2 * count].cast("B"))
                    continue
                if count == soapy.SOAPY_SDR_OVERFLOW:
                    self._losses.add(overflows=1)
                elif count != soapy.SOAPY_SDR_TIMEOUT:
                    failed += 1
                    if failed >= FAILED_READS:
                        return f"the stream failed: {soapy.errToStr(count)}", streamed
                if now - last_samples >= self._silence_s:
                    return "no samples: the device stopped answering", streamed
            return None
        except Exception as error:
            log.exception("the SoapySDR %s device failed", self._args["driver"])
            return f"the device failed: {_text(error)}", streamed
        finally:
            with self._buffer_lock:
                self._buffer = self._label = None
            if stream is not None:
                for name in ("deactivateStream", "closeStream"):
                    try:
                        getattr(device, name)(stream)
                    except Exception:
                        log.exception("the SoapySDR stream did not close cleanly (%s)", name)
            try:
                close = getattr(device, "close", None)
                if callable(close):
                    close()
                else:
                    soapy.Device.unmake(device)
            except Exception:
                log.exception("the SoapySDR device was not released cleanly")

    def _apply(self, soapy: ModuleType, device: Any, before: Settings | None, settings: Settings,
               capabilities: Capabilities) -> bool:
        """Ask the device for ``settings``, given those it has, ``before``
        (None: none yet); True when the samples change setting, which then
        start a new buffer, labelled with it."""
        receive = soapy.SOAPY_SDR_RX
        old = None if before is None else tuning(before, capabilities)
        new = tuning(settings, capabilities)
        rate = settings.sample_rate_hz
        rate_changed = before is None or before.sample_rate_hz != rate
        if rate_changed:
            device.setSampleRate(receive, 0, rate)
            bandwidth = capabilities.bandwidth_for(rate)
            if bandwidth is not None:
                device.setBandwidth(receive, 0, bandwidth)
        if capabilities.correction and (old is None or old.whole_ppm != new.whole_ppm):
            if hasattr(device, "setFrequencyCorrection"):
                device.setFrequencyCorrection(receive, 0, float(new.whole_ppm))
            else:
                device.setFrequency(receive, 0, "CORR", float(new.whole_ppm))
        retuned = rate_changed or old is None or (old.request_hz, old.whole_ppm) != (new.request_hz, new.whole_ppm)
        if retuned:
            # A CORR component given its value, so that the overall
            # frequency, which SoapySDR spreads across the components,
            # leaves the correction as it is.
            if capabilities.corr_component:
                device.setFrequency(receive, 0, new.request_hz, {"CORR": repr(float(new.whole_ppm))})
            else:
                device.setFrequency(receive, 0, new.request_hz)
        stage = capabilities.gain_stage
        if stage is not None and capabilities.gain_settable and (before is None or before.gain_db != settings.gain_db):
            if settings.gain_db is None:
                if capabilities.gain_mode:
                    device.setGainMode(receive, 0, True)
            else:
                if capabilities.gain_mode:
                    device.setGainMode(receive, 0, False)
                device.setGain(receive, 0, stage, settings.gain_db)
        inverted = _conversion(settings).inverted
        if before is not None and not retuned and inverted == _conversion(before).inverted \
                and new.centre_hz == old.centre_hz:
            return False
        buffer = NativeBuffer(capabilities.sample_format.sample_bytes, max(1, round(self._buffer_s * rate)))
        label = _Label(settings.centre_hz, rate, new.centre_hz, new.scale, inverted, capabilities.sample_format,
                       capabilities.full_scale)
        with self._buffer_lock:
            previous = self._buffer
            if previous is not None:
                self._losses.add(dropped=previous.dropped - self._dropped)
            self._buffer, self._label, self._dropped = buffer, label, 0
        return retuned

    def _keep(self, data: memoryview) -> None:
        """Keep samples for the engine, and tell a read that waits once
        what it waits for is there."""
        buffer = self._buffer
        if buffer is None:
            return
        buffer.put(data)
        waiting = self._waiting
        if waiting and len(buffer) >= waiting:
            self._post(self._arrived.set)

    def _take(self, count: int) -> tuple[bytes, _Label] | None:
        with self._buffer_lock:
            buffer, label = self._buffer, self._label
            if buffer is None or label is None:
                return None
            data = buffer.take_now(count)
            return None if data is None else (data, label)

    def _report(self) -> None:
        """Count the samples the buffer dropped since the latest count, and
        log the losses when due."""
        with self._buffer_lock:
            buffer = self._buffer
            if buffer is not None and buffer.dropped != self._dropped:
                self._losses.add(dropped=buffer.dropped - self._dropped)
                self._dropped = buffer.dropped
        self._losses.check()

    def _note_period(self) -> None:
        """Count the block that the engine took longer than its period to
        process: the time from the latest read that gave samples to this
        one. Longer than ``idle_s``, it was a pause, not a block."""
        returned = self._returned
        if returned is not None:
            spent = self._clock() - returned[0]
            if returned[1] < spent < self._idle_s:
                self._losses.add(late_blocks=1)

    def _idle(self) -> bool:
        """Nobody has read for a while: the engine has no client."""
        return not self._reading and time.monotonic() - self._last_read >= self._idle_s

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        """Run ``callback`` in the event loop, from the reading thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:        # the event loop has ended
            pass

    def _set_status(self, status: str, detail: str) -> None:
        if self._closed and status != "idle":
            return
        if (status, detail) != (self.status, self.detail):
            self.status, self.detail = status, detail
            self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()
