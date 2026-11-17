"""Tests for the automatic notch (demod.py: ``AutoNotch``, the audio
filter's notches and the demodulation settings' ``notch``) on synthetic,
seeded signals through the real audio path: whistles removed and released,
the voice around them kept, what is not a whistle left alone, the modes
where it does not act, and the fade from one set of taps to the next."""

import math
import unittest

import numpy as np

from radiolack.demod import (NOTCH_FADE_S, NOTCH_MODES, NOTCH_MOVE_HZ, SQUELCH_WINDOW_S, FADE_S, AutoNotch,
                             CarrierSearch, DemodSettings, Demodulator, _AudioPath, audio_taps)
from tests.test_demod import CENTRE, OFFSET, response_db, run, tone_amplitude

RATE_HZ = 48_000.0      # the channel's rate, and the detector's output's
BLOCK = 1_600           # the channel's samples in one of the engine's blocks at 2.4 MS/s
DECIMATION = 4
AUDIO_RATE_HZ = RATE_HZ / DECIMATION
WHISTLE_HZ = 1_234.5
CARRIER_HZ = 150.0      # an AM station's carrier, from the VFO
# The audio path's delay at 48 kHz: the squelch's look-ahead, then the audio filter's.
DELAY_S = (round(SQUELCH_WINDOW_S * RATE_HZ) + round(FADE_S * RATE_HZ)
           + (len(audio_taps("USB", RATE_HZ)) - 1) / 2) / RATE_HZ
MORSE = {"C": "-.-.", "D": "-..", "E": ".", "K": "-.-", "Q": "--.-", "S": "...", "T": "-"}


def times(seconds_s):
    return np.arange(round(seconds_s * RATE_HZ)) / RATE_HZ


def voice(t):
    """A voice-like sound, 0.1 rms: harmonics of a pitch that glides, under syllables."""
    pitch_phase = 2 * np.pi * (150 * t - 40 / (2 * np.pi * 1.7) * np.cos(2 * np.pi * 1.7 * t)
                               - 15 / (2 * np.pi * 4.3) * np.cos(2 * np.pi * 4.3 * t))
    sound = sum(np.sin(k * pitch_phase) / k for k in range(1, 25))
    sound *= 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t) ** 2
    return 0.1 * sound / np.std(sound)


def tone(frequency_hz, level, start_s=0.0, stop_s=math.inf):
    """A whistle: a function of time giving its phase in radians and its
    amplitude at each instant, ``level`` from ``start_s`` to ``stop_s``."""
    def at(t):
        return 2 * np.pi * frequency_hz * t, level * ((t >= start_s) & (t < stop_s))
    return at


def channel_of(mode, seconds_s, whistles, with_voice=True, seed=5):
    """The channel at 48 kHz whose detector gives a voice (or nothing) and
    ``whistles``: in USB and LSB, the audio itself, over a little noise; in
    AM and SAM, a station whose carrier, 150 Hz from the VFO, the voice
    modulates, and for each whistle a carrier that beats with it, its level
    relative to the station's carrier."""
    t = times(seconds_s)
    sound = (voice(t) if with_voice else 0.0) + np.random.default_rng(seed).normal(0, 0.001, len(t))
    parts = [whistle(t) for whistle in whistles]
    if mode in ("USB", "LSB"):
        return (sound + sum(level * np.sin(phase) for phase, level in parts)).astype(np.complex64)
    carrier = 2 * np.pi * CARRIER_HZ * t
    channel = 0.1 * (1 + 0.5 * sound / 0.3) * np.exp(1j * carrier)
    for phase, level in parts:
        channel = channel + 0.1 * level * np.exp(1j * (carrier + phase))
    return channel.astype(np.complex64)


def listen(mode, channel, notch=True, each=None):
    """The audio of ``channel`` through the audio path of ``mode``, its
    squelch open, block by block as the demodulator gives it, and the
    notches after each block; ``each`` is called before each block with the
    block's index and the path."""
    search = CarrierSearch(RATE_HZ) if mode == "SAM" else None
    path = _AudioPath(mode, RATE_HZ, DECIMATION, search, notch)
    audio, notches = [], []
    for index, start in enumerate(range(0, len(channel), BLOCK)):
        block = channel[start:start + BLOCK]
        if each is not None:
            each(index, path)
        if search is not None:
            search.add(block)
        audio.append(path.process(block, np.abs(block).astype(np.float64) ** 2, np.ones(len(block))))
        notches.append(path.notches)
    return np.concatenate(audio), notches


