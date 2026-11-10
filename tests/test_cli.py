"""Tests for the command line: options, interface choice, signals and exit codes.

No test opens a window or a browser: in-process tests replace them, and the
subprocesses run without any display. Radiolack runs with the test run's own
settings folder (tests/__init__.py), never the owner's.
"""

import contextlib
import functools
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import radiolack
from radiolack import cli
from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LOOPBACK_HOST
import tests     # tests/__init__.py gives the test run a settings folder of its own

PACKAGE_PARENT = str(Path(radiolack.__file__).resolve().parents[1])
URL_LINE = re.compile(r"Radiolack is running at (http://127\.0\.0\.1:\d+/) ")


@contextlib.contextmanager
def no_wait():
    """Stand in for cli._stop_signals: the stop event is already set."""
    stop = threading.Event()
    stop.set()
    yield stop


def run_main(*argv):
    """Run cli.main in-process without waiting for a signal."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_stop_signals", no_wait), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def own_settings():
    """A settings folder for one test, apart from the test run's: yields the
    path of its settings file."""
    with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config}):
        yield Path(config) / "radiolack" / "settings.json"


@contextlib.contextmanager
def silent_rtl_tcp():
    """A listening socket that never accepts, in place of rtl_tcp: yields it
    with its address."""
    with socket.socket() as rtl_tcp:
        rtl_tcp.bind((LOOPBACK_HOST, 0))
        rtl_tcp.listen()
        rtl_tcp.setblocking(False)
        yield rtl_tcp, f"{LOOPBACK_HOST}:{rtl_tcp.getsockname()[1]}"


class ParserTest(unittest.TestCase):
    def test_rejects_invalid_ports(self):
        for value in ("-1", "65536", "http"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(["--port", value])
                self.assertEqual(raised.exception.code, 2)

    def test_defaults(self):
        args = cli.build_parser().parse_args([])
        self.assertEqual((args.ui, args.port, args.rtl_sdr, args.rtl_tcp), ("window", 0, None, None))

    def test_rtl_sdr_devices(self):
        for argv, device in ((["--rtl-sdr"], "0"), (["--rtl-sdr", "1"], "1"),
                             (["--rtl-sdr", "00000001"], "00000001")):
            with self.subTest(argv=argv):
                self.assertEqual(cli.build_parser().parse_args(argv).rtl_sdr, device)

    def test_rejects_invalid_receivers_and_two_receivers(self):
        for argv in (["--rtl-sdr", ""], ["--rtl-sdr", " "], ["--rtl-sdr", "--rtl-tcp"],
                     ["--rtl-tcp", "--rtl-sdr"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_rtl_tcp_addresses(self):
        # Parsed only: nothing connects to these addresses.
        addresses = {
            None: (),       # no address: the source takes the saved one
            "40000": (LOOPBACK_HOST, 40000),
            "radio.example": ("radio.example", 1234),
            "192.0.2.10:40000": ("192.0.2.10", 40000),
            "[::1]": ("::1", 1234),
            "[::1]:40000": ("::1", 40000),
        }
        for value, address in addresses.items():
            with self.subTest(value=value):
                argv = ["--rtl-tcp"] if value is None else ["--rtl-tcp", value]
                self.assertEqual(cli.build_parser().parse_args(argv).rtl_tcp, address)

    def test_rejects_invalid_rtl_tcp_addresses(self):
        for value in ("", "0", "65536", "host:", ":40000", "host:port", "::1", "[::1", "[::1]40000", "[]:40000"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(["--rtl-tcp", value])
                self.assertEqual(raised.exception.code, 2)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(radiolack.__version__, out.getvalue())


class MainTest(unittest.TestCase):
    def test_busy_port(self):
        with socket.socket() as busy:
            busy.bind((LOOPBACK_HOST, 0))
            busy.listen()
            code, out, err = run_main("--ui", "none", "--port", str(busy.getsockname()[1]))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("cannot listen on 127.0.0.1:", err)

    def test_saves_in_the_settings_folder(self):
        with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
            code, out, err = run_main("--ui", "none")
        self.assertEqual((code, err), (0, ""))
        directory = server.call_args.kwargs["store"].directory
        self.assertEqual(directory, Path(os.environ["XDG_CONFIG_HOME"]) / "radiolack")
        # tests/__init__.py: the test run has a settings folder of its own.
        self.assertTrue(directory.is_relative_to(tempfile.gettempdir()))

    def test_rtl_tcp_source_connects_only_once_an_interface_listens(self):
        with silent_rtl_tcp() as (rtl_tcp, address), own_settings() as settings:
            with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
                code, out, err = run_main("--ui", "none", "--rtl-tcp", address)
            self.assertEqual((code, err), (0, ""))
            source = server.call_args.kwargs["source"]
            self.assertIsInstance(source, RtlTcpSource)
            self.assertEqual((source.address, source.given), (address, frozenset({"address"})))
            with self.assertRaises(BlockingIOError):
                rtl_tcp.accept()
            # Kept for the next start without an address.
            saved = json.loads(settings.read_text(encoding="utf-8"))["receivers"]["rtl_tcp"]["source"]
            self.assertEqual(saved["address"], address)

    def test_rtl_tcp_without_an_address_reopens_the_saved_one(self):
        with silent_rtl_tcp() as (rtl_tcp, address), own_settings() as settings:
            settings.parent.mkdir()
            saved = {"version": 1, "receivers": {"rtl_tcp": {"source": {"address": address}}}}
            settings.write_text(json.dumps(saved), encoding="utf-8")
            with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
                code, out, err = run_main("--ui", "none", "--rtl-tcp")
            self.assertEqual((code, err), (0, ""))
            source = server.call_args.kwargs["source"]
            self.assertEqual((source.address, source.given), (address, frozenset()))
            with self.assertRaises(BlockingIOError):
                rtl_tcp.accept()

    def test_rtl_sdr_source_starts_nothing_until_an_interface_listens(self):
        # A program that does not exist: nothing could open a real receiver.
        safe_source = functools.partial(RtlSdrSource, program=("/nonexistent/rtl_tcp",))
        with mock.patch.object(cli.shutil, "which", return_value="/usr/bin/rtl_tcp"), \
                mock.patch.object(cli, "RtlSdrSource", side_effect=safe_source) as created, \
                mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
            code, out, err = run_main("--ui", "none", "--rtl-sdr", "1")
        self.assertEqual((code, err), (0, ""))
        created.assert_called_once_with("1")
        source = server.call_args.kwargs["source"]
        self.assertIsInstance(source, RtlSdrSource)
        self.assertEqual((source.device, source.status), ("1", "idle"))

    def test_rtl_sdr_needs_rtl_tcp_installed(self):
        with mock.patch.object(cli.shutil, "which", return_value=None), \
                mock.patch.object(cli, "LocalServer") as server:
            code, out, err = run_main("--ui", "none", "--rtl-sdr")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("rtl_tcp is not installed", err)
        server.assert_not_called()

    def test_window_shows_served_interface(self):
        shown = []
        with mock.patch.object(cli.window, "unavailable_reason", return_value=None), \
                mock.patch.object(cli.window, "show", side_effect=shown.append):
            code, out, err = run_main()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(len(shown), 1)
        self.assertRegex(shown[0], r"^http://127\.0\.0\.1:\d+/$")

    def test_window_unavailable_falls_back_to_address(self):
        with mock.patch.object(cli.window, "unavailable_reason", return_value="no graphical display"), \
                mock.patch.object(cli.window, "show") as show:
            code, out, err = run_main("--ui", "window")
        self.assertEqual(code, 0)
        show.assert_not_called()
        self.assertIn("no native window (no graphical display)", err)
        self.assertRegex(out, URL_LINE)

    def test_browser(self):
        with mock.patch.object(cli.webbrowser, "open", return_value=True) as open_browser:
            code, out, err = run_main("--ui", "browser")
        self.assertEqual((code, err), (0, ""))
        url = URL_LINE.search(out).group(1)
        open_browser.assert_called_once_with(url)

    def test_browser_unavailable(self):
        with mock.patch.object(cli.webbrowser, "open", return_value=False):
            code, out, err = run_main("--ui", "browser")
        self.assertEqual(code, 0)
        self.assertIn("no web browser could be opened", err)
        self.assertRegex(out, URL_LINE)


@unittest.skipUnless(os.name == "posix", "POSIX signals")
class SignalTest(unittest.TestCase):
    """Run the real entry point and stop it the way a terminal or a service would."""

    def start(self, *argv):
        env = dict(os.environ)
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(name, None)
        env["GDK_BACKEND"] = "x11"  # With no DISPLAY, GTK finds no display.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (PACKAGE_PARENT, env.get("PYTHONPATH"))))
        process = subprocess.Popen(
            [sys.executable, "-m", "radiolack", *argv], env=env, text=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup, process)
        line = process.stdout.readline()
        match = URL_LINE.search(line)
        self.assertIsNotNone(match, f"unexpected output: {line!r}")
        return process, match.group(1)

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def assert_stops(self, process, signum):
        process.send_signal(signum)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        return err

    def test_serves_until_sigterm(self):
        process, url = self.start("--ui", "none")
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assert_stops(process, signal.SIGTERM)

    def test_stops_on_sigint(self):
        process, _ = self.start("--ui", "none")
        self.assert_stops(process, signal.SIGINT)

    def test_window_without_display(self):
        process, _ = self.start()
        err = self.assert_stops(process, signal.SIGTERM)
        # A missing display or toolkit, never a toolkit that failed to load.
        self.assertRegex(err, r"no native window \((no graphical display|PyGObject is not "
                              r"installed|GTK 3 is not installed|WebKitGTK is not installed)\)")


if __name__ == "__main__":
    unittest.main()
