#!/usr/bin/env python3
"""Measure whether several radios run in one Radiolack: later-milestones.md,
section 5, the first pull request, and spike 9 of scoping.md.

A radio is what the engine is for one receiver. Before the engine is split
into radios, two of them in one Radiolack are two engines in one process,
on one event loop, as the radios will be: the tool starts that process
(``--serve``, which it runs itself), with ``--radios`` engines, each served
by the server's own application on an ephemeral port of 127.0.0.1 and
reading an rtl_tcp source (``RtlTcpSource``, the client of an rtl_tcp that
the user runs): the receiver's samples come through a socket, the source's
buffer and its conversion, as from a receiver. The receivers are stand-ins
for rtl_tcp that this tool serves in a process of its own, outside
Radiolack as rtl_tcp is: each sends a scene of 2 s, made beforehand in
rtl_tcp's unsigned bytes, again and again, at 2.4 MS/s on the wall clock
(4.8 MB/s), and counts what it could not send, a Radiolack behind. They
declare an E4000 tuner, whose centre needs no grid, so that the VFO lies on
the station.

Each radio has a listener: an interface's stream in this tool's process,
over the WebSocket, which tunes the VFO to the scene's station in NFM, asks
for audio, takes the spectrum frames as they come and plays the audio frames
as the interface's AudioWorklet does (audio-worklet.js): it starts once
TARGET_S of audio is buffered, plays at the audio rate, runs dry when the
buffer empties, then waits for TARGET_S again; a gap in the frames'
positions is audio the engine dropped for a client behind. The clocks are
one computer's, so no drift needs following.

Radiolack's processor time is read from /proc every second, the whole
process and each of its threads, after ``--warmup-s`` that are not counted,
for ``--seconds``: ten minutes by default, as the design asks. The rule
fixed beforehand: the radios pass when no listener's audio runs dry and
Radiolack stays under 60% of two cores, 120% of one core as a system
monitor counts it, in every 10 s of the measure. If they fail, each radio
gets a process of its own.

The scene: white noise at -40 dBFS, an NFM station at -30 dBFS 300 kHz
above the centre frequency, the VFO on it, its 1 kHz tone at 2.5 kHz of
deviation, and three steady carriers within 1.2 MHz of the centre, for
detection, as in measure_rates.py; every frequency a whole number of hertz,
so that the scene repeats without a seam. Detection is on, as the engine
starts. No store, so nothing is saved; temporary XDG folders all the same.

Linux only (/proc). It needs no receiver and reaches no network: every
socket is on 127.0.0.1, on ports the system chooses. It runs from a
checkout of the repository; the Debian package does not install it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import signal
import socket
import statistics
import struct
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import numpy as np

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

RATE_HZ = 2_400_000.0
CENTRE_HZ = 145_000_000.0       # the rtl_tcp client's default: no command needed
VFO_OFFSET_HZ = 300_000.0
MODE = "NFM"
SCENE_S = 2.0                   # the receivers' scene, sent again and again
NOISE_DBFS = -40.0
STATION_DBFS = -30.0
TONE_HZ = 1_000.0
DEVIATION_HZ = 2_500.0
CARRIERS = ((-800_000.0, -45.0), (-450_000.0, -55.0), (900_000.0, -50.0))     # offset (Hz), dBFS
TUNER_E4000 = 1                 # rtl_tcp's tuner type: no grid for its centre
E4000_GAINS = 14
SEND_PERIOD_S = 0.005           # a receiver sends what is due this often
BEHIND_S = 0.5                  # a Radiolack this far behind a receiver loses the rest
TARGET_S = 0.15                 # audio-worklet.js: buffered when playback starts
WINDOW_S = 10                   # the rule's windows of processor time
LIMIT_PCT = 120.0               # 60% of two cores, in % of one core
MAX_RADIOS = 4                  # later-milestones.md, section 5
AUDIO_KIND = 2
AUDIO_HEADER = struct.Struct("<BBHId")
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 15.0
EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130


# --------------------------------------------------------------- the receivers

def scene_bytes(seed: int, rate_hz: float = RATE_HZ, seconds: float = SCENE_S) -> bytes:
    """The scene in rtl_tcp's unsigned bytes, I then Q, as a receiver
    sends them: deterministic for a seed."""
    count = round(seconds * rate_hz)
    t = np.arange(count) / rate_hz
    rng = np.random.default_rng(seed)
    noise_rms = 10 ** (NOISE_DBFS / 20) / math.sqrt(2)
    samples = rng.normal(0.0, noise_rms, count) + 1j * rng.normal(0.0, noise_rms, count)
    phase = 2 * np.pi * VFO_OFFSET_HZ * t + DEVIATION_HZ / TONE_HZ * np.sin(2 * np.pi * TONE_HZ * t)
    samples += 10 ** (STATION_DBFS / 20) * np.exp(1j * phase)
    for offset_hz, level_dbfs in CARRIERS:
        samples += 10 ** (level_dbfs / 20) * np.exp(2j * np.pi * offset_hz * t)
    pairs = np.empty(2 * count)
    pairs[0::2], pairs[1::2] = samples.real, samples.imag
    return np.clip(np.round(pairs * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


class Receiver:
    """An rtl_tcp stand-in on an ephemeral port of 127.0.0.1: rtl_tcp's
    header, then ``scene`` again and again at ``rate_hz`` on the wall clock,
    to one client at a time; the commands it receives are read and left
    alone. What it could not send, the client more than BEHIND_S behind,
    it drops, and counts in seconds of signal."""

    def __init__(self, scene: bytes, rate_hz: float = RATE_HZ) -> None:
        self.scene = scene
        self.rate_hz = rate_hz
        self.port = 0
        self.dropped_s = 0.0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        commands = asyncio.ensure_future(self._read_commands(reader))
        try:
            writer.write(struct.pack(">4sII", b"RTL0", TUNER_E4000, E4000_GAINS))
            await self._send(writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._tasks.discard(task)
            commands.cancel()
            writer.close()

    @staticmethod
    async def _read_commands(reader: asyncio.StreamReader) -> None:
        while await reader.read(4096):
            pass

    async def _send(self, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        bytes_per_s = 2 * self.rate_hz
        anchor = loop.time()
        position = 0            # bytes sent or dropped since the anchor
        offset = 0              # where the scene goes on
        while True:
            due = round((loop.time() - anchor) * bytes_per_s) // 2 * 2
            if due - position > BEHIND_S * bytes_per_s:
                self.dropped_s += (due - position) / bytes_per_s
                offset = (offset + due - position) % len(self.scene)
                position = due
            while position < due:
                part = self.scene[offset:offset + due - position]
                writer.write(part)
                position += len(part)
                offset = (offset + len(part)) % len(self.scene)
            await writer.drain()
            await asyncio.sleep(SEND_PERIOD_S)


# --------------------------------------------------------------- the listeners

@dataclass
class Playback:
    """The interface's playback of one radio's audio, as audio-worklet.js
    plays it, on the clock of the frames' arrivals."""

    target_s: float = TARGET_S
    frames: int = 0
    samples: int = 0            # received
    dry: int = 0                # times playback ran dry
    missing_s: float = 0.0      # audio the engine dropped, gaps in the positions
    lowest_s: float | None = None   # the least audio left buffered while playing, before a frame came
    longest_gap_s: float = 0.0  # the longest time between two frames
    rate_hz: float = 0.0
    _buffered: float = 0.0      # samples
    _playing: bool = False
    _at: float | None = None    # when the buffer was last brought up to date
    _next: int | None = None    # the position the next frame should have

    def frame(self, now: float, position: int, rate_hz: float, count: int) -> None:
        """A frame of ``count`` samples at ``position`` came at ``now``."""
        if rate_hz != self.rate_hz:
            self.rate_hz, self._next = rate_hz, None
        self.advance(now)
        if self._playing:
            left = self._buffered / rate_hz
            self.lowest_s = left if self.lowest_s is None else min(self.lowest_s, left)
        if self._next is not None:
            gap = (position - self._next) % 2 ** 32
            if gap and gap < 2 ** 31:
                self.missing_s += gap / rate_hz
                self._buffered += gap       # played as silence, as the worklet does
        self._next = (position + count) % 2 ** 32
        self._buffered += count
        self.frames += 1
        self.samples += count
        if not self._playing and self._buffered >= self.target_s * rate_hz:
            self._playing = True

    def advance(self, now: float) -> None:
        """Play the audio up to ``now``: it runs dry if the buffer empties."""
        if self._at is not None:
            self.longest_gap_s = max(self.longest_gap_s, now - self._at)
            if self._playing:
                self._buffered -= (now - self._at) * self.rate_hz
                if self._buffered < 0:
                    self.dry += 1
                    self._buffered = 0.0
                    self._playing = False
        self._at = now

    def counted_from(self, now: float) -> None:
        """Forget the warm-up's figures, keeping the buffer as it is."""
        self.advance(now)
        self.frames = self.samples = self.dry = 0
        self.missing_s = self.longest_gap_s = 0.0
        self.lowest_s = None


