"""Tests for the port of rtl-sdr's two PLL computations (tests/r82xx_pll.py),
on values derived by hand from the driver's arithmetic: a 28.8 MHz crystal,
2X = 57.6 MHz, and a 65536th of it, 878.90625 Hz, for the VCO's fraction."""

import unittest

from tests import r82xx_pll
from tests.r82xx_pll import (driver_xtal_hz, intermediate_hz, mix_divider, oscillator_hz, rate_if_hz,
                             rounding_lo_hz, truncating_lo_hz, tuned_hz)


class OscillatorTest(unittest.TestCase):
    def test_both_are_exact_on_a_vco_that_is_a_multiple_of_twice_the_crystal(self):
        # 144 MHz: divider 16 (144,000 kHz × 8 is below 1.77 GHz, × 16 is
        # not), VCO 2.304 GHz = 40 × 57.6 MHz: nint 40, nothing left, sdm 0.
        # Also a multiple of 112.5 kHz.
        self.assertEqual(mix_divider(144_000_000), 16)
        self.assertEqual(truncating_lo_hz(144_000_000), 144_000_000)
        self.assertEqual(rounding_lo_hz(144_000_000), 144_000_000)

    def test_both_take_the_same_fraction_when_truncation_lands_on_the_rounding(self):
        # 145 MHz at 2.56 MS/s, IF 3.57 MHz: oscillator 148.57 MHz, VCO
        # 2,377.12 MHz = 41 × 57.6 MHz + 15,520 kHz.
        # Truncating: 15,520 kHz takes 14,400 (sdm 16384), 900 (1024),
        # 112 (128), 56 (64), 28 (32), 14 (16), 7 (8) and 1 (2), leaving
        # 2 kHz: sdm 17658. Rounding: 15,520,000 / 878.90625 = 17658.31,
        # sdm 17658 too. The VCO falls 0.31 × 878.90625 = 273.4375 Hz short,
        # 273.4375 / 16 = 17.08984375 Hz on the oscillator.
        for computation in (truncating_lo_hz, rounding_lo_hz):
            with self.subTest(computation=computation.__name__):
                self.assertEqual(computation(148_570_000), 148_570_000 - 17.08984375)

    def test_truncation_drops_the_whole_kilohertz_that_rounding_keeps(self):
        # 144,000,096 Hz: VCO 2,304,001,536 Hz, 1536 Hz above 40 × 57.6 MHz.
        # Truncating: 1 kHz left, not above 1, so sdm 0: the oscillator
        # falls 1536 / 16 = 96 Hz short. Rounding: 1536 / 878.90625 = 1.75,
        # sdm 2, 1757.8125 Hz: 221.8125 / 16 = 13.86328125 Hz above.
        self.assertEqual(truncating_lo_hz(144_000_096), 144_000_096 - 96)
        self.assertEqual(rounding_lo_hz(144_000_096), 144_000_096 + 13.86328125)

    def test_on_a_multiple_of_112_5_khz_only_rounding_is_exact(self):
        # 144.1125 MHz: VCO 2,305.8 MHz, 1800 kHz above 40 × 57.6 MHz, which
        # is 2048 × 878.90625 Hz: rounding is exact. Truncating: 1800 kHz is
        # not above the 1800 kHz bit (n_sdm 32), so it takes the smaller
        # ones, 900, 450, 225, 112, 56, 28, 14, 7, 3 and 1 kHz: sdm
        # 1024 + 512 + 256 + 128 + 64 + 32 + 16 + 8 + 4 + 2 = 2046, two
        # 65536ths short: 1757.8125 / 16 = 109.86328125 Hz low.
        self.assertEqual(rounding_lo_hz(144_112_500), 144_112_500)
        self.assertEqual(truncating_lo_hz(144_112_500), 144_112_500 - 109.86328125)

    def test_rounding_stays_within_half_a_step_and_is_exact_on_every_multiple_of_112_5_khz(self):
        for lo_hz in range(28_125_000, 1_769_000_000, 112_500 * 97):
            with self.subTest(lo_hz=lo_hz):
                self.assertEqual(rounding_lo_hz(lo_hz), lo_hz)
                off = lo_hz + 12_345
                bound = 2 * r82xx_pll.XTAL_HZ / r82xx_pll.SDM_STEPS / 2 / mix_divider(off)
                self.assertLessEqual(abs(rounding_lo_hz(off) - off), bound)

    def test_the_divider_follows_the_vco_range_and_refuses_beyond_it(self):
        self.assertEqual(mix_divider(438_570_000), 8)       # 438,570 kHz × 8 = 3.51 GHz
        self.assertEqual(mix_divider(1_299_570_000), 2)
        self.assertEqual(mix_divider(27_700_000), 64)
        for lo_hz in (27_600_000, 1_771_000_000):       # VCO below 1.77 GHz × 64, above 3.54 GHz × 2
            with self.subTest(lo_hz=lo_hz), self.assertRaises(ValueError):
                truncating_lo_hz(lo_hz)


