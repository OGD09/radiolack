"""The names that the user gives the devices, shown on the Receivers
menu's cards, the chip beside the frequency and the device list in place of
the device's own name.

Each name belongs to a device, by the key that the interface also forms
from the radios message: its kind and the receiver it reads, such as
"rtl_sdr:RTLSDR1" (an RTL-SDR's serial number, or its index when opened by
one), "soapy:hackrf 0001" (a SoapySDR device's driver and serial number),
"rtl_tcp:127.0.0.1:1234" (an rtl_tcp server's address) or "synthetic:".
The settings file keeps them under "device_names"; an entry that is not a
name is left out when read.
"""

from __future__ import annotations

import unicodedata
from typing import Any

NAME_MAX = 40       # characters in a name
NAMES_MAX = 64      # names kept: the oldest go first beyond it
KEY_MAX = 200       # characters in a device's key


def device_key(summary: dict[str, Any]) -> str:
    """The key of the device that a radio reads, from its summary in the
    radios message (radio.py source_summary)."""
    return f"{summary['kind']}:{summary.get('device') or ''}"


def checked_name(value: object) -> str:
    """A name as given, its outer spaces removed; "" forgets the name.
    Raises ValueError for anything else than a text of NAME_MAX characters
    at most without control characters."""
    if not isinstance(value, str):
        raise ValueError("a device's name is a text")
    name = value.strip()
    if len(name) > NAME_MAX:
        raise ValueError(f"a device's name takes {NAME_MAX} characters at most")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise ValueError("a device's name takes no control characters")
    return name


def saved_names(saved: object) -> dict[str, str]:
    """The names as the settings file keeps them, without the entries that
    are not names, NAMES_MAX at most."""
    names: dict[str, str] = {}
    if not isinstance(saved, dict):
        return names
    for key, value in saved.items():
        if not isinstance(key, str) or not 0 < len(key) <= KEY_MAX or ":" not in key:
            continue
        try:
            name = checked_name(value)
        except ValueError:
            continue
        if name:
            names[key] = name
    return dict(list(names.items())[-NAMES_MAX:])


def renamed(names: dict[str, str], key: str, name: str) -> dict[str, str]:
    """``names`` with ``key``'s name changed to ``name``, a checked one, or
    forgotten when it is ""; the latest last, NAMES_MAX at most."""
    changed = {other: value for other, value in names.items() if other != key}
    if name:
        changed[key] = name
    return dict(list(changed.items())[-NAMES_MAX:])
