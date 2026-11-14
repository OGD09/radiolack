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
import select
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
from radiolack.sources import Sources
from radiolack.updates import UpdateCheck
import tests     # tests/__init__.py gives the test run a settings folder of its own

PACKAGE_PARENT = str(Path(radiolack.__file__).resolve().parents[1])
URL_LINE = re.compile(r"Radiolack is running at (http://127\.0\.0\.1:\d+/) ")


@contextlib.contextmanager
def no_wait():
    """Stand in for cli._stop_signals: the stop event is already set."""
    stop = threading.Event()
    stop.set()
    yield stop


def run_main(*argv, stop_signals=no_wait):
    """Run cli.main in-process, without waiting for a signal unless
    ``stop_signals`` stands in for cli._stop_signals."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_stop_signals", stop_signals), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def stop_once_connected(rtl_tcp: socket.socket):
    """Stand in for cli._stop_signals: the stop event is set once a
    connection waits on ``rtl_tcp``, a listening socket, or after 10 s."""
    select.select([rtl_tcp], [], [], 10)
    stop = threading.Event()
    stop.set()
    yield stop


@contextlib.contextmanager
def own_settings():
    """A settings folder for one test, apart from the test run's: yields the
    path of its settings file."""
    with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config}):
        yield Path(config) / "radiolack" / "settings.json"


def ephemeral_port() -> int:
    """A free port of 127.0.0.1, never a fixed one such as remote access's default."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


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
        self.assertEqual((args.ui, args.port, args.remote, args.scan, args.rtl_sdr, args.rtl_tcp),
                         ("window", 0, None, None, None, None))

    def test_remote_ports(self):
        for argv, port in ((["--remote"], 0), (["--remote", "--ui", "none"], 0),     # 0: the port used last
                           (["--remote", "1024"], 1024), (["--remote", "9443"], 9443),
                           (["--remote", "65535"], 65535)):
            with self.subTest(argv=argv):
                self.assertEqual(cli.build_parser().parse_args(argv).remote, port)

    def test_rejects_invalid_remote_ports(self):
        # Lower ports need administrator rights.
        for value in ("0", "80", "1023", "65536", "https"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(["--remote", value])
                self.assertEqual(raised.exception.code, 2)

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


class RemoteOptionTest(unittest.TestCase):
    """--remote, with 127.0.0.1 in place of this computer's local network
    addresses: no test listens on the owner's network."""

    def setUp(self):
        addresses = mock.patch.object(cli, "private_addresses", lambda: [LOOPBACK_HOST])
        addresses.start()
        self.addCleanup(addresses.stop)

    def test_turns_remote_access_on_and_prints_what_a_device_needs(self):
        port = ephemeral_port()
        with own_settings():
            runs = [run_main("--ui", "none", "--remote", str(port)),
                    run_main("--ui", "none", "--remote")]      # the port used last
        kept = []
        for code, out, err in runs:
            self.assertEqual((code, err), (0, ""))
            lines = out.splitlines()
            self.assertTrue(lines[0].startswith("Remote access is on: open one of these addresses"), lines[0])
            self.assertIn(f"  https://{LOOPBACK_HOST}:{port}/", lines)
            self.assertRegex(out, r"(?m)^Certificate fingerprint \(SHA-256\): [0-9A-F]{2}( [0-9A-F]{2}){31}$")
            self.assertRegex(out, r"(?m)^Access code: [2-9A-Z]{4}-[2-9A-Z]{4}$")
            self.assertRegex(lines[-1], URL_LINE)
            kept.append([line for line in lines if line.startswith(("Certificate fingerprint", "Access code"))])
        # The same authority and code at the next start: paired devices stay paired.
        self.assertEqual(kept[0], kept[1])

    def test_exits_with_an_error_without_a_local_network_address(self):
        with own_settings(), mock.patch.object(cli, "private_addresses", list):
            code, out, err = run_main("--ui", "none", "--remote", str(ephemeral_port()))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("radiolack: remote access cannot turn on: This computer has no address on a local network",
                      err)

    def test_exits_with_an_error_on_a_busy_port(self):
        with socket.socket() as busy, own_settings():
            busy.bind((LOOPBACK_HOST, 0))
            busy.listen()
            port = busy.getsockname()[1]
            code, out, err = run_main("--ui", "none", "--remote", str(port))
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"radiolack: remote access cannot turn on: Port {port} is in use by another program", err)

    def test_exits_with_an_error_when_turning_on_fails(self):
        # What LocalServer.turn_on_remote raises: a refused port, or no answer in time.
        for failure, message in ((ValueError("the port changes only while remote access is off"),
                                  "radiolack: remote access cannot turn on: the port changes only while remote "
                                  "access is off\n"),
                                 (TimeoutError(), "radiolack: remote access did not turn on in time\n")):
            with self.subTest(failure=failure), own_settings(), \
                    mock.patch.object(cli.LocalServer, "turn_on_remote", side_effect=failure):
                code, out, err = run_main("--ui", "none", "--remote")
            self.assertEqual((code, out, err), (1, "", message))


