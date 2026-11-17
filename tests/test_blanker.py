"""Tests for the noise blanker (demod.py: ``NoiseBlanker`` and the
demodulation settings' ``blanker``) on synthetic, seeded signals: the
impulses zeroed with their sides, across block borders or not, nothing
copied without them, the strong signals that must never be blanked, the
blocks of silence, of constants and shorter than an impulse's reach, the
count of the latest second; then through the real path, in every mode: the
audio's noise from impulses gone, a signal without them unchanged sample
for sample, the measure's tap, the display and detection untouched."""

import dataclasses
import math
import unittest

import numpy as np

from radiolack.carrier import CarrierMeasure, strongest_line
from radiolack.demod import (BLANKER_LONGEST, BLANKER_MOST, BLANKER_SIDES, MODES, DemodSettings, Demodulator,
                             NoiseBlanker, passband_hz, vfo_position_hz)
from radiolack.engine import _process_block
from radiolack.scanners import DETECTION_BLOCKS, ChannelWatch, Detector
from radiolack.spectrum import SpectrumAnalyzer
from tests.test_demod import BLOCK, CENTRE, OFFSET, RATE, WFM, am, carrier, fm, noise, open_in, run

IMPULSE_PERIOD = 24_000     # samples: 100 impulses a second at 2.4 MS/s
IMPULSE_SAMPLES = 3
IMPULSE_AMPLITUDE = 0.5     # -6 dBFS


def impulses(count: int, period: int = IMPULSE_PERIOD, first: int = 1_000, length: int = IMPULSE_SAMPLES,
             amplitude: float = IMPULSE_AMPLITUDE) -> np.ndarray:
    """``count`` samples holding only impulses of ``length`` samples, one
    every ``period`` from ``first``, each at a phase of its own, at random:
    their spectrum is flat, as crackle's, without lines."""
    samples = np.zeros(count, np.complex64)
    starts = range(first, count, period)
    phases = np.random.default_rng(first).uniform(0, 2 * np.pi, len(starts))
    for at, phase in zip(starts, phases):
        samples[at:at + length] = amplitude * np.exp(1j * phase)
    return samples


def weak_noise(count: int, rms: float = 0.01, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count)) * rms / math.sqrt(2)).astype(np.complex64)


def blocks(samples: np.ndarray, size: int = BLOCK):
    return [samples[start:start + size] for start in range(0, len(samples), size)]


def blank(samples: np.ndarray, size: int = BLOCK, blanker: NoiseBlanker | None = None) -> np.ndarray:
    """``samples`` through a blanker, block by block."""
    blanker = blanker if blanker is not None else NoiseBlanker(RATE)
    return np.concatenate([blanker.process(block) for block in blocks(samples, size)])


def blanked_settings(settings: DemodSettings) -> DemodSettings:
    return dataclasses.replace(settings, blanker=True)


# A signal on the VFO for each mode, at -40 dBFS: what each one hears.
SIGNALS = {
    "NFM": lambda seconds: fm(seconds, -40.0, [(1_000.0, 3_000.0)]),
    "AM": lambda seconds: am(seconds, -40.0, 0.5),
    "SAM": lambda seconds: am(seconds, -40.0, 0.5),
    "USB": lambda seconds: carrier(seconds, -40.0, 1_000.0),
    "LSB": lambda seconds: carrier(seconds, -40.0, -1_000.0),
    "CW": lambda seconds: carrier(seconds, -40.0),
    "WFM": lambda seconds: fm(seconds, -40.0, [(1_000.0, 37_500.0)]),
}


def settings_for(mode: str) -> DemodSettings:
    return WFM if mode == "WFM" else open_in(mode)


