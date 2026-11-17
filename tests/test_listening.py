"""Tests for the prototype of the last listening tool to build
(tools/listening.py), the fine spectrum, on synthetic signals that exercise
it, and for the tool that measures the listening tools' cost
(tools/measure_listening.py), whose figures they leave alone: they depend
on the computer, the tests on nothing but the samples. Synchronous AM, the
automatic notch and the noise blanker, in the package, have their own
tests (test_sam.py, test_notch.py, test_blanker.py)."""

import importlib.util
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import NoiseBlanker

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


def times(seconds):
    return np.arange(round(seconds * RATE_HZ)) / RATE_HZ


def noise(rng, count, rms):
    return (rng.normal(0, rms / math.sqrt(2), count) + 1j * rng.normal(0, rms / math.sqrt(2), count))


def blocks(signal, size=BLOCK):
    return (signal[start:start + size] for start in range(0, len(signal), size))


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

    def test_the_scene_without_impulses_is_the_same_less_them(self):
        size = measuring.block_size()
        scene, quiet = measuring.Scene(1), measuring.Scene(1, impulses=False)
        with_them, without = scene.block(size, size), quiet.block(size, size)
        magnitude = np.abs(without)
        self.assertFalse(np.any(magnitude > 6 * np.median(magnitude)))
        differ = np.flatnonzero(with_them != without)
        period = round(measuring.IMPULSE_PERIOD_S * measuring.RATE_HZ)
        self.assertEqual(differ.tolist(), [at - size + k for at in range(size, 2 * size) if at % period == 0
                                           for k in range(measuring.IMPULSE_SAMPLES)])

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
        self.assertIn("The scene with its impulses, three samples every 10 ms.", text)

    def test_without_impulses_the_blanker_finds_none(self):
        out, err = io.StringIO(), io.StringIO()
        status = measuring.main(["--runs", "1", "--seconds", "0.1", "--warmup-s", "0.2", "--without-impulses"],
                                out, err)
        self.assertEqual(status, measuring.EXIT_DONE, err.getvalue())
        self.assertIn("The scene without impulses.", out.getvalue())
        self.assertIn("The blanker zeroed impulses in 0 of", out.getvalue())

    def test_the_tools_work_where_the_measure_puts_them(self):
        run = measuring.measure(measuring.Scene(2), 0.1, 1.0)
        # The package's blanker, as the VFO's setting: no second copy.
        self.assertFalse(hasattr(listening, "NoiseBlanker"))
        pipeline = measuring.Pipeline(frozenset({"blanker"}))
        pipeline.process(measuring.Scene(2).block(0, measuring.block_size()))
        self.assertTrue(pipeline.settings.blanker)
        self.assertIsInstance(pipeline.demodulator._blanker, NoiseBlanker)
        self.assertGreater(pipeline.demodulator._blanker.impulses, 0)
        activity = run.activity
        self.assertGreater(activity["sam"].locked, 0)
        self.assertGreater(activity["notch"].notches, 0)
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
