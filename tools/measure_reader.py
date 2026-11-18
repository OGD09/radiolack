#!/usr/bin/env python3
"""Measure whether a Python thread reads a SoapySDR receiver without losing
samples: later-milestones.md, section 1, the first pull request's third
spike, and spike 7 of scoping.md. If it does not at 2.4 MS/s, the future
SoapySDR source reads in a process of its own.

What it does to the receiver: it opens the device that the SoapySDR device
arguments name, sets channel 0's sample rate and, with ``--frequency``, its
frequency, sets up one receive stream on channel 0 in the device's native
format, reads it for ``--seconds``, then closes the stream and the device,
on success, on failure and on Ctrl-C. It changes no gain, antenna, antenna
power or other setting, and has no code that transmits. It is run only on
the owner's word, on the owner's receiver: tests run it against a stand-in
for the SoapySDR module (tests/soapysdr/SoapySDR.py).

It reads as the future source will (later-milestones.md, section 1,
Samples): a thread calls ``readStream`` into an array in the device's
format and keeps what comes in a ``NativeBuffer`` (measure_rates.py), which
drops the oldest samples when the engine falls behind, as the rtl_tcp
client's buffer does. Meanwhile this thread takes the engine's blocks from
the buffer, the rate over 30, converts them to complex64 and, unless
``--without-engine``, processes them as the engine does
(``engine._process_block``, the VFO in NFM with audio and detection, as
measure_rates.py does), so that the reader competes for Python's lock with
the engine's work, as it will in Radiolack.

It reports the samples received against those the device's timestamps
span and against those the wall clock expects since the first samples
came; the overflows the device reported; the gaps in its timestamps; the
time-outs and stream errors; the samples the buffer dropped because the
engine fell behind; and the processor time of the reading thread and of
the engine's, per second of signal.

The verdict. Not every module's timestamps count samples: SoapySDR's
rtlsdr module 0.3.3 advances them by exactly twice the samples read, at
every read, as it counts the I and Q bytes. So the tool first looks at how
the timestamps advance from one read to the next, leaving out the reads
after an overflow or a stream error: when every read, at least
``MIN_PAIRS`` of them, puts the next timestamp ahead by the same multiple
of its samples, within one sample, and that multiple is not 1, the
timestamps run at a constant multiple of the samples read. The report then
says so ("the timestamps do not count samples: they advance N times as
fast") and the verdict leaves them out. Timestamps that count samples keep
their role: a gap in them is a loss. Timestamps that follow neither
pattern are taken as counting samples, so that the verdict errs towards a
loss, which the table's gaps and timestamps earlier than expected then
explain.

The reader kept up when samples came and none of these is seen: the
measure stopped early; an overflow reported by the device; a stream error
(a read that failed); timestamps that count samples with a gap; samples
the buffer dropped for the engine; or fewer samples received than the
wall clock expects, by more than ``CLOCK_SHORT`` of them plus
``END_READS`` reads of the stream's MTU. That allowance covers a crystal
tens of ppm off and the computer's clock, which NTP may slew by up to
500 ppm, and the clock's first and last readings, which are not instants:
each read hands over samples made up to one read earlier, and at the end
a read's worth may still wait in the device. Time-outs are reported and do
not decide: a stall whose samples never come shows on the clock, one whose
samples come later lost nothing, and at a low rate a device that hands
over its transfers less often than the read's wait times out without
losing anything.

It needs the SoapySDR Python module (Debian's python3-soapysdr) and the
device's SoapySDR module, and runs from a checkout of the repository; the
Debian package does not install it.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_rates import (FORMATS, NativeBuffer, Pipeline, block_size, load, processor,  # noqa: E402
                           to_complex)
from radiolack.rtltcp import BUFFER_S  # noqa: E402  (measure_rates puts src on the path)

RATE_HZ = 2_400_000.0
SECONDS = 60.0
TIMEOUT_US = 100_000            # a read's wait for samples
WAIT_S = 0.5                    # the engine's wait for a block, as the rtl_tcp client's READ_WAIT_S
FAILED_READS = 10               # stream errors in a row that end the measure
CLOCK_SHORT = 1e-3              # the shortfall allowed against the wall clock, as a share of what it expects
END_READS = 2                   # and in reads of the stream's MTU, for its first and last readings
MIN_PAIRS = 10                  # reads that must show the same multiple for timestamps that do not count samples
JOIN_S = 5.0

EXIT_KEPT_UP = 0
EXIT_LOST = 1
EXIT_UNABLE = 2
EXIT_INTERRUPTED = 130


@dataclass
class Reading:
    """What the reading thread saw."""

    received: int = 0           # samples
    reads: int = 0
    overflows: int = 0
    timeouts: int = 0
    errors: Counter[str] = field(default_factory=Counter)      # other negative returns, by name
    timestamps: bool = True     # every read that gave samples had a timestamp
    gaps: int = 0               # jumps forward in the timestamps
    gap_samples: int = 0        # the samples they skipped
    backwards: int = 0          # timestamps earlier than expected
    first_ns: int | None = None             # the first samples' timestamp
    end_ns: int | None = None               # the timestamp just after the last samples
    first_at: float | None = None           # monotonic time the first samples came
    last_at: float | None = None            # and the last
    first_count: int = 0                    # samples in the first read that gave any
    # Each read followed by another without an overflow or a stream error
    # between them: its samples, and how far the next timestamp lies ahead
    # of its own, in ns.
    advances: list[tuple[int, int]] = field(default_factory=list)
    cpu_ns: int = 0             # the thread's processor time
    problem: str = ""           # why it stopped early


def timestamp_factor(advances: Sequence[tuple[int, int]], rate_hz: float) -> float | None:
    """How many times as fast as the samples read the timestamps advance,
    when they run at a constant multiple of them other than 1: every one of
    at least ``MIN_PAIRS`` reads puts the next timestamp ahead by the same
    multiple of its samples, within one sample. None otherwise: the
    timestamps count samples, or follow no such pattern."""
    if len(advances) < MIN_PAIRS:
        return None
    counts = [count for count, _ in advances]
    ahead = [advance_ns * rate_hz / 1e9 for _, advance_ns in advances]      # in samples

    def fits(factor: float) -> bool:
        return all(abs(samples - factor * count) <= 1.0 for count, samples in zip(counts, ahead))

    factor = sum(ahead) / sum(counts)
    if fits(1.0) or not fits(factor):
        return None
    return factor


class Reader(threading.Thread):
    """Reads the stream into ``buffer`` until stopped."""

    def __init__(self, soapy: ModuleType, device: Any, stream: Any, sample_format: str, rate_hz: float,
                 mtu: int, buffer: NativeBuffer, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(name="soapy-reader", daemon=True)
        self.soapy = soapy
        self.device = device
        self.stream = stream
        self.rate_hz = rate_hz
        self.mtu = mtu
        self.buffer = buffer
        self.clock = clock
        self.reading = Reading()
        self.stopping = threading.Event()
        self._array = np.empty(2 * mtu, FORMATS[sample_format].component)
        self._failed = 0                # stream errors in a row
        self._interrupted = False       # an overflow or a stream error since the last samples
        self._previous: tuple[int, int] | None = None      # the last samples' timestamp and count

    def run(self) -> None:
        reading = self.reading
        started = time.thread_time_ns()
        try:
            while not self.stopping.is_set() and self.read_once():
                pass
        except Exception as error:      # a module's failure ends the measure, with its message
            reading.problem = f"the read failed: {error}"
        finally:
            reading.cpu_ns = time.thread_time_ns() - started

    def read_once(self) -> bool:
        """One read into the buffer, counted; False when the stream has
        failed too often in a row to go on."""
        soapy, reading, array = self.soapy, self.reading, self._array
        result = self.device.readStream(self.stream, [array], self.mtu, 0, TIMEOUT_US)
        reading.reads += 1
        count = result.ret
        if count > 0:
            self._failed = 0
            now = self.clock()
            self._timestamp(result, count)
            self._interrupted = False
            self.buffer.put(memoryview(array)[:2 * count].cast("B"))
            if reading.first_at is None:
                reading.first_at, reading.first_count = now, count
            reading.last_at = now
            reading.received += count
        elif count == soapy.SOAPY_SDR_TIMEOUT:
            reading.timeouts += 1
        elif count == soapy.SOAPY_SDR_OVERFLOW:
            reading.overflows += 1
            self._interrupted = True
        else:
            reading.errors[soapy.errToStr(count)] += 1
            self._interrupted = True
            self._failed += 1
            if self._failed >= FAILED_READS:
                reading.problem = f"the stream failed {self._failed} times in a row: {soapy.errToStr(count)}"
                return False
        return True

    def _timestamp(self, result: Any, count: int) -> None:
        reading = self.reading
        if not result.flags & self.soapy.SOAPY_SDR_HAS_TIME:
            reading.timestamps = False
            return
        at_ns = int(result.timeNs)
        if reading.end_ns is None:
            reading.first_ns = at_ns
        else:
            skipped = round((at_ns - reading.end_ns) * self.rate_hz / 1e9)
            if skipped > 0:
                reading.gaps += 1
                reading.gap_samples += skipped
            elif skipped < 0:
                reading.backwards += 1
        if self._previous is not None and not self._interrupted:
            previous_ns, previous_count = self._previous
            reading.advances.append((previous_count, at_ns - previous_ns))
        self._previous = at_ns, count
        reading.end_ns = at_ns + round(count * 1e9 / self.rate_hz)


@dataclass
class Engine:
    """What the engine's side saw."""

    blocks: int = 0
    missed: int = 0             # waits for a block that ended without one
    cpu_ns: int = 0


