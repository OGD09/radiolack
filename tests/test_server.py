"""Tests for the local web server, over real sockets on ephemeral ports."""

import asyncio
import html.parser
import http.client
import json
import os
import re
import socket
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath

import aiohttp

from radiolack import __version__
from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LOOPBACK_HOST, PROTOCOL_VERSION, LocalServer
from radiolack.sources import Sources
from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RTL_TCP, FakeRtlTcp, FakeRtlTcpRuns, FakeSource, FakeUpdates

# The states a stream of this computer's interface starts with, after hello, in this order.
STATES = ("source", "demod", "bookmarks", "scanner", "peaks", "preferences", "storage", "update", "remote")
FILE_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
# The band plan file main.js loads.
BAND_PLAN_URL = re.compile(r"^const BAND_PLAN_URL = '([a-z0-9-]+\.json)';$", re.MULTILINE)
# The audio worklet audio.js loads.
WORKLET_URL = re.compile(r"^const WORKLET_URL = '([a-z0-9-]+\.js)';$", re.MULTILINE)
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

    def test_serves_the_audio_worklet_the_interface_loads(self):
        name = WORKLET_URL.search(get(self.server, "/audio.js")[2].decode("utf-8")).group(1)
        status, headers, body = get(self.server, f"/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], FILE_TYPES[".js"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"registerProcessor(", body)

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

    async def start(self, ws) -> None:
        """Read the messages a stream starts with: hello, then the states."""
        await read_start(self, ws)

    async def test_sends_hello_then_the_states_then_spectrum_frames(self):
        async with self.connect(self.origin, compress=15) as ws:
            self.assertEqual(await ws.receive_json(timeout=5),
                             {"type": "hello", "protocol": 6, "remote": False, "scanners": ["peaks"]})
            self.assertEqual(PROTOCOL_VERSION, 6)
            self.assertEqual(ws.compress, 0)    # deflate, which browsers offer, is not worth it on the loopback
            state = await ws.receive_json(timeout=5)
            self.assertEqual((state["type"], state["kind"], state["centre_hz"]), ("source", "test", FAKE_CENTRE_HZ))
            settings = await ws.receive_json(timeout=5)
            self.assertEqual((settings["type"], settings["mode"], settings["acknowledged"]), ("demod", "NFM", 0))
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "bookmarks", "bookmarks": []})
            self.assertEqual(await ws.receive_json(timeout=5),
                             {"type": "scanner", "detection": True, "threshold_db": 12.0, "ignored_hz": []})
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "peaks", "peaks": []})
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "preferences", "values": {}})
            # Without a settings folder, nothing is saved.
            self.assertEqual(await ws.receive_json(timeout=5),
                             {"type": "storage", "directory": None, "saving": False, "problem": ""})
            # Without an update check, nothing checks.
            self.assertEqual(await ws.receive_json(timeout=5), {
                "type": "update", "status": "unavailable", "version": __version__, "latest": None,
                "detail": "This Radiolack does not check for updates.", "check_at_start": True})
            # Remote access is off, as at each start, and has never been on.
            self.assertEqual(await ws.receive_json(timeout=5), {
                "type": "remote", "on": False, "port": 8443, "urls": [], "code": None, "blocked": False,
                "devices": [], "fingerprint": None, "problem": ""})
            frames = 0
            while frames < 3:
                message = await ws.receive(timeout=5)
                if is_peaks(message):
                    continue
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
                kind, _, _, _, centre, *_ = FRAME_HEADER.unpack_from(message.data)
                self.assertEqual((kind, centre), (FRAME_KIND_SPECTRUM, FAKE_CENTRE_HZ))
                self.assertEqual(len(message.data), FRAME_HEADER.size + 8192)
                frames += 1

    async def test_a_refused_change_gets_an_error_for_its_sender_only(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "source", "centre_hz": 433e6})
            self.assertEqual(await next_text(sender), {
                "type": "error", "command": "source", "message": "the test source cannot change its settings"})
            await self.assert_no_text(other)

    async def test_a_demodulation_change_reaches_every_client_and_is_acknowledged(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "demod", "id": 5, "frequency_hz": 145_010_000})
            for ws, acknowledged in ((sender, 5), (other, 0)):
                state = await next_text(ws)
                self.assertEqual((state["type"], state["frequency_hz"], state["acknowledged"]),
                                 ("demod", 145_010_000.0, acknowledged))
            await sender.send_json({"type": "demod", "id": 6, "mode": "WFM"})
            state = await next_text(sender)
            self.assertEqual((state["type"], state["mode"], state["acknowledged"]), ("demod", "NFM", 6))
            error = await next_text(sender)
            self.assertEqual((error["type"], error["command"]), ("error", "demod"))
            await self.assert_no_text(other)

    async def test_a_bookmark_change_reaches_every_client(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "bookmark", "action": "add", "frequency_hz": 145_500_000,
                                    "name": "Calling", "mode": "NFM", "scan": False})
            for ws in (sender, other):
                self.assertEqual(await next_text(ws), {"type": "bookmarks", "bookmarks": [
                    {"id": 1, "frequency_hz": 145_500_000.0, "name": "Calling", "mode": "NFM", "scan": False}]})
            await sender.send_json({"type": "bookmark", "action": "remove", "id": 1})
            for ws in (sender, other):
                self.assertEqual(await next_text(ws), {"type": "bookmarks", "bookmarks": []})
            await other.send_json({"type": "bookmark", "action": "remove", "id": 1})
            self.assertEqual(await next_text(other), {
                "type": "error", "command": "bookmark",
                "message": "no such bookmark: another interface may have removed it"})
            await self.assert_no_text(sender)

    async def test_a_scanner_change_reaches_every_client(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "scanner", "ignore": 145_500_000})
            for ws in (sender, other):
                self.assertEqual(await next_text(ws), {"type": "scanner", "detection": True, "threshold_db": 12.0,
                                                       "ignored_hz": [145_500_000.0]})
            await sender.send_json({"type": "scanner", "detection": "off"})
            self.assertEqual(await next_text(sender), {
                "type": "error", "command": "scanner", "message": "detection must be turned on or off: true or false"})
            await self.assert_no_text(other)

    async def test_preferences_are_kept_for_the_next_interfaces(self):
        values = {"palette": "gray", "volume": 0.5, "showPlan": False, "top": None}
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "preferences", "values": values})
            await sender.send_json({"type": "preferences", "values": [1]})
            # The first answer is the refusal: open interfaces keep their own preferences.
            self.assertEqual(await next_text(sender), {
                "type": "error", "command": "preferences", "message": "the preferences must be a JSON object"})
            await self.assert_no_text(other)
        async with self.connect(self.origin) as later:
            for kind in ("hello", "source", "demod", "bookmarks", "scanner", "peaks"):
                self.assertEqual((await later.receive_json(timeout=5))["type"], kind)
            self.assertEqual(await later.receive_json(timeout=5), {"type": "preferences", "values": values})

    async def test_audio_flows_to_a_client_that_asks(self):
        async with self.connect(self.origin) as ws:
            await self.start(ws)
            await ws.send_json({"type": "audio", "on": True})
            while (message := await ws.receive(timeout=5)).data[0] != FRAME_KIND_AUDIO:
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
            kind, version, _, _, rate = AUDIO_HEADER.unpack_from(message.data)
            self.assertEqual((kind, version, rate), (FRAME_KIND_AUDIO, AUDIO_VERSION, 12_000.0))
            self.assertEqual((len(message.data) - AUDIO_HEADER.size) % 2, 0)
            await ws.send_json({"type": "audio", "on": False})
            await ws.send_str("[1]")    # its error marks the point after which audio stopped
            self.assertEqual((await next_text(ws))["message"], "unknown command")
            deadline = time.monotonic() + 0.3
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    message = await ws.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    break
                self.assertNotEqual(message.data[0], FRAME_KIND_AUDIO)

    async def test_malformed_commands_get_an_error(self):
        commands = {
            "not json": (None, "a command must be a JSON object"),
            "[" * 60000: (None, "a command must be a JSON object"),
            "[1]": (None, "unknown command"),
            '{"type": "tune"}': (None, "unknown command"),
            '{"type": "source", "volume": 3}': ("source", "the test source cannot change its settings"),
            '{"type": "demod", "volume": 3}': ("demod", "unknown demodulator setting: volume"),
            '{"type": "demod", "bandwidth_hz": 1' + "0" * 400 + "}": (     # too large for a float
                "demod", "the filter width must be between 100 and 25000"),
            '{"type": "demod", "id": 0, "mode": "AM"}': ("demod", "a change id must be a whole number from 1"),
            '{"type": "demod", "id": 1.5}': ("demod", "a change id must be a whole number from 1"),
            '{"type": "demod", "id": true}': ("demod", "a change id must be a whole number from 1"),
            '{"type": "demod", "id": 9007199254740992}': ("demod", "a change id must be a whole number from 1"),
            '{"type": "audio"}': ("audio", "audio must be turned on or off: true or false"),
            '{"type": "audio", "on": 1}': ("audio", "audio must be turned on or off: true or false"),
            '{"type": "audio", "on": true, "volume": 3}': ("audio", "unknown audio setting: volume"),
            '{"type": "bookmark", "action": "rename"}': (
                "bookmark", 'a bookmark action is "add", "change" or "remove"'),
            '{"type": "bookmark", "action": "add", "name": "A", "mode": "NFM"}': (
                "bookmark", "a bookmark needs a frequency"),
            '{"type": "preferences"}': ("preferences", "the preferences must be a JSON object"),
            '{"type": "preferences", "values": {}, "theme": "dark"}': (
                "preferences", "unknown preferences field: theme"),
            '{"type": "preferences", "values": {"palette": [1]}}': (
                "preferences", "the preference 'palette' must be a number, true or false, a text of at most "
                               "100 characters or null"),
            '{"type": "scanner"}': ("scanner", "a scanner message needs a setting"),
            '{"type": "scanner", "sweep": true}': ("scanner", "unknown scanner setting: sweep"),
            '{"type": "scanner", "threshold_db": 40}': (
                "scanner", "the threshold must be a number of dB from 6 to 30"),
            '{"type": "scanner", "ignore": 145e6, "restore": true}': (
                "scanner", "ignore and restore come alone in their message"),
            '{"type": "update", "install": true}': ("update", "unknown update setting: install"),
            '{"type": "update", "check": 1}': ("update", "check and check_at_start must be true or false"),
            '{"type": "update", "check_at_start": "no"}': (
                "update", "check and check_at_start must be true or false"),
        }
        async with self.connect(self.origin) as ws:
            await self.start(ws)
            await ws.send_bytes(b"ignored")
            for command, (kind, error) in commands.items():
                with self.subTest(command=command[:20]):
                    await ws.send_str(command)
                    self.assertEqual(await next_text(ws), {"type": "error", "command": kind, "message": error})

    async def assert_no_text(self, ws, duration: float = 0.3):
        """Only frames, and the signals found, arrive on ``ws`` for a while."""
        deadline = time.monotonic() + duration
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                message = await ws.receive(timeout=remaining)
            except asyncio.TimeoutError:
                return
            if not is_peaks(message):
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)

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


