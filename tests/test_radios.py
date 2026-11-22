"""Checks of the Receivers menu's receivers, the protocol's radios
(radios.js), and of the radios message's decoding (stream.js), typed modules without a browser: Node.js
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
const listed = { listing: 'sysfs', devices: [device('59196661', { inUse: true, radio: 1 }), device('RTLSDR1', { name: 'RTL2838UHIDIR' }),
  device('00000002', { radio: 3 })] };
const soapy = { search: 'done', detail: '', drivers: ['hackrf'], chosen: false, args: null, devices: [
  { driver: 'hackrf', serial: '0001', label: 'HackRF One', inUse: false },
  { driver: 'airspy', serial: '0002', label: 'Airspy', inUse: true }] };
const source = { kind: 'rtl_sdr', kinds };
const adds = (...args) => r.addOptions(...args).map((o) => `${o.label}|${JSON.stringify(o.source)}|${o.plugged}`);
const names = new Map();
r.learnNames(names, listed, soapy);
const tcp = radio(4, { kind: 'rtl_tcp', device: '127.0.0.1:1234', status: 'waiting' });
const band = radio(3, { kind: 'synthetic', device: null, status: 'idle', scan: 'sweep' });
const hackrf = radio(5, { kind: 'soapy', device: 'hackrf 0001', status: 'connecting' });
const out = {
  names: [...names.entries()].sort(),
  cards: r.receiverCards(state(2, nesdr, v3, band, tcp, hackrf), names, 104100000),
  cardsWithoutCentre: r.receiverCards(state(1, radio(1, { device: '0' })), new Map(), null),
  shownReceiver: [r.shownReceiver(state(2, nesdr, v3), names), r.shownReceiver(state(1, band), names), r.shownReceiver(null, names)],
  keys: [nesdr, radio(3, { kind: 'synthetic', device: null })].map(r.radioKey),
  badge: [[radio(1)], [nesdr, radio(2)], [radio(1), v3]].map(r.radiosBadge),
  add: adds(state(1, nesdr), source, listed, soapy, true),
  addWithoutList: adds(state(1, radio(1, { kind: 'synthetic', device: null })), source, null, null, true),
  addFull: adds({ ...state(1, nesdr, v3), max: 2 }, source, listed, soapy, true),
  addClosed: adds(state(1, nesdr), source, listed, soapy, false),
  addOlder: adds(null, source, listed, soapy, true),
  addRow: [
    r.addRow(state(1, nesdr), r.addOptions(state(1, nesdr), source, listed, soapy, true)),
    r.addRow(state(1, nesdr), r.addOptions(state(1, nesdr), source, null, null, true)),
    r.addRow(state(1, nesdr), []),
    r.addRow({ ...state(1, nesdr, v3), max: 2 }, [])],
  remove: [
    r.removeView(state(1, nesdr, v3), false), r.removeView(state(2, nesdr, v3), false),
    r.removeView(state(2, nesdr, v3), true), r.removeView(state(2, nesdr, radio(2)), true), r.removeView(null, false),
    r.removeView(state(1, nesdr), false)],
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

    def test_each_receiver_has_a_card_with_its_device_state_and_activity(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["names"], [["rtl_sdr:00000002", "NESDR SMArt v5"], ["rtl_sdr:59196661", "NESDR SMArt v5"], ["rtl_sdr:RTLSDR1", "RTL2838UHIDIR"],
                                        ["soapy:airspy 0002", "Airspy"], ["soapy:hackrf 0001", "HackRF One"]])
        self.assertEqual(out["cards"], [
            {"id": 1, "name": "NESDR SMArt v5", "serial": "59196661", "shown": False, "state": "live",
             "stateText": "Receiving", "tags": [{"kind": "scan", "text": "memory scan"}]},
            {"id": 2, "name": "RTL2838UHIDIR", "serial": "RTLSDR1", "shown": True, "state": "live",
             "stateText": "Receiving \N{MIDDLE DOT} 104.100 MHz",
             "tags": [{"kind": "rec", "text": "recording audio"}, {"kind": "rec", "text": "recording I/Q"}]},
            {"id": 3, "name": "Synthetic band", "serial": "", "shown": False, "state": "idle", "stateText": "Idle",
             "tags": [{"kind": "scan", "text": "range scan"}]},
            {"id": 4, "name": "rtl_tcp server", "serial": "127.0.0.1:1234", "shown": False, "state": "wait",
             "stateText": "Waiting for its device", "tags": []},
            {"id": 5, "name": "HackRF One", "serial": "0001", "shown": False, "state": "wait", "stateText": "Connecting",
             "tags": []}])
        # Before any list, an RTL-SDR is named by its kind; without the centre, the state alone.
        self.assertEqual(out["cardsWithoutCentre"], [
            {"id": 1, "name": "RTL-SDR", "serial": "0", "shown": True, "state": "live", "stateText": "Receiving", "tags": []}])
        self.assertEqual(out["shownReceiver"], [{"id": 2, "name": "RTL2838UHIDIR"}, None, None])
        self.assertEqual(out["keys"], ["rtl_sdr:59196661", "synthetic:"])
        self.assertEqual(out["badge"], [{"scanning": False, "recording": False}, {"scanning": True, "recording": False},
                                        {"scanning": False, "recording": True}])

    def test_a_receiver_is_added_from_a_device_that_none_reads(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        self.assertEqual(out["add"], [
            'Synthetic band (2 m)|{"kind":"synthetic"}|false',
            'RTL-SDR \N{MIDDLE DOT} RTL2838UHIDIR \N{MIDDLE DOT} RTLSDR1|{"kind":"rtl_sdr","device":"RTLSDR1"}|true',
            'RTL-SDR \N{MIDDLE DOT} rtl_tcp|{"kind":"rtl_tcp"}|false',
            'SoapySDR \N{MIDDLE DOT} HackRF One|{"kind":"soapy","driver":"hackrf","serial":"0001"}|true'])
        self.assertEqual(out["addWithoutList"], [
            'RTL-SDR \N{MIDDLE DOT} this computer|{"kind":"rtl_sdr","device":"0"}|false',
            'RTL-SDR \N{MIDDLE DOT} rtl_tcp|{"kind":"rtl_tcp"}|false'])
        self.assertEqual(out["addFull"], [])
        self.assertEqual(out["addClosed"], [])
        self.assertEqual(out["addOlder"], [])
        self.assertEqual(out["addRow"], [
            {"enabled": True, "detail": "2 more plugged in \N{MIDDLE DOT} up to 4"},
            {"enabled": True, "detail": "No other device plugged in \N{MIDDLE DOT} up to 4"},
            {"enabled": False, "detail": "No other device plugged in \N{MIDDLE DOT} up to 4"},
            {"enabled": False, "detail": "2 at most: remove one first"}])

    def test_the_first_receiver_stays_and_a_busy_one_takes_a_second_press(self):
        out = self.run_script(RADIOS_SCRIPT, "radios.js")
        first = {"shown": True, "enabled": False, "button": "Remove", "title": "Receiver 1 stays: change its device instead"}
        self.assertEqual(out["remove"], [
            first, {"shown": True, "enabled": True, "button": "Remove", "title": ""},
            {"shown": True, "enabled": True, "button": "Confirm: stops recording audio, recording I/Q", "title": ""},
            {"shown": True, "enabled": True, "button": "Remove", "title": ""},
            {"shown": False, "enabled": False, "button": "Remove", "title": ""},
            {**first, "shown": False}])
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
