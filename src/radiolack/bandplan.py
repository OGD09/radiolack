"""The band plans as the engine reads them: the mode one gives a frequency,
the channel it shows a signal on, and its WFM de-emphasis.

PLANS names the band plans, each in ``web/bandplan-ID.json``; the engine's
``band_plan`` setting chooses the one in use, Europe's unless chosen
otherwise, and the interface loads the same file (format in
docs/design/band-plan.md) in bandplan.js and channels.js, for all that it
shows. The engine reads what
its range scan and its lists of signals need: each range's mode and
step, a band without segments being one range, a band with segments one
range per segment, the parts that its segments leave out taking the
band's; the channel sets, with their channels and modes; and the known
frequencies' modes, and which of them are standard-frequency stations, which
the automatic calibration measures (calibration.py). ``BandPlan.mode_at`` gives the mode that the History's
Bookmark button gives a frequency: a known frequency's within
SPOT_TOLERANCE_HZ, else a channel set's within half its spacing of one of
its channels, else the range's, if any; ``plan_mode`` the mode that the
VFO takes there. ``BandPlan.channel`` gives the frequency that a signal
shows on: in a range whose step is CHANNEL_STEP_HZ or more, the nearest
channel of the step, for a signal WIDE_HZ wide or more, or within REACH_HZ
of it; elsewhere, its frequency to SHOWN_HZ. A plan that cannot be read
gives no mode and shows every signal to SHOWN_HZ, its WFM de-emphasis
being WFM_DEEMPHASIS_S, and the output says why.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import json
import logging
import math
from collections.abc import Iterable, Sequence
from pathlib import Path

from .demod import MODE_BANDWIDTHS_HZ, MODES, WFM_DEEMPHASIS_S

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).with_name("web")      # where the interface loads them from too
PLANS = ("europe", "france", "americas", "asia-pacific")     # the band plans, each in WEB_DIR / f"bandplan-{ID}.json"
DEFAULT_PLAN = "europe"         # the plan in use unless the user chose another
PLAN_FORMAT = 2
DEEMPHASIS_US = (50, 75)        # the WFM de-emphasis that a plan may name, in µs
SPOT_TOLERANCE_HZ = 50          # a frequency this close to a known frequency is on it, as in channels.js
CHANNEL_STEP_HZ = 12_500        # ranges whose step is this or more show their signals on their channels:
WIDE_HZ = 4_000                 # a signal this wide or wider on the nearest channel,
REACH_HZ = 1_500                # a narrower one when it lies this close to it,
                                # and any in a WFM range, where a station's detection narrows as its sound does
SHOWN_HZ = 100                  # elsewhere, signals show to this
STATION_REACH_HZ = MODE_BANDWIDTHS_HZ["WFM"] / 2    # what an ignored frequency covers in a WFM range: a station
                                                    # spreads over the WFM filter's width, and its detections may split


@dataclasses.dataclass(frozen=True)
class PlanRange:
    """A range of the band plan: a band without segments, a segment, or a
    part of a band that its segments leave out. Frequencies in Hz, ``to_hz``
    excluded."""

    from_hz: int
    to_hz: int
    mode: str | None
    step_hz: float | None


@dataclasses.dataclass(frozen=True)
class ChannelSet:
    """The channels of a channel set, lowest first, where it applies, from
    ``from_hz`` to ``to_hz`` excluded."""

    from_hz: int
    to_hz: int
    spacing_hz: int
    mode: str | None
    channels: tuple[int, ...]


class BandPlan:
    """A band plan's name, ranges, channel sets and known frequencies, each
    sorted and without overlaps, the frequencies of its standard-frequency
    stations, ``references``, lowest first, and its WFM de-emphasis's time
    constant, in seconds; an empty plan, without a name, by default."""

    def __init__(self, ranges: Iterable[PlanRange] = (), sets: Iterable[ChannelSet] = (),
                 spots: Iterable[tuple[int, str | None]] = (), references: Iterable[int] = (),
                 name: str = "", wfm_deemphasis_s: float = WFM_DEEMPHASIS_S) -> None:
        self.name = name
        self.wfm_deemphasis_s = wfm_deemphasis_s
        self.references = tuple(sorted(set(references)))
        self._ranges = tuple(ranges)
        self._range_starts = [entry.from_hz for entry in self._ranges]
        self._sets = tuple(sets)
        self._set_starts = [entry.from_hz for entry in self._sets]
        self._spots = tuple(sorted(spots, key=lambda spot: spot[0]))
        self._spot_hz = [f for f, _ in self._spots]

    @classmethod
    def parse(cls, data: object, modes: Sequence[str] = MODES) -> BandPlan:
        """The plan that a band plan file holds, parsed from its JSON. Raises
        ValueError, saying what is wrong, for a malformed one."""
        if not isinstance(data, dict) or data.get("format") != PLAN_FORMAT:
            raise ValueError(f"format {PLAN_FORMAT} expected")
        title = _text(data.get("name"), "the plan's name")
        deemphasis = data.get("wfm_deemphasis_us")
        if isinstance(deemphasis, bool) or deemphasis not in DEEMPHASIS_US:
            raise ValueError("wfm_deemphasis_us must be " + " or ".join(map(str, DEEMPHASIS_US)))
        bands, sets, spots = (_list(data.get(key), key) for key in ("bands", "channels", "spots"))
        ranges: list[PlanRange] = []
        edge = -math.inf
        for band in bands:
            name, low, high, mode, step = _entry(band, modes)
            if low < edge:
                raise ValueError(f"{name}: bands overlap or are out of order")
            edge = high
            segments = _list(band.get("segments", []), f"{name}: segments")
            if not segments:
                ranges.append(PlanRange(low, high, mode, step))
            cursor = low
            for segment in segments:
                part, start, stop, part_mode, part_step = _entry(segment, modes)
                if start < cursor or stop > high:
                    raise ValueError(f"{name}: {part} is out of order or outside the band")
                if start > cursor:
                    ranges.append(PlanRange(cursor, start, mode, step))
                ranges.append(PlanRange(start, stop, part_mode, part_step))
                cursor = stop
            if segments and cursor < high:
                ranges.append(PlanRange(cursor, high, mode, step))
        channel_sets = sorted((_channel_set(entry, modes) for entry in sets), key=lambda entry: entry.from_hz)
        for below, above in zip(channel_sets, channel_sets[1:]):
            if above.from_hz < below.to_hz:
                raise ValueError("channel sets overlap")
        known = [_spot(entry, modes) for entry in spots]
        return cls(ranges, channel_sets, (spot for spot, _ in known),
                   (spot[0] for spot, reference in known if reference), title, deemphasis / 1e6)

    def range_at(self, frequency_hz: float) -> PlanRange | None:
        """The range that holds ``frequency_hz``, or None outside the plan."""
        index = bisect.bisect_right(self._range_starts, frequency_hz) - 1
        if index >= 0 and frequency_hz < self._ranges[index].to_hz:
            return self._ranges[index]
        return None

    def mode_at(self, frequency_hz: float) -> str | None:
        """The mode that the plan gives ``frequency_hz``: a known frequency's,
        within SPOT_TOLERANCE_HZ; else a channel set's, within half its
        spacing of one of its channels; else, or where neither names one,
        the range's; None where none names one."""
        mode = None
        index = _nearest(self._spot_hz, frequency_hz)
        if index is not None and abs(self._spot_hz[index] - frequency_hz) <= SPOT_TOLERANCE_HZ:
            mode = self._spots[index][1]
        else:
            channel_set = self._set_at(frequency_hz)
            if channel_set is not None:
                channel = channel_set.channels[_nearest(channel_set.channels, frequency_hz)]
                if abs(channel - frequency_hz) <= channel_set.spacing_hz / 2:
                    mode = channel_set.mode
        if mode is None:
            found = self.range_at(frequency_hz)
            mode = None if found is None else found.mode
        return mode

    def channel(self, frequency_hz: float, width_hz: float) -> int:
        """The frequency, in whole Hz, that a signal found at
        ``frequency_hz``, ``width_hz`` wide, shows on: its range's nearest
        channel, in a range whose step is CHANNEL_STEP_HZ or more, when the
        signal is WIDE_HZ wide or more, lies within REACH_HZ of it, or lies
        in a WFM range; otherwise its frequency to SHOWN_HZ."""
        found = self.range_at(frequency_hz)
        if found is not None and found.step_hz is not None and found.step_hz >= CHANNEL_STEP_HZ:
            channel = _round(frequency_hz / found.step_hz) * found.step_hz
            if found.mode == "WFM" or width_hz >= WIDE_HZ or abs(frequency_hz - channel) <= REACH_HZ:
                return _round(channel)
        return _round(frequency_hz / SHOWN_HZ) * SHOWN_HZ

    def station_reach_hz(self, frequency_hz: float) -> float:
        """How far from ``frequency_hz`` a station there spreads: in a WFM
        range, STATION_REACH_HZ, half the WFM filter's width, as a broadcast
        station's detections may split across it; elsewhere 0, its channel
        alone counting."""
        found = self.range_at(frequency_hz)
        return STATION_REACH_HZ if found is not None and found.mode == "WFM" else 0.0

    def _set_at(self, frequency_hz: float) -> ChannelSet | None:
        index = bisect.bisect_right(self._set_starts, frequency_hz) - 1
        if index >= 0 and frequency_hz < self._sets[index].to_hz:
            return self._sets[index]
        return None


