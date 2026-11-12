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
following a slow drift. Ignored frequencies are never listed. The listed
signals found in each search are what the engine records as observations
(observations.py).

The memory scan watches the ticked bookmarks within the part of the span
that detection searches (``watched_channels``), all at once, in every block:
``ChannelWatch`` gives each one's power, in the passband of its mode's
filter, and the local noise there, from the same bin powers. ``MemoryScan``
follows the scan: it tunes to the strongest channel that would open the
squelch, checks that the squelch opens there, listens while it stays open,
waits the resume time once it closes, then watches them all again.

The sweep steps the receiver across a range in overlapping windows
(``sweep_windows``), as the airband explorer did: the first and last
windows' centres lie a tenth of the rate inside the range, and the windows
at most a quarter of the rate apart, so that every frequency of the range
lies in the part of some window that detection searches, its centre and its
edges included, even with the receiver's centre off by 15% of the rate.
``Sweep`` follows its progress: once the samples of a window come, it reads
there the blocks of SWEEP_SEARCHES searches, so that a signal found in two
of them is listed without the next window's help, then asks for the next
window, pass after pass. A window whose samples do not come while others do
is left after GIVE_UP_S.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .bookmarks import Bookmark
from .demod import MODE_BANDWIDTHS_HZ, passband_hz

SCANNERS = ("peaks", "memory", "history", "sweep")   # what the engine runs, as the hello lists it
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
RESUME_RANGE_S = (0.0, 8.0)
DEFAULT_RESUME_S = 2.0              # the memory scan watches again once the squelch stayed closed this long
CHECK_S = 0.25                      # once tuned to a channel, how long its squelch has to open
HOLD_S = 1.0                        # a channel whose squelch did not open is not tuned to again within this
SWEEP_EDGE = 0.1                    # the first and last windows' centres lie this far inside the range,
SWEEP_STEP = 0.25                   # and the windows at most this far apart, as fractions of the rate
SWEEP_SEARCHES = 2                  # detection searches in each window
WINDOW_RANGE_S = (0.25, 2.0)        # the sweep's time per window
DEFAULT_WINDOW_S = 0.5
DEFAULT_SWEEP_HZ = (430_000_000.0, 440_000_000.0)  # the 70 cm band
GIVE_UP_S = 2.0                     # a window whose samples do not come within this long, while others do, is left


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
            threshold_db: float, blocks: int | None = None) -> list[Detection] | None:
        """Add a block's bin powers: the signals found, once ``blocks`` blocks
        of the same capture have come, the detector's own number unless
        given, and None until then."""
        capture = (centre_hz, rate_hz, len(power))
        if capture != self._capture:
            self._capture = capture
            self.reset()
        self._sum = np.array(power, dtype=np.float64) if self._sum is None else self._sum + power
        self._count += 1
        if self._count < (self._blocks if blocks is None else blocks):
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

    def update(self, detections: Sequence[Detection], now: float) -> list[Signal]:
        """Add the detections of one search. A detection close to a listed
        signal updates it; one close to a detection of the previous search
        lists a new signal; others wait for the next search. Returns the
        listed signals found in this search, as they stand now."""
        candidates = []
        found: list[Signal] = []
        for detection in detections:
            if self.is_ignored(detection.frequency_hz, detection.width_hz):
                continue
            signal = self._nearest(detection)
            if signal is not None:
                signal.frequency_hz += DRIFT * (detection.frequency_hz - signal.frequency_hz)
                signal.width_hz = detection.width_hz
                signal.margin_db = detection.margin_db
                signal.last_s = now
                if not any(signal is other for other in found):     # two detections may update one signal
                    found.append(signal)
                continue
            earlier = self._candidate(detection)
            if earlier is None:
                candidates.append(detection)
                continue
            self._candidates.remove(earlier)
            self._signals.append(Signal((earlier.frequency_hz + detection.frequency_hz) / 2, detection.width_hz,
                                        detection.margin_db, now))
            found.append(self._signals[-1])
        self._candidates = candidates
        if len(self._signals) > MAX_SIGNALS:
            self._signals.sort(key=lambda signal: signal.last_s, reverse=True)
            del self._signals[MAX_SIGNALS:]
        return [dataclasses.replace(signal) for signal in found]

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


@dataclasses.dataclass(frozen=True)
class Channel:
    """A ticked bookmark that the memory scan watches, with the passband of
    its mode's filter, in Hz."""

    bookmark_id: int
    frequency_hz: float
    mode: str
    low_hz: float
    high_hz: float


