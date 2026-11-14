"""Tests for the display spectrum: levels, frequencies, the noise level
around the VFO or beyond a wide channel, the frame format and frames in
fewer bins."""

import math
import unittest

import numpy as np

from radiolack.spectrum import (DB_MIN, DB_STEP, FLAG_SQUELCH_OPEN, FRAME_HEADER, FRAME_KIND_SPECTRUM,
                                FRAME_VERSION, SpectrumAnalyzer, blackman_harris, encode_frame, fewer_bins)
from tests.fakes import wide_signal

RATE = 2_400_000.0
SIZE = 1024


def tone(frequency_hz: float, amplitude: float, count: int) -> np.ndarray:
    t = np.arange(count) / RATE
    return (amplitude * np.exp(2j * np.pi * frequency_hz * t)).astype(np.complex64)


class SpectrumAnalyzerTest(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpectrumAnalyzer(fft_size=SIZE, ffts_per_frame=4)

    def test_tone_on_a_bin_centre_reads_its_power_in_its_bin(self):
        for offset_bins in (-300, 0, 17, 511):
            with self.subTest(offset_bins=offset_bins):
                power = self.analyzer.power(tone(offset_bins * RATE / SIZE, 0.5, 4 * SIZE))
                peak = int(np.argmax(power))
                self.assertEqual(peak, SIZE // 2 + offset_bins)
                self.assertAlmostEqual(10 * np.log10(power[peak]), 20 * np.log10(0.5), delta=0.05)

    def test_white_noise_reads_its_power_per_bin(self):
        variance = 1e-4
        rng = np.random.default_rng(3)
        noise = (rng.standard_normal(2 * 64 * SIZE) * np.sqrt(variance / 2)).astype(np.float32)
        power = SpectrumAnalyzer(fft_size=SIZE, ffts_per_frame=64).power(noise.view(np.complex64))
        window = blackman_harris(SIZE).astype(np.float64)
        per_bin = variance * (window ** 2).sum() / window.sum() ** 2
        self.assertAlmostEqual(10 * np.log10(power.mean()), 10 * np.log10(per_bin), delta=0.3)

    def test_frame_uses_the_last_ffts_of_a_block(self):
        block = np.zeros(10 * SIZE, np.complex64)
        block[-4 * SIZE:] = tone(0.0, 1.0, 4 * SIZE)
        power = self.analyzer.power(block)
        self.assertAlmostEqual(10 * np.log10(power[SIZE // 2]), 0.0, delta=0.05)

    def test_codes_quantize_and_clip(self):
        db = np.array([-200.0, DB_MIN, DB_MIN + 10 * DB_STEP, -0.3, 10.0])
        codes = self.analyzer.codes(10 ** (db / 10))
        self.assertEqual(codes.dtype, np.uint8)
        self.assertEqual(codes.tolist(), [0, 0, 10, 255, 255])

    def test_frame_has_one_code_per_bin(self):
        codes = self.analyzer.codes(self.analyzer.power(tone(0.0, 0.1, 5 * SIZE)))
        self.assertEqual((codes.dtype, codes.shape), (np.uint8, (SIZE,)))

    def test_rejects_bad_settings_and_short_blocks(self):
        for settings in ({"fft_size": 1000}, {"fft_size": 8}, {"ffts_per_frame": 0}, {"db_step": 0}):
            with self.subTest(**settings), self.assertRaises(ValueError):
                SpectrumAnalyzer(**settings)
        with self.assertRaises(ValueError):
            self.analyzer.power(np.zeros(SIZE - 1, np.complex64))


class NoiseDensityTest(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpectrumAnalyzer()
        density = 1e-6 / RATE
        rng = np.random.default_rng(5)
        count = 4 * self.analyzer.fft_size
        noise = (rng.standard_normal(2 * count) * math.sqrt(density * RATE / 2)).astype(np.float32)
        noise = noise.view(np.complex64) + tone(300_000.0, 0.01, count)     # a signal does not move it
        self.power = self.analyzer.power(noise)
        self.density = density

    def test_reads_the_noise_power_per_hertz_around_the_vfo(self):
        for offset in (300_000.0, -1_000_000.0, RATE / 2 - 10_000.0):
            with self.subTest(offset=offset):
                estimate = self.analyzer.noise_density(self.power, 4 * self.analyzer.fft_size, offset, RATE)
                self.assertAlmostEqual(10 * math.log10(estimate / self.density), 0.0, delta=0.3)

    def test_needs_bins_around_the_vfo(self):
        for offset in (RATE, -RATE):        # above and below the span
            with self.subTest(offset=offset):
                self.assertIsNone(self.analyzer.noise_density(self.power, 4 * self.analyzer.fft_size, offset, RATE))


class WideChannelNoiseTest(unittest.TestCase):
    """Noise of -60 dBFS across the band and wide signals 20 dB above it,
    150 kHz wide, as broadcast FM stations: in WFM, the noise is read beyond
    the channel's edges, 90 kHz from the VFO with a 180 kHz filter."""

    def setUp(self):
        self.analyzer = SpectrumAnalyzer()
        self.count = 4 * self.analyzer.fft_size
        self.density = 1e-6 / RATE
        self.rng = np.random.default_rng(9)

    def power(self, *stations: float) -> np.ndarray:
        """The bin powers of the noise and a station at each offset of ``stations``."""
        samples = (self.rng.standard_normal(self.count) + 1j * self.rng.standard_normal(self.count)) * math.sqrt(1e-6 / 2)
        for offset in stations:
            samples = samples + wide_signal(self.count, RATE, offset, 100 * self.density, self.rng)
        return self.analyzer.power(samples.astype(np.complex64))

    def reading_db(self, power: np.ndarray, offset: float, clearance: float) -> float:
        """The noise estimate at ``offset``, from the noise density, in dB."""
        estimate = self.analyzer.noise_density(power, self.count, offset, RATE, clearance)
        return 10 * math.log10(estimate / self.density)

    def test_a_wide_channel_is_read_beyond_its_edges(self):
        power = self.power(300_000.0)
        self.assertGreater(self.reading_db(power, 300_000.0, 0.0), 10.0)    # the station itself
        self.assertAlmostEqual(self.reading_db(power, 300_000.0, 90_000.0), 0.0, delta=0.5)

    def test_on_the_quieter_side_when_a_neighbour_fills_the_other(self):
        for neighbour in (500_000.0, 100_000.0):
            with self.subTest(neighbour=neighbour):
                power = self.power(300_000.0, neighbour)
                self.assertAlmostEqual(self.reading_db(power, 300_000.0, 90_000.0), 0.0, delta=0.5)

    def test_one_side_is_enough_and_none_is_too_few(self):
        edge = RATE / 2 - 80_000.0              # the bins above the channel lie past the span
        power = self.power(edge)
        self.assertAlmostEqual(self.reading_db(power, edge, 90_000.0), 0.0, delta=0.5)
        for offset in (RATE, -RATE):
            self.assertIsNone(self.analyzer.noise_density(power, self.count, offset, RATE, 90_000.0))


class FrameEncodingTest(unittest.TestCase):
    def test_header_then_codes(self):
        codes = np.arange(8, dtype=np.uint8)
        frame = encode_frame(2 ** 32 + 5, 145e6, 2.4e6, -160.0, 0.625, codes)
        self.assertEqual((FRAME_HEADER.size, FRAME_VERSION), (40, 2))
        self.assertEqual(len(frame), 40 + 8)
        *header, level, squelch = FRAME_HEADER.unpack_from(frame)
        self.assertEqual(header, [FRAME_KIND_SPECTRUM, FRAME_VERSION, 0, 5, 145e6, 2.4e6, -160.0, 0.625])
        self.assertTrue(math.isnan(level) and math.isnan(squelch))   # unknown
        self.assertEqual(frame[40:], codes.tobytes())

    def test_header_carries_the_channel_meter(self):
        frame = encode_frame(1, 145e6, 2.4e6, -160.0, 0.625, np.zeros(4, np.uint8), -52.25, -60.5, True)
        self.assertEqual(FRAME_HEADER.unpack_from(frame)[2], FLAG_SQUELCH_OPEN)
        self.assertEqual(FRAME_HEADER.unpack_from(frame)[8:], (-52.25, -60.5))


class FewerBinsTest(unittest.TestCase):
    def test_each_bin_keeps_the_highest_code_it_groups(self):
        codes = np.full(16, 40, np.uint8)
        codes[5] = 200          # a narrow signal, in the second group of four
        codes[15] = 90
        self.assertEqual(fewer_bins(codes, 4).tolist(), [40, 200, 40, 90])
        self.assertEqual(fewer_bins(codes, 2).tolist(), [200, 90])

    def test_fewer_codes_stay_as_they_are(self):
        codes = np.arange(8, dtype=np.uint8)
        for bins in (8, 16):
            with self.subTest(bins=bins):
                self.assertIs(fewer_bins(codes, bins), codes)


if __name__ == "__main__":
    unittest.main()
