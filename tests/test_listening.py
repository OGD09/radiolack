"""Tests for the listening tools' prototypes (tools/listening.py), each on
synthetic signals that exercise it, and for the tool that measures their
cost (tools/measure_listening.py), whose figures they leave alone: they
depend on the computer, the tests on nothing but the samples. Synchronous
AM, in the package, has its own tests (test_sam.py)."""

import importlib.util
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import audio_taps
from radiolack.dsp import FftFilter

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # the measuring tool imports the prototypes by this name
    spec.loader.exec_module(module)
    return module


listening = load("listening")
measuring = load("measure_listening")

RATE_HZ = 48_000.0      # the channel's rate, and the detector's output's
BLOCK = 1_600           # the channel's samples in one of the engine's blocks at 2.4 MS/s
AUDIO_RATE_HZ = 12_000.0


def times(seconds):
    return np.arange(round(seconds * RATE_HZ)) / RATE_HZ


def voice(t):
    """A voice-like sound, 0.1 rms: harmonics of a pitch that glides, under syllables."""
    pitch_phase = 2 * np.pi * (150 * t - 40 / (2 * np.pi * 1.7) * np.cos(2 * np.pi * 1.7 * t)
                               - 15 / (2 * np.pi * 4.3) * np.cos(2 * np.pi * 4.3 * t))
    sound = sum(np.sin(k * pitch_phase) / k for k in range(1, 25))
    sound *= 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t) ** 2
    return 0.1 * sound / np.std(sound)


def noise(rng, count, rms):
    return (rng.normal(0, rms / math.sqrt(2), count) + 1j * rng.normal(0, rms / math.sqrt(2), count))


def blocks(signal, size=BLOCK):
    return (signal[start:start + size] for start in range(0, len(signal), size))


class AutoNotchTest(unittest.TestCase):
    def run_notch(self, signals, seconds):
        """Each of ``signals``, a function of time, through its own audio
        filter, all with the taps that the notch gives for their sum; the
        same through the plain filter; and when the notches changed."""
        t = times(seconds)
        parts = [signal(t) for signal in signals]
        notch = listening.AutoNotch(RATE_HZ)
        notched = [FftFilter(audio_taps("AM", RATE_HZ), RATE_HZ, down=4) for _ in parts]
        plain = [FftFilter(audio_taps("AM", RATE_HZ), RATE_HZ, down=4) for _ in parts]
        outputs = [[] for _ in parts]
        references = [[] for _ in parts]
        changes = []
        for start in range(0, len(t), BLOCK):
            pieces = [part[start:start + BLOCK] for part in parts]
            taps = notch.process(sum(pieces))
            if taps is not None:
                changes.append(((start + BLOCK) / RATE_HZ, notch.notches))
                for audio_filter in notched:
                    audio_filter.set_taps(taps)
            for piece, audio_filter, output in zip(pieces, notched, outputs):
                output.append(audio_filter.process(piece.astype(np.complex64)).real)
            for piece, audio_filter, reference in zip(pieces, plain, references):
                reference.append(audio_filter.process(piece.astype(np.complex64)).real)
        return ([np.concatenate(output) for output in outputs], [np.concatenate(ref) for ref in references],
                changes)

    @staticmethod
    def power_db(signal, start_s, stop_s):
        window = slice(round(start_s * AUDIO_RATE_HZ), round(stop_s * AUDIO_RATE_HZ))
        return 10 * math.log10(np.mean(signal[window] ** 2))

    def test_a_whistle_goes_within_a_second_the_voice_stays_and_the_notch_is_released(self):
        def whistle(t):
            return 0.1 * np.sin(2 * np.pi * 1234.5 * t) * ((t >= 1) & (t < 4))

        (heard_voice, heard_whistle), (voice_alone, whistle_alone), changes = self.run_notch((voice, whistle), 6.0)
        self.assertEqual(len(changes), 2, changes)
        (on_s, notches), (off_s, released) = changes
        self.assertLessEqual(on_s, 2.0)                     # within a second of the whistle's start
        self.assertGreaterEqual(on_s, 1.5)                  # not before it was steady for half a second
        self.assertEqual(len(notches), 1)
        self.assertAlmostEqual(notches[0], 1234.5, delta=1.0)
        self.assertEqual(released, ())
        self.assertGreaterEqual(off_s, 5.0)                 # a second after it stops
        self.assertLessEqual(off_s, 5.5)
        # The filters' delay is 21 ms: these windows hold the notched taps alone.
        self.assertLessEqual(self.power_db(heard_whistle, 2.1, 3.9) - self.power_db(whistle_alone, 2.1, 3.9), -20)
        self.assertAlmostEqual(self.power_db(heard_voice, 2.1, 3.9), self.power_db(voice_alone, 2.1, 3.9),
                               delta=1.0)

    def test_a_voice_alone_is_not_notched(self):
        _, _, changes = self.run_notch((voice,), 6.0)
        self.assertEqual(changes, [])

    def test_four_lines_at_most_the_strongest_and_none_outside_100_hz_to_5_khz(self):
        def lines(t):
            frequencies = (300.0, 800.0, 1500.0, 2200.0, 3100.0, 4000.0)
            levels = (0.05, 0.2, 0.03, 0.15, 0.1, 0.08)
            return sum(level * np.sin(2 * np.pi * f * t) for f, level in zip(frequencies, levels))

        def outside(t):
            return 0.2 * (np.sin(2 * np.pi * 60 * t) + np.sin(2 * np.pi * 5_400 * t))

        _, _, changes = self.run_notch((voice, lines, outside), 2.0)
        self.assertEqual(len(changes), 1, changes)
        self.assertEqual([round(f) for f in changes[0][1]], [800, 2200, 3100, 4000])

    def test_a_wide_line_is_not_notched(self):
        def wide(t):
            # A tone whose frequency wanders by 60 Hz, three times a second.
            return 0.1 * np.sin(2 * np.pi * (1000 * t - 30 / (2 * np.pi * 3) * np.cos(2 * np.pi * 3 * t)))

        _, _, changes = self.run_notch((voice, wide), 3.0)
        self.assertEqual(changes, [])

    def test_the_taps_keep_the_audio_filters_length(self):
        plain = audio_taps("AM", RATE_HZ)
        np.testing.assert_allclose(listening.notch_taps((), RATE_HZ), plain)
        self.assertEqual(len(listening.notch_taps((700.0, 1300.0, 2100.0, 3400.0), RATE_HZ)), len(plain))