def plan_mode(planned: str | None, current: str) -> str:
    """The mode that the VFO, in ``current``, takes where the plan names
    ``planned``: SAM counts as AM, so that synchronous AM stays where the
    plan names AM; where it names none, the mode stays."""
    if planned is None:
        return current
    return current if planned == "AM" and current == "SAM" else planned


def plan_path(plan: str) -> Path:
    """The file of the band plan named ``plan``, one of PLANS."""
    return WEB_DIR / f"bandplan-{plan}.json"


@functools.cache
def load_plan(plan: str) -> BandPlan:
    """The band plan named ``plan``, one of PLANS, read once: an empty one
    when it cannot be read, which the output tells."""
    if plan not in PLANS:
        raise ValueError(f"unknown band plan: {plan[:40]}")
    try:
        return BandPlan.parse(json.loads(plan_path(plan).read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:      # a JSON or text decoding error is a ValueError
        log.warning("the band plan %s gives no mode and no channel: %s", plan, error)
        return BandPlan()


def default_plan() -> BandPlan:
    """The band plan in use unless the user chose another."""
    return load_plan(DEFAULT_PLAN)


def checked_plan(value: object) -> str:
    """``value``, the name of a band plan, one of PLANS. Raises ValueError
    for any other."""
    if not isinstance(value, str) or value not in PLANS:
        raise ValueError(f"unknown band plan: {str(value)[:40]}")
    return value


def plan_choices() -> list[dict[str, str]]:
    """Each band plan's name in PLANS, ``id``, and the name it gives itself,
    ``name``, its ``id`` for one that cannot be read: the hello's
    ``band_plans`` (docs/design/protocol.md)."""
    return [{"id": plan, "name": load_plan(plan).name or plan} for plan in PLANS]


def _entry(item: object, modes: Sequence[str]) -> tuple[str, int, int, str | None, float | None]:
    """A band's or a segment's name, edges, mode and step."""
    if not isinstance(item, dict):
        raise ValueError("bands and segments must be objects")
    name = _text(item.get("name"), "a name")
    low, high = _hz(item.get("from"), f"{name}: from"), _hz(item.get("to"), f"{name}: to")
    if low >= high:
        raise ValueError(f"{name}: from must lie below to")
    step = item.get("step")
    if step is not None and (isinstance(step, bool) or not isinstance(step, (int, float))
                             or not math.isfinite(step) or step <= 0):
        raise ValueError(f"{name}: the step must be a positive number")
    return name, low, high, _mode(item.get("mode"), name, modes), step


def _channel_set(item: object, modes: Sequence[str]) -> ChannelSet:
    if not isinstance(item, dict):
        raise ValueError("channel sets must be objects")
    name = _text(item.get("name"), "a channel set's name")
    low, high = _hz(item.get("from"), f"{name}: from"), _hz(item.get("to"), f"{name}: to")
    spacing = _hz(item.get("spacing"), f"{name}: the spacing")
    if low >= high or spacing == 0:
        raise ValueError(f"{name}: from must lie below to, and the spacing above 0")
    if "list" in item:
        channels = sorted(_hz(entry.get("f") if isinstance(entry, dict) else None, f"{name}: a channel")
                          for entry in _list(item["list"], f"{name}: the list"))
    else:
        first = _hz(item.get("first"), f"{name}: first")
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{name}: count must be a whole number above 0")
        channels = [first + index * spacing for index in range(count)]
    if not channels or channels[0] < low or channels[-1] >= high:
        raise ValueError(f"{name}: its channels must lie inside the set")
    return ChannelSet(low, high, spacing, _mode(item.get("mode"), name, modes), tuple(channels))


def _spot(item: object, modes: Sequence[str]) -> tuple[tuple[int, str | None], bool]:
    """A known frequency and its mode, and whether it is a standard-frequency
    station, as bandplan.js reads it."""
    if not isinstance(item, dict):
        raise ValueError("spots must be objects")
    name = _text(item.get("name"), "a spot's name")
    reference = item.get("reference", False)
    if "reference" in item and reference is not True:
        raise ValueError(f"{name}: reference must be true or absent")
    return (_hz(item.get("f"), f"{name}: f"), _mode(item.get("mode"), name, modes)), reference


def _list(value: object, what: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty text")
    return value


def _hz(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be whole Hz")
    return value


def _mode(value: object, name: str, modes: Sequence[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in modes:
        raise ValueError(f"{name}: unknown mode {str(value)[:20]}")
    return value


def _nearest(values: Sequence[float], frequency_hz: float) -> int | None:
    """The index of the value nearest ``frequency_hz`` in ``values``, lowest
    first: of two as near, the lower one, as channels.js has it; None for
    no value."""
    if not values:
        return None
    above = bisect.bisect_left(values, frequency_hz)
    if above == len(values):
        return above - 1
    if above > 0 and frequency_hz - values[above - 1] <= values[above] - frequency_hz:
        return above - 1
    return above


def _round(value: float) -> int:
    """``value`` to the nearest whole number, halves upwards, as JavaScript's
    Math.round has it."""
    return math.floor(value + 0.5)
