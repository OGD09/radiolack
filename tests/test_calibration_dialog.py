"""Checks of the Calibrate dialog's calibration on the stations
(calibration.js) and of the calibration state's decoding (stream.js),
typed modules without a browser: Node.js runs a script that imports them
from the interface's folder and prints what they give, as JSON. Skipped
where Node.js is not installed; GitHub's runners and the type check's job
have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# What calibration.js gives, case by case: a source state reduced to what
# it reads, and calibration states as stream.js decodes them.
RUN_SCRIPT = """
const c = await import(process.argv[1]);
const source = { ppm: 0, settable: ['centre_hz', 'ppm'], converter: 'none' };
const measures = ['carrier', 'driver_pll', 'calibration'];
const stations = [2500000, 5000000, 10000000];
const received = (frequencyHz, carrierHz, marginDb, ppm, agrees) => (
  { frequencyHz, carrierHz, marginDb, ppm, agrees, reason: null });
const missed = (frequencyHz, reason, marginDb = null) => (
  { frequencyHz, carrierHz: null, marginDb, ppm: null, agrees: false, reason });
const state = (more) => ({ running: false, stations, results: [], proposal: null, reason: null, ended: null, ...more });
const view = (s, more = {}) => c.runView({ state: s, source, measures, open: true, recording: false, ...more });
const done = state({
  results: [missed(2500000, 'no_signal', 14.2), received(5000000, 4999978.9, 38.5, 4.22, true),
    received(10000000, 9999958.1, 44.0, 4.19, true)],
  proposal: { ppm: 4.21, stations: 2 }, ended: 'done',
});
const out = {
  idle: view(null),
  idleEnded: view(state({ stations: [] })),
  blocked: [
    view(null, { measures: ['carrier'] }), view(null, { source: { ...source, settable: ['ppm'] } }),
    view(null, { source: { ...source, converter: 'up' } }), view(null, { recording: true }),
  ].map((v) => [v.enabled, v.note]),
  closed: view(null, { open: false }).enabled,
  running: view(state({ running: true, results: [missed(2500000, 'refused')] })),
  runningRecording: view(state({ running: true }), { recording: true }),
  done: view(done),
  apart: view(state({ ...done, results: [received(2500000, 2500012.5, 31, 9.0, false), ...done.results.slice(1)] }))
    .rows.map((row) => row.apart),
  inUse: view(done, { source: { ...source, ppm: 4.21 } }),
  single: view(state({ results: [received(2500000, 2499990, 35, 4.0, true)], proposal: { ppm: 4, stations: 1 },
    ended: 'stopped' })),
  reasons: ['none', 'weak', 'disagree', 'range', 'later'].map((reason) => view(state({ reason, ended: 'done' })).note),
  endings: ['tuned', 'source', 'scan', 'recording', 'closed'].map((ended) => view(state({ ended })).status),
  again: [null, 'stopped', 'calibration', 'lost', 'scan'].map(c.driverCheckAgain),
};
console.log(JSON.stringify(out));
"""

# What stream.js decodes of calibration messages, valid or not.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const valid = { type: 'calibration', running: false, stations: [2500000, 5000000],
  results: [
    { frequency_hz: 2500000, carrier_hz: null, margin_db: 14.2, ppm: null, agrees: false, reason: 'no_signal' },
    { frequency_hz: 5000000, carrier_hz: 4999978.9, margin_db: 38.5, ppm: 4.22, agrees: true, reason: null }],
  proposal: { ppm: 4.22, stations: 1 }, reason: null, ended: 'done' };
const result = valid.results[1];
const out = {
  valid: s.decodeCalibration(valid),
  idle: s.decodeCalibration({ type: 'calibration', running: false, stations: [], results: [], proposal: null,
    reason: null, ended: null }),
  refused: [
    { ...valid, running: 'no' }, { ...valid, stations: [2.5] }, { ...valid, stations: null },
    { ...valid, results: [{ ...result, reason: 'several' }] }, { ...valid, results: [{ ...result, ppm: null }] },
    { ...valid, results: [{ ...result, agrees: 1 }] }, { ...valid, results: [{ ...result, carrier_hz: Infinity }] },
    { ...valid, proposal: { ppm: 4.2 } }, { ...valid, proposal: { ppm: 4.2, stations: 0 } },
    { ...valid, reason: 3 }, { ...valid, ended: false }, { ...valid, results: [null] },
  ].map(s.decodeCalibration),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class CalibrationDialogTest(unittest.TestCase):
    """calibration.js's calibration on the stations and stream.js's
    decoding, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        """What ``script`` prints, as JSON, given the interface's ``module``
        as its argument, a file URL."""
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_dialog_shows_the_run_its_stations_and_its_proposal(self):
        found = self.run_script(RUN_SCRIPT, "calibration.js")
        idle = found["idle"]
        self.assertEqual((idle["button"], idle["enabled"], idle["status"], idle["rows"], idle["proposal"],
                          idle["note"], idle["apply"]),
                         ("Calibrate on the stations", True, "", [], "—", "", None))
        self.assertEqual(found["idleEnded"]["rows"], [])
        self.assertEqual(found["blocked"], [
            [False, "Calibrating on the stations needs Radiolack to restart."],
            [False, "Calibrating on the stations needs a receiver that tunes."],
            [False, "A converter is set: its oscillator would pass for the crystal's error. Calibrate on a known "
                    "carrier, or set the converter to None first."],
            [False, "A recording runs: stop it before calibrating on the stations."],
        ])
        self.assertFalse(found["closed"])

        running = found["running"]
        self.assertEqual((running["button"], running["enabled"]), ("Stop", True))
        self.assertEqual(running["status"], "Station 2 of 3: the receiver and the VFO come back at the end.")
        self.assertEqual([(row["station"], row["carrier"]) for row in running["rows"]],
                         [("2.500", "not tuned"), ("5.000", "Measuring…"), ("10.000", "")])
        # A run that runs stops, whatever would keep one from starting.
        self.assertEqual((found["runningRecording"]["enabled"], found["runningRecording"]["note"]), (True, ""))

        done = found["done"]
        self.assertEqual((done["button"], done["status"]), ("Calibrate on the stations", "Done: 2 of 3 stations received."))
        self.assertEqual(done["rows"], [
            {"station": "2.500", "carrier": "no carrier", "margin": "14 dB", "ppm": "", "apart": False},
            {"station": "5.000", "carrier": "−21.1 Hz", "margin": "39 dB", "ppm": "4.22", "apart": False},
            {"station": "10.000", "carrier": "−41.9 Hz", "margin": "44 dB", "ppm": "4.19", "apart": False},
        ])
        self.assertEqual((done["proposal"], done["note"], done["apply"]), ("4.21 ppm", "2 stations agree on it.", 4.21))
        self.assertEqual(found["apart"], [True, False, False])
        in_use = found["inUse"]
        self.assertEqual((in_use["note"], in_use["apply"]), ("This is the correction in use: nothing to change.", None))

        single = found["single"]
        self.assertEqual(single["status"], "Stopped after 1 of 3 stations: 1 received.")
        self.assertEqual((single["proposal"], single["apply"]), ("4.00 ppm", 4))
        self.assertIn("one station alone", single["note"])

        self.assertEqual(found["reasons"], [
            "No station was received: try again at another hour, as propagation allows, or check the antenna.",
            "One station alone was received, less than 30 dB above the noise: too weak to rely on. Try again at "
            "another hour.",
            "The stations received do not agree within 0.3 ppm: try again at another hour.",
            "The stations agree on a correction beyond the -100 to 100 ppm a receiver takes: check the antenna.",
            "Nothing is proposed.",
        ])
        self.assertEqual(found["endings"], [
            "Stopped: the receiver or the VFO was changed.", "Stopped: another receiver was chosen.",
            "Stopped: a scan started.", "Stopped: a recording started.", "Stopped.",
        ])
        # A driver check stopped by a change or refused during the run is asked again.
        self.assertEqual(found["again"], [False, True, True, False, False])

    def test_the_calibration_state_decodes_and_malformed_ones_are_refused(self):
        found = self.run_script(DECODE_SCRIPT, "stream.js")
        self.assertEqual(found["valid"], {
            "running": False, "stations": [2500000, 5000000],
            "results": [
                {"frequencyHz": 2500000, "carrierHz": None, "marginDb": 14.2, "ppm": None, "agrees": False,
                 "reason": "no_signal"},
                {"frequencyHz": 5000000, "carrierHz": 4999978.9, "marginDb": 38.5, "ppm": 4.22, "agrees": True,
                 "reason": None}],
            "proposal": {"ppm": 4.22, "stations": 1}, "reason": None, "ended": "done"})
        self.assertEqual(found["idle"]["stations"], [])
        self.assertEqual(found["refused"], [None] * 12)


if __name__ == "__main__":
    unittest.main()
