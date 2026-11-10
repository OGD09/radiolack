"""Checks of the band plan the interface shows (format in docs/design/band-plan.md)."""

import json
import re
import unittest

from radiolack.server import WEB_DIR

# The interface's demodulators (MODES in main.js).
MODES = {"NFM", "AM", "USB", "LSB", "CW"}
COLOUR = re.compile(r"#[0-9a-f]{6}")
REQUIRED_KEYS = {"from", "to", "name", "type"}
BAND_KEYS = REQUIRED_KEYS | {"mode", "step", "segments"}
LOWEST_HZ = 9_000               # where the ITU allocation table starts
HIGHEST_HZ = 6_425_000_000      # above the 6 GHz of the highest receivers in scope
LONGEST_NAME = 40               # names show in the readout's single line
BAND_PLAN_URL = re.compile(r"^const BAND_PLAN_URL = '([a-z0-9-]+\.json)';$", re.MULTILINE)


def load_plan() -> dict:
    """Read the band plan file that main.js loads."""
    name = BAND_PLAN_URL.search((WEB_DIR / "main.js").read_text(encoding="utf-8")).group(1)
    return json.loads((WEB_DIR / name).read_text(encoding="utf-8"))


class BandPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_plan()
        cls.bands = cls.plan["bands"]
        cls.entries = [entry for band in cls.bands for entry in (band, *band.get("segments", ()))]

    def test_header(self):
        self.assertEqual(set(self.plan), {"format", "name", "types", "bands"})
        self.assertEqual(self.plan["format"], 1)
        self.assertTrue(self.plan["name"].strip())
        for name, colour in self.plan["types"].items():
            with self.subTest(type=name):
                self.assertRegex(name, r"^[a-z][a-z-]*$")
                self.assertTrue(COLOUR.fullmatch(colour), colour)

    def test_bands_cover_the_whole_range_in_order(self):
        self.assertEqual(self.bands[0]["from"], LOWEST_HZ)
        self.assertEqual(self.bands[-1]["to"], HIGHEST_HZ)
        for previous, band in zip(self.bands, self.bands[1:]):
            with self.subTest(band=band["name"], from_hz=band["from"]):
                self.assertEqual(band["from"], previous["to"])

    def test_segments_stay_in_order_inside_their_band(self):
        for band in self.bands:
            edge = band["from"]
            for segment in band.get("segments", ()):
                with self.subTest(band=band["name"], segment=segment["name"]):
                    self.assertNotIn("segments", segment)
                    self.assertGreaterEqual(segment["from"], edge)
                    self.assertLessEqual(segment["to"], band["to"])
                    edge = segment["to"]

    def test_entries_are_well_formed(self):
        for entry in self.entries:
            with self.subTest(name=entry.get("name"), from_hz=entry.get("from")):
                self.assertLessEqual(REQUIRED_KEYS, set(entry))
                self.assertLessEqual(set(entry), BAND_KEYS)
                self.assertIs(type(entry["from"]), int)
                self.assertIs(type(entry["to"]), int)
                self.assertLess(entry["from"], entry["to"])
                name = entry["name"]
                self.assertTrue(name and name == name.strip() and name.isascii(), name)
                self.assertLessEqual(len(name), LONGEST_NAME)
                self.assertIn(entry["type"], self.plan["types"])
                if "mode" in entry:
                    self.assertIn(entry["mode"], MODES)
                if "step" in entry:
                    self.assertIn(type(entry["step"]), (int, float))
                    self.assertGreater(entry["step"], 0)
                if "segments" in entry:
                    self.assertTrue(entry["segments"])

    def test_every_type_is_used(self):
        self.assertEqual({entry["type"] for entry in self.entries}, set(self.plan["types"]))


if __name__ == "__main__":
    unittest.main()
