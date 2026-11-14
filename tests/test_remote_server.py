"""Tests for remote access's HTTPS listener (server.py, remote.py), over real
sockets: the listener binds 127.0.0.1, which remote access accepts as a
private address, on an ephemeral port, and resolves the computer's name to
it; where the computer's addresses change, it moves to 127.0.0.2, loopback
too on Linux. This computer's interface is a stream to the loopback server."""

import asyncio
import hashlib
import http.cookies
import json
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp.abc import AbstractResolver

from radiolack.engine import REMOTE_BINS
from radiolack.remote import REMOTE_FILE, SESSION_COOKIE
from radiolack.server import CLOSE_MOVING, CLOSE_NOT_PAIRED, CLOSE_REMOTE_OFF, LocalServer, _limit_unsent
from radiolack.spectrum import FRAME_HEADER, FRAME_KIND_SPECTRUM
from radiolack.store import Store
from tests.fakes import LOOPBACK, FakeSource, free_port
from tests.test_server import next_text, read_start

HOST = "radiolack-test"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1")
REFUSED = "only Radiolack's interface on the computer may change this"
SECOND = "127.0.0.2"    # another address of this computer, for remote access to move to
WAIT_S = 10.0           # the longest a test waits for a message that frames may hide
NO_ADDRESS_FOR_NOW = ("This computer has no address on a local network for now: remote access "
                      "listens again once it has one.")


def binds(address: str) -> bool:
    """Whether a socket can listen on ``address`` here: Linux gives all of
    127.0.0.0/8 to loopback, macOS only 127.0.0.1."""
    try:
        with socket.socket() as sock:
            sock.bind((address, 0))
    except OSError:
        return False
    return True


MOVES = unittest.skipUnless(binds(SECOND), f"{SECOND} is not an address of this computer")


