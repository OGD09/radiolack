"""The check of the driver's computation of an R820T's or R828D's PLL,
which Calibrate runs (coverage-and-precision.md, pull request 14b).

rtl-sdr's driver programs the tuner's PLL one of two ways (r82xx.py), and
the tuner then sits where that computation puts it, which no setting tells.
The check tells them apart on one steady carrier: it measures the carrier
at the centre in use, then at centre frequencies off the grid, a little
apart, where the two computations put the tuner at least SEPARATION_HZ
apart from what they do at the centre in use. A carrier measured at a
centre frequency moves, in the samples, by the opposite of the tuner's own
move; so its moves from the first measure are compared with those that each
computation predicts, and a computation within TOLERANCE_HZ at every point
names the driver.

Nothing known beforehand enters: the crystal's error moves the carrier by
the same amount at every centre frequency, which the moves leave out; a
wrong correction scales the sample rate, which moves a carrier by its
distance from the centre times that error, at most 0.4 Hz over the
MAX_MOVE_HZ that the points span for corrections and crystals within
100 ppm; and the measures are taken from the label of each centre, which
the engine subtracts, so that the saved computation, which makes the label,
cancels out. The true frequency typed in the dialog is not needed either.

The tuner's intermediate frequency is the sample rate's (r82xx.rate_if_hz),
as Radiolack's commands leave it (rtltcp.py), and the driver's own
correction is 0, as Radiolack sends it for these tuners.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import r82xx

MAX_MOVE_HZ = 2_000         # the centres off the grid lie this close to the centre in use, on the tuner
STEP_HZ = 50                # the tuner's moves tried, in steps of this
SEPARATION_HZ = 40.0        # at each point, the two computations' predictions lie this far apart at least
TOLERANCE_HZ = 5.0          # a computation names the driver when every move is this close to its prediction
MIN_POINTS = 2              # centres off the grid, at the fewest
MAX_POINTS = 3              # and at the most
COMPUTATIONS = tuple(r82xx.COMPUTATIONS)

# Why a check names no computation: the carrier was not measured steadily at
# every point; its moves followed neither computation, or both.
LOST, NEITHER, BOTH = "lost", "neither", "both"


@dataclass(frozen=True)
class Point:
    """A centre frequency of the check: the one asked, on the air, and the
    frequency that the receiver asks of its tuner for it."""

    centre_hz: float
    request_hz: int


@dataclass(frozen=True)
class Identification:
    """What a check found: the computation, or why none (NEITHER or BOTH),
    and for each computation how far each move lay from its prediction."""

    computation: str | None
    reason: str | None
    residuals_hz: dict[str, tuple[float, ...]]


def predicted_move(computation: str, rate_hz: int, reference_hz: int, request_hz: int) -> float:
    """How far a carrier moves in the samples when the tuner, asked for
    ``reference_hz``, is asked for ``request_hz`` at the sample rate
    ``rate_hz`` and computes its PLL by ``computation``: the opposite of
    the tuner's own move. ValueError where the PLL cannot lock."""
    return -(r82xx.tuned_hz(computation, request_hz, rate_hz) - r82xx.tuned_hz(computation, reference_hz, rate_hz))


def separation_hz(rate_hz: int, reference_hz: int, request_hz: int) -> float:
    """How far apart the two computations put a carrier after the move from
    ``reference_hz`` to ``request_hz``. ValueError where the PLL cannot
    lock."""
    first, second = COMPUTATIONS
    return (predicted_move(first, rate_hz, reference_hz, request_hz)
            - predicted_move(second, rate_hz, reference_hz, request_hz))


def choose_points(request_hz: int, rate_hz: int, centre_for: Callable[[int], float | None]) -> tuple[Point, ...]:
    """Up to MAX_POINTS centres off the grid for a tuner asked for
    ``request_hz`` at ``rate_hz``: the nearest, within MAX_MOVE_HZ, where
    the two computations put the carrier SEPARATION_HZ apart at least, the
    second one with the other sign of that difference where one has it, so
    that a carrier that drifts meanwhile cannot follow a prediction at both.
    ``centre_for`` gives the centre asked for a request, or None where the
    receiver cannot ask for it. Fewer than MIN_POINTS: none."""
    candidates: list[tuple[Point, float]] = []
    for distance in range(STEP_HZ, MAX_MOVE_HZ + 1, STEP_HZ):
        for request in (request_hz - distance, request_hz + distance):
            try:
                apart = separation_hz(rate_hz, request_hz, request)
            except ValueError:      # beyond the PLL's reach
                continue
            if abs(apart) < SEPARATION_HZ:
                continue
            centre = centre_for(request)
            if centre is not None:
                candidates.append((Point(centre, request), apart))
    if len(candidates) < MIN_POINTS:
        return ()
    first = candidates.pop(0)
    other = next((candidate for candidate in candidates if (candidate[1] > 0) != (first[1] > 0)), candidates[0])
    candidates.remove(other)
    chosen = [first, other, *candidates[:MAX_POINTS - 2]]
    return tuple(point for point, _ in chosen)


def identify(rate_hz: int, points: Sequence[Point], offsets_hz: Sequence[float]) -> Identification:
    """Which computation the carrier's measures follow: ``offsets_hz``, one
    for each of ``points``, the first being the reference, is where the
    carrier measured at each, on the tuner's frequencies, from the label of
    that centre's samples."""
    if len(points) != len(offsets_hz) or len(points) < MIN_POINTS + 1:
        raise ValueError("a check needs a measure at the reference and at each point off the grid")
    reference = points[0].request_hz
    residuals: dict[str, tuple[float, ...]] = {}
    matching = []
    for computation in COMPUTATIONS:
        residuals[computation] = tuple(
            (offset - offsets_hz[0]) - predicted_move(computation, rate_hz, reference, point.request_hz)
            for point, offset in zip(points[1:], offsets_hz[1:]))
        if all(abs(residual) <= TOLERANCE_HZ for residual in residuals[computation]):
            matching.append(computation)
    if len(matching) == 1:
        return Identification(matching[0], None, residuals)
    return Identification(None, BOTH if matching else NEITHER, residuals)
