"""Tests for synchronous AM (demod.py: ``CarrierSearch``, ``SyncDetector``
and the SAM mode) on synthetic signals: the carrier found and held, the
lock, the audio through a fade, the switch between the synchronous detector
and the envelope, and the mode in the demodulator, beside AM."""

import math
import unittest

import numpy as np

from radiolack.demod import (AM_LEVEL, SAM_BAND_HZ, SAM_FADE_S, SAM_LOCK_DB, SAM_STAGES, CarrierSearch,
                             DemodSettings, Demodulator, MovingAverages, SyncDetector, _AmDetector, _AudioPath,
                             sam_band_pass_length)
from tests.test_demod import AUDIO_RATE, CENTRE, OFFSET, am, run, tone_amplitude
from tests.test_demod import noise as band_noise

RATE_HZ = 48_000.0      # the channel's rate, and the detector's output's
BLOCK = 1_600           # the channel's samples in one of the engine's blocks at 2.4 MS/s
DELAY = 720             # the AM audio path's delay at 48 kHz: 10 ms of squelch and 5 ms of fade
CARRIER_HZ = 150.0      # the station's carrier, from the VFO


def times(seconds):
    return np.arange(round(seconds * RATE_HZ)) / RATE_HZ


def noise(rng, count, rms):
    return rng.normal(0, rms / math.sqrt(2), count) + 1j * rng.normal(0, rms / math.sqrt(2), count)


def blocks(signal, size=BLOCK):
    return (signal[start:start + size] for start in range(0, len(signal), size))


def correlation(a, b):
    a, b = a - a.mean(), b - b.mean()
    return float(a @ b / math.sqrt((a @ a) * (b @ b)))


def distortion(audio, reference):
    """What of ``audio`` the best fit of ``reference`` and a constant leaves,
    over what it explains."""
    basis = np.column_stack((reference, np.ones(len(reference))))
    fit, *_ = np.linalg.lstsq(basis, audio, rcond=None)
    explained = basis @ fit
    return float(np.sum((audio - explained) ** 2) / np.sum((explained - explained.mean()) ** 2))


def tones_at(t):
    """Three tones, peaks of 1."""
    return (np.sin(2 * np.pi * 350 * t) + 0.7 * np.sin(2 * np.pi * 900 * t + 1)
            + 0.5 * np.sin(2 * np.pi * 1700 * t + 2)) / 2.2


def station(carrier, modulation, rms, seed=3, carrier_hz=CARRIER_HZ):
    """An AM station, its carrier at ``carrier`` (an array, 1 unfaded) of
    0.1, its sidebands at ``modulation`` of three tones, over noise of
    ``rms``; and the tones, ``DELAY`` samples late."""
    t = times(len(carrier) / RATE_HZ)
    tones = tones_at(t)
    channel = 0.1 * (carrier + modulation * tones) * np.exp(2j * np.pi * carrier_hz * t + 0.3j)
    channel += noise(np.random.default_rng(seed), len(t), rms)
    return channel.astype(np.complex64), np.concatenate((np.zeros(DELAY), tones))[:len(t)]


def margin_rms(margin_db):
    """The noise over the channel that leaves a carrier of 0.1 ``margin_db``
    over the noise in the band-pass's 80 Hz."""
    return 0.1 / math.sqrt(10 ** (margin_db / 10) * SAM_BAND_HZ / RATE_HZ)


def detect(channel, search=None, detector=None, each=None):
    """SAM's audio of ``channel``, block by block as the demodulator gives
    it, and its lock after each block; ``each`` is called before each block
    with its index, the search and the detector."""
    search = search if search is not None else CarrierSearch(RATE_HZ)
    detector = detector if detector is not None else SyncDetector(RATE_HZ, DELAY, search)
    audio, locks = [], []
    for index, block in enumerate(blocks(channel)):
        if each is not None:
            each(index, search, detector)
        search.add(block)
        audio.append(detector.process(block, np.abs(block) ** 2))
        locks.append(search.locked)
    return np.concatenate(audio), locks


def envelope_of(channel):
    detector = _AmDetector(RATE_HZ, DELAY)
    return np.concatenate([detector.process(block, np.abs(block) ** 2) for block in blocks(channel)])


