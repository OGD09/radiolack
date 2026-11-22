"""The automatic calibration (later-milestones.md, section 3): its rules
(calibration.py), and the engine's run on the fake rtl_tcp, which serves
carriers on the band plan's standard-frequency stations through a crystal
that is off."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from radiolack import calibration, radio as radio_module
from radiolack.bandplan import BandPlan, default_plan
from radiolack.calibration import (DISAGREE, NONE, RANGE, REFUSED, WEAK, Result, Station, filter_width_hz,
                                   implied_ppm, propose, station_centre, stations, window_hz)
from radiolack.engine import Engine, Subscription
from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from tests.fake_rtl_tcp import REFERENCES_CRYSTAL_PPB
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcp, FakeRtlTcpRuns, FakeSource, reference_carriers

RANGE_HZ = (1.0, 2e9)


def result(ppm: float | None, margin_db: float | None = 40.0, frequency_hz: int = 10_000_000) -> Result:
    """A station's result implying ``ppm``, or none received."""
    return Result(frequency_hz, None if ppm is None else 1.0, margin_db, ppm, None if ppm is not None else "no_signal")


class RulesTest(unittest.TestCase):
    """Which stations, where the receiver tunes, the correction a carrier
    implies and the proposal."""

    def test_the_window_is_200_ppm_up_to_2_khz(self):
        self.assertAlmostEqual(window_hz(60_000), 12.0)
        self.assertAlmostEqual(window_hz(5_000_000), 1_000.0)
        self.assertEqual(window_hz(10_000_000), 2_000.0)
        self.assertEqual(window_hz(25_000_000), 2_000.0)
        # The filter holds the window, at its narrowest the VFO's narrowest.
        self.assertEqual(filter_width_hz(60_000), 100.0)
        self.assertEqual(filter_width_hz(5_000_000), 5_000.0)
        self.assertEqual(filter_width_hz(25_000_000), 10_000.0)

    def test_the_receiver_tunes_a_quarter_of_the_rate_below_the_station_or_above(self):
        exact = float
        self.assertEqual(station_centre(10_000_000, 2_400_000.0, RANGE_HZ, exact), 9_400_000.0)
        # Below the range's start, above it; outside it, nowhere.
        self.assertEqual(station_centre(2_500_000, 2_400_000.0, (2_000_000.0, 2e9), exact), 3_100_000.0)
        self.assertIsNone(station_centre(162_000, 2_400_000.0, (1_200_000.0, 2e9), exact))
        self.assertIsNone(station_centre(25_000_000, 2_400_000.0, (24_500_000.0, 25_500_000.0), exact))
        # A centre in use that brings the station near the DC residue or the edge: the other side.
        moved = lambda centre: centre + 57_000.0 if centre < 10e6 else centre
        self.assertEqual(station_centre(10_000_000, 240_000.0, RANGE_HZ, moved), 10_060_000.0)
        everywhere = lambda centre: centre + 55_000.0 if centre < 10e6 else centre - 55_000.0
        self.assertIsNone(station_centre(10_000_000, 240_000.0, RANGE_HZ, everywhere))

    def test_twelve_stations_at_most_within_reach_lowest_first(self):
        references = [25_000_000, 60_000, 10_000_000, 77_500, *range(1_000_000, 13_000_000, 1_000_000), 60_000]
        found = stations(references, 2_400_000.0, (1_200_000.0, 2e9), float)
        # 1 MHz lies below the range, and 60 and 77.5 kHz too; 10 MHz counts once.
        self.assertEqual([station.frequency_hz for station in found],
                         [*range(2_000_000, 13_000_000, 1_000_000), 25_000_000])
        self.assertEqual(found[0], Station(2_000_000, 1_400_000.0))
        self.assertEqual(stations([60_000], 2_400_000.0, (1_200_000.0, 2e9), float), ())
        self.assertEqual(len(stations(range(2_000_000, 30_000_000, 1_000_000), 2_400_000.0, RANGE_HZ, float)), 12)

    def test_the_band_plan_s_standard_frequency_stations(self):
        self.assertEqual(default_plan().references,
                         (60_000, 77_500, 162_000, 2_500_000, 4_996_000, 5_000_000, 9_996_000, 10_000_000,
                          14_996_000, 15_000_000, 20_000_000, 25_000_000))
        with self.assertRaisesRegex(ValueError, "reference must be true or absent"):
            BandPlan.parse({"format": 2, "name": "Test", "wfm_deemphasis_us": 50, "bands": [], "channels": [],
                            "spots": [{"f": 1, "name": "A", "reference": False}]})

    def test_a_carrier_shown_low_implies_a_positive_correction(self):
        # A crystal 4 ppm fast shows 10 MHz 40 Hz low.
        self.assertAlmostEqual(implied_ppm(0.0, 10_000_000 / (1 + 4e-6), 10_000_000), 4.0, places=6)
        # The correction in use is part of it: what it left is added to it.
        self.assertAlmostEqual(implied_ppm(3.0, 10_000_000 / (1 + 1e-6), 10_000_000), 4.0, places=5)

    def test_the_proposal_is_the_median_of_those_that_agree(self):
        proposal = propose([result(4.1), result(None), result(4.3), result(4.2), result(7.0)])
        self.assertEqual((proposal.ppm, proposal.agreeing, proposal.reason), (4.2, (0, 2, 3), None))
        # Two alone agree: their mean, to the hundredth.
        proposal = propose([result(-3.104), result(-3.3)])
        self.assertEqual((proposal.ppm, proposal.agreeing), (-3.2, (0, 1)))
        # A hair beyond 0.3 ppm: they disagree.
        self.assertEqual(propose([result(1.0), result(1.31)]).reason, DISAGREE)

    def test_one_station_alone_needs_30_db(self):
        self.assertEqual(propose([result(None), result(2.5, 30.0)]), calibration.Proposal(2.5, (1,)))
        self.assertEqual(propose([result(2.5, 29.9)]).reason, WEAK)
        self.assertEqual(propose([result(None), result(None)]).reason, NONE)
        self.assertEqual(propose([]).reason, NONE)
        # Two strong stations that disagree propose nothing either.
        self.assertEqual(propose([result(2.5, 50.0), result(5.0, 50.0)]).reason, DISAGREE)

    def test_two_groups_as_large_propose_nothing_unless_they_overlap(self):
        self.assertEqual(propose([result(1.0), result(1.1), result(3.0), result(3.1)]).reason, DISAGREE)
        # Overlapping groups: the tightest.
        proposal = propose([result(1.0), result(1.25), result(1.3)])
        self.assertEqual((proposal.ppm, proposal.agreeing), (1.25, (0, 1, 2)))
        proposal = propose([result(1.0), result(1.2), result(1.45)])
        self.assertEqual((proposal.ppm, proposal.agreeing), (1.1, (0, 1)))

    def test_a_proposal_beyond_what_a_receiver_takes_is_none(self):
        self.assertEqual(propose([result(150.0), result(150.1)]).reason, RANGE)
        self.assertEqual(propose([result(-0.001), result(0.001)]).ppm, 0.0)


