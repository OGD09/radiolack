"""Checks of the interface's deep zoom (deepzoom.js) and of the spectra's
histories (history.js), typed modules without a browser: Node.js runs a
script that imports them from the interface's folder and prints what they
give, as JSON. Skipped where Node.js is not installed; GitHub's runners and
the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# What deepzoom.js gives, case by case.
DEEP_ZOOM_SCRIPT = """
const z = await import(process.argv[1]);
const cover = z.fineCover(145.6e6, 48000);      // 145.5808 to 145.6192 MHz
const wideBin = 2.4e6 / 8192, remoteBin = 2.4e6 / 2048;   // at 2.4 MS/s, as this computer and a paired device get it
const deep = { cover, narrowestHz: z.fineNarrowest(48000, 8192) };
const fit = (centre, width, from, d = deep) => z.fitDeepView(centre, width, [0, 6e9], z.wideNarrowest(wideBin), d, from);
const out = {
  frames: [['NFM', 145.6e6], ['CW', 7.0301e6], ['WFM', 98.1e6], ['USB', 14.2e6]].map(([mode, f]) => z.fineFrameFor(f, mode)),
  cover, wfmCover: z.fineCover(98.1e6, 320000), coverBins: [8192, 2048].map(z.coverBins),
  received: [
    { known: true, vfoHz: 145.6e6, mode: 'NFM', span: [143.8e6, 146.2e6] },
    { known: false, vfoHz: 145.6e6, mode: 'NFM', span: [143.8e6, 146.2e6] },
    { known: true, vfoHz: 146.3e6, mode: 'NFM', span: [143.8e6, 146.2e6] },
    { known: true, vfoHz: 98.1e6, mode: 'WFM', span: [97.975e6, 98.225e6] },
    { known: true, vfoHz: 98.1e6, mode: 'NFM', span: [97.975e6, 98.225e6] },
    { known: true, vfoHz: 98.1e6, mode: 'WFM', span: [97.6e6, 98.6e6] },
  ].map(z.fineReceived),
  shares: [[[0, 10], [0, 10]], [[0, 10], [5, 20]], [[0, 10], [20, 30]], [[5, 5], [0, 10]], [[0, 10], [-5, 2.5]]]
    .map(([view, c]) => z.coveredShare(view, c)),
  zoomed: [
    [false, [145.593e6, 145.593e6 + 47.9 * wideBin], wideBin],     // narrower than 48 wide bins, inside: in
    [false, [145.593e6, 145.593e6 + 48.1 * wideBin], wideBin],     // wider: out
    [false, [145.575e6, 145.585e6], wideBin],                       // partly beyond the cover: out
    [true, [145.590e6, 145.590e6 + 71.9 * wideBin], wideBin],      // already in, under 72 bins: stays
    [true, [145.590e6, 145.590e6 + 72.1 * wideBin], wideBin],      // 72 bins: back to the wide spectrum
    [true, [145.6142e6, 145.6242e6], wideBin],                      // half of it covered: stays
    [true, [145.6144e6, 145.6244e6], wideBin],                      // less than half: back
    [false, [145.5809e6, 145.6191e6], remoteBin],                   // a remote device: the cover sets it
    [true, [145.57e6, 145.63e6], remoteBin],                         // 60 kHz, 64% covered: stays
    [true, [145.55e6, 145.65e6], remoteBin],                         // 100 kHz, 38% covered: back
  ].map(([was, view, bin]) => z.deepZoomed(was, view, cover, bin)),
  wideNarrowest: [wideBin, remoteBin, 100].map(z.wideNarrowest),
  fineNarrowest: [[48000, 8192], [48000, 2048], [320000, 8192]].map(([w, n]) => z.fineNarrowest(w, n)),
  fitted: [
    z.fitDeepView(145.6e6, 50, [0, 6e9], 4687.5, null, 10000),   // without the fine spectrum: the wide limit
    fit(145.6e6, 50, 10000),            // the fine spectrum's narrowest view, 93.75 Hz
    fit(145.601e6, 1000, 4687.5),       // inside the cover: as asked
    fit(145.7e6, 1000, 4687.5),         // zooming in from the wide limit outside the cover: stops at the wide limit
    fit(145.619e6, 1000, 1000),         // a narrow view moved past the cover's edge: stops at the edge
    fit(145.5e6, 1000, 1000),           // far past it: at the edge too
    fit(145.6e6, 10000, 10000),         // wider than the wide limit: as asked, wherever it lies
  ],
  positions: [145.6e6 - 24000, 145.6e6, 145.6e6 + 5.859375, 145.6e6 + 19200].map((f) => z.binPosition(f, 145.6e6, 48000, 8192)),
  notes: [z.resolutionNote(true, 48000 / 8192), z.resolutionNote(true, 48000 / 2048), z.resolutionNote(false, wideBin),
    z.resolutionNote(false, remoteBin)],
  decimals: [[93.75, 1280], [4687.5, 1280], [2.4e6, 1280], [12800, 1280], [12801, 1280]].map(([v, w]) => z.readoutDecimals(v, w)),
};
console.log(JSON.stringify(out));
"""

# A history of 4 lines over 8 bins, 800 Hz wide around 1 kHz: what it keeps
# as frames come, as lines are made and as the capture slides.
HISTORY_SCRIPT = """
const { SpectrumHistory, HIST_DB_MIN, HIST_DB_STEP } = await import(process.argv[1]);
const h = new SpectrumHistory(4);
const out = { empty: h.commitLine(0, 0, 250), binHz: h.binHz };
h.reset(1000, 800, 8);
// Codes stand for -100 + c dB: bins at -90, -80, ..., -20 dB.
const codes = Uint8Array.from({ length: 8 }, (_, i) => 10 * (i + 1));
h.add(codes, -100, 1, 0.5, 1000);
out.first = Array.from(h.avgDb, (db) => Math.round(db * 10) / 10);
h.add(new Uint8Array(8), -100, 1, 0.5, 1010);       // -100 dB everywhere, averaged by half in power
out.averaged = Math.round(h.avgDb[7] * 100) / 100;
out.peak = Array.from(h.peakDb, Math.round);
out.line = h.commitLine(5000, 1020, 250);
out.code = h.line(h.head)[7];       // the mean power of -20 and -100 dB: -23 dB
out.expected = Math.round((-23.0103 - HIST_DB_MIN) / HIST_DB_STEP);
out.repeated = h.commitLine(5040, 1200, 250);       // no frame since: the latest repeats while 250 ms old at most
out.waits = h.commitLine(5080, 1300, 250);          // older: the history waits
out.rows = [h.filled, h.head, h.rowAt(0), h.rowAt(1), h.rowAt(2), h.rowAt(-1)];
const version = h.version;
h.slide(1200);      // 200 Hz up: two bins
out.slid = { offsets: Array.from(h.lineOffsets()), peak: Array.from(h.peakDb, Math.round), primed: h.avgPrimed,
  changed: h.version > version, slides: [h.slides(1500, 800, 8), h.slides(2000, 800, 8), h.slides(1500, 400, 8),
    h.slides(1500, 800, 16)] };
