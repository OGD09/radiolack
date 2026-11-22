"""Kept scans: each range scan under a name, with the channels it found,
kept across restarts until deleted, in scans.json in the settings folder
(store.py; docs/design/range-scan.md, Named scans).

A kept scan holds its name, start and end, range, whether it listened, the
passes set and done, and why it ended; and for each channel found, on the
frequency that the lists show it on (the band plan's channel, or the
100 Hz): when it was first and last found, how many searches found it, its
highest margin above the local noise and, when the scan listened, how long
the squelch stayed open there. Never audio or samples. Detections within
MERGE_HZ of a channel count for it.

At most MAX_SCANS are kept, the oldest leaving first, and MAX_CHANNELS in
each, the least recently found leaving first, never a scan that runs. A
scan runs on one radio, and each radio runs one at a time: its ``end_s``
and ``ended`` are None until it ends. A scan saved while it ran,
before a power cut for instance, is read back as ``interrupted`` at its
latest finding.
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import math
import time
import unicodedata
from collections.abc import Iterable
from typing import Any

from .scanners import FREQUENCY_RANGE_HZ, MERGE_HZ, PASSES_RANGE

MAX_SCANS = 100
MAX_CHANNELS = 1000
NAME_LENGTH = 60
# Why a kept scan ended: the engine's reasons for a scan that stops by
# itself, "stopped" when an interface stopped it, "closed" when Radiolack
# stopped, "interrupted" when it ran at the latest write before a restart.
ENDINGS = ("stopped", "tuned", "source", "range", "detection", "done", "closed", "interrupted")
_MAX_TIME_S = 1e11              # beyond any clock: the year 5138
_MAX_COUNT = 2 ** 53            # whole numbers that JSON numbers keep exactly
_MAX_ID = 2 ** 53 - 1
_MARGIN_RANGE_DB = (0.0, 200.0)

log = logging.getLogger(__name__)


@dataclasses.dataclass
class KeptChannel:
    """A channel a scan found. Times are ``time.time()``."""

    frequency_hz: int       # as the lists show it
    first_s: float
    last_s: float
    searches: int           # the searches that found it
    margin_db: float        # the highest
    open_s: float           # how long the squelch stayed open there while the scan listened

    def describe(self) -> dict[str, Any]:
        """As JSON values, for the interfaces and the file."""
        return {"frequency_hz": self.frequency_hz, "first_s": round(self.first_s), "last_s": round(self.last_s),
                "searches": self.searches, "margin_db": round(self.margin_db, 1), "open_s": round(self.open_s, 1)}


@dataclasses.dataclass
class KeptScan:
    """A range scan and what it found. Times are ``time.time()``."""

    id: int
    name: str
    start_s: float
    end_s: float | None     # None while it runs
    low_hz: float
    high_hz: float
    listen: bool
    passes_set: int | None  # None: endless
    passes: int             # the passes done
    ended: str | None       # why it ended (ENDINGS); None while it runs
    channels: list[KeptChannel] = dataclasses.field(default_factory=list)    # in frequency order

    def summary(self) -> dict[str, Any]:
        """What the list of kept scans shows of it, as JSON values."""
        return {"id": self.id, "name": self.name, "start_s": round(self.start_s),
                "end_s": None if self.end_s is None else round(self.end_s),
                "low_hz": round(self.low_hz), "high_hz": round(self.high_hz), "listen": self.listen,
                "passes_set": self.passes_set, "passes": self.passes, "ended": self.ended,
                "channels": len(self.channels)}

    def stored(self) -> dict[str, Any]:
        """What scans.json holds of it."""
        return {**self.summary(), "channels": [channel.describe() for channel in self.channels]}


def checked_name(value: object) -> str | None:
    """A scan's name an interface gave: one line of at most NAME_LENGTH
    characters, spaces around it left out; None, or an empty name, for the
    name the engine gives (``default_name``). Raises ValueError otherwise."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("a scan's name must be text")
    name = value.strip()
    if len(name) > NAME_LENGTH or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name):
        raise ValueError(f"a scan's name is one line of at most {NAME_LENGTH} characters")
    return name or None


