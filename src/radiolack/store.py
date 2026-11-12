"""Settings and bookmarks on disk.

Radiolack keeps them in a folder of their own: ``$XDG_CONFIG_HOME/radiolack``,
or ``~/.config/radiolack`` when that variable is unset or not an absolute path
(XDG Base Directory Specification). Each file there is a JSON object with a
format version: ``settings.json`` holds each receiver's settings and the
interfaces' preferences, ``bookmarks.json`` the bookmarks and
``observations.json`` what detection found (observations.py).

A file is written whole to a temporary file beside it, then renamed over it:
a crash leaves the previous file or the new one, never half of one. A file
that this version cannot read is renamed aside, never overwritten, and
Radiolack starts from its defaults; a file that cannot be opened, or that
comes from a newer Radiolack, is left alone and not written until the next
start. The store notes these problems, and failed writes, for the interfaces
to show: disk errors stay apart from receiver errors. A failed write is tried
again at the next save.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
SETTINGS_FILE = "settings.json"
BOOKMARKS_FILE = "bookmarks.json"
OBSERVATIONS_FILE = "observations.json"
_MAX_FILE_BYTES = 4 * 1024 * 1024       # far more than Radiolack writes

log = logging.getLogger(__name__)


def settings_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The folder Radiolack keeps its settings in."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME", "")
    if base and os.path.isabs(base):
        return Path(base) / "radiolack"
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".config" / "radiolack"


class Store:
    """The settings folder. ``load`` reads a file once, at the start; ``save``
    writes it whole. ``save`` may run in a worker thread, one at a time."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._notes: dict[str, str] = {}    # what happened to a file at the start
        self._errors: dict[str, str] = {}   # why the latest write of a file failed
        self._kept: set[str] = set()        # files left alone: not written until the next start

    @property
    def location(self) -> str:
        """The folder, as the interfaces show it."""
        try:
            return f"~/{self.directory.relative_to(Path.home()).as_posix()}"
        except (ValueError, RuntimeError):     # outside the home folder, or no home folder
            return str(self.directory)

    @property
    def saving(self) -> bool:
        """Whether changes reach the disk: no file is left alone and the
        latest writes worked."""
        return not self._kept and not self._errors

    @property
    def problem(self) -> str:
        """What went wrong with the files, in sentences; "" when nothing did."""
        return " ".join([*self._errors.values(), *self._notes.values()])

    def load(self, name: str) -> dict[str, Any]:
        """The content of file ``name`` without its version: empty when the
        file does not exist or cannot be read, which the store then notes."""
        path = self.directory / name
        try:
            with path.open("rb") as file:
                data = file.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as error:
            # Writing over a file that could not be read could lose it.
            self._kept.add(name)
            self._note(name, f"{name} cannot be read ({_reason(error)}): Radiolack started without it "
                             "and does not save it until the next start.")
            return {}
        try:
            if len(data) > _MAX_FILE_BYTES:
                raise ValueError("the file is too large")
            content = json.loads(data)
            if not isinstance(content, dict):
                raise ValueError("not a JSON object")
            version = content.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("no format version")
        except (ValueError, RecursionError) as error:     # bad UTF-8 or JSON included
            self._set_aside(name, str(error) or type(error).__name__)
            return {}
        if version > FORMAT_VERSION:
            self._kept.add(name)
            self._note(name, f"{name} comes from a newer Radiolack: it is left as it is, and not "
                             "saved until the next start.")
            return {}
        return {key: value for key, value in content.items() if key != "version"}

    def save(self, name: str, content: Mapping[str, Any]) -> bool:
        """Write file ``name`` whole; returns whether it was written. A file
        left alone at the start is not written."""
        if name in self._kept:
            return False
        text = json.dumps({"version": FORMAT_VERSION, **content}, indent=2, ensure_ascii=False) + "\n"
        temporary: str | None = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.directory)
            with open(descriptor, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.directory / name)
            temporary = None
            _sync_directory(self.directory)
        except OSError as error:
            message = f"{name} cannot be saved: {_reason(error)}."
            if self._errors.get(name) != message:
                log.warning("%s", message)
            self._errors[name] = message
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        self._errors.pop(name, None)
        return True

    def _set_aside(self, name: str, reason: str) -> None:
        """Rename an unreadable file, so that saving does not lose it."""
        path = self.directory / name
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = path.with_name(f"{name}.invalid-{stamp}")
        count = 1
        while kept.exists():
            count += 1
            kept = path.with_name(f"{name}.invalid-{stamp}-{count}")
        try:
            os.rename(path, kept)
        except OSError as error:
            self._kept.add(name)
            self._note(name, f"{name} cannot be read ({reason}) nor renamed ({_reason(error)}): Radiolack "
                             "started without it and does not save it until the next start.")
            return
        self._note(name, f"{name} could not be read ({reason}): it was kept as {kept.name}, and "
                         "Radiolack started without it.")

    def _note(self, name: str, message: str) -> None:
        log.warning("%s", message)
        self._notes[name] = message


def _sync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` last through a crash, where the system allows."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _reason(error: OSError) -> str:
    return error.strerror or str(error)
