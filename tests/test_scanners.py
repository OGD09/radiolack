"""Tests for detection: signals found above the local noise, away from the
span's edges and centre, the detector's average over one capture, the list
of signals found twice in a row, ignored frequencies; for the memory scan:
the bookmarks it watches, their channels' power and noise, its progress
from channel to channel, the windows its bookmarks take and its visits from
window to window; for the sweep: its windows across a range and its
progress from window to window; and the checks of the values the interfaces
give."""

import math
import unittest
from unittest import mock

import numpy as np

from radiolack.bookmarks import Bookmark
from radiolack.scanners import (ACTIVE_S, CHECK_S, DC_GAP_HZ, DEFAULT_THRESHOLD_DB, GIVE_UP_S, HOLD_S, MAX_IGNORED,
                                MERGE_HZ, SWEEP_SEARCHES, USABLE_FRACTION, VISIT_S, ChannelWatch, Detection, Detector,
                                MemoryScan, MemoryTour, Signals, Sweep, SweepWindows, checked_frequency,
                                checked_resume, checked_sweep_range, checked_threshold, checked_window, find_signals,
                                local_noise, memory_window, memory_windows, search_blocks, sweep_windows,
                                watched_channels)
from radiolack.spectrum import SpectrumAnalyzer
from tests.fakes import wide_signal

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

    def test_a_search_may_average_another_number_of_blocks(self):
        detector = Detector(blocks=10)
        loud = noise(1)
        add_signal(loud, 5000, 25.0)
        self.assertIsNone(detector.add(loud, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB, 2))
        [found] = detector.add(loud, CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB, 2)
        self.assertAlmostEqual(found.frequency_hz, frequency_of(5000), delta=BIN_HZ)
        self.assertEqual(detector.add(noise(2), CENTRE_HZ, RATE_HZ, DEFAULT_THRESHOLD_DB, 1), [])

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

    def test_an_update_returns_the_listed_signals_found_in_it(self):
        signals = Signals()
        self.assertEqual(signals.update([carrier(145_500_000)], 0.0), [])     # a candidate only
        [found] = signals.update([carrier(145_500_200, 21.0), carrier(145_500_400, 22.0),  # one signal
                                  carrier(146_000_000)], 0.3)                             # a new candidate
        self.assertEqual((found.frequency_hz, found.margin_db, found.last_s), (145_500_190.0, 22.0, 0.3))
        found.margin_db = 0.0       # a copy: the list keeps its own
        self.assertEqual(signals.describe(0.3)[0]["margin_db"], 22.0)
        self.assertEqual(len(signals.update([carrier(145_500_000), carrier(146_000_000)], 0.6)), 2)

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


class WatchedChannelsTest(unittest.TestCase):
    def test_the_ticked_bookmarks_of_the_searched_span_are_watched_with_their_passbands(self):
        ticked = [(1, Bookmark(CENTRE_HZ - 950_000, "near the edge", "NFM")),
                  (2, Bookmark(CENTRE_HZ - 970_000, "in the outer tenth", "NFM")),
                  (3, Bookmark(CENTRE_HZ + 2_000, "on the centre", "AM")),
                  (4, Bookmark(CENTRE_HZ + 500_000, "upper sideband", "USB")),
                  (5, Bookmark(CENTRE_HZ + 600_000, "lower sideband", "LSB")),
                  (6, Bookmark(CENTRE_HZ + 700_000, "beacon", "CW")),
                  (7, Bookmark(CENTRE_HZ + 800_000, "airband", "AM")),
                  (8, Bookmark(CENTRE_HZ + 2e6, "elsewhere", "NFM")),
                  (9, Bookmark(CENTRE_HZ - 300_000, "broadcast", "WFM"))]
        channels = watched_channels(ticked, CENTRE_HZ, RATE_HZ)
        self.assertEqual([(channel.bookmark_id, channel.mode) for channel in channels],
                         [(1, "NFM"), (4, "USB"), (5, "LSB"), (6, "CW"), (7, "AM"), (9, "WFM")])
        self.assertEqual([(channel.low_hz - channel.frequency_hz, channel.high_hz - channel.frequency_hz)
                          for channel in channels],
                         [(-5_000, 5_000), (0, 2_700), (-2_700, 0), (-250, 250), (-4_000, 4_000),
                          (-90_000, 90_000)])
        # Only a WFM channel's noise is read beyond its edges.
        self.assertEqual([channel.clearance_hz for channel in channels], [0, 0, 0, 0, 0, 90_000])
        self.assertEqual(watched_channels([], CENTRE_HZ, RATE_HZ), ())