def watched_channels(ticked: Iterable[tuple[int, Bookmark]], centre_hz: float,
                     rate_hz: float) -> tuple[Channel, ...]:
    """The channels of the ``ticked`` bookmarks, with their ids, that lie in
    the part of a span of ``rate_hz`` centred on ``centre_hz`` that
    detection searches: away from the outer tenth on each side and from the
    3 kHz on each side of the centre."""
    channels = []
    for bookmark_id, bookmark in ticked:
        if DC_GAP_HZ < abs(bookmark.frequency_hz - centre_hz) <= USABLE_FRACTION * rate_hz:
            low, high = passband_hz(bookmark.mode, MODE_BANDWIDTHS_HZ[bookmark.mode])
            channels.append(Channel(bookmark_id, bookmark.frequency_hz, bookmark.mode,
                                    bookmark.frequency_hz + low, bookmark.frequency_hz + high))
    return tuple(channels)


class ChannelWatch:
    """The power of the memory scan's channels and the noise there: each
    block's power in each channel's passband, so that a channel shows as
    soon as it becomes active, and the local noise in the passband, as
    detection finds it, in the bin powers averaged over ``blocks`` blocks of
    one capture. One thread at a time uses it."""

    def __init__(self, blocks: int = DETECTION_BLOCKS) -> None:
        if blocks < 1:
            raise ValueError(f"a noise estimate needs at least one block, not {blocks}")
        self._blocks = blocks
        self._sum: np.ndarray | None = None
        self._count = 0
        self._capture: tuple[float, float, int] | None = None
        self._noise: np.ndarray | None = None     # running sums of the local noise's bin powers, from 0

    def reset(self) -> None:
        """Forget the blocks and the noise found so far."""
        self._sum = None
        self._count = 0
        self._noise = None

    def levels(self, power: np.ndarray, centre_hz: float, rate_hz: float, bands: np.ndarray,
               enbw: float) -> tuple[np.ndarray, np.ndarray] | None:
        """Each band's power and noise, in dBFS, in a block of bin powers:
        ``bands`` holds one channel's lowest and highest frequency per row,
        and ``enbw`` is the FFT window's noise bandwidth in bins. None until
        the noise is known, ``blocks`` blocks after a new capture."""
        size = len(power)
        capture = (centre_hz, rate_hz, size)
        if capture != self._capture:
            self._capture = capture
            self.reset()
        self._sum = np.array(power, dtype=np.float64) if self._sum is None else self._sum + power
        self._count += 1
        if self._count >= self._blocks:
            db = 10 * np.log10(np.maximum(self._sum / self._count, LEVEL_FLOOR))
            noise = 10 ** (local_noise(db, rate_hz / size) / 10)
            self._noise = np.concatenate(([0.0], np.cumsum(noise)))
            self._sum = None
            self._count = 0
        if self._noise is None:
            return None
        bin_hz = rate_hz / size
        first_hz = centre_hz - rate_hz / 2          # the first bin's centre
        # The bins whose centres lie in each band, one at least.
        low = np.clip(np.ceil((bands[:, 0] - first_hz) / bin_hz), 0, size - 1).astype(np.intp)
        high = np.clip(np.ceil((bands[:, 1] - first_hz) / bin_hz), low + 1, size).astype(np.intp)
        sums = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
        channel = np.maximum((sums[high] - sums[low]) / enbw, LEVEL_FLOOR)
        noise = np.maximum((self._noise[high] - self._noise[low]) / enbw, LEVEL_FLOOR)
        return 10 * np.log10(channel), 10 * np.log10(noise)


class MemoryScan:
    """A memory scan's progress. ``state`` is "watching" every channel,
    "checking" that the squelch opens on the channel just tuned to,
    "listening" while it stays open, or "waiting" for the resume time since
    it closed; ``bookmark_id`` is the bookmark tuned to, None while
    watching."""

    def __init__(self) -> None:
        self.state = "watching"
        self.bookmark_id: int | None = None
        self._until = 0.0
        self._held: dict[int, float] = {}      # bookmarks whose squelch did not open, until when

    def step(self, now: float, active: Sequence[tuple[int, float]] | None, squelch_open: bool,
             resume_s: float) -> int | None:
        """Move on with a block that ended at ``now``: ``active`` lists the
        watched bookmarks whose channel would open the squelch, with their
        margins above the noise, or is None while the noise is unknown, and
        ``squelch_open`` is the squelch's state at the block's end, on the
        channel tuned to. Returns the bookmark to tune to, or None."""
        if self.state == "watching":
            held = self._held
            candidates = [(margin, bookmark_id) for bookmark_id, margin in active or ()
                          if held.get(bookmark_id, -math.inf) <= now]
            if not candidates:
                return None
            _, self.bookmark_id = max(candidates)
            self.state = "checking"
            self._until = now + CHECK_S
            return self.bookmark_id
        if squelch_open:
            self.state = "listening"
        elif self.state == "checking":
            if now >= self._until:
                self._held = {bookmark_id: until for bookmark_id, until in self._held.items() if until > now}
                if self.bookmark_id is not None:
                    self._held[self.bookmark_id] = now + HOLD_S
                self._watch()
        elif self.state == "listening":
            self.state = "waiting"
            self._until = now + resume_s
        elif now >= self._until:
            self._watch()
        return None

    def keep(self, bookmark_ids: Iterable[int]) -> None:
        """Watch only ``bookmark_ids``: away from a bookmark no longer
        watched, the scan watches every channel again."""
        kept = set(bookmark_ids)
        if self.bookmark_id is not None and self.bookmark_id not in kept:
            self._watch()
        self._held = {bookmark_id: until for bookmark_id, until in self._held.items() if bookmark_id in kept}

    def _watch(self) -> None:
        self.state = "watching"
        self.bookmark_id = None


