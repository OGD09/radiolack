"""Recordings: what the VFO plays, in WAV files, and the receiver's raw I/Q,
in SigMF files, and the list of the recordings in their folder.

Radiolack records only when an interface asks, never at start and never on
the simulated band (engine.py). A recording is written by a thread of its
own, so that a slow disk never holds up the receiver: the engine hands it
what comes, and the thread writes it about twice a second. Its files are
made with the first data, readable by the user only, and named after the
recording's start, in UTC, and its frequency; a name already taken gets
``-2``, ``-3`` and so on. A recording that ends before any data came leaves
no file.

An audio recording is a 16-bit mono WAV file at the audio rate, named after
the VFO's frequency and its mode, such as
``2026-11-14_15-30-45Z_145.500000MHz_NFM.wav``. The WAV header follows each
write, so that a crash leaves a file that plays.

An I/Q recording keeps the receiver's samples as they come, before
anything else, in a SigMF recording (https://sigmf.org): a ``.sigmf-data``
file of I/Q pairs, I first, in the receiver's own format, as SigMF names it
(``IQ_DATATYPES``): unsigned 8-bit samples as rtl_tcp sends them ("cu8"),
or a SoapySDR device's signed 8-bit or 16-bit integers or 32-bit floats
("ci8", "ci16_le", "cf32_le"); and a ``.sigmf-meta`` file, which gives the
format, the sample rate, the receiver, and a capture for each stretch of
samples: its first sample, its centre frequency and the UTC time it
started. A retune, and a gap in the samples, start a new capture. Integers
whose full scale is not their type's, such as a bladeRF's 12 bits in 16-bit
integers, are written as they come, and the metadata gives that full scale
in a field of Radiolack's own, ``radiolack:full_scale``, which
``core:extensions`` declares optional, as SigMF asks of a namespace other
than its core, which has no field for it. Both files are named after the
receiver's centre frequency and its sample rate, such as
``2026-11-14_15-30-45Z_145.000000MHz_2.400000MSps.sigmf-data``: the
format is the metadata's alone. The metadata is written whole, under a
temporary name then renamed, with the first samples, then after the
captures change, at most every META_INTERVAL_S, and at the end: a crash
leaves a recording that reads.

A recording does not start, or stops by itself, when less than 2 GiB stays
free on its disk (``space``): its thread checks the free space before each
write, against what it is about to write, so that the disk never goes
under 2 GiB through it, whatever the rate. An audio recording also stops
when its file reaches the WAV format's limit of 4 GiB (``full``), and both
stop on a disk error or a disk too slow for them (``disk``, with what went
wrong): more audio waiting for the disk than PENDING_LIMIT_BYTES, or I/Q
than IQ_PENDING_S of it (IQ_PENDING_LIMIT_BYTES at least). The thread
writes what came about twice a second, and as soon as WRITE_BATCH_BYTES
wait, so that it keeps up with 64 MB a second, a SoapySDR device's 8 MS/s
in 32-bit floats, on a disk that does.

The recordings go to ``$XDG_DATA_HOME/radiolack/recordings``, or
``~/.local/share/radiolack/recordings`` when that variable is unset or not
an absolute path, made when needed and readable by the user only, unless
the user chose another folder, which must exist. The list shows the latest
LIST_LIMIT recordings of the folder, the newest first: the regular files
named as Radiolack names them, which alone are downloaded or deleted. An
I/Q recording is listed and deleted as its data file, with its metadata,
whose format gives its length.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import math
import os
import re
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

from . import __version__
from .demod import MODES
from .store import shown_path, sync_directory

FOLDER_NAME = "recordings"
MIN_FREE_BYTES = 2 * 1024 ** 3      # a recording does not start, or stops, with less free on its disk
# A canonical 44-byte header: RIFF, the "fmt " chunk of 16-bit PCM, then the data chunk's header.
WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
WAV_DATA_LIMIT = (0xFFFFFFFF - (WAV_HEADER.size - 8)) & ~1     # RIFF sizes are 32-bit: 4 GiB
LIST_LIMIT = 500                    # the recordings listed, the latest
WRITE_INTERVAL_S = 0.5              # the thread writes what came this often,
WRITE_BATCH_BYTES = 4 * 1024 * 1024 # or as soon as this much waits
SPACE_CHECK_S = 1.0                 # it checks the free space before each write, and this often meanwhile
PENDING_LIMIT_BYTES = 16 * 1024 * 1024      # audio waiting for the disk: more stops the recording
IQ_PENDING_S = 2.0                  # I/Q waiting for the disk: more than this many seconds of it stops the
IQ_PENDING_LIMIT_BYTES = 64 * 1024 * 1024   # recording, or than this when more: 14 s at 2.4 MS/s in 8 bits
META_INTERVAL_S = 5.0               # an I/Q recording's metadata follows its captures this often
SIGMF_VERSION = "1.0.0"             # the SigMF version the metadata follows, its readers' oldest
# The I/Q formats recorded, as SigMF names them, with the bytes of an I/Q
# pair in each: rtl_tcp's unsigned bytes, and a SoapySDR device's CS8, CS16
# and CF32, in the computer's byte order.
IQ_DATATYPES = {"cu8": 2, "ci8": 2, "ci16_le": 4, "ci16_be": 4, "cf32_le": 8, "cf32_be": 8}
EXTENSION = {"name": "radiolack", "version": "1.0.0", "optional": True}   # Radiolack's namespace, for SigMF
FULL_SCALE_FIELD = "radiolack:full_scale"   # an integer format's full scale, where it is not its type's
META_HEAD_BYTES = 65536             # of an I/Q recording's metadata, read for its format in the list
IQ_MODE = "I/Q"                     # an I/Q recording's mode in the list
DATA_SUFFIX = ".sigmf-data"
META_SUFFIX = ".sigmf-meta"
MAX_COUNT = 999                     # recordings of the same name, -2 to -999 after the first
FOLDER_PATH_LIMIT = 4096            # characters in a folder that a user gives
_SAMPLE_BYTES = 2                   # 16-bit mono
_START = r"(?P<start>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})Z_(?P<mhz>\d{1,5}\.\d{6})MHz_"
_COUNT = r"(?:-(?P<count>[1-9]\d{0,2}))?"
_AUDIO_NAME = re.compile(_START + "(?P<mode>" + "|".join(MODES) + ")" + _COUNT + r"\.wav")
_IQ_NAME = re.compile(_START + r"(?P<msps>\d{1,3}\.\d{6})MSps" + _COUNT + r"\.(?P<part>sigmf-data|sigmf-meta)")
# SigMF's datatype: in the global object alone, which comes first in the metadata Radiolack writes.
_DATATYPE = re.compile(rb'"core:datatype"\s*:\s*"([a-z0-9_]{1,16})"')

log = logging.getLogger(__name__)

# What the engine is told from a recording's thread, as (recording, event, value):
# "opened" with the file's name, then "stopped" with (reason or None, detail).
Report = Callable[["Recording", str, Any], None]


def default_folder(environ: Mapping[str, str] | None = None) -> Path:
    """The folder Radiolack records into unless the user chose another."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_DATA_HOME", "")
    if base and os.path.isabs(base):
        return Path(base) / "radiolack" / FOLDER_NAME
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".local" / "share" / "radiolack" / FOLDER_NAME