def parse_audio(message: bytes) -> tuple[int, float, int] | None:
    """An audio frame's position, rate and number of samples (protocol.md,
    16), or None for another binary frame."""
    if len(message) < AUDIO_HEADER.size or message[0] != AUDIO_KIND:
        return None
    _, _, _, position, rate_hz = AUDIO_HEADER.unpack_from(message)
    return position, rate_hz, (len(message) - AUDIO_HEADER.size) // 2


@dataclass
class Listener:
    port: int
    playback: Playback = field(default_factory=Playback)
    spectra: int = 0            # spectrum and other binary frames taken
    hello: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


async def listen(listener: Listener, ready: asyncio.Event) -> None:
    """One interface's stream to one radio until cancelled."""
    import aiohttp
    origin = f"http://127.0.0.1:{listener.port}"
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{origin}/stream", headers={"Origin": origin},
                                      max_msg_size=0, autoping=True) as ws:
            listener.hello = await ws.receive_json()
            await ws.send_json({"type": "demod", "frequency_hz": CENTRE_HZ + VFO_OFFSET_HZ, "mode": MODE})
            await ws.send_json({"type": "audio", "on": True})
            ready.set()
            async for message in ws:
                if message.type is aiohttp.WSMsgType.BINARY:
                    audio = parse_audio(message.data)
                    if audio is None:
                        listener.spectra += 1
                    else:
                        listener.playback.frame(loop.time(), *audio)
                elif message.type is aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data.get("type") == "error":
                        listener.errors.append(str(data.get("message")))
                else:
                    break


