"""Tests for a radio in a process of its own (radioproc.py, radioproxy.py;
later-milestones.md, section 5, Processes): the messages' framing, and an
engine whose radios after the first run in processes of their own, on the
simulated band or the fake rtl_tcp program, never a receiver."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from radiolack.demod import FRAME_KIND_AUDIO
from radiolack.engine import Engine, Subscription
from radiolack.keptscans import KeptScans
from radiolack.radioproc import FRAME, Channel
from radiolack.radioproxy import RadioProcess
from radiolack.sources import Sources
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import SETTINGS_FILE, Store
from tests.fakes import FAKE_RTL_TCP, FakeRtlTcpRuns, FakeSource

WAIT_S = 20.0       # for a radio's process to start and answer, on a busy computer


async def message_where(subscription: Subscription, wanted: Callable[[object], bool],
                        timeout: float = WAIT_S) -> object:
    """The first message for ``subscription`` that ``wanted`` accepts."""
    deadline = time.monotonic() + timeout
    while True:
        message = await asyncio.wait_for(subscription.get(), max(0.01, deadline - time.monotonic()))
        if message is None:
            raise AssertionError("the engine stopped")
        if wanted(message):
            return message


def state(kind: str, test: Callable[[dict], bool] = lambda _: True) -> Callable[[object], bool]:
    return lambda message: isinstance(message, dict) and message.get("type") == kind and test(message)


async def until(condition: Callable[[], bool], timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("not reached in time")
        await asyncio.sleep(0.05)


class ChannelTest(unittest.IsolatedAsyncioTestCase):
    async def connected(self) -> tuple[Channel, Channel]:
        ours, theirs = socket.socketpair()
        one = Channel(*await asyncio.open_connection(sock=ours))
        other = Channel(*await asyncio.open_connection(sock=theirs))
        self.addCleanup(one.close)
        self.addCleanup(other.close)
        return one, other

    async def test_messages_cross_with_their_data_in_order(self):
        one, other = await self.connected()
        one.post({"op": "states", "states": {}})
        one.post({"op": "data", "kind": "frame", "clients": [1]}, b"\x01\x02\x03")
        self.assertEqual(await other.receive(), ({"op": "states", "states": {}}, b""))
        self.assertEqual(await other.receive(), ({"op": "data", "kind": "frame", "clients": [1]}, b"\x01\x02\x03"))
        one.close()
        self.assertIsNone(await other.receive())

    async def test_a_message_that_breaks_the_framing_is_refused(self):
        one, other = await self.connected()
        one._writer.write(FRAME.pack(1 << 30, 0))
        with self.assertRaises(ValueError):
            await other.receive()
        one, other = await self.connected()
        text = b'["no operation"]'
        one._writer.write(FRAME.pack(len(text), 0) + text)
        with self.assertRaises(ValueError):
            await other.receive()


class FollowTest(unittest.TestCase):
    """The parent's copy of a scan that a radio's process runs."""

    def entry(self, **changes) -> dict:
        scan = KeptScans().start("Far", 1000.0, 144e6, 146e6, False, None)
        return {**scan.stored(), "id": 7, **changes}

    def test_a_followed_scan_joins_under_an_id_of_its_own_and_keeps_its_name(self):
        kept = KeptScans()
        kept.start("Here", 500.0, 430e6, 440e6, False, None)

        def far() -> dict:
            return next(summary for summary in kept.summaries() if summary["id"] == 2)
        self.assertTrue(kept.follow((2, 7), self.entry()))
        self.assertEqual((far()["name"], far()["ended"]), ("Far", None))
        kept.rename(2, "Renamed")
        channels = [{"frequency_hz": 145_000_000, "first_s": 1001, "last_s": 1002, "searches": 2,
                     "margin_db": 12.0, "open_s": 0.0}]
        self.assertTrue(kept.follow((2, 7), self.entry(passes=1, channels=channels)))
        self.assertEqual((far()["name"], far()["passes"], far()["channels"]), ("Renamed", 1, 1))
        self.assertTrue(kept.follow((2, 7), self.entry(ended="stopped", end_s=1010)))
        self.assertEqual(far()["ended"], "stopped")
        self.assertEqual([scan.name for scan in kept.running], ["Here"])
        # Once ended it is no longer followed: a later message starts nothing.
        self.assertFalse(kept.follow((2, 7), self.entry(ended="stopped", end_s=1010)))
        self.assertEqual(len(kept), 2)

    def test_an_entry_that_is_not_valid_changes_nothing(self):
        kept = KeptScans()
        with self.assertLogs("radiolack.keptscans", "WARNING"):
            self.assertFalse(kept.follow((2, 7), {"id": 7}))
        self.assertEqual(len(kept), 0)


class RadioProcessTest(unittest.IsolatedAsyncioTestCase):
    """An engine whose first radio reads a fake source, and whose other
    radios run in processes of their own."""

    async def asyncSetUp(self):
        self.runs = FakeRtlTcpRuns(self)    # also where a radio's process imports from
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "config")
        self.engine = self.started()

    def started(self, given: bool = True) -> Engine:
        """An engine whose first radio reads a fake source, or without
        ``given`` the source used last."""
        engine = Engine(FakeSource() if given else None, sources=Sources(rtl_tcp_program=FAKE_RTL_TCP, usb_devices=self.root / "usb"),
                        store=self.store, recordings=self.root / "recordings", radio_processes=True,
                        save_delay_s=0.05)
        task = asyncio.ensure_future(engine.run())

        async def stop() -> None:
            engine.close()
            await asyncio.wait_for(engine.wait_closed(), WAIT_S)
            await asyncio.wait_for(task, WAIT_S)
        self.addAsyncCleanup(stop)
        return engine

    async def shown(self, described: dict) -> tuple[RadioProcess, Subscription]:
        subscription = self.engine.subscribe()
        radio = self.engine.add_radio(described)
        self.assertIsInstance(radio, RadioProcess)
        self.engine.configure_radios({"id": radio.id}, subscription)
        await message_where(subscription, state("source", lambda message: message["kind"] == described["kind"]
                                                and message["status"] == "streaming"))
        return radio, subscription

    async def test_an_added_radio_runs_in_its_process_and_sends_its_states_frames_and_audio(self):
        radio, subscription = await self.shown({"kind": "synthetic"})
        self.assertNotEqual(radio._process.pid, os.getpid())
        frame = await message_where(subscription, lambda message: isinstance(message, bytes)
                                    and message[0] == FRAME_KIND_SPECTRUM)
        source = radio.state()
        self.assertEqual(FRAME_HEADER.unpack_from(frame)[4], source["centre_in_use_hz"])
        radio.set_audio(subscription, True)
        await message_where(subscription, lambda message: isinstance(message, bytes) and message[0] == FRAME_KIND_AUDIO)
        # A change acknowledged with its new settings, and a refusal to the interface that asked.
        radio.configure_demod({"mode": "AM"}, subscription, 5)
        demod = await message_where(subscription, state("demod", lambda message: message["acknowledged"] == 5))
        self.assertEqual(demod["mode"], "AM")
        radio.configure_demod({"mode": "no such mode"}, subscription, 6)
        error = await message_where(subscription, state("error"))
        self.assertEqual(error["command"], "demod")
        # The radios' list names it, with what it reads.
        self.assertEqual([(entry["id"], entry["kind"], entry["status"])
                          for entry in self.engine.radios_state()["radios"]],
                         [(1, "test", "streaming"), (2, "synthetic", "streaming")])

    async def test_it_watches_the_bookmarks(self):
        radio, subscription = await self.shown({"kind": "synthetic"})
        centre_hz = radio.state()["centre_hz"]
        self.engine.edit_bookmarks({"action": "add", "frequency_hz": centre_hz + 100_000, "name": "Within",
                                    "mode": "NFM", "scan": True})
        radio.configure_scanner({"scan": "memory"})
        scanner = await message_where(subscription, state("scanner", lambda message: message["scan"] == "memory"))
        self.assertEqual(len(scanner["memory"]["watched"]), 1)

    async def test_its_range_scan_and_the_signals_it_finds_are_kept_here(self):
        # The fake rtl_tcp program's carrier: the simulated band's signals are kept nowhere.
        radio, subscription = await self.shown({"kind": "rtl_sdr", "device": "carrier"})
        radio.configure({"centre_hz": 145_000_000})
        await message_where(subscription, state("source", lambda message: message["centre_hz"] == 145_000_000))
        radio.configure_scanner({"scan": "signals"})
        await until(lambda: len(self.engine.kept.running) == 1)
        await until(lambda: bool(self.engine.observations.describe()))
        await until(lambda: self.engine.kept.summaries()[-1]["channels"] > 0)
        radio.configure_scanner({"scan": None})
        await until(lambda: not self.engine.kept.running)
        self.assertEqual(self.engine.kept.summaries()[-1]["ended"], "stopped")

    async def test_removed_it_releases_its_receiver_and_its_settings_are_saved(self):
        radio, subscription = await self.shown({"kind": "rtl_sdr", "device": "0"})
        [(pid, _)] = self.runs.started()
        frequency_hz = radio.state()["centre_hz"] + 123_000
        radio.configure_demod({"frequency_hz": frequency_hz}, subscription, 1)
        await message_where(subscription, state("demod", lambda message: message["acknowledged"] == 1))
        process = radio._process
        self.engine.configure_radios({"remove": radio.id}, subscription)
        self.assertIs(subscription.radio, self.engine.radios[0])
        await asyncio.wait_for(radio.wait_closed(), WAIT_S)
        self.assertEqual(process.returncode, 0)
        self.assertIn(pid, self.runs.stopping())
        self.assertEqual(self.engine.receivers[radio.source.settings_key]["demod"]["frequency_hz"], frequency_hz)

    async def test_a_process_that_ends_unasked_takes_its_radio_away(self):
        radio, subscription = await self.shown({"kind": "synthetic"})
        with self.assertLogs("radiolack.radioproxy", "ERROR"):
            radio._process.send_signal(signal.SIGKILL)
            error = await message_where(subscription, state("error"))
        self.assertEqual(error["command"], "radio")
        self.assertIn("radio 2 stopped", error["message"])
        self.assertEqual([entry["id"] for entry in self.engine.radios_state()["radios"]], [1])
        self.assertIs(subscription.radio, self.engine.radios[0])

    async def test_its_log_records_reach_radiolack_s_logger_with_its_number(self):
        radio, _ = await self.shown({"kind": "synthetic"})
        with self.assertLogs("radiolack.radio", "WARNING") as logs:
            radio._handle({"op": "log", "level": logging.WARNING, "name": "radiolack.radio", "message": "a test"}, b"")
        self.assertEqual(logs.records[0].getMessage(), "radio 2: a test")

    async def test_a_restart_brings_it_back(self):
        self.engine.close()     # one that starts from the sources used last, the simulated band the first time
        self.engine = self.started(given=False)
        radio, subscription = await self.shown({"kind": "rtl_sdr", "device": "0"})
        frequency_hz = radio.state()["centre_hz"] + 123_000
        radio.configure_demod({"frequency_hz": frequency_hz}, subscription, 1)
        await message_where(subscription, state("demod", lambda message: message["acknowledged"] == 1))
        self.engine.close()
        await asyncio.wait_for(self.engine.wait_closed(), WAIT_S)
        saved = self.store.load(SETTINGS_FILE)
        self.assertEqual(saved["radios"][0]["source"], {"kind": "rtl_sdr", "device": "0"})
        # Its receiver's settings as they stood, which it starts from again.
        self.assertEqual(saved["receivers"][radio.source.settings_key]["demod"]["frequency_hz"], frequency_hz)
        self.engine = self.started(given=False)
        self.assertEqual([(radio.id, radio.source.kind) for radio in self.engine.radios],
                         [(1, "synthetic"), (2, "rtl_sdr")])
        self.assertIsInstance(self.engine.radios[1], RadioProcess)


if __name__ == "__main__":
    unittest.main()
