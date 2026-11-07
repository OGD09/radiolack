"""Listen validation criteria and telemetry (ported from the prototype, plus v2 fields)."""

from datetime import datetime, timezone
import unittest

from radiolack.detection import Peak
from radiolack.listening import ListeningWindow
from radiolack.squelch import ACTIVE, QUIET, TRANSITION
from radiolack.stats import measurement_summary


def fixed_clock():
    return datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)


class ListeningTests(unittest.TestCase):
    def test_telemetry_summaries_keep_fft_meter_and_squelch_separate(self):
        window = ListeningWindow(131500000, 1, 0, clock_utc=fixed_clock)
        peak = Peak(131500000, -30, -60, 131500200)
        for index in range(1, 101):
            window.observe([peak], index / 10)
        window.observe_meter(-40, -50, ACTIVE)
        window.observe_meter(-60, -50, QUIET)
        window.observe_meter(-45, -50, TRANSITION)
        window.gain_stages_db = {"LNA": 25.3}
        result = window.result(10.2, 131000000, 2400000, 15, fft_guard_seconds=1.5)
        self.assertEqual(result["fft_measurements"]["signal_bin_dbfs"]["median"], -30)
        self.assertEqual(result["fft_measurements"]["peak_bin_offset_from_channel_hz"]["median"], 200)
        gqrx = result["gqrx_measurements"]
        self.assertEqual(gqrx["meter_dbfs"]["median"], -45)
        self.assertEqual(gqrx["meter_above_squelch_fraction"], 0.6667)
        self.assertEqual(gqrx["meter_dbfs_fft_active_settled"]["median"], -40)
        self.assertEqual(gqrx["meter_dbfs_fft_quiet_settled"]["median"], -60)
        self.assertEqual(gqrx["meter_samples_unsettled"], 1)
        self.assertEqual(gqrx["meter_above_squelch_fraction_fft_active_settled"], 1.0)
        self.assertEqual(gqrx["gain_stages_db"]["LNA"], 25.3)
        self.assertEqual(result["actual_elapsed_seconds"], 10.2)
        self.assertEqual(result["observed_signal_bursts"], 1)
        self.assertGreater(result["longest_active_run_seconds"], 9)
        self.assertEqual(result["telemetry_version"], 2)
        self.assertEqual(result["listening_seconds"], 10)
        self.assertEqual(result["minimum_observed_seconds"], 5)
        self.assertEqual(result["confirmed_at_utc"], "2026-11-07T08:00:00+00:00")

    def test_missing_or_invalid_telemetry_is_not_zero_filled(self):
        self.assertIsNone(measurement_summary([]))
        with self.assertRaises(ValueError):
            measurement_summary([float("nan")])
        window = ListeningWindow(131500000, 1, 0)
        with self.assertRaises(ValueError):
            window.observe_meter(-50, float("nan"))
        self.assertEqual(window.meter_levels, [])
        for index in range(1, 101):
            window.observe([Peak(131500000, -30, -60)], index / 10)
        gqrx = window.result(10, 131000000, 2400000, 15)["gqrx_measurements"]
        self.assertIsNone(gqrx["meter_dbfs"])
        self.assertIsNone(gqrx["meter_above_squelch_fraction"])
        self.assertIsNone(gqrx["meter_dbfs_fft_active_settled"])

    def test_only_completed_conclusive_listen_returns_event(self):
        window = ListeningWindow(131500000, 1, 0)
        peak = Peak(131500000, -35, -60)
        for index in range(1, 101):
            window.observe([peak] if index <= 20 else [], index / 10)
        with self.assertRaises(ValueError):
            window.result(9.9, 131000000, 2400000, 8)
        event = window.result(10, 131000000, 2400000, 8)
        self.assertEqual(event["frequency_hz"], 131500000)
        self.assertGreater(event["active_seconds"], 1)

    def test_brief_or_sparse_activity_is_not_conclusive(self):
        for sparse in [False, True]:
            window = ListeningWindow(131500000, 1, 0)
            peak = Peak(131500000, -35, -60)
            times = [1, 5, 9] if sparse else [i / 10 for i in range(1, 101)]
            for when in times:
                window.observe([peak] if sparse or when < 0.5 else [], when)
            self.assertIsNone(window.result(10, 131000000, 2400000, 8))

    def test_stream_gaps_earn_little_credit_and_fail_coverage(self):
        window = ListeningWindow(131500000, 1, 0)
        peak = Peak(131500000, -35, -60)
        for when in [0.1, 0.2, 0.3, 4.0, 4.1, 9.0, 9.1, 9.2]:
            window.observe([peak], when)
        self.assertLess(window.observed_seconds, 5)
        self.assertIsNone(window.result(10, 131000000, 2400000, 15))

    def test_other_frequency_cannot_validate_listen(self):
        window = ListeningWindow(131500000, 1, 0)
        for index in range(1, 101):
            window.observe([Peak(131700000, -30, -60)], index / 10)
        self.assertIsNone(window.result(10, 131000000, 2400000, 8))

    def test_late_or_out_of_order_frames_are_ignored(self):
        window = ListeningWindow(131500000, 1, 0)
        window.observe([Peak(131500000, -30, -60)], 0.5)
        window.observe([Peak(131500000, -30, -60)], 0.4)
        window.observe([Peak(131500000, -30, -60)], 10.5)
        self.assertEqual(window.observation_count, 1)

    def test_squelch_changes_are_recorded_relative_to_start(self):
        window = ListeningWindow(131500000, 1, 100.0)
        window.record_squelch_change(102.25, -49.0, -49.0, "carrier_guard")
        self.assertEqual(window.squelch_changes, [{"offset_seconds": 2.25, "requested_dbfs": -49.0,
                                                   "readback_dbfs": -49.0, "reason": "carrier_guard"}])


if __name__ == "__main__":
    unittest.main()
