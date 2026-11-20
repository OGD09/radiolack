"""Checks of what the Bookmarks menu's dock says of the memory scan's tour
(memorytour.js), a typed module, without a browser: Node.js runs a script
that imports it from the interface's folder and prints what it gives, as
JSON. Skipped where Node.js is not installed; GitHub's runners and the type
check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack import scanners
from radiolack.server import WEB_DIR

NODE = shutil.which("node")

SCRIPT = """
const t = await import(process.argv[1]);
console.log(JSON.stringify({
  constants: [t.WINDOW_TOUR_S, t.SEARCHED_FRACTION],
  notes: [[0, 2.4e6], [1, 2.4e6], [2, 2.4e6], [8, 2.4e6], [3, 8e6], [8, 0], [NaN, 2.4e6]].map(([w, r]) => t.tourNote(w, r)),
}));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class TourNoteTest(unittest.TestCase):
    """memorytour.js, run in Node.js."""

    def setUp(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, (WEB_DIR / "memorytour.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        self.found = json.loads(done.stdout)

    def test_one_window_says_nothing(self):
        notes = self.found["notes"]
        self.assertEqual(notes[0], "")
        self.assertEqual(notes[1], "")
        self.assertEqual(notes[5], "")      # no rate known
        self.assertEqual(notes[6], "")

    def test_several_windows_tell_the_tour_and_what_it_misses(self):
        notes = self.found["notes"]
        self.assertEqual(notes[3], (
            "8 windows, watched in turn about 1 s each: a tour takes about 14 s, more while listening, "
            "and may miss a transmission shorter than about 13 s. Bookmarks within 1.92 MHz take one window, "
            "watched without a break."))
        self.assertIn("about 4 s", notes[2])          # README: 2.5 s may be missed with two windows
        self.assertIn("shorter than about 3 s", notes[2])
        self.assertIn("within 6.40 MHz", notes[4])

    def test_the_constants_follow_the_engine(self):
        tour_s, fraction = self.found["constants"]
        self.assertEqual(fraction, 2 * scanners.USABLE_FRACTION)
        # The receiver's settling, measured, plus the noise's blocks and the watch.
        noise_s = scanners.DETECTION_BLOCKS / 30
        self.assertAlmostEqual(tour_s, 0.4 + noise_s + scanners.VISIT_S, delta=0.05)


if __name__ == "__main__":
    unittest.main()
