"""The interfaces' preferences: their own settings, such as the palette or the
volume, which the engine keeps without knowing their meaning, one set for
this computer's interfaces and one for each paired device (remote.py).

A set holds at most MAX_PREFERENCES values, each named by 1 to MAX_NAME
characters and a number, true or false, a text of at most MAX_TEXT characters
or null, as the preferences message carries them (docs/design/protocol.md).
"""

from __future__ import annotations

import logging
import math
from typing import Any

MAX_PREFERENCES = 100   # values in a set
MAX_NAME = 40           # characters in a value's name
MAX_TEXT = 100          # characters in a text value

log = logging.getLogger(__name__)


def check_preferences(values: object) -> dict[str, Any]:
    """A copy of ``values``, a set that an interface gave; raises ValueError
    when it is not a valid set."""
    if not isinstance(values, dict):
        raise ValueError("the preferences must be a JSON object")
    if len(values) > MAX_PREFERENCES:
        raise ValueError(f"there may be at most {MAX_PREFERENCES} preferences")
    for name, value in values.items():
        if not _valid(name, value):
            raise ValueError(f"the preference {str(name)[:MAX_NAME]!r} must be a number, "
                             f"true or false, a text of at most {MAX_TEXT} characters or null")
    return dict(values)


def saved_preferences(saved: object) -> dict[str, Any] | None:
    """A saved set, leaving out the values that are not valid and those beyond
    the limit; None when none was saved or what was saved is not a set."""
    if saved is None:
        return None
    if not isinstance(saved, dict):
        log.warning("saved preferences are not used: they are not a set of values")
        return None
    valid = {name: value for name, value in saved.items() if _valid(name, value)}
    if len(valid) < len(saved) or len(valid) > MAX_PREFERENCES:
        log.warning("some saved preferences are not used")
    return dict(list(valid.items())[:MAX_PREFERENCES])


def _valid(name: object, value: object) -> bool:
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= 2 ** 53       # what JavaScript counts exactly
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= MAX_TEXT