# --------------------------------------------------------------- Radiolack's process

async def serve(receiver_ports: Sequence[int], out: TextIO) -> None:
    """Radiolack with one engine per receiver, each with the server's own
    application, in one event loop: prints the applications' ports as one
    JSON line, then runs until SIGTERM or its input closes."""
    from aiohttp import web

    from radiolack.engine import Engine
    from radiolack.remote import RemoteAccess
    from radiolack.rtltcp import RtlTcpSource
    from radiolack.server import InterfaceFiles, RemoteListener, WEB_DIR, create_app

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_reader(sys.stdin.fileno(), lambda: sys.stdin.buffer.read1() or stop.set())
    interface = InterfaceFiles(WEB_DIR)
    served = []
    for receiver_port in receiver_ports:
        engine = Engine(RtlTcpSource("127.0.0.1", receiver_port))
        remote = RemoteListener(engine, RemoteAccess(), lambda: [], "measure", interface)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        runner = web.AppRunner(create_app(port, engine, remote, interface), handle_signals=False)
        await runner.setup()
        await web.SockSite(runner, sock).start()
        served.append((port, engine, remote, runner))
    print(json.dumps([port for port, *_ in served]), file=out, flush=True)
    try:
        await stop.wait()
    finally:
        for _, engine, remote, runner in served:
            engine.close()
            await remote.close()
            await runner.cleanup()


# --------------------------------------------------------------- processor time