class SavedSettingsTest(unittest.IsolatedAsyncioTestCase):
    """Settings, bookmarks and preferences outlive the server that kept them."""

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def serve(self) -> LocalServer:
        server = LocalServer(source=FakeSource(), store=Store(self.directory))
        server.start()
        self.addCleanup(server.stop)
        return server

    def connect(self, server: LocalServer):
        return self.session.ws_connect(f"{server.url}stream", origin=f"http://{LOOPBACK_HOST}:{server.port}")

    async def test_a_restarted_server_starts_from_them(self):
        values = {"palette": "gray", "volume": 0.5}
        bookmark = {"frequency_hz": 145_500_000.0, "name": "Calling", "mode": "NFM", "scan": True}
        first = self.serve()
        async with self.connect(first) as ws:
            states = await read_start(self, ws)
            self.assertEqual(states["storage"], {"type": "storage", "directory": Store(self.directory).location,
                                                 "saving": True, "problem": ""})
            # Commands apply in order: the last answer comes once all three apply.
            await ws.send_json({"type": "preferences", "values": values})
            await ws.send_json({"type": "demod", "id": 1, "frequency_hz": 145_010_000, "squelch_auto": False})
            await ws.send_json({"type": "bookmark", "action": "add", **bookmark})
            while (await next_text(ws))["type"] != "bookmarks":
                pass
        await asyncio.to_thread(first.stop)     # saves what changed less than a second ago
        settings = json.loads((self.directory / SETTINGS_FILE).read_text("utf-8"))
        self.assertEqual((settings["version"], settings["interface"]), (1, values))
        self.assertEqual(settings["receivers"]["test"]["demod"]["frequency_hz"], 145_010_000.0)
        self.assertEqual(json.loads((self.directory / BOOKMARKS_FILE).read_text("utf-8")),
                         {"version": 1, "bookmarks": [bookmark]})

        second = self.serve()
        async with self.connect(second) as ws:
            states = await read_start(self, ws)
        self.assertEqual((states["demod"]["frequency_hz"], states["demod"]["squelch_auto"],
                          states["demod"]["acknowledged"]), (145_010_000.0, False, 0))
        self.assertEqual(states["bookmarks"]["bookmarks"], [{"id": 1, **bookmark}])
        self.assertEqual(states["preferences"]["values"], values)
        self.assertEqual((states["storage"]["saving"], states["storage"]["problem"]), (True, ""))


