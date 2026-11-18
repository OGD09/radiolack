"""Tests for the tool that measures the listening tools' cost
(tools/measure_listening.py), whose figures they leave alone: they depend
on the computer, the tests on nothing but the samples. The tools, in the
package, have their own tests (test_sam.py, test_notch.py, test_blanker.py,
test_fine.py)."""

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import NoiseBlanker
from radiolack.fine import FineSpectrum

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up by this name
    spec.loader.exec_module(module)
    return module


measuring = load("measure_listening")

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
        self.assertEqual(measuring.ROWS, (*measuring.TOOLS, measuring.FINE_BLANKER, measuring.ALL))
        for name in measuring.ROWS:
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
        # The package's tools, no prototype: the blanker as the VFO's
        # setting, the fine spectrum as the engine gives it to a block.
        self.assertFalse((TOOLS / "listening.py").exists())
        pipeline = measuring.Pipeline(frozenset({"blanker", "fine"}))
        pipeline.process(measuring.Scene(2).block(0, measuring.block_size()))
        self.assertTrue(pipeline.settings.blanker)
        self.assertIsInstance(pipeline.demodulator._blanker, NoiseBlanker)
        self.assertGreater(pipeline.demodulator._blanker.impulses, 0)
        self.assertIsInstance(pipeline.fine[0], FineSpectrum)
        self.assertIsNotNone(pipeline.demodulator._unblanked)   # the samples as received, for the fine spectrum
        activity = run.activity
        self.assertGreater(activity["sam"].locked, 0)
        self.assertGreater(activity["notch"].notches, 0)
        self.assertGreater(activity["blanker"].blanked, 0)
        self.assertGreater(activity["fine"].frames, 0)
        self.assertGreater(activity[measuring.FINE_BLANKER].frames, 0)
        self.assertGreater(activity[measuring.FINE_BLANKER].blanked, 0)
        self.assertGreater(activity["all"].locked, 0)
        self.assertGreater(activity["all"].frames, 0)
        self.assertEqual(activity[measuring.BASELINE].locked, 0)
        self.assertEqual(activity[measuring.BASELINE].frames, 0)
        self.assertEqual(set(run.cpu_ns), set(measuring.CONFIGURATIONS))
        # The fine spectrum with the blanker adds to the blanker alone.
        self.assertEqual(run.added(measuring.FINE_BLANKER),
                         run.percent(measuring.FINE_BLANKER) - run.percent("blanker"))

    def test_options_must_be_positive(self):
        for argv in (["--runs", "0"], ["--seconds", "-1"], ["--warmup-s", "x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    measuring.build_parser().parse_args(argv)


if __name__ == "__main__":
    unittest.main()
