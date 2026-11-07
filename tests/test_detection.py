"""FFT peak detection on synthetic I/Q (ported from the prototype, plus grid tests)."""

from fractions import Fraction
import unittest

from radiolack.detection import ChannelGrid, compute_spectrum, extract_peaks, find_peaks

from tests.helpers import GRID, synthetic


class PeakDetectionTests(unittest.TestCase):
    def test_two_carriers_are_found_in_one_frame_without_retuning(self):
        peaks = find_peaks(synthetic([(131500000, 0.12), (131700000, 0.08)]), 131000000, 2400000, GRID)
        self.assertEqual({p.frequency_hz for p in peaks}, {131500000, 131700000})
        self.assertTrue(all(p.margin_db >= 8 for p in peaks))
        self.assertEqual(peaks[0].frequency_hz, 131500000, "sorted by decreasing margin")

    def test_noise_alone_is_not_a_candidate(self):
        self.assertEqual(find_peaks(synthetic(), 131000000, 2400000, GRID), [])

    def test_small_peak_is_rejected_by_the_default_margin(self):
        raw = synthetic([(131500000, 0.001)])
        old = find_peaks(raw, 131000000, 2400000, GRID, margin_db=8)
        self.assertTrue(any(peak.frequency_hz == 131500000 for peak in old))
        self.assertEqual(find_peaks(raw, 131000000, 2400000, GRID), [])

    def test_dc_spike_and_outer_edge_are_ignored(self):
        raw = synthetic([(131000000, 0.1), (132100000, 0.1), (131500000, 0.1)])
        peaks = find_peaks(raw, 131000000, 2400000, GRID)
        self.assertEqual({p.frequency_hz for p in peaks}, {131500000})

    def test_outside_band_is_not_reported(self):
        raw = synthetic([(137200000, 0.1)], center=136500000)
        self.assertEqual(find_peaks(raw, 136500000, 2400000, GRID), [])

    def test_carrier_offset_is_snapped_to_real_grid_carrier(self):
        peaks = find_peaks(synthetic([(131500900, 0.1)]), 131000000, 2400000, GRID)
        self.assertEqual(peaks[0].frequency_hz, 131500000)

    def test_invalid_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            find_peaks(b"bad", 131000000, 2400000, GRID)

    def test_relaxed_margin_reuses_one_spectrum(self):
        spectrum = compute_spectrum(synthetic([(131500000, 0.001)]), 131000000, 2400000)
        strict = extract_peaks(spectrum, GRID, 15)
        relaxed = extract_peaks(spectrum, GRID, 6)
        self.assertEqual(strict, [])
        self.assertIn(131500000, {peak.frequency_hz for peak in relaxed})
        with self.assertRaises(ValueError):
            extract_peaks(spectrum, GRID, float("nan"))


class ChannelGridTests(unittest.TestCase):
    def test_historical_grid_matches_prototype_snapping(self):
        for frequency in (118000000.4, 118008300.0, 121512345.6, 136991000.0):
            legacy = 118000000 + round(round((frequency - 118000000) * 3 / 25000) * 25000 / 3)
            self.assertEqual(GRID.nearest(frequency), legacy)
        channels = GRID.channels()
        self.assertEqual(len(channels), 2280)
        self.assertEqual(channels[:3], [118000000, 118008333, 118016667])
        self.assertEqual(channels[-1], 136991667)

    def test_25_khz_grid_is_configurable(self):
        grid = ChannelGrid(144000000, 148000000, 144000000, Fraction(25000), 3000)
        self.assertIsNone(grid.snap(146520900), "4.1 kHz from the nearest carrier")
        self.assertEqual(grid.snap(146522000), 146525000)
        self.assertIsNone(grid.snap(148000000))
        self.assertEqual(len(grid.channels()), 160)

    def test_invalid_grids_are_rejected(self):
        with self.assertRaises(ValueError):
            ChannelGrid(137000000, 118000000, 118000000, Fraction(25000), 3000)
        with self.assertRaises(ValueError):
            ChannelGrid(118000000, 137000000, 118000000, Fraction(25000), 12500)


if __name__ == "__main__":
    unittest.main()