@dataclasses.dataclass(frozen=True)
class SweepWindows:
    """A sweep's windows across a range: ``count`` of them, whose centres lie
    ``step_hz`` apart from ``first_hz`` up."""

    first_hz: float
    step_hz: float
    count: int

    def centre(self, index: int) -> float:
        """The centre frequency of window ``index``, from 0, in whole Hz, as a
        receiver takes it."""
        return float(round(self.first_hz + index * self.step_hz))


def sweep_windows(low_hz: float, high_hz: float, rate_hz: float) -> SweepWindows:
    """The windows of a sweep from ``low_hz`` to ``high_hz`` with a receiver
    that samples at ``rate_hz``: their centres go from a tenth of the rate
    above the lowest frequency to a tenth below the highest, evenly spaced,
    at most a quarter of the rate apart. A range too narrow for two such
    windows gets two a quarter of the rate apart around its middle, each
    covering the other's centre."""
    edge, step = SWEEP_EDGE * rate_hz, SWEEP_STEP * rate_hz
    first, last = low_hz + edge, high_hz - edge
    if last - first <= step:
        return SweepWindows((low_hz + high_hz - step) / 2, step, 2)
    count = math.ceil((last - first) / step) + 1
    return SweepWindows(first, (last - first) / (count - 1), count)


def search_blocks(window_s: float, frame_rate: float) -> int:
    """The blocks that each of a window's SWEEP_SEARCHES searches averages,
    for a time per window of ``window_s`` at ``frame_rate`` blocks a second."""
    return max(1, round(window_s * frame_rate / SWEEP_SEARCHES))


class Sweep:
    """A sweep's progress through its ``windows``: ``window``, from 0, is the
    one asked of the receiver, and ``passes`` the passes done. Each window
    reads ``search_blocks`` blocks for each of its SWEEP_SEARCHES searches,
    from the first block whose samples come from it."""

    def __init__(self, windows: SweepWindows, search_blocks: int, now: float) -> None:
        self.search_blocks = search_blocks
        self.restart(windows, now)

    def restart(self, windows: SweepWindows, now: float) -> None:
        """Start again from the first of ``windows``, asked for at ``now``."""
        self.windows = windows
        self.window = 0
        self.passes = 0
        self._read = 0          # blocks of the window read so far
        self._asked_s = now     # when the window was asked for

    @property
    def centre_hz(self) -> float:
        """The centre frequency of the window asked of the receiver."""
        return self.windows.centre(self.window)

    def step(self, now: float, centre_hz: float) -> float | None:
        """Move on with a block read at ``centre_hz`` and handled at ``now``.
        Returns the centre frequency of the next window, to ask of the
        receiver, once this window's blocks have been read, or once GIVE_UP_S
        passed since it was asked for without its samples while others came;
        None otherwise."""
        if centre_hz == self.centre_hz:
            self._read += 1
            if self._read < SWEEP_SEARCHES * self.search_blocks:
                return None
        elif now - self._asked_s < GIVE_UP_S:
            return None
        self.window += 1
        if self.window == self.windows.count:
            self.window = 0
            self.passes += 1
        self._read = 0
        self._asked_s = now
        return self.centre_hz


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


def checked_resume(value: object) -> float:
    """A resume time an interface gave; raises ValueError when it is not a
    number of seconds within RESUME_RANGE_S."""
    low, high = RESUME_RANGE_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"the resume time must be a number of seconds from {low:g} to {high:g}")
    return float(value)


def checked_window(value: object) -> float:
    """A sweep's time per window an interface gave; raises ValueError when it
    is not a number of seconds within WINDOW_RANGE_S."""
    low, high = WINDOW_RANGE_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"the time per window must be a number of seconds from {low:g} to {high:g}")
    return float(value)


def checked_sweep_range(low_hz: object, high_hz: object) -> tuple[float, float]:
    """A sweep's range an interface gave, its lowest and highest frequency,
    rounded to the Hz; raises ValueError unless both are frequencies, the
    lowest below the highest."""
    low = checked_frequency(low_hz, "the sweep's lowest frequency")
    high = checked_frequency(high_hz, "the sweep's highest frequency")
    if not low < high:
        raise ValueError("the sweep's lowest frequency must lie below its highest")
    return low, high
