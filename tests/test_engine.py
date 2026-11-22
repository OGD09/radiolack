"""Tests for the engine: frames, source state and demodulation settings for
every client, audio for the clients that ask, idle without clients, changes
passed to the source, switching to another kind of source, DC removal,
bookmarks and preferences, update checks, saved settings, detection, the
memory scan and its windows, the sweep, clean stop."""

import asyncio
import functools
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

from radiolack.carrier import CarrierMeasure
from radiolack.converter import Conversion
from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO, MODES, Demodulator
from radiolack import __version__, bandplan, r82xx, recordings
from radiolack.engine import MAX_RADIOS, Engine, Source, Subscription
from radiolack.recordings import meta_name, wav_header
from radiolack.remote import REMOTE_FILE, RemoteAccess
from radiolack.rtltcp import RtlTcpSource
from radiolack.scanners import VISIT_S, Placement, memory_windows, sweep_windows
from radiolack.spectrum import FLAG_SQUELCH_OPEN, FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import (FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, LOOPBACK, FakeRtlTcp, FakeSource, FakeUpdates,
                         fake_codes, free_port, wide_signal)

DC_BIN = 4096       # the centre frequency, with 8192-point FFTs
FAKE_TONE_HZ = FAKE_CENTRE_HZ + (FAKE_TONE_BIN - DC_BIN) * FAKE_RATE_HZ / 8192
# What a new client gets first; a remote client, without the last two.
STATES = ("source", "demod", "bookmarks", "band_plan", "scanner", "peaks", "preferences", "recording", "radios",
          "scans",
          "calibration", "storage", "update")

# The calibration state before any calibration ran.
CALIBRATION_IDLE = {"type": "calibration", "running": False, "stations": [], "results": [], "proposal": None,
                    "reason": None, "ended": None}


def scanner_state(**changes) -> dict:
    """The scanner state message: the defaults, without a scan or a ticked
    bookmark, with ``changes``."""
    return {"type": "scanner", "detection": True, "threshold_db": 12.0, "ignored_hz": [], "resume_s": 2.0,
            "listen_s": 10.0, "sweep_low_hz": 430e6, "sweep_high_hz": 440e6, "sweep_window_s": 0.5,
            "sweep_listen": False, "sweep_passes": None, "scan": None, "stopped": None, "stopped_scan": None,
            "memory": {"watched": [], "windows": 0, "window": None, "state": None, "bookmark_id": None},
            "sweep": {"windows": None, "window": None, "passes": None, "passes_set": None},
            "signals": {"state": None, "frequency_hz": None}, **changes}


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


async def next_demod(subscription: Subscription) -> dict:
    """The next demodulation state that a client receives."""
    while not isinstance(message := await next_message(subscription), dict) or message["type"] != "demod":
        pass
    return message


def waiting(subscription: Subscription) -> list:
    """Every message ready now, without waiting for more."""
    messages = []
    while True:
        try:
            messages.append(subscription.get_nowait())
        except asyncio.QueueEmpty:
            return messages