def changes(notches):
    """When the notches changed, at the end of a block, and to what."""
    found, previous = [], ()
    for index, now in enumerate(notches):
        if now != previous:
            found.append(((index + 1) * BLOCK / RATE_HZ, now))
            previous = now
    return found


def seconds(start_s, stop_s):
    return slice(round(start_s * AUDIO_RATE_HZ), round(stop_s * AUDIO_RATE_HZ))


def power_db(audio):
    return 10 * math.log10(float(np.mean(audio ** 2)))


def without(audio, frequencies):
    """``audio`` less its best fit by tones at ``frequencies``: the voice
    under the whistles."""
    t = np.arange(len(audio)) / AUDIO_RATE_HZ
    basis = np.column_stack([part for f in frequencies for part in (np.cos(2 * np.pi * f * t),
                                                                     np.sin(2 * np.pi * f * t))])
    fit, *_ = np.linalg.lstsq(basis, audio, rcond=None)
    return audio - basis @ fit


def removed_db(audio, plain, frequency_hz, window):
    """How far the notch took a steady tone down, against the plain path."""
    return 20 * math.log10(tone_amplitude(audio[window], frequency_hz) / tone_amplitude(plain[window], frequency_hz))


def keyed(t, text, wpm):
    """Morse keying of ``text`` at ``wpm``, repeated, with 5 ms edges: 1 on, 0 off."""
    keys = []
    for word in text.split():
        for letter in word:
            for symbol in MORSE[letter]:
                keys += [1.0] * (1 if symbol == "." else 3) + [0.0]
            keys += [0.0, 0.0]
        keys += [0.0] * 4
    units = np.floor(t / (1.2 / wpm)).astype(int) % len(keys)
    edge = round(0.005 * RATE_HZ)
    return np.convolve(np.array(keys)[units], np.ones(edge) / edge, mode="same")


class AudioTapsTest(unittest.TestCase):
    def test_each_notch_removes_90_hz_and_keeps_the_filters_length(self):
        plain = audio_taps("AM", RATE_HZ)
        np.testing.assert_array_equal(audio_taps("AM", RATE_HZ, ()), plain)
        for mode in NOTCH_MODES + ("CW",):
            with self.subTest(mode=mode):
                notched = audio_taps(mode, RATE_HZ, (1_000.0, 2_500.0))
                self.assertEqual(len(notched), len(plain))
                for frequency_hz in (985.0, 1_000.0, 1_015.0, 2_500.0):    # a line 30 Hz wide
                    self.assertLess(response_db(notched, frequency_hz, RATE_HZ), -40.0)
                for frequency_hz in (850.0, 1_150.0, 1_800.0, 3_500.0):     # the audio beside it
                    self.assertAlmostEqual(response_db(notched, frequency_hz, RATE_HZ),
                                           response_db(plain, frequency_hz, RATE_HZ), delta=0.2)

    def test_nfm_and_wfm_take_no_notch(self):
        for mode in ("NFM", "WFM"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, f"takes no notch in {mode}"):
                audio_taps(mode, RATE_HZ, (1_000.0,))


