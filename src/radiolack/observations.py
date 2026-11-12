"""Observations: what detection found, kept across restarts for the History
tab, in observations.json in the settings folder (store.py).

The engine records the signals that detection lists (scanners.py) each time
it finds them, apart from the simulated band's: frequencies, times and
levels, never audio or samples. A channel gathers the signals found within
1.5 kHz of its frequency (scanners.MERGE_HZ), or within a quarter of their
width, as the list of the signals found does, and its frequency follows
theirs. Each channel keeps when it was first and last found, its
transmissions, how long it was active, its highest margin above the local
noise and how many of its transmissions began in each hour of the day, on
the engine's computer. A transmission begins when a channel is found more
than GAP_S after it was last found, and lasts until it was last found: these
durations add up to how long the channel was active.

At most MAX_CHANNELS are kept, the least recently found leaving first, and a
channel not found for FORGET_S is forgotten.
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import math
from collections.abc import Iterable
from typing import Any

from .scanners import ACTIVE_S, DRIFT, FREQUENCY_RANGE_HZ, Signal, same_signal

MAX_CHANNELS = 2000
FORGET_S = 90 * 86_400.0        # a channel not found for 90 days is forgotten
GAP_S = ACTIVE_S                # found again within this long, a channel goes on with the same transmission
HOURS = 24
_MAX_TIME_S = 1e11              # beyond any clock: the year 5138
_MAX_COUNT = 2 ** 53            # whole numbers that JSON numbers keep exactly
_MARGIN_RANGE_DB = (0.0, 200.0)

log = logging.getLogger(__name__)


@dataclasses.dataclass
class Observation:
    """What detection found on one channel. Times are ``time.time()``."""

    frequency_hz: float
    first_s: float
    last_s: float
    transmissions: int
    active_s: float
    margin_db: float        # the highest
    hours: list[int]        # transmissions begun in each hour of the day, from 0 h

    def describe(self) -> dict[str, Any]:
        """As JSON values, for the interfaces and the file."""
        return {"frequency_hz": round(self.frequency_hz), "first_s": round(self.first_s),
                "last_s": round(self.last_s), "transmissions": self.transmissions,
                "active_s": round(self.active_s, 1), "margin_db": round(self.margin_db, 1),
                "hours": list(self.hours)}


class Observations:
    """The channels detection found, in frequency order. ``saved`` is what
    observations.json held, without its version: the channels that are not
    valid are left out, and those not found for FORGET_S before ``now``."""

    def __init__(self, saved: object = None, now: float = 0.0) -> None:
        self._channels: list[Observation] = []
        if saved is not None:
            self._restore(saved, now)

    def __len__(self) -> int:
        return len(self._channels)

    def record(self, found: Iterable[Signal], now: float, hour: int) -> bool:
        """Record the signals found in one search, at ``now``
        (``time.time()``), in ``hour`` of the day, from 0 to 23. Returns
        whether there were any. A clock set back meanwhile starts no
        transmission and adds no active time."""
        keys = [channel.frequency_hz for channel in self._channels]
        recorded = False
        for signal in found:
            recorded = True
            index = bisect.bisect_left(keys, signal.frequency_hz)
            nearest = self._nearest(keys, index, signal.frequency_hz, signal.width_hz)
            if nearest is None:
                hours = [0] * HOURS
                hours[hour] = 1
                self._channels.insert(index, Observation(signal.frequency_hz, now, now, 1, 0.0,
                                                         signal.margin_db, hours))
                keys.insert(index, signal.frequency_hz)
                continue
            channel = self._channels[nearest]
            gap = now - channel.last_s
            if gap > GAP_S:
                channel.transmissions += 1
                channel.hours[hour] += 1
            elif gap > 0:
                channel.active_s += gap
            channel.first_s = min(channel.first_s, now)
            channel.last_s = now
            channel.margin_db = max(channel.margin_db, signal.margin_db)
            channel.frequency_hz += DRIFT * (signal.frequency_hz - channel.frequency_hz)
            keys[nearest] = channel.frequency_hz
        if recorded:
            self._channels.sort(key=lambda channel: channel.frequency_hz)  # a channel may have moved past another
            if len(self._channels) > MAX_CHANNELS:
                self._keep_latest()
        return recorded

    def forget(self, now: float) -> bool:
        """Forget the channels not found for FORGET_S before ``now``.
        Returns whether there were any."""
        kept = [channel for channel in self._channels if now - channel.last_s <= FORGET_S]
        if len(kept) == len(self._channels):
            return False
        self._channels = kept
        return True

    def clear(self, frequency_hz: float) -> bool:
        """Forget the channel nearest ``frequency_hz``, within 1.5 kHz.
        Returns whether there was one."""
        keys = [channel.frequency_hz for channel in self._channels]
        nearest = self._nearest(keys, bisect.bisect_left(keys, frequency_hz), frequency_hz, 0.0)
        if nearest is None:
            return False
        del self._channels[nearest]
        return True

    def clear_all(self) -> bool:
        """Forget every channel. Returns whether there were any."""
        cleared = bool(self._channels)
        self._channels = []
        return cleared

    def describe(self) -> list[dict[str, Any]]:
        """The channels as JSON values, for the interfaces: the latest found
        first."""
        return [channel.describe()
                for channel in sorted(self._channels, key=lambda channel: channel.last_s, reverse=True)]

    def stored(self) -> dict[str, Any]:
        """What observations.json holds, without its version: the channels in
        frequency order."""
        return {"channels": [channel.describe() for channel in self._channels]}

    @staticmethod
    def _nearest(keys: list[float], index: int, frequency_hz: float, width_hz: float) -> int | None:
        """Of the channels on either side of ``index`` in ``keys``, their
        frequencies in order, the nearest one that a signal at
        ``frequency_hz``, ``width_hz`` wide, belongs to."""
        found: int | None = None
        for candidate in (index - 1, index):
            if (0 <= candidate < len(keys) and same_signal(keys[candidate], 0.0, frequency_hz, width_hz)
                    and (found is None or abs(keys[candidate] - frequency_hz) < abs(keys[found] - frequency_hz))):
                found = candidate
        return found

    def _keep_latest(self) -> None:
        """Keep the MAX_CHANNELS found last, in frequency order."""
        latest = sorted(self._channels, key=lambda channel: channel.last_s, reverse=True)[:MAX_CHANNELS]
        self._channels = sorted(latest, key=lambda channel: channel.frequency_hz)

    def _restore(self, saved: object, now: float) -> None:
        entries = saved.get("channels", []) if isinstance(saved, dict) else None
        if not isinstance(entries, list):
            log.warning("the saved observations are not used")
            return
        channels = [_observation(entry) for entry in entries]
        invalid = channels.count(None)
        if invalid:
            log.warning("%d saved observations are not valid and are left out", invalid)
        self._channels = sorted((channel for channel in channels
                                 if channel is not None and now - channel.last_s <= FORGET_S),
                                key=lambda channel: channel.frequency_hz)
        if len(self._channels) > MAX_CHANNELS:
            log.warning("only the %d saved observations found last are used", MAX_CHANNELS)
            self._keep_latest()


def _observation(entry: object) -> Observation | None:
    """A saved channel, or None when it is not valid."""
    if not isinstance(entry, dict):
        return None
    frequency_hz = _number(entry.get("frequency_hz"), *FREQUENCY_RANGE_HZ)
    first_s = _number(entry.get("first_s"), 0.0, _MAX_TIME_S)
    last_s = _number(entry.get("last_s"), 0.0, _MAX_TIME_S)
    active_s = _number(entry.get("active_s"), 0.0, _MAX_TIME_S)
    margin_db = _number(entry.get("margin_db"), *_MARGIN_RANGE_DB)
    transmissions, hours = entry.get("transmissions"), entry.get("hours")
    if (frequency_hz is None or first_s is None or last_s is None or active_s is None or margin_db is None
            or first_s > last_s or not _whole(transmissions, 1)
            or not isinstance(hours, list) or len(hours) != HOURS or not all(_whole(count, 0) for count in hours)):
        return None
    return Observation(frequency_hz, first_s, last_s, transmissions, active_s, margin_db, list(hours))


def _number(value: object, low: float, high: float) -> float | None:
    """``value`` as a float when it is a number from ``low`` to ``high``; a
    whole number too large for a float is compared as it is, never
    converted."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value) if low <= value <= high else None


def _whole(value: object, low: int) -> bool:
    """Whether ``value`` is a whole number from ``low`` that JSON keeps exactly."""
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= _MAX_COUNT
