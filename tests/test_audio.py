"""Checks of the audio worklet's losses (audio-worklet.js), without a
browser: Node.js stands in for the audio rendering thread's globals, runs
the processor on frames and prints the losses it told audio.js, as JSON.
Skipped where Node.js is not installed; GitHub's runners and the type
check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# The processor at 12 kHz on a 48 kHz device: it runs dry, misses frames,
# skips ahead, then meets a gap too long to fill and a reset.
LOSSES_SCRIPT = """
const told = [];
let Playback = null;
globalThis.sampleRate = 48000;
globalThis.AudioWorkletProcessor = class { port = { postMessage: (message) => told.push(message) }; };
globalThis.registerProcessor = (_name, processor) => { Playback = processor; };
await import(process.argv[1]);
const playback = new Playback();
const RATE = 12000;
const frame = (position, count) => ({ type: 'frame', position, rateHz: RATE, pcm: new Int16Array(count).fill(1000).buffer });
const play = (quanta) => { for (let i = 0; i < quanta; i += 1) playback.process([], [[new Float32Array(128)]]); };
const steps = {};
playback.receive(frame(0, 2400));           // 0.2 s: playback starts at 0.15 s buffered
play(40);                                   // 0.1 s played: no loss yet
steps.playing = told.length;
play(200);                                  // the rest, and more: it runs dry once
steps.dry = told.slice();
playback.receive(frame(3000, 2400));        // 600 samples (0.05 s) the engine dropped
steps.missing = told.slice();
playback.receive(frame(5400, 7200));        // 0.6 s at once, beyond 0.5 s buffered: skips ahead
steps.skipped = told.slice();
playback.receive(frame(5400 + 7200 + 12000, 2400));     // a 1 s gap: starts again, not counted
playback.receive({ type: 'reset' });
play(200);                                  // nothing buffered since the reset: never plays, never runs dry
steps.after = told.length;
console.log(JSON.stringify(steps));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's audio worklet")
class WorkletLossesTest(unittest.TestCase):
    """audio-worklet.js, run in Node.js."""

    def test_counts_dry_runs_missing_frames_and_skips(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", LOSSES_SCRIPT,
                               (WEB_DIR / "audio-worklet.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        steps = json.loads(done.stdout)
        self.assertEqual(steps["playing"], 0)
        self.assertEqual(steps["dry"], [{"type": "losses", "dry": 1, "missingS": 0, "skippedS": 0}])
        self.assertEqual(steps["missing"][1:], [{"type": "losses", "dry": 1, "missingS": 0.05, "skippedS": 0}])
        [skip] = steps["skipped"][2:]
        self.assertEqual((skip["dry"], skip["missingS"]), (1, 0.05))
        # From where it ran dry, about 2392.5 samples in, to 0.15 s before the 12600 samples written.
        self.assertAlmostEqual(skip["skippedS"], (12600 - 1800 - 2392.5) / 12000, delta=1 / 12000)
        self.assertEqual(steps["after"], 3)


if __name__ == "__main__":
    unittest.main()
