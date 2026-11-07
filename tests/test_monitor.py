"""Monitor loop with a simulated clock, I/Q capture and Gqrx (no sockets, no hardware).

The monitor's own ``sleep`` advances a virtual clock and produces a fresh
I/Q frame, so ten-second listens run in a fraction of a second.
"""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radiolack.detection import ChannelGrid
from radiolack.errors import GqrxError, ReceiverError, StoreWriteError
from radiolack.monitor import Monitor, MonitorSettings
from radiolack.receiver import SETTLE_SECONDS, Endpoint, Frame
from radiolack.scanning import FIXED_WINDOW_SCAN_INFO
from radiolack.storage import ObservationStore

from tests.helpers import AIRBAND, CONTEXT, synthetic

CONTROLS = tuple(tuple(item) for item in CONTEXT["receiver_controls"])
CARRIER_HZ = 131500000
CARRIER_DBFS, NOISE_DBFS = -46.0, -60.0
METER_LAG_S = 0.5
# Gqrx does not always center the hardware exactly on the requested focus.
HARDWARE_OFFSET_FRACTION = 0.072
ALWAYS = float("inf")


class FakeRadio:
    """Virtual clock plus the capture and bridge interfaces the monitor reads."""

    def __init__(self, carriers=((CARRIER_HZ, 0.0, ALWAYS),), center=131000000, rate=2400000):
        self.now = 0.0
        self.carriers = list(carriers)
        self.center = center
        self.rate = rate
        self.epoch = 1
        self.connected = True
        self.paused = False
        self.follows_moves = True
        self.failure = None
        # Bridge side: no client address skips the Gqrx ownership check.
        self.listen = Endpoint("127.0.0.1", 1235)
        self.client_address = None
        self.frame = None
        self.sequence = 0
        self.valid_after = 0.0
        self._frames = {}

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 6)
        if self.connected and not self.paused and self.now >= self.valid_after:
            # Only tones inside the receiver's anti-alias bandwidth reach the samples.
            tones = tuple((frequency, 0.1) for frequency, start, stop in self.carriers
                          if start <= self.now < stop and abs(frequency - self.center) < 0.45 * self.rate)
            key = (tones, self.center)
            if key not in self._frames:
                self._frames[key] = synthetic(tones, self.center, self.rate)
            self.sequence += 1
            self.frame = Frame(self.sequence, self.epoch, self.center, self.rate, self.now,
                               self._frames[key], CONTROLS)

    def snapshot(self):
        return self.frame, self.epoch, self.connected

    def invalidate(self):
        """What Capture does after a receiver change or a Gqrx (dis)connection."""
        self.epoch += 1
        self.frame = None
        self.valid_after = self.now + SETTLE_SECONDS

    def retune(self, center):
        self.center = center
        self.invalidate()

    def carrier_on(self, at, frequency):
        return any(item == frequency and start <= at < stop for item, start, stop in self.carriers)


