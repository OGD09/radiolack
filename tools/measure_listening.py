#!/usr/bin/env python3
"""Measure what each listening tool adds to the engine's processing of one
receiver at 2.4 MS/s: later-milestones.md, section 2, pull request 1, and
spike 8 of scoping.md.

The tools are synchronous AM, the package's SAM mode (demod.py) since its
pull request, and the prototypes of `tools/listening.py`: the automatic
notch, the noise blanker and the fine spectrum. The tool feeds
the engine's own block processing (``engine._process_block``: DC removal,
display spectrum, detection, the VFO's channel, its meter and its audio)
with synthetic samples, in the engine's block size, 1/30 s of samples,
and times it in six configurations: without any tool (the baseline), with
each tool alone, and with the four together. All six process the same
blocks, one after the other, in an order that turns with every block, so
that other work on the computer weighs on them alike; each block's
processor time is that of this thread (``time.thread_time_ns``), not the
wall clock's. A run does this over ``--seconds`` of signal after
``--warmup-s`` that are not timed, in which SAM locks and the notch finds
its whistle; the tool makes ``--runs`` runs, each with fresh state, and
reports for each tool the median of what it added per second of signal,
as a percentage of one core, with the lowest and the highest, against the
rule fixed beforehand: 1.5% for each, 6% together.

The tools sit where the design puts them, SAM as the VFO's mode, the
prototypes through subclasses in this file, so that the engine's code
stays as it is: the blanker on the samples that the demodulator receives,
after the display spectrum has read them; SAM in place of the AM detector
in the audio path, with the same delay, and its search for the carrier on
the channel; the notch reading the detector's output and changing the
audio filter's taps;
the fine spectrum reading the front filter's output, the channel at 48 kHz
before the channel filter, whose cover the design sets at 19.2 kHz on each
side of the VFO.

The signal: white noise at -40 dBFS, an AM station 300 kHz from the centre
frequency, 150 Hz off the VFO, modulated at 50% by a voice-like sound,
with a carrier that fades alone (selective fading) and a whole signal that
fades too, a steady carrier 1 kHz above the station's, which whistles in
its audio, and impulses of three samples every 10 ms at -6 dBFS. The VFO
is in AM with its 8 kHz filter, or in SAM with the same filter for
synchronous AM, the audio on and detection on, as an interface listening
would have them.

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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import numpy as np

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import listening  # noqa: E402
from radiolack.demod import MODE_BANDWIDTHS_HZ, DemodSettings, Demodulator  # noqa: E402
from radiolack.engine import FRAME_RATE, _process_block  # noqa: E402
from radiolack.scanners import DEFAULT_THRESHOLD_DB, DETECTION_BLOCKS, ChannelWatch, Detector  # noqa: E402
from radiolack.spectrum import SpectrumAnalyzer  # noqa: E402

RATE_HZ = 2_400_000.0
CENTRE_HZ = 9_500_000.0
VFO_OFFSET_HZ = 300_000.0
MODE = "AM"
SAM_MODE = "SAM"        # synchronous AM, the package's

TOOLS = ("sam", "notch", "blanker", "fine")
NAMES = {"sam": "Synchronous AM", "notch": "Automatic notch", "blanker": "Noise blanker",
         "fine": "Fine spectrum", "all": "The four together"}
BASELINE = "baseline"
ALL = "all"
CONFIGURATIONS: dict[str, frozenset[str]] = {
    BASELINE: frozenset(), **{tool: frozenset((tool,)) for tool in TOOLS}, ALL: frozenset(TOOLS)}
SHARE_PCT = 1.5         # each tool's share of one core, set by the planning session
TOGETHER_PCT = 6.0      # the four together, later-milestones.md, section 2

# The scene.
NOISE_DBFS = -40.0
CARRIER_DBFS = -30.0
CARRIER_OFFSET_HZ = 150.0       # the station's carrier from the VFO
MODULATION = 0.5
WHISTLE_HZ = 1_000.0            # the steady carrier, from the station's
WHISTLE_DB = -20.0              # from the station's carrier
IMPULSE_PERIOD_S = 0.01
IMPULSE_SAMPLES = 3
IMPULSE_DBFS = -6.0
EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130


class Scene:
    """The synthetic samples, computed from the sample's index, so that
    any block can be made on its own: deterministic for a seed."""

    def __init__(self, seed: int, rate_hz: float = RATE_HZ) -> None:
        self.seed = seed
        self.rate_hz = rate_hz

    def block(self, start: int, count: int) -> np.ndarray:
        t = (start + np.arange(count)) / self.rate_hz
        noise_rms = 10 ** (NOISE_DBFS / 20) / math.sqrt(2)
        rng = np.random.default_rng((self.seed, start))
        samples = rng.normal(0.0, noise_rms, count) + 1j * rng.normal(0.0, noise_rms, count)
        carrier = 10 ** (CARRIER_DBFS / 20)
        flat = 0.55 + 0.45 * np.cos(2 * np.pi * 0.2 * t)            # the whole signal, 20 dB down at worst
        selective = 0.6 + 0.4 * np.cos(2 * np.pi * 0.31 * t)        # the carrier alone, 14 dB down at worst
        station = carrier * flat * (selective + MODULATION * voice(t))
        offset_hz = VFO_OFFSET_HZ + CARRIER_OFFSET_HZ
        samples += station * np.exp(2j * np.pi * offset_hz * t)
        whistle = carrier * 10 ** (WHISTLE_DB / 20)
        samples += whistle * np.exp(2j * np.pi * (offset_hz + WHISTLE_HZ) * t + 1.0j)
        period = round(IMPULSE_PERIOD_S * self.rate_hz)
        first = -(-start // period) * period
        for at in range(first, start + count, period):
            stop = min(at + IMPULSE_SAMPLES, start + count)
            samples[at - start:stop - start] += 10 ** (IMPULSE_DBFS / 20) * np.exp(1j * (at % 7))
        return samples.astype(np.complex64)


def voice(t: np.ndarray) -> np.ndarray:
    """A voice-like sound at ``t`` seconds, peaks near 1: the harmonics of
    a pitch that glides between 110 and 190 Hz, under syllables of about
    three a second."""
    phase = 2 * np.pi * (150 * t - 40 / (2 * np.pi * 1.7) * np.cos(2 * np.pi * 1.7 * t))
    sound = sum(np.sin(k * phase) / k for k in range(1, 13))
    syllables = 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * t) ** 2
    return 0.55 * sound * syllables


class _Tapped:
    """A filter whose output also goes to ``receiver``."""

    def __init__(self, filter_: Any, receiver: Callable[[np.ndarray, float], None]) -> None:
        self._filter = filter_
        self._receiver = receiver

    def process(self, samples: np.ndarray) -> np.ndarray:
        output = self._filter.process(samples)
        self._receiver(output, self._filter.output_rate_hz)
        return output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._filter, name)


class _Notched:
    """A detector whose output the notch reads, changing the audio
    filter's taps when its notches change."""

    def __init__(self, detector: Any, notch: listening.AutoNotch, audio_filter: Any) -> None:
        self._detector = detector
        self.notch = notch
        self._filter = audio_filter

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        detected = self._detector.process(channel, power)
        taps = self.notch.process(detected)
        if taps is not None:
            self._filter.set_taps(taps)
        return detected


