"""Recordings: what the VFO plays, in WAV files, and the list of the
recordings in their folder.

Radiolack records only when an interface asks, never at start and never on
the simulated band (engine.py). A recording is written by a thread of its
own, so that a slow disk never holds up the receiver: the engine hands it
the audio of each block, and the thread writes what came about twice a
second. The file is made with the first audio, in 16-bit mono WAV at the
audio rate, readable by the user only, and named after the recording's
start, in UTC, the VFO's frequency and its mode, such as
``2026-11-14_15-30-45Z_145.500000MHz_NFM.wav``; a name already taken gets
``-2``, ``-3`` and so on. The WAV header follows each write, so that a crash
leaves a file that plays. A recording that ends before any audio came leaves
no file.

A recording does not start, or stops by itself, when less than 2 GiB stays
free on its disk (``space``); it also stops when its file reaches the WAV
format's limit of 4 GiB (``full``), and on a disk error or a disk too slow
for it (``disk``, with what went wrong).

The recordings go to ``$XDG_DATA_HOME/radiolack/recordings``, or
``~/.local/share/radiolack/recordings`` when that variable is unset or not
an absolute path, made when needed and readable by the user only, unless
the user chose another folder, which must exist. The list shows the latest
LIST_LIMIT recordings of the folder, the newest first: the regular files
named as Radiolack names them, which alone are downloaded or deleted.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

from .demod import MODES
from .store import shown_path

FOLDER_NAME = "recordings"
MIN_FREE_BYTES = 2 * 1024 ** 3      # a recording does not start, or stops, with less free on its disk
# A canonical 44-byte header: RIFF, the "fmt " chunk of 16-bit PCM, then the data chunk's header.
WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")
WAV_DATA_LIMIT = (0xFFFFFFFF - (WAV_HEADER.size - 8)) & ~1     # RIFF sizes are 32-bit: 4 GiB
LIST_LIMIT = 500                    # the recordings listed, the latest
WRITE_INTERVAL_S = 0.5              # the thread writes what came this often
SPACE_CHECK_S = 1.0                 # and checks the free space this often
PENDING_LIMIT_BYTES = 16 * 1024 * 1024      # audio waiting for the disk: more stops the recording
MAX_COUNT = 999                     # recordings of the same name, -2 to -999 after the first
FOLDER_PATH_LIMIT = 4096            # characters in a folder that a user gives
_SAMPLE_BYTES = 2                   # 16-bit mono
_NAME = re.compile(r"(?P<start>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})Z_(?P<mhz>\d{1,5}\.\d{6})MHz_(?P<mode>"
                   + "|".join(MODES) + r")(?:-(?P<count>[1-9]\d{0,2}))?\.wav")

log = logging.getLogger(__name__)

# What the engine is told from a recording's thread, as (recording, event, value):
# "opened" with the file's name, then "stopped" with (reason or None, detail).
Report = Callable[["AudioRecording", str, Any], None]


def default_folder(environ: Mapping[str, str] | None = None) -> Path:
    """The folder Radiolack records into unless the user chose another."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_DATA_HOME", "")
    if base and os.path.isabs(base):
        return Path(base) / "radiolack" / FOLDER_NAME
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".local" / "share" / "radiolack" / FOLDER_NAME


def recording_name(started: float, frequency_hz: float, mode: str, count: int = 1) -> str:
    """The name of a recording started at ``started`` (Unix time) on
    ``frequency_hz`` in ``mode``: the ``count``-th of that name."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%SZ", time.gmtime(started))
    suffix = "" if count == 1 else f"-{count}"
    return f"{stamp}_{frequency_hz / 1e6:.6f}MHz_{mode}{suffix}.wav"


class _Named(NamedTuple):
    """What a recording's name tells."""
    started: int            # Unix time
    frequency_hz: int
    mode: str
    count: int              # 1, then 2 and on for the names already taken


