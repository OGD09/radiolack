"""Checks of the overview bar (overview.js), a typed module, without a
browser: Node.js runs a script that imports it from the interface's folder
and prints what it gives, as JSON. Skipped where Node.js is not installed;
GitHub's runners and the type check's job have it."""

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

PLAN_END_HZ = 10_500_000_000        # the 3 cm band's upper edge, the end of every plan so far

# Four bands: 0 Hz to 8.3 kHz, which starts below the bar, 40 m, 41 m and
# 3 cm, with a gap between 41 m and 3 cm.
BANDS = [
    {"from": 0, "to": 8_300, "name": "Not allocated", "rgb": "1,1,1"},
    {"from": 7_000_000, "to": 7_200_000, "name": "40 m amateur", "rgb": "2,2,2"},
    {"from": 7_200_000, "to": 7_450_000, "name": "41 m broadcast", "rgb": "3,3,3"},
    {"from": 10_000_000_000, "to": 10_500_000_000, "name": "3 cm amateur", "rgb": "4,4,4"},
]

SCRIPT = """
const o = await import(process.argv[1]);
const bands = JSON.parse(process.argv[2]);
const end = Number(process.argv[3]);
const round = (v) => Math.round(v * 1000) / 1000;
const scale = o.overviewScale(end);
const at = (f) => o.positionOf(f, scale);
const under = (f) => { const b = o.bandUnder(bands, at(f), scale); return b ? b.name : null; };
const drawing = (state) => {
  const d = o.overviewDrawing({ bands, scale, width: 1000, pxPerCss: 2, vfo: 145.5e6, ...state });
  return {
    pieces: d.pieces.map((p) => [round(p.x0), round(p.x1), p.rgb, p.reach]),
    reach: d.reach.map(round), span: d.span.map(round), view: d.view.map(round), vfo: round(d.vfo),
    ticks: d.ticks.map(round),
  };
};
console.log(JSON.stringify({
  constants: [o.OVERVIEW_LOW_HZ, o.OVERVIEW_HEIGHT_PX, o.OVERVIEW_SPAN_MIN_PX, o.OVERVIEW_VIEW_MIN_PX],
  heights: [o.overviewHeight(false), o.overviewHeight(true)],
  scale: [scale.low, round(scale.high)],
  small: [o.overviewScale(0), o.overviewScale(5000), o.overviewScale(NaN)],
  positions: [0, 999, 1000, 1e4, 1e6, 1e8, end, 2 * end, NaN, -5].map(at).map(round),
  back: [0, 0.5, 1, -1, 2, NaN].map((s) => round(o.frequencyAt(s, scale))),
  roundTrip: [1234, 7.1e6, 145.5e6, 10.25e9].map((f) => round(o.frequencyAt(at(f), scale))),
  decades: o.decades(scale),
  decadesSmall: o.decades(o.overviewScale(5000)),
  under: [500, 5000, 7.1e6, 7.3e6, 8e6, 10.2e9, end].map(under),
  title: [o.overviewTitle(bands[1]), o.overviewTitle(bands[3])],
  wide: drawing({ reach: [24e6, 1766e6], span: [143.8e6, 146.2e6], view: [0, end] }),
  narrow: drawing({ reach: [143.8e6, 146.2e6], span: [143.8e6, 146.2e6], view: [145.4e6, 145.6e6] }),
}));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class OverviewTest(unittest.TestCase):
    """overview.js, run in Node.js."""

    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, (WEB_DIR / "overview.js").as_uri(),
                               json.dumps(BANDS), str(PLAN_END_HZ)],
                              capture_output=True, text=True, timeout=60, check=True)
        cls.found = json.loads(done.stdout)

    @staticmethod
    def share(f: float) -> float:
        """Where ``f`` lies on the bar, from 1 kHz to the plan's end."""
        return (math.log10(f) - 3) / (math.log10(PLAN_END_HZ) - 3)

    def test_the_bar_spans_seven_decades_from_1_khz_to_the_plans_end(self):
        found = self.found
        self.assertEqual(found["constants"], [1000, {"mouse": 10, "touch": 14}, 2, 5])
        self.assertEqual(found["heights"], [10, 14])
        self.assertEqual(found["scale"], [3, round(math.log10(PLAN_END_HZ), 3)])
        # A plan that ends within a decade of 1 kHz, or none, still gives a decade.
        self.assertEqual(found["small"], [{"low": 3, "high": 4}] * 3)
        # Below 1 kHz, 0 Hz included, at the lower end; beyond the end at the upper end.
        self.assertEqual(found["positions"], [0, 0, 0, *(js_round(self.share(f)) for f in (1e4, 1e6, 1e8)), 1, 1, 0, 0])
        self.assertEqual(found["decades"], [1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10])
        self.assertEqual(found["decadesSmall"], [])

    def test_a_share_of_the_bar_gives_back_its_frequency(self):
        found = self.found
        middle = 10 ** ((3 + math.log10(PLAN_END_HZ)) / 2)
        self.assertEqual(found["back"][:3], [1000, round(middle, 3), PLAN_END_HZ])
        self.assertEqual(found["back"][3:], [1000, PLAN_END_HZ, 1000])       # kept within the bar
        for f, back in zip([1234, 7.1e6, 145.5e6, 10.25e9], found["roundTrip"]):
            self.assertAlmostEqual(back, f, delta=f * 1e-9)

    def test_the_band_under_a_point_and_its_tooltip(self):
        found = self.found
        # Below 1 kHz, the bar's lower end: the band at 1 kHz; between 41 m and 3 cm, none.
        self.assertEqual(found["under"], ["Not allocated", "Not allocated", "40 m amateur", "41 m broadcast", None,
                                          "3 cm amateur", None])
        self.assertEqual(found["title"], ["40 m amateur \N{MIDDLE DOT} 7.000\N{EN DASH}7.200 MHz",
                                          "3 cm amateur \N{MIDDLE DOT} 10000.000\N{EN DASH}10500.000 MHz"])

    def test_the_bands_are_brighter_within_the_reach(self):
        wide = self.found["wide"]
        x = lambda f: js_round(self.share(f) * 1000)     # noqa: E731
        # 0 Hz to 8.3 kHz from the bar's lower end; 40 m and 41 m below the reach; 3 cm above it.
        self.assertEqual(wide["pieces"], [
            [0, x(8300), "1,1,1", False],
            [x(7e6), x(7.2e6), "2,2,2", False],
            [x(7.2e6), x(7.45e6), "3,3,3", False],
            [x(1e10), 1000, "4,4,4", False],
        ])
        self.assertEqual(wide["reach"], [x(24e6), x(1766e6)])
        self.assertEqual(wide["view"], [0, 1000])
        self.assertEqual(wide["vfo"], x(145.5e6))
        self.assertEqual(wide["ticks"], [x(10 ** d) for d in range(4, 11)])
        # A band that the reach's edges cut, in seven decades over 700 px, 100 px each from 1 kHz: the part
        # within the reach, from 10 to 100 MHz, is brighter.
        self.assertEqual(self.run_cut(), [[300, 400, "9,9,9", False], [400, 500, "9,9,9", True],
                                          [500, 600, "9,9,9", False]])

    def test_a_span_and_a_view_too_narrow_to_see_widen_around_their_middle(self):
        narrow = self.found["narrow"]
        # At 2 px per CSS px: the span 4 px wide at least, the view 10 px.
        for interval, least in ((narrow["span"], 4), (narrow["view"], 10)):
            self.assertAlmostEqual(interval[1] - interval[0], least, delta=0.002)
        middle = (self.share(143.8e6) + self.share(146.2e6)) * 500
        self.assertAlmostEqual((narrow["span"][0] + narrow["span"][1]) / 2, middle, delta=0.002)
        wide = self.found["wide"]
        self.assertGreater(wide["span"][1] - wide["span"][0], 0)

    @staticmethod
    def run_cut() -> list:
        """The pieces of one band from 1 MHz to 1 GHz, with a reach from
        10 MHz to 100 MHz inside it."""
        script = """
        const o = await import(process.argv[1]);
        const scale = o.overviewScale(1e10);
        const d = o.overviewDrawing({ bands: [{ from: 1e6, to: 1e9, rgb: '9,9,9' }], scale, width: 700, pxPerCss: 1,
          reach: [1e7, 1e8], span: [1e7, 1e8], view: [1e7, 1e8], vfo: 5e7 });
        console.log(JSON.stringify(d.pieces.map((p) => [Math.round(p.x0), Math.round(p.x1), p.rgb, p.reach])));
        """
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / "overview.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)


if __name__ == "__main__":
    unittest.main()
