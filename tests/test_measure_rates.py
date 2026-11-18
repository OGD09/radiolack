"""Tests for the tool that measures the sample rates the engine holds
(tools/measure_rates.py), whose figures they leave alone: they depend on
the computer, the tests on nothing but the samples."""

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up by this name
    spec.loader.exec_module(module)
    return module


rates = load("measure_rates")


class BufferTest(unittest.TestCase):
    def test_samples_come_out_in_order(self):
        buffer = rates.NativeBuffer(2, 10)
        buffer.put(bytes(range(6)))
        buffer.put(bytes(range(6, 10)))
        self.assertEqual(len(buffer), 5)
        self.assertEqual(buffer.take(3, 0.0), bytes(range(6)))
        self.assertEqual(buffer.take(2, 0.0), bytes(range(6, 10)))
        self.assertEqual(buffer.dropped, 0)

    def test_a_reader_behind_loses_the_oldest(self):
        buffer = rates.NativeBuffer(4, 3)
        buffer.put(bytes(range(20)))        # five samples, two too many
        self.assertEqual(buffer.dropped, 2)
        self.assertEqual(buffer.take(3, 0.0), bytes(range(8, 20)))

    def test_a_wait_for_more_than_the_capacity_keeps_what_it_waits_for(self):
        buffer = rates.NativeBuffer(1, 2)
        taken = []
        waiter = rates.threading.Thread(target=lambda: taken.append(buffer.take(4, 10.0)))
        waiter.start()
        while buffer._wanted != 4:     # noqa: SLF001 - the waiter is waiting
            rates.time.sleep(0.001)
        buffer.put(b"ab")
        buffer.put(b"cd")
        waiter.join(10.0)
        self.assertEqual(taken, [b"abcd"])
        self.assertEqual(buffer.dropped, 0)

    def test_a_take_that_waits_in_vain_gives_none(self):
        buffer = rates.NativeBuffer(2, 4)
        buffer.put(b"ab")
        self.assertIsNone(buffer.take(2, 0.0))
        self.assertEqual(len(buffer), 1)
        with self.assertRaises(ValueError):
            buffer.take(0, 0.0)

    def test_only_whole_samples_go_in(self):
        with self.assertRaises(ValueError):
            rates.NativeBuffer(2, 4).put(b"abc")
        with self.assertRaises(ValueError):
            rates.NativeBuffer(0, 4)


class FormatTest(unittest.TestCase):
    def test_each_format_comes_back_to_the_samples(self):
        samples = rates.Scene(3, 2_400_000.0).block(0, 1000)
        samples *= 0.99 / np.max(np.abs(samples.view(np.float32)))     # within the integers, unclipped
        for name, kind in rates.FORMATS.items():
            with self.subTest(format=name):
                native = rates.to_native(samples, kind)
                self.assertEqual(native.dtype, kind.component)
                self.assertEqual(native.nbytes, len(samples) * kind.sample_bytes)
                back = rates.to_complex(native.tobytes(), kind)
                self.assertEqual(back.dtype, np.complex64)
                tolerance = 1e-6 if name == "CF32" else 1 / kind.full_scale
                np.testing.assert_allclose(back, samples, atol=tolerance)

    def test_a_full_scale_given_replaces_the_formats(self):
        kind = rates.FORMATS["CS16"]
        native = np.array([16384, -16384], np.int16)
        self.assertEqual(rates.to_complex(native.tobytes(), kind)[0], np.complex64(0.5 - 0.5j))
        self.assertAlmostEqual(complex(rates.to_complex(native.tobytes(), kind, 32767.0)[0]).real, 16384 / 32767)

    def test_integers_clip_at_full_scale(self):
        native = rates.to_native(np.array([2 - 2j], np.complex64), rates.FORMATS["CS8"])
        self.assertEqual(native.tolist(), [127, -128])


class MeasureTest(unittest.TestCase):
    def test_the_engines_block_follows_the_rate(self):
        self.assertEqual(rates.block_size(2_400_000.0), 80_000)
        self.assertEqual(rates.block_size(10_000_000.0), 333_333)
        self.assertEqual(rates.block_size(48_000.0), 8192)

    def test_the_scene_is_the_same_for_a_seed(self):
        scene = rates.Scene(1, 4_000_000.0)
        first = scene.block(5000, 4096)
        np.testing.assert_array_equal(first, scene.block(5000, 4096))
        self.assertEqual(first.dtype, np.complex64)
        self.assertFalse(np.array_equal(first, rates.Scene(2, 4_000_000.0).block(5000, 4096)))

    def test_an_intake_takes_its_whole_block(self):
        intake = rates.Intake(2_400_000.0, rates.FORMATS["CS16"])
        samples = rates.Scene(1, 2_400_000.0).block(0, intake.block)
        self.assertGreater(intake.process(rates.to_native(samples, intake.format)), 0)
        self.assertEqual(len(intake.buffer), 0)
        self.assertEqual(intake.buffer.dropped, 0)

    def test_the_rule(self):
        self.assertTrue(rates.offered(3_200_000.0, [90.0]))     # the rtl_tcp client's rates, whatever they cost
        self.assertTrue(rates.offered(4_000_000.0, [10.0, 49.9, 80.0]))
        self.assertFalse(rates.offered(4_000_000.0, [10.0, 50.0, 80.0]))

    def test_a_short_run_writes_its_tables_with_every_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "table.md"
            out, err = io.StringIO(), io.StringIO()
            status = rates.main(["--runs", "2", "--seconds", "0.1", "--warmup-s", "0.1", "--rates", "2.4,4",
                                 "--output", str(output)], out, err)
            self.assertEqual(status, rates.EXIT_DONE, err.getvalue())
            text = output.read_text(encoding="utf-8")
        self.assertEqual(text, out.getvalue())
        rows = {line.split(" | ")[0].lstrip("| "): line for line in text.splitlines() if line.startswith("| ")}
        for rate, rule in (("2.4", r"offered \(up to 3\.2 MS/s\)"), ("4", r"(offered.*|not offered)")):
            cells = [cell.strip() for cell in rows[rate].strip("|").split("|")]
            self.assertEqual(len(cells), 11)
            for cell in cells[3:10]:
                float(cell.split(" ")[0])
            self.assertRegex(cells[10], f"^{rule}$")
        self.assertEqual(len([line for line in text.splitlines() if line[:4] in ("| 1 ", "| 2 ")]), 2)
        self.assertIn("NumPy ", text)
        self.assertIn("load average", text)

    def test_the_engine_works_where_the_measure_puts_it(self):
        run, activity = rates.measure((2_400_000.0,), 0.1, 0.4, 1)
        done = activity[2_400_000.0]
        self.assertEqual(done.blocks, 15)
        self.assertGreater(done.opened, 0)      # the station opens the squelch
        self.assertGreater(done.searches, 0)    # detection searched
        self.assertEqual(set(run.intake_ns), {(2_400_000.0, name) for name in rates.FORMATS})

    def test_options_are_checked(self):
        for argv in (["--runs", "0"], ["--seconds", "-1"], ["--rates", "0.1"], ["--rates", "x"], ["--rates", ""]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    rates.build_parser().parse_args(argv)
        self.assertEqual(rates.build_parser().parse_args(["--rates", "8,4"]).rates, (4e6, 8e6))


if __name__ == "__main__":
    unittest.main()