out.lineBins = [[1000, -400, 400], [1350, -400, 400], [1460, -400, 400], [1000, -300, 300], [690, -300, 300], [700, -300, 300]]
  .map(([f, lo, hi]) => h.lineBinAt(h.head, f, lo, hi));
h.clearPeaks();
out.cleared = Array.from(h.peakDb).every((db) => db === -300);
h.clear();
out.afterClear = [h.widthHz, h.filled, h.frames, h.slides(1200, 800, 8), h.commitLine(9000, 1300, 250)];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class InterfaceModulesTest(unittest.TestCase):
    """deepzoom.js and history.js, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        """What ``script`` prints, as JSON, given the interface's ``module``
        as its argument, a file URL."""
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_deep_zoom_decides_from_the_view_the_cover_and_the_bins(self):
        found = self.run_script(DEEP_ZOOM_SCRIPT, "deepzoom.js")
        # The channel's 0 Hz on the air: the VFO, and in CW 700 Hz below it.
        self.assertEqual(found["frames"], [{"centreHz": 145.6e6, "widthHz": 48_000}, {"centreHz": 7.0294e6, "widthHz": 48_000},
                                           {"centreHz": 98.1e6, "widthHz": 320_000}, {"centreHz": 14.2e6, "widthHz": 48_000}])
        self.assertEqual(found["cover"], [145_580_800, 145_619_200])
        self.assertEqual(found["wfmCover"], [97_972_000, 98_228_000])
        self.assertEqual(found["coverBins"], [[820, 7373], [205, 1844]])
        # Only an engine that knows it, with the VFO in the span and a receiver as fast as the channel.
        self.assertEqual(found["received"], [True, False, False, False, True, True])
        self.assertEqual(found["shares"], [1, 0.5, 0, 0, 0.25])
        self.assertEqual(found["zoomed"], [True, False, False, True, False, True, False, True, True, False])
        self.assertEqual(found["wideNarrowest"], [4687.5, 18_750, 4000])
        self.assertEqual(found["fineNarrowest"], [93.75, 375, 625])
        fitted = found["fitted"]
        self.assertEqual(fitted[0], [145_600_000 - 2343.75, 145_600_000 + 2343.75])
        self.assertEqual(fitted[1], [145_600_000 - 46.875, 145_600_000 + 46.875])
        self.assertEqual(fitted[2], [145_600_500, 145_601_500])
        self.assertEqual(fitted[3], [145_700_000 - 2343.75, 145_700_000 + 2343.75])
        self.assertEqual(fitted[4], [145_618_200, 145_619_200])
        self.assertEqual(fitted[5], [145_580_800, 145_581_800])
        self.assertEqual(fitted[6], [145_595_000, 145_605_000])
        # Bin i of 8192 over 48 kHz is centred on the centre less 24 kHz plus i bins of 5.859375 Hz.
        self.assertEqual(found["positions"], [0, 4096, 4097, 4096 + 0.4 * 8192])
        self.assertEqual(found["notes"], ["Fine spectrum of the VFO's channel: 5.9 Hz per bin.",
                                          "Fine spectrum of the VFO's channel: 23 Hz per bin.",
                                          "Wide spectrum: 293 Hz per bin.", "Wide spectrum: 1172 Hz per bin."])
        self.assertEqual(found["decimals"], [6, 6, 5, 5, 5])

    def test_a_history_averages_makes_lines_and_slides_with_its_capture(self):
        found = self.run_script(HISTORY_SCRIPT, "history.js")
        self.assertEqual((found["empty"], found["binHz"]), (False, 0))
        self.assertEqual(found["first"], [-90, -80, -70, -60, -50, -40, -30, -20])
        self.assertEqual(found["averaged"], -23.01)
        self.assertEqual(found["peak"], [-90, -80, -70, -60, -50, -40, -30, -20])
        self.assertTrue(found["line"])
        self.assertEqual(found["code"], found["expected"])
        self.assertEqual((found["repeated"], found["waits"]), (True, False))
        self.assertEqual(found["rows"], [2, 1, 1, 0, -1, -1])
        slid = found["slid"]
        # The two lines made at 1 kHz lie 2 bins below the capture's first bin, now at 1.2 kHz.
        self.assertEqual(slid["offsets"][:2], [-2, -2])
        self.assertEqual(slid["peak"], [-70, -60, -50, -40, -30, -20, -300, -300])
        self.assertFalse(slid["primed"])
        self.assertTrue(slid["changed"])
        self.assertEqual(slid["slides"], [True, False, False, False])
        # The newest line came from the capture around 1 kHz: its bins there, within the limits given.
        self.assertEqual(found["lineBins"], [4, 7, -1, 4, -1, 1])
        self.assertTrue(found["cleared"])
        self.assertEqual(found["afterClear"], [0, 0, 0, False, False])


if __name__ == "__main__":
    unittest.main()
