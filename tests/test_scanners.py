"""Tests for detection: signals found above the local noise, away from the
span's edges and centre, the detector's average over one capture, the list
of signals found twice in a row, ignored frequencies, and the checks of the
values the interfaces give."""

import unittest
from unittest import mock

import numpy as np

from radiolack.scanners import (ACTIVE_S, DEFAULT_THRESHOLD_DB, MAX_IGNORED, MERGE_HZ, Detection, Detector, Signals,
                                checked_frequency, checked_threshold, find_signals, local_noise)

SIZE = 8192
RATE_HZ = 2_400_000.0
CENTRE_HZ = 145_000_000.0
BIN_HZ = RATE_HZ / SIZE
NOISE = 1e-6        # -60 dB per bin


def noise(seed: int = 1, level: float = NOISE) -> np.ndarray:
    """Bin powers of noise averaged over ten blocks, as the detector gives them."""
    return np.random.default_rng(seed).exponential(level, (10, SIZE)).mean(axis=0)


def add_signal(power: np.ndarray, bin_index: int, above_db: float, width_bins: int = 3) -> None:
    """A signal ``above_db`` over the noise, flat over ``width_bins`` bins centred on ``bin_index``."""
    low = bin_index - width_bins // 2
    power[low:low + width_bins] += NOISE * 10 ** (above_db / 10)