class RunCase(unittest.IsolatedAsyncioTestCase):
    """An engine on the fake rtl_tcp, which records into a temporary folder."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "recordings"

    async def start(self, references: list[int] | None = None, *, crystal_ppb: int = 0,
                    carriers: dict[float, float] | None = None, rate_hz: float = 240_000.0,
                    sources: "KindOpener | None" = None, **settings) -> tuple[Engine, Subscription, FakeRtlTcp]:
        """An engine reading the fake rtl_tcp, which serves ``carriers``, by
        default on every standard-frequency station, on 145 MHz with its
        VFO on 145.1 MHz in NFM, shortwave by direct sampling; with
        ``references``, the band plan's stations are those; with
        ``sources``, it also switches to their kind."""
        fake = FakeRtlTcp(carriers=reference_carriers() if carriers is None else carriers, crystal_ppb=crystal_ppb,
                          chunk_bytes=16_384, interval_s=0.004)
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        engine = self.engine(RtlTcpSource(LOOPBACK, fake.port, sample_rate_hz=rate_hz, settle_s=0.1), references,
                             sources=sources)
        subscription = engine.subscribe()
        engine.radios[0].configure({"hf_input": "q", "centre_hz": 145_000_000, **settings})
        engine.radios[0].configure_demod({"frequency_hz": 145_100_000, "mode": "NFM", "bandwidth_hz": 12_000})
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        return engine, subscription, fake

    def engine(self, source, references: list[int] | None = None, *, sources: "KindOpener | None" = None) -> Engine:
        engine = Engine(source, sources=sources, recordings=self.folder)
        if references is not None:
            engine.plan = BandPlan(references=references)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=15)

        self.addAsyncCleanup(stop)
        return engine

    async def ended(self, engine: Engine, timeout: float = 60.0) -> dict:
        await eventually(lambda: not engine.radios[0].calibration_state()["running"], timeout)
        return engine.radios[0].calibration_state()

    def assert_home(self, engine: Engine) -> None:
        """The receiver and the VFO where the run found them."""
        demod = engine.radios[0].demod_state()
        self.assertEqual(engine.radios[0].state()["centre_hz"], 145_000_000)
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]), (145_100_000, "NFM", 12_000))


class RunTest(RunCase):
    """A run across the stations, its state, its proposal, and the receiver
    and the VFO brought back."""

    async def test_a_run_proposes_the_crystal_s_error_and_a_second_run_confirms_it(self):
        FakeRtlTcpRuns(self)
        source = RtlSdrSource("references", program=FAKE_RTL_TCP, sample_rate_hz=240_000.0, settle_s=0.1)
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure({"hf_input": "q", "centre_hz": 145_000_000})
        engine.radios[0].configure_demod({"frequency_hz": 145_100_000, "mode": "NFM", "bandwidth_hz": 12_000})
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        expected_ppm = REFERENCES_CRYSTAL_PPB / 1000
        proposals = []
        for applied in (False, True):
            engine.radios[0].calibrate({"action": "start"})
            state = engine.radios[0].calibration_state()
            # At 240 kS/s, direct sampling reaches 120 kHz: every station but the two lowest.
            self.assertEqual((state["running"], state["stations"], state["results"], state["ended"]),
                             (True, list(default_plan().references[2:]), [], None))
            self.assertEqual(engine.radios[0].demod_state()["mode"], "AM")
            self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], 162_000)
            state = await self.ended(engine, 90)
            self.assertEqual(state["ended"], "done")
            self.assertEqual(len(state["results"]), 10)
            for station in state["results"]:
                with self.subTest(applied=applied, station=station["frequency_hz"]):
                    self.assertIsNone(station["reason"])
                    shown_hz = station["frequency_hz"] * (1 + engine.radios[0].state()["ppm"] / 1e6) / (
                        1 + REFERENCES_CRYSTAL_PPB / 1e9)
                    self.assertAlmostEqual(station["carrier_hz"], shown_hz, delta=0.5)
            proposal = state["proposal"]
            self.assertGreaterEqual(proposal["stations"], 8)
            self.assertAlmostEqual(proposal["ppm"], expected_ppm, delta=0.05)
            self.assertEqual(sum(station["agrees"] for station in state["results"]), proposal["stations"])
            self.assert_home(engine)
            proposals.append(proposal["ppm"])
            # Apply is the ordinary correction.
            engine.radios[0].configure({"ppm": proposal["ppm"]})
        self.assertLess(abs(proposals[1] - proposals[0]), 0.1)
        # Every client learnt the run's progress.
        states = [message for message in waiting(subscription)
                  if isinstance(message, dict) and message["type"] == "calibration"]
        self.assertTrue(states)

    async def test_a_station_received_alone_strong_enough_proposes(self):
        engine, _, _ = await self.start([4_996_000, 10_000_000], crystal_ppb=37_420,
                                        carriers={10_000_000.0: 0.2})
        engine.radios[0].calibrate({"action": "start"})
        state = await self.ended(engine)
        self.assertEqual([station["reason"] for station in state["results"]], ["no_signal", None])
        self.assertEqual(state["results"][0]["carrier_hz"], None)
        self.assertEqual(state["proposal"]["stations"], 1)
        self.assertAlmostEqual(state["proposal"]["ppm"], 37.42, delta=0.05)
        self.assertEqual([station["agrees"] for station in state["results"]], [False, True])

    async def test_nothing_received_proposes_nothing(self):
        engine, _, _ = await self.start([5_000_000, 10_000_000], carriers={})
        engine.radios[0].calibrate({"action": "start"})
        state = await self.ended(engine)
        self.assertEqual((state["ended"], state["proposal"], state["reason"]), ("done", None, "none"))
        self.assert_home(engine)

    async def test_a_station_that_gives_no_samples_times_out(self):
        engine, _, fake = await self.start([5_000_000, 10_000_000])
        fake.sending = False
        with mock.patch.object(radio_module, "STATION_TIMEOUT_S", 0.3):
            engine.radios[0].calibrate({"action": "start"})
            state = await self.ended(engine, 10)
        self.assertEqual([station["reason"] for station in state["results"]], ["silent", "silent"])
        self.assertEqual(state["reason"], "none")

    async def test_a_station_whose_centre_is_refused_gives_no_carrier(self):
        engine, _, _ = await self.start([5_000_000, 10_000_000])
        configure = engine.radios[0].source.configure

        def refusing(changes):
            if changes.get("centre_hz") == 4_940_000:
                raise ValueError("refused")
            configure(changes)

        with mock.patch.object(engine.radios[0].source, "configure", refusing), self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].calibrate({"action": "start"})
            state = await self.ended(engine)
        self.assertEqual([station["reason"] for station in state["results"]], [REFUSED, None])
        self.assertEqual(state["proposal"]["stations"], 1)
        self.assert_home(engine)

    async def test_the_settings_saved_while_it_runs_are_those_of_its_start(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        saved = engine.radios[0].receiver_settings()
        self.assertEqual(saved["source"]["centre_hz"], 145_000_000)
        self.assertEqual((saved["demod"]["frequency_hz"], saved["demod"]["mode"], saved["demod"]["bandwidth_hz"]),
                         (145_100_000, "NFM", 12_000))
        self.assertEqual(engine.radios[0].state()["centre_hz"], 9_940_000)


class StopTest(RunCase):
    """What stops a run, and what it refuses: the receiver and the VFO come
    back first."""

    async def test_a_client_stops_it_and_it_proposes_from_what_it_measured(self):
        engine, _, _ = await self.start([5_000_000, 10_000_000, 15_000_000], crystal_ppb=2_000)
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].calibrate({"action": "start"})       # it runs already: nothing changes
        await eventually(lambda: len(engine.radios[0].calibration_state()["results"]) == 2, 30)
        engine.radios[0].calibrate({"action": "stop"})
        state = engine.radios[0].calibration_state()
        self.assertEqual((state["running"], state["ended"], len(state["results"])), (False, "stopped", 2))
        self.assertAlmostEqual(state["proposal"]["ppm"], 2.0, delta=0.05)
        self.assert_home(engine)
        engine.radios[0].calibrate({"action": "stop"})        # none runs: nothing changes
        self.assertEqual(engine.radios[0].calibration_state(), state)

    async def test_tuning_by_hand_stops_it_after_the_vfo_came_back(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure_demod({"frequency_hz": 145_200_000})
        state = engine.radios[0].calibration_state()
        self.assertEqual((state["ended"], state["proposal"], state["reason"]), ("tuned", None, None))
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]), (145_200_000, "NFM", 12_000))
        self.assertEqual(engine.radios[0].state()["centre_hz"], 145_000_000)
        # A new centre frequency stops another run, the change applying once the receiver is back.
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure({"centre_hz": 145_050_000})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "tuned")
        self.assertEqual((engine.radios[0].state()["centre_hz"], engine.radios[0].demod_state()["mode"]), (145_050_000, "NFM"))

    async def test_a_vfo_tuned_by_hand_outside_the_span_stays_there_once_the_receiver_is_back(self):
        # A press on the spectrum while it shows a station's window: the VFO set there, outside
        # the span the receiver comes back to, reads nothing but stays, rather than move to an
        # edge that nobody chose once that span's samples come.
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        await eventually(lambda: engine.radios[0]._capture is not None and engine.radios[0]._capture[0][0] != 145_000_000, 30)
        engine.radios[0].configure_demod({"frequency_hz": 9_990_000})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "tuned")
        await eventually(lambda: engine.radios[0]._capture[0] == (145_000_000, 240_000), 30)
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], 9_990_000)
        # A span that changes afterwards still brings a VFO left outside it to its nearest edge.
        engine.radios[0].configure({"centre_hz": 9_700_000})
        await eventually(lambda: engine.radios[0]._capture[0] == (9_700_000, 240_000), 30)
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], 9_700_000 + 120_000)

    async def test_the_gain_leaves_it_running(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure({"gain_db": 20.0})
        engine.radios[0].configure_demod({"squelch_db": -50.0})
        self.assertTrue(engine.radios[0].calibration_state()["running"])

    async def test_a_scan_or_a_recording_stops_it(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure_scanner({"sweep_low_hz": 144e6, "sweep_high_hz": 146e6})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "scan")
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "sweep")
        engine.radios[0].configure_scanner({"scan": None})
        self.assert_home(engine)
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure_recording({"audio": True})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "recording")
        self.assertIsNotNone(engine.radios[0].recording_state()["audio"])
        self.assert_home(engine)
        # And it does not start while one runs.
        with self.assertRaisesRegex(ValueError, "a recording runs"):
            engine.radios[0].calibrate({"action": "start"})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "recording")

    async def test_it_stops_a_scan(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].configure_scanner({"sweep_low_hz": 144e6, "sweep_high_hz": 146e6})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        engine.radios[0].calibrate({"action": "start"})
        self.assertIsNone(engine.radios[0].scanner_state()["scan"])
        self.assertEqual(engine.radios[0].calibration_state()["stations"], [10_000_000])

    async def test_a_carrier_s_measure_waits_for_its_end_and_the_driver_s_check_is_refused(self):
        engine, subscription, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].measure_carrier({"action": "measure", "id": 7}, subscription)
        engine.radios[0].measure_carrier({"action": "identify", "id": 8}, subscription)
        answer = next(message for message in waiting(subscription)
                      if isinstance(message, dict) and message["type"] == "driver_pll")
        self.assertEqual((answer["id"], answer["reason"]), (8, "calibration"))
        await self.ended(engine)
        # Measured at the VFO once back, where nothing is.
        while not isinstance(message := await asyncio.wait_for(subscription.get(), 20), dict) or (
                message["type"] != "carrier"):
            pass
        self.assertEqual((message["id"], message["reason"]), (7, "no_signal"))

    async def test_it_refuses_a_converter_and_a_receiver_without_a_correction(self):
        engine, _, _ = await self.start([10_000_000], converter="up")
        with self.assertRaisesRegex(ValueError, "a converter is set"):
            engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure({"converter": "none", "hf_input": "none"})
        with self.assertRaisesRegex(ValueError, "no standard-frequency station"):
            engine.radios[0].calibrate({"action": "start"})
        for changes, refusal in (({"action": "go"}, "the action start or stop"), ({"action": "start", "id": 1},
                                                                                  "unknown calibration field")):
            with self.assertRaisesRegex(ValueError, refusal):
                engine.radios[0].calibrate(changes)
        self.assertIsNone(engine.radios[0].calibration_state()["ended"])
        fixed = self.engine(FakeSource())
        with self.assertRaisesRegex(ValueError, "no frequency correction"):
            fixed.radios[0].calibrate({"action": "start"})

    async def test_it_goes_on_without_any_client_and_radiolack_s_stop_brings_the_receiver_back(self):
        engine, subscription, _ = await self.start([5_000_000, 10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        engine.unsubscribe(subscription)
        await eventually(lambda: engine.radios[0].calibration_state()["results"], 30)
        engine.close()
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "closed")
        self.assert_home(engine)

    async def test_another_kind_of_source_stops_it(self):
        engine, _, _ = await self.start([10_000_000])
        engine.radios[0].calibrate({"action": "start"})
        with self.assertRaisesRegex(ValueError, "this source cannot be changed"):
            engine.radios[0].configure({"kind": "synthetic"})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "source")
        self.assert_home(engine)

    async def test_another_receiver_forgets_the_proposal_made_for_the_one_before(self):
        kinds = KindOpener()
        engine, subscription, fake = await self.start([10_000_000], sources=kinds)
        kinds.port = fake.port
        engine.radios[0].calibrate({"action": "start"})
        self.assertIsNotNone((await self.ended(engine))["proposal"])
        waiting(subscription)
        engine.radios[0].configure({"kind": "test"})
        idle = {"type": "calibration", "running": False, "stations": [], "results": [], "proposal": None,
                "reason": None, "ended": None}
        self.assertEqual(engine.radios[0].calibration_state(), idle)
        self.assertIn(idle, waiting(subscription))
        # A run that the switch ended stays, to say so.
        engine.radios[0].configure({"kind": "rtl_tcp"})
        engine.radios[0].calibrate({"action": "start"})
        engine.radios[0].configure({"kind": "test"})
        self.assertEqual(engine.radios[0].calibration_state()["ended"], "source")


class KindOpener:
    """Opens the fake rtl_tcp's source again, or a FakeSource, by kind."""

    kinds = ("rtl_tcp", "test")

    port = 0        # the fake rtl_tcp's

    def open(self, reopen):
        if reopen.get("kind") == "test":
            return FakeSource()
        return RtlTcpSource(LOOPBACK, self.port, sample_rate_hz=240_000.0, settle_s=0.1)


def waiting(subscription: Subscription) -> list:
    """Every message ready now, without waiting for more."""
    messages = []
    while True:
        try:
            messages.append(subscription.get_nowait())
        except asyncio.QueueEmpty:
            return messages


async def eventually(condition, timeout: float = 10.0) -> None:
    """Return once ``condition()`` holds, checking every 10 ms."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)