def recording_name(started: float, frequency_hz: float, mode: str, count: int = 1) -> str:
    """The name of an audio recording started at ``started`` (Unix time) on
    ``frequency_hz`` in ``mode``: the ``count``-th of that name."""
    return f"{_stamp(started)}_{frequency_hz / 1e6:.6f}MHz_{mode}{_suffix(count)}.wav"


def iq_name(started: float, frequency_hz: float, rate_hz: float, count: int = 1) -> str:
    """The name of the data file of an I/Q recording started at ``started``
    (Unix time) on ``frequency_hz`` at ``rate_hz`` samples per second: the
    ``count``-th of that name. ``meta_name`` gives its metadata's."""
    return f"{_stamp(started)}_{frequency_hz / 1e6:.6f}MHz_{rate_hz / 1e6:.6f}MSps{_suffix(count)}{DATA_SUFFIX}"


def meta_name(data_name: str) -> str:
    """The name of the metadata of the I/Q recording whose data file is ``data_name``."""
    if not data_name.endswith(DATA_SUFFIX):
        raise ValueError(f"{data_name} is not an I/Q recording's data")
    return data_name.removesuffix(DATA_SUFFIX) + META_SUFFIX


def _stamp(started: float) -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%SZ", time.gmtime(started))


def _suffix(count: int) -> str:
    return "" if count == 1 else f"-{count}"