def of_type(messages: list, kind: str) -> list[dict]:
    """The JSON messages of ``kind`` among ``messages``, frames left out."""
    return [message for message in messages if isinstance(message, dict) and message["type"] == kind]


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
            "tuner": None, "driver": None, "centre_hz": FAKE_CENTRE_HZ, "centre_in_use_hz": FAKE_CENTRE_HZ,
            "sample_rate_hz": FAKE_RATE_HZ, "sample_rates_hz": [],
            "gain_db": None, "ppm": None, "gains_db": [], "range_hz": [FAKE_CENTRE_HZ, FAKE_CENTRE_HZ],
            "hf_input": None, "hf_inputs": [], "direct": False, "rtl_agc": None, "bias_tee": None,
            "converter": None, "converter_lo_hz": None, "driver_pll": None, "tuner_grid": None,
            "dc_removal": True, "settable": ["dc_removal"], "kinds": [],    # this engine has one source only
        })
        self.assertEqual(subscription.get_nowait(), {
            "type": "demod", "frequency_hz": FAKE_CENTRE_HZ + FAKE_RATE_HZ / 4, "mode": "NFM",
            "bandwidth_hz": 10_000.0, "squelch_db": -80.0, "squelch_auto": True, "notch": False,
            "blanker": False, "sam_locked": None, "notch_lines": None, "blanker_impulses": None,
            "modes": list(MODES), "audio_modes": list(MODES), "acknowledged": 0,
        })
        self.assertEqual(subscription.get_nowait(), {"type": "bookmarks", "bookmarks": [], "categories": []})
        self.assertEqual(subscription.get_nowait(), {"type": "band_plan", "plan": "europe"})
        self.assertEqual(subscription.get_nowait(), scanner_state())
        self.assertEqual(subscription.get_nowait(), {"type": "peaks", "peaks": []})
        self.assertEqual(subscription.get_nowait(), {"type": "preferences", "values": {}})
        # Without a recordings folder, nothing is recorded.
        self.assertEqual(subscription.get_nowait(), {"type": "recording", "audio": None, "iq": None, "stopped": None,
                                                     "folder": None, "default_folder": None})
        # One radio, which it shows.
        self.assertEqual(subscription.get_nowait(), {"type": "radios", "radios": [
            {"id": 1, "kind": "test", "device": None, "tuner": None, "status": "streaming", "scan": None,
             "recording": []}], "shown": 1, "max": 4})
        self.assertEqual(subscription.get_nowait(), {"type": "scans", "scans": []})
        self.assertEqual(subscription.get_nowait(), CALIBRATION_IDLE)     # no calibration ran
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
            self.engine.radios[0].configure({"centre_hz": 433e6})

    async def test_dc_removal_is_the_engines_own_setting_for_every_client(self):
        subscriptions = [self.engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            self.assertTrue(subscription.get_nowait()["dc_removal"])
        self.engine.radios[0].configure({"dc_removal": False})
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
                self.engine.radios[0].configure(changes)
        self.assertTrue(self.engine.radios[0].state()["dc_removal"])
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
        self.engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "bandwidth_hz": 12_500}, sender, 7)
        for subscription, acknowledged in ((sender, 7), (other, 0)):
            state = subscription.get_nowait()
            self.assertEqual((state["type"], state["frequency_hz"], state["bandwidth_hz"], state["acknowledged"]),
                             ("demod", 145_010_000.0, 12_500.0, acknowledged))
        # Later states tell the sender the same acknowledgement.
        self.engine.radios[0].configure_demod({"squelch_auto": False})
        self.assertEqual(sender.get_nowait()["acknowledged"], 7)

    async def test_a_refused_change_changes_nothing_but_is_acknowledged(self):
        sender, other = self.engine.subscribe(), self.engine.subscribe()
        waiting(sender), waiting(other)
        with self.assertRaisesRegex(ValueError, "the mode must be one of"):
            self.engine.radios[0].configure_demod({"bandwidth_hz": 6_000, "mode": "DSB"}, sender, 3)
        state = sender.get_nowait()
        self.assertEqual((state["mode"], state["bandwidth_hz"], state["acknowledged"]), ("NFM", 10_000.0, 3))
        self.assertFalse(any(isinstance(message, dict) for message in waiting(other)))

    async def test_frames_carry_the_channel_level_and_squelch(self):
        subscription = self.engine.subscribe()
        for threshold, is_open in ((-30.0, True), (-10.0, False)):
            with self.subTest(threshold=threshold):
                self.engine.radios[0].configure_demod({"frequency_hz": FAKE_TONE_HZ, "squelch_auto": False,
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
        self.engine.radios[0].set_audio(listener, True)
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
        self.engine.radios[0].set_audio(listener, False)
        reads = self.source.reads
        while self.source.reads < reads + 3:
            await asyncio.sleep(0.01)
        self.assertFalse(any(is_audio(message) for message in waiting(listener)))

    async def test_sam_s_lock_reaches_every_client_and_is_null_in_the_other_modes(self):
        clients = [self.engine.subscribe() for _ in range(2)]
        for subscription in clients:
            self.assertIsNone(waiting(subscription)[STATES.index("demod")]["sam_locked"])
        self.engine.radios[0].configure_demod({"frequency_hz": round(FAKE_TONE_HZ), "mode": "SAM", "bandwidth_hz": 8_000})
        self.assertIs(self.engine.radios[0].demod_state()["sam_locked"], False)     # until a block finds the carrier
        for subscription in clients:
            state = await next_demod(subscription)
            self.assertEqual((state["mode"], state["bandwidth_hz"]), ("SAM", 8_000.0))
            self.assertIn("SAM", state["modes"])
            self.assertIn("SAM", state["audio_modes"])
            while state["sam_locked"] is not True:      # the carrier on the VFO
                self.assertIs(state["sam_locked"], False)
                state = await next_demod(subscription)
        self.assertIs(self.engine.radios[0].demod_state()["sam_locked"], True)
        self.engine.radios[0].configure_demod({"mode": "AM"})
        for subscription in clients:
            state = await next_demod(subscription)
            self.assertEqual((state["mode"], state["sam_locked"]), ("AM", None))
        # In AM, SAM's lock goes without a word.
        reads = self.source.reads
        while self.source.reads < reads + 5:
            await asyncio.sleep(0.01)
        for subscription in clients:
            self.assertFalse([message for message in waiting(subscription)
                              if isinstance(message, dict) and message["type"] == "demod"])

    async def test_the_notch_is_shared_by_every_client_and_says_what_it_removes(self):
        # The fake's tone, 1 kHz above the VFO in USB, whistles.
        listener, other = self.engine.subscribe(), self.engine.subscribe()
        for subscription in (listener, other):
            state = waiting(subscription)[STATES.index("demod")]
            self.assertEqual((state["notch"], state["notch_lines"]), (False, None))
        self.engine.radios[0].set_audio(listener, True)
        self.engine.radios[0].configure_demod({"frequency_hz": round(FAKE_TONE_HZ) - 1_000, "mode": "USB",
                                     "bandwidth_hz": 2_700, "notch": True}, other, 4)
        for subscription in (listener, other):
            state = await next_demod(subscription)      # the state as it is when the client reads it
            self.assertEqual((state["mode"], state["notch"]), ("USB", True))
            while state["notch_lines"] != 1:        # steady for half a second, then removed
                self.assertEqual(state["notch_lines"], 0)
                state = await next_demod(subscription)
            self.assertEqual(state["acknowledged"], 4 if subscription is other else 0)
        # Kept in CW, where it does not act; off, it says nothing either.
        self.engine.radios[0].configure_demod({"mode": "CW"})
        for subscription in (listener, other):
            state = await next_demod(subscription)
            self.assertEqual((state["mode"], state["notch"], state["notch_lines"]), ("CW", True, None))
        self.engine.radios[0].configure_demod({"mode": "USB", "notch": False})
        for subscription in (listener, other):
            state = await next_demod(subscription)
            self.assertEqual((state["mode"], state["notch"], state["notch_lines"]), ("USB", False, None))
        reads = self.source.reads
        while self.source.reads < reads + 5:
            await asyncio.sleep(0.01)
        for subscription in (listener, other):
            self.assertFalse([message for message in waiting(subscription)
                              if isinstance(message, dict) and message["type"] == "demod"])
        with self.assertRaisesRegex(ValueError, "^the notch must be true or false$"):
            self.engine.radios[0].configure_demod({"notch": "on"})

    async def test_a_new_span_brings_the_vfo_inside_it(self):
        subscription = self.engine.subscribe()
        await next_frame(subscription)
        self.source.centre_hz = 146_000_000.0
        while not isinstance(message := await next_message(subscription), dict) or message["type"] != "demod":
            pass
        self.assertEqual(message["frequency_hz"], 146_000_000.0 - FAKE_RATE_HZ / 2)


class ImpulsesSource(FakeSource):
    """The fake source's tone, continuous across reads, over weak noise,
    with an impulse of three samples every ``period`` samples, at a phase of
    its own: 120 a second at first, in step with the seconds of samples."""

    def __init__(self) -> None:
        super().__init__()
        self.period = 2_048
        self._sent = 0
        self._rng = np.random.default_rng(5)

    async def read(self, count: int) -> np.ndarray | None:
        await asyncio.sleep(0.005)
        self.reads += 1
        index = self._sent + np.arange(count)
        self._sent += count
        offset_hz = (FAKE_TONE_BIN - 4096) * FAKE_RATE_HZ / 8192
        samples = 0.01 * np.exp(2j * np.pi * offset_hz * index / FAKE_RATE_HZ)
        samples += 0.001 * (self._rng.standard_normal(count) + 1j * self._rng.standard_normal(count))
        hit = index % self.period < 3
        samples[hit] = 0.5 * np.exp(2j * np.pi * self._rng.random(int(hit.sum())))
        return samples.astype(np.complex64)


class BlankerTest(unittest.IsolatedAsyncioTestCase):
    """The noise blanker: shared by every client as the mode is, and a
    count of what it blanks that changes at most once a second."""

    async def asyncSetUp(self):
        self.source = ImpulsesSource()
        self.engine = Engine(self.source)
        self.task = asyncio.ensure_future(self.engine.run())

    async def asyncTearDown(self):
        self.engine.close()
        await asyncio.wait_for(self.task, timeout=5)

    async def demod_states(self, subscription: Subscription, reads: int) -> list[dict]:
        """The demodulation states that a client receives while the source
        gives ``reads`` more blocks."""
        states, until = [], self.source.reads + reads
        while self.source.reads < until:
            states += [message for message in waiting(subscription)
                       if isinstance(message, dict) and message["type"] == "demod"]
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)       # the block in progress
        return states + [message for message in waiting(subscription)
                         if isinstance(message, dict) and message["type"] == "demod"]

    async def test_shared_by_every_client_saying_what_it_blanks_once_a_second_at_most(self):
        listener, other = self.engine.subscribe(), self.engine.subscribe()
        for subscription in (listener, other):
            state = waiting(subscription)[STATES.index("demod")]
            self.assertEqual((state["blanker"], state["blanker_impulses"]), (False, None))
        self.engine.radios[0].set_audio(listener, True)
        self.engine.radios[0].configure_demod({"frequency_hz": round(FAKE_TONE_HZ), "mode": "AM", "blanker": True}, other, 4)
        for subscription in (listener, other):
            state = await next_demod(subscription)      # the state as it is when the client reads it
            self.assertEqual((state["mode"], state["blanker"]), ("AM", True))
            self.assertEqual(state["acknowledged"], 4 if subscription is other else 0)
            while state["blanker_impulses"] != 120:     # after its first second of samples
                self.assertEqual(state["blanker_impulses"], 0)
                state = await next_demod(subscription)
        # Steady impulses in step with the seconds: nothing more to say.
        self.assertEqual(await self.demod_states(other, 90), [])
        # Out of step, 122.88 a second: once a second at most, 30 blocks of 8192 samples.
        self.source.period = 2_000
        states = await self.demod_states(other, 150)
        self.assertLessEqual(len(states), 6)
        self.assertTrue(states)
        self.assertTrue(all(state["blanker_impulses"] in (122, 123) for state in states[1:]), states)
        # Kept across modes; off, it says nothing.
        self.engine.radios[0].configure_demod({"mode": "USB"})
        self.assertEqual((await next_demod(listener))["blanker"], True)
        self.engine.radios[0].configure_demod({"blanker": False})
        for subscription in (listener, other):
            state = (await self.demod_states(subscription, 1))[-1]
            self.assertEqual((state["blanker"], state["blanker_impulses"]), (False, None))
        self.assertEqual(await self.demod_states(other, 40), [])
        with self.assertRaisesRegex(ValueError, "^the noise blanker must be true or false$"):
            self.engine.radios[0].configure_demod({"blanker": "on"})


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
        self.engine.radios[0].configure({"dc_removal": False})
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
        with self.assertLogs("radiolack", "ERROR") as logs:
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
        state = self.engine.radios[0].state()
        self.assertEqual((state["kind"], state["kinds"]), ("test", ["test", "other"]))

    async def test_another_kind_replaces_the_source_for_every_client(self):
        first, second = self.engine.subscribe(), self.engine.subscribe()
        for subscription in (first, second):
            ready_messages(subscription)
        self.engine.radios[0].configure({"kind": "other"})
        old, new = self.sources.opened
        self.assertTrue(old.closed)
        self.assertIs(self.engine.radios[0].source, new)
        for subscription in (first, second):
            states = {message["type"]: message for message in ready_messages(subscription)}
            self.assertEqual(states["source"]["kind"], "other")
            # The new receiver's own VFO: by default, a quarter of its span above its centre.
            self.assertEqual(states["demod"]["frequency_hz"], OtherSource.centre_hz + FAKE_RATE_HZ / 4)

    async def test_each_kind_comes_back_with_its_settings(self):
        self.engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 10_000, "mode": "AM"})
        self.engine.radios[0].configure({"kind": "other"})
        self.assertEqual(self.engine.radios[0].demod_state()["mode"], "NFM")
        self.engine.radios[0].configure({"kind": "test"})
        demod = self.engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"]), (FAKE_CENTRE_HZ + 10_000, "AM"))
        self.assertEqual(len(self.sources.opened), 3)     # a new source each time

    async def test_the_kind_comes_alone_and_must_be_one_of_the_kinds(self):
        for changes in ({"kind": "other", "dc_removal": False}, {"kind": "radio"}, {"kind": 1}, {"kind": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.radios[0].configure(changes)
        self.assertEqual((self.engine.radios[0].source.kind, len(self.sources.opened)), ("test", 1))
        self.assertTrue(self.engine.radios[0].state()["dc_removal"])

    async def test_the_kind_in_use_keeps_its_source(self):
        source = self.engine.radios[0].source
        subscription = self.engine.subscribe()
        ready_messages(subscription)
        self.engine.radios[0].configure({"kind": "test"})
        self.assertIs(self.engine.radios[0].source, source)
        self.assertFalse(source.closed)
        self.assertEqual([message["type"] for message in ready_messages(subscription)], ["source"])

    async def test_an_engine_without_sources_keeps_its_source(self):
        engine = Engine(FakeSource())
        self.addAsyncCleanup(self.stop, engine)
        self.assertEqual(engine.radios[0].state()["kinds"], [])
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            engine.radios[0].configure({"kind": "test"})

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
        engine.radios[0].configure({"kind": "other"})
        await asyncio.sleep(0.2)
        self.assertEqual(sources.opened[1].reads, 0)     # the first one still holds the receiver
        released.set()
        while FRAME_HEADER.unpack_from(await next_frame(subscription))[4] != OtherSource.centre_hz:
            pass
        self.assertGreater(sources.opened[1].reads, 0)


class ThirdSource(FakeSource):
    """A third kind of fake source, on yet another frequency."""

    kind = "third"
    centre_hz = FAKE_CENTRE_HZ + 2e6


def radios_of(engine: Engine) -> list[tuple[int, str]]:
    return [(radio["id"], radio["kind"]) for radio in engine.radios_state()["radios"]]


async def frame_from(subscription: Subscription, centre_hz: float) -> None:
    """Wait for a frame of the receiver at ``centre_hz``."""
    while FRAME_HEADER.unpack_from(await next_frame(subscription))[4] != centre_hz:
        pass


class SeveralRadiosTest(unittest.IsolatedAsyncioTestCase):
    """An engine with several radios, each reading a fake source of its own
    (later-milestones.md, section 5)."""

    async def asyncSetUp(self):
        self.sources = FakeSources(third=ThirdSource)
        self.sources.kinds = ("test", "other", "third")

    def running(self, engine: Engine) -> Engine:
        runner = asyncio.ensure_future(engine.run())

        async def stop() -> None:
            engine.close()
            await asyncio.wait_for(engine.wait_closed(), timeout=5)
            await asyncio.wait_for(runner, timeout=5)
        self.addAsyncCleanup(stop)
        return engine

    async def test_a_client_shows_the_first_radio_until_it_chooses_another(self):
        first, second = FakeSource(), OtherSource()
        engine = self.running(Engine(first, more_sources=[second], sources=self.sources))
        self.assertEqual(radios_of(engine), [(1, "test"), (2, "other")])
        subscription = engine.subscribe()
        states = {message["type"]: message for message in ready_messages(subscription) if isinstance(message, dict)}
        self.assertEqual((states["source"]["kind"], states["radios"]["shown"], states["radios"]["max"]),
                         ("test", 1, MAX_RADIOS))
        await frame_from(subscription, FAKE_CENTRE_HZ)
        engine.configure_radios({"id": 2}, subscription)
        states = {message["type"]: message for message in waiting(subscription) if isinstance(message, dict)}
        self.assertEqual({kind for kind in states} >= {"source", "demod", "scanner", "peaks", "recording",
                                                       "calibration", "radios"}, True)
        self.assertEqual((states["source"]["kind"], states["radios"]["shown"]), ("other", 2))
        self.assertEqual(states["demod"]["frequency_hz"], OtherSource.centre_hz + FAKE_RATE_HZ / 4)
        await frame_from(subscription, OtherSource.centre_hz)
        # The first radio, which no client shows, stops reading.
        await asyncio.sleep(0.1)
        reads = first.reads
        await asyncio.sleep(0.2)
        self.assertEqual(first.reads, reads)
        for refused in ({"id": 3}, {"id": "2"}, {"id": True}, {"id": 1, "remove": 2}, {}, {"show": 1}):
            with self.subTest(refused=refused), self.assertRaises(ValueError):
                engine.configure_radios(refused, subscription)
        self.assertIs(subscription.radio, engine.radios[1])

    async def test_each_radio_keeps_its_own_settings_and_clients(self):
        engine = Engine(FakeSource(), more_sources=[OtherSource()], sources=self.sources)
        self.addAsyncCleanup(self.close, engine)
        on_first, on_second = engine.subscribe(), engine.subscribe()
        engine.configure_radios({"id": 2}, on_second)
        for subscription in (on_first, on_second):
            ready_messages(subscription)
        engine.radios[1].configure_demod({"mode": "AM"})
        self.assertEqual(engine.radios[0].demod_state()["mode"], "NFM")
        self.assertEqual(of_type(waiting(on_first), "demod"), [])
        [demod] = of_type(waiting(on_second), "demod")
        self.assertEqual(demod["mode"], "AM")
        # Bookmarks are common to the radios.
        engine.edit_bookmarks({"action": "add", **CALLING})
        for subscription in (on_first, on_second):
            self.assertEqual(len(of_type(waiting(subscription), "bookmarks")), 1)

    async def test_any_client_adds_a_radio_from_the_sources_not_in_use(self):
        engine = self.running(Engine(sources=self.sources))
        subscription, other = engine.subscribe(), engine.subscribe()
        for client in (subscription, other):
            ready_messages(client)
        engine.configure_radios({"add": {"kind": "other"}}, subscription)
        self.assertEqual(radios_of(engine), [(1, "test"), (2, "other")])
        self.assertIs(subscription.radio, engine.radios[1])
        await frame_from(subscription, OtherSource.centre_hz)       # the new radio reads
        # Every client learns of it; the other still shows the first.
        [state] = of_type(waiting(other), "radios")
        self.assertEqual((len(state["radios"]), state["shown"]), (2, 1))
        # A receiver belongs to one radio: the first reads "test".
        with self.assertRaisesRegex(ValueError, "receiver 1 reads this device already"):
            engine.configure_radios({"add": {"kind": "test"}}, subscription)
        with self.assertRaisesRegex(ValueError, "receiver 1 reads this device already"):
            engine.radios[1].configure({"kind": "test"})
        self.assertEqual(engine.radios[1].source.kind, "other")
        for refused in ({"kind": "radio"}, {"kind": 3}, "other", {"device": "0"}):
            with self.subTest(refused=refused), self.assertRaises(ValueError):
                engine.add_radio(refused)
        engine.add_radio({"kind": "third"})
        self.assertEqual(radios_of(engine), [(1, "test"), (2, "other"), (3, "third")])

    async def test_four_radios_at_most(self):
        sources = FakeSources(**{str(index): type(f"Source{index}", (FakeSource,), {"kind": str(index)})
                                 for index in range(5)})
        sources.kinds = ("test", "0", "1", "2", "3", "4")
        engine = Engine(sources=sources)
        self.addAsyncCleanup(self.close, engine)
        for index in range(MAX_RADIOS - 1):
            engine.add_radio({"kind": str(index)})
        with self.assertRaisesRegex(ValueError, "4 receivers at most"):
            engine.add_radio({"kind": "4"})
        self.assertEqual(len(engine.radios), MAX_RADIOS)
        with self.assertRaisesRegex(ValueError, "4 radios at most"):
            Engine(FakeSource(), more_sources=[OtherSource() for _ in range(MAX_RADIOS)])
        with self.assertRaisesRegex(ValueError, "receiver 2 reads this device already"):
            Engine(FakeSource(), more_sources=[OtherSource(), OtherSource()])
        # Without sources to open, a client adds none.
        fixed = Engine(FakeSource())
        self.addAsyncCleanup(self.close, fixed)
        with self.assertRaisesRegex(ValueError, "reads one receiver"):
            fixed.add_radio({"kind": "test"})

    async def test_removing_a_radio_moves_its_clients_to_the_first_and_releases_its_receiver(self):
        engine = self.running(Engine(sources=self.sources))
        subscription = engine.subscribe()
        engine.configure_radios({"add": {"kind": "other"}}, subscription)
        added = engine.radios[1]
        await frame_from(subscription, OtherSource.centre_hz)
        engine.radios[1].configure_demod({"mode": "AM"})
        engine.configure_radios({"remove": 2}, subscription)
        self.assertEqual(radios_of(engine), [(1, "test")])
        self.assertIs(subscription.radio, engine.radios[0])
        self.assertTrue(added.source.closed)
        await frame_from(subscription, FAKE_CENTRE_HZ)
        with self.assertRaisesRegex(ValueError, "the first receiver stays"):
            engine.configure_radios({"remove": 1}, subscription)
        with self.assertRaisesRegex(ValueError, "no such receiver"):
            engine.configure_radios({"remove": 2}, subscription)
        # Its receiver comes back with its settings, on a new radio.
        engine.configure_radios({"add": {"kind": "other"}}, subscription)
        self.assertEqual([radio.id for radio in engine.radios], [1, 3])
        self.assertEqual(engine.radios[1].demod_state()["mode"], "AM")

    @staticmethod
    async def close(engine: Engine) -> None:
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)


class SeveralRadiosPersistenceTest(unittest.IsolatedAsyncioTestCase):
    """The radios used last come back at start, unless sources are given."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"
        self.sources = FakeSources(third=ThirdSource)
        self.sources.kinds = ("test", "other", "third")

    def read(self) -> dict:
        return json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))

    async def closed(self, engine: Engine) -> None:
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)

    async def test_the_radios_used_last_come_back_with_their_scanner_settings(self):
        engine = Engine(sources=self.sources, store=Store(self.directory))
        engine.add_radio({"kind": "third"})
        engine.radios[1].configure_scanner({"threshold_db": 20.0})
        engine.radios[1].configure_demod({"mode": "AM"})
        await self.closed(engine)
        saved = self.read()
        self.assertEqual(saved["source"], {"kind": "test"})
        # Each radio's receiver as it stood, not the first radio's alone.
        self.assertEqual(saved["receivers"][engine.radios[1].source.settings_key]["demod"]["mode"], "AM")
        self.assertEqual([radio["source"] for radio in saved["radios"]], [{"kind": "third"}])
        self.assertEqual(saved["radios"][0]["scanner"]["threshold_db"], 20.0)
        again = Engine(sources=self.sources, store=Store(self.directory))
        self.addAsyncCleanup(self.closed, again)
        self.assertEqual(radios_of(again), [(1, "test"), (2, "third")])
        self.assertEqual(again.radios[1].scanner_state()["threshold_db"], 20.0)
        self.assertEqual(again.radios[1].demod_state()["mode"], "AM")
        self.assertNotEqual(again.radios[0].scanner_state()["threshold_db"], 20.0)

    async def test_sources_given_at_start_replace_them(self):
        engine = Engine(sources=self.sources, store=Store(self.directory))
        engine.add_radio({"kind": "third"})
        await self.closed(engine)
        given = Engine(OtherSource(), sources=self.sources, store=Store(self.directory))
        await self.closed(given)
        self.assertEqual(radios_of(given), [(1, "other")])
        self.assertNotIn("radios", self.read())
        more = Engine(FakeSource(), more_sources=[ThirdSource()], sources=self.sources,
                      store=Store(self.directory))
        await self.closed(more)
        self.assertEqual([radio["source"] for radio in self.read()["radios"]], [{"kind": "third"}])

    async def test_saved_radios_that_cannot_open_are_left_out(self):
        self.directory.mkdir()
        (self.directory / SETTINGS_FILE).write_text(json.dumps({"version": 1, "source": {"kind": "test"}, "radios": [
            {"source": {"kind": "lost"}}, "radio", {"source": {"kind": "test"}}, {"source": {"kind": "other"}},
            {"source": {"kind": "third"}}, {"source": {"kind": "other"}}]}), encoding="utf-8")
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine = Engine(sources=self.sources, store=Store(self.directory))
        self.addAsyncCleanup(self.closed, engine)
        # Those that cannot open, or read a receiver in use, are left out.
        self.assertEqual(radios_of(engine), [(1, "test"), (2, "other"), (3, "third")])
        self.assertEqual(len(logs.output), 4)


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
        self.assertFalse(engine.radios[0].state()["dc_removal"])
        self.assertEqual({key: engine.radios[0].demod_state()[key] for key in demod}, demod)
        self.assertIs(engine.radios[0].demod_state()["notch"], False)    # saved before the notch: off
        self.assertIs(engine.radios[0].demod_state()["blanker"], False)  # and before the blanker
        self.assertEqual(engine.bookmarks_state(), {"type": "bookmarks", "bookmarks": [{"id": 1, **CALLING, "category": None}],
                                                    "categories": []})
        self.assertEqual(engine.preferences_state(),
                         {"type": "preferences", "values": {"volume": 40, "palette": "classic"}})
        self.assertEqual(engine.storage_state(),
                         {"type": "storage", "directory": str(self.directory), "saving": True, "problem": ""})

    async def test_sam_and_a_bookmark_in_sam_come_back_after_a_restart(self):
        engine = self.engine(save_delay_s=0.01)
        engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "mode": "SAM", "bandwidth_hz": 8_000})
        bookmark = {"frequency_hz": 145_020_000.0, "name": "Synchronous", "mode": "SAM", "scan": False}
        engine.edit_bookmarks({"action": "add", **bookmark})
        await self.stop(engine)
        demod = self.read(SETTINGS_FILE)["receivers"]["test"]["demod"]
        self.assertEqual((demod["mode"], demod["bandwidth_hz"]), ("SAM", 8_000.0))
        self.assertNotIn("sam_locked", demod)      # a state, not a setting
        again = self.engine()
        state = again.radios[0].demod_state()
        self.assertEqual((state["frequency_hz"], state["mode"], state["bandwidth_hz"], state["sam_locked"]),
                         (145_010_000.0, "SAM", 8_000.0, False))
        self.assertEqual(again.bookmarks_state()["bookmarks"], [{"id": 1, **bookmark, "category": None}])

    async def test_bookmark_categories_come_back_after_a_restart(self):
        engine = self.engine(save_delay_s=0.01)
        engine.edit_bookmarks({"action": "add", **CALLING})
        engine.edit_bookmarks({"action": "add_category", "name": "Local"})
        engine.edit_bookmarks({"action": "change", "ids": [1], "category": 1})
        await self.stop(engine)
        self.assertEqual(self.read(BOOKMARKS_FILE), {"version": 1, "bookmarks": [CALLING],
                                                     "categories": [{"name": "Local", "bookmarks": [0]}]})
        again = self.engine()
        self.assertEqual(again.bookmarks_state(), {"type": "bookmarks", "bookmarks": [{"id": 1, **CALLING, "category": 1}],
                                                   "categories": [{"id": 1, "name": "Local"}]})

    async def test_the_notch_comes_back_after_a_restart_and_no_bookmark_carries_it(self):
        engine = self.engine(save_delay_s=0.01)
        engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "mode": "USB", "notch": True})
        bookmark = {"frequency_hz": 145_020_000.0, "name": "Whistles", "mode": "USB", "scan": False}
        engine.edit_bookmarks({"action": "add", **bookmark})
        await self.stop(engine)
        demod = self.read(SETTINGS_FILE)["receivers"]["test"]["demod"]
        self.assertIs(demod["notch"], True)
        self.assertNotIn("notch_lines", demod)      # a state, not a setting
        again = self.engine()
        state = again.radios[0].demod_state()
        self.assertEqual((state["mode"], state["notch"], state["notch_lines"]), ("USB", True, 0))
        self.assertEqual(again.bookmarks_state()["bookmarks"], [{"id": 1, **bookmark, "category": None}])
        await self.stop(again)
        # A saved notch that is not true or false is left off.
        self.write(SETTINGS_FILE, {"receivers": {"test": {"demod": {"mode": "AM", "notch": "yes"}}}})
        with self.assertLogs("radiolack", "WARNING") as logs:
            third = self.engine()
        self.assertIn("the saved setting notch is not used: the notch must be true or false", logs.output[0])
        self.assertEqual((third.radios[0].demod_state()["mode"], third.radios[0].demod_state()["notch"]), ("AM", False))

    async def test_the_blanker_comes_back_after_a_restart_and_no_bookmark_carries_it(self):
        engine = self.engine(save_delay_s=0.01)
        engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "mode": "AM", "blanker": True})
        bookmark = {"frequency_hz": 145_020_000.0, "name": "Crackle", "mode": "AM", "scan": False}
        engine.edit_bookmarks({"action": "add", **bookmark})
        await self.stop(engine)
        demod = self.read(SETTINGS_FILE)["receivers"]["test"]["demod"]
        self.assertIs(demod["blanker"], True)
        self.assertNotIn("blanker_impulses", demod)     # a state, not a setting
        again = self.engine()
        state = again.radios[0].demod_state()
        self.assertEqual((state["mode"], state["blanker"], state["blanker_impulses"]), ("AM", True, 0))
        self.assertEqual(again.bookmarks_state()["bookmarks"], [{"id": 1, **bookmark, "category": None}])
        await self.stop(again)
        # A saved blanker that is not true or false is left off.
        self.write(SETTINGS_FILE, {"receivers": {"test": {"demod": {"mode": "AM", "blanker": 1}}}})
        with self.assertLogs("radiolack", "WARNING") as logs:
            third = self.engine()
        self.assertIn("the saved setting blanker is not used: the noise blanker must be true or false",
                      logs.output[0])
        self.assertEqual((third.radios[0].demod_state()["mode"], third.radios[0].demod_state()["blanker"]), ("AM", False))

    async def test_a_saved_setting_that_is_refused_keeps_its_default(self):
        self.write(SETTINGS_FILE, {"receivers": {"test": {
            "source": {"dc_removal": "no", "centre_hz": 433e6},    # the test source has no centre to set
            "demod": {"frequency_hz": 145_010_000, "mode": "DSB", "squelch_db": None,
                      "bandwidth_hz": 10 ** 400}}},     # a whole number too large for a float
            "interface": {"volume": 40, "nested": {"a": 1}, "": 1, "text": "x" * 101}})
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine = self.engine()
        self.assertEqual(len(logs.output), 5)   # DC removal, mode, squelch, filter width and some preferences
        self.assertTrue(engine.radios[0].state()["dc_removal"])
        state = engine.radios[0].demod_state()
        self.assertEqual((state["frequency_hz"], state["mode"], state["bandwidth_hz"], state["squelch_db"]),
                         (145_010_000.0, "NFM", 10_000.0, -80.0))
        self.assertEqual(engine.preferences_state()["values"], {"volume": 40})

    async def test_saves_changes_a_moment_later_and_keeps_the_other_receivers(self):
        other = {"source": {"ppm": -4, "gain_db": 40.2}, "demod": {"mode": "NFM"}}
        self.write(SETTINGS_FILE, {"receivers": {"rtl_sdr 0": other}})
        engine = self.engine(save_delay_s=0.05)
        engine.radios[0].configure({"dc_removal": False})
        engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "mode": "USB"})
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
        engine.radios[0].configure_demod({"mode": "CW"})
        engine.edit_bookmarks({"action": "add", **CALLING})
        self.assertFalse(self.directory.exists())
        await self.stop(engine)
        self.assertEqual(self.read(SETTINGS_FILE)["receivers"]["test"]["demod"]["mode"], "CW")
        self.assertEqual(self.read(BOOKMARKS_FILE)["bookmarks"], [CALLING])
        again = self.engine()
        self.assertEqual((again.radios[0].demod_state()["mode"], len(again.bookmarks_state()["bookmarks"])), ("CW", 1))

    async def test_nothing_is_written_until_something_changes(self):
        self.write(SETTINGS_FILE, {"receivers": {"test": {"source": {"dc_removal": True}}}})
        before = (self.directory / SETTINGS_FILE).read_bytes()
        engine = self.engine(save_delay_s=0.01)
        engine.radios[0].configure({"dc_removal": True})     # the same value
        await self.stop(engine)
        self.assertEqual((self.directory / SETTINGS_FILE).read_bytes(), before)
        self.assertFalse((self.directory / BOOKMARKS_FILE).exists())

    # The engine does not run in these tests: the rtl_tcp sources never read,
    # so nothing connects to their addresses.

    async def test_rtl_tcp_keeps_its_settings_and_the_address_used_last_whatever_the_address(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000", "ppm": -4}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        self.assertEqual((engine.radios[0].state()["address"], engine.radios[0].state()["ppm"]), ("192.0.2.10:40000", -4))
        engine.radios[0].configure({"address": "192.0.2.20"})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["address"] == "192.0.2.20:1234")
        self.assertEqual(settings["receivers"]["rtl_tcp"]["source"]["ppm"], -4)

    async def test_an_rtl_tcp_receiver_keeps_its_correction_to_the_hundredth(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"ppm": 37.42}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        self.assertEqual(engine.radios[0].state()["ppm"], 37.42)
        engine.radios[0].configure({"ppm": -52.73})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["ppm"] == -52.73)
        self.assertEqual(settings["receivers"]["rtl_tcp"]["source"]["centre_hz"], 145e6)    # as asked

    async def test_a_saved_correction_that_is_not_valid_is_not_used(self):
        for value in ("5", True, None, 100.5, float("nan")):
            with self.subTest(value=value):
                self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"ppm": value, "centre_hz": 433e6}}}})
                with self.assertLogs("radiolack", "WARNING"):
                    engine = self.engine(RtlTcpSource())
                self.assertEqual((engine.radios[0].state()["ppm"], engine.radios[0].state()["centre_hz"]), (0, 433e6))
                await self.stop(engine)

    async def test_an_rtl_tcp_receiver_keeps_its_shortwave_input(self):
        saved = {"centre_hz": 7_100_000.0, "hf_input": "q", "rtl_agc": True}    # in any order
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": saved}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        state = engine.radios[0].state()
        self.assertEqual((state["hf_input"], state["centre_hz"], state["rtl_agc"]), ("q", 7.1e6, True))
        engine.radios[0].configure({"hf_input": "i"})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["hf_input"] == "i")
        source = settings["receivers"]["rtl_tcp"]["source"]
        self.assertEqual((source["centre_hz"], source["rtl_agc"]), (7.1e6, True))
        self.assertEqual(list(source)[:5], ["address", "hf_input", "converter_lo_hz", "converter", "sample_rate_hz"])

    async def test_an_rtl_tcp_receiver_keeps_its_converter_with_its_centre_frequency(self):
        # One at a time, the converter would first meet the default 145 MHz: -45 MHz on the air.
        saved = {"centre_hz": 50e6, "converter": "down_inverted", "converter_lo_hz": 100e6}
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": saved, "demod": {"frequency_hz": 50_100_000}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        state = engine.radios[0].state()
        self.assertEqual((state["converter"], state["converter_lo_hz"], state["centre_hz"]),
                         ("down_inverted", 100e6, 50e6))
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], 50_100_000)      # as saved
        engine.radios[0].configure({"ppm": 1})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"].get("ppm") == 1)
        source = settings["receivers"]["rtl_tcp"]["source"]
        self.assertEqual({name: source[name] for name in saved}, saved)

    async def test_a_receiver_saved_before_the_converter_has_none(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"centre_hz": 433e6, "ppm": -4}}}})
        engine = self.engine(RtlTcpSource())
        state = engine.radios[0].state()
        self.assertEqual((state["converter"], state["converter_lo_hz"], state["centre_hz"], state["ppm"]),
                         ("none", 125e6, 433e6, -4))

    async def test_a_saved_converter_that_is_refused_leaves_the_other_settings(self):
        saved = {"converter": "sideways", "converter_lo_hz": 0, "centre_hz": 433e6, "ppm": -4}
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": saved}}})
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine = self.engine(RtlTcpSource())
        state = engine.radios[0].state()
        self.assertEqual((state["converter"], state["converter_lo_hz"], state["centre_hz"], state["ppm"]),
                         ("none", 125e6, 433e6, -4))
        self.assertEqual(len(logs.output), 2)       # the converter and its oscillator

    async def test_the_antenna_power_is_never_saved_nor_restored(self):
        # A hand-written file cannot turn it on either.
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"bias_tee": True, "ppm": -4}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        self.assertEqual((engine.radios[0].state()["bias_tee"], engine.radios[0].state()["ppm"]), (False, -4))
        engine.radios[0].power_antenna()
        self.assertTrue(engine.radios[0].state()["bias_tee"])
        engine.radios[0].configure({"ppm": 2})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["ppm"] == 2)
        self.assertNotIn("bias_tee", settings["receivers"]["rtl_tcp"]["source"])

    async def test_a_receiver_keeps_its_driver_s_computation_but_never_the_grid(self):
        # The grid stays on at every start: a hand-written file cannot turn it off.
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"driver_pll": "truncating",
                                                                         "tuner_grid": False}}}})
        engine = self.engine(RtlTcpSource(), save_delay_s=0.05)
        self.assertEqual((engine.radios[0].state()["driver_pll"], engine.radios[0].state()["tuner_grid"]), ("truncating", True))
        engine.radios[0].configure({"driver_pll": "rounding", "tuner_grid": False})
        settings = await self.saved(
            SETTINGS_FILE, lambda content: content["receivers"]["rtl_tcp"]["source"]["driver_pll"] == "rounding")
        self.assertNotIn("tuner_grid", settings["receivers"]["rtl_tcp"]["source"])
        await self.stop(engine)
        engine = self.engine(RtlTcpSource())
        self.assertEqual((engine.radios[0].state()["driver_pll"], engine.radios[0].state()["tuner_grid"]), ("rounding", True))

    async def test_a_saved_driver_s_computation_that_is_refused_stays_unknown(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"driver_pll": "2.0.2", "ppm": -4}}}})
        with self.assertLogs("radiolack", "WARNING"):
            engine = self.engine(RtlTcpSource())
        self.assertEqual((engine.radios[0].state()["driver_pll"], engine.radios[0].state()["ppm"]), ("unknown", -4))

    async def test_a_source_without_antenna_power_refuses_to_turn_it_on(self):
        engine = self.engine()
        with self.assertRaisesRegex(ValueError, "^the test source has no antenna power$"):
            engine.radios[0].power_antenna()

    async def test_an_address_given_at_start_replaces_the_saved_one(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000", "ppm": -4}}}})
        engine = self.engine(RtlTcpSource("192.0.2.20", 40000), save_delay_s=60)
        self.assertEqual((engine.radios[0].state()["address"], engine.radios[0].state()["ppm"]), ("192.0.2.20:40000", -4))
        await self.stop(engine)     # nothing changed meanwhile, but the given address replaces the saved one
        self.assertEqual(self.read(SETTINGS_FILE)["receivers"]["rtl_tcp"]["source"]["address"], "192.0.2.20:40000")

    async def test_an_address_given_as_saved_writes_nothing(self):
        self.write(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": "192.0.2.10:40000"}}}})
        before = (self.directory / SETTINGS_FILE).read_bytes()
        await self.stop(self.engine(RtlTcpSource("192.0.2.10", 40000)))
        self.assertEqual((self.directory / SETTINGS_FILE).read_bytes(), before)

    async def test_opens_the_source_used_last(self):
        self.write(SETTINGS_FILE, {"source": {"kind": "other"}})
        self.assertEqual(self.engine(sources=FakeSources()).radios[0].source.kind, "other")

    async def test_a_saved_source_that_cannot_open_gives_way_to_the_first_kind(self):
        self.write(SETTINGS_FILE, {"source": {"kind": "gone"}})
        with self.assertLogs("radiolack", "WARNING"):
            engine = self.engine(sources=FakeSources())
        self.assertEqual(engine.radios[0].source.kind, "test")
        await self.stop(engine)
        self.assertEqual(self.read(SETTINGS_FILE)["source"], {"kind": "test"})

    async def test_the_source_in_use_opens_at_the_next_start(self):
        engine = self.engine(sources=FakeSources(), save_delay_s=0.05)
        engine.radios[0].configure({"kind": "other"})
        await self.saved(SETTINGS_FILE, lambda content: content.get("source") == {"kind": "other"})
        await self.stop(engine)
        self.assertEqual(self.engine(sources=FakeSources()).radios[0].source.kind, "other")

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
            engine.radios[0].configure_demod({"mode": "AM"})
            state = await asyncio.wait_for(storage_state(), timeout=5)
        self.assertEqual((state["saving"], state["problem"]),
                         (False, "settings.json cannot be saved: No space left on device."))
        engine.radios[0].configure_demod({"mode": "CW"})
        state = await asyncio.wait_for(storage_state(), timeout=5)
        self.assertEqual((state["saving"], state["problem"]), (True, ""))

    async def test_bookmark_changes_reach_every_client(self):
        engine = self.engine()
        subscriptions = [engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            waiting(subscription)
        engine.edit_bookmarks({"action": "add", **CALLING})
        for subscription in subscriptions:
            self.assertEqual(subscription.get_nowait(), {
                "type": "bookmarks", "bookmarks": [{"id": 1, **CALLING, "category": None}], "categories": []})
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
                with self.assertLogs("radiolack", "WARNING"):
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
        with self.assertLogs("radiolack", "ERROR"):
            engine.check_updates()
            state = await self.settled(engine)
        self.assertEqual((state["status"], state["detail"]), ("failed", "The check stopped on an error."))

    async def test_a_failed_check_is_logged(self):
        engine = self.engine(FakeUpdates(UpdateResult("failed", detail="No answer within 30 s.")), run=False)
        with self.assertLogs("radiolack", "WARNING") as logs:
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


class BandPlanTest(unittest.IsolatedAsyncioTestCase):
    """The band plan in use: Europe's unless chosen otherwise, shared by every
    client and saved, with a second plan, ``test``, beside Europe's, whose
    WFM de-emphasis is 75 us and whose 2 m band takes 25 kHz channels."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "radiolack")
        web = Path(temporary.name) / "web"
        web.mkdir()
        (web / "bandplan-europe.json").write_bytes(bandplan.plan_path("europe").read_bytes())
        (web / "bandplan-test.json").write_text(json.dumps({
            "format": 2, "name": "Test plan", "wfm_deemphasis_us": 75, "types": {"amateur": "#123456"},
            "bands": [{"from": 144_000_000, "to": 146_000_000, "name": "2 m", "type": "amateur", "mode": "NFM",
                       "step": 25_000}],
            "channels": [], "spots": []}), encoding="utf-8")
        for patch in (mock.patch.object(bandplan, "WEB_DIR", web),
                      mock.patch.object(bandplan, "PLANS", ("europe", "test"))):
            patch.start()
            self.addCleanup(patch.stop)
        bandplan.load_plan.cache_clear()
        self.addCleanup(bandplan.load_plan.cache_clear)

    def engine(self) -> Engine:
        engine = Engine(FakeSource(), store=self.store, save_delay_s=0.01)
        self.addAsyncCleanup(self.stop, engine)
        return engine

    @staticmethod
    async def stop(engine: Engine) -> None:
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), timeout=5)

    async def test_a_new_plan_reaches_every_client_and_is_saved(self):
        engine = self.engine()
        self.assertEqual((engine.band_plan_state(), engine.plan.wfm_deemphasis_s),
                         ({"type": "band_plan", "plan": "europe"}, 50e-6))
        self.assertEqual(engine.plan.channel(145_013_000, 600), 145_012_500)     # Europe's 12.5 kHz channel
        subscriptions = [engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            waiting(subscription)
        engine.configure_band_plan({"plan": "test"})
        for subscription in subscriptions:
            # The signals found show on the new plan's channels.
            self.assertEqual([message["type"] for message in waiting(subscription)], ["band_plan", "peaks"])
        self.assertEqual(engine.band_plan_state(), {"type": "band_plan", "plan": "test"})
        self.assertEqual((engine.plan.name, engine.plan.wfm_deemphasis_s), ("Test plan", 75e-6))
        self.assertEqual(engine.plan.channel(145_013_000, 600), 145_013_000)     # 12 kHz off its channels
        engine.configure_band_plan({"plan": "test"})     # the plan in use: nothing changes
        self.assertEqual(waiting(subscriptions[0]), [])
        deadline = time.monotonic() + 5
        while self.store.load(SETTINGS_FILE).get("band_plan") != "test":
            self.assertLess(time.monotonic(), deadline, "the plan was not saved")
            await asyncio.sleep(0.01)
        self.assertEqual(self.engine().band_plan_state()["plan"], "test")

    async def test_the_demodulator_takes_the_plan_s_wfm_de_emphasis(self):
        engine = self.engine()
        given: list[float] = []
        process = Demodulator.process

        def spy(demodulator, *args, **kwargs):
            given.append(args[8])       # after the taps, the de-emphasis
            return process(demodulator, *args, **kwargs)

        with mock.patch.object(Demodulator, "process", spy):
            task = asyncio.ensure_future(engine.run())
            self.addAsyncCleanup(asyncio.gather, task, return_exceptions=True)
            engine.subscribe()
            deadline = time.monotonic() + 5
            while not given:
                self.assertLess(time.monotonic(), deadline, "no block was processed")
                await asyncio.sleep(0.01)
            engine.configure_band_plan({"plan": "test"})
            given.clear()
            while len(given) < 3:
                self.assertLess(time.monotonic(), deadline, "too few blocks were processed")
                await asyncio.sleep(0.01)
        self.assertEqual(given[-1], 75e-6)

    async def test_refused_changes_change_nothing(self):
        engine = self.engine()
        subscription = engine.subscribe()
        waiting(subscription)
        for changes in ({}, {"plan": "mars"}, {"plan": None}, {"plan": "test", "region": 2}, {"region": "test"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_band_plan(changes)
        self.assertEqual((engine.band_plan_state()["plan"], waiting(subscription)), ("europe", []))

    async def test_a_saved_plan_that_is_not_offered_gives_europe_s(self):
        for saved in ("mars", 3, ["test"]):
            with self.subTest(saved=saved):
                self.store.save(SETTINGS_FILE, {"band_plan": saved})
                with self.assertLogs("radiolack", "WARNING"):
                    engine = self.engine()
                self.assertEqual(engine.band_plan_state()["plan"], "europe")


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
        engine.radios[0].configure({"dc_removal": False})     # the radio's states reach both
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
        self.assertEqual(set(engine.radios[0].state()["settable"]) - set(engine.radios[0].state(remote=True)["settable"]), {"address"})
        self.assertEqual(engine.radios[0].state(remote=True)["address"], engine.radios[0].state()["address"])


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
            # 5 kHz from the 2 m band's nearest 12.5 kHz channel: to the 100 Hz.
            self.assertEqual(found["channel_hz"], round(found["frequency_hz"] / 100) * 100)

    async def test_scanner_changes_reach_every_client_and_are_checked(self):
        engine = self.engine(run=False)
        subscriptions = [engine.subscribe(), engine.subscribe(device="5f0c2a91")]
        for subscription in subscriptions:
            waiting(subscription)
        engine.radios[0].configure_scanner({"threshold_db": 20, "detection": True, "resume_s": 0, "listen_s": 30})
        engine.radios[0].configure_scanner({"sweep_high_hz": 1300e6, "sweep_window_s": 2})
        engine.radios[0].configure_scanner({"sweep_low_hz": 1240e6})
        changed = scanner_state(threshold_db=20.0, resume_s=0.0, listen_s=30.0, sweep_low_hz=1240e6,
                                sweep_high_hz=1300e6, sweep_window_s=2.0)
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription)[-1], changed)
        refused = ({}, {"threshold": 20}, {"detection": "on"}, {"detection": None}, {"threshold_db": 5},
                   {"threshold_db": 51}, {"threshold_db": "12"}, {"threshold_db": float("nan")},
                   {"resume_s": -0.5}, {"resume_s": 8.5}, {"resume_s": True}, {"resume_s": "2"},
                   {"listen_s": 1}, {"listen_s": 61}, {"listen_s": None}, {"listen_s": "10"},
                   {"sweep_low_hz": 1300e6}, {"sweep_high_hz": 1200e6}, {"sweep_low_hz": 0},
                   {"sweep_high_hz": "1300 MHz"}, {"sweep_low_hz": None}, {"sweep_window_s": 0.2},
                   {"sweep_window_s": 2.5}, {"sweep_window_s": float("inf")}, {"sweep_window_s": False},
                   {"sweep_listen": 1}, {"sweep_listen": None}, {"sweep_passes": 0}, {"sweep_passes": 100},
                   {"sweep_passes": 2.5}, {"sweep_passes": True}, {"sweep_passes": "3"},
                   {"sweep_low_hz": 1250e6, "threshold_db": 60},
                   {"ignore": 145e6, "restore": True}, {"ignore": 145e6, "detection": False},
                   {"ignore": "145 MHz"}, {"ignore": 0}, {"restore": False}, {"restore": 1},
                   {"scan": "panorama"}, {"scan": True}, {"scan": "memory", "resume_s": 2},
                   {"scan": "signals", "listen_s": 10},
                   {"scan": "memory"},      # no ticked bookmark in the span
                   {"scan": "sweep"})       # a source that does not tune
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.radios[0].configure_scanner(changes)
        self.assertEqual(engine.radios[0].scanner_state(), changed)
        for subscription in subscriptions:
            self.assertEqual(waiting(subscription), [])

    async def test_an_ignored_frequency_leaves_the_list_at_once_and_stays_out(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        [found] = await next_peaks(subscription, until=bool)
        engine.radios[0].configure_scanner({"ignore": found["frequency_hz"] + 500})
        self.assertEqual(kinds_of(waiting(subscription)), ["scanner", "peaks"])
        self.assertEqual(engine.radios[0].scanner_state()["ignored_hz"], [found["frequency_hz"] + 500])
        self.assertEqual(engine.radios[0].peaks_state()["peaks"], [])
        reads = source.reads
        while source.reads < reads + 30:    # three searches
            await asyncio.sleep(0.01)
        self.assertEqual(engine.radios[0].peaks_state()["peaks"], [])
        engine.radios[0].configure_scanner({"restore": True})
        self.assertEqual(kinds_of(waiting(subscription)), ["scanner"])     # the list is the same, until found again
        await next_peaks(subscription, until=bool)

    async def test_detection_turned_off_empties_the_list_and_finds_nothing(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        await next_peaks(subscription, until=bool)
        engine.radios[0].configure_scanner({"detection": False})
        self.assertEqual(waiting(subscription)[:2], [scanner_state(detection=False), {"type": "peaks", "peaks": []}])
        reads = source.reads
        while source.reads < reads + 30:
            await asyncio.sleep(0.01)
        self.assertNotIn("peaks", kinds_of(waiting(subscription)))
        engine.radios[0].configure_scanner({"detection": True})
        await next_peaks(subscription, until=bool)

    async def test_another_receiver_starts_an_empty_list(self):
        engine = self.engine(sources=FakeSources())
        subscription = engine.subscribe()
        await next_peaks(subscription, until=bool)
        engine.radios[0].configure({"kind": "other"})
        self.assertEqual(engine.radios[0].peaks_state()["peaks"], [])
        self.assertIn("peaks", kinds_of(waiting(subscription)))
        [found] = await next_peaks(subscription, until=bool)    # the other source's tone
        self.assertAlmostEqual(found["frequency_hz"], FAKE_TONE_HZ + 1e6, delta=FAKE_RATE_HZ / 8192)

    async def test_the_list_goes_out_at_most_once_an_interval(self):
        engine = self.engine(run=False)
        subscription = engine.subscribe()
        waiting(subscription)
        with mock.patch("radiolack.radio.PEAKS_INTERVAL_S", 0.2):
            for _ in range(3):
                engine.radios[0]._peaks_changed()
            self.assertEqual(kinds_of(waiting(subscription)), ["peaks"])
            started = time.monotonic()
            self.assertEqual((await next_message(subscription))["type"], "peaks")     # the next two, together
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
            engine.radios[0]._peaks_changed(at_once=True)      # after an interface's change
            self.assertEqual(kinds_of(waiting(subscription)), ["peaks"])
            engine.radios[0]._peaks_changed()
            engine.close()
            self.assertIsNone(engine.radios[0]._peaks_timer)

    async def test_the_scanner_settings_are_saved_with_the_others(self):
        engine = self.engine(run=False, store=Store(self.directory), save_delay_s=0.01)
        engine.radios[0].configure_scanner({"detection": False, "threshold_db": 18.5, "resume_s": 4.5, "listen_s": 25.5})
        engine.radios[0].configure_scanner({"sweep_low_hz": 118e6, "sweep_high_hz": 137_000_000.4, "sweep_window_s": 1.25,
                                  "sweep_listen": True, "sweep_passes": 3.0})
        engine.radios[0].configure_scanner({"ignore": 145_500_000.4})
        engine.radios[0].configure_scanner({"ignore": 144_800_000})
        engine.close()
        await engine.wait_closed()
        saved = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["scanner"], {"detection": False, "threshold_db": 18.5, "resume_s": 4.5,
                                            "listen_s": 25.5, "sweep_low_hz": 118e6, "sweep_high_hz": 137e6,
                                            "sweep_window_s": 1.25, "sweep_listen": True, "sweep_passes": 3,
                                            "ignored_hz": [144_800_000.0, 145_500_000.0]})
        again = self.engine(run=False, store=Store(self.directory))
        self.assertEqual(again.radios[0].scanner_state(), scanner_state(
            detection=False, threshold_db=18.5, resume_s=4.5, listen_s=25.5, sweep_low_hz=118e6,
            sweep_high_hz=137e6, sweep_window_s=1.25, sweep_listen=True, sweep_passes=3,
            ignored_hz=[144_800_000.0, 145_500_000.0]))

    async def test_saved_scanner_settings_that_are_not_valid_keep_their_defaults(self):
        self.directory.mkdir()
        settings = self.directory / SETTINGS_FILE
        for saved, expected in (
                ({"detection": "yes", "threshold_db": 99, "resume_s": -1, "listen_s": 0, "sweep_low_hz": 440e6,
                  "sweep_high_hz": 430e6, "sweep_window_s": 0.1, "sweep_listen": "yes", "sweep_passes": 0,
                  "ignored_hz": [145e6, "x", 0, 10 ** 400, 145e6 + 100, 144e6]},
                 (True, 12.0, [144e6, 145e6])),
                ({"sweep_low_hz": 118e6, "sweep_window_s": True, "listen_s": "10", "sweep_passes": 2.5,
                  "ignored_hz": {"145e6": True}},
                 (True, 12.0, [])),
                ([1], (True, 12.0, [])),
                ({"ignored_hz": [144e6 + n * 10_000 for n in range(250)]}, (True, 12.0, None))):
            with self.subTest(saved=saved):
                settings.write_text(json.dumps({"version": 1, "scanner": saved}), encoding="utf-8")
                with self.assertLogs("radiolack", "WARNING"):
                    engine = Engine(FakeSource(), store=Store(self.directory))
                state = engine.radios[0].scanner_state()
                engine.close()
                detection, threshold_db, ignored = expected
                self.assertEqual((state["detection"], state["threshold_db"], state["resume_s"], state["listen_s"],
                                  state["sweep_low_hz"], state["sweep_high_hz"], state["sweep_window_s"],
                                  state["sweep_listen"], state["sweep_passes"]),
                                 (detection, threshold_db, 2.0, 10.0, 430e6, 440e6, 0.5, False, None))
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
        self.assertEqual(engine.radios[0].scanner_state()["memory"], {"watched": [weak, strong, quiet], "windows": 1,
                                                            "window": None, "state": None, "bookmark_id": None})
        engine.radios[0].configure_scanner({"scan": "memory"})
        for subscription in (local, remote):
            state = await next_scanner(subscription, until=in_state("listening"))
            self.assertEqual((state["scan"], state["stopped"], state["memory"]["bookmark_id"]),
                             ("memory", None, strong))
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ + 40_000, "NFM", 10_000.0))
        self.assertNotIn("scan", engine._contents()[SETTINGS_FILE]["scanner"])     # never saved

    async def test_waits_the_resume_time_then_watches_again(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(source)
        subscription = engine.subscribe()
        first, second = self.bookmarks(engine, [(40_000, "NFM", True), (-60_000, "AM", True)])
        engine.radios[0].configure_scanner({"resume_s": 0.5})
        engine.radios[0].configure_scanner({"scan": "memory"})
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
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ - 60_000, "AM", 8_000.0))

    async def test_a_manual_squelch_decides_which_bookmarks_are_active(self):
        engine = self.engine(TonesSource({-60_000: 0.03}))     # about -30 dBFS
        subscription = engine.subscribe()
        [bookmark] = self.bookmarks(engine, [(-60_000, "AM", True)])
        engine.radios[0].configure_demod({"squelch_auto": False, "squelch_db": -25.0})
        engine.radios[0].configure_scanner({"scan": "memory"})
        await asyncio.sleep(0.5)
        self.assertEqual(engine.radios[0].scanner_state()["memory"]["state"], "watching")
        engine.radios[0].configure_demod({"squelch_db": -35.0})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], bookmark)

    async def test_tuning_by_hand_another_source_or_no_bookmark_left_stops_it(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()
        [bookmark] = self.bookmarks(engine, [(40_000, "NFM", True)])

        async def listening() -> None:
            engine.radios[0].configure_scanner({"scan": "memory"})
            await next_scanner(subscription, until=in_state("listening"))

        await listening()
        engine.radios[0].configure_demod({"mode": "AM", "bandwidth_hz": 6_000, "squelch_db": -70})
        engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 40_000})     # where it is
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "memory")
        stops = (
            (lambda: engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 45_000}), "tuned"),
            (lambda: engine.radios[0].configure({"centre_hz": FAKE_CENTRE_HZ + 1_000}), "tuned"),
            (lambda: engine.edit_bookmarks({"action": "change", "id": bookmark, "scan": False}), "bookmarks"),
            (lambda: engine.radios[0].configure({"kind": "other"}), "source"),
        )
        for stop, reason in stops:
            with self.subTest(reason=reason):
                if engine.radios[0].scanner_state()["memory"]["watched"]:
                    await listening()
                # The output tells of a stop for want of bookmarks.
                logs = (self.assertLogs("radiolack", "WARNING") if reason == "bookmarks"
                        else contextlib.nullcontext())
                with logs:
                    stop()
                state = engine.radios[0].scanner_state()
                self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"], state["memory"]["state"]),
                                 (None, reason, "memory", None))
                if reason == "bookmarks":
                    self.assertEqual(state["memory"]["watched"], [])
                    engine.edit_bookmarks({"action": "change", "id": bookmark, "scan": True})
                    engine.radios[0].configure({"centre_hz": FAKE_CENTRE_HZ})

    async def test_goes_on_when_the_span_stays_or_another_source_does_not_open(self):
        source = TonesSource({40_000: 0.1})

        def unavailable():
            raise ValueError("no such receiver")

        engine = self.engine(sources=FakeSources(test=lambda: source, other=unavailable))
        subscription = engine.subscribe()
        self.bookmarks(engine, [(40_000, "NFM", True)])
        engine.radios[0].configure_scanner({"scan": "memory"})
        await next_scanner(subscription, until=in_state("listening"))
        engine.radios[0].configure({"centre_hz": FAKE_CENTRE_HZ})       # where it is
        with self.assertLogs("radiolack", "WARNING"), self.assertRaises(ValueError):
            engine.radios[0].configure({"kind": "other"})
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "memory")

    async def test_goes_on_without_any_client_until_stopped(self):
        source = TonesSource({40_000: 0.1})
        engine = self.engine(source)
        subscription = engine.subscribe()
        self.bookmarks(engine, [(40_000, "NFM", True)])
        engine.radios[0].configure_scanner({"scan": "memory"})
        engine.unsubscribe(subscription)
        reads = source.reads
        while source.reads < reads + 20:
            await asyncio.sleep(0.01)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], engine.radios[0].scanner_state()["stopped"]), (None, None))
        await asyncio.sleep(0.05)
        reads = source.reads
        await asyncio.sleep(0.1)
        self.assertLessEqual(source.reads, reads + 1)


