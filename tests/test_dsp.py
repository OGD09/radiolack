"""Tests for the DSP building blocks: filter design and the overlap-save FFT
filter, with its frequency shift and rate change."""

import unittest

import numpy as np

from radiolack.dsp import FftFilter, kaiser_length, lowpass, rate_ratio, shaped, smooth_numbers

RNG = np.random.default_rng(7)


def complex_noise(count: int) -> np.ndarray:
    return (RNG.standard_normal(count) + 1j * RNG.standard_normal(count)).astype(np.complex64)


def in_chunks(filter_: FftFilter, samples: np.ndarray, cuts) -> np.ndarray:
    return np.concatenate([filter_.process(chunk) for chunk in np.split(samples, cuts)])


def gain_db(taps: np.ndarray, frequency_hz: float, rate_hz: float) -> float:
    t = np.arange(len(taps)) - (len(taps) - 1) / 2
    return 20 * np.log10(abs(np.sum(taps * np.exp(-2j * np.pi * frequency_hz / rate_hz * t))))


def tone_purity_db(samples: np.ndarray) -> float:
    """The strongest component other than the main one, in dB below it."""
    spectrum = np.abs(np.fft.fft(samples * np.blackman(len(samples)))) ** 2
    peak = int(np.argmax(spectrum))
    others = np.delete(spectrum, np.arange(peak - 8, peak + 9) % len(spectrum))
    return 10 * np.log10(others.max() / spectrum[peak])


class DesignTest(unittest.TestCase):
    def test_lowpass_passes_stops_and_halves_at_its_cutoff(self):
        taps = lowpass(5_000, 1_000, 48_000)
        self.assertEqual(len(taps) % 2, 1)
        self.assertAlmostEqual(gain_db(taps, 0, 48_000), 0.0, places=6)
        self.assertAlmostEqual(gain_db(taps, 4_400, 48_000), 0.0, delta=0.01)
        self.assertAlmostEqual(gain_db(taps, 5_000, 48_000), -6.02, delta=0.1)
        for frequency in (5_550, 7_000, 20_000):
            self.assertLess(gain_db(taps, frequency, 48_000), -69.0)

    def test_shaped_follows_its_response(self):
        taps = shaped(lambda f: np.where(f < 3_000, 2.0, 0.5), 200, 48_000, 60)
        self.assertAlmostEqual(gain_db(taps, 1_000, 48_000), 20 * np.log10(2.0), delta=0.01)
        self.assertAlmostEqual(gain_db(taps, 10_000, 48_000), 20 * np.log10(0.5), delta=0.01)

    def test_kaiser_length_refuses_impossible_transitions(self):
        for transition in (0, -5, 24_000):
            with self.subTest(transition=transition), self.assertRaises(ValueError):
                kaiser_length(60, transition, 48_000)

    def test_smooth_numbers(self):
        self.assertEqual(smooth_numbers(30), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 16, 18, 20, 21,
                                              24, 25, 27, 28, 30])
        self.assertIn(1024, smooth_numbers(1024))

    def test_rate_ratio_is_exact_for_the_usual_rates(self):
        for rate in (250_000, 245_760, 960_000, 1_024_000, 1_400_000, 1_536_000, 1_792_000, 1_800_000,
                     1_920_000, 2_048_000, 2_160_000, 2_400_000, 2_560_000, 2_880_000, 3_200_000):
            with self.subTest(rate=rate):
                up, down = rate_ratio(rate, 48_000)
                self.assertEqual(rate * up / down, 48_000)
                self.assertLessEqual(down, 1024)

    def test_rate_ratio_comes_close_for_other_rates(self):
        for rate in (2_345_678, 1_000_003, 3_199_999):
            with self.subTest(rate=rate):
                up, down = rate_ratio(rate, 48_000)
                self.assertAlmostEqual(rate * up / down / 48_000, 1.0, delta=0.005)
        with self.assertRaises(ValueError):
            rate_ratio(24_000, 48_000)


