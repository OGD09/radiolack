#!/usr/bin/env python3
"""Measure which sample rates the engine holds: later-milestones.md,
section 1, the first pull request's first spike, and spike 7 of
scoping.md.

The tool feeds the engine's own block processing (``engine._process_block``:
DC removal, display spectrum, detection, the VFO's channel, its meter and
its audio) with synthetic samples at each rate, in the block the engine
takes at that rate (``Engine.run``: the rate over the frame rate, 30 blocks
a second, at least one FFT), the VFO in NFM with its 10 kHz filter, audio
and detection on, as an interface listening would have them. It also times
what bringing those samples in costs a SoapySDR source, which the rule's
50% of one core includes: the samples of a block in the device's native
format, CS8, CS16 or CF32, copied out in reads of ``READ_SAMPLES`` as
``readStream`` hands them, kept in a ``NativeBuffer`` that drops the oldest
for a reader that falls behind, as the rtl_tcp client does, then taken by
the engine and converted to complex64 (``to_complex``).

Two measures, which tell different things.

Back to back, the rule's measure: every rate and every format processes its
blocks one after the other, in an order that turns with every block, so
that other work on the computer weighs on them alike; each block's
processor time is that of this thread (``time.thread_time_ns``), not the
wall clock's. A run does this over ``--seconds`` of signal after
``--warmup-s`` that are not timed. It gives the work to do, the processor
kept busy, and the rule fixed beforehand decides on it: a rate is offered
when the engine and the intake together stay under 50% of one core; above
3.2 MS/s, the rtl_tcp client's highest rate, a rate that does not is not
offered.

Paced: then each rate in turn processes its blocks one every period, the
block over the rate (1/30 s), as a receiver delivers them, for the same
seconds after the same warm-up, the engine alone (the intake is a few
percent of it). The blocks are one second of the scene made beforehand and
processed again in turn, so that nothing but the processing runs between
them; the scene repeats every second, its tone and carriers on whole
hertz. A processor that slows down between blocks (a governor such as
intel_pstate's powersave) takes longer over each, so the processor time
per second of signal is higher than back to back: it is what a system
monitor shows while Radiolack runs. Each block's time on the wall clock,
against its period, says whether a block fits its period on this
computer; one that takes longer delays the next, which the buffer absorbs
while the mean stays under the period. This measure does not decide.

The tool makes ``--runs`` runs, each with fresh state, back to back then
paced, and reports for each rate the median of the processor time per
second of signal, as a percentage of one core, with the lowest and the
highest, the paced blocks' mean and longest time and how many ran over
their period, the load average after each measure, and the rule.

The signal: white noise at -40 dBFS, an NFM station at -30 dBFS 300 kHz
above the centre frequency, the VFO on it, its 1 kHz tone at 2.5 kHz of
deviation, and three steady carriers within 1.2 MHz of the centre, for
detection: the same scene at every rate.

It needs no receiver and no network, and runs from a checkout of the
repository; the Debian package does not install it.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import numpy as np

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
from radiolack.demod import MODE_BANDWIDTHS_HZ, DemodSettings, Demodulator  # noqa: E402
from radiolack.engine import FRAME_RATE, _process_block  # noqa: E402
from radiolack.rtltcp import BUFFER_S, SAMPLE_RATES_HZ  # noqa: E402
from radiolack.scanners import DEFAULT_THRESHOLD_DB, DETECTION_BLOCKS, ChannelWatch, Detector  # noqa: E402
from radiolack.spectrum import FFT_SIZE, FFTS_PER_FRAME, SpectrumAnalyzer  # noqa: E402

RATES_HZ = (2_400_000.0, 3_200_000.0, 4_000_000.0, 5_000_000.0, 6_000_000.0, 8_000_000.0, 10_000_000.0)
REFERENCE_HZ = 2_400_000.0      # the rtl_tcp client's usual rate
ALWAYS_HZ = SAMPLE_RATES_HZ[-1][1]      # 3.2 MS/s: offered whatever the measure
LIMIT_PCT = 50.0                # of one core, for the engine and the intake together
CENTRE_HZ = 145_000_000.0
VFO_OFFSET_HZ = 300_000.0
MODE = "NFM"
READ_SAMPLES = 16_384           # samples a read hands over, about a device's MTU or less

# The scene.
NOISE_DBFS = -40.0
STATION_DBFS = -30.0
TONE_HZ = 1_000.0
DEVIATION_HZ = 2_500.0
CARRIERS = ((-800_000.0, -45.0), (-450_000.0, -55.0), (900_000.0, -50.0))     # offset (Hz), dBFS

EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130


@dataclass(frozen=True)
class Format:
    """A device's sample format, as SoapySDR names it."""

    name: str
    component: type[np.generic]     # the type of each of I and Q
    full_scale: float               # the value of a full-scale component

    @property
    def sample_bytes(self) -> int:
        return 2 * np.dtype(self.component).itemsize