CHANNEL_A_HZ = FAKE_CENTRE_HZ + 37_500      # 145.0375 MHz and 144.9375 MHz: 12.5 kHz channels of the 2 m band,
CHANNEL_B_HZ = FAKE_CENTRE_HZ - 62_500      # where the band plan names NFM


def scanning(state: str, frequency_hz: float | None = None):
    """Whether a scanner state shows the range scan listening in ``state``, on
    ``frequency_hz`` when given."""
    return lambda message: message["signals"]["state"] == state and (
        frequency_hz is None or message["signals"]["frequency_hz"] == frequency_hz)


class SignalsScanTest(unittest.IsolatedAsyncioTestCase):
    """The range scan over the current window that "signals" starts, as the
    signals scan did before it: it tunes the VFO to the active signals of
    the span in turn, on their channels, with the band plan's mode, for
    every client, until stopped, and goes on without any client, its saved
    settings left as they are."""

    def engine(self, source: Source | None = None, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    async def test_listens_to_each_active_signal_in_turn_for_every_client(self):
        engine = self.engine(TonesSource({37_500: 0.1, -62_500: 0.03}))
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        engine.radios[0].configure_demod({"bandwidth_hz": 12_500})
        engine.radios[0].configure_scanner({"listen_s": 2})
        await next_peaks(local, until=lambda peaks: len(peaks) == 2)
        engine.radios[0].configure_scanner({"scan": "signals"})
        # The lowest first, neither having had its turn; in NFM, as the
        # band plan names, with the filter as it was.
        for subscription in (local, remote):
            state = await next_scanner(subscription, until=scanning("listening", CHANNEL_B_HZ))
            self.assertEqual((state["scan"], state["stopped"], state["sweep"]["windows"]), ("sweep", None, 1))
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (CHANNEL_B_HZ, "NFM", 12_500.0))
        # Both stay on the air: once the longest listening time is over, the other.
        started = time.monotonic()
        await next_scanner(local, until=scanning("listening", CHANNEL_A_HZ))
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], CHANNEL_A_HZ)
        await next_scanner(local, until=scanning("listening", CHANNEL_B_HZ))
        saved = engine._contents()[SETTINGS_FILE]["scanner"]
        self.assertNotIn("scan", saved)     # never saved, nor its range and switch
        self.assertEqual((saved["sweep_listen"], saved["sweep_passes"], saved["sweep_low_hz"]),
                         (False, None, engine.radios[0].scanner_state()["sweep_low_hz"]))
        self.assertNotEqual(engine.radios[0].scanner_state()["sweep_low_hz"], FAKE_CENTRE_HZ - 0.4 * FAKE_RATE_HZ)

    async def test_takes_the_band_plan_s_mode_with_its_filter_width(self):
        source = TonesSource({-70_000: 0.1, 87_500: 0.1})
        source.centre_hz = 144_450_000.0    # 144.380 MHz in the SSB part, 144.5375 MHz in the FM part
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure_demod({"bandwidth_hz": 12_500})
        engine.radios[0].configure_scanner({"listen_s": 2})
        await next_peaks(subscription, until=lambda peaks: len(peaks) == 2)
        engine.radios[0].configure_scanner({"scan": "signals"})
        for frequency, mode, width in ((144_380_000, "USB", 2_700.0), (144_537_500, "NFM", 10_000.0)):
            with self.subTest(frequency=frequency):
                await next_scanner(subscription, until=scanning("listening", frequency))
                demod = engine.radios[0].demod_state()
                self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                                 (frequency, mode, width))

    async def test_leaves_a_signal_at_once_when_it_is_ignored(self):
        engine = self.engine(TonesSource({37_500: 0.1, -62_500: 0.03}))
        subscription = engine.subscribe()
        await next_peaks(subscription, until=lambda peaks: len(peaks) == 2)
        engine.radios[0].configure_scanner({"scan": "signals"})
        await next_scanner(subscription, until=scanning("listening", CHANNEL_B_HZ))
        [ignored] = [peak["frequency_hz"] for peak in engine.radios[0].peaks_state()["peaks"]
                     if peak["channel_hz"] == CHANNEL_B_HZ]
        engine.radios[0].configure_scanner({"ignore": ignored})
        self.assertEqual(engine.radios[0].scanner_state()["signals"], {"state": "watching", "frequency_hz": None})
        # Well before the longest listening time, 10 s.
        await asyncio.wait_for(next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ)), timeout=3)

    async def test_tuning_by_hand_detection_turned_off_or_another_source_stops_it_where_it_is(self):
        source = TonesSource({37_500: 0.1})
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()
        stops = (
            (lambda: engine.radios[0].configure_demod({"frequency_hz": CHANNEL_A_HZ + 5_000}), "tuned"),
            (lambda: engine.radios[0].configure({"centre_hz": FAKE_CENTRE_HZ + 1_000}), "tuned"),
            (lambda: engine.radios[0].configure_scanner({"detection": False}), "detection"),
            (lambda: engine.radios[0].configure({"kind": "other"}), "source"),
        )
        for stop, reason in stops:
            with self.subTest(reason=reason):
                engine.radios[0].configure_scanner({"scan": "signals"})
                await next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ))
                engine.radios[0].configure_demod({"mode": "AM"})      # the scan goes on
                self.assertEqual(engine.radios[0].scanner_state()["scan"], "sweep")
                vfo_hz = engine.radios[0].demod_state()["frequency_hz"]
                stop()
                state = engine.radios[0].scanner_state()
                self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"], state["signals"]),
                                 (None, reason, "sweep", {"state": None, "frequency_hz": None}))
                if reason == "detection":     # on the signal it listened to
                    self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], vfo_hz)
                    engine.radios[0].configure_scanner({"detection": True})
                elif reason == "tuned":
                    engine.radios[0].configure({"centre_hz": FAKE_CENTRE_HZ})

    async def test_keeps_listening_through_other_settings_and_brings_the_vfo_back_off_a_signal(self):
        engine = self.engine(TonesSource({37_500: 0.1}))
        subscription = engine.subscribe()
        engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 10_000})
        engine.radios[0].configure_scanner({"scan": "signals"})
        await next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ))
        # The saved switch is off: other settings leave the scan listening.
        engine.radios[0].configure_scanner({"threshold_db": 14, "listen_s": 5, "sweep_window_s": 0.5})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["sweep_listen"], state["signals"]["state"]),
                         ("sweep", False, "listening"))
        # Ignored, the signal is left: the scan watches, and Stop brings the VFO back.
        [ignored] = [peak["frequency_hz"] for peak in engine.radios[0].peaks_state()["peaks"]]
        engine.radios[0].configure_scanner({"ignore": ignored})
        self.assertEqual(engine.radios[0].scanner_state()["signals"], {"state": "watching", "frequency_hz": None})
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], FAKE_CENTRE_HZ + 10_000)

    async def test_the_range_scan_with_its_saved_settings_takes_its_place(self):
        engine = self.engine(TonesSource({37_500: 0.1}))
        subscription = engine.subscribe()
        low, high = FAKE_CENTRE_HZ - 50_000, FAKE_CENTRE_HZ + 50_000     # within the span: one window
        engine.radios[0].configure_scanner({"sweep_low_hz": low, "sweep_high_hz": high, "sweep_passes": 2})
        engine.radios[0].configure_scanner({"scan": "signals"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["sweep"]["passes_set"]), ("sweep", None))
        await next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ))
        engine.radios[0].configure_scanner({"scan": "signals"})       # it runs: nothing changes
        self.assertEqual(engine.radios[0].scanner_state()["signals"]["state"], "listening")
        engine.radios[0].configure_scanner({"scan": "sweep"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["sweep"]["passes_set"], state["signals"]["state"]),
                         ("sweep", 2, None))
        # Two passes without listening, then done.
        state = await next_scanner(subscription, until=lambda message: message["stopped"] is not None)
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "done", "sweep"))

    async def test_goes_on_without_any_client_and_saves_the_vfo_of_its_start(self):
        source = TonesSource({37_500: 0.1})
        engine = self.engine(source)
        engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + 10_000, "mode": "AM", "bandwidth_hz": 6_000})
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"scan": "signals"})
        await next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ))
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], demod["bandwidth_hz"]),
                         (CHANNEL_A_HZ, "NFM", 10_000.0))
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]["demod"]
        self.assertEqual((saved["frequency_hz"], saved["mode"], saved["bandwidth_hz"]),
                         (FAKE_CENTRE_HZ + 10_000, "AM", 6_000.0))
        engine.unsubscribe(subscription)
        reads = source.reads
        while source.reads < reads + 20:
            await asyncio.sleep(0.01)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], engine.radios[0].scanner_state()["stopped"]), (None, None))
        # The VFO stays where the scan left it, and is saved there.
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], CHANNEL_A_HZ)
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]["demod"]
        self.assertEqual((saved["frequency_hz"], saved["mode"]), (CHANNEL_A_HZ, "NFM"))
        await asyncio.sleep(0.05)
        reads = source.reads
        await asyncio.sleep(0.1)
        self.assertLessEqual(source.reads, reads + 1)

    async def test_needs_detection_and_takes_the_place_of_another_scan(self):
        engine = self.engine(TonesSource({37_500: 0.1}))
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"detection": False})
        with self.assertRaisesRegex(ValueError, "the range scan stops to listen only while detection is on: turn it on first"):
            engine.radios[0].configure_scanner({"scan": "signals"})
        self.assertEqual(engine.radios[0].scanner_state()["scan"], None)
        engine.radios[0].configure_scanner({"detection": True})
        engine.edit_bookmarks({"action": "add", "frequency_hz": CHANNEL_A_HZ, "name": "A", "mode": "NFM",
                               "scan": True})
        engine.radios[0].configure_scanner({"scan": "memory"})
        await next_scanner(subscription, until=in_state("listening"))
        engine.radios[0].configure_scanner({"scan": "signals"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["state"], state["signals"]["state"]),
                         ("sweep", None, None, None))
        await next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ))
        engine.radios[0].configure_scanner({"scan": "memory"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["signals"]), ("memory", {"state": None, "frequency_hz": None}))


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
                engine.radios[0].configure_demod({"frequency_hz": FAKE_CENTRE_HZ + offset, "mode": "WFM"})
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
        engine.radios[0].configure_scanner({"scan": "memory"})
        state = await asyncio.wait_for(next_scanner(subscription, until=in_state("listening")), timeout=10)
        self.assertEqual(state["memory"]["bookmark_id"], station)
        demod = engine.radios[0].demod_state()
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
        engine.radios[0].configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1],
                                  "sweep_window_s": 0.25})
        engine.radios[0].configure_demod({"frequency_hz": HOME_VFO_HZ})
        return engine

    async def test_steps_across_the_range_lists_what_it_finds_and_comes_back_once_stopped(self):
        source = TuningSource(self.CARRIERS)
        engine = self.engine(source)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        engine.radios[0].configure_scanner({"scan": "sweep"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["sweep"]),
                         ("sweep", None, {"windows": 4, "window": 0, "passes": 0, "passes_set": None}))
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
        engine.radios[0].configure_scanner({"scan": None})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"], state["sweep"]),
                         (None, None, None, {"windows": None, "window": None, "passes": None, "passes_set": None}))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_tuning_by_hand_or_another_source_stops_it_where_it_is(self):
        source = TuningSource(self.CARRIERS)
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()

        async def sweeping() -> None:
            engine.radios[0].configure_scanner({"scan": "sweep"})
            await next_scanner(subscription, until=at_window(2))

        await sweeping()
        engine.radios[0].configure_demod({"mode": "AM", "bandwidth_hz": 6_000, "squelch_db": -70, "squelch_auto": False})
        engine.radios[0].configure_demod({"frequency_hz": engine.radios[0].demod_state()["frequency_hz"]})     # where it is
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "sweep")
        vfo = engine.radios[0].demod_state()["frequency_hz"] + 1_000
        window = source.centre_hz
        engine.radios[0].configure_demod({"frequency_hz": vfo})
        self.assertEqual((engine.radios[0].scanner_state()["stopped"], engine.radios[0].scanner_state()["stopped_scan"]),
                         ("tuned", "sweep"))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (window, vfo))
        await sweeping()
        engine.radios[0].configure({"centre_hz": 145_500_000})
        self.assertEqual((engine.radios[0].scanner_state()["stopped"], source.centre_hz), ("tuned", 145_500_000))
        home = (source.centre_hz, engine.radios[0].demod_state()["frequency_hz"])
        await sweeping()
        tuned = len(source.tuned)
        engine.radios[0].configure({"kind": "other"})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], engine.radios[0].scanner_state()["stopped"]), (None, "source"))
        self.assertEqual(len(source.tuned), tuned)      # left where it was
        saved = engine.receivers["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), home)

    async def test_refuses_a_range_beyond_the_receiver_and_stops_once_it_refuses_a_window(self):
        source = TuningSource({}, centre_hz=145_050_000)
        engine = self.engine(source)
        # The VFO within the receiver's span, where the engine keeps it, but outside the second window's:
        # the sweep moves it, and the stop brings it back, where the receiver's next samples leave it.
        vfo_hz = 144_940_000.0
        self.assertTrue(abs(vfo_hz - source.centre_hz) <= FAKE_RATE_HZ / 2 < abs(vfo_hz - SWEEP_WINDOWS.centre(1)))
        engine.radios[0].configure_demod({"frequency_hz": vfo_hz})
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"sweep_low_hz": 20e6, "sweep_high_hz": 30e6})
        with self.assertRaisesRegex(ValueError, "it tunes from 24 to 1766 MHz"):
            engine.radios[0].configure_scanner({"scan": "sweep"})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], source.tuned), (None, []))
        engine.radios[0].configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1]})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        with self.assertRaisesRegex(ValueError, "it tunes from 24 to 1766 MHz"):
            engine.radios[0].configure_scanner({"sweep_low_hz": 20e6})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], engine.radios[0].scanner_state()["sweep_low_hz"]),
                         ("sweep", SWEEP_HZ[0]))
        source.range_hz = (24e6, 145_100_000)       # it no longer tunes to the last two windows
        with self.assertLogs("radiolack", "WARNING"):
            state = await next_scanner(subscription, until=lambda message: message["scan"] is None)
        self.assertEqual((state["stopped"], state["stopped_scan"], source.centre_hz), ("range", "sweep", 145_050_000))
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], vfo_hz)

    async def test_a_new_range_starts_it_again_from_its_first_window(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(2))
        engine.radios[0].configure_scanner({"sweep_low_hz": 433e6, "sweep_high_hz": 434e6, "sweep_window_s": 2})
        windows = sweep_windows(433e6, 434e6, FAKE_RATE_HZ)
        self.assertEqual(engine.radios[0].scanner_state()["sweep"], {"windows": windows.count, "window": 0, "passes": 0,
                                                             "passes_set": None})
        self.assertEqual((source.centre_hz, engine.radios[0]._sweep.search_blocks), (windows.centre(0), 30))
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual(source.centre_hz, HOME_HZ)

    async def test_goes_on_without_any_client_until_stopped(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"scan": "sweep"})
        engine.unsubscribe(subscription)
        tuned = len(source.tuned)
        await eventually(lambda: len(source.tuned) >= tuned + 6)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual(source.centre_hz, HOME_HZ)
        await asyncio.sleep(0.05)
        reads = source.reads
        await asyncio.sleep(0.1)
        self.assertLessEqual(source.reads, reads + 1)

    async def test_one_scan_at_a_time(self):
        source = TuningSource({HOME_HZ + 30_000: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(1))
        with self.assertRaisesRegex(ValueError, "no ticked bookmark"):
            engine.radios[0].configure_scanner({"scan": "memory"})
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "sweep")
        engine.edit_bookmarks({"action": "add", "frequency_hz": HOME_HZ + 30_000, "name": "Home", "mode": "NFM",
                               "scan": True})
        [bookmark] = [entry["id"] for entry in engine.bookmarks_state()["bookmarks"]]
        engine.radios[0].configure_scanner({"scan": "memory"})    # it watches the span the sweep comes back to
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["watched"], source.centre_hz),
                         ("memory", None, [bookmark], HOME_HZ))
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual(state["memory"]["bookmark_id"], bookmark)
        engine.radios[0].configure_scanner({"scan": "sweep"})
        state = engine.radios[0].scanner_state()
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
        self.assertEqual(engine.radios[0]._windows, windows)
        engine.radios[0].configure_scanner({"scan": "sweep"})
        for window in (1, 2):
            state = await next_scanner(subscription, until=at_window(window))
            self.assertEqual((state["memory"]["watched"], state["memory"]["windows"]), ([bookmark], 1))
            self.assertEqual(engine.radios[0]._windows, windows)
        engine.radios[0].configure_demod({"frequency_hz": source.centre_hz + 10_000})     # it stays in the window
        self.assertEqual(engine.radios[0]._windows, (SWEEP_WINDOWS.centre(2),))


