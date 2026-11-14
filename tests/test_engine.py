"""Tests for the engine: frames, source state and demodulation settings for
every client, audio for the clients that ask, idle without clients, changes
passed to the source, switching to another kind of source, DC removal,
bookmarks and preferences, update checks, saved settings, detection, the
memory scan and its windows, the sweep, clean stop."""

import asyncio
import contextlib
import errno
import json
import math
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO, MODES
from radiolack import __version__, recordings
from radiolack.engine import Engine, Source, Subscription
from radiolack.recordings import wav_header
from radiolack.remote import REMOTE_FILE, RemoteAccess
from radiolack.rtltcp import RtlTcpSource
from radiolack.scanners import VISIT_S, memory_windows, sweep_windows
from radiolack.spectrum import FLAG_SQUELCH_OPEN, FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import (FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, LOOPBACK, FakeRtlTcp, FakeSource, FakeUpdates,
                         free_port, wide_signal)

DC_BIN = 4096       # the centre frequency, with 8192-point FFTs
FAKE_TONE_HZ = FAKE_CENTRE_HZ + (FAKE_TONE_BIN - DC_BIN) * FAKE_RATE_HZ / 8192
# What a new client gets first; a remote client, without the last two.
STATES = ("source", "demod", "bookmarks", "scanner", "peaks", "preferences", "recording", "storage", "update")


def scanner_state(**changes) -> dict:
    """The scanner state message: the defaults, without a scan or a ticked
    bookmark, with ``changes``."""
    return {"type": "scanner", "detection": True, "threshold_db": 12.0, "ignored_hz": [], "resume_s": 2.0,
            "sweep_low_hz": 430e6, "sweep_high_hz": 440e6, "sweep_window_s": 0.5, "scan": None, "stopped": None,
            "stopped_scan": None,
            "memory": {"watched": [], "windows": 0, "window": None, "state": None, "bookmark_id": None},
            "sweep": {"windows": None, "window": None, "passes": None}, **changes}


async def next_message(subscription: Subscription):
    return await asyncio.wait_for(subscription.get(), timeout=5)


def ready_messages(subscription: Subscription) -> list:
    """The messages ready for a client, now."""
    messages = []
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            messages.append(subscription.get_nowait())
    return messages


class OtherSource(FakeSource):
    """Another kind of fake source, on another frequency."""

    kind = "other"
    centre_hz = FAKE_CENTRE_HZ + 1e6


class AlternatingSource(FakeSource):
    """A fake source whose tone reads -20 dBFS, then -30 dBFS, in turn."""

    async def read(self, count: int) -> np.ndarray | None:
        samples = await super().read(count)
        if samples is not None and self.reads % 2 == 0:
            samples = samples * np.float32(10 ** -0.5)
        return samples


async def stop(engine: Engine, task: asyncio.Future) -> None:
    engine.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class FakeSources:
    """Opens fake sources by kind, and keeps what it opened."""

    kinds = ("test", "other")

    def __init__(self, **factories) -> None:
        self.factories = {"test": FakeSource, "other": OtherSource, **factories}
        self.opened: list[FakeSource] = []

    def open(self, reopen) -> FakeSource:
        factory = self.factories.get(reopen.get("kind"))
        if factory is None:
            raise ValueError(f"unknown kind of source: {reopen.get('kind')!r}")
        self.opened.append(factory())
        return self.opened[-1]


async def next_frame(subscription: Subscription) -> bytes | None:
    """The next frame, skipping state messages; None once the engine stopped."""
    while isinstance(message := await next_message(subscription), dict):
        pass
    return message


def level_db(frame: bytes, index: int) -> float:
    """The level of bin ``index`` of a frame, in dBFS."""
    db_min, db_step = FRAME_HEADER.unpack_from(frame)[6:8]
    return db_min + frame[FRAME_HEADER.size + index] * db_step


def is_audio(message) -> bool:
    return isinstance(message, bytes) and message[0] == FRAME_KIND_AUDIO


def waiting(subscription: Subscription) -> list:
    """Every message ready now, without waiting for more."""
    messages = []
    while True:
        try:
            messages.append(subscription.get_nowait())
        except asyncio.QueueEmpty:
            return messages


