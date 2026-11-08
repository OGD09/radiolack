"""Local web server that serves the Radiolack interface.

The server listens on the loopback interface only and runs its own asyncio
event loop in a background thread, so that the main thread stays free for the
desktop window toolkit, which must own it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from pathlib import Path

from aiohttp import web

LOOPBACK_HOST = "127.0.0.1"
WEB_DIR = Path(__file__).with_name("web")

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0

# The interface is one page with inline scripts and styles. It may only talk
# to this server, and other pages may not frame it.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
))


def create_app(port: int) -> web.Application:
    """Build the web application for a server listening on ``port``."""
    names = (LOOPBACK_HOST, "localhost")
    allowed_hosts = {f"{name}:{port}" for name in names}
    if port == 80:  # Browsers omit the default port from the Host header.
        allowed_hosts.update(names)

    @web.middleware
    async def check_host(request: web.Request, handler):
        # A page from another site can reach a loopback server through DNS
        # rebinding, but its requests then carry that site's host name.
        if (request.host or "").lower() not in allowed_hosts:
            raise web.HTTPForbidden(text="Unexpected Host header.\n")
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    app = web.Application(middlewares=[check_host])
    app.router.add_get("/", _index)
    return app


async def _index(request: web.Request) -> web.Response:
    # Read at each request so that a changed interface shows on reload.
    return web.Response(
        body=(WEB_DIR / "index.html").read_bytes(),
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        },
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
    server, leaving stops it.
    """

    def __init__(self, port: int = 0) -> None:
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
        runner = web.AppRunner(create_app(self.port), handle_signals=False)
        await runner.setup()
        try:
            await web.SockSite(runner, self._sock).start()
            self._stop_requested = asyncio.Event()
            self._loop = asyncio.get_running_loop()
            self._ready.set()
            await self._stop_requested.wait()
        finally:
            await runner.cleanup()
