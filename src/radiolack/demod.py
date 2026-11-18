"""Demodulation of one channel: the VFO's signal, its level, the squelch and
its audio at 12 kHz, in NFM, AM, SAM, USB, LSB and CW, or at 32 kHz in WFM.

The receiver's samples go through three FFT filters (dsp.FftFilter). The
front filter brings the VFO's frequency to 0 Hz, or in CW to the CW pitch,
and the rate down to 48 kHz, or to 320 kHz in WFM; the channel filter keeps
the mode's passband, whose power is the channel level; after the mode's
detector, the audio filter keeps the audio band and brings the rate down to
12 kHz, or to 32 kHz in WFM. NFM goes through the FM discriminator, then a
de-emphasis and a high-pass filter against CTCSS tones; WFM, broadcast FM
in mono, through the same discriminator, then a 50 µs de-emphasis and a
low-pass filter at 15 kHz, below the stereo pilot; AM through an envelope
detector that follows the carrier's level; SAM, synchronous AM, through the
channel turned by its carrier's phase, which a band-pass 80 Hz wide follows
(``CarrierSearch``, ``SyncDetector``), or AM's envelope while it has no
lock; USB, LSB and CW through the channel's real part, which is the
sideband's audio, under an AGC. The squelch compares the channel level with
a threshold, set by hand or, automatically, 8 dB above the noise that the
spectrum shows around the VFO; it opens and closes in 5 ms fades, in step
with the audio. The automatic notch, when on, in AM, SAM, USB and LSB,
finds the steady lines in the detector's output (``AutoNotch``) and takes
them out of the audio filter's response, the audio fading from the old
taps to the new. The noise blanker, when on, in every mode, zeroes the
short impulses of the receiver's samples before the front filter
(``NoiseBlanker``): only the VFO's channel takes them blanked, and a
carrier's measure, which taps the channel, takes it unblanked, as the fine
spectrum (fine.py) takes the front filter's output, which it taps.
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import math
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsp import FftFilter, hann, lowpass, rate_ratio, shaped

MODES = ("NFM", "AM", "SAM", "USB", "LSB", "CW", "WFM")
AUDIO_MODES = MODES                 # every mode plays
CHANNEL_RATE_HZ = 48_000.0
WFM_CHANNEL_RATE_HZ = 320_000.0     # WFM: room for its widest channel
AUDIO_DECIMATION = 4                # 12 kHz audio
WFM_AUDIO_DECIMATION = 10           # WFM: 32 kHz audio, which holds broadcast's 15 kHz
BANDWIDTH_RANGE_HZ = (100.0, 25_000.0)
WFM_BANDWIDTH_RANGE_HZ = (50_000.0, 250_000.0)
# Each mode's filter width once tuned to a bookmark, as the interface's (main.js MODES).
MODE_BANDWIDTHS_HZ = {"NFM": 10_000.0, "AM": 8_000.0, "SAM": 8_000.0, "USB": 2_700.0, "LSB": 2_700.0,
                      "CW": 500.0, "WFM": 180_000.0}
SQUELCH_RANGE_DB = (-160.0, 0.0)
SQUELCH_MARGIN_DB = 8.0             # automatic squelch: noise + 8 dB
SQUELCH_HYSTERESIS_DB = 3.0         # closes this far below the threshold
SQUELCH_WINDOW_S = 0.010            # channel power averaged over 10 ms
FADE_S = 0.005
MAX_DEVIATION_HZ = 5_000.0          # NFM: full-scale audio
DEEMPHASIS_S = 750e-6               # NFM de-emphasis, -6 dB per octave above 212 Hz
DEEMPHASIS_REFERENCE_HZ = 1_000.0   # unit gain there
NFM_HIGH_PASS_HZ = 280.0            # removes CTCSS tones (up to 250.3 Hz)
NFM_LOW_PASS_HZ = 3_500.0
WFM_MAX_DEVIATION_HZ = 75_000.0     # WFM: full-scale audio, broadcast's peak deviation
WFM_DEEMPHASIS_S = 50e-6            # WFM de-emphasis in Europe, -6 dB per octave above 3.2 kHz
WFM_HIGH_PASS_HZ = 40.0             # WFM: removes the offset of a station tuned beside its centre
WFM_LOW_PASS_HZ = 15_000.0          # WFM: broadcast's audio, without the 19 kHz stereo pilot
AUDIO_HIGH_PASS_HZ = 100.0          # AM, SAM, SSB and CW: no hum, nothing left of a carrier
AUDIO_LOW_PASS_HZ = 5_000.0         # AM, SAM, SSB and CW: below the 6 kHz that 12 kHz audio holds
AUDIO_TRANSITION_HZ = 60.0
AUDIO_ATTENUATION_DB = 45.0
FRONT_CUTOFF_HZ = 20_000.0          # flat to 16 kHz, -70 dB from 24 kHz
FRONT_TRANSITION_HZ = 8_000.0
WFM_FRONT_CUTOFF_HZ = 142_500.0     # WFM: flat to 125 kHz, -70 dB from 160 kHz
WFM_FRONT_TRANSITION_HZ = 35_000.0
CHANNEL_ATTENUATION_DB = 70.0
CW_PITCH_HZ = 700.0                 # a carrier on the VFO sounds at this pitch in CW
AM_WINDOW_S = 0.010                 # AM: the carrier's level, over the 10 ms before and after each sample
AM_LEVEL = 0.8                      # AM and SAM: audio amplitude at 100% modulation
AGC_LEVEL = 0.4                     # SSB and CW: audio amplitude of a steady signal
AGC_DECAY_DB_S = 10.0               # SSB and CW: how fast the gain comes back after a signal (slow AGC)
AGC_FLOOR_DB = -200.0               # SSB and CW: the AGC's level before any signal
# Synchronous AM (later-milestones.md, section 2).
SAM_SEARCH_HZ = 1_000.0             # the carrier: the strongest line within this of the VFO, without a lock
SAM_BAND_HZ = 80.0                  # the band-pass that follows it, between its half-gain points
SAM_STAGES = 3                      # moving averages in cascade make the band-pass
SINC_CUBED_HALF = 0.36606           # where sinc(x) ** 3, the cascade's response at x / length, is one half
SAM_LOCK_DB = 10.0                  # carrier over the noise within the band-pass, or no lock
SAM_HOLD_DB = 3.0                   # a lock holds this far under SAM_LOCK_DB
SAM_LOCK_SEARCHES = 5               # the lock goes by the mean of this many searches' margins, half a second
SAM_MARGIN_DB = (-10.0, 60.0)       # a search's margin counts within these in that mean
SAM_SEARCH_SIZE = 4096              # the search's FFT: 11.7 Hz per bin at 48 kHz
SAM_SEARCH_S = 0.1                  # one search this often
SAM_LOBE_BINS = 2                   # a Hann window's main lobe, on each side of its peak
SAM_FLOOR = 1e-9                    # -180 dBFS: no carrier at all, silence
SAM_FADE_S = FADE_S                 # the switch between the synchronous detector and the envelope
# The automatic notch (later-milestones.md, section 2).
NOTCH_MODES = ("AM", "SAM", "USB", "LSB")   # not CW, whose signal is a steady tone, nor NFM and WFM
NOTCH_MARGIN_DB = 15.0              # a line stands this far over its surroundings
NOTCH_WIDTH_HZ = 30.0               # and is at most this wide
NOTCH_STEADY_S = 0.5                # for this long, before it is removed
NOTCH_RELEASE_S = 1.0               # removed until this long after it stops
NOTCH_RANGE_HZ = (100.0, 5_000.0)
NOTCH_MAX = 4
NOTCH_BIN_HZ = 6.0                  # the analysis's bins are at most this wide
NOTCH_ANALYSIS_S = 0.1              # one analysis this often
NOTCH_SURROUNDINGS_HZ = 300.0       # a line's surroundings: the bins this far on each side, beyond its own
NOTCH_MATCH_HZ = 9.0                # a line found again this close to where it was is the same line
NOTCH_MOVE_HZ = 5.0                 # a notch follows its line once it moved this far
# Each notch removes the line's width and half the audio filter's transition
# on each side, so that the filter's whole attenuation reaches the line.
NOTCH_HALF_HZ = NOTCH_WIDTH_HZ / 2 + AUDIO_TRANSITION_HZ / 2
NOTCH_FADE_S = FADE_S               # the audio fades from the old taps to the new over this
# The noise blanker (later-milestones.md, section 2).
BLANKER_FACTOR = 6.0                # samples over this many times the block's median magnitude are impulses
BLANKER_SIDES = 2                   # samples zeroed on each side of an impulse
BLANKER_STRIDE = 16                 # the block's median magnitude, from every 16th of its samples
BLANKER_LONGEST = 16                # an impulse spans at most this many samples; longer, it is a signal
BLANKER_MOST = 0.02                 # a block where more of its samples would be zeroed is left whole

# Audio frame header, little-endian: kind, version, reserved, position of the
# first sample (counted at the audio rate, modulo 2^32), audio rate (Hz);
# 16-bit signed samples follow.
AUDIO_HEADER = struct.Struct("<BBHId")
FRAME_KIND_AUDIO = 2
AUDIO_VERSION = 1


@dataclass(frozen=True)
class DemodSettings:
    """The receiver's demodulation settings, shared by every interface."""

    frequency_hz: float
    mode: str = "NFM"
    bandwidth_hz: float = 10_000.0
    squelch_db: float = -80.0       # the threshold set by hand
    squelch_auto: bool = True
    notch: bool = False             # the automatic notch, which acts in NOTCH_MODES only
    blanker: bool = False           # the noise blanker, which acts in every mode

    def changed(self, changes: Mapping[str, object]) -> DemodSettings:
        """These settings with ``changes`` applied; raises ValueError, naming
        the first refused one, when any is. The filter width must suit the
        mode that the changes leave: a mode that the current width does not
        suit, into or out of WFM, takes its usual width unless the changes
        give one."""
        mode = changes.get("mode", self.mode)
        width_range = bandwidth_range_hz(mode if isinstance(mode, str) and mode in MODES else self.mode)
        values: dict[str, Any] = {}
        for name, value in changes.items():
            if name == "frequency_hz":
                values[name] = float(round(_number(value, "the VFO frequency", 1.0, 1e11)))
            elif name == "mode":
                if value not in MODES:
                    raise ValueError(f"the mode must be one of {', '.join(MODES)}")
                values[name] = value
            elif name == "bandwidth_hz":
                values[name] = _number(value, "the filter width", *width_range)
            elif name == "squelch_db":
                values[name] = _number(value, "the squelch threshold", *SQUELCH_RANGE_DB)
            elif name == "squelch_auto":
                if not isinstance(value, bool):
                    raise ValueError("the automatic squelch must be true or false")
                values[name] = value
            elif name == "notch":
                if not isinstance(value, bool):
                    raise ValueError("the notch must be true or false")
                values[name] = value
            elif name == "blanker":
                if not isinstance(value, bool):
                    raise ValueError("the noise blanker must be true or false")
                values[name] = value
            else:
                raise ValueError(f"unknown demodulator setting: {name}")
        low, high = width_range
        if "bandwidth_hz" not in values and not low <= self.bandwidth_hz <= high:
            values["bandwidth_hz"] = MODE_BANDWIDTHS_HZ[values.get("mode", self.mode)]
        return dataclasses.replace(self, **values)

    def describe(self) -> dict[str, Any]:
        return {"frequency_hz": self.frequency_hz, "mode": self.mode,
                "bandwidth_hz": self.bandwidth_hz, "squelch_db": self.squelch_db,
                "squelch_auto": self.squelch_auto, "notch": self.notch, "blanker": self.blanker}

    @property
    def notching(self) -> bool:
        """Whether the automatic notch acts: on, in one of NOTCH_MODES."""
        return self.notch and self.mode in NOTCH_MODES