@dataclass
class Activity:
    """What the tools did, so that the figures are those of tools at work."""

    blocks: int = 0
    locked: int = 0         # blocks after which SAM held a lock
    notches: int = 0        # the most notches at once
    blanked: int = 0        # blocks with impulses zeroed
    frames: int = 0         # fine spectrum frames


class ToolDemodulator(Demodulator):
    """The engine's demodulator with the prototypes of the listening tools
    where the design puts them; SAM is its own, through the mode. It reaches
    into the demodulator's audio path, which the package keeps private, as
    the pull requests that build the tools will do in demod.py itself; the
    audio path made for the first block runs without the notch, which the
    warm-up leaves out."""

    def __init__(self, tools: frozenset[str]) -> None:
        super().__init__()
        self.tools = tools
        self.activity = Activity()
        self._blanker = listening.NoiseBlanker() if "blanker" in tools else None
        self._fine = listening.FineSpectrum() if "fine" in tools else None
        self._notched: _Notched | None = None
        self._hooked: object = None

    def _start(self, rate_hz: float, wide: bool) -> None:
        super()._start(rate_hz, wide)
        if self._fine is not None and self._front is not None and not isinstance(self._front, _Tapped):
            self._front = _Tapped(self._front, self._fine_frames)

    def _fine_frames(self, channel: np.ndarray, rate_hz: float) -> None:
        assert self._fine is not None
        self.activity.frames += len(self._fine.add(channel, rate_hz))

    def process(self, samples: np.ndarray, centre_hz: float, rate_hz: float, settings: DemodSettings,
                noise_density: float | None, audio: bool,
                tap: Callable[[np.ndarray], None] | None = None) -> tuple[Any, np.ndarray | None]:
        if self._blanker is not None:
            blanked = self._blanker.process(samples)
            self.activity.blanked += blanked is not samples
            samples = blanked
        meter, pcm = super().process(samples, centre_hz, rate_hz, settings, noise_density, audio, tap)
        path = self._audio
        if path is not None and path is not self._hooked:
            self._hooked = path
            if "notch" in self.tools:
                self._notched = _Notched(path._detector, listening.AutoNotch(self.channel_rate_hz), path._filter)
                path._detector = self._notched
        self.activity.blocks += 1
        self.activity.locked += bool(meter.sam_locked)
        if self._notched is not None:
            self.activity.notches = max(self.activity.notches, len(self._notched.notch.notches))
        return meter, pcm


