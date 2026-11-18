"""Checks of a SoapySDR device's own controls in the Receiver menu
(advanced.js), of the antenna power's warning for such a device (power.js)
and of the decoding of their fields in the source state (stream.js), typed
modules without a browser: Node.js runs a script that imports them from the
interface's folder and prints what they give, as JSON. Skipped where
Node.js is not installed; GitHub's runners and the type check's job have
it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# A HackRF-shaped and an RSPdx-shaped device's fields as the engine sends
# them, decoded by stream.js, then shown by advanced.js and power.js.
SCRIPT = """
const base = new URL(process.argv[1]);
const s = await import(new URL('stream.js', base));
const a = await import(new URL('advanced.js', base));
const p = await import(new URL('power.js', base));
const message = {
  type: 'source', kind: 'soapy', status: 'streaming', detail: '', address: null, tuner: 'RSPdx-R2',
  centre_hz: 145e6, sample_rate_hz: 2e6, gain_db: 30, ppm: 0, dc_removal: true, gains_db: [20, 30, 86],
  range_hz: [1e3, 2e9], settable: ['gain_db', 'stage_gains_db', 'antenna', 'device_settings', 'bias_tee'],
  bias_tee: false,
  gain_stages: [{ name: 'IFGR', gains_db: [20, 40, 59] }, { name: 'RFGR', gains_db: [0, 4, 27] }],
  stage_gains_db: { IFGR: 41, RFGR: 4 },
  antenna: 'Antenna B', antennas: ['Antenna A', 'Antenna B'],
  device_settings: { rfnotch_ctrl: true, agc_setpoint: -30, mode: '2' },
  device_setting_info: [
    { key: 'rfnotch_ctrl', name: '<b>RF Notch</b>', description: 'RF notch filter', type: 'bool', options: [],
      range: null, integer: false },
    { key: 'agc_setpoint', name: 'AGC Setpoint', description: '', type: 'number', options: [],
      range: [-60, 0, 1], integer: true },
    { key: 'mode', name: 'Mode', description: '', type: 'choice', options: [{ value: '2', name: 'Two' }],
      range: null, integer: false },
    { key: 'broken', name: 'Broken', description: '', type: 'number', options: [], range: null, integer: false },
    { key: 'free', name: 'Free', description: '', type: 'text', options: [], range: null, integer: false },
  ],
  device_refusal: 'the device refused HDR Mode: busy',
};
const decoded = s.decodeSource(message);
const view = a.advancedView(decoded);
const setpoint = view.settings[1];
const older = { ...message };
for (const key of ['gain_stages', 'stage_gains_db', 'antenna', 'antennas', 'device_settings', 'device_setting_info',
  'device_refusal']) delete older[key];
