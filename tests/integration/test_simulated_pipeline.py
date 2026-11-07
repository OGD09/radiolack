"""End-to-end run of `radiolack watch` against simulated rtl_tcp and Gqrx.

Real local TCP sockets on ephemeral ports, no SDR hardware and no real
Gqrx. Checks the bit-exact I/Q relay, the Gqrx command sequence, the
ten-second listen before any write, the squelch read-back and the clean
stops (SIGINT, and `radiolack stop` with SIGTERM) restoring Gqrx settings.
"""

import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from radiolack.storage import load_store

from tests.helpers import synthetic

SRC = Path(__file__).resolve().parents[2] / "src"
INITIAL_GQRX = {"frequency": "131000000", "mode": "AM", "width": "6000", "sql": "-50.5", "offset": "0", "dsp": "0"}
RESTORE = ["LNB_LO 0", "M AM 6000", "F 131000000", "L SQL -50.5", "U DSP 0"]


class Simulation:
    """rtl_tcp source, Gqrx remote control and Gqrx I/Q client on ephemeral ports."""

    def __init__(self, raw, *, client_delay_s):
        self.raw = raw
        self.client_delay_s = client_delay_s
        self.stopped = threading.Event()
        self.relayed_exactly = threading.Event()
        self.errors = []
        self.commands = []
        self.listening_at = []
        servers = []
        for _ in range(3):
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(0.5)
            servers.append(server)
        self.source, self.remote, reservation = servers
        self.source_port, self.remote_port, self.bridge_port = (server.getsockname()[1] for server in servers)
        reservation.close()  # the port RadioLack's bridge will listen on
        self.threads = [threading.Thread(target=self._guarded, args=(worker,), daemon=True)
                        for worker in (self._source_worker, self._remote_worker, self._client_worker)]

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        self.source.close()
        self.remote.close()
        for thread in self.threads:
            thread.join(timeout=3)

    def environment(self):
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])))
        for name in ("RADIOLACK_STORE", "RADIOLACK_DATA_DIR"):
            environment.pop(name, None)
        return environment

    def launch(self, store: Path, log):
        return subprocess.Popen(
            [sys.executable, "-m", "radiolack", "watch", "--external-source",
             "--rtl-tcp", f"127.0.0.1:{self.source_port}", "--bridge", f"127.0.0.1:{self.bridge_port}",
             "--gqrx", f"127.0.0.1:{self.remote_port}", "--center", "131000000",
             "--data-dir", str(store.parent), "--store", str(store), "--create-store"],
            stdout=log, stderr=subprocess.STDOUT, env=self.environment(), cwd=store.parent)

    def _guarded(self, worker):
        try:
            worker()
        except (OSError, RuntimeError) as error:
            if not self.stopped.is_set():
                self.errors.append((worker.__name__, repr(error)))
        except BaseException as error:  # assertion failures inside a simulator
            self.errors.append((worker.__name__, repr(error)))

    def _accept(self, server):
        while not self.stopped.is_set():
            try:
                return server.accept()[0]
            except socket.timeout:
                continue
        raise RuntimeError("stopped before a client connected")

    def _source_worker(self):
        with self._accept(self.source) as connection:
            connection.settimeout(10)
            connection.sendall(b"RTL0" + struct.pack("!II", 5, 29))
            while not self.stopped.is_set():
                if select.select([connection], [], [], 0)[0] and not connection.recv(4096):
                    return
                connection.sendall(self.raw)
                time.sleep(0.025)

    def _remote_worker(self):
        state = dict(INITIAL_GQRX)
        with self._accept(self.remote) as connection:
            connection.settimeout(30)
            stream = connection.makefile("rwb", buffering=0)
            while not self.stopped.is_set():
                data = stream.readline()
                if not data:
                    return
                command = data.decode().strip()
                self.commands.append(command)
                reply = "RPRT 0"
                if command == "f":
                    reply = state["frequency"]
                elif command == "m":
                    reply = f"{state['mode']}\n{state['width']}"
                elif command == "l SQL":
                    reply = state["sql"]
                elif command == "l STRENGTH":
                    reply = "-40"
                elif command == "l ?":
                    reply = "SQL STRENGTH LNA_GAIN"
                elif command == "l LNA_GAIN":
                    reply = "29.6"
                elif command == "LNB_LO":
                    reply = state["offset"]
                elif command == "u DSP":
                    reply = state["dsp"]
                elif command.startswith("F "):
                    state["frequency"] = command.split()[1]
                elif command.startswith("M "):
                    _, state["mode"], state["width"] = command.split()
                elif command.startswith("L SQL "):
                    state["sql"] = command.split()[2]
                    if float(state["sql"]) < 0 and not self.listening_at:
                        self.listening_at.append(time.monotonic())
                elif command.startswith("LNB_LO "):
                    state["offset"] = command.split()[1]
                elif command.startswith("U DSP "):
                    state["dsp"] = command.split()[2]
                else:
                    raise AssertionError(f"unexpected Gqrx command {command!r}")
                stream.write((reply + "\n").encode())

    def _client_worker(self):
        deadline = time.monotonic() + 10
        while True:
            try:
                connection = socket.create_connection(("127.0.0.1", self.bridge_port), timeout=1)
                break
            except ConnectionRefusedError:
                if time.monotonic() > deadline or self.stopped.is_set():
                    raise
                time.sleep(0.05)
        with connection:
            connection.settimeout(2)
            header = bytearray()
            while len(header) < 12:
                chunk = connection.recv(12 - len(header))
                if not chunk:
                    raise RuntimeError("bridge closed before the rtl_tcp header")
                header.extend(chunk)
            if header[:4] != b"RTL0":
                raise AssertionError("the bridge did not relay the rtl_tcp header")
            control = struct.pack("!BI", 1, 131000000) + struct.pack("!BI", 2, 2400000)
            connection.sendall(control[:3])  # a command split across TCP segments
            connection.sendall(control[3:])
            time.sleep(self.client_delay_s)  # Gqrx may initialise before consuming I/Q
            first = bytearray()
            while not self.stopped.is_set():
                try:
                    data = connection.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    return
                if len(first) < len(self.raw):
                    first.extend(data[:len(self.raw) - len(first)])
                    if len(first) == len(self.raw):
                        if bytes(first) != self.raw:
                            raise AssertionError("the bridge changed the I/Q stream")
                        self.relayed_exactly.set()


