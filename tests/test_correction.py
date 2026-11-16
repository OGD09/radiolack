"""Tests for the frequency correction in hundredths of ppm and its split
between the receiver and Radiolack."""

import unittest

from radiolack.correction import checked_ppm, corrected_hz, whole_ppm


class CheckedPpmTest(unittest.TestCase):
    def test_keeps_hundredths_and_reads_a_whole_number_as_it_is(self):
        for value, ppm in ((-4, -4.0), (0, 0.0), (37.42, 37.42), (-2.6, -2.6), (1.004, 1.0), (-0.006, -0.01),
                           (100, 100.0), (-100, -100.0), (100.004, 100.0), (-100.004, -100.0)):
            with self.subTest(value=value):
                self.assertEqual(checked_ppm(value), ppm)

    def test_refuses_anything_else(self):
        for value in (100.01, -100.006, 101, 10**400, float("nan"), float("inf"), -float("inf"),
                      "5", None, True, False, [1]):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "^the frequency correction must be a number from -100 to 100 ppm$"):
                checked_ppm(value)


class WholePpmTest(unittest.TestCase):
    def test_the_receiver_takes_the_nearest_whole_number_the_halves_away_from_zero(self):
        for ppm, whole in ((0.0, 0), (0.49, 0), (0.5, 1), (-0.49, 0), (-0.5, -1), (2.5, 3), (-2.5, -3),
                           (37.42, 37), (-52.73, -53), (99.49, 99), (99.5, 100), (-99.5, -100),
                           (100.0, 100), (-100.0, -100)):
            with self.subTest(ppm=ppm):
                self.assertEqual(whole_ppm(ppm, True), whole)

    def test_a_receiver_without_a_correction_of_its_own_takes_none(self):
        for ppm in (0.0, 0.5, -37.42, 100.0):
            with self.subTest(ppm=ppm):
                self.assertEqual(whole_ppm(ppm, False), 0)


class CorrectedHzTest(unittest.TestCase):
    def test_a_whole_correction_asks_for_the_frequency_itself(self):
        for tuned_hz in (1, 7_100_000, 145_000_000, 1_766_000_000, 0xFFFF_FFFF):
            for ppm in (-100, -4, 0, 7, 100):
                with self.subTest(tuned_hz=tuned_hz, ppm=ppm):
                    self.assertEqual(corrected_hz(tuned_hz, float(ppm), ppm), tuned_hz)

    def test_the_rest_scales_the_frequency_asked(self):
        # A crystal 0.42 ppm faster than the receiver takes it: a lower request.
        self.assertEqual(corrected_hz(145_000_000, 37.42, 37), round(145e6 * (1 + 37e-6) / (1 + 37.42e-6)))
        self.assertEqual(corrected_hz(145_000_000, 37.42, 37), 144_999_939)
        self.assertEqual(corrected_hz(145_000_000, -2.6, -3), 144_999_942)
        self.assertEqual(corrected_hz(7_100_000, 0.4, 0), 7_099_997)
        # Without a correction of the receiver's own, the whole of it.
        self.assertEqual(corrected_hz(145_000_000, 10.0, 0), 144_998_550)

    def test_the_tuned_frequency_follows_within_half_a_hertz(self):
        # What a crystal ppm off truly tunes for the request: the frequency wanted.
        for tuned_hz in (500_000, 7_100_000, 145_000_000, 435_123_456, 1_766_000_000):
            for ppm in (-100.0, -52.73, -0.5, -0.01, 0.01, 0.49, 37.42, 99.5, 100.0):
                for hardware in (True, False):
                    whole = whole_ppm(ppm, hardware)
                    request = corrected_hz(tuned_hz, ppm, whole)
                    truly = request * (1 + ppm / 1e6) / (1 + whole / 1e6)
                    with self.subTest(tuned_hz=tuned_hz, ppm=ppm, hardware=hardware):
                        self.assertLessEqual(abs(truly - tuned_hz), 0.5 + 1e-6)


if __name__ == "__main__":
    unittest.main()
