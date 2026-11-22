"""Tests for the fine spectrum of the VFO's channel (fine.py): its FFTs and
frames on synthetic signals, the demodulator's tap of its front filter, the
engine's block processing with and without it, and the engine's fine frames
for the clients that ask, at the right frequencies on the air in every mode.
No receiver: seeded synthetic samples and fake sources."""

import asyncio
import math
import unittest
from unittest import mock

import numpy as np

from radiolack.demod import CW_PITCH_HZ, DemodSettings, Demodulator
from radiolack.dsp import FftFilter
from radiolack.engine import CLIENT_FINE, Engine, Subscription
from radiolack.radio import _process_block
from radiolack.fine import (FINE_SIZE, FINE_VERSION, FRAME_KIND_FINE, REMOTE_FINE_BINS, FineSpectrum,
                            encode_fine)
from radiolack.scanners import DETECTION_BLOCKS, ChannelWatch, Detector
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM, SpectrumAnalyzer
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RATE_HZ, FakeSource
from tests.test_engine import FakeSources, next_message, waiting

RATE_HZ = 48_000.0      # the channel's rate
BLOCK = 1_600           # the channel's samples in one of the engine's blocks at 2.4 MS/s
WIDE_RATE_HZ = 4 * FAKE_RATE_HZ     # fast enough for WFM's 320 kHz channel
VFO_HZ = FAKE_CENTRE_HZ + 30_000.0  # on a bin of the display spectrum, 30 Hz wide at the fake's rate


def times(seconds, rate_hz=RATE_HZ):
    return np.arange(round(seconds * rate_hz)) / rate_hz


def noise(rng, count, rms):
    return rng.normal(0, rms / math.sqrt(2), count) + 1j * rng.normal(0, rms / math.sqrt(2), count)


def blocks(signal, size=BLOCK):
    return (signal[start:start + size] for start in range(0, len(signal), size))


def strongest_lines(db: np.ndarray, count: int = 2) -> list[int]:
    """The bins of the ``count`` highest local maxima, lowest first."""
    maxima = np.flatnonzero((db[1:-1] > db[:-2]) & (db[1:-1] >= db[2:])) + 1
    return sorted(maxima[np.argsort(db[maxima])[-count:]].tolist())