@dataclass
class Result:
    args: str
    sample_format: str
    rate_hz: float
    mtu: int
    seconds: float              # of reading, on the wall clock
    reading: Reading
    engine: Engine
    dropped: int                # samples the buffer dropped for the engine
    with_engine: bool

    @property
    def spanned(self) -> int | None:
        """The samples the device's timestamps span, if it gave them, read
        as if they counted samples."""
        reading = self.reading
        if not reading.timestamps or reading.first_ns is None or reading.end_ns is None:
            return None
        return round((reading.end_ns - reading.first_ns) * self.rate_hz / 1e9)

    @property
    def factor(self) -> float | None:
        """How many times as fast as the samples read the timestamps
        advance, when they do not count samples (``timestamp_factor``)."""
        if self.spanned is None:
            return None
        return timestamp_factor(self.reading.advances, self.rate_hz)

    @property
    def counts_samples(self) -> bool:
        """Whether the timestamps are taken as counting samples."""
        return self.spanned is not None and self.factor is None

    @property
    def by_clock(self) -> int | None:
        """The samples the wall clock expects from the first read that gave
        any to the last."""
        reading = self.reading
        if reading.first_at is None or reading.last_at is None:
            return None
        return reading.first_count + round((reading.last_at - reading.first_at) * self.rate_hz)

    @property
    def allowance(self) -> int:
        """The shortfall against the wall clock that is not a loss."""
        return round(CLOCK_SHORT * (self.by_clock or 0)) + END_READS * self.mtu

    @property
    def losses(self) -> list[str]:
        """What lost samples, by the verdict's rule; empty when the reader
        kept up."""
        reading = self.reading
        if reading.received == 0:
            return ["no sample came"]
        found = []
        if reading.problem:
            found.append("the measure stopped early")
        if reading.overflows:
            found.append(f"{reading.overflows} overflows reported")
        if reading.errors:
            found.append(f"{sum(reading.errors.values())} reads failed")
        if self.counts_samples and reading.gaps:
            found.append(f"{reading.gaps} gaps in the timestamps")
        if self.dropped:
            found.append(f"{self.dropped} samples dropped by the buffer")
        short = (self.by_clock or 0) - reading.received
        if short > self.allowance:
            found.append(f"{short} samples fewer than the wall clock expects, over the {self.allowance} allowed")
        return found

    @property
    def kept_up(self) -> bool:
        return not self.losses


