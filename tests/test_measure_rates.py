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


class FakeClock:
    """A wall clock that moves only when told to, and a sleep that moves
    it: the paced measure runs without waiting."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def tables_of(text):
    """Each Markdown table of ``text``: its rows of cells, the header's
    first, without the separator."""
    tables, current = [], None
    for line in text.splitlines():
        if line.startswith("| "):
            if current is None:
                current = []
                tables.append(current)
            if not line.startswith("| ---"):
                current.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        else:
            current = None
    return tables


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
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "table.md"
            out, err = io.StringIO(), io.StringIO()
            status = rates.main(["--runs", "2", "--seconds", "0.1", "--warmup-s", "0.1", "--rates", "2.4,4",
                                 "--output", str(output)], out, err, clock, clock.sleep)
            self.assertEqual(status, rates.EXIT_DONE, err.getvalue())
            text = output.read_text(encoding="utf-8")
        self.assertEqual(text, out.getvalue())
        self.assertGreater(clock.slept, 0.0)        # paced on the clock given, without waiting
        tables = tables_of(text)
        self.assertEqual(len(tables), 4)
        rows = {row[0]: row for row in tables[0][1:]}
        for rate, rule in (("2.4", r"offered \(up to 3\.2 MS/s\)"), ("4", r"(offered.*|not offered)")):
            cells = rows[rate]
            self.assertEqual(len(cells), 11)
            for cell in cells[3:10]:
                float(cell.split(" ")[0])
            self.assertRegex(cells[10], f"^{rule}$")
        paced = {row[0]: row for row in tables[2][1:]}
        self.assertEqual(tables[2][0][3], "Engine, paced")
        for rate, period in (("2.4", "33.3"), ("4", "33.3")):
            cells = paced[rate]
            self.assertEqual(len(cells), 7)
            self.assertEqual(cells[1], period)
            for cell in cells[2:6]:
                float(cell.split(" ")[0])
            self.assertRegex(cells[6], r"^0 of 6$")      # the given clock stands still while a block runs
        for table in (tables[1], tables[3]):
            self.assertEqual([row[0] for row in table[1:]], ["1", "2"])
        self.assertIn("NumPy ", text)
        self.assertIn("load average", text)
        self.assertIn("the rule decides on it", text)
        self.assertIn("This measure does not decide.", text)

    def test_the_engine_works_where_the_measure_puts_it(self):
        run, activity = rates.measure((2_400_000.0,), 0.1, 0.4, 1)
        done = activity[2_400_000.0]
        self.assertEqual(done.blocks, 15)
        self.assertGreater(done.opened, 0)      # the station opens the squelch
        self.assertGreater(done.searches, 0)    # detection searched
        self.assertEqual(set(run.intake_ns), {(2_400_000.0, name) for name in rates.FORMATS})

    def test_the_paced_measure_follows_a_receivers_pace(self):
        clock = FakeClock()
        durations = iter([0.010, 0.050, 0.010, 0.020, 0.040, 0.010])     # the timed blocks', on the clock
        timeline = []

        class Busy:
            """Takes a set time on the clock, and a set processor time."""

            def __init__(self, rate_hz):
                self.rate_hz = rate_hz
                self.warm = 2       # warm-up blocks, 0.01 s each

            def process(self, samples):
                timeline.append(clock.now)
                self.warm -= 1
                clock.now += 0.010 if self.warm >= 0 else next(durations)
                return 5_000_000

        run = rates.measure_paced((2_400_000.0,), 6 / 30, 2 / 30, 1, clock, clock.sleep, Busy)
        paced = run.rates[2_400_000.0]
        period = 1 / 30
        self.assertAlmostEqual(paced.period_s, period)
        self.assertEqual(paced.blocks, 6)
        self.assertEqual(paced.over, 2)
        self.assertAlmostEqual(max(paced.took_s), 0.050)
        self.assertAlmostEqual(paced.percent, 0.005 * 6 / (6 * period) * 100)
        # Each block starts once delivered, or at once when the one before
        # ran late: the blocks after the two that ran over wait for nothing.
        due = [(index + 1) * period for index in range(8)]
        expected = due[:4] + [due[3] + 0.050, due[5], due[6], due[6] + 0.040]
        for started, wanted in zip(timeline, expected):
            self.assertAlmostEqual(started, wanted)
        self.assertEqual(len(timeline), 8)

    def test_the_paced_measure_runs_the_engine(self):
        clock = FakeClock()
        run = rates.measure_paced((2_400_000.0, 4_000_000.0), 0.1, 0.1, 1, clock, clock.sleep)
        self.assertEqual(set(run.rates), {2_400_000.0, 4_000_000.0})
        for paced in run.rates.values():
            self.assertEqual(paced.blocks, 3)
            self.assertGreater(paced.cpu_ns, 0)
        self.assertAlmostEqual(clock.slept, 2 * 6 / 30, places=3)

    def test_options_are_checked(self):
        for argv in (["--runs", "0"], ["--seconds", "-1"], ["--rates", "0.1"], ["--rates", "x"], ["--rates", ""]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    rates.build_parser().parse_args(argv)
        self.assertEqual(rates.build_parser().parse_args(["--rates", "8,4"]).rates, (4e6, 8e6))


if __name__ == "__main__":
    unittest.main()