class Pipeline:
    """One configuration's state: the engine's objects for one receiver."""

    def __init__(self, tools: frozenset[str]) -> None:
        self.analyzer = SpectrumAnalyzer()
        self.demodulator = ToolDemodulator(tools)
        self.detector = Detector()
        self.watch = ChannelWatch()
        mode = SAM_MODE if "sam" in tools else MODE
        self.settings = DemodSettings(CENTRE_HZ + VFO_OFFSET_HZ, mode, MODE_BANDWIDTHS_HZ[mode])

    def process(self, samples: np.ndarray) -> int:
        """Process one block as the engine does; the processor time it took, in ns."""
        started = time.thread_time_ns()
        _process_block(self.analyzer, self.demodulator, self.detector, self.watch, samples, CENTRE_HZ, RATE_HZ,
                       True, self.settings, True, DEFAULT_THRESHOLD_DB, DETECTION_BLOCKS, None)
        return time.thread_time_ns() - started


@dataclass
class Run:
    seconds: float
    cpu_ns: dict[str, int]
    activity: dict[str, Activity] = field(default_factory=dict)

    def percent(self, name: str) -> float:
        """The share of one core that a configuration took."""
        return self.cpu_ns[name] / 1e9 / self.seconds * 100

    def added(self, name: str) -> float:
        """What a configuration took more than the baseline, in % of one core."""
        return self.percent(name) - self.percent(BASELINE)


def block_size(rate_hz: float = RATE_HZ) -> int:
    """The engine's block at this rate (engine.Engine.run)."""
    return max(SpectrumAnalyzer().fft_size, round(rate_hz / FRAME_RATE))


def measure(scene: Scene, seconds: float, warmup_s: float,
            progress: Callable[[str], None] = lambda line: None) -> Run:
    """One run: every configuration over the same blocks."""
    size = block_size(scene.rate_hz)
    warmup = math.ceil(warmup_s * scene.rate_hz / size)
    timed = max(1, round(seconds * scene.rate_hz / size))
    pipelines = {name: Pipeline(tools) for name, tools in CONFIGURATIONS.items()}
    names = list(pipelines)
    cpu = dict.fromkeys(names, 0)
    for index in range(warmup + timed):
        samples = scene.block(index * size, size)
        turn = index % len(names)
        for name in names[turn:] + names[:turn]:
            spent = pipelines[name].process(samples)
            if index >= warmup:
                cpu[name] += spent
        if (index + 1) % round(FRAME_RATE) == 0:
            progress(f"  {(index + 1) * size / scene.rate_hz:.0f} s of signal")
    return Run(timed * size / scene.rate_hz, cpu,
               {name: pipeline.demodulator.activity for name, pipeline in pipelines.items()})


def redesign_ms(repeats: int = 5) -> float:
    """The median time to compute the audio filter's taps with four
    notches, which the notch does when its notches change."""
    rate_hz = 48_000.0
    times = []
    for _ in range(repeats):
        started = time.thread_time_ns()
        listening.notch_taps((700.0, 1_300.0, 2_100.0, 3_400.0), rate_hz)
        times.append((time.thread_time_ns() - started) / 1e6)
    return statistics.median(times)


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
    return f"{statistics.median(values):.2f} ({min(values):.2f} to {max(values):.2f})"


def verdict(name: str, values: Sequence[float]) -> str:
    limit = TOGETHER_PCT if name == ALL else SHARE_PCT
    over = statistics.median(values) > limit
    return f"over its {limit:g}%" if over else f"within its {limit:g}%"


