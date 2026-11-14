"""Local web server: the Radiolack interface and its data stream.

The server listens on the loopback interface only and runs its own asyncio
event loop in a background thread, so that the main thread stays free for the
desktop window toolkit, which must own it. The engine runs in that loop and
streams its frames, its audio, its source state, its demodulation settings,
its bookmarks, the signals it finds and what its update check found to each
interface over a WebSocket (``/stream``; see docs/design/protocol.md), which
also carries the interfaces' changes, their preferences, their requests for
audio, for an update check and for the observations.

The server reads the interface's files once, when it is made, and serves
them from memory: a running Radiolack keeps serving the interface it started
with after a newer package replaced the files, so that a page and its engine
always come from the same version.

Remote access (remote.py), once this computer's interface or the command
line turns it on, adds a second web application to the same loop, with a
runner of its own: it serves the interface and the stream over HTTPS on
this computer's private addresses, which it follows while on, to devices
that entered the access code, and turning it off stops it without stopping
the first. Remote devices may not change remote access, the update check or
the rtl_tcp server's address, and the preferences they give are their own.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import json
import logging
import os
import re
import socket
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from .engine import LOCAL_SETTINGS, Engine, Source, SourceOpener, Subscription, UpdateChecker
from .remote import (SESSION_COOKIE, SESSION_MAX_AGE_S, Device, RemoteAccess, device_label, host_name,
                     is_private, issue_server_certificate, remote_urls, server_context)
from .scanners import SCANNERS
from .store import Store
from .synthetic import SyntheticBand

LOOPBACK_HOST = "127.0.0.1"
PROTOCOL_VERSION = 6
WEB_DIR = Path(__file__).with_name("web")
CLOSE_NOT_PAIRED = 4001     # closes a remote stream whose device is not paired, or no longer
CLOSE_REMOTE_OFF = 4002     # closes the remote streams when remote access turns off
CLOSE_MOVING = 1012         # (Service Restart) closes them when remote access moves to new addresses
ADDRESS_CHECK_S = 10.0      # how often remote access, while on, looks at this computer's addresses

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0
_REMOTE_START_TIMEOUT_S = 30.0  # the first turn-on also makes the local authority
_CLOSE_TIMEOUT_S = 2.0          # wait for a client to acknowledge a closed stream
_CLIENT_MESSAGE_LIMIT = 64 * 1024
_MAX_CHANGE_ID = 2 ** 53 - 1         # the largest whole number JavaScript counts exactly
_COMMANDS = ("source", "demod", "audio", "bookmark", "scanner", "observations", "preferences", "update",
             "remote")
_LOCAL_COMMANDS = ("update", "remote")     # refused to remote devices
_REMOTE_HEARTBEAT_S = 20.0      # a device that stops answering pings loses its stream
_UNSENT_LIMIT = 16 * 1024       # bytes a device's stream keeps unsent, in its socket and in its transport
_REMOTE_KEEPALIVE_S = 15.0      # an idle HTTPS connection closes after this long
_REMOTE_BODY_LIMIT = 4 * 1024   # a pairing request's body
_WRONG_CODE_DELAY_S = 1.0       # a wrong code is answered this late, and codes are checked one at a time
_PAIRING_FILES = frozenset({"pair.js", "pair.css", "style.css"})   # what a device loads before pairing
# How long stopping remote access waits for its requests: aiohttp 3.9 moved
# that setting from the sites, where it now warns, to the runner, while
# aiohttp 3.8 (Debian 12) takes it on the sites only.
_REMOTE_SHUTDOWN = {"shutdown_timeout": _CLOSE_TIMEOUT_S}
_SHUTDOWN_ON_RUNNER = "shutdown_timeout" in inspect.signature(web.BaseRunner.__init__).parameters
_CLOSE_REASONS = {CLOSE_NOT_PAIRED: b"This device is not paired.", CLOSE_REMOTE_OFF: b"Remote access is off.",
                  CLOSE_MOVING: b"Remote access moves to this computer's new addresses."}
_BLOCKED = "Too many wrong codes: make a new code in Radiolack's settings on the computer."

log = logging.getLogger(__name__)

# The page loads its scripts and styles from files served next to it, never
# inline. It may only talk to this server, and other pages may not frame it.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
))

# Interface files served next to the page: flat lower-case names only, so a
# request can never reach a file outside WEB_DIR.
_FILE_TYPES = {".css": "text/css", ".js": "text/javascript", ".json": "application/json"}
_FILE_NAME = r"[a-z0-9][a-z0-9_-]*\.(?:css|js|json)"
_FILE_ROUTE = rf"/{{name:{_FILE_NAME}}}"
_PAGES = ("index.html", "pair.html")


class InterfaceUnreadable(Exception):
    """The interface's files cannot be read: the message says which, to the user."""


