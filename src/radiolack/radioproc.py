"""A radio in a process of its own (docs/design/later-milestones.md,
section 5, Processes): the messages between Radiolack's process and a
radio's, and the radio's process, ``python3 -m radiolack.radioproc FD``.

Radiolack's process starts one for each radio after the first
(radioproxy.py) and hands it one end of a socket pair, ``FD``. The radio's
process opens the radio's receiver and runs ``Radio``, the radio's code
unchanged, against ``ChildEngine``, which stands in for the engine: it
holds copies of what the radios share, which Radiolack's process sends
whenever they change (the bookmarks, the band plan, the receivers' saved
settings, the other radios' receivers, the search for SoapySDR devices,
the recordings folder), and sends back what the radio changes there (the
signals it found, its kept scan, its receiver's settings). ``ClientProxy``
stands in for each interface that shows the radio: what the radio sends an
interface crosses once, whatever the number of interfaces, with the ids of
those it goes to.

Each message is a JSON header and binary data, framed by their lengths
(``Channel``). Nothing reaches the network. The radio's process ends once
Radiolack's asks it to, its socket closes or it receives SIGTERM, having
released its receiver as a radio does: the antenna power off and rtl_tcp
stopped. It ignores SIGINT, the terminal's Ctrl-C, which Radiolack's
process handles. Its log records go to Radiolack's, with its radio's
number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .bandplan import load_plan
from .bookmarks import Bookmark
from .keptscans import KeptScans
from .scanners import Signal
from .soapysearch import Device, DeviceSearch

log = logging.getLogger(__name__)

FRAME = struct.Struct(">II")        # the header's length, then the data's
MAX_HEADER = 16 * 1024 * 1024       # bytes: the largest state, a band plan's bookmarks, is far smaller
MAX_DATA = 16 * 1024 * 1024         # bytes: a frame or an audio block is far smaller
BEHIND_BYTES = 4 * 1024 * 1024      # what waits to be written before data is left out
RECORDING_CLOSE_S = 10.0            # as the engine's, for a recording to reach the disk
# The radio's methods that Radiolack's process may call, with whether they
# take the interface that asked, after their arguments.
CALLS = {"configure": False, "configure_demod": True, "configure_scanner": False,
         "configure_recording": True, "measure_carrier": True, "calibrate": False,
         "configure_devices": False, "configure_rtl_devices": True, "end_kept_scan": False}
# The states whose message differs for a remote client.
REMOTE_VARIANTS = ("source", "recording", "devices")


class Channel:
    """One end of the socket pair: messages as a JSON header and binary
    data. Writing never waits: what cannot be written yet waits in the
    transport, and data that ``post`` may leave out is left out while
    BEHIND_BYTES wait, as for an interface behind."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.left_out = 0       # messages left out for a reader behind

    def post(self, header: Mapping[str, Any], data: bytes = b"", *, droppable: bool = False) -> None:
        """Send one message, unless the channel is closing, or ``droppable``
        while the other end is behind."""
        if self._writer.is_closing():
            return
        if droppable and self._writer.transport.get_write_buffer_size() > BEHIND_BYTES:
            self.left_out += 1
            return
        text = json.dumps(header, separators=(",", ":")).encode()
        self._writer.write(FRAME.pack(len(text), len(data)) + text + data)

    async def receive(self) -> tuple[dict[str, Any], bytes] | None:
        """The next message, or None once the other end closed. Raises
        ValueError for a message that breaks the framing."""
        try:
            lengths = await self._reader.readexactly(FRAME.size)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        header_length, data_length = FRAME.unpack(lengths)
        if header_length > MAX_HEADER or data_length > MAX_DATA:
            raise ValueError("a message too long for a radio's channel")
        try:
            text = await self._reader.readexactly(header_length)
            data = await self._reader.readexactly(data_length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        header = json.loads(text)
        if not isinstance(header, dict) or not isinstance(header.get("op"), str):
            raise ValueError("a radio's message without its operation")
        return header, data

    async def drain(self) -> None:
        """Wait until what was posted has been handed to the system."""
        try:
            await self._writer.drain()
        except ConnectionError:
            pass

    def close(self) -> None:
        self._writer.close()


class ClientProxy:
    """An interface that shows the radio, as the radio sees it in its own
    process: what it sends to it goes to Radiolack's process, which hands
    it to the interface (``Subscription``)."""

    def __init__(self, outlet: Outlet, client_id: int, remote: bool, audio: bool, fine: bool) -> None:
        self._outlet = outlet
        self.id = client_id
        self.remote = remote
        self.audio = audio
        self.fine = fine
        self._acknowledged = 0
        self.radio: Any = None

    @property
    def acknowledged(self) -> int:
        return self._acknowledged

    @acknowledged.setter
    def acknowledged(self, change_id: int) -> None:
        """The interface's demodulation change ``change_id`` is handled."""
        self._acknowledged = change_id
        self._outlet.acknowledge(self, change_id)

    def changed(self, kind: str) -> None:
        self._outlet.changed(self, kind)

    def reply(self, message: dict[str, Any]) -> None:
        self._outlet.flush()        # after the states it follows
        self._outlet.channel.post({"op": "reply", "client": self.id, "message": message})

    def put_frame(self, frame: bytes) -> None:
        self._outlet.data("frame", self, frame)

    def put_audio(self, frame: bytes) -> None:
        if self.audio:
            self._outlet.data("audio", self, frame)

    def put_fine(self, frame: bytes) -> None:
        if self.fine:
            self._outlet.data("fine", self, frame)

    def set_audio(self, on: bool) -> None:
        self.audio = on

    def set_fine(self, on: bool) -> None:
        self.fine = on


class Outlet:
    """What the radio sends its interfaces, gathered until the event loop's
    next turn: each state changed once, computed once for local and remote
    clients, and each frame once with the interfaces it goes to."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.states: dict[str, Callable[[bool], dict[str, Any] | None]] = {}   # set by the radio's process
        self._due: dict[str, set[int]] = {}
        self._data: list[tuple[str, bytes, list[int]]] = []
        self._acks: dict[int, int] = {}     # a client's id: the latest demodulation change handled
        self._scheduled = False

    def acknowledge(self, client: ClientProxy, change_id: int) -> None:
        """Acknowledge ``change_id`` once the settings it changed are sent."""
        self._acks[client.id] = change_id
        self._schedule()

    def changed(self, client: ClientProxy, kind: str) -> None:
        self._due.setdefault(kind, set()).add(client.id)
        self._schedule()

    def data(self, kind: str, client: ClientProxy, data: bytes) -> None:
        last = self._data[-1] if self._data else None
        if last is not None and last[0] == kind and last[1] is data:
            last[2].append(client.id)
        else:
            self._data.append((kind, data, [client.id]))
        self._schedule()

    def publish(self, kinds: Sequence[str], clients: Sequence[int]) -> None:
        """Send ``kinds`` now, as they stand, marking them changed for
        ``clients``."""
        for kind in kinds:
            self._due.setdefault(kind, set()).update(clients)
        self.flush()

    def flush(self) -> None:
        self._scheduled = False
        if self._due:
            states: dict[str, list[Any]] = {}
            for kind in self._due:
                describe = self.states.get(kind)
                if describe is None:
                    continue
                local = describe(False)
                states[kind] = [local, describe(True) if kind in REMOTE_VARIANTS else None]
            due, self._due = self._due, {}
            self.channel.post({"op": "states", "states": states,
                               "clients": {kind: sorted(ids) for kind, ids in due.items() if kind in states}})
        acks, self._acks = self._acks, {}
        for client_id, change_id in acks.items():   # after the settings they acknowledge
            self.channel.post({"op": "ack", "client": client_id, "id": change_id})
        data, self._data = self._data, []
        for kind, block, ids in data:
            self.channel.post({"op": "data", "kind": kind, "clients": ids}, block, droppable=True)

    def _schedule(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            asyncio.get_running_loop().call_soon(self.flush)


class Followed(dict):  # type: ignore[type-arg]
    """A dict whose changes are noted, to send them to Radiolack's process:
    the receivers' saved settings and the receivers used last, which the
    radio changes as it switches receivers."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__(values)
        self.changes: dict[str, Any] = {}       # None for a key removed

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.changes[key] = value

    def pop(self, key: str, *default: Any) -> Any:  # type: ignore[override]
        self.changes[key] = None
        return super().pop(key, *default)

    def taken(self) -> dict[str, Any]:
        changes, self.changes = self.changes, {}
        return changes


class OtherSource:
    """Another radio's receiver, as the copies tell it: enough for a radio
    to refuse that receiver and to name the radio that reads it."""

    def __init__(self, described: Mapping[str, Any]) -> None:
        self.kind = described.get("kind")
        self.reopen = described.get("reopen")
        self.settings_key = described.get("settings_key")


class OtherRadio:
    def __init__(self, described: Mapping[str, Any]) -> None:
        self.id = described.get("id")
        self.source = OtherSource(described)


class FollowedSearch(DeviceSearch):
    """The search for SoapySDR devices that Radiolack's process runs, as
    its copies tell it: a radio's process never searches."""

    def __init__(self, described: Mapping[str, Any]) -> None:
        super().__init__(drivers=tuple(described.get("drivers", ())))
        self.follow(described)

    def follow(self, described: Mapping[str, Any]) -> None:
        self.status = str(described.get("status", "never"))
        self.detail = str(described.get("detail", ""))
        self.devices = tuple(Device(str(driver), str(serial), str(label))
                             for driver, serial, label in described.get("devices", ()))


class FollowedObservations:
    """The observations, which Radiolack's process keeps: the signals found
    go there."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    def record(self, found: Sequence[Signal], now: float, hour: int) -> bool:
        self._channel.post({"op": "observe", "now": now, "hour": hour,
                            "found": [[signal.frequency_hz, signal.width_hz, signal.margin_db, signal.last_s]
                                      for signal in found]})
        return True


class TickedBookmarks:
    """The bookmarks that the memory scan goes through, under the ids that
    Radiolack's process gives them: what a radio reads of the bookmarks."""

    def __init__(self, described: object) -> None:
        self._ticked: list[tuple[int, Bookmark]] = []
        for entry in described if isinstance(described, list) else ():
            try:
                fields = {name: entry[name] for name in ("frequency_hz", "name", "mode", "scan", "category")}
                self._ticked.append((int(entry["id"]), Bookmark(**fields)))
            except (KeyError, TypeError, ValueError) as error:
                log.warning("a bookmark from Radiolack is left out: %s", error)

    def ticked(self) -> list[tuple[int, Bookmark]]:
        return list(self._ticked)


class ChildEngine:
    """What a radio needs of the engine, in the radio's own process: the
    copies of what the radios share, and what sends the radio's changes
    there to Radiolack's process (see the module's docstring)."""

    def __init__(self, channel: Channel, outlet: Outlet, start: Mapping[str, Any], sources: Any) -> None:
        self._channel = channel
        self._outlet = outlet
        self.radio: Any = None
        self.sources = sources
        self.rtl_listing = getattr(sources, "rtl_devices", None) if "rtl_sdr" in sources.kinds else None
        self.receivers = Followed(start.get("receivers", {}))
        self.reopen = Followed(start.get("reopen", {}))
        self.closing: set[asyncio.Future[None]] = set()
        self.unfinished: set[Any] = set()
        self.kept = KeptScans()     # this radio's scan alone: Radiolack's process keeps them all
        self.observations = FollowedObservations(channel)
        self.bookmarks = TickedBookmarks([])
        self.plan = load_plan(str(start.get("band_plan")))
        self.device_search: FollowedSearch | None = None
        self.default_recordings: Path | None = None
        self._recordings: Path | None = None
        self._others: list[OtherRadio] = []
        self.stopped = asyncio.Event()
        self.failure: str | None = None
        self.share(start)

    @property
    def radios(self) -> list[Any]:
        return [*self._others, self.radio]

    def share(self, shared: Mapping[str, Any]) -> None:
        """Take the copies that ``shared`` gives of what the radios share."""
        if "bookmarks" in shared:
            self.bookmarks = TickedBookmarks(shared["bookmarks"])
        if "band_plan" in shared:
            self.plan = load_plan(str(shared["band_plan"]))
        if "receivers" in shared:
            dict.update(self.receivers, shared["receivers"])
        if "radios" in shared:
            self._others = [OtherRadio(described) for described in shared["radios"]]
        if "search" in shared:
            if shared["search"] is None:
                self.device_search = None
            elif self.device_search is None:
                self.device_search = FollowedSearch(shared["search"])
            else:
                self.device_search.follow(shared["search"])
        if "recordings" in shared:
            default, chosen = shared["recordings"]
            self.default_recordings = None if default is None else Path(default)
            self._recordings = None if chosen is None else Path(chosen)

    # What the radio asks of the engine.

    def settings_changed(self) -> None:
        if self.radio is None:
            return
        self._channel.post({"op": "settings", "receiver": self.radio.receiver_settings(),
                            "scanner": self.radio.scanner_settings(), "receivers": self.receivers.taken(),
                            "reopen": self.reopen.taken()})

    def observations_changed(self, soon: bool = False) -> None:
        pass        # Radiolack's process saves them as the signals come

    def scans_changed(self, soon: bool = False) -> None:
        scan = None if self.radio is None else self.radio.kept_scan
        if scan is not None:
            self._channel.post({"op": "kept", "scan": scan.stored(), "soon": soon})

    def radios_changed(self) -> None:
        if self.radio is None:
            return
        radio = self.radio
        device = radio.soapy_in_use()
        self._channel.post({"op": "summary", "summary": radio.summary(),
                            "kind": radio.source.kind, "reopen": radio.source.reopen,
                            "settings_key": radio.settings_key,
                            "running_files": sorted(radio.running_files()),
                            "recording_runs": radio.recording_runs(),
                            "soapy": None if device is None else [device.driver, device.serial, device.label]})

    def check_free(self, source: Any, radio: Any = None) -> None:
        for other in self._others:
            if other.source.settings_key == source.settings_key or other.source.reopen == source.reopen:
                raise ValueError(f"receiver {other.id} reads this device already")

    def check_no_search(self) -> None:
        if self.device_search is not None and self.device_search.running:
            raise ValueError("a search for SoapySDR devices is running: choose once it ends")

    def start_search(self) -> None:
        self._channel.post({"op": "search"})

    def recordings_folder(self) -> Path | None:
        if self.default_recordings is None:
            return None
        return self._recordings if self._recordings is not None else self.default_recordings

    def check_recordings(self) -> None:
        if self.recordings_folder() is None:
            raise ValueError("this Radiolack does not record")

    def choose_recordings_folder(self, value: object, subscription: Any) -> None:
        """Radiolack's process chooses the folder, for every radio."""
        self.check_recordings()
        self._channel.post({"op": "folder", "value": value,
                            "client": None if subscription is None else subscription.id})

    def close(self) -> None:
        """The radio stopped after an error (``Radio.run``)."""
        self.failure = "it stopped after an error"
        self.stopped.set()


class _Forward(logging.Handler):
    """Sends the radio's process's log records to Radiolack's process."""

    def __init__(self, channel: Channel) -> None:
        super().__init__()
        self._channel = channel

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self._channel.post({"op": "log", "level": record.levelno, "name": record.name, "message": text})
        except Exception:      # a log record must never stop the radio
            self.handleError(record)


def _states(radio: Any, engine: ChildEngine, outlet: Outlet) -> None:
    """The messages of the radio's states, for local and remote clients."""
    outlet.states = {
        "source": lambda remote: radio.state(remote),
        "demod": lambda remote: radio.demod_state(0),
        "scanner": lambda remote: radio.scanner_state(),
        "peaks": lambda remote: radio.peaks_state(),
        "recording": lambda remote: radio.recording_state(remote),
        "calibration": lambda remote: radio.calibration_state(),
        "devices": lambda remote: (None if engine.device_search is None else radio.devices_state(remote)),
    }


async def _run(fd: int) -> int:
    from .radio import Radio        # imported here: the radio imports the engine, which this module serves
    from .sources import Sources

    loop = asyncio.get_running_loop()
    reader, writer = await asyncio.open_connection(sock=socket.socket(fileno=fd))
    channel = Channel(reader, writer)
    logging.getLogger("radiolack").addHandler(_Forward(channel))
    logging.getLogger("radiolack").setLevel(logging.INFO)
    first = await channel.receive()
    if first is None or first[0]["op"] != "start":
        return 2
    start = first[0]
    outlet = Outlet(channel)
    try:
        sources = Sources.from_recipe(start["sources"])
        engine = ChildEngine(channel, outlet, start, sources)
        radio = Radio(engine, sources.open(start["source"]), radio_id=int(start["id"]),
                      saved_scanner=start.get("scanner"), frame_rate=float(start["frame_rate"]))
    except (ValueError, KeyError, TypeError) as error:
        channel.post({"op": "failed", "message": str(error)})
        await channel.drain()
        return 1
    engine.radio = radio
    _states(radio, engine, outlet)
    clients: dict[int, ClientProxy] = {}
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    channel.post({"op": "ready"})
    outlet.publish(list(outlet.states), [])
    engine.radios_changed()
    engine.settings_changed()
    running = asyncio.ensure_future(radio.run())

    async def commands() -> None:
        while (message := await channel.receive()) is not None:
            header, _ = message
            op = header["op"]
            if op == "close":
                return
            if op == "share":
                engine.share(header)
                if "bookmarks" in header:
                    radio.watch_bookmarks()
                if "band_plan" in header:
                    for client in radio.clients:
                        client.changed("peaks")     # on the new plan's channels
                if "search" in header:
                    radio.follow_devices()
                if "recordings" in header:
                    for client in radio.clients:
                        client.changed("recording")
            elif op == "attach":
                client = ClientProxy(outlet, int(header["client"]), bool(header["remote"]),
                                     bool(header["audio"]), bool(header["fine"]))
                client.radio = radio
                clients[client.id] = client
                radio.attach(client)
                outlet.publish(list(outlet.states), [client.id])
            elif op == "detach":
                client = clients.pop(int(header["client"]), None)
                if client is not None:
                    radio.detach(client)
            elif op in ("audio", "fine"):
                client = clients.get(int(header["client"]))
                if client is not None:
                    (radio.set_audio if op == "audio" else radio.set_fine)(client, bool(header["on"]))
            elif op == "call":
                _call(radio, engine, clients, header, channel)
            else:
                log.warning("an unknown operation from Radiolack: %s", op[:40])

    listening = asyncio.ensure_future(commands())
    stopped = asyncio.ensure_future(engine.stopped.wait())
    signalled = asyncio.ensure_future(stop.wait())
    await asyncio.wait({listening, stopped, signalled, running}, return_when=asyncio.FIRST_COMPLETED)
    if listening.done() and not listening.cancelled() and listening.exception() is not None:
        error = listening.exception()
        log.error("radio %d stops: %s", radio.id, error, exc_info=error)
        engine.failure = f"it could not follow Radiolack: {error}"
    for task in (listening, stopped, signalled):
        task.cancel()
    # Settings as they stand before the radio closes: a scan that runs gives those of its start.
    engine.settings_changed()
    radio.close()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    await radio.wait_closed()
    if engine.closing:
        await asyncio.wait(set(engine.closing))
    for recording in list(engine.unfinished):
        if not await asyncio.to_thread(recording.join, RECORDING_CLOSE_S):
            log.warning("the recording %s did not reach the disk in time", recording.file or "just started")
    if engine.failure is not None:
        channel.post({"op": "failed", "message": engine.failure})
    outlet.flush()
    engine.radios_changed()
    channel.post({"op": "closed"})
    await channel.drain()
    channel.close()
    return 0


def _call(radio: Any, engine: ChildEngine, clients: Mapping[int, ClientProxy], header: Mapping[str, Any],
          channel: Channel) -> None:
    """Call the radio's method that ``header`` names; a refusal goes to the
    interface that asked."""
    method, args, client_id = header.get("method"), header.get("args", []), header.get("client")
    client = clients.get(client_id) if isinstance(client_id, int) else None
    if method not in CALLS or not isinstance(args, list):
        log.warning("an unknown call from Radiolack: %s", str(method)[:40])
        return
    if CALLS[method]:   # the interface that asked follows the changes, before any other argument
        args = [*args[:1], client, *args[1:]]
    try:
        if getattr(radio, method)(*args) and method == "end_kept_scan":
            engine.scans_changed(soon=True)     # Radiolack's process keeps the scans
    except ValueError as error:
        channel.post({"op": "error", "client": client_id, "command": header.get("command"), "message": str(error)})


def main(argv: Sequence[str] | None = None) -> int:
    """The radio's process: ``FD``, the socket to Radiolack's process."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python3 -m radiolack.radioproc FD (Radiolack starts it)", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # Radiolack's process handles the terminal's Ctrl-C
    return asyncio.run(_run(int(argv[0])))


if __name__ == "__main__":
    sys.exit(main())