LOW_CHANNEL_HZ = 145_037_500.0      # 12.5 kHz channels of the 2 m band, where the band plan names NFM:
HIGH_CHANNEL_HZ = 145_162_500.0     # the first in the sweep's first two windows, the second in its last three


class ListeningSweepTest(unittest.IsolatedAsyncioTestCase):
    """The sweep that listens (the range scan, docs/design/range-scan.md):
    in each window, once its searches end, it listens to the window's
    active signals in turn as the signals scan does, then goes on; a range
    within the span never tunes the receiver; the passes set end it; Stop
    leaves it on the signal it listens to."""

    def engine(self, source: Source | None = None, **options) -> Engine:
        engine = Engine(source, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    def sweeping(self, source: Source, **settings) -> Engine:
        engine = self.engine(source)
        engine.radios[0].configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1],
                                  "sweep_window_s": 0.25, "listen_s": 2, **settings})
        engine.radios[0].configure_demod({"frequency_hz": HOME_VFO_HZ})
        return engine

    async def test_listens_in_each_window_once_a_pass_and_stays_on_the_signal_once_stopped(self):
        source = TuningSource({LOW_CHANNEL_HZ: 0.03, HIGH_CHANNEL_HZ: 0.03})
        engine = self.sweeping(source, sweep_listen=True)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        engine.radios[0].configure_scanner({"scan": "sweep"})
        for subscription in (local, remote):
            state = await next_scanner(subscription, until=scanning("listening", LOW_CHANNEL_HZ))
            self.assertEqual((state["scan"], state["sweep"]["window"]), ("sweep", 0))
        demod = engine.radios[0].demod_state()
        self.assertEqual((demod["frequency_hz"], demod["mode"], source.centre_hz),
                         (LOW_CHANNEL_HZ, "NFM", SWEEP_WINDOWS.centre(0)))
        # Both carriers stay on the air: each is left after the longest
        # listening time, and the low one, heard in the first window, is not
        # listened to again in the second, which holds both.
        heard = []
        while HIGH_CHANNEL_HZ not in heard:
            state = await asyncio.wait_for(next_scanner(local, until=scanning("checking")), 10)
            heard.append(state["signals"]["frequency_hz"])
        self.assertEqual(heard, [HIGH_CHANNEL_HZ])
        self.assertEqual(state["sweep"]["window"], 1)
        await next_scanner(local, until=scanning("listening", HIGH_CHANNEL_HZ))
        window = source.centre_hz
        engine.radios[0].configure_scanner({"scan": None})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["signals"]), (None, None,
                                                                              {"state": None, "frequency_hz": None}))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (window, HIGH_CHANNEL_HZ))

    async def test_the_passes_set_stop_it_and_bring_the_receiver_back(self):
        source = TuningSource({})
        engine = self.sweeping(source, sweep_passes=1)
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"scan": "sweep"})
        self.assertEqual(engine.radios[0].scanner_state()["sweep"]["passes_set"], 1)
        state = await asyncio.wait_for(next_scanner(subscription, until=lambda message: message["scan"] is None), 10)
        self.assertEqual((state["stopped"], state["stopped_scan"]), ("done", "sweep"))
        self.assertEqual(source.tuned, [SWEEP_WINDOWS.centre(index) for index in range(4)] + [HOME_HZ])
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], HOME_VFO_HZ)

    async def test_a_range_within_the_span_never_tunes_the_receiver(self):
        engine = self.engine(TonesSource({37_500: 0.1, -62_500: 0.03}))      # a source that does not tune
        subscription = engine.subscribe()
        vfo_hz = FAKE_CENTRE_HZ + 10_000
        engine.radios[0].configure_demod({"frequency_hz": vfo_hz})
        engine.radios[0].configure_scanner({"sweep_low_hz": FAKE_CENTRE_HZ - 90_000, "sweep_high_hz": FAKE_CENTRE_HZ + 90_000,
                                  "sweep_window_s": 0.25, "listen_s": 2})
        engine.radios[0].configure_scanner({"scan": "sweep"})       # without listening, pass after pass
        state = await next_scanner(subscription, until=lambda message: message["sweep"]["passes"] == 2)
        self.assertEqual(state["sweep"]["windows"], 1)
        engine.radios[0].configure_scanner({"scan": None})          # not on a signal: the VFO comes back
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], vfo_hz)
        engine.radios[0].configure_scanner({"sweep_listen": True})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        for channel in (CHANNEL_B_HZ, CHANNEL_A_HZ, CHANNEL_B_HZ):
            await asyncio.wait_for(next_scanner(subscription, until=scanning("listening", channel)), 10)
        self.assertEqual(engine.radios[0].scanner_state()["sweep"]["window"], 0)

    async def test_detection_turned_off_stops_it_and_an_ignored_signal_is_left(self):
        engine = self.engine(TonesSource({37_500: 0.1, -62_500: 0.03}))
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"sweep_low_hz": FAKE_CENTRE_HZ - 90_000, "sweep_high_hz": FAKE_CENTRE_HZ + 90_000,
                                  "sweep_window_s": 0.25, "sweep_listen": True})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=scanning("listening", CHANNEL_B_HZ))
        [ignored] = [peak["frequency_hz"] for peak in engine.radios[0].peaks_state()["peaks"]
                     if peak["channel_hz"] == CHANNEL_B_HZ]
        engine.radios[0].configure_scanner({"ignore": ignored})
        self.assertEqual(engine.radios[0].scanner_state()["signals"], {"state": "watching", "frequency_hz": None})
        await asyncio.wait_for(next_scanner(subscription, until=scanning("listening", CHANNEL_A_HZ)), timeout=3)
        engine.radios[0].configure_scanner({"detection": False})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "detection", "sweep"))
        with self.assertRaisesRegex(ValueError, "stops to listen only while detection is on"):
            engine.radios[0].configure_scanner({"scan": "sweep"})
        engine.radios[0].configure_scanner({"sweep_listen": False})
        engine.radios[0].configure_scanner({"scan": "sweep"})       # a sweep that does not listen runs without detection
        with self.assertRaisesRegex(ValueError, "stops to listen only while detection is on"):
            engine.radios[0].configure_scanner({"sweep_listen": True})
        self.assertEqual((engine.radios[0].scanner_state()["scan"], engine.radios[0].scanner_state()["sweep_listen"]), ("sweep", False))


