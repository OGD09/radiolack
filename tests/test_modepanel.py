"""Checks of the Mode and filter panel's widths (modepanel.js), a typed
module, without a browser: Node.js runs a script that imports it from the
interface's folder and prints what it gives, as JSON. Skipped where Node.js
is not installed; GitHub's runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# The interface's modes' widths (main.js, MODES), Hz.
WIDTHS = {
    "NFM": {"bw": 10000, "min": 4000, "max": 25000, "step": 500},
    "AM": {"bw": 8000, "min": 3000, "max": 15000, "step": 500},
    "USB": {"bw": 2700, "min": 1500, "max": 4000, "step": 100},
    "CW": {"bw": 500, "min": 100, "max": 2000, "step": 50},
    "WFM": {"bw": 180000, "min": 50000, "max": 250000, "step": 10000},
    "tight": {"bw": 1000, "min": 900, "max": 1100, "step": 100},
}

SCRIPT = """
const t = await import(process.argv[1]);
const widths = JSON.parse(process.argv[2]);
const presets = Object.fromEntries(Object.entries(widths).map(([k, w]) => [k, t.widthPresets(w)]));
const at = (k, bw) => t.presetAt(widths[k], bw);
console.log(JSON.stringify({
  shares: [t.NARROW_SHARE, t.WIDE_SHARE],
  presets,
  at: [at('NFM', 10000), at('NFM', 6000), at('NFM', 15000), at('NFM', 12500), at('USB', 4000), at('tight', 1000)],
}));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class WidthPresetsTest(unittest.TestCase):
    """modepanel.js, run in Node.js."""

    def setUp(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, (WEB_DIR / "modepanel.js").as_uri(),
                               json.dumps(WIDTHS)], capture_output=True, text=True, timeout=60, check=True)
        self.found = json.loads(done.stdout)

    def test_normal_is_the_width_a_mode_opens_with_and_the_others_lie_on_its_step(self):
        presets = self.found["presets"]
        self.assertEqual(self.found["shares"], [0.6, 1.5])
        self.assertEqual(presets["NFM"], {"narrow": 6000, "normal": 10000, "wide": 15000})
        self.assertEqual(presets["AM"], {"narrow": 5000, "normal": 8000, "wide": 12000})
        self.assertEqual(presets["CW"], {"narrow": 300, "normal": 500, "wide": 750})
        for name, widths in WIDTHS.items():
            for hz in presets[name].values():
                self.assertEqual(hz % widths["step"], 0, name)

    def test_the_presets_stay_within_the_modes_limits(self):
        presets = self.found["presets"]
        self.assertEqual(presets["USB"], {"narrow": 1600, "normal": 2700, "wide": 4000})        # 4050 capped
        self.assertEqual(presets["WFM"], {"narrow": 110000, "normal": 180000, "wide": 250000})  # 270 kHz capped
        self.assertEqual(presets["tight"], {"narrow": 900, "normal": 1000, "wide": 1100})

    def test_a_width_names_its_preset_or_none(self):
        self.assertEqual(self.found["at"], ["normal", "narrow", "wide", "", "wide", "normal"])


if __name__ == "__main__":
    unittest.main()