class UpdateStreamTest(unittest.IsolatedAsyncioTestCase):
    """An interface asks the engine to check for updates, and turns the check
    at start off; every interface learns the outcome."""

    async def asyncSetUp(self):
        self.updates = FakeUpdates(UpdateResult("available", latest="9.9.9"))
        self.server = LocalServer(source=FakeSource(), updates=self.updates)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.origin = f"http://{LOOPBACK_HOST}:{self.server.port}"

    def connect(self):
        return self.session.ws_connect(f"{self.server.url}stream", origin=self.origin)

    @staticmethod
    async def next_update(ws) -> dict:
        while (message := await next_text(ws))["type"] != "update":
            continue
        return message

    async def test_every_interface_learns_what_the_checks_find(self):
        async with self.connect() as first, self.connect() as other:
            for ws in (first, other):
                state = (await read_start(self, ws))["update"]
                while state["status"] != "available":       # the check at start
                    state = await self.next_update(ws)
                self.assertEqual((state["latest"], state["check_at_start"]), ("9.9.9", True))
            await first.send_json({"type": "update", "check": True})
            for ws in (first, other):
                self.assertEqual((await self.next_update(ws))["status"], "checking")
                self.assertEqual((await self.next_update(ws))["status"], "available")
            self.assertEqual(self.updates.checks, 2)
            await other.send_json({"type": "update", "check_at_start": False})
            for ws in (first, other):
                state = await self.next_update(ws)
                self.assertEqual((state["status"], state["check_at_start"]), ("available", False))


