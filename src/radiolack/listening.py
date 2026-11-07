"""Ten-second listen validation and per-listen telemetry.

A listen produces an observation only when it completed its full duration,
had enough confirmed RF presence and enough fresh FFT coverage. A validation
means "a radio signal was present", not "intelligible speech was heard".
"""

from __future__ import annotations

from datetime import datetime, timezone
import math

import numpy as np

from .detection import FFT_AVERAGES, FFT_SIZE, WINDOW_NAME
from .squelch import ACTIVE, QUIET, TRANSITION
from .stats import measurement_summary

TELEMETRY_VERSION = 2
# Cap credit for dropped frames: a stale sample cannot prove that a carrier
# persisted during a long period with no received data.
MAX_CREDIT_PER_OBSERVATION_S = 0.25
LIMITED_COVERAGE_FRACTION = 0.8


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(value):
    """Store whole-number floats as integers (10.0 -> 10) for readable JSON."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


class ListeningWindow:
    """Validate persistence during a fixed listening period and collect telemetry."""

    def __init__(self, frequency, epoch, started, *, listen_seconds=10.0, min_active=1.0,
                 min_observed=5.0, mode="AM", filter_hz=8000, clock_utc=utc_now):
        self.frequency = frequency
        self.epoch = epoch
        self.started = started
        self.listen_seconds = listen_seconds
        self.deadline = started + listen_seconds
        self.min_active = min_active
        self.min_observed = min_observed
        self.mode = mode
        self.filter_hz = filter_hz
        self.clock_utc = clock_utc
        self.last_at = started
        self.previous_active = False
        self.active_seconds = 0.0
        self.observed_seconds = 0.0
        self.margins = []
        self.powers = []
        self.noise_levels = []
        self.bin_offsets = []
        self.meter_levels = []
        self.squelch_levels = []
        self.meter_states = []
        self.invalid_meter_readings = 0
        self.squelch_changes = []
        self.observation_count = 0
        self.signal_bursts = 0
        self.active_run = 0.0
        self.longest_active_run = 0.0
        self.max_observation_gap = 0.0
        self.gain_stages_db = {}
        self.scan_info = {}
        self.started_at_utc = clock_utc().isoformat()

    def observe(self, peaks, received_at):
        if received_at <= self.last_at or received_at > self.deadline:
            return
        gap = received_at - self.last_at
        self.max_observation_gap = max(self.max_observation_gap, gap)
        self.observation_count += 1
        elapsed = min(MAX_CREDIT_PER_OBSERVATION_S, gap)
        current = next((peak for peak in peaks if peak.frequency_hz == self.frequency), None)
        self.observed_seconds += elapsed
        if current is not None:
            self.margins.append(current.margin_db)
            self.powers.append(current.power_dbfs)
            self.noise_levels.append(current.noise_dbfs)
            if current.bin_frequency_hz is not None:
                self.bin_offsets.append(current.bin_frequency_hz - self.frequency)
            if self.previous_active:
                self.active_seconds += elapsed
                self.active_run += elapsed
            else:
                self.signal_bursts += 1
                self.active_run = 0.0
            self.longest_active_run = max(self.longest_active_run, self.active_run)
        else:
            self.active_run = 0.0
        self.previous_active = current is not None
        self.last_at = received_at

    def observe_meter(self, level, squelch, fft_state=TRANSITION):
        """Keep Gqrx meter and read-back squelch separate from FFT-bin power."""
        if not all(math.isfinite(value) and -150 <= value <= 0 for value in (level, squelch)):
            raise ValueError("Invalid Gqrx telemetry")
        self.meter_levels.append(level)
        self.squelch_levels.append(squelch)
        self.meter_states.append(fft_state)

    def record_squelch_change(self, at, requested, readback, reason):
        self.squelch_changes.append({"offset_seconds": round(at - self.started, 3),
                                     "requested_dbfs": requested, "readback_dbfs": readback,
                                     "reason": reason})

    def _meter_by_state(self, state):
        return [level for level, item in zip(self.meter_levels, self.meter_states) if item == state]

    def result(self, now, center, sample_rate, threshold, *, fft_guard_seconds=None):
        """Return an observation only for a completed, conclusive listen."""
        if now < self.deadline:
            raise ValueError("Listening period has not finished")
        if self.active_seconds < self.min_active or self.observed_seconds < self.min_observed:
            return None
        active_pairs = [(level, sql) for level, sql, state
                        in zip(self.meter_levels, self.squelch_levels, self.meter_states) if state == ACTIVE]
        return {
            "listening_started_at_utc": self.started_at_utc,
            "confirmed_at_utc": self.clock_utc().isoformat(),
            "frequency_hz": self.frequency,
            "frequency_mhz": round(self.frequency / 1000000, 6),
            "mode": self.mode, "filter_hz": self.filter_hz,
            "listening_seconds": _plain(self.listen_seconds),
            "active_seconds": round(self.active_seconds, 3),
            "observed_seconds": round(self.observed_seconds, 3),
            "median_margin_db": round(float(np.median(self.margins)), 1),
            "peak_margin_db": round(max(self.margins), 1),
            "required_margin_db": _plain(threshold),
            "minimum_active_seconds": _plain(self.min_active),
            "minimum_observed_seconds": _plain(self.min_observed),
            "receiver_center_hz": center,
            "sample_rate_hz": sample_rate,
            "telemetry_version": TELEMETRY_VERSION,
            "actual_elapsed_seconds": round(now - self.started, 3),
            "coverage_fraction": round(min(1, self.observed_seconds / self.listen_seconds), 4),
            "active_fraction_of_observed": round(self.active_seconds / self.observed_seconds, 4),
            "fft_observations": self.observation_count,
            "signal_peak_observations": len(self.margins),
            "observed_signal_bursts": self.signal_bursts,
            "longest_active_run_seconds": round(self.longest_active_run, 3),
            "max_observation_gap_seconds": round(self.max_observation_gap, 3),
            "fft_measurements": {
                "signal_bin_dbfs": measurement_summary(self.powers),
                "local_noise_bin_dbfs": measurement_summary(self.noise_levels),
                "margin_db": measurement_summary(self.margins),
                "peak_bin_offset_from_channel_hz": measurement_summary(self.bin_offsets),
                "fft_size": FFT_SIZE, "averages": FFT_AVERAGES, "window": WINDOW_NAME,
                "resolution_hz": round(sample_rate / FFT_SIZE, 3),
            },
            "gqrx_measurements": {
                "meter_dbfs": measurement_summary(self.meter_levels),
                "readback_squelch_dbfs": measurement_summary(self.squelch_levels),
                "meter_above_squelch_fraction": (
                    round(sum(level >= sql for level, sql in zip(self.meter_levels, self.squelch_levels))
                          / len(self.meter_levels), 4) if self.meter_levels else None),
                "gain_stages_db": self.gain_stages_db,
                "fft_state_guard_seconds": fft_guard_seconds,
                "meter_dbfs_fft_active_settled": measurement_summary(self._meter_by_state(ACTIVE)),
                "meter_dbfs_fft_quiet_settled": measurement_summary(self._meter_by_state(QUIET)),
                "meter_samples_unsettled": sum(state not in (ACTIVE, QUIET) for state in self.meter_states),
                "meter_above_squelch_fraction_fft_active_settled": (
                    round(sum(level >= sql for level, sql in active_pairs) / len(active_pairs), 4)
                    if active_pairs else None),
                "invalid_meter_readings": self.invalid_meter_readings,
            },
            "squelch_changes": list(self.squelch_changes),
            "scan": dict(self.scan_info),
        }
