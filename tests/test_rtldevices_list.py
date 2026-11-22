"""Checks of the Receiver list's entries (rtldevices.js) and of the RTL-SDR
receivers message's decoding (stream.js), typed modules without a browser:
Node.js runs a script that imports them from the interface's folder and
prints what they give, as JSON. Skipped where Node.js is not installed;
GitHub's runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# What rtldevices.js gives, case by case: a source state reduced to what it
# reads, and lists as stream.js decodes them.
LIST_SCRIPT = """
const r = await import(process.argv[1]);
const kinds = ['synthetic', 'rtl_sdr', 'rtl_tcp', 'soapy'];
const synthetic = { kind: 'synthetic', kinds };
const rtl = { kind: 'rtl_sdr', kinds };
const device = (serial, more = {}) => ({ device: serial, name: 'NESDR SMArt v5', manufacturer: 'Nooelec', drivers: [],
  shared: false, readsAsIndex: false, inUse: false, found: true, ...more });
const nesdr = device('59196661');
const blog = device('RTLSDR1', { name: 'RTL2838UHIDIR', manufacturer: 'Realtek' });
const listed = (...devices) => ({ listing: 'sysfs', devices });
const entries = (list) => list.options.map((o) => `${o.value}|${o.label}|${o.disabled}`);
const out = {
  // Before the engine answered, or an older engine: the entry that opens the one used last.
  none: entries(r.receiverList(synthetic, null, false, true)),
  noneValue: r.receiverList(rtl, null, false, true).value,
  unavailable: entries(r.receiverList(synthetic, { listing: 'unavailable', devices: [] }, false, true)),
  empty: entries(r.receiverList(synthetic, listed(), false, true)),
  two: entries(r.receiverList(synthetic, listed(blog, nesdr), false, true)),
  twoValue: r.receiverList(synthetic, listed(blog, nesdr), false, true).value,
  inUse: r.receiverList(rtl, listed(blog, { ...nesdr, inUse: true, drivers: ['usbfs'] }), false, true),
  picked: r.receiverList(rtl, listed(blog, { ...nesdr, inUse: true }), true, true).value,
  stale: r.receiverList(rtl, listed(blog, nesdr), false, true).value,
  held: [['usbfs'], ['dvb_usb_rtl28xxu'], ['other_driver'], []].map((drivers) => r.rtlLabel(device('A', { drivers }))),
  notFound: [r.rtlLabel(device('59196661', { found: false, inUse: true, name: '' })),
    r.rtlLabel(device('0', { found: false, inUse: true, name: '', readsAsIndex: true }))],
  // Receivers that rtl_tcp cannot tell by their serial numbers: listed, not chosen, and the fallback.
  untold: r.receiverList(synthetic, listed(device('00000001', { shared: true, readsAsIndex: true }),
    device('00000001', { shared: true, readsAsIndex: true }), device(null)), false, true),
  index: r.receiverList(rtl, listed(blog, nesdr, device('0', { found: false, inUse: true, readsAsIndex: true })),
    false, true),
  closed: entries(r.receiverList(synthetic, listed(nesdr), false, false)),
  onlySynthetic: entries(r.receiverList({ kind: 'synthetic', kinds: ['synthetic'] }, null, false, true)),
  at: ['rtl_sdr:1', 'rtl_sdr:2', 'rtl_sdr:', 'rtl_sdr:x', 'rtl_sdr', 'soapy'].map((v) => r.rtlDeviceAt(listed(blog, nesdr), v)?.device ?? null),
  atNone: r.rtlDeviceAt(null, 'rtl_sdr:0'),
};
console.log(JSON.stringify(out));
"""

# What stream.js decodes of RTL-SDR receivers messages, valid or not.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const device = { device: '59196661', name: 'NESDR SMArt v5', manufacturer: 'Nooelec', drivers: ['usbfs'], shared: false,
  reads_as_index: false, in_use: true, found: true };
const valid = { type: 'rtl_devices', listing: 'sysfs', devices: [device, { ...device, device: null }] };
const out = {
  valid: s.decodeRtlDevices(valid),
  refused: [
    { ...valid, listing: 'usb' }, { ...valid, devices: null }, { ...valid, devices: [{ ...device, device: '' }] },
    { ...valid, devices: [{ ...device, drivers: [1] }] }, { ...valid, devices: [{ ...device, found: 'yes' }] },
    { ...valid, devices: [{ ...device, reads_as_index: undefined }] }, { ...valid, devices: [null] },
  ].map(s.decodeRtlDevices),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class RtlDevicesModuleTest(unittest.TestCase):
    """rtldevices.js and stream.js's decoding, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        """What ``script`` prints, as JSON, given the interface's ``module``
        as its argument, a file URL."""
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_one_entry_per_receiver_plugged_in(self):
        out = self.run_script(LIST_SCRIPT, "rtldevices.js")
        fallback = ["synthetic|Synthetic band (2 m)|false", "rtl_sdr|RTL-SDR · this computer|false",
                    "rtl_tcp|RTL-SDR · rtl_tcp|false", "soapy|SoapySDR device|false"]
        self.assertEqual(out["none"], fallback)
        self.assertEqual(out["noneValue"], "rtl_sdr")
        self.assertEqual(out["unavailable"], fallback)
        self.assertEqual(out["empty"], fallback)
        self.assertEqual(out["two"], [
            "synthetic|Synthetic band (2 m)|false", "rtl_sdr:0|RTL-SDR · RTL2838UHIDIR · RTLSDR1|false",
            "rtl_sdr:1|RTL-SDR · NESDR SMArt v5 · 59196661|false", "rtl_tcp|RTL-SDR · rtl_tcp|false",
            "soapy|SoapySDR device|false"])
        self.assertEqual(out["twoValue"], "synthetic")
        # The receiver in use is selected; Radiolack's own hold on it is not "another program".
        self.assertEqual(out["inUse"]["value"], "rtl_sdr:1")
        self.assertEqual(out["inUse"]["options"][2]["label"], "RTL-SDR · NESDR SMArt v5 · 59196661")
        self.assertEqual(out["inUse"]["note"], "")
        self.assertEqual(out["picked"], "soapy")
        self.assertEqual(out["stale"], "")      # a list from before the switch: asked again
        self.assertEqual(out["held"], ["RTL-SDR · NESDR SMArt v5 · A · in use by another program",
                                       "RTL-SDR · NESDR SMArt v5 · A · held by the DVB driver",
                                       "RTL-SDR · NESDR SMArt v5 · A · held by other_driver",
                                       "RTL-SDR · NESDR SMArt v5 · A"])
        self.assertEqual(out["notFound"], ["RTL-SDR · 59196661 · not found", "RTL-SDR · receiver 0 by its index"])
        self.assertEqual(out["closed"], [entry.replace("|false", "|true") for entry in [
            "synthetic|Synthetic band (2 m)|false", "rtl_sdr:0|RTL-SDR · NESDR SMArt v5 · 59196661|false",
            "rtl_tcp|RTL-SDR · rtl_tcp|false", "soapy|SoapySDR device|false"]])
        self.assertEqual(out["onlySynthetic"], ["synthetic|Synthetic band (2 m)|false",
                                                "rtl_sdr|RTL-SDR · this computer|true",
                                                "rtl_tcp|RTL-SDR · rtl_tcp|true", "soapy|SoapySDR device|true"])
        self.assertEqual(out["at"], ["59196661", None, None, None, None, None])
        self.assertIsNone(out["atNone"])

    def test_receivers_rtl_tcp_cannot_tell_apart_are_listed_but_not_chosen(self):
        out = self.run_script(LIST_SCRIPT, "rtldevices.js")
        untold = out["untold"]
        self.assertEqual([(o["label"], o["disabled"]) for o in untold["options"][1:5]], [
            ("RTL-SDR · this computer", False),
            ("RTL-SDR · NESDR SMArt v5 · 00000001", True), ("RTL-SDR · NESDR SMArt v5 · 00000001", True),
            ("RTL-SDR · NESDR SMArt v5 · no serial number", True)])
        self.assertEqual(untold["note"], (
            "Several receivers share the serial number 00000001: rtl_tcp cannot tell them apart. "
            "Give each receiver its own serial number with rtl_eeprom -s, one plugged in at a time. "
            "A receiver without a serial number opens with --rtl-sdr and its index."))
        index = out["index"]
        self.assertEqual(index["value"], "rtl_sdr:2")
        self.assertEqual(index["options"][3]["label"], "RTL-SDR · receiver 0 by its index")
        self.assertEqual(index["note"],
                         "The receiver in use was opened by its index, which need not follow this list's order.")

    def test_decodes_the_message(self):
        out = self.run_script(DECODE_SCRIPT, "stream.js")
        self.assertEqual(out["valid"], {"listing": "sysfs", "devices": [
            {"device": "59196661", "name": "NESDR SMArt v5", "manufacturer": "Nooelec", "drivers": ["usbfs"],
             "shared": False, "readsAsIndex": False, "inUse": True, "found": True},
            {"device": None, "name": "NESDR SMArt v5", "manufacturer": "Nooelec", "drivers": ["usbfs"],
             "shared": False, "readsAsIndex": False, "inUse": True, "found": True}]})
        self.assertEqual(out["refused"], [None] * 7)


if __name__ == "__main__":
    unittest.main()