def _named(name: object) -> _Named | None:
    """What ``name`` tells, if Radiolack could have named a recording so;
    None otherwise."""
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        return None
    try:
        started = datetime.strptime(match["start"], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    except ValueError:      # such as a 13th month
        return None
    return _Named(int(started.timestamp()), int(match["mhz"].replace(".", "")), match["mode"],
                  int(match["count"] or 1))


def is_recording_name(name: object) -> bool:
    """Whether ``name`` is named as Radiolack names its recordings."""
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


def list_recordings(folder: Path, running: str | None, limit: int = LIST_LIMIT) -> dict[str, Any]:
    """The recordings of ``folder``, the latest ``limit`` of them, the newest
    first, ``running`` being the file of the recording that runs, how many
    others it holds, the free space and what went wrong reading it: the
    recordings message's fields (docs/design/protocol.md). For a worker
    thread."""
    found: dict[str, _Named] = {}
    problem = ""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                named = _named(entry.name)
                if named is not None and entry.is_file(follow_symlinks=False):
                    found[entry.name] = named
    except FileNotFoundError:
        pass        # nothing recorded there yet
    except OSError as error:
        problem = f"{shown_path(folder)} cannot be read: {_reason(error)}."
    # In the order they started.
    names = sorted(found, key=lambda name: (found[name].started, found[name].count), reverse=True)
    files = [described for name in names[:limit]
             if (described := _describe(folder / name, found[name], name == running)) is not None]
    return {"files": files, "more": max(0, len(names) - limit), "free_bytes": free_bytes(folder),
            "problem": problem}


def delete_recording(folder: Path, name: str) -> None:
    """Delete the recording ``name`` of ``folder``. Raises ValueError for a
    file that is not a recording, FileNotFoundError when it is no longer
    there and OSError when it cannot be deleted. For a worker thread."""
    if not is_recording_name(name):
        raise ValueError("not a recording")
    path = folder / name
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError(f"{name} is not a recording")
    os.unlink(path)


def open_recording(folder: Path, name: str) -> tuple[BinaryIO, int]:
    """The recording ``name`` of ``folder``, open for reading, and its size.
    Raises FileNotFoundError when there is no such recording, a link or
    another kind of file included, and OSError when it cannot be read. For
    a worker thread."""
    if not is_recording_name(name):
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


class AudioRecording:
    """A recording of the VFO's audio, written by a thread of its own into
    ``folder``, which it makes first if ``make_folder``, the default
    folder's way. ``started``, ``frequency_hz`` and ``mode`` name its file.
    The thread tells ``report`` of its file once made, and that it stopped,
    with the reason when it stopped by itself. ``file`` is for the engine,
    which sets it once told."""

    kind = "audio"

    def __init__(self, folder: Path, started: float, frequency_hz: float, mode: str, *,
                 make_folder: bool, report: Report) -> None:
        self.folder = folder
        self.started = started
        self.frequency_hz = frequency_hz
        self.mode = mode
        self.file: str | None = None
        self._make_folder = make_folder
        self._report = report
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # Shared with the thread, under the lock.
        self._chunks: list[bytes] = []
        self._pending = 0               # bytes in _chunks
        self._rate: int | None = None   # the WAV's rate, from the first audio
        self._stopping = False
        # The thread's own.
        self._descriptor: int | None = None
        self._data = 0                  # bytes of audio written
        self._thread = threading.Thread(target=self._run, name="radiolack-recording", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def write(self, pcm: bytes, rate_hz: int) -> bool:
        """Hand over 16-bit audio at ``rate_hz``, which the first audio sets
        for the whole recording. Returns False, taking nothing, when too much
        already waits for the disk."""
        with self._lock:
            if self._pending + len(pcm) > PENDING_LIMIT_BYTES:
                return False
            if self._rate is None:
                self._rate = rate_hz
            self._chunks.append(pcm)
            self._pending += len(pcm)
        return True

    def stop(self) -> None:
        """Write what came, then close the file. Safe to repeat."""
        with self._lock:
            self._stopping = True
            self._wake.notify()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the thread to end; returns whether it did."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        reason: str | None = None
        detail = ""
        try:
            reason = self._prepare()
            checked_s = time.monotonic()
            while reason is None:
                chunks, rate, stopping = self._take()
                if chunks:
                    if self._descriptor is None:
                        self._create(rate)
                    if not self._append(b"".join(chunks)):
                        reason = "full"
                        break
                if time.monotonic() - checked_s >= SPACE_CHECK_S:
                    checked_s = time.monotonic()
                    if self._free_bytes() < MIN_FREE_BYTES:
                        reason = "space"
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

    def _take(self) -> tuple[list[bytes], int | None, bool]:
        """What came, once WRITE_INTERVAL_S passed or a stop was asked, and
        the rate, and whether to stop."""
        with self._lock:
            if not self._stopping:
                self._wake.wait(WRITE_INTERVAL_S)
            chunks, self._chunks, self._pending = self._chunks, [], 0
            return chunks, self._rate, self._stopping

    def _create(self, rate_hz: int | None) -> None:
        """Make the file, under the first name not taken, with its header."""
        if rate_hz is None:
            raise RuntimeError("audio came without its rate")
        for count in range(1, MAX_COUNT + 1):
            name = recording_name(self.started, self.frequency_hz, self.mode, count)
            try:
                descriptor = os.open(self.folder / name,
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except FileExistsError:
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


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _describe(path: Path, named: _Named, running: bool) -> dict[str, Any] | None:
    """A recording's entry in the list; None once it is no longer there."""
    entry: dict[str, Any] = {"name": path.name, "kind": "audio", "started": named.started,
                             "frequency_hz": named.frequency_hz, "mode": named.mode, "bytes": None, "seconds": None,
                             "running": running}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return entry
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        entry["bytes"] = info.st_size
        entry["seconds"] = wav_seconds(os.pread(descriptor, WAV_HEADER.size, 0), info.st_size)
    except OSError:
        pass
    finally:
        os.close(descriptor)
    return entry


def _reason(error: OSError) -> str:
    return error.strerror or str(error)