def preferences_of(subscription: Subscription) -> dict:
    """The values of the preferences message ready for a client, now."""
    [message] = [message for message in waiting(subscription)
                 if isinstance(message, dict) and message["type"] == "preferences"]
    return message["values"]


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = FakeSource()
        self.engine = Engine(self.source, queue_size=4)
        self.task = asyncio.ensure_future(self.engine.run())

    async def asyncTearDown(self):
        self.engine.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def test_idle_until_a_client_listens(self):
        await asyncio.sleep(0.05)
        self.assertEqual(self.source.reads, 0)
        subscription = self.engine.subscribe()
        await next_frame(subscription)
        self.engine.unsubscribe(subscription)
        await asyncio.sleep(0.05)       # lets a read in progress finish
        reads = self.source.reads
        await asyncio.sleep(0.1)
        self.assertEqual(self.source.reads, reads)

    async def test_a_new_client_first_gets_the_states_then_the_frames(self):
        subscription = self.engine.subscribe()
        self.assertEqual(subscription.get_nowait(), {
            "type": "source", "kind": "test", "status": "streaming", "detail": "", "address": None,
            "tuner": None, "centre_hz": FAKE_CENTRE_HZ, "sample_rate_hz": FAKE_RATE_HZ,
            "gain_db": None, "ppm": None, "gains_db": [], "range_hz": [FAKE_CENTRE_HZ, FAKE_CENTRE_HZ],
            "dc_removal": True, "settable": ["dc_removal"], "kinds": [],    # this engine has one source only
        })
        self.assertEqual(subscription.get_nowait(), {
            "type": "demod", "frequency_hz": FAKE_CENTRE_HZ + FAKE_RATE_HZ / 4, "mode": "NFM",
            "bandwidth_hz": 10_000.0, "squelch_db": -80.0, "squelch_auto": True, "modes": list(MODES),
            "audio_modes": list(MODES), "acknowledged": 0,
        })
        self.assertEqual(subscription.get_nowait(), {"type": "bookmarks", "bookmarks": []})
        self.assertEqual(subscription.get_nowait(), scanner_state())
        self.assertEqual(subscription.get_nowait(), {"type": "peaks", "peaks": []})
        self.assertEqual(subscription.get_nowait(), {"type": "preferences", "values": {}})
        # Without a recordings folder, nothing is recorded.
        self.assertEqual(subscription.get_nowait(), {"type": "recording", "audio": None, "stopped": None,
                                                     "folder": None, "default_folder": None})
        # Without a store, nothing is saved.
        self.assertEqual(subscription.get_nowait(),
                         {"type": "storage", "directory": None, "saving": False, "problem": ""})
        # Without an update check, the engine never checks.
        self.assertEqual(subscription.get_nowait(), {
            "type": "update", "status": "unavailable", "version": __version__, "latest": None,
            "detail": "This Radiolack does not check for updates.", "check_at_start": True})
        self.assertIsInstance(await next_message(subscription), bytes)

    async def test_every_client_gets_the_frames(self):
        subscriptions = [self.engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            sequences = []
            for _ in range(3):
                frame = await next_frame(subscription)
                kind, version, _, sequence, centre, rate, db_min, db_step, *_ = FRAME_HEADER.unpack_from(frame)
                self.assertEqual((kind, version), (FRAME_KIND_SPECTRUM, 2))
                sequences.append(sequence)
                self.assertEqual((centre, rate), (FAKE_CENTRE_HZ, FAKE_RATE_HZ))
                codes = np.frombuffer(frame, np.uint8, offset=FRAME_HEADER.size)
                self.assertEqual(len(codes), 8192)
                self.assertEqual(int(np.argmax(codes)), FAKE_TONE_BIN)
                self.assertAlmostEqual(db_min + int(codes.max()) * db_step, -20.0, delta=0.5)
            self.assertEqual(sequences, sorted(sequences))

    async def test_a_slow_client_keeps_only_the_latest_frames(self):
        subscription = self.engine.subscribe()
        for kind in STATES:
            self.assertEqual(subscription.get_nowait()["type"], kind)
        while self.source.reads < 12:
            await asyncio.sleep(0.01)
        frames = waiting(subscription)
        self.assertEqual(len(frames), 4)
        sequences = [FRAME_HEADER.unpack_from(frame)[3] for frame in frames]
        self.assertEqual(sequences, list(range(sequences[0], sequences[0] + 4)))
        self.assertGreaterEqual(sequences[0], 6)

    async def test_a_state_change_reaches_every_client_before_more_frames(self):
        subscriptions = [self.engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            await next_frame(subscription)
        while self.source.reads < 8:    # frames are waiting for both clients
            await asyncio.sleep(0.01)
        self.source.on_change()
        for subscription in subscriptions:
            self.assertEqual(subscription.get_nowait()["type"], "source")
            self.assertTrue(all(isinstance(message, bytes) for message in waiting(subscription)))

    async def test_replies_go_to_one_client_before_its_frames(self):
        first, second = self.engine.subscribe(), self.engine.subscribe()
        for subscription in (first, second):
            await next_frame(subscription)
        while self.source.reads < 8:
            await asyncio.sleep(0.01)
        first.reply({"type": "error", "message": "refused"})
        self.assertEqual(first.get_nowait(), {"type": "error", "message": "refused"})
        self.assertTrue(all(isinstance(message, bytes) for message in waiting(second)))

    async def test_changes_go_to_the_source(self):
        with self.assertRaisesRegex(ValueError, "the test source cannot change its settings"):
            self.engine.configure({"centre_hz": 433e6})

    async def test_dc_removal_is_the_engines_own_setting_for_every_client(self):
        subscriptions = [self.engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            self.assertTrue(subscription.get_nowait()["dc_removal"])
        self.engine.configure({"dc_removal": False})
        for subscription in subscriptions:
            self.assertFalse(subscription.get_nowait()["dc_removal"])

    async def test_a_refused_change_changes_nothing(self):
        subscription = self.engine.subscribe()
        waiting(subscription)
        refused = (
            ({"dc_removal": "off"}, "DC removal must be true or false"),
            ({"dc_removal": None}, "DC removal must be true or false"),
            ({"dc_removal": 0}, "DC removal must be true or false"),
            ({"dc_removal": False, "centre_hz": 433e6}, "the test source cannot change its settings"),
        )
        for changes, error in refused:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                self.engine.configure(changes)
        self.assertTrue(self.engine.state()["dc_removal"])
        self.assertFalse(any(isinstance(message, dict) for message in waiting(subscription)))

    async def test_close_ends_every_stream_and_closes_the_source(self):
        subscription = self.engine.subscribe()
        await next_frame(subscription)
        self.engine.close()
        self.assertIsNone(await next_message(subscription))
        await asyncio.wait_for(self.task, timeout=5)
        self.assertIsNone(await next_message(self.engine.subscribe()))
        self.assertTrue(self.source.closed)
        await asyncio.wait_for(self.engine.wait_closed(), timeout=5)


class DemodulationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = FakeSource()
        self.engine = Engine(self.source)
        self.task = asyncio.ensure_future(self.engine.run())

    async def asyncTearDown(self):
        self.engine.close()
        await asyncio.wait_for(self.task, timeout=5)

    async def test_a_change_reaches_every_client_and_is_acknowledged_to_its_sender(self):
        sender, other = self.engine.subscribe(), self.engine.subscribe()
        waiting(sender), waiting(other)
        self.engine.configure_demod({"frequency_hz": 145_010_000, "bandwidth_hz": 12_500}, sender, 7)
        for subscription, acknowledged in ((sender, 7), (other, 0)):
            state = subscription.get_nowait()
            self.assertEqual((state["type"], state["frequency_hz"], state["bandwidth_hz"], state["acknowledged"]),
                             ("demod", 145_010_000.0, 12_500.0, acknowledged))
        # Later states tell the sender the same acknowledgement.
        self.engine.configure_demod({"squelch_auto": False})
        self.assertEqual(sender.get_nowait()["acknowledged"], 7)

    async def test_a_refused_change_changes_nothing_but_is_acknowledged(self):
        sender, other = self.engine.subscribe(), self.engine.subscribe()
        waiting(sender), waiting(other)
        with self.assertRaisesRegex(ValueError, "the mode must be one of"):
            self.engine.configure_demod({"bandwidth_hz": 6_000, "mode": "DSB"}, sender, 3)
        state = sender.get_nowait()
        self.assertEqual((state["mode"], state["bandwidth_hz"], state["acknowledged"]), ("NFM", 10_000.0, 3))
        self.assertFalse(any(isinstance(message, dict) for message in waiting(other)))

    async def test_frames_carry_the_channel_level_and_squelch(self):
        subscription = self.engine.subscribe()
        for threshold, is_open in ((-30.0, True), (-10.0, False)):
            with self.subTest(threshold=threshold):
                self.engine.configure_demod({"frequency_hz": FAKE_TONE_HZ, "squelch_auto": False,
                                             "squelch_db": threshold})
                for _ in range(20):     # frames computed before the change come first
                    if FRAME_HEADER.unpack_from(await next_frame(subscription))[9] == threshold:
                        break
                # The next frame, clear of the filters' response to the new VFO.
                _, _, flags, *_, level, squelch = FRAME_HEADER.unpack_from(await next_frame(subscription))
                self.assertEqual(squelch, threshold)
                self.assertAlmostEqual(level, -20.0, delta=0.1)
                self.assertEqual(bool(flags & FLAG_SQUELCH_OPEN), is_open)

    async def test_audio_goes_only_to_the_clients_that_ask(self):
        listener, other = self.engine.subscribe(), self.engine.subscribe()
        self.engine.set_audio(listener, True)
        frames = []
        while len(frames) < 5:
            message = await next_message(listener)
            if is_audio(message):
                frames.append(message)
        position = None
        for frame in frames:
            kind, version, _, start, rate = AUDIO_HEADER.unpack_from(frame)
            self.assertEqual((kind, version, rate), (FRAME_KIND_AUDIO, AUDIO_VERSION, 12_000.0))
            if position is not None:
                self.assertEqual(start, position)
            position = start + (len(frame) - AUDIO_HEADER.size) // 2
        self.assertFalse(any(is_audio(message) for message in waiting(other)))
        self.engine.set_audio(listener, False)
        reads = self.source.reads
        while self.source.reads < reads + 3:
            await asyncio.sleep(0.01)
        self.assertFalse(any(is_audio(message) for message in waiting(listener)))

    async def test_a_new_span_brings_the_vfo_inside_it(self):
        subscription = self.engine.subscribe()
        await next_frame(subscription)
        self.source.centre_hz = 146_000_000.0
        while not isinstance(message := await next_message(subscription), dict) or message["type"] != "demod":
            pass
        self.assertEqual(message["frequency_hz"], 146_000_000.0 - FAKE_RATE_HZ / 2)


class DcRemovalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine(FakeSource(dc_offset=0.05))    # -26 dBFS on the centre frequency
        self.task = asyncio.ensure_future(self.engine.run())

    async def asyncTearDown(self):
        self.engine.close()
        await asyncio.wait_for(self.task, timeout=5)

    async def test_removes_a_constant_offset_unless_asked_not_to(self):
        subscription = self.engine.subscribe()
        frame = await next_frame(subscription)
        self.assertLess(level_db(frame, DC_BIN), -90.0)
        self.assertAlmostEqual(level_db(frame, FAKE_TONE_BIN), -20.0, delta=0.5)
        self.engine.configure({"dc_removal": False})
        for _ in range(10):     # frames computed before the change come first
            frame = await next_frame(subscription)
            if level_db(frame, DC_BIN) > -90.0:
                break
        self.assertAlmostEqual(level_db(frame, DC_BIN), -26.0, delta=0.5)
        self.assertAlmostEqual(level_db(frame, FAKE_TONE_BIN), -20.0, delta=0.5)


class EngineSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_again_while_the_source_has_no_samples(self):
        source = FakeSource(empty_reads=3)
        engine = Engine(source)
        task = asyncio.ensure_future(engine.run())
        try:
            self.assertIsInstance(await next_frame(engine.subscribe()), bytes)
        finally:
            engine.close()
            await asyncio.wait_for(task, timeout=5)

    async def test_a_failing_source_is_logged_and_ends_the_streams(self):
        source = FakeSource(fail_after=2)
        engine = Engine(source)
        subscription = engine.subscribe()
        with self.assertLogs("radiolack.engine", "ERROR") as logs:
            await asyncio.wait_for(engine.run(), timeout=5)
        self.assertIn("the engine stopped", logs.output[0])
        frames = []
        while (frame := await next_frame(subscription)) is not None:
            frames.append(frame)
        self.assertLessEqual(len(frames), 2)
        self.assertTrue(source.closed)


class SourceSwitchTest(unittest.IsolatedAsyncioTestCase):
    """An engine that switches between fake sources, without a store."""

    async def asyncSetUp(self):
        self.sources = FakeSources()
        self.engine = Engine(sources=self.sources)
        self.addAsyncCleanup(self.stop, self.engine)

    @staticmethod
    async def stop(engine: Engine) -> None:
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)

    async def test_starts_with_the_first_kind_and_lists_them_all(self):
        state = self.engine.state()
        self.assertEqual((state["kind"], state["kinds"]), ("test", ["test", "other"]))

    async def test_another_kind_replaces_the_source_for_every_client(self):
        first, second = self.engine.subscribe(), self.engine.subscribe()
        for subscription in (first, second):
            ready_messages(subscription)
        self.engine.configure({"kind": "other"})
        old, new = self.sources.opened
        self.assertTrue(old.closed)
        self.assertIs(self.engine.source, new)
        for subscription in (first, second):
            states = {message["type"]: message for message in ready_messages(subscription)}
            self.assertEqual(states["source"]["kind"], "other")
            # The new receiver's own VFO: by default, a quarter of its span above its centre.
            self.assertEqual(states["demod"]["frequency_hz"], OtherSource.centre_hz + FAKE_RATE_HZ / 4)

    async def test_each_kind_comes_back_with_its_settings(self):
        self.engine.configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 10_000, "mode": "AM"})
        self.engine.configure({"kind": "other"})
        self.assertEqual(self.engine.demod_state()["mode"], "NFM")
        self.engine.configure({"kind": "test"})
        demod = self.engine.demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"]), (FAKE_CENTRE_HZ + 10_000, "AM"))
        self.assertEqual(len(self.sources.opened), 3)     # a new source each time

    async def test_the_kind_comes_alone_and_must_be_one_of_the_kinds(self):
        for changes in ({"kind": "other", "dc_removal": False}, {"kind": "radio"}, {"kind": 1}, {"kind": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.configure(changes)
        self.assertEqual((self.engine.source.kind, len(self.sources.opened)), ("test", 1))
        self.assertTrue(self.engine.state()["dc_removal"])

    async def test_the_kind_in_use_keeps_its_source(self):
        source = self.engine.source
        subscription = self.engine.subscribe()
        ready_messages(subscription)
        self.engine.configure({"kind": "test"})
        self.assertIs(self.engine.source, source)
        self.assertFalse(source.closed)
        self.assertEqual([message["type"] for message in ready_messages(subscription)], ["source"])

    async def test_an_engine_without_sources_keeps_its_source(self):
        engine = Engine(FakeSource())
        self.addAsyncCleanup(self.stop, engine)
        self.assertEqual(engine.state()["kinds"], [])
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            engine.configure({"kind": "test"})

    async def test_the_new_source_reads_once_the_old_one_released_its_receiver(self):
        released = asyncio.Event()

        class SlowRelease(FakeSource):
            async def wait_closed(self) -> None:
                await released.wait()

        sources = FakeSources(test=SlowRelease)
        engine = Engine(sources=sources)
        runner = asyncio.ensure_future(engine.run())

        async def stop() -> None:
            released.set()
            await self.stop(engine)
            await asyncio.wait_for(runner, timeout=5)
        self.addAsyncCleanup(stop)
        subscription = engine.subscribe()
        self.assertEqual(FRAME_HEADER.unpack_from(await next_frame(subscription))[4], FAKE_CENTRE_HZ)
        engine.configure({"kind": "other"})
        await asyncio.sleep(0.2)
        self.assertEqual(sources.opened[1].reads, 0)     # the first one still holds the receiver
        released.set()
        while FRAME_HEADER.unpack_from(await next_frame(subscription))[4] != OtherSource.centre_hz:
            pass
        self.assertGreater(sources.opened[1].reads, 0)


CALLING = {"frequency_hz": 145_500_000.0, "name": "FM calling", "mode": "NFM", "scan": True}


class PersistenceTest(unittest.IsolatedAsyncioTestCase):
    """The engine with a store in a temporary folder."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"

    def write(self, name: str, content: dict) -> None:
        self.directory.mkdir(exist_ok=True)
        (self.directory / name).write_text(json.dumps({"version": 1, **content}), encoding="utf-8")

    def read(self, name: str) -> dict:
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def engine(self, source: Source | None = None, sources: FakeSources | None = None, **options) -> Engine:
        if source is None and sources is None:
            source = FakeSource()
        engine = Engine(source, sources=sources, store=Store(self.directory), **options)
        self.addAsyncCleanup(self.stop, engine)
        return engine

    @staticmethod
    async def stop(engine: Engine) -> None:
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)

    async def saved(self, name: str, check) -> dict:
        """The file once ``check`` holds for its content."""
        deadline = time.monotonic() + 5
        while True:
            try:
                content = self.read(name)
            except FileNotFoundError:
                content = None
            if content is not None and check(content):
                return content
            self.assertLess(time.monotonic(), deadline, f"{name} holds {content}")
            await asyncio.sleep(0.01)

    async def test_starts_from_what_was_saved_for_its_receiver(self):
        demod = {"frequency_hz": 145_010_000.0, "mode": "AM", "bandwidth_hz": 8_000.0, "squelch_db": -70.0,
                 "squelch_auto": False}
        self.write(SETTINGS_FILE, {"receivers": {"test": {"source": {"dc_removal": False}, "demod": demod},
                                                 "rtl_sdr 0": {"source": {"ppm": -4}}},
                                   "interface": {"volume": 40, "palette": "classic"}})
        self.write(BOOKMARKS_FILE, {"bookmarks": [CALLING]})
        engine = self.engine()
        self.assertFalse(engine.state()["dc_removal"])
        self.assertEqual({key: engine.demod_state()[key] for key in demod}, demod)
        self.assertEqual(engine.bookmarks_state(), {"type": "bookmarks", "bookmarks": [{"id": 1, **CALLING}]})
        self.assertEqual(engine.preferences_state(),
                         {"type": "preferences", "values": {"volume": 40, "palette": "classic"}})
        self.assertEqual(engine.storage_state(),
                         {"type": "storage", "directory": str(self.directory), "saving": True, "problem": ""})

    async def test_a_saved_setting_that_is_refused_keeps_its_default(self):
        self.write(SETTINGS_FILE, {"receivers": {"test": {
            "source": {"dc_removal": "no", "centre_hz": 433e6},    # the test source has no centre to set
            "demod": {"frequency_hz": 145_010_000, "mode": "DSB", "squelch_db": None,
                      "bandwidth_hz": 10 ** 400}}},     # a whole number too large for a float
            "interface": {"volume": 40, "nested": {"a": 1}, "": 1, "text": "x" * 101}})
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine = self.engine()
        self.assertEqual(len(logs.output), 5)   # DC removal, mode, squelch, filter width and some preferences
        self.assertTrue(engine.state()["dc_removal"])
        state = engine.demod_state()
        self.assertEqual((state["frequency_hz"], state["mode"], state["bandwidth_hz"], state["squelch_db"]),
                         (145_010_000.0, "NFM", 10_000.0, -80.0))
        self.assertEqual(engine.preferences_state()["values"], {"volume": 40})

    async def test_saves_changes_a_moment_later_and_keeps_the_other_receivers(self):
        other = {"source": {"ppm": -4, "gain_db": 40.2}, "demod": {"mode": "NFM"}}
        self.write(SETTINGS_FILE, {"receivers": {"rtl_sdr 0": other}})
        engine = self.engine(save_delay_s=0.05)
        engine.configure({"dc_removal": False})
        engine.configure_demod({"frequency_hz": 145_010_000, "mode": "USB"})
        engine.edit_bookmarks({"action": "add", **CALLING})
        engine.set_preferences({"volume": 25, "palette": "ice", "autoMode": False})
        settings = await self.saved(SETTINGS_FILE, lambda content: content.get("interface"))
        self.assertEqual(settings["version"], 1)
        self.assertEqual(settings["receivers"]["rtl_sdr 0"], other)
        self.assertEqual(settings["receivers"]["test"]["source"], {"dc_removal": False})
        self.assertEqual((settings["receivers"]["test"]["demod"]["frequency_hz"],
                          settings["receivers"]["test"]["demod"]["mode"]), (145_010_000.0, "USB"))
        self.assertEqual(settings["interface"], {"volume": 25, "palette": "ice", "autoMode": False})
        bookmarks = await self.saved(BOOKMARKS_FILE, lambda content: content["bookmarks"])
        self.assertEqual(bookmarks, {"version": 1, "bookmarks": [CALLING]})
        engine.edit_bookmarks({"action": "change", "id": 1, "name": "Calling"})
        await self.saved(BOOKMARKS_FILE, lambda content: content["bookmarks"][0]["name"] == "Calling")

    async def test_a_devices_preferences_are_saved_with_it(self):
        access = RemoteAccess()
        device = access.session(access.pair("iPhone · Safari")).id
        engine = Engine(FakeSource(), store=Store(self.directory), remote=access, save_delay_s=0.05)
        self.addAsyncCleanup(self.stop, engine)
        engine.set_preferences({"palette": "ice"})
        engine.set_preferences({"palette": "gray", "volume": 10}, device=device)
        saved = await self.saved(REMOTE_FILE, lambda content: content["devices"][0].get("preferences"))
        self.assertEqual(saved["devices"][0]["preferences"], {"palette": "gray", "volume": 10})
        settings = await self.saved(SETTINGS_FILE, lambda content: content.get("interface"))
        self.assertEqual(settings["interface"], {"palette": "ice"})
        await self.stop(engine)
        # The next start: the same device starts with them, this computer's interfaces with their own.
        restarted = Engine(FakeSource(), store=Store(self.directory), remote=RemoteAccess())
        self.addAsyncCleanup(self.stop, restarted)
        self.assertEqual(restarted.preferences_state(device)["values"], {"palette": "gray", "volume": 10})
        self.assertEqual(restarted.preferences_state()["values"], {"palette": "ice"})

    async def test_saves_the_last_changes_when_it_stops(self):
        engine = Engine(FakeSource(), store=Store(self.directory), save_delay_s=60)
        engine.configure_demod({"mode": "CW"})
        engine.edit_bookmarks({"action": "add", **CALLING})
        self.assertFalse(self.directory.exists())
        await self.stop(engine)
        self.assertEqual(self.read(SETTINGS_FILE)["receivers"]["test"]["demod"]["mode"], "CW")
        self.assertEqual(self.read(BOOKMARKS_FILE)["bookmarks"], [CALLING])
        again = self.engine()
        self.assertEqual((again.demod_state()["mode"], len(again.bookmarks_state()["bookmarks"])), ("CW", 1))

    async def test_nothing_is_written_until_something_changes(self):
        self.write(SETTINGS_FILE, {"receivers": {"test": {"source": {"dc_removal": True}}}})
        before = (self.directory / SETTINGS_FILE).read_bytes()
        engine = self.engine(save_delay_s=0.01)
        engine.configure({"dc_removal": True})     # the same value
        await self.stop(engine)
        self.assertEqual((self.directory / SETTINGS_FILE).read_bytes(), before)
        self.assertFalse((self.directory / BOOKMARKS_FILE).exists())

    # The engine does not run in these tests: the rtl_tcp sources never read,
    # so nothing connects to their addresses.

    async def test_rtl_tcp_keeps_its_settings_and_the_address_used_last_whatever_the_address(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000", "ppm": -4}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        self.assertEqual((engine.state()["address"], engine.state()["ppm"]), ("192.0.2.10:40000", -4))
        engine.configure({"address": "192.0.2.20"})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["address"] == "192.0.2.20:1234")
        self.assertEqual(settings["receivers"]["rtl_tcp"]["source"]["ppm"], -4)

    async def test_an_address_given_at_start_replaces_the_saved_one(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000", "ppm": -4}}}})
        engine = self.engine(RtlTcpSource("192.0.2.20", 40000), save_delay_s=60)
        self.assertEqual((engine.state()["address"], engine.state()["ppm"]), ("192.0.2.20:40000", -4))
        await self.stop(engine)     # nothing changed meanwhile, but the given address replaces the saved one
        self.assertEqual(self.read(SETTINGS_FILE)["receivers"]["rtl_tcp"]["source"]["address"], "192.0.2.20:40000")

    async def test_an_address_given_as_saved_writes_nothing(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000"}}}})
        before = (self.directory / SETTINGS_FILE).read_bytes()
        await self.stop(self.engine(RtlTcpSource("192.0.2.10", 40000)))
        self.assertEqual((self.directory / SETTINGS_FILE).read_bytes(), before)

    async def test_opens_the_source_used_last(self):
        self.write(SETTINGS_FILE, {"source": {"kind": "other"}})
        self.assertEqual(self.engine(sources=FakeSources()).source.kind, "other")

    async def test_a_saved_source_that_cannot_open_gives_way_to_the_first_kind(self):
        self.write(SETTINGS_FILE, {"source": {"kind": "gone"}})
        with self.assertLogs("radiolack.engine", "WARNING"):
            engine = self.engine(sources=FakeSources())
        self.assertEqual(engine.source.kind, "test")
        await self.stop(engine)
        self.assertEqual(self.read(SETTINGS_FILE)["source"], {"kind": "test"})

    async def test_the_source_in_use_opens_at_the_next_start(self):
        engine = self.engine(sources=FakeSources(), save_delay_s=0.05)
        engine.configure({"kind": "other"})
        await self.saved(SETTINGS_FILE, lambda content: content.get("source") == {"kind": "other"})
        await self.stop(engine)
        self.assertEqual(self.engine(sources=FakeSources()).source.kind, "other")

    async def test_a_source_given_at_start_replaces_the_one_used_last(self):
        self.write(SETTINGS_FILE, {"source": {"kind": "other"}})
        await self.stop(self.engine(FakeSource(), sources=FakeSources(), save_delay_s=60))
        self.assertEqual(self.read(SETTINGS_FILE)["source"], {"kind": "test"})

    async def test_the_first_kind_unchanged_writes_nothing(self):
        await self.stop(self.engine(sources=FakeSources()))
        self.assertFalse((self.directory / SETTINGS_FILE).exists())

    async def test_a_failed_save_reaches_the_clients_until_a_save_works(self):
        engine = self.engine(save_delay_s=0.01)
        subscription = engine.subscribe()
        self.assertTrue(waiting(subscription)[STATES.index("storage")]["saving"])

        async def storage_state() -> dict:
            while True:
                states = [message for message in waiting(subscription)
                          if isinstance(message, dict) and message["type"] == "storage"]
                if states:
                    return states[-1]
                await asyncio.sleep(0.01)

        full = OSError(28, "No space left on device")
        with self.assertLogs("radiolack.store", "WARNING"), mock.patch("radiolack.store.os.replace", side_effect=full):
            engine.configure_demod({"mode": "AM"})
            state = await asyncio.wait_for(storage_state(), timeout=5)
        self.assertEqual((state["saving"], state["problem"]),
                         (False, "settings.json cannot be saved: No space left on device."))
        engine.configure_demod({"mode": "CW"})
        state = await asyncio.wait_for(storage_state(), timeout=5)
        self.assertEqual((state["saving"], state["problem"]), (True, ""))

    async def test_bookmark_changes_reach_every_client(self):
        engine = self.engine()
        subscriptions = [engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            waiting(subscription)
        engine.edit_bookmarks({"action": "add", **CALLING})
        for subscription in subscriptions:
            self.assertEqual(subscription.get_nowait(),
                             {"type": "bookmarks", "bookmarks": [{"id": 1, **CALLING}]})
        with self.assertRaisesRegex(ValueError, "no such bookmark"):
            engine.edit_bookmarks({"action": "remove", "id": 2})
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription), [])

    async def test_preferences_go_to_the_next_clients_only(self):
        engine = self.engine()
        subscription = engine.subscribe()
        waiting(subscription)
        engine.set_preferences({"volume": 30})
        self.assertEqual(waiting(subscription), [])
        later = engine.subscribe()
        self.assertEqual(waiting(later)[STATES.index("preferences")], {"type": "preferences", "values": {"volume": 30}})
        for values in ([1], {"volume": [30]}, {"volume": float("inf")}, {"big": 2 ** 60},
                       {f"name {n}": n for n in range(101)}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "preference"):
                engine.set_preferences(values)
        self.assertEqual(engine.preferences_state()["values"], {"volume": 30})



def update_states(subscription: Subscription) -> list[dict]:
    """The update states ready now, without the other messages."""
    return [message for message in waiting(subscription)
            if isinstance(message, dict) and message["type"] == "update"]


class UpdateTest(unittest.IsolatedAsyncioTestCase):
    """The engine with a stand-in update check, and a store in a temporary
    folder."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "radiolack")

    def engine(self, updates: FakeUpdates | None, run: bool = True) -> Engine:
        engine = Engine(FakeSource(), store=self.store, updates=updates, save_delay_s=0.01)
        task = asyncio.ensure_future(engine.run()) if run else None

        async def stop():
            engine.close()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)
        self.addAsyncCleanup(stop)
        return engine

    async def settled(self, engine: Engine) -> dict:
        """The update state once no check is running."""
        deadline = time.monotonic() + 5
        while (state := engine.update_state())["status"] in ("idle", "checking"):
            self.assertLess(time.monotonic(), deadline, f"the check did not end: {state}")
            await asyncio.sleep(0.01)
        return state

    async def test_checks_at_start_and_every_client_learns_what_it_found(self):
        updates = FakeUpdates(UpdateResult("available", latest="9.9.9"))
        updates.release.clear()
        engine = self.engine(updates, run=False)
        subscription = engine.subscribe()
        self.assertEqual(update_states(subscription), [{
            "type": "update", "status": "idle", "version": __version__, "latest": None, "detail": "",
            "check_at_start": True}])
        task = asyncio.ensure_future(engine.run())
        self.addAsyncCleanup(asyncio.gather, task, return_exceptions=True)
        while updates.checks == 0:
            await asyncio.sleep(0.01)
        self.assertEqual([state["status"] for state in update_states(subscription)], ["checking"])
        updates.release.set()
        state = await self.settled(engine)
        self.assertEqual((state["status"], state["latest"], state["detail"]), ("available", "9.9.9", ""))
        self.assertEqual(update_states(subscription)[-1], state)
        self.assertEqual(updates.checks, 1)

    async def test_a_check_at_start_turned_off_waits_for_a_request(self):
        self.store.save(SETTINGS_FILE, {"updates": {"check_at_start": False}})
        updates = FakeUpdates()
        engine = self.engine(updates)
        await asyncio.sleep(0.1)
        self.assertEqual((updates.checks, engine.update_state()["status"]), (0, "idle"))
        self.assertFalse(engine.update_state()["check_at_start"])
        engine.configure_updates({"check": True})
        self.assertEqual((await self.settled(engine))["status"], "current")
        self.assertEqual(updates.checks, 1)

    async def test_one_check_at_a_time(self):
        updates = FakeUpdates()
        updates.release.clear()
        engine = self.engine(updates)
        while updates.checks == 0:
            await asyncio.sleep(0.01)
        engine.configure_updates({"check": True})
        engine.check_updates()
        await asyncio.sleep(0.05)
        self.assertEqual((updates.checks, engine.update_state()["status"]), (1, "checking"))
        updates.release.set()
        await self.settled(engine)
        engine.check_updates()
        self.assertEqual((await self.settled(engine))["status"], "current")
        self.assertEqual(updates.checks, 2)

    async def test_the_check_at_start_setting_reaches_every_client_and_is_saved(self):
        engine = self.engine(FakeUpdates())
        await self.settled(engine)
        subscriptions = [engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            waiting(subscription)
        engine.configure_updates({"check_at_start": False})
        for subscription in subscriptions:
            self.assertEqual([state["check_at_start"] for state in update_states(subscription)], [False])
        deadline = time.monotonic() + 5
        while self.store.load(SETTINGS_FILE).get("updates") != {"check_at_start": False}:
            self.assertLess(time.monotonic(), deadline, "the setting was not saved")
            await asyncio.sleep(0.01)
        updates = FakeUpdates()
        again = self.engine(updates)
        await asyncio.sleep(0.05)
        self.assertEqual((again.update_state()["check_at_start"], updates.checks), (False, 0))

    async def test_refused_settings_change_nothing(self):
        updates = FakeUpdates()
        engine = self.engine(updates, run=False)
        for changes in ({"later": True}, {"check": "yes"}, {"check_at_start": 0}, {"check": True, "check_at_start": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_updates(changes)
        await asyncio.sleep(0.05)
        self.assertEqual((updates.checks, engine.update_state()["check_at_start"]), (0, True))

    async def test_a_saved_setting_that_is_not_valid_keeps_the_check_at_start(self):
        for saved in ({"check_at_start": "no"}, [False]):
            with self.subTest(saved=saved):
                self.store.save(SETTINGS_FILE, {"updates": saved})
                with self.assertLogs("radiolack.engine", "WARNING"):
                    engine = self.engine(FakeUpdates(), run=False)
                self.assertTrue(engine.update_state()["check_at_start"])

    async def test_an_unavailable_check_says_why_and_does_not_run_at_start(self):
        updates = FakeUpdates(reason="Radiolack's APT source is missing.")
        engine = self.engine(updates)
        await asyncio.sleep(0.05)
        self.assertEqual(updates.checks, 0)
        state = engine.update_state()
        self.assertEqual((state["status"], state["detail"]), ("unavailable", "Radiolack's APT source is missing."))

    async def test_without_an_update_check_nothing_checks(self):
        engine = self.engine(None)
        engine.configure_updates({"check": True})
        await asyncio.sleep(0.05)
        self.assertEqual(engine.update_state()["status"], "unavailable")

    async def test_a_check_that_raises_fails_and_is_logged(self):
        engine = self.engine(FakeUpdates(RuntimeError("a bug")), run=False)
        with self.assertLogs("radiolack.engine", "ERROR"):
            engine.check_updates()
            state = await self.settled(engine)
        self.assertEqual((state["status"], state["detail"]), ("failed", "The check stopped on an error."))

    async def test_a_failed_check_is_logged(self):
        engine = self.engine(FakeUpdates(UpdateResult("failed", detail="No answer within 30 s.")), run=False)
        with self.assertLogs("radiolack.engine", "WARNING") as logs:
            engine.check_updates()
            await self.settled(engine)
        self.assertIn("No answer within 30 s.", logs.output[0])

    async def test_closing_stops_a_running_check(self):
        updates = FakeUpdates()
        updates.release.clear()
        engine = self.engine(updates)
        while updates.checks == 0:
            await asyncio.sleep(0.01)
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)
        self.assertTrue(updates.cancelled)
        engine.check_updates()      # closed: nothing starts
        self.assertEqual(updates.checks, 1)


class RemoteClientTest(unittest.IsolatedAsyncioTestCase):
    """The devices that remote access serves share the radio, not this
    computer's own states and settings, and get lighter frames."""

    async def test_a_remote_client_gets_neither_storage_updates_nor_remote_access(self):
        access = RemoteAccess()
        engine = Engine(FakeSource(), remote=access)
        self.addCleanup(engine.close)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        self.assertEqual([message["type"] for message in ready_messages(local)], [*STATES, "remote"])
        self.assertEqual([message["type"] for message in ready_messages(remote)],
                         list(STATES[:-2]))
        access.set_port(9000)
        self.assertEqual(ready_messages(local), [{"type": "remote", **access.describe()}])
        self.assertEqual(ready_messages(remote), [])
        engine.configure({"dc_removal": False})     # the radio's states reach both
        self.assertEqual([message["type"] for message in ready_messages(remote)], ["source"])
        self.assertEqual([message["type"] for message in ready_messages(local)], ["source"])

    async def test_a_remote_client_gets_one_frame_for_two_blocks_in_fewer_bins(self):
        source = AlternatingSource()
        engine = Engine(source, queue_size=64)
        task = asyncio.ensure_future(engine.run())
        self.addAsyncCleanup(stop, engine, task)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        while source.reads < 9:
            await asyncio.sleep(0.01)
        # Both clients' frames at once: each block reached the local client.
        local_frames, remote_frames = ([message for message in waiting(subscription) if isinstance(message, bytes)]
                                       for subscription in (local, remote))
        self.assertGreaterEqual(len(local_frames), 8)
        self.assertEqual({len(frame) - FRAME_HEADER.size for frame in local_frames}, {8192})
        self.assertEqual(len(remote_frames), len(local_frames) // 2)
        for index, frame in enumerate(remote_frames):
            _, _, _, sequence, centre, rate, *_ = FRAME_HEADER.unpack_from(frame)
            self.assertEqual((sequence, centre, rate), (index, FAKE_CENTRE_HZ, FAKE_RATE_HZ))
            codes = np.frombuffer(frame, np.uint8, offset=FRAME_HEADER.size)
            self.assertEqual(len(codes), 2048)
            self.assertEqual(int(np.argmax(codes)), FAKE_TONE_BIN // 4)
            # The mean power of -20 and -30 dBFS: neither the louder block nor a mean in dB.
            self.assertAlmostEqual(level_db(frame, FAKE_TONE_BIN // 4), 10 * math.log10((1e-2 + 1e-3) / 2),
                                   delta=0.4)

    async def test_a_device_starts_from_this_computers_preferences_until_it_gives_its_own(self):
        access = RemoteAccess()
        first, second = (access.session(access.pair("Device")).id for _ in range(2))
        engine = Engine(FakeSource(), remote=access)
        self.addCleanup(engine.close)
        engine.set_preferences({"palette": "ice", "volume": 70})
        opened = engine.subscribe(device=first)
        self.assertEqual(preferences_of(opened), {"palette": "ice", "volume": 70})
        engine.set_preferences({"palette": "gray", "volume": 10}, device=first)
        self.assertEqual(waiting(opened), [])      # open interfaces keep their own
        for device, values in ((first, {"palette": "gray", "volume": 10}), (second, {"palette": "ice", "volume": 70}),
                               (None, {"palette": "ice", "volume": 70})):
            with self.subTest(device=device):
                self.assertEqual(preferences_of(engine.subscribe(device=device)), values)
        # A change on this computer reaches the devices that gave none.
        engine.set_preferences({"palette": "classic"})
        self.assertEqual(preferences_of(engine.subscribe(device=second)), {"palette": "classic"})
        self.assertEqual(preferences_of(engine.subscribe(device=first)), {"palette": "gray", "volume": 10})
        for values in ([1], {"volume": [30]}, {f"name {n}": n for n in range(101)}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "preference"):
                engine.set_preferences(values, device=first)
        self.assertEqual(engine.preferences_state(first)["values"], {"palette": "gray", "volume": 10})

    async def test_a_forgotten_device_keeps_no_preferences(self):
        access = RemoteAccess()
        device = access.session(access.pair("Device")).id
        engine = Engine(FakeSource(), remote=access)
        self.addCleanup(engine.close)
        engine.set_preferences({"volume": 10}, device=device)
        access.forget(device)
        self.assertEqual(engine.preferences_state(device)["values"], {})
        with self.assertRaisesRegex(ValueError, "no longer paired"):
            engine.set_preferences({"volume": 20}, device=device)
        without_remote_access = Engine(FakeSource())
        self.addCleanup(without_remote_access.close)
        with self.assertRaisesRegex(ValueError, "no longer paired"):
            without_remote_access.set_preferences({"volume": 20}, device=device)
        self.assertEqual(without_remote_access.preferences_state(device)["values"], {})

    async def test_a_remote_client_may_not_change_the_rtl_tcp_address(self):
        engine = Engine(RtlTcpSource(LOOPBACK, free_port()), remote=RemoteAccess())   # never connects
        self.addCleanup(engine.close)
        self.assertEqual(set(engine.state()["settable"]) - set(engine.state(remote=True)["settable"]), {"address"})
        self.assertEqual(engine.state(remote=True)["address"], engine.state()["address"])


if __name__ == "__main__":
    unittest.main()


async def next_peaks(subscription: Subscription, until=lambda peaks: True) -> list[dict]:
    """The signals found, from the next peaks message whose list satisfies ``until``."""
    while True:
        message = await next_message(subscription)
        if isinstance(message, dict) and message["type"] == "peaks" and until(message["peaks"]):
            return message["peaks"]


def kinds_of(messages: list) -> list[str]:
    """The types of the JSON messages among ``messages``."""
    return [message["type"] for message in messages if isinstance(message, dict)]


class ScannerTest(unittest.IsolatedAsyncioTestCase):
    """Detection in the engine: the signals found and the scanner settings,
    the same for every client, and saved."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"

    def engine(self, source: Source | None = None, run: bool = True, **options) -> Engine:
        engine = Engine(source if source is not None else FakeSource(), **options)
        task = asyncio.ensure_future(engine.run()) if run else None

        async def stop():
            engine.close()
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    async def test_every_client_receives_the_signals_found(self):
        engine = self.engine()
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        for subscription in (local, remote):
            [found] = await next_peaks(subscription, until=bool)
            self.assertAlmostEqual(found["frequency_hz"], FAKE_TONE_HZ, delta=FAKE_RATE_HZ / 8192)
            self.assertEqual((found["active"], found["age_s"]), (True, 0.0))
            self.assertGreater(found["margin_db"], 12.0)

    async def test_scanner_changes_reach_every_client_and_are_checked(self):
        engine = self.engine(run=False)
        subscriptions = [engine.subscribe(), engine.subscribe(device="5f0c2a91")]
        for subscription in subscriptions:
            waiting(subscription)
        engine.configure_scanner({"threshold_db": 20, "detection": True, "resume_s": 0})
        engine.configure_scanner({"sweep_high_hz": 1300e6, "sweep_window_s": 2})
        engine.configure_scanner({"sweep_low_hz": 1240e6})
        changed = scanner_state(threshold_db=20.0, resume_s=0.0, sweep_low_hz=1240e6, sweep_high_hz=1300e6,
                                sweep_window_s=2.0)
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription)[-1], changed)
        refused = ({}, {"threshold": 20}, {"detection": "on"}, {"detection": None}, {"threshold_db": 5},
                   {"threshold_db": 31}, {"threshold_db": "12"}, {"threshold_db": float("nan")},
                   {"resume_s": -0.5}, {"resume_s": 8.5}, {"resume_s": True}, {"resume_s": "2"},
                   {"sweep_low_hz": 1300e6}, {"sweep_high_hz": 1200e6}, {"sweep_low_hz": 0},
                   {"sweep_high_hz": "1300 MHz"}, {"sweep_low_hz": None}, {"sweep_window_s": 0.2},
                   {"sweep_window_s": 2.5}, {"sweep_window_s": float("inf")}, {"sweep_window_s": False},
                   {"sweep_low_hz": 1250e6, "threshold_db": 40},
                   {"ignore": 145e6, "restore": True}, {"ignore": 145e6, "detection": False},
                   {"ignore": "145 MHz"}, {"ignore": 0}, {"restore": False}, {"restore": 1},
                   {"scan": "panorama"}, {"scan": True}, {"scan": "memory", "resume_s": 2},
                   {"scan": "memory"},      # no ticked bookmark in the span
                   {"scan": "sweep"})       # a source that does not tune
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_scanner(changes)
        self.assertEqual(engine.scanner_state(), changed)
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription), [])

    async def test_an_ignored_frequency_leaves_the_list_at_once_and_stays_out(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        [found] = await next_peaks(subscription, until=bool)
        engine.configure_scanner({"ignore": found["frequency_hz"] + 500})
        self.assertEqual(kinds_of(waiting(subscription)), ["scanner", "peaks"])
        self.assertEqual(engine.scanner_state()["ignored_hz"], [found["frequency_hz"] + 500])
        self.assertEqual(engine.peaks_state()["peaks"], [])
        reads = source.reads
        while source.reads < reads + 30:    # three searches
            await asyncio.sleep(0.01)
        self.assertEqual(engine.peaks_state()["peaks"], [])
        engine.configure_scanner({"restore": True})
        self.assertEqual(kinds_of(waiting(subscription)), ["scanner"])     # the list is the same, until found again
        await next_peaks(subscription, until=bool)

    async def test_detection_turned_off_empties_the_list_and_finds_nothing(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        await next_peaks(subscription, until=bool)
        engine.configure_scanner({"detection": False})
        self.assertEqual(waiting(subscription)[:2], [scanner_state(detection=False), {"type": "peaks", "peaks": []}])
        reads = source.reads
        while source.reads < reads + 30:
            await asyncio.sleep(0.01)
        self.assertNotIn("peaks", kinds_of(waiting(subscription)))
        engine.configure_scanner({"detection": True})
        await next_peaks(subscription, until=bool)

    async def test_another_receiver_starts_an_empty_list(self):
        engine = self.engine(sources=FakeSources())
        subscription = engine.subscribe()
        await next_peaks(subscription, until=bool)
        engine.configure({"kind": "other"})
        self.assertEqual(engine.peaks_state()["peaks"], [])
        self.assertIn("peaks", kinds_of(waiting(subscription)))
        [found] = await next_peaks(subscription, until=bool)    # the other source's tone
        self.assertAlmostEqual(found["frequency_hz"], FAKE_TONE_HZ + 1e6, delta=FAKE_RATE_HZ / 8192)

    async def test_the_list_goes_out_at_most_once_an_interval(self):
        engine = self.engine(run=False)
        subscription = engine.subscribe()
        waiting(subscription)
        with mock.patch("radiolack.engine.PEAKS_INTERVAL_S", 0.2):
            for _ in range(3):
                engine._peaks_changed()
            self.assertEqual(kinds_of(waiting(subscription)), ["peaks"])
            started = time.monotonic()
            self.assertEqual((await next_message(subscription))["type"], "peaks")     # the next two, together
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
            engine._peaks_changed(at_once=True)      # after an interface's change
            self.assertEqual(kinds_of(waiting(subscription)), ["peaks"])
            engine._peaks_changed()
            engine.close()
            self.assertIsNone(engine._peaks_timer)

    async def test_the_scanner_settings_are_saved_with_the_others(self):
        engine = self.engine(run=False, store=Store(self.directory), save_delay_s=0.01)
        engine.configure_scanner({"detection": False, "threshold_db": 18.5, "resume_s": 4.5})
        engine.configure_scanner({"sweep_low_hz": 118e6, "sweep_high_hz": 137_000_000.4, "sweep_window_s": 1.25})
        engine.configure_scanner({"ignore": 145_500_000.4})
        engine.configure_scanner({"ignore": 144_800_000})
        engine.close()
        await engine.wait_closed()
        saved = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["scanner"], {"detection": False, "threshold_db": 18.5, "resume_s": 4.5,
                                            "sweep_low_hz": 118e6, "sweep_high_hz": 137e6, "sweep_window_s": 1.25,
                                            "ignored_hz": [144_800_000.0, 145_500_000.0]})
        again = self.engine(run=False, store=Store(self.directory))
        self.assertEqual(again.scanner_state(), scanner_state(
            detection=False, threshold_db=18.5, resume_s=4.5, sweep_low_hz=118e6, sweep_high_hz=137e6,
            sweep_window_s=1.25, ignored_hz=[144_800_000.0, 145_500_000.0]))

    async def test_saved_scanner_settings_that_are_not_valid_keep_their_defaults(self):
        self.directory.mkdir()
        settings = self.directory / SETTINGS_FILE
        for saved, expected in (
                ({"detection": "yes", "threshold_db": 99, "resume_s": -1, "sweep_low_hz": 440e6,
                  "sweep_high_hz": 430e6, "sweep_window_s": 0.1,
                  "ignored_hz": [145e6, "x", 0, 10 ** 400, 145e6 + 100, 144e6]},
                 (True, 12.0, [144e6, 145e6])),
                ({"sweep_low_hz": 118e6, "sweep_window_s": True, "ignored_hz": {"145e6": True}}, (True, 12.0, [])),
                ([1], (True, 12.0, [])),
                ({"ignored_hz": [144e6 + n * 10_000 for n in range(250)]}, (True, 12.0, None))):
            with self.subTest(saved=saved):
                settings.write_text(json.dumps({"version": 1, "scanner": saved}), encoding="utf-8")
                with self.assertLogs("radiolack.engine", "WARNING"):
                    engine = Engine(FakeSource(), store=Store(self.directory))
                state = engine.scanner_state()
                engine.close()
                detection, threshold_db, ignored = expected
                self.assertEqual((state["detection"], state["threshold_db"], state["resume_s"], state["sweep_low_hz"],
                                  state["sweep_high_hz"], state["sweep_window_s"]),
                                 (detection, threshold_db, 2.0, 430e6, 440e6, 0.5))
                if ignored is None:
                    self.assertEqual(state["ignored_hz"], [144e6 + n * 10_000 for n in range(200)])
                else:
                    self.assertEqual(state["ignored_hz"], ignored)


class TonesSource(FakeSource):
    """A fake source with a tone at each offset of ``tones`` from the centre
    frequency, in Hz, with its amplitude, over white noise of -60 dBFS; the
    tests change the tones as it runs. Its centre frequency may change."""

    def __init__(self, tones: dict[float, float]) -> None:
        super().__init__()
        self.tones = dict(tones)
        self._rng = np.random.default_rng(1)

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.005)
        self.reads += 1
        t = np.arange(count) / FAKE_RATE_HZ
        samples = (self._rng.normal(size=count) + 1j * self._rng.normal(size=count)) * math.sqrt(1e-6 / 2)
        for offset_hz, amplitude in self.tones.items():
            samples = samples + amplitude * np.exp(2j * np.pi * offset_hz * t)
        return samples.astype(np.complex64)

    def configure(self, changes) -> None:
        if set(changes) != {"centre_hz"}:
            raise ValueError("only the centre frequency changes")
        self.centre_hz = float(changes["centre_hz"])


async def next_scanner(subscription: Subscription, until=lambda state: True) -> dict:
    """The next scanner state that satisfies ``until``."""
    while True:
        message = await next_message(subscription)
        if isinstance(message, dict) and message["type"] == "scanner" and until(message):
            return message


def in_state(state: str):
    """Whether a scanner state shows the memory scan in ``state``."""
    return lambda message: message["memory"]["state"] == state


class MemoryScanTest(unittest.IsolatedAsyncioTestCase):
    """The memory scan in the engine: it tunes the VFO to the active ticked
    bookmarks of the span, for every client, until stopped, and goes on
    without any client."""

    def engine(self, source: Source | None = None, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    @staticmethod
    def bookmarks(engine: Engine, entries) -> list[int]:
        """Add a bookmark for each (offset from the centre frequency, mode,
        ticked): their ids."""
        for offset_hz, mode, scan in entries:
            engine.edit_bookmarks({"action": "add", "frequency_hz": FAKE_CENTRE_HZ + offset_hz,
                                   "name": f"{offset_hz:+g} Hz", "mode": mode, "scan": scan})
        ids = {entry["frequency_hz"]: entry["id"] for entry in engine.bookmarks_state()["bookmarks"]}
        return [ids[FAKE_CENTRE_HZ + offset_hz] for offset_hz, _, _ in entries]

    async def test_tunes_every_client_to_the_strongest_active_bookmark(self):
        engine = self.engine(TonesSource({40_000: 0.1, -60_000: 0.03}))
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        strong, weak, quiet, outside, unticked = self.bookmarks(engine, [
            (40_000, "NFM", True), (-60_000, "AM", True), (70_000, "NFM", True), (110_000, "NFM", True),
            (50_000, "NFM", False)])
        self.assertEqual(engine.scanner_state()["memory"], {"watched": [weak, strong, quiet], "windows": 1,
                                                            "window": None, "state": None, "bookmark_id": None})
        engine.configure_scanner({"scan": "memory"})
        for subscription in (local, remote):
            state = await next_scanner(subscription, until=in_state("listening"))
            self.assertEqual((state["scan"], state["stopped"], state["memory"]["bookmark_id"]),
                             ("memory", None, strong))
        demod = engine.demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ + 40_000, "NFM", 10_000.0))
        self.assertNotIn("scan", engine._contents()[SETTINGS_FILE]["scanner"])     # never saved

    async def test_waits_the_resume_time_then_watches_again(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(source)
        subscription = engine.subscribe()
        first, second = self.bookmarks(engine, [(40_000, "NFM", True), (-60_000, "AM", True)])
        engine.configure_scanner({"resume_s": 0.5})
        engine.configure_scanner({"scan": "memory"})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], first)
        source.tones = {}
        await next_scanner(subscription, until=in_state("waiting"))
        started = time.monotonic()
        await next_scanner(subscription, until=in_state("watching"))
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        source.tones = {-60_000: 0.03}
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], second)
        demod = engine.demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ - 60_000, "AM", 8_000.0))

    async def test_a_manual_squelch_decides_which_bookmarks_are_active(self):
        engine = self.engine(TonesSource({-60_000: 0.03}))     # about -30 dBFS
        subscription = engine.subscribe()
        [bookmark] = self.bookmarks(engine, [(-60_000, "AM", True)])
        engine.configure_demod({"squelch_auto": False, "squelch_db": -25.0})
        engine.configure_scanner({"scan": "memory"})
        await asyncio.sleep(0.5)
        self.assertEqual(engine.scanner_state()["memory"]["state"], "watching")
        engine.configure_demod({"squelch_db": -35.0})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], bookmark)

    async def test_tuning_by_hand_another_source_or_no_bookmark_left_stops_it(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()
        [bookmark] = self.bookmarks(engine, [(40_000, "NFM", True)])

        async def listening() -> None:
            engine.configure_scanner({"scan": "memory"})
            await next_scanner(subscription, until=in_state("listening"))

        await listening()
        engine.configure_demod({"mode": "AM", "bandwidth_hz": 6_000, "squelch_db": -70})
        engine.configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 40_000})     # where it is
        self.assertEqual(engine.scanner_state()["scan"], "memory")
        stops = (
            (lambda: engine.configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 45_000}), "tuned"),
            (lambda: engine.configure({"centre_hz": FAKE_CENTRE_HZ + 1_000}), "tuned"),
            (lambda: engine.edit_bookmarks({"action": "change", "id": bookmark, "scan": False}), "bookmarks"),
            (lambda: engine.configure({"kind": "other"}), "source"),
        )
        for stop, reason in stops:
            with self.subTest(reason=reason):
                if engine.scanner_state()["memory"]["watched"]:
                    await listening()
                # The output tells of a stop for want of bookmarks.
                logs = (self.assertLogs("radiolack.engine", "WARNING") if reason == "bookmarks"
                        else contextlib.nullcontext())
                with logs:
                    stop()
                state = engine.scanner_state()
                self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"], state["memory"]["state"]),
                                 (None, reason, "memory", None))
                if reason == "bookmarks":
                    self.assertEqual(state["memory"]["watched"], [])
                    engine.edit_bookmarks({"action": "change", "id": bookmark, "scan": True})
                    engine.configure({"centre_hz": FAKE_CENTRE_HZ})

    async def test_goes_on_when_the_span_stays_or_another_source_does_not_open(self):
        source = TonesSource({40_000: 0.1})

        def unavailable():
            raise ValueError("no such receiver")

        engine = self.engine(sources=FakeSources(test=lambda: source, other=unavailable))
        subscription = engine.subscribe()
        self.bookmarks(engine, [(40_000, "NFM", True)])
        engine.configure_scanner({"scan": "memory"})
        await next_scanner(subscription, until=in_state("listening"))
        engine.configure({"centre_hz": FAKE_CENTRE_HZ})       # where it is
        with self.assertLogs("radiolack.engine", "WARNING"), self.assertRaises(ValueError):
            engine.configure({"kind": "other"})
        self.assertEqual(engine.scanner_state()["scan"], "memory")

    async def test_goes_on_without_any_client_until_stopped(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(source)
        subscription = engine.subscribe()
        self.bookmarks(engine, [(40_000, "NFM", True)])
        engine.configure_scanner({"scan": "memory"})
        engine.unsubscribe(subscription)
        reads = source.reads
        while source.reads < reads + 20:
            await asyncio.sleep(0.01)
        engine.configure_scanner({"scan": None})
        self.assertEqual((engine.scanner_state()["scan"], engine.scanner_state()["stopped"]), (None, None))
        await asyncio.sleep(0.05)
        reads = source.reads
        await asyncio.sleep(0.1)
        self.assertLessEqual(source.reads, reads + 1)


STATION_HZ = 250_000.0      # WideSource's station, from its centre frequency


class WideSource(FakeSource):
    """A fake source fast enough for WFM's 320 kHz channel, with a station
    STATION_HZ above its centre frequency that fills 150 kHz, 20 dB above
    white noise of -60 dBFS, as broadcast FM does."""

    sample_rate_hz = 4 * FAKE_RATE_HZ

    def __init__(self) -> None:
        super().__init__()
        self._rng = np.random.default_rng(6)

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.005)
        self.reads += 1
        noise = (self._rng.normal(size=count) + 1j * self._rng.normal(size=count)) * math.sqrt(1e-6 / 2)
        station = wide_signal(count, self.sample_rate_hz, STATION_HZ, 100 * 1e-6 / self.sample_rate_hz, self._rng)
        return (noise + station).astype(np.complex64)


class WfmTest(unittest.IsolatedAsyncioTestCase):
    """WFM in the engine: the automatic squelch and the memory scan read the
    noise beyond a WFM channel, which a station fills."""

    def engine(self) -> Engine:
        engine = Engine(WideSource())
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    async def test_the_automatic_squelch_opens_on_a_station_and_not_on_the_noise(self):
        engine = self.engine()
        subscription = engine.subscribe()
        # 8 dB above the noise in the 180 kHz channel.
        threshold = 10 * math.log10(1e-6 * 180_000 / WideSource.sample_rate_hz) + 8.0
        for offset, station in ((STATION_HZ, True), (-STATION_HZ, False)):
            with self.subTest(offset=offset):
                engine.configure_demod({"frequency_hz": FAKE_CENTRE_HZ + offset, "mode": "WFM"})
                for _ in range(30):     # frames computed before the change come first
                    *_, level, squelch = FRAME_HEADER.unpack_from(await next_frame(subscription))
                    if abs(squelch - threshold) < 1.0 and (level > threshold) == station:
                        break
                _, _, flags, *_, level, squelch = FRAME_HEADER.unpack_from(await next_frame(subscription))
                self.assertAlmostEqual(squelch, threshold, delta=1.0)
                self.assertEqual(level > threshold + 10.0, station)
                self.assertEqual(bool(flags & FLAG_SQUELCH_OPEN), station)

    async def test_the_memory_scan_stops_on_a_wfm_bookmark_with_a_station(self):
        engine = self.engine()
        subscription = engine.subscribe()
        station, _ = MemoryScanTest.bookmarks(engine, [(STATION_HZ, "WFM", True), (-STATION_HZ, "WFM", True)])
        engine.configure_scanner({"scan": "memory"})
        state = await asyncio.wait_for(next_scanner(subscription, until=in_state("listening")), timeout=10)
        self.assertEqual(state["memory"]["bookmark_id"], station)
        demod = engine.demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ + STATION_HZ, "WFM", 180_000.0))


SWEEP_HZ = (145_000_000.0, 145_200_000.0)
SWEEP_WINDOWS = sweep_windows(*SWEEP_HZ, FAKE_RATE_HZ)     # four windows
HOME_HZ = 146_000_000.0         # where the receiver is when a sweep starts
HOME_VFO_HZ = 146_050_000.0


class TuningSource(FakeSource):
    """A fake receiver that tunes at once, from ``range_hz[0]`` to
    ``range_hz[1]``: carriers at fixed frequencies, each with its amplitude,
    over white noise of -60 dBFS. ``tuned`` lists the centre frequencies
    asked of it, in order."""

    def __init__(self, carriers: dict[float, float], centre_hz: float = HOME_HZ,
                 range_hz: tuple[float, float] = (24e6, 1766e6)) -> None:
        super().__init__()
        self.carriers = dict(carriers)
        self.centre_hz = centre_hz
        self.range_hz = range_hz
        self.tuned: list[float] = []
        self._rng = np.random.default_rng(3)

    def describe(self) -> dict:
        return {**super().describe(), "range_hz": list(self.range_hz), "settable": ["centre_hz"]}

    def configure(self, changes) -> None:
        if set(changes) != {"centre_hz"}:
            raise ValueError("only the centre frequency changes")
        centre = float(round(changes["centre_hz"]))
        low, high = self.range_hz
        if not low <= centre <= high:
            raise ValueError(f"the receiver tunes from {low / 1e6:g} to {high / 1e6:g} MHz")
        self.centre_hz = centre
        self.tuned.append(centre)
        if self.on_change is not None:
            self.on_change()

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.005)
        self.reads += 1
        t = np.arange(count) / FAKE_RATE_HZ
        samples = (self._rng.normal(size=count) + 1j * self._rng.normal(size=count)) * math.sqrt(1e-6 / 2)
        for frequency_hz, amplitude in self.carriers.items():
            offset_hz = frequency_hz - self.centre_hz
            if abs(offset_hz) < FAKE_RATE_HZ / 2:
                samples = samples + amplitude * np.exp(2j * np.pi * offset_hz * t)
        return samples.astype(np.complex64)


async def eventually(condition, timeout: float = 10.0) -> None:
    """Return once ``condition()`` holds, checking every 10 ms."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)


def found_all(peaks: list[dict], frequencies) -> bool:
    """Whether ``peaks`` holds a signal within 200 Hz of each frequency."""
    return all(any(abs(peak["frequency_hz"] - frequency) <= 200 for peak in peaks) for frequency in frequencies)


def at_window(index: int):
    """Whether a scanner state shows the sweep at window ``index``."""
    return lambda message: message["sweep"]["window"] == index


class SweepTest(unittest.IsolatedAsyncioTestCase):
    """The sweep in the engine: it steps a receiver that tunes across a range,
    window after window, lists what it finds there, and brings the receiver
    and the VFO back once a client stops it; tuning by hand or another
    source stops it where it is. It goes on without any client, and one
    scan runs at a time."""

    # One carrier near each end, and one on the centre of the second
    # window, where detection does not look: its neighbours find it.
    CARRIERS = {SWEEP_HZ[0] + 10_000: 0.01, SWEEP_WINDOWS.centre(1): 0.01, SWEEP_HZ[1] - 10_000: 0.01}

    def engine(self, source: Source | None = None, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        engine.configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1],
                                  "sweep_window_s": 0.25})
        engine.configure_demod({"frequency_hz": HOME_VFO_HZ})
        return engine

    async def test_steps_across_the_range_lists_what_it_finds_and_comes_back_once_stopped(self):
        source = TuningSource(self.CARRIERS)
        engine = self.engine(source)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        engine.configure_scanner({"scan": "sweep"})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["sweep"]),
                         ("sweep", None, {"windows": 4, "window": 0, "passes": 0}))
        self.assertEqual(source.centre_hz, SWEEP_WINDOWS.centre(0))
        for subscription in (local, remote):
            await next_scanner(subscription, until=lambda message: message["sweep"]["passes"] >= 1)
            await asyncio.wait_for(next_peaks(subscription, until=lambda peaks: found_all(peaks, self.CARRIERS)), 10)
        centres = [SWEEP_WINDOWS.centre(index) for index in range(4)]
        self.assertEqual(source.tuned[:5], centres + centres[:1])
        # Saved meanwhile, the receiver's settings are those it comes back to.
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))
        self.assertNotIn("scan", engine._contents()[SETTINGS_FILE]["scanner"])     # never saved
        engine.configure_scanner({"scan": None})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"], state["sweep"]),
                         (None, None, None, {"windows": None, "window": None, "passes": None}))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_tuning_by_hand_or_another_source_stops_it_where_it_is(self):
        source = TuningSource(self.CARRIERS)
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()

        async def sweeping() -> None:
            engine.configure_scanner({"scan": "sweep"})
            await next_scanner(subscription, until=at_window(2))

        await sweeping()
        engine.configure_demod({"mode": "AM", "bandwidth_hz": 6_000, "squelch_db": -70, "squelch_auto": False})
        engine.configure_demod({"frequency_hz": engine.demod_state()["frequency_hz"]})     # where it is
        self.assertEqual(engine.scanner_state()["scan"], "sweep")
        vfo = engine.demod_state()["frequency_hz"] + 1_000
        window = source.centre_hz
        engine.configure_demod({"frequency_hz": vfo})
        self.assertEqual((engine.scanner_state()["stopped"], engine.scanner_state()["stopped_scan"]),
                         ("tuned", "sweep"))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (window, vfo))
        await sweeping()
        engine.configure({"centre_hz": 145_500_000})
        self.assertEqual((engine.scanner_state()["stopped"], source.centre_hz), ("tuned", 145_500_000))
        home = (source.centre_hz, engine.demod_state()["frequency_hz"])
        await sweeping()
        tuned = len(source.tuned)
        engine.configure({"kind": "other"})
        self.assertEqual((engine.scanner_state()["scan"], engine.scanner_state()["stopped"]), (None, "source"))
        self.assertEqual(len(source.tuned), tuned)      # left where it was
        saved = engine._receivers["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), home)

    async def test_refuses_a_range_beyond_the_receiver_and_stops_once_it_refuses_a_window(self):
        source = TuningSource({}, centre_hz=145_050_000)
        engine = self.engine(source)
        # The VFO within the receiver's span, where the engine keeps it, but outside the second window's:
        # the sweep moves it, and the stop brings it back, where the receiver's next samples leave it.
        vfo_hz = 144_940_000.0
        self.assertTrue(abs(vfo_hz - source.centre_hz) <= FAKE_RATE_HZ / 2 < abs(vfo_hz - SWEEP_WINDOWS.centre(1)))
        engine.configure_demod({"frequency_hz": vfo_hz})
        subscription = engine.subscribe()
        engine.configure_scanner({"sweep_low_hz": 20e6, "sweep_high_hz": 30e6})
        with self.assertRaisesRegex(ValueError, "it tunes from 24 to 1766 MHz"):
            engine.configure_scanner({"scan": "sweep"})
        self.assertEqual((engine.scanner_state()["scan"], source.tuned), (None, []))
        engine.configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1]})
        engine.configure_scanner({"scan": "sweep"})
        with self.assertRaisesRegex(ValueError, "it tunes from 24 to 1766 MHz"):
            engine.configure_scanner({"sweep_low_hz": 20e6})
        self.assertEqual((engine.scanner_state()["scan"], engine.scanner_state()["sweep_low_hz"]),
                         ("sweep", SWEEP_HZ[0]))
        source.range_hz = (24e6, 145_100_000)       # it no longer tunes to the last two windows
        with self.assertLogs("radiolack.engine", "WARNING"):
            state = await next_scanner(subscription, until=lambda message: message["scan"] is None)
        self.assertEqual((state["stopped"], state["stopped_scan"], source.centre_hz), ("range", "sweep", 145_050_000))
        self.assertEqual(engine.demod_state()["frequency_hz"], vfo_hz)

    async def test_a_new_range_starts_it_again_from_its_first_window(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(2))
        engine.configure_scanner({"sweep_low_hz": 433e6, "sweep_high_hz": 434e6, "sweep_window_s": 2})
        windows = sweep_windows(433e6, 434e6, FAKE_RATE_HZ)
        self.assertEqual(engine.scanner_state()["sweep"], {"windows": windows.count, "window": 0, "passes": 0})
        self.assertEqual((source.centre_hz, engine._sweep.search_blocks), (windows.centre(0), 30))
        engine.configure_scanner({"scan": None})
        self.assertEqual(source.centre_hz, HOME_HZ)

    async def test_goes_on_without_any_client_until_stopped(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.configure_scanner({"scan": "sweep"})
        engine.unsubscribe(subscription)
        tuned = len(source.tuned)
        await eventually(lambda: len(source.tuned) >= tuned + 6)
        engine.configure_scanner({"scan": None})
        self.assertEqual(source.centre_hz, HOME_HZ)
        await asyncio.sleep(0.05)
        reads = source.reads
        await asyncio.sleep(0.1)
        self.assertLessEqual(source.reads, reads + 1)

    async def test_one_scan_at_a_time(self):
        source = TuningSource({HOME_HZ + 30_000: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(1))
        with self.assertRaisesRegex(ValueError, "no ticked bookmark"):
            engine.configure_scanner({"scan": "memory"})
        self.assertEqual(engine.scanner_state()["scan"], "sweep")
        engine.edit_bookmarks({"action": "add", "frequency_hz": HOME_HZ + 30_000, "name": "Home", "mode": "NFM",
                               "scan": True})
        [bookmark] = [entry["id"] for entry in engine.bookmarks_state()["bookmarks"]]
        engine.configure_scanner({"scan": "memory"})    # it watches the span the sweep comes back to
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["watched"], source.centre_hz),
                         ("memory", None, [bookmark], HOME_HZ))
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], bookmark)
        engine.configure_scanner({"scan": "sweep"})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["state"]), ("sweep", None, None))

    async def test_the_memory_scan_takes_its_windows_from_the_span_it_comes_back_to(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        bookmark_hz = SWEEP_WINDOWS.centre(2) + 30_000      # outside the span it comes back to
        engine.edit_bookmarks({"action": "add", "frequency_hz": bookmark_hz, "name": "Window", "mode": "NFM",
                               "scan": True})
        [bookmark] = [entry["id"] for entry in engine.bookmarks_state()["bookmarks"]]
        windows = memory_windows([bookmark_hz], FAKE_RATE_HZ, 24e6, 1766e6)
        self.assertEqual(engine._windows, windows)
        engine.configure_scanner({"scan": "sweep"})
        for window in (1, 2):
            state = await next_scanner(subscription, until=at_window(window))
            self.assertEqual((state["memory"]["watched"], state["memory"]["windows"]), ([bookmark], 1))
            self.assertEqual(engine._windows, windows)
        engine.configure_demod({"frequency_hz": source.centre_hz + 10_000})     # it stays in the window
        self.assertEqual(engine._windows, (SWEEP_WINDOWS.centre(2),))


class RtlTcpSweepTest(unittest.IsolatedAsyncioTestCase):
    """The sweep with an rtl_tcp receiver: the fake rtl_tcp's carriers at
    fixed frequencies, found across its windows."""

    async def test_finds_the_carriers_across_its_windows_and_tuning_by_hand_stops_it_at_once(self):
        low, high, rate = 145_000_000.0, 145_600_000.0, 1_024_000.0
        windows = sweep_windows(low, high, rate)    # three
        carriers = {low + 50_000: 0.2, windows.centre(1): 0.2, high - 50_000: 0.2}
        fake = FakeRtlTcp(carriers=carriers, chunk_bytes=16_384, interval_s=0.004)
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, fake.port, centre_hz=HOME_HZ, sample_rate_hz=rate, settle_s=0.1)
        engine = Engine(source)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        subscription = engine.subscribe()
        engine.configure_scanner({"sweep_low_hz": low, "sweep_high_hz": high, "sweep_window_s": 0.25})
        await eventually(lambda: engine.state()["status"] == "streaming")
        home = (engine.state()["centre_hz"], engine.demod_state()["frequency_hz"])
        engine.configure_scanner({"scan": "sweep"})
        await asyncio.wait_for(next_peaks(subscription, until=lambda peaks: found_all(peaks, carriers)), 30)
        engine.configure_scanner({"scan": None})
        self.assertEqual((engine.state()["centre_hz"], engine.demod_state()["frequency_hz"]), home)
        await eventually(lambda: fake.frequency_hz == HOME_HZ)
        engine.configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(1))
        engine.configure({"centre_hz": 145_350_000})    # before its samples come
        self.assertEqual((engine.scanner_state()["stopped"], engine.state()["centre_hz"]), ("tuned", 145_350_000))


