"""Tests for the kept scans (keptscans.py): what a scan keeps, its limits,
its names and the checks of the saved file; and the interface's decoding
of what the engine sends of them (stream.js), run in Node.js where it is
installed."""

import json
import shutil
import subprocess
import time
import unittest

from radiolack import keptscans
from radiolack.keptscans import MAX_CHANNELS, MAX_SCANS, KeptScans, checked_name, default_name
from radiolack.server import WEB_DIR

NODE = shutil.which("node")

START_S = 1_795_000_000.0


def started(scans: KeptScans, name: str | None = "Repeaters", now: float = START_S) -> keptscans.KeptScan:
    return scans.start(name, now, 145_575_000.0, 145_800_000.0, True, 3)


class KeptScansTest(unittest.TestCase):

    def test_keeps_the_channels_found_and_the_time_heard(self):
        scans = KeptScans()
        scan = started(scans)
        self.assertIs(scans.running, scan)
        self.assertTrue(scans.record([(145_600_000, 20.0), (145_725_000, 14.0)], START_S + 1))
        self.assertTrue(scans.record([(145_600_400, 25.0)], START_S + 2))     # within 1.5 kHz: the same channel
        self.assertFalse(scans.record([], START_S + 3))
        scans.listened(145_600_000, 1.5)
        scans.listened(145_900_000, 1.0)        # never found: nothing to add to
        self.assertTrue(scans.progress(1))
        self.assertFalse(scans.progress(1))
        self.assertTrue(scans.end(START_S + 10, "done"))
        self.assertIsNone(scans.running)
        self.assertFalse(scans.end(START_S + 11, "stopped"))
        self.assertEqual(scans.channels(scan.id), {
            "id": 1, "name": "Repeaters", "start_s": round(START_S), "end_s": round(START_S + 10),
            "low_hz": 145_575_000, "high_hz": 145_800_000, "listen": True, "passes_set": 3, "passes": 1,
            "ended": "done", "channels": [
                {"frequency_hz": 145_600_000, "first_s": round(START_S + 1), "last_s": round(START_S + 2),
                 "searches": 2, "margin_db": 25.0, "open_s": 1.5},
                {"frequency_hz": 145_725_000, "first_s": round(START_S + 1), "last_s": round(START_S + 1),
                 "searches": 1, "margin_db": 14.0, "open_s": 0.0}]})
        self.assertEqual(scans.summaries()[0]["channels"], 2)
        self.assertFalse(scans.record([(145_650_000, 20.0)], START_S + 12))     # it ended

    def test_the_latest_first_and_at_most_max_scans(self):
        scans = KeptScans()
        for index in range(MAX_SCANS + 2):
            started(scans, f"Scan {index}", START_S + index)
            scans.end(START_S + index + 0.5, "stopped")
        summaries = scans.summaries()
        self.assertEqual(len(summaries), MAX_SCANS)
        self.assertEqual((summaries[0]["name"], summaries[-1]["name"]), (f"Scan {MAX_SCANS + 1}", "Scan 2"))
        # A new scan ends the one that runs.
        first = started(scans, "First", START_S + 1000)
        started(scans, "Second", START_S + 1001)
        self.assertEqual((first.ended, first.end_s), ("stopped", START_S + 1001))

    def test_at_most_max_channels_the_latest_found(self):
        scans = KeptScans()
        scan = started(scans)
        for index in range(MAX_CHANNELS + 5):
            scans.record([(100_000_000 + 10_000 * index, 10.0)], START_S + index)
        frequencies = [channel.frequency_hz for channel in scan.channels]
        self.assertEqual(len(frequencies), MAX_CHANNELS)
        self.assertEqual(frequencies[0], 100_050_000)       # the five found first left
        self.assertEqual(frequencies, sorted(frequencies))

    def test_rename_delete_and_clear_all(self):
        scans = KeptScans()
        old = started(scans, "Old")
        scans.end(START_S + 5, "stopped")
        running = started(scans, None, START_S + 60)
        self.assertEqual(running.name, default_name(145_575_000.0, 145_800_000.0, START_S + 60))
        scans.rename(running.id, "  2 m repeaters  ")
        self.assertEqual(running.name, "2 m repeaters")
        for scan_id, name in ((old.id, ""), (old.id, None), (old.id, "x" * 61), (old.id, "two\nlines"),
                              (old.id, 3), (99, "Missing"), ("1", "Text id"), (True, "Bool id")):
            with self.subTest(scan_id=scan_id, name=name), self.assertRaises(ValueError):
                scans.rename(scan_id, name)
        self.assertEqual(old.name, "Old")
        with self.assertRaisesRegex(ValueError, "the scan that runs cannot be deleted"):
            scans.delete(running.id)
        scans.delete(old.id)
        with self.assertRaisesRegex(ValueError, "no such kept scan"):
            scans.delete(old.id)
        self.assertFalse(scans.clear_all())     # the one that runs stays
        self.assertEqual([scan["id"] for scan in scans.summaries()], [running.id])
        scans.end(START_S + 70, "tuned")
        self.assertTrue(scans.clear_all())
        self.assertEqual(scans.summaries(), [])
        self.assertEqual(started(scans).id, 1)

    def test_comes_back_from_the_file_and_one_that_ran_is_interrupted(self):
        scans = KeptScans()
        started(scans, "Done")
        scans.record([(145_600_000, 20.0)], START_S + 1)
        scans.end(START_S + 10, "done")
        started(scans, "Cut", START_S + 20)
        scans.record([(145_725_000, 18.0)], START_S + 25)
        again = KeptScans(scans.stored())
        summaries = again.summaries()
        self.assertEqual([(scan["name"], scan["ended"], scan["end_s"]) for scan in summaries],
                         [("Cut", "interrupted", round(START_S + 25)), ("Done", "done", round(START_S + 10))])
        self.assertIsNone(again.running)
        self.assertEqual(again.channels(1), scans.channels(1))

    def test_leaves_out_what_is_not_valid(self):
        good = KeptScans()
        started(good, "Good")
        good.record([(145_600_000, 20.0)], START_S + 1)
        good.end(START_S + 2, "stopped")
        entry = good.stored()["scans"][0]
        bad_channel = dict(entry["channels"][0], searches=0)
        broken = [
            {**entry, "id": 2, "name": "x" * 61}, {**entry, "id": 3, "low_hz": 146e6}, {**entry, "id": 4, "ended": "lost"},
            {**entry, "id": 5, "end_s": None}, {**entry, "id": 6, "passes_set": 0}, {**entry, "id": True},
            {**entry, "id": 7, "listen": "yes"}, {**entry, "id": 8, "channels": None}, "scan",
            {**entry, "id": 1, "name": "Same id"},
        ]
        with self.assertLogs("radiolack.keptscans", "WARNING"):
            scans = KeptScans({"scans": [entry, *broken, {**entry, "id": 9, "channels": [bad_channel]}]})
        self.assertEqual([(scan["id"], scan["channels"]) for scan in scans.summaries()], [(9, 0), (1, 1)])
        for saved in (None, {"scans": "none"}, []):
            with self.subTest(saved=saved):
                if saved is None:
                    self.assertEqual(len(KeptScans(saved)), 0)
                else:
                    with self.assertLogs("radiolack.keptscans", "WARNING"):
                        self.assertEqual(len(KeptScans(saved)), 0)


