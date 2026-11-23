"""Checks of the waterfall's time scale (wfscale.js) and of the history's
lines drawn again at another speed (history.js), typed modules, without a
browser: Node.js runs a script that imports them from the interface's
folder and prints what they give, as JSON. Skipped where Node.js is not
installed; GitHub's runners and the type check's job have it."""

import json
import math
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")


def js_round(value: float) -> float:
    """``value`` to 3 decimals, as the scripts round: half up."""
    return math.floor(value * 1000 + 0.5) / 1000

SCALE_SCRIPT = """
const t = await import(process.argv[1]);
const round = (v) => Math.round(v * 1000) / 1000;
console.log(JSON.stringify({
  limits: [t.MIN_SPAN_S, t.MAX_SPAN_S, t.MIN_SPEED, t.MAX_SPEED],
  spans: [t.spanOf(600, 25), t.spanOf(600, 2), t.speedFor(600, 60), t.speedFor(600, 5), t.speedFor(600, 5000)].map(round),
  clamped: [t.clampSpeed(0), t.clampSpeed(1000), t.clampSpeed(25)],
  wheel: [t.wheeledSpan(60, 100), t.wheeledSpan(60, -100), t.wheeledSpan(60, 400), t.wheeledSpan(60, 50),
          t.wheeledSpan(1190, 100), t.wheeledSpan(10.5, -100)].map(round),
  drag: [t.draggedSpan(60, 0), t.draggedSpan(60, 100), t.draggedSpan(60, -100), t.draggedSpan(60, 1000)].map(round),
  slider: [t.sliderSpan(0), t.sliderSpan(1000), t.sliderSpan(500), t.sliderValue(10), t.sliderValue(1200),
           t.sliderValue(t.sliderSpan(321)), t.sliderValue(5), t.sliderValue(5000)].map(round),
  text: [3.2, 24, 59.4, 59.6, 90, 150, 299, 570, 600, 1200].map(t.spanText),
  steps: [[24, 600], [60, 600], [300, 600], [1200, 600], [1200, 100], [10, 200]].map(([s, h]) => t.stampStep(s, h)),
  stamps: [t.stampText(Date.UTC(2026, 10, 23, 8, 5, 30), 10).length, t.stampText(Date.UTC(2026, 10, 23, 8, 5, 30), 60).length],
}));
"""

# A history of 6 lines over 2 bins: what a rescale keeps. Line k, made at
# time 1000 + k, has codes [k, 10 - k]; line 4 came from another centre.
RESCALE_SCRIPT = """
const { SpectrumHistory } = await import(process.argv[1]);
const make = () => {
  const h = new SpectrumHistory(6);
  h.reset(1000, 200, 2);
  for (let k = 0; k < 6; k += 1) {
    h.add(Uint8Array.of(0, 0), -160, 0.625, 0, k);
    h.commitLine(1000 + k, k, 250);
    h.line(h.head).set([k, 10 - k]);
    h.histCentre[h.head] = k === 4 ? 1100 : 1000;
  }
  return h;
};
const lines = (h) => Array.from({ length: h.filled }, (_, age) => {
  const row = h.rowAt(age);
  return [Array.from(h.line(row)), h.histTime[row], h.histCentre[row]];
});
const out = {};
let h = make();
out.before = lines(h);
const version = h.version;
h.rescale(2);
out.slower = { lines: lines(h), head: h.head, changed: h.version > version };
h = make();
h.rescale(0.5);
out.faster = lines(h);      // 12 lines asked, 6 kept
h = make();
h.rescale(1.5);
out.between = lines(h);
h = make();
const same = h.version;
h.rescale(1);
h.rescale(0);
h.rescale(NaN);
out.unchanged = [h.version === same, h.filled];
const empty = new SpectrumHistory(4);
empty.rescale(2);
out.empty = [empty.filled, empty.head];
h = make();
h.rescale(2);
h.rescale(2);
out.twice = lines(h);
console.log(JSON.stringify(out));
"""


