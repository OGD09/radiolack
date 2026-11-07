"""Observation store (one JSON document, atomic writes) and legacy journal import.

Store layout::

    {"format": "radiolack.observations", "format_version": 1,
     "created_at_utc": "...", "observations": [...], "stations": {...}, "imports": [...]}

* ``observations`` is an append-only ledger of validated listens. Imported
  rows keep every original field unchanged and only gain ``id`` and ``origin``.
* ``stations`` holds one squelch memory per frequency (Hz, as a string).
* ``imports`` records each import that changed the store.

Every update writes a temporary file in the same directory, fsyncs it and
atomically replaces the store: a failed or interrupted write leaves the
previous content intact. A lock file allows one writing process per store.
The legacy pc_repair journal is only ever read, never written.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import __version__
from .errors import StorageError, StoreFormatError, StoreLockedError, StoreMissingError, StoreWriteError
from .squelch import SOURCES
from .stats import frequency_statistics, is_number

FORMAT = "radiolack.observations"
FORMAT_VERSION = 1
LEGACY_SCHEMA_VERSION = 1
LEGACY_DETECTOR = "fft-peaks"
LEGACY_SOURCES = ("noise", "signal_fallback")
METADATA_KEYS = ("id", "origin")
_OPTIONAL_LEVELS = ("noise_floor_dbfs", "last_verified_squelch_dbfs", "provisional_squelch_dbfs",
                    "carrier_meter_dbfs")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def lock_path_for(store_path: Path) -> Path:
    return store_path.with_name(store_path.name + ".lock")


def new_document(created_at: str) -> dict:
    return {"format": FORMAT, "format_version": FORMAT_VERSION, "created_at_utc": created_at,
            "observations": [], "stations": {}, "imports": []}


def validate_station(key, record, sources=SOURCES):
    """Squelch memory rules inherited from the prototype, plus optional v2 levels."""
    if (not isinstance(key, str) or not key.isdigit() or not isinstance(record, dict)
            or not is_number(record.get("squelch_dbfs")) or not -150 <= record["squelch_dbfs"] <= 0
            or not isinstance(record.get("receiver_context"), dict)
            or record.get("source") not in sources):
        raise StoreFormatError(f"Invalid squelch memory for frequency {key!r}")
    for name in _OPTIONAL_LEVELS:
        value = record.get(name)
        if value is not None and not (is_number(value) and -150 <= value <= 0):
            raise StoreFormatError(f"Invalid {name} in squelch memory for frequency {key!r}")


def is_legacy_journal(data) -> bool:
    return (isinstance(data, dict) and "format" not in data
            and data.get("schema_version") == LEGACY_SCHEMA_VERSION
            and data.get("detector") == LEGACY_DETECTOR)


def validate_store(data, path):
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise StoreFormatError(f"{path} is not a RadioLack observation store; it was left untouched")
    if data.get("format_version") != FORMAT_VERSION:
        raise StoreFormatError(f"{path} uses store format version {data.get('format_version')!r}; "
                               f"this RadioLack reads version {FORMAT_VERSION}")
    observations, stations, imports = data.get("observations"), data.get("stations"), data.get("imports")
    if not isinstance(observations, list) or not isinstance(stations, dict) or not isinstance(imports, list):
        raise StoreFormatError(f"{path} is missing observations, stations or imports")
    seen = set()
    for index, observation in enumerate(observations):
        if (not isinstance(observation, dict) or not isinstance(observation.get("id"), str)
                or not observation["id"] or observation["id"] in seen
                or type(observation.get("frequency_hz")) is not int or observation["frequency_hz"] <= 0
                or not isinstance(observation.get("origin"), dict)
                or not isinstance(observation["origin"].get("kind"), str)):
            raise StoreFormatError(f"{path}: invalid or duplicate observation #{index}")
        seen.add(observation["id"])
    for key, record in stations.items():
        validate_station(key, record)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def load_store(path) -> dict:
    """Read and validate a store without locking it (safe during atomic writes)."""
    path = Path(path).expanduser().absolute()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StoreMissingError(f"No observation store at {path}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise StorageError(f"Cannot read {path}: {getattr(error, 'strerror', None) or error}") from None
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise StoreFormatError(f"{path} is not valid JSON ({error.msg}, line {error.lineno}); "
                               "it was left untouched") from None
    except ValueError as error:
        raise StoreFormatError(f"{path} contains a {error}; it was left untouched") from None
    if is_legacy_journal(data):
        raise StoreFormatError(f"{path} is a legacy pc_repair journal. RadioLack never writes to it: "
                               f"import it into a separate store with `radiolack import {path}`.")
    validate_store(data, path)
    return data


def _fsync_directory(directory: Path):
    """Best effort: the replace already committed the data on most file systems."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, data):
    """Write ``data`` to a temporary file, fsync it, then atomically replace ``path``."""
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise StoreWriteError(f"Could not write {path}: {error.strerror or error}. "
                              "The previous content is intact.") from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    _fsync_directory(path.parent)


