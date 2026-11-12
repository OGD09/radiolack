"""Scanners: what the engine finds in the receiver's span, for every interface.

Detection looks for signals in the bin powers that the engine computes for
its display frames (spectrum.py), at their full resolution whatever the
interface. ``Detector`` averages the bin powers of successive blocks of one
capture, about a third of a second, then ``find_signals`` keeps the runs of
bins that rise at least a threshold above the local noise: the median of the
bins within 50 kHz. It leaves out the outer tenth of the span on each side,
where the receiver's filter falls off and strong signals beyond the span fold
in, and 3 kHz around its centre, where a receiver's DC offset shows. These
follow the airband explorer's lessons (docs/design/lessons.md). Levels are
dBFS bin powers: not calibrated, and not comparable with the channel meter.

``Signals`` keeps one entry for each signal found while Radiolack runs: a
signal is listed once found in two averaged spectra in a row, so that a
stray peak of noise is not, and detections close to an entry update it,
following a slow drift. Ignored frequencies are never listed.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SCANNERS = ("peaks",)               # what the engine runs, as the hello lists it
DETECTION_BLOCKS = 10               # blocks averaged for each search: 1/3 s at 30 blocks a second
USABLE_FRACTION = 0.4               # bins kept on each side of the centre, as a fraction of the rate
DC_GAP_HZ = 3_000.0                 # left out on each side of the centre frequency
NOISE_SPAN_HZ = 100_000.0           # the local noise: the median of the bins this wide around a bin
MIN_NOISE_BINS = 16
LEVEL_FLOOR = 1e-15                 # -150 dBFS: lower bin powers are rounding errors, below any receiver's noise
SKIRT_DB = 4.0                      # a signal's bins: within this of the threshold, at least SKIRT_MIN_DB
SKIRT_MIN_DB = 3.0                  # above the noise
MAX_RUNS = 1000                     # signals kept from one search, the strongest
THRESHOLD_RANGE_DB = (6.0, 30.0)
DEFAULT_THRESHOLD_DB = 12.0
MERGE_HZ = 1_500.0                  # detections closer than this, or a quarter of their width, are one signal
DRIFT = 0.3                         # how far a signal's frequency moves towards a new detection
ACTIVE_S = 1.5                      # a signal found within this long is active
MAX_SIGNALS = 200                   # signals kept, the latest found
LISTED = 50                         # signals sent to the interfaces
MAX_IGNORED = 200
FREQUENCY_RANGE_HZ = (1.0, 1e11)    # as the VFO's and the bookmarks'


@dataclasses.dataclass(frozen=True)
class Detection:
    """A run of bins above the threshold in one averaged spectrum."""

    frequency_hz: float     # its centre of power
    width_hz: float
    margin_db: float        # its highest bin above the local noise


def find_signals(power: np.ndarray, centre_hz: float, rate_hz: float, threshold_db: float) -> list[Detection]:
    """The signals in ``power``, bin powers of a span of ``rate_hz`` centred
    on ``centre_hz``, lowest frequency first: the runs of bins whose highest
    rises at least ``threshold_db`` above the local noise, each with the
    bins around it that stay near the threshold."""
    size = len(power)
    if size < MIN_NOISE_BINS or not rate_hz > 0:
        return []
    bin_hz = rate_hz / size
    half = size // 2                            # the centre frequency's bin
    reach = min(half - 1, int(USABLE_FRACTION * size))
    low = half - reach
    usable = np.asarray(power[low:half + reach + 1], dtype=np.float64)
    db = 10 * np.log10(np.maximum(usable, LEVEL_FLOOR))
    noise_db = local_noise(db, bin_hz)
    excess = db - noise_db
    gap = int(DC_GAP_HZ / bin_hz)
    excess[max(0, reach - gap):reach + gap + 1] = -np.inf
    above = excess > max(threshold_db - SKIRT_DB, SKIRT_MIN_DB)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], above.view(np.int8), [0]))))
    starts, stops = edges[0::2], edges[1::2]
    if not len(starts):
        return []
    margins = np.maximum.reduceat(np.where(above, excess, -np.inf), starts)
    strong = margins >= threshold_db
    if strong.sum() > MAX_RUNS:
        strong &= margins >= np.sort(margins[strong])[-MAX_RUNS]
    # Each run's centre of power, over what rises above the noise.
    weights = np.where(above, np.maximum(usable - 10 ** (noise_db / 10), 0), 0)
    totals = np.add.reduceat(weights, starts)
    moments = np.add.reduceat(weights * np.arange(len(weights)), starts)
    detections = []
    for start, stop, margin, total, moment in zip(starts[strong], stops[strong], margins[strong],
                                                  totals[strong], moments[strong]):
        index = moment / total if total > 0 else (start + stop - 1) / 2
        detections.append(Detection(centre_hz + (low + index - half) * bin_hz,
                                    float((stop - start) * bin_hz), float(margin)))
    return detections


def local_noise(db: np.ndarray, bin_hz: float) -> np.ndarray:
    """The noise level at each bin of ``db``: the median of the bins within
    NOISE_SPAN_HZ / 2, which the signals among them barely move, taken every
    quarter of that span and interpolated in between."""
    width = min(len(db), max(MIN_NOISE_BINS, int(NOISE_SPAN_HZ / bin_hz)))
    step = max(1, width // 4)
    medians = np.median(sliding_window_view(db, width)[::step], axis=1)
    centres = np.arange(len(medians)) * step + (width - 1) / 2
    return np.interp(np.arange(len(db)), centres, medians)


class Detector:
    """Averages the bin powers of successive blocks of one capture and finds
    the signals in every ``blocks`` of them. Blocks of another capture, or of
    another size, start the average again. One thread at a time uses it."""

    def __init__(self, blocks: int = DETECTION_BLOCKS) -> None:
        if blocks < 1:
            raise ValueError(f"a search needs at least one block, not {blocks}")
        self._blocks = blocks
        self._sum: np.ndarray | None = None
        self._count = 0
        self._capture: tuple[float, float, int] | None = None

    def reset(self) -> None:
        """Forget the blocks added so far."""
        self._sum = None
        self._count = 0

    def add(self, power: np.ndarray, centre_hz: float, rate_hz: float,
            threshold_db: float) -> list[Detection] | None:
        """Add a block's bin powers: the signals found, once ``blocks`` blocks
        of the same capture have come, and None until then."""
        capture = (centre_hz, rate_hz, len(power))
        if capture != self._capture:
            self._capture = capture
            self.reset()
        self._sum = np.array(power, dtype=np.float64) if self._sum is None else self._sum + power
        self._count += 1
        if self._count < self._blocks:
            return None
        mean = self._sum / self._count
        self.reset()
        return find_signals(mean, centre_hz, rate_hz, threshold_db)


@dataclasses.dataclass
class Signal:
    """A signal found while Radiolack runs."""

    frequency_hz: float
    width_hz: float
    margin_db: float    # in the latest detection
    last_s: float       # when last found, in ``time.monotonic()``


def same_signal(frequency_hz: float, width_hz: float, other_hz: float, other_width_hz: float) -> bool:
    """Whether two detections are one signal: closer than MERGE_HZ, or than a
    quarter of the wider one's width, as a voice moves an FM signal's centre
    of power by more than a narrow signal drifts."""
    return abs(frequency_hz - other_hz) <= max(MERGE_HZ, max(width_hz, other_width_hz) / 4)


class Signals:
    """The signals found while Radiolack runs, at most MAX_SIGNALS, the
    latest found, and the frequencies the interfaces chose to ignore."""

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._candidates: list[Detection] = []      # found in the latest search only
        self._ignored: list[float] = []             # lowest first
        self._listed: tuple = ()        # the list when last found changed, apart from the ages

    @property
    def ignored(self) -> list[float]:
        """The ignored frequencies, lowest first."""
        return list(self._ignored)

    def update(self, detections: Sequence[Detection], now: float) -> None:
        """Add the detections of one search. A detection close to a listed
        signal updates it; one close to a detection of the previous search
        lists a new signal; others wait for the next search."""
        candidates = []
        for detection in detections:
            if self.is_ignored(detection.frequency_hz, detection.width_hz):
                continue
            signal = self._nearest(detection)
            if signal is not None:
                signal.frequency_hz += DRIFT * (detection.frequency_hz - signal.frequency_hz)
                signal.width_hz = detection.width_hz
                signal.margin_db = detection.margin_db
                signal.last_s = now
                continue
            earlier = self._candidate(detection)
            if earlier is None:
                candidates.append(detection)
                continue
            self._candidates.remove(earlier)
            self._signals.append(Signal((earlier.frequency_hz + detection.frequency_hz) / 2, detection.width_hz,
                                        detection.margin_db, now))
        self._candidates = candidates
        if len(self._signals) > MAX_SIGNALS:
            self._signals.sort(key=lambda signal: signal.last_s, reverse=True)
            del self._signals[MAX_SIGNALS:]

    def describe(self, now: float) -> list[dict[str, Any]]:
        """At most LISTED signals, as JSON values: the active ones first,
        strongest first, then the others, latest found first."""
        def order(signal: Signal) -> tuple:
            active = now - signal.last_s <= ACTIVE_S
            return (not active, -signal.margin_db if active else now - signal.last_s)

        return [{"frequency_hz": round(signal.frequency_hz), "width_hz": round(signal.width_hz),
                 "margin_db": round(signal.margin_db, 1), "active": now - signal.last_s <= ACTIVE_S,
                 "age_s": round(max(0.0, now - signal.last_s), 1)}
                for signal in sorted(self._signals, key=order)[:LISTED]]

    def changed(self, now: float) -> bool:
        """Whether the list differs from when this last returned true, beyond
        the time since each signal was found: a signal came or went, became
        active or inactive, or moved by 100 Hz or 1 dB."""
        listed = tuple((round(entry["frequency_hz"], -2), entry["active"], round(entry["margin_db"]))
                       for entry in self.describe(now))
        if listed == self._listed:
            return False
        self._listed = listed
        return True

    def is_ignored(self, frequency_hz: float, width_hz: float = 0.0) -> bool:
        return any(same_signal(frequency_hz, width_hz, ignored, 0.0) for ignored in self._ignored)

    def ignore(self, frequency_hz: float) -> None:
        """Ignore a frequency: the signals there leave the list and are not
        listed again. Raises ValueError when MAX_IGNORED are ignored already."""
        if not any(abs(frequency_hz - ignored) <= MERGE_HZ for ignored in self._ignored):
            if len(self._ignored) >= MAX_IGNORED:
                raise ValueError(f"at most {MAX_IGNORED} frequencies may be ignored")
            self._ignored.append(frequency_hz)
            self._ignored.sort()
        self._signals = [signal for signal in self._signals
                         if not same_signal(signal.frequency_hz, signal.width_hz, frequency_hz, 0.0)]
        self._candidates = [detection for detection in self._candidates
                            if not same_signal(detection.frequency_hz, detection.width_hz, frequency_hz, 0.0)]

    def restore(self) -> None:
        """Ignore no frequency any more."""
        self._ignored.clear()

    def clear(self) -> None:
        """Forget the signals found, keeping the ignored frequencies."""
        self._signals.clear()
        self._candidates.clear()

    def _nearest(self, detection: Detection) -> Signal | None:
        found, distance = None, math.inf
        for signal in self._signals:
            gap = abs(signal.frequency_hz - detection.frequency_hz)
            if gap < distance and same_signal(signal.frequency_hz, signal.width_hz,
                                              detection.frequency_hz, detection.width_hz):
                found, distance = signal, gap
        return found

    def _candidate(self, detection: Detection) -> Detection | None:
        found, distance = None, math.inf
        for candidate in self._candidates:
            gap = abs(candidate.frequency_hz - detection.frequency_hz)
            if gap < distance and same_signal(candidate.frequency_hz, candidate.width_hz,
                                              detection.frequency_hz, detection.width_hz):
                found, distance = candidate, gap
        return found


def checked_frequency(value: object, what: str) -> float:
    """A frequency an interface gave, rounded to the Hz; raises ValueError
    when it is not a number within FREQUENCY_RANGE_HZ."""
    low, high = FREQUENCY_RANGE_HZ
    # NaN and infinities fail the comparison, as do whole numbers too large for a float.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} must be a number from {low:g} Hz to {high:g} Hz")
    return float(round(value))


def checked_threshold(value: object) -> float:
    """A detection threshold an interface gave; raises ValueError when it is
    not a number within THRESHOLD_RANGE_DB."""
    low, high = THRESHOLD_RANGE_DB
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"the threshold must be a number of dB from {low:g} to {high:g}")
    return float(value)
