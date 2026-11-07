"""Observation store: atomic writes, locking, squelch memories and restarts."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radiolack.errors import StoreFormatError, StoreLockedError, StoreMissingError, StoreWriteError
from radiolack.storage import ObservationStore, load_store, lock_path_for

from tests.helpers import CONTEXT, legacy_journal

STATION = {"squelch_dbfs": -57.0, "noise_floor_dbfs": -60.0, "source": "noise", "receiver_context": CONTEXT}


def event(frequency=131500000, second=0):
    return {"listening_started_at_utc": f"2026-11-07T08:00:{second:02d}+00:00",
            "confirmed_at_utc": f"2026-11-07T08:00:{second + 10:02d}+00:00",
            "frequency_hz": frequency, "active_seconds": 9.5, "observed_seconds": 9.9}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "work" / "observations.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_store_is_only_created_on_request(self):
        with self.assertRaisesRegex(StoreMissingError, "radiolack import"):
            ObservationStore(self.path)
        self.assertFalse(self.path.exists())
        with ObservationStore(self.path, create=True) as store:
            self.assertTrue(store.created)
        self.assertEqual(load_store(self.path)["observations"], [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_restart_keeps_rows_counts_and_memories_without_double_counting(self):
        with ObservationStore(self.path, create=True) as store:
            first = store.append(event(), station=(131500000, STATION))
            store.append(event(second=30))
            store.append(event(136925000))
            self.assertEqual(store.count(131500000), 2)
        written = self.path.read_bytes()
        with ObservationStore(self.path) as store:
            self.assertFalse(store.created)
            self.assertEqual((store.count(131500000), store.count(136925000)), (2, 1))
            self.assertEqual(store.observations[0]["id"], first)
            self.assertEqual(store.observations[0]["origin"]["kind"], "radiolack")
            self.assertEqual(store.stations["131500000"]["squelch_dbfs"], -57.0)
        self.assertEqual(self.path.read_bytes(), written, "opening a store never rewrites it")

    def test_identical_events_get_distinct_identities(self):
        with ObservationStore(self.path, create=True) as store:
            self.assertNotEqual(store.append(event()), store.append(event()))

    def test_reserved_keys_and_invalid_frequency_are_refused(self):
        with ObservationStore(self.path, create=True) as store:
            for bad in (dict(event(), id="x"), dict(event(), origin={}), dict(event(), frequency_hz=131.5),
                        dict(event(), frequency_hz=True)):
                with self.assertRaises(ValueError):
                    store.append(bad)
            self.assertEqual(store.observations, [])

    def test_failed_write_keeps_previous_content_memory_and_counters(self):
        with ObservationStore(self.path, create=True) as store:
            store.append(event())
            before = self.path.read_bytes()
            full = OSError(28, "No space left on device")
            with patch("radiolack.storage.os.replace", side_effect=full):
                with self.assertRaisesRegex(StoreWriteError, "previous content is intact"):
                    store.append(event(second=30), station=(131500000, STATION))
                with self.assertRaises(StoreWriteError):
                    store.remember_squelch(131500000, STATION)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual((len(store.observations), store.count(131500000), store.stations), (1, 1, {}))
            self.assertEqual(sorted(entry.name for entry in self.path.parent.iterdir()),
                             ["observations.json", "observations.json.lock"], "no temporary file left")
            store.append(event(second=30))
        self.assertEqual(len(load_store(self.path)["observations"]), 2)

    @unittest.skipIf(os.geteuid() == 0, "permissions do not apply to root")
    def test_unwritable_directory_is_a_storage_error(self):
        self.path.parent.mkdir()
        self.path.parent.chmod(0o500)
        try:
            with self.assertRaises(StoreWriteError):
                ObservationStore(self.path, create=True)
        finally:
            self.path.parent.chmod(0o700)

    def test_invalid_or_foreign_files_are_reported_and_left_untouched(self):
        self.path.parent.mkdir()
        legacy = json.dumps(legacy_journal())
        for text, message in (("{not json", "not valid JSON"), ('{"format": "other"}', "not a RadioLack"),
                              (legacy, "radiolack import"), ('{"x": NaN}', "non-finite")):
            self.path.write_text(text)
            with self.assertRaisesRegex(StoreFormatError, message):
                ObservationStore(self.path)
            self.assertEqual(self.path.read_text(), text)

    def test_duplicate_ids_or_future_format_are_refused(self):
        with ObservationStore(self.path, create=True) as store:
            store.append(event())
        data = load_store(self.path)
        for broken in (dict(data, observations=data["observations"] * 2), dict(data, format_version=2)):
            self.path.write_text(json.dumps(broken))
            with self.assertRaises(StoreFormatError):
                load_store(self.path)

    def test_second_writer_is_refused_with_the_owner_pid(self):
        with ObservationStore(self.path, create=True):
            self.assertEqual(lock_path_for(self.path).read_text().strip(), str(os.getpid()))
            with self.assertRaisesRegex(StoreLockedError, f"pid {os.getpid()}"):
                ObservationStore(self.path)
            load_store(self.path)  # readers never need the lock
        with ObservationStore(self.path):
            pass

    def test_squelch_memory_requires_the_same_receiver_context(self):
        with ObservationStore(self.path, create=True) as store:
            store.remember_squelch(131500000, STATION)
            self.assertEqual(store.squelch_record(131500000, CONTEXT)["squelch_dbfs"], -57.0)
            controls = [[4, 400] if item[0] == 4 else item for item in CONTEXT["receiver_controls"]]
            for changed in (dict(CONTEXT, receiver_controls=controls), dict(CONTEXT, sample_rate_hz=2048000),
                            dict(CONTEXT, filter_hz=6000), dict(CONTEXT, mode="FM")):
                self.assertIsNone(store.squelch_record(131500000, changed))
            self.assertIsNone(store.squelch_record(131000000, CONTEXT))
        with ObservationStore(self.path) as store:
            self.assertEqual(store.squelch_record(131500000, CONTEXT)["noise_floor_dbfs"], -60.0)

    def test_invalid_squelch_memory_is_refused(self):
        with ObservationStore(self.path, create=True) as store:
            for record in (dict(STATION, squelch_dbfs=5), dict(STATION, source="guess"),
                           dict(STATION, noise_floor_dbfs="low"), dict(STATION, carrier_meter_dbfs=-200),
                           {"squelch_dbfs": -50, "source": "noise"}):
                with self.assertRaises(StoreFormatError):
                    store.remember_squelch(131500000, record)
            self.assertEqual(store.stations, {})


if __name__ == "__main__":
    unittest.main()
