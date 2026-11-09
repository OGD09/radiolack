"""Tests for the demodulator on synthetic signals: channel level, squelch,
NFM audio and its frames, and the settings."""

import math
import unittest

import numpy as np

from radiolack.demod import (AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO, DemodSettings, Demodulator,
                             Meter, channel_taps)

RATE = 2_400_000.0
CENTRE = 145_000_000.0
OFFSET = 100_000.0          # the VFO, from the centre frequency
BLOCK = 80_000              # 1/30 s, as the engine reads them
AUDIO_RATE = 12_000.0
RNG = np.random.default_rng(11)


def fm(seconds: float, level_db: float, tones=(), rate: float = RATE) -> np.ndarray:
    """A carrier on the VFO, frequency-modulated by ``tones``, pairs of a
    frequency and a deviation in Hz."""
    t = np.arange(round(seconds * rate)) / rate
    phase = 2 * np.pi * OFFSET * t
    for frequency, deviation in tones:
        phase += deviation / frequency * np.sin(2 * np.pi * frequency * t)
    return (10 ** (level_db / 20) * np.exp(1j * phase)).astype(np.complex64)


def noise(seconds: float, level_db: float) -> np.ndarray:
    """White noise of ``level_db`` dBFS over the whole band."""
    count = round(seconds * RATE)
    scale = math.sqrt(10 ** (level_db / 10) / 2)
    return (RNG.standard_normal(count) * scale + 1j * RNG.standard_normal(count) * scale).astype(np.complex64)


def run(samples: np.ndarray, settings: DemodSettings, noise_density: float | None = None,
        demodulator: Demodulator | None = None, rate: float = RATE, block: int = BLOCK):
    """The meters and audio blocks (as fractions of full scale) of ``samples``."""
    demodulator = demodulator if demodulator is not None else Demodulator()
    meters, sound = [], []
    for start in range(0, len(samples), block):
        meter, pcm = demodulator.process(samples[start:start + block], CENTRE, rate, settings,
                                         noise_density, True)
        meters.append(meter)
        sound.append(pcm.astype(np.float64) / 32767)
    return meters, sound


def tone_amplitude(audio: np.ndarray, frequency_hz: float) -> float:
    t = np.arange(len(audio)) / AUDIO_RATE
    return 2 * abs(np.mean(audio * np.exp(-2j * np.pi * frequency_hz * t)))


def response_db(taps: np.ndarray, frequency_hz: float, rate_hz: float) -> float:
    t = np.arange(len(taps)) - (len(taps) - 1) / 2
    return 20 * math.log10(abs(np.sum(taps * np.exp(-2j * np.pi * frequency_hz / rate_hz * t))))


def channel_noise_db(level_db: float, bandwidth_hz: float = 10_000.0) -> float:
    """What white noise of ``level_db`` over the band reads in an NFM channel."""
    taps = channel_taps("NFM", bandwidth_hz, 48_000.0)
    return level_db + 10 * math.log10(float(np.sum(np.abs(taps) ** 2)) * 48_000.0 / RATE)


VFO = DemodSettings(frequency_hz=CENTRE + OFFSET)
ALWAYS_OPEN = DemodSettings(frequency_hz=CENTRE + OFFSET, squelch_auto=False, squelch_db=-160.0)


class NfmTest(unittest.TestCase):
    def test_a_tone_comes_out_at_its_deviation_without_the_ctcss(self):
        signal = fm(1.5, -40.0, [(1_000.0, 3_000.0), (100.0, 500.0)]) + noise(1.5, -34.0)
        meters, sound = run(signal, ALWAYS_OPEN)
        audio = np.concatenate(sound)
        self.assertEqual(len(audio), 1.5 * AUDIO_RATE)
        steady = audio[3_000:]
        self.assertAlmostEqual(tone_amplitude(steady, 1_000.0), 0.6, delta=0.02)   # 3 kHz of 5 kHz
        self.assertLess(tone_amplitude(steady, 100.0), 0.01 * tone_amplitude(steady, 1_000.0))
        self.assertTrue(all(meter.open for meter in meters))

    def test_other_modes_are_metered_but_silent(self):
        meters, sound = run(fm(0.5, -40.0, [(1_000.0, 3_000.0)]),
                            DemodSettings(frequency_hz=CENTRE + OFFSET, mode="AM", squelch_auto=False,
                                          squelch_db=-160.0))
        self.assertAlmostEqual(meters[-1].level_db, -40.0, delta=0.1)
        self.assertTrue(meters[-1].open)
        self.assertFalse(np.any(np.concatenate(sound)))

    def test_sideband_filters_keep_their_side(self):
        for mode, kept in (("USB", 1), ("LSB", -1)):
            with self.subTest(mode=mode):
                taps = channel_taps(mode, 2_700.0, 48_000.0)
                self.assertAlmostEqual(response_db(taps, kept * 1_350.0, 48_000.0), 0.0, delta=0.05)
                self.assertLess(response_db(taps, -kept * 1_350.0, 48_000.0), -60.0)