class FftFilterTest(unittest.TestCase):
    def test_filters_like_a_convolution_whatever_the_chunks(self):
        taps = RNG.standard_normal(301)
        samples = complex_noise(20_000)
        expected = np.convolve(samples.astype(np.complex128), taps)[:len(samples)]
        for cuts in ([], [1, 2, 3, 4000, 4001, 15000], list(range(0, 20_000, 777))):
            with self.subTest(cuts=cuts[:3]):
                output = in_chunks(FftFilter(taps, 1.0), samples, cuts)
                self.assertEqual(len(output), len(samples))
                np.testing.assert_allclose(output, expected, atol=1e-4 * np.abs(expected).max())

    def test_lowers_the_rate_like_keeping_every_nth_sample(self):
        taps = lowpass(0.08, 0.04, 1.0)
        samples = complex_noise(20_003)
        output = in_chunks(FftFilter(taps, 1.0, down=4), samples, [5, 999, 1003, 12_001])
        self.assertEqual(len(output), 20_003 // 4)
        expected = np.convolve(samples.astype(np.complex128), taps)[:len(samples)][::4]
        np.testing.assert_allclose(output, expected[:len(output)], atol=1e-3 * np.abs(expected).max())

    def test_shifts_down_like_a_mixer_up_to_a_constant_phase(self):
        rate = 2_400_000.0
        taps = lowpass(20_000, 8_000, rate)
        t = np.arange(240_000)
        # The last tone lies past the band's upper edge: it wraps around like in any mixer.
        for shift, offset in ((499_875.0, 1234.5), (500_000.0, 1234.5), (-731_234.5, 1234.5),
                              (1_195_000.0, 10_000.0)):
            with self.subTest(shift=shift):
                samples = np.exp(2j * np.pi * (shift + offset) * t / rate).astype(np.complex64)
                filter_ = FftFilter(taps, rate, down=50)
                filter_.set_shift(shift)
                output = in_chunks(filter_, samples, range(0, len(t), 7_919))
                mixed = samples.astype(np.complex128) * np.exp(-2j * np.pi * shift * t / rate)
                expected = np.convolve(mixed, taps)[:len(t)][::50][:len(output)]
                ratio = output[100:] / expected[100:]
                np.testing.assert_allclose(np.abs(ratio), 1.0, atol=1e-4)
                self.assertLess(np.ptp(np.unwrap(np.angle(ratio))), 1e-3)

    def test_changes_the_rate_by_a_fraction_cleanly(self):
        rate = 2_048_000.0
        up, down = rate_ratio(rate, 48_000)
        self.assertEqual((up, down), (3, 128))
        filter_ = FftFilter(lowpass(20_000, 8_000, rate), rate, up=up, down=down)
        filter_.set_shift(-300_000)
        t = np.arange(400_000)
        samples = np.exp(2j * np.pi * (-300_000 + 2_000) * t / rate).astype(np.complex64)
        output = in_chunks(filter_, samples, range(0, len(t), 7_547))
        self.assertEqual(len(output), len(t) * 3 // 128)
        steady = output[3_000:]
        np.testing.assert_allclose(np.abs(steady), 1.0, atol=1e-3)
        self.assertLess(tone_purity_db(steady), -60.0)
        spectrum = np.abs(np.fft.fft(steady * np.blackman(len(steady))))
        self.assertAlmostEqual(abs(np.fft.fftfreq(len(steady), 1 / 48_000)[np.argmax(spectrum)]), 2_000, delta=10)

    def test_new_taps_keep_the_remembered_samples(self):
        taps = lowpass(0.1, 0.05, 1.0)
        samples = complex_noise(10_000)
        steady, changed = FftFilter(taps, 1.0), FftFilter(taps, 1.0)
        first = np.concatenate((steady.process(samples[:4_000]), steady.process(samples[4_000:])))
        part = changed.process(samples[:4_000])
        changed.set_taps(taps)
        np.testing.assert_allclose(np.concatenate((part, changed.process(samples[4_000:]))), first, atol=1e-6)
        longer = FftFilter(taps, 1.0)
        longer.process(samples[:4_000])
        longer.set_taps(lowpass(0.1, 0.01, 1.0))
        self.assertEqual(len(longer.process(samples[4_000:])), 6_000)

    def test_refuses_raising_the_rate_and_empty_filters(self):
        with self.assertRaises(ValueError):
            FftFilter(np.ones(3), 1.0, up=2, down=1)
        with self.assertRaises(ValueError):
            FftFilter(np.ones(0), 1.0)


if __name__ == "__main__":
    unittest.main()
