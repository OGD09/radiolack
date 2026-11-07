"""Gqrx remote-control commands and separate profile preparation (no real Gqrx)."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from radiolack.errors import GqrxProtocolError, SquelchVerificationError
from radiolack.gqrx import GqrxControl, prepare_gqrx_profile
from radiolack.receiver import Endpoint


def control(mode="AM", filter_hz=8000):
    instance = GqrxControl.__new__(GqrxControl)
    instance.mode, instance.filter_hz, instance.saved = mode, filter_hz, []
    return instance


class GqrxCommandTests(unittest.TestCase):
    def test_gain_telemetry_queries_only_gain_settings(self):
        gqrx = control()
        with patch.object(gqrx, "command", side_effect=[["SQL STRENGTH LNA_GAIN"], ["25.3"]]) as command:
            self.assertEqual(gqrx.gain_stages(), {"LNA": 25.3})
        self.assertEqual([call.args[0] for call in command.call_args_list], ["l ?", "l LNA_GAIN"])

    def test_window_move_forces_hardware_retune_and_keeps_audio_muted(self):
        gqrx = control()
        with patch.object(gqrx, "command") as command:
            gqrx.move_window(118240000, 2400000)
        self.assertEqual([c.args[0] for c in command.call_args_list],
                         ["L SQL 0", "F 123040000", "F 118240000"])

    def test_cached_squelch_is_last_setting_after_retune_and_read_back(self):
        gqrx = control()
        calls = []

        def command(text, lines=1):
            calls.append(text)
            return ["-57.0"] if text == "l SQL" else ["RPRT 0"]

        with patch.object(gqrx, "command", side_effect=command), patch.object(gqrx, "strength") as strength:
            self.assertEqual(gqrx.tune(131500000, -57), -57)
        strength.assert_not_called()
        self.assertEqual(calls, ["L SQL 0", "M AM 8000", "F 131500000", "L SQL -57.0", "l SQL"])

    def test_provisional_squelch_is_below_the_lowest_settled_meter_reading(self):
        gqrx = control()
        calls = []

        def command(text, lines=1):
            calls.append(text)
            return ["-52.0"] if text == "l SQL" else ["RPRT 0"]

        with patch.object(gqrx, "command", side_effect=command), \
                patch.object(gqrx, "strength", side_effect=[-45.0, -46.0, -44.5]):
            self.assertEqual(gqrx.tune(131500000, None, sleep=lambda seconds: None), -52.0)
        self.assertEqual(calls[-2:], ["L SQL -52.0", "l SQL"])

    def test_profile_mode_and_filter_are_used(self):
        gqrx = control("FM", 12500)
        with patch.object(gqrx, "command", return_value=["-50.0"]) as command:
            gqrx.tune(146520000, -50)
        self.assertIn("M FM 12500", [c.args[0] for c in command.call_args_list])

    def test_squelch_readback_mismatch_is_not_silently_accepted(self):
        gqrx = control()
        with patch.object(gqrx, "command", side_effect=[["RPRT 0"], ["-150"]]):
            with self.assertRaisesRegex(SquelchVerificationError, "verification failed"):
                gqrx.apply_squelch(-57)

    def test_rejected_or_malformed_replies_are_protocol_errors(self):
        gqrx = control()
        gqrx.stream = MagicMock()
        gqrx.stream.readline.return_value = b"RPRT 1\n"
        with self.assertRaisesRegex(GqrxProtocolError, "rejected"):
            gqrx.command("F 1")
        with patch.object(gqrx, "command", return_value=["abc"]):
            with self.assertRaises(GqrxProtocolError):
                gqrx.strength()
        with patch.object(gqrx, "command", return_value=["-160"]):
            with self.assertRaises(GqrxProtocolError):
                gqrx.strength()

    def test_closed_connection_is_a_recoverable_disconnection(self):
        gqrx = control()
        gqrx.stream = MagicMock()
        gqrx.stream.readline.return_value = b""
        with self.assertRaises(ConnectionError):
            gqrx.command("f")

    def test_dead_control_socket_can_be_closed_without_restoring(self):
        gqrx = control()
        gqrx.stream = MagicMock()
        gqrx.connection = MagicMock()
        gqrx.saved = ["F 136500000"]
        with patch.object(gqrx, "command") as command:
            gqrx.close(restore=False)
        command.assert_not_called()
        gqrx.stream.close.assert_called_once()
        gqrx.connection.close.assert_called_once()

    def test_close_restores_saved_settings_in_order(self):
        gqrx = control()
        gqrx.stream, gqrx.connection = MagicMock(), MagicMock()
        gqrx.saved = ["LNB_LO 0", "M AM 6000", "F 131000000", "L SQL -50.5", "U DSP 0"]
        with patch.object(gqrx, "command") as command:
            gqrx.close()
        self.assertEqual([c.args[0] for c in command.call_args_list], gqrx.saved)


class GqrxProfileTests(unittest.TestCase):
    def arguments(self):
        return {"bridge": Endpoint("127.0.0.1", 1235), "remote": Endpoint("127.0.0.1", 7356),
                "sample_rate": 2400000, "center_hz": 136500000, "mode": "AM", "filter_hz": 8000}

    def test_profile_creation_does_not_overwrite_a_file_or_modify_the_base(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "default.conf"
            base.write_text("[input]\ndevice=rtl=0\ngains=@Variant(x)\n[gui]\ngeometry=keep\n")
            original_base = base.read_bytes()
            path = Path(directory) / "gqrx.conf"
            prepare_gqrx_profile(path, base=base, **self.arguments())
            text = path.read_text()
            self.assertIn('device="rtl_tcp=127.0.0.1:1235"', text)
            self.assertIn("enabled=true", text)
            self.assertIn("port=7356", text)
            self.assertIn("filter_low_cut=-4000", text)
            self.assertIn("geometry=keep", text)
            self.assertNotIn("gains=", text)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                prepare_gqrx_profile(path, base=base, **self.arguments())
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(base.read_bytes(), original_base)

    def test_usual_gqrx_profile_is_never_an_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "default.conf"
            with self.assertRaisesRegex(ValueError, "usual profile"):
                prepare_gqrx_profile(base, base=base, **self.arguments())
            self.assertFalse(base.exists())


if __name__ == "__main__":
    unittest.main()
