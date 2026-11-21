"""Automatic calibration (later-milestones.md, section 3): the frequency
correction found on the standard-frequency stations that the band plan
marks ``reference``.

The engine runs it on demand, like a scan: it tunes the receiver to each
station within its reach, twelve at most, lowest first, the VFO on the
station in AM, and measures the strongest carrier within WINDOW_PPM of the
station, MAX_WINDOW_HZ at most, as the Calibrate dialog's measure does
(carrier.py): two seconds that agree within a hertz. Each carrier received
implies a correction; the proposal is the median of those that agree
within AGREEMENT_PPM, and needs two stations, or one alone, the only one
received, at SINGLE_MARGIN_DB or more above the noise. This module holds
those rules, apart from the engine: which stations, where the receiver
tunes for each, the correction a carrier implies and the proposal.

Why the window scales with the station: a crystal off by up to 200 ppm,
as cheap receivers' are, moves a station by as much; capped at 2 kHz, the
window never reaches the next station, 4 kHz away at 5, 10 and 15 MHz.

Why the receiver tunes a quarter of the sample rate off the station: the
station then lies clear of the DC residue at the centre and of the span's
edges, where the receiver's filters roll off, as the VFO does by default.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .correction import PPM_RANGE

MAX_STATIONS = 12           # a run measures this many stations at most, lowest first
WINDOW_PPM = 200.0          # the carrier is sought this far from the station, in ppm of its frequency,
MAX_WINDOW_HZ = 2_000.0     # and this far at most
AGREEMENT_PPM = 0.3         # the corrections that agree lie within this of each other
SINGLE_MARGIN_DB = 30.0     # a station received alone proposes a correction this far above the noise
MODE = "AM"                 # the VFO's mode on a station: its filter is centred on the VFO
MIN_WIDTH_HZ = 100.0        # the narrowest filter the VFO takes (demod.BANDWIDTH_RANGE_HZ)
WIDTH_FACTOR = 2.5          # the filter's width, in windows: the window lies well within its passband
DC_CLEARANCE_HZ = 3_000.0   # a station's filter lies this far from the centre in use at least
EDGE_FRACTION = 0.4         # and within this fraction of the sample rate from it, as detection's search

# Why no correction is proposed after a run: no station was received; one
# alone, below SINGLE_MARGIN_DB; stations whose corrections disagree; or a
# proposal beyond the corrections that a receiver takes (PPM_RANGE).
NONE, WEAK, DISAGREE, RANGE = "none", "weak", "disagree", "range"
# Why a station gave no carrier, besides carrier.py's reasons: the receiver
# refused its centre frequency.
REFUSED = "refused"


@dataclass(frozen=True)
class Station:
    """A station of a run: its frequency, on the air, and the centre
    frequency that the receiver asks for it."""

    frequency_hz: int
    centre_hz: float


@dataclass(frozen=True)
class Result:
    """What a run measured at a station: the carrier on the air, its
    margin over the noise and the correction it implies, unrounded; or why
    there is no carrier."""

    frequency_hz: int
    carrier_hz: float | None
    margin_db: float | None
    ppm: float | None
    reason: str | None = None


@dataclass(frozen=True)
class Proposal:
    """The correction proposed, in ppm to the hundredth, with the indices
    of the results that agreed on it; or, without one, why not."""

    ppm: float | None
    agreeing: tuple[int, ...] = ()
    reason: str | None = None


def window_hz(frequency_hz: float) -> float:
    """How far from a station its carrier is sought."""
    return min(frequency_hz * WINDOW_PPM / 1e6, MAX_WINDOW_HZ)


def filter_width_hz(frequency_hz: float) -> float:
    """The VFO's filter width on a station, wide enough for its window."""
    return max(MIN_WIDTH_HZ, round(WIDTH_FACTOR * 2 * window_hz(frequency_hz)))