class InterfaceFiles:
    """The interface's pages and the files served next to them, read from
    ``folder`` once and served from memory. Raises InterfaceUnreadable when
    one cannot be read."""

    def __init__(self, folder: Path) -> None:
        try:
            self._pages = {name: (folder / name).read_bytes() for name in _PAGES}
            self._files = {path.name: path.read_bytes() for path in folder.iterdir()
                           if re.fullmatch(_FILE_NAME, path.name) and path.is_file()}
        except OSError as error:
            raise InterfaceUnreadable(f"cannot read the interface: {error.filename or folder}: "
                                      f"{error.strerror or error}") from error

    def page(self, name: str) -> web.Response:
        """A page of the interface, under its Content-Security-Policy."""
        return web.Response(
            body=self._pages[name],
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            },
        )

    def file(self, name: str) -> web.Response:
        """A file served next to the pages; HTTPNotFound for any other name."""
        body = self._files.get(name)
        if body is None:
            raise web.HTTPNotFound()
        return web.Response(
            body=body,
            content_type=_FILE_TYPES[Path(name).suffix],
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


def create_app(port: int, engine: Engine, remote: RemoteListener, interface: InterfaceFiles) -> web.Application:
    """Build the web application for a server listening on ``port``.

    The application serves ``interface``, runs ``engine`` from its startup
    to its cleanup, and passes the ``remote`` messages of its interfaces to
    ``remote``.
    """
    names = (LOOPBACK_HOST, "localhost")
    allowed_hosts = {f"{name}:{port}" for name in names}
    if port == 80:  # Browsers omit the default port from the Host header.
        allowed_hosts.update(names)
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @web.middleware
    async def check_host(request: web.Request, handler):
        # A page from another site can reach a loopback server through DNS
        # rebinding, but its requests then carry that site's host name.
        if (request.host or "").lower() not in allowed_hosts:
            raise web.HTTPForbidden(text="Unexpected Host header.\n")
        response = await handler(request)
        if not response.prepared:   # a stream has already sent its headers
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    async def index(_request: web.Request) -> web.Response:
        return interface.page("index.html")

    async def interface_file(request: web.Request) -> web.Response:
        return interface.file(request.match_info["name"])

    async def stream(request: web.Request) -> web.StreamResponse:
        # Any page may open a WebSocket to any address, but browsers always
        # tell the page's origin: only the interface itself may connect.
        if request.headers.get("Origin", "").lower() not in allowed_origins:
            raise web.HTTPForbidden(text="Unexpected Origin header.\n")
        ws = web.WebSocketResponse(timeout=_CLOSE_TIMEOUT_S, compress=False,
                                   max_msg_size=_CLIENT_MESSAGE_LIMIT)
        await ws.prepare(request)
        await _serve_stream(ws, engine, remote, device=None)
        return ws

    async def stop_streams(_app: web.Application) -> None:
        engine.close()

    async def run_engine(_app: web.Application):
        task = asyncio.ensure_future(engine.run())
        yield
        engine.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.wait_closed()      # such as rtl_tcp stopped, before Radiolack ends

    app = web.Application(middlewares=[check_host])
    app.router.add_get("/", index)
    app.router.add_get("/stream", stream)
    app.router.add_get(_FILE_ROUTE, interface_file)
    app.on_shutdown.append(stop_streams)
    app.cleanup_ctx.append(run_engine)
    return app


async def _serve_stream(ws: web.WebSocketResponse, engine: Engine, remote: RemoteListener,
                        device: str | None) -> None:
    """Run an open stream, of this computer's interface or of the paired
    ``device`` named by its id: the hello, then the engine's messages one way
    and the interface's commands the other, until either side ends it."""
    try:
        await ws.send_json({"type": "hello", "protocol": PROTOCOL_VERSION, "remote": device is not None,
                            "scanners": list(SCANNERS)})
    except ConnectionResetError:
        return
    subscription = engine.subscribe(device=device)
    sender = asyncio.ensure_future(_send(ws, subscription))
    receiver = asyncio.ensure_future(_receive(ws, engine, subscription, remote))
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if not receiver.done():     # the engine stopped, or sending failed
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Radiolack is stopping.")
    finally:
        engine.unsubscribe(subscription)
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


async def _send(ws: web.WebSocketResponse, subscription: Subscription) -> None:
    """Forward the engine's messages to one client until the engine stops."""
    while (message := await subscription.get()) is not None:
        if isinstance(message, bytes):
            await ws.send_bytes(message)
        else:
            await ws.send_json(message)


async def _receive(ws: web.WebSocketResponse, engine: Engine, subscription: Subscription,
                   remote: RemoteListener) -> None:
    """Apply the client's commands until it closes the stream. A refused
    command gets an error message, for this client only."""
    async for message in ws:
        if message.type is not WSMsgType.TEXT:
            continue
        kind: str | None = None
        try:
            kind, refusal = _apply_command(engine, subscription, remote, message.data)
        except Exception:
            log.exception("a command from the interface failed")
            refusal = "the command failed"
        if refusal is not None:
            subscription.reply({"type": "error", "command": kind, "message": refusal})


def _apply_command(engine: Engine, subscription: Subscription, remote: RemoteListener,
                   text: str) -> tuple[str | None, str | None]:
    """Apply one command from an interface: returns its type, if known, and
    why it was refused, or None."""
    try:
        command = json.loads(text)
    except (ValueError, RecursionError):
        return None, "a command must be a JSON object"
    if not isinstance(command, dict) or command.get("type") not in _COMMANDS:
        return None, "unknown command"
    kind = command["type"]
    changes = {key: value for key, value in command.items() if key != "type"}
    if subscription.remote and (kind in _LOCAL_COMMANDS
                                or kind == "source" and any(name in changes for name in LOCAL_SETTINGS)):
        return kind, "only Radiolack's interface on the computer may change this"
    try:
        if kind == "source":
            engine.configure(changes)
        elif kind == "demod":
            change_id = changes.pop("id", None)
            if change_id is not None and (isinstance(change_id, bool) or not isinstance(change_id, int)
                                          or not 0 < change_id <= _MAX_CHANGE_ID):
                return kind, "a change id must be a whole number from 1"
            engine.configure_demod(changes, subscription, change_id)
        elif kind == "bookmark":
            engine.edit_bookmarks(changes)
        elif kind == "scanner":
            engine.configure_scanner(changes)
        elif kind == "observations":
            engine.configure_observations(changes, subscription)
        elif kind == "preferences":
            values = changes.pop("values", None)
            if changes:
                return kind, f"unknown preferences field: {next(iter(changes))}"
            engine.set_preferences(values, subscription.device)
        elif kind == "update":
            engine.configure_updates(changes)
        elif kind == "remote":
            remote.command(changes)
        else:
            on = changes.pop("on", None)
            if changes:
                return kind, f"unknown audio setting: {next(iter(changes))}"
            if not isinstance(on, bool):
                return kind, "audio must be turned on or off: true or false"
            engine.set_audio(subscription, on)
    except ValueError as error:
        return kind, str(error)
    return kind, None


def _bind(host: str, port: int) -> socket.socket:
    """Return a listening TCP socket on ``host``, an IPv4 address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            # Lets a restart reuse a fixed port still in TIME_WAIT. Windows
            # gives this option another meaning (sharing a busy port).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


class _Refusal(Exception):
    """Remote access cannot turn on: the message says why, to the user."""


class RemoteListener:
    """Remote access's HTTPS listener (remote.py): a web application with a
    runner of its own, which shares the engine, on this computer's private
    addresses.

    This computer's interfaces turn it on and off, and change its settings,
    with ``remote`` messages (``command``). It listens only while on, on the
    private addresses among those that ``addresses`` gives, which it looks
    at every ADDRESS_CHECK_S: once they change, it listens on the new ones,
    with a new certificate, and stays on while there are none, or while it
    cannot listen on them, until they change again. Moving, or turning off,
    it refuses every request at once, then closes the remote streams and
    stops listening. ``host`` is this computer's mDNS name, without
    ``.local``, or "" for none. It serves ``interface``, the local
    server's."""

    def __init__(self, engine: Engine, access: RemoteAccess, addresses: Callable[[], Sequence[str]],
                 host: str, interface: InterfaceFiles) -> None:
        self._engine = engine
        self._access = access
        self._addresses = addresses
        self._host = host
        self._interface = interface
        self._runner: web.AppRunner | None = None
        self._sockets: list[socket.socket] = []
        self._serving = False       # listening, and not stopping: requests are answered
        self._hosts: frozenset[str] = frozenset()     # the Host headers that name this listener
        self._streams: dict[web.WebSocketResponse, Device] = {}     # the open remote streams
        self._switching = asyncio.Lock()    # one turn-on, turn-off or move at a time
        self._pairing = asyncio.Lock()      # one code checked at a time
        self._tasks: set[asyncio.Future] = set()
        self._closed = False
        self._on = False            # turned on, and not off since: listens where it can
        # The addresses listened on, or tried last; None to try them again at the next check.
        self._tried: frozenset[str] | None = frozenset()
        self._follower: asyncio.Task | None = None     # looks at the addresses while on

    def command(self, changes: Mapping[str, object]) -> None:
        """Apply a ``remote`` message from this computer's interface: one of
        ``on``, ``port``, ``new_code`` and ``forget``. Raises ValueError,
        changing nothing, when refused. Turning on or off goes on in the
        background: the remote access state tells how it went."""
        if len(changes) != 1:
            raise ValueError("a remote message changes one setting at a time")
        [(name, value)] = changes.items()
        if name == "on":
            if not isinstance(value, bool):
                raise ValueError("remote access is turned on or off: true or false")
            self._run(self.turn_on() if value else self.turn_off())
        elif name == "port":
            if self._switching.locked():
                raise ValueError("the port changes only while remote access is off")
            self._access.set_port(value)
        elif name == "new_code":
            if value is not True:
                raise ValueError("new_code takes true")
            forgotten = set(self._access.renew_code())
            self._run(self._close_streams(lambda device: device.id in forgotten, CLOSE_NOT_PAIRED))
        elif name == "forget":
            self._access.forget(value)
            self._run(self._close_streams(lambda device: device.id == value, CLOSE_NOT_PAIRED))
        else:
            raise ValueError(f"unknown remote access setting: {str(name)[:40]}")

    async def turn_on(self) -> None:
        """Listen on this computer's private addresses with a new server
        certificate, then follow them; the remote access state says why when
        it cannot, and remote access then stays off."""
        async with self._switching:
            if self._on or self._closed:
                return
            try:
                addresses = self._private_addresses()
                if not addresses:
                    raise _Refusal("This computer has no address on a local network: connect it to one, "
                                   "then turn remote access on.")
                await self._listen(addresses)
            except _Refusal as refusal:
                self._access.stopped(str(refusal))
                return
            except Exception:
                log.exception("remote access could not turn on")
                self._access.stopped("Remote access could not turn on: Radiolack's error output says why.")
                return
            self._on, self._tried = True, frozenset(addresses)
            self._follower = asyncio.ensure_future(self._follow())

    async def turn_off(self) -> None:
        """Stop following the addresses and stop listening: every remote
        request is refused at once, then the remote streams close and the
        port is released."""
        async with self._switching:
            if not self._on:
                return
            self._on = False
            follower, self._follower = self._follower, None
            if follower is not None:    # between two checks, as a move holds the lock held here
                follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)
            await self._stop_listening(CLOSE_REMOTE_OFF)
            self._access.stopped()

    async def close(self) -> None:
        """Turn off for good, once the turn-ons and turn-offs asked for have ended."""
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.turn_off()

    def _private_addresses(self) -> list[str]:
        """The private addresses among those that ``addresses`` gives now, once each, in its order."""
        return [address for address in dict.fromkeys(self._addresses()) if is_private(address)]

    async def _follow(self) -> None:
        """While on, look at this computer's addresses every ADDRESS_CHECK_S:
        move once they change, and try again where listening was refused."""
        while True:
            await asyncio.sleep(ADDRESS_CHECK_S)
            async with self._switching:
                if not self._on or self._closed:
                    return
                try:
                    addresses = self._private_addresses()
                    if frozenset(addresses) != self._tried:
                        await self._move(addresses)
                except Exception:
                    log.exception("remote access could not follow this computer's addresses")

    async def _move(self, addresses: list[str]) -> None:
        """Listen on ``addresses``, this computer's private addresses now, in
        place of those before: the remote streams close with CLOSE_MOVING, and
        the devices on an address that remains reconnect. Without addresses,
        or where it cannot listen, remote access stays on, listening nowhere,
        and its state says why."""
        self._tried = frozenset(addresses)
        await self._stop_listening(CLOSE_MOVING)
        if not addresses:
            log.warning("remote access listens nowhere until this computer has an address on a local network")
            problem = ("This computer has no address on a local network for now: remote access listens "
                       "again once it has one.")
        else:
            try:
                await self._listen(addresses)
                return
            except _Refusal as refusal:
                problem = str(refusal)
                self._tried = None      # the port, say, may be free at the next check
                if problem != self._access.problem:     # once, and not at each check
                    log.warning("remote access cannot listen on this computer's new addresses: %s", problem)
            except Exception:
                log.exception("remote access could not listen on this computer's new addresses")
                problem = ("Remote access could not listen on this computer's new addresses: Radiolack's "
                           "error output says why.")
        self._access.started([], problem)

    async def _stop_listening(self, code: int) -> None:
        """Refuse every remote request at once, close the remote streams with
        ``code``, then release the port."""
        runner, sockets = self._runner, self._sockets
        if runner is None:
            return
        self._serving = False
        self._runner, self._sockets = None, []
        await self._close_streams(lambda _device: True, code)
        try:
            await asyncio.wait_for(runner.cleanup(), _STOP_TIMEOUT_S)
        except Exception:
            log.exception("remote access did not stop cleanly")
        finally:
            for sock in sockets:
                sock.close()

    async def _listen(self, addresses: Sequence[str]) -> None:
        """Listen on ``addresses``, private ones, at least one, with a new
        server certificate that names them. Raises _Refusal when it cannot."""
        port = self._access.port
        names = [f"{self._host}.local"] if self._host else []
        try:
            chain, key = issue_server_certificate(self._access.prepare(self._host), names, addresses)
        except ImportError:
            raise _Refusal("Remote access needs the Python package cryptography "
                           "(python3-cryptography on Debian and Ubuntu).") from None
        context = server_context(chain, key)
        sockets: list[socket.socket] = []
        try:
            for address in addresses:
                sockets.append(_bind(address, port))
        except OSError as error:
            for sock in sockets:
                sock.close()
            if error.errno == errno.EADDRINUSE:
                raise _Refusal(f"Port {port} is in use by another program: choose another port.") from None
            raise _Refusal(f"Remote access cannot listen on {address}, port {port}: "
                           f"{error.strerror or error}.") from None
        runner = web.AppRunner(self._app(), handle_signals=False, keepalive_timeout=_REMOTE_KEEPALIVE_S,
                               **(_REMOTE_SHUTDOWN if _SHUTDOWN_ON_RUNNER else {}))
        try:
            await runner.setup()
            for sock in sockets:
                await web.SockSite(runner, sock, ssl_context=context,
                                   **({} if _SHUTDOWN_ON_RUNNER else _REMOTE_SHUTDOWN)).start()
        except BaseException:
            try:
                await runner.cleanup()
            finally:
                for sock in sockets:
                    sock.close()
            raise
        self._hosts = frozenset(f"{name}:{port}" for name in [*names, *addresses])
        self._runner, self._sockets, self._serving = runner, sockets, True
        self._access.started(remote_urls(self._host, addresses, port))

    def _run(self, work: Awaitable[None]) -> None:
        """Run ``work`` in the background, unless closed."""
        if self._closed:
            if asyncio.iscoroutine(work):
                work.close()
            return
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close_streams(self, match: Callable[[Device], bool], code: int) -> None:
        """Close the remote streams of the devices that ``match``, with ``code``."""
        closing = [ws.close(code=code, message=_CLOSE_REASONS[code])
                   for ws, device in list(self._streams.items()) if match(device)]
        await asyncio.gather(*closing, return_exceptions=True)

    def _device(self, request: web.Request) -> Device | None:
        """The paired device that sent ``request``, or None."""
        return self._access.session(request.cookies.get(SESSION_COOKIE))

    def _app(self) -> web.Application:
        @web.middleware
        async def check(request: web.Request, handler):
            # Off, nothing is answered, not even on connections opened before.
            if not self._serving:
                raise web.HTTPServiceUnavailable(text="Remote access is off.\n")
            if not is_private(request.remote or ""):
                raise web.HTTPForbidden(text="Unexpected peer address.\n")
            # Against DNS rebinding, as on the loopback listener.
            if (request.host or "").lower() not in self._hosts:
                raise web.HTTPForbidden(text="Unexpected Host header.\n")
            response = await handler(request)
            if not response.prepared:
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("Referrer-Policy", "no-referrer")
                response.headers.setdefault("Cache-Control", "no-store")
            return response

        app = web.Application(middlewares=[check], client_max_size=_REMOTE_BODY_LIMIT)
        app.router.add_get("/", self._index)
        app.router.add_get("/stream", self._stream)
        app.router.add_post("/pair", self._pair)
        app.router.add_get("/certificate", self._certificate)
        app.router.add_get(_FILE_ROUTE, self._file)
        return app

    async def _index(self, request: web.Request) -> web.Response:
        """The interface for a paired device, the pairing page for another."""
        return self._interface.page("index.html" if self._device(request) is not None else "pair.html")

    async def _file(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in _PAIRING_FILES and self._device(request) is None:
            raise web.HTTPForbidden(text="This device is not paired.\n")
        return self._interface.file(name)

    async def _certificate(self, _request: web.Request) -> web.Response:
        """The local authority's root certificate, which a device installs."""
        authority = self._access.authority
        if authority is None:
            raise web.HTTPNotFound()
        name = f"radiolack-{self._host}.crt" if self._host else "radiolack.crt"
        return web.Response(body=authority.root.encode("ascii"), content_type="application/x-x509-ca-cert",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def _pair(self, request: web.Request) -> web.Response:
        """Check the access code a device sends, as JSON: the right one pairs
        the device, which gets its session cookie. Wrong codes are answered
        late, one at a time, and too many block the code."""
        if not _same_origin(request):
            raise web.HTTPForbidden(text="Unexpected Origin header.\n")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Send the code as JSON.\n")
        try:
            body = json.loads(await request.text())     # a larger body than allowed raises 413
        except (ValueError, RecursionError):
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return _answer(400, "Send the access code.")
        if self._device(request) is not None:
            return web.json_response({"paired": True})
        async with self._pairing:
            access = self._access
            if not self._serving:
                raise web.HTTPServiceUnavailable(text="Remote access is off.\n")
            if access.blocked:
                return _answer(429, _BLOCKED)
            if not access.check_code(code):
                await asyncio.sleep(_WRONG_CODE_DELAY_S)
                return _answer(429, _BLOCKED) if access.blocked else _answer(403, "Wrong code.")
            try:
                token = access.pair(device_label(request.headers.get("User-Agent", "")))
            except ValueError as error:
                reason = str(error)
                return _answer(409, f"{reason[:1].upper()}{reason[1:]}.")
        response = web.json_response({"paired": True})
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE_S, path="/", secure=True,
                            httponly=True, samesite="Strict")
        return response

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        if not _same_origin(request):
            raise web.HTTPForbidden(text="Unexpected Origin header.\n")
        # Deflate (RFC 7692), which the browsers offer, halves the spectrum
        # frames for the device's link; the stream carries no secret that
        # compression could reveal. A device's messages stay limited once
        # inflated.
        ws = web.WebSocketResponse(timeout=_CLOSE_TIMEOUT_S, compress=True, max_msg_size=_CLIENT_MESSAGE_LIMIT,
                                   heartbeat=_REMOTE_HEARTBEAT_S)
        transport = request.transport   # which the request no longer gives once prepared
        await ws.prepare(request)
        _limit_unsent(transport)
        # Checked once the handshake is over, so that the page learns why from
        # the close code; then noted at once, so that turning off or
        # forgetting the device closes the stream.
        device = self._device(request)
        if not self._serving or device is None:
            # Not serving: remote access turns off, or moves while it stays on.
            code = CLOSE_NOT_PAIRED if self._serving else CLOSE_MOVING if self._on else CLOSE_REMOTE_OFF
            await ws.close(code=code, message=_CLOSE_REASONS[code])
            return ws
        self._streams[ws] = device
        self._access.connected(device)
        try:
            await _serve_stream(ws, self._engine, self, device=device.id)
        finally:
            del self._streams[ws]
            self._access.disconnected(device)
        return ws


def _limit_unsent(transport: asyncio.BaseTransport | None) -> None:
    """Keep few bytes unsent for a device whose link may be slower than its
    stream: otherwise the system keeps more and more of it, and the device
    hears the radio later and later. Its socket (with TCP_NOTSENT_LOWAT, on
    Linux and macOS) and its transport then hold little, and the engine
    drops the device's oldest frames instead."""
    if not isinstance(transport, asyncio.WriteTransport):
        return
    transport.set_write_buffer_limits(high=_UNSENT_LIMIT)
    sock = transport.get_extra_info("socket")
    option = getattr(socket, "TCP_NOTSENT_LOWAT", None)
    if sock is not None and option is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, _UNSENT_LIMIT)
        except OSError as error:
            log.warning("a remote stream may fall behind on a slow link: %s", error)


