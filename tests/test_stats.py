"""Per-frequency statistics and global summaries over every history generation."""

import unittest

from radiolack.stats import frequency_statistics, parse_utc, store_summary

from tests.helpers import legacy_journal


def summary(mean, maximum, samples):
    return {"samples": samples, "min": maximum - 10, "p10": mean, "median": mean, "mean": mean,
            "p90": maximum, "max": maximum, "stddev": 1.0}


class FrequencyStatisticsTests(unittest.TestCase):
    def test_repeated_hits_and_separate_frequencies(self):
        stats = frequency_statistics([
            {"frequency_hz": 131500000, "confirmed_at_utc": "2026-11-07T08:00:00+00:00"},
            {"frequency_hz": 136925000, "confirmed_at_utc": "2026-11-07T08:01:00+00:00"},
            {"frequency_hz": 131500000, "confirmed_at_utc": "2026-11-07T08:02:00+00:00"}])
        self.assertEqual(list(stats), ["131500000", "136925000"])
        self.assertEqual(stats["131500000"]["validated_detection_count"], 2)
        self.assertEqual(stats["136925000"]["validated_detection_count"], 1)
        self.assertEqual(stats["131500000"]["first_confirmed_at_utc"], "2026-11-07T08:00:00+00:00")
        self.assertEqual(stats["131500000"]["last_confirmed_at_utc"], "2026-11-07T08:02:00+00:00")

    def test_matched_durations_utc_hours_and_unknown_values(self):
        events = legacy_journal()["detections"] + [
            {"frequency_hz": 135925000, "confirmed_at_utc": "2026-11-07T23:59:59Z", "observed_seconds": 4.0}]
        stats = frequency_statistics(events)
        busy, single = stats["135925000"], stats["136575000"]
        self.assertEqual(busy["validated_detection_count"], 3)
        self.assertEqual(busy["confirmations_by_utc_hour"], {"08": 1, "09": 1, "23": 1})
        self.assertEqual(busy["first_confirmed_at_utc"], "2026-11-07T08:31:30.599166+00:00")
        self.assertEqual(busy["last_confirmed_at_utc"], "2026-11-07T23:59:59Z")
        self.assertEqual((busy["total_observed_seconds"], busy["observed_seconds_supporting_detections"]),
                         (23.876, 3))
        self.assertEqual((busy["total_active_seconds"], busy["active_seconds_supporting_detections"]),
                         (19.766, 2))
        # The row without an active duration does not dilute the ratio.
        self.assertEqual(busy["active_fraction_of_observed"], round(19.766 / 19.876, 4))
        self.assertEqual(busy["last_validated_squelch_dbfs"], -57.8)
        self.assertEqual(busy["telemetry_detections"], 1)
        self.assertEqual(busy["validations_by_required_margin_db"], {"15": 2})
        self.assertEqual((busy["mean_signal_bin_dbfs"], busy["max_signal_bin_dbfs"]), (-45.5, -43.0))
        self.assertEqual((busy["mean_gqrx_meter_dbfs"], busy["max_gqrx_meter_dbfs"]), (-48.0, -40.0))
        self.assertEqual((busy["signal_bin_supporting_detections"], busy["gqrx_meter_supporting_detections"]),
                         (1, 1))
        self.assertEqual(single["validations_by_required_margin_db"], {"8": 1})
        for name in ("mean_signal_bin_dbfs", "max_signal_bin_dbfs", "mean_gqrx_meter_dbfs",
                     "last_validated_squelch_dbfs"):
            self.assertIsNone(single[name], name)
        self.assertEqual((single["telemetry_detections"], single["signal_bin_supporting_detections"]), (0, 0))

    def test_levels_are_weighted_by_their_sample_counts(self):
        stats = frequency_statistics([
            {"frequency_hz": 131500000, "fft_measurements": {"signal_bin_dbfs": summary(-40.0, -35.0, 10)}},
            {"frequency_hz": 131500000, "fft_measurements": {"signal_bin_dbfs": summary(-50.0, -45.0, 30)}},
            {"frequency_hz": 131500000, "fft_measurements": {"signal_bin_dbfs": None}}])
        item = stats["131500000"]
        self.assertEqual((item["mean_signal_bin_dbfs"], item["max_signal_bin_dbfs"]), (-47.5, -35.0))
        self.assertEqual(item["signal_bin_supporting_detections"], 2)

    def test_unparsable_timestamps_are_counted_not_guessed(self):
        stats = frequency_statistics([{"frequency_hz": 131500000, "confirmed_at_utc": "yesterday"},
                                      {"frequency_hz": 131500000, "confirmed_at_utc": "2026-11-07T08:00:00"},
                                      {"frequency_hz": 131500000}])
        item = stats["131500000"]
        self.assertEqual((item["validated_detection_count"], item["unparsed_timestamps"]), (3, 2))
        self.assertIsNone(item["first_confirmed_at_utc"])
        self.assertEqual(item["confirmations_by_utc_hour"], {})
        self.assertIsNone(item["total_observed_seconds"])
        self.assertIsNone(item["active_fraction_of_observed"])

    def test_timestamps_are_converted_to_utc(self):
        self.assertEqual(parse_utc("2026-11-07T04:00:00-04:00").hour, 8)
        stats = frequency_statistics([{"frequency_hz": 1, "confirmed_at_utc": "2026-11-07T04:00:00-04:00"}])
        self.assertEqual(stats["1"]["confirmations_by_utc_hour"], {"08": 1})

    def test_invalid_rows_are_rejected(self):
        for bad in ({"frequency_hz": "131.5"}, {"frequency_hz": 0}, {"frequency_hz": 1, "confirmed_at_utc": 5}, []):
            with self.assertRaises(ValueError):
                frequency_statistics([bad])


class StoreSummaryTests(unittest.TestCase):
    def test_summary_counts_generations_origins_and_period(self):
        rows = [dict(row, origin={"kind": "legacy-import"}) for row in legacy_journal()["detections"]]
        rows.append({"frequency_hz": 131500000, "origin": {"kind": "radiolack"}, "telemetry_version": 2,
                     "required_margin_db": 15, "confirmed_at_utc": "2026-11-08T01:00:00+00:00"})
        result = store_summary(rows, legacy_journal()["stations"])
        self.assertEqual(result["validated_listens"], 4)
        self.assertEqual(result["distinct_frequencies"], 3)
        self.assertEqual(result["stations_with_squelch_memory"], 1)
        self.assertEqual(result["by_origin"], {"legacy-import": 3, "radiolack": 1})
        self.assertEqual(result["by_telemetry_version"], {"1": 1, "2": 1, "none": 2})
        self.assertEqual(result["by_required_margin_db"], {"8": 1, "15": 3})
        self.assertEqual(result["first_confirmed_at_utc"], "2026-11-07T08:14:54.844564+00:00")
        self.assertEqual(result["last_confirmed_at_utc"], "2026-11-08T01:00:00+00:00")

    def test_empty_history(self):
        result = store_summary([], {})
        self.assertEqual((result["validated_listens"], result["first_confirmed_at_utc"]), (0, None))


if __name__ == "__main__":
    unittest.main()
