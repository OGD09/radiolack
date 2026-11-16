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
CHANNEL_SET_KEYS = {"name", "from", "to", "label", "spacing", "mode", "first", "count", "number", "list"}
CHANNEL_KEYS = {"n", "f", "note"}
SPOT_KEYS = {"f", "name", "type", "mode", "reference"}
LOWEST_HZ = 0
HIGHEST_HZ = 10_500_000_000     # keeps the 3 cm amateur band whole
ALLOCATION_TABLE_HZ = 8_300     # where the ITU allocation table starts: "Not allocated" below
LONGEST_NAME = 40               # names show in the readout's single line
LARGEST_FILE = 150_000          # bytes: the interface loads the whole plan at start
LONGEST_SHORT_NAME = 12         # short names stand in where a band's name does not fit in the band strip
MOST_CHANNEL_SETS = 60          # coverage-and-precision.md, pull request 12
MOST_SPOTS = 400
SPOT_TOLERANCE_HZ = 50          # a frequency within this of a spot is on it (channels.js)
# A step is a range's channel spacing, never a default: broadcast's 9 and
# 5 kHz, the airband's 25 kHz / 3, the 10 m and 6 m FM channels' 10 kHz, the
# 12.5 and 25 kHz channels, and FM broadcast's and the VOR's 50 kHz. A channel
# set's spacing takes its place where the set gives one (CB, PMR446, maritime
# VHF). Elsewhere the Automatic step follows the zoom (tuning.js).
CHANNEL_SPACINGS = {5_000, 25_000 / 3, 9_000, 10_000, 12_500, 25_000, 50_000}
BAND_PLAN_URL = re.compile(r"^const BAND_PLAN_URL = '([a-z0-9-]+\.json)';$", re.MULTILINE)


def plan_path():
    """The band plan file that main.js loads."""
    return WEB_DIR / BAND_PLAN_URL.search((WEB_DIR / "main.js").read_text(encoding="utf-8")).group(1)


def load_plan() -> dict:
    """Read the band plan file that main.js loads."""
    return json.loads(plan_path().read_text(encoding="utf-8"))


def band_at(bands: list, hz: int) -> dict | None:
    """The band that holds a frequency, or None outside the plan."""
    return next((band for band in bands if band["from"] <= hz < band["to"]), None)


class BandPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_plan()
        cls.bands = cls.plan["bands"]
        cls.entries = [entry for band in cls.bands for entry in (band, *band.get("segments", ()))]

    def test_header(self):
        self.assertEqual(set(self.plan), {"format", "name", "types", "bands", "channels", "spots"})
        self.assertEqual(self.plan["format"], 2)
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

    def test_nothing_is_allocated_below_the_table(self):
        self.assertEqual((self.bands[0]["to"], self.bands[0]["name"]), (ALLOCATION_TABLE_HZ, "Not allocated"))
        self.assertNotIn("Not allocated", [band["name"] for band in self.bands[1:]])

    def test_the_3_cm_band_is_whole_with_its_satellite_segment(self):
        band = band_at(self.bands, 10_400_000_000)
        self.assertEqual((band["from"], band["to"], band["type"]), (10_000_000_000, HIGHEST_HZ, "amateur"))
        satellites = band["segments"][-1]
        self.assertEqual((satellites["from"], satellites["to"], satellites["type"]),
                         (10_450_000_000, HIGHEST_HZ, "satellite"))

    def test_one_entry_per_line(self):
        # Bands and segments are written one per line, which keeps a change's
        # diff to the entries it touches.
        for line in plan_path().read_text(encoding="utf-8").splitlines():
            with self.subTest(line=line):
                self.assertLessEqual(line.count('"from"'), 1)
                self.assertLessEqual(line.count('"f"'), 1)

    def test_the_file_stays_small(self):
        self.assertLessEqual(plan_path().stat().st_size, LARGEST_FILE)

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
                self.assert_name(entry["name"])
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
        used = {entry["type"] for entry in self.entries} | {spot["type"] for spot in self.plan["spots"]}
        self.assertEqual(used, set(self.plan["types"]))

    def test_channel_sets_are_well_formed(self):
        sets = sorted(self.plan["channels"], key=lambda channel_set: channel_set["from"])
        self.assertLessEqual(len(sets), MOST_CHANNEL_SETS)
        for previous, channel_set in zip(sets, sets[1:]):
            with self.subTest(channel_set=channel_set["name"]):
                self.assertGreaterEqual(channel_set["from"], previous["to"])     # sets do not overlap
        for channel_set in sets:
            with self.subTest(channel_set=channel_set.get("name")):
                self.assertLessEqual({"name", "from", "to", "label", "spacing"}, set(channel_set))
                self.assertLessEqual(set(channel_set), CHANNEL_SET_KEYS)
                self.assert_name(channel_set["name"])
                for key in ("from", "to", "spacing"):
                    self.assertIs(type(channel_set[key]), int)
                self.assertLess(channel_set["from"], channel_set["to"])
                self.assertGreater(channel_set["spacing"], 0)
                # Inside the plan, over one band or several: maritime VHF spans four.
                self.assertGreaterEqual(channel_set["from"], LOWEST_HZ)
                self.assertLessEqual(channel_set["to"], HIGHEST_HZ)
                self.assertIn("{n}", channel_set["label"])
                if "mode" in channel_set:
                    self.assertIn(channel_set["mode"], MODES)
                regular = {"first", "count", "number"}
                if "list" in channel_set:
                    self.assertFalse(regular & set(channel_set))
                    self.assertTrue(channel_set["list"])
                    centres = []
                    for channel in channel_set["list"]:
                        self.assertLessEqual({"n", "f"}, set(channel))
                        self.assertLessEqual(set(channel), CHANNEL_KEYS)
                        self.assertTrue(channel["n"].strip() and channel["n"].isascii())
                        self.assertIs(type(channel["f"]), int)
                        if "note" in channel:
                            self.assert_name(channel["note"])
                        centres.append(channel["f"])
                else:
                    self.assertLessEqual(regular, set(channel_set))
                    for key in regular:
                        self.assertIs(type(channel_set[key]), int)
                    self.assertGreater(channel_set["count"], 0)
                    centres = [channel_set["first"] + i * channel_set["spacing"] for i in range(channel_set["count"])]
                for centre in centres:
                    self.assertGreaterEqual(centre, channel_set["from"])
                    self.assertLess(centre, channel_set["to"])
                # A frequency is on a channel within half the spacing: channels lie at least a spacing apart.
                centres.sort()
                for lower, upper in zip(centres, centres[1:]):
                    self.assertGreaterEqual(upper - lower, channel_set["spacing"], (lower, upper))

    def test_a_set_spanning_several_bands_holds(self):
        maritime = next(channel_set for channel_set in self.plan["channels"] if channel_set["name"] == "Maritime VHF")
        spanned = [band["name"] for band in self.bands if band["to"] > maritime["from"] and band["from"] < maritime["to"]]
        self.assertGreater(len(spanned), 1)
        channels = {channel["n"]: channel["f"] for channel in maritime["list"] if channel.get("note") != "Coast station"}
        self.assertEqual(channels["16"], 156_800_000)       # RR Appendix 18
        self.assertEqual(channels["AIS 2"], 162_025_000)

    def test_spots_are_well_formed(self):
        self.assertLessEqual(len(self.plan["spots"]), MOST_SPOTS)
        frequencies = sorted(spot["f"] for spot in self.plan["spots"])
        for lower, upper in zip(frequencies, frequencies[1:]):
            self.assertGreater(upper - lower, 2 * SPOT_TOLERANCE_HZ, (lower, upper))     # one spot at a time
        for spot in self.plan["spots"]:
            with self.subTest(spot=spot.get("name"), f=spot.get("f")):
                self.assertLessEqual({"f", "name", "type"}, set(spot))
                self.assertLessEqual(set(spot), SPOT_KEYS)
                self.assertIs(type(spot["f"]), int)
                self.assertIsNotNone(band_at(self.bands, spot["f"]))
                self.assert_name(spot["name"])
                self.assertIn(spot["type"], self.plan["types"])
                if "mode" in spot:
                    self.assertIn(spot["mode"], MODES)
                if "reference" in spot:
                    self.assertIs(spot["reference"], True)
                    self.assertEqual(spot["type"], "science")

    def test_spots_do_not_repeat_a_channel(self):
        # The line under the frequency names a spot before a channel: a spot on
        # a channel would hide the channel's name.
        for spot in self.plan["spots"]:
            for channel_set in self.plan["channels"]:
                if not channel_set["from"] <= spot["f"] < channel_set["to"]:
                    continue
                if "list" in channel_set:
                    centres = [channel["f"] for channel in channel_set["list"]]
                else:
                    centres = [channel_set["first"] + i * channel_set["spacing"] for i in range(channel_set["count"])]
                with self.subTest(spot=spot["name"], f=spot["f"]):
                    self.assertGreater(min(abs(centre - spot["f"]) for centre in centres), channel_set["spacing"] / 2)

    def test_the_standard_frequencies_are_references(self):
        references = {spot["f"] for spot in self.plan["spots"] if spot.get("reference")}
        self.assertLessEqual({77_500, 10_000_000}, references)

    def assert_name(self, name):
        self.assertIs(type(name), str)
        self.assertTrue(name and name == name.strip() and name.isascii(), name)
        self.assertLessEqual(len(name), LONGEST_NAME)


if __name__ == "__main__":
    unittest.main()