async def read_start(test: unittest.TestCase, ws) -> dict[str, dict]:
    """Read the messages a stream starts with, hello then the states; return
    the states by type."""
    test.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")
    states = [await ws.receive_json(timeout=5) for _ in STATES]
    test.assertEqual(tuple(state["type"] for state in states), STATES)
    return {state["type"]: state for state in states}


def is_peaks(message) -> bool:
    """Whether a stream message is the list of the signals found, which the
    engine sends whenever it changes, like frames, and not in answer."""
    return message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get("type") == "peaks"


async def next_text(ws) -> dict:
    """The next JSON message, skipping frames and the signals found."""
    while True:
        message = await ws.receive(timeout=5)
        if message.type == aiohttp.WSMsgType.TEXT and not is_peaks(message):
            return json.loads(message.data)
        if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            raise AssertionError(f"the stream ended: {message}")


async def next_frame_centre(ws) -> float:
    """The centre frequency of the next frame, skipping JSON messages."""
    while True:
        message = await ws.receive(timeout=5)
        if message.type == aiohttp.WSMsgType.BINARY:
            return FRAME_HEADER.unpack_from(message.data)[4]
        if message.type != aiohttp.WSMsgType.TEXT:
            raise AssertionError(f"the stream ended: {message}")


class RtlTcpStreamTest(unittest.IsolatedAsyncioTestCase):
    """The whole path, from a fake rtl_tcp server to two interfaces."""

    async def asyncSetUp(self):
        self.fake = FakeRtlTcp(chunk_bytes=8000)
        await self.fake.__aenter__()
        self.addAsyncCleanup(self.fake.__aexit__, None, None, None)
        self.server = LocalServer(source=RtlTcpSource(LOOPBACK_HOST, self.fake.port, idle_s=0.5))
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def connect(self):
        return self.session.ws_connect(f"{self.server.url}stream",
                                       origin=f"http://{LOOPBACK_HOST}:{self.server.port}")

    async def test_a_change_from_one_interface_retunes_the_receiver_for_all(self):
        async with self.connect() as first, self.connect() as second:
            for ws in (first, second):
                self.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")
                self.assertEqual((await ws.receive_json(timeout=5))["kind"], "rtl_tcp")
                self.assertEqual(await next_frame_centre(ws), 145e6)
            await first.send_json({"type": "source", "centre_hz": 433_000_000, "gain_db": 30, "ppm": -3,
                                   "dc_removal": False})
            for ws in (first, second):
                while (state := await next_text(ws))["centre_hz"] != 433e6:
                    pass
                self.assertEqual(
                    (state["status"], state["tuner"], state["gain_db"], state["ppm"], state["dc_removal"]),
                    ("streaming", "Rafael Micro R820T", 29.7, -3, False))
                while await next_frame_centre(ws) != 433e6:
                    pass
            self.assertIn((0x01, 433_000_000), self.fake.commands)
            self.assertIn((0x04, 297), self.fake.commands)
            self.assertIn((0x05, 2**32 - 3), self.fake.commands)

    async def test_an_interface_moves_the_receiver_to_another_address_for_all(self):
        other = FakeRtlTcp(chunk_bytes=8000)
        await other.__aenter__()
        self.addAsyncCleanup(other.__aexit__, None, None, None)
        address = f"{LOOPBACK_HOST}:{other.port}"
        async with self.connect() as first, self.connect() as second:
            for ws in (first, second):
                self.assertEqual(await next_frame_centre(ws), 145e6)
            await first.send_json({"type": "source", "address": str(other.port), "ppm": -3})
            for ws in (first, second):
                while (state := await next_text(ws))["address"] != address or state["status"] != "streaming":
                    pass
                self.assertEqual((state["centre_hz"], state["ppm"]), (145e6, -3))
                self.assertEqual(await next_frame_centre(ws), 145e6)
            self.assertEqual(other.connections, 1)
            self.assertIn((0x05, 2**32 - 3), other.commands)
            deadline = time.monotonic() + 5
            while self.fake.open_connections:
                self.assertLess(time.monotonic(), deadline, "the first rtl_tcp is still connected")
                await asyncio.sleep(0.05)

    async def test_hangs_up_once_no_interface_listens(self):
        async with self.connect() as ws:
            await next_frame_centre(ws)
            self.assertEqual(self.fake.open_connections, 1)
        deadline = time.monotonic() + 5
        while self.fake.open_connections:
            self.assertLess(time.monotonic(), deadline, "rtl_tcp is still connected")
            await asyncio.sleep(0.05)