class SimulatedSource(TonesSource):
    """TonesSource as the simulated band: the kind that is never kept."""

    kind = "synthetic"


class KeptScanTest(unittest.IsolatedAsyncioTestCase):
    """The kept scans (keptscans.py) in the engine: each range scan under
    its name, with the channels it found, for every client, and in
    scans.json across restarts."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"

    def engine(self, source: Source, **options) -> Engine:
        engine = Engine(source, store=Store(self.directory), save_delay_s=0.01, **options)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        return engine

    async def test_keeps_a_named_scan_with_its_channels_and_the_time_heard(self):
        source = TuningSource({LOW_CHANNEL_HZ: 0.03, HIGH_CHANNEL_HZ: 0.03})
        engine = self.engine(source)
        local, remote = engine.subscribe(), engine.subscribe(device="5f0c2a91")
        engine.radios[0].configure_scanner({"sweep_low_hz": SWEEP_HZ[0], "sweep_high_hz": SWEEP_HZ[1], "sweep_window_s": 0.25,
                                  "listen_s": 2, "sweep_listen": True, "sweep_passes": 1})
        engine.radios[0].configure_scanner({"scan": "sweep", "name": " 2 m repeaters "})
        [running] = engine.scans_state()["scans"]
        self.assertEqual((running["name"], running["ended"], running["low_hz"], running["high_hz"],
                          running["listen"], running["passes_set"]),
                         ("2 m repeaters", None, SWEEP_HZ[0], SWEEP_HZ[1], True, 1))
        state = await asyncio.wait_for(next_scanner(local, until=lambda message: message["scan"] is None), 30)
        self.assertEqual(state["stopped"], "done")
        for subscription in (local, remote):     # every client receives the kept scans
            [scan] = of_type(waiting(subscription), "scans")[-1]["scans"]
            self.assertEqual((scan["name"], scan["ended"], scan["passes"], scan["channels"]),
                             ("2 m repeaters", "done", 1, 2))
        engine.configure_scans({"get": scan["id"]}, remote)
        [reply] = of_type(waiting(remote), "scan")
        self.assertEqual([channel["frequency_hz"] for channel in reply["channels"]], [LOW_CHANNEL_HZ, HIGH_CHANNEL_HZ])
        for channel in reply["channels"]:
            self.assertGreaterEqual(channel["searches"], 1)
            self.assertGreater(channel["open_s"], 0.5)      # heard for up to the longest listening time
        self.assertEqual(of_type(waiting(local), "scan"), [])
        # It is written to its file at its end.
        saved = await self.saved(lambda content: content["scans"] and content["scans"][0]["ended"] == "done")
        self.assertEqual(saved["scans"][0]["name"], "2 m repeaters")

    async def test_rename_delete_clear_and_the_refusals(self):
        engine = self.engine(TonesSource({37_500: 0.1}))
        subscription = engine.subscribe()
        engine.radios[0].configure_scanner({"sweep_low_hz": FAKE_CENTRE_HZ - 90_000, "sweep_high_hz": FAKE_CENTRE_HZ + 90_000,
                                  "sweep_window_s": 0.25})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        [running] = engine.scans_state()["scans"]
        self.assertRegex(running["name"], r"^144\.910-145\.090 MHz, \d{4}-\d\d-\d\d \d\d:\d\d$")
        await eventually(lambda: engine.kept.running and engine.kept.running[0].channels)
        engine.configure_scans({"rename": running["id"], "name": "Calling"}, subscription)
        refused = ({}, {"get": 9}, {"delete": running["id"]}, {"rename": running["id"], "name": ""},
                   {"rename": running["id"]}, {"clear_all": False}, {"clear_all": True, "get": 1}, {"forget": 1})
        for changes in refused:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.configure_scans(changes, subscription)
        for changes in ({"scan": "memory", "name": "Bookmarks"}, {"scan": None, "name": "Stop"},
                        {"scan": "sweep", "name": "x" * 61}, {"name": "Alone"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.radios[0].configure_scanner(changes)
        engine.radios[0].configure_scanner({"scan": "signals", "name": "Here"})       # the current window: a new kept scan
        names = [(scan["name"], scan["ended"]) for scan in engine.scans_state()["scans"]]
        self.assertEqual(names, [("Here", None), ("Calling", "stopped")])
        engine.configure_scans({"clear_all": True}, subscription)
        self.assertEqual([scan["name"] for scan in engine.scans_state()["scans"]], ["Here"])
        engine.radios[0].configure_scanner({"scan": None})
        engine.configure_scans({"delete": engine.scans_state()["scans"][0]["id"]}, subscription)
        self.assertEqual(engine.scans_state()["scans"], [])
        self.assertEqual(of_type(waiting(subscription), "scans")[-1]["scans"], [])

    async def test_the_simulated_band_is_never_kept(self):
        engine = self.engine(SimulatedSource({37_500: 0.1}))
        engine.radios[0].configure_scanner({"scan": "signals"})
        await asyncio.sleep(0.5)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual(engine.scans_state()["scans"], [])

    async def test_one_that_ran_at_close_comes_back_closed(self):
        engine = Engine(TonesSource({37_500: 0.1}), store=Store(self.directory), save_delay_s=0.01)
        task = asyncio.ensure_future(engine.run())
        engine.radios[0].configure_scanner({"scan": "signals", "name": "Before"})
        await eventually(lambda: engine.kept.running and engine.kept.running[0].channels)
        engine.close()
        await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(engine.wait_closed(), timeout=5)
        again = self.engine(FakeSource())
        [scan] = again.scans_state()["scans"]
        self.assertEqual((scan["name"], scan["ended"], scan["channels"]), ("Before", "closed", 1))

    async def saved(self, check) -> dict:
        """scans.json once ``check`` holds for its content."""
        deadline = time.monotonic() + 5
        while True:
            path = self.directory / "scans.json"
            if path.exists():
                content = json.loads(path.read_text(encoding="utf-8"))
                if check(content):
                    return content
            self.assertLess(time.monotonic(), deadline, "scans.json was not written")
            await asyncio.sleep(0.02)


class RtlTcpSweepTest(unittest.IsolatedAsyncioTestCase):
    """The sweep with an rtl_tcp receiver: the fake rtl_tcp's carriers at
    fixed frequencies, found across its windows."""

    async def test_finds_the_carriers_across_its_windows_and_tuning_by_hand_stops_it_at_once(self):
        await self.sweep(1_024_000.0, 145_600_000.0)    # three windows

    async def test_sweeps_at_librtlsdr_s_lowest_rates_too(self):
        await self.sweep(240_000.0, 145_150_000.0)      # three windows too

    async def sweep(self, rate: float, high: float) -> None:
        low = 145_000_000.0
        windows = sweep_windows(low, high, rate)
        self.assertEqual(windows.count, 3)
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
        engine.radios[0].configure_scanner({"sweep_low_hz": low, "sweep_high_hz": high, "sweep_window_s": 0.25})
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        home = (engine.radios[0].state()["centre_hz"], engine.radios[0].demod_state()["frequency_hz"])
        await eventually(lambda: fake.frequency_hz != 100_000_000)     # left by a previous client
        home_request = fake.frequency_hz        # on the R820T's grid
        engine.radios[0].configure_scanner({"scan": "sweep"})
        await asyncio.wait_for(next_peaks(subscription, until=lambda peaks: found_all(peaks, carriers)), 30)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual((engine.radios[0].state()["centre_hz"], engine.radios[0].demod_state()["frequency_hz"]), home)
        await eventually(lambda: fake.frequency_hz == home_request)
        engine.radios[0].configure_scanner({"scan": "sweep"})
        await next_scanner(subscription, until=at_window(1))
        engine.radios[0].configure({"centre_hz": 145_350_000})    # before its samples come
        self.assertEqual((engine.radios[0].scanner_state()["stopped"], engine.radios[0].state()["centre_hz"]), ("tuned", 145_350_000))
        self.assertEqual(engine.radios[0].state()["sample_rate_hz"], rate)


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
        engine.radios[0].configure_demod({"frequency_hz": HOME_VFO_HZ})
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
        self.assertEqual(engine.radios[0].scanner_state()["memory"], {"watched": [near, far], "windows": 2, "window": None,
                                                            "state": None, "bookmark_id": None})
        engine.radios[0].configure_scanner({"scan": "memory"})
        self.assertEqual(engine.radios[0].scanner_state()["memory"]["window"], 0)     # the nearest
        for subscription in (local, remote):
            await next_scanner(subscription, until=at_memory_window(1))
        await eventually(lambda: len(source.tuned) >= 4)
        self.assertEqual(source.tuned[:4], [*self.WINDOWS, *self.WINDOWS])
        # Saved meanwhile, the receiver's settings are those of the scan's start.
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))
        engine.radios[0].configure_scanner({"scan": None})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["memory"]["window"]), (None, None, None))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_stays_on_a_channel_of_another_window_while_it_listens_and_once_stopped(self):
        source = TuningSource({FAR_HZ: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        self.bookmark(engine, NEAR_HZ, "Near")
        far = self.bookmark(engine, FAR_HZ, "Far")
        engine.radios[0].configure_scanner({"scan": "memory"})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual((state["memory"]["bookmark_id"], state["memory"]["window"]), (far, 1))
        tuned = len(source.tuned)
        await asyncio.sleep(3 * TOUR_VISIT_S)
        self.assertEqual(len(source.tuned), tuned)      # it stays while it listens
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (self.WINDOWS[1], FAR_HZ))
        saved = engine._contents()[SETTINGS_FILE]["receivers"]["test"]
        self.assertEqual((saved["source"]["centre_hz"], saved["demod"]["frequency_hz"]), (self.WINDOWS[1], FAR_HZ))

    async def test_never_tunes_the_receiver_while_its_bookmarks_lie_in_the_span(self):
        source = TuningSource({NEAR_HZ: 0.03})
        engine = self.engine(source)
        subscription = engine.subscribe()
        near = self.bookmark(engine, NEAR_HZ, "Near")
        self.bookmark(engine, FAR_HZ, "Far", scan=False)
        self.assertEqual(engine.radios[0].scanner_state()["memory"]["windows"], 1)
        engine.radios[0].configure_scanner({"resume_s": 0})
        engine.radios[0].configure_scanner({"scan": "memory"})
        state = await next_scanner(subscription, until=in_state("listening"))
        self.assertEqual((state["memory"]["bookmark_id"], state["memory"]["window"]), (near, 0))
        source.carriers = {}
        await next_scanner(subscription, until=in_state("watching"))
        await asyncio.sleep(3 * TOUR_VISIT_S)
        engine.radios[0].configure_scanner({"scan": None})
        self.assertEqual(source.tuned, [])
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], NEAR_HZ)     # where the scan left the VFO

    async def test_leaves_out_the_bookmarks_beyond_the_receivers_reach(self):
        source = TuningSource({})
        engine = self.engine(source)
        self.bookmark(engine, 10e6, "Low")
        self.bookmark(engine, 2400e6, "High")
        state = engine.radios[0].scanner_state()["memory"]
        self.assertEqual((state["watched"], state["windows"]), ([], 0))
        with self.assertRaisesRegex(ValueError, "no ticked bookmark lies within the receiver's reach"):
            engine.radios[0].configure_scanner({"scan": "memory"})
        far = self.bookmark(engine, FAR_HZ, "Far")
        state = engine.radios[0].scanner_state()["memory"]
        self.assertEqual((state["watched"], state["windows"]), ([far], 1))
        engine.radios[0].configure_scanner({"scan": "memory"})
        [window] = memory_windows([FAR_HZ], FAKE_RATE_HZ, 24e6, 1766e6)
        await eventually(lambda: source.tuned == [window])

    async def test_follows_the_bookmarks_ticked_meanwhile_and_stops_once_none_is_left(self):
        source = TuningSource({})
        engine = self.engine(source)
        subscription = engine.subscribe()
        near, far = self.bookmark(engine, NEAR_HZ, "Near"), self.bookmark(engine, FAR_HZ, "Far")
        engine.radios[0].configure_scanner({"scan": "memory"})
        await next_scanner(subscription, until=at_memory_window(1))
        engine.edit_bookmarks({"action": "change", "id": far, "scan": False})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["memory"]["watched"], state["memory"]["windows"],
                          state["memory"]["window"]), ("memory", [near], 1, 0))
        await eventually(lambda: source.centre_hz == HOME_HZ)      # the span it started from holds the one left
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine.edit_bookmarks({"action": "change", "id": near, "scan": False})
        self.assertEqual(logs.output, ["WARNING:radiolack.radio:the memory scan stopped: no ticked bookmark lies "
                                       "within the receiver's reach any more"])
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "bookmarks", "memory"))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

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
        engine.radios[0].configure_scanner({"scan": "memory"})
        with self.assertLogs("radiolack", "WARNING"):
            state = await next_scanner(subscription, until=lambda message: message["scan"] is None)
        self.assertEqual((state["stopped"], state["stopped_scan"]), ("range", "memory"))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (HOME_HZ, HOME_VFO_HZ))

    async def test_tuning_by_hand_or_another_source_stops_it_where_it_is(self):
        source = TuningSource({})
        engine = self.engine(sources=FakeSources(test=lambda: source))
        subscription = engine.subscribe()
        self.bookmark(engine, NEAR_HZ, "Near")
        self.bookmark(engine, FAR_HZ, "Far")

        async def on_the_second_window() -> None:
            engine.radios[0].configure_scanner({"scan": "memory"})
            await next_scanner(subscription, until=at_memory_window(1))
            await eventually(lambda: engine.radios[0]._capture is not None
                             and engine.radios[0]._capture[0] == (self.WINDOWS[1], FAKE_RATE_HZ))

        await on_the_second_window()
        vfo = engine.radios[0].demod_state()["frequency_hz"] + 1_000
        engine.radios[0].configure_demod({"frequency_hz": vfo})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "tuned", "memory"))
        self.assertEqual((source.centre_hz, engine.radios[0].demod_state()["frequency_hz"]), (self.WINDOWS[1], vfo))
        engine.radios[0].configure({"centre_hz": HOME_HZ})
        await on_the_second_window()
        tuned = len(source.tuned)
        engine.radios[0].configure({"kind": "other"})
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "source", "memory"))
        self.assertEqual(len(source.tuned), tuned)      # left where it was
        self.assertEqual(engine.receivers["test"]["source"]["centre_hz"], HOME_HZ)     # saved as at the start


class RtlTcpMemoryScanTest(unittest.IsolatedAsyncioTestCase):
    """The memory scan with an rtl_tcp receiver: it finds a bookmark of
    another window on the air, through the receiver's settling."""

    async def test_listens_on_a_bookmark_of_another_window(self):
        await self.listen(1_024_000.0)

    async def test_listens_at_librtlsdr_s_lowest_rates_too(self):
        await self.listen(240_000.0)

    async def listen(self, rate: float) -> None:
        # The same places in the span whatever the rate: 100 kHz, 1.8 and 2 MHz at 1.024 MS/s.
        near, quiet, on_air = (HOME_HZ + round(rate * fraction) for fraction in (0.0977, 1.7578, 1.9531))
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
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        # Where the R820T is placed on its grid, from the centre in use.
        windows = memory_windows([near, quiet, on_air], rate, 24e6, 1766e6,
                                 Placement(source.centre_in_use, source.grid_hz))     # two
        self.assertEqual(len(windows), 2)
        self.assertEqual(engine.radios[0].scanner_state()["memory"]["windows"], len(windows))
        engine.radios[0].configure_scanner({"scan": "memory"})
        state = await asyncio.wait_for(next_scanner(subscription, until=in_state("listening")), 30)
        self.assertEqual(state["memory"]["bookmark_id"], ids["On the air"])
        self.assertEqual((engine.radios[0].state()["centre_hz"], engine.radios[0].demod_state()["frequency_hz"]), (windows[1], on_air))
        await eventually(lambda: fake.frequency_hz == round(source.centre_in_use(windows[1])))


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
        engine.radios[0].start_given_scan("memory")
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "memory")
        await eventually(lambda: engine.radios[0].scanner_state()["memory"]["state"] == "listening", timeout=30)
        self.assertEqual((engine.radios[0].state()["status"], engine.radios[0].demod_state()["frequency_hz"]), ("streaming", on_air))
        await eventually(lambda: heard(engine, on_air), timeout=30)

    async def test_the_sweep_runs_with_its_saved_range_without_any_client_and_fills_the_history(self):
        low, high = 145_000_000.0, 145_600_000.0
        on_air = high - 50_000      # in the last of its three windows
        Store(self.directory).save(SETTINGS_FILE, {"scanner": {"sweep_low_hz": low, "sweep_high_hz": high,
                                                               "sweep_window_s": 0.25}})
        engine = self.engine(await self.rtl_tcp({on_air: 0.2}))
        engine.radios[0].start_given_scan("sweep")
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["sweep"]["windows"]), ("sweep", 3))
        await eventually(lambda: heard(engine, on_air), timeout=30)
        self.assertEqual(engine.radios[0].state()["status"], "streaming")

    async def test_the_range_scan_over_the_current_window_runs_without_any_client_and_fills_the_history(self):
        on_air = HOME_HZ + 200_000
        engine = self.engine(await self.rtl_tcp({on_air: 0.2}))
        engine.radios[0].start_given_scan("signals")
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["sweep"]["windows"]), ("sweep", 1))
        await eventually(lambda: engine.radios[0].scanner_state()["signals"]["state"] == "listening", timeout=30)
        self.assertEqual(engine.radios[0].state()["status"], "streaming")
        self.assertAlmostEqual(engine.radios[0].demod_state()["frequency_hz"], on_air, delta=100)
        await eventually(lambda: heard(engine, on_air), timeout=30)

    async def test_one_that_cannot_start_says_why_and_the_engine_stays_idle(self):
        refusals = (
            ("memory", FakeSource, "no ticked bookmark lies within the receiver's reach"),
            ("sweep", FakeSource, "a range wider than the current window needs a receiver that tunes, such as "
                                  "an RTL-SDR"),
            # Saved before the start: a range beyond the receiver's reach.
            ("sweep", lambda: TuningSource({}), "the receiver cannot tune to every window of the range "
                                                "scan's range: it tunes from 24 to 1766 MHz"),
            ("signals", FakeSource, "the range scan stops to listen only while detection is on: turn it on first"),
        )
        Store(self.directory).save(SETTINGS_FILE, {"scanner": {"sweep_low_hz": 1_800e6, "sweep_high_hz": 1_810e6,
                                                               "detection": False}})
        for scan, make_source, reason in refusals:
            with self.subTest(scan=scan, reason=reason):
                source = make_source()
                engine = self.engine(source)
                with self.assertRaises(ValueError) as raised:
                    engine.radios[0].start_given_scan(scan)
                self.assertEqual(str(raised.exception), reason)
                await asyncio.sleep(0.1)
                self.assertEqual(source.reads, 0)
                # An interface that opens later learns why.
                subscription = engine.subscribe()
                state = await next_scanner(subscription)
                engine.unsubscribe(subscription)
                self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]),
                                 (None, "start", "memory" if scan == "memory" else "sweep"))

    async def test_a_refusal_reaches_the_clients_and_leaves_the_other_scan_running(self):
        engine = self.engine(FakeSource())
        subscription = engine.subscribe()
        with self.assertRaises(ValueError):
            engine.radios[0].start_given_scan("sweep")    # the source does not tune
        state = await asyncio.wait_for(next_scanner(subscription, until=lambda message: message["stopped"] is not None),
                                       timeout=10)
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "start", "sweep"))
        engine.edit_bookmarks({"action": "add", "frequency_hz": FAKE_CENTRE_HZ + 40_000, "name": "Near",
                               "mode": "NFM", "scan": True})
        engine.radios[0].configure_scanner({"scan": "memory"})
        self.assertEqual(engine.radios[0].scanner_state()["stopped"], None)
        with self.assertRaises(ValueError):
            engine.radios[0].start_given_scan("sweep")
        for scan in ("history", "Memory", None):
            with self.subTest(scan=scan), self.assertRaisesRegex(ValueError,
                                                                 '^a scan is "memory", "sweep" or "signals", not '):
                engine.radios[0].start_given_scan(scan)
        state = engine.radios[0].scanner_state()
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), ("memory", None, None))