class LocalNames(AbstractResolver):
    """Resolves every name to 127.0.0.1, as mDNS resolves this computer's name."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{"hostname": host, "host": LOOPBACK, "port": port, "family": socket.AF_INET, "proto": 0,
                 "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        pass


class RemoteAccessTest(unittest.IsolatedAsyncioTestCase):
    """A server whose remote access listens on 127.0.0.1 once turned on."""

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        self.addresses = [LOOPBACK]     # this computer's addresses, which a test may change
        self.server = self.serve()
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.local = await self.connect_local(self.server)
        self.port = free_port()
        await self.local.send_json({"type": "remote", "port": self.port})
        await self.next_remote(lambda state: state["port"] == self.port)
        self.base = f"https://{LOOPBACK}:{self.port}/"
        self.origin = f"https://{LOOPBACK}:{self.port}"
        self.trust: ssl.SSLContext | None = None

    def serve(self) -> LocalServer:
        server = LocalServer(source=FakeSource(), store=Store(self.directory),
                             remote_addresses=lambda: list(self.addresses), host=HOST)
        server.start()
        self.addCleanup(server.stop)
        return server

    async def connect_local(self, server: LocalServer):
        """This computer's interface: a stream to the loopback server, past its start."""
        ws = await self.session.ws_connect(f"{server.url}stream", origin=f"http://{LOOPBACK}:{server.port}")
        self.addAsyncCleanup(ws.close)
        self.remote_state = (await read_start(self, ws))["remote"]
        return ws

    async def next_remote(self, until=lambda state: True, ws=None) -> dict:
        """The next remote access state that this computer's interface receives
        and that satisfies ``until``, within WAIT_S, as frames keep coming."""
        async def wait() -> dict:
            while (message := await next_text(ws or self.local))["type"] != "remote" or not until(message):
                continue
            return message

        self.remote_state = await asyncio.wait_for(wait(), WAIT_S)
        return self.remote_state

    async def next_error(self, ws=None) -> dict:
        """The next refusal that this computer's interface receives, within WAIT_S."""
        async def wait() -> dict:
            while (message := await next_text(ws or self.local))["type"] != "error":
                continue
            return message

        return await asyncio.wait_for(wait(), WAIT_S)

    async def turn_on(self) -> dict:
        await self.local.send_json({"type": "remote", "on": True})
        state = await self.next_remote(lambda state: state["on"] or state["problem"])
        self.assertTrue(state["on"], state["problem"])
        if self.trust is None:
            # As a device does: download the certificate past the browser's warning, then trust it.
            async with self.session.get(f"{self.base}certificate", ssl=False) as response:
                self.assertEqual(response.status, 200)
                self.trust = ssl.create_default_context(cadata=await response.text())
        return state

    async def turn_off(self) -> dict:
        await self.local.send_json({"type": "remote", "on": False})
        return await self.next_remote(lambda state: not state["on"])

    def device(self, user_agent: str = IPHONE, resolver: AbstractResolver | None = None,
               cookie_jar: aiohttp.CookieJar | None = None) -> aiohttp.ClientSession:
        """A phone's browser, which trusts the certificate: it keeps cookies,
        those of addresses too, in ``cookie_jar`` if given."""
        connector = aiohttp.TCPConnector(ssl=self.trust, resolver=resolver or aiohttp.DefaultResolver())
        if cookie_jar is None:
            cookie_jar = aiohttp.CookieJar(unsafe=True)
        session = aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar,
                                        headers={"User-Agent": user_agent})
        self.addAsyncCleanup(session.close)
        return session

    async def pair(self, device: aiohttp.ClientSession, code: str | None = None, **kwargs):
        """Send the access code, by default the right one: returns the status and the answer."""
        code = self.remote_state["code"] if code is None else code
        kwargs.setdefault("headers", {"Origin": self.origin})
        async with device.post(f"{self.base}pair", json={"code": code}, **kwargs) as response:
            return response.status, await response.json(content_type=None)

    async def remote_stream(self, device: aiohttp.ClientSession, **kwargs):
        ws = await device.ws_connect(f"wss://{LOOPBACK}:{self.port}/stream", origin=self.origin, **kwargs)
        self.addAsyncCleanup(ws.close)
        return ws

    async def get(self, device: aiohttp.ClientSession, path: str = "", **kwargs):
        async with device.get(f"{self.base}{path}", **kwargs) as response:
            return response.status, response.headers, await response.read()

    async def closed(self, ws) -> int:
        """Read until the server closes the stream, within WAIT_S: returns
        the code of its close frame, which browsers report."""
        async def wait() -> aiohttp.WSMessage:
            while (message := await ws.receive()).type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                continue
            return message

        message = await asyncio.wait_for(wait(), WAIT_S)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        return message.data

    def assert_refused(self, port: int, address: str = LOOPBACK) -> None:
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((address, port), timeout=5).close()

    def follow_quickly(self) -> None:
        """Remote access looks at this computer's addresses every 50 ms, not every 10 s."""
        self.enterContext(mock.patch("radiolack.server.ADDRESS_CHECK_S", 0.05))

    async def stays_open(self, ws, seconds: float) -> None:
        """Read the stream for ``seconds``, during which the server must not close it."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while (left := end - loop.time()) > 0:
            try:
                message = await ws.receive(timeout=left)
            except asyncio.TimeoutError:
                return
            self.assertIn(message.type, (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY))

    async def test_off_until_turned_on_and_off_again_once_turned_off(self):
        self.assertEqual(self.remote_state, {
            "type": "remote", "on": False, "port": self.port, "urls": [], "code": None, "blocked": False,
            "devices": [], "fingerprint": None, "problem": ""})
        self.assert_refused(self.port)
        state = await self.turn_on()
        self.assertEqual(state["urls"], [f"https://{LOOPBACK}:{self.port}/", f"https://{HOST}.local:{self.port}/"])
        self.assertRegex(state["code"], r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
        self.assertRegex(state["fingerprint"], r"^[0-9A-F]{2}( [0-9A-F]{2}){31}$")
        state = await self.turn_off()
        self.assertEqual((state["urls"], state["code"], state["problem"]), ([], None, ""))
        self.assert_refused(self.port)

    async def test_the_certificate_verifies_for_the_name_and_the_address(self):
        state = await self.turn_on()
        async with self.session.get(f"{self.base}certificate", ssl=False) as response:
            self.assertEqual(response.headers["Content-Type"], "application/x-x509-ca-cert")
            self.assertEqual(response.headers["Content-Disposition"], f'attachment; filename="radiolack-{HOST}.crt"')
            root = ssl.PEM_cert_to_DER_cert(await response.text())
        self.assertEqual(state["fingerprint"],
                         " ".join(f"{byte:02X}" for byte in hashlib.sha256(root).digest()))
        for url in state["urls"]:
            with self.subTest(url=url):
                async with self.device(resolver=LocalNames()).get(url) as response:
                    self.assertEqual(response.status, 200)
        # A name the certificate does not hold, as on a rebinding attack.
        with self.assertRaises(aiohttp.ClientConnectorCertificateError):
            await self.device(resolver=LocalNames()).get(f"https://radiolack.example:{self.port}/")

    async def test_refuses_plain_http(self):
        await self.turn_on()
        with self.assertRaises(aiohttp.ClientError):
            async with self.session.get(f"http://{LOOPBACK}:{self.port}/") as response:
                await response.read()

    async def test_refuses_other_host_names(self):
        await self.turn_on()
        device = self.device()
        for host in (f"radiolack.example:{self.port}", f"{LOOPBACK}:{self.port + 1}", "localhost"):
            with self.subTest(host=host):
                status, _, body = await self.get(device, headers={"Host": host})
                self.assertEqual((status, body), (403, b"Unexpected Host header.\n"))

    async def test_an_unpaired_device_gets_the_pairing_page_and_nothing_else(self):
        await self.turn_on()
        device = self.device()
        status, headers, body = await self.get(device)
        self.assertEqual(status, 200)
        self.assertIn(b'<form id="pair-form"', body)
        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
        self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"], headers["Referrer-Policy"]),
                         ("no-store", "nosniff", "no-referrer"))
        for name in ("pair.js", "pair.css", "style.css"):
            with self.subTest(name=name):
                self.assertEqual((await self.get(device, name))[0], 200)
        for name in ("main.js", "stream.js", "audio-worklet.js", "bandplan-europe.json"):
            with self.subTest(name=name):
                self.assertEqual((await self.get(device, name))[0], 403)
        ws = await self.remote_stream(device)
        self.assertEqual(await self.closed(ws), CLOSE_NOT_PAIRED)
        self.assertEqual(self.remote_state["devices"], [])

    async def test_the_right_code_pairs_the_device(self):
        await self.turn_on()
        device = self.device()
        code = self.remote_state["code"]
        async with device.post(f"{self.base}pair", json={"code": f" {code.lower().replace('-', ' ')} "},
                               headers={"Origin": self.origin}) as response:
            self.assertEqual((response.status, await response.json()), (200, {"paired": True}))
            [header] = response.headers.getall("Set-Cookie")
        cookie = http.cookies.SimpleCookie(header)[SESSION_COOKIE]
        self.assertEqual((cookie["path"], cookie["samesite"], cookie["max-age"], cookie["domain"]),
                         ("/", "Strict", str(400 * 24 * 3600), ""))
        self.assertTrue(cookie["secure"] and cookie["httponly"])
        self.assertGreaterEqual(len(cookie.value), 43)
        [paired] = (await self.next_remote(lambda state: state["devices"]))["devices"]
        self.assertEqual((paired["label"], paired["connected"]), ("iPhone · Safari", False))
        status, _, body = await self.get(device)
        self.assertEqual(status, 200)
        self.assertIn(b'<script type="module" src="main.js">', body)
        self.assertEqual((await self.get(device, "main.js"))[0], 200)
        # Pairing again changes nothing.
        self.assertEqual(await self.pair(device, "WRONG"), (200, {"paired": True}))
        ws = await self.remote_stream(device)
        self.assertEqual(await ws.receive_json(timeout=5),
                         {"type": "hello", "protocol": 6, "remote": True,
                          "scanners": ["peaks", "memory", "history", "sweep"]})
        kinds = [(await ws.receive_json(timeout=5))["type"] for _ in range(6)]
        self.assertEqual(kinds, ["source", "demod", "bookmarks", "scanner", "peaks", "preferences"])
        [paired] = (await self.next_remote(lambda state: state["devices"][0]["connected"]))["devices"]
        await ws.close()
        await self.next_remote(lambda state: not state["devices"][0]["connected"])

    async def test_wrong_codes_and_requests_from_elsewhere_are_refused(self):
        self.enterContext(mock.patch("radiolack.server._WRONG_CODE_DELAY_S", 0.0))
        await self.turn_on()
        device = self.device()
        self.assertEqual(await self.pair(device, "ABCD-EFGH"), (403, {"error": "Wrong code."}))
        for headers in ({}, {"Origin": "https://radiolack.example"}, {"Origin": f"http://{LOOPBACK}:{self.port}"},
                        {"Origin": "null"}):
            with self.subTest(headers=headers):
                async with device.post(f"{self.base}pair", json={"code": self.remote_state["code"]},
                                       headers=headers) as response:
                    self.assertEqual((response.status, await response.text()), (403, "Unexpected Origin header.\n"))
        async with device.post(f"{self.base}pair", data="code=X", headers={"Origin": self.origin}) as response:
            self.assertEqual(response.status, 415)
        async with device.post(f"{self.base}pair", json={"code": "X" * 5000}, headers={"Origin": self.origin}) as response:
            self.assertEqual(response.status, 413)
        for body in ({}, {"code": 12345678}, ["code"]):
            with self.subTest(body=body):
                async with device.post(f"{self.base}pair", json=body, headers={"Origin": self.origin}) as response:
                    self.assertEqual((response.status, await response.json()), (400, {"error": "Send the access code."}))
        for origin in ("https://radiolack.example", None):
            with self.subTest(origin=origin), self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await device.ws_connect(f"wss://{LOOPBACK}:{self.port}/stream", origin=origin)
            self.assertEqual(caught.exception.status, 403)
        self.assertEqual([c.key for c in device.cookie_jar], [])
        self.assertEqual(self.remote_state["devices"], [])

    async def test_a_wrong_code_is_answered_late(self):
        await self.turn_on()
        started = time.monotonic()
        self.assertEqual(await self.pair(self.device(), "ABCD-EFGH"), (403, {"error": "Wrong code."}))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    async def test_ten_wrong_codes_block_the_code_until_a_new_one(self):
        self.enterContext(mock.patch("radiolack.server._WRONG_CODE_DELAY_S", 0.0))
        await self.turn_on()
        device = self.device()
        blocked = (429, {"error": "Too many wrong codes: make a new code in Radiolack's settings on the computer."})
        answers = [await self.pair(device, "ABCD-EFGH") for _ in range(10)]
        self.assertEqual(answers, [(403, {"error": "Wrong code."})] * 9 + [blocked])
        self.assertEqual(await self.pair(device), blocked)
        self.assertTrue((await self.next_remote(lambda state: state["blocked"]))["blocked"])
        code = self.remote_state["code"]
        await self.local.send_json({"type": "remote", "new_code": True})
        state = await self.next_remote(lambda state: not state["blocked"])
        self.assertNotEqual(state["code"], code)
        self.assertEqual(await self.pair(device), (200, {"paired": True}))

    async def test_a_remote_device_controls_the_radio_but_not_the_computer(self):
        await self.turn_on()
        device = self.device()
        await self.pair(device)
        ws = await self.remote_stream(device)
        self.assertEqual((await ws.receive_json(timeout=5))["remote"], True)
        source = await ws.receive_json(timeout=5)
        self.assertNotIn("address", source["settable"])
        await ws.send_json({"type": "demod", "id": 1, "frequency_hz": 145_010_000})
        while (state := await next_text(self.local))["type"] != "demod":
            continue
        self.assertEqual(state["frequency_hz"], 145_010_000)
        await ws.send_json({"type": "scanner", "threshold_db": 20})
        while (state := await next_text(self.local))["type"] != "scanner":
            continue
        self.assertEqual(state["threshold_db"], 20.0)
        await ws.send_json({"type": "observations", "clear_all": True})    # as the owner chose
        while (message := await next_text(ws))["type"] != "observations":
            self.assertNotEqual(message["type"], "error")
        self.assertEqual(message["channels"], [])
        for command in ({"type": "remote", "on": False}, {"type": "remote", "new_code": True},
                        {"type": "update", "check": True}, {"type": "source", "address": "192.168.1.2:1234"}):
            with self.subTest(command=command):
                await ws.send_json(command)
                self.assertEqual(await self.next_error(ws),
                                 {"type": "error", "command": command["type"], "message": REFUSED})
        # Remote access's state changes, as another device pairs: only this computer's interface learns it.
        await self.pair(self.device())
        await self.next_remote(lambda state: len(state["devices"]) == 2)
        await ws.send_json({"type": "demod", "id": 2, "frequency_hz": 145_020_000})
        while (message := await next_text(ws))["type"] != "demod":
            self.assertNotIn(message["type"], ("storage", "update", "remote"))
        self.assertEqual(message["frequency_hz"], 145_020_000)

    async def test_a_device_keeps_its_own_preferences_until_forgotten(self):
        await self.local.send_json({"type": "preferences", "values": {"palette": "ice"}})
        await self.turn_on()
        first, second = self.device(), self.device()
        await self.pair(first)
        [first_id] = [device["id"] for device in (await self.next_remote(lambda state: state["devices"]))["devices"]]
        await self.pair(second)
        ws = await self.remote_stream(first)
        self.assertEqual(await self.start_preferences(ws), {"palette": "ice"})     # this computer's, at first
        await ws.send_json({"type": "preferences", "values": {"palette": "gray", "volume": 10}})
        await ws.send_json({"type": "demod", "id": 1})     # acknowledged once the preferences are kept
        while (message := await next_text(ws))["type"] != "demod" or message["acknowledged"] != 1:
            self.assertNotEqual(message["type"], "error", message)
        await ws.close()
        self.assertEqual(await self.start_preferences(await self.remote_stream(first)),
                         {"palette": "gray", "volume": 10})
        self.assertEqual(await self.start_preferences(await self.remote_stream(second)), {"palette": "ice"})
        local = await self.session.ws_connect(f"{self.server.url}stream", origin=f"http://{LOOPBACK}:{self.server.port}")
        self.addAsyncCleanup(local.close)
        self.assertEqual((await read_start(self, local))["preferences"]["values"], {"palette": "ice"})
        # Forgotten, the device starts again from this computer's preferences.
        await self.local.send_json({"type": "remote", "forget": first_id})
        await self.next_remote(lambda state: len(state["devices"]) == 1)
        self.assertEqual(await self.pair(first), (200, {"paired": True}))
        self.assertEqual(await self.start_preferences(await self.remote_stream(first)), {"palette": "ice"})

    async def start_preferences(self, ws) -> dict:
        """The preferences that a stream starts with."""
        while (message := await next_text(ws))["type"] != "preferences":
            continue
        return message["values"]

    async def test_a_remote_device_gets_lighter_frames_deflated(self):
        await self.turn_on()
        device = self.device()
        await self.pair(device)
        ws = await self.remote_stream(device, compress=15)     # offers deflate, as browsers do
        self.assertEqual(ws.compress, 15)
        while (message := await ws.receive(timeout=5)).type is aiohttp.WSMsgType.TEXT:
            continue
        self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
        self.assertEqual(FRAME_HEADER.unpack_from(message.data)[0], FRAME_KIND_SPECTRUM)
        self.assertEqual(len(message.data), FRAME_HEADER.size + REMOTE_BINS)
        # A device's message must fit the limit once inflated: a megabyte of
        # spaces, which deflates to a few kilobytes, is far over it.
        await ws.send_str(json.dumps({"type": "demod", "padding": " " * (1 << 20)}))
        self.assertEqual(await self.closed(ws), aiohttp.WSCloseCode.MESSAGE_TOO_BIG)

    async def test_forgetting_a_device_or_making_a_new_code_closes_its_streams(self):
        await self.turn_on()
        first, second = self.device(), self.device()
        ids: list[str] = []
        for device in (first, second):
            self.assertEqual((await self.pair(device))[0], 200)
            state = await self.next_remote(lambda state: len(state["devices"]) == len(ids) + 1)
            ids.extend(d["id"] for d in state["devices"] if d["id"] not in ids)
        streams = [await self.remote_stream(device) for device in (first, second)]
        await self.next_remote(lambda state: all(d["connected"] for d in state["devices"]))
        await self.local.send_json({"type": "remote", "forget": ids[0]})
        self.assertEqual(await self.closed(streams[0]), CLOSE_NOT_PAIRED)
        self.assertIn(b'<form id="pair-form"', (await self.get(first))[2])
        self.assertIn(b"main.js", (await self.get(second))[2])
        await self.local.send_json({"type": "remote", "new_code": True})
        self.assertEqual(await self.closed(streams[1]), CLOSE_NOT_PAIRED)
        self.assertEqual((await self.next_remote(lambda state: not state["devices"]))["devices"], [])
        await self.local.send_json({"type": "remote", "forget": ids[1]})
        self.assertEqual((await self.next_error())["message"], "no such device: it may have been forgotten already")

    async def test_turning_off_closes_the_remote_streams_and_releases_the_port(self):
        await self.turn_on()
        device = self.device()
        await self.pair(device)
        ws = await self.remote_stream(device)
        await self.next_remote(lambda state: state["devices"] and state["devices"][0]["connected"])
        started = time.monotonic()
        await self.local.send_json({"type": "remote", "on": False})
        self.assertEqual(await self.closed(ws), CLOSE_REMOTE_OFF)
        await self.next_remote(lambda state: not state["on"])
        self.assertLess(time.monotonic() - started, 3.0)
        self.assert_refused(self.port)
        # This computer's interface carries on, and the device stays paired for the next time.
        while (await self.local.receive(timeout=5)).type != aiohttp.WSMsgType.BINARY:
            continue
        self.assertEqual(len(self.remote_state["devices"]), 1)
        await self.turn_on()
        status, _, body = await self.get(device)
        self.assertIn(b"main.js", body)

    async def test_the_port_changes_only_while_off(self):
        await self.turn_on()
        await self.local.send_json({"type": "remote", "port": self.port + 1})
        self.assertEqual(await self.next_error(), {
            "type": "error", "command": "remote", "message": "the port changes only while remote access is off"})
        for command in ({"port": 80}, {"on": "yes"}, {"new_code": False}, {"on": True, "port": 9000},
                        {"listen": "0.0.0.0"}, {}):
            with self.subTest(command=command):
                await self.local.send_json({"type": "remote", **command})
                self.assertEqual((await self.next_error())["command"], "remote")

    async def test_a_busy_port_keeps_remote_access_off(self):
        with socket.socket() as busy:
            busy.bind((LOOPBACK, self.port))
            busy.listen()
            await self.local.send_json({"type": "remote", "on": True})
            state = await self.next_remote(lambda state: state["problem"])
        self.assertEqual((state["on"], state["problem"]),
                         (False, f"Port {self.port} is in use by another program: choose another port."))

    @MOVES
    async def test_follows_this_computers_addresses_while_on(self):
        self.follow_quickly()
        state = await self.turn_on()
        device = self.device()
        await self.pair(device)
        ws = await self.remote_stream(device)
        await self.next_remote(lambda state: state["devices"] and state["devices"][0]["connected"])
        await self.stays_open(ws, 0.3)      # the same addresses at each check: nothing moves
        # An address comes, as a VPN's that comes up: remote access listens
        # there too, with a new certificate that names it, and with the same
        # code and authority.
        self.addresses.append(SECOND)
        moved = await self.next_remote(lambda state: len(state["urls"]) == 3)
        self.assertEqual(moved["urls"], [f"https://{LOOPBACK}:{self.port}/", f"https://{SECOND}:{self.port}/",
                                         f"https://{HOST}.local:{self.port}/"])
        self.assertEqual((moved["on"], moved["code"], moved["fingerprint"], moved["problem"]),
                         (True, state["code"], state["fingerprint"], ""))
        self.assertEqual(await self.closed(ws), CLOSE_MOVING)
        async with self.device().get(f"https://{SECOND}:{self.port}/") as response:
            self.assertEqual(response.status, 200)
        # The device on the address that remains reconnects, still paired.
        ws = await self.remote_stream(device)
        hello = await ws.receive_json(timeout=5)
        self.assertEqual((hello["type"], hello["remote"]), ("hello", True))
        # An address goes: remote access no longer listens there.
        self.addresses.remove(LOOPBACK)
        moved = await self.next_remote(lambda state: len(state["urls"]) == 2)
        self.assertEqual(moved["urls"], [f"https://{SECOND}:{self.port}/", f"https://{HOST}.local:{self.port}/"])
        self.assertEqual(await self.closed(ws), CLOSE_MOVING)
        self.assert_refused(self.port)

    @MOVES
    async def test_stays_on_without_an_address_until_it_has_one(self):
        self.follow_quickly()
        state = await self.turn_on()
        with self.assertLogs("radiolack.server", "WARNING") as logs:
            self.addresses.clear()
            waiting = await self.next_remote(lambda state: not state["urls"])
            await asyncio.sleep(0.3)        # checks that find no address still
        self.assertEqual((waiting["on"], waiting["code"], waiting["problem"]),
                         (True, state["code"], NO_ADDRESS_FOR_NOW))
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["remote access listens nowhere until this computer has an address on a local network"])
        self.assert_refused(self.port)
        await self.local.send_json({"type": "remote", "port": self.port + 1})
        self.assertEqual((await self.next_error())["message"], "the port changes only while remote access is off")
        # An address again, another one here: remote access listens there.
        self.addresses.append(SECOND)
        state = await self.next_remote(lambda state: state["urls"])
        self.assertEqual((state["urls"][0], state["problem"]), (f"https://{SECOND}:{self.port}/", ""))
        async with self.device().get(f"https://{SECOND}:{self.port}/") as response:
            self.assertEqual(response.status, 200)

    @MOVES
    async def test_a_busy_port_on_a_new_address_is_tried_again_at_each_check(self):
        self.follow_quickly()
        await self.turn_on()
        busy_port = f"Port {self.port} is in use by another program: choose another port."
        with socket.socket() as busy:
            busy.bind((SECOND, self.port))
            busy.listen()
            with self.assertLogs("radiolack.server", "WARNING") as logs:
                self.addresses.append(SECOND)
                state = await self.next_remote(lambda state: state["problem"])
                await asyncio.sleep(0.3)    # refused at each check, and said once
            self.assertEqual((state["on"], state["urls"], state["problem"]), (True, [], busy_port))
            self.assertEqual([record.getMessage() for record in logs.records],
                             [f"remote access cannot listen on this computer's new addresses: {busy_port}"])
        # The other program has stopped: remote access listens on both addresses.
        state = await self.next_remote(lambda state: state["urls"])
        self.assertEqual((len(state["urls"]), state["problem"]), (3, ""))
        async with self.device().get(f"https://{SECOND}:{self.port}/") as response:
            self.assertEqual(response.status, 200)

    @MOVES
    async def test_turned_off_it_follows_nothing(self):
        self.follow_quickly()
        await self.turn_on()
        await self.turn_off()
        self.addresses[:] = [SECOND]
        await asyncio.sleep(0.3)
        self.assert_refused(self.port, SECOND)
        self.assertEqual((await self.turn_on())["urls"][0], f"https://{SECOND}:{self.port}/")

    async def test_what_remote_access_keeps_outlives_the_server(self):
        state = await self.turn_on()
        device = self.device()
        await self.pair(device)
        await self.next_remote(lambda state: state["devices"])
        # The device hangs up first: the server's loop ends before a TLS
        # connection still open has finished closing, and leaves its socket
        # to the garbage collector, which warns of it in a later test.
        await device.close()
        await asyncio.to_thread(self.server.stop)
        saved = json.loads((self.directory / REMOTE_FILE).read_text("utf-8"))
        self.assertEqual(set(saved), {"version", "port", "code", "wrong_codes", "devices", "authority"})
        self.assertEqual((saved["port"], saved["code"]), (self.port, state["code"].replace("-", "")))
        self.assertEqual((self.directory / REMOTE_FILE).stat().st_mode & 0o777, 0o600)
        cookie = next(c.value for c in device.cookie_jar if c.key == SESSION_COOKIE)
        self.assertNotIn(cookie, json.dumps(saved))
        # Off at the next start, with the same port, code, certificate and devices.
        server = self.serve()
        self.local = await self.connect_local(server)
        restarted = self.remote_state
        self.assertEqual((restarted["on"], restarted["port"], restarted["fingerprint"]),
                         (False, self.port, state["fingerprint"]))
        self.assertEqual([d["label"] for d in restarted["devices"]], ["iPhone · Safari"])
        self.assertEqual((await self.turn_on())["code"], state["code"])
        # The same device comes back with its cookies: it is still paired.
        self.assertIn(b"main.js", (await self.get(self.device(cookie_jar=device.cookie_jar)))[2])


class WithoutAddressTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_access_needs_an_address_on_a_local_network(self):
        server = LocalServer(source=FakeSource(), remote_addresses=lambda: ["8.8.8.8"])
        server.start()
        self.addCleanup(server.stop)
        async with aiohttp.ClientSession() as session, session.ws_connect(
                f"{server.url}stream", origin=f"http://{LOOPBACK}:{server.port}") as ws:
            await read_start(self, ws)
            await ws.send_json({"type": "remote", "on": True})
            while (state := await next_text(ws))["type"] != "remote":
                continue
        self.assertEqual((state["on"], state["problem"]), (False, (
            "This computer has no address on a local network: connect it to one, then turn remote access on.")))
        self.assertIsNone(state["fingerprint"])


class UnsentLimitTest(unittest.IsolatedAsyncioTestCase):
    """A device's stream keeps few bytes unsent, so that a slow link does not
    make it hear the radio later and later."""

    async def test_the_socket_and_the_transport_hold_little(self):
        accepted: asyncio.Future = asyncio.get_running_loop().create_future()
        listener = await asyncio.start_server(lambda _reader, writer: accepted.set_result(writer), LOOPBACK, 0)
        self.addAsyncCleanup(listener.wait_closed)
        self.addCleanup(listener.close)
        _, client = await asyncio.open_connection(LOOPBACK, listener.sockets[0].getsockname()[1])
        self.addCleanup(client.close)
        server_side = await asyncio.wait_for(accepted, timeout=5)
        self.addCleanup(server_side.close)
        _limit_unsent(server_side.transport)
        self.assertEqual(server_side.transport.get_write_buffer_limits()[1], 16 * 1024)
        if hasattr(socket, "TCP_NOTSENT_LOWAT"):
            sock = server_side.transport.get_extra_info("socket")
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NOTSENT_LOWAT), 16 * 1024)
        _limit_unsent(None)     # a request whose connection already closed


if __name__ == "__main__":
    unittest.main()
