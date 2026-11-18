"""Tests for the tool that checks that a SoapySDR search by driver stays off
the network (tools/check_search.py). They never run a search, which loads
SoapySDR's modules and reads the USB devices: they check its arguments and
its report, and, where this computer lets a process make a network
namespace of its own and compile C, that its trap refuses and reports
sockets and replaces librtlsdr's listing."""

import importlib.util
import io
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module     # its dataclasses look their module up by this name
    spec.loader.exec_module(module)
    return module


check = load("check_search")


def namespaces() -> bool:
    """Whether this process may make a user and network namespace."""
    if shutil.which("unshare") is None:
        return False
    try:
        return subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class ArgumentsTest(unittest.TestCase):
    def test_the_rtlsdr_driver_and_odd_names_are_refused(self):
        for argv in (["rtlsdr"], ["hackrf", "rtlsdr"], [""], ["driver=hackrf"], ["hack rf"], ["HackRF"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr", io.StringIO()):
                    check.build_parser().parse_args(argv)

    def test_the_designs_drivers_by_default(self):
        self.assertEqual(check.build_parser().parse_args([]).drivers, list(check.DRIVERS))
        self.assertEqual(check.DRIVERS, ("hackrf", "airspy", "airspyhf", "lime", "bladerf", "sdrplay"))
        self.assertEqual(check.build_parser().parse_args(["remote", "lime"]).drivers, ["remote", "lime"])


class ReportTest(unittest.TestCase):
    def test_the_traps_lines_are_read(self):
        stderr = "\n".join((
            f"{check.TRAP} installed",
            "[INFO] something a module says",
            f"{check.TRAP} socket family=1 type=1",
            f"{check.TRAP} socket family=10 type=2",
            f"{check.TRAP} socket family=99 type=7",
            f"{check.TRAP} rtlsdr_get_device_count",
        ))
        sockets, rtlsdr, installed = check.trapped(stderr)
        self.assertTrue(installed)
        self.assertEqual(sockets, ["unix stream", "inet6 datagram", "family 99 type 7"])
        self.assertEqual(rtlsdr, ["rtlsdr_get_device_count"])
        self.assertEqual(check.trapped("nothing")[2], False)

    def test_each_verdict(self):
        searches = [check.Search("hackrf", registered=True, found=0),
                    check.Search("sdrplay", registered=False, found=0),
                    check.Search("remote", registered=True, found=0, sockets=["unix stream"]),
                    check.Search("lime", registered=True, rtlsdr=["rtlsdr_open"]),
                    check.Search("airspy", registered=True, problem="no answer within 60 s")]
        rows = {line.split(" | ")[0].lstrip("| "): line.rstrip(" |").rsplit(" | ", 1)[1]
                for line in check.report(searches)[2:]}
        self.assertEqual(rows, {"hackrf": "stays off the network",
                                "sdrplay": "no module installed here registers it: nothing searched",
                                "remote": "tried to make a socket", "lime": "called librtlsdr",
                                "airspy": "could not run: no answer within 60 s"})
        self.assertTrue(searches[0].clean)
        self.assertFalse(searches[2].clean)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "x86_64", "the trap is for Linux on x86-64")
@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
@unittest.skipUnless(namespaces(), "this computer does not let a process make a network namespace")
class TrapTest(unittest.TestCase):
    def test_the_trap_refuses_and_reports_sockets_and_replaces_librtlsdr(self):
        with tempfile.TemporaryDirectory() as folder:
            library = check.build(Path(folder), "cc")
            self.assertEqual(check.self_check(library), "")
            # A socket the trap lets through, netlink's, still works.
            result = check.run(check.command(library, *check.python(
                "import socket; socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 15).close(); print('netlink')")))
        self.assertEqual(result.stdout.strip(), "netlink")
        self.assertEqual(check.trapped(result.stderr)[:2], ([], []))


if __name__ == "__main__":
    unittest.main()
