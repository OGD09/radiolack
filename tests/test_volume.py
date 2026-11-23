"""Checks of the volume at hand (volume.js), a typed module, without a
browser: Node.js runs a script that imports it from the interface's folder
and prints what it gives, as JSON. Skipped where Node.js is not installed;
GitHub's runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

SCRIPT = """
const t = await import(process.argv[1]);
console.log(JSON.stringify({
  constants: [t.VOLUME_STEP, t.DRAG_PX_PER_PERCENT, t.VOLUME_DRAG_START_PX],
  dragged: [t.draggedVolume(50, 0), t.draggedVolume(50, 16), t.draggedVolume(50, -16),
            t.draggedVolume(50, 1000), t.draggedVolume(50, -1000), t.draggedVolume(0, 160)],
  stepped: [t.steppedVolume(72, 1), t.steppedVolume(72, -1), t.steppedVolume(70, 1), t.steppedVolume(70, -1),
            t.steppedVolume(70, 2), t.steppedVolume(3, -1), t.steppedVolume(98, 1), t.steppedVolume(100, 3),
            t.steppedVolume(0, -2), t.steppedVolume(72, 0)],
  text: [t.volumeText(0), t.volumeText(5), t.volumeText(100)],
}));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class VolumeAtHandTest(unittest.TestCase):
    """volume.js, run in Node.js."""

    def setUp(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, (WEB_DIR / "volume.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        self.found = json.loads(done.stdout)

    def test_a_drag_follows_the_finger_along_the_gauge_within_the_range(self):
        self.assertEqual(self.found["constants"], [5, 1.6, 6])
        self.assertEqual(self.found["dragged"], [50, 60, 40, 100, 0, 100])

    def test_a_step_lands_on_the_grid_and_stays_within_the_range(self):
        self.assertEqual(self.found["stepped"], [75, 70, 75, 65, 80, 0, 100, 100, 0, 72])

    def test_the_gauge_says_muted_at_zero(self):
        self.assertEqual(self.found["text"], ["Muted", "5%", "100%"])


if __name__ == "__main__":
    unittest.main()
