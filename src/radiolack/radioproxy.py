"""A radio that runs in a process of its own, as Radiolack's process sees
it (docs/design/later-milestones.md, section 5, Processes; radioproc.py
for the radio's process).

``RadioProcess`` stands in for ``Radio`` in the engine and the server: it
starts the radio's process, sends it the interfaces' commands and the
copies of what the radios share, and keeps the radio's states, summary and
settings as the process last sent them, so that an interface that turns to
the radio gets them at once. What the process sends an interface, it
hands to that interface. A command's refusal comes later than a radio in
Radiolack's process gives it, to the interface that sent it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .radio import source_summary
from .radioproc import CALLS, Channel
from .report import Report, check_page, receiver_line
from .scanners import Signal
from .soapysearch import Device, device as soapy_device

if TYPE_CHECKING:
    from .engine import Engine, Source, Subscription

log = logging.getLogger(__name__)

CLOSE_S = 10.0      # for the radio's process to release its receiver and end, before it is killed
KILLED_S = 5.0      # for a killed process to end


class _SourceMirror:
    """The radio's source as its process last described it: what the
    engine reads of a radio's source."""

    def __init__(self, kind: str, reopen: dict[str, Any], settings_key: str) -> None:
        self.kind = kind
        self.reopen = reopen
        self.settings_key = settings_key
        self.given: frozenset[str] = frozenset()