class ChannelWatchTest(unittest.TestCase):
    """On the bin powers of the engine's analyzer: a tone of -20 dBFS 300 kHz
    above the centre, over noise of -40 dBFS across the span."""

    def setUp(self):
        self.analyzer = SpectrumAnalyzer()
        count = 4 * self.analyzer.fft_size
        rng = np.random.default_rng(3)
        noise = (rng.normal(size=count) + 1j * rng.normal(size=count)) * math.sqrt(1e-4 / 2)
        tone = 0.1 * np.exp(2j * np.pi * 300_000 / RATE_HZ * np.arange(count))
        self.power = self.analyzer.power((tone + noise).astype(np.complex64))
        self.bands = np.array([(CENTRE_HZ + 295_000, CENTRE_HZ + 305_000, 0.0),    # the tone's channel
                               (CENTRE_HZ - 405_000, CENTRE_HZ - 395_000, 0.0)])   # a quiet one

    def levels(self, watch: ChannelWatch, centre_hz: float = CENTRE_HZ):
        return watch.levels(self.power, centre_hz, RATE_HZ, self.bands, self.analyzer.enbw)

    def test_gives_each_channels_power_and_noise_once_the_noise_is_known(self):
        watch = ChannelWatch(blocks=2)
        self.assertIsNone(self.levels(watch))
        power_db, noise_db = self.levels(watch)
        in_band_noise_db = 10 * math.log10(1e-4 * 10_000 / RATE_HZ)
        self.assertAlmostEqual(power_db[0], -20.0, delta=0.3)
        self.assertAlmostEqual(power_db[1], in_band_noise_db, delta=1.0)
        for level in noise_db:
            self.assertAlmostEqual(level, in_band_noise_db, delta=1.0)
        self.assertIsNotNone(self.levels(watch))        # every block, from then on

    def test_another_capture_waits_for_its_own_noise(self):
        watch = ChannelWatch(blocks=2)
        self.levels(watch)
        self.assertIsNotNone(self.levels(watch))
        self.assertIsNone(self.levels(watch, CENTRE_HZ + 1_000))
        watch.reset()
        self.assertIsNone(self.levels(watch, CENTRE_HZ + 1_000))
        self.assertIsNotNone(self.levels(watch, CENTRE_HZ + 1_000))

    def test_reads_a_wide_channels_noise_beyond_its_edges_on_the_quieter_side(self):
        # A broadcast station 300 kHz above the centre, 20 dB above the
        # noise, and its neighbour 200 kHz above it, in a WFM channel.
        rng = np.random.default_rng(4)
        count = 4 * self.analyzer.fft_size
        samples = (rng.normal(size=count) + 1j * rng.normal(size=count)) * math.sqrt(1e-4 / 2)
        for offset in (300_000.0, 500_000.0):
            samples = samples + wide_signal(count, RATE_HZ, offset, 100 * 1e-4 / RATE_HZ, rng)
        power = self.analyzer.power(samples.astype(np.complex64))
        channel = (CENTRE_HZ + 210_000, CENTRE_HZ + 390_000)
        bands = np.array([(*channel, 90_000.0), (*channel, 0.0)])
        power_db, noise_db = ChannelWatch(blocks=1).levels(power, CENTRE_HZ, RATE_HZ, bands, self.analyzer.enbw)
        in_band_noise_db = 10 * math.log10(1e-4 * 180_000 / RATE_HZ)
        self.assertAlmostEqual(noise_db[0], in_band_noise_db, delta=1.0)
        self.assertGreater(noise_db[1], in_band_noise_db + 10.0)    # without the clearance, the station itself
        self.assertGreater(power_db[0] - noise_db[0], 15.0)


