"""Checks of the band plans the interface shows (format in docs/design/band-plan.md),
and of what the engine reads of them (bandplan.py): the plans it offers,
the modes one gives, which Node.js checks against the interface's where it
is installed, the channels its signals show on and its WFM de-emphasis."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radiolack import bandplan
from radiolack.bandplan import BandPlan, plan_mode
from radiolack.demod import WFM_DEEMPHASIS_S
from radiolack.server import WEB_DIR

# The modes the band plan may name: the interface's (MODES in main.js), but SAM,
# which counts as AM where the plan names AM.
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
# 12.5 and 25 kHz channels, and FM broadcast's and the VOR's 50 kHz; in the
# Americas, the 1.25 m repeaters' 20 kHz and FM broadcast's 100 kHz, which
# reaches its 200 kHz channels on the odd 100 kHz. A channel set's spacing
# takes its place where the set gives one (CB, PMR446, maritime VHF).
# Elsewhere the Automatic step follows the zoom (tuning.js).
CHANNEL_SPACINGS = {5_000, 25_000 / 3, 9_000, 10_000, 12_500, 20_000, 25_000, 50_000, 100_000}
# The file of the band plan that the engine names, as main.js loads it.
PLAN_URL = "loadBandPlan(`bandplan-${id}.json`, MODE_ORDER)"


def plan_path(plan: str = bandplan.DEFAULT_PLAN) -> Path:
    """A band plan's file, the one in use unless the user chose another by default."""
    return WEB_DIR / f"bandplan-{plan}.json"


def load_plan(plan: str = bandplan.DEFAULT_PLAN) -> dict:
    """Read a band plan's file, the one in use unless the user chose another by default."""
    return json.loads(plan_path(plan).read_text(encoding="utf-8"))


def band_at(bands: list, hz: int) -> dict | None:
    """The band that holds a frequency, or None outside the plan."""
    return next((band for band in bands if band["from"] <= hz < band["to"]), None)


class PlanFileChecks:
    """The structure that every band plan's file keeps: a subclass per plan
    of bandplan.PLANS names it in plan_id."""

    plan_id = ""
    deemphasis_us = 50          # the plan's broadcast FM
    references_hz = {77_500, 10_000_000}     # standard frequencies the plan must mark

    @classmethod
    def setUpClass(cls):
        cls.path = plan_path(cls.plan_id)
        cls.plan = load_plan(cls.plan_id)
        cls.bands = cls.plan["bands"]
        cls.entries = [entry for band in cls.bands for entry in (band, *band.get("segments", ()))]

    def test_header(self):
        self.assertEqual(set(self.plan), {"format", "name", "wfm_deemphasis_us", "types", "bands", "channels",
                                          "spots"})
        self.assertEqual(self.plan["format"], 2)
        self.assertTrue(self.plan["name"].strip())
        self.assertEqual(self.plan["wfm_deemphasis_us"], self.deemphasis_us)
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
        for line in self.path.read_text(encoding="utf-8").splitlines():
            with self.subTest(line=line):
                self.assertLessEqual(line.count('"from"'), 1)
                self.assertLessEqual(line.count('"f"'), 1)

    def test_the_file_stays_small(self):
        self.assertLessEqual(self.path.stat().st_size, LARGEST_FILE)

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
        self.assertLessEqual(self.references_hz, references)

    def assert_name(self, name):
        self.assertIs(type(name), str)
        self.assertTrue(name and name == name.strip() and name.isascii(), name)
        self.assertLessEqual(len(name), LONGEST_NAME)


class EuropePlanTest(PlanFileChecks, unittest.TestCase):
    plan_id = "europe"


class FrancePlanTest(PlanFileChecks, unittest.TestCase):
    plan_id = "france"


