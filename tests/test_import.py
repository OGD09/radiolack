"""Legacy journal import: lossless, idempotent, and the source is only read."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from radiolack.errors import StorageError, StoreFormatError
from radiolack.storage import ObservationStore, import_legacy, load_store

from tests.helpers import legacy_journal


def clock(day=7):
    return lambda: f"2026-11-{day:02d}T12:00:00+00:00"


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.source = root / "airband-peaks.json"
        self.store = root / "work" / "observations.json"
        self.write_source(legacy_journal())

    def tearDown(self):
        self.directory.cleanup()

    def write_source(self, data):
        self.source.write_text(json.dumps(data, indent=2))
        self.source_bytes = self.source.read_bytes()

    def test_every_row_is_kept_unchanged_with_its_own_era_parameters(self):
        report = import_legacy(self.source, self.store, clock_utc=clock())
        self.assertEqual((report.added, report.already_present, report.conflicts), (3, 0, []))
        self.assertEqual((report.source_observations, report.source_frequencies, report.source_stations,
                          report.source_enriched), (3, 2, 1, 1))
        self.assertTrue(report.store_created and report.written and report.legacy_counters_checked)
        self.assertEqual(report.legacy_counter_mismatches, {})
        data = load_store(self.store)
        legacy = legacy_journal()
        for index, (stored, original) in enumerate(zip(data["observations"], legacy["detections"])):
            self.assertEqual({key: value for key, value in stored.items() if key not in ("id", "origin")},
                             original)
            self.assertEqual(stored["origin"], {"kind": "legacy-import", "import_id": report.import_id,
                                                "source_index": index})
        oldest = data["observations"][0]
        self.assertEqual(oldest["required_margin_db"], 8)
        for later_field in ("minimum_observed_seconds", "telemetry_version", "squelch_dbfs", "scan"):
            self.assertNotIn(later_field, oldest)
        self.assertEqual(data["observations"][2]["scan"]["mode"], "full_airband")
        self.assertEqual(data["stations"], legacy["stations"])
        record = data["imports"][0]
        self.assertEqual(record["source_sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertEqual((record["observations_added"], record["source_observations"]), (3, 3))
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertEqual(sorted(entry.name for entry in self.source.parent.iterdir()),
                         ["airband-peaks.json", "work"], "nothing is created next to the source")

    def test_repeated_import_adds_nothing_and_does_not_rewrite(self):
        import_legacy(self.source, self.store, clock_utc=clock())
        before, mtime = self.store.read_bytes(), self.store.stat().st_mtime_ns
        report = import_legacy(self.source, self.store, clock_utc=clock(8))
        self.assertEqual((report.added, report.already_present, report.written), (0, 3, False))
        self.assertEqual((report.stations_added, report.stations_unchanged), (0, 1))
        self.assertIsNone(report.import_id)
        self.assertEqual((self.store.read_bytes(), self.store.stat().st_mtime_ns), (before, mtime))
        with ObservationStore(self.store) as store:
            self.assertEqual((store.count(135925000), store.count(136575000)), (2, 1))

    def test_longer_history_adds_only_new_rows(self):
        import_legacy(self.source, self.store, clock_utc=clock())
        longer = legacy_journal()
        longer["detections"].append(dict(longer["detections"][0],
                                         listening_started_at_utc="2026-11-07T11:00:00+00:00",
                                         confirmed_at_utc="2026-11-07T11:00:10+00:00"))
        longer["frequency_stats"]["136575000"]["validated_detection_count"] = 2
        self.write_source(longer)
        report = import_legacy(self.source, self.store, clock_utc=clock(8))
        self.assertEqual((report.added, report.already_present), (1, 3))
        data = load_store(self.store)
        self.assertEqual((len(data["observations"]), len(data["imports"])), (4, 2))
        self.assertEqual(data["observations"][3]["origin"]["source_index"], 3)

    def test_dry_run_writes_nothing(self):
        report = import_legacy(self.source, self.store, dry_run=True, clock_utc=clock())
        self.assertEqual((report.added, report.store_created, report.written), (3, True, False))
        self.assertFalse(self.store.parent.exists())
        import_legacy(self.source, self.store, clock_utc=clock())
        before = self.store.read_bytes()
        report = import_legacy(self.source, self.store, dry_run=True, clock_utc=clock())
        self.assertEqual((report.added, report.already_present, report.store_created), (0, 3, False))
        self.assertEqual(self.store.read_bytes(), before)

    def test_working_copy_squelch_memory_is_never_overwritten(self):
        import_legacy(self.source, self.store, clock_utc=clock())
        with ObservationStore(self.store) as store:
            store.remember_squelch(135925000, dict(store.stations["135925000"], squelch_dbfs=-59.0))
        report = import_legacy(self.source, self.store, clock_utc=clock())
        self.assertEqual((report.stations_kept, report.written), (["135925000"], False))
        self.assertEqual(load_store(self.store)["stations"]["135925000"]["squelch_dbfs"], -59.0)

    def test_conflicting_row_is_reported_and_the_stored_row_kept(self):
        import_legacy(self.source, self.store, clock_utc=clock())
        changed = legacy_journal()
        changed["detections"][1]["active_seconds"] = 1.5
        self.write_source(changed)
        report = import_legacy(self.source, self.store, clock_utc=clock())
        self.assertEqual((len(report.conflicts), report.added, report.written), (1, 0, False))
        self.assertEqual(load_store(self.store)["observations"][1]["active_seconds"], 9.883)

    def test_legacy_counter_mismatch_is_reported_not_trusted(self):
        broken = legacy_journal()
        broken["frequency_stats"]["135925000"]["validated_detection_count"] = 5
        del broken["frequency_stats"]["136575000"]
        self.write_source(broken)
        report = import_legacy(self.source, self.store, clock_utc=clock())
        self.assertEqual(report.legacy_counter_mismatches, {"135925000": (5, 2), "136575000": (None, 1)})
        with ObservationStore(self.store) as store:
            self.assertEqual(store.count(135925000), 2)

    def test_rows_without_timestamps_are_identified_by_content_and_occurrence(self):
        bare = {"schema_version": 1, "detector": "fft-peaks",
                "detections": [{"frequency_hz": 121500000}, {"frequency_hz": 121500000}]}
        self.write_source(bare)
        first = import_legacy(self.source, self.store, clock_utc=clock())
        second = import_legacy(self.source, self.store, clock_utc=clock())
        self.assertEqual((first.added, second.added, second.already_present), (2, 0, 2))
        self.assertFalse(first.legacy_counters_checked)
        identifiers = [row["id"] for row in load_store(self.store)["observations"]]
        self.assertEqual(len(set(identifiers)), 2)

    def test_source_is_never_the_store(self):
        with self.assertRaisesRegex(StorageError, "different files"):
            import_legacy(self.source, self.source)
        link = self.source.with_name("link.json")
        link.symlink_to(self.source)
        with self.assertRaisesRegex(StorageError, "different files"):
            import_legacy(self.source, link)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_other_formats_are_rejected_without_creating_a_store(self):
        cases = (('{"schema_version": 1, "detections": []}', "not a pc_repair FFT journal"),
                 ("[1, 2]", "not a pc_repair FFT journal"), ("{oops", "not valid JSON"),
                 (json.dumps(dict(legacy_journal(), detections=[{"frequency_hz": "131.5"}])), "invalid legacy"),
                 (json.dumps(dict(legacy_journal(), detections=[{"frequency_hz": 1, "id": "x"}])), "reserved"),
                 ('{"format": "radiolack.observations"}', "already a RadioLack store"))
        for text, message in cases:
            self.source.write_text(text)
            with self.assertRaisesRegex(StoreFormatError, message):
                import_legacy(self.source, self.store)
            self.assertFalse(self.store.exists())
        with self.assertRaisesRegex(StorageError, "Cannot read"):
            import_legacy(self.source.with_name("missing.json"), self.store)


if __name__ == "__main__":
    unittest.main()