class NoiseBlankerTest(unittest.TestCase):
    def test_impulses_go_with_two_samples_on_each_side_and_nothing_else(self):
        rng = np.random.default_rng(7)
        signal = noise(rng, 20_000, 0.01).astype(np.complex64)
        hit = signal.copy()
        for at in (100, 5_000, 12_345):
            hit[at:at + 3] = 0.5
        blanker = listening.NoiseBlanker()
        blanked = blanker.process(hit)
        expected = signal.copy()
        for at in (100, 5_000, 12_345):
            expected[at - 2:at + 5] = 0
        np.testing.assert_array_equal(blanked, expected)
        self.assertEqual(hit[100], 0.5)         # the samples given stay as they were

    def test_a_block_without_impulses_is_untouched(self):
        rng = np.random.default_rng(8)
        t = np.arange(20_000) / 2.4e6
        # Noise, and a carrier far stronger than the noise: its magnitude is the median.
        for signal in (noise(rng, 20_000, 0.01), 0.3 * np.exp(2j * np.pi * 1e5 * t) + noise(rng, 20_000, 0.01),
                       np.zeros(20_000)):
            signal = signal.astype(np.complex64)
            with self.subTest(rms=float(np.std(signal))):
                self.assertIs(listening.NoiseBlanker().process(signal), signal)

    def test_an_impulse_at_a_blocks_end_reaches_the_next(self):
        rng = np.random.default_rng(9)
        first = noise(rng, 1_000, 0.01).astype(np.complex64)
        second = noise(rng, 1_000, 0.01).astype(np.complex64)
        first[-1] = 0.5
        blanker = listening.NoiseBlanker()
        self.assertFalse(np.any(blanker.process(first)[-3:]))
        after = blanker.process(second)
        self.assertFalse(np.any(after[:2]))
        np.testing.assert_array_equal(after[2:], second[2:])
        self.assertIs(blanker.process(second), second)