def _same_origin(request: web.Request) -> bool:
    """Whether the request comes from a page of the host it names."""
    return request.headers.get("Origin", "").lower() == f"https://{(request.host or '').lower()}"


def _answer(status: int, message: str) -> web.Response:
    """A pairing request's refusal, with a sentence for the user."""
    return web.json_response({"error": message}, status=status)


def _no_addresses() -> list[str]:
    return []


class LocalServer:
    """Serve the interface on the loopback interface from a background thread.

    The constructor reads the interface's files, then binds the port, so
    that an unreadable file raises InterfaceUnreadable and a busy port
    ``OSError`` before anything starts; the server serves the files as they
    were read, until it stops. Use it as a context manager: entering starts the
    server, leaving stops it. The engine reads ``source``, or without one the
    source that ``sources`` opened last, and closes it when the server stops;
    with ``sources``, an interface may switch to another kind of source.
    Without either, it reads the synthetic band. With a ``store``, the engine
    starts from the saved settings and saves their changes; with ``updates``,
    it checks for a newer Radiolack. No scan runs until an interface or
    ``start_scan`` starts one.

    Remote access, which stays off until an interface or ``turn_on_remote``
    turns it on, listens on the addresses that ``remote_addresses`` gives,
    such as remote.private_addresses, at each turn-on and then every
    ADDRESS_CHECK_S while on: without it, it finds none and cannot turn on.
    ``host`` is this computer's mDNS name, by default its host name.
    """

    def __init__(self, port: int = 0, source: Source | None = None, store: Store | None = None,
                 sources: SourceOpener | None = None, updates: UpdateChecker | None = None,
                 remote_addresses: Callable[[], Sequence[str]] | None = None, host: str | None = None) -> None:
        self._source = source
        self._sources = sources
        self._store = store
        self._updates = updates
        self._remote_addresses = remote_addresses if remote_addresses is not None else _no_addresses
        self._host = host_name() if host is None else host
        self._interface = InterfaceFiles(WEB_DIR)
        self._sock = _bind(LOOPBACK_HOST, port)
        self.port: int = self._sock.getsockname()[1]
        self.url = f"http://{LOOPBACK_HOST}:{self.port}/"
        self._thread = threading.Thread(target=self._run, name="radiolack-server", daemon=True)
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested: asyncio.Event | None = None
        self._error: BaseException | None = None
        # Made in the server's loop before it is ready, and used only there.
        self._engine: Engine | None = None
        self._access: RemoteAccess | None = None
        self._remote: RemoteListener | None = None

    def __enter__(self) -> LocalServer:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> None:
        """Start serving; on failure, release the port and raise."""
        self._thread.start()
        started = self._ready.wait(_START_TIMEOUT_S)
        if started and self._error is None:
            return
        self.stop()
        if self._error is not None:
            raise self._error
        raise RuntimeError("the web server did not start in time")

    def stop(self) -> None:
        """Stop serving and release the ports. Safe to call more than once."""
        loop, stop_requested = self._loop, self._stop_requested
        if loop is not None and stop_requested is not None and self._thread.is_alive():
            try:
                loop.call_soon_threadsafe(stop_requested.set)
            except RuntimeError:  # The loop has just closed.
                pass
        if self._thread.is_alive():
            self._thread.join(_STOP_TIMEOUT_S)
        self._sock.close()

    def turn_on_remote(self, port: int | None = None) -> dict[str, Any]:
        """Turn remote access on, as this computer's interface does in
        Settings, on ``port`` if given, which it keeps for the next turn-ons
        as Settings would, and wait until it listens or cannot. Returns the
        remote access state that this computer's interfaces get
        (RemoteAccess.describe): ``on`` says which, and ``problem`` why not.
        Raises ValueError for a port that remote access refuses, and
        TimeoutError when turning on takes too long."""
        loop, access, remote = self._loop, self._access, self._remote
        if loop is None or access is None or remote is None:
            raise RuntimeError("the server is not running")

        async def turn_on() -> dict[str, Any]:
            if port is not None:
                access.set_port(port)
            await remote.turn_on()
            return access.describe()

        future = asyncio.run_coroutine_threadsafe(turn_on(), loop)
        try:
            return future.result(_REMOTE_START_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise

    def start_scan(self, scan: str) -> None:
        """Start ``scan``, "memory" or "sweep", given at start, as
        Engine.start_given_scan does: it goes on without any interface.
        Raises ValueError, saying why, when it cannot start, and TimeoutError
        when the server does not answer in time."""
        loop, engine = self._loop, self._engine
        if loop is None or engine is None:
            raise RuntimeError("the server is not running")

        async def start() -> None:
            engine.start_given_scan(scan)

        future = asyncio.run_coroutine_threadsafe(start(), loop)
        try:
            future.result(_START_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:  # Reported to the starting thread.
            self._error = error
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        source = SyntheticBand() if self._source is None and self._sources is None else self._source
        access = RemoteAccess()
        engine = Engine(source, sources=self._sources, store=self._store, updates=self._updates, remote=access)
        remote = RemoteListener(engine, access, self._remote_addresses, self._host, self._interface)
        self._engine, self._access, self._remote = engine, access, remote
        runner = web.AppRunner(create_app(self.port, engine, remote, self._interface), handle_signals=False)
        await runner.setup()
        try:
            await web.SockSite(runner, self._sock).start()
            self._stop_requested = asyncio.Event()
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            await self._stop_requested.wait()
        finally:
            engine.close()      # ends the streams, so that no client holds up the shutdown
            await remote.close()
            await runner.cleanup()
