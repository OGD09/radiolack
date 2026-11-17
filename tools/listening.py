"""Prototypes of the listening tools (later-milestones.md, section 2), for
the spike that measures their cost (`tools/measure_listening.py`) and as the
starting point of the pull requests that build them for good.

Each one is written as the design says and as it could move into the
package: vectorised NumPy, block by block, with no loop per sample, and no
knowledge of the engine, the stream or the interface. Synchronous AM, the
first, moved into the package with its pull request: demod.py's
``CarrierSearch`` and ``SyncDetector``, the SAM mode, which the measure
takes from there.

- ``AutoNotch``, the automatic notch: it reads the detector's output, finds
  the lines at least 15 dB over their surroundings, at most 30 Hz wide,
  steady for half a second, between 100 Hz and 5 kHz, four at most, and
  gives the audio filter's taps with them removed (``notch_taps``); a notch
  is released a second after its line stops. The audio filter keeps its
  length, so a notch costs no filtering of its own.
- ``NoiseBlanker``: on the samples that go to the VFO's channel, those over
  six times the block's median magnitude are zeroed, with two samples on
  each side.
- ``FineSpectrum``: FFTs of 8192 bins of the VFO's channel before the
  demodulator, about ten frames a second, each the mean of those since the
  last.

They live in `tools/` until their pull requests move them: the Debian
package and pip take `src/radiolack` alone.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from radiolack.demod import AUDIO_ATTENUATION_DB, AUDIO_HIGH_PASS_HZ, AUDIO_LOW_PASS_HZ, AUDIO_TRANSITION_HZ
from radiolack.dsp import hann, shaped

# The automatic notch.
NOTCH_MARGIN_DB = 15.0      # a line stands this far over its surroundings
NOTCH_WIDTH_HZ = 30.0       # and is at most this wide
NOTCH_STEADY_S = 0.5        # for this long, before it is removed
NOTCH_RELEASE_S = 1.0       # removed until this long after it stops
NOTCH_RANGE_HZ = (100.0, 5_000.0)
NOTCH_MAX = 4
NOTCH_BIN_HZ = 6.0          # the analysis's bins are at most this wide
NOTCH_ANALYSIS_S = 0.1      # one analysis this often
NOTCH_SURROUNDINGS_HZ = 300.0   # a line's surroundings: the bins this far on each side, beyond its own
NOTCH_MATCH_HZ = 9.0        # a line found again this close to where it was is the same line
NOTCH_MOVE_HZ = 5.0         # a notch follows its line once it moved this far
# Each notch removes the line's width and half the audio filter's transition
# on each side, so that the filter's whole attenuation reaches the line.
NOTCH_HALF_HZ = NOTCH_WIDTH_HZ / 2 + AUDIO_TRANSITION_HZ / 2

# The noise blanker.
BLANKER_FACTOR = 6.0        # samples over this many times the block's median magnitude are impulses
BLANKER_SIDES = 2           # samples zeroed on each side of one
BLANKER_STRIDE = 16         # the block's median magnitude, from every 16th of its samples

# The fine spectrum.
FINE_SIZE = 8192
FINE_FRAME_RATE = 10.0      # frames a second, about


def notch_response(frequencies: np.ndarray, notches: Sequence[float]) -> np.ndarray:
    """The audio filter's gain in AM, SAM, USB and LSB (demod.audio_taps),
    with ``notches`` removed: each zeroes ``NOTCH_HALF_HZ`` on each side of
    its line."""
    gain = ((frequencies >= AUDIO_HIGH_PASS_HZ) & (frequencies <= AUDIO_LOW_PASS_HZ)).astype(np.float64)
    for notch in notches:
        gain[np.abs(frequencies - notch) <= NOTCH_HALF_HZ] = 0.0
    return gain


def notch_taps(notches: Sequence[float], rate_hz: float) -> np.ndarray:
    """The audio filter's taps with ``notches`` removed, as many as without
    any: demod.audio_taps for those modes, with the notches."""
    return shaped(lambda frequencies: notch_response(frequencies, notches), AUDIO_TRANSITION_HZ, rate_hz,
                  AUDIO_ATTENUATION_DB)


@dataclass
class _Line:
    frequency_hz: float
    margin_db: float
    first_s: float      # found first, without a gap since
    last_s: float       # found last
    notched: bool = False


class AutoNotch:
    """Finds the lines to remove in the detector's output, real at
    ``rate_hz``, before the audio filter.

    Every ``NOTCH_ANALYSIS_S`` it computes a Hann-windowed FFT of the latest
    output with bins at most ``NOTCH_BIN_HZ`` wide. A line is a bin
    between 100 Hz and 5 kHz that is the highest of its neighbours, whose
    level falls by ``NOTCH_MARGIN_DB`` within ``NOTCH_WIDTH_HZ / 2`` on both
    sides (at most 30 Hz wide) and that stands ``NOTCH_MARGIN_DB`` over the
    median of its surroundings. Found again within ``NOTCH_MATCH_HZ`` at
    every analysis for ``NOTCH_STEADY_S``, it is notched, four at most, the
    strongest first; a notch is released ``NOTCH_RELEASE_S`` after its line
    was last found. The analysis reads what comes before the audio filter,
    so a notched line is still found while it lasts. Not thread-safe."""

    def __init__(self, rate_hz: float) -> None:
        self.rate_hz = float(rate_hz)
        self.size = 1 << math.ceil(math.log2(self.rate_hz / NOTCH_BIN_HZ))
        self.bin_hz = self.rate_hz / self.size
        self._window = hann(self.size).astype(np.float64)
        self._low = math.ceil(NOTCH_RANGE_HZ[0] / self.bin_hz)
        self._high = math.floor(NOTCH_RANGE_HZ[1] / self.bin_hz)
        self._width = math.ceil(NOTCH_WIDTH_HZ / 2 / self.bin_hz)      # bins on each side of a line's peak
        self._around = math.ceil(NOTCH_SURROUNDINGS_HZ / self.bin_hz)
        # Where each line's surroundings lie, from its peak: beyond its width, within the surroundings.
        offsets = np.arange(-self._around, self._around + 1)
        self._offsets = offsets[np.abs(offsets) > self._width]
        self._recent = np.zeros(0)
        self._since = 0
        self._interval = max(1, round(NOTCH_ANALYSIS_S * self.rate_hz))
        self._time_s = 0.0          # of the signal read
        self._lines: list[_Line] = []
        self.notches: tuple[float, ...] = ()    # the frequencies notched, as the taps have them

    def process(self, detected: np.ndarray) -> np.ndarray | None:
        """Read ``detected``, the detector's output; the audio filter's new
        taps when the notches change, else None."""
        recent = np.concatenate((self._recent, detected))
        self._recent = recent[-self.size:]
        self._since += len(detected)
        self._time_s += len(detected) / self.rate_hz
        if self._since < self._interval or len(self._recent) < self.size:
            return None
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

    def _update(self) -> np.ndarray | None:
        """The new taps when a notch came, went or moved."""
        wanted = sorted(line.frequency_hz for line in self._lines if line.notched)
        if len(wanted) == len(self.notches) and all(
                abs(new - old) < NOTCH_MOVE_HZ for new, old in zip(wanted, self.notches)):
            return None
        self.notches = tuple(wanted)
        return notch_taps(self.notches, self.rate_hz)


class NoiseBlanker:
    """Zeroes the impulses of the samples that go to the VFO's channel:
    those whose magnitude exceeds ``BLANKER_FACTOR`` times the block's
    median, with ``BLANKER_SIDES`` samples on each side.

    The median is that of every ``BLANKER_STRIDE``th sample, 5000 of a block
    at 2.4 MS/s, which estimates the block's within about 1% for noise, far
    finer than the factor of 6; a whole block's median took most of the
    blanker's time. The impulses are sought in the samples' real and
    imaginary parts, which NumPy compares far faster than it computes
    magnitudes: a magnitude over the limit has a part over the limit over
    the square root of 2, and only the samples with such a part have their
    magnitude computed.

    An impulse at the end of a block zeroes the first samples of the next;
    one at the start of a block does not reach back into the previous one,
    which has gone. A block whose median magnitude is 0, more than half of
    its samples exact zeros, is left alone. Not thread-safe."""

    def __init__(self) -> None:
        self._pending = 0           # samples to zero at the start of the next block

    def process(self, samples: np.ndarray) -> np.ndarray:
        """``samples`` with their impulses zeroed: the same array when none
        was found, a new one otherwise."""
        count = len(samples)
        pending, self._pending = min(self._pending, count), max(0, self._pending - count)
        if not count:
            return samples
        every = samples[::BLANKER_STRIDE]
        median = float(np.median(every.real.astype(np.float64) ** 2 + every.imag.astype(np.float64) ** 2))
        hits = np.zeros(0, np.intp)
        if median > 0:
            limit = BLANKER_FACTOR ** 2 * median
            parts = np.ascontiguousarray(samples, np.complex64).view(np.float32)
            candidates = np.unique(np.flatnonzero(np.abs(parts) > math.sqrt(limit / 2)) // 2)
            if len(candidates):
                values = samples[candidates]
                hits = candidates[values.real.astype(np.float64) ** 2 + values.imag.astype(np.float64) ** 2 > limit]
        if not len(hits) and not pending:
            return samples
        blanked = samples.copy()
        blanked[:pending] = 0
        if len(hits):
            reach = np.arange(-BLANKER_SIDES, BLANKER_SIDES + 1)
            zeroed = (hits[:, np.newaxis] + reach).ravel()
            blanked[zeroed[(zeroed >= 0) & (zeroed < count)]] = 0
            self._pending = max(self._pending, int(hits[-1]) + BLANKER_SIDES + 1 - count)
        return blanked


class FineSpectrum:
    """The fine spectrum of the VFO's channel: Hann-windowed FFTs of
    ``size`` samples, one every ``hop`` samples, the frame's length at
    most, and a frame every ``rate / FINE_FRAME_RATE`` samples, the mean
    power of the FFTs since the last, in full-scale units (a full-scale
    tone centred on a bin reads 1), the lowest frequency first: bin ``i``
    lies ``(i - size / 2) * rate / size`` from the channel's 0 Hz. At
    48 kHz an FFT spans 0.17 s and FFTs overlap; at WFM's 320 kHz they
    follow each other and a frame averages about four. Not thread-safe."""

    def __init__(self, size: int = FINE_SIZE) -> None:
        if size < 16 or size & (size - 1):
            raise ValueError(f"the FFT size must be a power of two of at least 16, not {size}")
        self.size = size
        self._window = hann(size)
        self._scale = 1.0 / float(self._window.astype(np.float64).sum()) ** 2
        self._rate_hz: float | None = None

    def _start(self, rate_hz: float) -> None:
        self._rate_hz = rate_hz
        self._frame = max(1, round(rate_hz / FINE_FRAME_RATE))
        self._hop = min(self.size, self._frame)
        self._recent = np.zeros(0, np.complex64)
        self._total = 0                 # samples received
        self._next_frame = self._frame  # where the next frame ends, in samples received
        self._sum = np.zeros(self.size)
        self._count = 0

    def add(self, channel: np.ndarray, rate_hz: float) -> list[np.ndarray]:
        """Take a block of the channel, complex at ``rate_hz``: the frames
        it completes, usually none or one. Another rate starts again."""
        if rate_hz != self._rate_hz:
            self._start(rate_hz)
        if not len(channel):
            return []
        size, hop = self.size, self._hop
        data = np.concatenate((self._recent, channel))
        first = self._total - len(self._recent)            # where ``data`` starts, in samples received
        self._total += len(channel)
        self._recent = data[-size:]
        # The FFTs that end in this block: every ``hop`` samples, once ``size`` came.
        ends = np.arange(max(size, (self._total - len(channel)) // hop * hop + hop), self._total + 1, hop)
        if not len(ends):
            return []
        starts = ends - size - first
        segments = data[starts[:, np.newaxis] + np.arange(size)]
        spectra = np.fft.fft(segments * self._window, axis=1)
        powers = (spectra.real ** 2 + spectra.imag ** 2) * self._scale
        frames = []
        for end, power in zip(ends, powers):
            self._sum += power
            self._count += 1
            if end >= self._next_frame:
                frames.append(np.fft.fftshift(self._sum / self._count).astype(np.float32))
                self._sum = np.zeros(size)
                self._count = 0
                self._next_frame += (end - self._next_frame) // self._frame * self._frame + self._frame
        return frames
