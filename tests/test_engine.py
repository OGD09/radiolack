"""Tests for the engine: frames, source state and demodulation settings for
every client, audio for the clients that ask, idle without clients, changes
passed to the source, switching to another kind of source, DC removal,
bookmarks and preferences, update checks, saved settings, detection and the
memory scan, clean stop."""

import asyncio
import contextlib
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO, MODES
from radiolack import __version__
from radiolack.engine import Engine, Source, Subscription
from radiolack.remote import REMOTE_FILE, RemoteAccess
from radiolack.rtltcp import RtlTcpSource
from radiolack.spectrum import FLAG_SQUELCH_OPEN, FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, LOOPBACK, FakeSource, FakeUpdates, free_port

DC_BIN = 4096       # the centre frequency, with 8192-point FFTs
FAKE_TONE_HZ = FAKE_CENTRE_HZ + (FAKE_TONE_BIN - DC_BIN) * FAKE_RATE_HZ / 8192
# What a new client gets first; a remote client, without the last two.
STATES = ("source", "demod", "bookmarks", "scanner", "peaks", "preferences", "storage", "update")


def scanner_state(**changes) -> dict:
    """The scanner state message: the defaults, without a scan or a ticked
    bookmark, with ``changes``."""
    return {"type": "scanner", "detection": True, "threshold_db": 12.0, "ignored_hz": [], "resume_s": 2.0,
            "scan": None, "stopped": None, "memory": {"watched": [], "state": None, "bookmark_id": None},
            **changes}


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
            self.engine.configure_demod({"bandwidth_hz": 6_000, "mode": "WFM"}, sender, 3)
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
            "demod": {"frequency_hz": 145_010_000, "mode": "WFM", "squelch_db": None,
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
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription), [scanner_state(threshold_db=20.0, resume_s=0.0)])
        refused = ({}, {"threshold": 20}, {"detection": "on"}, {"detection": None}, {"threshold_db": 5},
                   {"threshold_db": 31}, {"threshold_db": "12"}, {"threshold_db": float("nan")},
                   {"resume_s": -0.5}, {"resume_s": 8.5}, {"resume_s": True}, {"resume_s": "2"},
                   {"ignore": 145e6, "restore": True}, {"ignore": 145e6, "detection": False},
                   {"ignore": "145 MHz"}, {"ignore": 0}, {"restore": False}, {"restore": 1},
                   {"scan": "sweep"}, {"scan": True}, {"scan": "memory", "resume_s": 2},
                   {"scan": "memory"})      # no ticked bookmark in the span
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_scanner(changes)
        self.assertEqual(engine.scanner_state(), scanner_state(threshold_db=20.0, resume_s=0.0))
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
        engine.configure_scanner({"ignore": 145_500_000.4})
        engine.configure_scanner({"ignore": 144_800_000})
        engine.close()
        await engine.wait_closed()
        saved = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["scanner"], {"detection": False, "threshold_db": 18.5, "resume_s": 4.5,
                                            "ignored_hz": [144_800_000.0, 145_500_000.0]})
        again = self.engine(run=False, store=Store(self.directory))
        self.assertEqual(again.scanner_state(), scanner_state(detection=False, threshold_db=18.5, resume_s=4.5,
                                                              ignored_hz=[144_800_000.0, 145_500_000.0]))

    async def test_saved_scanner_settings_that_are_not_valid_keep_their_defaults(self):
        self.directory.mkdir()
        settings = self.directory / SETTINGS_FILE
        for saved, expected in (
                ({"detection": "yes", "threshold_db": 99, "resume_s": -1,
                  "ignored_hz": [145e6, "x", 0, 10 ** 400, 145e6 + 100, 144e6]},
                 (True, 12.0, [144e6, 145e6])),
                ({"ignored_hz": {"145e6": True}}, (True, 12.0, [])),
                ([1], (True, 12.0, [])),
                ({"ignored_hz": [144e6 + n * 10_000 for n in range(250)]}, (True, 12.0, None))):
            with self.subTest(saved=saved):
                settings.write_text(json.dumps({"version": 1, "scanner": saved}), encoding="utf-8")
                with self.assertLogs("radiolack.engine", "WARNING"):
                    engine = Engine(FakeSource(), store=Store(self.directory))
                state = engine.scanner_state()
                engine.close()
                detection, threshold_db, ignored = expected
                self.assertEqual((state["detection"], state["threshold_db"], state["resume_s"]),
                                 (detection, threshold_db, 2.0))
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
        self.assertEqual(engine.scanner_state()["memory"], {"watched": [weak, strong, quiet], "state": None,
                                                            "bookmark_id": None})
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
                stop()
                state = engine.scanner_state()
                self.assertEqual((state["scan"], state["stopped"], state["memory"]["state"]), (None, reason, None))
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