def default_name(low_hz: float, high_hz: float, start_s: float) -> str:
    """The name of a scan started without one: its range and its start, in
    the local time of the engine's computer, as "430.000-440.000 MHz,
    2026-11-20 14:05"."""
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_s))
    return f"{low_hz / 1e6:.3f}-{high_hz / 1e6:.3f} MHz, {start}"


class KeptScans:
    """The kept scans, the oldest first. ``saved`` is what scans.json held,
    without its version: the scans that are not valid are left out."""

    def __init__(self, saved: object = None) -> None:
        self._scans: list[KeptScan] = []
        self._followed: dict[object, KeptScan] = {}     # scans that run elsewhere, by their key there
        if saved is not None:
            self._restore(saved)

    def __len__(self) -> int:
        return len(self._scans)

    @property
    def running(self) -> tuple[KeptScan, ...]:
        """The scans that run, one at most on each radio: those that have
        not ended."""
        return tuple(scan for scan in self._scans if scan.ended is None)

    def start(self, name: str | None, now: float, low_hz: float, high_hz: float, listen: bool,
              passes_set: int | None) -> KeptScan:
        """Keep a new scan, which runs from ``now``, until ``end``; the
        oldest that does not run leaves when MAX_SCANS are kept."""
        scan_id = max((scan.id for scan in self._scans), default=0) + 1
        if scan_id > _MAX_ID:     # never in practice: ids start again above the ones kept
            scan_id = 1
        scan = KeptScan(scan_id, name or default_name(low_hz, high_hz, now), now, None, low_hz, high_hz, listen,
                        passes_set, 0, None)
        self._scans.append(scan)
        ended = [kept for kept in self._scans if kept.ended is not None]
        for kept in ended[:max(0, len(self._scans) - MAX_SCANS)]:
            self._scans.remove(kept)
        return scan

    @staticmethod
    def record(scan: KeptScan | None, found: Iterable[tuple[int, float]], now: float) -> bool:
        """Record, for ``scan`` while it runs, the channels found in one
        search, each as its frequency and margin. Returns whether any was
        recorded."""
        if scan is None or scan.ended is not None:
            return False
        recorded = False
        for frequency_hz, margin_db in found:
            recorded = True
            channel = KeptScans._nearest(scan, frequency_hz)
            if channel is None:
                bisect.insort(scan.channels, KeptChannel(frequency_hz, now, now, 1, margin_db, 0.0),
                              key=lambda kept: kept.frequency_hz)
                continue
            channel.first_s = min(channel.first_s, now)
            channel.last_s = max(channel.last_s, now)
            channel.searches += 1
            channel.margin_db = max(channel.margin_db, margin_db)
        if len(scan.channels) > MAX_CHANNELS:
            latest = sorted(scan.channels, key=lambda kept: kept.last_s, reverse=True)[:MAX_CHANNELS]
            scan.channels = sorted(latest, key=lambda kept: kept.frequency_hz)
        return recorded

    @staticmethod
    def listened(scan: KeptScan | None, frequency_hz: float, seconds: float) -> None:
        """Add ``seconds`` of open squelch on ``scan``'s channel at
        ``frequency_hz``, as the lists show it, once found, while it runs."""
        if scan is None or scan.ended is not None:
            return
        channel = KeptScans._nearest(scan, frequency_hz)
        if channel is not None and seconds > 0:
            channel.open_s += seconds

    @staticmethod
    def progress(scan: KeptScan | None, passes: int) -> bool:
        """The passes ``scan`` has done while it runs. Returns whether they
        changed."""
        if scan is None or scan.ended is not None or scan.passes == passes:
            return False
        scan.passes = passes
        return True

    @staticmethod
    def end(scan: KeptScan | None, now: float, ended: str) -> bool:
        """End ``scan`` at ``now``, for ``ended`` (ENDINGS). Returns whether
        it ran."""
        if scan is None or scan.ended is not None:
            return False
        scan.end_s = max(now, scan.start_s)
        scan.ended = ended
        return True

    def follow(self, key: object, entry: object) -> bool:
        """Follow a scan that runs in a radio's own process (radioproc.py):
        ``entry`` is what ``KeptScan.stored`` gives of it there, ``key``
        names it among those followed. The first time, it joins the kept
        scans under an id of their own, as ``start`` keeps a scan; then its
        progress replaces what is kept, its id and name staying, as an
        interface may rename it. Once it ended it is no longer followed.
        False, changing nothing, for an entry that is not valid, or for a
        scan deleted since it ended."""
        scan = _scan(entry)
        if scan is None:
            log.warning("a radio's kept scan is not valid and is left out")
            return False
        kept = self._followed.get(key)
        if kept is None:
            if scan.ended is not None:
                return False        # it ended before it was followed: never shown running
            kept = self.start(scan.name, scan.start_s, scan.low_hz, scan.high_hz, scan.listen, scan.passes_set)
            self._followed[key] = kept
        elif kept not in self._scans:
            del self._followed[key]
            return False
        kept.passes, kept.channels = scan.passes, scan.channels
        if scan.ended is not None:
            kept.end_s, kept.ended = scan.end_s, scan.ended
            del self._followed[key]
        return True

    def end_all(self, now: float, ended: str) -> bool:
        """End every scan that runs, as Radiolack stops. Returns whether any
        ran."""
        return any([self.end(scan, now, ended) for scan in self.running])

    def rename(self, scan_id: object, name: object) -> None:
        """Rename a kept scan, the running one included. Raises ValueError
        for an unknown scan or a name that is not valid, or empty."""
        scan = self._find(scan_id)
        checked = checked_name(name)
        if checked is None:
            raise ValueError("a scan's new name cannot be empty")
        scan.name = checked

    def delete(self, scan_id: object) -> None:
        """Forget a kept scan. Raises ValueError for an unknown scan, or one
        that runs."""
        scan = self._find(scan_id)
        if scan.ended is None:
            raise ValueError("the scan that runs cannot be deleted: stop it first")
        self._scans.remove(scan)

    def clear_all(self) -> bool:
        """Forget every kept scan but those that run. Returns whether there
        were any."""
        kept = list(self.running)
        cleared = len(self._scans) > len(kept)
        self._scans = kept
        return cleared

    def summaries(self) -> list[dict[str, Any]]:
        """The kept scans as the list shows them: the latest first, the one
        that run among them."""
        return [scan.summary() for scan in reversed(self._scans)]

    def channels(self, scan_id: object) -> dict[str, Any]:
        """A kept scan with its channels, in frequency order. Raises
        ValueError for an unknown scan."""
        return self._find(scan_id).stored()

    def stored(self) -> dict[str, Any]:
        """What scans.json holds, without its version: the oldest first."""
        return {"scans": [scan.stored() for scan in self._scans]}

    def _find(self, scan_id: object) -> KeptScan:
        if isinstance(scan_id, bool) or not isinstance(scan_id, int):
            raise ValueError("a kept scan's id must be a whole number")
        for scan in self._scans:
            if scan.id == scan_id:
                return scan
        raise ValueError("no such kept scan: it may have been deleted")

    @staticmethod
    def _nearest(scan: KeptScan, frequency_hz: float) -> KeptChannel | None:
        """The scan's channel within MERGE_HZ of ``frequency_hz``, the nearest."""
        index = bisect.bisect_left(scan.channels, frequency_hz, key=lambda kept: kept.frequency_hz)
        found: KeptChannel | None = None
        for candidate in scan.channels[max(0, index - 1):index + 1]:
            if (abs(candidate.frequency_hz - frequency_hz) <= MERGE_HZ
                    and (found is None or abs(candidate.frequency_hz - frequency_hz)
                         < abs(found.frequency_hz - frequency_hz))):
                found = candidate
        return found

    def _restore(self, saved: object) -> None:
        entries = saved.get("scans", []) if isinstance(saved, dict) else None
        if not isinstance(entries, list):
            log.warning("the saved scans are not used")
            return
        scans = [_scan(entry) for entry in entries]
        invalid = scans.count(None)
        if invalid:
            log.warning("%d saved scans are not valid and are left out", invalid)
        ids: set[int] = set()
        for scan in scans:
            if scan is None or scan.id in ids:
                continue
            ids.add(scan.id)
            if scan.ended is None:      # it ran at the latest write
                scan.ended = "interrupted"
                scan.end_s = max([scan.start_s] + [channel.last_s for channel in scan.channels])
            self._scans.append(scan)
        self._scans.sort(key=lambda scan: scan.start_s)
        if len(self._scans) > MAX_SCANS:
            log.warning("only the %d latest saved scans are used", MAX_SCANS)
            del self._scans[:-MAX_SCANS]