def stop(process, how):
    process.send_signal(how)
    try:
        return process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise


class SimulatedPipelineTests(unittest.TestCase):
    def test_bit_exact_relay_then_ten_second_listen_before_the_write(self):
        raw = synthetic([(131500000, 0.12), (131700000, 0.08)])
        with tempfile.TemporaryDirectory() as directory, Simulation(raw, client_delay_s=2.5) as simulation:
            store, log_path = Path(directory) / "observations.json", Path(directory) / "stdout.log"
            with log_path.open("w") as log:
                process = simulation.launch(store, log)
                try:
                    event = None
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline and event is None:
                        self.assertEqual(simulation.errors, [], log_path.read_text())
                        self.assertIsNone(process.poll(), log_path.read_text())
                        observations = load_store(store)["observations"] if store.exists() else []
                        if simulation.listening_at:
                            elapsed = time.monotonic() - simulation.listening_at[0]
                            if elapsed < 9.5:
                                self.assertEqual(observations, [], "stored before the ten-second listen ended")
                        if observations:
                            self.assertGreaterEqual(time.monotonic() - simulation.listening_at[0], 9.9)
                            event = observations[0]
                        time.sleep(0.1)
                    self.assertIsNotNone(event, log_path.read_text())
                    tunes = [command for command in simulation.commands if command.startswith("F ")]
                    data = load_store(store)
                finally:
                    code = stop(process, signal.SIGINT)
            output = log_path.read_text()
        self.assertEqual(code, 130, output)
        self.assertTrue(simulation.relayed_exactly.is_set(), output)
        self.assertEqual((event["frequency_hz"], event["listening_seconds"], event["telemetry_version"]),
                         (131500000, 10, 2), event)
        self.assertGreaterEqual(event["active_seconds"], 8)
        self.assertGreater(event["fft_measurements"]["signal_bin_dbfs"]["samples"], 5)
        self.assertGreater(event["gqrx_measurements"]["meter_dbfs"]["samples"], 5)
        self.assertEqual(event["gqrx_measurements"]["gain_stages_db"], {"LNA": 29.6})
        self.assertEqual(event["scan"]["mode"], "fixed_window")
        self.assertEqual(event["squelch_dbfs"], -46)
        station = data["stations"]["131500000"]
        self.assertEqual((station["squelch_dbfs"], station["last_verified_squelch_dbfs"]), (-46, -46))
        self.assertIn("L SQL -46.0", simulation.commands)
        self.assertIn("l SQL", simulation.commands)
        self.assertTrue(set(tunes) <= {"F 131500000", "F 131700000"}, tunes)
        self.assertEqual(simulation.commands[-len(RESTORE):], RESTORE)
        self.assertIn("Stopped. Incomplete listens were not logged.", output)

    def test_stop_command_terminates_the_monitor_and_restores_gqrx(self):
        with tempfile.TemporaryDirectory() as directory, Simulation(synthetic(), client_delay_s=0) as simulation:
            store, log_path = Path(directory) / "observations.json", Path(directory) / "stdout.log"
            with log_path.open("w") as log:
                process = simulation.launch(store, log)
                try:
                    deadline = time.monotonic() + 15
                    while "Gqrx connected" not in log_path.read_text() and time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), log_path.read_text())
                        time.sleep(0.1)
                    self.assertIn("Gqrx connected", log_path.read_text())
                    self.assertEqual(simulation.errors, [])
                    result = subprocess.run(
                        [sys.executable, "-m", "radiolack", "stop", "--store", str(store), "--timeout", "10"],
                        capture_output=True, text=True, timeout=30, env=simulation.environment())
                    code = process.wait(timeout=15)
                finally:
                    if process.poll() is None:
                        stop(process, signal.SIGKILL)
            output = log_path.read_text()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Stopped RadioLack process {process.pid}", result.stdout)
        self.assertEqual(code, 143, output)
        self.assertEqual(simulation.commands[-len(RESTORE):], RESTORE)


if __name__ == "__main__":
    unittest.main()