class ScanOptionTest(unittest.TestCase):
    """--scan, on the simulated band and against a stand-in for rtl_tcp: the
    scan starts before any interface opens, and one that cannot start is
    reported while Radiolack runs on."""

    def test_scans(self):
        for scan in ("memory", "sweep"):
            with self.subTest(scan=scan):
                self.assertEqual(cli.build_parser().parse_args(["--scan", scan]).scan, scan)
        for argv in (["--scan"], ["--scan", "history"], ["--scan", "Memory"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_says_why_a_scan_cannot_start_and_runs_on(self):
        # The simulated band, the first time: it does not tune, and no bookmark is ticked.
        for scan, message in (
                ("sweep", "the sweep cannot start: the sweep needs a receiver that tunes, such as an RTL-SDR"),
                ("memory", "the memory scan cannot start: no ticked bookmark lies within the receiver's reach")):
            with self.subTest(scan=scan), own_settings():
                code, out, err = run_main("--ui", "none", "--scan", scan)
            self.assertEqual((code, err), (0, f"radiolack: {message}\n"))
            self.assertRegex(out, URL_LINE)

    def test_starts_the_memory_scan_on_the_simulated_band(self):
        with own_settings() as settings:
            settings.parent.mkdir()
            bookmarks = {"version": 1, "bookmarks": [{"frequency_hz": 145_500_000.0, "name": "Calling",
                                                      "mode": "NFM", "scan": True}]}
            (settings.parent / "bookmarks.json").write_text(json.dumps(bookmarks), encoding="utf-8")
            with mock.patch.object(cli.LocalServer, "start_scan", autospec=True,
                                   side_effect=cli.LocalServer.start_scan) as start_scan:
                code, out, err = run_main("--ui", "none", "--scan", "memory")
        self.assertEqual((code, err), (0, ""))
        start_scan.assert_called_once_with(mock.ANY, "memory")
        self.assertRegex(out, URL_LINE)

    def test_reads_the_receiver_at_start_without_any_interface(self):
        # Without --scan, nothing connects until an interface opens (MainTest).
        with silent_rtl_tcp() as (rtl_tcp, address), own_settings():
            code, out, err = run_main("--ui", "none", "--rtl-tcp", address, "--scan", "sweep",
                                      stop_signals=functools.partial(stop_once_connected, rtl_tcp))
            self.assertEqual((code, err), (0, ""))
            connection, _ = rtl_tcp.accept()
            connection.close()

    def test_starts_the_scan_before_the_window_opens(self):
        events = []
        with mock.patch.object(cli.LocalServer, "start_scan", autospec=True,
                               side_effect=lambda server, scan: events.append(f"scan {scan}")), \
                mock.patch.object(cli.window, "unavailable_reason", return_value=None), \
                mock.patch.object(cli.window, "show", side_effect=lambda url: events.append("window")):
            code, out, err = run_main("--scan", "sweep")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(events, ["scan sweep", "window"])

    def test_says_when_the_scan_did_not_start_in_time(self):
        with mock.patch.object(cli.LocalServer, "start_scan", side_effect=TimeoutError()):
            code, out, err = run_main("--ui", "none", "--scan", "memory")
        self.assertEqual((code, err), (0, "radiolack: the memory scan did not start in time\n"))
        self.assertRegex(out, URL_LINE)


class MainTest(unittest.TestCase):
    def test_busy_port(self):
        with socket.socket() as busy:
            busy.bind((LOOPBACK_HOST, 0))
            busy.listen()
            code, out, err = run_main("--ui", "none", "--port", str(busy.getsockname()[1]))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("cannot listen on 127.0.0.1:", err)

    def test_unreadable_interface(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("radiolack.server.WEB_DIR", Path(folder)):
            code, out, err = run_main("--ui", "none")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith(f"radiolack: cannot read the interface: {Path(folder) / 'index.html'}: "),
                        err)

    def test_saves_in_the_settings_folder(self):
        with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
            code, out, err = run_main("--ui", "none")
        self.assertEqual((code, err), (0, ""))
        directory = server.call_args.kwargs["store"].directory
        self.assertEqual(directory, Path(os.environ["XDG_CONFIG_HOME"]) / "radiolack")
        # tests/__init__.py: the test run has a settings folder of its own.
        self.assertTrue(directory.is_relative_to(tempfile.gettempdir()))

    def test_records_in_the_data_folder(self):
        with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
            code, out, err = run_main("--ui", "none")
        self.assertEqual((code, err), (0, ""))
        folder = server.call_args.kwargs["recordings"]
        self.assertEqual(folder, Path(os.environ["XDG_DATA_HOME"]) / "radiolack" / "recordings")
        # tests/__init__.py: the test run has a data folder of its own, where nothing was recorded.
        self.assertTrue(folder.is_relative_to(tempfile.gettempdir()))
        self.assertFalse(folder.exists())

    def test_checks_for_updates_from_the_debian_package_only(self):
        with mock.patch.object(cli, "LocalServer", wraps=cli.LocalServer) as server:
            code, out, err = run_main("--ui", "none")
        self.assertEqual((code, err), (0, ""))
        updates = server.call_args.kwargs["updates"]
        self.assertIsInstance(updates, UpdateCheck)
        # The tests' Radiolack, from the checkout or pip, never runs apt.
        self.assertEqual(updates.unavailable_reason(),
                         "Radiolack checks for updates when it runs from its Debian package.")

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

    def test_without_a_source_option_opens_the_receiver_used_last(self):
        opened = []

        class Recording(Sources):
            def open(self, reopen):
                opened.append(super().open(reopen))
                return opened[-1]

        with silent_rtl_tcp() as (rtl_tcp, address), own_settings() as settings:
            with mock.patch.object(cli, "Sources", Recording):
                self.assertEqual(run_main("--ui", "none")[0], 0)
                self.assertEqual([source.kind for source in opened], ["synthetic"])    # the first time
                settings.parent.mkdir(exist_ok=True)
                saved = {"version": 1, "source": {"kind": "rtl_tcp"},
                         "receivers": {"rtl_tcp": {"source": {"address": address}}}}
                settings.write_text(json.dumps(saved), encoding="utf-8")
                opened.clear()
                code, out, err = run_main("--ui", "none")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual([(source.kind, source.address) for source in opened], [("rtl_tcp", address)])
            with self.assertRaises(BlockingIOError):
                rtl_tcp.accept()

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