def clock_ticks() -> int:
    return os.sysconf("SC_CLK_TCK")


def read_cpu(pid: int, proc: Path = Path("/proc")) -> tuple[int, dict[int, tuple[str, int]]]:
    """The processor time of process ``pid`` in clock ticks, user and
    system, and that of each of its threads with its name."""
    def ticks(stat: str) -> tuple[str, int]:
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2:].split()
        return name, int(fields[11]) + int(fields[12])     # utime, stime: fields 14 and 15
    total = ticks((proc / str(pid) / "stat").read_text())[1]
    threads = {}
    for task in (proc / str(pid) / "task").iterdir():
        try:
            threads[int(task.name)] = ticks((task / "stat").read_text())
        except (OSError, ValueError):
            continue        # a thread that just ended
    return total, threads


@dataclass
class Sample:
    at: float               # time.monotonic()
    total: int              # ticks
    threads: dict[int, tuple[str, int]]


def windows_pct(samples: Sequence[Sample], window_s: float = WINDOW_S, hz: int | None = None) -> list[float]:
    """Radiolack's processor time in each whole window of ``window_s``, in %
    of one core."""
    hz = clock_ticks() if hz is None else hz
    found = []
    start = 0
    for index, sample in enumerate(samples):
        if sample.at - samples[start].at >= window_s - 1e-6:
            spent = (sample.total - samples[start].total) / hz
            found.append(spent / (sample.at - samples[start].at) * 100)
            start = index
    return found


def mean_pct(samples: Sequence[Sample], hz: int | None = None) -> float:
    hz = clock_ticks() if hz is None else hz
    return (samples[-1].total - samples[0].total) / hz / (samples[-1].at - samples[0].at) * 100


def threads_pct(samples: Sequence[Sample], pid: int, hz: int | None = None) -> list[tuple[str, float]]:
    """Each thread's share over the measure, in % of one core: the event
    loop's, the process's first thread, then the others, the busiest first,
    which the engines' blocks and the sources take turns in (Python names
    none of them for the system)."""
    hz = clock_ticks() if hz is None else hz
    first, last = samples[0], samples[-1]
    seconds = last.at - first.at
    shares = []
    for tid, (_, ticks) in last.threads.items():
        before = first.threads.get(tid, ("", 0))[1]
        shares.append((tid, (ticks - before) / hz / seconds * 100))
    shares.sort(key=lambda share: (share[0] != pid, -share[1]))
    return [("event loop (main thread)" if tid == pid else f"worker thread {number}", share)
            for number, (tid, share) in enumerate(shares)]


# --------------------------------------------------------------- the measure

@dataclass
class Result:
    radios: int
    seconds: float
    samples: list[Sample]
    pid: int
    listeners: list[Listener]
    receivers: list[Receiver]
    warnings: list[str]
    load: tuple[str, str]

    @property
    def windows(self) -> list[float]:
        return windows_pct(self.samples)

    @property
    def mean(self) -> float:
        return mean_pct(self.samples)

    @property
    def dry(self) -> int:
        return sum(listener.playback.dry for listener in self.listeners)

    @property
    def passed(self) -> bool:
        windows = self.windows or [self.mean]
        return self.dry == 0 and max(windows) < LIMIT_PCT


