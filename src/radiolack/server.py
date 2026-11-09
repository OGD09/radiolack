"""Local web server: the Radiolack interface and its data stream.

The server listens on the loopback interface only and runs its own asyncio
event loop in a background thread, so that the main thread stays free for the
desktop window toolkit, which must own it. The engine runs in that loop and
streams its frames, its audio, its source state, its demodulation settings
and its bookmarks to each interface over a WebSocket (``/stream``; see
docs/design/protocol.md), which also carries the interfaces' changes, their
preferences and their requests for audio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from .engine import Engine, Source, Subscription
from .store import Store
from .synthetic import SyntheticBand

LOOPBACK_HOST = "127.0.0.1"
PROTOCOL_VERSION = 4
WEB_DIR = Path(__file__).with_name("web")

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0
_CLOSE_TIMEOUT_S = 2.0          # wait for a client to acknowledge a closed stream
_CLIENT_MESSAGE_LIMIT = 64 * 1024
_MAX_CHANGE_ID = 2 ** 53 - 1         # the largest whole number JavaScript counts exactly
_COMMANDS = ("source", "demod", "audio", "bookmark", "preferences")

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
_FILE_ROUTE = r"/{name:[a-z0-9][a-z0-9_-]*\.(?:css|js|json)}"


def create_app(port: int, engine: Engine) -> web.Application:
    """Build the web application for a server listening on ``port``.

    The application runs ``engine`` from its startup to its cleanup.
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

    async def stream(request: web.Request) -> web.StreamResponse:
        # Any page may open a WebSocket to any address, but browsers always
        # tell the page's origin: only the interface itself may connect.
        if request.headers.get("Origin", "").lower() not in allowed_origins:
            raise web.HTTPForbidden(text="Unexpected Origin header.\n")
        ws = web.WebSocketResponse(timeout=_CLOSE_TIMEOUT_S, compress=False,
                                   max_msg_size=_CLIENT_MESSAGE_LIMIT)
        await ws.prepare(request)
        try:
            await ws.send_json({"type": "hello", "protocol": PROTOCOL_VERSION})
        except ConnectionResetError:
            return ws
        subscription = engine.subscribe()
        sender = asyncio.ensure_future(_send(ws, subscription))
        receiver = asyncio.ensure_future(_receive(ws, engine, subscription))
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if not receiver.done():     # the engine stopped, or sending failed
                await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Radiolack is stopping.")
        finally:
            engine.unsubscribe(subscription)
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
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
    app.router.add_get("/", _index)
    app.router.add_get("/stream", stream)
    app.router.add_get(_FILE_ROUTE, _interface_file)
    app.on_shutdown.append(stop_streams)
    app.cleanup_ctx.append(run_engine)
    return app


async def _send(ws: web.WebSocketResponse, subscription: Subscription) -> None:
    """Forward the engine's messages to one client until the engine stops."""
    while (message := await subscription.get()) is not None:
        if isinstance(message, bytes):
            await ws.send_bytes(message)
        else:
            await ws.send_json(message)


async def _receive(ws: web.WebSocketResponse, engine: Engine, subscription: Subscription) -> None:
    """Apply the client's commands until it closes the stream. A refused
    command gets an error message, for this client only."""
    async for message in ws:
        if message.type is not WSMsgType.TEXT:
            continue
        kind: str | None = None
        try:
            kind, refusal = _apply_command(engine, subscription, message.data)
        except Exception:
            log.exception("a command from the interface failed")
            refusal = "the command failed"
        if refusal is not None:
            subscription.reply({"type": "error", "command": kind, "message": refusal})


def _apply_command(engine: Engine, subscription: Subscription, text: str) -> tuple[str | None, str | None]:
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
        elif kind == "preferences":
            values = changes.pop("values", None)
            if changes:
                return kind, f"unknown preferences field: {next(iter(changes))}"
            engine.set_preferences(values)
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


# Interface files are read at each request, so that a change shows on reload.

async def _index(request: web.Request) -> web.Response:
    return web.Response(
        body=(WEB_DIR / "index.html").read_bytes(),
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        },
    )


async def _interface_file(request: web.Request) -> web.Response:
    path = WEB_DIR / request.match_info["name"]
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        raise web.HTTPNotFound() from None
    return web.Response(
        body=body,
        content_type=_FILE_TYPES[path.suffix],
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _bind(port: int) -> socket.socket:
    """Return a listening TCP socket on the loopback interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            # Lets a restart reuse a fixed port still in TIME_WAIT. Windows
            # gives this option another meaning (sharing a busy port).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


class LocalServer:
    """Serve the interface on the loopback interface from a background thread.

    The port is bound in the constructor, so a busy port raises ``OSError``
    before anything starts. Use it as a context manager: entering starts the
    server, leaving stops it. The engine reads ``source``, by default the
    synthetic band, and closes it when the server stops. With a ``store``,
    the engine starts from the saved settings and saves their changes.
    """

    def __init__(self, port: int = 0, source: Source | None = None, store: Store | None = None) -> None:
        self._source = source
        self._store = store
        self._sock = _bind(port)
        self.port: int = self._sock.getsockname()[1]
        self.url = f"http://{LOOPBACK_HOST}:{self.port}/"
        self._thread = threading.Thread(target=self._run, name="radiolack-server", daemon=True)
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested: asyncio.Event | None = None
        self._error: BaseException | None = None

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
        """Stop serving and release the port. Safe to call more than once."""
        loop, stop_requested = self._loop, self._stop_requested
        if loop is not None and stop_requested is not None and self._thread.is_alive():
            try:
                loop.call_soon_threadsafe(stop_requested.set)
            except RuntimeError:  # The loop has just closed.
                pass
        if self._thread.is_alive():
            self._thread.join(_STOP_TIMEOUT_S)
        self._sock.close()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:  # Reported to the starting thread.
            self._error = error
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        engine = Engine(self._source if self._source is not None else SyntheticBand(), store=self._store)
        runner = web.AppRunner(create_app(self.port, engine), handle_signals=False)
        await runner.setup()
        try:
            await web.SockSite(runner, self._sock).start()
            self._stop_requested = asyncio.Event()
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            await self._stop_requested.wait()
        finally:
            engine.close()      # ends the streams, so that no client holds up the shutdown
            await runner.cleanup()