def native_format(soapy: ModuleType, device: Any) -> str:
    """The device's native format among CS8, CS16 and CF32: as
    ``getNativeStreamFormat`` gives it where the binding can call it, or
    else the first of those the device lists, SoapySDR converting to CF32
    for one that lists none."""
    receive = soapy.SOAPY_SDR_RX
    try:
        native = device.getNativeStreamFormat(receive, 0)
    except TypeError:
        # SoapySDR 0.8's binding takes the full scale as a C++ reference,
        # which Python cannot pass: the call cannot be made.
        native = None
    if isinstance(native, (tuple, list)) and native:
        native = native[0]
    if isinstance(native, str) and native in FORMATS:
        return native
    for listed in device.getStreamFormats(receive, 0):
        if listed in FORMATS:
            return listed
    return "CF32"


def measure(soapy: ModuleType, args: str, rate_hz: float, seconds: float, frequency_hz: float | None,
            sample_format: str | None, with_engine: bool,
            progress: Callable[[str], None] = lambda line: None) -> Result:
    """Open the device, read it for ``seconds``, close it. Raises
    RuntimeError, with the module's message, when the device cannot be
    opened or set up."""
    receive = soapy.SOAPY_SDR_RX
    try:
        device = soapy.Device(args)
    except Exception as error:
        raise RuntimeError(f"cannot open the device {args!r}: {error}") from error
    stream = None
    try:
        try:
            device.setSampleRate(receive, 0, rate_hz)
            actual_hz = float(device.getSampleRate(receive, 0))
            if frequency_hz is not None:
                device.setFrequency(receive, 0, frequency_hz)
            chosen = sample_format or native_format(soapy, device)
            stream = device.setupStream(receive, chosen, [0])
            mtu = int(device.getStreamMTU(stream))
        except Exception as error:
            raise RuntimeError(f"cannot set the device up: {error}") from error
        if mtu < 1:
            raise RuntimeError(f"the stream's MTU is {mtu}")
        buffer = NativeBuffer(FORMATS[chosen].sample_bytes, round(BUFFER_S * actual_hz))
        reader = Reader(soapy, device, stream, chosen, actual_hz, mtu, buffer)
        engine = Engine()
        pipeline = Pipeline(actual_hz) if with_engine else None
        block = block_size(actual_hz)
        status = device.activateStream(stream)
        if status != 0:
            raise RuntimeError(f"cannot start the stream: {soapy.errToStr(status)}")
        started = time.monotonic()
        reader.start()
        try:
            engine_started = time.thread_time_ns()
            next_progress = started + 10.0
            while time.monotonic() - started < seconds and reader.is_alive():
                data = buffer.take(block, WAIT_S)
                if data is None:
                    engine.missed += 1
                    continue
                samples = to_complex(data, FORMATS[chosen])
                if pipeline is not None:
                    pipeline.process(samples)
                engine.blocks += 1
                if time.monotonic() >= next_progress:
                    progress(f"  {next_progress - started:.0f} s read")
                    next_progress += 10.0
            engine.cpu_ns = time.thread_time_ns() - engine_started
        finally:
            reader.stopping.set()
            reader.join(JOIN_S)
            elapsed = time.monotonic() - started
            device.deactivateStream(stream)
        if reader.is_alive():
            raise RuntimeError(f"the reading thread did not stop within {JOIN_S:g} s")
        return Result(args, chosen, actual_hz, mtu, elapsed, reader.reading, engine, buffer.dropped, with_engine)
    finally:
        if stream is not None:
            device.closeStream(stream)
        close = getattr(device, "close", None)
        if callable(close):
            close()
        else:
            soapy.Device.unmake(device)