async def measure(radios: int, seconds: float, warmup_s: float, seed: int,
                  progress: Callable[[str], None] = lambda line: None) -> Result:
    """Start the receivers, Radiolack and the listeners, measure, and stop
    them all, whatever ends the measure."""
    scene = scene_bytes(seed)
    receivers = [Receiver(scene) for _ in range(radios)]
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Future] = []
    warnings: list[str] = []
    first_load = load()
    with tempfile.TemporaryDirectory(prefix="radiolack-radios-") as folder:
        try:
            for receiver in receivers:
                await receiver.start()
            environment = {**os.environ, "XDG_CONFIG_HOME": str(Path(folder) / "config"),
                           "XDG_DATA_HOME": str(Path(folder) / "data"),
                           "PYTHONPATH": os.pathsep.join(filter(None, (str(REPOSITORY / "src"),
                                                                       os.environ.get("PYTHONPATH"))))}
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--serve",
                ",".join(str(receiver.port) for receiver in receivers),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=environment)
            assert process.stdout is not None and process.stderr is not None
            tasks.append(asyncio.ensure_future(_collect(process.stderr, warnings)))
            line = await asyncio.wait_for(process.stdout.readline(), START_TIMEOUT_S)
            if not line:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
                raise RuntimeError("Radiolack's process ended at start: " + " | ".join(warnings[-5:]))
            ports = json.loads(line)
            listeners = [Listener(port) for port in ports]
            readies = [asyncio.Event() for _ in listeners]
            tasks += [asyncio.ensure_future(listen(listener, ready)) for listener, ready in zip(listeners, readies)]
            await asyncio.wait_for(asyncio.gather(*(ready.wait() for ready in readies)), START_TIMEOUT_S)
            progress(f"{radios} radios on Radiolack's process {process.pid}, warming up for {warmup_s:g} s")
            await _watch(tasks, warmup_s)
            loop = asyncio.get_running_loop()
            for listener in listeners:
                listener.playback.counted_from(loop.time())
                listener.spectra = 0
            for receiver in receivers:
                receiver.dropped_s = 0.0
            samples = [Sample(time.monotonic(), *read_cpu(process.pid))]
            started = samples[0].at
            while (elapsed := time.monotonic() - started) < seconds:
                await _watch(tasks, min(1.0, seconds - elapsed))
                samples.append(Sample(time.monotonic(), *read_cpu(process.pid)))
                if len(samples) % 60 == 1:
                    progress(f"  {elapsed:.0f} s of {seconds:g}, {mean_pct(samples):.1f}% of one core so far")
            for listener in listeners:
                listener.playback.advance(loop.time())
            return Result(radios, samples[-1].at - started, samples, process.pid, listeners, receivers,
                          warnings, (first_load, load()))
        finally:
            for task in tasks[1:]:
                task.cancel()
            await asyncio.gather(*tasks[1:], return_exceptions=True)
            if process is not None and process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if tasks:
                await asyncio.gather(tasks[0], return_exceptions=True)
            for receiver in receivers:
                await receiver.close()


async def _collect(stream: asyncio.StreamReader, lines: list[str]) -> None:
    while line := await stream.readline():
        lines.append(line.decode(errors="replace").rstrip())


