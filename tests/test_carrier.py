"""Tests for the measure of a carrier (carrier.py): synthetic channels only."""

import unittest

import numpy as np

from radiolack.carrier import (NO_SIGNAL, SEVERAL, UNSTABLE, Carrier, CarrierMeasure, MIN_MARGIN_DB,
                               strongest_line)

RATE_HZ = 48_000.0
SECOND = int(RATE_HZ)


def channel(seconds: float, tones: dict[float, float], noise: float = 0.01, seed: int = 3,
            start_s: float = 0.0) -> np.ndarray:
    """``seconds`` of a channel at RATE_HZ: each tone, a frequency in Hz for
    its amplitude, over complex white noise."""
    rng = np.random.default_rng(seed)
    t = start_s + np.arange(round(seconds * RATE_HZ)) / RATE_HZ
    samples = noise * (rng.normal(size=len(t)) + 1j * rng.normal(size=len(t)))
    for frequency, amplitude in tones.items():
        samples = samples + amplitude * np.exp(2j * np.pi * frequency * t)
    return samples.astype(np.complex64)


def measure(samples: np.ndarray, low_hz: float = -4000.0, high_hz: float = 4000.0, block: int = 1600,
            key: object = "setting") -> list[Carrier]:
    """What a CarrierMeasure gives for ``samples`` in blocks of ``block``."""
    gatherer = CarrierMeasure()
    results = []
    for start in range(0, len(samples), block):
        result = gatherer.add(samples[start:start + block], RATE_HZ, low_hz, high_hz, key)
        if result is not None:
            results.append(result)
            gatherer = CarrierMeasure()
    return results


class StrongestLineTest(unittest.TestCase):
    def test_places_a_carrier_between_bins_within_a_twentieth_of_a_hertz(self):
        for frequency in np.linspace(1234.0, 1235.0, 11):
            with self.subTest(frequency=frequency):
                line = strongest_line(channel(1.0, {frequency: 0.5}), RATE_HZ, -4000, 4000)
                self.assertAlmostEqual(line.frequency_hz, frequency, delta=0.05)
                self.assertGreater(line.margin_db, 60)
                self.assertFalse(line.rival)

    def test_noise_alone_stands_less_than_the_margin_above_itself(self):
        line = strongest_line(channel(1.0, {}), RATE_HZ, -4000, 4000)
        self.assertLess(line.margin_db, MIN_MARGIN_DB)

    def test_silence_has_no_line(self):
        self.assertIsNone(strongest_line(np.zeros(SECOND, np.complex64), RATE_HZ, -4000, 4000))

    def test_a_carrier_beyond_the_filter_s_edge_is_not_one_of_its_lines(self):
        # Its slope reaches the edge's bin, which is no peak.
        self.assertIsNone(strongest_line(channel(1.0, {251.0: 0.5}), RATE_HZ, -250, 250))
        # Nearest to the edge's bin, it is: a carrier on the VFO in USB lies at its filter's lower edge.
        for frequency, low, high in ((250.4, -250, 250), (249.6, 250, 2950)):
            line = strongest_line(channel(1.0, {frequency: 0.5}), RATE_HZ, low, high)
            self.assertAlmostEqual(line.frequency_hz, frequency, delta=0.05)

    def test_a_filter_narrower_than_three_bins_has_no_line(self):
        self.assertIsNone(strongest_line(channel(1.0, {10.0: 0.5}), RATE_HZ, 9.5, 10.5))

    def test_two_carriers_of_nearly_equal_strength_are_rivals(self):
        self.assertTrue(strongest_line(channel(1.0, {100.0: 0.5, 900.0: 0.4}), RATE_HZ, -4000, 4000).rival)
        # 15 dB apart, the stronger is the carrier.
        line = strongest_line(channel(1.0, {100.0: 0.5, 900.0: 0.09}), RATE_HZ, -4000, 4000)
        self.assertFalse(line.rival)
        self.assertAlmostEqual(line.frequency_hz, 100.0, delta=0.05)

    def test_an_am_signal_s_carrier_stands_above_its_sidebands(self):
        t = np.arange(SECOND) / RATE_HZ
        am = (1 + 0.9 * np.cos(2 * np.pi * 1000 * t)) * np.exp(2j * np.pi * 37.3 * t)
        line = strongest_line(am.astype(np.complex64), RATE_HZ, -4000, 4000)
        self.assertFalse(line.rival)
        self.assertAlmostEqual(line.frequency_hz, 37.3, delta=0.05)

    def test_an_fm_signal_whose_carrier_vanishes_has_rival_lines(self):
        # A modulation index of 2.405 leaves no carrier, and two equal first sidebands.
        t = np.arange(SECOND) / RATE_HZ
        fm = np.exp(2j * np.pi * 37.0 * t + 1j * 2.405 * np.sin(2 * np.pi * 1000 * t))
        self.assertTrue(strongest_line(fm.astype(np.complex64), RATE_HZ, -5000, 5000).rival)


class CarrierMeasureTest(unittest.TestCase):
    def test_two_seconds_that_agree_give_their_mean(self):
        [result] = measure(channel(2.0, {1234.56: 0.5}))
        self.assertIsNone(result.reason)
        self.assertAlmostEqual(result.frequency_hz, 1234.56, delta=0.05)
        self.assertGreater(result.margin_db, 60)

    def test_a_carrier_that_moves_more_than_a_hertz_is_unstable(self):
        first = channel(1.0, {1234.0: 0.5})
        second = channel(1.0, {1235.5: 0.5}, seed=4, start_s=1.0)
        [result] = measure(np.concatenate((first, second)))
        self.assertEqual(result.reason, UNSTABLE)
        self.assertIsNone(result.frequency_hz)
        self.assertGreater(result.margin_db, 60)

    def test_noise_ends_the_measure_after_one_second(self):
        results = measure(channel(2.0, {}))
        self.assertEqual(len(results), 2)        # one per second: each ends its measure at once
        self.assertEqual(results[0].reason, NO_SIGNAL)
        self.assertLess(results[0].margin_db, MIN_MARGIN_DB)

    def test_silence_has_no_margin(self):
        [result] = measure(np.zeros(SECOND, np.complex64))
        self.assertEqual((result.reason, result.margin_db), (NO_SIGNAL, None))

    def test_two_carriers_of_nearly_equal_strength_give_no_frequency(self):
        [result] = measure(channel(1.0, {100.0: 0.5, 900.0: 0.45}))
        self.assertEqual((result.reason, result.frequency_hz), (SEVERAL, None))

    def test_another_setting_starts_the_measure_again(self):
        gatherer = CarrierMeasure()
        samples = channel(3.0, {1234.0: 0.5})
        self.assertFalse(gatherer.received)
        self.assertIsNone(gatherer.add(samples[:SECOND + 100], RATE_HZ, -4000, 4000, "before"))
        self.assertTrue(gatherer.received)
        # A second and a half more with another setting is not enough: it starts from nothing.
        self.assertIsNone(gatherer.add(samples[SECOND:SECOND * 5 // 2], RATE_HZ, -4000, 4000, "after"))
        result = gatherer.add(samples[SECOND * 5 // 2:], RATE_HZ, -4000, 4000, "after")
        self.assertAlmostEqual(result.frequency_hz, 1234.0, delta=0.05)

    def test_an_empty_block_waits(self):
        gatherer = CarrierMeasure()
        self.assertIsNone(gatherer.add(np.zeros(0, np.complex64), RATE_HZ, -4000, 4000, None))
        self.assertTrue(gatherer.received)


if __name__ == "__main__":
    unittest.main()
