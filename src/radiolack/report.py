"""Problem reports: what a user may share when Radiolack misbehaves.

Radiolack sends nothing by itself. Its interface on this computer asks the
engine for a report, shows it, and offers to copy it or to open GitHub's
page for a new issue with it, where the user describes the problem and
submits it themselves. A report gives Radiolack's version, Python's and the
libraries', the system, the window's toolkit, what the interface tells of
itself, the receiver's kind and state, how long Radiolack has run and its
latest warnings and errors, which this module keeps in memory from the
start, with the home folder written "~".
"""

from __future__ import annotations

import collections
import logging
import platform
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
import numpy

from . import PROJECT_URL, __version__
from .updates import PACKAGE_DIR

ISSUES_URL = f"{PROJECT_URL}/issues/new"
RECENT_MESSAGES = 50    # the latest warnings and errors that a report gives
MAX_MESSAGE = 500       # characters of one of them, beyond which it is cut
MAX_PAGE = 300          # characters of what the interface tells of itself
# GitHub refuses the addresses of about 8 KB and longer: the new issue's
# address leaves out the oldest messages beyond this; a copy keeps them.
MAX_ISSUE_URL = 7000
LOGGER = "radiolack"    # the logger whose messages a report gives
_ISSUE_START = ("**What happened**\n\n"
                "(What you did, what you expected and what Radiolack did instead.)\n\n"
                "**Report from Radiolack**\n\n")

_started = time.monotonic()
_recent: RecentMessages | None = None
_window: str | None = None


class RecentMessages(logging.Handler):
    """The latest warnings and errors, in memory, each on one line, with its
    time in UTC; an error's exception, if any, by its type and message."""

    def __init__(self, size: int = RECENT_MESSAGES) -> None:
        super().__init__(logging.WARNING)
        self._lines: collections.deque[str] = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            error = record.exc_info[1] if record.exc_info else None
            if error is not None:
                text += f" ({type(error).__name__}: {error})"
            when = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(record.created))
            line = " ".join(f"{when} {record.levelname} {record.name}: {text}".split())
        except Exception:   # as logging's own handlers: a message that cannot be written is no error of the program
            self.handleError(record)
            return
        self._lines.append(line if len(line) <= MAX_MESSAGE else line[:MAX_MESSAGE - 3] + "...")

    def lines(self) -> list[str]:
        """The messages kept, oldest first."""
        with self.lock:
            return list(self._lines)


class _ErrorOutput(logging.StreamHandler):
    """Writes on the error output of the moment, as Python does for a
    program that sets up no logging."""

    def __init__(self) -> None:
        super().__init__()
        self.setLevel(logging.WARNING)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def keep_recent_messages() -> RecentMessages:
    """Keep Radiolack's latest warnings and errors for the reports, from
    now on, once per process. Its logger then writes them on the error
    output itself, as Python did without a handler."""
    global _recent
    if _recent is None:
        logger = logging.getLogger(LOGGER)
        logger.addHandler(_ErrorOutput())
        _recent = RecentMessages()
        logger.addHandler(_recent)
    return _recent


def set_window(toolkit: str) -> None:
    """Name the desktop window's toolkit for the reports, such as
    "GTK 3.24.38, WebKitGTK 2.50.6"."""
    global _window
    _window = toolkit


