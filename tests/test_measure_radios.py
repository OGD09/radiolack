"""Tests for the tool that measures several radios in one Radiolack
(tools/measure_radios.py), whose figures they leave alone: they depend on
the computer. A short measure runs it whole, on 127.0.0.1 and ports the
system chooses, with stand-ins for rtl_tcp: no receiver, no network."""

import asyncio
import importlib.util
import io
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up by this name
    spec.loader.exec_module(module)
    return module


measuring = load("measure_radios")
LINUX = Path("/proc/self/task").is_dir()


def audio_frame(position, count, rate_hz=12_000.0):
    return struct.pack("<BBHId", 2, 1, 0, position, rate_hz) + bytes(2 * count)


class SceneTest(unittest.TestCase):
    def test_the_scene_is_the_same_for_a_seed_and_repeats_without_a_seam(self):
        scene = measuring.scene_bytes(1, seconds=0.01)
        self.assertEqual(scene, measuring.scene_bytes(1, seconds=0.01))
        self.assertEqual(len(scene), 2 * round(0.01 * measuring.RATE_HZ))
        # Whole hertz over the scene's length: its last sample leads into its first.
        for frequency in (measuring.VFO_OFFSET_HZ, measuring.TONE_HZ,
                          *(offset for offset, _ in measuring.CARRIERS)):
            self.assertEqual(frequency * measuring.SCENE_S % 1, 0)
        levels = np.frombuffer(scene, np.uint8)
        self.assertTrue(110 < levels.mean() < 145)
        self.assertLess(np.count_nonzero((levels == 0) | (levels == 255)), 10)


class PlaybackTest(unittest.TestCase):
    def test_frames_on_time_never_run_dry(self):
        playback = measuring.Playback()
        for index in range(300):
            playback.frame(index / 30, index * 400, 12_000.0, 400)
        self.assertEqual(playback.dry, 0)
        self.assertEqual(playback.missing_s, 0)
        self.assertEqual(playback.frames, 300)
        self.assertAlmostEqual(playback.longest_gap_s, 1 / 30)
        # Playback started once 150 ms were buffered: five frames, then a frame's worth less at worst.
        self.assertAlmostEqual(playback.lowest_s, 5 * 400 / 12_000 - 1 / 30)

    def test_a_late_frame_runs_dry_then_waits_for_the_target_again(self):
        playback = measuring.Playback()
        for index in range(10):
            playback.frame(index / 30, index * 400, 12_000.0, 400)
        playback.frame(10 / 30 + 0.3, 4000, 12_000.0, 400)
        self.assertEqual(playback.dry, 1)
        self.assertEqual(playback.lowest_s, 0.0)
        [(at, short)] = playback.dry_at                 # when, from the first frame here, and how short
        self.assertAlmostEqual(at, 10 / 30 + 0.3)
        self.assertAlmostEqual(short, 1 / 30 + 0.3 - 5 * 400 / 12_000)  # five frames buffered
        self.assertAlmostEqual(playback.longest_gap_s, 1 / 30 + 0.3)
        playback.frame(10 / 30 + 0.31, 4400, 12_000.0, 400)     # 67 ms buffered: not playing yet
        playback.frame(10 / 30 + 0.9, 4800, 12_000.0, 400)
        self.assertEqual(playback.dry, 1)

    def test_a_gap_in_the_positions_is_audio_dropped(self):
        playback = measuring.Playback()
        playback.frame(0.0, 2 ** 32 - 400, 12_000.0, 400)
        playback.frame(0.01, 0, 12_000.0, 400)                  # wrapped: nothing missing
        playback.frame(0.02, 1200, 12_000.0, 400)
        self.assertAlmostEqual(playback.missing_s, 800 / 12_000)

    def test_the_warm_up_is_forgotten(self):
        playback = measuring.Playback()
        playback.frame(0.0, 0, 12_000.0, 4000)
        playback.frame(1.0, 4000, 12_000.0, 400)
        self.assertEqual(playback.dry, 1)
        playback.counted_from(1.01)
        self.assertEqual((playback.frames, playback.dry, playback.samples), (0, 0, 0))
        self.assertEqual(playback.dry_at, [])
        self.assertIsNone(playback.lowest_s)

    def test_audio_frames_are_told_from_the_others(self):
        self.assertEqual(measuring.parse_audio(audio_frame(7, 400)), (7, 12_000.0, 400))
        self.assertIsNone(measuring.parse_audio(bytes([1]) + bytes(40)))
        self.assertIsNone(measuring.parse_audio(bytes([2, 1])))