def report(result: Result, started: datetime, loads: tuple[str, str]) -> list[str]:
    reading, engine = result.reading, result.engine
    seconds = max(result.seconds, 1e-9)
    spanned, by_clock, factor = result.spanned, result.by_clock, result.factor
    lines = [
        "# Reading thread (spike 7)",
        "",
        f"- Measured on {started:%Y-%m-%d %H:%M} UTC: {result.seconds:.1f} s of reading.",
        f"- Device: {result.args}; {result.rate_hz / 1e6:g} MS/s, native format {result.sample_format}, reads of up"
        f" to {result.mtu} samples (the stream's MTU).",
        f"- Processor: {processor()}; load average {loads[0]} at start, {loads[1]} at the end.",
        "- The engine's blocks " + ("processed as the engine does (NFM, audio and detection)."
                                    if result.with_engine else "taken and converted, not processed."),
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Samples received | {reading.received} |",
    ]
    if spanned is None:
        lines.append("| Samples the timestamps span | no timestamps |")
    elif factor is not None:
        lines.append(f"| Samples the timestamps span | {spanned}: they advance {factor:.4g} times as fast as the"
                     " samples read, not used |")
    else:
        lines.append(f"| Samples the timestamps span | {spanned} ({spanned - reading.received} not received) |")
    if by_clock is not None:
        short = (by_clock - reading.received) / by_clock * 100 if by_clock else 0.0
        lines.append(f"| Samples the wall clock expects since the first came | {by_clock} ({short:.2f}% short,"
                     f" {result.allowance} samples allowed) |")
    lines += [
        f"| Overflows the device reported | {reading.overflows} |",
        "| Gaps in the timestamps | " + ("not used" if factor is not None else
                                         f"{reading.gaps} ({reading.gap_samples} samples)") + " |",
        f"| Timestamps earlier than expected | {reading.backwards} |",
        f"| Reads | {reading.reads}, {reading.timeouts} timed out |",
        "| Stream errors | " + (", ".join(f"{name} {count}" for name, count in sorted(reading.errors.items()))
                                or "none") + " |",
        f"| Samples the buffer dropped, the engine behind | {result.dropped} |",
        f"| The engine's blocks | {engine.blocks}, {engine.missed} waits of {WAIT_S:g} s without one |",
        f"| Reading thread, % of one core | {reading.cpu_ns / 1e9 / seconds * 100:.2f} |",
        f"| Engine's thread, % of one core | {engine.cpu_ns / 1e9 / seconds * 100:.2f} |",
        "",
    ]
    if reading.problem:
        lines.append(f"The measure stopped early: {reading.problem}.")
    if factor is not None:
        lines.append(f"The timestamps do not count samples: they advance {factor:.4g} times as fast as the samples"
                     " read, at every read. The verdict leaves them out.")
    losses = result.losses
    lines.append("The reader kept up: no sample lost." if not losses else
                 f"Samples were lost, or none came ({'; '.join(losses)}): by the rule, the reader moves to a"
                 " process of its own.")
    return lines


