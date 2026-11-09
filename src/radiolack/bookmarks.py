"""Bookmarks: named frequencies with a mode, shared by every interface.

The engine keeps them, and store.py saves them. Each bookmark gets an id that
interfaces use to change or remove it; ids last as long as the engine runs.
Bookmarks marked for scanning are the ones the memory scan goes through.
Interfaces change them with commands (docs/design/protocol.md):
``{"action": "add", "frequency_hz", "name", "mode", "scan"}``,
``{"action": "change", "id", ...the fields that change}`` and
``{"action": "remove", "id"}``.
"""

from __future__ import annotations

import dataclasses
import logging
import unicodedata
from collections.abc import Mapping
from typing import Any

from .demod import MODES

MAX_BOOKMARKS = 2000
MAX_NAME_CHARS = 60
FREQUENCY_RANGE_HZ = (1.0, 1e11)        # as the VFO's
_FIELDS = ("frequency_hz", "name", "mode", "scan")
_REQUIRED = ("frequency_hz", "name", "mode")

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Bookmark:
    frequency_hz: float
    name: str
    mode: str
    scan: bool = True       # the memory scan stops here

    def describe(self) -> dict[str, Any]:
        return {"frequency_hz": self.frequency_hz, "name": self.name, "mode": self.mode, "scan": self.scan}


class Bookmarks:
    """The bookmarks, in frequency order."""

    def __init__(self, stored: object = ()) -> None:
        """Start from ``stored``, a list read from the bookmarks file: entries
        that are not valid bookmarks are left out."""
        self._items: dict[int, Bookmark] = {}
        self._next_id = 1
        if not isinstance(stored, (list, tuple)):
            log.warning("the saved bookmarks are not a list: none is used")
            return
        for entry in stored:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("not a JSON object")
                if len(self._items) >= MAX_BOOKMARKS:
                    raise ValueError(f"more than {MAX_BOOKMARKS} bookmarks")
                self._add(Bookmark(**_checked(entry, _REQUIRED)))
            except ValueError as error:
                log.warning("a saved bookmark is left out: %s", error)

    def apply(self, command: Mapping[str, object]) -> None:
        """Add, change or remove a bookmark; raises ValueError, changing
        nothing, when the command is refused."""
        fields = dict(command)
        action = fields.pop("action", None)
        if action == "add":
            if len(self._items) >= MAX_BOOKMARKS:
                raise ValueError(f"there are already {MAX_BOOKMARKS} bookmarks, the most Radiolack keeps")
            self._add(Bookmark(**_checked(fields, _REQUIRED)))
        elif action == "change":
            bookmark_id = self._existing(fields.pop("id", None))
            self._items[bookmark_id] = dataclasses.replace(self._items[bookmark_id], **_checked(fields, ()))
        elif action == "remove":
            bookmark_id = self._existing(fields.pop("id", None))
            if fields:
                raise ValueError(f"unknown bookmark field: {next(iter(fields))}")
            del self._items[bookmark_id]
        else:
            raise ValueError('a bookmark action is "add", "change" or "remove"')

    def describe(self) -> list[dict[str, Any]]:
        """The bookmarks with their ids, for the interfaces."""
        return [{"id": bookmark_id, **bookmark.describe()} for bookmark_id, bookmark in self._sorted()]

    def stored(self) -> list[dict[str, Any]]:
        """The bookmarks as the bookmarks file keeps them, without ids."""
        return [bookmark.describe() for _, bookmark in self._sorted()]

    def _add(self, bookmark: Bookmark) -> None:
        self._items[self._next_id] = bookmark
        self._next_id += 1

    def _existing(self, bookmark_id: object) -> int:
        if isinstance(bookmark_id, bool) or not isinstance(bookmark_id, int):
            raise ValueError("a bookmark id must be a whole number")
        if bookmark_id not in self._items:
            raise ValueError("no such bookmark: another interface may have removed it")
        return bookmark_id

    def _sorted(self) -> list[tuple[int, Bookmark]]:
        return sorted(self._items.items(),
                      key=lambda item: (item[1].frequency_hz, item[1].name.casefold(), item[0]))


def _checked(fields: Mapping[str, object], required: tuple[str, ...]) -> dict[str, Any]:
    """``fields`` checked and normalised; ValueError for a missing, unknown or
    invalid one."""
    missing = [name for name in required if name not in fields]
    if missing:
        raise ValueError(f"a bookmark needs a {missing[0].removesuffix('_hz').replace('_', ' ')}")
    values: dict[str, Any] = {}
    for name, value in fields.items():
        if name not in _FIELDS:
            raise ValueError(f"unknown bookmark field: {name}")
        if name == "frequency_hz":
            low, high = FREQUENCY_RANGE_HZ
            # NaN and infinities fail the comparison, as do whole numbers too large for a float.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"a bookmark's frequency must be a number from {low:g} to {high:g} Hz")
            values[name] = float(round(value))
        elif name == "name":
            values[name] = _name(value)
        elif name == "mode":
            if value not in MODES:
                raise ValueError(f"a bookmark's mode must be one of {', '.join(MODES)}")
            values[name] = value
        else:
            if not isinstance(value, bool):
                raise ValueError("a bookmark's scan setting must be true or false")
            values[name] = value
    return values


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a bookmark's name must be text")
    name = " ".join(value.split())         # no line breaks or runs of spaces
    if not name:
        raise ValueError("a bookmark's name cannot be empty")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"a bookmark's name is at most {MAX_NAME_CHARS} characters")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in name):
        raise ValueError("a bookmark's name cannot hold control characters")
    return name