class AmericasPlanTest(PlanFileChecks, unittest.TestCase):
    plan_id = "americas"
    deemphasis_us = 75
    # TF.768's stations of Region 2: 60 kHz, 2.5 to 25 MHz, and 3330, 7850 and 14670 kHz.
    references_hz = {60_000, 2_500_000, 3_330_000, 5_000_000, 7_850_000, 10_000_000, 14_670_000, 15_000_000,
                     20_000_000, 25_000_000}

    def names_at(self, hz: int) -> tuple:
        """The band's and the segment's names at a frequency."""
        band = band_at(self.bands, hz)
        segment = next((s for s in band.get("segments", ()) if s["from"] <= hz < s["to"]), {})
        return band["name"], segment.get("name")

    def test_the_references_are_region_2_s_alone(self):
        references = {spot["f"] for spot in self.plan["spots"] if spot.get("reference")}
        self.assertEqual(references, self.references_hz)

    def test_region_2_s_own_allocations(self):
        names = self.names_at
        self.assertEqual(names(1_900_000), ("160 m amateur", "All modes"))         # 1800-2000 kHz
        self.assertEqual(names(3_900_000), ("80 m amateur", "All modes"))          # 3500-4000 kHz
        self.assertEqual(names(7_250_000), ("40 m amateur", "All modes"))          # 7000-7300 kHz
        self.assertEqual(names(7_350_000), ("41 m broadcast", None))               # 7300-7400 kHz
        self.assertEqual(names(1_650_000), ("Medium wave broadcast", None))        # up to 1705 kHz
        self.assertEqual(names(53_000_000), ("6 m amateur", "All modes"))          # 50-54 MHz
        self.assertEqual(names(147_000_000), ("2 m amateur", "Repeater inputs"))   # 144-148 MHz
        self.assertEqual(names(223_000_000), ("1.25 m amateur", "Repeater inputs"))
        self.assertEqual(names(915_000_000), ("33 cm amateur, ISM", "Broadband, amateur TV"))
        self.assertEqual(names(3_350_000_000), ("9 cm amateur", "Radio astronomy (protected)"))
        self.assertEqual(names(5_900_000_000), ("5 cm amateur", "All modes"))      # up to 5925 MHz
        self.assertEqual(names(462_600_000), ("Fixed and mobile", "FRS and GMRS"))
        self.assertEqual(names(162_475_000), ("Fixed and mobile", "Weather radio"))
        self.assertIsNone(band_at(self.bands, 70_200_000).get("segments"))         # no 4 m band

    def test_region_2_s_channels(self):
        sets = {channel_set["name"]: channel_set for channel_set in self.plan["channels"]}
        self.assertEqual(sorted(sets), ["CB radio", "FM broadcast", "FRS and GMRS", "Maritime VHF"])
        fm = sets["FM broadcast"]
        self.assertEqual((fm["first"], fm["count"], fm["spacing"], fm["number"]), (88_100_000, 100, 200_000, 201))
        self.assertEqual(len(sets["FRS and GMRS"]["list"]), 22)
        europe = load_plan("europe")
        for name in ("CB radio", "Maritime VHF"):        # the same tables, read in other sources
            with self.subTest(channel_set=name):
                self.assertIn(sets[name], europe["channels"])
        self.assertEqual(band_at(self.bands, 98_100_000)["step"], 100_000)
        self.assertEqual(band_at(self.bands, 124_500_000)["step"], 25_000)
        self.assertEqual(band_at(self.bands, 1_000_000)["step"], 10_000)


# Where France's national table (TNRBF) differs from Europe's plan, as
# docs/design/band-plan.md lists it: the French plan is Europe's elsewhere.
FRANCE_DIFFERS_HZ = (
    (1_606_500, 1_625_000), (1_850_000, 2_000_000), (5_900_000, 5_950_000), (9_400_000, 9_500_000),
    (11_600_000, 11_650_000), (12_050_000, 12_100_000), (13_360_000, 13_410_000), (13_570_000, 13_600_000),
    (13_800_000, 13_870_000), (15_600_000, 15_800_000), (17_480_000, 17_550_000), (18_900_000, 19_020_000),
    (47_000_000, 50_000_000), (52_000_000, 74_800_000), (138_000_000, 144_000_000),
    (160_600_000, 161_962_500), (174_000_000, 242_950_000), (380_000_000, 399_900_000),
    (694_000_000, 862_000_000), (870_000_000, 874_400_000), (915_000_000, 919_400_000), (1_900_000_000, 1_920_000_000),
    (2_010_000_000, 2_025_000_000), (2_200_000_000, 2_300_000_000), (3_400_000_000, 4_200_000_000),
    (8_400_000_000, 8_500_000_000),
)