class FineSpectrumTest(unittest.TestCase):
    def test_two_carriers_20_hz_apart_show_as_two_lines_where_they_are(self):
        t = times(1.0)
        rng = np.random.default_rng(4)
        channel = (0.01 * np.exp(2j * np.pi * 1_000.0 * t) + 0.01 * np.exp(2j * np.pi * 1_020.0 * t + 1.0)
                   + noise(rng, len(t), 1e-4)).astype(np.complex64)
        fine = FineSpectrum()
        frames = [frame for block in blocks(channel) for frame in fine.add(block, RATE_HZ)]
        self.assertTrue(frames)
        db = 10 * np.log10(frames[-1])
        bin_hz = RATE_HZ / fine.size
        frequencies = (np.arange(fine.size) - fine.size // 2) * bin_hz
        strongest = strongest_lines(db)
        for index, frequency_hz in zip(strongest, (1_000.0, 1_020.0)):
            self.assertLessEqual(abs(frequencies[index] - frequency_hz), bin_hz / 2)
        self.assertGreater(min(db[strongest]) - db[strongest[0]:strongest[1] + 1].min(), 6.0)
        self.assertLess(np.median(db), max(db) - 60)

    def test_about_ten_frames_a_second_each_the_mean_of_those_since_the_last(self):
        for rate_hz, per_frame in ((48_000.0, 1), (320_000.0, 4)):
            with self.subTest(rate_hz=rate_hz):
                t = times(3, rate_hz)
                bin_hz = rate_hz / FINE_SIZE
                tone = np.exp(2j * np.pi * 100 * bin_hz * t).astype(np.complex64)     # full scale, on a bin
                fine = FineSpectrum()
                block = round(rate_hz / 30)
                frames = [frame for start in range(0, len(t), block)
                          for frame in fine.add(tone[start:start + block], rate_hz)]
                self.assertIn(len(frames), range(29, 31))
                for frame in frames:
                    self.assertAlmostEqual(float(frame[FINE_SIZE // 2 + 100]), 1.0, places=4)
                # The tone in every other FFT's samples: a frame of several FFTs reads their mean.
                fine = FineSpectrum()
                every_other = tone * ((np.arange(len(tone)) // FINE_SIZE) % 2 == 0)
                levels = [float(frame[FINE_SIZE // 2 + 100]) for start in range(0, len(t), block)
                          for frame in fine.add(every_other[start:start + block], rate_hz)]
                if per_frame == 1:
                    self.assertGreater(max(levels), 0.99)
                else:
                    self.assertTrue(all(0.2 < level < 0.8 for level in levels), levels)

    def test_another_rate_starts_again(self):
        fine = FineSpectrum()
        self.assertEqual(fine.add(np.ones(8_000, np.complex64), 48_000.0), [])
        self.assertEqual(fine.add(np.ones(4_000, np.complex64), 320_000.0), [])   # not yet 8192 at this rate
        self.assertEqual(len(fine.add(np.ones(4_200, np.complex64), 320_000.0)), 0)
        self.assertEqual(len(fine.add(np.ones(32_000, np.complex64), 320_000.0)), 1)

    def test_another_setting_drops_the_partial_mean_and_the_samples_kept(self):
        # A tone under one setting, then another under the next, before and
        # after the first frame: no frame holds the first tone.
        bin_hz = RATE_HZ / FINE_SIZE
        t = times(1.0)
        first = np.exp(2j * np.pi * 200 * bin_hz * t).astype(np.complex64)
        second = np.exp(2j * np.pi * -300 * bin_hz * t).astype(np.complex64)
        for kept in (8_000, 20_000):
            with self.subTest(kept=kept):
                fine = FineSpectrum()
                before = [frame for block in blocks(first[:kept]) for frame in fine.add(block, RATE_HZ, "VFO 1")]
                self.assertEqual(len(before), 0 if kept < FINE_SIZE else 3)
                after = [frame for block in blocks(second) for frame in fine.add(block, RATE_HZ, "VFO 2")]
                self.assertIn(len(after), range(8, 11))
                for frame in after:
                    self.assertLess(float(frame[FINE_SIZE // 2 + 200]), 1e-6)
                    self.assertAlmostEqual(float(frame[FINE_SIZE // 2 - 300]), 1.0, places=4)
                # Reset by hand too.
                fine.reset()
                self.assertEqual(fine.add(second[:FINE_SIZE - 1], RATE_HZ, "VFO 2"), [])

    def test_a_frame_carries_the_spectrum_frames_header_and_its_codes(self):
        codes = np.arange(FINE_SIZE) % 256
        frame = encode_fine(2 ** 32 + 5, 145_000_700.5, 48_000.0, -160.0, 0.625, codes)
        kind, version, flags, sequence, centre, width, db_min, db_step, level, squelch = FRAME_HEADER.unpack_from(frame)
        self.assertEqual((kind, version, flags, sequence, centre, width, db_min, db_step),
                         (FRAME_KIND_FINE, FINE_VERSION, 0, 5, 145_000_700.5, 48_000.0, -160.0, 0.625))
        self.assertTrue(math.isnan(level) and math.isnan(squelch))
        self.assertEqual(frame[FRAME_HEADER.size:], codes.astype(np.uint8).tobytes())
        self.assertNotEqual(FRAME_KIND_FINE, FRAME_KIND_SPECTRUM)

    def test_the_size_must_be_a_power_of_two(self):
        for size in (8, 100):
            with self.subTest(size=size), self.assertRaises(ValueError):
                FineSpectrum(size)


def impulsive_block(start: int, count: int, rate_hz: float = FAKE_RATE_HZ, impulses: bool = True) -> np.ndarray:
    """``count`` samples from ``start``: a tone at VFO_HZ, weak noise, and
    with ``impulses`` three samples at -6 dBFS every 2048 samples."""
    index = start + np.arange(count)
    samples = 0.01 * np.exp(2j * np.pi * (VFO_HZ - FAKE_CENTRE_HZ) * index / rate_hz)
    samples = samples + noise(np.random.default_rng((7, start)), count, 1e-3)
    if impulses:
        samples[index % 2_048 < 3] = 0.5
    return samples.astype(np.complex64)


class Pipeline:
    """The engine's objects for one receiver, through ``_process_block``."""

    def __init__(self, settings: DemodSettings, fine: bool, rate_hz: float = FAKE_RATE_HZ) -> None:
        self.analyzer, self.demodulator = SpectrumAnalyzer(), Demodulator()
        self.detector, self.watch = Detector(), ChannelWatch()
        self.settings = settings
        self.rate_hz = rate_hz
        self.fine = FineSpectrum() if fine else None

    def process(self, samples: np.ndarray, key: object = "steady", rate_scale: float = 1.0,
                settings: DemodSettings | None = None):
        return _process_block(self.analyzer, self.demodulator, self.detector, self.watch, samples, FAKE_CENTRE_HZ,
                              self.rate_hz, True, settings or self.settings, True, 12.0, DETECTION_BLOCKS, None,
                              None, rate_scale, None if self.fine is None else (self.fine, key, rate_scale))


class ProcessBlockTest(unittest.TestCase):
    """The fine spectrum in the engine's processing of a block."""

    def test_without_it_the_processing_is_unchanged_and_nothing_is_tapped(self):
        # The blanker on, which a tap would make filter the samples zeroed.
        settings = DemodSettings(VFO_HZ, "AM", 8_000.0, blanker=True)
        plain, fine = Pipeline(settings, fine=False), Pipeline(settings, fine=True)
        calls = []      # each block's filterings: of every sample, of a few
        process, process_sparse = FftFilter.process, FftFilter.process_sparse

        def counted(filter_, samples):
            calls[-1][0] += 1
            return process(filter_, samples)

        def counted_sparse(filter_, count, indices, values):
            calls[-1][1] += 1
            return process_sparse(filter_, count, indices, values)

        frames = 0
        with (mock.patch.object(FftFilter, "process", autospec=True, side_effect=counted),
              mock.patch.object(FftFilter, "process_sparse", autospec=True, side_effect=counted_sparse)):
            for index in range(45):
                block = impulsive_block(index * 8_192, 8_192)
                calls.append([0, 0])
                without = plain.process(block)
                plain_calls = calls[-1]
                self.assertEqual(plain_calls[1], 0)
                self.assertIsNone(plain.demodulator._unblanked)
                calls.append([0, 0])
                with_it = fine.process(block)
                # The samples zeroed alone filtered again, for the samples as received.
                self.assertEqual(calls[-1], [plain_calls[0], 1])
                for got, expected in zip(with_it[:7], without[:7]):
                    if isinstance(expected, np.ndarray):
                        np.testing.assert_array_equal(got, expected)
                    else:
                        self.assertEqual(got, expected)
                self.assertEqual(without[7], [])
                frames += len(with_it[7])
        self.assertIn(frames, range(14, 16))    # 1.5 s of signal
        self.assertGreater(without[2].blanker_impulses, 0)

    def test_it_takes_the_samples_as_received_whatever_the_blanker(self):
        codes = {}
        levels = {}
        for blanker in (False, True):
            pipeline = Pipeline(DemodSettings(VFO_HZ, "AM", 8_000.0, blanker=blanker), fine=True)
            results = [pipeline.process(impulsive_block(index * 8_192, 8_192)) for index in range(45)]
            codes[blanker] = [frame for result in results for frame in result[7]]
            levels[blanker] = results[-1][2].level_db
        self.assertEqual(len(codes[True]), len(codes[False]))
        # The same within rounding: a level on a code's edge may fall on
        # the next code, a few bins in a thousand at most.
        for blanked, received in zip(codes[True], codes[False]):
            difference = np.abs(blanked.astype(int) - received)
            self.assertLessEqual(int(difference.max()), 1)
            self.assertLess(np.count_nonzero(difference), len(received) / 1000)
        self.assertLess(levels[True], levels[False] - 0.3)     # the channel itself is blanked

    def test_about_ten_frames_a_second_at_2_4_ms_s(self):
        rate_hz = 2_400_000.0
        pipeline = Pipeline(DemodSettings(FAKE_CENTRE_HZ + 300_000, "AM", 8_000.0), fine=True, rate_hz=rate_hz)
        rng = np.random.default_rng(3)
        frames = sum(len(pipeline.process(noise(rng, 80_000, 0.01).astype(np.complex64))[7]) for _ in range(90))
        self.assertIn(frames, range(29, 31))

    def test_no_frame_and_no_error_where_the_channel_receives_nothing(self):
        rng = np.random.default_rng(2)
        cases = (
            ("the VFO beyond the span", DemodSettings(FAKE_CENTRE_HZ + FAKE_RATE_HZ, "AM", 8_000.0), FAKE_RATE_HZ),
            ("a receiver slower than the channel", DemodSettings(VFO_HZ, "AM", 8_000.0), 32_000.0),
            ("WFM on a receiver slower than its channel", DemodSettings(VFO_HZ, "WFM", 180_000.0), FAKE_RATE_HZ),
        )
        for name, settings, rate_hz in cases:
            with self.subTest(name):
                pipeline = Pipeline(settings, fine=True, rate_hz=rate_hz)
                for _ in range(30):
                    self.assertEqual(pipeline.process(noise(rng, 8_192, 0.01).astype(np.complex64))[7], [])

    def test_a_vfo_that_moves_at_every_block_gives_no_frame_until_it_stays(self):
        pipeline = Pipeline(DemodSettings(VFO_HZ, "AM", 8_000.0), fine=True)
        for index in range(30):
            settings = DemodSettings(VFO_HZ + 1_000 * (index % 7), "AM", 8_000.0)
            result = pipeline.process(impulsive_block(index * 8_192, 8_192, impulses=False),
                                      key=settings.frequency_hz, settings=settings)
            self.assertEqual(result[7], [])
        # Once it stays, frames come: the first after 8192 samples of the channel.
        frames = [len(pipeline.process(impulsive_block((30 + index) * 8_192, 8_192, impulses=False))[7])
                  for index in range(9)]
        self.assertEqual(frames[:5], [0, 0, 0, 0, 0])
        self.assertGreater(sum(frames), 0)


class FrontTapTest(unittest.TestCase):
    """The demodulator's tap of its front filter, beside the channel's."""

    def run_taps(self, blanker: bool, channel_from: int = 0) -> tuple[list, list]:
        demodulator = Demodulator()
        settings = DemodSettings(VFO_HZ, "AM", 8_000.0, blanker=blanker)
        fronts, channels = [], []
        for index in range(12):
            samples = impulsive_block(index * 8_192, 8_192)
            demodulator.process(samples, FAKE_CENTRE_HZ, FAKE_RATE_HZ, settings, None, False,
                                channels.append if index >= channel_from else None, fronts.append)
        return fronts, channels

    def assert_same(self, got: np.ndarray, expected: np.ndarray) -> None:
        """The same samples within rounding: the samples as received filter
        to the blanked ones' output plus that of the samples zeroed."""
        self.assertEqual(got.shape, expected.shape)
        np.testing.assert_allclose(got, expected, rtol=0, atol=1e-6 * float(np.abs(expected).max()))

    def test_both_taps_take_the_samples_as_received_with_the_blanker(self):
        received_fronts, received_channels = self.run_taps(blanker=False)
        fronts, channels = self.run_taps(blanker=True)
        for got, expected in zip(fronts + channels, received_fronts + received_channels, strict=True):
            self.assert_same(got, expected)

    def test_a_channel_tap_that_begins_later_takes_them_from_its_second_block(self):
        received_fronts, received_channels = self.run_taps(blanker=False)
        fronts, channels = self.run_taps(blanker=True, channel_from=5)
        for got, expected in zip(fronts, received_fronts, strict=True):
            self.assert_same(got, expected)
        self.assertEqual(len(channels), 7)
        for got, expected in zip(channels[1:], received_channels[6:], strict=True):
            self.assert_same(got, expected)


class CarriersSource(FakeSource):
    """A fake source with carriers at frequencies on the air, each with its
    amplitude, continuous across reads, over white noise of -80 dBFS, from
    a receiver that samples ``rate_scale`` times faster than its rate: a
    source of ``kind``."""

    def __init__(self, carriers: dict[float, float], rate_hz: float = FAKE_RATE_HZ, rate_scale: float = 1.0,
                 centre_hz: float = FAKE_CENTRE_HZ, kind: str = "test") -> None:
        super().__init__()
        self.kind = kind
        self.carriers = dict(carriers)
        self.sample_rate_hz = rate_hz
        self.rate_scale = rate_scale
        self.centre_hz = centre_hz
        self._sent = 0
        self._rng = np.random.default_rng(9)

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.002)
        self.reads += 1
        seconds = (self._sent + np.arange(count)) / (self.sample_rate_hz * self.rate_scale)
        self._sent += count
        samples = noise(self._rng, count, 1e-4)
        for frequency_hz, amplitude in self.carriers.items():
            samples = samples + amplitude * np.exp(2j * np.pi * (frequency_hz - self.centre_hz) * seconds)
        return samples.astype(np.complex64)


def ready_fine(subscription: Subscription) -> list[bytes]:
    """The fine frames among the messages ready for a client, now."""
    return [message for message in waiting(subscription)
            if isinstance(message, bytes) and message[0] == FRAME_KIND_FINE]


def decode(frame: bytes) -> tuple[int, int, float, float, np.ndarray]:
    """A fine frame's kind, sequence, centre, width and levels in dBFS."""
    kind, _, _, sequence, centre, width, db_min, db_step, _, _ = FRAME_HEADER.unpack_from(frame)
    codes = np.frombuffer(frame, np.uint8, offset=FRAME_HEADER.size)
    return kind, sequence, centre, width, db_min + codes * db_step


async def fine_frames(subscription: Subscription, count: int, centre_hz: float | None = None) -> list[bytes]:
    """The next ``count`` fine frames a client receives, with ``centre_hz``
    if given, after the first such: frames of the setting before a change
    may come first."""
    frames: list[bytes] = []
    skipped = False
    while len(frames) < count:
        message = await next_message(subscription)
        if not isinstance(message, bytes) or message[0] != FRAME_KIND_FINE:
            continue
        if centre_hz is not None and FRAME_HEADER.unpack_from(message)[4] != centre_hz:
            continue
        if centre_hz is not None and not skipped:
            skipped = True
            continue
        frames.append(message)
    return frames


class EngineFineTest(unittest.IsolatedAsyncioTestCase):
    """The engine's fine frames, for the clients that ask."""

    def start(self, source: FakeSource, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop() -> None:
            engine.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return engine

    def assert_lines(self, frame: bytes, frequencies: tuple[float, ...]) -> None:
        """The frame shows a line at each of ``frequencies`` on the air, the
        highest it holds, within a bin: half a bin, and the codes' step,
        which may make the second nearest bin of a line between two read
        as high as the nearest."""
        _, _, centre, width, db = decode(frame)
        bin_hz = width / len(db)
        found = [centre - width / 2 + index * bin_hz for index in strongest_lines(db, len(frequencies))]
        for got, expected in zip(found, sorted(frequencies), strict=True):
            self.assertLessEqual(abs(got - expected), bin_hz, (found, frequencies))

    async def test_two_carriers_20_hz_apart_on_the_air_in_every_mode_for_the_client_that_asks(self):
        lines = (VFO_HZ + 1_000, VFO_HZ + 1_020)
        engine = self.start(CarriersSource(dict.fromkeys(lines, 0.01)))
        listener, other = engine.subscribe(), engine.subscribe()
        engine.radios[0].set_fine(listener, True)
        for mode, width_hz in (("AM", 8_000), ("USB", 2_700), ("LSB", 2_700), ("CW", 500)):
            with self.subTest(mode=mode):
                engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": mode, "bandwidth_hz": width_hz})
                centre_hz = VFO_HZ - (CW_PITCH_HZ if mode == "CW" else 0.0)
                frames = await fine_frames(listener, 2, centre_hz)
                for frame in frames:
                    kind, _, _, width, db = decode(frame)
                    self.assertEqual((kind, width, len(db)), (FRAME_KIND_FINE, 48_000.0, FINE_SIZE))
                    self.assert_lines(frame, lines)
                sequences = [decode(frame)[1] for frame in frames]
                self.assertEqual(sequences[1], sequences[0] + 1)
        self.assertFalse(ready_fine(other))

    async def test_frequencies_on_the_air_with_a_correction_that_scales_the_vfo_s_distance(self):
        # Exaggerated, so that a width or a pitch left unscaled would put
        # the lines 30 Hz, five bins, away.
        scale = 1.002
        lines = (VFO_HZ + 15_000, VFO_HZ + 15_020)
        engine = self.start(CarriersSource(dict.fromkeys(lines, 0.01), rate_scale=scale))
        listener = engine.subscribe()
        engine.radios[0].set_fine(listener, True)
        engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": "CW", "bandwidth_hz": 500})
        for frame in await fine_frames(listener, 2, VFO_HZ - CW_PITCH_HZ * scale):
            self.assertEqual(decode(frame)[3], 48_000.0 * scale)
            self.assert_lines(frame, lines)

    async def test_wfm_s_frames_at_their_resolution(self):
        lines = (VFO_HZ + 5_000, VFO_HZ + 5_200)
        engine = self.start(CarriersSource(dict.fromkeys(lines, 0.01), rate_hz=WIDE_RATE_HZ))
        listener = engine.subscribe()
        engine.radios[0].set_fine(listener, True)
        engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": "WFM", "bandwidth_hz": 180_000})
        for frame in await fine_frames(listener, 2, VFO_HZ):
            _, _, _, width, db = decode(frame)
            self.assertEqual((width, len(db)), (320_000.0, FINE_SIZE))     # 39 Hz a bin
            self.assert_lines(frame, lines)

    async def test_a_carrier_reads_the_level_that_the_display_spectrum_gives(self):
        # On a bin of both, so that neither window's scalloping counts: the
        # codes' step, 0.625 dB, and the front filter's ripple remain.
        engine = self.start(CarriersSource({VFO_HZ: 0.1}))
        listener = engine.subscribe()
        engine.radios[0].set_fine(listener, True)
        engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": "AM"})
        [frame] = await fine_frames(listener, 1, VFO_HZ)
        fine_db = decode(frame)[4][FINE_SIZE // 2]
        while not isinstance(message := await next_message(listener), bytes) or message[0] != FRAME_KIND_SPECTRUM:
            pass
        db_min, db_step = FRAME_HEADER.unpack_from(message)[6:8]
        display_db = db_min + message[FRAME_HEADER.size + 4096 + 1_000] * db_step
        self.assertAlmostEqual(fine_db, -20.0, delta=1.0)
        self.assertAlmostEqual(fine_db, display_db, delta=1.0)

    async def test_a_remote_client_gets_2048_bins_each_the_highest_of_its_group(self):
        engine = self.start(CarriersSource({VFO_HZ + 1_000: 0.01}))
        remote = engine.subscribe(device="phone")
        engine.radios[0].set_fine(remote, True)
        engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": "AM"})
        frame = (await fine_frames(remote, 1, VFO_HZ))[0]
        kind, _, centre, width, db = decode(frame)
        self.assertEqual((kind, centre, width, len(db)), (FRAME_KIND_FINE, VFO_HZ, 48_000.0, REMOTE_FINE_BINS))
        self.assert_lines(frame, (VFO_HZ + 1_000,))
        # The same frame as this computer's interface receives, in fewer bins.
        local = engine.subscribe()
        engine.radios[0].set_fine(local, True)
        waiting(local), waiting(remote)
        codes = np.random.default_rng(1).integers(0, 256, FINE_SIZE, np.uint8)
        engine.radios[0]._publish_fine(engine.radios[0]._fine_measure((FAKE_CENTRE_HZ, FAKE_RATE_HZ), 1.0), [codes])
        [full] = ready_fine(local)
        [light] = ready_fine(remote)
        self.assertEqual(full[:FRAME_HEADER.size], light[:FRAME_HEADER.size])
        np.testing.assert_array_equal(np.frombuffer(full, np.uint8, offset=FRAME_HEADER.size), codes)
        np.testing.assert_array_equal(np.frombuffer(light, np.uint8, offset=FRAME_HEADER.size),
                                      codes.reshape(REMOTE_FINE_BINS, -1).max(axis=1))

    async def test_computed_only_while_a_client_asks_and_once_for_several(self):
        engine = self.start(CarriersSource({VFO_HZ: 0.01}))
        added = []
        add = FineSpectrum.add

        def counted(spectrum, *args):
            added.append(spectrum)
            return add(spectrum, *args)

        with mock.patch.object(FineSpectrum, "add", autospec=True, side_effect=counted):
            first, second = engine.subscribe(), engine.subscribe()
            source = engine.radios[0].source
            while source.reads < 20:
                await next_message(first)
            self.assertEqual((added, engine.radios[0]._fine), ([], None))
            # Asked twice by one, once by another: one spectrum, one frame each time.
            engine.radios[0].set_fine(first, True)
            engine.radios[0].set_fine(first, True)
            engine.radios[0].set_fine(second, True)
            spectrum = engine.radios[0]._fine
            frames = {subscription: await fine_frames(subscription, 3) for subscription in (first, second)}
            self.assertEqual({id(item) for item in added}, {id(spectrum)})
            for subscription in (first, second):
                sequences = [decode(frame)[1] for frame in frames[subscription]]
                self.assertEqual(len(set(sequences)), 3)
            # Off for one: the other still gets them, from the same spectrum.
            engine.radios[0].set_fine(first, False)
            engine.radios[0].set_fine(first, False)       # off again, without having asked again
            self.assertIs(engine.radios[0]._fine, spectrum)
            waiting(first)
            await fine_frames(second, 2)
            self.assertFalse(ready_fine(first))
            # The last one leaves: nothing more is computed.
            engine.unsubscribe(second)
            self.assertIsNone(engine.radios[0]._fine)
            reads = source.reads
            while source.reads < reads + 2:     # the block in progress, then one more
                await next_message(first)
            computed = len(added)
            while source.reads < reads + 12:
                await next_message(first)
            self.assertEqual(len(added), computed)

    async def test_a_slow_client_keeps_the_latest_frames_only(self):
        engine = self.start(CarriersSource({VFO_HZ: 0.01}), queue_size=4)
        slow, reader = engine.subscribe(), engine.subscribe()
        for subscription in (slow, reader):
            engine.radios[0].set_fine(subscription, True)
        frames = await fine_frames(reader, 3 * CLIENT_FINE + 2)
        kept = ready_fine(slow)
        self.assertLessEqual(len(kept), CLIENT_FINE)
        self.assertTrue(kept)
        self.assertGreaterEqual(decode(kept[-1])[1], decode(frames[-1])[1])

    async def test_idle_once_the_only_client_leaves(self):
        engine = self.start(CarriersSource({VFO_HZ: 0.01}))
        subscription = engine.subscribe()
        engine.radios[0].set_fine(subscription, True)
        await fine_frames(subscription, 1)
        engine.unsubscribe(subscription)
        self.assertIsNone(engine.radios[0]._fine)
        self.assertFalse(engine.radios[0]._wanted.is_set())     # the engine stops reading after the block in progress

    async def test_a_frame_of_a_block_whose_setting_changed_meanwhile_is_dropped(self):
        engine = Engine(CarriersSource({VFO_HZ: 0.01}))
        self.addCleanup(engine.close)
        subscription = engine.subscribe()
        engine.radios[0].set_fine(subscription, True)
        waiting(subscription)
        label = (FAKE_CENTRE_HZ, FAKE_RATE_HZ)
        codes = np.zeros(FINE_SIZE, np.uint8)
        for change in ({"frequency_hz": VFO_HZ + 5}, {"mode": "CW"}, {"dc_removal": False}):
            with self.subTest(change=change):
                fine = engine.radios[0]._fine_measure(label, 1.0)
                if "dc_removal" in change:
                    engine.radios[0].configure(change)
                else:
                    engine.radios[0].configure_demod(change)
                engine.radios[0]._publish_fine(fine, [codes])
                self.assertFalse(ready_fine(subscription))
        fine = engine.radios[0]._fine_measure(label, 1.0)
        engine.radios[0]._publish_fine(fine, [codes])
        [frame] = ready_fine(subscription)
        self.assertEqual(decode(frame)[2], VFO_HZ + 5 - CW_PITCH_HZ)
        # A spectrum dropped meanwhile, as the last client stopped asking, sends nothing either.
        fine = engine.radios[0]._fine_measure(label, 1.0)
        engine.radios[0].set_fine(subscription, False)
        engine.radios[0].set_fine(subscription, True)
        engine.radios[0]._publish_fine(fine, [codes])
        self.assertFalse(ready_fine(subscription))

    async def test_a_switch_of_source_starts_again_from_the_new_receiver(self):
        sources = FakeSources(test=lambda: CarriersSource({VFO_HZ: 0.01}),
                              other=lambda: CarriersSource({}, centre_hz=FAKE_CENTRE_HZ + 1e6, kind="other"))
        engine = self.start(None, sources=sources)
        subscription = engine.subscribe()
        engine.radios[0].set_fine(subscription, True)
        engine.radios[0].configure_demod({"frequency_hz": VFO_HZ, "mode": "AM"})
        await fine_frames(subscription, 1, VFO_HZ)
        engine.radios[0].configure({"kind": "other"})
        vfo_hz = engine.radios[0].demod_state()["frequency_hz"]       # the other receiver's, a quarter of its span up
        self.assertNotEqual(vfo_hz, VFO_HZ)
        [frame] = await fine_frames(subscription, 1, vfo_hz)
        self.assertLess(decode(frame)[4].max(), -60)    # noise only: nothing of the first receiver's carrier


if __name__ == "__main__":
    unittest.main()
