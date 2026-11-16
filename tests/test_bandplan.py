"""Checks of the band plan the interface shows (format in docs/design/band-plan.md)."""

import json
import re
import unittest

from radiolack.server import WEB_DIR

# The interface's demodulators (MODES in main.js).
MODES = {"NFM", "AM", "USB", "LSB", "CW", "WFM"}
COLOUR = re.compile(r"#[0-9a-f]{6}")
REQUIRED_KEYS = {"from", "to", "name", "type"}
BAND_KEYS = REQUIRED_KEYS | {"short", "mode", "step", "segments"}
LOWEST_HZ = 9_000               # where the ITU allocation table starts
HIGHEST_HZ = 6_425_000_000      # above the 6 GHz of the highest receivers in scope
LONGEST_NAME = 40               # names show in the readout's single line
LONGEST_SHORT_NAME = 12         # short names stand in where a band's name does not fit in the band strip
# A step is a range's channel spacing, never a default: broadcast's 9 and
# 5 kHz, CB's 5 kHz, PMR446's 6.25 kHz grid, the airband's 25 kHz / 3, the
# 10 m and 6 m FM channels' 10 kHz, the 12.5 and 25 kHz channels, and FM
# broadcast's and the VOR's 50 kHz. Elsewhere the Automatic step follows the
# zoom (tuning.js).
CHANNEL_SPACINGS = {5_000, 6_250, 25_000 / 3, 9_000, 10_000, 12_500, 25_000, 50_000}
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
                    self.assertIn(entry["step"], CHANNEL_SPACINGS)
                if "segments" in entry:
                    self.assertTrue(entry["segments"])

    def test_short_names_are_shorter_and_on_bands_only(self):
        for band in self.bands:
            for segment in band.get("segments", ()):
                with self.subTest(segment=segment["name"]):
                    self.assertNotIn("short", segment)
            if "short" not in band:
                continue
            short = band["short"]
            with self.subTest(band=band["name"], short=short):
                self.assertIs(type(short), str)
                self.assertTrue(short and short == short.strip() and short.isascii(), short)
                self.assertLess(len(short), len(band["name"]))
                self.assertLessEqual(len(short), LONGEST_SHORT_NAME)

    def test_every_type_is_used(self):
        self.assertEqual({entry["type"] for entry in self.entries}, set(self.plan["types"]))


if __name__ == "__main__":
    unittest.main()
