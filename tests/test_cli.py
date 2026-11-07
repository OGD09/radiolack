"""Command line: usage errors, import, stats, Gqrx profile, check and stop (no reception)."""

import contextlib
import functools
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from radiolack import cli
from radiolack.gqrx import prepare_gqrx_profile
from radiolack.storage import ObservationStore, lock_path_for

from tests.helpers import legacy_journal

# Child processes that hold a store lock; the PID is written like RadioLack does.
LOCK_HOLDER = """
import fcntl, os, time
handle = open(os.environ["LOCK_PATH"], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
handle.seek(0)
handle.truncate()
handle.write(f"{os.getpid()}\\n")
handle.flush()
print("ready", flush=True)
time.sleep(60)
"""
# The base interpreter path: a virtual environment inside the project would
# put "radiolack" in the child's command line.
PYTHON = os.path.realpath(sys.executable)


def free_ports(count):
    """Distinct loopback ports that nothing listens on (never the default ports)."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(item) for item in argv])
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = self.root / "data" / "observations.json"
        self.source = self.root / "airband-peaks.json"
        self.source.write_text(json.dumps(legacy_journal()))
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("RADIOLACK_STORE", "RADIOLACK_DATA_DIR"):
            os.environ.pop(name, None)
        rtl, bridge, gqrx = free_ports(3)
        self.ports = ["--rtl-tcp", f"127.0.0.1:{rtl}", "--bridge", f"127.0.0.1:{bridge}",
                      "--gqrx", f"127.0.0.1:{gqrx}"]
        self.bridge_port, self.gqrx_port = bridge, gqrx


class InformationCommandTests(CliTestCase):
    def test_profiles_and_plan(self):
        code, out, _ = run("profiles")
        self.assertEqual(code, 0)
        self.assertIn("airband-8k33", out)
        code, out, _ = run("plan")
        self.assertEqual(code, 0)
        self.assertIn("32 overlapping windows", out)
        self.assertIn("all 2280 grid channels", out)
        self.assertEqual(run("plan", "--rate", "1000")[0], 2)
        code, _, err = run("plan", "--profile", "no-such-profile")
        self.assertEqual(code, 2)
        self.assertIn("Unknown profile", err)


class ImportAndStatsTests(CliTestCase):
    def test_import_is_idempotent_and_stats_read_the_store(self):
        code, out, _ = run("import", self.source, "--store", self.store, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing was written.", out)
        self.assertFalse(self.store.exists())
        code, out, _ = run("import", self.source, "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("Observations: 3 added, 0 already present, 0 conflicts", out)
        self.assertIn("Legacy counters: identical", out)
        self.assertIn("The source file was only read.", out)
        code, out, _ = run("import", self.source, "--store", self.store)
        self.assertIn("Nothing new: the store was not modified.", out)
        code, out, _ = run("stats", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("Validated listens: 3 | frequencies: 2 | squelch memories: 1", out)
        self.assertIn("135.925000", out)
        self.assertIn("not conversations", out)
        code, out, _ = run("stats", "--store", self.store, "--json")
        report = json.loads(out)
        self.assertEqual(report["summary"]["validated_listens"], 3)
        self.assertEqual(list(report["frequencies"]), ["135925000", "136575000"])
        _, out, _ = run("stats", "--store", self.store, "--json", "--frequency", "136.575")
        self.assertEqual(list(json.loads(out)["frequencies"]), ["136575000"])
        _, out, _ = run("stats", "--store", self.store, "--json", "--limit", "1")
        self.assertEqual(list(json.loads(out)["frequencies"]), ["135925000"])

    def test_storage_problems_use_their_own_exit_code(self):
        code, _, err = run("stats", "--store", self.store)
        self.assertEqual(code, 3)
        self.assertIn("storage error: No observation store", err)
        self.store.parent.mkdir()
        self.store.write_text("{broken")
        code, _, err = run("stats", "--store", self.store)
        self.assertEqual((code, self.store.read_text()), (3, "{broken"))
        self.assertIn("not valid JSON", err)
        self.assertEqual(run("import", self.store, "--store", self.root / "other.json")[0], 3)
        self.assertEqual(run("import", self.source, "--store", self.source)[0], 3)

    def test_environment_variables_select_the_data_directory_and_store(self):
        os.environ["RADIOLACK_DATA_DIR"] = str(self.root / "env-data")
        self.assertEqual(run("import", self.source)[0], 0)
        self.assertTrue((self.root / "env-data" / "observations.json").exists())
        os.environ["RADIOLACK_STORE"] = str(self.root / "elsewhere.json")
        code, _, err = run("stats")
        self.assertEqual(code, 3)
        self.assertIn("elsewhere.json", err)


class GqrxProfileCommandTests(CliTestCase):
    def test_separate_profile_is_created_once(self):
        with patch.object(cli, "prepare_gqrx_profile", functools.partial(prepare_gqrx_profile, base=None)):
            code, out, _ = run("gqrx-profile", "--data-dir", self.root / "data")
            self.assertEqual(code, 0)
            path = self.root / "data" / "gqrx-radiolack.conf"
            text = path.read_text()
            self.assertIn('device="rtl_tcp=127.0.0.1:1235"', text)
            self.assertIn("port=7356", text)
            self.assertIn(f"gqrx -c {path}", out)
            code, _, err = run("gqrx-profile", "--data-dir", self.root / "data")
            self.assertEqual(code, 1)
            self.assertIn("already exists; it was not overwritten", err)
            self.assertEqual(path.read_text(), text)
        self.assertEqual(run("gqrx-profile", "--output", self.root / "x.conf", "--bridge", "192.168.1.5:1235")[0], 2)
        self.assertEqual(run("gqrx-profile", "--output", self.root / "x.conf", "--gqrx", "127.0.0.1:1235")[0], 2)
        self.assertFalse((self.root / "x.conf").exists())


class MonitorCommandTests(CliTestCase):
    def test_invalid_options_are_rejected_before_any_action(self):
        before = signal.getsignal(signal.SIGTERM)
        missing = self.root / "missing.json"
        cases = (["--bridge", "10.0.0.1:1235"], ["--gqrx", "192.168.1.2:7356"], ["--rtl-tcp", "192.168.1.2:1234"],
                 ["--rtl-tcp", "127.0.0.1:7356", "--gqrx", "127.0.0.1:7356"], ["--rate", "1000"],
                 ["--center", "140000000"], ["--margin-db", "2"], ["--min-active-seconds", "20"],
                 ["--meter-guard-seconds", "0.1"], ["--gain", "60"], ["--window-seconds", "0.1"])
        for extra in cases:
            code, _, err = run("scan", *self.ports, "--store", missing, *extra)
            self.assertEqual(code, 2, (extra, err))
        self.assertEqual(run("watch", *self.ports, "--store", missing, "--window-seconds", "2")[0], 2)
        self.assertFalse(missing.exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_missing_store_stops_before_reception_starts(self):
        code, out, err = run("watch", "--external-source", *self.ports, "--store", self.root / "missing.json")
        self.assertEqual(code, 3)
        self.assertIn("--create-store", err)
        self.assertEqual(out, "")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.bridge_port))  # the bridge port was never opened


class CheckCommandTests(CliTestCase):
    def check(self, *extra):
        with patch.object(cli.shutil, "which", return_value="/usr/bin/tool"), \
                patch.object(cli, "_usb_rtl_sdr_present", return_value=[]):
            return run("check", *self.ports, "--data-dir", self.root / "data", "--store", self.store, *extra)

    def test_read_only_report(self):
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn(f"bridge port {self.bridge_port}", out)
        self.assertIn("not reachable", out)
        self.assertIn("missing: run `radiolack import ...`", out)
        self.assertFalse(self.store.exists())
        with socket.socket() as gqrx:
            gqrx.bind(("127.0.0.1", self.gqrx_port))
            gqrx.listen(1)
            # The listener is this test process, not a Gqrx started with the RadioLack profile.
            code, out, _ = self.check()
            self.assertEqual(code, 0)
            self.assertRegex(out, rf"WARN +Gqrx remote control +reachable on 127\.0\.0\.1:{self.gqrx_port} but "
                                  rf"held by PID {os.getpid()} \(.*not by a Gqrx started with gqrx-radiolack\.conf")
            with patch.object(cli, "command_line", return_value="gqrx -c /home/u/data/gqrx-radiolack.conf"):
                out = self.check()[1]
            self.assertRegex(out, rf"OK +Gqrx remote control +reachable on 127\.0\.0\.1:{self.gqrx_port}: "
                                  rf"PID {os.getpid()} \(")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.bridge_port))
            busy.listen(1)
            code, out, _ = self.check()
            self.assertEqual(code, 1)
            self.assertIn("in use by another program", out)

    def test_store_in_use_is_reported(self):
        with ObservationStore(self.store, create=True):
            code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn(f"in use by RadioLack process {os.getpid()}", out)


class StopCommandTests(CliTestCase):
    def holder(self, *marker):
        lock = lock_path_for(self.store)
        lock.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen([PYTHON, "-c", LOCK_HOLDER, *marker], stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, LOCK_PATH=str(lock)))
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def test_nothing_to_stop(self):
        code, out, _ = run("stop", "--store", self.store)
        self.assertEqual(code, 0)
        self.assertIn("No RadioLack process is using", out)

    def test_radiolack_process_is_terminated_and_waited_for(self):
        child = self.holder("radiolack")
        code, out, _ = run("stop", "--store", self.store, "--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn(f"Stopped RadioLack process {child.pid}", out)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_unrelated_process_is_never_signalled(self):
        child = self.holder()
        code, _, err = run("stop", "--store", self.store, "--timeout", "1")
        self.assertEqual(code, 1)
        self.assertIn("does not look like RadioLack; it was not signalled", err)
        self.assertIsNone(child.poll())


if __name__ == "__main__":
    unittest.main()