class WhistleTest(unittest.TestCase):
    def test_a_whistle_on_a_voice_goes_within_a_second_and_comes_back_a_second_after_it_stops(self):
        for mode in NOTCH_MODES:
            with self.subTest(mode=mode):
                channel = channel_of(mode, 6.0, [tone(WHISTLE_HZ, 0.15, 1.0, 4.0)])
                audio, notches = listen(mode, channel)
                plain, unchanged = listen(mode, channel, notch=False)
                self.assertEqual(set(unchanged), {None})
                found = changes(notches)
                (on_s, first), (off_s, last) = found[0], found[-1]
                self.assertGreaterEqual(on_s, 1.5)      # steady for half a second first
                self.assertLessEqual(on_s, 2.0)         # within a second of the whistle's start
                self.assertTrue(any(abs(f - WHISTLE_HZ) <= 1.0 for f in first), first)
                self.assertEqual(last, ())
                self.assertGreaterEqual(off_s, 5.0)     # a second after it stops
                self.assertLessEqual(off_s, 5.5)
                window = seconds(2.1, 3.9)              # past the change and the paths' delay
                self.assertLessEqual(removed_db(audio, plain, WHISTLE_HZ, window), -20.0)
                # The voice around it stays.
                lines = (WHISTLE_HZ, 2 * WHISTLE_HZ)    # AM's envelope gives the second too
                self.assertAlmostEqual(power_db(without(audio[window], lines)),
                                       power_db(without(plain[window], lines)), delta=1.0)
                # Released, the filter is the plain one again, to the sample.
                after = seconds(off_s + 0.05, 6.0)
                np.testing.assert_allclose(audio[after], plain[after], atol=1e-6)

    def test_two_whistles_then_five_the_four_strongest_go(self):
        # Two whistles, then three more: those notched stay, the two
        # strongest newcomers join them, the weakest is heard.
        whistles = [tone(700.0, 0.1), tone(1_900.0, 0.1), tone(1_100.0, 0.2, 2.0), tone(2_500.0, 0.15, 2.0),
                    tone(3_300.0, 0.03, 2.0)]
        channel = channel_of("USB", 4.0, whistles)
        audio, notches = listen("USB", channel)
        plain, _ = listen("USB", channel, notch=False)
        found = changes(notches)
        self.assertEqual([round(f) for f in found[0][1]], [700, 1_900])
        self.assertLessEqual(found[0][0], 1.0)
        self.assertEqual([round(f) for f in notches[-1]], [700, 1_100, 1_900, 2_500])
        window = seconds(3.2, 4.0)
        for frequency_hz in (700.0, 1_100.0, 1_900.0, 2_500.0):
            self.assertLessEqual(removed_db(audio, plain, frequency_hz, window), -20.0)
        self.assertAlmostEqual(removed_db(audio, plain, 3_300.0, window), 0.0, delta=0.5)
        # Five at once: the four that stand highest over their surroundings,
        # where the voice counts too.
        levels = {600.0: 0.1, 1_100.0: 0.2, 1_700.0: 0.01, 2_500.0: 0.15, 3_300.0: 0.05}
        channel = channel_of("USB", 2.0, [tone(f, level) for f, level in levels.items()])
        _, notches = listen("USB", channel)
        self.assertEqual([round(f) for f in notches[-1]], [600, 1_100, 2_500, 3_300])

    def test_a_whistle_that_drifts_slowly_is_followed(self):
        def drifting(t):            # 8 Hz a second up from 1 kHz
            return 2 * np.pi * (1_000.0 * t + 4.0 * t ** 2), 0.1 * np.ones(len(t))

        channel = channel_of("USB", 8.0, [drifting])
        audio, notches = listen("USB", channel)
        plain, _ = listen("USB", channel, notch=False)
        found = changes(notches)
        self.assertLessEqual(found[0][0], 1.0)
        self.assertGreaterEqual(len(found), 8)                  # it moved with its line
        self.assertTrue(all(len(now) == 1 for _, now in found))     # never released on the way
        self.assertLessEqual(abs(notches[-1][0] - (1_000.0 + 8.0 * 8.0)), NOTCH_MOVE_HZ + 1.0)
        t = np.arange(len(audio)) / AUDIO_RATE_HZ
        heard = np.exp(-1j * drifting(t - DELAY_S)[0])
        for start_s in np.arange(1.25, 8.0, 0.25):
            window = seconds(start_s, start_s + 0.25)
            level = abs(np.mean(audio[window] * heard[window]))
            before = abs(np.mean(plain[window] * heard[window]))
            self.assertLessEqual(20 * math.log10(level / before), -20.0, start_s)

    def test_a_short_tone_a_keyed_tone_and_tones_that_move_are_left_alone(self):
        def beeps(t):               # 0.4 s every 1.5 s, as a roger beep
            return 2 * np.pi * 1_000.0 * t, 0.1 * ((t % 1.5) < 0.4)

        def morse(wpm):
            def at(t):              # a CW signal heard in USB
                return 2 * np.pi * 700.0 * t, 0.2 * keyed(t, "CQ CQ DE TEST TEST K", wpm)
            return at

        def melody(t):              # a note every quarter of a second
            notes = np.array([440.0, 523.3, 659.3, 587.3, 493.9, 392.0, 698.5, 784.0])
            frequencies = notes[np.floor(t / 0.25).astype(int) % len(notes)]
            return 2 * np.pi * np.cumsum(frequencies) / RATE_HZ, 0.1 * np.ones(len(t))

        def vibrato(t):             # 1 kHz, 60 Hz up and down three times a second
            return 2 * np.pi * (1_000.0 * t - 30.0 / (2 * np.pi * 3.0) * np.cos(2 * np.pi * 3.0 * t)), 0.1

        cases = {"voice": [], "beeps": [beeps], "morse 12 wpm": [morse(12)], "morse 20 wpm": [morse(20)],
                 "morse 30 wpm": [morse(30)], "melody": [melody], "vibrato": [vibrato]}
        for name, whistles in cases.items():
            for with_voice in (True, False):
                with self.subTest(name, voice=with_voice):
                    _, notches = listen("USB", channel_of("USB", 8.0, whistles, with_voice))
                    self.assertEqual(set(notches), {()})