def bandwidth_range_hz(mode: str) -> tuple[float, float]:
    """The filter widths that ``mode`` takes: WFM's are wider than the others'."""
    return WFM_BANDWIDTH_RANGE_HZ if mode == "WFM" else BANDWIDTH_RANGE_HZ


def noise_clearance_hz(mode: str, bandwidth_hz: float) -> float:
    """How far from the VFO the automatic squelch's noise estimate starts: in
    WFM beyond the channel's edges, since a station fills the bins around
    the VFO; at the VFO in the other modes, whose channel barely moves the
    median of the bins around it."""
    return bandwidth_hz / 2 if mode == "WFM" else 0.0


def _number(value: object, what: str, low: float, high: float) -> float:
    # Only a float can be NaN or infinite; a whole number too large for a float
    # fails the range check, which compares it as it is, before any conversion.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f"{what} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{what} must be between {low:g} and {high:g}")
    return float(value)


@dataclass(frozen=True)
class Meter:
    """The channel's state for one block of samples."""

    level_db: float | None      # channel power in dBFS; None: nothing received there
    squelch_db: float | None    # threshold in use; None: no noise estimate yet
    open: bool
    sam_locked: bool | None = None  # in SAM, whether it holds a carrier; None in the other modes
    notch_lines: int | None = None  # while the notch acts, the lines it removes now; None otherwise
    blanker_impulses: int | None = None     # with the blanker on, the impulses it zeroed in the latest second


def has_channel(mode: str, rate_hz: float) -> bool:
    """Whether a receiver at ``rate_hz`` gives the mode a channel: WFM needs
    its channel rate, the other modes theirs."""
    return rate_hz >= (WFM_CHANNEL_RATE_HZ if mode == "WFM" else CHANNEL_RATE_HZ)


def vfo_position_hz(mode: str) -> float:
    """Where the front filter puts the VFO's frequency in the channel: 0 Hz,
    or in CW the CW pitch, so that a carrier on the VFO sounds at that pitch."""
    return CW_PITCH_HZ if mode == "CW" else 0.0


def passband_hz(mode: str, bandwidth_hz: float) -> tuple[float, float]:
    """The mode's passband, from the VFO's frequency: [0, width] above it in
    USB, [-width, 0] below it in LSB, [-width / 2, width / 2] around it in
    the other modes."""
    low = {"USB": 0.0, "LSB": -bandwidth_hz}.get(mode, -bandwidth_hz / 2)
    return low, low + bandwidth_hz


def channel_taps(mode: str, bandwidth_hz: float, rate_hz: float) -> np.ndarray:
    """The channel filter: the mode's passband (``passband_hz``) around the
    VFO, at ``rate_hz``; in CW the passband moves with the VFO to the CW
    pitch."""
    transition = min(max(0.1 * bandwidth_hz, 100.0), 1_500.0)
    taps = lowpass(bandwidth_hz / 2, transition, rate_hz, CHANNEL_ATTENUATION_DB).astype(np.complex128)
    centre = vfo_position_hz(mode) + sum(passband_hz(mode, bandwidth_hz)) / 2
    if centre:
        taps *= np.exp(2j * np.pi * centre / rate_hz * (np.arange(len(taps)) - (len(taps) - 1) / 2))
    return taps