def seconds(start_s, stop_s):
    return slice(round(start_s * RATE_HZ), round(stop_s * RATE_HZ))


class SyncDetectorTest(unittest.TestCase):
    def test_it_locks_on_the_carrier_and_follows_the_modulation_through_a_fade_where_am_distorts(self):
        t = times(6.0)
        carrier = np.ones(len(t))
        fade = (t > 2) & (t < 4)
        carrier[fade] = 1 - 0.9 * np.sin(np.pi * (t[fade] - 2) / 2)     # 20 dB down at 3 s, the sidebands not
        channel, tones = station(carrier, 0.8, 0.001)
        search = CarrierSearch(RATE_HZ)
        audio, locks = detect(channel, search)
        envelope = envelope_of(channel)
        self.assertEqual(len(audio), len(t))
        self.assertTrue(all(locks[10:]))
        self.assertAlmostEqual(search.carrier_hz, CARRIER_HZ, delta=1.0)
        for start_s in np.arange(2.5, 3.5, 0.1):     # where the carrier is below its sidebands
            window = seconds(start_s, start_s + 0.1)
            self.assertGreater(correlation(audio[window], tones[window]), 0.99)
        # The envelope distorts there, SAM far less: what SAM is for.
        bottom = seconds(2.8, 3.2)
        sam, envelope_distortion = distortion(audio[bottom], tones[bottom]), distortion(envelope[bottom],
                                                                                         tones[bottom])
        self.assertGreater(envelope_distortion, 0.5)
        self.assertLess(sam, envelope_distortion / 20)
        clear = seconds(1.0, 1.5)
        self.assertGreater(correlation(audio[clear], tones[clear]), 0.99)
        # Unfaded, as loud as the AM detector.
        self.assertAlmostEqual(np.std(audio[clear]) / np.std(envelope[clear]), 1.0, delta=0.05)

    def test_it_finds_a_carrier_up_to_a_kilohertz_from_the_vfo_and_no_further(self):
        t = times(1.0)
        for offset_hz, locked in ((-950.0, True), (0.0, True), (640.0, True), (950.0, True), (1_300.0, False)):
            with self.subTest(offset_hz=offset_hz):
                channel = 0.1 * np.exp(2j * np.pi * offset_hz * t) + noise(np.random.default_rng(1), len(t), 0.001)
                search = CarrierSearch(RATE_HZ)
                detect(channel.astype(np.complex64), search)
                self.assertIs(search.locked, locked)
                if locked:
                    self.assertAlmostEqual(search.carrier_hz, offset_hz, delta=1.0)
                else:
                    self.assertIsNone(search.carrier_hz)

    def test_it_holds_its_carrier_while_a_sideband_line_outgrows_it(self):
        # A steady line 300 Hz above the carrier, a sideband's, which stands
        # 6 dB over the carrier while the carrier fades alone by 20 dB.
        t = times(4.0)
        carrier = np.ones(len(t))
        fade = (t > 1.5) & (t < 2.5)
        carrier[fade] = 0.1
        line = 0.2 * np.exp(2j * np.pi * (CARRIER_HZ + 300.0) * t)
        channel = (0.1 * carrier * np.exp(2j * np.pi * CARRIER_HZ * t) + 0.1 * line
                   + noise(np.random.default_rng(5), len(t), 0.001)).astype(np.complex64)
        found = []

        def follow(index, search, detector):
            found.append(search.carrier_hz)

        _, locks = detect(channel, each=follow)
        self.assertTrue(all(locks[3:]))
        for carrier_hz in found[4:]:
            self.assertAlmostEqual(carrier_hz, CARRIER_HZ, delta=1.0)
        # Without a lock, the strongest line takes the carrier: the hold is what keeps it.
        search = CarrierSearch(RATE_HZ)
        search.add(channel[seconds(1.6, 1.7)])
        self.assertAlmostEqual(search.carrier_hz, CARRIER_HZ + 300.0, delta=1.0)

    def test_under_10_db_of_carrier_it_has_no_lock_and_gives_the_envelope(self):
        for margin_db, locked in ((4.0, False), (8.0, False), (13.0, True), (20.0, True)):
            with self.subTest(margin_db=margin_db):
                channel, _ = station(np.ones(round(2 * RATE_HZ)), 0.3, margin_rms(margin_db))
                search = CarrierSearch(RATE_HZ)
                audio, locks = detect(channel, search)
                self.assertTrue(all(lock is locked for lock in locks[10:]), locks)
                self.assertAlmostEqual(search.margin_db, margin_db, delta=2.5)
                if not locked:
                    self.assertIsNone(search.carrier_hz)
                    np.testing.assert_array_equal(audio, envelope_of(channel))

    def test_a_carrier_at_the_edge_does_not_gain_and_lose_the_lock_again_and_again(self):
        # Search by search, a carrier at the lock's level changed it five
        # times a second: the mean of several searches and the margin under
        # which a lock holds leave a change or two in half a minute.
        t = times(30.0)
        carrier = 0.1 * np.exp(2j * np.pi * CARRIER_HZ * t + 0.3j)
        for margin_db in (SAM_LOCK_DB - 1.5, SAM_LOCK_DB, SAM_LOCK_DB + 2.0):
            with self.subTest(margin_db=margin_db):
                channel = carrier + noise(np.random.default_rng(5), len(t), margin_rms(margin_db))
                search = CarrierSearch(RATE_HZ)
                locks = []
                for block in blocks(channel.astype(np.complex64)):
                    search.add(block)
                    locks.append(search.locked)
                self.assertLessEqual(sum(a != b for a, b in zip(locks, locks[1:])), 3)

    def test_it_locks_again_once_the_carrier_returns(self):
        t = times(4.0)
        present = ((t < 1.5) | (t >= 2.5)).astype(float)    # the station goes for a second
        channel, tones = station(present, 0.5 * present, 0.001)
        search = CarrierSearch(RATE_HZ)
        audio, locks = detect(channel, search)
        per_second = round(RATE_HZ / BLOCK)
        self.assertTrue(all(locks[per_second // 2:round(1.5 * per_second)]))
        # Lost once the mean of the latest searches says so, half a second after it went.
        self.assertFalse(any(locks[round(2.1 * per_second):round(2.5 * per_second)]))
        self.assertTrue(all(locks[round(2.8 * per_second):]))
        self.assertEqual(search.acquisitions, 2)
        self.assertAlmostEqual(search.carrier_hz, CARRIER_HZ, delta=1.0)
        back = seconds(3.0, 4.0)
        self.assertGreater(correlation(audio[back], tones[back]), 0.99)

    def test_the_switch_between_the_detectors_makes_no_step(self):
        # In a selective fade, where the two detectors' outputs differ most,
        # the lock goes and comes back by hand: the audio fades from one to
        # the other, moving no faster than either does plus their difference
        # spread over the fade. A switch at once would step by their whole
        # difference.
        t = times(4.0)
        carrier = np.ones(len(t))
        fade = (t > 1) & (t < 3)
        carrier[fade] = 1 - 0.9 * np.sin(np.pi * (t[fade] - 1) / 2)
        channel, _ = station(carrier, 0.8, 0.001)
        per_second = round(RATE_HZ / BLOCK)
        lose, regain = round(1.8 * per_second), round(2.2 * per_second)

        class Frozen(CarrierSearch):
            """A search that stops searching once locked, so that the test
            alone decides the lock."""

            def add(self, channel):
                if not self.acquisitions:
                    super().add(channel)

        class Recorded(SyncDetector):
            """The detector, which keeps the synchronous output of the latest
            block, None when it computed none."""

            latest = None

            def process(self, channel, power):
                self.latest = None
                return super().process(channel, power)

            def _synchronous(self, *arguments):
                self.latest = super()._synchronous(*arguments)
                return self.latest

        search = Frozen(RATE_HZ)
        detector = Recorded(RATE_HZ, DELAY, search)
        audio, synchronous = [], []
        for index, block in enumerate(blocks(channel)):
            if index == lose:
                search.locked = False
            elif index == regain:
                search.locked = True
                search.acquisitions += 1
            search.add(block)
            audio.append(detector.process(block, np.abs(block) ** 2))
            synchronous.append(detector.latest)
        audio = np.concatenate(audio)
        self.assertEqual(search.acquisitions, 2)
        envelope = envelope_of(channel)
        fade = round(SAM_FADE_S * RATE_HZ)
        self.assertIsNone(synchronous[regain])      # a lock gained waits a block for the band-pass
        for switch, losing in ((lose, True), (regain + 1, False)):
            with self.subTest(switch=switch):
                start = switch * BLOCK
                here = slice(start, start + BLOCK)
                sync = synchronous[switch]
                self.assertIsNotNone(sync)
                # Before, one detector's output; after the fade, the other's.
                previous = synchronous[switch - 1] if losing else envelope[start - BLOCK:start]
                np.testing.assert_array_equal(audio[start - BLOCK:start], previous)
                np.testing.assert_allclose(audio[start + fade:start + BLOCK],
                                           (envelope[here] if losing else sync)[fade:], rtol=0, atol=1e-12)
                # Through the fade, from the sample before it.
                both = np.vstack((np.concatenate(([previous[-1] if losing else np.nan], sync[:fade + 1])),
                                  envelope[start - 1:start + fade + 1]))
                own = np.nanmax(np.abs(np.diff(both, axis=1)))
                difference = np.abs(sync[:fade + 1] - envelope[here][:fade + 1]).max()
                step = np.abs(np.diff(audio[start - 1:start + fade + 1])).max()
                self.assertGreater(difference, 0.2)
                self.assertLessEqual(step, own + difference / fade * 1.001)
                self.assertLess(step, difference / 3)

    def test_a_lock_gained_waits_for_the_band_pass_and_makes_no_thump(self):
        # The band-pass held what the turn left of the carrier before the
        # lock: its output takes its span to hold the carrier found.
        t = times(1.0)
        channel, tones = station(np.ones(len(t)), 0.5, 0.001)
        audio, locks = detect(channel)
        first = locks.index(True)
        self.assertEqual(audio[(first + 1) * BLOCK - 1], envelope_of(channel)[(first + 1) * BLOCK - 1])
        # After the envelope's own start, where its delay held nothing yet.
        self.assertLess(np.abs(audio[round(0.05 * RATE_HZ):]).max(), 0.5 * AM_LEVEL * 1.1)
        steady = slice(round(0.5 * RATE_HZ), None)
        self.assertGreater(correlation(audio[steady], tones[steady]), 0.99)

    def test_silence_stays_silent(self):
        audio, locks = detect(np.zeros(round(RATE_HZ), np.complex64))
        self.assertFalse(any(locks))
        self.assertFalse(np.any(audio))

    def test_an_empty_block_gives_nothing(self):
        search = CarrierSearch(RATE_HZ)
        detector = SyncDetector(RATE_HZ, DELAY, search)
        search.add(np.zeros(0, np.complex64))
        self.assertEqual(len(detector.process(np.zeros(0, np.complex64), np.zeros(0))), 0)

    def test_the_band_pass_is_80_hz_wide_and_lags_by_less_than_the_delay(self):
        length = sam_band_pass_length(RATE_HZ)
        averages = MovingAverages(length, SAM_STAGES)
        impulse = np.zeros(4 * length, np.complex64)
        impulse[0] = 1
        # In pieces, as blocks come: the cumulative sums carry over.
        response = np.concatenate([averages.process(piece) for piece in np.array_split(impulse, 7)]).real
        lag = SAM_STAGES * (length - 1) // 2
        self.assertEqual(int(np.argmax(response)), lag)
        self.assertLessEqual(lag, DELAY)
        self.assertAlmostEqual(float(response.sum()), 1.0, places=9)
        np.testing.assert_allclose(response[:2 * lag + 1], response[2 * lag::-1], atol=1e-12)    # linear phase
        gains = np.abs(np.fft.rfft(response, 1 << 20))
        frequencies = np.fft.rfftfreq(1 << 20, 1 / RATE_HZ)

        def gain_db(low_hz, high_hz):
            inside = (frequencies >= low_hz) & (frequencies <= high_hz)
            return 20 * np.log10(np.maximum(gains[inside], 1e-30))

        self.assertAlmostEqual(float(gain_db(SAM_BAND_HZ / 2, SAM_BAND_HZ / 2 + 0.1)[0]), -6.0, delta=0.3)
        self.assertLess(float(gain_db(100.0, 5_000.0).max()), -39.0)    # the audio, from its lowest

    def test_refused_parameters(self):
        for length, stages in ((0, 3), (5, 0)):
            with self.subTest(length=length, stages=stages), self.assertRaises(ValueError):
                MovingAverages(length, stages)
        for delay in (0, 100):      # none, and less than the band-pass's lag
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                SyncDetector(RATE_HZ, delay, CarrierSearch(RATE_HZ))
        with self.assertRaisesRegex(ValueError, "SAM needs the search for its carrier"):
            _AudioPath("SAM", RATE_HZ, 4)


def sam_in(mode, **changes):
    """``mode`` at AM's filter width."""
    return DemodSettings(frequency_hz=CENTRE + OFFSET, mode=mode, bandwidth_hz=8_000.0, **changes)


class SamModeTest(unittest.TestCase):
    def test_the_squelch_and_the_meter_are_am_s(self):
        # A carrier that starts and stops at once, under the automatic
        # squelch, as AmTest's: the same meter, the same gate, the same tone.
        density = 10 ** (-34.0 / 10) / 2_400_000.0
        silence = np.zeros(round(0.5 * 2_400_000.0), np.complex64)
        signal = np.concatenate((silence, am(1.0, -40.0, 0.5), silence))
        results = {mode: run(signal, sam_in(mode), noise_density=density) for mode in ("AM", "SAM")}
        (am_meters, am_sound), (meters, sound) = results["AM"], results["SAM"]
        for meter, am_meter in zip(meters, am_meters):
            self.assertEqual((meter.level_db, meter.squelch_db, meter.open),
                             (am_meter.level_db, am_meter.squelch_db, am_meter.open))
            self.assertIsNone(am_meter.sam_locked)
        self.assertEqual([meter.open for meter in meters], [False] * 15 + [True] * 30 + [False] * 15)
        self.assertEqual([meter.sam_locked for meter in meters[18:45]], [True] * 27)
        self.assertFalse(any(meter.sam_locked for meter in meters[:15] + meters[50:]))
        self.assertAlmostEqual(tone_amplitude(np.concatenate(sound[20:40]), 1_000.0), 0.5 * AM_LEVEL, delta=0.01)
        self.assertLess(np.abs(np.concatenate(sound)).max(), 0.5 * AM_LEVEL * 1.05)
        self.assertFalse(np.any(np.concatenate(sound[:15] + sound[48:])))     # where AM is silent too
        self.assertFalse(np.any(np.concatenate(am_sound[:15] + am_sound[48:])))
        self.assertEqual([len(block) for block in sound], [len(block) for block in am_sound])

    def test_its_audio_is_am_s_on_a_steady_station(self):
        signal = am(1.0, -40.0, 0.5) + band_noise(1.0, -60.0)
        _, am_sound = run(signal, sam_in("AM", squelch_auto=False, squelch_db=-160.0))
        _, sound = run(signal, sam_in("SAM", squelch_auto=False, squelch_db=-160.0))
        steady = slice(round(0.3 * AUDIO_RATE), None)
        self.assertGreater(correlation(np.concatenate(sound)[steady], np.concatenate(am_sound)[steady]), 0.99)

    def test_the_lock_is_known_without_audio(self):
        demodulator = Demodulator()
        signal = am(0.5, -40.0, 0.5)
        meters = [demodulator.process(signal[start:start + 80_000], CENTRE, 2_400_000.0, sam_in("SAM"), None,
                                      False)[0] for start in range(0, len(signal), 80_000)]
        self.assertTrue(meters[-1].sam_locked)
        self.assertIsNone(demodulator.process(signal[:80_000], CENTRE, 2_400_000.0, sam_in("AM"), None,
                                              False)[0].sam_locked)
        # Back in SAM, the search starts again.
        self.assertFalse(demodulator.process(signal[:80_000], CENTRE, 2_400_000.0, sam_in("SAM"), None,
                                             False)[0].sam_locked)

    def test_a_receiver_slower_than_the_channel_knows_no_lock(self):
        meter, pcm = Demodulator().process(np.ones(4_000, np.complex64), CENTRE, 40_000.0, sam_in("SAM"), None,
                                           True)
        self.assertIsNone(meter.sam_locked)
        self.assertIsNone(pcm)


if __name__ == "__main__":
    unittest.main()