class _Named(NamedTuple):
    """What a recording's file name tells."""
    started: int            # Unix time
    frequency_hz: int
    mode: str               # the audio's mode, or IQ_MODE
    count: int              # 1, then 2 and on for the names already taken
    part: str               # "wav", or "sigmf-data" or "sigmf-meta" for I/Q
    rate_hz: int | None     # an I/Q recording's sample rate


def _named(name: object) -> _Named | None:
    """What ``name`` tells, if Radiolack could have named a recording's file
    so; None otherwise."""
    if not isinstance(name, str):
        return None
    match = _AUDIO_NAME.fullmatch(name) or _IQ_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        started = datetime.strptime(match["start"], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    except ValueError:      # such as a 13th month
        return None
    count = int(match["count"] or 1)
    # Six decimals of MHz or MS/s: their digits are hertz, or samples per second.
    frequency_hz = int(match["mhz"].replace(".", ""))
    if match.re is _AUDIO_NAME:
        return _Named(int(started.timestamp()), frequency_hz, match["mode"], count, "wav", None)
    rate_hz = int(match["msps"].replace(".", ""))
    if not rate_hz:
        return None
    return _Named(int(started.timestamp()), frequency_hz, IQ_MODE, count, match["part"], rate_hz)


def is_recording_name(name: object) -> bool:
    """Whether ``name`` is named as Radiolack names its recordings, as listed
    and deleted: a WAV file, or an I/Q recording's data file."""
    named = _named(name)
    return named is not None and named.part != "sigmf-meta"


def is_recording_file(name: object) -> bool:
    """Whether ``name`` is named as Radiolack names its recordings' files, to
    download: a recording's, or an I/Q recording's metadata."""
    return _named(name) is not None


def chosen_folder(value: object) -> Path | None:
    """The recordings folder that a user gave, checked for its form: a full
    path, ``~`` standing for the home folder; None for the default (null or
    an empty text). Raises ValueError, saying why."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > FOLDER_PATH_LIMIT or "\0" in value:
        raise ValueError("the recordings folder is a path, such as ~/Music/Radiolack")
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        raise ValueError("give the folder's full path, such as ~/Music/Radiolack or /media/radio")
    return Path(path)


def check_folder(folder: Path) -> None:
    """Raise ValueError, saying why, unless ``folder`` is a folder that
    Radiolack may write into. For a worker thread: the disk may be slow."""
    shown = shown_path(folder)
    try:
        info = os.stat(folder)
    except FileNotFoundError:
        raise ValueError(f"{shown} does not exist") from None
    except OSError as error:
        raise ValueError(f"{shown} cannot be reached: {_reason(error)}") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{shown} is not a folder")
    if not os.access(folder, os.W_OK | os.X_OK):
        raise ValueError(f"Radiolack may not write into {shown}")


def free_bytes(folder: Path) -> int | None:
    """The space free for Radiolack on the disk of ``folder``, or of its
    nearest folder that exists; None when it cannot be known."""
    path = folder
    while True:
        try:
            info = os.statvfs(path)
        except FileNotFoundError:
            if path.parent == path:
                return None
            path = path.parent
        except OSError:
            return None
        else:
            return info.f_bavail * info.f_frsize


def list_recordings(folder: Path, running: Collection[str] = (), limit: int = LIST_LIMIT) -> dict[str, Any]:
    """The recordings of ``folder``, the latest ``limit`` of them, the newest
    first, ``running`` holding the files of the recordings that run, how
    many others it holds, the free space and what went wrong reading it: the
    recordings message's fields (docs/design/protocol.md). For a worker
    thread."""
    found: dict[str, _Named] = {}
    problem = ""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                named = _named(entry.name)
                # An I/Q recording is listed as its data file.
                if named is not None and named.part != "sigmf-meta" and entry.is_file(follow_symlinks=False):
                    found[entry.name] = named
    except FileNotFoundError:
        pass        # nothing recorded there yet
    except OSError as error:
        problem = f"{shown_path(folder)} cannot be read: {_reason(error)}."
    # In the order they started.
    names = sorted(found, key=lambda name: (found[name].started, found[name].count, name), reverse=True)
    files = [described for name in names[:limit]
             if (described := _describe(folder, name, found[name], name in running)) is not None]
    return {"files": files, "more": max(0, len(names) - limit), "free_bytes": free_bytes(folder),
            "problem": problem}


def delete_recording(folder: Path, name: str) -> None:
    """Delete the recording ``name`` of ``folder``: an I/Q recording's data
    file, with its metadata. Raises ValueError for a file that is not a
    recording, FileNotFoundError when it is no longer there and OSError when
    it cannot be deleted. For a worker thread."""
    if not is_recording_name(name):
        raise ValueError("not a recording")
    path = folder / name
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError(f"{name} is not a recording")
    if name.endswith(DATA_SUFFIX):
        # The metadata first: a failure leaves the data, which the list shows.
        meta = folder / meta_name(name)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISREG(os.lstat(meta).st_mode):
                os.unlink(meta)
    os.unlink(path)


def open_recording(folder: Path, name: str) -> tuple[BinaryIO, int]:
    """The recording's file ``name`` of ``folder``, open for reading, and its
    size. Raises FileNotFoundError when there is no such file, a link or
    another kind of file included, and OSError when it cannot be read. For
    a worker thread."""
    if not is_recording_file(name):
        raise FileNotFoundError(errno.ENOENT, "not a recording", name)
    try:
        # Without following a link, and without waiting for a named pipe's writer.
        descriptor = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise FileNotFoundError(errno.ENOENT, "not a recording", name) from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError(errno.ENOENT, "not a recording", name)
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, "rb"), info.st_size
    except BaseException:
        os.close(descriptor)
        raise


def wav_header(rate_hz: int, data_bytes: int) -> bytes:
    """The header of a 16-bit mono WAV file at ``rate_hz`` with ``data_bytes`` of audio."""
    return WAV_HEADER.pack(b"RIFF", WAV_HEADER.size - 8 + data_bytes, b"WAVE", b"fmt ", 16, 1, 1, rate_hz,
                           rate_hz * _SAMPLE_BYTES, _SAMPLE_BYTES, 8 * _SAMPLE_BYTES, b"data", data_bytes)


def wav_seconds(header: bytes, size: int) -> float | None:
    """The length in seconds, to the tenth, of the 16-bit PCM WAV file of
    ``size`` bytes whose first bytes are ``header``, as Radiolack writes
    them; None for another file."""
    if len(header) < WAV_HEADER.size:
        return None
    riff, _, wave, fmt, fmt_bytes, form, channels, rate, _, align, bits, data, data_bytes = \
        WAV_HEADER.unpack_from(header)
    if ((riff, wave, fmt, data) != (b"RIFF", b"WAVE", b"fmt ", b"data") or (fmt_bytes, form, bits) != (16, 1, 16)
            or not rate or align != channels * _SAMPLE_BYTES or not align):
        return None
    # A crash leaves the header behind the audio after it, by up to a write.
    audio_bytes = max(0, min(data_bytes, size - WAV_HEADER.size))
    return round(audio_bytes / (rate * align), 1)


class Recording:
    """What both kinds of recordings share: a thread of their own writes
    what the engine hands over into ``folder``, which it makes first if
    ``make_folder``, the default folder's way. ``started`` and
    ``frequency_hz`` name the files. The thread tells ``report`` of its file
    once made, and that it stopped, with the reason when it stopped by
    itself. ``file`` is for the engine, which sets it once told."""

    kind = ""

    def __init__(self, folder: Path, started: float, frequency_hz: float, *, make_folder: bool,
                 report: Report) -> None:
        self.folder = folder
        self.started = started
        self.frequency_hz = frequency_hz
        self.file: str | None = None
        self._make_folder = make_folder
        self._report = report
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # Shared with the thread, under the lock.
        self._items: list[Any] = []
        self._pending = 0               # bytes in _items
        self._stopping = False
        # The thread's own.
        self._descriptor: int | None = None
        self._thread = threading.Thread(target=self._run, name=f"radiolack-{self.kind}-recording", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Write what came, then close the files. Safe to repeat."""
        with self._lock:
            self._stopping = True
            self._wake.notify()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the thread to end; returns whether it did."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _queue(self, item: Any, size: int, limit: int) -> bool:
        """Hand ``item``, of ``size`` bytes, to the thread; False, taking
        nothing, when more than ``limit`` bytes would wait for the disk."""
        with self._lock:
            if self._pending + size > limit:
                return False
            self._items.append(item)
            self._pending += size
            if self._pending >= WRITE_BATCH_BYTES:
                self._wake.notify()
        return True

    def _consume(self, items: list[Any]) -> str | None:
        """Write ``items``, making the file first; the reason to stop, if any."""
        raise NotImplementedError

    def _run(self) -> None:
        reason: str | None = None
        detail = ""
        try:
            reason = self._prepare()
            checked_s = time.monotonic()
            while reason is None:
                items, size, stopping = self._take()
                # Before each write, against what it writes: whatever the
                # rate, the disk keeps MIN_FREE_BYTES; and meanwhile, as
                # other programs may fill it.
                if items or time.monotonic() - checked_s >= SPACE_CHECK_S:
                    checked_s = time.monotonic()
                    if self._free_bytes() - size < MIN_FREE_BYTES:
                        reason = "space"
                        break
                if items:
                    reason = self._consume(items)
                    if reason is not None:
                        break
                if stopping:
                    break
        except OSError as error:
            reason, detail = "disk", _reason(error)
        except Exception:
            log.exception("the recording stopped on an error")
            reason, detail = "disk", "an unexpected error, which Radiolack's error output gives"
        problem = self._close()
        if problem and reason is None:
            reason, detail = "disk", problem
        self._report(self, "stopped", (reason, detail))

    def _prepare(self) -> str | None:
        """Make the default folder if needed; "space" when its disk is short of space."""
        if self._make_folder:
            _make_folders(self.folder)
        return "space" if self._free_bytes() < MIN_FREE_BYTES else None

    def _take(self) -> tuple[list[Any], int, bool]:
        """What came, its bytes, and whether to stop: once WRITE_INTERVAL_S
        passed, WRITE_BATCH_BYTES came or a stop was asked."""
        with self._lock:
            if not self._stopping and self._pending < WRITE_BATCH_BYTES:
                self._wake.wait(WRITE_INTERVAL_S)
            items, size = self._items, self._pending
            self._items, self._pending = [], 0
            return items, size, self._stopping

    def _free_bytes(self) -> int:
        info = os.fstatvfs(self._descriptor) if self._descriptor is not None else os.statvfs(self.folder)
        return info.f_bavail * info.f_frsize

    def _close(self) -> str:
        """Close the file once it reached the disk; what went wrong, or ""."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return ""
        problem = ""
        try:
            os.fsync(descriptor)
        except OSError as error:
            problem = _reason(error)
        try:
            os.close(descriptor)
        except OSError as error:
            problem = problem or _reason(error)
        return problem


class AudioRecording(Recording):
    """A recording of the VFO's audio into a WAV file, named after
    ``started``, ``frequency_hz`` and ``mode``."""

    kind = "audio"

    def __init__(self, folder: Path, started: float, frequency_hz: float, mode: str, *,
                 make_folder: bool, report: Report) -> None:
        super().__init__(folder, started, frequency_hz, make_folder=make_folder, report=report)
        self.mode = mode
        self._data = 0                  # bytes of audio written, the thread's own

    def write(self, pcm: bytes, rate_hz: int) -> bool:
        """Hand over 16-bit audio at ``rate_hz``, which the first audio sets
        for the whole recording. Returns False, taking nothing, when too much
        already waits for the disk."""
        return self._queue((pcm, rate_hz), len(pcm), PENDING_LIMIT_BYTES)

    def _consume(self, items: list[tuple[bytes, int]]) -> str | None:
        if self._descriptor is None:
            self._create(items[0][1])
        return None if self._append(b"".join(pcm for pcm, _ in items)) else "full"

    def _create(self, rate_hz: int) -> None:
        """Make the file, under the first name not taken, with its header."""
        for count in range(1, MAX_COUNT + 1):
            name = recording_name(self.started, self.frequency_hz, self.mode, count)
            descriptor = _new_file(self.folder / name)
            if descriptor is None:
                continue
            self._descriptor = descriptor
            _write_all(descriptor, wav_header(rate_hz, 0))
            self._report(self, "opened", name)
            return
        raise FileExistsError(errno.EEXIST, "every name for this recording is taken")

    def _append(self, data: bytes) -> bool:
        """Write ``data`` and bring the header up to date; returns False once
        the file is full, with what fitted written."""
        descriptor = self._descriptor
        room = WAV_DATA_LIMIT - self._data
        full = len(data) > room
        if full:
            data = data[:room]
        _write_all(descriptor, data)
        self._data += len(data)
        os.pwrite(descriptor, struct.pack("<I", WAV_HEADER.size - 8 + self._data), 4)
        os.pwrite(descriptor, struct.pack("<I", self._data), WAV_HEADER.size - 4)
        return not full


class IQRecording(Recording):
    """A recording of the receiver's raw I/Q into a SigMF recording, named
    after ``started``, ``frequency_hz``, the receiver's centre frequency, and
    ``rate_hz``, its sample rate, which stays the same throughout.
    ``hardware`` names the receiver in the metadata: the engine may change
    it, before handing the samples that follow a gap. ``datatype``, one of
    IQ_DATATYPES, is the samples' format, and ``full_scale`` an integer
    format's full scale where it is not its type's, or None; with
    ``datatype`` None, the format is the first that ``take_format`` gives,
    before the first samples."""

    kind = "iq"

    def __init__(self, folder: Path, started: float, frequency_hz: float, rate_hz: float, *, hardware: str,
                 make_folder: bool, report: Report, datatype: str | None = "cu8",
                 full_scale: float | None = None) -> None:
        super().__init__(folder, started, frequency_hz, make_folder=make_folder, report=report)
        self.rate_hz = rate_hz
        self.hardware = hardware
        self.datatype: str | None = None
        self.full_scale: float | None = None
        if datatype is not None:
            self.take_format(datatype, full_scale)
        # The thread's own.
        self._meta: str | None = None           # the metadata's name, once the data file is made
        self._samples = 0                       # I/Q pairs written
        self._captures: list[dict[str, Any]] = []
        self._centre_hz: float | None = None    # that of the latest capture
        self._meta_due = False                  # the captures changed since the metadata was written
        self._meta_next_s = 0.0                 # when it may be written again

    @property
    def pair_bytes(self) -> int | None:
        """The bytes of an I/Q pair, once the format is known."""
        return None if self.datatype is None else IQ_DATATYPES[self.datatype]

    def take_format(self, datatype: str, full_scale: float | None) -> bool:
        """Whether samples in ``datatype`` at ``full_scale`` (None: their
        type's) may be handed over: the first format given becomes the
        recording's, and only that one may. Raises ValueError for a format
        that SigMF does not name or a full scale that is not a positive
        number."""
        if datatype not in IQ_DATATYPES:
            raise ValueError(f"no I/Q recording in {datatype}")
        if full_scale is not None and not (math.isfinite(full_scale) and full_scale > 0):
            raise ValueError(f"no full scale of {full_scale}")
        with self._lock:
            if self.datatype is None:
                self.datatype, self.full_scale = datatype, full_scale
                return True
            return (self.datatype, self.full_scale) == (datatype, full_scale)

    def write(self, data: bytes, centre_hz: float, when_s: float, joined: bool) -> bool:
        """Hand over raw samples: whole I/Q pairs in the recording's format,
        I first, taken on ``centre_hz``, which came at ``when_s`` (Unix
        time), and whether they follow those handed before without a gap.
        Returns False, taking nothing, when too much already waits for the
        disk. Raises ValueError before the format is known."""
        pair_bytes = self.pair_bytes
        if pair_bytes is None:
            raise ValueError("an I/Q recording's format comes before its samples")
        limit = max(IQ_PENDING_LIMIT_BYTES, round(IQ_PENDING_S * self.rate_hz * pair_bytes))
        return self._queue((data, centre_hz, when_s, joined), len(data), limit)

    def _consume(self, items: list[tuple[bytes, float, float, bool]]) -> str | None:
        pair_bytes = self.pair_bytes
        assert pair_bytes is not None       # set before the first samples
        for data, centre_hz, when_s, joined in items:
            if self._descriptor is None:
                self._create()
            if not joined or centre_hz != self._centre_hz:
                # The first sample came the chunk's length before the chunk ended.
                first_s = when_s - len(data) / pair_bytes / self.rate_hz
                self._captures.append({"core:sample_start": self._samples, "core:frequency": _number(centre_hz),
                                       "core:datetime": _utc(first_s)})
                self._centre_hz = centre_hz
                self._meta_due = True
            _write_all(self._descriptor, data)
            self._samples += len(data) // pair_bytes
        if self._meta_due and time.monotonic() >= self._meta_next_s:
            self._write_meta()
        return None

    def _create(self) -> None:
        """Make the data file, under the first name not taken by either file."""
        for count in range(1, MAX_COUNT + 1):
            name = iq_name(self.started, self.frequency_hz, self.rate_hz, count)
            if os.path.lexists(self.folder / meta_name(name)):
                continue        # metadata whose data went: it stays as it is
            descriptor = _new_file(self.folder / name)
            if descriptor is None:
                continue
            self._descriptor, self._meta = descriptor, meta_name(name)
            self._report(self, "opened", name)
            return
        raise FileExistsError(errno.EEXIST, "every name for this recording is taken")

    def _write_meta(self) -> None:
        """Write the metadata whole, under a temporary name that is then
        renamed, so that a crash leaves the previous one."""
        description: dict[str, Any] = {
            "core:datatype": self.datatype,
            "core:sample_rate": _number(self.rate_hz),
            "core:version": SIGMF_VERSION,
            "core:hw": self.hardware,
            "core:recorder": f"Radiolack {__version__}",
        }
        if self.full_scale is not None:
            description["core:extensions"] = [EXTENSION]
            description[FULL_SCALE_FIELD] = _number(self.full_scale)
        metadata = {"global": description, "captures": self._captures, "annotations": []}
        text = json.dumps(metadata, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self._meta}.", suffix=".tmp", dir=self.folder)
        try:
            try:
                _write_all(descriptor, text.encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.folder / self._meta)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        sync_directory(self.folder)
        self._meta_due, self._meta_next_s = False, time.monotonic() + META_INTERVAL_S

    def _close(self) -> str:
        """Close the data file, then bring the metadata up to date."""
        problem = super()._close()
        if self._meta_due:
            try:
                self._write_meta()
            except OSError as error:
                problem = problem or _reason(error)
        return problem


def _make_folders(folder: Path) -> None:
    """Make ``folder`` and the folders missing above it, readable by the user only."""
    missing = []
    path = folder
    while not path.is_dir():
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not path.is_dir():
                raise


def _new_file(path: Path) -> int | None:
    """A new file at ``path``, open for writing and readable by the user
    only; None when the name is taken."""
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return None


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _number(value: float) -> int | float:
    """A number for the metadata, without a decimal part when it has none."""
    return int(value) if float(value).is_integer() else value


def _utc(when: float) -> str:
    """``when`` (Unix time) as SigMF writes times: in UTC, to the millisecond."""
    return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _describe(folder: Path, name: str, named: _Named, running: bool) -> dict[str, Any] | None:
    """A recording's entry in the list; None once it is no longer there."""
    iq = named.part == "sigmf-data"
    entry: dict[str, Any] = {"name": name, "kind": "iq" if iq else "audio", "started": named.started,
                             "frequency_hz": named.frequency_hz, "mode": named.mode, "bytes": None, "seconds": None,
                             "running": running}
    if iq:
        entry.update(rate_hz=named.rate_hz, meta=None)
    try:
        descriptor = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return entry
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        entry["bytes"] = info.st_size
        if not iq:
            entry["seconds"] = wav_seconds(os.pread(descriptor, WAV_HEADER.size, 0), info.st_size)
    except OSError:
        pass
    finally:
        os.close(descriptor)
    if iq:
        data_bytes = entry["bytes"]
        meta = _iq_meta(folder / meta_name(name))
        if meta is not None:
            entry["meta"] = meta_name(name)
            meta_bytes, pair_bytes = meta
            if data_bytes is not None:
                entry["bytes"] = data_bytes + meta_bytes
                # Its length from its format, which only its metadata gives.
                if pair_bytes:
                    entry["seconds"] = round(data_bytes // pair_bytes / named.rate_hz, 1)
    return entry


def _iq_meta(meta: Path) -> tuple[int, int] | None:
    """The size of the I/Q recording's metadata ``meta`` and the bytes of
    an I/Q pair in the format it gives, 0 when it gives none that
    IQ_DATATYPES knows; None without a regular file there. Only its first
    META_HEAD_BYTES are read: the format comes first."""
    try:
        descriptor = os.open(meta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        try:
            found = _DATATYPE.search(os.pread(descriptor, META_HEAD_BYTES, 0))
        except OSError:
            found = None
        pair_bytes = IQ_DATATYPES.get(found[1].decode("ascii"), 0) if found is not None else 0
        return info.st_size, pair_bytes
    finally:
        os.close(descriptor)


def _reason(error: OSError) -> str:
    return error.strerror or str(error)