FORMATS = {kind.name: kind for kind in (
    Format("CS8", np.int8, 128.0), Format("CS16", np.int16, 32768.0), Format("CF32", np.float32, 1.0))}


def to_complex(data: bytes | bytearray | memoryview, sample_format: Format, full_scale: float | None = None) -> np.ndarray:
    """Samples in ``format``, I then Q, as complex64 scaled to full scale 1."""
    components = np.frombuffer(data, sample_format.component)
    if sample_format.component is np.float32:
        samples = components.copy()
    else:
        samples = components.astype(np.float32)
    scale = sample_format.full_scale if full_scale is None else full_scale
    if scale != 1.0:
        samples *= np.float32(1 / scale)
    return samples.view(np.complex64)


def to_native(samples: np.ndarray, sample_format: Format) -> np.ndarray:
    """complex64 samples as a device in ``format`` would give them: I then
    Q, scaled and rounded to its integers, clipped at full scale."""
    components = samples.view(np.float32)
    if sample_format.component is np.float32:
        return components.copy()
    info = np.iinfo(sample_format.component)
    scaled = np.rint(components * sample_format.full_scale)
    return np.clip(scaled, info.min, info.max).astype(sample_format.component)


class NativeBuffer:
    """The samples a reading thread brings in for the engine, kept in the
    device's format as bytes: at most ``capacity`` samples, or what the
    engine asks at once if more; past that the oldest are dropped, and
    counted, so that the engine stays current when it falls behind, as the
    rtl_tcp client's buffer does (rtltcp.py)."""

    def __init__(self, sample_bytes: int, capacity: int) -> None:
        if sample_bytes < 1 or capacity < 1:
            raise ValueError("a buffer holds at least one sample of at least one byte")
        self.sample_bytes = sample_bytes
        self.capacity = capacity
        self.dropped = 0            # samples dropped, the oldest
        self._data = bytearray()
        self._wanted = 0            # samples the engine waits for
        self._ready = threading.Condition()

    def __len__(self) -> int:
        with self._ready:
            return len(self._data) // self.sample_bytes

    def put(self, data: bytes | bytearray | memoryview) -> None:
        """Keep whole samples: ``data`` holds a whole number of them."""
        if len(data) % self.sample_bytes:
            raise ValueError(f"{len(data)} bytes are not whole samples of {self.sample_bytes} bytes")
        with self._ready:
            self._data += data
            limit = max(self.capacity, self._wanted) * self.sample_bytes
            excess = len(self._data) - limit
            if excess > 0:
                del self._data[:excess]
                self.dropped += excess // self.sample_bytes
            if len(self._data) >= self._wanted * self.sample_bytes:
                self._ready.notify_all()

    def take(self, count: int, timeout_s: float) -> bytes | None:
        """The oldest ``count`` samples, or None when they did not come
        within ``timeout_s``."""
        if count < 1:
            raise ValueError(f"cannot take {count} samples")
        size = count * self.sample_bytes
        with self._ready:
            self._wanted = count
            try:
                if not self._ready.wait_for(lambda: len(self._data) >= size, timeout_s):
                    return None
            finally:
                self._wanted = 0
            data = bytes(self._data[:size])
            del self._data[:size]
            return data

    def clear(self) -> None:
        with self._ready:
            self._data.clear()


def block_size(rate_hz: float) -> int:
    """The engine's block at this rate (engine.Engine.run)."""
    return max(FFT_SIZE, round(rate_hz / FRAME_RATE))