def _scan(entry: object) -> KeptScan | None:
    """A saved scan, or None when it is not valid; its channels that are
    not valid are left out."""
    if not isinstance(entry, dict):
        return None
    scan_id, name, passes_set, ended = entry.get("id"), entry.get("name"), entry.get("passes_set"), entry.get("ended")
    start_s = _number(entry.get("start_s"), 0.0, _MAX_TIME_S)
    end_s = None if entry.get("end_s") is None else _number(entry.get("end_s"), 0.0, _MAX_TIME_S)
    low_hz = _number(entry.get("low_hz"), *FREQUENCY_RANGE_HZ)
    high_hz = _number(entry.get("high_hz"), *FREQUENCY_RANGE_HZ)
    channels = entry.get("channels")
    try:
        checked = checked_name(name)
    except ValueError:
        checked = None
    if (not _whole(scan_id, 1) or scan_id > _MAX_ID or checked is None or start_s is None
            or low_hz is None or high_hz is None or low_hz >= high_hz
            or not isinstance(entry.get("listen"), bool) or not _whole(entry.get("passes"), 0)
            or not (passes_set is None or (_whole(passes_set, PASSES_RANGE[0]) and passes_set <= PASSES_RANGE[1]))
            or not (ended is None or ended in ENDINGS) or (ended is None) != (entry.get("end_s") is None)
            or (entry.get("end_s") is not None and (end_s is None or end_s < start_s))
            or not isinstance(channels, list)):
        return None
    kept = sorted((channel for channel in map(_channel, channels) if channel is not None),
                  key=lambda channel: channel.frequency_hz)[:MAX_CHANNELS]
    return KeptScan(scan_id, checked, start_s, end_s, low_hz, high_hz, entry["listen"], passes_set,
                    entry["passes"], ended, kept)


def _channel(entry: object) -> KeptChannel | None:
    """A saved channel, or None when it is not valid."""
    if not isinstance(entry, dict):
        return None
    frequency_hz = entry.get("frequency_hz")
    first_s = _number(entry.get("first_s"), 0.0, _MAX_TIME_S)
    last_s = _number(entry.get("last_s"), 0.0, _MAX_TIME_S)
    margin_db = _number(entry.get("margin_db"), *_MARGIN_RANGE_DB)
    open_s = _number(entry.get("open_s"), 0.0, _MAX_TIME_S)
    if (not _whole(frequency_hz, 1) or frequency_hz > FREQUENCY_RANGE_HZ[1] or first_s is None or last_s is None
            or first_s > last_s or margin_db is None or open_s is None or not _whole(entry.get("searches"), 1)):
        return None
    return KeptChannel(frequency_hz, first_s, last_s, entry["searches"], margin_db, open_s)


def _number(value: object, low: float, high: float) -> float | None:
    """``value`` as a float when it is a number from ``low`` to ``high``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)):
        return None
    return float(value) if low <= value <= high else None


def _whole(value: object, low: int) -> bool:
    """Whether ``value`` is a whole number from ``low`` that JSON keeps exactly."""
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= _MAX_COUNT