def frequency_of(bin_index: float) -> float:
    return CENTRE_HZ + (bin_index - SIZE // 2) * BIN_HZ


class FindSignalsTest(unittest.TestCase):
    def test_finds_a_carrier_at_its_frequency_with_its_margin(self):
        power = noise()
        add_signal(power, 5000, 20.0)
        [found] = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertAlmostEqual(found.frequency_hz, frequency_of(5000), delta=BIN_HZ / 2)
        self.assertAlmostEqual(found.margin_db, 20.0, delta=1.5)
        self.assertAlmostEqual(found.width_hz, 3 * BIN_HZ, delta=2 * BIN_HZ)

    def test_noise_alone_and_weaker_signals_are_not_found(self):
        power = noise()
        self.assertEqual(find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])
        add_signal(power, 5000, 8.0)
        self.assertEqual(find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])
        self.assertEqual(len(find_signals(power, CENTRE_HZ, RATE_HZ, 6.0)), 1)

    def test_a_wide_signal_is_one_signal_with_its_width(self):
        power = noise()
        width_bins = round(12_000 / BIN_HZ)
        add_signal(power, 3000, 15.0, width_bins)
        [found] = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertAlmostEqual(found.frequency_hz, frequency_of(3000), delta=BIN_HZ)
        self.assertAlmostEqual(found.width_hz, width_bins * BIN_HZ, delta=2 * BIN_HZ)

    def test_leaves_out_the_span_edges_and_its_centre(self):
        power = noise()
        dc_gap_bins = 3_000 / BIN_HZ
        for index in (100, SIZE - 100, SIZE // 2, round(SIZE // 2 + dc_gap_bins / 2)):
            add_signal(power, index, 30.0)
        self.assertEqual(find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])
        add_signal(power, round(SIZE // 2 + 0.39 * SIZE), 30.0)     # within the usable span
        self.assertEqual(len(find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)), 1)

    def test_the_noise_is_local(self):
        # A receiver's response that slopes by 20 dB across the span is not a signal.
        slope_db = np.linspace(-10, 10, SIZE)
        power = noise() * 10 ** (slope_db / 10)
        self.assertEqual(find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])
        power[5999:6002] += NOISE * 10 ** ((slope_db[6000] + 18) / 10)
        [found] = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertAlmostEqual(found.margin_db, 18.0, delta=1.5)

    def test_signals_come_lowest_first_and_the_strongest_are_kept(self):
        power = noise()
        for index, above_db in ((1500, 25.0), (2500, 14.0), (5500, 20.0), (6500, 16.0)):
            add_signal(power, index, above_db)
        found = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertEqual([round(signal.frequency_hz) for signal in found],
                         sorted(round(signal.frequency_hz) for signal in found))
        self.assertEqual(len(found), 4)
        with mock.patch("radiolack.scanners.MAX_RUNS", 2):
            strongest = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertAlmostEqual(strongest[0].frequency_hz, frequency_of(1500), delta=BIN_HZ)
        self.assertAlmostEqual(strongest[1].frequency_hz, frequency_of(5500), delta=BIN_HZ)
        self.assertEqual(len(strongest), 2)

    def test_rounding_errors_below_any_receivers_noise_are_not_signals(self):
        # Samples without noise, as a test source gives, leave rounding errors between the bins of a tone.
        power = np.random.default_rng(1).exponential(1e-21, SIZE)
        power[::32] *= 1e3
        power[5000] = 1e-2
        [found] = find_signals(power, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        self.assertAlmostEqual(found.frequency_hz, frequency_of(5000), delta=1.0)

    def test_degenerate_spectra_find_nothing(self):
        self.assertEqual(find_signals(np.ones(8), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])
        self.assertEqual(find_signals(noise(), CENTRE_HZ, 0.0, DEFAULT_THRESHOLD_DB), [])
        self.assertEqual(find_signals(np.zeros(SIZE), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])

    def test_local_noise_is_the_median_around_each_bin(self):
        db = np.full(4096, -60.0)
        db[2000:2010] = -20.0           # a signal barely moves the median
        db[3000:] = -40.0               # a step does, beyond half the window
        level = local_noise(db, BIN_HZ)
        self.assertAlmostEqual(level[2005], -60.0, delta=0.1)
        self.assertAlmostEqual(level[1000], -60.0, delta=0.1)
        self.assertAlmostEqual(level[4000], -40.0, delta=0.1)


class DetectorTest(unittest.TestCase):
    def test_finds_the_signals_of_the_mean_of_its_blocks(self):
        detector = Detector(blocks=3)
        quiet, loud = noise(1), noise(2)
        add_signal(loud, 5000, 25.0)
        self.assertIsNone(detector.add(loud, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB))
        self.assertIsNone(detector.add(quiet, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB))
        [found] = detector.add(quiet, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        # One block in three: 25 dB less 4.8 dB.
        self.assertAlmostEqual(found.margin_db, 20.2, delta=1.5)
        self.assertIsNone(detector.add(quiet, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB))     # a new average

    def test_another_capture_starts_the_average_again(self):
        detector = Detector(blocks=2)
        self.assertIsNone(detector.add(noise(1), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB))
        for capture in ((CENTRE_HZ + 1e6, RATE_HZ, SIZE), (CENTRE_HZ, RATE_HZ / 2, SIZE), (CENTRE_HZ, RATE_HZ, 4096)):
            with self.subTest(capture=capture):
                centre, rate, size = capture
                self.assertIsNone(detector.add(noise(2)[:size], centre, rate, DEFAULT_THRESHOLD_DB))
        self.assertEqual(detector.add(noise(3)[:4096], CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB), [])

    def test_reset_forgets_the_blocks(self):
        detector = Detector(blocks=2)
        detector.add(noise(1), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB)
        detector.reset()
        self.assertIsNone(detector.add(noise(2), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB))

    def test_needs_a_block(self):
        with self.assertRaises(ValueError):
            Detector(blocks=0)


def carrier(frequency_hz: float, margin_db: float = 20.0, width_hz: float = 600.0) -> Detection:
    return Detection(frequency_hz, width_hz, margin_db)


class SignalsTest(unittest.TestCase):
    def listed(self, signals: Signals, now: float) -> list[tuple[int, bool]]:
        return [(entry["frequency_hz"], entry["active"]) for entry in signals.describe(now)]

    def test_a_signal_is_listed_once_found_twice_in_a_row(self):
        signals = Signals()
        signals.update([carrier(145_500_000)], 0.0)
        self.assertEqual(signals.describe(0.0), [])
        signals.update([carrier(145_500_200, 21.0)], 0.3)
        self.assertEqual(signals.describe(0.3), [{"frequency_hz": 145_500_100, "width_hz": 600, "margin_db": 21.0,
                                                  "active": True, "age_s": 0.0}])

    def test_a_signal_found_once_is_not_listed(self):
        signals = Signals()
        signals.update([carrier(145_500_000)], 0.0)
        signals.update([], 0.3)
        signals.update([carrier(145_500_000)], 0.6)
        self.assertEqual(signals.describe(0.6), [])

    def test_close_detections_follow_the_signal(self):
        signals = Signals()
        for now in (0.0, 0.3):
            signals.update([carrier(145_500_000)], now)
        signals.update([carrier(145_501_000)], 0.6)     # within MERGE_HZ: the same signal, drifting
        self.assertEqual(self.listed(signals, 0.6), [(145_500_300, True)])
        signals.update([carrier(145_500_000 + 2 * MERGE_HZ)], 0.9)     # farther: another
        self.assertEqual(len(signals._candidates), 1)

    def test_wide_signals_merge_within_a_quarter_of_their_width(self):
        signals = Signals()
        for now, offset in ((0.0, 0), (0.3, 3_000), (0.6, -3_000)):
            signals.update([carrier(145_600_000 + offset, width_hz=16_000)], now)
        [entry] = signals.describe(0.6)
        self.assertAlmostEqual(entry["frequency_hz"], 145_600_000, delta=1_500)

    def test_active_signals_come_first_strongest_first_then_the_latest_found(self):
        signals = Signals()
        for now in (0.0, 0.3):
            signals.update([carrier(144_000_000, 15.0), carrier(144_100_000, 30.0)], now)
        for now in (5.0, 5.3):
            signals.update([carrier(145_000_000, 18.0), carrier(145_100_000, 25.0)], now)
        for now in (10.0, 10.3):
            signals.update([carrier(146_000_000, 12.0)], now)
        self.assertEqual(self.listed(signals, 10.3), [(146_000_000, True), (145_000_000, False),
                                                      (145_100_000, False), (144_000_000, False),
                                                      (144_100_000, False)])
        signals.update([carrier(145_000_000, 18.0), carrier(145_100_000, 25.0), carrier(146_000_000, 12.0)], 10.6)
        self.assertEqual([entry["frequency_hz"] for entry in signals.describe(10.6)][:3],
                         [145_100_000, 145_000_000, 146_000_000])
        self.assertEqual(signals.describe(10.6 + ACTIVE_S + 1)[0]["age_s"], ACTIVE_S + 1)

    def test_only_the_latest_signals_are_kept_and_listed(self):
        signals = Signals()
        with mock.patch("radiolack.scanners.MAX_SIGNALS", 3), mock.patch("radiolack.scanners.LISTED", 2):
            for index in range(5):
                for now in (index, index + 0.3):
                    signals.update([carrier(145_000_000 + index * 25_000)], now)
            self.assertEqual(len(signals._signals), 3)
            self.assertEqual([entry["frequency_hz"] for entry in signals.describe(10.0)], [145_100_000, 145_075_000])

    def test_changed_tells_when_the_list_changes_beyond_the_ages(self):
        signals = Signals()
        self.assertFalse(signals.changed(0.0))
        for now in (0.0, 0.3):
            signals.update([carrier(145_500_000, 20.0)], now)
        self.assertTrue(signals.changed(0.3))
        self.assertFalse(signals.changed(0.5))
        signals.update([carrier(145_500_000, 20.3)], 0.6)    # less than 1 dB, and 100 Hz
        self.assertFalse(signals.changed(0.6))
        signals.update([carrier(145_500_000, 22.0)], 0.9)
        self.assertTrue(signals.changed(0.9))
        self.assertTrue(signals.changed(0.9 + ACTIVE_S + 0.1))    # no longer active
        self.assertFalse(signals.changed(60.0))

    def test_ignored_frequencies_are_not_listed(self):
        signals = Signals()
        for now in (0.0, 0.3):
            signals.update([carrier(145_500_000), carrier(145_600_000)], now)
        signals.update([carrier(145_700_000)], 0.3)
        signals.ignore(145_500_400)
        signals.ignore(145_700_000)
        signals.ignore(145_500_000)     # within MERGE_HZ of one ignored already
        self.assertEqual(signals.ignored, [145_500_400, 145_700_000])
        self.assertEqual(self.listed(signals, 0.3), [(145_600_000, True)])
        for now in (2.0, 2.3):
            signals.update([carrier(145_500_000), carrier(145_700_000)], now)
        self.assertEqual(self.listed(signals, 2.3), [(145_600_000, False)])
        signals.restore()
        self.assertEqual(signals.ignored, [])
        self.assertEqual(len(signals.describe(2.3)), 1)      # the signals left out stay out until found again
        signals.clear()
        self.assertEqual(signals.describe(2.3), [])

    def test_at_most_max_ignored_frequencies(self):
        signals = Signals()
        for index in range(MAX_IGNORED):
            signals.ignore(144_000_000 + index * 10_000)
        signals.ignore(144_000_000)         # already ignored
        with self.assertRaisesRegex(ValueError, f"at most {MAX_IGNORED}"):
            signals.ignore(146_000_000)
        self.assertEqual(len(signals.ignored), MAX_IGNORED)
        self.assertNotIn(146_000_000, signals.ignored)


class CheckTest(unittest.TestCase):
    def test_frequencies(self):
        self.assertEqual(checked_frequency(145_500_000.4, "a frequency"), 145_500_000.0)
        self.assertEqual(checked_frequency(1, "a frequency"), 1.0)
        for value in (True, "145500000", None, float("nan"), float("inf"), [1], 0, -1, 1e11 + 1, 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, r"a frequency must be a number from 1 Hz to 1e\+11 Hz"):
                checked_frequency(value, "a frequency")

    def test_thresholds(self):
        self.assertEqual(checked_threshold(6), 6.0)
        self.assertEqual(checked_threshold(12.5), 12.5)
        for value in (5.9, 30.1, True, "12", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "from 6 to 30"):
                checked_threshold(value)


if __name__ == "__main__":
    unittest.main()