class SimulatedBandSource(FakeSource):
    """A fake source that passes for the simulated band."""

    kind = "synthetic"


def observations_of(subscription: Subscription) -> list[list[dict]]:
    """The channels of each observations message ready for a client, now."""
    return [message["channels"] for message in waiting(subscription)
            if isinstance(message, dict) and message["type"] == "observations"]


def as_saved(channel: dict) -> dict:
    """A channel of the observations as the file keeps it: without the
    channel it shows on, which the band plan gives at each start."""
    return {key: value for key, value in channel.items() if key != "channel_hz"}


class ObservationsTest(unittest.IsolatedAsyncioTestCase):
    """What detection found, kept across restarts: recorded from a receiver
    but not from the simulated band, sent to the client that asks, cleared,
    written at most every so often, at once after a clear and at the stop."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "radiolack"

    def engine(self, source: Source | None = None, run: bool = True, store: Store | None = None,
               **options) -> Engine:
        engine = Engine(source if source is not None else FakeSource(),
                        store=store if store is not None else Store(self.directory), **options)
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
                    engine.radios[0].configure_scanner({"ignore": ignored})
                subscription = engine.subscribe()
                await self.searches(source, 3)
                self.assertEqual(engine.observations_state()["channels"], [])
                if ignored is None:
                    self.assertTrue(engine.radios[0].peaks_state()["peaks"])     # found, all the same
                engine.unsubscribe(subscription)

    async def test_each_channel_shows_on_the_band_plan_s(self):
        recent = time.time() - 60
        self.write([{"frequency_hz": frequency, "first_s": recent, "last_s": recent, "transmissions": 1,
                     "active_s": 1.0, "margin_db": 15.0, "hours": [1] + [0] * 23}
                    for frequency in (144_301_249, 145_601_000, 145_603_000)])
        engine = self.engine(run=False)
        shown = {channel["frequency_hz"]: channel["channel_hz"] for channel in engine.observations_state()["channels"]}
        # The SSB part to the 100 Hz; a 12.5 kHz channel within 1.5 kHz.
        self.assertEqual(shown, {144_301_249: 144_301_200, 145_601_000: 145_600_000, 145_603_000: 145_603_000})
        stored = engine._contents(observations=True)[OBSERVATIONS_FILE]["channels"]
        self.assertEqual([sorted(channel) for channel in stored], [sorted(self.read()["channels"][0])] * 3)

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
        while (self.read() or {}).get("channels") != [as_saved(remaining)]:
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

    async def written(self, count: int) -> list[tuple[float, dict]]:
        """The file's first ``count`` contents, each with when it was seen."""
        writes: list[tuple[float, dict]] = []
        deadline = time.monotonic() + 10
        while len(writes) < count:
            self.assertLess(time.monotonic(), deadline, f"written {len(writes)} times")
            content = self.read()
            if content is not None and (not writes or content != writes[-1][1]):
                writes.append((time.monotonic(), content))
            await asyncio.sleep(0.01)
        return writes

    async def test_written_at_most_every_interval_while_it_runs(self):
        started = time.monotonic()
        engine = self.engine(save_delay_s=0.01, observations_save_s=0.5)
        engine.subscribe()
        writes = await self.written(2)
        self.assertGreaterEqual(writes[0][0] - started, 0.45)
        self.assertGreaterEqual(writes[1][0] - writes[0][0], 0.45)
        [channel] = writes[1][1]["channels"]
        self.assertAlmostEqual(channel["frequency_hz"], FAKE_TONE_HZ, delta=FAKE_RATE_HZ / 8192)

    async def test_the_interval_counts_from_the_end_of_a_slow_write(self):
        class SlowAtFirst(Store):
            """A disk that takes a moment over the observations' first write."""

            slow = True

            def save(self, name, content):
                if name == OBSERVATIONS_FILE and self.slow:
                    self.slow = False
                    time.sleep(0.2)
                return super().save(name, content)

        engine = self.engine(store=SlowAtFirst(self.directory), save_delay_s=0.01, observations_save_s=0.5)
        engine.subscribe()
        writes = await self.written(2)
        self.assertGreaterEqual(writes[1][0] - writes[0][0], 0.45)

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
        [read] = self.engine(run=False).observations_state()["channels"]
        self.assertEqual(as_saved(read), saved)

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


def with_iq_file(state: dict) -> bool:
    """Whether an I/Q recording runs and its files were made."""
    return state["iq"] is not None and state["iq"]["file"] is not None


class SimulatedBandSources(FakeSources):
    """Fake sources whose last kind passes for the simulated band."""

    kinds = ("test", "other", "synthetic")

    def __init__(self) -> None:
        super().__init__(synthetic=SimulatedBandSource)


RECORDED_NAME = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"


class RecordingCase(unittest.IsolatedAsyncioTestCase):
    """An engine that records into a temporary folder."""

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


class RecordingTest(RecordingCase):
    """Recording the audio: started and stopped by any client, kept up
    without one, never on the simulated band, stopped by a new audio rate,
    a disk short of space and the simulated band; the list of the
    recordings, their deletion, and the folder, chosen and saved."""

    async def test_records_the_audio_until_a_client_stops_it(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        state = await next_recording(subscription, lambda state: with_file(state) and state["audio"]["seconds"] >= 0.5)
        audio = state["audio"]
        self.assertRegex(audio["file"], r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z_145\.061440MHz_NFM\.wav$")
        self.assertEqual((audio["frequency_hz"], audio["mode"], audio["rate_hz"]),
                         (FAKE_CENTRE_HZ + FAKE_RATE_HZ / 4, "NFM", 12_000))
        self.assertAlmostEqual(audio["started"], time.time(), delta=10)
        self.assertAlmostEqual(audio["seconds"], (audio["bytes"] - 44) / 2 / 12_000, delta=0.05)    # to the tenth
        self.assertEqual((state["stopped"], state["folder"]), (None, str(self.folder)))
        engine.radios[0].configure_recording({"audio": False})
        self.assertEqual(engine.radios[0].recording_state(), {"type": "recording", "audio": None, "iq": None, "stopped": None,
                                                    "folder": str(self.folder), "default_folder": str(self.folder)})
        await self.stopped(engine)
        with wave.open(str(self.folder / audio["file"])) as recorded:
            self.assertEqual((recorded.getnchannels(), recorded.getframerate()), (1, 12_000))
            self.assertGreaterEqual(recorded.getnframes(), (audio["bytes"] - 44) / 2)     # and what came later

    async def test_goes_on_without_any_client_until_stopped(self):
        source = FakeSource()
        engine = self.engine(source)
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        engine.unsubscribe(subscription)
        reads = source.reads
        await asyncio.sleep(0.3)
        self.assertGreater(source.reads, reads + 5)
        engine.radios[0].configure_recording({"audio": False})
        await asyncio.sleep(0.05)       # the block read meanwhile
        reads = source.reads
        await asyncio.sleep(0.3)
        self.assertLessEqual(source.reads, reads + 1)

    async def test_a_recording_in_sam_is_named_after_it(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.radios[0].configure_demod({"frequency_hz": round(FAKE_TONE_HZ), "mode": "SAM"})
        engine.radios[0].configure_recording({"audio": True})
        audio = (await next_recording(subscription, with_file))["audio"]
        self.assertRegex(audio["file"], r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z_\d+\.\d{6}MHz_SAM\.wav$")
        self.assertEqual(audio["mode"], "SAM")

    async def test_never_on_the_simulated_band(self):
        engine = self.engine(SimulatedBandSource())
        with self.assertRaisesRegex(ValueError, "^the simulated band is not recorded$"):
            engine.radios[0].configure_recording({"audio": True})
        self.assertIsNone(engine.radios[0].recording_state()["audio"])
        self.assertFalse(self.folder.exists())

    async def test_the_simulated_band_stops_it_and_another_receiver_does_not(self):
        engine = self.engine(sources=SimulatedBandSources())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        await next_recording(subscription, with_file)
        engine.radios[0].configure({"kind": "other"})
        self.assertIsNotNone(engine.radios[0].recording_state()["audio"])
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine.radios[0].configure({"kind": "synthetic"})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["audio"], state["stopped"]),
                         (None, {"kind": "audio", "reason": "source", "detail": ""}))
        self.assertIn("stopped: the simulated band was chosen", logs.output[0])

    async def test_a_new_audio_rate_stops_it(self):
        engine = self.engine(WideSource())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        await next_recording(subscription, lambda state: with_file(state) and state["audio"]["rate_hz"] == 12_000)
        engine.radios[0].configure_demod({"mode": "AM"})      # the same rate: it goes on
        await asyncio.sleep(0.2)
        self.assertIsNotNone(engine.radios[0].recording_state()["audio"])
        with self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].configure_demod({"mode": "WFM"})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "rate", "detail": ""})

    async def test_stops_when_its_disk_falls_short_and_says_why_until_the_next(self):
        engine = self.engine()
        subscription = engine.subscribe()
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62), \
                self.assertLogs("radiolack", "WARNING") as logs:
            engine.radios[0].configure_recording({"audio": True})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "space", "detail": ""})
        self.assertIn("less than 2 GiB is free on its disk", logs.output[0])
        self.assertEqual(list(self.folder.iterdir()), [])
        engine.radios[0].configure_recording({"audio": True})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["audio"]["file"], state["stopped"]), (None, None))

    async def test_a_disk_error_stops_it(self):
        engine = self.engine()
        subscription = engine.subscribe()
        failure = OSError(errno.EIO, "Input/output error")
        with mock.patch.object(recordings, "_write_all", side_effect=failure), \
                self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].configure_recording({"audio": True})
            state = await next_recording(subscription, lambda state: state["audio"] is None)
        self.assertEqual(state["stopped"], {"kind": "audio", "reason": "disk", "detail": "Input/output error"})

    async def test_stopping_the_engine_closes_the_file(self):
        engine = self.engine()
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
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
        engine.radios[0].configure_recording({"audio": True})
        name = (await next_recording(subscription, with_file))["audio"]["file"]
        self.assertIsNone(engine.download_folder(name))
        with self.assertRaisesRegex(ValueError, "^this recording runs"):
            engine.configure_recordings({"delete": name}, subscription)
        engine.configure_recordings({}, subscription)
        [entry] = (await next_reply(subscription, "recordings"))["files"]
        self.assertEqual((entry["name"], entry["running"]), (name, True))
        engine.radios[0].configure_recording({"audio": False})
        self.assertEqual(engine.download_folder(name), self.folder)
        self.assertIsNone(engine.download_folder("../settings.json"))

    async def test_records_into_a_folder_chosen_which_is_saved(self):
        engine = self.engine(store=Store(self.directory), save_delay_s=0.01)
        subscription = engine.subscribe()
        chosen = self.base / "chosen"
        chosen.mkdir()
        engine.radios[0].configure_recording({"folder": str(chosen)}, subscription)
        state = await next_recording(subscription, lambda state: state["folder"] == str(chosen))
        self.assertEqual(state["default_folder"], str(self.folder))
        engine.radios[0].configure_recording({"audio": True})
        name = (await next_recording(subscription, with_file))["audio"]["file"]
        with self.assertRaisesRegex(ValueError, "^a recording runs"):
            engine.radios[0].configure_recording({"folder": None}, subscription)
        await self.stopped(engine)
        self.assertTrue((chosen / name).is_file())
        self.assertFalse(self.folder.exists())
        settings = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(settings["recordings"], {"folder": str(chosen)})
        again = self.engine(store=Store(self.directory), save_delay_s=0.01)
        self.assertEqual(again.radios[0].recording_state()["folder"], str(chosen))
        subscription = again.subscribe()
        again.radios[0].configure_recording({"folder": None}, subscription)
        await next_recording(subscription, lambda state: state["folder"] == str(self.folder))

    async def test_refuses_a_folder_it_cannot_record_into(self):
        engine = self.engine()
        subscription = engine.subscribe()
        missing = self.base / "missing"
        engine.radios[0].configure_recording({"folder": str(missing)}, subscription)
        self.assertEqual(await next_reply(subscription, "error"),
                         {"type": "error", "command": "recording", "message": f"{missing} does not exist"})
        for changes in ({"folder": "relative"}, {"folder": 3}, {"audio": "yes"}, {"audio": True, "folder": None},
                        {}, {"other": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.radios[0].configure_recording(changes, subscription)
        self.assertEqual(engine.radios[0].recording_state()["folder"], str(self.folder))

    async def test_a_remote_client_does_not_learn_the_folder(self):
        engine = Engine(FakeSource(), remote=RemoteAccess(), recordings=self.folder)
        self.addCleanup(engine.close)
        [state] = [message for message in ready_messages(engine.subscribe(device="5f0c2a91"))
                   if message["type"] == "recording"]
        self.assertEqual(state, {"type": "recording", "audio": None, "iq": None, "stopped": None})

    async def test_without_a_folder_nothing_is_recorded(self):
        engine = Engine(FakeSource())
        self.addCleanup(engine.close)
        subscription = engine.subscribe()
        for command in (lambda: engine.radios[0].configure_recording({"audio": True}),
                        lambda: engine.radios[0].configure_recording({"folder": None}),
                        lambda: engine.configure_recordings({}, subscription)):
            with self.assertRaisesRegex(ValueError, "^this Radiolack does not record$"):
                command()
        self.assertIsNone(engine.download_folder(RECORDED_NAME))


class IQRecordingTest(RecordingCase):
    """Recording the receiver's raw I/Q, from the fake rtl_tcp, whose
    samples tell their setting: a capture for each retune, stopped by a new
    sample rate, another receiver and the disk, never on the simulated band
    nor from a source without raw samples, at the same time as the audio or
    not; its files listed, downloaded and deleted together."""

    async def receiver(self, **options) -> RtlTcpSource:
        """An rtl_tcp receiver: the fake rtl_tcp, on 145 MHz."""
        self.fake = FakeRtlTcp()
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        return RtlTcpSource(LOOPBACK, self.fake.port, settle_s=0.1, **options)

    @staticmethod
    def iq_bytes(state: dict) -> int:
        """How much the I/Q recording that runs was given; -1 when none runs."""
        return state["iq"]["bytes"] if state["iq"] is not None else -1

    async def test_records_the_samples_with_a_capture_for_each_retune(self):
        engine = self.engine(await self.receiver(sample_rate_hz=1_024_000))
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription, lambda state: with_iq_file(state) and state["iq"]["bytes"] >= 50_000)
        iq = state["iq"]
        self.assertRegex(iq["file"], r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z_145\.000000MHz_1\.024000MSps\.sigmf-data$")
        self.assertEqual((iq["frequency_hz"], iq["rate_hz"]), (145e6, 1_024_000))
        self.assertAlmostEqual(iq["started"], time.time(), delta=10)
        self.assertAlmostEqual(iq["seconds"], iq["bytes"] / 2 / 1_024_000, delta=0.05)    # to the tenth
        self.assertEqual((state["audio"], state["stopped"]), (None, None))
        for changes in ({"centre_hz": 433_000_000}, {"ppm": 3}):    # a correction too: on the same frequency
            given = self.iq_bytes(engine.radios[0].recording_state())
            engine.radios[0].configure(changes)
            await next_recording(subscription, lambda state: self.iq_bytes(state) >= given + 200_000)
        engine.radios[0].configure_recording({"iq": False})
        self.assertIsNone(engine.radios[0].recording_state()["iq"])
        await self.stopped(engine)
        data = (self.folder / iq["file"]).read_bytes()
        metadata = json.loads((self.folder / meta_name(iq["file"])).read_text(encoding="ascii"))
        self.assertEqual(metadata["global"], {"core:datatype": "cu8", "core:sample_rate": 1_024_000,
                                              "core:version": "1.0.0", "core:hw": "RTL-SDR, Rafael Micro R820T",
                                              "core:recorder": f"Radiolack {__version__}"})
        captures = metadata["captures"]
        # 145 and 433 MHz lie on the R820T's grid at 1.024 MS/s; the correction keeps the
        # tuner on its grid point, which it moves on the air by 3 ppm: the samples' label.
        self.assertEqual([capture["core:frequency"] for capture in captures], [145_000_000, 433_000_000, 433_001_299])
        # Each capture holds the samples of its setting, as rtl_tcp sent them.
        starts = [capture["core:sample_start"] for capture in captures] + [len(data) // 2]
        for tuned_hz, start, end in zip((145_000_000, 433_000_000, 433_000_000), starts, starts[1:]):
            segment = data[2 * start:2 * end]
            i_code, q_code = fake_codes(tuned_hz, 1_024_000)
            self.assertTrue(segment)
            self.assertEqual((set(segment[0::2]), set(segment[1::2])), ({i_code}, {q_code}))

    async def test_holds_the_impulses_as_received_with_the_noise_blanker_on(self):
        self.fake = FakeRtlTcp(carriers={145_010_000.0: 0.1}, impulses=(1_000, 2))
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        engine = self.engine(RtlTcpSource(LOOPBACK, self.fake.port, sample_rate_hz=240_000, settle_s=0.1))
        subscription = engine.subscribe()
        engine.radios[0].configure_demod({"frequency_hz": 145_010_000, "mode": "AM", "blanker": True})
        await eventually(lambda: engine.radios[0].demod_state()["blanker_impulses"])      # 240 a second, blanked
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription, lambda state: with_iq_file(state) and state["iq"]["bytes"] >= 400_000)
        engine.radios[0].configure_recording({"iq": False})
        await self.stopped(engine)
        data = np.frombuffer((self.folder / state["iq"]["file"]).read_bytes(), np.uint8).reshape(-1, 2)
        # Every impulse the fake sent, two samples every 1000, as it sent them.
        full = np.flatnonzero(np.any((data == 0) | (data == 255), axis=1))
        starts = full[np.concatenate(([True], np.diff(full) > 1))]
        ends = full[np.concatenate((np.diff(full) > 1, [True]))]
        self.assertGreater(len(starts), 150)
        # The first and the last may be cut by the recording's start and end.
        self.assertEqual(set(np.diff(starts)[1:].tolist()), {1_000})
        self.assertEqual(set((ends - starts + 1)[1:-1].tolist()), {2})

    async def test_goes_on_without_any_client(self):
        engine = self.engine(await self.receiver())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        await next_recording(subscription, with_iq_file)
        engine.unsubscribe(subscription)
        given = self.iq_bytes(engine.radios[0].recording_state())
        await eventually(lambda: self.iq_bytes(engine.radios[0].recording_state()) > given + 100_000)
        engine.radios[0].configure_recording({"iq": False})
        self.assertIsNone(engine.radios[0].recording_state()["iq"])

    async def test_a_new_sample_rate_or_another_receiver_stops_it(self):
        engine = self.engine(await self.receiver())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        await next_recording(subscription, with_iq_file)
        engine.radios[0].configure({"gain_db": 20})       # it goes on
        with self.assertRaises(ValueError):
            engine.radios[0].configure({"sample_rate_hz": 0})     # refused: it goes on
        self.assertIsNotNone(engine.radios[0].recording_state()["iq"])
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine.radios[0].configure({"sample_rate_hz": 1_024_000})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"], state["stopped"]), (None, {"kind": "iq", "reason": "sample_rate", "detail": ""}))
        self.assertIn("stopped: the receiver's sample rate changed", logs.output[0])
        # An audio recording leaves the I/Q's stop as it is; an I/Q recording clears it.
        engine.radios[0].configure_recording({"audio": True})
        engine.radios[0].configure_recording({"audio": False})
        self.assertEqual(engine.radios[0].recording_state()["stopped"]["kind"], "iq")
        engine.radios[0].configure_recording({"iq": True})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"]["rate_hz"], state["stopped"]), (1_024_000, None))
        await next_recording(subscription, with_iq_file)
        with self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].configure({"address": f"{LOOPBACK}:{free_port()}"})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"], state["stopped"]), (None, {"kind": "iq", "reason": "receiver", "detail": ""}))

    async def test_another_kind_of_source_stops_it(self):
        engine = self.engine(await self.receiver(), sources=FakeSources())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        engine.radios[0].configure_recording({"iq": True})
        await next_recording(subscription, lambda state: with_file(state) and with_iq_file(state))
        with self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].configure({"kind": "test"})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"], state["stopped"]), (None, {"kind": "iq", "reason": "receiver", "detail": ""}))
        self.assertIsNotNone(state["audio"])        # another receiver goes on with the audio

    async def test_never_on_the_simulated_band_nor_from_a_source_without_raw_samples(self):
        for source, refusal in ((SimulatedBandSource(), "^the simulated band is not recorded$"),
                                (FakeSource(), "^the test source has no raw samples to record$")):
            engine = self.engine(source)
            with self.subTest(kind=source.kind), self.assertRaisesRegex(ValueError, refusal):
                engine.radios[0].configure_recording({"iq": True})
            self.assertIsNone(engine.radios[0].recording_state()["iq"])
        with self.assertRaisesRegex(ValueError, "^the I/Q is recorded or not: true or false$"):
            engine.radios[0].configure_recording({"iq": "yes"})
        self.assertFalse(self.folder.exists())

    async def test_a_disk_error_stops_it(self):
        engine = self.engine(await self.receiver())
        subscription = engine.subscribe()
        failure = OSError(errno.EIO, "Input/output error")
        with mock.patch.object(recordings, "_write_all", side_effect=failure), \
                self.assertLogs("radiolack", "WARNING"):
            engine.radios[0].configure_recording({"iq": True})
            state = await next_recording(subscription, lambda state: state["stopped"] is not None)
        self.assertEqual((state["iq"], state["stopped"]),
                         (None, {"kind": "iq", "reason": "disk", "detail": "Input/output error"}))

    async def test_records_with_the_audio_its_files_neither_deleted_nor_downloaded_while_it_runs(self):
        engine = self.engine(await self.receiver(sample_rate_hz=1_024_000))
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"audio": True})
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription, lambda state: with_file(state) and with_iq_file(state))
        audio, iq = state["audio"]["file"], state["iq"]["file"]
        meta = meta_name(iq)
        await eventually(lambda: (self.folder / meta).exists())
        for name in (audio, iq, meta):
            self.assertIsNone(engine.download_folder(name))
        for name in (audio, iq):
            with self.assertRaisesRegex(ValueError, "^this recording runs"):
                engine.configure_recordings({"delete": name}, subscription)
        with self.assertRaisesRegex(ValueError, "^delete takes the file of a recording$"):
            engine.configure_recordings({"delete": meta}, subscription)     # it goes with its data
        with self.assertRaisesRegex(ValueError, "^a recording runs"):
            engine.radios[0].configure_recording({"folder": None}, subscription)
        engine.configure_recordings({}, subscription)
        files = (await next_reply(subscription, "recordings"))["files"]
        self.assertEqual({(entry["name"], entry["kind"], entry["running"]) for entry in files},
                         {(audio, "audio", True), (iq, "iq", True)})
        engine.radios[0].configure_recording({"audio": False})
        self.assertIsNotNone(engine.radios[0].recording_state()["iq"])       # each stops on its own
        engine.radios[0].configure_recording({"iq": False})
        for name in (audio, iq, meta):
            self.assertEqual(engine.download_folder(name), self.folder)

    async def test_a_recording_deleted_as_it_stops_leaves_nothing(self):
        engine = self.engine(await self.receiver())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        iq = (await next_recording(subscription, with_iq_file))["iq"]["file"]
        given = self.iq_bytes(engine.radios[0].recording_state())
        engine.radios[0].configure({"centre_hz": 433_000_000})    # a capture that its metadata gets at the end
        await next_recording(subscription, lambda state: self.iq_bytes(state) >= given + 100_000)
        close = recordings.IQRecording._close

        def slow_close(recording):
            time.sleep(0.3)         # a large file reaching the disk
            return close(recording)

        with mock.patch.object(recordings.IQRecording, "_close", slow_close):
            engine.radios[0].configure_recording({"iq": False})
            engine.configure_recordings({"delete": iq}, subscription)
            self.assertEqual((await next_reply(subscription, "recordings"))["files"], [])
            await self.stopped(engine)      # its thread done too
        self.assertEqual(list(self.folder.iterdir()), [])

    async def test_stopping_the_engine_closes_its_files(self):
        engine = self.engine(await self.receiver())
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription, lambda state: self.iq_bytes(state) >= 100_000)
        await self.stopped(engine)
        iq = state["iq"]["file"]
        self.assertGreaterEqual((self.folder / iq).stat().st_size, state["iq"]["bytes"])
        metadata = json.loads((self.folder / meta_name(iq)).read_text(encoding="ascii"))
        self.assertEqual(len(metadata["captures"]), 1)


