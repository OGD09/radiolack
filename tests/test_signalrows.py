"""Checks of the Signals list's rows (signalrows.js), a typed module, without
a browser: Node.js runs a script that imports it from the interface's folder
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
const peak = (frequencyHz, channelHz, marginDb, active, ageS) =>
  ({ frequencyHz, channelHz, widthHz: 1000, marginDb, active, ageS });
const rows = (peaks) => t.signalRows(peaks).map((row) => [row.channelHz, row.shown.frequencyHz, row.frequenciesHz]);
console.log(JSON.stringify({
  // A broadcast station's narrow and wide signals, on one channel, among others.
  station: rows([peak(98_803_400, 98_800_000, 15, true, 0), peak(145_500_000, 145_500_000, 20, false, 4),
                 peak(98_796_000, 98_800_000, 25, true, 0), peak(98_500_000, 98_500_000, 12, false, 9)]),
  // An active signal speaks for its row before a stronger inactive one.
  active: rows([peak(100_210_000, 100_200_000, 29, false, 4), peak(100_200_100, 100_200_000, 15, true, 0)]),
  // Without an active one, the one found latest.
  latest: rows([peak(100_210_000, 100_200_000, 29, false, 40), peak(100_200_100, 100_200_000, 15, false, 4)]),
  empty: rows([]),
}));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class SignalRowsTest(unittest.TestCase):
    """signalrows.js, run in Node.js."""

    def setUp(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, (WEB_DIR / "signalrows.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        self.found = json.loads(done.stdout)

    def test_the_signals_of_one_channel_share_its_row_the_lowest_channel_first(self):
        self.assertEqual(self.found["station"], [
            [98_500_000, 98_500_000, [98_500_000]],
            [98_800_000, 98_796_000, [98_796_000, 98_803_400]],     # the stronger of the two active ones
            [145_500_000, 145_500_000, [145_500_000]],
        ])

    def test_an_active_signal_speaks_for_its_row_or_else_the_latest_found(self):
        self.assertEqual(self.found["active"], [[100_200_000, 100_200_100, [100_200_100, 100_210_000]]])
        self.assertEqual(self.found["latest"], [[100_200_000, 100_200_100, [100_200_100, 100_210_000]]])

    def test_no_signal_gives_no_row(self):
        self.assertEqual(self.found["empty"], [])


if __name__ == "__main__":
    unittest.main()
