"""Checks of the Receiver menu's SoapySDR devices (devices.js) and of the
devices state's decoding (stream.js), typed modules without a browser:
Node.js runs a script that imports them from the interface's folder and
prints what they give, as JSON. Skipped where Node.js is not installed;
GitHub's runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR
from radiolack.soapysearch import LISTED_DRIVERS

NODE = shutil.which("node")

# What devices.js gives, case by case: a source state reduced to what it
# reads, and devices states as stream.js decodes them.
DEVICES_SCRIPT = """
const d = await import(process.argv[1]);
const drivers = JSON.parse(process.argv[2]);
const synthetic = { kind: 'synthetic', kinds: ['synthetic', 'rtl_sdr', 'rtl_tcp', 'soapy'] };
const soapy = { kind: 'soapy', kinds: synthetic.kinds };
const hackrf = { driver: 'hackrf', serial: '0001', label: 'HackRF One 0001', inUse: false };
const airspy = { driver: 'airspy', serial: 'a2', label: 'Airspy a2', inUse: false };
const state = (search, devices, more = {}) => ({ search, detail: '', drivers, devices, chosen: false, args: '', ...more });
const never = state('never', []);
const found = state('done', [hackrf, airspy]);
const inUse = state('done', [{ ...hackrf, inUse: true }, airspy], { chosen: true, args: 'driver=hackrf,serial=0001' });
const view = (...args) => d.devicesView(...args);
const out = {
  names: [[], ['a'], ['a', 'b'], ['a', 'b', 'c']].map(d.nameList),
  atOnce: [null, never, inUse].map(d.switchesAtOnce),
  notes: [
    d.searchNote(never, false), d.searchNote(never, true), d.searchNote(state('searching', []), false),
    d.searchNote(state('unavailable', [], { detail: 'the SoapySDR Python module is not installed' }), false),
    d.searchNote(state('failed', [], { detail: 'the airspy search failed: busy' }), false),
    d.searchNote(found, false), d.searchNote(state('done', []), false), d.searchNote(state('done', []), true),
    d.searchNote(state('done', [{ ...hackrf, inUse: true }]), false),
  ],
  hidden: [view(synthetic, found, false, false, true), view(null, found, true, false, true), view(synthetic, null, true, false, true),
    view({ ...synthetic, kinds: ['synthetic'] }, found, true, false, true)].map((v) => v.shown),
  picked: view(synthetic, never, true, false, true),
  pickedRemote: view(synthetic, never, true, true, true),
  searching: view(synthetic, state('searching', []), true, false, true),
  chooseFrom: view(synthetic, found, true, false, true),
  named: view(synthetic, found, true, false, true, { 'soapy:hackrf 0001': 'Bench HackRF' }).options.map((o) => o.label),
  closed: view(synthetic, found, true, false, false),
  using: view(soapy, inUse, false, false, true),
  usingRemote: view(soapy, { ...inUse, args: null }, false, true, true),
  at: ['0', '1', '2', '', 'x', '-1'].map((value) => d.deviceAt(found, value)),
  atNone: d.deviceAt(null, '0'),
};
console.log(JSON.stringify(out));
"""

# What stream.js decodes of devices messages, valid or not.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const valid = { type: 'devices', search: 'done', detail: '', drivers: ['hackrf'], chosen: true, args: 'driver=hackrf',
  devices: [{ driver: 'hackrf', serial: '0001', label: 'HackRF One', in_use: true }] };
const out = {
  valid: s.decodeDevices(valid),
  remote: s.decodeDevices({ ...valid, args: null }),
  refused: [
    { ...valid, search: 'later' }, { ...valid, detail: 1 }, { ...valid, drivers: 'hackrf' }, { ...valid, chosen: 'yes' },
    { ...valid, args: 3 }, { ...valid, devices: [{ driver: '', serial: '', label: '', in_use: false }] },
    { ...valid, devices: [{ driver: 'hackrf', serial: '0001', label: 'x' }] }, { ...valid, devices: null },
  ].map(s.decodeDevices),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class DevicesModuleTest(unittest.TestCase):
    """devices.js and stream.js's decoding, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str, *args: str) -> dict:
        """What ``script`` prints, as JSON, given the interface's ``module``
        as its first argument, a file URL, and ``args`` after it."""
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri(), *args],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_part_shows_lists_and_says_what_the_search_did(self):
        found = self.run_script(DEVICES_SCRIPT, "devices.js", json.dumps(list(LISTED_DRIVERS)))
        self.assertEqual(found["names"], ["", "a", "a and b", "a, b and c"])
        # An engine without devices state switches at once and says why it refuses.
        self.assertEqual(found["atOnce"], [True, False, True])
        searched = "hackrf, airspy, airspyhf, lime and bladerf"
        self.assertEqual(found["notes"], [
            "Not searched yet.", "Not searched yet: the computer searches.", f"Searching {searched}…",
            "The SoapySDR Python module is not installed.", f"The airspy search failed: busy. Searched: {searched}.",
            f"Searched: {searched}.", f"No device found. Searched: {searched}. Any other device opens by its arguments.",
            f"No device found. Searched: {searched}.",
            f"No other device found. Searched: {searched}. Any other device opens by its arguments.",
        ])
        # Hidden while another kind is in use, without a state, or without SoapySDR.
        self.assertEqual(found["hidden"], [False, False, False, False])

        picked = found["picked"]
        self.assertTrue(picked["shown"])
        self.assertEqual(picked["options"], [{"value": "", "label": "No device found"}])
        self.assertEqual(picked["value"], "")
        self.assertFalse(picked["listEnabled"])
        self.assertTrue(picked["searchShown"] and picked["searchEnabled"] and picked["argsShown"] and picked["argsEnabled"])
        self.assertTrue(picked["firstSearch"])

        # A device that remote access serves neither searches nor gives arguments.
        remote = found["pickedRemote"]
        self.assertTrue(remote["shown"])
        self.assertFalse(remote["searchShown"] or remote["searchEnabled"] or remote["argsShown"] or remote["argsEnabled"])
        self.assertFalse(remote["firstSearch"])

        searching = found["searching"]
        self.assertEqual(searching["options"], [{"value": "", "label": "Searching…"}])
        self.assertFalse(searching["listEnabled"] or searching["searchEnabled"] or searching["argsEnabled"])
        self.assertFalse(searching["firstSearch"])

        choose = found["chooseFrom"]
        self.assertEqual(choose["options"], [{"value": "", "label": "Choose a device"},
                                             {"value": "0", "label": "HackRF One 0001"}, {"value": "1", "label": "Airspy a2"}])
        self.assertTrue(choose["listEnabled"])
        self.assertFalse(choose["firstSearch"])
        # A name given to a device replaces its label.
        self.assertEqual(found["named"], ["Choose a device", "Bench HackRF", "Airspy a2"])
        closed = found["closed"]
        self.assertFalse(closed["listEnabled"] or closed["searchEnabled"] or closed["argsEnabled"] or closed["firstSearch"])

        using = found["using"]
        self.assertEqual(using["options"], [{"value": "0", "label": "HackRF One 0001"}, {"value": "1", "label": "Airspy a2"}])
        self.assertEqual(using["value"], "0")
        self.assertEqual(using["args"], "driver=hackrf,serial=0001")
        self.assertEqual(found["usingRemote"]["args"], "")

        self.assertEqual([device and device["serial"] for device in found["at"]], ["0001", "a2", None, None, None, None])
        self.assertIsNone(found["atNone"])

    def test_a_devices_message_decodes_only_when_whole(self):
        found = self.run_script(DECODE_SCRIPT, "stream.js")
        self.assertEqual(found["valid"], {
            "search": "done", "detail": "", "drivers": ["hackrf"], "chosen": True, "args": "driver=hackrf",
            "devices": [{"driver": "hackrf", "serial": "0001", "label": "HackRF One", "inUse": True}],
        })
        self.assertIsNone(found["remote"]["args"])
        self.assertEqual(found["refused"], [None] * 8)


if __name__ == "__main__":
    unittest.main()