class Scene:
    """The synthetic samples at one rate, computed from the sample's index,
    so that any block can be made on its own: deterministic for a seed."""

    def __init__(self, seed: int, rate_hz: float) -> None:
        self.seed = seed
        self.rate_hz = rate_hz

    def block(self, start: int, count: int) -> np.ndarray:
        t = (start + np.arange(count)) / self.rate_hz
        rng = np.random.default_rng((self.seed, round(self.rate_hz), start))
        noise_rms = np.float32(10 ** (NOISE_DBFS / 20) / math.sqrt(2))
        samples = rng.standard_normal(2 * count, dtype=np.float32).view(np.complex64) * noise_rms
        phase = (2 * np.pi * VFO_OFFSET_HZ * t
                 + DEVIATION_HZ / TONE_HZ * np.sin(2 * np.pi * TONE_HZ * t))
        signal = 10 ** (STATION_DBFS / 20) * np.exp(1j * phase)
        for offset_hz, level_dbfs in CARRIERS:
            signal += 10 ** (level_dbfs / 20) * np.exp(2j * np.pi * offset_hz * t)
        samples += signal.astype(np.complex64)
        return samples


class Pipeline:
    """The engine's objects for one receiver at one rate."""

    def __init__(self, rate_hz: float) -> None:
        self.rate_hz = rate_hz
        self.analyzer = SpectrumAnalyzer()
        self.demodulator = Demodulator()
        self.detector = Detector()
        self.watch = ChannelWatch()
        self.settings = DemodSettings(CENTRE_HZ + VFO_OFFSET_HZ, MODE, MODE_BANDWIDTHS_HZ[MODE])
        self.blocks = 0
        self.opened = 0             # blocks after which the squelch was open
        self.searches = 0           # detection's searches done

    def process(self, samples: np.ndarray) -> int:
        """Process one block as the engine does; the processor time it
        took, in ns."""
        started = time.thread_time_ns()
        result = _process_block(self.analyzer, self.demodulator, self.detector, self.watch, samples, CENTRE_HZ,
                                self.rate_hz, True, self.settings, True, DEFAULT_THRESHOLD_DB, DETECTION_BLOCKS, None)
        spent = time.thread_time_ns() - started
        meter, detections = result[2], result[4]
        self.blocks += 1
        self.opened += bool(meter.open)
        self.searches += detections is not None
        return spent


class Intake:
    """A SoapySDR source's intake at one rate in one format: the reads
    copied into the buffer, the engine's block taken and converted."""

    def __init__(self, rate_hz: float, sample_format: Format) -> None:
        self.format = sample_format
        self.block = block_size(rate_hz)
        self.buffer = NativeBuffer(sample_format.sample_bytes, round(BUFFER_S * rate_hz))
        self._read = np.empty(2 * READ_SAMPLES, sample_format.component)     # what readStream fills

    def process(self, native: np.ndarray) -> int:
        """Bring in one block of ``native`` samples, I then Q; the
        processor time it took, in ns."""
        read = self._read
        started = time.thread_time_ns()
        for start in range(0, len(native), len(read)):
            part = native[start:start + len(read)]
            read[:len(part)] = part
            self.buffer.put(memoryview(read)[:len(part)].cast("B"))
        data = self.buffer.take(self.block, 0.0)
        assert data is not None, "a whole block was put"
        to_complex(data, self.format)
        return time.thread_time_ns() - started


@dataclass
class Run:
    seconds: float
    engine_ns: dict[float, int]
    intake_ns: dict[tuple[float, str], int]
    load: str                       # the load average at the run's end

    def engine(self, rate_hz: float) -> float:
        return self.engine_ns[rate_hz] / 1e9 / self.seconds * 100

    def intake(self, rate_hz: float, name: str) -> float:
        return self.intake_ns[rate_hz, name] / 1e9 / self.seconds * 100

    def total(self, rate_hz: float, name: str) -> float:
        return self.engine(rate_hz) + self.intake(rate_hz, name)


@dataclass
class Activity:
    blocks: int = 0
    opened: int = 0
    searches: int = 0


