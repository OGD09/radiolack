"""The frequency correction: how far the receiver's crystal is off, in
hundredths of parts per million, and how it is split between the receiver
and Radiolack.

A receiver that takes a correction itself, such as an RTL-SDR through
librtlsdr, takes whole ppm only: it gets the nearest whole number, and
Radiolack corrects the rest by scaling the frequency it asks for. A source
without a correction of its own corrects the whole of it that way. Either
way, the samples keep the frequency asked as their label.

Why the scale is (1 + whole / 10⁶) / (1 + ppm / 10⁶): a receiver derives
every frequency it tunes from its crystal. librtlsdr takes a correction c
to mean that the crystal runs at its nominal frequency X times
(1 + c / 10⁶), and programs the tuner's PLL, or the RTL2832's mixer while
sampling directly, for a request R as if it did. A crystal that truly runs
at X × (1 + ppm / 10⁶) then gives R × (1 + ppm / 10⁶) / (1 + c / 10⁶), as
every frequency scales with it. With c the whole part w, asking for
R = F × (1 + w / 10⁶) / (1 + ppm / 10⁶) puts the receiver on F. A crystal
that runs fast (ppm > 0) shows signals too low, and needs a lower request.
"""

from __future__ import annotations

PPM_RANGE = (-100.0, 100.0)     # corrections offered, in ppm
_HUNDREDTHS = 100


def checked_ppm(value: object) -> float:
    """``value`` as a correction, rounded to the hundredth of a ppm, within
    PPM_RANGE; ValueError for anything else, booleans and text included. A
    whole number reads as it is."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            hundredths = round(value * _HUNDREDTHS)
        except (OverflowError, ValueError):     # infinite or not a number
            hundredths = None
        low, high = PPM_RANGE
        if hundredths is not None and low * _HUNDREDTHS <= hundredths <= high * _HUNDREDTHS:
            return hundredths / _HUNDREDTHS
    low, high = PPM_RANGE
    raise ValueError(f"the frequency correction must be a number from {low:g} to {high:g} ppm")


def whole_ppm(ppm: float, hardware: bool) -> int:
    """The part of ``ppm`` that the receiver takes itself: its nearest whole
    number, the halves away from zero, for a receiver with a correction of
    its own (``hardware``); 0 for one without."""
    if not hardware:
        return 0
    hundredths = round(ppm * _HUNDREDTHS)
    whole = (abs(hundredths) + _HUNDREDTHS // 2) // _HUNDREDTHS
    return whole if hundredths >= 0 else -whole


def corrected_hz(tuned_hz: float, ppm: float, whole: int) -> int:
    """The frequency to ask of a receiver that takes ``whole`` ppm itself so
    that it tunes ``tuned_hz`` with a crystal ``ppm`` off, rounded to the
    hertz: ``tuned_hz`` itself when ``whole`` is ``ppm``."""
    if whole == ppm:
        return round(tuned_hz)
    return round(tuned_hz * (1 + whole / 1e6) / (1 + ppm / 1e6))