class MemoryScanTest(unittest.TestCase):
    def test_tunes_to_the_strongest_active_channel_listens_waits_and_watches_again(self):
        scan = MemoryScan()
        self.assertIsNone(scan.step(0.0, None, False, 2.0))     # the noise is not known yet
        self.assertIsNone(scan.step(0.1, [], False, 2.0))
        self.assertEqual(scan.step(0.2, [(1, 10.0), (2, 25.0)], False, 2.0), 2)
        self.assertEqual((scan.state, scan.bookmark_id), ("checking", 2))
        self.assertIsNone(scan.step(0.3, [(1, 30.0)], True, 2.0))      # the others wait
        self.assertEqual((scan.state, scan.bookmark_id), ("listening", 2))
        scan.step(1.0, [], False, 2.0)
        self.assertEqual(scan.state, "waiting")
        scan.step(2.0, [], True, 2.0)       # open again within the resume time
        self.assertEqual(scan.state, "listening")
        scan.step(3.0, [], False, 2.0)
        scan.step(4.9, [(1, 10.0)], False, 2.0)
        self.assertEqual((scan.state, scan.bookmark_id), ("waiting", 2))
        self.assertIsNone(scan.step(5.0, [(1, 10.0)], False, 2.0))
        self.assertEqual((scan.state, scan.bookmark_id), ("watching", None))
        self.assertEqual(scan.step(5.1, [(1, 10.0)], False, 2.0), 1)

    def test_without_a_resume_time_it_watches_again_once_the_squelch_closes(self):
        scan = MemoryScan()
        scan.step(0.0, [(1, 10.0)], False, 0.0)
        scan.step(0.1, [], True, 0.0)
        scan.step(0.2, [], False, 0.0)
        scan.step(0.3, [], False, 0.0)
        self.assertEqual((scan.state, scan.bookmark_id), ("watching", None))

    def test_a_channel_whose_squelch_stays_closed_is_held_a_moment(self):
        scan = MemoryScan()
        self.assertEqual(scan.step(0.0, [(1, 10.0), (2, 5.0)], False, 2.0), 1)
        scan.step(CHECK_S - 0.01, [], False, 2.0)
        self.assertEqual(scan.state, "checking")
        scan.step(CHECK_S, [], False, 2.0)
        self.assertEqual(scan.state, "watching")
        self.assertEqual(scan.step(CHECK_S + 0.01, [(1, 10.0), (2, 5.0)], False, 2.0), 2)
        scan.step(2 * CHECK_S + 0.01, [], False, 2.0)
        self.assertIsNone(scan.step(1.0, [(1, 10.0), (2, 5.0)], False, 2.0))      # both held
        self.assertEqual(scan.step(CHECK_S + HOLD_S, [(1, 10.0), (2, 5.0)], False, 2.0), 1)

    def test_a_bookmark_no_longer_watched_is_left(self):
        scan = MemoryScan()
        scan.step(0.0, [(1, 10.0)], False, 2.0)
        scan.keep([1, 2])
        self.assertEqual((scan.state, scan.bookmark_id), ("checking", 1))
        scan.keep([2])
        self.assertEqual((scan.state, scan.bookmark_id), ("watching", None))


R820T_HZ = (24e6, 1766e6)       # the centre frequencies an RTL-SDR's usual tuner tunes to


def watching(frequency_hz: float, centres, rate_hz: float) -> list[float]:
    """The windows, by their centres, whose searched part holds ``frequency_hz``."""
    return [centre for centre in centres
            if DC_GAP_HZ < abs(frequency_hz - centre) <= USABLE_FRACTION * rate_hz]