class DriverTest(unittest.TestCase):
    def test_the_if_follows_the_sample_rate(self):
        # Above 1.7 + 0.35 + 0.38 = 2.43 MHz: 3.57 MHz. At 2.4 MS/s: 2.3 MHz,
        # + 0.38 (2.4 > 2.05; 2.02 left), + 0.35 (2.02 > 1.7; 1.67 left), the
        # first filter not above 1.67 MHz being 1.7 MHz: 3.03 − (0.38 + 0.35
        # + 1.7) / 2 = 1.815 MHz. At 1.024 MS/s: no step, the 1.2 MHz
        # filter: 2.3 − 0.6 = 1.7 MHz. At 240 kS/s, below every filter, the
        # narrowest, 350 kHz: 2.125 MHz.
        self.assertEqual(rate_if_hz(2_560_000), 3_570_000)
        self.assertEqual(rate_if_hz(2_400_000), 1_815_000)
        self.assertEqual(rate_if_hz(1_024_000), 1_700_000)
        self.assertEqual(rate_if_hz(240_000), 2_125_000)
        self.assertEqual(rate_if_hz(8_000_000), 4_570_000)
        self.assertEqual(intermediate_hz(2_400_000), 1_815_000)     # with the rate's bandwidth by default

    def test_a_tuner_just_started_again_runs_at_3_57_mhz_whatever_the_rate(self):
        # r82xx_init's r82xx_set_tv_standard, until the next sample rate.
        for rate in (240_000, 1_024_000, 2_400_000, 2_560_000):
            with self.subTest(rate=rate):
                self.assertEqual(intermediate_hz(rate, "started"), 3_570_000)
                self.assertEqual(oscillator_hz(145_000_000, rate, "started"), 148_570_000)
        self.assertEqual(oscillator_hz(145_000_000, 2_400_000), 146_815_000)
        with self.assertRaisesRegex(ValueError, "rate, started"):
            intermediate_hz(2_400_000, "asleep")

    def test_the_driver_takes_the_crystal_as_corrected(self):
        self.assertEqual(driver_xtal_hz(0), 28_800_000)
        self.assertEqual(driver_xtal_hz(-3), 28_799_913)        # 28,799,913.6, truncated
        self.assertEqual(driver_xtal_hz(37), 28_801_065)

    def test_the_tuned_frequency_is_the_oscillator_less_the_if(self):
        asked = 144_000_096 - 3_570_000
        self.assertEqual(tuned_hz("truncating", asked, 2_560_000), asked - 96)
        self.assertEqual(tuned_hz("rounding", asked, 2_560_000), asked + 13.86328125)
        self.assertEqual(tuned_hz("rounding", 144_000_000 - 1_815_000, 2_400_000), 144_000_000 - 1_815_000)
        # Just started again, the tuner takes 3.57 MHz at 2.4 MS/s too.
        self.assertEqual(tuned_hz("truncating", asked, 2_400_000, state="started"), asked - 96)


if __name__ == "__main__":
    unittest.main()
