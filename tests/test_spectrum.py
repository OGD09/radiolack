"""Tests for the display spectrum: levels, frequencies and the frame format."""

import unittest

import numpy as np

from radiolack.spectrum import (DB_MIN, DB_STEP, FRAME_HEADER, FRAME_KIND_SPECTRUM, FRAME_VERSION,
                                SpectrumAnalyzer, blackman_harris, encode_frame)

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
        codes = self.analyzer.frame(tone(0.0, 0.1, 5 * SIZE))
        self.assertEqual((codes.dtype, codes.shape), (np.uint8, (SIZE,)))

    def test_rejects_bad_settings_and_short_blocks(self):
        for settings in ({"fft_size": 1000}, {"fft_size": 8}, {"ffts_per_frame": 0}, {"db_step": 0}):
            with self.subTest(**settings), self.assertRaises(ValueError):
                SpectrumAnalyzer(**settings)
        with self.assertRaises(ValueError):
            self.analyzer.frame(np.zeros(SIZE - 1, np.complex64))


class FrameEncodingTest(unittest.TestCase):
    def test_header_then_codes(self):
        codes = np.arange(8, dtype=np.uint8)
        frame = encode_frame(2 ** 32 + 5, 145e6, 2.4e6, -160.0, 0.625, codes)
        self.assertEqual(FRAME_HEADER.size, 32)
        self.assertEqual(len(frame), 32 + 8)
        self.assertEqual(FRAME_HEADER.unpack_from(frame),
                         (FRAME_KIND_SPECTRUM, FRAME_VERSION, 0, 5, 145e6, 2.4e6, -160.0, 0.625))
        self.assertEqual(frame[32:], codes.tobytes())


if __name__ == "__main__":
    unittest.main()