class MemoryWindowsTest(unittest.TestCase):
    def test_each_frequency_within_reach_lies_in_the_searched_part_of_a_window(self):
        rng = np.random.default_rng(7)
        raster = [430e6 + n * 12_500 for n in range(0, 800, 7)]     # across 70 cm
        dense = [145e6 + n * 5_000 for n in range(60)]               # closer than the DC gap allows
        spread = list(np.round(rng.uniform(24e6, 1766e6, 200)))
        edges = [23_100_000.0, 24e6, 1766e6, 1766_900_000.0]    # all within reach at 2.4 MS/s
        for rate in (250_000.0, 1_024_000.0, 2_400_000.0):
            for name, frequencies in (("raster", raster), ("dense", dense), ("spread", spread), ("edges", edges)):
                with self.subTest(rate=rate, frequencies=name):
                    centres = memory_windows(frequencies, rate, *R820T_HZ)
                    self.assertEqual(list(centres), sorted(set(centres)))
                    for centre in centres:
                        self.assertEqual(centre, round(centre))
                        self.assertTrue(R820T_HZ[0] <= centre <= R820T_HZ[1])
                    within = [frequency for frequency in frequencies
                              if memory_window(frequency, rate, *R820T_HZ) is not None]
                    if rate == 2_400_000.0 or name != "edges":
                        self.assertEqual(within, frequencies)
                    for frequency in within:
                        self.assertTrue(watching(frequency, centres, rate), frequency)

    def test_takes_as_few_windows_as_the_frequencies_need(self):
        rate = 2_400_000.0      # a window holds 1.92 MHz
        cases = (([433e6, 433.5e6, 434.9e6], 1), ([145.5e6, 433.5e6], 2), ([145.5e6, 145.5e6], 1),
                 ([430e6 + n * 25_000 for n in range(401)], 6), ([], 0))
        for frequencies, count in cases:
            with self.subTest(frequencies=frequencies[:3], count=count):
                self.assertEqual(len(memory_windows(frequencies, rate, *R820T_HZ)), count)

    def test_a_window_moves_below_a_frequency_that_would_fall_in_its_dc_gap(self):
        rate = 2_400_000.0
        lowest, other = 433e6, 433e6 + 962_500      # the other one 2.5 kHz above the highest window's centre
        self.assertEqual(memory_window(lowest, rate, *R820T_HZ), lowest + 960_000)
        [centre] = memory_windows([lowest, other], rate, *R820T_HZ)
        self.assertEqual(centre, other - DC_GAP_HZ - 1)
        self.assertEqual((watching(lowest, [centre], rate), watching(other, [centre], rate)), ([centre], [centre]))

    def test_frequencies_beyond_the_receivers_reach_are_left_out(self):
        rate = 2_400_000.0
        self.assertEqual(memory_window(23.5e6, rate, *R820T_HZ), 24.46e6)
        self.assertIsNone(memory_window(23e6, rate, *R820T_HZ))
        # Near the top of its range, a window holds the frequency in its upper half.
        self.assertEqual(memory_window(1766.5e6, rate, *R820T_HZ), 1766e6)
        self.assertEqual(memory_window(1766e6, rate, *R820T_HZ), 1766e6 - DC_GAP_HZ - 1)
        self.assertIsNone(memory_window(1767e6, rate, *R820T_HZ))
        self.assertEqual(memory_windows([10e6, 145.5e6, 2400e6], rate, *R820T_HZ), (146.46e6,))
        self.assertEqual(memory_windows([10e6, 2400e6], rate, *R820T_HZ), ())


class MemoryTourTest(unittest.TestCase):
    WINDOWS = (430e6, 432e6, 434e6)

    def test_watches_each_window_its_time_once_its_noise_is_known_then_goes_on(self):
        tour = MemoryTour(self.WINDOWS, 430e6, now=0.0)
        self.assertEqual((tour.window, tour.moved), (0, False))
        self.assertIsNone(tour.step(0.1, 430e6, True, False))      # its noise is not known yet
        self.assertIsNone(tour.step(0.4, 430e6, True, True))       # it is: the window's time starts
        self.assertIsNone(tour.step(0.4 + VISIT_S - 0.01, 430e6, True, True))
        self.assertEqual(tour.step(0.4 + VISIT_S, 430e6, True, True), 432e6)
        self.assertEqual((tour.window, tour.moved), (1, True))
        self.assertIsNone(tour.step(2.0, 430e6, True, True))       # the first window's samples still come
        self.assertIsNone(tour.step(2.1, 432e6, True, False))
        self.assertIsNone(tour.step(2.2, 432e6, True, True))
        self.assertEqual(tour.step(2.2 + VISIT_S, 432e6, True, True), 434e6)
        tour.step(3.5, 434e6, True, True)
        self.assertEqual(tour.step(3.5 + VISIT_S, 434e6, True, True), 430e6)     # the first after the last
        self.assertEqual(tour.window, 0)

    def test_a_channel_it_stops_on_gives_the_window_its_time_again(self):
        tour = MemoryTour(self.WINDOWS, 430e6, now=0.0)
        tour.step(0.0, 430e6, True, True)
        self.assertIsNone(tour.step(0.5, 430e6, False, True))      # on a channel
        self.assertIsNone(tour.step(5.0, 430e6, False, True))      # it stays while it listens
        self.assertIsNone(tour.step(6.0, 430e6, True, True))       # watching again
        self.assertIsNone(tour.step(6.0 + VISIT_S - 0.01, 430e6, True, True))
        self.assertEqual(tour.step(6.0 + VISIT_S, 430e6, True, True), 432e6)

    def test_a_window_whose_samples_do_not_come_is_left(self):
        tour = MemoryTour(self.WINDOWS, 430e6, now=0.0)
        tour.step(0.0, 430e6, True, True)
        self.assertEqual(tour.step(VISIT_S, 430e6, True, True), 432e6)
        self.assertIsNone(tour.step(VISIT_S + GIVE_UP_S - 0.01, 430e6, True, True))
        self.assertEqual(tour.step(VISIT_S + GIVE_UP_S, 430e6, True, True), 434e6)

    def test_starts_from_the_nearest_window_which_it_asks_for_once_it_watches(self):
        tour = MemoryTour(self.WINDOWS, 433.1e6, now=0.0)
        self.assertEqual(tour.window, 2)
        self.assertIsNone(tour.step(0.1, 433.1e6, False, True))    # on a channel: not yet
        self.assertEqual(tour.step(0.2, 433.1e6, True, True), 434e6)
        self.assertEqual((tour.window, tour.moved), (2, True))

    def test_one_window_is_never_left_and_a_restart_goes_on_where_the_receiver_is(self):
        tour = MemoryTour((145e6,), 145e6, now=0.0)
        for now in (0.0, 5.0, 50.0):
            self.assertIsNone(tour.step(now, 145e6, True, True))
        self.assertIsNone(tour.step(60.0, 146e6, True, True))
        self.assertFalse(tour.moved)
        tour.restart(self.WINDOWS, 432e6, now=61.0)
        self.assertEqual(tour.window, 1)
        self.assertIsNone(tour.step(61.1, 432e6, True, True))      # it is there already
        tour.restart((145e6,), 432e6, now=62.0)
        self.assertEqual(tour.step(62.1, 432e6, True, True), 145e6)
        with self.assertRaisesRegex(ValueError, "one window at least"):
            tour.restart((), 145e6, now=63.0)