@dataclass(frozen=True)
class Report:
    """A problem report: lines of facts, then the latest warnings and
    errors, oldest first, or None when this process keeps none."""

    facts: tuple[str, ...]
    messages: tuple[str, ...] | None

    @classmethod
    def now(cls, receiver: str, page: str = "") -> Report:
        """The report of this moment, with the ``receiver``'s kind and state
        and what the interface tells of itself, ``page``."""
        installed = " (Debian package)" if Path(__file__).parent.resolve() == PACKAGE_DIR.resolve() else ""
        facts = [f"Radiolack {__version__}{installed}",
                 f"Python {platform.python_version()}, aiohttp {aiohttp.__version__}, NumPy {numpy.__version__}",
                 f"System: {_system()}"]
        if _window is not None:
            facts.append(f"Window: {_window}")
        if page:
            facts.append(f"Interface: {page}")
        facts += [f"Receiver: {receiver}", f"Running for {_duration(time.monotonic() - _started)}"]
        messages = tuple(_recent.lines()) if _recent is not None else None
        return cls(tuple(_home_hidden(fact) for fact in facts),
                   None if messages is None else tuple(_home_hidden(line) for line in messages))

    def text(self, left_out: int = 0) -> str:
        """The report in plain text, without its ``left_out`` oldest messages."""
        lines = [*self.facts, ""]
        if self.messages is None:
            lines.append("Warnings and errors: not kept.")
        elif not self.messages:
            lines.append("No warnings or errors since Radiolack started.")
        else:
            lines.append("Latest warnings and errors (UTC):")
            if left_out:
                lines.append(f"({left_out} older left out: copy the report in Radiolack to give them all)")
            lines += self.messages[left_out:]
        return "\n".join(lines)

    def issue_url(self) -> str:
        """GitHub's page for a new issue, with the report as its description,
        under a heading where the user describes the problem: the oldest
        messages left out, as many as needed to keep the address under
        MAX_ISSUE_URL."""
        for left_out in range(len(self.messages or ()) + 1):
            body = f"{_ISSUE_START}```text\n{self.text(left_out)}\n```\n"
            url = f"{ISSUES_URL}?{urlencode({'body': body})}"
            if len(url) <= MAX_ISSUE_URL:
                break
        return url


def check_page(page: object) -> str:
    """What the interface tells of itself, as a report takes it: a line of
    text of at most MAX_PAGE characters. Raises ValueError otherwise."""
    if not isinstance(page, str) or len(page) > MAX_PAGE or not page.isprintable():
        raise ValueError(f"page must be a line of text of at most {MAX_PAGE} characters")
    return page


# What a direct sampling input uses while the receiver samples directly.
_DIRECT_INPUTS = {"q": "direct sampling, Q input", "i": "direct sampling, I input"}


def receiver_line(state: Mapping[str, Any]) -> str:
    """The receiver's kind and state from its source state, such as
    "rtl_tcp, waiting, tuner R820T, 2.048 MS/s (no answer)", with the input
    in use for shortwave once one is chosen and the converter in front of
    it: neither its address nor a frequency, its oscillator's included."""
    parts = [str(state.get("kind")), str(state.get("status"))]
    if state.get("tuner"):
        parts.append(f"tuner {state['tuner']}")
    hf_input = state.get("hf_input")
    if hf_input in ("q", "i", "tuner"):
        in_use = _DIRECT_INPUTS.get(hf_input, "the tuner") if state.get("direct") else "the tuner"
        parts.append(f"shortwave input {hf_input}, using {in_use}")
    if state.get("converter") in ("up", "down", "down_inverted"):
        parts.append(f"converter {state['converter']}")
    rate = state.get("sample_rate_hz")
    if isinstance(rate, (int, float)) and rate > 0:
        parts.append(f"{rate / 1e6:g} MS/s")
    line = ", ".join(parts)
    return f"{line} ({state['detail']})" if state.get("detail") else line


def _system() -> str:
    """The system's name and version, the kernel's and the machine's type."""
    try:
        name = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except OSError:
        name = ""
    kernel = f"{platform.system()} {platform.release()}, {platform.machine()}"
    return f"{name}, {kernel}" if name else kernel


def _duration(seconds: float) -> str:
    """``seconds`` in days, hours and minutes, such as "1 h 50 min"."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return " ".join(f"{count} {unit}" for count, unit in ((days, "d"), (hours, "h"), (minutes, "min")) if count)


def _home_hidden(text: str) -> str:
    """``text`` with the home folder written "~"."""
    home = str(Path.home())
    return text.replace(home, "~") if home.strip("/") else text