async def _watch(tasks: Sequence[asyncio.Future], seconds: float) -> None:
    """Wait ``seconds``; raise if a listener ended meanwhile."""
    listeners = tasks[1:]
    done, _ = await asyncio.wait(listeners, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        error = task.exception()
        raise RuntimeError(f"a listener ended: {error!r}" if error else "a listener's stream ended")


# --------------------------------------------------------------- the report

def processor() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def load() -> str:
    try:
        return " ".join(f"{value:.2f}" for value in os.getloadavg())
    except OSError:
        return "unknown"


def governor() -> str:
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        return "unknown"


def report(result: Result, options: argparse.Namespace, started: datetime) -> list[str]:
    import aiohttp
    windows = result.windows
    lines = [
        f"# Several radios in one Radiolack: {result.radios} radios (spike 9)",
        "",
        f"- Measured on {started:%Y-%m-%d %H:%M} UTC on {platform.node() or 'this computer'}:"
        f" {result.seconds:.0f} s after {options.warmup_s:g} s not counted, seed {options.seed}.",
        f"- Processor: {processor()}, {os.cpu_count()} logical processors, governor {governor()};"
        f" load average {result.load[0]} at start, {result.load[1]} at the end.",
        f"- Python {platform.python_version()}, NumPy {np.__version__}, aiohttp {aiohttp.__version__}.",
        f"- Each radio: an rtl_tcp source at {RATE_HZ / 1e6:g} MS/s, its VFO in {MODE} on the scene's station,"
        " detection on, and one listener taking its spectrum frames and playing its audio.",
        "",
        "| Radiolack's processor time | % of one core |",
        "| --- | --- |",
        f"| Mean over the measure | {result.mean:.1f} |",
    ]
    if windows:
        lines += [f"| {WINDOW_S} s windows: median | {statistics.median(windows):.1f} |",
                  f"| {WINDOW_S} s windows: lowest to highest | {min(windows):.1f} to {max(windows):.1f} |"]
    lines += ["", "Its threads over the measure, in % of one core:", "",
              "| Thread | % of one core |", "| --- | --- |"]
    for name, share in threads_pct(result.samples, result.pid):
        if share >= 0.05:
            lines.append(f"| {name} | {share:.1f} |")
    lines += ["", "| Radio | Audio frames | Ran dry | Audio dropped (s) | Least buffered (ms) |"
              " Longest between frames (ms) | Spectrum frames | Receiver behind (s) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for number, (listener, receiver) in enumerate(zip(result.listeners, result.receivers), 1):
        playback = listener.playback
        lowest = "never played" if playback.lowest_s is None else f"{playback.lowest_s * 1e3:.0f}"
        lines.append(f"| {number} | {playback.frames} | {playback.dry} | {playback.missing_s:.2f} | {lowest}"
                     f" | {playback.longest_gap_s * 1e3:.0f} | {listener.spectra} | {receiver.dropped_s:.2f} |")
    errors = [error for listener in result.listeners for error in listener.errors]
    lines += ["", f"Radiolack's warnings and errors: {len(result.warnings)}."
              + "".join(f"\n- {line}" for line in result.warnings[:10]),
              f"Commands refused: {len(errors)}." + "".join(f"\n- {error}" for error in errors[:10]), "",
              f"Rule: no listener's audio runs dry and every {WINDOW_S} s window stays under {LIMIT_PCT:g}% of"
              f" one core (60% of two cores): {'passed' if result.passed else 'failed'}."]
    return lines


def positive(kind: type) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        value = kind(text)
        if not value > 0:
            raise argparse.ArgumentTypeError(f"must be positive, not {text}")
        return value
    return parse


def radio_count(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_RADIOS:
        raise argparse.ArgumentTypeError(f"must be 1 to {MAX_RADIOS}, not {text}")
    return value


def ports(text: str) -> list[int]:
    return [int(part) for part in text.split(",")]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure several radios in one Radiolack: engines in one process, each reading a stand-in"
                    " rtl_tcp at 2.4 MS/s on 127.0.0.1, each with a listener playing its audio; Radiolack's"
                    " processor time and whether the audio runs dry. No receiver, no network. Linux only.")
    parser.add_argument("--radios", type=radio_count, default=2, help=f"radios, 1 to {MAX_RADIOS} (default 2)")
    parser.add_argument("--seconds", type=positive(float), default=600.0,
                        help="seconds measured (default 600, ten minutes)")
    parser.add_argument("--warmup-s", type=positive(float), default=5.0,
                        help="seconds run first, not counted (default 5)")
    parser.add_argument("--seed", type=int, default=1, help="the scene's seed (default 1)")
    parser.add_argument("--output", type=Path, help="also write the report to this file")
    parser.add_argument("--serve", type=ports, help=argparse.SUPPRESS)  # the tool's own Radiolack process
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    options = build_parser().parse_args(argv)
    if options.serve is not None:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        asyncio.run(serve(options.serve, out))
        return EXIT_DONE
    started = datetime.now(timezone.utc)
    try:
        result = asyncio.run(measure(options.radios, options.seconds, options.warmup_s, options.seed,
                                     lambda line: print(line, file=err, flush=True)))
    except KeyboardInterrupt:
        print("Interrupted: no report.", file=err)
        return EXIT_INTERRUPTED
    except (OSError, RuntimeError, asyncio.TimeoutError, ValueError) as error:
        print(f"The measure failed: {error}", file=err)
        return EXIT_FAILED
    text = "\n".join(report(result, options, started)) + "\n"
    out.write(text)
    if options.output is not None:
        try:
            options.output.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"Cannot write {options.output}: {error}", file=err)
            return EXIT_FAILED
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())