class ConverterTest(RecordingCase):
    """A converter in front of an rtl_tcp receiver, which stays where it is
    tuned when the converter changes: the VFO and the signals stay on what
    they were on, at their new frequencies; a scan and an I/Q recording,
    whose frequencies move, stop."""

    async def receiver(self, carriers: dict[float, float] | None = None, **options) -> RtlTcpSource:
        """An rtl_tcp receiver: the fake rtl_tcp, on 145 MHz."""
        self.fake = FakeRtlTcp(carriers=carriers, chunk_bytes=16_384, interval_s=0.004)
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        return RtlTcpSource(LOOPBACK, self.fake.port, settle_s=0.1, **options)

    async def test_the_vfo_stays_on_its_signal_at_its_new_frequency(self):
        engine = self.engine(RtlTcpSource(LOOPBACK, free_port()), run=False)
        self.assertEqual(engine.radios[0].demod_state()["frequency_hz"], 145_600_000)
        for changes, centre_hz, vfo_hz in (({"converter": "up"}, 20e6, 20.6e6),
                                           ({"converter": "down_inverted", "converter_lo_hz": 1_000_000_000},
                                            855e6, 854.4e6),
                                           ({"converter": "none"}, 145e6, 145.6e6)):
            with self.subTest(changes=changes):
                engine.radios[0].configure(changes)
                self.assertEqual((engine.radios[0].state()["centre_hz"], engine.radios[0].demod_state()["frequency_hz"]),
                                 (centre_hz, vfo_hz))
        with self.assertRaisesRegex(ValueError, "would be -20 MHz on the air"):
            engine.radios[0].configure({"converter": "down_inverted", "converter_lo_hz": 125_000_000})
        self.assertEqual((engine.radios[0].state()["converter"], engine.radios[0].demod_state()["frequency_hz"]), ("none", 145.6e6))

    async def test_a_scan_stops_and_the_signals_found_are_found_again_at_their_new_frequency(self):
        carrier_hz = 145_300_000.0
        engine = self.engine(await self.receiver(carriers={carrier_hz: 0.2}))
        subscription = engine.subscribe()
        await asyncio.wait_for(next_peaks(subscription, until=lambda peaks: found_all(peaks, [carrier_hz])), 15)
        engine.radios[0].configure_scanner({"sweep_low_hz": 144e6, "sweep_high_hz": 146e6})
        engine.radios[0].configure_scanner({"scan": "sweep"})
        engine.radios[0].configure({"converter": "down", "converter_lo_hz": 9_750_000_000})
        self.assertEqual(engine.radios[0].scanner_state()["stopped"], "tuned")
        self.assertEqual(engine.radios[0].peaks_state()["peaks"], [])     # found through the previous converter
        engine.radios[0].configure({"centre_hz": 9_895_000_000})          # 145 MHz for the receiver, as before the sweep
        await asyncio.wait_for(
            next_peaks(subscription, until=lambda peaks: found_all(peaks, [carrier_hz + 9_750_000_000])), 15)

    async def test_an_iq_recording_stops_and_its_metadata_gives_the_frequencies_on_the_air(self):
        engine = self.engine(await self.receiver(sample_rate_hz=1_024_000))
        engine.radios[0].configure({"converter": "down_inverted", "converter_lo_hz": 5_150_000_000, "centre_hz": 3.8e9})
        subscription = engine.subscribe()
        engine.radios[0].configure_recording({"iq": True})
        state = await next_recording(subscription, lambda state: with_iq_file(state) and state["iq"]["bytes"] >= 50_000)
        iq = state["iq"]
        self.assertRegex(iq["file"], r"_3800\.000000MHz_1\.024000MSps\.sigmf-data$")
        engine.radios[0].configure({"centre_hz": 3.7e9})          # a retune goes on recording
        await next_recording(subscription, lambda state: self.iq_bytes(state) >= iq["bytes"] + 200_000)
        engine.radios[0].configure({"converter_lo_hz": 5_150_000_000})     # the one in use: nothing changes
        self.assertIsNotNone(engine.radios[0].recording_state()["iq"])
        with self.assertLogs("radiolack", "WARNING") as logs:
            engine.radios[0].configure({"converter_lo_hz": 5_150_001_000})
        state = engine.radios[0].recording_state()
        self.assertEqual((state["iq"], state["stopped"]), (None, {"kind": "iq", "reason": "converter", "detail": ""}))
        self.assertIn("stopped: the converter in front of the receiver changed", logs.output[0])
        await self.stopped(engine)
        data = (self.folder / iq["file"]).read_bytes()
        metadata = json.loads((self.folder / meta_name(iq["file"])).read_text(encoding="ascii"))
        self.assertEqual(metadata["global"]["core:hw"], "RTL-SDR, Rafael Micro R820T, behind a downconverter that "
                                                        "inverts the spectrum with its oscillator at 5150 MHz")
        captures = metadata["captures"]
        self.assertEqual([capture["core:frequency"] for capture in captures], [3_800_012_500, 3_700_000_000])
        # The receiver tuned 1350 MHz placed on its grid, then 1450 MHz, on it; I and Q come
        # swapped, as on the air. The file is named after the centre asked.
        starts = [capture["core:sample_start"] for capture in captures] + [len(data) // 2]
        for tuned_hz, start, end in zip((1_349_987_500, 1_450_000_000), starts, starts[1:]):
            segment = data[2 * start:2 * end]
            i_code, q_code = fake_codes(tuned_hz, 1_024_000)
            self.assertTrue(segment)
            self.assertEqual((set(segment[0::2]), set(segment[1::2])), ({q_code}, {i_code}))

    @staticmethod
    def iq_bytes(state: dict) -> int:
        """How much the I/Q recording that runs was given; -1 when none runs."""
        return state["iq"]["bytes"] if state["iq"] is not None else -1


async def next_carrier(subscription: Subscription, timeout: float = 20.0) -> dict:
    """The next carrier measure that ``subscription`` receives."""
    async def wait() -> dict:
        while True:
            message = await subscription.get()
            if isinstance(message, dict) and message["type"] == "carrier":
                return message
    return await asyncio.wait_for(wait(), timeout)


def calibrated_ppm(ppm: float, measured_hz: float, true_hz: float, tuned=lambda f: f) -> float:
    """The correction that calibration.js's calibratedPpm computes, to the
    hundredth: (1 + ppm) × T_s / T_m − 1, on the frequencies the receiver
    tunes."""
    return round(((1 + ppm / 1e6) * tuned(true_hz) / tuned(measured_hz) - 1) * 1e6, 2)


class CarrierTest(unittest.IsolatedAsyncioTestCase):
    """The carrier's measure, on the fake rtl_tcp: the frequency on the air,
    in every mode, through direct sampling and a converter, with and
    without a crystal error, and the correction that a calibration
    computes from it, which brings the carrier to its true frequency."""

    async def start(self, carriers: dict[float, float], *, crystal_ppb: int = 0, rate_hz: float = 1_024_000.0,
                    centre_hz: float = 145_000_000.0, tuner_pll=None, impulses: tuple[int, int] | None = None,
                    **settings) -> tuple[Engine, Subscription, FakeRtlTcp]:
        fake = FakeRtlTcp(carriers=carriers, crystal_ppb=crystal_ppb, chunk_bytes=16_384, interval_s=0.004,
                          tuner_pll=tuner_pll, impulses=impulses)
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, fake.port, sample_rate_hz=rate_hz, settle_s=0.1)
        engine = Engine(source)
        task = asyncio.ensure_future(engine.run())

        async def stop():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=5)

        self.addAsyncCleanup(stop)
        subscription = engine.subscribe()
        engine.radios[0].configure({**settings, "centre_hz": centre_hz})      # on the air, behind a converter
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")
        return engine, subscription, fake

    async def measure(self, engine: Engine, subscription: Subscription, request_id: int = 1) -> dict:
        engine.radios[0].measure_carrier({"action": "measure", "id": request_id}, subscription)
        result = await next_carrier(subscription)
        self.assertEqual(result["id"], request_id)
        self.assertIsNone(engine.radios[0]._carrier)          # nobody waits: nothing more is measured
        return result

    async def test_measures_the_frequency_on_the_air_in_every_mode(self):
        air_hz = 145_012_345.6
        # The VFO on the carrier, or beside it in SSB, so that it lies within the filter.
        vfos = {"NFM": 0, "AM": 0, "SAM": 0, "USB": -1000, "LSB": 1000, "CW": 0, "WFM": 0}
        for crystal_ppb in (0, 37_420):
            # WFM last, at a sample rate that holds its channel; the others at the lowest, which is faster.
            engine, subscription, fake = await self.start({air_hz: 0.2}, crystal_ppb=crystal_ppb, rate_hz=240_000.0,
                                                           ppm=crystal_ppb / 1000)
            for mode in sorted(MODES, key=lambda mode: mode == "WFM"):
                with self.subTest(crystal_ppb=crystal_ppb, mode=mode):
                    if mode == "WFM":
                        engine.radios[0].configure({"sample_rate_hz": 1_024_000.0})
                    engine.radios[0].configure_demod({"frequency_hz": round(air_hz + vfos[mode]), "mode": mode})
                    result = await self.measure(engine, subscription)
                    self.assertIsNone(result["reason"])
                    self.assertAlmostEqual(result["frequency_hz"], air_hz, delta=1.0)
                    self.assertGreater(result["margin_db"], 40)
            engine.close()          # the next receiver alone streams
            fake.sending = False

    async def test_a_calibration_brings_the_carrier_to_its_true_frequency(self):
        offset_hz = 10_000.0
        # The frequency on the air, the settings, and where the receiver tunes for a frequency on the air.
        paths = (("tuner", 145_000_000.0, {}, lambda f: f),
                 ("direct sampling", 7_100_000.0, {"hf_input": "q"}, lambda f: f),
                 ("upconverter", 7_100_000.0, {"converter": "up", "converter_lo_hz": 125_000_000.0},
                  lambda f: f + 125_000_000.0))
        for crystal_ppb in (37_420, -52_730):
            for path, centre_hz, settings, tuned in paths:
                with self.subTest(crystal_ppb=crystal_ppb, path=path):
                    air_hz = centre_hz + offset_hz
                    engine, subscription, fake = await self.start(
                        {tuned(air_hz): 0.2}, crystal_ppb=crystal_ppb, rate_hz=240_000.0, **settings,
                        centre_hz=centre_hz)
                    # Off by the crystal's error at the frequency the receiver tunes: the VFO on it.
                    expected_hz = tuned(air_hz) / (1 + crystal_ppb / 1e9) - (tuned(air_hz) - air_hz)
                    engine.radios[0].configure_demod({"frequency_hz": round(expected_hz), "mode": "AM"})
                    before = await self.measure(engine, subscription)
                    self.assertAlmostEqual(before["frequency_hz"], expected_hz, delta=1.0)
                    self.assertGreater(abs(before["frequency_hz"] - air_hz), 100.0)
                    ppm = calibrated_ppm(0.0, before["frequency_hz"], air_hz, tuned)
                    self.assertAlmostEqual(ppm, crystal_ppb / 1000, delta=0.02)
                    # Applied as the dialog does: the correction, and the VFO on the true frequency.
                    engine.radios[0].configure({"ppm": ppm})
                    engine.radios[0].configure_demod({"frequency_hz": air_hz})
                    after = await self.measure(engine, subscription, 2)
                    self.assertAlmostEqual(after["frequency_hz"], air_hz, delta=1.0)
                    engine.close()          # the next path's receiver alone streams
                    fake.sending = False

    # Two or three centres for each divider of the tuner at 240 kS/s (IF 2.125 MHz): 64 up to an
    # oscillator of 55.3 MHz, then 32, 16, 8, 4 and 2. None where the VCO is a multiple of 57.6 MHz.
    GRID_CENTRES_HZ = {64: (30_100_000, 47_000_000), 32: (70_000_000, 99_000_000),
                       16: (120_000_000, 145_000_000, 200_000_000), 8: (230_000_000, 433_000_000),
                       4: (500_000_000, 860_000_000), 2: (1_000_000_000, 1_296_000_000, 1_700_000_000)}

    async def carriers_on_the_grid(self, computation: str, driver_pll: str, crystal_ppb: int = 0
                                   ) -> dict[int, list[tuple[float, float]]]:
        """The carrier measured, and where it is on the air, at every centre
        of GRID_CENTRES_HZ, by divider, the tuner following ``computation``
        and Radiolack labelling by ``driver_pll``, its crystal
        ``crystal_ppb`` off and corrected."""
        rate = 240_000
        tuned = functools.partial(r82xx.tuned_hz, computation)
        engine, subscription, fake = await self.start({}, crystal_ppb=crystal_ppb, rate_hz=float(rate),
                                                       tuner_pll=tuned, ppm=crystal_ppb / 1000,
                                                       driver_pll=driver_pll)
        source = engine.radios[0].source
        measures: dict[int, list[tuple[float, float]]] = {}
        for divider, centres in self.GRID_CENTRES_HZ.items():
            for centre_hz in centres:
                air_hz = centre_hz + 30_000.4
                fake.carriers = {air_hz: 0.2}
                engine.radios[0].configure({"centre_hz": centre_hz})
                engine.radios[0].configure_demod({"frequency_hz": round(air_hz), "mode": "AM"})
                in_use = source.centre_in_use(centre_hz)
                self.assertLessEqual(abs(in_use - centre_hz), 58_000)
                # The oscillator on the grid nearest the centre in use: the tuner errs by far less.
                oscillator = round((in_use / (1 + crystal_ppb / 1e9) + r82xx.rate_if_hz(rate)) / 112_500) * 112_500
                self.assertEqual(r82xx.mix_divider(oscillator), divider)
                await eventually(lambda: engine.radios[0]._capture is not None and engine.radios[0]._capture[1][0] == in_use
                                 and fake.frequency_hz + r82xx.rate_if_hz(rate) == oscillator)
                self.assertEqual(engine.radios[0].state()["centre_in_use_hz"], in_use)    # what the source reports
                result = await self.measure(engine, subscription, centre_hz)
                self.assertIsNone(result["reason"], centre_hz)
                measures.setdefault(divider, []).append((result["frequency_hz"], air_hz))
        engine.close()
        fake.sending = False
        return measures

    async def test_on_the_grid_a_carrier_measures_where_it_is_once_the_driver_is_known(self):
        for computation, crystal_ppb in (("truncating", 0), ("rounding", -23_450)):
            with self.subTest(computation=computation):
                measures = await self.carriers_on_the_grid(computation, computation, crystal_ppb)
                for divider, found in measures.items():
                    for measured_hz, air_hz in found:
                        self.assertAlmostEqual(measured_hz, air_hz, delta=1.0, msg=f"divider {divider}")

    async def test_on_the_grid_the_older_driver_unknown_sits_one_constant_off_in_each_divider(self):
        measures = await self.carriers_on_the_grid("truncating", "unknown")
        # The tuner sits that far low, so the carrier shows that far high: 1757.8 Hz over the divider.
        for divider, found in measures.items():
            for measured_hz, air_hz in found:
                self.assertAlmostEqual(measured_hz - air_hz, 1757.8 / divider, delta=1.0, msg=f"divider {divider}")

    async def test_takes_the_channel_as_received_with_the_noise_blanker_on(self):
        # Two samples of impulse every 500, which raise the noise within the
        # filter tenfold: blanked, the channel would give a margin about
        # 10 dB higher (test_blanker.py); the measure takes it unblanked.
        air_hz = 145_010_000.0
        engine, subscription, _ = await self.start({air_hz: 0.1}, rate_hz=240_000.0, impulses=(500, 2))
        engine.radios[0].configure_demod({"frequency_hz": round(air_hz), "mode": "AM"})
        plain = await self.measure(engine, subscription)
        engine.radios[0].configure_demod({"blanker": True})
        await eventually(lambda: engine.radios[0].demod_state()["blanker_impulses"])      # 480 a second
        self.assertGreater(engine.radios[0].demod_state()["blanker_impulses"], 400)
        blanked = await self.measure(engine, subscription, 2)
        for result in (plain, blanked):
            self.assertIsNone(result["reason"])
            self.assertAlmostEqual(result["frequency_hz"], air_hz, delta=1.0)
        self.assertAlmostEqual(blanked["frequency_hz"], plain["frequency_hz"], delta=0.2)
        self.assertAlmostEqual(blanked["margin_db"], plain["margin_db"], delta=1.0)

    async def test_says_why_it_gives_no_frequency(self):
        air_hz = 145_010_000.0
        engine, subscription, _ = await self.start({air_hz: 0.2}, rate_hz=240_000.0)
        cases = ((145_050_000, "AM", "no_signal"),      # within the span, nothing there
                 (145_500_000, "AM", "silent"),         # outside the span
                 (air_hz, "WFM", "silent"))             # WFM needs a faster sample rate
        for frequency, mode, reason in cases:
            with self.subTest(frequency=frequency, mode=mode):
                engine.radios[0].configure_demod({"frequency_hz": frequency, "mode": mode})
                result = await self.measure(engine, subscription)
                self.assertEqual((result["reason"], result["frequency_hz"]), (reason, None))
                if reason == "no_signal":
                    self.assertLess(result["margin_db"], 20)
                else:
                    self.assertIsNone(result["margin_db"])

    async def test_a_change_meanwhile_starts_the_measure_again(self):
        air_hz = 145_010_000.0
        engine, subscription, _ = await self.start({air_hz: 0.2}, rate_hz=240_000.0)
        engine.radios[0].configure_demod({"frequency_hz": air_hz, "mode": "AM"})
        engine.radios[0].measure_carrier({"action": "measure", "id": 1}, subscription)
        await asyncio.sleep(0.2)
        engine.radios[0].configure_demod({"mode": "CW"})        # the carrier moves to the CW pitch in the channel
        result = await next_carrier(subscription)
        self.assertAlmostEqual(result["frequency_hz"], air_hz, delta=1.0)

    async def test_measures_while_a_scan_runs_which_goes_on(self):
        air_hz = 145_010_000.0
        engine, subscription, _ = await self.start({air_hz: 0.2}, rate_hz=240_000.0)
        engine.edit_bookmarks({"action": "add", "frequency_hz": air_hz, "name": "Carrier", "mode": "AM",
                               "scan": True})
        engine.radios[0].configure_scanner({"scan": "memory"})
        await asyncio.wait_for(next_scanner(subscription, until=in_state("listening")), 10)
        result = await self.measure(engine, subscription)
        self.assertAlmostEqual(result["frequency_hz"], air_hz, delta=1.0)
        self.assertEqual(engine.radios[0].scanner_state()["scan"], "memory")

    async def test_answers_an_unsteady_measure_once_its_time_is_up(self):
        engine, subscription, fake = await self.start({145_010_000.0: 0.2}, rate_hz=240_000.0)
        fake.sending = False        # no samples: the source waits
        with mock.patch("radiolack.radio.CARRIER_TIMEOUT_S", 0.5):
            engine.radios[0].measure_carrier({"action": "measure"}, subscription)
            result = await next_carrier(subscription)
        self.assertEqual((result["id"], result["reason"], result["frequency_hz"]), (None, "silent", None))