const olderDecoded = s.decodeSource(older);
const out = {
  decoded: {
    gainStages: decoded.gainStages, stageGainsDb: decoded.stageGainsDb, antenna: decoded.antenna,
    antennas: decoded.antennas, deviceSettings: decoded.deviceSettings,
    keys: decoded.deviceSettingInfo.map((info) => info.key), refusal: decoded.deviceRefusal,
  },
  malformed: s.decodeDeviceParts({ gain_stages: 'x', stage_gains_db: [1], antenna: 3, antennas: [1],
    device_settings: { a: {}, b: 'x' }, device_setting_info: [{ key: 'k' }], device_refusal: '' }),
  older: olderDecoded && a.advancedView(olderDecoded),
  view,
  other: a.advancedView({ ...decoded, kind: 'rtl_sdr' }),
  none: a.advancedView(null),
  numbers: ['-40.4', '-61', '5', '', 'x', '-0.6'].map((text) => a.settingNumber(text, setpoint)),
  free: a.settingNumber('0.25', { ...setpoint, step: 0, integer: false, min: 0, max: 1 }),
  texts: view.settings.map(a.settingText),
  nearest: [a.nearestIndex([0, 8, 16], 11), a.nearestIndex([0, 8, 16], 13), a.nearestIndex([0, 8, 16], -5)],
  armed: p.powerView(decoded, true).button,
  armedRtl: p.powerView({ ...decoded, kind: 'rtl_sdr' }, true).button,
  automaticGain: [false, true, null, undefined, 'x'].map((value) => {
    const decodedGain = s.decodeSource({ ...message, automatic_gain: value });
    return decodedGain && decodedGain.automaticGain;
  }),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class AdvancedModuleTest(unittest.TestCase):
    """advanced.js, power.js and stream.js's decoding, run in Node.js."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT, WEB_DIR.as_uri() + "/"],
                              capture_output=True, text=True, timeout=60, check=True)
        cls.found = json.loads(done.stdout)

    def test_the_state_decodes_what_is_whole_and_leaves_out_the_rest(self):
        decoded = self.found["decoded"]
        self.assertEqual(decoded["gainStages"], [{"name": "IFGR", "gainsDb": [20, 40, 59]},
                                                 {"name": "RFGR", "gainsDb": [0, 4, 27]}])
        self.assertEqual((decoded["stageGainsDb"], decoded["antenna"], decoded["antennas"]),
                         ({"IFGR": 41, "RFGR": 4}, "Antenna B", ["Antenna A", "Antenna B"]))
        self.assertEqual(decoded["deviceSettings"], {"rfnotch_ctrl": True, "agc_setpoint": -30, "mode": "2"})
        # A number without a range and a type this interface does not know are left out.
        self.assertEqual(decoded["keys"], ["rfnotch_ctrl", "agc_setpoint", "mode"])
        self.assertEqual(decoded["refusal"], "the device refused HDR Mode: busy")
        self.assertEqual(self.found["malformed"], {
            "gainStages": [], "stageGainsDb": {}, "antenna": None, "antennas": [], "deviceSettings": {"b": "x"},
            "deviceSettingInfo": [], "deviceRefusal": None})

    def test_automatic_gain_reads_as_offered_unless_the_engine_says_otherwise(self):
        # False, true, unknown, not sent (an older engine or another source), malformed.
        self.assertEqual(self.found["automaticGain"], [False, True, True, True, None])

    def test_an_engine_without_these_fields_shows_no_device_controls(self):
        older = self.found["older"]
        self.assertFalse(older["shown"] or older["antennaShown"])
        for view in (self.found["other"], self.found["none"]):
            self.assertEqual((view["shown"], view["antennaShown"], view["stages"], view["settings"]),
                             (False, False, [], []))

    def test_the_device_controls(self):
        view = self.found["view"]
        self.assertTrue(view["shown"] and view["antennaShown"])
        self.assertEqual((view["antennas"], view["antenna"]), (["Antenna A", "Antenna B"], "Antenna B"))
        # Each stage's slider at the gain it holds, the nearest offered.
        self.assertEqual([(stage["name"], stage["index"], stage["valueText"]) for stage in view["stages"]],
                         [("IFGR", 1, "40.0 dB"), ("RFGR", 1, "4.0 dB")])
        rfnotch, setpoint, mode = view["settings"]
        self.assertEqual((rfnotch["type"], rfnotch["name"], rfnotch["help"], rfnotch["value"]),
                         ("bool", "<b>RF Notch</b>", "RF notch filter", True))
        self.assertEqual((setpoint["min"], setpoint["max"], setpoint["step"], setpoint["integer"]), (-60, 0, 1, True))
        self.assertEqual((mode["options"], mode["value"]), ([{"value": "2", "name": "Two"}], "2"))
        self.assertEqual(self.found["texts"], ["true", "-30", "2"])
        self.assertEqual(self.found["nearest"], [1, 2, 0])

    def test_a_number_typed_within_the_range_and_on_its_steps(self):
        self.assertEqual(self.found["numbers"], [-40, None, None, None, None, -1])
        self.assertEqual(self.found["free"], 0.25)

    def test_the_antenna_powers_second_press_names_no_voltage_for_a_soapysdr_device(self):
        self.assertEqual(self.found["armed"], "Confirm: power on the antenna socket")
        self.assertEqual(self.found["armedRtl"], "Confirm: 4.5 V on the antenna socket")


if __name__ == "__main__":
    unittest.main()