class SweepWindowsTest(unittest.TestCase):
    RATES_HZ = (900_001.0, 1_024_000.0, 2_048_000.0, 2_400_000.0, 3_200_000.0)

    @staticmethod
    def centres(windows: SweepWindows) -> np.ndarray:
        return np.array([windows.centre(index) for index in range(windows.count)])

    def test_first_and_last_lie_a_tenth_of_the_rate_inside_the_range_at_most_a_quarter_apart(self):
        for (low, high), count in (((430e6, 440e6), 17), ((118e6, 137e6), 32), ((1240e6, 1300e6), 101)):
            with self.subTest(low=low, high=high):
                windows = sweep_windows(low, high, RATE_HZ)
                centres = self.centres(windows)
                self.assertEqual(windows.count, count)
                self.assertEqual((centres[0], centres[-1]), (low + 0.1 * RATE_HZ, high - 0.1 * RATE_HZ))
                self.assertTrue(np.all(centres == np.round(centres)))
                steps = np.diff(centres)
                self.assertLessEqual(steps.max(), 0.25 * RATE_HZ + 1)
                self.assertGreaterEqual(steps.min(), steps.max() - 1)

    def test_a_narrow_range_gets_two_windows_a_quarter_of_the_rate_apart_around_its_middle(self):
        for width_hz in (1.0, 25_000.0, 0.2 * RATE_HZ, 0.45 * RATE_HZ):
            with self.subTest(width_hz=width_hz):
                windows = sweep_windows(CENTRE_HZ, CENTRE_HZ + width_hz, RATE_HZ)
                middle = CENTRE_HZ + width_hz / 2
                self.assertEqual(list(self.centres(windows)),
                                 [round(middle - 0.125 * RATE_HZ), round(middle + 0.125 * RATE_HZ)])

    def test_every_frequency_lies_in_a_searched_part_even_with_the_centre_off_by_15_percent(self):
        for rate in self.RATES_HZ:
            for low, high in ((430e6, 440e6), (118e6, 137e6), (144e6, 146e6), (145e6, 145.1e6),
                              (145e6, 145e6 + 0.2 * rate + 2_000), (145e6, 145e6 + 0.46 * rate)):
                centres = self.centres(sweep_windows(low, high, rate))
                frequencies = np.concatenate((np.linspace(low, high, 5001), centres,
                                              centres + DC_GAP_HZ, centres - DC_GAP_HZ))
                frequencies = frequencies[(frequencies >= low) & (frequencies <= high)]
                for offset in np.linspace(-0.15, 0.15, 13) * rate:
                    with self.subTest(rate=rate, low=low, high=high, offset=offset):
                        # The searched part, a bin narrower on each side.
                        distances = np.abs(frequencies[:, None] - centres[None, :] - offset)
                        searched = ((distances > DC_GAP_HZ + rate / 8192)
                                    & (distances < USABLE_FRACTION * rate - rate / 8192))
                        self.assertTrue(searched.any(axis=1).all())