def report(runs: Sequence[Run], options: argparse.Namespace, started: datetime, loads: tuple[str, str]) -> list[str]:
    """The table and what it was measured on, in Markdown."""
    lines = [
        "# Listening tools: processor time (spike 8)",
        "",
        f"- Measured on {started:%Y-%m-%d %H:%M} UTC, {len(runs)} runs of {runs[0].seconds:g} s of signal each,"
        f" after {options.warmup_s:g} s not timed, seed {options.seed}.",
        f"- Processor: {processor()}, {os.cpu_count()} logical processors; load average"
        f" {loads[0]} at start, {loads[1]} at the end.",
        f"- Python {platform.python_version()}, NumPy {np.__version__}.",
        f"- Engine block: {block_size()} samples at {RATE_HZ / 1e6:g} MS/s ({FRAME_RATE:g} blocks a second);"
        f" the VFO in {MODE} ({SAM_MODE} for synchronous AM), {MODE_BANDWIDTHS_HZ[MODE] / 1e3:g} kHz, audio and"
        " detection on.",
        "- Processor time of the processing thread per second of signal, in % of one core:"
        " the median of the runs, then the lowest and the highest.",
        "",
        "| Configuration | % of one core | Added by the tool | Rule |",
        "| --- | --- | --- | --- |",
        f"| Baseline, without any tool | {spread([run.percent(BASELINE) for run in runs])} | | |",
    ]
    for name in (*TOOLS, ALL):
        added = [run.added(name) for run in runs]
        lines.append(f"| {NAMES[name]} | {spread([run.percent(name) for run in runs])} | {spread(added)}"
                     f" | {verdict(name, added)} |")
    activity = {name: [run.activity[name] for run in runs] for name in CONFIGURATIONS}
    sam = activity["sam"]
    lines += [
        "",
        "What the tools did in the timed blocks and the warm-up, over every run:",
        "",
        f"- SAM held a lock after {sum(a.locked for a in sam)} of {sum(a.blocks for a in sam)} blocks.",
        f"- The notch removed at most {max(a.notches for a in activity['notch'])} lines at once; computing the"
        f" audio filter's taps with four notches, when they change, takes {redesign_ms():.1f} ms.",
        f"- The blanker zeroed impulses in {sum(a.blanked for a in activity['blanker'])} of"
        f" {sum(a.blocks for a in activity['blanker'])} blocks.",
        f"- The fine spectrum made {sum(a.frames for a in activity['fine'])} frames.",
        "",
        "Each run, % of one core added by each tool (the baseline's own in the first column):",
        "",
        "| Run | Baseline | " + " | ".join(NAMES[name] for name in (*TOOLS, ALL)) + " |",
        "| --- | --- | " + " | ".join("---" for _ in (*TOOLS, ALL)) + " |",
    ]
    for number, run in enumerate(runs, 1):
        lines.append(f"| {number} | {run.percent(BASELINE):.2f} | "
                     + " | ".join(f"{run.added(name):.2f}" for name in (*TOOLS, ALL)) + " |")
    return lines


def positive(kind: type) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        value = kind(text)
        if not value > 0:
            raise argparse.ArgumentTypeError(f"must be positive, not {text}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the processor time that each listening tool (SAM, then the prototypes of"
                    " tools/listening.py) adds to the engine's processing of one receiver at 2.4 MS/s, on"
                    " synthetic samples. No receiver, no network.")
    parser.add_argument("--runs", type=positive(int), default=7, help="runs, each with fresh state (default 7)")
    parser.add_argument("--seconds", type=positive(float), default=10.0,
                        help="seconds of signal timed in each run (default 10)")
    parser.add_argument("--warmup-s", type=positive(float), default=2.0,
                        help="seconds of signal processed first in each run, not timed (default 2)")
    parser.add_argument("--seed", type=int, default=1, help="the synthetic signal's seed (default 1)")
    parser.add_argument("--output", type=Path, help="also write the table to this file")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    options = build_parser().parse_args(argv)
    started = datetime.now(timezone.utc)
    first_load = load()
    scene = Scene(options.seed)
    runs = []
    try:
        for number in range(1, options.runs + 1):
            print(f"Run {number} of {options.runs}", file=err, flush=True)
            runs.append(measure(scene, options.seconds, options.warmup_s,
                                lambda line: print(line, file=err, flush=True)))
    except KeyboardInterrupt:
        print("Interrupted: no table.", file=err)
        return EXIT_INTERRUPTED
    lines = report(runs, options, started, (first_load, load()))
    text = "\n".join(lines) + "\n"
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