class FakeGqrx:
    """Remote control whose S-meter shows the carrier METER_LAG_S after the I/Q tap."""

    def __init__(self, radio):
        self.radio = radio
        self.vfo = radio.center
        self.squelch = -50.5
        self.calls = []
        self.lost = False
        self.closed = None

    def _call(self, text):
        if self.lost:
            raise ConnectionResetError("Gqrx closed the connection")
        self.calls.append(text)

    def frequency(self):
        self._call("f")
        return self.vfo

    def mute(self):
        self._call("L SQL 0")
        self.squelch = 0.0

    def read_squelch(self):
        self._call("l SQL")
        return self.squelch

    def strength(self):
        self._call("l STRENGTH")
        return CARRIER_DBFS if self.radio.carrier_on(self.radio.now - METER_LAG_S, self.vfo) else NOISE_DBFS

    def gain_stages(self):
        self._call("l ?")
        return {"LNA": 25.3}

    def apply_squelch(self, value):
        self._call(f"L SQL {value:.1f}")
        self.squelch = round(value, 1)
        return self.squelch

    def tune(self, frequency, squelch=None):
        self._call(f"TUNE {frequency} {squelch}")
        self.vfo = frequency
        # Provisional value below the lowest settled reading, as GqrxControl.tune does.
        return self.apply_squelch(CARRIER_DBFS - 6 if squelch is None else squelch)

    def move_window(self, focus, sample_rate):
        self._call(f"MOVE {focus}")
        self.vfo = focus
        if self.radio.follows_moves:
            self.radio.retune(focus + round(HARDWARE_OFFSET_FRACTION * sample_rate))

    def close(self, restore=True):
        self.closed = "restored" if restore else "dropped"


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.directory.name) / "observations.json", create=True)
        self.messages = []
        self.controls = []

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def build(self, radio, *, full_band=False, profile=AIRBAND, refusals=0, backend=None):
        self.radio = radio
        pending = [ConnectionRefusedError("Connection refused")] * refusals

        def factory():
            if pending:
                raise pending.pop()
            control = FakeGqrx(radio)
            self.controls.append(control)
            return control

        settings = MonitorSettings(profile=profile, full_band=full_band)
        return Monitor(settings, radio, radio, self.store, backend=backend, control_factory=factory,
                       clock=radio.clock, sleep=radio.sleep,
                       emit=lambda text: self.messages.append((radio.now, text)))

    def run_until(self, monitor, end):
        while self.radio.now < end - 1e-9:
            monitor.tick()

    def said(self, fragment):
        return [(when, text) for when, text in self.messages if fragment in text]