def measure(rates_hz: Sequence[float], seconds: float, warmup_s: float, seed: int,
            progress: Callable[[str], None] = lambda line: None) -> tuple[Run, dict[float, Activity]]:
    """One run: every rate and format over the same span of signal."""
    warmup = math.ceil(warmup_s * FRAME_RATE)
    timed = max(1, round(seconds * FRAME_RATE))
    scenes = {rate: Scene(seed, rate) for rate in rates_hz}
    pipelines = {rate: Pipeline(rate) for rate in rates_hz}
    intakes = {(rate, name): Intake(rate, sample_format) for rate in rates_hz for name, sample_format in FORMATS.items()}
    engine = dict.fromkeys(rates_hz, 0)
    intake = dict.fromkeys(intakes, 0)
    order = list(rates_hz)
    for index in range(warmup + timed):
        turn = index % len(order)
        for rate in order[turn:] + order[:turn]:
            size = block_size(rate)
            samples = scenes[rate].block(index * size, size)
            spent = pipelines[rate].process(samples)
            if index >= warmup:
                engine[rate] += spent
            names = list(FORMATS)
            spin = index % len(names)
            for name in names[spin:] + names[:spin]:
                native = to_native(samples, FORMATS[name])
                spent = intakes[rate, name].process(native)
                if index >= warmup:
                    intake[rate, name] += spent
        if (index + 1) % round(FRAME_RATE) == 0:
            progress(f"  {(index + 1) / FRAME_RATE:.0f} s of signal")
    activity = {rate: Activity(pipeline.blocks, pipeline.opened, pipeline.searches)
                for rate, pipeline in pipelines.items()}
    return Run(timed / FRAME_RATE, engine, intake, load()), activity


@dataclass
class Paced:
    """One rate's blocks processed at a receiver's pace."""

    period_s: float                 # a block's time at the rate: the block over the rate
    cpu_ns: int = 0                 # the processing thread's processor time
    took_s: list[float] = field(default_factory=list)      # each block's time on the wall clock

    def add(self, cpu_ns: int, took_s: float) -> None:
        self.cpu_ns += cpu_ns
        self.took_s.append(took_s)

    @property
    def blocks(self) -> int:
        return len(self.took_s)

    @property
    def percent(self) -> float:
        """The processor time per second of signal, in % of one core."""
        return self.cpu_ns / 1e9 / max(self.blocks * self.period_s, 1e-9) * 100

    @property
    def over(self) -> int:
        """The blocks that took longer than their period."""
        return sum(took > self.period_s for took in self.took_s)


@dataclass
class PacedRun:
    rates: dict[float, Paced]
    load: str                       # the load average at the paced measure's end


def measure_paced(rates_hz: Sequence[float], seconds: float, warmup_s: float, seed: int,
                  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                  pipeline: Callable[[float], Pipeline] = Pipeline,
                  progress: Callable[[str], None] = lambda line: None) -> PacedRun:
    """Each rate in turn, its blocks processed one every period, as a
    receiver delivers them: block ``index`` is processed once the receiver
    has delivered it, a period after the one before, or at once when the
    processing is late. The blocks are about one second of the scene, made
    beforehand and processed again in turn."""
    warmup = math.ceil(warmup_s * FRAME_RATE)
    timed = max(1, round(seconds * FRAME_RATE))
    results: dict[float, Paced] = {}
    for rate in rates_hz:
        size = block_size(rate)
        scene = Scene(seed, rate)
        blocks = [scene.block(index * size, size) for index in range(round(FRAME_RATE))]
        processing = pipeline(rate)
        paced = Paced(size / rate)
        started = clock()
        for index in range(warmup + timed):
            wait = started + (index + 1) * paced.period_s - clock()
            if wait > 0:
                sleep(wait)
            began = clock()
            spent = processing.process(blocks[index % len(blocks)])
            took = clock() - began
            if index >= warmup:
                paced.add(spent, took)
        results[rate] = paced
        progress(f"  {megas(rate)} MS/s paced")
    return PacedRun(results, load())


def offered(rate_hz: float, totals: Sequence[float]) -> bool:
    """The rule: at or below 3.2 MS/s always; above, when the median of the
    engine and the intake together stays under 50% of one core."""
    return rate_hz <= ALWAYS_HZ or statistics.median(totals) < LIMIT_PCT


def processor() -> str:
    """The processor's model, as the system names it."""
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


def spread(values: Sequence[float]) -> str:
    return f"{statistics.median(values):.1f} ({min(values):.1f} to {max(values):.1f})"


