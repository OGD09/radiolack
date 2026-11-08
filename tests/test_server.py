"""Tests for the local web server, over real sockets on ephemeral ports."""

import http.client
import socket
import unittest

from radiolack.server import LOOPBACK_HOST, LocalServer


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