def positive(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be positive, not {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a SoapySDR receiver from a Python thread, as Radiolack's SoapySDR source will, and"
                    " report the samples lost. It opens the device that ARGS name, sets channel 0's sample rate"
                    " and, with --frequency, its frequency, opens one receive stream, reads it for --seconds and"
                    " closes it; it changes no gain, antenna or antenna power, and never transmits. Run it only"
                    " on a receiver that nothing else uses.")
    parser.add_argument("args", help="SoapySDR device arguments, such as driver=rtlsdr")
    parser.add_argument("--rate", type=positive, default=RATE_HZ / 1e6,
                        help=f"sample rate in MS/s (default {RATE_HZ / 1e6:g})")
    parser.add_argument("--frequency", type=positive, help="centre frequency in MHz (default: the device's)")
    parser.add_argument("--seconds", type=positive, default=SECONDS, help=f"seconds of reading (default {SECONDS:g})")
    parser.add_argument("--format", choices=sorted(FORMATS), help="the stream's format (default: the native one)")
    parser.add_argument("--without-engine", dest="engine", action="store_false",
                        help="take and convert the engine's blocks without processing them")
    parser.add_argument("--output", type=Path, help="also write the table to this file")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    options = build_parser().parse_args(argv)
    try:
        soapy = importlib.import_module("SoapySDR")     # only now: the tests give a stand-in
    except ImportError as error:
        print(f"The SoapySDR Python module is missing (Debian: python3-soapysdr): {error}", file=err)
        return EXIT_UNABLE
    started = datetime.now(timezone.utc)
    first_load = load()
    frequency_hz = None if options.frequency is None else options.frequency * 1e6
    try:
        result = measure(soapy, options.args, options.rate * 1e6, options.seconds, frequency_hz, options.format,
                         options.engine, lambda line: print(line, file=err, flush=True))
    except RuntimeError as error:
        print(str(error), file=err)
        return EXIT_UNABLE
    except KeyboardInterrupt:
        print("Interrupted: the device is closed, no table.", file=err)
        return EXIT_INTERRUPTED
    text = "\n".join(report(result, started, (first_load, load()))) + "\n"
    out.write(text)
    if options.output is not None:
        try:
            options.output.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"Cannot write {options.output}: {error}", file=err)
            return EXIT_UNABLE
    return EXIT_KEPT_UP if result.kept_up else EXIT_LOST


if __name__ == "__main__":
    sys.exit(main())
