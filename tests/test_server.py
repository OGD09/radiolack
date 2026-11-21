"""Tests for the local web server, over real sockets on ephemeral ports."""

import asyncio
import html.parser
import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import time
import unittest
import wave
from pathlib import Path, PurePosixPath
from unittest import mock

import aiohttp

from radiolack import __version__, report
from radiolack.recordings import meta_name, wav_header
from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LOOPBACK_HOST, PROTOCOL_VERSION, WEB_DIR, InterfaceUnreadable, LocalServer
from radiolack.sources import Sources
from radiolack.demod import AUDIO_HEADER, AUDIO_VERSION, FRAME_KIND_AUDIO
from radiolack.fine import FINE_SIZE, FINE_VERSION, FRAME_KIND_FINE
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import BOOKMARKS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_CENTRE_HZ, FAKE_RTL_TCP, FakeRtlTcp, FakeRtlTcpRuns, FakeSource, FakeUpdates

# The states a stream of this computer's interface starts with, after hello, in this order.
STATES = ("source", "demod", "bookmarks", "scanner", "peaks", "preferences", "recording", "scans", "calibration",
          "storage", "update", "remote")
RECORDING_NAME = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"
IQ_RECORDING_NAME = "2026-11-06_21-20-00Z_145.000000MHz_2.400000MSps.sigmf-data"
# The scanner state of an engine without a scan or a ticked bookmark, with the default settings.
SCANNER_STATE = {"type": "scanner", "detection": True, "threshold_db": 12.0, "ignored_hz": [], "resume_s": 2.0,
                 "listen_s": 10.0, "sweep_low_hz": 430e6, "sweep_high_hz": 440e6, "sweep_window_s": 0.5,
                 "sweep_listen": False, "sweep_passes": None, "scan": None, "stopped": None, "stopped_scan": None,
                 "memory": {"watched": [], "windows": 0, "window": None, "state": None, "bookmark_id": None},
                 "sweep": {"windows": None, "window": None, "passes": None, "passes_set": None},
                 "signals": {"state": None, "frequency_hz": None}}