class ProcessorTimeTest(unittest.TestCase):
    def test_windows_and_mean(self):
        hz = 100
        samples = [measuring.Sample(float(at), at * 50, {}) for at in range(31)]
        self.assertEqual(measuring.windows_pct(samples, 10, hz), [50.0, 50.0, 50.0])
        self.assertEqual(measuring.mean_pct(samples, hz), 50.0)

    def test_threads_put_the_event_loop_first(self):
        first = measuring.Sample(0.0, 0, {10: ("python3", 0), 11: ("python3", 0), 12: ("python3", 0)})
        last = measuring.Sample(10.0, 300, {10: ("python3", 100), 11: ("python3", 50), 12: ("python3", 150)})
        self.assertEqual(measuring.threads_pct([first, last], 10, 100),
                         [("event loop (main thread)", 10.0), ("worker thread 1", 15.0), ("worker thread 2", 5.0)])

    @unittest.skipUnless(LINUX, "reads /proc")
    def test_this_process_reads_its_own_time(self):
        total, threads = measuring.read_cpu(os.getpid())
        self.assertGreaterEqual(total, 0)
        self.assertIn(os.getpid(), threads)


class OptionsTest(unittest.TestCase):
    def test_options_are_checked(self):
        for argv in (["--radios", "0"], ["--radios", "5"], ["--seconds", "-1"], ["--warmup-s", "x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    measuring.build_parser().parse_args(argv)
        self.assertEqual(measuring.build_parser().parse_args([]).seconds, 600.0)


@unittest.skipUnless(LINUX, "reads /proc")
class MeasureTest(unittest.TestCase):
    def test_a_short_measure_plays_both_radios(self):
        out, err = io.StringIO(), io.StringIO()
        status = measuring.main(["--radios", "2", "--seconds", "2", "--warmup-s", "1.5"], out, err)
        self.assertEqual(status, measuring.EXIT_DONE, err.getvalue())
        text = out.getvalue()
        rows = [line for line in text.splitlines() if line.startswith(("| 1 |", "| 2 |"))]
        self.assertEqual(len(rows), 2, text)
        for row in rows:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            self.assertGreater(int(cells[1]), 30, row)      # audio frames, about 30 a second
            self.assertGreater(int(cells[6]), 30, row)      # spectrum frames
        self.assertIn("| event loop (main thread) |", text)
        self.assertIn("Commands refused: 0.", text)
        self.assertRegex(text, r"The measuring process's own longest pause: \d+ ms\.")
        self.assertRegex(text, r"Rule: .*: (passed|failed)\.")

    def test_a_receiver_sends_on_time_and_drops_what_a_client_behind_cannot_take(self):
        async def run():
            scene = bytes(range(256)) * 64
            receiver = measuring.Receiver(scene, rate_hz=20_000_000.0)    # 40 MB/s: the socket soon fills
            await receiver.start()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", receiver.port)
                header = await reader.readexactly(12)
                writer.write(struct.pack(">BI", 0x01, 145_000_000))     # a command, left alone
                self.assertEqual(await reader.readexactly(len(scene) + 4), scene + scene[:4])
                await asyncio.sleep(measuring.BEHIND_S + 0.5)           # a reader behind
                for _ in range(200):
                    await reader.read(1 << 20)
                writer.close()
            finally:
                await receiver.close()
            return header, receiver.dropped_s
        header, dropped_s = asyncio.run(run())
        self.assertEqual(header, struct.pack(">4sII", b"RTL0", measuring.TUNER_E4000, measuring.E4000_GAINS))
        self.assertGreater(dropped_s, 0.0)

if __name__ == "__main__":
    unittest.main()