def natural_key(event) -> str:
    """Stable identity of one listen, independent of the file it came from."""
    started, confirmed = event.get("listening_started_at_utc"), event.get("confirmed_at_utc")
    if isinstance(started, str) and isinstance(confirmed, str):
        return f"listen|{event['frequency_hz']}|{started}|{confirmed}"
    return "content|" + json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)


def observation_id(key: str, occurrence: int = 0) -> str:
    return "obs-" + hashlib.sha256(f"{key}#{occurrence}".encode("utf-8")).hexdigest()[:20]


def _acquire_lock(path: Path):
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError as error:
        raise StoreWriteError(f"Cannot create the lock file {path}: {error.strerror or error}") from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        handle.close()
        raise StoreLockedError("The store is in use by another RadioLack process"
                               f"{f' (pid {owner})' if owner else ''}; stop it first "
                               "(Ctrl+C in its terminal or `radiolack stop`).") from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


class ObservationStore:
    """Writable store: append validated listens and squelch memories atomically."""

    def __init__(self, path, *, create=False, clock_utc=utc_now_iso):
        self.path = Path(path).expanduser().absolute()
        self.clock_utc = clock_utc
        self.created = False
        self._lock = None
        if not self.path.exists() and not create:
            raise StoreMissingError(
                f"No observation store at {self.path}. Import the existing history first "
                "(radiolack import ...) or pass --create-store to start an empty one.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StoreWriteError(f"Cannot create {self.path.parent}: {error.strerror or error}") from None
        self._lock = _acquire_lock(lock_path_for(self.path))
        try:
            if self.path.exists():
                self.data = load_store(self.path)
            else:
                data = new_document(clock_utc())
                write_json_atomic(self.path, data)
                self.data = data
                self.created = True
        except BaseException:
            self.close()
            raise
        self._ids = {observation["id"] for observation in self.data["observations"]}
        self._counts = Counter(observation["frequency_hz"] for observation in self.data["observations"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._lock is not None:
            try:
                self._lock.seek(0)
                self._lock.truncate()
            except OSError:
                pass
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    @property
    def observations(self) -> list:
        return self.data["observations"]

    @property
    def stations(self) -> dict:
        return self.data["stations"]

    def count(self, frequency: int) -> int:
        return self._counts[frequency]

    def squelch_record(self, frequency, context):
        """Memorized squelch for this frequency, only if the receiver context matches."""
        record = self.stations.get(str(frequency))
        return dict(record) if record and record["receiver_context"] == context else None

    def _commit(self, updated):
        write_json_atomic(self.path, updated)
        self.data = updated

    def remember_squelch(self, frequency, record):
        validate_station(str(frequency), record)
        stations = dict(self.stations)
        stations[str(frequency)] = dict(record)
        self._commit(dict(self.data, stations=stations))

    def append(self, event, station=None) -> str:
        """Append one validated listen (and its station memory) in a single atomic write."""
        if set(METADATA_KEYS) & set(event):
            raise ValueError("Observation id and origin are assigned by the store")
        if type(event.get("frequency_hz")) is not int or event["frequency_hz"] <= 0:
            raise ValueError("Observation frequency must be a positive integer")
        key, occurrence = natural_key(event), 0
        identifier = observation_id(key)
        while identifier in self._ids:
            occurrence += 1
            identifier = observation_id(key, occurrence)
        observation = {"id": identifier, "origin": {"kind": "radiolack", "version": __version__}, **event}
        updated = dict(self.data, observations=self.observations + [observation])
        if station is not None:
            frequency, record = station
            validate_station(str(frequency), record)
            updated["stations"] = {**self.stations, str(frequency): dict(record)}
        self._commit(updated)
        self._ids.add(identifier)
        self._counts[event["frequency_hz"]] += 1
        return identifier


@dataclass
class ImportReport:
    source_path: str
    source_sha256: str
    store_path: str
    dry_run: bool
    store_created: bool = False
    source_observations: int = 0
    source_frequencies: int = 0
    source_stations: int = 0
    source_enriched: int = 0
    added: int = 0
    already_present: int = 0
    conflicts: list = field(default_factory=list)
    stations_added: int = 0
    stations_unchanged: int = 0
    stations_kept: list = field(default_factory=list)
    legacy_counters_checked: bool = False
    legacy_counter_mismatches: dict = field(default_factory=dict)
    written: bool = False
    import_id: str | None = None


def read_legacy_journal(source: Path):
    """Parse and validate a pc_repair FFT journal; return (data, raw bytes)."""
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise StorageError(f"Cannot read {source}: {error.strerror or error}") from None
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise StoreFormatError(f"{source} is not valid JSON: {error}") from None
    if isinstance(data, dict) and data.get("format") == FORMAT:
        raise StoreFormatError(f"{source} is already a RadioLack store, not a legacy journal")
    if not is_legacy_journal(data) or not isinstance(data.get("detections"), list):
        raise StoreFormatError(f"{source} is not a pc_repair FFT journal "
                               f"(schema_version {LEGACY_SCHEMA_VERSION}, detector \"{LEGACY_DETECTOR}\")")
    for index, event in enumerate(data["detections"]):
        if (not isinstance(event, dict) or type(event.get("frequency_hz")) is not int
                or event["frequency_hz"] <= 0):
            raise StoreFormatError(f"{source}: invalid legacy detection #{index}")
        clash = set(METADATA_KEYS) & set(event)
        if clash:
            raise StoreFormatError(f"{source}: detection #{index} already uses reserved keys "
                                   f"{', '.join(sorted(clash))}")
    stations = data.get("stations", {})
    if not isinstance(stations, dict):
        raise StoreFormatError(f"{source}: invalid stations table")
    for key, record in stations.items():
        validate_station(key, record, LEGACY_SOURCES)
    if data.get("frequency_stats") is not None and not isinstance(data["frequency_stats"], dict):
        raise StoreFormatError(f"{source}: invalid frequency_stats table")
    return data, raw


def _plan_import(data, legacy, import_id, report):
    existing = {observation["id"]: observation for observation in data["observations"]}
    occurrences = Counter()
    new_observations = []
    for index, event in enumerate(legacy["detections"]):
        key = natural_key(event)
        identifier = observation_id(key, occurrences[key])
        occurrences[key] += 1
        if identifier in existing:
            stored = {name: value for name, value in existing[identifier].items() if name not in METADATA_KEYS}
            if stored == event:
                report.already_present += 1
            else:
                report.conflicts.append(identifier)
            continue
        new_observations.append({"id": identifier, "origin": {"kind": "legacy-import", "import_id": import_id,
                                                              "source_index": index}, **event})
    report.added = len(new_observations)
    new_stations = {}
    for key, record in legacy.get("stations", {}).items():
        current = data["stations"].get(key)
        if current is None:
            new_stations[key] = dict(record)
        elif current == record:
            report.stations_unchanged += 1
        else:
            # The working copy evolves after import; never overwrite it with older memory.
            report.stations_kept.append(key)
    report.stations_added = len(new_stations)
    return new_observations, new_stations


def import_legacy(source, store_path, *, dry_run=False, clock_utc=utc_now_iso) -> ImportReport:
    """Import a legacy journal into a separate store; repeated imports add nothing.

    Observations are identified by (frequency, listen start, confirmation)
    timestamps, or by their full content when timestamps are missing, so a
    second import of the same history neither duplicates rows nor counters.
    When nothing new is found, the store file is not rewritten at all.
    """
    source = Path(source).expanduser().absolute()
    store_path = Path(store_path).expanduser().absolute()
    if source == store_path or (source.exists() and store_path.exists() and os.path.samefile(source, store_path)):
        raise StorageError("The import source and the store must be different files; "
                           "the legacy journal is never written.")
    legacy, raw = read_legacy_journal(source)
    digest = hashlib.sha256(raw).hexdigest()
    report = ImportReport(str(source), digest, str(store_path), dry_run)
    detections, stations = legacy["detections"], legacy.get("stations", {})
    report.source_observations = len(detections)
    report.source_frequencies = len({event["frequency_hz"] for event in detections})
    report.source_stations = len(stations)
    report.source_enriched = sum(event.get("telemetry_version") is not None for event in detections)
    stored_stats = legacy.get("frequency_stats")
    if isinstance(stored_stats, dict):
        report.legacy_counters_checked = True
        recomputed = {key: item["validated_detection_count"]
                      for key, item in frequency_statistics(detections).items()}
        for key in sorted(set(recomputed) | set(stored_stats), key=str):
            entry = stored_stats.get(key)
            stored = entry.get("validated_detection_count") if isinstance(entry, dict) else None
            if stored != recomputed.get(key):
                report.legacy_counter_mismatches[key] = (stored, recomputed.get(key))
    now = clock_utc()
    compact = "".join(character for character in now[:19] if character.isdigit())
    import_id = f"imp-{digest[:12]}-{compact}"
    if dry_run:
        data = load_store(store_path) if store_path.exists() else new_document(now)
        report.store_created = not store_path.exists()
        _plan_import(data, legacy, import_id, report)
        return report
    store = ObservationStore(store_path, create=True, clock_utc=clock_utc)
    try:
        report.store_created = store.created
        new_observations, new_stations = _plan_import(store.data, legacy, import_id, report)
        if new_observations or new_stations:
            record = {
                "id": import_id, "kind": "legacy-pc_repair-fft-peaks", "imported_at_utc": now,
                "imported_by": f"radiolack {__version__}", "source_path": str(source),
                "source_sha256": digest, "source_bytes": len(raw),
                "source_schema_version": legacy["schema_version"], "source_detector": legacy["detector"],
                "source_observations": report.source_observations,
                "observations_added": report.added,
                "observations_already_present": report.already_present,
                "observation_conflicts": len(report.conflicts),
                "source_stations": report.source_stations, "stations_added": report.stations_added,
                "stations_unchanged": report.stations_unchanged,
                "stations_kept_existing": len(report.stations_kept),
                "legacy_counters_checked": report.legacy_counters_checked,
                "legacy_counter_mismatches": len(report.legacy_counter_mismatches),
            }
            store._commit(dict(store.data, observations=store.observations + new_observations,
                               stations={**store.stations, **new_stations},
                               imports=store.data["imports"] + [record]))
            report.written = True
            report.import_id = import_id
    finally:
        store.close()
    return report