@unittest.skipUnless(os.name == "posix", "POSIX processes and signals")
class RtlSdrStreamTest(unittest.IsolatedAsyncioTestCase):
    """The server's event loop, in its own thread, runs the fake rtl_tcp
    program for an interface, and stops it before the server stops."""

    async def asyncSetUp(self):
        self.runs = FakeRtlTcpRuns(self)
        self.server = LocalServer(source=RtlSdrSource(program=FAKE_RTL_TCP))
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    async def test_stopping_the_server_stops_rtl_tcp(self):
        async with self.session.ws_connect(f"{self.server.url}stream",
                                           origin=f"http://{LOOPBACK_HOST}:{self.server.port}") as ws:
            self.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")
            state = await ws.receive_json(timeout=5)
            self.assertEqual((state["kind"], state["address"]), ("rtl_sdr", None))
            self.assertEqual(await next_frame_centre(ws), 145e6)
            [(pid, _)] = self.runs.started()
            await asyncio.to_thread(self.server.stop)
        self.assertEqual(self.runs.stopping(), {pid})
        with self.assertRaises(ProcessLookupError):     # ended, and its exit status collected
            os.kill(pid, 0)


@unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
class SourceSwitchStreamTest(unittest.IsolatedAsyncioTestCase):
    """An interface switches the engine's source for every interface: the
    simulated band, then the fake rtl_tcp program as a receiver on this
    computer, then the simulated band again."""

    async def asyncSetUp(self):
        self.runs = FakeRtlTcpRuns(self)
        self.server = LocalServer(sources=Sources(rtl_tcp_program=FAKE_RTL_TCP))
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def connect(self):
        return self.session.ws_connect(f"{self.server.url}stream",
                                       origin=f"http://{LOOPBACK_HOST}:{self.server.port}")

    @staticmethod
    async def next_of(ws, kind: str) -> dict:
        """The next JSON message of type ``kind``."""
        while (message := await next_text(ws))["type"] != kind:
            pass
        return message

    async def test_an_interface_switches_the_source_for_all(self):
        async with self.connect() as first, self.connect() as second:
            for ws in (first, second):
                self.assertEqual((await ws.receive_json(timeout=5))["type"], "hello")
                state = await ws.receive_json(timeout=5)
                self.assertEqual((state["kind"], state["kinds"]), ("synthetic", ["synthetic", "rtl_sdr", "rtl_tcp"]))
            await first.send_json({"type": "source", "kind": "rtl_sdr", "ppm": 1})
            self.assertEqual(await self.next_of(first, "error"), {
                "type": "error", "command": "source",
                "message": "the kind of source changes on its own, without other settings"})
            await first.send_json({"type": "source", "kind": "rtl_sdr"})
            for ws in (first, second):
                state = await self.next_of(ws, "source")
                while (state["kind"], state["status"]) != ("rtl_sdr", "streaming"):
                    state = await self.next_of(ws, "source")
                self.assertEqual(state["tuner"], "Rafael Micro R820T")
            [(pid, _)] = self.runs.started()
            await second.send_json({"type": "source", "kind": "synthetic"})
            for ws in (first, second):
                while (await self.next_of(ws, "source"))["kind"] != "synthetic":
                    pass
            deadline = time.monotonic() + 5
            while self.runs.stopping() != {pid}:
                self.assertLess(time.monotonic(), deadline, "rtl_tcp still runs")
                await asyncio.sleep(0.05)


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