def megas(rate_hz: float) -> str:
    return f"{rate_hz / 1e6:g}"


def report(runs: Sequence[Run], paced_runs: Sequence[PacedRun], activity: dict[float, Activity],
           options: argparse.Namespace, started: datetime, first_load: str) -> list[str]:
    """The tables and what they were measured on, in Markdown."""
    rates = options.rates
    names = list(FORMATS)
    lines = [
        "# Sample rates the engine holds (spike 7)",
        "",
        f"- Measured on {started:%Y-%m-%d %H:%M} UTC, {len(runs)} runs of {runs[0].seconds:g} s of signal at each"
        f" rate, after {options.warmup_s:g} s not timed, seed {options.seed}; back to back, then paced.",
        f"- Processor: {processor()}, {os.cpu_count()} logical processors; load average {first_load} at start,"
        f" {paced_runs[-1].load} at the end.",
        f"- Python {platform.python_version()}, NumPy {np.__version__}.",
        f"- The engine's blocks: the rate over {FRAME_RATE:g}, its display FFTs {FFTS_PER_FRAME} of {FFT_SIZE}"
        f" samples a block; the VFO in {MODE}, {MODE_BANDWIDTHS_HZ[MODE] / 1e3:g} kHz, audio and detection on.",
        f"- The intake: reads of {READ_SAMPLES} samples copied into a buffer of {BUFFER_S:g} s that drops the"
        " oldest, the engine's block taken and converted to complex64.",
        "- Processor time of the measuring thread per second of signal, in % of one core: the median of the"
        " runs, then the lowest and the highest.",
        "",
        "Back to back, the rule's measure: the blocks processed one after the other, the processor kept busy."
        " It gives the work to do, and the rule decides on it.",
        "",
        "| MS/s | Block (samples) | Display bin (Hz) | Engine | " + " | ".join(f"Intake {name}" for name in names)
        + " | " + " | ".join(f"Total {name}" for name in names) + " | Rule |",
        "| --- | --- | --- | --- | " + " | ".join("---" for _ in names * 2) + " | --- |",
    ]
    for rate in rates:
        totals = {name: [run.total(rate, name) for run in runs] for name in names}
        allowed = [name for name in names if offered(rate, totals[name])]
        if rate <= ALWAYS_HZ:
            rule = "offered (up to 3.2 MS/s)"
        elif allowed:
            rule = "offered" + ("" if len(allowed) == len(names) else " in " + ", ".join(allowed))
        else:
            rule = "not offered"
        lines.append(f"| {megas(rate)} | {block_size(rate)} | {rate / FFT_SIZE:.0f} |"
                     f" {spread([run.engine(rate) for run in runs])} | "
                     + " | ".join(spread([run.intake(rate, name) for run in runs]) for name in names) + " | "
                     + " | ".join(spread(totals[name]) for name in names) + f" | {rule} |")
    lines += [
        "",
        "What the engine did, over every run: the squelch open after "
        + ", ".join(f"{activity[rate].opened} of {activity[rate].blocks} blocks at {megas(rate)} MS/s"
                    for rate in rates) + "; detection searched "
        + ", ".join(f"{activity[rate].searches} times at {megas(rate)}" for rate in rates) + ".",
        "",
        "Each run, the engine's % of one core at each rate back to back, then the load average at that"
        " measure's end:",
        "",
        "| Run | " + " | ".join(f"{megas(rate)} MS/s" for rate in rates) + " | Load average |",
        "| --- | " + " | ".join("---" for _ in rates) + " | --- |",
    ]
    for number, run in enumerate(runs, 1):
        lines.append(f"| {number} | " + " | ".join(f"{run.engine(rate):.1f}" for rate in rates)
                     + f" | {run.load} |")
    lines += [
        "",
        "Paced: each rate's blocks processed one every period, as a receiver delivers them, the engine alone."
        " The processor time per second of signal is what a system monitor shows while Radiolack runs, higher"
        " than back to back where the processor slows down between blocks; each block's time on the wall clock"
        " says whether a block fits its period on this computer, a block over it delaying the next, which the"
        " buffer absorbs while the mean stays under it. This measure does not decide. The mean block is that of"
        " every run's blocks; the longest and the blocks over the period are over every run.",
        "",
        "| MS/s | Period (ms) | Engine, back to back | Engine, paced | Mean block (ms) | Longest block (ms)"
        " | Blocks over the period |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rate in rates:
        paced = [run.rates[rate] for run in paced_runs]
        took = [value for one in paced for value in one.took_s]
        lines.append(f"| {megas(rate)} | {paced[0].period_s * 1e3:.1f} | {spread([run.engine(rate) for run in runs])}"
                     f" | {spread([one.percent for one in paced])} | {statistics.fmean(took) * 1e3:.1f}"
                     f" | {max(took) * 1e3:.1f} | {sum(one.over for one in paced)} of {len(took)} |")
    lines += [
        "",
        "Each run, the engine's paced % of one core at each rate, then the load average at the paced measure's end:",
        "",
        "| Run | " + " | ".join(f"{megas(rate)} MS/s" for rate in rates) + " | Load average |",
        "| --- | " + " | ".join("---" for _ in rates) + " | --- |",
    ]
    for number, run in enumerate(paced_runs, 1):
        lines.append(f"| {number} | " + " | ".join(f"{run.rates[rate].percent:.1f}" for rate in rates)
                     + f" | {run.load} |")
    return lines


def positive(kind: type) -> Callable[[str], object]:
    def parse(text: str) -> object:
        value = kind(text)
        if not value > 0:
            raise argparse.ArgumentTypeError(f"must be positive, not {text}")
        return value
    return parse


def rate_list(text: str) -> tuple[float, ...]:
    """Rates in MS/s, comma-separated, from 0.25 to 20."""
    try:
        rates = tuple(sorted({float(part) * 1e6 for part in text.split(",") if part.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not rates in MS/s: {text}") from None
    if not rates or not all(250_000.0 <= rate <= 20_000_000.0 for rate in rates):
        raise argparse.ArgumentTypeError(f"rates from 0.25 to 20 MS/s, not {text}")
    return rates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the processor time of the engine's processing of one receiver, one NFM channel"
                    " with audio and detection, and of bringing a SoapySDR device's samples in, at sample rates"
                    " from 2.4 to 10 MS/s, on synthetic samples: back to back, the work to do, on which the rule"
                    " decides, then paced, one block every 1/30 s as a receiver delivers them, what a system"
                    " monitor shows and whether a block fits its period. No receiver, no network.")
    parser.add_argument("--runs", type=positive(int), default=5, help="runs, each with fresh state (default 5)")
    parser.add_argument("--seconds", type=positive(float), default=5.0,
                        help="seconds of signal timed at each rate in each run and each measure (default 5)")
    parser.add_argument("--warmup-s", type=positive(float), default=1.0,
                        help="seconds of signal processed first in each run, not timed (default 1)")
    parser.add_argument("--rates", type=rate_list, default=RATES_HZ,
                        help="sample rates in MS/s, comma-separated (default 2.4,3.2,4,5,6,8,10)")
    parser.add_argument("--seed", type=int, default=1, help="the synthetic signal's seed (default 1)")
    parser.add_argument("--output", type=Path, help="also write the tables to this file")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run the measures; ``clock`` and ``sleep`` pace the paced one."""
    options = build_parser().parse_args(argv)
    started = datetime.now(timezone.utc)
    first_load = load()
    runs: list[Run] = []
    paced_runs: list[PacedRun] = []
    activity: dict[float, Activity] = {rate: Activity() for rate in options.rates}

    def progress(line: str) -> None:
        print(line, file=err, flush=True)

    try:
        for number in range(1, options.runs + 1):
            progress(f"Run {number} of {options.runs}, back to back")
            run, done = measure(options.rates, options.seconds, options.warmup_s, options.seed, progress)
            runs.append(run)
            for rate, counts in done.items():
                total = activity[rate]
                total.blocks += counts.blocks
                total.opened += counts.opened
                total.searches += counts.searches
            progress(f"Run {number} of {options.runs}, paced")
            paced_runs.append(measure_paced(options.rates, options.seconds, options.warmup_s, options.seed,
                                            clock, sleep, progress=progress))
    except KeyboardInterrupt:
        print("Interrupted: no table.", file=err)
        return EXIT_INTERRUPTED
    text = "\n".join(report(runs, paced_runs, activity, options, started, first_load)) + "\n"
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
