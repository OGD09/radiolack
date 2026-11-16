"""A converter in front of a receiver: an upconverter for shortwave, or a
downconverter such as a satellite LNB, set by its oscillator's frequency.

Every frequency that Radiolack shows and stores is the one on the air: only
the source converts, for the frequency it asks of the receiver. An
upconverter puts a signal at the frequency plus its oscillator's, a
downconverter at the frequency minus its oscillator's, and a downconverter
whose oscillator lies above the band at the oscillator's minus the
frequency, which mirrors the spectrum: its source swaps I and Q as the
samples arrive, so that they show the band as it is on the air.

A source's reach is the receiver's, converted, kept at or above half the
sample rate, so that the span never goes below 0 Hz, and at or below
Radiolack's highest frequency less half the sample rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# What may stand in front of the receiver: nothing, an upconverter, a
# downconverter (an LNB) and a downconverter that mirrors the spectrum.
CONVERTERS = ("none", "up", "down", "down_inverted")
DEFAULT_LO_HZ = 125_000_000.0   # the common upconverters' oscillator, until the user types another
HIGHEST_HZ = 1e11               # the highest frequency Radiolack shows: as the VFO's

_NAMES = {"up": "an upconverter", "down": "a downconverter",
          "down_inverted": "a downconverter that inverts the spectrum"}


@dataclass(frozen=True)
class Conversion:
    """A converter and its oscillator's frequency, in whole hertz."""

    kind: str = "none"
    lo_hz: float = DEFAULT_LO_HZ

    @property
    def active(self) -> bool:
        return self.kind != "none"

    @property
    def inverted(self) -> bool:
        """Whether the spectrum reaches the receiver mirrored."""
        return self.kind == "down_inverted"

    @property
    def key(self) -> tuple[str, float | None]:
        """What changes the frequencies: the oscillator does nothing without a converter."""
        return (self.kind, self.lo_hz if self.active else None)

    def tuned(self, air_hz: float) -> float:
        """Where the receiver tunes for ``air_hz`` on the air."""
        if self.kind == "up":
            return air_hz + self.lo_hz
        if self.kind == "down":
            return air_hz - self.lo_hz
        if self.kind == "down_inverted":
            return self.lo_hz - air_hz
        return air_hz

    def air(self, tuned_hz: float) -> float:
        """The frequency on the air that the receiver gets at ``tuned_hz``."""
        if self.kind == "up":
            return tuned_hz - self.lo_hz
        if self.kind == "down":
            return tuned_hz + self.lo_hz
        if self.kind == "down_inverted":
            return self.lo_hz - tuned_hz
        return tuned_hz

    def reach(self, ranges: list[tuple[float, float, str]],
              rate_hz: float) -> list[tuple[float, float, str]]:
        """The centre frequencies on the air for those the receiver tunes,
        ``ranges`` with a label each, at a sample rate of ``rate_hz``:
        lowest first, without those below half the sample rate or above
        HIGHEST_HZ less half of it; none when nothing is left. Without a
        converter, the receiver's ranges as they are."""
        if not self.active:
            return list(ranges)
        half = float(math.ceil(rate_hz / 2))    # whole hertz, as the centre is
        floor, ceiling = half, HIGHEST_HZ - half
        reached = []
        for low, high, label in ranges:
            low, high = sorted((self.air(low), self.air(high)))
            low, high = max(low, floor), min(high, ceiling)
            if low <= high:
                reached.append((low, high, label))
        return sorted(reached)

    def text(self) -> str:
        """The converter in a few words, such as "an upconverter with its
        oscillator at 125 MHz"; empty without one."""
        if not self.active:
            return ""
        return f"{_NAMES[self.kind]} with its oscillator at {mhz(self.lo_hz)} MHz"


def checked_converter(value: object) -> str:
    """``value`` as a converter; ValueError otherwise."""
    if not isinstance(value, str) or value not in CONVERTERS:
        raise ValueError(f"the converter must be one of: {', '.join(CONVERTERS)}")
    return value


def checked_lo(value: object) -> float:
    """``value`` as an oscillator's frequency, rounded to the hertz, above
    0 Hz and at most HIGHEST_HZ; ValueError otherwise."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            hz = float(round(value))
        except (OverflowError, ValueError):     # infinite or not a number
            hz = math.nan
        if 1 <= hz <= HIGHEST_HZ:
            return hz
    raise ValueError(f"the converter's oscillator must be from 1 Hz to {HIGHEST_HZ / 1e9:g} GHz")


def mhz(hz: float) -> str:
    """``hz`` in MHz to the hertz, without the zeros at the end: 9750, 10489.5."""
    return f"{hz / 1e6:.6f}".rstrip("0").rstrip(".")