class NameTest(unittest.TestCase):

    def test_checked_name(self):
        self.assertIsNone(checked_name(None))
        self.assertIsNone(checked_name("   "))
        self.assertEqual(checked_name(" Airband "), "Airband")
        self.assertEqual(checked_name("é" * 60), "é" * 60)
        for value in ("x" * 61, "a\tb", "a b", "a​b", 12, ["Airband"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_name(value)

    def test_default_name(self):
        start = time.mktime((2026, 11, 20, 14, 5, 30, 0, 0, -1))
        self.assertEqual(default_name(430e6, 440e6, start), "430.000-440.000 MHz, 2026-11-20 14:05")


# What stream.js decodes of scans and scan messages, given those the engine sends.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const scans = JSON.parse(process.argv[2]), scan = JSON.parse(process.argv[3]);
const summary = scans.scans[0];
const out = {
  scans: s.decodeScans(scans),
  scan: s.decodeScan(scan),
  refused: [
    s.decodeScans({ type: 'scans' }), s.decodeScans({ type: 'scans', scans: [{ ...summary, id: '1' }] }),
    s.decodeScans({ type: 'scans', scans: [{ ...summary, end_s: 'later' }] }),
    s.decodeScans({ type: 'scans', scans: [{ ...summary, passes_set: 1.5 }] }),
    s.decodeScan({ ...scan, channels: null }), s.decodeScan({ ...scan, channels: [{ ...scan.channels[0], searches: 'two' }] }),
  ],
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class DecodeTest(unittest.TestCase):

    def test_the_interface_decodes_what_the_engine_sends(self):
        scans = KeptScans()
        scan = started(scans)
        scans.record([(145_600_000, 20.0)], START_S + 1)
        scans.listened(145_600_000, 2.0)
        state = {"type": "scans", "scans": scans.summaries()}
        reply = {"type": "scan", **scans.channels(scan.id)}
        done = subprocess.run([NODE, "--input-type=module", "-e", DECODE_SCRIPT, (WEB_DIR / "stream.js").as_uri(),
                               json.dumps(state), json.dumps(reply)], capture_output=True, text=True, timeout=60, check=True)
        found = json.loads(done.stdout)
        summary = {"id": 1, "name": "Repeaters", "startS": round(START_S), "endS": None, "lowHz": 145_575_000,
                   "highHz": 145_800_000, "listen": True, "passesSet": 3, "passes": 0, "ended": None, "channels": 1}
        self.assertEqual(found["scans"], [summary])
        self.assertEqual(found["scan"], {"scan": summary, "channels": [
            {"frequencyHz": 145_600_000, "firstS": round(START_S + 1), "lastS": round(START_S + 1), "searches": 1,
             "marginDb": 20.0, "openS": 2.0}]})
        self.assertEqual(found["refused"], [None] * 6)


if __name__ == "__main__":
    unittest.main()