class RadioProcess:
    """A radio in a process of its own (see the module's docstring). It
    reads ``source``'s receiver, a source that has read nothing and that
    it closes: its process opens the receiver again, from ``reopen``."""

    def __init__(self, engine: Engine, source: Source, *, radio_id: int, saved_scanner: object = None,
                 frame_rate: float) -> None:
        self.id = radio_id
        self._engine = engine
        self.source = _SourceMirror(source.kind, dict(source.reopen), source.settings_key)
        self._summary: dict[str, Any] = {"id": radio_id, **source_summary(source), "scan": None, "recording": []}
        source.close()      # it never read: nothing to release
        self.clients: set[Subscription] = set()
        self._states: dict[str, list[Any]] = {}     # each state's message, for local and remote clients
        self._receiver: dict[str, Any] = {}
        self._scanner: Any = saved_scanner
        self._running_files: set[str] = set()
        self._recording_runs = False
        self._soapy: Device | None = None
        self._start = {"op": "start", "id": radio_id, "source": self.source.reopen, "scanner": saved_scanner,
                       "frame_rate": frame_rate}
        self._waiting: list[tuple[dict[str, Any], bytes]] = []     # what to send once the process runs
        self._channel: Channel | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Future[None] | None = None
        self._closed = False
        self.failure: str | None = None

    # The radio's states, as its process last sent them; None until it did.

    def _state(self, kind: str, remote: bool = False) -> dict[str, Any] | None:
        message = self._states.get(kind)
        if message is None:
            return None
        return message[1] if remote and message[1] is not None else message[0]

    def state(self, remote: bool = False) -> dict[str, Any] | None:
        return self._state("source", remote)

    def demod_state(self, acknowledged: int = 0) -> dict[str, Any] | None:
        state = self._state("demod")
        return None if state is None else {**state, "acknowledged": acknowledged}

    def scanner_state(self) -> dict[str, Any] | None:
        return self._state("scanner")

    def peaks_state(self) -> dict[str, Any] | None:
        return self._state("peaks")

    def recording_state(self, remote: bool = False) -> dict[str, Any] | None:
        return self._state("recording", remote)

    def devices_state(self, remote: bool = False) -> dict[str, Any] | None:
        return self._state("devices", remote)

    def calibration_state(self) -> dict[str, Any] | None:
        return self._state("calibration")

    def summary(self) -> dict[str, Any]:
        return dict(self._summary)

    def receiver_settings(self) -> dict[str, Any]:
        return dict(self._receiver)

    def scanner_settings(self) -> Any:
        return self._scanner

    def running_files(self) -> set[str]:
        return set(self._running_files)

    def recording_runs(self) -> bool:
        return self._recording_runs

    def soapy_in_use(self) -> Device | None:
        return self._soapy

    # The interfaces that show it.

    def attach(self, subscription: Subscription) -> None:
        self.clients.add(subscription)
        self._post({"op": "attach", "client": id(subscription), "remote": subscription.remote,
                    "audio": subscription.audio, "fine": subscription.fine})

    def detach(self, subscription: Subscription) -> None:
        self.clients.discard(subscription)
        self._post({"op": "detach", "client": id(subscription)})

    def set_audio(self, subscription: Subscription, on: bool) -> None:
        subscription.set_audio(on)
        self._post({"op": "audio", "client": id(subscription), "on": on})

    def set_fine(self, subscription: Subscription, on: bool) -> None:
        subscription.set_fine(on)
        self._post({"op": "fine", "client": id(subscription), "on": on})

    # The interfaces' commands, which the radio's process answers.

    def configure(self, changes: Mapping[str, object]) -> None:
        self._call("configure", "source", changes, share_receivers=True)

    def configure_demod(self, changes: Mapping[str, object], subscription: Subscription | None = None,
                        change_id: int | None = None) -> None:
        self._call("configure_demod", "demod", changes, change_id, subscription=subscription)

    def configure_scanner(self, changes: Mapping[str, object]) -> None:
        self._call("configure_scanner", "scanner", changes)

    def configure_recording(self, changes: Mapping[str, object], subscription: Subscription | None = None) -> None:
        self._call("configure_recording", "recording", changes, subscription=subscription)

    def measure_carrier(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        self._call("measure_carrier", "carrier", changes, subscription=subscription)

    def calibrate(self, changes: Mapping[str, object]) -> None:
        self._call("calibrate", "calibration", changes)

    def configure_devices(self, changes: Mapping[str, object]) -> None:
        if changes == {"search": True}:
            self._engine.start_search()     # Radiolack's process alone searches
            return
        self._call("configure_devices", "devices", changes, share_receivers=True)

    def configure_rtl_devices(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        self._call("configure_rtl_devices", "rtl_devices", changes, subscription=subscription,
                   share_receivers=True)

    def report(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """A problem report, made here, where the radios' log records are
        kept (Radio.report)."""
        for name in changes:
            if name != "page":
                raise ValueError(f"unknown report field: {str(name)[:40]}")
        made = Report.now(receiver_line(self.state() or {"kind": self.source.kind, "status": "opening"}),
                          check_page(changes.get("page", "")))
        subscription.reply({"type": "report", "text": made.text(), "issue_url": made.issue_url()})

    def end_kept_scan(self, ended: str) -> bool:
        """The process ends its kept scan, then sends it: never at once."""
        self._post({"op": "call", "method": "end_kept_scan", "args": [ended]})
        return False

    # What the radios share, sent again whenever it changes.

    def share(self, **shared: Any) -> None:
        self._post({"op": "share", **shared})

    def watch_bookmarks(self) -> None:
        self.share(bookmarks=self._engine.ticked_copy())

    def follow_devices(self) -> None:
        self.share(search=self._engine.search_copy())

    # Its process.

    def close(self) -> None:
        """Ask the process to end, releasing the receiver. Safe to repeat."""
        if self._closed:
            return
        self._closed = True
        self._post({"op": "close"})

    async def wait_closed(self) -> None:
        """Return once the process has ended, killed if it has not CLOSE_S
        after it was asked to, and what it sent before has been handled."""
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), CLOSE_S)
        except asyncio.TimeoutError:
            process = self._process
            if process is not None and process.returncode is None:
                log.warning("radio %d's process did not end in time: it is killed", self.id)
                process.kill()
            await asyncio.wait_for(asyncio.shield(task), KILLED_S)

    async def run(self) -> None:
        """Start the process, unless closed, and hand on what it sends until
        it ends; cancelled, the process goes on until it is closed, and what
        it sends meanwhile is still handled, its latest settings included.
        One that ends unasked takes the radio away (Engine.radio_failed)."""
        if self._task is None:
            if self._closed:
                return
            self._task = asyncio.ensure_future(self._serve())
        await asyncio.shield(self._task)

    async def _serve(self) -> None:
        ours, theirs = socket.socketpair()
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "radiolack.radioproc", str(theirs.fileno()),
                pass_fds=(theirs.fileno(),), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)})
        except OSError as error:
            ours.close()
            self._fail(f"its process cannot start: {error.strerror or error}")
            return
        finally:
            theirs.close()
        reader, writer = await asyncio.open_connection(sock=ours)
        channel = self._channel = Channel(reader, writer)
        channel.post({**self._start, **self._engine.shared_copy(self)})
        for header, data in self._waiting:
            channel.post(header, data)
        self._waiting.clear()
        try:
            while (message := await channel.receive()) is not None:
                self._handle(*message)
        except ValueError as error:
            log.error("radio %d's process sent what it may not: %s", self.id, error)
            self._process.kill()
        finally:
            channel.close()
        await self._process.wait()
        if not self._closed:
            self._fail(self.failure or f"its process ended ({self._process.returncode})")

    def _post(self, header: dict[str, Any], data: bytes = b"") -> None:
        if self._channel is None:
            self._waiting.append((header, data))
        else:
            self._channel.post(header, data)

    def _call(self, method: str, command: str, *args: Any, subscription: Subscription | None = None,
              share_receivers: bool = False) -> None:
        assert method in CALLS
        if self._closed:
            raise ValueError("this receiver is being removed")
        if share_receivers:     # the receiver it opens starts from the settings saved for it
            self.share(receivers=self._engine.receivers, radios=self._engine.others(self))
        self._post({"op": "call", "method": method, "command": command, "args": list(args),
                    "client": None if subscription is None else id(subscription)})

    def _client(self, client_id: object) -> Subscription | None:
        return next((subscription for subscription in self.clients if id(subscription) == client_id), None)

    def _handle(self, header: dict[str, Any], data: bytes) -> None:
        op = header["op"]
        if op == "data":
            put = {"frame": "put_frame", "audio": "put_audio", "fine": "put_fine"}.get(header.get("kind"))
            for client_id in header.get("clients", ()):
                subscription = self._client(client_id)
                if put is not None and subscription is not None:
                    getattr(subscription, put)(data)
        elif op == "states":
            self._states.update(header["states"])
            for kind, ids in header.get("clients", {}).items():
                for client_id in ids:
                    subscription = self._client(client_id)
                    if subscription is not None:
                        subscription.changed(kind)
            if "devices" in header["states"] or "source" in header["states"]:
                self._engine.radios_changed()
        elif op == "ack":
            subscription = self._client(header.get("client"))
            if subscription is not None:
                subscription.acknowledged = header["id"]
                subscription.changed("demod")
        elif op in ("reply", "error"):
            subscription = self._client(header.get("client"))
            if subscription is not None:
                subscription.reply(header["message"] if op == "reply" else
                                   {"type": "error", "command": header.get("command"), "message": header["message"]})
        elif op == "summary":
            self._summary = {**header["summary"], "id": self.id}
            self.source = _SourceMirror(header["kind"], header["reopen"], header["settings_key"])
            self._running_files = set(header["running_files"])
            self._recording_runs = bool(header["recording_runs"])
            soapy = header.get("soapy")
            self._soapy = None if soapy is None else soapy_device(*soapy)
            self._engine.radios_changed()
        elif op == "settings":
            self._receiver, self._scanner = header["receiver"], header["scanner"]
            self._engine.follow_settings(self, header.get("receivers", {}), header.get("reopen", {}))
        elif op == "kept":
            self._engine.follow_kept_scan(self, header["scan"], bool(header.get("soon")))
        elif op == "observe":
            found = [Signal(*entry) for entry in header["found"]]
            self._engine.follow_observations(found, header["now"], header["hour"])
        elif op == "search":
            self._engine.start_search()
        elif op == "folder":
            subscription = self._client(header.get("client"))
            try:
                self._engine.choose_recordings_folder(header.get("value"), subscription)
            except ValueError as error:
                if subscription is not None:
                    subscription.reply({"type": "error", "command": "recording", "message": str(error)})
        elif op == "log":
            logging.getLogger(str(header.get("name", "radiolack"))).log(
                int(header.get("level", logging.WARNING)), "radio %d: %s", self.id, header.get("message"))
        elif op == "failed":
            self.failure = str(header.get("message"))
        elif op not in ("ready", "closed"):
            log.warning("radio %d's process sent an unknown operation: %s", self.id, op[:40])

    def _fail(self, reason: str) -> None:
        log.error("radio %d stopped: %s", self.id, reason)
        self._closed = True
        self._engine.radio_failed(self, reason)