class CarrierRequestTest(unittest.IsolatedAsyncioTestCase):
    """Who receives a carrier's measure, and the refused requests."""

    async def asyncSetUp(self):
        self.engine = Engine(FakeSource())
        self.task = asyncio.ensure_future(self.engine.run())

    async def asyncTearDown(self):
        self.engine.close()
        await asyncio.wait_for(self.task, timeout=5)

    async def test_interfaces_that_ask_meanwhile_share_one_measure_each_with_its_id(self):
        first, second, third = (self.engine.subscribe() for _ in range(3))
        self.engine.radios[0].measure_carrier({"action": "measure", "id": 1}, first)
        self.engine.radios[0].measure_carrier({"action": "measure", "id": 7}, second)
        measure = self.engine.radios[0]._carrier
        self.engine.radios[0].measure_carrier({"action": "measure", "id": 8}, second)     # asks again: one answer
        self.assertIs(self.engine.radios[0]._carrier, measure)
        answers = [await next_carrier(first), await next_carrier(second)]
        self.assertEqual([answer["id"] for answer in answers], [1, 8])
        self.assertEqual(answers[0], {**answers[1], "id": 1})
        await asyncio.sleep(0.2)
        self.assertNotIn("carrier", [m["type"] for m in ready_messages(second) if isinstance(m, dict)])
        self.assertNotIn("carrier", [m["type"] for m in ready_messages(third) if isinstance(m, dict)])

    async def test_an_interface_that_goes_away_stops_the_measure_unless_another_waits(self):
        first, second = self.engine.subscribe(), self.engine.subscribe()
        self.engine.radios[0].measure_carrier({"action": "measure"}, first)
        self.engine.radios[0].measure_carrier({"action": "measure"}, second)
        self.engine.unsubscribe(first)
        self.assertIsNotNone(self.engine.radios[0]._carrier)
        self.engine.unsubscribe(second)
        self.assertIsNone(self.engine.radios[0]._carrier)

    async def test_refuses_another_action_field_or_id(self):
        subscription = self.engine.subscribe()
        for changes in ({}, {"action": "start"}, {"action": "measure", "id": 0}, {"action": "measure", "id": True},
                        {"action": "measure", "id": 1.5}, {"action": "measure", "id": 2 ** 53},
                        {"action": "measure", "every": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.radios[0].measure_carrier(changes, subscription)
        self.assertIsNone(self.engine.radios[0]._carrier)


async def next_driver(subscription: Subscription, timeout: float = 30.0) -> dict:
    """The next answer to a check of the driver's computation that
    ``subscription`` receives."""
    async def wait() -> dict:
        while True:
            message = await subscription.get()
            if isinstance(message, dict) and message["type"] == "driver_pll":
                return message
    return await asyncio.wait_for(wait(), timeout)


class DriverCheckTest(unittest.IsolatedAsyncioTestCase):
    """The check of the driver's computation, on the fake rtl_tcp whose
    tuner follows either computation: the computation found whatever the
    crystal, the correction and the computation set; the receiver back on
    its centre and its grid whatever ends the check; the refusals; and the
    correction that a calibration then computes."""

    RATE_HZ = 240_000.0
    CENTRE_HZ = 145_000_000.0       # placed at 145.025 MHz at this rate
    OFFSET_HZ = 35_000.4            # the carrier, from the centre asked, on the tuner's frequencies

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    async def start(self, computation: str | None = "truncating", *, crystal_ppb: int = 37_420, ppm: float = 0.0,
                    driver_pll: str = "unknown", tuner_type: int = 5, converter: dict | None = None,
                    level: float = 0.2, impulses: tuple[int, int] | None = None,
                    **settings) -> tuple[Engine, Subscription, FakeRtlTcp]:
        """An engine on the fake rtl_tcp with a carrier OFFSET_HZ above the
        centre asked, its tuner following ``computation`` (exact when None)
        and its crystal ``crystal_ppb`` off, with the correction ``ppm``,
        the driver set to ``driver_pll`` and the VFO on the carrier as it
        measures, in AM; it saves into a temporary folder, and records
        there. The carrier's amplitude is ``level``, and the fake's
        ``impulses`` come with it."""
        conversion = Conversion(converter["converter"], converter["converter_lo_hz"]) if converter else Conversion()
        tuned_carrier = self.CENTRE_HZ + self.OFFSET_HZ
        fake = FakeRtlTcp(carriers={tuned_carrier: level}, crystal_ppb=crystal_ppb, chunk_bytes=16_384,
                          interval_s=0.004, tuner_type=tuner_type, impulses=impulses,
                          tuner_pll=None if computation is None else functools.partial(r82xx.tuned_hz, computation))
        await fake.__aenter__()
        self.addAsyncCleanup(fake.__aexit__, None, None, None)
        source = RtlTcpSource(LOOPBACK, fake.port, sample_rate_hz=self.RATE_HZ, settle_s=0.1)
        engine = Engine(source, store=Store(self.base / "config"), recordings=self.base / "recordings",
                        save_delay_s=0.05)
        task = asyncio.ensure_future(engine.run())

        async def stop_engine():
            engine.close()
            await asyncio.wait_for(task, timeout=5)
            await asyncio.wait_for(engine.wait_closed(), timeout=15)

        self.addAsyncCleanup(stop_engine)
        subscription = engine.subscribe()
        await eventually(lambda: engine.radios[0].state()["status"] == "streaming")     # the tuner known
        if converter:
            engine.radios[0].configure(converter)
        engine.radios[0].configure({**settings, "ppm": ppm, "driver_pll": driver_pll,
                          "centre_hz": conversion.air(self.CENTRE_HZ)})
        # Where the carrier measures, near enough: the crystal's error less the correction.
        engine.radios[0].configure_demod({"frequency_hz": round(conversion.air(
            tuned_carrier * (1 + ppm / 1e6) / (1 + crystal_ppb / 1e9))), "mode": "AM"})
        self.home = engine.radios[0].state()["centre_hz"]
        self.air_hz = conversion.air(tuned_carrier)
        return engine, subscription, fake

    async def identify(self, engine: Engine, subscription: Subscription, request_id: int = 1) -> dict:
        engine.radios[0].measure_carrier({"action": "identify", "id": request_id}, subscription)
        answer = await next_driver(subscription)
        self.assertEqual(answer["id"], request_id)
        return answer

    @staticmethod
    async def off_the_grid(engine: Engine) -> None:
        """Return once the check moved the receiver off its grid."""
        await eventually(lambda: not engine.radios[0].state()["tuner_grid"])

    async def assert_back(self, engine: Engine, fake: FakeRtlTcp | None = None) -> None:
        """The receiver on the centre and the grid of the check's start,
        and with ``fake``, asked for the grid's point again."""
        state = engine.radios[0].state()
        self.assertEqual((state["centre_hz"], state["tuner_grid"]), (self.home, True))
        self.assertIsNone(engine.radios[0]._driver_check)
        request = engine.radios[0].source.pll_tuning()[0]
        self.assertEqual((request + r82xx.rate_if_hz(int(self.RATE_HZ))) % r82xx.GRID_HZ, 0)    # on the grid
        if fake is not None:
            await eventually(lambda: fake.frequency_hz == request)

    def saved_source(self) -> dict | None:
        """The receiver's settings as saved, None before the first write."""
        path = self.base / "config" / SETTINGS_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["receivers"]["rtl_tcp"]["source"]

    async def test_names_the_computation_whatever_the_crystal_the_correction_and_the_one_set(self):
        # The crystal 37.42 ppm off, the correction none, wrong or nearly right, the computation set
        # unknown or the other one; behind a converter that inverts the spectrum too.
        inverted = {"converter": "down_inverted", "converter_lo_hz": 1_000_000_000.0}
        cases = (("truncating", "unknown", 0.0, None), ("rounding", "truncating", -12.5, None),
                 ("truncating", "rounding", 50.0, None), ("rounding", "unknown", 37.42, None),
                 ("truncating", "unknown", 5.0, inverted))
        for computation, driver_pll, ppm, converter in cases:
            with self.subTest(computation=computation, driver_pll=driver_pll, ppm=ppm, converter=converter):
                engine, subscription, fake = await self.start(computation, ppm=ppm, driver_pll=driver_pll,
                                                               converter=converter)
                answer = await self.identify(engine, subscription)
                self.assertEqual(answer, {"type": "driver_pll", "id": 1, "driver_pll": computation,
                                          "previous": driver_pll, "reason": None})
                self.assertEqual(engine.radios[0].state()["driver_pll"], computation)
                await self.assert_back(engine, fake)
                engine.close()          # the next receiver alone streams
                fake.sending = False

    async def test_takes_the_samples_as_received_with_the_noise_blanker_on(self):
        # The impulses of CarrierTest's: blanked, every point's margin would rise by about 10 dB.
        engine, subscription, _ = await self.start("rounding", level=0.1, impulses=(500, 2))
        engine.radios[0].configure_demod({"blanker": True})
        margins = []
        add = CarrierMeasure.add

        def spy(measure, *args, **kwargs):
            result = add(measure, *args, **kwargs)
            check = engine.radios[0]._driver_check
            if result is not None and check is not None and measure is check.gatherer:
                margins.append(result.margin_db)
            return result
        with mock.patch.object(CarrierMeasure, "add", spy):
            answer = await self.identify(engine, subscription)
        self.assertEqual((answer["driver_pll"], answer["reason"]), ("rounding", None))
        self.assertGreater(engine.radios[0].demod_state()["blanker_impulses"], 400)
        self.assertGreaterEqual(len(margins), 3)
        engine.radios[0].configure_demod({"blanker": False})
        engine.radios[0].measure_carrier({"action": "measure", "id": 2}, subscription)
        plain = await next_carrier(subscription)
        self.assertIsNone(plain["reason"])
        for margin in margins:
            self.assertAlmostEqual(margin, plain["margin_db"], delta=1.5)

    async def test_saves_the_computation_found_with_the_centre_asked(self):
        engine, subscription, _ = await self.start("rounding")
        await self.identify(engine, subscription)
        await eventually(lambda: (self.saved_source() or {}).get("driver_pll") == "rounding")
        saved = self.saved_source()
        self.assertEqual(saved["centre_hz"], self.CENTRE_HZ)
        self.assertNotIn("tuner_grid", saved)

    async def test_after_the_check_the_correction_is_the_crystal_s_alone(self):
        for computation, crystal_ppb in (("truncating", 37_420), ("rounding", -23_450), ("truncating", -51_230)):
            with self.subTest(computation=computation, crystal_ppb=crystal_ppb):
                engine, subscription, fake = await self.start(computation, crystal_ppb=crystal_ppb)
                self.assertEqual((await self.identify(engine, subscription))["driver_pll"], computation)
                engine.radios[0].measure_carrier({"action": "measure", "id": 2}, subscription)
                measured = await next_carrier(subscription)
                self.assertEqual(measured["id"], 2)
                ppm = calibrated_ppm(0.0, measured["frequency_hz"], self.air_hz)
                self.assertAlmostEqual(ppm, crystal_ppb / 1000, delta=0.01)
                engine.close()
                fake.sending = False

    async def test_a_measure_asked_meanwhile_waits_and_takes_the_label_found(self):
        # The crystal corrected, the older computation found: the carrier measures where it is.
        engine, subscription, _ = await self.start("truncating", ppm=37.42)
        engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
        engine.radios[0].measure_carrier({"action": "measure", "id": 2}, subscription)
        answers = []
        while not answers or answers[-1]["type"] != "carrier":
            message = await asyncio.wait_for(subscription.get(), 30)
            if isinstance(message, dict) and message["type"] in ("carrier", "driver_pll"):
                answers.append(message)
        self.assertEqual([answer["type"] for answer in answers], ["driver_pll", "carrier"])
        self.assertEqual(answers[0]["driver_pll"], "truncating")
        self.assertAlmostEqual(answers[1]["frequency_hz"], self.air_hz, delta=1.0)

    async def test_a_second_calibration_replaces_a_computation_that_changed(self):
        engine, subscription, fake = await self.start("truncating")
        self.assertEqual((await self.identify(engine, subscription))["driver_pll"], "truncating")
        fake.tuner_pll = functools.partial(r82xx.tuned_hz, "rounding")      # the driver upgraded
        answer = await self.identify(engine, subscription, 2)
        self.assertEqual((answer["driver_pll"], answer["previous"], answer["reason"]),
                         ("rounding", "truncating", None))
        self.assertEqual(engine.radios[0].state()["driver_pll"], "rounding")
        await eventually(lambda: (self.saved_source() or {}).get("driver_pll") == "rounding")

    async def test_a_tuner_that_follows_neither_computation_leaves_the_one_set(self):
        engine, subscription, fake = await self.start(None, driver_pll="truncating")     # an exact tuner
        answer = await self.identify(engine, subscription)
        self.assertEqual((answer["driver_pll"], answer["previous"], answer["reason"]),
                         (None, "truncating", "neither"))
        self.assertEqual(engine.radios[0].state()["driver_pll"], "truncating")
        await self.assert_back(engine, fake)

    async def test_a_carrier_or_a_receiver_lost_meanwhile_leaves_the_computation_set(self):
        for lost in ("carrier", "receiver"):
            with self.subTest(lost=lost):
                engine, subscription, fake = await self.start("truncating", driver_pll="rounding")
                with mock.patch("radiolack.radio.CHECK_POINT_TIMEOUT_S", 1.0):
                    engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
                    await self.off_the_grid(engine)
                    if lost == "carrier":
                        fake.carriers = {}      # no line: the point's measure ends at once
                    else:
                        fake.sending = False    # nothing more comes: the point's time runs out
                    answer = await next_driver(subscription)
                self.assertEqual((answer["driver_pll"], answer["previous"], answer["reason"]),
                                 (None, "rounding", "lost"))
                self.assertEqual(engine.radios[0].state()["driver_pll"], "rounding")
                await self.assert_back(engine)
                engine.close()
                fake.sending = False

    async def test_an_interface_that_leaves_brings_the_receiver_back(self):
        engine, subscription, fake = await self.start("truncating")
        engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
        await self.off_the_grid(engine)
        engine.unsubscribe(subscription)
        self.assertEqual(engine.radios[0].state()["driver_pll"], "unknown")
        await self.assert_back(engine, fake)
        self.assertNotIn("driver_pll", [message["type"] for message in ready_messages(subscription)
                                        if isinstance(message, dict)])

    async def test_another_interface_waits_for_the_same_check(self):
        engine, first, _ = await self.start("rounding")
        second = engine.subscribe()
        engine.radios[0].measure_carrier({"action": "identify", "id": 4}, first)
        engine.radios[0].measure_carrier({"action": "identify", "id": 9}, second)
        check = engine.radios[0]._driver_check
        engine.unsubscribe(first)       # the other still waits: the check goes on
        self.assertIs(engine.radios[0]._driver_check, check)
        answer = await next_driver(second)
        self.assertEqual((answer["id"], answer["driver_pll"]), (9, "rounding"))

    async def test_an_interface_s_change_stops_it_and_applies_to_the_receiver_as_it_was(self):
        for change in ("centre", "vfo", "gain"):
            with self.subTest(change=change):
                engine, subscription, fake = await self.start("truncating")
                engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
                await self.off_the_grid(engine)
                if change == "centre":
                    engine.radios[0].configure({"centre_hz": 145_200_000.0})
                elif change == "vfo":
                    engine.radios[0].configure_demod({"frequency_hz": engine.radios[0].demod_state()["frequency_hz"] + 500})
                else:
                    engine.radios[0].configure({"gain_db": 20.7})
                answer = await next_driver(subscription)
                self.assertEqual((answer["driver_pll"], answer["reason"]), (None, "stopped"))
                state = engine.radios[0].state()
                self.assertEqual((state["driver_pll"], state["tuner_grid"]), ("unknown", True))
                self.assertEqual(state["centre_hz"], 145_200_000.0 if change == "centre" else self.CENTRE_HZ)
                engine.close()
                fake.sending = False

    async def test_a_scan_or_an_i_q_recording_that_starts_stops_it(self):
        for start in ("scan", "iq"):
            with self.subTest(start=start):
                engine, subscription, fake = await self.start("truncating")
                engine.edit_bookmarks({"action": "add", "frequency_hz": 145_060_000.0, "name": "Carrier",
                                       "mode": "AM", "scan": True})
                engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
                await self.off_the_grid(engine)
                if start == "scan":
                    engine.radios[0].configure_scanner({"scan": "memory"})
                else:
                    engine.radios[0].configure_recording({"iq": True})
                self.assertEqual((await next_driver(subscription))["reason"], "stopped")
                self.assertEqual(engine.radios[0].state()["centre_hz"], self.CENTRE_HZ)    # where it began
                self.assertTrue(engine.radios[0].state()["tuner_grid"])
                engine.radios[0].configure_scanner({"scan": None})
                engine.radios[0].configure_recording({"iq": False})
                engine.close()
                fake.sending = False

    async def test_the_engine_s_stop_brings_the_receiver_back_and_saves_the_centre_asked(self):
        engine, subscription, _ = await self.start("truncating")
        engine.radios[0].measure_carrier({"action": "identify", "id": 1}, subscription)
        await self.off_the_grid(engine)
        self.assertEqual(engine.radios[0].receiver_settings()["source"]["centre_hz"], self.CENTRE_HZ)
        engine.close()
        await asyncio.wait_for(engine.wait_closed(), 15)
        await self.assert_back(engine)
        saved = self.saved_source()
        self.assertEqual((saved["centre_hz"], saved["driver_pll"]), (self.CENTRE_HZ, "unknown"))

    async def test_refused_where_it_cannot_run_moving_nothing(self):
        cases = (("scan", {}, None), ("iq", {}, None),
                 ("direct", {"hf_input": "q"}, 7_100_000.0),
                 ("receiver", {"tuner_type": 1}, None),         # an Elonics E4000
                 ("range", {"tuner_type": 6, "hf_input": "tuner"}, 20_000_000.0),   # an R828D below 28.8 MHz
                 ("position", {}, "vfo"),
                 ("lost", {}, "wfm"))       # no WFM channel at 240 kS/s: no carrier to measure
        for reason, settings, change in cases:
            with self.subTest(reason=reason):
                engine, subscription, fake = await self.start("truncating", **settings)
                if reason == "scan":
                    engine.edit_bookmarks({"action": "add", "frequency_hz": 145_060_000.0, "name": "Carrier",
                                           "mode": "AM", "scan": True})
                    engine.radios[0].configure_scanner({"scan": "memory"})
                elif reason == "iq":
                    engine.radios[0].configure_recording({"iq": True})
                elif change == "vfo":       # 1.5 kHz from the centre in use: within the check's moves
                    engine.radios[0].configure_demod({"frequency_hz": engine.radios[0].state()["centre_in_use_hz"] + 1500})
                elif change == "wfm":
                    engine.radios[0].configure_demod({"mode": "WFM"})
                elif change is not None:
                    engine.radios[0].configure({"centre_hz": change})
                before = engine.radios[0].state()
                answer = await self.identify(engine, subscription)
                self.assertEqual(answer, {"type": "driver_pll", "id": 1, "driver_pll": None, "previous": "unknown",
                                          "reason": reason})
                self.assertIsNone(engine.radios[0]._driver_check)
                self.assertEqual(engine.radios[0].state(), before)
                engine.radios[0].configure_scanner({"scan": None})
                engine.radios[0].configure_recording({"iq": False})
                engine.close()
                fake.sending = False

    async def test_refused_on_the_simulated_band(self):
        engine = Engine(FakeSource())
        task = asyncio.ensure_future(engine.run())
        self.addAsyncCleanup(stop, engine, task)
        subscription = engine.subscribe()
        engine.radios[0].measure_carrier({"action": "identify"}, subscription)
        answer = await next_driver(subscription)
        self.assertEqual(answer, {"type": "driver_pll", "id": None, "driver_pll": None, "previous": None,
                                  "reason": "receiver"})
