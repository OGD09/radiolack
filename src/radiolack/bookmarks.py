"""Bookmarks: named frequencies with a mode, shared by every interface,
and the categories they are sorted into (docs/design/bookmark-categories.md).

The engine keeps them, and store.py saves them. Each bookmark and each
category gets an id that interfaces use to change or remove it; ids last as
long as the engine runs. Bookmarks marked for scanning are the ones the
memory scan goes through. A bookmark is in one category at most; categories
hold bookmarks alone, one level deep. Interfaces change them with commands
(docs/design/protocol.md):
``{"action": "add", "frequency_hz", "name", "mode", "scan", "category"}``,
``{"action": "change", "id", ...the fields that change}``,
``{"action": "change", "ids", "category" and/or "scan"}``,
``{"action": "remove", "id"}``, ``{"action": "add_category", "name"}``,
``{"action": "rename_category", "id", "name"}`` and
``{"action": "remove_category", "id"}``, whose bookmarks become
uncategorized.

The bookmarks file keeps the bookmarks as an older Radiolack reads them, and
the categories beside them, each with the positions of its bookmarks in the
file's list: an older Radiolack, which does not write the categories back,
loses them, never a bookmark.
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
MAX_CATEGORIES = 100
MAX_CATEGORY_CHARS = 40
FREQUENCY_RANGE_HZ = (1.0, 1e11)        # as the VFO's
_FIELDS = ("frequency_hz", "name", "mode", "scan", "category")
_SAVED_FIELDS = ("frequency_hz", "name", "mode", "scan")     # what a saved bookmark may hold
_REQUIRED = ("frequency_hz", "name", "mode")
_SHARED_FIELDS = ("category", "scan")     # what a change of several bookmarks may set

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Bookmark:
    frequency_hz: float
    name: str
    mode: str
    scan: bool = True       # the memory scan stops here
    category: int | None = None     # its category's id

    def describe(self) -> dict[str, Any]:
        """As the interfaces receive it, without its id."""
        return {**self.saved(), "category": self.category}

    def saved(self) -> dict[str, Any]:
        """As the bookmarks file keeps it: its category is kept beside it."""
        return {"frequency_hz": self.frequency_hz, "name": self.name, "mode": self.mode, "scan": self.scan}


class Bookmarks:
    """The bookmarks, in frequency order, and their categories, in name
    order."""

    def __init__(self, stored: object = (), categories: object = ()) -> None:
        """Start from ``stored``, the bookmarks file's list, and
        ``categories``, its categories: entries that are not valid are left
        out, a category's positions that name no bookmark kept, or one
        already placed, too."""
        self._items: dict[int, Bookmark] = {}
        self._next_id = 1
        self._categories: dict[int, str] = {}
        self._next_category = 1
        if not isinstance(stored, (list, tuple)):
            log.warning("the saved bookmarks are not a list: none is used")
            stored = ()
        kept: dict[int, int] = {}       # a position in the file's list: the id of the bookmark kept from it
        for position, entry in enumerate(stored):
            try:
                if not isinstance(entry, dict):
                    raise ValueError("not a JSON object")
                if len(self._items) >= MAX_BOOKMARKS:
                    raise ValueError(f"more than {MAX_BOOKMARKS} bookmarks")
                unknown = next((name for name in entry if name not in _SAVED_FIELDS), None)
                if unknown is not None:
                    raise ValueError(f"unknown bookmark field: {unknown}")
                kept[position] = self._add(Bookmark(**_checked(entry, _REQUIRED)))
            except ValueError as error:
                log.warning("a saved bookmark is left out: %s", error)
        self._restore_categories(categories, kept)

    def _restore_categories(self, categories: object, kept: Mapping[int, int]) -> None:
        if not isinstance(categories, (list, tuple)):
            log.warning("the saved categories are not a list: none is used")
            return
        for entry in categories:
            try:
                if not isinstance(entry, dict) or set(entry) != {"name", "bookmarks"}:
                    raise ValueError("not a JSON object with a name and bookmarks")
                if len(self._categories) >= MAX_CATEGORIES:
                    raise ValueError(f"more than {MAX_CATEGORIES} categories")
                name = self._new_category_name(entry["name"])
                positions = entry["bookmarks"]
                if not isinstance(positions, list):
                    raise ValueError(f"the bookmarks of {name} are not a list")
            except ValueError as error:
                log.warning("a saved category is left out: %s", error)
                continue
            category_id = self._next_category
            self._next_category += 1
            self._categories[category_id] = name
            for position in positions:
                bookmark_id = None if isinstance(position, bool) or not isinstance(position, int) else kept.get(position)
                if bookmark_id is None or self._items[bookmark_id].category is not None:
                    log.warning("a bookmark of the saved category %s is left out of it: no bookmark at %r, "
                                "or one in another category", name, position)
                    continue
                self._items[bookmark_id] = dataclasses.replace(self._items[bookmark_id], category=category_id)

    def apply(self, command: Mapping[str, object]) -> None:
        """Add, change or remove a bookmark, change several, or add, rename
        or remove a category; raises ValueError, changing nothing, when the
        command is refused."""
        fields = dict(command)
        action = fields.pop("action", None)
        if action == "add":
            if len(self._items) >= MAX_BOOKMARKS:
                raise ValueError(f"there are already {MAX_BOOKMARKS} bookmarks, the most Radiolack keeps")
            self._add(Bookmark(**self._checked(fields, _REQUIRED)))
        elif action == "change" and "ids" in fields:
            ids = self._several(fields.pop("ids"))
            if not fields:
                raise ValueError("a change of several bookmarks sets their category or scan")
            unknown = next((name for name in fields if name not in _SHARED_FIELDS), None)
            if unknown is not None:
                raise ValueError(f"a change of several bookmarks sets their category or scan alone, not {unknown}")
            changes = self._checked(fields, ())
            for bookmark_id in ids:
                self._items[bookmark_id] = dataclasses.replace(self._items[bookmark_id], **changes)
        elif action == "change":
            bookmark_id = self._existing(fields.pop("id", None))
            self._items[bookmark_id] = dataclasses.replace(self._items[bookmark_id], **self._checked(fields, ()))
        elif action == "remove":
            bookmark_id = self._existing(fields.pop("id", None))
            _no_more(fields)
            del self._items[bookmark_id]
        elif action == "add_category":
            name = self._new_category_name(fields.pop("name", None))
            _no_more(fields)
            if len(self._categories) >= MAX_CATEGORIES:
                raise ValueError(f"there are already {MAX_CATEGORIES} categories, the most Radiolack keeps")
            self._categories[self._next_category] = name
            self._next_category += 1
        elif action == "rename_category":
            category_id = self._existing_category(fields.pop("id", None))
            name = self._new_category_name(fields.pop("name", None), category_id)
            _no_more(fields)
            self._categories[category_id] = name
        elif action == "remove_category":
            category_id = self._existing_category(fields.pop("id", None))
            _no_more(fields)
            del self._categories[category_id]
            for bookmark_id, bookmark in list(self._items.items()):
                if bookmark.category == category_id:
                    self._items[bookmark_id] = dataclasses.replace(bookmark, category=None)
        else:
            raise ValueError('a bookmark action is "add", "change", "remove", "add_category", '
                             '"rename_category" or "remove_category"')

    def describe(self) -> list[dict[str, Any]]:
        """The bookmarks with their ids, for the interfaces."""
        return [{"id": bookmark_id, **bookmark.describe()} for bookmark_id, bookmark in self._sorted()]

    def categories(self) -> list[dict[str, Any]]:
        """The categories with their ids, by name ignoring case, for the
        interfaces."""
        return [{"id": category_id, "name": name} for category_id, name in self._sorted_categories()]

    def ticked(self) -> list[tuple[int, Bookmark]]:
        """The bookmarks that the memory scan goes through, with their ids, in
        frequency order."""
        return [(bookmark_id, bookmark) for bookmark_id, bookmark in self._sorted() if bookmark.scan]

    def stored(self) -> dict[str, Any]:
        """The bookmarks file's content: the bookmarks without ids, then the
        categories, if any, each with the positions of its bookmarks in that
        list."""
        ordered = self._sorted()
        positions: dict[int, list[int]] = {category_id: [] for category_id in self._categories}
        for position, (_, bookmark) in enumerate(ordered):
            if bookmark.category is not None:
                positions[bookmark.category].append(position)
        content: dict[str, Any] = {"bookmarks": [bookmark.saved() for _, bookmark in ordered]}
        if self._categories:        # the file stays as before until a category is made
            content["categories"] = [{"name": name, "bookmarks": positions[category_id]}
                                     for category_id, name in self._sorted_categories()]
        return content

    def _add(self, bookmark: Bookmark) -> int:
        bookmark_id = self._next_id
        self._items[bookmark_id] = bookmark
        self._next_id += 1
        return bookmark_id

    def _existing(self, bookmark_id: object) -> int:
        if isinstance(bookmark_id, bool) or not isinstance(bookmark_id, int):
            raise ValueError("a bookmark id must be a whole number")
        if bookmark_id not in self._items:
            raise ValueError("no such bookmark: another interface may have removed it")
        return bookmark_id

    def _several(self, ids: object) -> list[int]:
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BOOKMARKS:
            raise ValueError(f"ids must be a list of 1 to {MAX_BOOKMARKS} bookmark ids")
        checked = [self._existing(bookmark_id) for bookmark_id in ids]
        if len(set(checked)) != len(checked):
            raise ValueError("ids names a bookmark twice")
        return checked

    def _existing_category(self, category_id: object) -> int:
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            raise ValueError("a category id must be a whole number")
        if category_id not in self._categories:
            raise ValueError("no such category: another interface may have removed it")
        return category_id

    def _new_category_name(self, value: object, renamed: int | None = None) -> str:
        """``value`` as a category's name, which no other category has,
        ignoring case; ``renamed`` may keep its own."""
        name = _name(value, "a category's name", MAX_CATEGORY_CHARS)
        folded = name.casefold()
        if any(other.casefold() == folded for category_id, other in self._categories.items() if category_id != renamed):
            raise ValueError(f"a category is already named {name}")
        return name

    def _checked(self, fields: Mapping[str, object], required: tuple[str, ...]) -> dict[str, Any]:
        """``fields`` checked and normalised, a category among them naming one
        that exists, or null."""
        values = _checked(fields, required)
        if "category" in values and values["category"] is not None:
            self._existing_category(values["category"])
        return values

    def _sorted(self) -> list[tuple[int, Bookmark]]:
        return sorted(self._items.items(),
                      key=lambda item: (item[1].frequency_hz, item[1].name.casefold(), item[0]))

    def _sorted_categories(self) -> list[tuple[int, str]]:
        return sorted(self._categories.items(), key=lambda item: (item[1].casefold(), item[1], item[0]))


def _no_more(fields: Mapping[str, object]) -> None:
    if fields:
        raise ValueError(f"unknown bookmark field: {next(iter(fields))}")


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
            values[name] = _name(value, "a bookmark's name", MAX_NAME_CHARS)
        elif name == "mode":
            if value not in MODES:
                raise ValueError(f"a bookmark's mode must be one of {', '.join(MODES)}")
            values[name] = value
        elif name == "category":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError("a bookmark's category must be a category id or null")
            values[name] = value
        else:
            if not isinstance(value, bool):
                raise ValueError("a bookmark's scan setting must be true or false")
            values[name] = value
    return values


def _name(value: object, what: str, longest: int) -> str:
    """``value`` as ``what``, a name on one line of at most ``longest``
    characters."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    name = " ".join(value.split())         # no line breaks or runs of spaces
    if not name:
        raise ValueError(f"{what} cannot be empty")
    if len(name) > longest:
        raise ValueError(f"{what} is at most {longest} characters")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in name):
        raise ValueError(f"{what} cannot hold control characters")
    return name