class SweepTest(unittest.TestCase):
    WINDOWS = SweepWindows(430e6, 600e3, 3)

    def blocks(self, sweep: Sweep, now: float, centre_hz: float, count: int) -> list[float | None]:
        return [sweep.step(now, centre_hz) for _ in range(count)]

    def test_reads_its_searches_in_each_window_then_goes_on_pass_after_pass(self):
        sweep = Sweep(self.WINDOWS, search_blocks=2, now=0.0)
        self.assertEqual((sweep.window, sweep.passes, sweep.centre_hz), (0, 0, 430e6))
        per_window = SWEEP_SEARCHES * 2
        self.assertEqual(self.blocks(sweep, 1.0, 430e6, per_window), [None] * (per_window - 1) + [430.6e6])
        self.assertEqual(self.blocks(sweep, 2.0, 430.6e6, per_window)[-1], 431.2e6)
        self.assertEqual((sweep.window, sweep.passes), (2, 0))
        self.assertEqual(self.blocks(sweep, 3.0, 431.2e6, per_window)[-1], 430e6)
        self.assertEqual((sweep.window, sweep.passes), (0, 1))

    def test_blocks_of_another_window_do_not_count_and_one_that_does_not_come_is_left(self):
        sweep = Sweep(self.WINDOWS, search_blocks=1, now=10.0)
        self.assertEqual(self.blocks(sweep, 10.5, 145e6, 5), [None] * 5)
        self.assertIsNone(sweep.step(10.0 + GIVE_UP_S - 0.01, 145e6))
        self.assertEqual(sweep.step(10.0 + GIVE_UP_S, 145e6), 430.6e6)
        self.assertEqual(self.blocks(sweep, 13.0, 430.6e6, SWEEP_SEARCHES)[-1], 431.2e6)

    def test_fewer_blocks_apply_at_once_and_a_restart_begins_with_the_first_window(self):
        sweep = Sweep(self.WINDOWS, search_blocks=4, now=0.0)
        self.assertEqual(self.blocks(sweep, 1.0, 430e6, 3), [None] * 3)
        sweep.search_blocks = 1
        self.assertEqual(sweep.step(1.0, 430e6), 430.6e6)
        sweep.restart(SweepWindows(1240e6, 500e3, 5), 2.0)
        self.assertEqual((sweep.window, sweep.passes, sweep.windows.count, sweep.centre_hz), (0, 0, 5, 1240e6))

    def test_each_window_searches_twice_whatever_its_time(self):
        self.assertEqual(SWEEP_SEARCHES, 2)
        self.assertEqual([search_blocks(window_s, 30.0) for window_s in (0.25, 0.5, 1.0, 2.0)], [4, 8, 15, 30])
        self.assertEqual(search_blocks(0.25, 4.0), 1)


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

    def test_resume_times(self):
        self.assertEqual(checked_resume(0), 0.0)
        self.assertEqual(checked_resume(2.5), 2.5)
        self.assertEqual(checked_resume(8), 8.0)
        for value in (-0.1, 8.1, True, "2", None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "from 0 to 8"):
                checked_resume(value)

    def test_times_per_window(self):
        self.assertEqual(checked_window(0.25), 0.25)
        self.assertEqual(checked_window(2), 2.0)
        for value in (0.2, 2.1, 0, True, "0.5", None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "from 0.25 to 2"):
                checked_window(value)

    def test_sweep_ranges(self):
        self.assertEqual(checked_sweep_range(430e6, 440_000_000.4), (430e6, 440e6))
        self.assertEqual(checked_sweep_range(1, 2), (1.0, 2.0))
        for low, high in ((440e6, 430e6), (430e6, 430e6), (430e6, 430_000_000.4)):
            with self.subTest(low=low, high=high), self.assertRaisesRegex(ValueError, "must lie below its highest"):
                checked_sweep_range(low, high)
        for low, high in ((0, 440e6), (430e6, None), ("430 MHz", 440e6), (430e6, 1e12)):
            with self.subTest(low=low, high=high), self.assertRaisesRegex(ValueError, "must be a number"):
                checked_sweep_range(low, high)


if __name__ == "__main__":
    unittest.main()