# Two frames 10 ms apart in one bin, at -20 and -100 dB (codes for -100 + c dB):
# the line made from them at a period of 40 ms or more, 30 ms and none.
PEAK_SCRIPT = """
const { SpectrumHistory, PEAK_LINE_FRAMES, HIST_DB_MIN, HIST_DB_STEP } = await import(process.argv[1]);
const lineDb = (periodMs, frames = 2) => {
  const h = new SpectrumHistory(2);
  h.reset(1000, 100, 1);
  h.add(Uint8Array.of(80), -100, 1, 0, 1000);
  if (frames > 1) h.add(Uint8Array.of(0), -100, 1, 0, 1010);
  h.commitLine(5000, 1020, 250, periodMs);
  return [Math.round(h.frameMs), HIST_DB_MIN + h.line(h.head)[0] * HIST_DB_STEP];
};
console.log(JSON.stringify({ frames: PEAK_LINE_FRAMES, at40: lineDb(40), at30: lineDb(30), none: lineDb(undefined),
  oneFrame: lineDb(1000, 1) }));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class WaterfallScaleTest(unittest.TestCase):
    """wfscale.js and history.js, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_time_shown_follows_the_height_the_speed_and_the_gestures(self):
        found = self.run_script(SCALE_SCRIPT, "wfscale.js")
        self.assertEqual(found["limits"], [10, 1200, 0.05, 120])
        # 600 px at 25 lines/s show 24 s; 60 s on 600 px take 10 lines/s; 5 s and 5000 s are out of reach.
        self.assertEqual(found["spans"], [24, 300, 10, 60, 0.5])
        self.assertEqual(found["clamped"], [0.05, 120, 25])
        # A notch multiplies by 1.15, down lengthening; one notch at most per event, within 10 s to 20 min.
        self.assertEqual(found["wheel"], [js_round(v) for v in (69, 60 / 1.15, 69, 60 * 1.15 ** 0.5, 1200, 10)])
        self.assertEqual(found["drag"], [js_round(v) for v in (60, 60 * 1.012 ** 100, 60 / 1.012 ** 100, 1200)])
        self.assertEqual(found["slider"], [10, 1200, js_round(math.sqrt(10 * 1200)), 0, 1000, 321, 0, 1000])

    def test_the_time_and_the_timestamps_read_as_the_interface_writes_them(self):
        found = self.run_script(SCALE_SCRIPT, "wfscale.js")
        self.assertEqual(found["text"], ["3 s", "24 s", "59 s", "1 min", "1.5 min", "2.5 min", "5 min", "9.5 min",
                                         "10 min", "20 min"])
        # At least 40 px between two timestamps, at 10 s at the shortest.
        self.assertEqual(found["steps"], [10, 10, 30, 120, 600, 10])
        self.assertEqual(found["stamps"], [8, 5])     # 08:05:30, or 08:05 once the step is a minute or more

    def test_a_line_keeps_the_highest_power_once_it_spans_four_frames(self):
        found = self.run_script(PEAK_SCRIPT, "history.js")
        self.assertEqual(found["frames"], 4)
        self.assertEqual(found["at40"], [10, -20])         # four frames' time: the highest
        self.assertEqual(found["at30"], [10, -23.125])     # fewer: the mean, -23 dB, on the codes' 0.625 dB steps
        self.assertEqual(found["none"], [10, -23.125])
        self.assertEqual(found["oneFrame"], [0, -20])      # one frame alone: its own level, the interval unknown

    def test_a_rescale_keeps_the_highest_code_of_the_lines_each_new_line_takes(self):
        found = self.run_script(RESCALE_SCRIPT, "history.js")
        # Newest first: line 5, then 4 (another centre), ..., then 0.
        self.assertEqual(found["before"][0], [[5, 5], 1005, 1000])
        self.assertEqual(found["before"][1], [[4, 6], 1004, 1100])
        slower = found["slower"]
        # Two lines make one, with the newest's time and centre: lines 5 and 4, whose centre differs, give
        # line 5 alone; 3 and 2 give their highest codes, [3, 8]; 1 and 0 give [1, 10].
        self.assertEqual(slower["lines"], [[[5, 5], 1005, 1000], [[3, 8], 1003, 1000], [[1, 10], 1001, 1000]])
        self.assertEqual(slower["head"], 2)
        self.assertTrue(slower["changed"])
        # Faster: each line makes two, as many as the history keeps.
        self.assertEqual([line[1] for line in found["faster"]], [1005, 1005, 1004, 1004, 1003, 1003])
        self.assertEqual(found["faster"][2], [[4, 6], 1004, 1100])
        # 1.5 lines per new line: lines [0, 1), [1, 3), [3, 4), [4, 6) by age.
        self.assertEqual(found["between"], [[[5, 5], 1005, 1000], [[4, 6], 1004, 1100], [[2, 8], 1002, 1000],
                                            [[1, 10], 1001, 1000]])
        self.assertEqual(found["unchanged"], [True, 6])
        self.assertEqual(found["empty"], [0, -1])
        # Twice in a row, through the buffers swapped back and forth: [5, 5] and [3, 8] make one line.
        self.assertEqual(found["twice"], [[[5, 8], 1005, 1000]])


if __name__ == "__main__":
    unittest.main()
