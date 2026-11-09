"""Tests for the synthetic band: determinism, continuity, levels and pacing."""

import asyncio
import time
import unittest

import numpy as np

from radiolack.spectrum import SpectrumAnalyzer
from radiolack.synthetic import (CENTRE_HZ, NOISE_DBFS, RAMP_S, SAMPLE_RATE_HZ, Emitter, Schedule,
                                 SyntheticBand, default_scene, morse, traffic)

ALWAYS = Schedule(((0.0, 1e9),), 2e9)
RAMP = round(RAMP_S * SAMPLE_RATE_HZ)       # samples of a transmission's rising edge


def quiet_band(*emitters: Emitter) -> SyntheticBand:
    return SyntheticBand(seed=1, emitters=list(emitters), noise_dbfs=None)


class SyntheticBandTest(unittest.TestCase):
    def test_same_seed_gives_the_same_samples(self):
        first, second = SyntheticBand(seed=7), SyntheticBand(seed=7)
        for _ in range(3):
            np.testing.assert_array_equal(first.generate(10_000), second.generate(10_000))

    def test_noise_power(self):
        block = SyntheticBand(seed=1, emitters=[]).generate(200_000)
        self.assertEqual(block.dtype, np.complex64)
        self.assertAlmostEqual(10 * np.log10(np.mean(np.abs(block) ** 2)), NOISE_DBFS, delta=0.2)

    def test_carrier_keeps_its_level_and_phase_across_blocks(self):
        offset_hz = 250_000.0
        band = quiet_band(Emitter(CENTRE_HZ + offset_hz, -20.0, ALWAYS, "cw"))
        band.generate(RAMP)
        samples = np.concatenate([band.generate(5_000) for _ in range(3)])
        np.testing.assert_allclose(np.abs(samples), 0.1, rtol=1e-4)
        steps = np.angle(samples[1:] * np.conj(samples[:-1]))
        np.testing.assert_allclose(steps, 2 * np.pi * offset_hz / SAMPLE_RATE_HZ, atol=1e-4)

    def test_fm_frequency_has_no_jump_between_blocks(self):
        voice = default_scene(np.random.default_rng(0))[0]
        emitter = Emitter(voice.frequency_hz, -20.0, ALWAYS, "fm", voice.tones, voice.phases,
                          True, voice.ctcss_hz)
        band = quiet_band(emitter)
        samples = np.concatenate([band.generate(4_000) for _ in range(4)])
        frequency = np.angle(samples[1:] * np.conj(samples[:-1]))
        self.assertLess(np.max(np.abs(np.diff(frequency))), 1e-3)

    def test_block_sizes_do_not_change_the_band(self):
        # Without voice-like level changes, which are interpolated per block,
        # the samples depend on time only: keyed and modulated emitters included.
        scene = [e for e in default_scene(np.random.default_rng(5)) if not e.syllabic]
        whole = quiet_band(*scene)
        split = quiet_band(*scene)
        for _ in range(30):     # 0.2 s, through beacon keying edges
            expected = whole.generate(16_000)
            actual = np.concatenate([split.generate(10_000), split.generate(6_000)])
            np.testing.assert_allclose(actual, expected, atol=2e-5)

    def test_emitter_levels_in_the_spectrum(self):
        offset_hz = 100 * SAMPLE_RATE_HZ / 8192     # on a bin centre
        band = SyntheticBand(seed=2, emitters=[Emitter(CENTRE_HZ + offset_hz, -40.0, ALWAYS, "cw")])
        band.generate(RAMP)
        power = SpectrumAnalyzer().power(band.generate(4 * 8192))
        self.assertEqual(int(np.argmax(power)), 4096 + 100)
        self.assertAlmostEqual(10 * np.log10(power.max()), -40.0, delta=0.2)
        self.assertAlmostEqual(10 * np.log10(np.median(power)), NOISE_DBFS + 10 * np.log10(2.0044 / 8192),
                               delta=0.5)

    def test_rejects_block_sizes_out_of_range(self):
        band = quiet_band()
        for count in (0, 1 << 20):
            with self.subTest(count=count), self.assertRaises(ValueError):
                band.generate(count)


class ScheduleTest(unittest.TestCase):
    def test_spans_repeat_with_the_period(self):
        schedule = Schedule(((1.0, 2.0), (5.0, 9.5)), 10.0)
        self.assertEqual(schedule.spans(0.0, 1.0), [])
        self.assertEqual(schedule.spans(1.5, 1.6), [(1.0, 2.0)])
        self.assertEqual(schedule.spans(9.0, 11.5), [(5.0, 9.5), (11.0, 12.0)])
        self.assertEqual(schedule.spans(2.0, 5.0), [])

    def test_morse_timing(self):
        schedule = morse("V", 12.0, 3.0, 1.0)       # dot = 0.1 s
        starts_and_lengths = [(round(a, 6), round(b - a, 6)) for a, b in schedule.intervals]
        self.assertEqual(starts_and_lengths, [(0.0, 0.1), (0.2, 0.1), (0.4, 0.1), (0.6, 0.3), (1.2, 3.0)])
        self.assertAlmostEqual(schedule.period_s, 5.2)

    def test_traffic_is_sorted_disjoint_and_inside_the_period(self):
        schedule = traffic(np.random.default_rng(4), 900.0)
        self.assertTrue(schedule.intervals)
        previous_stop = 0.0
        for start, stop in schedule.intervals:
            self.assertGreaterEqual(start - previous_stop, 2 * RAMP_S)
            self.assertLess(start, stop)
            previous_stop = stop
        self.assertLess(previous_stop, 900.0)


class PacingTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_returns_samples_at_the_sample_rate(self):
        band = SyntheticBand(seed=1, emitters=[], sample_rate_hz=100_000.0)
        started = time.monotonic()
        for _ in range(4):
            block = await band.read(5_000)       # 50 ms each
        self.assertEqual(len(block), 5_000)
        self.assertGreaterEqual(time.monotonic() - started, 0.19)

    async def test_late_reader_restarts_instead_of_catching_up(self):
        band = SyntheticBand(seed=1, emitters=[], sample_rate_hz=100_000.0)
        await band.read(5_000)
        await asyncio.sleep(0.5)                 # far behind the band
        started = time.monotonic()
        await band.read(5_000)
        self.assertGreaterEqual(time.monotonic() - started, 0.045)


if __name__ == "__main__":
    unittest.main()