FILE_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
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
        self.icons: list[tuple[str, str]] = []
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
        elif tag == "link" and values.get("rel") in ("icon", "apple-touch-icon"):
            self.icons.append((values["rel"], values["href"]))
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
        self.assertEqual(page.icons, [("icon", "favicon.svg"), ("apple-touch-icon", "apple-touch-icon.png")])
        for name in [src for src, _ in page.scripts] + page.stylesheets + [href for _, href in page.icons]:
            with self.subTest(name=name):
                status, headers, body = get(self.server, f"/{name}")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], FILE_TYPES[PurePosixPath(name).suffix])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(body, (WEB_DIR / name).read_bytes())

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
        self.assertEqual(json.loads(body)["format"], 2)

    def test_serves_the_audio_worklet_the_interface_loads(self):
        name = WORKLET_URL.search(get(self.server, "/audio.js")[2].decode("utf-8")).group(1)
        status, headers, body = get(self.server, f"/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], FILE_TYPES[".js"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"registerProcessor(", body)

    def test_serves_nothing_outside_the_interface_files(self):
        paths = ("/server.py", "/../server.py", "/%2e%2e/server.py", "/..%2fserver.py",
                 "/web/main.js", "/MAIN.JS", "/main.js.map", "/missing.js", "/missing.json", "/index.html",
                 "/missing.svg", "/missing.png", "/favicon.ico", "/apple-touch-icon-precomposed.png")
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


class InterfaceReadAtStartTest(unittest.TestCase):
    """The server serves the interface's files as it read them when made,
    whatever happens to them afterwards, as when a newer package replaces
    them while Radiolack runs. The files are a copy of the interface's."""

    def setUp(self):
        self.web = Path(self.enterContext(tempfile.TemporaryDirectory())) / "web"
        shutil.copytree(WEB_DIR, self.web)
        self.enterContext(mock.patch("radiolack.server.WEB_DIR", self.web))

    def serve(self) -> LocalServer:
        server = LocalServer()
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_serves_the_files_as_they_were_read(self):
        server = self.serve()
        page, script = get(server)[2], get(server, "/main.js")[2]
        (self.web / "index.html").write_text("<p>Newer</p>\n", encoding="utf-8")
        (self.web / "main.js").write_text("// Newer\n", encoding="utf-8")
        (self.web / "style.css").unlink()
        (self.web / "newer.js").write_text("// Newer\n", encoding="utf-8")
        self.assertEqual(get(server)[2], page)
        self.assertEqual(get(server, "/main.js")[2], script)
        status, headers, body = get(server, "/style.css")
        self.assertEqual((status, headers["Content-Type"]), (200, FILE_TYPES[".css"]))
        self.assertTrue(body)
        self.assertEqual(get(server, "/newer.js")[0], 404)

    def test_an_unreadable_file_stops_the_start_before_the_port_is_bound(self):
        (self.web / "pair.html").unlink()
        with mock.patch("radiolack.server._bind") as bind, self.assertRaises(InterfaceUnreadable) as raised:
            self.serve()
        bind.assert_not_called()
        self.assertTrue(str(raised.exception).startswith(f"cannot read the interface: {self.web / 'pair.html'}: "),
                        str(raised.exception))


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
                             {"type": "hello", "protocol": 6, "version": __version__, "remote": False,
                              "scanners": ["peaks", "memory", "history", "sweep", "signals", "scans"], "recording": ["audio", "iq"],
                              "measures": ["carrier", "driver_pll", "calibration"], "spectra": ["fine"]})
            self.assertEqual(PROTOCOL_VERSION, 6)
            self.assertEqual(ws.compress, 0)    # deflate, which browsers offer, is not worth it on the loopback
            state = await ws.receive_json(timeout=5)
            self.assertEqual((state["type"], state["kind"], state["centre_hz"]), ("source", "test", FAKE_CENTRE_HZ))
            settings = await ws.receive_json(timeout=5)
            self.assertEqual((settings["type"], settings["mode"], settings["acknowledged"]), ("demod", "NFM", 0))
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "bookmarks", "bookmarks": [], "categories": []})
            self.assertEqual(await ws.receive_json(timeout=5),
                             SCANNER_STATE)
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "peaks", "peaks": []})
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "preferences", "values": {}})
            # Without a recordings folder, nothing is recorded.
            self.assertEqual(await ws.receive_json(timeout=5),
                             {"type": "recording", "audio": None, "iq": None, "stopped": None, "folder": None,
                              "default_folder": None})
            self.assertEqual(await ws.receive_json(timeout=5), {"type": "scans", "scans": []})
            self.assertEqual((await ws.receive_json(timeout=5))["type"], "calibration")
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
            await sender.send_json({"type": "demod", "id": 6, "mode": "DSB"})
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
                    {"id": 1, "frequency_hz": 145_500_000.0, "name": "Calling", "mode": "NFM", "scan": False,
                     "category": None}], "categories": []})
            await sender.send_json({"type": "bookmark", "action": "remove", "id": 1})
            for ws in (sender, other):
                self.assertEqual(await next_text(ws), {"type": "bookmarks", "bookmarks": [], "categories": []})
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
                self.assertEqual(await next_text(ws), {**SCANNER_STATE, "ignored_hz": [145_500_000.0]})
            await sender.send_json({"type": "scanner", "detection": "off"})
            self.assertEqual(await next_text(sender), {
                "type": "error", "command": "scanner", "message": "detection must be turned on or off: true or false"})
            await self.assert_no_text(other)

    async def test_any_client_starts_and_stops_the_memory_scan(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "bookmark", "action": "add", "frequency_hz": FAKE_CENTRE_HZ + 50_000,
                                    "name": "Quiet", "mode": "NFM", "scan": True})
            for ws in (sender, other):
                [bookmark] = (await next_text(ws))["bookmarks"]
                self.assertEqual(await next_text(ws), {
                    **SCANNER_STATE, "memory": {"watched": [bookmark["id"]], "windows": 1, "window": None,
                                                "state": None, "bookmark_id": None}})
            await sender.send_json({"type": "scanner", "scan": "memory"})
            for ws in (sender, other):
                state = await next_text(ws)
                self.assertEqual((state["scan"], state["memory"]["state"], state["memory"]["window"]),
                                 ("memory", "watching", 0))
            await other.send_json({"type": "scanner", "scan": None})
            for ws in (sender, other):
                state = await next_text(ws)
                self.assertEqual((state["scan"], state["stopped"], state["memory"]["state"]), (None, None, None))

    async def test_the_observations_go_to_the_client_that_asks(self):
        async with self.connect(self.origin) as sender, self.connect(self.origin) as other:
            for ws in (sender, other):
                await self.start(ws)
            await sender.send_json({"type": "observations", "clear_all": True})
            self.assertEqual(await next_text(sender), {"type": "observations", "channels": []})
            for _ in range(100):    # the fake source's tone, found and recorded again
                await sender.send_json({"type": "observations"})
                if channels := (await next_text(sender))["channels"]:
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("nothing was recorded")
            [channel] = channels
            self.assertEqual(sorted(channel), ["active_s", "channel_hz", "first_s", "frequency_hz", "hours",
                                               "last_s", "margin_db", "transmissions"])
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

    async def test_fine_frames_flow_to_a_client_that_asks_until_it_stops(self):
        async with self.connect(self.origin) as ws, self.connect(self.origin) as other:
            await self.start(ws)
            await self.start(other)
            # Off without having asked, then twice on: no error, one stream of frames.
            for on in (False, True, True):
                await ws.send_json({"type": "fine", "on": on})
            sequences = []
            while len(sequences) < 3:
                message = await ws.receive(timeout=5)
                self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
                if message.data[0] == FRAME_KIND_FINE:
                    kind, version, flags, sequence, centre, width, *_ = FRAME_HEADER.unpack_from(message.data)
                    self.assertEqual((kind, version, flags, width), (FRAME_KIND_FINE, FINE_VERSION, 0, 48_000.0))
                    self.assertEqual(len(message.data) - FRAME_HEADER.size, FINE_SIZE)
                    sequences.append(sequence)
            self.assertEqual(sequences, list(range(sequences[0], sequences[0] + 3)))
            await ws.send_json({"type": "fine", "on": False})
            await ws.send_str("[1]")    # its error marks the point after which the fine frames stopped
            self.assertEqual((await next_text(ws))["message"], "unknown command")
            # Neither it since, nor the client that never asked, at any time.
            for client in (ws, other):
                deadline = time.monotonic() + 0.3
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        message = await client.receive(timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if message.type == aiohttp.WSMsgType.BINARY:
                        self.assertNotEqual(message.data[0], FRAME_KIND_FINE)

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
            '{"type": "fine"}': ("fine", "the fine spectrum must be turned on or off: true or false"),
            '{"type": "fine", "on": "yes"}': ("fine", "the fine spectrum must be turned on or off: true or false"),
            '{"type": "fine", "on": true, "bins": 4}': ("fine", "unknown fine setting: bins"),
            '{"type": "bookmark", "action": "rename"}': (
                "bookmark", 'a bookmark action is "add", "change", "remove", "add_category", '
                '"rename_category" or "remove_category"'),
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
            '{"type": "scanner", "threshold_db": 60}': (
                "scanner", "the threshold must be a number of dB from 6 to 50"),
            '{"type": "scanner", "ignore": 145e6, "restore": true}': (
                "scanner", "ignore, restore and scan come alone in their message"),
            '{"type": "scanner", "scan": "panorama"}': (
                "scanner", 'a scan is "memory", "sweep" or "signals", or null to stop it'),
            '{"type": "scanner", "scan": "sweep"}': (
                "scanner", "a range wider than the current window needs a receiver that tunes, such as an RTL-SDR"),
            '{"type": "scanner", "sweep_low_hz": 440e6}': (
                "scanner", "the sweep's lowest frequency must lie below its highest"),
            '{"type": "scanner", "scan": "memory"}': (
                "scanner", "no ticked bookmark lies within the receiver's reach"),
            '{"type": "scanner", "resume_s": 9}': (
                "scanner", "the resume time must be a number of seconds from 0 to 8"),
            '{"type": "observations", "forget": 145e6}': ("observations", "unknown observations field: forget"),
            '{"type": "observations", "clear": 145e6, "clear_all": true}': (
                "observations", "clear and clear_all come alone in their message"),
            '{"type": "observations", "clear": "145 MHz"}': (
                "observations", "a channel to clear must be a number from 1 Hz to 1e+11 Hz"),
            '{"type": "observations", "clear_all": 1}': ("observations", "clear_all takes true"),
            '{"type": "update", "install": true}': ("update", "unknown update setting: install"),
            '{"type": "update", "check": 1}': ("update", "check and check_at_start must be true or false"),
            '{"type": "update", "check_at_start": "no"}': (
                "update", "check and check_at_start must be true or false"),
            '{"type": "report", "text": "mine"}': ("report", "unknown report field: text"),
            '{"type": "report", "page": "two\\nlines"}': (
                "report", "page must be a line of text of at most 300 characters"),
            '{"type": "carrier"}': ("carrier", "a carrier message takes the action measure or identify"),
            '{"type": "carrier", "action": "measure", "id": -1}': (
                "carrier", "a request id must be a whole number from 1"),
            '{"type": "calibration"}': ("calibration", "a calibration message takes the action start or stop"),
            '{"type": "calibration", "action": "start"}': (
                "calibration", "this receiver has no frequency correction to calibrate"),
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
        self.assertEqual(states["bookmarks"]["bookmarks"], [{"id": 1, **bookmark, "category": None}])
        self.assertEqual(states["preferences"]["values"], values)
        self.assertEqual((states["storage"]["saving"], states["storage"]["problem"]), (True, ""))


class StartScanTest(unittest.IsolatedAsyncioTestCase):
    """LocalServer.start_scan, for --scan: the scan goes on without any
    interface, and an interface that opens later learns why one could not
    start."""

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    async def scanner_state(self, server: LocalServer) -> dict:
        async with self.session.ws_connect(f"{server.url}stream", origin=f"http://{LOOPBACK_HOST}:{server.port}") as ws:
            return (await read_start(self, ws))["scanner"]

    async def test_starts_a_scan_that_goes_on_without_any_interface(self):
        Store(self.directory).save(BOOKMARKS_FILE, {"bookmarks": [
            {"frequency_hz": FAKE_CENTRE_HZ + 40_000, "name": "Near", "mode": "NFM", "scan": True}]})
        source = FakeSource()
        server = LocalServer(source=source, store=Store(self.directory))
        self.addCleanup(server.stop)
        server.start()
        await asyncio.to_thread(server.start_scan, "memory")
        deadline = time.monotonic() + 5
        while source.reads == 0:
            self.assertLess(time.monotonic(), deadline, "the engine did not read its source")
            await asyncio.sleep(0.01)
        state = await self.scanner_state(server)
        self.assertEqual((state["scan"], state["stopped"]), ("memory", None))

    async def test_says_why_a_scan_cannot_start(self):
        server = LocalServer(source=FakeSource())
        self.addCleanup(server.stop)
        with self.assertRaisesRegex(RuntimeError, "^the server is not running$"):
            server.start_scan("memory")
        server.start()
        with self.assertRaises(ValueError) as raised:
            await asyncio.to_thread(server.start_scan, "sweep")
        self.assertEqual(str(raised.exception), "a range wider than the current window needs a receiver that tunes, such as an RTL-SDR")
        state = await self.scanner_state(server)
        self.assertEqual((state["scan"], state["stopped"], state["stopped_scan"]), (None, "start", "sweep"))


class RecordingStreamTest(unittest.IsolatedAsyncioTestCase):
    """An interface records the audio, lists the recordings and downloads
    one: the server serves the recordings the engine lists, and nothing
    else."""

    async def asyncSetUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
        self.server = LocalServer(source=FakeSource(), recordings=self.folder)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def connect(self):
        return self.session.ws_connect(f"{self.server.url}stream", origin=f"http://{LOOPBACK_HOST}:{self.server.port}")

    @staticmethod
    async def next_message(ws, kind: str, until=lambda message: True) -> dict:
        async def wait() -> dict:
            while (message := await next_text(ws))["type"] != kind or not until(message):
                continue
            return message
        return await asyncio.wait_for(wait(), 10)

    async def test_records_lists_and_serves_a_recording(self):
        async with self.connect() as ws:
            states = await read_start(self, ws)
            self.assertEqual(states["recording"], {"type": "recording", "audio": None, "iq": None, "stopped": None,
                                                   "folder": str(self.folder), "default_folder": str(self.folder)})
            await ws.send_json({"type": "recording", "audio": True})
            state = await self.next_message(ws, "recording", lambda state: state["audio"] and state["audio"]["file"])
            name = state["audio"]["file"]
            # Not while it runs.
            self.assertEqual((await asyncio.to_thread(get, self.server, f"/recordings/{name}"))[0], 404)
            await ws.send_json({"type": "recording", "audio": False})
            await self.next_message(ws, "recording", lambda state: state["audio"] is None)
            await ws.send_json({"type": "recordings"})
            listed = await self.next_message(ws, "recordings")
            self.assertEqual([(entry["name"], entry["running"]) for entry in listed["files"]], [(name, False)])
        path = self.folder / name
        deadline = time.monotonic() + 5
        while path.stat().st_size != listed["files"][0]["bytes"] or path.stat().st_size <= 44:
            self.assertLess(time.monotonic(), deadline)     # its thread closes the file
            await asyncio.sleep(0.05)
        status, headers, body = await asyncio.to_thread(get, self.server, f"/recordings/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(body, path.read_bytes())
        self.assertEqual((headers["Content-Type"], headers["Content-Disposition"], headers["Content-Length"]),
                         ("audio/wav", f'attachment; filename="{name}"', str(len(body))))
        self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"]), ("no-store", "nosniff"))
        with wave.open(io.BytesIO(body)) as recorded:
            self.assertEqual(recorded.getframerate(), 12_000)

    async def test_serves_an_iq_recording_and_its_metadata(self):
        self.folder.mkdir()
        data, meta = self.folder / IQ_RECORDING_NAME, self.folder / meta_name(IQ_RECORDING_NAME)
        data.write_bytes(bytes(range(256)) * 4)
        meta.write_text('{"global": {"core:datatype": "cu8"}, "captures": [], "annotations": []}\n', encoding="ascii")
        for path in (data, meta):
            with self.subTest(file=path.name):
                status, headers, body = await asyncio.to_thread(get, self.server, f"/recordings/{path.name}")
                self.assertEqual((status, body), (200, path.read_bytes()))
                # Neither guessed nor JSON: browsers keep the name as it is.
                self.assertEqual((headers["Content-Type"], headers["Content-Disposition"]),
                                 ("application/octet-stream", f'attachment; filename="{path.name}"'))

    async def test_serves_nothing_else(self):
        self.folder.mkdir()
        target = self.folder.parent / "secret.txt"
        target.write_text("secret")
        os.symlink(target, self.folder / RECORDING_NAME)
        os.symlink(target, self.folder / meta_name(IQ_RECORDING_NAME))
        (self.folder / "notes.txt").write_text("not a recording")
        for path in (f"/recordings/{RECORDING_NAME}", f"/recordings/{meta_name(IQ_RECORDING_NAME)}",
                     "/recordings/notes.txt", "/recordings/../secret.txt",
                     "/recordings/%2E%2E%2Fsecret.txt", f"/recordings/{RECORDING_NAME.replace('.wav', '-2.wav')}",
                     "/recordings/", "/recordings"):
            with self.subTest(path=path):
                self.assertEqual((await asyncio.to_thread(get, self.server, path))[0], 404)
        self.assertEqual((await asyncio.to_thread(get, self.server, f"/recordings/{RECORDING_NAME}",
                                                  host="attacker.example"))[0], 403)

    async def test_a_recording_is_neither_downloaded_with_head_nor_from_another_folder(self):
        self.folder.mkdir()
        (self.folder / RECORDING_NAME).write_bytes(wav_header(12_000, 0))
        connection = http.client.HTTPConnection(LOOPBACK_HOST, self.server.port, timeout=5)
        self.addCleanup(connection.close)
        await asyncio.to_thread(connection.request, "HEAD", f"/recordings/{RECORDING_NAME}")
        self.assertEqual((await asyncio.to_thread(connection.getresponse)).status, 405)
        other = self.folder.parent / "other"
        other.mkdir()
        async with self.connect() as ws:
            await read_start(self, ws)
            await ws.send_json({"type": "recording", "folder": str(other)})
            await self.next_message(ws, "recording", lambda state: state["folder"] == str(other))
        self.assertEqual((await asyncio.to_thread(get, self.server, f"/recordings/{RECORDING_NAME}"))[0], 404)


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


class ReportStreamTest(unittest.IsolatedAsyncioTestCase):
    """This computer's interface asks for a problem report, which comes to it
    alone."""

    async def asyncSetUp(self):
        self.server = LocalServer(source=FakeSource())
        self.server.start()
        self.addCleanup(self.server.stop)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)

    def connect(self):
        return self.session.ws_connect(f"{self.server.url}stream", origin=f"http://{LOOPBACK_HOST}:{self.server.port}")

    async def test_sends_a_report_to_the_interface_that_asks(self):
        async with self.connect() as asking, self.connect() as other:
            for ws in (asking, other):
                await read_start(self, ws)
            await asking.send_json({"type": "report", "page": "a browser; WebGL2 views"})
            while (message := await next_text(asking))["type"] != "report":
                continue
            lines = message["text"].splitlines()
            self.assertTrue(lines[0].startswith(f"Radiolack {__version__}"), lines[0])
            self.assertIn("Interface: a browser; WebGL2 views", lines)
            self.assertIn(f"Receiver: test, streaming, {FakeSource.sample_rate_hz / 1e6:g} MS/s", lines)
            self.assertTrue(message["issue_url"].startswith(f"{report.ISSUES_URL}?body="), message["issue_url"])
            # The other interface learns the next change, and no report before it.
            await asking.send_json({"type": "scanner", "threshold_db": 20})
            while (message := await next_text(other))["type"] != "scanner":
                self.assertNotEqual(message["type"], "report")


class CarrierStreamTest(ReportStreamTest):
    """Any interface, a remote device's included, asks for the measure of
    the carrier at the VFO, which comes to it alone."""

    async def test_sends_the_measure_to_the_interface_that_asks(self):
        async with self.connect() as asking, self.connect() as other:
            for ws in (asking, other):
                await read_start(self, ws)
            await asking.send_json({"type": "carrier", "action": "measure", "id": 4})
            while (message := await next_text(asking))["type"] != "carrier":
                continue
            self.assertEqual(set(message), {"type", "id", "frequency_hz", "margin_db", "reason"})
            self.assertEqual(message["id"], 4)
            await asking.send_json({"type": "scanner", "threshold_db": 20})
            while (message := await next_text(other))["type"] != "scanner":
                self.assertNotEqual(message["type"], "carrier")

    async def test_answers_a_check_of_the_driver_to_the_interface_that_asks(self):
        async with self.connect() as asking, self.connect() as other:
            for ws in (asking, other):
                await read_start(self, ws)
            await asking.send_json({"type": "carrier", "action": "identify", "id": 5})
            while (message := await next_text(asking))["type"] != "driver_pll":
                self.assertNotEqual(message["type"], "error")
            # A source without an R820T or R828D has no driver to identify.
            self.assertEqual(message, {"type": "driver_pll", "id": 5, "driver_pll": None, "previous": None,
                                       "reason": "receiver"})
            await asking.send_json({"type": "scanner", "threshold_db": 20})
            while (message := await next_text(other))["type"] != "scanner":
                self.assertNotEqual(message["type"], "driver_pll")


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
                # The R820T sits on its grid, 2.5 kHz below 145 MHz at 2.4 MS/s: the frames say so.
                self.assertEqual(await next_frame_centre(ws), 144_997_500)
            await first.send_json({"type": "source", "centre_hz": 433_000_000, "gain_db": 30, "ppm": -3,
                                   "dc_removal": False})
            for ws in (first, second):
                while (state := await next_text(ws))["centre_hz"] != 433e6 or state["status"] != "streaming":
                    pass
                self.assertEqual(
                    (state["tuner"], state["gain_db"], state["ppm"], state["dc_removal"], state["driver_pll"]),
                    ("Rafael Micro R820T", 29.7, -3, False, "unknown"))
                # Radiolack corrects the whole of the error itself, on the grid's point.
                self.assertAlmostEqual(state["centre_in_use_hz"], 432_996_201.0075, places=3)
                while await next_frame_centre(ws) != state["centre_in_use_hz"]:
                    pass
            self.assertIn((0x01, 432_997_500), self.fake.commands)
            self.assertIn((0x04, 297), self.fake.commands)
            self.assertNotIn((0x05, 2**32 - 3), self.fake.commands)     # the driver corrects nothing

    async def test_an_interface_moves_the_receiver_to_another_address_for_all(self):
        other = FakeRtlTcp(chunk_bytes=8000)
        await other.__aenter__()
        self.addAsyncCleanup(other.__aexit__, None, None, None)
        address = f"{LOOPBACK_HOST}:{other.port}"
        async with self.connect() as first, self.connect() as second:
            for ws in (first, second):
                self.assertEqual(await next_frame_centre(ws), 144_997_500)
            await first.send_json({"type": "source", "address": str(other.port), "ppm": -3})
            for ws in (first, second):
                while (state := await next_text(ws))["address"] != address or state["status"] != "streaming":
                    pass
                self.assertEqual((state["centre_hz"], state["ppm"]), (145e6, -3))
                self.assertAlmostEqual(state["centre_in_use_hz"], 144_997_065.0075, places=3)
                while await next_frame_centre(ws) != state["centre_in_use_hz"]:
                    pass
            self.assertEqual(other.connections, 1)
            self.assertIn((0x01, 144_997_500), other.commands)
            self.assertNotIn((0x05, 2**32 - 3), other.commands)
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
            self.assertEqual(await next_frame_centre(ws), 144_997_500)     # the R820T's grid
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
                self.assertEqual((state["kind"], state["kinds"]), ("synthetic", ["synthetic", "rtl_sdr", "rtl_tcp", "soapy"]))
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
