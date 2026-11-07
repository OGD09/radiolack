"""Socket ownership from /proc: fake process tables, then real loopback sockets."""

import os
from pathlib import Path
import socket
import tempfile
import unittest

from radiolack.ownership import command_line, connected_pid, describe, foreign_remote_control, listening_pids

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def row(local, remote, state, inode):
    return (f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000  1000        0 "
            f"{inode} 1 0000000000000000 100 0 0 10 0\n")


class FakeProc:
    """Minimal /proc: TCP tables, per-process descriptors and command lines."""

    def __init__(self, root: Path):
        self.root = root
        (root / "net").mkdir()
        self.tables = {"net/tcp": [HEADER], "net/tcp6": [HEADER]}

    def socket(self, table, local_port, remote_port, state, inode):
        address = "0100007F" if table == "net/tcp" else "0" * 24 + "0100007F"
        remote = f"{address}:{remote_port:04X}" if remote_port else f"{'0' * len(address)}:0000"
        self.tables[table].append(row(f"{address}:{local_port:04X}", remote, state, inode))

    def process(self, pid, arguments, *inodes):
        descriptors = self.root / str(pid) / "fd"
        descriptors.mkdir(parents=True)
        (self.root / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments) + b"\0")
        for number, inode in enumerate(inodes, start=3):
            os.symlink(f"socket:[{inode}]", descriptors / str(number))

    def write(self):
        for name, lines in self.tables.items():
            (self.root / name).write_text("".join(lines))


class FakeProcTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.proc = FakeProc(Path(self.directory.name))
        self.root = self.proc.root

    def tearDown(self):
        self.directory.cleanup()

    def test_remote_control_held_by_another_gqrx_is_described(self):
        proc = self.proc
        proc.socket("net/tcp6", 7356, 0, "0A", 1001)       # old Gqrx: remote control on IPv6 any
        proc.socket("net/tcp", 40000, 1235, "01", 2002)    # new Gqrx: I/Q connection to the bridge
        proc.socket("net/tcp", 1235, 40000, "01", 2003)    # bridge side of the same connection
        proc.socket("net/tcp", 7356, 51000, "06", 0)       # TIME_WAIT leftover, owned by nobody
        proc.process(10410, ["gqrx", "-c", "/home/u/pc_repair/gqrx-peaks.conf"], 1001)
        proc.process(68024, ["gqrx", "-c", "/home/u/radiolack/data/gqrx-radiolack.conf"], 2002)
        proc.process(67839, ["python3", "-m", "radiolack", "scan"], 2003)
        proc.write()
        self.assertEqual(listening_pids(7356, proc=self.root), {10410})
        self.assertEqual(connected_pid(40000, 1235, proc=self.root), 68024)
        self.assertEqual(foreign_remote_control(40000, 1235, 7356, proc=self.root),
                         "the Gqrx remote-control port 7356 belongs to PID 10410 (gqrx -c "
                         "/home/u/pc_repair/gqrx-peaks.conf), not to the Gqrx reading the I/Q bridge "
                         "(PID 68024)")

    def test_a_second_listener_on_the_port_is_reported_too(self):
        proc = self.proc
        proc.socket("net/tcp", 7356, 0, "0A", 1001)
        proc.socket("net/tcp6", 7356, 0, "0A", 1002)
        proc.socket("net/tcp", 40000, 1235, "01", 2002)
        proc.process(68024, ["gqrx"], 1001, 2002)
        proc.process(10410, ["gqrx", "-c", "old.conf"], 1002)
        proc.write()
        self.assertIn("belongs to PID 10410 (gqrx -c old.conf)",
                      foreign_remote_control(40000, 1235, 7356, proc=self.root))

    def test_the_reader_holding_the_remote_control_or_unknown_owners_never_block(self):
        proc = self.proc
        proc.socket("net/tcp", 7356, 0, "0A", 1001)
        proc.socket("net/tcp", 40000, 1235, "01", 2002)
        proc.socket("net/tcp", 7357, 0, "0A", 1003)        # owner not visible (another user)
        proc.process(68024, ["gqrx"], 1001, 2002)
        (self.root / "4242").mkdir()                       # process without a readable fd directory
        proc.write()
        self.assertIsNone(foreign_remote_control(40000, 1235, 7356, proc=self.root), "same Gqrx")
        self.assertIsNone(foreign_remote_control(40001, 1235, 7356, proc=self.root), "reader unknown")
        self.assertIsNone(foreign_remote_control(40000, 1235, 7357, proc=self.root), "holder unknown")
        self.assertIsNone(foreign_remote_control(40000, 1235, 7358, proc=self.root), "nothing listening")
        self.assertEqual(listening_pids(7357, proc=self.root), set())

    def test_missing_tables_and_malformed_rows(self):
        self.assertEqual(listening_pids(7356, proc=self.root / "absent"), set())
        self.assertIsNone(connected_pid(40000, 1235, proc=self.root / "absent"))
        proc = self.proc
        proc.tables["net/tcp"] += ["   1: truncated\n", row("0100007F:ZZZZ", "00000000:0000", "0A", 1009)]
        proc.socket("net/tcp", 7356, 0, "0A", 1001)
        proc.process(68024, ["gqrx"], 1001, 1009)
        proc.write()
        self.assertEqual(listening_pids(7356, proc=self.root), {68024})

    def test_command_lines_are_read_and_shortened(self):
        self.proc.process(5, ["gqrx", "-c", "x" * 200])
        self.assertEqual(command_line(5, proc=self.root), "gqrx -c " + "x" * 200)
        described = describe(5, proc=self.root)
        self.assertTrue(described.startswith("PID 5 (gqrx -c xxx") and described.endswith("...)"))
        self.assertEqual(len(described), len("PID 5 ()") + 100)
        self.assertEqual(command_line(6, proc=self.root), "?")


@unittest.skipUnless(Path("/proc/net/tcp").exists(), "needs Linux /proc")
class RealSocketTests(unittest.TestCase):
    def test_this_process_owns_both_ends_of_a_loopback_connection(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                accepted, _ = server.accept()
                with accepted:
                    client_port = client.getsockname()[1]
                    self.assertIn(os.getpid(), listening_pids(port))
                    self.assertEqual(connected_pid(client_port, port), os.getpid())
                    self.assertIsNone(foreign_remote_control(client_port, port, port))

    def test_ipv6_listener_is_found(self):
        try:
            server = socket.socket(socket.AF_INET6)
        except OSError:
            self.skipTest("no IPv6 support")
        with server:
            try:
                server.bind(("::1", 0))
            except OSError:
                self.skipTest("no IPv6 loopback")
            server.listen(1)
            self.assertIn(os.getpid(), listening_pids(server.getsockname()[1]))


if __name__ == "__main__":
    unittest.main()
