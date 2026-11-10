"""Tests for the engine: frames, source state and demodulation settings for
every client, audio for the clients that ask, idle without clients, changes
passed to the source, switching to another kind of source, DC removal,
bookmarks and preferences, saved settings, clean stop."""

import asyncio
import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO, MODES
from radiolack.engine import Engine, Source, Subscription
from radiolack.rtltcp import RtlTcpSource
from radiolack.spectrum import FLAG_SQUELCH_OPEN, FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, SETTINGS_FILE, Store
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, FakeSource

DC_BIN = 4096       # the centre frequency, with 8192-point FFTs
FAKE_TONE_HZ = FAKE_CENTRE_HZ + (FAKE_TONE_BIN - DC_BIN) * FAKE_RATE_HZ / 8192
STATES = ("source", "demod", "bookmarks", "preferences", "storage")     # what a new client gets first


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
        self.assertEqual(subscription.get_nowait(), {"type": "preferences", "values": {}})
        # Without a store, nothing is saved.
        self.assertEqual(subscription.get_nowait(),
                         {"type": "storage", "directory": None, "saving": False, "problem": ""})
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
            "demod": {"frequency_hz": 145_010_000, "mode": "WFM", "squelch_db": None}}},
            "interface": {"volume": 40, "nested": {"a": 1}, "": 1, "text": "x" * 101}})
        with self.assertLogs("radiolack.engine", "WARNING") as logs:
            engine = self.engine()
        self.assertEqual(len(logs.output), 4)   # DC removal, mode, squelch and some preferences
        self.assertTrue(engine.state()["dc_removal"])
        state = engine.demod_state()
        self.assertEqual((state["frequency_hz"], state["mode"], state["squelch_db"]), (145_010_000.0, "NFM", -80.0))
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
        self.assertTrue(waiting(subscription)[-1]["saving"])

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
        self.assertEqual(waiting(later)[3], {"type": "preferences", "values": {"volume": 30}})
        for values in ([1], {"volume": [30]}, {"volume": float("inf")}, {"big": 2 ** 60},
                       {f"name {n}": n for n in range(101)}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "preference"):
                engine.set_preferences(values)
        self.assertEqual(engine.preferences_state()["values"], {"volume": 30})


if __name__ == "__main__":
    unittest.main()
