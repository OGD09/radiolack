"""Tests for the local web server, over real sockets on ephemeral ports."""

import asyncio
import html.parser
import http.client
import json
import re
import socket
import time
import unittest
from pathlib import PurePosixPath

import aiohttp

from radiolack.server import LOOPBACK_HOST, PROTOCOL_VERSION, LocalServer
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM
from tests.fakes import FAKE_CENTRE_HZ, FakeSource

FILE_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
# The band plan file main.js loads.
BAND_PLAN_URL = re.compile(r"^const BAND_PLAN_URL = '([a-z0-9-]+\.json)';$", re.MULTILINE)
# Static imports and re-exports of the interface's own modules.
MODULE_IMPORT = re.compile(r"""^\s*(?:import|export)\b[^;'"]*?['"](\./[^'"]+)['"]""", re.MULTILINE)


class PageParser(html.parser.HTMLParser):
    """Collect the files a page loads and the inline code it contains."""

    def __init__(self):
        super().__init__()
        self.scripts: list[tuple[str, str | None]] = []
        self.stylesheets: list[str] = []
        self.inline: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            if values.get("src"):
                self.scripts.append((values["src"], values.get("type")))
            else:
                self.inline.append("<script>")
        elif tag == "style":
            self.inline.append("<style>")
        elif tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(values["href"])
        self.inline.extend(f"{tag} {name}=" for name, _ in attrs if name == "style" or name.startswith("on"))


def get(server: LocalServer, path: str = "/", host: str | None = None):
    """Send a GET request; return the status, headers and body."""
    connection = http.client.HTTPConnection(LOOPBACK_HOST, server.port, timeout=5)
    try:
        headers = {"Host": host} if host is not None else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


class LocalServerTest(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_listens_on_loopback_ephemeral_port(self):
        self.assertGreater(self.server.port, 0)
        self.assertEqual(self.server.url, f"http://{LOOPBACK_HOST}:{self.server.port}/")

    def test_serves_interface(self):
        status, headers, body = get(self.server)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"<title>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_policy_forbids_inline_code_and_page_has_none(self):
        _, headers, body = get(self.server)
        policy = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", policy)
        self.assertIn("style-src 'self';", policy)
        self.assertNotIn("unsafe", policy)
        page = PageParser()
        page.feed(body.decode("utf-8"))
        self.assertEqual(page.inline, [])

    def test_serves_the_files_the_page_loads(self):
        page = PageParser()
        page.feed(get(self.server)[2].decode("utf-8"))
        self.assertEqual(page.scripts, [("main.js", "module")])
        self.assertEqual(page.stylesheets, ["style.css"])
        for name in [src for src, _ in page.scripts] + page.stylesheets:
            with self.subTest(name=name):
                status, headers, body = get(self.server, f"/{name}")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], FILE_TYPES[PurePosixPath(name).suffix])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(body)

    def test_serves_every_module_the_interface_imports(self):
        pending, seen = ["main.js"], set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            status, headers, body = get(self.server, f"/{name}")
            with self.subTest(module=name):
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], FILE_TYPES[".js"])
            pending.extend(spec[2:] for spec in MODULE_IMPORT.findall(body.decode("utf-8")))
        self.assertIn("stream.js", seen)

    def test_serves_the_band_plan_the_interface_loads(self):
        name = BAND_PLAN_URL.search(get(self.server, "/main.js")[2].decode("utf-8")).group(1)
        status, headers, body = get(self.server, f"/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], FILE_TYPES[".json"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(json.loads(body)["format"], 1)

    def test_serves_nothing_outside_the_interface_files(self):
        paths = ("/server.py", "/../server.py", "/%2e%2e/server.py", "/..%2fserver.py",
                 "/web/main.js", "/MAIN.JS", "/main.js.map", "/missing.js", "/missing.json", "/index.html")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(get(self.server, path)[0], 404)

    def test_accepts_localhost_name(self):
        status, _, _ = get(self.server, host=f"localhost:{self.server.port}")
        self.assertEqual(status, 200)

    def test_refuses_other_host_names(self):
        for host in (f"radiolack.example:{self.server.port}", f"{LOOPBACK_HOST}:1", "localhost"):
            with self.subTest(host=host):
                status, _, _ = get(self.server, host=host)
                self.assertEqual(status, 403)

    def test_unknown_path(self):
        status, _, _ = get(self.server, "/missing")
        self.assertEqual(status, 404)


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = LocalServer(source=FakeSource())
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.url = f"{self.server.url}stream"
        self.origin = f"http://{LOOPBACK_HOST}:{self.server.port}"

    def connect(self, origin: str | None, **kwargs):
        """Open the stream: await the result, or use it with ``async with``."""
        return self.session.ws_connect(self.url, origin=origin, **kwargs)

    async def test_sends_hello_then_spectrum_frames(self):
        async with self.connect(self.origin) as ws:
            self.assertEqual(await ws.receive_json(timeout=5),
                             {"type": "hello", "protocol": PROTOCOL_VERSION, "source": "test"})
            for _ in range(3):
                message = await ws.receive(timeout=5)
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
                kind, _, _, _, centre, *_ = FRAME_HEADER.unpack_from(message.data)
                self.assertEqual((kind, centre), (FRAME_KIND_SPECTRUM, FAKE_CENTRE_HZ))
                self.assertEqual(len(message.data), FRAME_HEADER.size + 8192)

    async def test_accepts_the_localhost_origin(self):
        origin = f"http://localhost:{self.server.port}"
        async with self.connect(origin, headers={"Host": f"localhost:{self.server.port}"}) as ws:
            self.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")

    async def test_refuses_other_origins(self):
        port = self.server.port
        for origin in (None, "null", "http://radiolack.example", f"http://{LOOPBACK_HOST}:{port + 1}",
                       f"https://{LOOPBACK_HOST}:{port}"):
            with self.subTest(origin=origin):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                    await self.connect(origin)
                self.assertEqual(caught.exception.status, 403)

    async def test_refuses_other_host_names(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.connect(self.origin, headers={"Host": f"radiolack.example:{self.server.port}"})
        self.assertEqual(caught.exception.status, 403)

    async def test_stop_closes_open_streams_promptly(self):
        ws = await self.connect(self.origin)
        await ws.receive_json(timeout=5)

        async def read_until_closed():
            async for _message in ws:
                pass

        reader = asyncio.ensure_future(read_until_closed())
        started = time.monotonic()
        await asyncio.to_thread(self.server.stop)
        self.assertLess(time.monotonic() - started, 3.0)
        await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(ws.close_code, aiohttp.WSCloseCode.GOING_AWAY)


class LocalServerLifecycleTest(unittest.TestCase):
    def test_stop_releases_port_and_is_idempotent(self):
        server = LocalServer()
        with server:
            self.assertEqual(get(server)[0], 200)
        server.stop()
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((LOOPBACK_HOST, server.port), timeout=5).close()

    def test_busy_port_raises_before_starting(self):
        with socket.socket() as busy:
            busy.bind((LOOPBACK_HOST, 0))
            busy.listen()
            with self.assertRaises(OSError):
                LocalServer(busy.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
