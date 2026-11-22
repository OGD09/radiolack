"""Checks of the Receiver menu's radios (radios.js) and of the radios
message's decoding (stream.js), typed modules without a browser: Node.js
runs a script that imports them from the interface's folder and prints
what they give, as JSON. Skipped where Node.js is not installed; GitHub's
runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

RADIOS_SCRIPT = """
const r = await import(process.argv[1]);
const kinds = ['synthetic', 'rtl_sdr', 'rtl_tcp', 'soapy'];
const radio = (id, more = {}) => ({ id, kind: 'rtl_sdr', device: '59196661', tuner: 'Rafael Micro R820T',
  status: 'streaming', scan: null, recording: [], ...more });
const nesdr = radio(1, { scan: 'memory' });
const v3 = radio(2, { device: 'RTLSDR1', recording: ['audio', 'iq'] });
const state = (shown, ...radios) => ({ radios, shown, max: 4 });
const device = (serial, more = {}) => ({ device: serial, name: 'NESDR SMArt v5', manufacturer: 'Nooelec', drivers: [],
  shared: false, readsAsIndex: false, inUse: false, radio: null, found: true, ...more });
const listed = { listing: 'sysfs', devices: [device('59196661', { inUse: true, radio: 1 }), device('RTLSDR1'),
  device('00000002', { radio: 3 })] };
const soapy = { search: 'done', detail: '', drivers: ['hackrf'], chosen: false, args: null, devices: [
  { driver: 'hackrf', serial: '0001', label: 'HackRF One', inUse: false },
  { driver: 'airspy', serial: '0002', label: 'Airspy', inUse: true }] };
const source = { kind: 'rtl_sdr', kinds };
const adds = (...args) => r.addOptions(...args).map((o) => `${o.label}|${JSON.stringify(o.source)}`);
const out = {
  labels: [nesdr, v3, radio(3, { kind: 'synthetic', device: null, status: 'streaming', scan: 'sweep' }),
    radio(4, { kind: 'rtl_tcp', device: '127.0.0.1:1234', status: 'waiting' })].map(r.radioLabel),
  keys: [nesdr, radio(3, { kind: 'synthetic', device: null })].map(r.radioKey),
  badge: [[radio(1)], [nesdr, radio(2)], [radio(1), v3]].map(r.radiosBadge),
  add: adds(state(1, nesdr), source, listed, soapy, true),
  addWithoutList: adds(state(1, radio(1, { kind: 'synthetic', device: null })), source, null, null, true),
  addFull: adds({ ...state(1, nesdr, v3), max: 2 }, source, listed, soapy, true),
  addClosed: adds(state(1, nesdr), source, listed, soapy, false),
  addOlder: adds(null, source, listed, soapy, true),
  remove: [
    r.removeView(state(1, nesdr, v3), false), r.removeView(state(2, nesdr, v3), false),
    r.removeView(state(2, nesdr, v3), true), r.removeView(state(2, nesdr, radio(2)), true), r.removeView(null, false)],
  press: [r.removePress(state(1, nesdr, v3), false), r.removePress(state(2, nesdr, v3), false),
    r.removePress(state(2, nesdr, v3), true), r.removePress(state(2, nesdr, radio(2)), false)],
  restore: [r.radioToRestore(state(1, nesdr, v3), 'rtl_sdr:RTLSDR1'), r.radioToRestore(state(2, nesdr, v3), 'rtl_sdr:RTLSDR1'),
    r.radioToRestore(state(1, nesdr, v3), 'soapy:hackrf 0001'), r.radioToRestore(state(1, nesdr, v3), '')],
};
console.log(JSON.stringify(out));
"""

DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const radio = { id: 1, kind: 'rtl_sdr', device: '59196661', tuner: 'Rafael Micro R820T', status: 'streaming',
  scan: 'memory', recording: ['audio'] };
const valid = { type: 'radios', radios: [radio, { ...radio, id: 2, device: null, tuner: null, scan: null, recording: [] }],
  shown: 2, max: 4 };
const out = {
  valid: s.decodeRadios(valid),
  refused: [
    { ...valid, radios: [] }, { ...valid, radios: null }, { ...valid, shown: 3 }, { ...valid, shown: null },
    { ...valid, max: 0 }, { ...valid, radios: [{ ...radio, id: 0 }] }, { ...valid, radios: [{ ...radio, recording: [1] }] },
    { ...valid, radios: [{ ...radio, scan: 1 }] }, { ...valid, radios: [null] },
  ].map(s.decodeRadios),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class RadiosModuleTest(unittest.TestCase):
    """radios.js and stream.js's decoding, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_radio_list_says_what_each_radio_reads_and_runs(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["labels"], [
            "Radio 1 · RTL-SDR · 59196661 · memory scan",
            "Radio 2 · RTL-SDR · RTLSDR1 · recording audio · recording I/Q",
            "Radio 3 · Synthetic band · range scan",
            "Radio 4 · rtl_tcp · 127.0.0.1:1234 · waiting"])
        self.assertEqual(out["keys"], ["rtl_sdr:59196661", "synthetic:"])
        self.assertEqual(out["badge"], [{"scanning": False, "recording": False}, {"scanning": True, "recording": False},
                                        {"scanning": False, "recording": True}])

    def test_a_radio_is_added_from_a_receiver_that_no_radio_reads(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["add"], [
            'Synthetic band (2 m)|{"kind":"synthetic"}',
            'RTL-SDR · NESDR SMArt v5 · RTLSDR1|{"kind":"rtl_sdr","device":"RTLSDR1"}',
            'RTL-SDR · rtl_tcp|{"kind":"rtl_tcp"}',
            'SoapySDR · HackRF One|{"kind":"soapy","driver":"hackrf","serial":"0001"}'])
        self.assertEqual(out["addWithoutList"], [
            'RTL-SDR · this computer|{"kind":"rtl_sdr","device":"0"}', 'RTL-SDR · rtl_tcp|{"kind":"rtl_tcp"}'])
        self.assertEqual(out["addFull"], [])
        self.assertEqual(out["addClosed"], [])
        self.assertEqual(out["addOlder"], [])

    def test_the_first_radio_stays_and_a_busy_one_takes_a_second_press(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["remove"], [
            {"enabled": False, "button": "Remove this radio"}, {"enabled": True, "button": "Remove this radio"},
            {"enabled": True, "button": "Confirm: stops recording audio, recording I/Q"},
            {"enabled": True, "button": "Remove this radio"}, {"enabled": False, "button": "Remove this radio"}])
        self.assertEqual(out["press"], ["none", "arm", "remove", "remove"])

    def test_the_radio_shown_last_is_shown_again_while_it_runs(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["restore"], [2, None, None, None])

    def test_decodes_the_message(self):
        out = self.run_script(DECODE_SCRIPT, "stream.js")
        self.assertEqual(out["valid"], {"radios": [
            {"id": 1, "kind": "rtl_sdr", "device": "59196661", "tuner": "Rafael Micro R820T", "status": "streaming",
             "scan": "memory", "recording": ["audio"]},
            {"id": 2, "kind": "rtl_sdr", "device": None, "tuner": None, "status": "streaming", "scan": None,
             "recording": []}], "shown": 2, "max": 4})
        self.assertEqual(out["refused"], [None] * 9)


if __name__ == "__main__":
    unittest.main()