def audio_taps(mode: str, rate_hz: float, notches: Sequence[float] = ()) -> np.ndarray:
    """The audio filter at the channel rate. NFM: de-emphasis with unit gain
    at 1 kHz, high-pass at 280 Hz and low-pass at 3.5 kHz. WFM: de-emphasis
    with unit gain at the lowest frequencies, as receivers apply it,
    high-pass at 40 Hz and low-pass at 15 kHz. The other modes, SAM as AM:
    high-pass at 100 Hz and low-pass at 5 kHz, with each of ``notches``, in
    Hz, removed: ``NOTCH_HALF_HZ`` on each side of it. The taps are as many
    with notches as without; NFM and WFM take none."""
    if notches and mode in ("NFM", "WFM"):
        raise ValueError(f"the audio filter takes no notch in {mode}")
    if mode == "NFM":
        corner = 1 / (2 * math.pi * DEEMPHASIS_S)

        def response(frequency: np.ndarray) -> np.ndarray:
            deemphasis = np.sqrt((1 + (DEEMPHASIS_REFERENCE_HZ / corner) ** 2) / (1 + (frequency / corner) ** 2))
            return deemphasis * ((frequency >= NFM_HIGH_PASS_HZ) & (frequency <= NFM_LOW_PASS_HZ))
    elif mode == "WFM":
        corner = 1 / (2 * math.pi * WFM_DEEMPHASIS_S)

        def response(frequency: np.ndarray) -> np.ndarray:
            deemphasis = 1 / np.sqrt(1 + (frequency / corner) ** 2)
            return deemphasis * ((frequency >= WFM_HIGH_PASS_HZ) & (frequency <= WFM_LOW_PASS_HZ))
    else:
        def response(frequency: np.ndarray) -> np.ndarray:
            gain = ((frequency >= AUDIO_HIGH_PASS_HZ) & (frequency <= AUDIO_LOW_PASS_HZ)).astype(np.float64)
            for notch in notches:
                gain[np.abs(frequency - notch) <= NOTCH_HALF_HZ] = 0.0
            return gain

    return shaped(response, AUDIO_TRANSITION_HZ, rate_hz, AUDIO_ATTENUATION_DB)


@dataclass
class _Line:
    """A line that the notch follows."""

    frequency_hz: float
    margin_db: float
    first_s: float      # found first, without a gap since
    last_s: float       # found last
    notched: bool = False


class AutoNotch:
    """Finds the lines to remove in the detector's output, real at
    ``rate_hz``, before the squelch's gate and the audio filter.

    Every ``NOTCH_ANALYSIS_S`` it computes a Hann-windowed FFT of the latest
    output with bins at most ``NOTCH_BIN_HZ`` wide: 8192 samples, 5.9 Hz per
    bin, at 48 kHz. A line is a bin between 100 Hz and 5 kHz that is the
    highest of its neighbours, whose level falls by ``NOTCH_MARGIN_DB``
    within ``NOTCH_WIDTH_HZ / 2`` on both sides (at most 30 Hz wide) and
    that stands ``NOTCH_MARGIN_DB`` over the median of its surroundings,
    ``NOTCH_SURROUNDINGS_HZ`` on each side beyond its width; a parabola
    through the levels in dB places it. Found again within
    ``NOTCH_MATCH_HZ`` at every analysis for ``NOTCH_STEADY_S``, it is
    notched, four at most, those that stand highest over their
    surroundings first, a notched line keeping its notch while it lasts,
    whatever comes after it; a single miss starts its half second again,
    which leaves keyed and moving tones alone. A notch is released
    ``NOTCH_RELEASE_S`` after its line was last found, and follows its line
    once it moved ``NOTCH_MOVE_HZ``. The analysis reads what comes before
    the audio filter, so a notched line is still found while it lasts.
    ``notches`` holds the frequencies notched, lowest first, as the audio
    filter's taps have them. Not thread-safe."""

    def __init__(self, rate_hz: float) -> None:
        self.rate_hz = float(rate_hz)
        self.size = 1 << math.ceil(math.log2(self.rate_hz / NOTCH_BIN_HZ))
        self.bin_hz = self.rate_hz / self.size
        self._window = hann(self.size).astype(np.float64)
        self._low = math.ceil(NOTCH_RANGE_HZ[0] / self.bin_hz)
        self._high = math.floor(NOTCH_RANGE_HZ[1] / self.bin_hz)
        self._width = math.ceil(NOTCH_WIDTH_HZ / 2 / self.bin_hz)      # bins on each side of a line's peak
        self._around = math.ceil(NOTCH_SURROUNDINGS_HZ / self.bin_hz)
        if self._low - self._width < 1 or self._high + self._width + 1 >= self.size // 2:
            raise ValueError(f"the notch cannot analyse audio at {self.rate_hz:g} Hz")
        # Where each line's surroundings lie, from its peak: beyond its width, within the surroundings.
        offsets = np.arange(-self._around, self._around + 1)
        self._offsets = offsets[np.abs(offsets) > self._width]
        self._recent = np.zeros(0)
        self._since = 0
        self._interval = max(1, round(NOTCH_ANALYSIS_S * self.rate_hz))
        self._time_s = 0.0          # of the output read
        self._lines: list[_Line] = []
        self.notches: tuple[float, ...] = ()

    def process(self, detected: np.ndarray) -> bool:
        """Read ``detected``, the detector's output; whether ``notches``
        changed."""
        recent = np.concatenate((self._recent, detected))
        self._recent = recent[-self.size:]
        self._since += len(detected)
        self._time_s += len(detected) / self.rate_hz
        if self._since < self._interval or len(self._recent) < self.size:
            return False
        self._since = 0
        self._follow(self._find(), self._time_s)
        return self._update()

    def _find(self) -> list[tuple[float, float]]:
        """The lines of the latest output: (frequency, margin over the surroundings)."""
        spectrum = np.fft.rfft(self._recent * self._window)
        db = 10 * np.log10(np.maximum(spectrum.real ** 2 + spectrum.imag ** 2, 1e-30))
        low, high, width = self._low, self._high, self._width
        middle = db[low:high + 1]
        below = db[low - width:high + 1 - width]
        above = db[low + width:high + 1 + width]
        peaks = ((middle >= db[low - 1:high]) & (middle > db[low + 1:high + 2])
                 & (middle - below >= NOTCH_MARGIN_DB) & (middle - above >= NOTCH_MARGIN_DB))
        found = low + np.flatnonzero(peaks)
        if not len(found):
            return []
        around = np.clip(found[:, np.newaxis] + self._offsets, 0, len(db) - 1)
        margins = db[found] - np.median(db[around], axis=1)
        lines = []
        for index, margin in zip(found[margins >= NOTCH_MARGIN_DB], margins[margins >= NOTCH_MARGIN_DB]):
            left, centre, right = db[index - 1:index + 2]
            curvature = left - 2 * centre + right
            offset = 0.5 * (left - right) / curvature if curvature < 0 else 0.0
            lines.append(((index + offset) * self.bin_hz, float(margin)))
        return lines

    def _follow(self, found: list[tuple[float, float]], now_s: float) -> None:
        """Match the lines found with those followed: a line not found again
        is dropped, unless notched, until its release."""
        kept: list[_Line] = []
        for line in self._lines:
            match = min(found, key=lambda item: abs(item[0] - line.frequency_hz), default=None)
            if match is not None and abs(match[0] - line.frequency_hz) <= NOTCH_MATCH_HZ:
                found.remove(match)
                line.frequency_hz, line.margin_db, line.last_s = match[0], match[1], now_s
                kept.append(line)
            elif line.notched and now_s - line.last_s <= NOTCH_RELEASE_S:
                kept.append(line)
        kept.extend(_Line(frequency, margin, now_s, now_s) for frequency, margin in found)
        steady = sorted((line for line in kept if not line.notched and line.last_s - line.first_s >= NOTCH_STEADY_S
                         - NOTCH_ANALYSIS_S / 2), key=lambda line: -line.margin_db)
        room = NOTCH_MAX - sum(line.notched for line in kept)
        for line in steady[:max(0, room)]:
            line.notched = True
        self._lines = kept

    def _update(self) -> bool:
        """Whether a notch came, went or moved."""
        wanted = sorted(line.frequency_hz for line in self._lines if line.notched)
        if len(wanted) == len(self.notches) and all(
                abs(new - old) < NOTCH_MOVE_HZ for new, old in zip(wanted, self.notches)):
            return False
        self.notches = tuple(wanted)
        return True


