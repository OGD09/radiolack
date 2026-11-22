"""The search for SoapySDR devices, whose findings the Receiver menu lists
(later-milestones.md, section 1, Any receiver, pull request 3).

Radiolack searches only the local USB drivers that it lists,
``LISTED_DRIVERS``, one at a time and each with ``driver=NAME`` alone:
never without a driver, as such a search runs every module's, SoapyRemote's
included, which asks avahi-daemon to search the network, and never
``rtlsdr``, as an RTL-SDR has the Receiver menu's entries of its own.
``tools/check_search.py`` found that none of the listed drivers' searches
makes a socket (scoping.md, spike 7). A search opens no receiver's stream,
but each module's search talks to the devices plugged in, through libusb or
the maker's library: some open a device to read its serial number.

A search runs in a thread of its own, one at a time, when this computer's
interface asks for it (the engine decides when): never at start, nor for a
remote client. It loads the SoapySDR module first; without it, nothing is
searched and the state says which package brings it. The driver of a
SoapySDR device in use is not searched again, so that no search disturbs
the device that streams: its devices stay as the previous search found
them, and the device in use is listed whatever the searches find.

What is kept of each device found is its driver, its serial number and its
label, as its module gives them: nothing else of its listing, such as a
position on the bus. The list lives in memory only; choosing a device opens
it by its driver and serial number (``Device.args``), which the engine then
saves as for any receiver.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .soapy import STOP_S, args_text, load_module

# The drivers searched, in this order. ``sdrplay`` joins them, as the last
# entry, once tools/check_search.py has checked its search on a computer
# with SDRplay's API and module installed (later-milestones.md, section 1,
# The drivers listed).
LISTED_DRIVERS = ("hackrf", "airspy", "airspyhf", "lime", "bladerf")
NEVER_SEARCHED = ("rtlsdr",)    # an RTL-SDR may be plugged in and in use: it has entries of its own
MAX_DEVICES = 64                # devices kept from one search at most
MAX_LABEL = 100                 # characters of a label
MAX_SERIAL = 64                 # characters of a serial number
# The search's states: none yet, one under way, its findings, a driver's
# search that failed, or the module missing.
STATUSES = ("never", "searching", "done", "failed", "unavailable")
_TEXT_CHARS = 200               # of a module's message, in a detail

log = logging.getLogger(__name__)

assert not set(LISTED_DRIVERS) & set(NEVER_SEARCHED)


@dataclass(frozen=True)
class Device:
    """A SoapySDR device as the Receiver menu lists it."""

    driver: str
    serial: str         # "" when its module gives none, or one that device arguments cannot carry
    label: str

    @property
    def args(self) -> str:
        """The device arguments that open it: its driver and serial number."""
        pairs = {"driver": self.driver}
        if self.serial:
            pairs["serial"] = self.serial
        return args_text(pairs)

    def describe(self, in_use: bool) -> dict[str, Any]:
        return {"driver": self.driver, "serial": self.serial, "label": self.label, "in_use": in_use}


def _clean(value: object, limit: int) -> str:
    """``value`` as one line of printable text, at most ``limit`` characters."""
    text = " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def device(driver: str, serial: object, label: object) -> Device:
    """A device from what its module or its source gives: a serial number
    that device arguments cannot carry, with a comma, an equals sign or
    more than MAX_SERIAL characters, is left out; the label, on one line,
    is its driver and serial number when the module gives none."""
    serial_text = str(serial or "").strip()
    if (len(serial_text) > MAX_SERIAL or not serial_text.isprintable()
            or any(char in serial_text for char in ",=")):
        serial_text = ""
    label_text = _clean(label or "", MAX_LABEL) or _clean(f"{driver} {serial_text}", MAX_LABEL)
    return Device(driver, serial_text, label_text)


def _message(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS - 1] + "…"


@dataclass(frozen=True)
class Found:
    """What one search found: the devices, the drivers whose search failed,
    with why, and, when nothing could be searched, why not."""

    devices: tuple[Device, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    problem: str = ""


def search(load: Callable[[], tuple[ModuleType | None, str]], drivers: tuple[str, ...]) -> Found:
    """Search each of ``drivers`` in turn, with its name alone, in the
    calling thread. A driver whose search raises is reported and the others
    are searched; without the module, nothing is."""
    soapy, problem = load()
    if soapy is None:
        return Found(problem=problem)
    devices: list[Device] = []
    failures: list[tuple[str, str]] = []
    for driver in drivers:
        if not driver or driver in NEVER_SEARCHED:
            raise ValueError(f"not a driver Radiolack searches: {driver!r}")
        try:
            listings = soapy.Device.enumerate({"driver": driver})
        except Exception as error:      # the module's own error, such as its library failing
            log.warning("the SoapySDR %s search failed: %s", driver, _message(error))
            failures.append((driver, _message(error)))
            continue
        for listing in listings:
            pairs: Mapping[str, Any] = dict(listing)
            found = device(driver, pairs.get("serial"), pairs.get("label"))
            if found not in devices and len(devices) < MAX_DEVICES:
                devices.append(found)
    return Found(tuple(devices), tuple(failures))


class DeviceSearch:
    """The engine's search for SoapySDR devices: one at a time, in a thread
    of its own (module docstring), and what the latest found, which
    ``describe`` gives as the devices state. ``on_change`` is called, in
    the event loop, whenever that state changes."""

    def __init__(self, drivers: tuple[str, ...] = LISTED_DRIVERS,
                 load: Callable[[], tuple[ModuleType | None, str]] = load_module, stop_s: float = STOP_S) -> None:
        if any(not driver or driver in NEVER_SEARCHED for driver in drivers):
            raise ValueError("a search names listed drivers only")
        self.drivers = tuple(drivers)
        self.on_change: Callable[[], None] | None = None
        self.status = "never"
        self.detail = ""
        self.devices: tuple[Device, ...] = ()
        self._before = (self.status, self.detail)     # the state before the search under way
        self._load = load
        self._stop_s = stop_s
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self.status == "searching"

    def begin(self) -> bool:
        """Mark a search as under way, unless one is or the search is
        closed: False then. ``run`` follows."""
        if self.running or self._closed:
            return False
        self._before = (self.status, self.detail)
        self._set("searching", "")
        return True

    def abandon(self) -> None:
        """Bring the state back to what it was before ``begin``, for a
        search that ``begin`` started but ``run`` never ended."""
        if self.running and not self._closed:
            self._set(*self._before)

    async def run(self, skip: Collection[str] = ()) -> None:
        """Search every listed driver but those of ``skip``, the drivers of
        the devices in use, whose devices stay as the previous search found
        them, in a thread of its own; the state then holds what it found.
        Cancelled, the thread goes on, and what it finds is dropped."""
        loop = asyncio.get_running_loop()
        done: asyncio.Future[Found] = loop.create_future()
        drivers = tuple(driver for driver in self.drivers if driver not in skip)

        def finish(found: Found) -> None:
            if not done.done():
                done.set_result(found)

        def target() -> None:
            try:
                found = search(self._load, drivers)
            except Exception as error:
                log.exception("the SoapySDR device search failed")
                found = Found(failures=tuple((driver, _message(error)) for driver in drivers))
            try:
                loop.call_soon_threadsafe(finish, found)
            except RuntimeError:        # the event loop has ended: nobody waits
                pass

        before = self._before
        self._thread = threading.Thread(target=target, name="radiolack-soapy-search", daemon=True)
        try:
            self._thread.start()
            found = await done
        except BaseException:
            if not self._closed:
                self._set(*before)     # nothing found: the state as it was
            raise
        if self._closed:
            return
        if found.problem:
            self.devices = ()
            self._set("unavailable", found.problem)
            return
        kept = tuple(entry for entry in self.devices if entry.driver in skip)
        self.devices = kept + tuple(entry for entry in found.devices if entry not in kept)
        detail = "; ".join(f"the {driver} search failed: {text}" for driver, text in found.failures)
        self._set("failed" if found.failures else "done", detail)

    def find(self, driver: object, serial: object) -> Device | None:
        """The device found with ``driver`` and ``serial``, or None."""
        return next((entry for entry in self.devices if (entry.driver, entry.serial) == (driver, serial)), None)

    def describe(self, in_use: Device | None) -> list[dict[str, Any]]:
        """The devices to list: those found, and the device in use, first,
        whatever the searches found."""
        entries = [entry.describe(False) for entry in self.devices
                   if in_use is None or (entry.driver, entry.serial) != (in_use.driver, in_use.serial)]
        if in_use is not None:
            entries.insert(0, in_use.describe(True))
        return entries

    def close(self) -> None:
        """Stop for good: a search under way finishes in its thread, and
        what it finds is dropped."""
        self._closed = True

    async def wait_closed(self) -> None:
        """Return once a search under way has ended, or ``stop_s`` has
        passed for it, which the log then says."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, self._stop_s)
            if thread.is_alive():
                log.warning("the SoapySDR device search did not end within %g s", self._stop_s)

    def _set(self, status: str, detail: str) -> None:
        if (status, detail) != (self.status, self.detail):
            self.status, self.detail = status, detail
            if self.on_change is not None:
                self.on_change()
