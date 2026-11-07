"""Shared test helpers: synthetic I/Q, the historical profile and legacy journal rows."""

import numpy as np

from radiolack.detection import FRAME_BYTES
from radiolack.profiles import load_profile

AIRBAND = load_profile("airband-8k33")
GRID = AIRBAND.grid
CONTEXT = {"sample_rate_hz": 2400000, "filter_hz": 8000, "mode": "AM",
           "receiver_controls": [[3, 1], [4, 253], [8, 0], [9, 0], [10, 0], [14, 0]]}


def synthetic(tones=(), center=131000000, rate=2400000, seed=42):
    """Deterministic unsigned 8-bit I/Q with independent noise and carrier tones."""
    rng = np.random.default_rng(seed)
    count = FRAME_BYTES // 2
    signal = rng.normal(0, 0.015, count) + 1j * rng.normal(0, 0.015, count)
    for frequency, amplitude in tones:
        signal += amplitude * np.exp(2j * np.pi * (frequency - center) * np.arange(count) / rate)
    iq = np.column_stack([signal.real, signal.imag]) * 128 + 127.5
    return np.clip(iq, 0, 255).astype(np.uint8).tobytes()


def legacy_journal():
    """Three generations of prototype rows, mirroring the migrated history's shapes."""
    first = {"listening_started_at_utc": "2026-11-07T08:14:44.740921+00:00",
             "confirmed_at_utc": "2026-11-07T08:14:54.844564+00:00",
             "frequency_hz": 136575000, "frequency_mhz": 136.575, "mode": "AM", "filter_hz": 8000,
             "listening_seconds": 10, "active_seconds": 9.883, "observed_seconds": 9.938,
             "median_margin_db": 10.1, "peak_margin_db": 12.2, "required_margin_db": 8,
             "minimum_active_seconds": 1, "receiver_center_hz": 136500000, "sample_rate_hz": 2400000}
    second = dict(first, listening_started_at_utc="2026-11-07T08:31:20.373591+00:00",
                  confirmed_at_utc="2026-11-07T08:31:30.599166+00:00", frequency_hz=135925000,
                  frequency_mhz=135.925, required_margin_db=15, squelch_dbfs=-57.8,
                  squelch_source="noise", gqrx_noise_floor_dbfs=-60.8)
    summary = {"samples": 10, "min": -50.0, "p10": -49.0, "median": -45.0, "mean": -45.5,
               "p90": -44.0, "max": -43.0, "stddev": 1.0}
    third = dict(second, listening_started_at_utc="2026-11-07T09:36:46.613193+00:00",
                 confirmed_at_utc="2026-11-07T09:36:56.622886+00:00", telemetry_version=1,
                 fft_measurements={"signal_bin_dbfs": summary, "fft_size": 8192},
                 gqrx_measurements={"meter_dbfs": dict(summary, mean=-48.0, max=-40.0, samples=5)},
                 scan={"mode": "full_airband", "cycle": 1, "window_number": 4, "window_count": 32,
                       "window_observation_seconds": 1.0},
                 receiver_context=CONTEXT,
                 quality_flags={"limited_fft_coverage": False, "provisional_squelch": False})
    stations = {"135925000": {"squelch_dbfs": -57.8, "noise_floor_dbfs": -60.8, "source": "noise",
                              "receiver_context": CONTEXT, "updated_at_utc": "2026-11-07T08:31:00+00:00",
                              "last_verified_squelch_dbfs": -57.8,
                              "last_verified_at_utc": "2026-11-07T09:36:46+00:00"}}
    return {"schema_version": 1, "detector": "fft-peaks", "detections": [first, second, third],
            "stations": stations,
            "frequency_stats": {"135925000": {"validated_detection_count": 2},
                                "136575000": {"validated_detection_count": 1}}}