class ListenTests(MonitorTestCase):
    def test_ten_second_listen_is_stored_only_at_the_end_with_telemetry(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 10.1)
        self.assertEqual(len(self.said("LISTEN 131.500000 MHz")), 1)
        self.assertIn("memory new", self.said("LISTEN")[0][1])
        self.assertEqual(self.store.observations, [], "nothing is stored before the listen ends")
        self.run_until(monitor, 10.4)
        [event] = self.store.observations
        self.assertEqual(event["origin"]["kind"], "radiolack")
        self.assertEqual((event["frequency_hz"], event["mode"], event["filter_hz"]), (CARRIER_HZ, "AM", 8000))
        self.assertEqual((event["listening_seconds"], event["required_margin_db"], event["minimum_active_seconds"],
                          event["minimum_observed_seconds"]), (10, 15, 1, 5))
        self.assertGreaterEqual(event["active_seconds"], 9.5)
        self.assertGreaterEqual(event["actual_elapsed_seconds"], 10.0)
        self.assertEqual((event["receiver_center_hz"], event["sample_rate_hz"]), (131000000, 2400000))
        self.assertEqual(event["receiver_context"], CONTEXT)
        self.assertEqual(event["scan"], FIXED_WINDOW_SCAN_INFO)
        self.assertEqual((event["profile"], event["confirmation_observations"], event["cooldown_seconds"]),
                         ("airband-8k33", 2, 20))
        self.assertEqual((event["squelch_dbfs"], event["squelch_source"]), (-52.0, "signal_fallback"))
        self.assertEqual(event["squelch_carrier_meter_dbfs"], CARRIER_DBFS)
        self.assertEqual(event["quality_flags"], {"limited_fft_coverage": False, "provisional_squelch": True})
        gqrx = event["gqrx_measurements"]
        self.assertEqual(gqrx["gain_stages_db"], {"LNA": 25.3})
        self.assertEqual(gqrx["fft_state_guard_seconds"], 1.5)
        self.assertEqual(gqrx["meter_dbfs_fft_active_settled"]["median"], CARRIER_DBFS)
        self.assertEqual(gqrx["meter_above_squelch_fraction_fft_active_settled"], 1.0)
        self.assertGreater(event["fft_measurements"]["signal_bin_dbfs"]["samples"], 50)
        station = self.store.stations[str(CARRIER_HZ)]
        self.assertEqual((station["squelch_dbfs"], station["last_verified_squelch_dbfs"]), (-52.0, -52.0))
        self.assertEqual(self.controls[0].calls.count("L SQL 0"), 1, "audio muted once the listen ends")
        self.assertEqual(len(self.said("SAVED 131.500000 MHz")), 1)

    def test_noise_learned_after_the_carrier_lowers_the_squelch_during_the_listen(self):
        monitor = self.build(FakeRadio(carriers=((CARRIER_HZ, 0.0, 4.0),)))
        self.run_until(monitor, 10.4)
        [event] = self.store.observations
        self.assertEqual((event["squelch_dbfs"], event["squelch_source"]), (-57.0, "noise"))
        self.assertEqual(event["gqrx_noise_floor_dbfs"], NOISE_DBFS)
        [change] = event["squelch_changes"]
        self.assertEqual((change["requested_dbfs"], change["readback_dbfs"], change["reason"]), (-57.0, -57.0, "noise"))
        # Quiet readings count only once the FFT has been silent for the guard and the meter caught up.
        self.assertGreaterEqual(change["offset_seconds"], 4.0 + 1.5 + 0.6 - 0.2)
        self.assertFalse(event["quality_flags"]["provisional_squelch"])
        self.assertLess(event["active_seconds"], 4.0)
        self.assertEqual(self.store.stations[str(CARRIER_HZ)]["noise_floor_dbfs"], NOISE_DBFS)
        self.assertEqual(len(self.said("SQL LEARNED 131.500000 MHz | noise -60.0 dBFS | squelch -57.0")), 1)

    def test_memorized_squelch_above_the_carrier_is_lowered_and_saved_at_once(self):
        contaminated = {"squelch_dbfs": -43.0, "noise_floor_dbfs": -45.3, "source": "noise",
                        "receiver_context": CONTEXT, "updated_at_utc": "2026-11-07T10:38:25+00:00"}
        self.store.remember_squelch(CARRIER_HZ, contaminated)
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.assertIn("memory reapplied", self.said("LISTEN")[0][1])
        self.assertIn(f"TUNE {CARRIER_HZ} -43.0", self.controls[0].calls)
        # The legacy noise estimate sits above this carrier: only the carrier ceiling is usable.
        self.assertEqual(len(self.said("SQL GUARD 131.500000 MHz | carrier -46.0 dBFS | no usable stored "
                                       "noise | squelch -49.0 dBFS")), 1)
        saved = self.store.stations[str(CARRIER_HZ)]
        self.assertEqual((saved["squelch_dbfs"], saved["source"], saved["learning_version"]),
                         (-49.0, "carrier_guard", 2))
        self.assertEqual(saved["noise_floor_dbfs"], -45.3, "the legacy estimate is kept, not rewritten")
        self.run_until(monitor, 10.4)
        [event] = self.store.observations
        self.assertEqual((event["squelch_dbfs"], event["squelch_source"]), (-49.0, "carrier_guard"))
        self.assertEqual(event["squelch_changes"][0]["reason"], "carrier_guard")
        self.assertTrue(event["quality_flags"]["provisional_squelch"])

    def test_guard_uses_a_stored_noise_clearly_below_the_carrier(self):
        # Busy channel: the memory sits near the carrier and no quiet period is long enough to
        # measure noise, but the stored noise is credible because it lies well below the carrier.
        memory = {"squelch_dbfs": -43.0, "noise_floor_dbfs": -60.0, "source": "noise",
                  "receiver_context": CONTEXT, "updated_at_utc": "2026-11-07T10:41:36+00:00"}
        self.store.remember_squelch(CARRIER_HZ, memory)
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 10.4)
        self.assertEqual(len(self.said("SQL GUARD 131.500000 MHz | carrier -46.0 dBFS | stored noise "
                                       "-60.0 dBFS | squelch -57.0 dBFS")), 1)
        [event] = self.store.observations
        self.assertEqual((event["squelch_dbfs"], event["squelch_source"]), (-57.0, "carrier_guard"))
        # Only the readings that trigger the guard see the carrier below the old -43.0 squelch.
        self.assertGreater(event["gqrx_measurements"]["meter_above_squelch_fraction_fft_active_settled"], 0.9)
        saved = self.store.stations[str(CARRIER_HZ)]
        self.assertEqual((saved["squelch_dbfs"], saved["noise_floor_dbfs"]), (-57.0, -60.0))

    def test_cooldown_then_memory_is_reapplied_on_the_next_listen(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 41.0)
        listens = self.said("LISTEN 131.500000 MHz")
        self.assertEqual(len(listens), 2)
        saved_at = self.said("SAVED")[0][0]
        self.assertGreaterEqual(listens[1][0] - saved_at, 20.0 - 1e-6)
        self.assertIn("memory reapplied", listens[1][1])
        self.assertIn(f"TUNE {CARRIER_HZ} -52.0", self.controls[0].calls)
        self.assertEqual(len(self.store.observations), 2)
        self.assertEqual(self.store.count(CARRIER_HZ), 2)


