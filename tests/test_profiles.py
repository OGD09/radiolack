"""Band profile parsing and validation."""

import copy
from dataclasses import replace
from fractions import Fraction
from importlib import resources
from pathlib import Path
import tempfile
import tomllib
import unittest

from radiolack.profiles import ProfileError, builtin_profile_names, load_profile, parse_profile

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = tomllib.loads((resources.files("radiolack") / "builtin_profiles" / "airband-8k33.toml")
                        .read_text(encoding="utf-8"))


def variant(**changes):
    """Copy of the built-in profile data with ``table__key=value`` changes."""
    data = copy.deepcopy(BUILTIN)
    for name, value in changes.items():
        table, _, key = name.partition("__")
        if value is None:
            del data[table][key]
        else:
            data[table][key] = value
    return data


class ProfileTests(unittest.TestCase):
    def test_builtin_profile_matches_the_prototype(self):
        self.assertIn("airband-8k33", builtin_profile_names())
        profile = load_profile("airband-8k33")
        grid = profile.grid
        self.assertEqual((grid.low_hz, grid.high_hz, grid.origin_hz), (118000000, 137000000, 118000000))
        self.assertEqual((grid.spacing_hz, grid.snap_tolerance_hz), (Fraction(25000, 3), 3000))
        self.assertEqual((profile.mode, profile.filter_hz), ("AM", 8000))
        self.assertEqual((profile.margin_db, profile.confirm_observations), (15.0, 2))
        self.assertEqual((profile.listen_seconds, profile.min_active_seconds, profile.min_observed_seconds,
                          profile.cooldown_seconds), (10.0, 1.0, 5.0, 20.0))
        self.assertEqual((profile.window_seconds, profile.initial_center_hz), (1.0, 136500000))
        self.assertEqual(profile.summary, "118.000-137.000 MHz | AM 8 kHz | grid 25000/3 Hz")

    def test_versioned_example_profile_is_valid(self):
        profile = load_profile(str(ROOT / "profiles" / "example.toml"))
        self.assertTrue(profile.source.endswith("example.toml"))

    def test_profile_file_by_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.toml"
            path.write_text((resources.files("radiolack") / "builtin_profiles" / "airband-8k33.toml")
                            .read_text(encoding="utf-8").replace('"airband-8k33"', '"custom"'))
            self.assertEqual(load_profile(str(path)).name, "custom")
            path.write_text("name = [")
            with self.assertRaisesRegex(ProfileError, "Invalid TOML"):
                load_profile(str(path))
        with self.assertRaisesRegex(ProfileError, "Cannot read"):
            load_profile(str(Path(directory) / "missing.toml"))
        with self.assertRaisesRegex(ProfileError, "Unknown profile"):
            load_profile("no-such-profile")

    def test_schema_errors_are_explicit(self):
        cases = (
            (dict(BUILTIN, extra=1), "Unknown profile keys"),
            ({key: value for key, value in BUILTIN.items() if key != "band"}, r"Missing \[band\]"),
            (variant(detection__margin=15), r"Unknown keys in \[detection\]"),
            (variant(listening__cooldown_seconds=None), r"Missing keys in \[listening\]"),
        )
        for data, message in cases:
            with self.assertRaisesRegex(ProfileError, message):
                parse_profile(data, "test")

    def test_values_are_range_checked(self):
        cases = (
            variant(demodulation__mode="USB"), variant(demodulation__filter_hz=500),
            variant(demodulation__filter_hz=8000.5), variant(detection__confirm_observations=True),
            variant(detection__margin_db=float("nan")), variant(listening__min_active_seconds=10.0),
            variant(listening__min_observed_seconds=11.0), variant(exploration__initial_center_hz=140000000),
            variant(channels__spacing_hz="abc"), variant(channels__spacing_hz="0"),
            variant(channels__snap_tolerance_hz=5000), variant(band__low_hz=137000000),
        )
        for data in cases:
            with self.assertRaises(ProfileError):
                parse_profile(data, "test")

    def test_25_khz_grid_and_overrides_are_validated(self):
        profile = parse_profile(variant(channels__spacing_hz=25000), "test")
        self.assertEqual(len(profile.grid.channels()), 760)
        self.assertEqual(replace(profile, margin_db=10.0).margin_db, 10.0)
        with self.assertRaises(ProfileError):
            replace(profile, margin_db=2.0)


if __name__ == "__main__":
    unittest.main()
