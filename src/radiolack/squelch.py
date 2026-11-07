"""Per-frequency squelch learning from Gqrx S-meter readings.

Squelch values come from Gqrx's integrated S-meter, never from FFT-bin
levels. The FFT is only used to classify each meter reading as "carrier
present" or "quiet". Because the FFT taps the shared I/Q stream before Gqrx
receives it, Gqrx's meter lags the FFT: right after a carrier stops, the
meter still shows the carrier. The prototype learned such tails as noise,
then set the squelch above the carrier and muted later visits.

Safeguards:

* readings count as quiet or carrier only after the FFT state has been
  stable for ``fft_guard_s`` (default 1.5 s); readings in between are ignored;
* a noise estimate is rejected when it is less than ``carrier_separation_db``
  below the carrier level measured at this frequency (this visit or stored);
* the requested squelch is kept at least ``carrier_headroom_db`` below that
  carrier level, and an existing memory above its safe target is lowered at
  once (carrier guard): to the stored noise + ``noise_offset_db`` when that
  noise sits at least ``carrier_separation_db`` below the carrier, else to
  the carrier ceiling. Busy channels rarely stay quiet long enough to measure
  noise, so a ceiling alone would mute weaker stations there;
* raising the squelch is limited to ``max_raise_db`` per update, while
  lowering it (the safe direction for reception) is immediate.

Record fields (legacy names kept): ``squelch_dbfs`` is the requested value
applied on the next visit, ``noise_floor_dbfs`` the estimated noise,
``provisional_squelch_dbfs`` the signal-based fallback, and
``last_verified_squelch_dbfs`` the value actually read back from Gqrx.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math

import numpy as np

LEARNING_VERSION = 2
SOURCES = ("noise", "signal_fallback", "carrier_guard")
MIN_DBFS, MAX_DBFS = -150.0, 0.0
SQUELCH_LIMITS_DBFS = (-149.0, -1.0)

# FFT activity states used to classify meter readings.
ACTIVE = "active_settled"
QUIET = "quiet_settled"
TRANSITION = "transition"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class SquelchSettings:
    noise_offset_db: float = 3.0
    deadband_db: float = 1.5
    max_raise_db: float = 2.0
    min_update_interval_s: float = 2.0
    noise_window: int = 6
    min_noise_samples: int = 4
    min_noise_span_s: float = 0.5
    max_noise_range_db: float = 3.0
    fft_guard_s: float = 1.5
    carrier_separation_db: float = 6.0
    carrier_headroom_db: float = 3.0
    min_carrier_samples: int = 3
    min_guard_correction_db: float = 0.5
    relaxed_margin_db: float = 6.0


@dataclass(frozen=True)
class SquelchUpdate:
    requested_dbfs: float
    reason: str
    # Noise level the value derives from; None when it derives from the carrier ceiling.
    noise_dbfs: float | None = None


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_level(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and MIN_DBFS <= value <= MAX_DBFS)


def _clamp(value: float) -> float:
    low, high = SQUELCH_LIMITS_DBFS
    return max(low, min(high, value))


class FftActivity:
    """Carrier presence near the tuned frequency, as seen by the FFT tap."""

    def __init__(self):
        self.active = None
        self.since = None

    def update(self, active: bool, at: float):
        if self.active is None or active != self.active:
            self.active = active
            self.since = at

    def state(self, now: float, guard_s: float) -> str:
        if self.active is None:
            return UNKNOWN
        if now - self.since < guard_s:
            return TRANSITION
        return ACTIVE if self.active else QUIET


class SquelchLearner:
    """Learn a squelch from settled quiet meter readings, never from carrier tails."""

    def __init__(self, context, squelch, saved=None, *, settings: SquelchSettings = SquelchSettings(),
                 clock_utc=iso_utc):
        self.settings = settings
        self.clock_utc = clock_utc
        if saved:
            self.record = dict(saved)
        else:
            stamp = clock_utc()
            self.record = {"squelch_dbfs": squelch, "noise_floor_dbfs": None, "source": "signal_fallback",
                           "receiver_context": context, "updated_at_utc": stamp,
                           "provisional_squelch_dbfs": squelch, "provisional_measured_at_utc": stamp,
                           "learning_version": LEARNING_VERSION}
        self.noise_samples = deque(maxlen=settings.noise_window)
        self.carrier_samples: list[float] = []
        self.last_update = -math.inf
        self.rejected_near_carrier = 0

    def verified(self, actual: float):
        self.record.update(last_verified_squelch_dbfs=actual, last_verified_at_utc=self.clock_utc())

    @property
    def carrier_reference(self) -> float | None:
        """Median settled carrier reading of this visit, else the stored one, else None.

        The median resists the first readings of a carrier when Gqrx's meter
        lags more than the guard (they still show noise).
        """
        if len(self.carrier_samples) >= self.settings.min_carrier_samples:
            return float(np.median(self.carrier_samples))
        stored = self.record.get("carrier_meter_dbfs")
        return float(stored) if valid_level(stored) else None

    def observe(self, level: float, state: str, now: float) -> SquelchUpdate | None:
        """Classify one meter reading; return a new requested squelch, if any."""
        if not valid_level(level):
            raise ValueError("Invalid Gqrx signal level")
        if state == ACTIVE:
            self.noise_samples.clear()
            self.carrier_samples.append(level)
            return self._guard_carrier()
        if state != QUIET:
            # Transition or unknown: the meter may still show the previous state.
            self.noise_samples.clear()
            return None
        self.noise_samples.append((now, level))
        return self._learn_noise(now)

    def _set(self, value: float, source: str, noise: float | None, **fields) -> SquelchUpdate:
        stamp = self.clock_utc()
        self.record.update(squelch_dbfs=value, source=source, updated_at_utc=stamp,
                           learning_version=LEARNING_VERSION, **fields)
        return SquelchUpdate(value, source, noise)

    def _guard_carrier(self) -> SquelchUpdate | None:
        settings = self.settings
        if len(self.carrier_samples) < settings.min_carrier_samples:
            return None
        carrier = self.carrier_reference
        self.record.update(carrier_meter_dbfs=round(carrier, 1), carrier_measured_at_utc=self.clock_utc())
        target = _clamp(carrier - settings.carrier_headroom_db)
        noise = self.record.get("noise_floor_dbfs")
        # The stored noise counts only when clearly below the carrier: prototype
        # estimates learned from carrier tails sit close to or above it.
        if valid_level(noise) and noise <= carrier - settings.carrier_separation_db:
            target = min(target, _clamp(noise + settings.noise_offset_db))
        else:
            noise = None
        target = round(target, 1)
        if self.record["squelch_dbfs"] - target < settings.min_guard_correction_db:
            return None
        return self._set(target, "carrier_guard", noise)

    def _learn_noise(self, now: float) -> SquelchUpdate | None:
        settings = self.settings
        samples = self.noise_samples
        if (len(samples) < settings.min_noise_samples
                or samples[-1][0] - samples[0][0] < settings.min_noise_span_s
                or now - self.last_update < settings.min_update_interval_s):
            return None
        levels = [level for _, level in samples]
        if max(levels) - min(levels) > settings.max_noise_range_db:
            return None
        noise = float(np.median(levels))
        carrier = self.carrier_reference
        if carrier is not None and noise > carrier - settings.carrier_separation_db:
            # Too close to the carrier: most likely a carrier tail, not noise.
            self.rejected_near_carrier += 1
            samples.clear()
            return None
        target = _clamp(noise + settings.noise_offset_db)
        if carrier is not None:
            target = min(target, _clamp(carrier - settings.carrier_headroom_db))
        previous = self.record["squelch_dbfs"]
        if abs(target - previous) < settings.deadband_db:
            return None
        if target > previous:
            target = min(target, previous + settings.max_raise_db)
        self.last_update = now
        noise = round(noise, 1)
        return self._set(round(target, 1), "noise", noise, noise_floor_dbfs=noise,
                         noise_measured_at_utc=self.clock_utc())