class LevelTest(unittest.TestCase):
    def test_a_carrier_reads_its_power(self):
        meters, _ = run(fm(0.5, -40.0), ALWAYS_OPEN)
        for meter in meters[2:]:
            self.assertAlmostEqual(meter.level_db, -40.0, delta=0.05)

    def test_noise_reads_its_power_in_the_channel(self):
        meters, _ = run(noise(1.0, -34.0), ALWAYS_OPEN)
        level = 10 * math.log10(np.mean([10 ** (meter.level_db / 10) for meter in meters[2:]]))
        self.assertAlmostEqual(level, channel_noise_db(-34.0), delta=0.3)

    def test_a_vfo_outside_the_band_reads_nothing(self):
        settings = DemodSettings(frequency_hz=CENTRE + 1_500_000.0, squelch_auto=False, squelch_db=-160.0)
        meters, sound = run(fm(0.2, -40.0), settings)
        self.assertEqual(meters[-1], Meter(None, -160.0, False))
        self.assertEqual(len(sound[-1]), BLOCK / 200)
        self.assertFalse(np.any(np.concatenate(sound)))

    def test_a_receiver_slower_than_the_channel_gives_nothing(self):
        demodulator = Demodulator()
        self.assertEqual(demodulator.process(np.ones(1_000, np.complex64), CENTRE, 24_000.0, VFO, 1e-9, True),
                         (Meter(None, None, False), None))
        # A faster receiver then works again.
        meters, _ = run(fm(0.3, -40.0), ALWAYS_OPEN, demodulator=demodulator)
        self.assertAlmostEqual(meters[-1].level_db, -40.0, delta=0.05)


class SquelchTest(unittest.TestCase):
    def test_the_automatic_threshold_sits_above_the_noise(self):
        density = 10 ** (-34.0 / 10) / RATE
        meters, sound = run(noise(1.0, -34.0), VFO, noise_density=density)
        self.assertAlmostEqual(meters[-1].squelch_db, channel_noise_db(-34.0) + 8.0, delta=0.01)
        self.assertFalse(any(meter.open for meter in meters))
        self.assertFalse(np.any(np.concatenate(sound)))

    def test_opens_on_a_signal_and_closes_cleanly_after_it(self):
        density = 10 ** (-34.0 / 10) / RATE
        carrier = channel_noise_db(-34.0) + 15.0
        signal = np.concatenate((noise(0.5, -34.0),
                                 fm(1.0, carrier, [(1_000.0, 3_000.0)]) + noise(1.0, -34.0),
                                 noise(0.5, -34.0)))
        meters, sound = run(signal, VFO, noise_density=density)
        self.assertEqual([meter.open for meter in meters], [False] * 15 + [True] * 30 + [False] * 15)
        self.assertFalse(np.any(np.concatenate(sound[:15])))
        self.assertAlmostEqual(tone_amplitude(np.concatenate(sound[20:40]), 1_000.0), 0.6, delta=0.03)
        self.assertFalse(np.any(np.concatenate(sound[48:])))
        # No burst of noise as the squelch opens or closes.
        self.assertLess(np.abs(np.concatenate(sound)).max(), 0.75)

    def test_closes_only_well_below_the_threshold(self):
        settings = DemodSettings(frequency_hz=CENTRE + OFFSET, squelch_auto=False, squelch_db=-41.0)
        demodulator = Demodulator()
        for level, expected in ((-42.5, False), (-40.0, True), (-42.5, True), (-45.0, False), (-42.5, False)):
            with self.subTest(level=level):
                meters, _ = run(fm(0.2, level), settings, demodulator=demodulator)
                self.assertEqual(meters[-1].open, expected)

    def test_stays_closed_without_a_noise_estimate(self):
        meters, sound = run(fm(0.2, -40.0, [(1_000.0, 3_000.0)]), VFO, noise_density=None)
        self.assertEqual((meters[-1].squelch_db, meters[-1].open), (None, False))
        self.assertAlmostEqual(meters[-1].level_db, -40.0, delta=0.05)
        self.assertFalse(np.any(np.concatenate(sound)))


