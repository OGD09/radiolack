"""Band exploration with overlapping FFT windows and confirmed hardware moves.

The receiver never sees the whole band at once. Full-band mode walks a plan
of overlapping windows; each move is confirmed from the actual rtl_tcp
center observed in the I/Q stream (moving only Gqrx's cursor is not enough).
After a ten-second listen the explorer advances to the next window even if
the carrier is still present, and wraps to the first window at the end.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .detection import DC_EXCLUSION_HZ, USABLE_FRACTION, ChannelGrid
from .errors import GqrxError

# First/last window centers stay this fraction of the rate inside the band.
EDGE_FRACTION = 0.1
# Quarter-rate spacing leaves overlap even when Gqrx offsets the hardware center.
STEP_FRACTION = 0.25
# Gqrx may place the hardware center up to this fraction away from the focus.
RECEIVER_OFFSET_FRACTION = 0.15
# A move is confirmed when the observed center lands this close to the focus.
CENTER_TOLERANCE_FRACTION = 0.18
CONFIRM_TIMEOUT_S = 6.0
SETTLE_S = 0.4


def window_centers(sample_rate: int, low_hz: int, high_hz: int) -> list[int]:
    """Overlapping focus frequencies covering band edges and the DC exclusion."""
    if sample_rate <= 0 or low_hz >= high_hz:
        raise ValueError("Invalid sample rate or band range")
    first = low_hz + round(sample_rate * EDGE_FRACTION)
    last = high_hz - round(sample_rate * EDGE_FRACTION)
    if last < first:
        # Narrow band: two windows so that each one's DC gap is seen by the other.
        middle = (low_hz + high_hz) // 2
        half_step = round(sample_rate * STEP_FRACTION / 2)
        return [middle - half_step, middle + half_step]
    step = max(1, round(sample_rate * STEP_FRACTION))
    centers = list(range(first, last + 1, step))
    if not centers or centers[-1] != last:
        centers.append(last)
    return centers


def uncovered_channels(grid: ChannelGrid, sample_rate: int, centers: list[int],
                       offsets=(-RECEIVER_OFFSET_FRACTION, 0.0, RECEIVER_OFFSET_FRACTION)) -> list[int]:
    """Grid channels outside every window's usable region for some receiver offset."""
    channels = np.asarray(grid.channels(), dtype=np.int64)
    missing = set()
    for offset in offsets:
        shifted = np.asarray([center + round(sample_rate * offset) for center in centers], dtype=np.int64)
        distance = np.abs(channels[:, None] - shifted[None, :])
        covered = ((distance > DC_EXCLUSION_HZ) & (distance < sample_rate * USABLE_FRACTION)).any(axis=1)
        missing.update(int(channel) for channel in channels[~covered])
    return sorted(missing)


def usable_span(center: int, sample_rate: int, grid: ChannelGrid) -> tuple[int, int]:
    half = round(sample_rate * USABLE_FRACTION)
    return max(grid.low_hz, center - half), min(grid.high_hz, center + half)


@dataclass
class PendingWindow:
    focus: int
    old_epoch: int
    requested_at: float
    index: int


class BandExplorer:
    """Full-band exploration state; the monitor drives the actual Gqrx commands."""

    def __init__(self, grid: ChannelGrid, window_seconds: float, *,
                 confirm_timeout_s: float = CONFIRM_TIMEOUT_S, settle_s: float = SETTLE_S):
        self.grid = grid
        self.window_seconds = window_seconds
        self.confirm_timeout_s = confirm_timeout_s
        self.settle_s = settle_s
        self.sample_rate = None
        self.centers: list[int] = []
        self.next_index = 0
        self.current_index = None
        self.cycle = 0
        self.pending: PendingWindow | None = None
        self.deadline = 0.0
        self.advance = True

    def sync_rate(self, sample_rate: int):
        """Re-plan when the actual sample rate changes."""
        if sample_rate != self.sample_rate:
            self.sample_rate = sample_rate
            self.centers = window_centers(sample_rate, self.grid.low_hz, self.grid.high_hz)
            self.next_index = 0
            self.current_index = None
            self.advance = True
            self.pending = None

    def reset(self):
        """Gqrx input disconnected: forget any unconfirmed move, move again on reconnect."""
        self.pending = None
        self.advance = True

    def check_timeout(self, now: float):
        if self.pending is not None and now - self.pending.requested_at > self.confirm_timeout_s:
            raise GqrxError("Gqrx did not move the receiver to the requested spectrum window within "
                            f"{self.confirm_timeout_s:g} s. Keep Gqrx's input on the RadioLack bridge, "
                            "do not tune manually in full-band mode, or use the fixed-window mode.")

    def due(self, now: float) -> bool:
        return self.pending is None and (self.advance or now >= self.deadline)

    def next_focus(self) -> int:
        return self.centers[self.next_index]

    def mark_requested(self, epoch: int, now: float):
        """Record a move that Gqrx accepted; confirmation comes from the I/Q stream."""
        self.pending = PendingWindow(self.centers[self.next_index], epoch, now, self.next_index)
        if self.next_index == 0:
            self.cycle += 1
        self.next_index = (self.next_index + 1) % len(self.centers)
        self.advance = False

    def confirm(self, frame, epoch: int, now: float) -> bool:
        """True once fresh samples come from a hardware center near the focus."""
        pending = self.pending
        if (epoch != pending.old_epoch and frame.received_at >= pending.requested_at + self.settle_s
                and abs(frame.center - pending.focus) <= frame.sample_rate * CENTER_TOLERANCE_FRACTION):
            self.pending = None
            self.current_index = pending.index
            self.deadline = now + self.window_seconds
            return True
        return False

    def after_listen(self):
        self.advance = True

    def scan_info(self) -> dict:
        return {"mode": "full_band", "cycle": self.cycle,
                "window_number": None if self.current_index is None else self.current_index + 1,
                "window_count": len(self.centers),
                "window_observation_seconds": self.window_seconds}


FIXED_WINDOW_SCAN_INFO = {"mode": "fixed_window", "cycle": None, "window_number": None,
                          "window_count": None, "window_observation_seconds": None}
