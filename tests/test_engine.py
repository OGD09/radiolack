"""Tests for the engine: frames and source state for every client, idle
without clients, changes passed to the source, DC removal, clean stop."""

import asyncio
import unittest

import numpy as np

from radiolack.engine import Engine, Subscription
from radiolack.spectrum import FRAME_HEADER
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, FakeSource

DC_BIN = 4096       # the centre frequency, with 8192-point FFTs


async def next_message(subscription: Subscription):
    return await asyncio.wait_for(subscription.get(), timeout=5)


async def next_frame(subscription: Subscription) -> bytes | None:
    """The next frame, skipping state messages; None once the engine stopped."""
    while isinstance(message := await next_message(subscription), dict):
        pass
    return message


def level_db(frame: bytes, index: int) -> float:
    """The level of bin ``index`` of a frame, in dBFS."""
    *_, db_min, db_step = FRAME_HEADER.unpack_from(frame)
    return db_min + frame[FRAME_HEADER.size + index] * db_step


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

    async def test_a_new_client_first_gets_the_source_state(self):
        subscription = self.engine.subscribe()
        self.assertEqual(subscription.get_nowait(), {
            "type": "source", "kind": "test", "status": "streaming", "detail": "", "address": None,
            "tuner": None, "centre_hz": FAKE_CENTRE_HZ, "sample_rate_hz": FAKE_RATE_HZ,
            "gain_db": None, "ppm": None, "gains_db": [], "range_hz": [FAKE_CENTRE_HZ, FAKE_CENTRE_HZ],
            "dc_removal": True, "settable": ["dc_removal"],
        })
        self.assertIsInstance(await next_message(subscription), bytes)

    async def test_every_client_gets_the_frames(self):
        subscriptions = [self.engine.subscribe() for _ in range(2)]
        for subscription in subscriptions:
            sequences = []
            for _ in range(3):
                frame = await next_frame(subscription)
                _, _, _, sequence, centre, rate, db_min, db_step = FRAME_HEADER.unpack_from(frame)
                sequences.append(sequence)
                self.assertEqual((centre, rate), (FAKE_CENTRE_HZ, FAKE_RATE_HZ))
                codes = np.frombuffer(frame, np.uint8, offset=FRAME_HEADER.size)
                self.assertEqual(len(codes), 8192)
                self.assertEqual(int(np.argmax(codes)), FAKE_TONE_BIN)
                self.assertAlmostEqual(db_min + int(codes.max()) * db_step, -20.0, delta=0.5)
            self.assertEqual(sequences, sorted(sequences))

    async def test_a_slow_client_keeps_only_the_latest_frames(self):
        subscription = self.engine.subscribe()
        self.assertEqual(subscription.get_nowait()["type"], "source")
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
        subscription.get_nowait()
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
        self.assertIn("the display engine stopped", logs.output[0])
        frames = []
        while (frame := await next_frame(subscription)) is not None:
            frames.append(frame)
        self.assertLessEqual(len(frames), 2)
        self.assertTrue(source.closed)


if __name__ == "__main__":
    unittest.main()
