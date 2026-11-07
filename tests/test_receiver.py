"""I/Q capture, endpoints, port checks and the rtl_tcp child (no hardware)."""

from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import unittest

from radiolack.detection import FRAME_BYTES
from radiolack.errors import ReceiverError
from radiolack.receiver import Bridge, Capture, Endpoint, RtlTcpProcess, parse_endpoint, port_in_use


class CaptureTests(unittest.TestCase):
    def test_capture_tracks_tuner_and_drops_old_frames(self):
        capture = Capture(131000000, 2400000)
        capture.feed(bytes(FRAME_BYTES), 1)
        self.assertIsNotNone(capture.snapshot()[0])
        capture.commands(struct.pack("!BI", 1, 136000000))
        self.assertIsNone(capture.snapshot()[0])
        capture.feed(bytes(FRAME_BYTES), capture.valid_after + 1)
        frame, epoch, _ = capture.snapshot()
        self.assertEqual(frame.center, 136000000)
        self.assertEqual(frame.epoch, epoch)

    def test_odd_tcp_fragments_keep_iq_alignment_during_retune(self):
        capture = Capture(131000000, 2400000)
        capture.feed(b"\x01", 1)
        capture.commands(struct.pack("!BI", 1, 136000000))
        capture.feed(b"\x02\x03", capture.valid_after - 0.1)
        capture.feed(b"\x04" + b"\x05\x06" * (FRAME_BYTES // 2 - 1), capture.valid_after + 1)
        self.assertEqual(capture.snapshot()[0].raw[:4], b"\x03\x04\x05\x06")

    def test_gain_change_is_tracked_and_invalidates_samples(self):
        capture = Capture(131000000, 2400000)
        capture.feed(bytes(FRAME_BYTES), 1)
        epoch = capture.snapshot()[1]
        capture.commands(struct.pack("!BI", 4, 296))
        self.assertGreater(capture.snapshot()[1], epoch)
        capture.feed(bytes(FRAME_BYTES), capture.valid_after + 1)
        self.assertEqual(capture.snapshot()[0].receiver_controls, ((4, 296),))
        # Repeating an unchanged setting does not invalidate fresh samples.
        before = capture.snapshot()[1]
        capture.commands(struct.pack("!BI", 4, 296))
        self.assertEqual(capture.snapshot()[1], before)

    def test_only_the_latest_frame_is_kept(self):
        capture = Capture(131000000, 2400000)
        capture.feed(b"\x01" * FRAME_BYTES + b"\x02" * FRAME_BYTES + b"\x03" * 10, 1)
        frame = capture.snapshot()[0]
        self.assertEqual(frame.sequence, 2)
        self.assertEqual(frame.raw[:1], b"\x02")
        self.assertEqual(len(capture.buffer), 10)


class EndpointTests(unittest.TestCase):
    def test_parsing_and_loopback_detection(self):
        self.assertEqual(parse_endpoint("127.0.0.1:1234"), Endpoint("127.0.0.1", 1234))
        self.assertEqual(parse_endpoint("[::1]:7356"), Endpoint("::1", 7356))
        self.assertTrue(Endpoint("localhost", 1).is_loopback)
        self.assertFalse(Endpoint("192.168.1.10", 1234).is_loopback)
        for text in ("1234", ":1234", "host:0", "host:99999", "host:abc"):
            with self.assertRaises(ValueError):
                parse_endpoint(text)

    def test_bridge_refuses_non_loopback_and_busy_ports(self):
        with self.assertRaises(ReceiverError):
            Bridge(Capture(1, 2400000), Endpoint("0.0.0.0", 0), Endpoint("127.0.0.1", 1))
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            self.assertTrue(port_in_use(Endpoint("127.0.0.1", port)))
            with self.assertRaisesRegex(ReceiverError, "already use this port"):
                Bridge(Capture(1, 2400000), Endpoint("127.0.0.1", port), Endpoint("127.0.0.1", 1))
        self.assertFalse(port_in_use(Endpoint("127.0.0.1", port)))


class RtlTcpProcessTests(unittest.TestCase):
    def process(self, directory, executable, port=0):
        if port == 0:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        return RtlTcpProcess(Endpoint("127.0.0.1", port), center_hz=136500000, sample_rate=2400000,
                             gain_db=29.6, device_index=0, log_path=Path(directory) / "rtl.log",
                             executable=executable, startup_wait_s=0.3)

    def fake(self, directory, body):
        script = Path(directory) / "fake_rtl_tcp"
        script.write_text(f"#!{sys.executable}\n{body}\n")
        script.chmod(0o755)
        return str(script)

    def test_missing_executable_is_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ReceiverError, "not installed"):
                self.process(directory, "radiolack-no-such-rtl-tcp").start()

    def test_unavailable_device_is_reported_from_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = self.fake(directory, "print('No supported devices found.'); raise SystemExit(1)")
            backend = self.process(directory, fake)
            with self.assertRaisesRegex(ReceiverError, "no RTL-SDR device was found"):
                backend.start()
            backend.stop()

    def test_busy_port_is_detected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            fake = self.fake(directory, "raise SystemExit(0)")
            with self.assertRaisesRegex(ReceiverError, "already in use"):
                self.process(directory, fake, holder.getsockname()[1]).start()

    def test_only_the_owned_child_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = self.fake(directory, "import time\nprint('listening...', flush=True)\ntime.sleep(60)")
            backend = self.process(directory, fake)
            backend.start()
            self.assertIsNone(backend.process.poll())
            log = Path(directory) / "rtl.log"
            deadline = time.monotonic() + 5
            while "listening" not in log.read_text() and time.monotonic() < deadline:
                time.sleep(0.02)
            backend.stop()
            self.assertIsNotNone(backend.process.poll())
            self.assertIn("listening", log.read_text())

    def test_remote_managed_rtl_tcp_is_refused(self):
        with self.assertRaises(ReceiverError):
            RtlTcpProcess(Endpoint("192.168.1.10", 1234), center_hz=1, sample_rate=2400000, gain_db=0,
                          device_index=0, log_path=Path("unused.log"))


if __name__ == "__main__":
    unittest.main()