class InterruptionTests(MonitorTestCase):
    def test_receiver_change_cancels_the_listen(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.radio.invalidate()  # e.g. a gain command forwarded to rtl_tcp
        self.run_until(monitor, 13.0)
        self.assertEqual(len(self.said("Listen cancelled: receiver settings changed; not logged.")), 1)
        self.assertEqual(len(self.said("LISTEN")), 2, "the carrier is detected again after settling")
        self.assertEqual(self.store.observations, [])
        self.run_until(monitor, 14.0)
        self.assertEqual(len(self.store.observations), 1)

    def test_stale_stream_cancels_the_listen_and_warns_once(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.radio.paused = True
        self.run_until(monitor, 9.5)
        self.assertEqual(len(self.said("Listen cancelled: I/Q stream interrupted; not logged.")), 1)
        self.assertEqual(len(self.said("No fresh I/Q samples for 5 s")), 1)
        self.radio.paused = False
        self.run_until(monitor, 20.0)
        self.assertEqual(len(self.store.observations), 1)
        self.assertGreater(self.said("SAVED")[0][0], 19.0)

    def test_gqrx_input_disconnect_uses_a_new_control_connection(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.radio.connected = False
        self.radio.invalidate()
        self.run_until(monitor, 5.0)
        self.assertEqual(len(self.said("Listen cancelled: Gqrx input disconnected; not logged.")), 1)
        self.assertEqual(len(self.said("Waiting for Gqrx to connect to the I/Q bridge")), 1)
        self.assertEqual(self.controls[0].closed, "dropped")
        self.radio.connected = True
        self.radio.invalidate()
        self.run_until(monitor, 6.0)
        self.assertEqual(len(self.controls), 2)
        self.assertEqual(len(self.said("Gqrx connected")), 2)
        self.radio.failure = ConnectionError("rtl_tcp stopped sending samples")
        with self.assertRaisesRegex(ReceiverError, "rtl_tcp stopped sending samples"):
            monitor.run()
        self.assertEqual(self.controls[1].closed, "restored")
        self.assertEqual(self.store.observations, [])

    def test_unreachable_remote_control_is_retried_with_one_message(self):
        monitor = self.build(FakeRadio(), refusals=2)
        self.run_until(monitor, 6.0)
        self.assertEqual(len(self.said("Waiting for Gqrx remote control on 127.0.0.1:7356")), 1)
        connected = self.said("Gqrx connected")
        self.assertEqual(len(connected), 1)
        self.assertAlmostEqual(connected[0][0], 4.1)
        self.assertEqual(len(self.said("LISTEN")), 1)

    def test_remote_control_of_another_gqrx_is_never_driven(self):
        radio = FakeRadio()
        radio.client_address = ("127.0.0.1", 40000)
        conflict = ("the Gqrx remote-control port 7356 belongs to PID 10410 (gqrx -c old.conf), "
                    "not to the Gqrx reading the I/Q bridge (PID 68024)")
        answers = [conflict, conflict, None]  # the other Gqrx is closed before the third attempt
        with patch("radiolack.monitor.foreign_remote_control",
                   side_effect=lambda *ports: answers.pop(0) if answers else None) as check:
            monitor = self.build(radio)
            self.run_until(monitor, 4.0)
            self.assertEqual(self.controls, [], "no connection, hence no command, to the other Gqrx")
            self.assertEqual(len(self.said(f"Waiting: {conflict}. RadioLack only drives the Gqrx reading its "
                                           "bridge")), 1)
            self.run_until(monitor, 4.2)
        check.assert_called_with(40000, 1235, 7356)
        self.assertEqual(len(self.controls), 1)
        self.assertAlmostEqual(self.said("Gqrx connected")[0][0], 4.1)

    def test_lost_remote_control_during_a_listen_is_not_logged(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.controls[0].lost = True
        self.run_until(monitor, 12.5)
        self.assertEqual(len(self.said("Gqrx remote control disconnected; listen not logged. Reconnecting...")), 1)
        self.assertEqual(self.controls[0].closed, "dropped")
        self.assertEqual(len(self.controls), 2)
        self.assertEqual(self.store.observations, [])

    def test_manual_vfo_change_cancels_logging(self):
        monitor = self.build(FakeRadio())
        self.run_until(monitor, 3.0)
        self.controls[0].vfo = 131700000
        self.run_until(monitor, 10.5)
        self.assertEqual(len(self.said("Listen cancelled: VFO changed manually; not logged.")), 1)
        self.assertEqual(self.store.observations, [])

    def test_disk_error_stops_the_monitor_and_is_not_a_radio_disconnection(self):
        monitor = self.build(FakeRadio())
        failure = StoreWriteError("Could not write observations.json: No space left on device. "
                                  "The previous content is intact.")
        with patch.object(self.store, "append", side_effect=failure):
            with self.assertRaises(StoreWriteError):
                monitor.run()
        self.assertEqual(self.said("disconnected"), [])
        self.assertEqual(self.controls[0].closed, "restored")
        self.assertEqual(len(self.controls), 1)

    def test_rtl_tcp_child_failure_stops_the_monitor(self):
        class Backend:
            def check(self):
                raise ReceiverError("rtl_tcp exited with code 1: no RTL-SDR device was found (unplugged?)")

        monitor = self.build(FakeRadio(), backend=Backend())
        with self.assertRaisesRegex(ReceiverError, "no RTL-SDR device"):
            monitor.tick()


class FullBandTests(MonitorTestCase):
    def profile(self):
        grid = ChannelGrid(130000000, 133000000, 118000000, Fraction(25000, 3), 3000)
        return replace(AIRBAND, grid=grid, initial_center_hz=131000000)

    def moves(self):
        return [int(call.split()[1]) for call in self.controls[0].calls if call.startswith("MOVE")]

    def test_explorer_moves_on_after_a_listen_even_if_the_carrier_persists(self):
        monitor = self.build(FakeRadio(), full_band=True, profile=self.profile())
        self.run_until(monitor, 12.5)
        [event] = self.store.observations
        self.assertEqual(event["scan"], {"mode": "full_band", "cycle": 1, "window_number": 2, "window_count": 6,
                                         "window_observation_seconds": 1.0})
        calls = self.controls[0].calls
        tune = calls.index(f"TUNE {CARRIER_HZ} None")
        self.assertEqual(self.moves()[:2], [130240000, 130840000])
        self.assertEqual(next(call for call in calls[tune:] if call.startswith("MOVE")), "MOVE 131440000")
        # Each quiet window: 0.4 s settling + 1 s observation; window 2 of cycle 2 is requested at 19.1 s.
        self.run_until(monitor, 19.5)
        self.assertEqual(self.moves()[:8], [130240000, 130840000, 131440000, 132040000, 132640000, 132760000,
                                            130240000, 130840000])
        self.assertEqual(monitor.explorer.cycle, 2)
        self.assertEqual(len(self.store.observations), 1, "cooldown prevents an immediate repeat")
        window_lines = self.said("WINDOW 2/6")
        self.assertIn("FFT center 131.012800 MHz", window_lines[0][1])

    def test_receiver_that_does_not_move_stops_full_band_mode(self):
        radio = FakeRadio()
        radio.follows_moves = False
        monitor = self.build(radio, full_band=True, profile=self.profile())
        with self.assertRaisesRegex(GqrxError, "did not move the receiver"):
            self.run_until(monitor, 10.0)
        self.assertEqual(self.moves(), [130240000])


if __name__ == "__main__":
    unittest.main()
