"""Tests for tools/measure_pll.py: its arithmetic, then whole runs against a
Radiolack on the fake rtl_tcp, whose tuner follows one of the driver's PLL
computations or none, over real sockets on ephemeral ports."""

import asyncio
import functools
import importlib.util
import io
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp

from radiolack import r82xx
from radiolack.rtltcp import RtlTcpSource
from radiolack.server import LocalServer
from tests.fakes import LOOPBACK, FakeRtlTcp

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "measure_pll.py"
WEB = Path(__file__).resolve().parents[1] / "src" / "radiolack" / "web"


def load_tool():
    spec = importlib.util.spec_from_file_location("measure_pll", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


tool = load_tool()

RATE_HZ = 240_000           # the fastest to measure: the fake sends about eight times faster than that
VFO_OFFSET_HZ = 40_000      # the VFO above the first centre frequency
# The first centre frequencies where each computation moves the carrier most
# across four steps, and the other computation predicts otherwise.
TRUNCATING_CENTRE_HZ = 145_038_000
ROUNDING_CENTRE_HZ = 144_991_000
QUICK = ["--steps", "4", "--multiples", "2", "--reference-every", "3", "--timeout-s", "10"]


def pll(computation: str):
    """The fake's tuner, following one of the driver's computations."""
    return functools.partial(r82xx.tuned_hz, computation)


def alternating(error_hz: float):
    """A tuner that errs by ``error_hz`` on every other step of 1.234 kHz,
    as neither computation does."""
    return lambda frequency_hz, rate_hz, correction_ppm, state: frequency_hz + error_hz * ((frequency_hz // 1234) % 2)


def receiver(centre_hz: int = 145_000_000, ppm: float = 0.0, setting: str = "unknown",
             vfo_offset_hz: int = VFO_OFFSET_HZ, rate_hz: int = RATE_HZ) -> object:
    return tool.Receiver(centre_hz, rate_hz, ppm, centre_hz + vfo_offset_hz, "AM", 3000.0, "Rafael Micro R820T",
                         setting, 24e6, 1766e6)


class ArithmeticTest(unittest.TestCase):
    def test_the_plan_steps_off_the_grid_then_lets_radiolack_place_the_centre_on_it(self):
        points = tool.plan(receiver(), 4, 8)
        steps = [point for point in points if point.kind == tool.STEP]
        self.assertEqual([point.centre_hz for point in steps], [145_000_000 + k * 1234 for k in range(4)])
        self.assertFalse(any(point.grid for point in steps))
        on_grid = [point for point in points if point.kind == tool.GRID]
        self.assertTrue(on_grid)
        errors = set()
        for point in on_grid:
            self.assertTrue(point.grid)
            self.assertEqual(receiver().oscillator_hz(point) % 112_500, 0)
            self.assertIsNone(receiver().fits(point))
            request = receiver().request_hz(point)
            self.assertEqual(r82xx.tuned_hz("rounding", request, RATE_HZ), request)     # exact there
            errors.add(round(r82xx.tuned_hz("truncating", request, RATE_HZ) - request, 6))
        self.assertEqual(len(errors), 1)        # one constant within a divider

    def test_the_first_sweep_goes_the_way_that_keeps_the_vfo_off_the_centre_and_within_the_span(self):
        def first_step(vfo_offset_hz: int) -> int:
            plan = tool.plan(receiver(vfo_offset_hz=vfo_offset_hz, rate_hz=2_400_000), 40, 0)
            return plan[1].centre_hz - 145_000_000

        self.assertEqual(first_step(-50_000), 1234)
        self.assertEqual(first_step(40_000), -1234)         # upwards, the centre would cross the VFO
        self.assertEqual(first_step(-930_000), -1234)       # upwards, the VFO would near the span's edge
        with self.assertRaisesRegex(tool.Refused, "either way"):
            first_step(2000)

    def test_the_grid_and_the_labels_take_the_correction_into_account(self):
        corrected = receiver(ppm=12.34)
        on_grid = [point for point in tool.plan(corrected, 4, 8) if point.kind == tool.GRID]
        self.assertTrue(on_grid)
        for point in on_grid:
            self.assertEqual(corrected.oscillator_hz(point) % 112_500, 0)
            self.assertAlmostEqual(corrected.label_hz(point), corrected.request_hz(point) * (1 + 12.34e-6))
        step = tool.Point(tool.STEP, 145_001_234, False)
        self.assertEqual(corrected.request_hz(step), round(145_001_234 / (1 + 12.34e-6)))

    def test_the_predictions_are_the_carrier_moving_against_the_label(self):
        first = tool.Point(tool.STEP, 145_000_000, False)
        point = tool.Point(tool.STEP, 145_001_234, False)
        for computation in tool.COMPUTATIONS:
            with self.subTest(computation=computation):
                error = r82xx.tuned_hz(computation, point.centre_hz, RATE_HZ) - point.centre_hz
                first_error = r82xx.tuned_hz(computation, first.centre_hz, RATE_HZ) - first.centre_hz
                unknown = receiver()
                self.assertAlmostEqual(unknown.predicted_hz(computation, point, first), -(error - first_error))
                self.assertEqual(unknown.predicted_hz(computation, first, first), 0)
                # Labelled by the computation the tuner follows, the carrier stays.
                self.assertEqual(receiver(setting=computation).predicted_hz(computation, point, first), 0)
                self.assertAlmostEqual(receiver(setting=computation).label_hz(point), point.centre_hz + error)

    def test_references_follow_each_measure_count_and_end_the_run(self):
        points = [tool.Point(tool.STEP, 100 + k, False) for k in range(7)]
        order = tool.schedule(points, 3)
        self.assertEqual([point.kind for point in order],
                         [tool.STEP] * 4 + [tool.REFERENCE] + [tool.STEP] * 3 + [tool.REFERENCE])
        self.assertEqual((order[-1].centre_hz, order[-1].grid), (100, False))
        self.assertEqual(tool.schedule(points[:4], 3)[-1].kind, tool.REFERENCE)
        self.assertEqual(len(tool.schedule(points[:4], 3)), 5)

    def measures(self, offsets: list[float], drift_per_s: float, reference_every: int = 3) -> list:
        """Measures at one per second of a carrier that sits at offsets from
        its first measure, on a crystal drifting steadily."""
        points = [tool.Point(tool.STEP, 145_000_000 + k * 1234, False) for k in range(len(offsets))]
        order = tool.schedule(points, reference_every)
        values = iter(offsets)
        measures = []
        for second, point in enumerate(order):
            offset = 0.0 if point.kind == tool.REFERENCE else next(values)
            measures.append(tool.Measure(point, 145_040_000 + offset + drift_per_s * second, 40.0, float(second),
                                         float(point.centre_hz)))
        return receiver(), measures

    def test_a_steady_drift_comes_off_the_offsets(self):
        receiver, measures = self.measures([0.0, 12.0, -3.0, 7.0, 1.0], drift_per_s=0.5)
        analysis = tool.analyse(receiver, measures)
        offsets = [measure.offset_hz for measure in measures if measure.point.kind == tool.STEP]
        for got, expected in zip(offsets, [0.0, 12.0, -3.0, 7.0, 1.0]):
            self.assertAlmostEqual(got, expected)
        self.assertAlmostEqual(analysis.spread_hz, 15.0)
        self.assertAlmostEqual(analysis.drift_hz, 3.0)      # references at 0, 4 and 6 s
        self.assertAlmostEqual(analysis.drift_step_hz, 2.0)
        self.assertEqual((analysis.verdict, analysis.follows), ("table", []))
        self.assertIn("from 10 to 30 Hz", analysis.reason)

    def test_a_reference_that_moves_too_much_leaves_the_decision_to_the_owner(self):
        receiver, measures = self.measures([0.0, 0.5, -0.5, 0.2, 0.1], drift_per_s=2.0)
        analysis = tool.analyse(receiver, measures)
        self.assertLess(analysis.spread_hz, 10)
        self.assertEqual(analysis.verdict, "table")
        self.assertIn("warm up", analysis.reason)

    def test_a_small_spread_holds_at_any_divider(self):
        receiver, measures = self.measures([0.0, 0.5, -0.5, 0.2, 0.1], drift_per_s=0.0)
        self.assertEqual(tool.analyse(receiver, measures).verdict, "holds")
        far = tool.Receiver(435_000_000, RATE_HZ, 0.0, 435_040_000, "AM", 3000.0, "Rafael Micro R820T",
                            "rounding", 24e6, 1766e6)
        self.assertEqual(tool.analyse(far, measures).verdict, "holds")

    def test_the_menu_choices_it_names_are_the_interface_s(self):
        page = (WEB / "index.html").read_text(encoding="utf-8")
        for computation, label in tool.MENU.items():
            with self.subTest(computation=computation):
                self.assertIn(f'<option value="{computation}">{label}</option>', page)


class RunTest(unittest.IsolatedAsyncioTestCase):
    """Whole runs against a Radiolack on the fake rtl_tcp."""

    async def start(self, centre_hz: int = 145_000_000, *, tuner_pll=None, carrier: bool = True,
                    mode: str = "AM", bandwidth_hz: float = 3000.0, driver_pll: str = "unknown",
                    ppm: float = 0.0) -> None:
        self.centre_hz = centre_hz
        self.carrier_hz = centre_hz + VFO_OFFSET_HZ + 150      # near the VFO, not on it
        carriers = {self.carrier_hz: 0.3} if carrier else {}
        self.fake = FakeRtlTcp(carriers=carriers, noise=0.01, chunk_bytes=16_384, interval_s=0.004,
                               tuner_pll=tuner_pll, crystal_ppb=round(ppm * 1000))
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, self.fake.port, centre_hz=centre_hz, sample_rate_hz=RATE_HZ, settle_s=0.1)
        self.server = LocalServer(source=source)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        # The operator's interface, which puts the VFO on the carrier and stays open.
        origin = f"http://{LOOPBACK}:{self.server.port}"
        self.operator = await self.session.ws_connect(f"{origin}/stream", origin=origin)
        self.addAsyncCleanup(self.operator.close)
        await self.operator.send_json({"type": "demod", "frequency_hz": centre_hz + VFO_OFFSET_HZ, "mode": mode,
                                       "bandwidth_hz": bandwidth_hz})
        if driver_pll != "unknown" or ppm:
            await self.operator.send_json({"type": "source", "driver_pll": driver_pll, "ppm": ppm})
        await self.until_streaming(driver_pll, ppm)
        # On the grid, where Radiolack placed it.
        self.home_request = receiver(centre_hz, ppm).request_hz(tool.Point(tool.GRID, centre_hz, True))
        deadline = asyncio.get_running_loop().time() + 5
        while self.fake.frequency_hz != self.home_request:
            self.assertLess(asyncio.get_running_loop().time(), deadline, "the receiver never tuned")
            await asyncio.sleep(0.01)
        self.commands_before = len(self.fake.commands)

    async def until_streaming(self, driver_pll: str, ppm: float) -> None:
        while True:
            message = await self.operator.receive_json(timeout=5)
            if (message.get("type") == "source" and message["status"] == "streaming"
                    and message["driver_pll"] == driver_pll and message["ppm"] == ppm):
                return

    def frequencies_asked(self) -> list[int]:
        return [value for command, value in self.fake.commands[self.commands_before:] if command == 0x01]

    async def run_tool(self, *arguments: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        code = await tool.run([str(self.server.port), *QUICK, *arguments], out, err)
        return code, out.getvalue()

    def assert_restored_and_only_retuned(self) -> None:
        commands = self.fake.commands[self.commands_before:]
        self.assertTrue(commands, "the tool tuned nothing")
        self.assertEqual({command for command, _ in commands}, {0x01}, "the tool changed more than the centre")
        self.assertEqual(commands[-1], (0x01, self.home_request))

    async def test_a_tuner_that_truncates_names_its_driver_while_it_is_unknown(self):
        await self.start(TRUNCATING_CENTRE_HZ, tuner_pll=pll("truncating"))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "pll.md"
            code, out = await self.run_tool("--output", str(output))
            written = output.read_text(encoding="utf-8")
        self.assertEqual(code, 0, out)
        self.assertIn('driver set to "Unknown"', out)
        self.assertIn('choose "rtl-sdr up to 2.0.1, RTL-SDR Blog" as the driver in the Receiver menu', out)
        self.assertIn("Follows, within 5 Hz at every point: up to 2.0.1 (", out)
        self.assertIn("; not 2.0.2 on, largest difference", out)
        self.assertIn(f"Centre frequency restored to {TRUNCATING_CENTRE_HZ} Hz.", out)
        # The table: a heading, its rule, four steps, the points on the grid and the references.
        rows = [line for line in out.splitlines() if line.startswith("| ")]
        self.assertGreaterEqual(len(rows), 1 + 4 + 1 + 2)
        self.assertTrue(any(f"| {tool.GRID} |" in row for row in rows))
        self.assertIn(rows[0], written)
        self.assertIn("as the driver in the Receiver menu", written)
        self.assert_restored_and_only_retuned()
        asked = self.frequencies_asked()
        self.assertIn(TRUNCATING_CENTRE_HZ + 3 * 1234, asked)       # off the grid
        self.assertTrue(any((value + r82xx.rate_if_hz(RATE_HZ)) % 112_500 == 0 for value in asked[1:-1]))

    async def test_a_tuner_that_rounds_names_its_driver_while_it_is_unknown(self):
        await self.start(ROUNDING_CENTRE_HZ, tuner_pll=pll("rounding"))
        code, out = await self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('choose "rtl-sdr 2.0.2 and later" as the driver', out)
        self.assertIn("Follows, within 5 Hz at every point: 2.0.2 on (", out)
        self.assert_restored_and_only_retuned()

    async def test_the_labels_hold_once_the_driver_is_set(self):
        # A crystal 1.5 ppm slow too, which Radiolack corrects, all of it, itself.
        for computation, centre_hz in (("truncating", TRUNCATING_CENTRE_HZ), ("rounding", ROUNDING_CENTRE_HZ)):
            with self.subTest(computation=computation):
                await self.start(centre_hz, tuner_pll=pll(computation), driver_pll=computation, ppm=-1.5)
                code, out = await self.run_tool()
                self.assertEqual(code, 0, out)
                self.assertIn(f'with the driver set to "{tool.MENU[computation]}": the samples are labelled '
                              "where the tuner sits", out)
                self.assert_restored_and_only_retuned()

    async def test_a_driver_set_wrongly_is_named(self):
        await self.start(TRUNCATING_CENTRE_HZ, tuner_pll=pll("truncating"), driver_pll="rounding")
        code, out = await self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('driver set to "rtl-sdr 2.0.2 and later", that follows the computation of rtl-sdr up to '
                      '2.0.1', out)
        self.assertIn('choose "rtl-sdr up to 2.0.1, RTL-SDR Blog"', out)
        self.assert_restored_and_only_retuned()

    async def test_an_exact_tuner_holds_with_the_driver_unknown(self):
        await self.start()
        code, out = await self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('Verdict: a spread under 10 Hz with the driver set to "Unknown"', out)
        self.assertIn("as if the tuner were exact, as it was here", out)
        self.assert_restored_and_only_retuned()

    async def test_a_spread_in_between_goes_to_the_owner(self):
        await self.start(tuner_pll=alternating(20.0))
        code, out = await self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn("none applies, a spread from 10 to 30 Hz: the owner decides from the table", out)
        self.assert_restored_and_only_retuned()

    async def test_a_wide_spread_that_follows_no_prediction_goes_to_the_owner(self):
        await self.start(tuner_pll=alternating(45.0))
        code, out = await self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn("Follows neither computation within 5 Hz", out)
        self.assertIn("a spread of 30 Hz or more that follows neither computation: the owner decides", out)
        self.assert_restored_and_only_retuned()

    async def test_a_carrier_lost_midway_gives_no_verdict(self):
        await self.start()
        retunes = 0

        def lose(command: int, value: int) -> None:
            nonlocal retunes
            if command == 0x01:
                retunes += 1
                if retunes == 3:      # the first centre off the grid, the second step, the third
                    self.fake.carriers = {}

        self.fake.on_command = lose
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn("Incomplete after 2 measures: the carrier does not measure cleanly", out)
        self.assertIn("No verdict", out)
        self.assertNotIn("Verdict:", out)
        self.assert_restored_and_only_retuned()

    async def test_a_stream_that_closes_gives_no_verdict_and_says_the_centre_was_not_restored(self):
        await self.start()
        loop = asyncio.get_running_loop()

        def stop(command: int, value: int) -> None:
            if command == 0x01 and value != self.centre_hz:
                self.fake.on_command = None
                loop.run_in_executor(None, self.server.stop)

        self.fake.on_command = stop
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn("Incomplete after 1 measures: the stream closed", out)
        self.assertNotIn("Verdict:", out)
        self.assertIn(f"The centre frequency could not be restored to {self.centre_hz} Hz", out)

    async def test_another_interface_changing_the_vfo_stops_the_measure(self):
        await self.start()

        def move(command: int, value: int) -> None:
            if command == 0x01 and value != self.centre_hz:
                self.fake.on_command = None
                asyncio.ensure_future(self.operator.send_json({"type": "demod", "frequency_hz":
                                                               self.centre_hz + VFO_OFFSET_HZ + 500}))

        self.fake.on_command = move
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn("settings changed during the measure", out)
        self.assert_restored_and_only_retuned()

    async def test_refuses_to_start_without_a_clean_carrier_and_says_why(self):
        await self.start(carrier=False)
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn(f"Not started: the carrier does not measure cleanly at {self.centre_hz} Hz: no_signal", out)
        # Only the first centre, off the grid, then back.
        self.assertEqual(self.frequencies_asked(), [self.centre_hz, self.home_request])

    async def test_refuses_a_mode_whose_filter_is_not_centred_or_too_narrow(self):
        await self.start(mode="USB")
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn("Not started: the mode is USB", out)
        self.assertEqual(self.frequencies_asked(), [])
        await self.operator.send_json({"type": "demod", "mode": "CW", "bandwidth_hz": 300})
        code, out = await self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn("widen it", out)
        self.assertEqual(self.frequencies_asked(), [self.centre_hz, self.home_request])

    async def test_refuses_the_simulated_band_and_a_port_where_nothing_answers(self):
        server = LocalServer()
        server.start()
        self.addCleanup(server.stop)
        out = io.StringIO()
        self.assertEqual(await tool.run([str(server.port), *QUICK], out, io.StringIO()), 1)
        self.assertIn("Not started: the receiver in use is not an RTL-SDR", out.getvalue())
        server.stop()
        out = io.StringIO()
        self.assertEqual(await tool.run([str(server.port), *QUICK], out, io.StringIO()), 1)
        self.assertIn("Not started: no Radiolack answers", out.getvalue())

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    async def test_ctrl_c_restores_the_centre_frequency(self):
        await self.start()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(TOOL_PATH), str(self.server.port), "--timeout-s", "10",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.addAsyncCleanup(self.kill, process)
        loop = asyncio.get_running_loop()
        interrupted = loop.create_future()
        retunes = 0

        def interrupt(command: int, value: int) -> None:
            nonlocal retunes
            retunes += command == 0x01 and value != self.centre_hz
            if retunes == 2 and not interrupted.done():
                process.send_signal(signal.SIGINT)
                interrupted.set_result(None)

        self.fake.on_command = interrupt
        out, _ = await asyncio.wait_for(process.communicate(), 60)
        self.assertTrue(interrupted.done())
        self.assertEqual(process.returncode, 130)
        text = out.decode()
        self.assertIn("interrupted. No verdict", text)
        self.assertIn(f"Centre frequency restored to {self.centre_hz} Hz.", text)
        self.assert_restored_and_only_retuned()

    async def kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == "__main__":
    unittest.main()