class RateTest(unittest.TestCase):
    def test_another_rate_keeps_the_channel_and_the_audio_rate(self):
        demodulator = Demodulator()
        run(fm(0.2, -40.0), ALWAYS_OPEN, demodulator=demodulator)
        meters, sound = run(fm(0.3, -40.0, rate=2_048_000.0), ALWAYS_OPEN, demodulator=demodulator,
                            rate=2_048_000.0, block=68_267)
        self.assertAlmostEqual(meters[-1].level_db, -40.0, delta=0.05)
        self.assertEqual(demodulator.audio_rate_hz, AUDIO_RATE)
        self.assertAlmostEqual(len(np.concatenate(sound)), 0.3 * AUDIO_RATE, delta=2)

    def test_an_odd_rate_gives_an_audio_rate_close_to_12_khz(self):
        demodulator = Demodulator()
        meters, sound = run(fm(0.2, -40.0, rate=2_345_678.0), ALWAYS_OPEN, demodulator=demodulator,
                            rate=2_345_678.0, block=78_190)
        self.assertAlmostEqual(meters[-1].level_db, -40.0, delta=0.1)
        self.assertAlmostEqual(demodulator.audio_rate_hz / AUDIO_RATE, 1.0, delta=0.005)
        frame = demodulator.encode_audio(np.zeros(4, "<i2"))
        self.assertEqual(AUDIO_HEADER.unpack_from(frame)[4], demodulator.audio_rate_hz)


class AudioFrameTest(unittest.TestCase):
    def test_frames_count_their_samples(self):
        demodulator = Demodulator()
        first = demodulator.encode_audio(np.array([1, -2, 32767], "<i2"))
        self.assertEqual(AUDIO_HEADER.unpack_from(first), (FRAME_KIND_AUDIO, AUDIO_VERSION, 0, 0, AUDIO_RATE))
        self.assertEqual(np.frombuffer(first, "<i2", offset=AUDIO_HEADER.size).tolist(), [1, -2, 32767])
        second = demodulator.encode_audio(np.zeros(5, "<i2"))
        self.assertEqual(AUDIO_HEADER.unpack_from(second)[3], 3)
        demodulator.position = 2 ** 32 - 2
        demodulator.encode_audio(np.zeros(5, "<i2"))
        self.assertEqual(demodulator.position, 3)


class SettingsTest(unittest.TestCase):
    def test_changes_apply_and_round_the_frequency(self):
        settings = VFO.changed({"frequency_hz": 145_500_000.4, "mode": "AM", "bandwidth_hz": 6_000,
                                "squelch_db": -95.5, "squelch_auto": False})
        self.assertEqual(settings.describe(), {"frequency_hz": 145_500_000.0, "mode": "AM",
                                               "bandwidth_hz": 6_000.0, "squelch_db": -95.5,
                                               "squelch_auto": False})

    def test_refused_changes(self):
        refused = (
            ({"frequency_hz": "145e6"}, "the VFO frequency must be a number"),
            ({"frequency_hz": 0}, "the VFO frequency must be between"),
            ({"mode": "WFM"}, "the mode must be one of NFM, AM, USB, LSB, CW"),
            ({"bandwidth_hz": 50}, "the filter width must be between 100 and 25000"),
            ({"bandwidth_hz": True}, "the filter width must be a number"),
            ({"squelch_db": math.nan}, "the squelch threshold must be a number"),
            ({"squelch_auto": 1}, "the automatic squelch must be true or false"),
            ({"volume": 3}, "unknown demodulator setting: volume"),
        )
        for changes, error in refused:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                VFO.changed(changes)


if __name__ == "__main__":
    unittest.main()
