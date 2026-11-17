"""Prototypes of the listening tools (later-milestones.md, section 2), for
the spike that measures their cost (`tools/measure_listening.py`) and as the
starting point of the pull requests that build them for good.

Each one is written as the design says and as it could move into the
package: vectorised NumPy, block by block, with no loop per sample, and no
knowledge of the engine, the stream or the interface.

- ``SyncDetector``, synchronous AM: a band-pass 80 Hz wide follows the
  strongest line within 1 kHz of the VFO, the channel is turned by that
  carrier's phase and its real part is the audio; under 10 dB of carrier
  over the noise within the band-pass it detects the envelope, as AM, and
  says that it has no lock. It takes the place of the AM detector in the
  demodulator's audio path (demod.py), with the same ``process`` and the
  same delay, so that the squelch's gate stays in step.
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
package and pip take `src/radiolack` alone. ``SyncDetector`` takes the AM
detector from demod.py for its envelope, a name that module keeps private;
in the package it would sit beside it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from radiolack.demod import (AGC_DECAY_DB_S, AM_LEVEL, AUDIO_ATTENUATION_DB, AUDIO_HIGH_PASS_HZ, AUDIO_LOW_PASS_HZ,
                             AUDIO_TRANSITION_HZ, _AmDetector)
from radiolack.dsp import shaped

# Synchronous AM.
SAM_SEARCH_HZ = 1_000.0     # the carrier: the strongest line within this of the VFO
SAM_BAND_HZ = 80.0          # the band-pass that follows it, between its half-gain points
SAM_STAGES = 3              # moving averages in cascade make the band-pass
SINC_CUBED_HALF = 0.36606   # where sinc(x) ** 3, a cascade's response at x / length, is one half
SAM_LOCK_DB = 10.0          # carrier over the noise within the band-pass, or no lock
SAM_SEARCH_SIZE = 4096      # the search's FFT: 11.7 Hz per bin at 48 kHz
SAM_SEARCH_S = 0.1          # one search this often
SAM_LOBE_BINS = 2           # a Hann window's main lobe, on each side of its peak
LEVEL_FLOOR = 1e-9          # -180 dBFS: no carrier at all, silence

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


def hann(size: int) -> np.ndarray:
    """Periodic Hann window."""
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(size) / size)).astype(np.float32)


def band_pass_length(rate_hz: float) -> int:
    """The length of each of SAM's moving averages at ``rate_hz``: odd, so
    that each lags by a whole number of samples, and such that the cascade
    has half its gain at ``SAM_BAND_HZ / 2`` from 0 Hz."""
    return max(1, round(SINC_CUBED_HALF * rate_hz / (SAM_BAND_HZ / 2))) | 1


class MovingAverages:
    """``stages`` moving averages of ``length`` samples in cascade, on a
    stream of complex samples, each by cumulative sums: a low-pass filter
    whose response is sinc(f * length / rate) to the power ``stages``,
    lagging by ``stages * (length - 1) / 2`` samples, whose cost does not
    grow with its length."""

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


class SyncDetector:
    """Synchronous AM detector for the channel at ``channel_rate_hz``, whose
    0 Hz is the VFO, ``delay`` samples late, as the AM detector it replaces.

    Every ``SAM_SEARCH_S`` it finds the strongest line within
    ``SAM_SEARCH_HZ`` of the VFO in an FFT of the latest channel, or once
    locked within the band-pass around the carrier it follows, placed by
    a parabola through the levels in dB, and its margin over the noise
    within the band-pass: the line's power over the median of the searched
    bins, which is the noise's mean power per bin times ln 2, scaled to the
    band-pass's width, the noise's own share of the line's bins taken off.
    The channel is turned by a phase that runs at that frequency, then
    filtered by the band-pass, three moving averages in cascade
    (``MovingAverages``, ``band_pass_length``), which keeps the carrier and
    its slow drifts; the angle of what comes out is the carrier's phase.
    The audio is the real part of the channel turned by that phase, less
    the carrier's amplitude, over a level that follows the carrier's at
    once and falls at the SSB AGC's 10 dB a second: a carrier that fades
    alone, as in selective fading, leaves the sidebands' audio as it is.
    Without a lock, the envelope, as AM. The band-pass lags by less than
    ``delay``; the channel waits for the rest. Not thread-safe."""

    def __init__(self, channel_rate_hz: float, delay: int) -> None:
        if delay < 1:
            raise ValueError(f"SAM needs a delay of at least one sample, not {delay}")
        self.rate_hz = float(channel_rate_hz)
        self.delay = delay
        self.locked = False
        self.carrier_hz: float | None = None    # from the channel's 0 Hz, while locked
        self.margin_db: float | None = None     # the carrier over the noise within the band-pass
        length = band_pass_length(self.rate_hz)
        lag = SAM_STAGES * (length - 1) // 2
        if lag > delay:
            raise ValueError(f"SAM's band-pass lags by {lag} samples, more than the delay of {delay}")
        self._filter = MovingAverages(length, SAM_STAGES)
        self._envelope = _AmDetector(self.rate_hz, delay)
        self._early = np.zeros(delay - lag, np.complex64)   # the channel, waiting for the rest of the delay
        self._turned = np.zeros(lag, np.complex64)          # the turned channel, waiting for the band-pass
        self._phase = 0.0                                   # radians, of the turn
        self._frequency_hz = 0.0                            # of the turn
        self._level: float | None = None                    # the carrier's level at the end of the last block
        self._window = hann(SAM_SEARCH_SIZE)
        frequencies = np.fft.fftfreq(SAM_SEARCH_SIZE, 1 / self.rate_hz)
        order = np.argsort(frequencies)
        self._bins = order[np.abs(frequencies[order]) <= SAM_SEARCH_HZ]    # the searched bins, lowest first
        self._frequencies = frequencies[self._bins]
        self._recent = np.zeros(0, np.complex64)            # the latest channel, for the search
        self._since = 0                                     # samples since the last search
        self._interval = max(1, round(SAM_SEARCH_S * self.rate_hz))

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        """The audio of ``channel``, ``delay`` samples late; ``power`` is the
        channel's power at each sample, which the envelope detector takes."""
        count = len(channel)
        envelope = self._envelope.process(channel, power)
        if not count:
            return envelope
        self._search(channel)
        if len(self._early):
            waiting = np.concatenate((self._early, channel))
            self._early = waiting[count:]
            channel = waiting[:count]
        step = 2 * np.pi * self._frequency_hz / self.rate_hz
        phases = (self._phase + step * np.arange(count)).astype(np.float32)
        self._phase = float((self._phase + step * count) % (2 * np.pi))
        turned = channel * (np.cos(phases) - 1j * np.sin(phases)).astype(np.complex64)
        carrier = self._filter.process(turned)
        waiting = np.concatenate((self._turned, turned))
        self._turned = waiting[count:]
        late = waiting[:count]              # in step with the band-pass's output
        amplitude = np.abs(carrier)
        level = self._carrier_level(float(amplitude.mean()), count)
        if not self.locked or level[-1] <= LEVEL_FLOOR:
            return envelope
        # Where the carrier vanishes, so does the part of the channel along it.
        in_phase = (late.real * carrier.real + late.imag * carrier.imag) / np.maximum(amplitude, LEVEL_FLOOR)
        return AM_LEVEL * (in_phase - amplitude) / np.maximum(level, LEVEL_FLOOR)

    def _carrier_level(self, mean: float, count: int) -> np.ndarray:
        """The level across this block: from the last block's to this one's,
        which follows the carrier's mean amplitude at once when it rises and
        falls no faster than the AGC's decay."""
        previous = mean if self._level is None else self._level
        decayed = previous * 10 ** (-AGC_DECAY_DB_S * count / self.rate_hz / 20)
        self._level = max(mean, decayed)
        return np.linspace(previous, self._level, count + 1)[1:]

    def _search(self, channel: np.ndarray) -> None:
        """Find the carrier again once ``SAM_SEARCH_S`` of channel came."""
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
            # Held: the carrier is followed within the band-pass, so that a
            # sideband's line that outgrows a fading carrier does not take it.
            near = np.flatnonzero(np.abs(self._frequencies - self._frequency_hz) <= SAM_BAND_HZ / 2)
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
        if margin < SAM_LOCK_DB:
            self._lose()
            return
        floor = 1e-30
        left, centre, right = (10 * math.log10(max(float(value), floor)) for value in power[peak - 1:peak + 2])
        curvature = left - 2 * centre + right
        offset = 0.5 * (left - right) / curvature if curvature < 0 else 0.0
        bin_hz = self.rate_hz / SAM_SEARCH_SIZE
        self._frequency_hz = float(self._frequencies[peak] + offset * bin_hz)
        self.carrier_hz = self._frequency_hz
        self.locked = True

    def _lose(self) -> None:
        self.locked = False
        self.carrier_hz = None


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