class ImpulseTest(unittest.TestCase):
    def test_impulses_go_with_two_samples_on_each_side_and_nothing_else(self):
        signal = weak_noise(20_000)
        hit = signal.copy()
        for at in (100, 5_000, 12_345):
            hit[at:at + 3] = 0.5
        blanker = NoiseBlanker(RATE)
        blanked = blanker.process(hit)
        expected = signal.copy()
        for at in (100, 5_000, 12_345):
            expected[at - 2:at + 5] = 0
        np.testing.assert_array_equal(blanked, expected)
        self.assertEqual(blanker.impulses, 3)
        self.assertEqual(hit[100], 0.5)         # the samples given stay as they were

    def test_a_block_without_impulses_is_given_back_as_it_is(self):
        t = np.arange(20_000) / RATE
        # Noise, a carrier far stronger than the noise, whose magnitude is the median, and silence.
        for signal in (weak_noise(20_000), (0.3 * np.exp(2j * np.pi * 1e5 * t)).astype(np.complex64) + weak_noise(20_000),
                       np.zeros(20_000, np.complex64), np.full(20_000, 0.3 - 0.2j, np.complex64),
                       np.zeros(0, np.complex64)):
            with self.subTest(rms=float(np.std(signal)) if len(signal) else None):
                blanker = NoiseBlanker(RATE)
                self.assertIs(blanker.process(signal), signal)
                self.assertEqual(blanker.impulses, 0)

    def test_an_impulse_at_a_blocks_end_reaches_into_the_next_but_not_back_from_a_blocks_start(self):
        first, second, third = weak_noise(1_000, seed=1), weak_noise(1_000, seed=2), weak_noise(1_000, seed=4)
        first[-1] = 0.5
        third[0] = 0.5
        blanker = NoiseBlanker(RATE)
        out = blanker.process(first)
        self.assertFalse(np.any(out[-3:]))
        np.testing.assert_array_equal(out[:-3], first[:-3])
        after = blanker.process(second)
        self.assertFalse(np.any(after[:2]))
        np.testing.assert_array_equal(after[2:], second[2:])
        self.assertEqual(blanker.impulses, 0)   # the sides of the previous block's impulse
        # At a block's start: its two samples after it go, the previous block's last two stay.
        self.assertIs(blanker.process(second), second)
        after = blanker.process(third)
        self.assertFalse(np.any(after[:3]))
        np.testing.assert_array_equal(after[3:], third[3:])
        self.assertEqual(blanker.impulses, 1)

    def test_blocks_shorter_than_an_impulses_reach(self):
        blanker = NoiseBlanker(RATE)
        block = weak_noise(1_000)
        block[-1] = 0.5
        blanker.process(block)
        # The two samples after it, over blocks of one sample, then nothing more.
        tiny = [weak_noise(1, seed=seed) for seed in (5, 6, 7)]
        self.assertEqual([blanker.process(sample)[0] for sample in tiny[:2]], [0, 0])
        self.assertIs(blanker.process(tiny[2]), tiny[2])
        # A block of four samples whose third is an impulse: its reach goes past both ends.
        short = np.array([0.01, 0.01j, 0.5, -0.01], np.complex64)
        self.assertFalse(np.any(blanker.process(short)))
        self.assertEqual(blanker.impulses, 1)
        follows = weak_noise(10, seed=8)
        out = blanker.process(follows)
        self.assertEqual(out[0], 0)         # the one sample of its reach past the end
        np.testing.assert_array_equal(out[1:], follows[1:])
        # A block of two samples has its median in its first: the second, six times as strong, goes.
        pair = np.array([0.01, 0.1], np.complex64)
        self.assertFalse(np.any(blanker.process(pair)))

    def test_silence_with_an_impulse_is_left_alone_and_a_constant_one_is_blanked(self):
        # The median of silence is 0: no limit, nothing blanked.
        silence = np.zeros(5_000, np.complex64)
        silence[100:103] = 0.5
        self.assertIs(NoiseBlanker(RATE).process(silence), silence)
        constant = np.full(5_000, 0.01 + 0.01j, np.complex64)
        constant[100:103] = 0.5
        out = NoiseBlanker(RATE).process(constant)
        self.assertFalse(np.any(out[98:105]))
        np.testing.assert_array_equal(out[105:], constant[105:])

    def test_never_gives_a_sample_that_is_not_a_number(self):
        rng = np.random.default_rng(12)
        blanker = NoiseBlanker(RATE)
        for index in range(50):
            block = weak_noise(8_000, rms=float(10 ** rng.uniform(-6, 0)), seed=index)
            at = rng.integers(0, len(block), 20)
            block[at] = (10 ** rng.uniform(-3, 30, len(at))).astype(np.float32) * np.exp(1j * rng.uniform(0, 7, len(at)))
            out = blanker.process(block)
            self.assertTrue(np.all(np.isfinite(out)))
            self.assertTrue(np.all((out == block) | (out == 0)))
        # Samples that are not numbers stay as they came, and make none.
        odd = weak_noise(1_000)
        odd[::2] = np.nan
        out = NoiseBlanker(RATE).process(odd)
        self.assertEqual(np.isnan(out).sum(), np.isnan(odd).sum())


