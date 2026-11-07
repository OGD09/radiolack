"""Import of the migrated history, when data/airband-peaks.json is present.

The legacy journal is opened read-only; the import goes to a temporary
store. The test checks that the source bytes and modification time do not
change and that nothing is created next to it.
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from radiolack.stats import frequency_statistics
from radiolack.storage import import_legacy, load_store

SOURCE = Path(__file__).resolve().parents[1] / "data" / "airband-peaks.json"
INITIAL_SHA256 = "df6b04929d85bff5ba524c1fd86448675409de7614579b2f9ab386de19e680cd"


@unittest.skipUnless(SOURCE.is_file(), "the migrated history is not present")
class RealHistoryTests(unittest.TestCase):
    def test_import_is_lossless_idempotent_and_read_only(self):
        before = SOURCE.read_bytes()
        stat_before = SOURCE.stat()
        neighbours = sorted(entry.name for entry in SOURCE.parent.iterdir())
        legacy = json.loads(before)
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "observations.json"
            report = import_legacy(SOURCE, store)
            self.assertEqual(report.source_observations, len(legacy["detections"]))
            self.assertEqual((report.added, report.already_present, report.conflicts),
                             (len(legacy["detections"]), 0, []))
            self.assertEqual(report.legacy_counter_mismatches, {})
            if report.source_sha256 == INITIAL_SHA256:
                self.assertEqual((report.source_observations, report.source_frequencies,
                                  report.source_stations, report.source_enriched), (304, 30, 29, 227))
            data = load_store(store)
            rows = [{key: value for key, value in row.items() if key not in ("id", "origin")}
                    for row in data["observations"]]
            self.assertEqual(rows, legacy["detections"])
            self.assertEqual(data["stations"], legacy.get("stations", {}))
            # Every legacy per-frequency field is recomputed identically from the rows.
            recomputed = frequency_statistics(legacy["detections"])
            for key, stored in legacy.get("frequency_stats", {}).items():
                for name, value in stored.items():
                    self.assertEqual(recomputed[key][name], value, f"{key} {name}")
            written = store.read_bytes()
            again = import_legacy(SOURCE, store)
            self.assertEqual((again.added, again.written), (0, False))
            self.assertEqual(store.read_bytes(), written)
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).digest(), hashlib.sha256(before).digest())
        self.assertEqual(SOURCE.stat().st_mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual(sorted(entry.name for entry in SOURCE.parent.iterdir()), neighbours)


if __name__ == "__main__":
    unittest.main()
