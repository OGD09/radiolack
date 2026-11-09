"""Tests for the engine: frames for every client, idle without clients, clean stop."""

import asyncio
import unittest

import numpy as np

from radiolack.engine import Engine
from radiolack.spectrum import FRAME_HEADER
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RATE_HZ, FAKE_TONE_BIN, FakeSource


async def next_frame(queue: asyncio.Queue) -> bytes | None:
    return await asyncio.wait_for(queue.get(), timeout=5)


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
        queue = self.engine.subscribe()
        await next_frame(queue)
        self.engine.unsubscribe(queue)
        await asyncio.sleep(0.05)       # lets a read in progress finish
        reads = self.source.reads
        await asyncio.sleep(0.1)
        self.assertEqual(self.source.reads, reads)

    async def test_every_client_gets_the_frames(self):
        queues = [self.engine.subscribe() for _ in range(2)]
        for queue in queues:
            sequences = []
            for _ in range(3):
                frame = await next_frame(queue)
                _, _, _, sequence, centre, rate, db_min, db_step = FRAME_HEADER.unpack_from(frame)
                sequences.append(sequence)
                self.assertEqual((centre, rate), (FAKE_CENTRE_HZ, FAKE_RATE_HZ))
                codes = np.frombuffer(frame, np.uint8, offset=FRAME_HEADER.size)
                self.assertEqual(len(codes), 8192)
                self.assertEqual(int(np.argmax(codes)), FAKE_TONE_BIN)
                self.assertAlmostEqual(db_min + int(codes.max()) * db_step, -20.0, delta=0.5)
            self.assertEqual(sequences, sorted(sequences))

    async def test_a_slow_client_keeps_only_the_latest_frames(self):
        queue = self.engine.subscribe()
        while self.source.reads < 12:
            await asyncio.sleep(0.01)
        self.assertEqual(queue.qsize(), 4)
        sequences = [FRAME_HEADER.unpack_from(queue.get_nowait())[3] for _ in range(4)]
        self.assertEqual(sequences, list(range(sequences[0], sequences[0] + 4)))
        self.assertGreaterEqual(sequences[0], 6)

    async def test_close_ends_every_stream(self):
        queue = self.engine.subscribe()
        await next_frame(queue)
        self.engine.close()
        self.assertIsNone(await next_frame(queue))
        await asyncio.wait_for(self.task, timeout=5)
        self.assertIsNone(await next_frame(self.engine.subscribe()))


class EngineFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_source_is_logged_and_ends_the_streams(self):
        engine = Engine(FakeSource(fail_after=2))
        queue = engine.subscribe()
        with self.assertLogs("radiolack.engine", "ERROR") as logs:
            await asyncio.wait_for(engine.run(), timeout=5)
        self.assertIn("the display engine stopped", logs.output[0])
        frames = []
        while (frame := await next_frame(queue)) is not None:
            frames.append(frame)
        self.assertLessEqual(len(frames), 2)


if __name__ == "__main__":
    unittest.main()
