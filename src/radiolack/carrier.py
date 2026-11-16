"""The measure of a carrier: where the strongest line of the VFO's channel
lies, to a fraction of a hertz, and how far it stands above the noise.

``strongest_line`` measures one stretch of a channel, through a Hann window
and an FFT whose bins are a hertz wide for a second of samples: the strongest
bin within the filter's passband, placed between its neighbours by a
parabola through their levels in dB, and its margin over the median of the
passband's bins. ``CarrierMeasure`` gathers the channel as the demodulator
makes it, before any detector, and measures it one second at a time: two
seconds in a row must agree within a hertz. It knows nothing of the engine,
the interface or the correction, so that a later run across several
reference stations, or a frequency meter, can call it as it is.

Why a parabola in dB: a Hann window's main lobe is close to a Gaussian, whose
logarithm is a parabola; through the strongest bin and its two neighbours it
places a steady carrier within a few hundredths of a bin.
"""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass

import numpy as np

MEASURE_S = 1.0             # each measure takes this much of the channel: bins of 1 Hz
AGREEMENT_HZ = 1.0          # two measures in a row agree within this
MIN_MARGIN_DB = 20.0        # a line stands this far above the median of the filter's bins, or there is none
RIVAL_DB = 6.0              # another line this close to the strongest: two carriers, no telling which
RIVAL_CLEARANCE_HZ = 5.0    # lines this close to the strongest are its own: the window's main lobe, a drift

# Why a measure gives no frequency: the VFO's channel receives nothing (outside
# the span, or a mode the sample rate cannot hold); no line stands out within
# the filter; two lines of nearly equal strength; two measures in a row that
# disagree, or settings that kept changing.
SILENT, NO_SIGNAL, SEVERAL, UNSTABLE = "silent", "no_signal", "several", "unstable"
REASONS = (SILENT, NO_SIGNAL, SEVERAL, UNSTABLE)


@dataclass(frozen=True)
class Line:
    """The strongest line of a stretch of channel."""

    frequency_hz: float     # from the channel's 0 Hz
    margin_db: float        # over the median of the filter's bins
    rival: bool             # another line within RIVAL_DB of it, beyond RIVAL_CLEARANCE_HZ


@dataclass(frozen=True)
class Carrier:
    """What a measure found: the carrier's frequency, from the channel's
    0 Hz, and its margin, or why there is no frequency. ``margin_db`` is
    None when nothing was measured, or no line stands out within the
    filter."""

    frequency_hz: float | None
    margin_db: float | None
    reason: str | None = None


def strongest_line(samples: np.ndarray, rate_hz: float, low_hz: float, high_hz: float) -> Line | None:
    """The strongest line of ``samples``, complex at ``rate_hz``, within the
    passband from ``low_hz`` to ``high_hz``; None when the passband holds
    fewer than three bins, or its strongest bin is not a peak: a line just
    beyond the passband's edge, whose slope reaches into it, is not one of
    its lines."""
    count = len(samples)
    if count < 3:
        return None
    n = np.arange(count)
    window = 0.5 - 0.5 * np.cos(2 * np.pi * n / count)     # periodic Hann
    power = np.abs(np.fft.fftshift(np.fft.fft(samples * window))) ** 2
    bin_hz = rate_hz / count
    frequencies = (np.arange(count) - count // 2) * bin_hz  # fftshift's order: lowest first
    first = max(1, int(np.searchsorted(frequencies, low_hz, side="left")))
    stop = min(count - 1, int(np.searchsorted(frequencies, high_hz, side="right")))
    if stop - first < 3:
        return None
    inside = power[first:stop]
    peak = first + int(np.argmax(inside))
    if not (power[peak] >= power[peak - 1] and power[peak] >= power[peak + 1]) or power[peak] <= 0:
        return None
    floor = 1e-30           # a bin of exact zeros, as from silence, has no level in dB
    left, centre, right = (10 * math.log10(max(float(value), floor)) for value in power[peak - 1:peak + 2])
    curvature = left - 2 * centre + right
    offset = 0.5 * (left - right) / curvature if curvature < 0 else 0.0
    median = float(np.median(inside))
    margin = centre - 10 * math.log10(max(median, floor))
    # The other lines: the passband's local maxima beyond the strongest's own.
    clearance = max(1, math.ceil(RIVAL_CLEARANCE_HZ / bin_hz))
    others = inside.copy()
    others[max(0, peak - first - clearance):peak - first + clearance + 1] = 0.0
    maxima = (others[1:-1] >= others[:-2]) & (others[1:-1] >= others[2:])
    rival_level = float(others[1:-1][maxima].max()) if maxima.any() else 0.0
    rival = rival_level > 0 and 10 * math.log10(max(rival_level, floor)) >= centre - RIVAL_DB
    return Line(float(frequencies[peak] + offset * bin_hz), margin, bool(rival))


class CarrierMeasure:
    """Measures a carrier in a channel as it streams: ``add`` takes each
    block of the channel and, once it has a result, gives it. One second is
    measured, then the next: a second without a line, or whose line stands
    less than MIN_MARGIN_DB above the noise or has a rival, ends the measure
    at once; two that agree within AGREEMENT_HZ give their mean, and two
    that do not, ``UNSTABLE``. The samples must come from one setting: a
    block that comes with another ``key`` than the previous starts the
    measure again. Not thread-safe: one block at a time."""

    def __init__(self) -> None:
        self._key: Hashable = None
        self._blocks: list[np.ndarray] = []
        self._count = 0
        self._first: Line | None = None
        self.received = False   # a block came since the measure was made

    def add(self, channel: np.ndarray, rate_hz: float, low_hz: float, high_hz: float,
            key: Hashable) -> Carrier | None:
        """Take a block of the channel, complex at ``rate_hz``, whose filter
        passes ``low_hz`` to ``high_hz`` from the channel's 0 Hz, taken with
        the setting ``key``; the result once there is one, else None."""
        if key != self._key:
            self._key = key
            self._blocks, self._count, self._first = [], 0, None
        self.received = True
        if not len(channel):
            return None
        self._blocks.append(channel)
        self._count += len(channel)
        needed = max(3, round(MEASURE_S * rate_hz))
        if self._count < needed:
            return None
        gathered = np.concatenate(self._blocks)
        second, rest = gathered[:needed], gathered[needed:]
        self._blocks, self._count = ([rest], len(rest)) if len(rest) else ([], 0)
        line = strongest_line(second, rate_hz, low_hz, high_hz)
        if line is None:
            return Carrier(None, None, NO_SIGNAL)
        margin = round(line.margin_db, 1)
        if line.margin_db < MIN_MARGIN_DB:
            return Carrier(None, margin, NO_SIGNAL)
        if line.rival:
            return Carrier(None, margin, SEVERAL)
        first, self._first = self._first, line
        if first is None:
            return None
        margin = round(min(first.margin_db, line.margin_db), 1)
        if abs(line.frequency_hz - first.frequency_hz) > AGREEMENT_HZ:
            return Carrier(None, margin, UNSTABLE)
        return Carrier((first.frequency_hz + line.frequency_hz) / 2, margin)
