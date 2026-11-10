"""Tests for the rtl_tcp sources, against a fake rtl_tcp server on an
ephemeral port, and a fake rtl_tcp program that never opens a receiver."""

import asyncio
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

import numpy as np

from radiolack.rtltcp import RtlSdrSource, RtlTcpSource, parse_address, tuner_for
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcp, FakeRtlTcpRuns, fake_codes, free_port, sample_codes

FAST = {"settle_s": 0.15, "idle_s": 1.0, "read_wait_s": 0.2, "timeout_s": 0.5, "retry_s": (0.05, 0.2)}
COUNT = 400         # samples per read: less than one chunk of the fake server
# Stands in for Radiolack: runs the fake rtl_tcp program given as arguments
# through an RtlSdrSource, says when samples arrive, then waits to be killed.
KILLED_RADIOLACK = """
import asyncio, sys
from radiolack.rtltcp import RtlSdrSource

async def main():
    source = RtlSdrSource(program=sys.argv[1:], idle_s=60)
    while await source.read(1000) is None:
        pass
    print("streaming", flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""


async def eventually(condition, timeout: float = 5.0) -> None:
    """Wait until ``condition()`` holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)


def running(pid: int) -> bool:
    """Whether a process runs, an ended one waiting for its parent aside (Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


class SourceTest(unittest.IsolatedAsyncioTestCase):
    """One source on one fake server; each test closes its own server."""

    async def start(self, fake: FakeRtlTcp | None = None, **options) -> RtlTcpSource:
        self.fake = fake if fake is not None else FakeRtlTcp()
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, self.fake.port, **{**FAST, **options})
        self.addCleanup(source.close)
        self.changes = 0

        def changed():
            self.changes += 1

        source.on_change = changed
        return source

    async def read(self, source: RtlTcpSource, count: int = COUNT, timeout: float = 5.0) -> np.ndarray:
        """The next samples, reading again while the source waits."""
        deadline = time.monotonic() + timeout
        while (samples := await source.read(count)) is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"no samples: {source.status} ({source.detail})")
        self.assertEqual(len(samples), count)
        self.assertEqual(samples.dtype, np.complex64)
        return samples

    def assert_labelled(self, source: RtlTcpSource, samples: np.ndarray) -> None:
        """The samples were taken with the setting the source gives them."""
        i_codes, q_codes = sample_codes(samples)
        i_code, q_code = fake_codes(source.centre_hz, source.sample_rate_hz)
        self.assertEqual((i_codes, q_codes), ({i_code}, {q_code}))


class StreamingTest(SourceTest):
    async def test_sends_its_settings_then_labels_the_samples_with_them(self):
        source = await self.start(centre_hz=433_000_000, sample_rate_hz=1_024_000)
        self.assertEqual(source.status, "idle")
        for _ in range(5):
            self.assert_labelled(source, await self.read(source))
        self.assertEqual((source.centre_hz, source.sample_rate_hz), (433e6, 1_024_000.0))
        self.assertEqual(self.fake.commands, [(0x03, 0), (0x05, 0), (0x02, 1_024_000), (0x01, 433_000_000)])
        self.assertEqual(source.status, "streaming")

    async def test_converts_unsigned_bytes_to_full_scale(self):
        source = await self.start()
        samples = await self.read(source)
        i_code, q_code = fake_codes(145e6, 2.4e6)
        expected = complex((i_code - 127.5) / 127.5, (q_code - 127.5) / 127.5)
        np.testing.assert_allclose(samples, np.full(COUNT, expected), rtol=0, atol=1e-6)

    async def test_describes_the_receiver(self):
        source = await self.start(gain_db=30)
        self.assertEqual(source.describe()["tuner"], None)
        await self.read(source)
        state = source.describe()
        self.assertEqual(state, {
            "kind": "rtl_tcp", "status": "streaming", "detail": "",
            "address": f"{LOOPBACK}:{self.fake.port}", "tuner": "Rafael Micro R820T",
            "centre_hz": 145e6, "sample_rate_hz": 2.4e6, "gain_db": 29.7, "ppm": 0,
            "gains_db": state["gains_db"], "range_hz": [24e6, 1766e6],
            "settable": ["address", "centre_hz", "sample_rate_hz", "gain_db", "ppm"],
        })
        # One set of settings whatever the address, which was given here.
        self.assertEqual((source.settings_key, source.given), ("rtl_tcp", frozenset({"address"})))
        self.assertEqual(len(state["gains_db"]), 29)
        self.assertEqual(self.fake.commands[:2], [(0x03, 1), (0x04, 297)])
        self.assertGreaterEqual(self.changes, 2)    # connecting, then streaming

    async def test_a_reader_that_falls_behind_gets_the_latest_samples(self):
        source = await self.start(buffer_s=0.01)
        await self.read(source)
        self.fake.frequency_hz = 433_000_000    # what the server sends from now on
        await asyncio.sleep(0.4)                # far more than the source keeps
        i_codes, _ = sample_codes(await self.read(source))
        self.assertEqual(i_codes, {fake_codes(433e6, 0)[0]})


class SettingsTest(SourceTest):
    async def test_retunes_and_drops_the_samples_of_the_previous_setting(self):
        source = await self.start()
        await self.read(source)
        source.configure({"centre_hz": 433_000_000})
        await eventually(lambda: self.fake.commands[-1] == (0x01, 433_000_000))
        for _ in range(5):
            self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.centre_hz, 433e6)

    async def test_without_settling_the_old_samples_would_be_mislabelled(self):
        # The fake server keeps sending the old setting for a while, as a
        # receiver does: the settling time is what keeps the labels right.
        source = await self.start(settle_s=0.0)
        await self.read(source)
        await eventually(lambda: self.fake.frequency_hz == 145_000_000)
        source.configure({"centre_hz": 433_000_000})
        i_codes, _ = sample_codes(await self.read(source))
        self.assertEqual(source.centre_hz, 433e6)
        self.assertIn(fake_codes(145e6, 0)[0], i_codes)

    async def test_a_new_sample_rate_also_sends_the_frequency(self):
        source = await self.start()
        await self.read(source)
        source.configure({"sample_rate_hz": 1_024_000})
        await eventually(lambda: len(self.fake.commands) == 6)
        self.assertEqual(self.fake.commands[4:], [(0x02, 1_024_000), (0x01, 145_000_000)])
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.sample_rate_hz, 1_024_000.0)

    async def test_gain_snaps_to_the_tuner_and_none_is_automatic(self):
        source = await self.start()
        await self.read(source)
        source.configure({"gain_db": 30})
        source.configure({"gain_db": -5})
        source.configure({"gain_db": None})
        await eventually(lambda: len(self.fake.commands) == 9)
        self.assertEqual(self.fake.commands[4:], [(0x03, 1), (0x04, 297), (0x03, 1), (0x04, 0), (0x03, 0)])
        self.assertIsNone(source.describe()["gain_db"])

    async def test_negative_gains_go_in_twos_complement(self):
        source = await self.start(FakeRtlTcp(tuner_type=1, gain_count=14))
        await self.read(source)
        source.configure({"gain_db": -1})
        await eventually(lambda: self.fake.commands[-1:] == [(0x04, 2**32 - 10)])
        self.assertEqual(source.describe()["gain_db"], -1.0)

    async def test_a_frequency_correction_retunes_and_drops_the_samples_on_their_way(self):
        source = await self.start(ppm=5)
        await self.read(source)
        self.assertEqual(self.fake.commands, [(0x03, 0), (0x05, 5), (0x02, 2_400_000), (0x01, 145_000_000)])
        changed = time.monotonic()
        source.configure({"ppm": -2.6})     # whole ppm, as librtlsdr takes them
        await eventually(lambda: len(self.fake.commands) == 6)
        self.assertEqual(self.fake.commands[4:], [(0x05, 2**32 - 3), (0x01, 145_000_000)])
        self.assertEqual(source.describe()["ppm"], -3)
        await self.read(source)
        self.assertGreaterEqual(time.monotonic() - changed, FAST["settle_s"])

    async def test_refuses_invalid_changes_and_changes_nothing(self):
        source = await self.start()
        await self.read(source)
        before, commands, changes = source.describe(), list(self.fake.commands), self.changes
        refused = [
            {"frequency": 145e6},
            {"centre_hz": 145e6, "volume": 3},
            {"centre_hz": "145000000"},
            {"centre_hz": True},
            {"centre_hz": float("nan")},
            {"centre_hz": float("inf")},
            {"centre_hz": 10**400},
            {"centre_hz": 20e6},
            {"centre_hz": 1800e6},
            {"sample_rate_hz": 250_000},
            {"sample_rate_hz": 3_300_000},
            {"gain_db": "auto"},
            {"ppm": 101},
            {"ppm": -100.6},
            {"ppm": "5"},
            {"ppm": None},
            {"ppm": True},
            {"centre_hz": 433e6, "sample_rate_hz": 100},
            {"centre_hz": 433e6, "ppm": 200},
            {"address": ""},
            {"address": None},
            {"address": 1234},
            {"address": "host:port"},
            {"address": "::1"},
            {"address": "192.0.2.1 1234"},
            {"address": "192.0.2.1:65536"},
            {"address": "h" * 254},
            {"address": "192.0.2.1", "ppm": 200},
        ]
        for change in refused:
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.configure(change)
        await asyncio.sleep(0.05)
        self.assertEqual(source.describe(), before)
        self.assertEqual(self.fake.commands, commands)
        self.assertEqual(self.changes, changes)

    async def test_a_tuner_without_gains_offers_automatic_gain_only(self):
        for fake in (FakeRtlTcp(tuner_type=5, gain_count=12), FakeRtlTcp(tuner_type=9, gain_count=29)):
            with self.subTest(tuner=fake.tuner_type, gains=fake.gain_count):
                source = await self.start(fake, gain_db=20)
                await self.read(source)
                self.assertIsNone(source.describe()["gain_db"])
                self.assertEqual(source.describe()["gains_db"], [])
                with self.assertRaises(ValueError):
                    source.configure({"gain_db": 20})
                source.configure({"gain_db": None})

    async def test_settings_out_of_the_tuner_range_are_brought_within_it(self):
        source = await self.start(FakeRtlTcp(tuner_type=4, gain_count=1))
        source.configure({"centre_hz": 2000e6})     # the tuner is not known yet
        await self.read(source)
        self.assertEqual(source.centre_hz, 924e6)
        self.assertEqual(source.describe()["tuner"], "FCI FC2580")

    async def test_changes_while_disconnected_apply_at_the_next_connection(self):
        source = await self.start()
        source.configure({"centre_hz": 433_000_000, "gain_db": 40})
        self.assertEqual(self.fake.commands, [])
        await self.read(source)
        self.assertEqual(self.fake.commands,
                         [(0x03, 1), (0x04, 402), (0x05, 0), (0x02, 2_400_000), (0x01, 433_000_000)])

    def test_refuses_invalid_settings_at_creation(self):
        for options in ({"centre_hz": 1e10}, {"gain_db": "auto"}, {"ppm": 101}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RtlTcpSource(LOOPBACK, 1, **options)

    def test_the_header_names_the_tuner(self):
        self.assertEqual(tuner_for(6, 29).name, "Rafael Micro R828D")
        self.assertEqual(tuner_for(1, 14).range_hz, (52e6, 2200e6))
        self.assertEqual(tuner_for(0, 0).name, "Unknown tuner (type 0)")


class AddressTest(SourceTest):
    async def other_server(self) -> FakeRtlTcp:
        """A second fake server, closed after the test."""
        fake = FakeRtlTcp()
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        return fake

    async def test_a_new_address_hangs_up_and_connects_there_with_the_same_settings(self):
        source = await self.start(centre_hz=433_000_000, gain_db=40, ppm=-4)
        await self.read(source)
        other = await self.other_server()
        source.configure({"address": f"{LOOPBACK}:{other.port}"})
        state = source.describe()
        self.assertEqual((state["address"], state["status"], state["tuner"]),
                         (f"{LOOPBACK}:{other.port}", "connecting", None))
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(source.status, "streaming")
        self.assertEqual(other.commands,
                         [(0x03, 1), (0x04, 402), (0x05, 2**32 - 4), (0x02, 2_400_000), (0x01, 433_000_000)])
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertEqual(self.fake.connections, 1)

    async def test_a_new_address_while_waiting_is_tried_at_once(self):
        source = RtlTcpSource(LOOPBACK, free_port(), **{**FAST, "retry_s": (5.0, 5.0)})
        self.addCleanup(source.close)
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        other = await self.other_server()
        changed = time.monotonic()
        source.configure({"address": str(other.port)})     # on 127.0.0.1, rtl_tcp's default host
        self.assert_labelled(source, await self.read(source))
        self.assertLess(time.monotonic() - changed, 2.0)    # not after the 5 s wait before a retry

    async def test_the_address_in_use_changes_nothing(self):
        source = await self.start()
        await self.read(source)
        for same in (f"{LOOPBACK}:{self.fake.port}", str(self.fake.port), f" {LOOPBACK}:{self.fake.port} "):
            source.configure({"address": same})
        self.assert_labelled(source, await self.read(source))
        self.assertEqual((source.status, self.fake.connections), ("streaming", 1))

    def test_without_an_address_the_default_one_waits_for_the_saved_one(self):
        source = RtlTcpSource()     # nothing reads: nothing connects
        self.assertEqual((source.address, source.given), ("127.0.0.1:1234", frozenset()))
        source.configure({"address": "192.0.2.10"})     # as the engine restores a saved address
        self.assertEqual((source.address, source.status), ("192.0.2.10:1234", "idle"))


class AddressParsingTest(unittest.TestCase):
    def test_reads_a_host_a_port_or_both(self):
        addresses = {
            "40000": (LOOPBACK, 40000),
            "radio.example": ("radio.example", 1234),
            " 192.0.2.10:40000 ": ("192.0.2.10", 40000),
            "[::1]": ("::1", 1234),
            "[fe80::1%eth0]:40000": ("fe80::1%eth0", 40000),
        }
        for text, address in addresses.items():
            with self.subTest(text=text):
                self.assertEqual(parse_address(text), address)

    def test_says_why_it_refuses_an_address(self):
        reasons = {
            " ": "no address given",
            "192.0.2.10 40000": "the address must be HOST, PORT or HOST:PORT",
            ":40000": "the address must be HOST, PORT or HOST:PORT",
            "::1": "an IPv6 address goes in brackets",
            "[::1]40000": "an IPv6 address goes in brackets",
            "host:0": "the port must be a whole number from 1 to 65535",
            "host:" + "9" * 5000: "the port must be a whole number from 1 to 65535",
            "x" * 254: "the host name is too long",
        }
        for text, reason in reasons.items():
            with self.subTest(text=text[:20]), self.assertRaisesRegex(ValueError, reason):
                parse_address(text)


class ConnectionTest(SourceTest):
    async def test_waits_while_refused_then_connects(self):
        port = free_port()
        source = RtlTcpSource(LOOPBACK, port, **FAST)
        self.addCleanup(source.close)
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        self.assertEqual(source.detail, "connection refused")
        async with FakeRtlTcp(port=port) as fake:
            self.fake = fake
            samples = await self.read(source)
            self.assert_labelled(source, samples)
            self.assertEqual(source.status, "streaming")
            source.close()

    async def test_reconnects_after_a_lost_connection_and_sends_the_settings_again(self):
        source = await self.start()
        source.configure({"centre_hz": 433_000_000})
        await self.read(source)
        self.fake.drop()
        await eventually(lambda: self.fake.connections == 2 and source.status == "streaming")
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(self.fake.commands[4:], self.fake.commands[:4])

    async def test_a_lost_connection_is_a_waiting_state(self):
        source = await self.start()
        await self.read(source)
        statuses = []
        source.on_change = lambda: statuses.append((source.status, source.detail))
        self.fake.drop()
        await eventually(lambda: ("streaming", "") in statuses)
        self.assertEqual(statuses[0], ("waiting", "connection lost"))

    async def test_another_server_is_not_rtl_tcp(self):
        source = await self.start(FakeRtlTcp(magic=b"SSH-"))
        self.assertIsNone(await source.read(COUNT))
        await eventually(lambda: source.status == "waiting")
        self.assertEqual(source.detail, "not an rtl_tcp server")

    async def test_a_server_that_never_answers(self):
        source = await self.start(FakeRtlTcp(header=False))
        while source.status != "waiting":
            self.assertIsNone(await source.read(COUNT))
        self.assertEqual(source.detail, "no answer (rtl_tcp may be serving another client)")

    async def test_a_silent_server_is_left_then_tried_again(self):
        source = await self.start()
        await self.read(source)
        statuses = []
        source.on_change = lambda: statuses.append((source.status, source.detail))
        self.fake.sending = False
        while ("waiting", "no samples") not in statuses:
            self.assertIsNone(await source.read(COUNT))
        self.fake.sending = True
        self.assert_labelled(source, await self.read(source))
        self.assertGreaterEqual(self.fake.connections, 2)

    async def test_hangs_up_when_nobody_reads_and_connects_again_at_the_next_read(self):
        source = await self.start(idle_s=0.3)
        await self.read(source)
        await eventually(lambda: self.fake.open_connections == 0)
        await eventually(lambda: source.status == "idle")
        self.assert_labelled(source, await self.read(source))
        self.assertEqual(self.fake.connections, 2)

    async def test_close_hangs_up_for_good(self):
        source = await self.start()
        await self.read(source)
        source.close()
        await eventually(lambda: self.fake.open_connections == 0)
        self.assertIsNone(await source.read(COUNT))
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.connections, 1)


@unittest.skipUnless(os.name == "posix", "POSIX processes and signals")
class RtlSdrSourceTest(SourceTest):
    """RtlSdrSource running the fake rtl_tcp program in place of rtl_tcp."""

    def setUp(self):
        self.runs = FakeRtlTcpRuns(self)

    async def start_sdr(self, device: str = "0", **options) -> RtlSdrSource:
        # A whole Python starts the fake program: give it time.
        source = RtlSdrSource(device, program=FAKE_RTL_TCP,
                              **{**FAST, "timeout_s": 5.0, "stop_s": 2.0, **options})

        async def close():
            source.close()
            await asyncio.wait_for(source.wait_closed(), 10)

        self.addAsyncCleanup(close)
        return source

    async def read_until(self, source: RtlSdrSource, condition, timeout: float = 10.0) -> None:
        """Read until ``condition()`` holds: reading keeps the source trying."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError(f"the condition never held: {source.status} ({source.detail})")
            await source.read(COUNT)

    def assert_ended(self, pid: int) -> None:
        """The process has ended, and its parent has collected its exit status."""
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_runs_rtl_tcp_on_a_free_loopback_port_and_streams(self):
        source = await self.start_sdr()
        self.assertEqual(self.runs.started(), [])
        self.assert_labelled(source, await self.read(source, timeout=10))
        [(_, arguments)] = self.runs.started()
        self.assertRegex(arguments, r"^-a 127\.0\.0\.1 -p [1-9]\d* -n 10 -d 0$")
        state = source.describe()
        self.assertEqual((state["kind"], state["status"], state["address"], state["tuner"]),
                         ("rtl_sdr", "streaming", None, "Rafael Micro R820T"))

    async def test_stops_rtl_tcp_when_nobody_reads_and_starts_it_again_at_the_next_read(self):
        source = await self.start_sdr(idle_s=0.3)
        await self.read(source, timeout=10)
        [(first, _)] = self.runs.started()
        await eventually(lambda: source.status == "idle")
        self.assertEqual(self.runs.stopping(), {first})
        self.assert_ended(first)
        self.assert_labelled(source, await self.read(source, timeout=10))
        self.assertEqual(len(self.runs.started()), 2)

    async def test_close_stops_rtl_tcp(self):
        source = await self.start_sdr()
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        source.close()
        await asyncio.wait_for(source.wait_closed(), 10)
        self.assertEqual(self.runs.stopping(), {pid})
        self.assert_ended(pid)
        self.assertIsNone(await source.read(COUNT))

    async def test_a_receiver_rtl_tcp_cannot_open_is_a_waiting_state_with_the_reason(self):
        reasons = {
            "none": "no receiver found",
            "busy": "the receiver is in use by another program",
            "denied": "no permission to open the receiver",
        }
        for device, reason in reasons.items():
            with self.subTest(device=device):
                source = await self.start_sdr(device)
                await self.read_until(source, lambda: source.status == "waiting")
                self.assertEqual(source.detail, reason)
                runs = len(self.runs.started())
                await self.read_until(source, lambda: len(self.runs.started()) > runs)
                source.close()
                await asyncio.wait_for(source.wait_closed(), 10)
        for pid, _ in self.runs.started():
            self.assert_ended(pid)

    async def test_an_unknown_failure_gives_the_exit_code_and_logs_the_messages(self):
        source = await self.start_sdr("crash")
        with self.assertLogs("radiolack.rtltcp", "WARNING") as logs:
            await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp stopped (exit code 3)")
        self.assertIn("Something unexpected happened", logs.output[0])

    async def test_rtl_tcp_that_never_listens_is_stopped_then_tried_again(self):
        source = await self.start_sdr("mute", timeout_s=1.5)
        await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp did not start")
        first = self.runs.started()[0][0]
        self.assertIn(first, self.runs.stopping())
        self.assert_ended(first)
        await self.read_until(source, lambda: len(self.runs.started()) > 1)

    async def test_rtl_tcp_that_ignores_sigterm_is_killed(self):
        source = await self.start_sdr("stubborn", stop_s=0.5)
        await self.read(source, timeout=10)
        [(pid, _)] = self.runs.started()
        with self.assertLogs("radiolack.rtltcp", "WARNING") as logs:
            source.close()
            await asyncio.wait_for(source.wait_closed(), 10)
        self.assertIn("did not stop within 0.5 s", logs.output[0])
        self.assert_ended(pid)
        self.assertEqual(self.runs.stopping(), set())

    async def test_a_missing_program_is_a_waiting_state(self):
        source = RtlSdrSource(program=("/nonexistent/rtl_tcp",), **FAST)
        self.addCleanup(source.close)
        await self.read_until(source, lambda: source.status == "waiting")
        self.assertEqual(source.detail, "rtl_tcp is not installed")

    def test_needs_a_receiver_and_a_program(self):
        with self.assertRaisesRegex(ValueError, "no receiver given"):
            RtlSdrSource("")
        with self.assertRaisesRegex(ValueError, "no program given"):
            RtlSdrSource(program=())

    def test_chooses_its_own_address(self):
        source = RtlSdrSource(program=FAKE_RTL_TCP)     # nothing reads: nothing starts
        state = source.describe()
        self.assertIsNone(state["address"])
        self.assertEqual(state["settable"], ["centre_hz", "sample_rate_hz", "gain_db", "ppm"])
        with self.assertRaisesRegex(ValueError, "unknown setting: address"):
            source.configure({"address": "192.0.2.10"})

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("setpriv"),
                         "setpriv, on Linux")
    async def test_rtl_tcp_stops_when_radiolack_is_killed(self):
        radiolack = await asyncio.create_subprocess_exec(
            sys.executable, "-c", KILLED_RADIOLACK, *FAKE_RTL_TCP, stdout=asyncio.subprocess.PIPE)
        try:
            line = await asyncio.wait_for(radiolack.stdout.readline(), 20)
            self.assertEqual(line, b"streaming\n")
            [(pid, _)] = self.runs.started()
        finally:
            radiolack.kill()
            await radiolack.wait()
        await eventually(lambda: not running(pid), timeout=10)
        self.assertEqual(self.runs.stopping(), {pid})


if __name__ == "__main__":
    unittest.main()