class FineSpectrumTest(unittest.TestCase):
    def test_two_carriers_20_hz_apart_show_as_two_lines_where_they_are(self):
        t = times(1.0)
        rng = np.random.default_rng(4)
        channel = (0.01 * np.exp(2j * np.pi * 1_000.0 * t) + 0.01 * np.exp(2j * np.pi * 1_020.0 * t + 1.0)
                   + noise(rng, len(t), 1e-4)).astype(np.complex64)
        fine = listening.FineSpectrum()
        frames = [frame for block in blocks(channel) for frame in fine.add(block, RATE_HZ)]
        self.assertTrue(frames)
        db = 10 * np.log10(frames[-1])
        bin_hz = RATE_HZ / fine.size
        frequencies = (np.arange(fine.size) - fine.size // 2) * bin_hz
        maxima = np.flatnonzero((db[1:-1] > db[:-2]) & (db[1:-1] >= db[2:])) + 1
        strongest = sorted(maxima[np.argsort(db[maxima])[-2:]])
        for index, frequency_hz in zip(strongest, (1_000.0, 1_020.0)):
            self.assertLessEqual(abs(frequencies[index] - frequency_hz), bin_hz / 2)
        self.assertGreater(min(db[strongest]) - db[strongest[0]:strongest[1] + 1].min(), 6.0)
        self.assertLess(np.median(db), max(db) - 60)

    def test_about_ten_frames_a_second_each_the_mean_of_those_since_the_last(self):
        for rate_hz, per_frame in ((48_000.0, 1), (320_000.0, 4)):
            with self.subTest(rate_hz=rate_hz):
                t = np.arange(round(3 * rate_hz)) / rate_hz
                bin_hz = rate_hz / listening.FINE_SIZE
                tone = np.exp(2j * np.pi * 100 * bin_hz * t).astype(np.complex64)     # full scale, on a bin
                fine = listening.FineSpectrum()
                block = round(rate_hz / 30)
                frames = [frame for start in range(0, len(t), block)
                          for frame in fine.add(tone[start:start + block], rate_hz)]
                self.assertIn(len(frames), range(29, 31))
                for frame in frames:
                    self.assertAlmostEqual(float(frame[listening.FINE_SIZE // 2 + 100]), 1.0, places=4)
                # The tone in every other FFT's samples: a frame of several FFTs reads their mean.
                fine = listening.FineSpectrum()
                every_other = tone * ((np.arange(len(tone)) // listening.FINE_SIZE) % 2 == 0)
                levels = [float(frame[listening.FINE_SIZE // 2 + 100]) for start in range(0, len(t), block)
                          for frame in fine.add(every_other[start:start + block], rate_hz)]
                if per_frame == 1:
                    self.assertGreater(max(levels), 0.99)
                else:
                    self.assertTrue(all(0.2 < level < 0.8 for level in levels), levels)

    def test_another_rate_starts_again(self):
        fine = listening.FineSpectrum()
        self.assertEqual(fine.add(np.ones(8_000, np.complex64), 48_000.0), [])
        self.assertEqual(fine.add(np.ones(4_000, np.complex64), 320_000.0), [])   # not yet 8192 at this rate
        self.assertEqual(len(fine.add(np.ones(4_200, np.complex64), 320_000.0)), 0)
        self.assertEqual(len(fine.add(np.ones(32_000, np.complex64), 320_000.0)), 1)


class MeasureTest(unittest.TestCase):
    def test_the_scene_is_the_same_for_a_seed_and_holds_impulses(self):
        scene = measuring.Scene(1)
        size = measuring.block_size()
        self.assertEqual(size, 80_000)
        first = scene.block(size, size)
        np.testing.assert_array_equal(first, scene.block(size, size))
        self.assertEqual(first.dtype, np.complex64)
        magnitude = np.abs(first)
        impulses = np.flatnonzero(magnitude > 6 * np.median(magnitude))
        period = round(measuring.IMPULSE_PERIOD_S * measuring.RATE_HZ)
        starts = [at - size for at in range(size, 2 * size) if at % period == 0]
        self.assertEqual(impulses.tolist(), [start + k for start in starts for k in range(3)])

    def test_a_short_run_writes_its_table_with_every_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "table.md"
            out, err = io.StringIO(), io.StringIO()
            status = measuring.main(["--runs", "2", "--seconds", "0.1", "--warmup-s", "0.6", "--output",
                                     str(output)], out, err)
            self.assertEqual(status, measuring.EXIT_DONE, err.getvalue())
            text = output.read_text(encoding="utf-8")
        self.assertEqual(text, out.getvalue())
        rows = {line.split(" | ")[0].lstrip("| "): line for line in text.splitlines() if line.startswith("| ")}
        for name in (*measuring.TOOLS, measuring.ALL):
            row = rows[measuring.NAMES[name]]
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            self.assertEqual(len(cells), 4)
            for cell in cells[1:3]:
                float(cell.split(" ")[0])
            self.assertRegex(cells[3], r"^(within|over) its (1\.5|6)%$")
        self.assertIn("Baseline, without any tool", rows)
        self.assertEqual(len([line for line in text.splitlines() if line[:4] in ("| 1 ", "| 2 ")]), 2)
        self.assertIn("Python ", text)
        self.assertIn("NumPy ", text)

    def test_the_tools_work_where_the_measure_puts_them(self):
        run = measuring.measure(measuring.Scene(2), 0.1, 1.0)
        activity = run.activity
        self.assertGreater(activity["sam"].locked, 0)
        self.assertGreater(activity["blanker"].blanked, 0)
        self.assertGreater(activity["fine"].frames, 0)
        self.assertGreater(activity["all"].locked, 0)
        self.assertGreater(activity["all"].frames, 0)
        self.assertEqual(activity[measuring.BASELINE].locked, 0)
        self.assertEqual(set(run.cpu_ns), set(measuring.CONFIGURATIONS))

    def test_options_must_be_positive(self):
        for argv in (["--runs", "0"], ["--seconds", "-1"], ["--warmup-s", "x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    measuring.build_parser().parse_args(argv)


if __name__ == "__main__":
    unittest.main()