NEAR_HZ = HOME_HZ + 30_000      # a bookmark in the span where the receiver is when a memory scan starts
FAR_HZ = HOME_HZ + 500_000      # and one outside it
TOUR_VISIT_S = 0.2              # each window's time, in these tests


def at_memory_window(index: int):
    """Whether a scanner state shows the memory scan at window ``index``."""
    return lambda message: message["memory"]["window"] == index


class MemoryScanWindowsTest(unittest.IsolatedAsyncioTestCase):
    """The memory scan with a receiver that tunes: the ticked bookmarks
    outside the span take as few windows as it can, which it visits in
    turn; once a client stops it, the receiver and the VFO come back,
    unless it stays on a channel. Those beyond the receiver's reach are left
    out, and with them all in the span, it never tunes the receiver."""

    WINDOWS = memory_windows([NEAR_HZ, FAR_HZ], FAKE_RATE_HZ, 24e6, 1766e6)

    def engine(self, source: Source | None = None, **options) -> Engine:
        self.enterContext(mock.patch("radiolack.scanners.VISIT_S", TOUR_VISIT_S))
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        engine.configure_demod({"frequency_hz": HOME_VFO_HZ})
        return engine

    @staticmethod
    def bookmark(engine: Engine, frequency_hz: float, name: str, scan: bool = True) -> int:
        """Add a bookmark: its id."""
        engine.edit_bookmarks({"action": "add", "frequency_hz": frequency_hz, "name": name, "mode": "NFM",
                               "scan": scan})
        return next(entry["id"] for entry in engine.bookmarks_state()["bookmarks"] if entry["name"] == name)

    def test_the_two_bookmarks_take_two_windows(self):
        self.assertEqual(len(self.WINDOWS), 2)
        self.assertNotIn(HOME_HZ, self.WINDOWS)
        self.assertGreater(TOUR_VISIT_S, 0)
        self.assertLess(TOUR_VISIT_S, VISIT_S)

    async def test_visits_the_windows_in_turn_and_brings_the_receiver_back_once_stopped(self):
        source = TuningSource({})
        engine = self.engine(source)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        near, far = self.bookmark(engine, NEAR_HZ, "Near"), self.bookmark(engine, FAR_HZ, "Far")
        self.assertEqual(engine.scanner_state()["memory"], {"watched": [near, far], "windows": 2, "window": None,
                                                            "state": None, "bookmark_id": None})
        engine.configure_scanner({"scan": "memory"})
        self.assertEqual(engine.scanner_state()["memory"]["window"], 0)     # the nearest
        for subscription in (local, remote):
            await next_scanner(subscription, until=at_memory_window(1))
        await eventually(lambda: len(source.tuned) >= 4)
        self.assertEqual(source.tuned[:4], [*self.WINDOWS, *self.WINDOWS])
        # Saved meanwhile, the receiver's settings are those of the scan's start.
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))
        engine.configure_scanner({"scan": None})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["window"]), (None, None, None))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_stays_on_a_channel_of_another_window_while_it_listens_and_once_stopped(self):
        source = TuningSource({FAR_HZ: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        self.bookmark(engine, NEAR_HZ, "Near")
        far = self.bookmark(engine, FAR_HZ, "Far")
        engine.configure_scanner({"scan": "memory"})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual((state["memory"]["bookmark_id"], state["memory"]["window"]), (far, 1))
        tuned = len(source.tuned)
        await asyncio.sleep(3 * TOUR_VISIT_S)
        self.assertEqual(len(source.tuned), tuned)      # it stays while it listens
        engine.configure_scanner({"scan": None})
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (self.WINDOWS[1], FAR_HZ))
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), (self.WINDOWS[1], FAR_HZ))

    async def test_never_tunes_the_receiver_while_its_bookmarks_lie_in_the_span(self):
        source = TuningSource({NEAR_HZ: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        near = self.bookmark(engine, NEAR_HZ, "Near")
        self.bookmark(engine, FAR_HZ, "Far", scan=False)
        self.assertEqual(engine.scanner_state()["memory"]["windows"], 1)
        engine.configure_scanner({"resume_s": 0})
        engine.configure_scanner({"scan": "memory"})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual((state["memory"]["bookmark_id"], state["memory"]["window"]), (near, 0))
        source.carriers = {}
        await next_scanner(subscription, until=in_state("watching"))
        await asyncio.sleep(3 * TOUR_VISIT_S)
        engine.configure_scanner({"scan": None})
        self.assertEqual(source.tuned, [])
        self.assertEqual(engine.demod_state()["frequency_hz"], NEAR_HZ)     # where the scan left the VFO

    async def test_leaves_out_the_bookmarks_beyond_the_receivers_reach(self):
        source = TuningSource({})
        engine = self.engine(source)
        self.bookmark(engine, 10e6, "Low")
        self.bookmark(engine, 2400e6, "High")
        state = engine.scanner_state()["memory"]
        self.assertEqual((state["watched"], state["windows"]), ([], 0))
        with self.assertRaisesRegex(ValueError, "no ticked bookmark lies within the receiver's reach"):
            engine.configure_scanner({"scan": "memory"})
        far = self.bookmark(engine, FAR_HZ, "Far")
        state = engine.scanner_state()["memory"]
        self.assertEqual((state["watched"], state["windows"]), ([far], 1))
        engine.configure_scanner({"scan": "memory"})
        [window] = memory_windows([FAR_HZ], FAKE_RATE_HZ, 24e6, 1766e6)
        await eventually(lambda: source.tuned == [window])

    async def test_follows_the_bookmarks_ticked_meanwhile_and_stops_once_none_is_left(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        near, far = self.bookmark(engine, NEAR_HZ, "Near"), self.bookmark(engine, FAR_HZ, "Far")
        engine.configure_scanner({"scan": "memory"})
        await next_scanner(subscription, until=at_memory_window(1))
        engine.edit_bookmarks({"action": "change", "id": far, "scan": False})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["memory"]["watched"], state["memory"]["windows"],
                          state["memory"]["window"]), ("memory", [near], 1, 0))
        await eventually(lambda: source.centre_hz == HOME_HZ)      # the span it started from holds the one left
        with self.assertLogs("radiolack.engine", "WARNING") as logs:
            engine.edit_bookmarks({"action": "change", "id": near, "scan": False})
        self.assertEqual(logs.output, ["WARNING:radiolack.engine:the memory scan stopped: no ticked bookmark lies "
                                       "within the receiver's reach any more"])
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "bookmarks", "memory"))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_stops_once_the_receiver_refuses_a_window_and_brings_it_back(self):
        source = TuningSource({})
        configure = source.configure

        def refuse_the_second_window(changes) -> None:
            if changes.get("centre_hz") == self.WINDOWS[1]:
                raise ValueError("the tuner did not lock")
            configure(changes)

        source.configure = refuse_the_second_window
        engine = self.engine(source)
        subscription = engine.subscribe()
        self.bookmark(engine, NEAR_HZ, "Near")
        self.bookmark(engine, FAR_HZ, "Far")
        engine.configure_scanner({"scan": "memory"})
        with self.assertLogs("radiolack.engine", "WARNING"):
            state = await next_scanner(subscription, until=lambda message: message["scan"] is None)
        self.assertEqual((state["stopped"], state["stopped_scan"]), ("range", "memory"))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_tuning_by_hand_or_another_source_stops_it_where_it_is(self):
        source = TuningSource({})
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()
        self.bookmark(engine, NEAR_HZ, "Near")
        self.bookmark(engine, FAR_HZ, "Far")

        async def on_the_second_window() -> None:
            engine.configure_scanner({"scan": "memory"})
            await next_scanner(subscription, until=at_memory_window(1))
            await eventually(lambda: engine._capture == (self.WINDOWS[1], FAKE_RATE_HZ))

        await on_the_second_window()
        vfo = engine.demod_state()["frequency_hz"] + 1_000
        engine.configure_demod({"frequency_hz": vfo})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "tuned", "memory"))
        self.assertEqual((source.centre_hz, engine.demod_state()["frequency_hz"]), (self.WINDOWS[1], vfo))
        engine.configure({"centre_hz": HOME_HZ})
        await on_the_second_window()
        tuned = len(source.tuned)
        engine.configure({"kind": "other"})
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "source", "memory"))
        self.assertEqual(len(source.tuned), tuned)      # left where it was
        self.assertEqual(engine._receivers["test"]["source"]["centre_hz"], HOME_HZ)     # saved as at the start