class StrongSignalTest(unittest.TestCase):
    """What crosses six times the median and is no impulse: never blanked,
    or within a bound."""

    def test_a_strong_keyed_carrier_is_never_blanked(self):
        # Keyed on for 7 ms of every 23 ms, abruptly, far over the noise: most
        # blocks hold it for less than half their time, so their median is
        # the noise's, and every sample of the carrier crosses the limit.
        seconds = 2.0
        t = np.arange(round(seconds * RATE)) / RATE
        keyed = (t % 0.023) < 0.007
        signal = (0.5 * keyed * np.exp(2j * np.pi * 3e5 * t)).astype(np.complex64) + weak_noise(len(t))
        edges = np.flatnonzero(np.diff(keyed.astype(int))) + 1
        self.assertFalse(np.any(np.abs(edges % BLOCK - BLOCK / 2) > BLOCK / 2 - BLANKER_LONGEST - BLANKER_SIDES))
        blanker = NoiseBlanker(RATE)
        quiet = 0
        for block in blocks(signal):
            magnitude = np.abs(block)
            quiet += np.median(magnitude) < 0.05 and np.any(magnitude > 0.4)
            self.assertIs(blanker.process(block), block)
        self.assertGreater(quiet, 20)       # blocks where the carrier crosses the limit

    def test_a_strong_signal_starting_at_a_blocks_end_loses_no_more_than_the_bound(self):
        for before_end in (1, 5, BLANKER_LONGEST):
            with self.subTest(before_end=before_end):
                signal = weak_noise(3 * BLOCK)
                start = 2 * BLOCK - before_end
                signal[start:] += 0.5
                out = blank(signal)
                lost = np.flatnonzero(out != signal)
                np.testing.assert_array_equal(lost, np.arange(start - BLANKER_SIDES, 2 * BLOCK + BLANKER_SIDES))
                self.assertLessEqual(np.sum(lost >= start), BLANKER_LONGEST + BLANKER_SIDES)
        # One sample more and it is a signal: nothing is lost.
        signal = weak_noise(3 * BLOCK)
        signal[2 * BLOCK - BLANKER_LONGEST - 1:] += 0.5
        np.testing.assert_array_equal(blank(signal), signal)

    def test_a_strong_am_station_at_full_modulation_is_never_blanked(self):
        rng = np.random.default_rng(9)
        t = np.arange(round(1.0 * RATE)) / RATE
        # A tone, and a voice-like sound with peaks at full modulation.
        sound = sum(np.sin(2 * np.pi * k * 150 * t + rng.uniform(0, 7)) / k for k in range(1, 13))
        sound *= 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t) ** 2
        for modulation in (np.sin(2 * np.pi * 1_000 * t), sound / np.abs(sound).max()):
            station = (0.3 * (1 + modulation) * np.exp(2j * np.pi * 3e5 * t)).astype(np.complex64)
            signal = station + weak_noise(len(t))
            blanker = NoiseBlanker(RATE)
            for block in blocks(signal):
                self.assertIs(blanker.process(block), block)

    def test_a_wfm_station_and_its_neighbours_are_never_blanked(self):
        t = np.arange(round(1.0 * RATE)) / RATE
        stations = fm(1.0, -6.0, [(1_000.0, 75_000.0)])
        for offset_hz, level in ((-600_000.0, 0.2), (400_000.0, 0.1), (800_000.0, 0.05)):
            stations += (level * np.exp(2j * np.pi * offset_hz * t
                                        + 1j * 75_000 / 3_000 * np.sin(2 * np.pi * 3_000 * t))).astype(np.complex64)
        signal = stations + weak_noise(len(t))
        blanker = NoiseBlanker(RATE)
        for block in blocks(signal):
            self.assertIs(blanker.process(block), block)

    def test_impulses_too_dense_to_be_impulses_leave_the_block_whole(self):
        # Each zeroes 7 samples: one every 200 zeroes 3.5% of the block, one every 400 1.75%.
        for period, whole in ((200, True), (400, False)):
            with self.subTest(period=period):
                signal = weak_noise(BLOCK) + impulses(BLOCK, period, first=50)
                blanker = NoiseBlanker(RATE)
                out = blanker.process(signal)
                if whole:
                    self.assertIs(out, signal)
                    self.assertEqual(blanker.impulses, 0)
                else:
                    self.assertEqual(blanker.impulses, len(range(50, BLOCK, period)))
                    self.assertLessEqual(np.count_nonzero(out != signal), BLANKER_MOST * BLOCK)

    def test_what_crosses_the_limit_at_random_is_never_more_than_its_share(self):
        # A carrier just below the limit, for part of the block: the noise
        # takes some of its samples over it, in short runs.
        rng = np.random.default_rng(21)
        for index in range(40):
            block = weak_noise(BLOCK, seed=100 + index)
            start, length = rng.integers(0, BLOCK // 2), rng.integers(BLOCK // 50, BLOCK // 2)
            level = 0.01 * rng.uniform(3.0, 6.0)
            block[start:start + length] += level
            out = NoiseBlanker(RATE).process(block)
            self.assertLessEqual(np.count_nonzero(out != block), BLANKER_MOST * BLOCK)


class CountTest(unittest.TestCase):
    def test_counts_the_impulses_of_the_latest_second_and_changes_once_a_second_at_most(self):
        # 100 impulses a second, in step with the seconds; then 137, which are not.
        for period, rate in ((IMPULSE_PERIOD, 100), (17_518, 137)):
            with self.subTest(rate=rate):
                blanker = NoiseBlanker(RATE)
                counts = []
                for second in range(6):
                    signal = weak_noise(round(RATE), seed=second) + impulses(round(RATE), period,
                                                                           first=(1_000 - second * round(RATE)) % period)
                    for block in blocks(signal):
                        blanker.process(block)
                        counts.append(blanker.per_second)
                self.assertEqual(counts[:29], [0] * 29)
                changes = [index for index in range(1, len(counts)) if counts[index] != counts[index - 1]]
                self.assertTrue(all(index % 30 == 29 for index in changes), changes)
                self.assertTrue(all(abs(count - rate) <= 1 for count in counts[29:]))
                if rate == 100:
                    self.assertEqual(changes, [29])


class PathTest(unittest.TestCase):
    """Through the demodulator, in every mode."""

    def test_impulses_leave_the_audio_and_a_signal_without_them_is_unchanged(self):
        for mode, make in SIGNALS.items():
            with self.subTest(mode=mode):
                settings = settings_for(mode)
                clean = make(1.0) + weak_noise(round(RATE), rms=1e-3)      # -60 dBFS
                hit = clean + impulses(len(clean))
                reference = np.concatenate(run(clean, settings)[1])
                meters, unchanged = run(clean, blanked_settings(settings))
                np.testing.assert_array_equal(np.concatenate(unchanged), reference)
                self.assertEqual({meter.blanker_impulses for meter in meters}, {0})
                plain = np.concatenate(run(hit, settings)[1]) - reference
                meters, blanked = run(hit, blanked_settings(settings))
                blanked = np.concatenate(blanked) - reference
                skip = len(reference) // 10       # the filters' start
                plain_db = 10 * math.log10(np.mean(plain[skip:] ** 2))
                blanked_db = 10 * math.log10(np.mean(blanked[skip:] ** 2) + 1e-30)
                self.assertGreater(plain_db, -50.0)     # the impulses are heard
                self.assertLess(blanked_db, plain_db - 15.0)
                self.assertEqual(meters[-1].blanker_impulses, 100)
                self.assertIsNone(run(hit[:BLOCK], settings)[0][0].blanker_impulses)

    def test_the_meter_takes_the_blanked_channel(self):
        settings = open_in("AM")
        clean = am(0.5, -80.0, 0.5) + noise(0.5, -100.0)
        hit = clean + impulses(len(clean))
        level = run(clean, settings)[0][-1].level_db
        self.assertGreater(run(hit, settings)[0][-1].level_db, level + 3.0)
        self.assertAlmostEqual(run(hit, blanked_settings(settings))[0][-1].level_db, level, delta=0.5)

    def test_the_measure_s_tap_takes_the_channel_as_received(self):
        # A carrier at the VFO, impulses strong enough to raise the floor
        # within the filter: the measure must find the same with or without
        # the blanker, which would otherwise raise the carrier's margin.
        settings = open_in("AM")
        signal = carrier(2.2, -30.0, 150.0) + noise(2.2, -50.0) + impulses(round(2.2 * RATE), 2_000)
        low, high = passband_hz("AM", settings.bandwidth_hz)
        position = vfo_position_hz("AM")
        results, taps, channels = {}, {}, {}
        for on in (False, True):
            demodulator, gatherer = Demodulator(), CarrierMeasure()
            tapped, found = [], []

            def tap(channel, tapped=tapped, found=found, gatherer=gatherer, demodulator=demodulator):
                tapped.append(channel)
                result = gatherer.add(channel, demodulator.channel_rate_hz, position + low, position + high, "key")
                if result is not None:
                    found.append(result)
            for block in blocks(signal):
                demodulator.process(block, CENTRE, RATE, dataclasses.replace(settings, blanker=on), None, False, tap)
            results[on], taps[on] = found, np.concatenate(tapped)
            # What the demodulator's own channel held: through a tap with the blanker off on blanked samples.
            demodulator, kept = Demodulator(), []
            source = blank(signal) if on else signal
            for block in blocks(source):
                demodulator.process(block, CENTRE, RATE, settings, None, False, kept.append)
            channels[on] = np.concatenate(kept)
        np.testing.assert_array_equal(taps[True], taps[False])
        self.assertEqual(results[True], results[False])
        self.assertEqual(len(results[True]), 1)
        self.assertIsNotNone(results[True][0].frequency_hz)
        # The blanked channel would have measured another margin.
        rate = 48_000.0
        second = slice(round(rate), round(2 * rate))
        plain = strongest_line(channels[False][second], rate, position + low, position + high)
        blanked = strongest_line(channels[True][second], rate, position + low, position + high)
        self.assertGreater(blanked.margin_db - plain.margin_db, 5.0)

    def test_a_tap_that_starts_after_a_blanked_block_is_the_channel_as_received_from_its_next_block(self):
        settings = blanked_settings(open_in("AM"))
        signal = carrier(0.5, -30.0) + noise(0.5, -50.0) + impulses(round(0.5 * RATE), 2_000)
        plain, blanked = Demodulator(), Demodulator()
        expected, got = [], []
        for index, block in enumerate(blocks(signal)):
            plain.process(block, CENTRE, RATE, open_in("AM"), None, False, expected.append)
            blanked.process(block, CENTRE, RATE, settings, None, False, got.append if index >= 3 else None)
        np.testing.assert_array_equal(np.concatenate(got[1:]), np.concatenate(expected[4:]))
        # Its first block differs only by the filters' memory of the blanked block before it.
        np.testing.assert_allclose(got[0][len(got[0]) // 2:], expected[3][len(got[0]) // 2:], atol=1e-6)

    def test_the_display_detection_and_the_memory_scan_take_the_samples_as_received(self):
        rate, centre = RATE, CENTRE
        signal = carrier(1.2, -30.0) + noise(1.2, -60.0) + impulses(round(1.2 * rate))
        outputs = {}
        for on in (False, True):
            analyzer, demodulator, detector, watch = SpectrumAnalyzer(), Demodulator(), Detector(), ChannelWatch()
            settings = dataclasses.replace(open_in("AM"), blanker=on)
            bands = np.array([[centre + OFFSET - 4_000.0, centre + OFFSET + 4_000.0, 0.0]])
            got = []
            for block in blocks(signal):
                power, codes, meter, _, detections, levels, _ = _process_block(
                    analyzer, demodulator, detector, watch, block, centre, rate, True, settings, True, 12.0,
                    DETECTION_BLOCKS, bands)
                got.append((power, codes, detections, levels, meter.blanker_impulses))
            outputs[on] = got
        for (power, codes, detections, levels, _), (power_on, codes_on, detections_on, levels_on, count) in zip(
                outputs[False], outputs[True]):
            np.testing.assert_array_equal(power, power_on)
            np.testing.assert_array_equal(codes, codes_on)
            self.assertEqual(detections, detections_on)
            if levels is None:
                self.assertIsNone(levels_on)
            else:
                for one, other in zip(levels, levels_on):
                    np.testing.assert_array_equal(one, other)
        self.assertTrue(any(entry[2] for entry in outputs[True]))         # the carrier is found
        self.assertEqual((outputs[False][-1][4], outputs[True][-1][4]), (None, 100))

    def test_every_mode_takes_the_setting_which_starts_afresh_after_a_new_rate(self):
        self.assertEqual(DemodSettings(frequency_hz=1.0).blanker, False)
        demodulator = Demodulator()
        signal = noise(0.1, -60.0) + impulses(round(0.1 * RATE))
        for mode in MODES:
            settings = blanked_settings(settings_for(mode))
            run(signal, settings, demodulator=demodulator)
            self.assertIsNotNone(demodulator._blanker)
            self.assertTrue(demodulator._blanker.impulses)
        blanker = demodulator._blanker
        demodulator.process(signal[:BLOCK], CENTRE, RATE, blanked_settings(open_in("AM")), None, False)
        self.assertIs(demodulator._blanker, blanker)
        demodulator.process(signal[:BLOCK // 2], CENTRE, RATE / 2, blanked_settings(open_in("AM")), None, False)
        self.assertIsNot(demodulator._blanker, blanker)
        demodulator.process(signal[:BLOCK], CENTRE, RATE, open_in("AM"), None, False)
        self.assertIsNone(demodulator._blanker)

    def test_the_setting_must_be_true_or_false(self):
        settings = DemodSettings(frequency_hz=CENTRE)
        self.assertTrue(settings.changed({"blanker": True}).blanker)
        self.assertEqual(settings.changed({"blanker": True}).describe()["blanker"], True)
        for value in ("on", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,
                                                                   "^the noise blanker must be true or false$"):
                settings.changed({"blanker": value})


if __name__ == "__main__":
    unittest.main()