class FrancePlanDiffersTest(unittest.TestCase):
    """The French plan: Europe's, but where the TNRBF differs."""

    @classmethod
    def setUpClass(cls):
        cls.europe, cls.france = load_plan("europe"), load_plan("france")

    @staticmethod
    def entry_at(plan: dict, hz: int) -> tuple:
        """What the band strip names at a frequency: the band, its segment, and their mode and step,
        then the band's edges."""
        band = band_at(plan["bands"], hz)
        segment = next((s for s in band.get("segments", ()) if s["from"] <= hz < s["to"]), {})
        return (band["name"], band["type"], band.get("mode"), band.get("step"),
                segment.get("name"), segment.get("type"), segment.get("mode"), segment.get("step"),
                band["from"], band["to"])

    def names_at(self, hz: int) -> tuple:
        """The French plan's band and segment names at a frequency."""
        entry = self.entry_at(self.france, hz)
        return entry[0], entry[4]

    def test_the_french_plan_is_europe_s_outside_the_differences(self):
        edges = set()
        for plan in (self.europe, self.france):
            for band in plan["bands"]:
                for entry in (band, *band.get("segments", ())):
                    edges.update((entry["from"], entry["to"] - 1, (entry["from"] + entry["to"]) // 2))
        outside = sorted(f for f in edges if 0 <= f < HIGHEST_HZ
                         and not any(low <= f < high for low, high in FRANCE_DIFFERS_HZ))
        differ = [f for f in outside if self.entry_at(self.europe, f) != self.entry_at(self.france, f)]
        self.assertEqual(differ, [])
        self.assertGreater(len(outside), 900)
        inside = [low for low, _ in FRANCE_DIFFERS_HZ]
        self.assertTrue(all(self.entry_at(self.europe, f) != self.entry_at(self.france, f) for f in inside))
        self.assertEqual(self.france["channels"][:6], self.europe["channels"][:6])
        self.assertEqual(self.france["channels"][7:], self.europe["channels"][7:])
        self.assertEqual([spot for spot in self.europe["spots"] if spot not in self.france["spots"]],
                         [spot for spot in self.europe["spots"]
                          if 69_900_000 <= spot["f"] < 70_500_000 or 3_400_000_000 <= spot["f"] < 3_410_000_000])
        self.assertEqual([spot for spot in self.france["spots"] if spot not in self.europe["spots"]], [])

    def test_france_s_amateur_bands(self):
        names = self.names_at
        self.assertEqual(names(1_845_000), ("160 m amateur", "All modes"))
        self.assertEqual(names(1_900_000), ("160 m amateur", "All modes, outside France"))
        self.assertEqual(names(70_200_000), ("Mobile", None))                       # no 4 m band
        self.assertEqual(names(3_405_000_000), ("Mobile networks (3.5 GHz)", None))  # no 9 cm band
        for hz in (5_355_000, 50_150_000, 144_300_000, 435_000_000, 1_296_200_000, 2_320_200_000,
                   5_760_000_000, 10_368_200_000):
            with self.subTest(hz=hz):
                self.assertEqual(band_at(self.france["bands"], hz), band_at(self.europe["bands"], hz))

    def test_france_s_own_ranges(self):
        dab = next(cs for cs in self.france["channels"] if cs["name"] == "DAB (Band III)")
        self.assertEqual((dab["to"], dab["list"][-1]["n"]), (225_000_000, "12A"))     # Band III ends at 225 MHz
        self.assertEqual(self.names_at(227_360_000), ("Military (UHF)", None))
        self.assertEqual(self.names_at(5_920_000), ("49 m broadcast", "Outside France"))
        self.assertEqual(self.names_at(6_000_000), ("49 m broadcast", None))
        for hz in (381_000_000, 391_000_000, 700_000_000, 734_000_000, 755_000_000, 790_500_000):
            with self.subTest(hz=hz):
                self.assertEqual(self.names_at(hz), ("Emergency services (PPDR)", None))
        self.assertEqual(self.entry_at(self.france, 385_000_000)[:4], self.entry_at(self.europe, 370_000_000)[:4])
        self.assertEqual(self.france["wfm_deemphasis_us"], 50)


class EveryPlanTest(unittest.TestCase):
    def test_every_plan_offered_is_checked(self):
        self.assertEqual(sorted(checks.plan_id for checks in PlanFileChecks.__subclasses__()),
                         sorted(bandplan.PLANS))


NODE = shutil.which("node")

# The mode the interface's History gives each frequency of its first
# argument, a JSON list, for the plan of its second, a file URL: as its
# Bookmark button does.
INTERFACE_MODES_SCRIPT = """
const { readFileSync } = await import('node:fs');
const { parseBandPlan, rangeAt } = await import(new URL('bandplan.js', process.argv[2]));
const { modeAt } = await import(new URL('channels.js', process.argv[2]));
const plan = parseBandPlan(JSON.parse(readFileSync(new URL(process.argv[2]), 'utf8')),
  ['NFM', 'AM', 'SAM', 'USB', 'LSB', 'CW', 'WFM']);
const modes = JSON.parse(process.argv[1]).map((f) => modeAt(plan, f) ?? rangeAt(plan.ranges, f)?.mode ?? null);
console.log(JSON.stringify(modes));
"""

# What stream.js decodes of the band plan message and of the hello's band plans.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
console.log(JSON.stringify({
  plans: [{ type: 'band_plan', plan: 'europe' }, {}, { plan: '' }, { plan: 2 }].map(s.decodeBandPlan),
  offered: s.decodeBandPlans([{ id: 'europe', name: 'Europe (ITU Region 1)' }, { id: '', name: 'x' },
    { id: 'test' }, null, 'europe', { id: 'test', name: 'Test', extra: true }]),
  older: [undefined, 'europe', {}].map(s.decodeBandPlans),
}));
"""

# A small plan: a band whose segments leave parts out, a band with a step,
# a channel set and spots.
SMALL_PLAN = {
    "format": 2, "name": "Test", "wfm_deemphasis_us": 75, "types": {"amateur": "#123456"},
    "bands": [
        {"from": 100_000_000, "to": 101_000_000, "name": "Band", "type": "amateur", "mode": "NFM", "step": 12_500,
         "segments": [
             {"from": 100_100_000, "to": 100_200_000, "name": "SSB", "type": "amateur", "mode": "USB"},
             {"from": 100_500_000, "to": 100_600_000, "name": "No mode", "type": "amateur"}]},
        {"from": 118_000_000, "to": 137_000_000, "name": "Airband", "type": "amateur", "mode": "AM",
         "step": 25_000 / 3},
        {"from": 156_000_000, "to": 157_000_000, "name": "Marine", "type": "amateur"},
    ],
    "channels": [
        {"name": "Marine VHF", "from": 156_000_000, "to": 157_000_000, "label": "Ch {n}", "spacing": 25_000,
         "mode": "NFM", "first": 156_025_000, "count": 3, "number": 1},
        {"name": "Listed", "from": 100_150_000, "to": 100_160_000, "label": "L{n}", "spacing": 5_000, "mode": "NFM",
         "list": [{"n": "2", "f": 100_155_000}, {"n": "1", "f": 100_150_000}]},
    ],
    "spots": [
        {"f": 100_300_000, "name": "Image modes", "type": "amateur", "mode": "USB"},
        {"f": 100_152_000, "name": "Beacon", "type": "amateur"},
    ],
}


class AsiaPacificPlanTest(PlanFileChecks, unittest.TestCase):
    plan_id = "asia-pacific"
    # TF.768's stations of Region 3: 40 and 60 kHz, 2.5, 5, 10 and 15 MHz.
    references_hz = {40_000, 60_000, 2_500_000, 5_000_000, 10_000_000, 15_000_000}

    def names_at(self, hz: int) -> tuple:
        """The band's and the segment's names at a frequency."""
        band = band_at(self.bands, hz)
        segment = next((s for s in band.get("segments", ()) if s["from"] <= hz < s["to"]), {})
        return band["name"], segment.get("name")

    def test_the_references_are_region_3_s_alone(self):
        references = {spot["f"] for spot in self.plan["spots"] if spot.get("reference")}
        self.assertEqual(references, self.references_hz)

    def test_region_3_s_own_allocations(self):
        names = self.names_at
        self.assertEqual(names(1_900_000), ("160 m amateur", "All modes"))         # 1800-2000 kHz
        self.assertEqual(names(3_850_000), ("80 m amateur", "CW, phone and digital"))   # 3500-3900 kHz
        self.assertEqual(names(3_920_000), ("75 m broadcast", None))
        self.assertEqual(names(7_150_000), ("40 m amateur", "All modes"))          # 7000-7200 kHz
        self.assertEqual(names(7_250_000), ("41 m broadcast", None))
        self.assertEqual(names(1_000_000), ("Medium wave broadcast", None))
        self.assertEqual(names(53_000_000), ("6 m amateur", "All modes, wide band"))   # 50-54 MHz
        self.assertEqual(names(147_000_000), ("2 m amateur", "All modes"))         # 144-148 MHz
        self.assertEqual(names(3_350_000_000), ("9 cm amateur", "Radio astronomy (protected)"))
        self.assertEqual(names(5_840_000_000), ("5 cm amateur", "Satellite downlinks"))
        self.assertIsNone(band_at(self.bands, 5_900_000_000).get("short"))         # no amateur band above 5850 MHz
        self.assertIsNone(band_at(self.bands, 223_000_000).get("short"))           # no 1.25 m band
        self.assertIsNone(band_at(self.bands, 915_000_000).get("short"))           # no 33 cm band
        self.assertIsNone(band_at(self.bands, 70_200_000).get("segments"))         # no 4 m band

    def test_region_3_s_channels_and_steps(self):
        self.assertEqual([channel_set["name"] for channel_set in self.plan["channels"]], ["Maritime VHF"])
        self.assertIn(self.plan["channels"][0], load_plan("europe")["channels"])
        self.assertEqual(band_at(self.bands, 98_100_000)["step"], 50_000)
        self.assertEqual(band_at(self.bands, 124_500_000)["step"], 25_000)
        self.assertEqual(band_at(self.bands, 1_000_000)["step"], 9_000)


class EnginePlanTest(unittest.TestCase):
    """What the engine reads of the band plan."""

    def test_the_engine_offers_the_plans_that_main_js_loads(self):
        self.assertIn(PLAN_URL, (WEB_DIR / "main.js").read_text(encoding="utf-8"))
        files = sorted(path.name for path in WEB_DIR.glob("bandplan-*.json"))
        self.assertEqual(files, sorted(f"bandplan-{plan}.json" for plan in bandplan.PLANS))
        self.assertIn(bandplan.DEFAULT_PLAN, bandplan.PLANS)
        self.assertEqual(bandplan.plan_path(bandplan.DEFAULT_PLAN), plan_path())
        self.assertEqual(bandplan.plan_choices(), [{"id": "europe", "name": "Europe (ITU Region 1)"},
                                                   {"id": "france", "name": "France (metropolitan)"},
                                                   {"id": "americas", "name": "Americas (ITU Region 2)"},
                                                   {"id": "asia-pacific", "name": "Asia-Pacific (ITU Region 3)"}])
        for plan in bandplan.PLANS:
            self.assertEqual(bandplan.checked_plan(plan), plan)
        for value in ("", "europa", "../europe", None, 1, ["europe"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bandplan.checked_plan(value)
        with self.assertRaises(ValueError):
            bandplan.load_plan("antarctica")

    def test_the_engine_reads_the_plan_in_use_unless_another_is_chosen(self):
        plan = bandplan.default_plan()
        self.assertIs(plan, bandplan.load_plan("europe"))
        self.assertEqual(plan.name, "Europe (ITU Region 1)")
        self.assertAlmostEqual(plan.wfm_deemphasis_s, 50e-6)
        self.assertEqual(plan.range_at(145_600_000).mode, "NFM")
        self.assertEqual(plan.range_at(144_300_000).mode, "USB")
        self.assertEqual(plan.mode_at(156_800_000), "NFM")      # maritime VHF's channel 16
        self.assertEqual(plan.mode_at(98_100_000), "WFM")
        self.assertEqual(plan.mode_at(124_500_000), "AM")

    def test_the_americas_plan_gives_wfm_its_75_us(self):
        plan = bandplan.load_plan("americas")
        self.assertEqual(plan.name, "Americas (ITU Region 2)")
        self.assertAlmostEqual(plan.wfm_deemphasis_s, 75e-6)
        self.assertEqual(plan.mode_at(98_100_000), "WFM")
        self.assertEqual(plan.channel(98_102_000, 150_000), 98_100_000)     # an FM station on its channel
        self.assertEqual(plan.mode_at(162_550_000), "NFM")                  # weather radio
        self.assertEqual(plan.mode_at(156_800_000), "NFM")

    def test_the_asia_pacific_plan_gives_wfm_its_50_us(self):
        plan = bandplan.load_plan("asia-pacific")
        self.assertEqual(plan.name, "Asia-Pacific (ITU Region 3)")
        self.assertAlmostEqual(plan.wfm_deemphasis_s, 50e-6)
        self.assertEqual(plan.mode_at(87_300_000), "WFM")                   # Region 3's FM from 87 MHz
        self.assertEqual(plan.mode_at(124_500_000), "AM")
        self.assertEqual(plan.mode_at(156_800_000), "NFM")

    def test_a_plan_names_itself_and_its_wfm_de_emphasis(self):
        plan = BandPlan.parse(SMALL_PLAN)
        self.assertEqual(plan.name, "Test")
        self.assertAlmostEqual(plan.wfm_deemphasis_s, 75e-6)
        self.assertAlmostEqual(BandPlan.parse({**SMALL_PLAN, "wfm_deemphasis_us": 50}).wfm_deemphasis_s, 50e-6)
        empty = BandPlan()
        self.assertEqual(empty.name, "")
        self.assertEqual(empty.wfm_deemphasis_s, WFM_DEEMPHASIS_S)

    def test_ranges_take_their_own_mode_and_step_and_gaps_the_band_s(self):
        plan = BandPlan.parse(SMALL_PLAN)
        cases = [(100_000_000, "NFM", 12_500), (100_150_000, "USB", None), (100_300_000, "NFM", 12_500),
                 (100_550_000, None, None), (100_999_999, "NFM", 12_500), (156_500_000, None, None)]
        for frequency, mode, step in cases:
            with self.subTest(frequency=frequency):
                found = plan.range_at(frequency)
                self.assertEqual((found.mode, found.step_hz), (mode, step))
        for frequency in (99_999_999, 101_000_000, 0, 200_000_000):
            self.assertIsNone(plan.range_at(frequency))

    def test_a_spot_s_mode_comes_first_then_a_channel_set_s_then_the_range_s(self):
        plan = BandPlan.parse(SMALL_PLAN)
        cases = [
            (100_300_050, "USB"),       # on the image modes' spot, in an NFM part
            (100_300_051, "NFM"),       # beyond its tolerance
            (100_152_000, "USB"),       # a spot without a mode: the range's, not the channel set's
            (100_150_000, "NFM"),       # the listed set's channel, in the SSB segment
            (100_157_400, "NFM"),       # within half the spacing of its channel 2
            (156_037_500, "NFM"),       # between the marine channels 1 and 2, within half the spacing
            (156_087_501, None),        # more than half the spacing above its last channel: the range names none
            (100_550_000, None),
            (50_000_000, None),         # outside the plan
        ]
        for frequency, mode in cases:
            with self.subTest(frequency=frequency):
                self.assertEqual(plan.mode_at(frequency), mode)

    def test_signals_show_on_their_channel_where_the_step_is_wide(self):
        plan = BandPlan.parse(SMALL_PLAN)
        cases = [
            (100_012_000, 600, 100_012_500),        # within 1.5 kHz of a 12.5 kHz channel
            (100_010_900, 600, 100_010_900),        # beyond: to the 100 Hz
            (100_010_900, 4_000, 100_012_500),      # but a wide one on the nearest channel
            (100_006_250, 600, 100_006_300),        # halfway between two channels, too far from either
            (100_006_250, 5_000, 100_012_500),      # a wide one: the upper one, as Math.round has it
            (118_008_040, 600, 118_008_000),        # the airband's 8.33 kHz step is too narrow: to the 100 Hz
            (100_150_049, 600, 100_150_000),        # no step: to the 100 Hz
            (100_150_050, 600, 100_150_100),        # halves upwards
            (50_000_049, 600, 50_000_000),          # outside the plan
        ]
        for frequency, width, shown in cases:
            with self.subTest(frequency=frequency, width=width):
                self.assertEqual(plan.channel(frequency, width), shown)
                self.assertIsInstance(plan.channel(frequency, width), int)

    def test_every_signal_of_a_wfm_range_shows_on_its_nearest_channel(self):
        plan = bandplan.load_plan("europe")
        cases = [
            (100_196_200, 3_200, 100_200_000),      # a station's narrow detection, 3.8 kHz off its channel
            (100_224_900, 600, 100_200_000),        # within half the 50 kHz step
            (100_225_100, 600, 100_250_000),        # beyond it: the next channel
            (145_506_000, 600, 145_506_000),        # an NFM range keeps its reach
        ]
        for frequency, width, shown in cases:
            with self.subTest(frequency=frequency, width=width):
                self.assertEqual(plan.range_at(frequency).mode, "WFM" if frequency < 108e6 else "NFM")
                self.assertEqual(plan.channel(frequency, width), shown)

    def test_the_vfo_s_mode_where_the_plan_names_one(self):
        self.assertEqual(plan_mode(None, "USB"), "USB")
        self.assertEqual(plan_mode("NFM", "USB"), "NFM")
        self.assertEqual(plan_mode("AM", "SAM"), "SAM")
        self.assertEqual(plan_mode("AM", "NFM"), "AM")
        self.assertEqual(plan_mode("USB", "SAM"), "USB")

    def test_a_malformed_plan_is_refused(self):
        def changed(**changes) -> dict:
            data = json.loads(json.dumps(SMALL_PLAN))
            for path, value in changes.items():
                *parents, last = path.split("__")
                item = data
                for key in parents:
                    item = item[int(key)] if isinstance(item, list) else item[key]
                item[last] = value
            return data

        without = {key: value for key, value in SMALL_PLAN.items() if key != "wfm_deemphasis_us"}
        for data in ([], {**SMALL_PLAN, "format": 1}, without, {**SMALL_PLAN, "wfm_deemphasis_us": 60},
                     {**SMALL_PLAN, "wfm_deemphasis_us": "50"}, {**SMALL_PLAN, "wfm_deemphasis_us": True},
                     {**SMALL_PLAN, "name": " "}, changed(bands="none"), changed(bands__0__mode="FM"),
                     changed(bands__1__from=99_000_000), changed(bands__0__segments__1__from=100_150_000),
                     changed(bands__0__step=0), changed(bands__0__to=100_000_000),
                     changed(channels__0__count=0), changed(channels__0__first=155_000_000),
                     changed(channels__1__from=156_100_000, channels__1__to=156_200_000,    # overlapping
                             channels__1__list__0__f=156_155_000, channels__1__list__1__f=156_150_000),
                     changed(spots__0__f=-1), changed(spots__0__mode="DIGI"), changed(bands__2__name="")):
            with self.subTest(data=str(data)[:80]), self.assertRaises(ValueError):
                BandPlan.parse(data)

    def test_an_unreadable_plan_gives_no_mode_and_no_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "broken"
            broken.mkdir()
            (broken / "bandplan-europe.json").write_text("{", encoding="utf-8")
            for web in (broken, Path(folder) / "missing"):
                with self.subTest(folder=web.name), mock.patch.object(bandplan, "WEB_DIR", web):
                    bandplan.load_plan.cache_clear()
                    try:
                        with self.assertLogs("radiolack.bandplan", "WARNING"):
                            plan = bandplan.default_plan()
                        choices = bandplan.plan_choices()
                    finally:
                        bandplan.load_plan.cache_clear()
                    self.assertIsNone(plan.mode_at(145_600_000))
                    self.assertEqual(plan.channel(145_600_049, 10_000), 145_600_000)
                    self.assertEqual(plan.wfm_deemphasis_s, WFM_DEEMPHASIS_S)
                    self.assertEqual(choices, [{"id": plan, "name": plan} for plan in bandplan.PLANS])

    @unittest.skipUnless(NODE, "Node.js runs the interface's modules")
    def test_the_interface_decodes_the_plan_in_use_and_the_plans_offered(self):
        done = subprocess.run([NODE, "--input-type=module", "-e", DECODE_SCRIPT, (WEB_DIR / "stream.js").as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        found = json.loads(done.stdout)
        self.assertEqual(found["plans"], ["europe", None, None, None])
        self.assertEqual(found["offered"], [{"id": "europe", "name": "Europe (ITU Region 1)"},
                                            {"id": "test", "name": "Test"}])
        self.assertEqual(found["older"], [[], [], []])

    @unittest.skipUnless(NODE, "Node.js runs the interface's modules")
    def test_the_engine_gives_the_modes_that_the_interface_gives(self):
        for plan_id in bandplan.PLANS:
            with self.subTest(plan=plan_id):
                self.assert_same_modes(plan_id)

    def assert_same_modes(self, plan_id):
        data = load_plan(plan_id)
        frequencies = set()
        for entry in [entry for band in data["bands"] for entry in (band, *band.get("segments", ()))]:
            frequencies.update((entry["from"], entry["to"] - 1, (entry["from"] + entry["to"]) // 2))
        for spot in data["spots"]:
            frequencies.update(spot["f"] + offset for offset in (-51, -50, 0, 50, 51))
        plan = BandPlan.parse(data)
        for channel_set in plan._sets:
            half = channel_set.spacing_hz // 2
            for channel in channel_set.channels[:3] + channel_set.channels[-3:]:
                frequencies.update(channel + offset for offset in (-half - 1, -half, 0, half, half + 1))
        frequencies = sorted(f for f in frequencies if f >= 0)
        done = subprocess.run([NODE, "--input-type=module", "-e", INTERFACE_MODES_SCRIPT, json.dumps(frequencies),
                               plan_path(plan_id).as_uri()], capture_output=True, text=True, timeout=60, check=True)
        interface = json.loads(done.stdout)
        engine = [plan.mode_at(f) for f in frequencies]
        differ = [(f, mine, theirs) for f, mine, theirs in zip(frequencies, engine, interface) if mine != theirs]
        self.assertEqual(differ, [])
        self.assertGreater(len(frequencies), 1000)


if __name__ == "__main__":
    unittest.main()