class RtlTcpMemoryScanTest(unittest.IsolatedAsyncioTestCase):
    """The memory scan with an rtl_tcp receiver: it finds a bookmark of
    another window on the air, through the receiver's settling."""

    async def test_listens_on_a_bookmark_of_another_window(self):
        rate = 1_024_000.0
        near, quiet, on_air = HOME_HZ + 100_000, HOME_HZ + 1_800_000, HOME_HZ + 2_000_000
        windows = memory_windows([near, quiet, on_air], rate, 24e6, 1766e6)     # two
        fake = FakeRtlTcp(carriers={on_air: 0.2}, chunk_bytes=16_384, interval_s=0.004)
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, fake.port, centre_hz=HOME_HZ, sample_rate_hz=rate, settle_s=0.1)
        engine = Engine(source)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        subscription = engine.subscribe()
        for frequency_hz, name in ((near, "Near"), (quiet, "Quiet"), (on_air, "On the air")):
            engine.edit_bookmarks({"action": "add", "frequency_hz": frequency_hz, "name": name, "mode": "NFM",
                                   "scan": True})
        ids = {entry["name"]: entry["id"] for entry in engine.bookmarks_state()["bookmarks"]}
        await eventually(lambda: engine.state()["status"] == "streaming")
        self.assertEqual(engine.scanner_state()["memory"]["windows"], len(windows))
        engine.configure_scanner({"scan": "memory"})
        state = await asyncio.wait_for(next_scanner(subscription, until=in_state("listening")), 30)
        self.assertEqual(state["memory"]["bookmark_id"], ids["On the air"])
        self.assertEqual((engine.state()["centre_hz"], engine.demod_state()["frequency_hz"]), (windows[1], on_air))
        await eventually(lambda: fake.frequency_hz == windows[1])