class FadeTest(unittest.TestCase):
    def check_fade(self, audio, before, after, start):
        """``audio`` is ``before`` up to output sample ``start``, then fades
        linearly into ``after`` over the notch's fade: from one sample to
        the next it moves no more than either does, plus their difference
        spread over the fade."""
        fade = round(NOTCH_FADE_S * AUDIO_RATE_HZ)
        np.testing.assert_allclose(audio[:start], before[:start], atol=1e-9)
        np.testing.assert_allclose(audio[start + fade:], after[start + fade:], atol=1e-9)
        region = slice(start - 1, start + fade + 1)
        gap = float(np.max(np.abs(after[region] - before[region])))
        natural = max(float(np.max(np.abs(np.diff(before[region])))), float(np.max(np.abs(np.diff(after[region])))))
        step = float(np.max(np.abs(np.diff(audio[region]))))
        self.assertLessEqual(step, natural + gap / fade * 1.01 + 1e-9)
        self.assertLessEqual(float(np.max(np.abs(audio[region] - before[region]))), gap + 1e-9)
        return gap, natural, step

    def test_new_taps_fade_in_without_a_step_and_so_does_the_notch_turned_off(self):
        channel = channel_of("USB", 3.0, [tone(WHISTLE_HZ, 0.3)])
        audio, notches = listen("USB", channel)
        first = next(index for index, now in enumerate(notches) if now)
        start = first * BLOCK // DECIMATION         # the first output sample of that block
        plain, _ = listen("USB", channel, notch=False)

        def switch(index, path):    # the same taps, all at once
            if index == first:
                path._filter.set_taps(audio_taps("USB", RATE_HZ, notches[first]))

        hard, _ = listen("USB", channel, notch=False, each=switch)
        gap, natural, step = self.check_fade(audio, plain, hard, start)
        # What the new taps remove is the whistle, which the AGC brings near 0.4 of full scale.
        whistle = tone_amplitude(plain[seconds(0.5, 1.0)], WHISTLE_HZ)
        self.assertGreater(whistle, 0.2)
        self.assertLessEqual(gap, 1.2 * whistle)
        self.assertLess(step - natural, gap / 20)
        # Turned off, the notch's taps leave through the same fade.
        off = first + 20

        def turn_off(index, path):
            if index == off:
                path.set_notch(False)

        audio, notches = listen("USB", channel, each=turn_off)
        self.assertTrue(notches[off - 1])
        self.assertEqual(set(notches[off:]), {None})
        notched, _ = listen("USB", channel)
        self.check_fade(audio, notched, plain, off * BLOCK // DECIMATION)


class ThresholdTest(unittest.TestCase):
    def test_a_line_at_the_threshold_changes_the_taps_once_a_minute_at_most(self):
        # Lines that the analysis finds in about a quarter, half and three
        # quarters of its FFTs, in noise: gained only after half a second
        # found at every analysis, held while found once a second, and read
        # before the filter, a notch neither comes and goes nor moves.
        for amplitude in (0.0014, 0.0018, 0.0022):
            for seed in range(3):
                with self.subTest(amplitude=amplitude, seed=seed):
                    rng = np.random.default_rng(seed)
                    notch = AutoNotch(RATE_HZ)
                    count = 0
                    for second in range(60):
                        t = second + np.arange(round(RATE_HZ)) / RATE_HZ
                        audio = amplitude * np.sin(2 * np.pi * WHISTLE_HZ * t) + rng.normal(0, 0.01, len(t))
                        count += sum(notch.process(audio[start:start + BLOCK])
                                     for start in range(0, len(audio), BLOCK))
                    self.assertLessEqual(count, 1)
                    if amplitude == 0.0022:
                        self.assertEqual(count, 1)      # found often enough to be gained, and held


class SettingTest(unittest.TestCase):
    def test_off_by_default_kept_across_modes_and_acting_in_four(self):
        settings = DemodSettings(frequency_hz=CENTRE + OFFSET)
        self.assertFalse(settings.notch)
        self.assertFalse(settings.describe()["notch"])
        on = settings.changed({"notch": True})
        self.assertTrue(on.describe()["notch"])
        for mode in ("NFM", "AM", "SAM", "USB", "LSB", "CW", "WFM"):
            moved = on.changed({"mode": mode})
            self.assertTrue(moved.notch)
            self.assertEqual(moved.notching, mode in ("AM", "SAM", "USB", "LSB"))
            self.assertFalse(settings.changed({"mode": mode}).notching)
        for value in (1, "on", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^the notch must be true or false$"):
                settings.changed({"notch": value})

    def test_nothing_acts_in_nfm_wfm_and_cw_nor_when_off(self):
        rate_hz = 960_000.0         # holds WFM's channel
        t = np.arange(round(2.5 * rate_hz)) / rate_hz
        # A carrier on the VFO and one 1 kHz above it, which whistles.
        samples = (0.1 * np.exp(2j * np.pi * OFFSET * t) + 0.03 * np.exp(2j * np.pi * (OFFSET + 1_000.0) * t)
                   + np.random.default_rng(2).normal(0, 1e-4, len(t))).astype(np.complex64)
        widths = {"NFM": 10_000.0, "WFM": 180_000.0, "CW": 500.0, "USB": 2_700.0}
        for mode, width in widths.items():
            with self.subTest(mode=mode):
                settings = DemodSettings(frequency_hz=CENTRE + OFFSET, mode=mode, bandwidth_hz=width,
                                         squelch_auto=False, squelch_db=-160.0)
                meters_off, sound_off = run(samples, settings, rate=rate_hz, block=32_000)
                meters_on, sound_on = run(samples, settings.changed({"notch": True}), rate=rate_hz, block=32_000)
                self.assertEqual({meter.notch_lines for meter in meters_off}, {None})
                if mode == "USB":
                    self.assertEqual(meters_on[0].notch_lines, 0)
                    self.assertEqual(meters_on[-1].notch_lines, 1)
                    tail = np.concatenate(sound_on[-15:]), np.concatenate(sound_off[-15:])
                    self.assertLessEqual(20 * math.log10(tone_amplitude(tail[0], 1_000.0)
                                                         / tone_amplitude(tail[1], 1_000.0)), -20.0)
                else:
                    self.assertEqual({meter.notch_lines for meter in meters_on}, {None})
                    np.testing.assert_array_equal(np.concatenate(sound_on), np.concatenate(sound_off))

    def test_it_starts_afresh_after_another_mode_and_removes_nothing_without_audio(self):
        rate_hz, block = 240_000.0, 8_000
        t = np.arange(round(1.5 * rate_hz)) / rate_hz
        samples = (0.1 * np.exp(2j * np.pi * (OFFSET + 1_000.0) * t)).astype(np.complex64)
        demodulator = Demodulator()
        usb = DemodSettings(frequency_hz=CENTRE + OFFSET, mode="USB", bandwidth_hz=2_700.0, notch=True)
        meters, _ = run(samples, usb, demodulator=demodulator, rate=rate_hz, block=block)
        self.assertEqual(meters[-1].notch_lines, 1)
        meters, _ = run(samples[:block * 3], usb.changed({"mode": "CW", "bandwidth_hz": 500}),
                        demodulator=demodulator, rate=rate_hz, block=block)
        self.assertEqual({meter.notch_lines for meter in meters}, {None})
        meters, _ = run(samples, usb, demodulator=demodulator, rate=rate_hz, block=block)
        self.assertEqual(meters[0].notch_lines, 0)
        self.assertEqual(meters[-1].notch_lines, 1)
        for settings, lines in ((usb, 0), (usb.changed({"mode": "CW", "bandwidth_hz": 500}), None),
                                (usb.changed({"notch": False}), None)):
            meter, pcm = demodulator.process(samples[:block], CENTRE, rate_hz, settings, None, False)
            self.assertIsNone(pcm)
            self.assertEqual(meter.notch_lines, lines)


if __name__ == "__main__":
    unittest.main()