def stations(references: Iterable[int], rate_hz: float, range_hz: tuple[float, float],
             centre_in_use: Callable[[float], float]) -> tuple[Station, ...]:
    """The stations of a run among ``references``: those the receiver
    reaches, at ``rate_hz`` and with centre frequencies within
    ``range_hz``, lowest first, MAX_STATIONS at most, each with its centre
    (``station_centre``)."""
    found = []
    for frequency_hz in sorted(set(references)):
        centre_hz = station_centre(frequency_hz, rate_hz, range_hz, centre_in_use)
        if centre_hz is not None:
            found.append(Station(frequency_hz, centre_hz))
        if len(found) == MAX_STATIONS:
            break
    return tuple(found)


def station_centre(frequency_hz: int, rate_hz: float, range_hz: tuple[float, float],
                   centre_in_use: Callable[[float], float]) -> float | None:
    """The centre frequency to ask for a station: a quarter of the sample
    rate below it, else above it, whichever the receiver tunes, within
    ``range_hz``, and where the centre in use that ``centre_in_use`` gives
    for it leaves the station's filter clear of the DC residue and of the
    span's edges; None when neither does, or the station itself lies
    outside ``range_hz``."""
    low_hz, high_hz = range_hz
    if not low_hz <= frequency_hz <= high_hz:
        return None
    half_width = filter_width_hz(frequency_hz) / 2
    for centre_hz in (float(round(frequency_hz - rate_hz / 4)), float(round(frequency_hz + rate_hz / 4))):
        if not low_hz <= centre_hz <= high_hz:
            continue
        distance = abs(frequency_hz - centre_in_use(centre_hz))
        if distance - half_width >= DC_CLEARANCE_HZ and distance + half_width <= EDGE_FRACTION * rate_hz:
            return centre_hz
    return None


def implied_ppm(ppm_in_use: float, carrier_hz: float, station_hz: float) -> float:
    """The correction that brings a carrier measured at ``carrier_hz``, with
    the correction ``ppm_in_use``, to the station's ``station_hz``, as the
    Calibrate dialog computes it without a converter (calibration.js,
    calibratedPpm): (1 + p) × T_s / T_m − 1."""
    return ((1 + ppm_in_use / 1e6) * station_hz / carrier_hz - 1) * 1e6


def propose(results: Sequence[Result]) -> Proposal:
    """The correction that ``results`` propose: the median of the largest
    group of corrections that lie within AGREEMENT_PPM of each other, two
    or more; or the correction of the one station received, alone, at
    SINGLE_MARGIN_DB or more. Two largest groups that share no station
    leave no telling which is right: nothing is proposed. Of largest groups
    that overlap, the tightest proposes, the lowest of equally tight ones."""
    received = sorted(((result.ppm, index) for index, result in enumerate(results) if result.ppm is not None))
    if not received:
        return Proposal(None, reason=NONE)
    if len(received) == 1:
        ppm, index = received[0]
        margin = results[index].margin_db
        if margin is None or margin < SINGLE_MARGIN_DB:
            return Proposal(None, reason=WEAK)
        return _proposal([ppm], (index,))
    # Each start's group: the corrections from it up to AGREEMENT_PPM above it.
    groups = []
    for start, (low, _) in enumerate(received):
        stop = start
        while stop + 1 < len(received) and received[stop + 1][0] - low <= AGREEMENT_PPM + 1e-9:
            stop += 1
        groups.append((start, stop + 1))
    size = max(stop - start for start, stop in groups)
    largest = [(start, stop) for start, stop in groups if stop - start == size]
    if size < 2 or largest[-1][0] - largest[0][0] >= size:
        return Proposal(None, reason=DISAGREE)
    start, stop = min(largest, key=lambda group: (received[group[1] - 1][0] - received[group[0]][0], group[0]))
    chosen = received[start:stop]
    return _proposal([ppm for ppm, _ in chosen], tuple(sorted(index for _, index in chosen)))


def _proposal(corrections: Sequence[float], agreeing: tuple[int, ...]) -> Proposal:
    """The median of ``corrections`` to the hundredth, unless a receiver
    takes no such correction."""
    ppm = round(statistics.median(corrections), 2) + 0.0     # no -0.0
    low, high = PPM_RANGE
    if not low <= ppm <= high:
        return Proposal(None, reason=RANGE)
    return Proposal(ppm, agreeing)