def heard(engine: Engine, frequency_hz: float) -> bool:
    """Whether the engine's observations hold a channel within 2 kHz of ``frequency_hz``."""
    return any(abs(channel["frequency_hz"] - frequency_hz) <= 2_000
               for channel in engine.observations_state()["channels"])


class GivenScanTest(unittest.IsolatedAsyncioTestCase):
    """A scan given at start (cli's --scan), with its saved settings: it
    goes on without any client, reading the receiver, and the History
    fills; one that cannot start says why to its caller and in the scanner
    state, and the engine stays idle."""

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"

    def engine(self, source: Source) -> Engine:
        engine = Engine(source, store=Store(self.directory))
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    async def rtl_tcp(self, carriers: dict[float, float]) -> RtlTcpSource:
        """A receiver at HOME_HZ behind the fake rtl_tcp, with ``carriers``,
        not yet connected, as at start."""
        fake = FakeRtlTcp(carriers=carriers, chunk_bytes=16_384, interval_s=0.004)
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        return RtlTcpSource(LOOPBACK, fake.port, centre_hz=HOME_HZ, sample_rate_hz=1_024_000.0, settle_s=0.1)

    async def test_the_memory_scan_runs_without_any_client_and_fills_the_history(self):
        on_air = HOME_HZ + 200_000
        Store(self.directory).save(BOOKMARKS_FILE, {"bookmarks": [
            {"frequency_hz": on_air, "name": "On the air", "mode": "NFM", "scan": True}]})
        engine = self.engine(await self.rtl_tcp({on_air: 0.2}))
        engine.start_given_scan("memory")
        self.assertEqual(engine.scanner_state()["scan"], "memory")
        await eventually(lambda: engine.scanner_state()["memory"]["state"] == "listening", timeout=30)
        self.assertEqual((engine.state()["status"], engine.demod_state()["frequency_hz"]), ("streaming", on_air))
        await eventually(lambda: heard(engine, on_air), timeout=30)

    async def test_the_sweep_runs_with_its_saved_range_without_any_client_and_fills_the_history(self):
        low, high = 145_000_000.0, 145_600_000.0
        on_air = high - 50_000      # in the last of its three windows
        Store(self.directory).save(SETTINGS_FILE, {"scanner": {"sweep_low_hz": low, "sweep_high_hz": high,
                                                               "sweep_window_s": 0.25}})
        engine = self.engine(await self.rtl_tcp({on_air: 0.2}))
        engine.start_given_scan("sweep")
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["sweep"]["windows"]), ("sweep", 3))
        await eventually(lambda: heard(engine, on_air), timeout=30)
        self.assertEqual(engine.state()["status"], "streaming")

    async def test_one_that_cannot_start_says_why_and_the_engine_stays_idle(self):
        refusals = (
            ("memory", FakeSource, "no ticked bookmark lies within the receiver's reach"),
            ("sweep", FakeSource, "the sweep needs a receiver that tunes, such as an RTL-SDR"),
            # Saved before the start: a range beyond the receiver's reach.
            ("sweep", lambda: TuningSource({}), "the receiver cannot tune to every window of the sweep's "
                                                "range: it tunes from 24 to 1766 MHz"),
        )
        Store(self.directory).save(SETTINGS_FILE, {"scanner": {"sweep_low_hz": 1_800e6, "sweep_high_hz": 1_810e6}})
        for scan, make_source, reason in refusals:
            with self.subTest(scan=scan, reason=reason):
                source = make_source()
                engine = self.engine(source)
                with self.assertRaises(ValueError) as raised:
                    engine.start_given_scan(scan)
                self.assertEqual(str(raised.exception), reason)
                await asyncio.sleep(0.1)
                self.assertEqual(source.reads, 0)
                # An interface that opens later learns why.
                subscription = engine.subscribe()
                state = await next_scanner(subscription)
                engine.unsubscribe(subscription)
                self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "start", scan))

    async def test_a_refusal_reaches_the_clients_and_leaves_the_other_scan_running(self):
        engine = self.engine(FakeSource())
        subscription = engine.subscribe()
        with self.assertRaises(ValueError):
            engine.start_given_scan("sweep")    # the source does not tune
        state = await asyncio.wait_for(next_scanner(subscription, until=lambda message: message["stopped"] is not None),
                                       timeout=10)
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "start", "sweep"))
        engine.edit_bookmarks({"action": "add", "frequency_hz": FAKE_CENTRE_HZ + 40_000, "name": "Near",
                               "mode": "NFM", "scan": True})
        engine.configure_scanner({"scan": "memory"})
        self.assertEqual(engine.scanner_state()["stopped"], None)
        with self.assertRaises(ValueError):
            engine.start_given_scan("sweep")
        for scan in ("history", "Memory", None):
            with self.subTest(scan=scan), self.assertRaisesRegex(ValueError, '^a scan is "memory" or "sweep", not '):
                engine.start_given_scan(scan)
        state = engine.scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), ("memory", None, None))