class NoiseBlanker:
    """Zeroes the impulses of the receiver's samples at ``rate_hz``, block
    by block, before the VFO's channel is made of them.

    An impulse is a short run of samples whose magnitude exceeds
    ``BLANKER_FACTOR`` times the block's median magnitude: they are zeroed,
    with ``BLANKER_SIDES`` samples on each side. Samples over the limit
    whose zeroed spans touch make one impulse. So that a legitimate strong
    signal is never blanked, an impulse that spans more than
    ``BLANKER_LONGEST`` samples is a signal, such as a strong carrier keyed
    on during the block, which its median does not hold, and is left as it
    is; and a block where the impulses would zero more than
    ``BLANKER_MOST`` of its samples, or than one impulse's reach in a block
    of fewer than 1000 samples, is left whole, as what crosses the limit
    that often is a signal or noise, not impulses. A signal that
    fills half the block or more is the median; AM at 100% modulation
    peaks at twice its carrier, FM's envelope is steady: neither crosses
    the limit.

    The median is that of every ``BLANKER_STRIDE``th sample, 5000 of a block
    at 2.4 MS/s, which estimates the block's within about 1% for noise, far
    finer than the factor of 6; a whole block's median took most of the
    blanker's time (spike 8). The impulses are sought in the samples' real
    and imaginary parts, which NumPy compares far faster than it computes
    magnitudes: a magnitude over the limit has a part over the limit over
    the square root of 2, and only the samples with such a part have their
    magnitude computed. A block without an impulse is given back as it is,
    not copied. A block whose median magnitude is 0, more than half of its
    samples exact zeros, is left alone.

    Across blocks: an impulse at the end of a block zeroes the first
    samples of the next; one at the start of a block does not reach back
    into the previous one, which the channel has taken, so at most
    ``BLANKER_SIDES`` samples before it stay. Each block judges its own
    part of a run over the limit: a strong signal that starts within the
    last ``BLANKER_LONGEST`` samples of a block, or ends within the first
    of one, loses those and ``BLANKER_SIDES`` more there, 18 samples at
    most at such an edge, 7.5 µs at 2.4 MS/s.

    ``zeroed`` holds the indices of the samples zeroed in the latest block,
    in order, so that the samples as received can be filtered from the
    blanked ones and the few zeroed (``FftFilter.process_sparse``).
    ``impulses`` is the number zeroed in the latest block, and
    ``per_second`` that of the latest second of samples, counted over the
    whole blocks that make it up and scaled to a second: 0 before the
    first second, it changes at most once a second. Not thread-safe."""

    def __init__(self, rate_hz: float) -> None:
        self.rate_hz = float(rate_hz)
        self.impulses = 0
        self.per_second = 0
        self._none = np.zeros(0, np.intp)
        self.zeroed = self._none    # the samples zeroed in the latest block, in order
        self._pending = 0           # samples to zero at the start of the next block
        self._counted = 0           # impulses in the second being counted
        self._samples = 0           # samples of that second so far

    def process(self, samples: np.ndarray) -> np.ndarray:
        """``samples`` with their impulses zeroed: the same array when there
        was nothing to zero, a new one otherwise."""
        count = len(samples)
        pending = min(self._pending, count)
        self._pending -= pending
        first, last = self._find(samples)
        low = np.maximum(first - BLANKER_SIDES, 0)
        high = np.minimum(last + BLANKER_SIDES, count - 1)
        if int(np.sum(high - low + 1)) > max(BLANKER_MOST * count, BLANKER_LONGEST + 2 * BLANKER_SIDES):
            low = high = np.zeros(0, np.intp)
        self.impulses = len(low)
        self._count(count)
        if not len(low) and not pending:
            self.zeroed = self._none
            return samples
        blanked = samples.copy()
        # Zeroed spans are apart from each other, each at most this long.
        spans = low[:, np.newaxis] + np.arange(BLANKER_LONGEST + 2 * BLANKER_SIDES)
        self.zeroed = np.union1d(np.arange(pending), spans[spans <= high[:, np.newaxis]])
        blanked[self.zeroed] = 0
        if len(low):
            self._pending = max(self._pending, int(last[-1]) + BLANKER_SIDES + 1 - count)
        return blanked

    def _find(self, samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The first and the last sample over the limit of each impulse of
        ``samples``, in order."""
        none = np.zeros(0, np.intp)
        if not len(samples):
            return none, none
        every = samples[::BLANKER_STRIDE]
        median = float(np.median(every.real.astype(np.float64) ** 2 + every.imag.astype(np.float64) ** 2))
        if not median > 0:          # silence, or samples that are not numbers
            return none, none
        limit = BLANKER_FACTOR ** 2 * median
        parts = np.ascontiguousarray(samples, np.complex64).view(np.float32)
        candidates = np.unique(np.flatnonzero(np.abs(parts) > math.sqrt(limit / 2)) // 2)
        if not len(candidates):
            return none, none
        values = samples[candidates]
        hits = candidates[values.real.astype(np.float64) ** 2 + values.imag.astype(np.float64) ** 2 > limit]
        if not len(hits):
            return none, none
        apart = np.diff(hits) > 2 * BLANKER_SIDES + 1
        first = hits[np.flatnonzero(np.concatenate(([True], apart)))]
        last = hits[np.flatnonzero(np.concatenate((apart, [True])))]
        short = last - first < BLANKER_LONGEST
        return first[short], last[short]

    def _count(self, count: int) -> None:
        self._counted += self.impulses
        self._samples += count
        if self._samples >= self.rate_hz:
            self.per_second = round(self._counted * self.rate_hz / self._samples)
            self._counted = self._samples = 0


class Demodulator:
    """Turns blocks of receiver samples into a channel level, a squelch
    state and audio. Not thread-safe: one block at a time."""

    def __init__(self) -> None:
        self._rate_hz: float | None = None
        self._wide = False                  # in WFM, at its channel rate
        self._front: FftFilter | None = None
        self._channel: FftFilter | None = None
        self._channel_key: tuple[str, float, float] | None = None
        self._noise_bandwidth_hz = 0.0
        self.channel_rate_hz = CHANNEL_RATE_HZ
        self._decimation = AUDIO_DECIMATION
        self._window = 1
        self._powers = np.zeros(0)          # the last channel powers, for the moving average
        self._open = False
        self._audio: _AudioPath | None = None
        self._search: CarrierSearch | None = None   # in SAM, with or without audio
        self._blanker: NoiseBlanker | None = None   # while the noise blanker is on
        # While the blanker is on and the channel or the front filter's output
        # is tapped: the front filter of the samples zeroed alone, whose
        # output added to the front's gives the unblanked samples' that the
        # taps take, and the channel filter after it while the channel is.
        self._unblanked: tuple[FftFilter, FftFilter | None] | None = None
        self.position = 0                   # audio samples sent, modulo 2^32

    @property
    def audio_rate_hz(self) -> float:
        return self.channel_rate_hz / self._decimation

    def process(self, samples: np.ndarray, centre_hz: float, rate_hz: float, settings: DemodSettings,
                noise_density: float | None, audio: bool,
                tap: Callable[[np.ndarray], None] | None = None,
                front_tap: Callable[[np.ndarray], None] | None = None) -> tuple[Meter, np.ndarray | None]:
        """The meter for ``samples``, received at ``centre_hz`` and ``rate_hz``,
        and with ``audio`` the 16-bit audio they make. ``noise_density`` is
        the noise power per hertz around the VFO, for the automatic squelch.
        ``tap`` receives the channel, at ``channel_rate_hz``, before any
        detector: its 0 Hz is the VFO's frequency less ``vfo_position_hz``.
        ``front_tap`` receives the front filter's output, the same 0 Hz and
        rate before the channel filter, for the fine spectrum (fine.py),
        and nothing while the VFO lies outside the received band.
        A VFO outside the received band reads nothing and stays silent; a
        receiver slower than the channel rate gives no meter, no audio and
        no channel. In SAM the carrier is sought with or without audio, so
        that the meter says whether SAM holds one. The notch reads the
        audio path's detector: without audio it removes nothing. The noise
        blanker, in every mode, acts on the samples before the channel is
        made of them, with or without audio, so the meter, the squelch and
        SAM's search take the blanked channel; ``tap`` and ``front_tap``
        take the samples as received while the blanker is on and has
        blanked since a tap began: the front filter's output plus what a
        silent copy of it gives for the samples zeroed alone, a few, which
        costs far less than filtering every sample again, and for ``tap`` a
        copy of the channel filter."""
        wide = settings.mode == "WFM"
        if rate_hz != self._rate_hz or wide != self._wide:
            self._start(rate_hz, wide)
        if self._front is None or self._channel is None:
            return Meter(None, None, False), None       # too slow a receiver for a channel
        if settings.blanker:
            if self._blanker is None:
                self._blanker = NoiseBlanker(rate_hz)
            blanked = self._blanker.process(samples)
        else:
            self._blanker = None
            blanked = samples
        shift_hz = settings.frequency_hz - centre_hz
        front_shift_hz = shift_hz - vfo_position_hz(settings.mode)
        self._front.set_shift(front_shift_hz)
        key = (settings.mode, settings.bandwidth_hz, self.channel_rate_hz)
        taps: np.ndarray | None = None
        if key != self._channel_key:
            taps = channel_taps(settings.mode, settings.bandwidth_hz, self.channel_rate_hz)
            self._channel.set_taps(taps)
            self._channel_key = key
            self._noise_bandwidth_hz = float(np.sum(np.abs(taps) ** 2)) * self.channel_rate_hz
        if (tap is None and front_tap is None) or not settings.blanker:
            self._unblanked = None
        elif self._unblanked is not None:
            difference, channel_copy = self._unblanked
            difference.set_shift(front_shift_hz)
            if tap is None:
                channel_copy = None
            elif channel_copy is None:
                # The tap begins while the front's difference runs: from
                # here on, a copy of the channel filter follows it.
                channel_copy = copy.deepcopy(self._channel)
            elif taps is not None:
                channel_copy.set_taps(taps)
            self._unblanked = (difference, channel_copy)
        elif blanked is not samples:
            # Until now the channel's filters took the samples as received:
            # from here on, the front filter's output plus what it gives for
            # the samples zeroed, and a copy of the channel filter, do so.
            self._unblanked = (self._front.silent_copy(), None if tap is None else copy.deepcopy(self._channel))
        received = abs(shift_hz) <= rate_hz / 2         # false: the VFO lies outside the received band
        front_output = self._front.process(blanked)
        channel = self._channel.process(front_output)
        if not received:
            channel = np.zeros_like(channel)
        if self._unblanked is not None:
            # The filters are linear: the samples as received filter to the
            # blanked ones' output plus that of the few samples zeroed.
            assert self._blanker is not None    # the blanker is on
            zeroed = self._blanker.zeroed
            front_output = front_output + self._unblanked[0].process_sparse(len(samples), zeroed, samples[zeroed])
        if front_tap is not None and received:
            front_tap(front_output)
        if tap is not None:
            if self._unblanked is None:
                tap(channel)
            else:
                channel_copy = self._unblanked[1]
                assert channel_copy is not None     # made with the tap
                unblanked = channel_copy.process(front_output)
                tap(unblanked if received else np.zeros_like(unblanked))

        power = channel.real.astype(np.float64) ** 2 + channel.imag.astype(np.float64) ** 2
        mean = float(power.mean()) if len(power) else 0.0
        level_db = 10 * math.log10(mean) if mean > 1e-20 else None
        if settings.squelch_auto:
            threshold = (10 * math.log10(noise_density * self._noise_bandwidth_hz) + SQUELCH_MARGIN_DB
                         if noise_density else None)
        else:
            threshold = settings.squelch_db
        gate = self._squelch(power, threshold)
        locked = None
        if settings.mode == "SAM":
            if self._search is None:
                self._search = CarrierSearch(self.channel_rate_hz)
            self._search.add(channel)
            locked = self._search.locked
        else:
            self._search = None

        impulses = None if self._blanker is None else self._blanker.per_second
        if not audio:
            self._audio = None
            return Meter(level_db, threshold, self._open, locked, 0 if settings.notching else None, impulses), None
        if self._audio is None or self._audio.mode != settings.mode:
            self._audio = _AudioPath(settings.mode, self.channel_rate_hz, self._decimation, self._search,
                                     settings.notch)
        else:
            self._audio.set_notch(settings.notch)
        sound = self._audio.process(channel, power, gate)
        pcm = np.clip(np.rint(sound * 32767), -32768, 32767).astype("<i2")
        notches = self._audio.notches
        return Meter(level_db, threshold, self._open, locked, None if notches is None else len(notches),
                     impulses), pcm

    def encode_audio(self, pcm: np.ndarray) -> bytes:
        """An audio frame (docs/design/protocol.md) for samples that follow the previous ones."""
        frame = AUDIO_HEADER.pack(FRAME_KIND_AUDIO, AUDIO_VERSION, 0, self.position, self.audio_rate_hz)
        self.position = (self.position + len(pcm)) & 0xFFFFFFFF
        return frame + pcm.tobytes()

    def _start(self, rate_hz: float, wide: bool) -> None:
        """New receiver rate, or into or out of WFM: a new front filter, and
        new channel and audio filters if the channel rate changes with it. A
        receiver slower than the channel rate gives no channel at all."""
        if rate_hz != self._rate_hz:
            self._blanker = None        # its impulses counted at the old rate
        self._unblanked = None          # a new front filter
        self._rate_hz, self._wide = rate_hz, wide
        target_hz = WFM_CHANNEL_RATE_HZ if wide else CHANNEL_RATE_HZ
        self._decimation = WFM_AUDIO_DECIMATION if wide else AUDIO_DECIMATION
        if not rate_hz >= target_hz:
            self._front = None
            self._open = False
            self._audio = None
            self._search = None
            return
        up, down = rate_ratio(rate_hz, target_hz)
        cutoff_hz, transition_hz = ((WFM_FRONT_CUTOFF_HZ, WFM_FRONT_TRANSITION_HZ) if wide
                                    else (FRONT_CUTOFF_HZ, FRONT_TRANSITION_HZ))
        front = lowpass(cutoff_hz, transition_hz, rate_hz, CHANNEL_ATTENUATION_DB)
        self._front = FftFilter(front, rate_hz, up=up, down=down)
        channel_rate = self._front.output_rate_hz
        if self._channel is None or channel_rate != self.channel_rate_hz:
            self.channel_rate_hz = channel_rate
            self._channel = FftFilter(np.ones(1), channel_rate)
            self._channel_key = None
            self._window = max(1, round(SQUELCH_WINDOW_S * channel_rate))
            self._powers = np.zeros(self._window - 1)
            self._audio = None
            self._search = None

    def _squelch(self, power: np.ndarray, threshold_db: float | None) -> np.ndarray:
        """Open (1) or closed (0) at each channel sample: it opens once the
        power averaged over 10 ms reaches the threshold, and closes once that
        average falls ``SQUELCH_HYSTERESIS_DB`` below it. Without a
        threshold it stays closed."""
        window = self._window
        history = np.concatenate((self._powers, power))
        self._powers = history[len(history) - (window - 1):]
        if threshold_db is None:
            self._open = False
        if threshold_db is None or not len(power):
            return np.full(len(power), float(self._open))
        sums = np.cumsum(np.concatenate(([0.0], history)))
        averages = (sums[window:] - sums[:-window]) / window
        # +1 where the average opens the squelch, 0 where it closes it,
        # -1 in between, where the previous state holds.
        events = np.full(len(power), -1)
        events[averages >= 10 ** (threshold_db / 10)] = 1
        events[averages < 10 ** ((threshold_db - SQUELCH_HYSTERESIS_DB) / 10)] = 0
        latest = np.maximum.accumulate(np.where(events >= 0, np.arange(len(power)), -1))
        gate = np.where(latest >= 0, events[latest], int(self._open)).astype(np.float64)
        self._open = bool(gate[-1])
        return gate


class _AudioPath:
    """A mode's detector, the squelch gate and the audio filter: the channel
    rate in, the audio rate, ``decimation`` times lower, out.

    The gate acts on the detector's output, before the audio filter, whose
    long response would otherwise spread the noise around a closing squelch.
    Its decision averages the last 10 ms, so the detector's output waits that
    long, plus a fade, for the gate to look ahead: a sample passes only if
    the squelch stays open until the decisions about it are in, and the gate
    fades out before the noise that follows a carrier. The AM detector and
    the AGC use that wait to look ahead too, and SAM's band-pass lags within
    it. SAM takes ``search``, the demodulator's, which finds its carrier.

    With ``notch``, in NOTCH_MODES, ``AutoNotch`` reads the detector's
    output, before the gate, so that a line is found whether or not the
    squelch is open, and its notches go into the audio filter's taps,
    computed again, in the thread that processes the block, only when a
    notch comes, goes or moves. The filter keeps the samples it remembers,
    so the new taps would take over between two samples, and the audio
    would step by what they remove; instead, a copy of the filter keeps the
    old taps for ``NOTCH_FADE_S``, and the audio fades linearly from its
    output to the new one, which runs in step: from one sample to the next
    it moves no more than either output does, plus their difference, the
    removed line, spread over the fade. A change during a fade fades on
    from the same old taps to the newest."""

    def __init__(self, mode: str, channel_rate_hz: float, decimation: int,
                 search: CarrierSearch | None = None, notch: bool = False) -> None:
        self.mode = mode
        self._rate_hz = channel_rate_hz
        window = max(1, round(SQUELCH_WINDOW_S * channel_rate_hz))
        self._fade = max(1, round(FADE_S * channel_rate_hz))
        self._span = window + self._fade
        self._detector: _FmDetector | _AmDetector | SyncDetector | _SidebandDetector
        if mode == "NFM":
            self._detector = _FmDetector(channel_rate_hz, self._span, MAX_DEVIATION_HZ)
        elif mode == "WFM":
            self._detector = _FmDetector(channel_rate_hz, self._span, WFM_MAX_DEVIATION_HZ)
        elif mode == "AM":
            self._detector = _AmDetector(channel_rate_hz, self._span)
        elif mode == "SAM":
            if search is None:
                raise ValueError("SAM needs the search for its carrier")
            self._detector = SyncDetector(channel_rate_hz, self._span, search)
        else:
            self._detector = _SidebandDetector(channel_rate_hz, self._span, window)
        self._filter = FftFilter(audio_taps(mode, channel_rate_hz), channel_rate_hz, up=1, down=decimation)
        self._decisions = np.zeros(self._span)      # the squelch's last decisions
        self._passed = np.zeros(self._fade - 1)     # the gate's last values, for the fades
        self._notch: AutoNotch | None = None
        self._leaving: FftFilter | None = None      # the filter with the old taps, while the audio fades from it
        self._faded = 0                             # audio samples of that fade so far
        self._fade_out = max(1, round(NOTCH_FADE_S * self._filter.output_rate_hz))
        self.set_notch(notch)

    @property
    def notches(self) -> tuple[float, ...] | None:
        """The frequencies notched, lowest first; None while the notch does
        not act."""
        return None if self._notch is None else self._notch.notches

    def set_notch(self, on: bool) -> None:
        """Turn the automatic notch on or off; it acts in NOTCH_MODES only.
        Turned off, its notches leave the taps, through a fade; turned on
        again, it starts afresh."""
        acts = on and self.mode in NOTCH_MODES
        if acts == (self._notch is not None):
            return
        if acts:
            self._notch = AutoNotch(self._rate_hz)
            return
        notched = self._notch is not None and self._notch.notches
        self._notch = None
        if notched:
            self._retap(())

    def _retap(self, notches: Sequence[float]) -> None:
        """New taps for the audio filter, which the audio fades into."""
        if self._leaving is None:
            self._leaving = copy.deepcopy(self._filter)
            self._faded = 0
        self._filter.set_taps(audio_taps(self.mode, self._rate_hz, notches))

    def process(self, channel: np.ndarray, power: np.ndarray, gate: np.ndarray) -> np.ndarray:
        """Audio for ``channel``, whose power at each sample is ``power``,
        silent where ``gate``, the squelch's decision at each channel sample,
        is closed."""
        detected = self._detector.process(channel, power)
        if self._notch is not None and self._notch.process(detected):
            self._retap(self._notch.notches)
        # A detected sample passes if every decision from its own to the latest is open.
        span = self._span
        decisions = np.concatenate((self._decisions, gate))
        self._decisions = decisions[len(gate):]
        sums = np.cumsum(np.concatenate(([0.0], decisions)))
        passed = (sums[span + 1:] - sums[:-(span + 1)]) > span + 0.5
        # A moving average of the gate turns its steps into linear fades.
        recent = np.concatenate((self._passed, passed))
        self._passed = recent[len(recent) - (self._fade - 1):]
        sums = np.cumsum(np.concatenate(([0.0], recent)))
        fades = (sums[self._fade:] - sums[:-self._fade]) / self._fade
        gated = (detected * fades).astype(np.complex64)
        audio = self._filter.process(gated).real.astype(np.float64)
        if self._leaving is None:
            return audio
        old = self._leaving.process(gated).real.astype(np.float64)
        mix = np.minimum((self._faded + np.arange(1, len(audio) + 1)) / self._fade_out, 1.0)
        self._faded += len(audio)
        if self._faded >= self._fade_out:
            self._leaving = None
        return old + mix * (audio - old)


class _Delay:
    """Real samples, given back ``samples`` later."""

    def __init__(self, samples: int) -> None:
        self._waiting = np.zeros(samples)

    def process(self, values: np.ndarray) -> np.ndarray:
        both = np.concatenate((self._waiting, values))
        self._waiting = both[len(values):]
        return both[:len(values)]


class _FmDetector:
    """FM discriminator: the phase step from one channel sample to the next,
    full scale at ``max_deviation_hz``, ``delay`` samples late."""

    def __init__(self, channel_rate_hz: float, delay: int, max_deviation_hz: float) -> None:
        self._scale = channel_rate_hz / (2 * math.pi * max_deviation_hz)
        self._previous = np.complex64(0)
        self._delay = _Delay(delay)

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        previous = np.concatenate(([self._previous], channel[:-1])) if len(channel) else channel
        if len(channel):
            self._previous = channel[-1]
        return self._delay.process(np.angle(channel * np.conj(previous)) * self._scale)


class _AmDetector:
    """Envelope detector, ``delay`` samples late. The envelope over the
    carrier's level, less one, is the modulation, whatever the signal's
    strength: AM needs no other AGC, and the carrier never reaches the
    squelch's gate, which would make it thump. The carrier's level is the
    higher of the envelope's averages over the 10 ms before and the 10 ms
    after each sample, which the delay makes available: where a carrier
    starts or stops, one of them still holds all of it."""

    def __init__(self, channel_rate_hz: float, delay: int) -> None:
        self._window = min(max(1, round(AM_WINDOW_S * channel_rate_hz)), delay)
        self._before = np.zeros(self._window + delay)   # the envelope's last samples

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        count, window = len(channel), self._window
        envelope = np.concatenate((self._before, np.abs(channel).astype(np.float64)))
        self._before = envelope[count:]
        # Output i is envelope[window + i], between envelope[i:window + i]
        # and envelope[window + i + 1:2 * window + i + 1].
        sums = np.cumsum(np.concatenate(([0.0], envelope)))
        before = sums[window:window + count] - sums[:count]
        after = sums[2 * window + 1:2 * window + 1 + count] - sums[window + 1:window + 1 + count]
        carrier = np.maximum(before, after) / window
        audio = np.zeros(count)
        heard = carrier > 1e-9                  # -180 dBFS: no carrier at all, silence
        audio[heard] = envelope[window:window + count][heard] / carrier[heard] - 1
        return audio * AM_LEVEL


def sam_band_pass_length(rate_hz: float) -> int:
    """The length of each of SAM's moving averages at ``rate_hz``: odd, so
    that each lags by a whole number of samples, and such that the cascade
    has half its gain at ``SAM_BAND_HZ / 2`` from 0 Hz."""
    return max(1, round(SINC_CUBED_HALF * rate_hz / (SAM_BAND_HZ / 2))) | 1


class MovingAverages:
    """``stages`` moving averages of ``length`` samples in cascade, on a
    stream of complex samples, each by cumulative sums: a low-pass filter
    whose response is sinc(f * length / rate) to the power ``stages``,
    lagging by ``stages * (length - 1) / 2`` samples, whose cost does not
    grow with its length. Not thread-safe."""

    def __init__(self, length: int, stages: int) -> None:
        if length < 1 or stages < 1:
            raise ValueError(f"cannot average {stages} times over {length} samples")
        self.length = length
        self._histories = [np.zeros(length - 1, np.complex128) for _ in range(stages)]

    def process(self, values: np.ndarray) -> np.ndarray:
        length = self.length
        for stage, history in enumerate(self._histories):
            both = np.concatenate((history, values))
            self._histories[stage] = both[len(both) - (length - 1):]
            sums = np.concatenate(([0], np.cumsum(both)))
            values = (sums[length:] - sums[:-length]) / length
        return values


class CarrierSearch:
    """SAM's carrier in the channel at ``channel_rate_hz``, whose 0 Hz is the
    VFO, and its lock.

    Every ``SAM_SEARCH_S`` it takes an FFT of the latest ``SAM_SEARCH_SIZE``
    samples of the channel (Hann) and finds the strongest line within
    ``SAM_SEARCH_HZ`` of the VFO, or, while locked, within the band-pass
    around the carrier it follows, so that a sideband's line that outgrows a
    fading carrier does not take it; the line is placed by a parabola
    through the levels in dB. Its margin is the line's power over the
    noise's within the band-pass: the power of the line's main lobe, less
    the noise in its bins, over the median of the searched bins, which is
    the noise's mean power per bin times ln 2, scaled to ``SAM_BAND_HZ``.
    One search's margin scatters by 1 to 2 dB around the carrier's, so the
    lock goes by the mean of the latest ``SAM_LOCK_SEARCHES``: under
    ``SAM_LOCK_DB`` no lock is gained, and one held is lost ``SAM_HOLD_DB``
    lower. Search by search, a carrier at 10 dB gained and lost the lock
    five times a second. Without a lock the next search looks within
    ``SAM_SEARCH_HZ`` again. ``frequency_hz`` keeps the latest carrier found
    after a loss. Not thread-safe."""

    def __init__(self, channel_rate_hz: float) -> None:
        self.rate_hz = float(channel_rate_hz)
        self.locked = False
        self.carrier_hz: float | None = None    # from the channel's 0 Hz, while locked
        self.margin_db: float | None = None     # the carrier over the noise within the band-pass, at the latest search
        self._margins: collections.deque[float] = collections.deque(maxlen=SAM_LOCK_SEARCHES)
        self.frequency_hz = 0.0                 # the latest carrier found, from the channel's 0 Hz
        self.acquisitions = 0                   # the locks gained so far
        self._window = hann(SAM_SEARCH_SIZE)
        frequencies = np.fft.fftfreq(SAM_SEARCH_SIZE, 1 / self.rate_hz)
        order = np.argsort(frequencies)
        self._bins = order[np.abs(frequencies[order]) <= SAM_SEARCH_HZ]    # the searched bins, lowest first
        self._frequencies = frequencies[self._bins]
        self._recent = np.zeros(0, np.complex64)    # the latest channel
        self._since = 0                             # samples since the last search
        self._interval = max(1, round(SAM_SEARCH_S * self.rate_hz))

    def add(self, channel: np.ndarray) -> None:
        """Take a block of the channel; search again once ``SAM_SEARCH_S`` of
        it came."""
        recent = np.concatenate((self._recent, channel))
        self._recent = recent[-SAM_SEARCH_SIZE:]
        self._since += len(channel)
        if self._since < self._interval or len(self._recent) < SAM_SEARCH_SIZE:
            return
        self._since = 0
        spectrum = np.fft.fft(self._recent * self._window)[self._bins]
        power = spectrum.real.astype(np.float64) ** 2 + spectrum.imag.astype(np.float64) ** 2
        median = float(np.median(power))
        if self.locked:
            near = np.flatnonzero(np.abs(self._frequencies - self.frequency_hz) <= SAM_BAND_HZ / 2)
            peak = int(near[np.argmax(power[near])]) if len(near) else 0
        else:
            peak = int(np.argmax(power))
        if not 0 < peak < len(power) - 1 or power[peak] <= 0:
            self._lose()
            return
        lobe = power[max(0, peak - SAM_LOBE_BINS):peak + SAM_LOBE_BINS + 1]
        # The carrier's power over the noise's within the band-pass, from the
        # bins' sums: Parseval for the line, less the noise in its bins, and
        # an exponential median for the noise.
        line = float(lobe.sum()) - len(lobe) * median / math.log(2)
        margin = (math.inf if median <= 0 else -math.inf if line <= 0 else
                  10 * math.log10(line * math.log(2) * self.rate_hz / (SAM_SEARCH_SIZE * median * SAM_BAND_HZ)))
        self.margin_db = margin
        self._margins.append(min(max(margin, SAM_MARGIN_DB[0]), SAM_MARGIN_DB[1]))
        steady = sum(self._margins) / len(self._margins)
        if steady < (SAM_LOCK_DB - SAM_HOLD_DB if self.locked else SAM_LOCK_DB):
            self._lose()
            return
        left, centre, right = (10 * math.log10(max(float(value), 1e-30)) for value in power[peak - 1:peak + 2])
        curvature = left - 2 * centre + right
        offset = 0.5 * (left - right) / curvature if curvature < 0 else 0.0
        self.frequency_hz = float(self._frequencies[peak] + offset * self.rate_hz / SAM_SEARCH_SIZE)
        self.carrier_hz = self.frequency_hz
        if not self.locked:
            self.acquisitions += 1
        self.locked = True

    def _lose(self) -> None:
        self.locked = False
        self.carrier_hz = None


class SyncDetector:
    """Synchronous AM detector for the channel at ``channel_rate_hz``, whose
    0 Hz is the VFO, ``delay`` samples late, as the AM detector; ``search``,
    given each block of the channel before this detector, finds the carrier.

    The channel is turned by a phase that runs at the carrier's frequency,
    then filtered by the band-pass, three moving averages in cascade
    (``MovingAverages``, ``sam_band_pass_length``), which keeps the carrier
    and its slow drifts; the angle of what comes out is the carrier's
    phase. The audio is the real part of the channel turned by that phase,
    less the carrier's amplitude, over a level that follows the carrier's at
    once and falls at the SSB AGC's 10 dB a second: a carrier that fades
    alone, as in selective fading, leaves the sidebands' audio as it is.
    The band-pass lags by less than ``delay``; the channel waits for the
    rest, so that the squelch's gate stays in step.

    Without a lock, the envelope, as AM, which is computed for every block.
    A lock gained waits for the band-pass to hold only the carrier found,
    its span of samples, then the level starts from the larger of the
    carrier's mean amplitude and the channel's, AM's level, so that a lock
    gained in a fade of the carrier alone is no louder than AM. The switch
    between the two detectors fades over ``SAM_FADE_S``, from one's output to
    the other's, which are in step: the audio moves no further than they
    differ. Not thread-safe."""

    def __init__(self, channel_rate_hz: float, delay: int, search: CarrierSearch) -> None:
        if delay < 1:
            raise ValueError(f"SAM needs a delay of at least one sample, not {delay}")
        self.rate_hz = float(channel_rate_hz)
        self.search = search
        length = sam_band_pass_length(self.rate_hz)
        lag = SAM_STAGES * (length - 1) // 2
        if lag > delay:
            raise ValueError(f"SAM's band-pass lags by {lag} samples, more than the delay of {delay}")
        self._filter = MovingAverages(length, SAM_STAGES)
        self._span = SAM_STAGES * (length - 1) + 1         # the band-pass's response, in samples
        self._envelope = _AmDetector(self.rate_hz, delay)
        self._early = np.zeros(delay - lag, np.complex64)   # the channel, waiting for the rest of the delay
        self._turned = np.zeros(lag, np.complex64)          # the turned channel, waiting for the band-pass
        self._phase = 0.0                                   # radians, of the turn
        self._level: float | None = None                    # the carrier's level at the end of the last block
        self._acquisitions = search.acquisitions            # the search's, as this detector last saw them
        self._settling = self._span if search.locked else 0     # samples before the band-pass holds the carrier
        self._fade = max(1, round(SAM_FADE_S * self.rate_hz))
        self._mix = 0.0                                     # 0: the envelope, 1: synchronous, at the last sample

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        """The audio of ``channel``, ``delay`` samples late; ``power`` is the
        channel's power at each sample, which the envelope detector takes."""
        count = len(channel)
        envelope = self._envelope.process(channel, power)
        if not count:
            return envelope
        search = self.search
        if search.acquisitions != self._acquisitions:
            self._acquisitions = search.acquisitions
            self._settling = self._span
        if len(self._early):
            waiting = np.concatenate((self._early, channel))
            self._early = waiting[count:]
            channel = waiting[:count]
        step = 2 * np.pi * search.frequency_hz / self.rate_hz
        phases = (self._phase + step * np.arange(count)).astype(np.float32)
        self._phase = float((self._phase + step * count) % (2 * np.pi))
        turned = channel * (np.cos(phases) - 1j * np.sin(phases)).astype(np.complex64)
        carrier = self._filter.process(turned)
        waiting = np.concatenate((self._turned, turned))
        self._turned = waiting[count:]
        late = waiting[:count]              # in step with the band-pass's output
        amplitude = np.abs(carrier)
        mean = float(amplitude.mean())
        start = mean if self._level is None else self._level
        if self._level is None and self._settling <= 0:
            start = max(mean, float(np.abs(late).mean()))
        level = self._carrier_level(start, mean, count)
        heard = level[-1] > SAM_FLOOR
        wanted = search.locked and heard and self._settling <= 0
        if self._settling > 0:
            # The band-pass still fills: the level starts again from the
            # block after, rather than rise across it.
            self._level = None
            self._settling = max(0, self._settling - count)
        if not wanted and self._mix <= 0.0:
            return envelope
        sync = self._synchronous(late, carrier, amplitude, level) if heard else envelope
        if wanted and self._mix >= 1.0:
            return sync
        ramp = np.arange(1, count + 1) / self._fade
        mix = np.clip(self._mix + ramp if wanted else self._mix - ramp, 0.0, 1.0)
        self._mix = float(mix[-1])
        return envelope + mix * (sync - envelope)

    @staticmethod
    def _synchronous(late: np.ndarray, carrier: np.ndarray, amplitude: np.ndarray,
                     level: np.ndarray) -> np.ndarray:
        """The synchronous detector's output: ``late``, the turned channel in
        step with ``carrier``, the band-pass's output, along the carrier, less
        its ``amplitude``, over ``level``."""
        # Where the carrier vanishes, so does the part of the channel along it.
        in_phase = (late.real * carrier.real + late.imag * carrier.imag) / np.maximum(amplitude, SAM_FLOOR)
        return AM_LEVEL * (in_phase - amplitude) / np.maximum(level, SAM_FLOOR)

    def _carrier_level(self, previous: float, mean: float, count: int) -> np.ndarray:
        """The level across this block: from ``previous``, the last block's,
        to this one's, which follows ``mean``, the carrier's mean amplitude,
        at once when it rises and falls no faster than the AGC's decay."""
        decayed = previous * 10 ** (-AGC_DECAY_DB_S * count / self.rate_hz / 20)
        self._level = max(mean, decayed)
        return np.linspace(previous, self._level, count + 1)[1:]


class _SidebandDetector:
    """USB, LSB and CW: the channel's real part, which is the sideband's
    audio (in CW, the front filter put the VFO at the CW pitch), ``delay``
    samples late, under an AGC that brings a steady signal to the same level
    whatever its strength. The AGC follows the channel's power averaged over
    ``window`` samples: it takes the gain down as soon as that average rises,
    ``window`` samples ahead of the audio thanks to the delay, and lets it
    come back at 10 dB per second."""

    def __init__(self, channel_rate_hz: float, delay: int, window: int) -> None:
        self._window = window
        self._delay = _Delay(delay)
        self._target_db = 20 * math.log10(AGC_LEVEL)
        self._decay_db = AGC_DECAY_DB_S / channel_rate_hz       # per sample
        self._powers = np.zeros(window - 1)                     # the last powers, for the average
        self._levels = np.full(delay - window, AGC_FLOOR_DB)    # levels that the next outputs wait for
        self._level = AGC_FLOOR_DB                              # the latest level

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        count, window = len(channel), self._window
        audio = self._delay.process(channel.real.astype(np.float64))
        history = np.concatenate((self._powers, power))
        self._powers = history[len(history) - (window - 1):]
        sums = np.cumsum(np.concatenate(([0.0], history)))
        averages = (sums[window:] - sums[:-window]) / window
        averages_db = 10 * np.log10(np.maximum(averages, 10 ** (AGC_FLOOR_DB / 10)))
        # The level follows each rise at once and falls no faster than the
        # decay: the running maximum of the averages, each less the decay
        # since it.
        decay = np.arange(count) * self._decay_db
        levels = np.maximum.accumulate(np.concatenate(([self._level - self._decay_db], averages_db + decay)))
        levels = levels[1:] - decay
        if count:
            self._level = float(levels[-1])
        # Output i takes its gain from the level ``window`` samples after it.
        levels = np.concatenate((self._levels, levels))
        self._levels = levels[count:]
        return audio * 10 ** ((self._target_db - levels[:count]) / 20)