class SimulatedBandSource(FakeSource):
    """A fake source that passes for the simulated band."""

    kind = "synthetic"


def observations_of(subscription: Subscription) -> list[list[dict]]:
    """The channels of each observations message ready for a client, now."""
    return [message["channels"] for message in waiting(subscription)
            if isinstance(message, dict) and message["type"] == "observations"]


class ObservationsTest(unittest.IsolatedAsyncioTestCase):
    """What detection found, kept across restarts: recorded from a receiver
    but not from the simulated band, sent to the client that asks, cleared,
    written at most every so often, at once after a clear and at the stop."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"

    def engine(self, source: Source | None = None, run: bool = True, **options) -> Engine:
        engine = Engine(source if source is not None else FakeSource(), store=Store(self.directory), **options)
        task = asyncio.ensure_future(engine.run()) if run else None

        async def stop():
            engine.close()
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    def write(self, channels: list[dict]) -> None:
        self.directory.mkdir(exist_ok=True)
        (self.directory / OBSERVATIONS_FILE).write_text(json.dumps({"version": 1, "channels": channels}),
                                                        encoding="utf-8")

    def read(self) -> dict | None:
        try:
            return json.loads((self.directory / OBSERVATIONS_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    async def recorded(self, engine: Engine) -> list[dict]:
        """The observations, once the engine recorded some."""
        deadline = time.monotonic() + 5
        while not (channels := engine.observations_state()["channels"]):
            self.assertLess(time.monotonic(), deadline, "nothing was recorded")
            await asyncio.sleep(0.01)
        return channels

    @staticmethod
    async def searches(source: FakeSource, count: int) -> None:
        """Wait for ``count`` more searches of the detector."""
        reads = source.reads
        while source.reads < reads + 10 * count:
            await asyncio.sleep(0.01)

    async def test_the_signals_found_are_recorded_for_the_client_that_asks(self):
        engine = self.engine(observations_save_s=3600)
        subscription, other = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        [channel] = await self.recorded(engine)
        self.assertAlmostEqual(channel["frequency_hz"], FAKE_TONE_HZ, delta=FAKE_RATE_HZ / 8192)
        self.assertGreaterEqual(channel["transmissions"], 1)    # one, unless the test runs slow
        self.assertEqual(sum(channel["hours"]), channel["transmissions"])
        self.assertLessEqual(channel["first_s"], channel["last_s"])
        self.assertAlmostEqual(channel["last_s"], time.time(), delta=5)
        self.assertGreater(channel["margin_db"], 12.0)
        waiting(other)
        engine.configure_observations({}, subscription)
        [[sent]] = observations_of(subscription)
        self.assertEqual(sent["frequency_hz"], channel["frequency_hz"])
        self.assertEqual(observations_of(other), [])      # only the client that asks

    async def test_neither_the_simulated_band_nor_an_ignored_frequency_is_recorded(self):
        for source, ignored in ((SimulatedBandSource(), None), (FakeSource(), FAKE_TONE_HZ)):
            with self.subTest(kind=source.kind):
                engine = self.engine(source)
                if ignored is not None:
                    engine.configure_scanner({"ignore": ignored})
                subscription = engine.subscribe()
                await self.searches(source, 3)
                self.assertEqual(engine.observations_state()["channels"], [])
                if ignored is None:
                    self.assertTrue(engine.peaks_state()["peaks"])     # found, all the same
                engine.unsubscribe(subscription)

    async def test_a_client_clears_a_channel_or_all_and_the_file_follows_at_once(self):
        recent = time.time() - 60
        self.write([{"frequency_hz": 145_500_000, "first_s": recent, "last_s": recent, "transmissions": 3,
                     "active_s": 12.5, "margin_db": 18.0, "hours": [1] * 3 + [0] * 21},
                    {"frequency_hz": 433_500_000, "first_s": recent, "last_s": recent + 1, "transmissions": 1,
                     "active_s": 0.0, "margin_db": 15.0, "hours": [0] * 23 + [1]}])
        engine = self.engine(run=False, save_delay_s=0.01, observations_save_s=3600)
        subscription = engine.subscribe()
        self.assertEqual([channel["frequency_hz"] for channel in engine.observations_state()["channels"]],
                         [433_500_000, 145_500_000])
        waiting(subscription)
        engine.configure_observations({"clear": 145_501_000}, subscription)
        [[remaining]] = observations_of(subscription)
        self.assertEqual(remaining["frequency_hz"], 433_500_000)
        deadline = time.monotonic() + 5
        while (self.read() or {}).get("channels") != [remaining]:
            self.assertLess(time.monotonic(), deadline, "the clear was not saved")
            await asyncio.sleep(0.01)
        engine.configure_observations({"clear": 145_501_000}, subscription)    # none left there: no change
        self.assertEqual(observations_of(subscription), [[remaining]])
        engine.configure_observations({"clear_all": True}, subscription)
        self.assertEqual(observations_of(subscription), [[]])
        while self.read()["channels"]:
            self.assertLess(time.monotonic(), deadline, "the clear was not saved")
            await asyncio.sleep(0.01)

    async def test_refused_messages_change_nothing(self):
        engine = self.engine(run=False)
        subscription = engine.subscribe()
        waiting(subscription)
        refused = ({"clear": 145e6, "clear_all": True}, {"forget": 145e6}, {"clear": "145 MHz"}, {"clear": 0},
                   {"clear": True}, {"clear": 10 ** 400}, {"clear": float("nan")}, {"clear_all": False},
                   {"clear_all": 1})
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_observations(changes, subscription)
        self.assertEqual(observations_of(subscription), [])

    async def test_written_at_most_every_interval_while_it_runs(self):
        started = time.monotonic()
        engine = self.engine(save_delay_s=0.01, observations_save_s=0.5)
        engine.subscribe()
        writes = []
        deadline = started + 10
        while len(writes) < 2:
            self.assertLess(time.monotonic(), deadline, f"written {len(writes)} times")
            content = self.read()
            if content is not None and (not writes or content != writes[-1][1]):
                writes.append((time.monotonic(), content))
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(writes[0][0] - started, 0.45)
        self.assertGreaterEqual(writes[1][0] - writes[0][0], 0.45)
        [channel] = writes[1][1]["channels"]
        self.assertAlmostEqual(channel["frequency_hz"], FAKE_TONE_HZ, delta=FAKE_RATE_HZ / 8192)

    async def test_written_when_the_engine_stops_and_read_at_the_next_start(self):
        engine = self.engine(observations_save_s=3600)
        engine.subscribe()
        [channel] = await self.recorded(engine)
        await asyncio.sleep(0.1)
        self.assertIsNone(self.read())         # not yet
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)
        [saved] = self.read()["channels"]
        self.assertEqual(saved["frequency_hz"], channel["frequency_hz"])
        self.assertEqual(self.engine(run=False).observations_state()["channels"], [saved])

    async def test_an_unreadable_file_is_set_aside(self):
        self.directory.mkdir()
        (self.directory / OBSERVATIONS_FILE).write_text("{not json", encoding="utf-8")
        with self.assertLogs("radiolack.store", "WARNING"):
            engine = self.engine(run=False)
        self.assertEqual(engine.observations_state()["channels"], [])
        self.assertIn("observations.json could not be read", engine.storage_state()["problem"])
        self.assertEqual(len(list(self.directory.glob(f"{OBSERVATIONS_FILE}.invalid-*"))), 1)


def recording_states(subscription: Subscription) -> list[dict]:
    """The recording states ready for a client, now."""
    return [message for message in waiting(subscription)
            if isinstance(message, dict) and message["type"] == "recording"]


async def next_recording(subscription: Subscription, until=lambda state: True) -> dict:
    """The next recording state for which ``until`` holds."""
    async def wait() -> dict:
        while True:
            message = await next_message(subscription)
            if isinstance(message, dict) and message["type"] == "recording" and until(message):
                return message
    return await asyncio.wait_for(wait(), timeout=10)


async def next_reply(subscription: Subscription, kind: str) -> dict:
    """The next message of type ``kind``, skipping the others."""
    async def wait() -> dict:
        while True:
            message = await next_message(subscription)
            if isinstance(message, dict) and message["type"] == kind:
                return message
    return await asyncio.wait_for(wait(), timeout=10)


def with_file(state: dict) -> bool:
    """Whether a recording runs and its file was made."""
    return state["audio"] is not None and state["audio"]["file"] is not None


class SimulatedBandSources(FakeSources):
    """Fake sources whose last kind passes for the simulated band."""

    kinds = ("test", "other", "synthetic")

    def __init__(self) -> None:
        super().__init__(synthetic=SimulatedBandSource)


RECORDED_NAME = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    """Recording the audio: started and stopped by any client, kept up
    without one, never on the simulated band, stopped by a new audio rate,
    a disk short of space and the simulated band; the list of the
    recordings, their deletion, and the folder, chosen and saved."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.folder = self.base / "data" / "radiolack" / "recordings"
        self.directory = self.base / "config" / "radiolack"

    def engine(self, source: Source | None = None, sources: FakeSources | None = None, run: bool = True,
               **options) -> Engine:
        if source is None and sources is None:
            source = FakeSource()
        engine = Engine(source, sources=sources, recordings=self.folder, **options)
        task = asyncio.ensure_future(engine.run()) if run else None

        async def stop():
            engine.close()
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=15)

        self.addAsyncCleanup(stop)
        return engine

    @staticmethod
    async def stopped(engine: Engine) -> None:
        """Stop the engine: its recordings reach the disk."""
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=15)

    async def test_records_the_audio_until_a_client_stops_it(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        state = await next_recording(subscription, lambda state: with_file(state) and state["audio"]["seconds"] >= 0.5)
        audio = state["audio"]
        self.assertRegex(audio["file"], r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z_145\.061440MHz_NFM\.wav$")
        self.assertEqual((audio["frequency_hz"], audio["mode"], audio["rate_hz"]),
                         (FAKE_CENTRE_HZ + FAKE_RATE_HZ / 4, "NFM", 12_000))
        self.assertAlmostEqual(audio["started"], time.time(), delta=10)
        self.assertAlmostEqual(audio["seconds"], (audio["bytes"] - 44) / 2 / 12_000, delta=0.05)    # to the tenth
        self.assertEqual((state["stopped"], state["folder"]), (None, str(self.folder)))
        engine.configure_recording({"audio": False})
        self.assertEqual(engine.recording_state(), {"type": "recording", "audio": None, "stopped": None,
                                                    "folder": str(self.folder), "default_folder": str(self.folder)})
        await self.stopped(engine)
        with wave.open(str(self.folder / audio["file"])) as recorded:
            self.assertEqual((recorded.getnchannels(), recorded.getframerate()), (1, 12_000))
            self.assertGreaterEqual(recorded.getnframes(), (audio["bytes"] - 44) / 2)     # and what came later

    async def test_goes_on_without_any_client_until_stopped(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        engine.unsubscribe(subscription)
        reads = source.reads
        await asyncio.sleep(0.3)
        self.assertGreater(source.reads, reads + 5)
        engine.configure_recording({"audio": False})
        await asyncio.sleep(0.05)       # the block read meanwhile
        reads = source.reads
        await asyncio.sleep(0.3)
        self.assertLessEqual(source.reads, reads + 1)

    async def test_never_on_the_simulated_band(self):
        engine = self.engine(SimulatedBandSource())
        with self.assertRaisesRegex(ValueError, "^the simulated band is not recorded$"):
            engine.configure_recording({"audio": True})
        self.assertIsNone(engine.recording_state()["audio"])
        self.assertFalse(self.folder.exists())

    async def test_the_simulated_band_stops_it_and_another_receiver_does_not(self):
        engine = self.engine(sources=SimulatedBandSources())
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        await next_recording(subscription, with_file)
        engine.configure({"kind": "other"})
        self.assertIsNotNone(engine.recording_state()["audio"])
        with self.assertLogs("radiolack.engine", "WARNING") as logs:
            engine.configure({"kind": "synthetic"})
        state = engine.recording_state()
        self.assertEqual((state["audio"], state["stopped"]),
                         (None, {"kind": "audio", "reason": "source", "detail": ""}))
        self.assertIn("stopped: the simulated band was chosen", logs.output[0])

    async def test_a_new_audio_rate_stops_it(self):
        engine = self.engine(WideSource())
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        await next_recording(subscription, lambda state: with_file(state) and state["audio"]["rate_hz"] == 12_000)
        engine.configure_demod({"mode": "AM"})      # the same rate: it goes on
        await asyncio.sleep(0.2)
        self.assertIsNotNone(engine.recording_state()["audio"])
        with self.assertLogs("radiolack.engine", "WARNING"):
            engine.configure_demod({"mode": "WFM"})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "rate", "detail": ""})

    async def test_stops_when_its_disk_falls_short_and_says_why_until_the_next(self):
        engine = self.engine()
        subscription = engine.subscribe()
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62), \
                self.assertLogs("radiolack.engine", "WARNING") as logs:
            engine.configure_recording({"audio": True})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "space", "detail": ""})
        self.assertIn("less than 2 GiB is free on its disk", logs.output[0])
        self.assertEqual(list(self.folder.iterdir()), [])
        engine.configure_recording({"audio": True})
        state = engine.recording_state()
        self.assertEqual((state["audio"]["file"], state["stopped"]), (None, None))

    async def test_a_disk_error_stops_it(self):
        engine = self.engine()
        subscription = engine.subscribe()
        failure = OSError(errno.EIO, "Input/output error")
        with mock.patch.object(recordings, "_write_all", side_effect=failure), \
                self.assertLogs("radiolack.engine", "WARNING"):
            engine.configure_recording({"audio": True})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "disk", "detail": "Input/output error"})

    async def test_stopping_the_engine_closes_the_file(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        state = await next_recording(subscription, lambda state: with_file(state) and state["audio"]["seconds"] >= 0.3)
        await self.stopped(engine)
        path = self.folder / state["audio"]["file"]
        with wave.open(str(path)) as recorded:
            self.assertEqual(path.stat().st_size, 44 + 2 * recorded.getnframes())
            self.assertGreaterEqual(recorded.getnframes(), (state["audio"]["bytes"] - 44) / 2)

    async def test_lists_and_deletes_the_recordings(self):
        engine = self.engine()
        subscription = engine.subscribe()
        self.folder.mkdir(parents=True)
        (self.folder / RECORDED_NAME).write_bytes(wav_header(12_000, 24_000) + bytes(24_000))
        engine.configure_recordings({}, subscription)
        reply = await next_reply(subscription, "recordings")
        self.assertEqual(reply["files"], [{"name": RECORDED_NAME, "kind": "audio", "started": 1_794_000_000,
                                           "frequency_hz": 145_500_000, "mode": "NFM", "bytes": 24_044,
                                           "seconds": 1.0, "running": False}])
        self.assertEqual((reply["more"], reply["problem"]), (0, ""))
        self.assertEqual(engine.download_folder(RECORDED_NAME), self.folder)
        engine.configure_recordings({"delete": RECORDED_NAME}, subscription)
        self.assertEqual((await next_reply(subscription, "recordings"))["files"], [])
        self.assertFalse((self.folder / RECORDED_NAME).exists())
        for changes in ({"delete": "notes.txt"}, {"delete": None}, {"clear": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_recordings(changes, subscription)

    async def test_the_recording_that_runs_is_neither_deleted_nor_downloaded(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.configure_recording({"audio": True})
        name = (await next_recording(subscription, with_file))["audio"]["file"]
        self.assertIsNone(engine.download_folder(name))
        with self.assertRaisesRegex(ValueError, "^this recording runs"):
            engine.configure_recordings({"delete": name}, subscription)
        engine.configure_recordings({}, subscription)
        [entry] = (await next_reply(subscription, "recordings"))["files"]
        self.assertEqual((entry["name"], entry["running"]), (name, True))
        engine.configure_recording({"audio": False})
        self.assertEqual(engine.download_folder(name), self.folder)
        self.assertIsNone(engine.download_folder("../settings.json"))

    async def test_records_into_a_folder_chosen_which_is_saved(self):
        engine = self.engine(store=Store(self.directory), save_delay_s=0.01)
        subscription = engine.subscribe()
        chosen = self.base / "chosen"
        chosen.mkdir()
        engine.configure_recording({"folder": str(chosen)}, subscription)
        state = await next_recording(subscription, lambda state: state["folder"] == str(chosen))
        self.assertEqual(state["default_folder"], str(self.folder))
        engine.configure_recording({"audio": True})
        name = (await next_recording(subscription, with_file))["audio"]["file"]
        with self.assertRaisesRegex(ValueError, "^a recording runs"):
            engine.configure_recording({"folder": None}, subscription)
        await self.stopped(engine)
        self.assertTrue((chosen / name).is_file())
        self.assertFalse(self.folder.exists())
        settings = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(settings["recordings"], {"folder": str(chosen)})
        again = self.engine(store=Store(self.directory), save_delay_s=0.01)
        self.assertEqual(again.recording_state()["folder"], str(chosen))
        subscription = again.subscribe()
        again.configure_recording({"folder": None}, subscription)
        await next_recording(subscription, lambda state: state["folder"] == str(self.folder))

    async def test_refuses_a_folder_it_cannot_record_into(self):
        engine = self.engine()
        subscription = engine.subscribe()
        missing = self.base / "missing"
        engine.configure_recording({"folder": str(missing)}, subscription)
        self.assertEqual(await next_reply(subscription, "error"),
                         {"type": "error", "command": "recording", "message": f"{missing} does not exist"})
        for changes in ({"folder": "relative"}, {"folder": 3}, {"audio": "yes"}, {"audio": True, "folder": None},
                        {}, {"other": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_recording(changes, subscription)
        self.assertEqual(engine.recording_state()["folder"], str(self.folder))

    async def test_a_remote_client_does_not_learn_the_folder(self):
        engine = Engine(FakeSource(), remote=RemoteAccess(), recordings=self.folder)
        self.addCleanup(engine.close)
        [state] = [message for message in ready_messages(engine.subscribe(device="5f0c2a91"))
                   if message["type"] == "recording"]
        self.assertEqual(state, {"type": "recording", "audio": None, "stopped": None})

    async def test_without_a_folder_nothing_is_recorded(self):
        engine = Engine(FakeSource())
        self.addCleanup(engine.close)
        subscription = engine.subscribe()
        for command in (lambda: engine.configure_recording({"audio": True}),
                        lambda: engine.configure_recording({"folder": None}),
                        lambda: engine.configure_recordings({}, subscription)):
            with self.assertRaisesRegex(ValueError, "^this Radiolack does not record$"):
                command()
        self.assertIsNone(engine.download_folder(RECORDED_NAME))
