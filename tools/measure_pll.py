#!/usr/bin/env python3
"""Measure the error of an RTL-SDR tuner's PLL through a Radiolack that is
already running on the receiver: coverage-and-precision.md, pull request 13.

It drives that Radiolack through its stream on 127.0.0.1, as an interface
does, and changes nothing but the centre frequency, which it restores at
the end, on success, on failure and on Ctrl-C. With the VFO on one steady
carrier above 24 MHz, it moves the centre frequency through 40 values
1.234 kHz apart, then through values that put the tuner's oscillator on
multiples of 112.5 kHz, and asks the engine to measure the carrier at each
(carrier.py). No reference is needed: the carrier stays where it is on the
air, so an error of the PLL shows as a carrier that moves with the centre
frequency. The two computations of the driver (tests/r82xx_pll.py) predict
how it moves.

Drift: the crystal warms up and drifts over the minutes a run takes, which
moves the carrier too, slowly and whatever the centre frequency. The tool
measures the first centre again every few points and at the end, takes
each point's offset from the reference interpolated in time between the
measures around it, and reports the drift beside the spread. When the
reference moves too much between two of its measures, the interpolation
cannot be trusted and no rule applies: the owner sees the table.

The rule, fixed beforehand: a spread of 30 Hz or more near 145 MHz (the
tuner's divider of 145 MHz) that follows one of the two predictions, pull
request 14 is built; under 10 Hz, the error is recorded as not observed; in
between, the owner sees the table. A run that stops midway gives no
verdict.

It takes its predictions from tests/r82xx_pll.py and the correction's
arithmetic from Radiolack's own source, so it runs from a checkout of the
repository; the Debian package does not install it.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import aiohttp

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
from radiolack.correction import corrected_hz, whole_ppm  # noqa: E402


def _load_pll() -> Any:
    """tests/r82xx_pll.py, by its path: importing the tests' package would
    point the configuration folders elsewhere for the whole process."""
    spec = importlib.util.spec_from_file_location("r82xx_pll", REPOSITORY / "tests" / "r82xx_pll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLL = _load_pll()

HOST = "127.0.0.1"
PROTOCOL = 6
STEP_HZ = 1234              # the first sweep's step
STEPS = 40                  # and its centre frequencies, the first one included
MULTIPLE_HZ = 112_500       # the second sweep's oscillators are multiples of this
MULTIPLES = 8
REFERENCE_EVERY = 10        # the first centre measured again after this many points
ATTEMPTS = 2                # measures asked at one point before the carrier counts as lost
TIMEOUT_S = 20.0            # for a retune or a measure to come back

BUILD_HZ = 30.0             # the rule: this spread or more near 145 MHz, following a prediction
NOT_OBSERVED_HZ = 10.0      # under this spread, the error is not observed
FOLLOW_HZ = 5.0             # a prediction is followed when every offset lies this close to it
DRIFT_STEP_HZ = 5.0         # the reference may move this much between two of its measures
RULE_HZ = 145_000_000       # the rule holds for the tuner's divider of this frequency

TUNERS = ("Rafael Micro R820T", "Rafael Micro R828D")
CENTRED_MODES = ("AM", "NFM", "CW")     # their filter is centred on the VFO
TUNER_LOW_HZ = 24_000_000
V4_UPCONVERSION_HZ = 28_800_000         # an RTL-SDR Blog V4 upconverts below this
DC_CLEARANCE_HZ = 1000.0    # the filter's edge stays this far from the centre frequency
SPAN_USE = 0.4              # and within this share of the sample rate from it
FILTER_MARGIN_HZ = 200.0    # the carrier stays this far inside the filter at every prediction

PREDICTIONS = {
    "truncating": "rtl-sdr up to 2.0.1, Debian 12's 0.6.0 among them, and the RTL-SDR Blog fork",
    "rounding": "rtl-sdr 2.0.2 and later",
}
NAMES = {"truncating": "up to 2.0.1", "rounding": "2.0.2 on"}
STEP, MULTIPLE, REFERENCE = "1.234 kHz step", "112.5 kHz oscillator", "reference"

EXIT_VERDICT, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 130


class Refused(Exception):
    """The measure cannot start, and why."""


class Incomplete(Exception):
    """The measure stopped midway, and why."""


@dataclass(frozen=True)
class Receiver:
    """What the measure needs of the receiver's settings, read at start."""

    centre_hz: int
    rate_hz: int
    ppm: float
    whole_ppm: int
    vfo_hz: int
    mode: str
    bandwidth_hz: float
    tuner: str
    lowest_hz: float
    highest_hz: float

    def asked_hz(self, centre_hz: int) -> int:
        """The frequency that Radiolack asks of rtl_tcp for ``centre_hz``."""
        return corrected_hz(centre_hz, self.ppm, self.whole_ppm)

    def oscillator_hz(self, centre_hz: int) -> int:
        return PLL.oscillator_hz(self.asked_hz(centre_hz), self.rate_hz)

    def true_centre_hz(self, computation: str, centre_hz: int) -> float:
        """The centre the receiver truly tunes for ``centre_hz`` by the
        driver's ``computation``, the crystal as corrected."""
        scale = (1 + self.ppm / 1e6) / (1 + self.whole_ppm / 1e6)
        return PLL.tuned_hz(computation, self.asked_hz(centre_hz), self.rate_hz, self.whole_ppm) * scale

    def predicted_hz(self, computation: str, centre_hz: int) -> float:
        """How far the carrier moves from its first measure at
        ``centre_hz``: it shows at the centre asked plus its distance from
        the centre truly tuned."""
        def shift(centre: int) -> float:
            return centre - self.true_centre_hz(computation, centre)
        return shift(centre_hz) - shift(self.centre_hz)

    def fits(self, centre_hz: int) -> str | None:
        """Why the VFO's channel cannot be measured at ``centre_hz``, or None."""
        distance = abs(self.vfo_hz - centre_hz)
        lowest = max(self.lowest_hz, TUNER_LOW_HZ)
        if not lowest <= centre_hz <= self.highest_hz:
            return f"{centre_hz} Hz lies beyond the tuner's range"
        if distance < self.bandwidth_hz / 2 + DC_CLEARANCE_HZ:
            return f"the VFO would lie within its filter of the centre frequency {centre_hz} Hz"
        if distance + self.bandwidth_hz / 2 > SPAN_USE * self.rate_hz:
            return f"the VFO would lie too close to the edge of the span around {centre_hz} Hz"
        try:
            for computation in PLL.COMPUTATIONS:
                self.true_centre_hz(computation, centre_hz)
        except ValueError as problem:
            return str(problem)
        return None


@dataclass(frozen=True)
class Point:
    kind: str
    centre_hz: int


@dataclass
class Measure:
    point: Point
    carrier_hz: float
    margin_db: float
    at: float                   # monotonic time
    drift_hz: float = 0.0       # the reference at that time, from the first measure
    offset_hz: float = 0.0      # from the first measure, the drift taken off


@dataclass
class Analysis:
    spread_hz: float
    residual_hz: dict[str, float]
    follows: list[str]
    references: list[float]
    drift_hz: float
    drift_step_hz: float
    near_rule: bool
    verdict: str
    reason: str = ""


def plan(receiver: Receiver, steps: int, multiples: int) -> list[Point]:
    """The centre frequencies to measure, the first one first: ``steps``
    1.234 kHz apart, upwards or else downwards, then up to ``multiples``
    whose oscillators lie on multiples of 112.5 kHz, nearest to the first
    one's; Refused when the first sweep fits neither way."""
    first = receiver.centre_hz
    why = ""
    for direction in (1, -1):
        centres = [first + direction * k * STEP_HZ for k in range(steps)]
        problems = [problem for problem in map(receiver.fits, centres) if problem]
        if not problems:
            break
        why = why or problems[0]
    else:
        raise Refused(f"the centre frequency cannot move by {STEP_HZ} Hz {steps - 1} times either way: {why}. "
                      f"Put the VFO further from the centre frequency, within the span.")
    points = [Point(STEP, centre) for centre in centres]
    intermediate = PLL.intermediate_hz(receiver.rate_hz)
    scale = (1 + receiver.ppm / 1e6) / (1 + receiver.whole_ppm / 1e6)
    nearest = round(receiver.oscillator_hz(first) / MULTIPLE_HZ)
    found: list[int] = []
    reach = int(receiver.rate_hz // MULTIPLE_HZ) + 2
    for j in sorted(range(-reach, reach + 1), key=abs):
        if len(found) >= multiples:
            break
        centre = round(((nearest + j) * MULTIPLE_HZ - intermediate) * scale)
        if receiver.fits(centre) is None and centre not in found:
            found.append(centre)
    return points + [Point(MULTIPLE, centre) for centre in sorted(found)]


def schedule(points: Sequence[Point], reference_every: int) -> list[Point]:
    """The measures in their order: the points, with the first centre
    measured again after every ``reference_every`` of them and at the end."""
    reference = Point(REFERENCE, points[0].centre_hz)
    order: list[Point] = [points[0]]
    for index, point in enumerate(points[1:], start=1):
        order.append(point)
        if index % reference_every == 0:
            order.append(reference)
    if order[-1] != reference:
        order.append(reference)
    return order


def remove_drift(measures: list[Measure]) -> None:
    """Set each measure's drift, the reference interpolated at its time, and
    its offset from the first measure without it."""
    references = [measure for measure in measures if measure.point.kind == REFERENCE or measure is measures[0]]
    first = measures[0].carrier_hz
    for measure in measures:
        before = max((ref for ref in references if ref.at <= measure.at), key=lambda ref: ref.at)
        after = min((ref for ref in references if ref.at >= measure.at), key=lambda ref: ref.at, default=before)
        if after.at > before.at:
            share = (measure.at - before.at) / (after.at - before.at)
            reference = before.carrier_hz + share * (after.carrier_hz - before.carrier_hz)
        else:
            reference = before.carrier_hz
        measure.drift_hz = reference - first
        measure.offset_hz = measure.carrier_hz - reference


def analyse(receiver: Receiver, measures: list[Measure]) -> Analysis:
    """The spread, the predictions followed, the drift and the verdict."""
    remove_drift(measures)
    points = [measure for measure in measures if measure.point.kind != REFERENCE]
    offsets = [measure.offset_hz for measure in points]
    spread = max(offsets) - min(offsets)
    residual = {computation: max(abs(measure.offset_hz - receiver.predicted_hz(computation, measure.point.centre_hz))
                                 for measure in points)
                for computation in PLL.COMPUTATIONS}
    follows = [computation for computation, value in residual.items() if value <= FOLLOW_HZ]
    references = [measures[0].carrier_hz] + [measure.carrier_hz for measure in measures[1:]
                                             if measure.point.kind == REFERENCE]
    drift = max(references) - min(references)
    drift_step = max((abs(b - a) for a, b in zip(references, references[1:])), default=0.0)
    intermediate = PLL.intermediate_hz(receiver.rate_hz)
    near_rule = (PLL.mix_divider(receiver.oscillator_hz(receiver.centre_hz))
                 == PLL.mix_divider(RULE_HZ + intermediate))
    analysis = Analysis(spread, residual, follows, references, drift, drift_step, near_rule, "table")
    if drift_step > DRIFT_STEP_HZ:
        analysis.reason = (f"the reference moved {drift_step:.1f} Hz between two of its measures, more than "
                           f"{DRIFT_STEP_HZ:g} Hz: let the receiver warm up and measure again")
    elif not near_rule:
        analysis.reason = ("the rule was fixed near 145 MHz, and this carrier is measured with another divider "
                           "of the tuner")
    elif spread >= BUILD_HZ and follows:
        analysis.verdict = "build"
    elif spread < NOT_OBSERVED_HZ:
        analysis.verdict = "not_observed"
    elif spread >= BUILD_HZ:
        analysis.reason = f"a spread of {BUILD_HZ:g} Hz or more that follows neither prediction"
    else:
        analysis.reason = f"a spread from {NOT_OBSERVED_HZ:g} to {BUILD_HZ:g} Hz"
    return analysis


def table(receiver: Receiver, measures: Sequence[Measure]) -> list[str]:
    """The measures as a Markdown table."""
    columns = ["#", "Kind", "Centre asked (Hz)", "Oscillator (Hz)", "Carrier (Hz)", "Drift (Hz)", "Offset (Hz)",
               *(f"Predicted, {NAMES[computation]} (Hz)" for computation in PLL.COMPUTATIONS)]
    lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for number, measure in enumerate(measures, start=1):
        centre = measure.point.centre_hz
        cells = [str(number), measure.point.kind, str(centre), str(receiver.oscillator_hz(centre)),
                 f"{measure.carrier_hz:.1f}", f"{measure.drift_hz:+.1f}", f"{measure.offset_hz:+.1f}",
                 *(f"{receiver.predicted_hz(computation, centre):+.1f}" for computation in PLL.COMPUTATIONS)]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def describe(receiver: Receiver, started: datetime) -> list[str]:
    intermediate = PLL.intermediate_hz(receiver.rate_hz)
    divider = PLL.mix_divider(receiver.oscillator_hz(receiver.centre_hz))
    return [
        f"PLL measure of {started:%Y-%m-%d %H:%M} UTC: {receiver.tuner}, {receiver.rate_hz} S/s "
        f"(IF {intermediate} Hz), correction {receiver.ppm:g} ppm, VFO at {receiver.vfo_hz} Hz in "
        f"{receiver.mode} with a {receiver.bandwidth_hz:g} Hz filter, first centre {receiver.centre_hz} Hz, "
        f"tuner divider {divider}.",
        "",
        "Oscillator: the one librtlsdr asks of the tuner. Offset: the carrier's distance from its first measure, "
        "without the drift, the first centre measured again at the time, interpolated. "
        f"Predicted: where each computation of the driver moves the carrier; up to 2.0.1: "
        f"{PREDICTIONS['truncating']}; 2.0.2 on: {PREDICTIONS['rounding']}. A 28.8 MHz crystal is assumed.",
        "",
    ]


def conclusion(analysis: Analysis) -> list[str]:
    spread = f"Spread: {analysis.spread_hz:.1f} Hz, the drift taken off."
    references = len(analysis.references)
    drift = (f"Drift: the first centre measured {references} times moved by {analysis.drift_hz:.1f} Hz in all, "
             f"at most {analysis.drift_step_hz:.1f} Hz between two of its measures.")
    followed = [f"{NAMES[computation]} ({PREDICTIONS[computation]}), largest difference "
                f"{analysis.residual_hz[computation]:.1f} Hz" for computation in analysis.follows]
    others = [f"{NAMES[computation]}, largest difference {analysis.residual_hz[computation]:.1f} Hz"
              for computation in PLL.COMPUTATIONS if computation not in analysis.follows]
    follows = (f"Follows, within {FOLLOW_HZ:g} Hz at every point: " + "; ".join(followed) if followed
               else f"Follows neither prediction within {FOLLOW_HZ:g} Hz")
    if others and followed:
        follows += "; not " + "; ".join(others)
    elif others:
        follows += ": " + "; ".join(others)
    verdicts = {
        "build": f"Verdict: a spread of {BUILD_HZ:g} Hz or more near 145 MHz that follows one prediction: "
                 "pull request 14 is built.",
        "not_observed": f"Verdict: a spread under {NOT_OBSERVED_HZ:g} Hz: the PLL's error is recorded as not "
                        "observed, and nothing is built.",
        "table": f"Verdict: neither rule applies, {analysis.reason}: the owner decides from the table.",
    }
    return ["", spread, drift, follows + ".", verdicts[analysis.verdict]]


class Stream:
    """The stream of a Radiolack, read as messages come: the latest states,
    the carrier's measures by id and the errors."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.hello: dict | None = None
        self.states: dict[str, dict] = {}
        self.carriers: dict[int, dict] = {}
        self.errors: list[str] = []
        self.closed = False
        self.guard: Callable[[], None] | None = None     # raises when the settings changed
        self._news = asyncio.Event()

    async def read(self) -> None:
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue        # spectrum and audio frames
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                if kind == "hello":
                    self.hello = data
                elif kind in ("source", "demod", "scanner"):
                    self.states[kind] = data
                elif kind == "carrier" and isinstance(data.get("id"), int):
                    self.carriers[data["id"]] = data
                elif kind == "error":
                    self.errors.append(f"{data.get('message')}")
                self._news.set()
        finally:
            self.closed = True
            self._news.set()

    async def until(self, found: Callable[[], Any], timeout_s: float, what: str) -> Any:
        """``found()``'s first true value, as messages come; Incomplete when
        the stream closes, Radiolack refuses a message, the guard finds the
        settings changed, or nothing comes within ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        while True:
            self._news.clear()
            if self.errors:
                raise Incomplete(f"Radiolack refused a message: {self.errors[0]}")
            if self.guard is not None:
                self.guard()
            value = found()
            if value:
                return value
            if self.closed:
                raise Incomplete("the stream closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Incomplete(f"no answer within {timeout_s:g} s: {what}")
            try:
                await asyncio.wait_for(self._news.wait(), remaining)
            except TimeoutError:
                pass

    async def send(self, message: dict) -> None:
        if self.closed:
            raise Incomplete("the stream closed")
        try:
            await self.ws.send_json(message)
        except (ConnectionError, RuntimeError) as problem:   # closing, or closed meanwhile
            raise Incomplete(f"the stream closed: {problem}") from problem


class Measurement:
    """One run of the measure against the Radiolack on ``port``."""

    def __init__(self, options: argparse.Namespace, out: TextIO, err: TextIO) -> None:
        self.options = options
        self.out = out
        self.err = err
        self.stream: Stream | None = None
        self.reader: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        self.receiver: Receiver | None = None
        self.first_centre_hz: int | None = None     # the centre found, to restore
        self.retuned = False
        self.allowed: set[int] = set()              # centres the source state may show
        self.baseline: tuple = ()                   # the settings that must not change
        self.next_id = 1
        self.measures: list[Measure] = []
        self.started = datetime.now(timezone.utc)

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.out, flush=True)

    def progress(self, line: str) -> None:
        print(line, file=self.err, flush=True)

    async def run(self) -> int:
        self.session = aiohttp.ClientSession()
        interrupted = False
        try:
            try:
                code = await self.measure()
            except asyncio.CancelledError:
                interrupted = True
                code = EXIT_INTERRUPTED
                self.report_incomplete("interrupted")
            except Exception as problem:        # a bug, or a message unlike the protocol's
                code = EXIT_FAILED
                self.report_incomplete(f"unexpected {problem!r}")
            if self.retuned and not await self.restore():
                code = EXIT_FAILED if code == EXIT_VERDICT else code
        finally:
            await self.disconnect()
            await self.session.close()
        if interrupted:
            raise asyncio.CancelledError
        return code

    async def connect(self) -> Stream:
        url = f"http://{HOST}:{self.options.port}"
        try:
            ws = await asyncio.wait_for(self.session.ws_connect(f"{url}/stream", origin=url),
                                        self.options.timeout_s)
        except (aiohttp.ClientError, OSError, TimeoutError) as problem:
            raise Refused(f"no Radiolack answers on {url}: {problem or type(problem).__name__}") from problem
        self.stream = Stream(ws)
        self.reader = asyncio.ensure_future(self.stream.read())
        return self.stream

    async def disconnect(self) -> None:
        if self.stream is not None:
            await self.stream.ws.close()
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)
        self.stream = self.reader = None

    async def measure(self) -> int:
        try:
            stream = await self.connect()
            receiver = await self.start(stream)
            points = plan(receiver, self.options.steps, self.options.multiples)
        except (Refused, Incomplete) as problem:
            self.say(f"Not started: {problem}.")
            return EXIT_FAILED
        order = schedule(points, self.options.reference_every)
        self.say(*describe(receiver, self.started))
        stream.guard = self.check_settings
        try:
            first = await self.measure_at(points[0], attempts=1)
            self.check_filter(first, points)
            self.measures.append(first)
            self.progress(f"1/{len(order)}: {first.point.centre_hz} Hz, carrier at {first.carrier_hz:.1f} Hz")
            for number, point in enumerate(order[1:], start=2):
                measure = await self.measure_at(point, self.options.attempts)
                self.measures.append(measure)
                self.progress(f"{number}/{len(order)}: {point.centre_hz} Hz, carrier at {measure.carrier_hz:.1f} Hz")
        except Refused as problem:
            self.say(f"Not started: {problem}.")
            return EXIT_FAILED
        except Incomplete as problem:
            self.report_incomplete(str(problem))
            return EXIT_FAILED
        analysis = analyse(receiver, self.measures)
        lines = table(receiver, self.measures) + conclusion(analysis)
        self.say(*lines)
        return EXIT_VERDICT if self.write(describe(receiver, self.started) + lines) else EXIT_FAILED

    def report_incomplete(self, why: str) -> None:
        lines = []
        if self.measures and self.receiver is not None:
            remove_drift(self.measures)
            lines = table(self.receiver, self.measures)
        lines += ["", f"Incomplete after {len(self.measures)} measures: {why}. No verdict: measure again."]
        self.say(*lines)
        if self.receiver is not None and self.measures:
            self.write(describe(self.receiver, self.started) + lines)

    def write(self, lines: list[str]) -> bool:
        if self.options.output is None:
            return True
        try:
            self.options.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as problem:
            self.say(f"The table could not be written to {self.options.output}: {problem.strerror or problem}.")
            return False
        self.say(f"Written to {self.options.output}.")
        return True

    async def start(self, stream: Stream) -> Receiver:
        """The receiver's settings, once the stream gave them; Refused when
        they do not allow the measure."""
        timeout = self.options.timeout_s
        await stream.until(lambda: stream.hello and {"source", "demod", "scanner"} <= stream.states.keys(),
                           timeout, "the stream's hello and states")
        hello = stream.hello
        if hello.get("protocol") != PROTOCOL:
            raise Refused(f"this Radiolack speaks protocol {hello.get('protocol')}, the tool {PROTOCOL}")
        if "carrier" not in (hello.get("measures") or []):
            raise Refused("this Radiolack does not measure carriers: it is older than the calibration")
        source = await stream.until(lambda: (state := stream.states["source"]) and state.get("status") == "streaming"
                                    and state, timeout, "a receiver that streams")
        demod, scanner = stream.states["demod"], stream.states["scanner"]
        if source.get("kind") not in ("rtl_sdr", "rtl_tcp"):
            raise Refused("the receiver in use is not an RTL-SDR")
        if source.get("tuner") not in TUNERS:
            raise Refused(f"the tuner is {source.get('tuner')}, not an R820T or an R828D")
        if source.get("direct"):
            raise Refused("the receiver samples directly: the measure needs its tuner")
        if source.get("converter") not in (None, "none"):
            raise Refused("a converter is set: the measure needs the receiver alone")
        if "centre_hz" not in (source.get("settable") or []):
            raise Refused("this interface may not change the centre frequency")
        if scanner.get("scan") is not None:
            raise Refused("a scan runs and moves the centre frequency: stop it first")
        if demod.get("mode") not in CENTRED_MODES:
            raise Refused(f"the mode is {demod.get('mode')}: set AM, NFM or CW, whose filter is centred on the VFO")
        centre, vfo = round(source["centre_hz"]), round(demod["frequency_hz"])
        lowest, highest = source.get("range_hz") or (TUNER_LOW_HZ, 1_766_000_000)
        if source.get("hf_input") == "tuner":
            lowest = max(lowest, V4_UPCONVERSION_HZ)
        if vfo < max(lowest, TUNER_LOW_HZ):
            raise Refused(f"the VFO lies at {vfo} Hz: the measure needs a carrier above {max(lowest, TUNER_LOW_HZ)} Hz")
        ppm = float(source.get("ppm") or 0.0)
        self.receiver = Receiver(centre, round(source["sample_rate_hz"]), ppm, whole_ppm(ppm, True), vfo,
                                 demod["mode"], float(demod["bandwidth_hz"]), source["tuner"], lowest, highest)
        self.first_centre_hz = centre
        self.allowed = {centre}
        self.baseline = (self.source_key(source), self.demod_key(demod))
        problem = self.receiver.fits(centre)
        if problem:
            raise Refused(problem)
        return self.receiver

    @staticmethod
    def source_key(state: dict) -> tuple:
        return tuple(state.get(name) for name in ("kind", "address", "sample_rate_hz", "ppm", "direct",
                                                  "hf_input", "converter", "converter_lo_hz"))

    @staticmethod
    def demod_key(state: dict) -> tuple:
        return tuple(state.get(name) for name in ("frequency_hz", "mode", "bandwidth_hz"))

    def check_settings(self) -> None:
        """Incomplete when the receiver's or the VFO's settings changed, or
        the centre frequency moved, other than by the measure itself."""
        source, demod = self.stream.states["source"], self.stream.states["demod"]
        if (self.source_key(source), self.demod_key(demod)) != self.baseline:
            raise Incomplete("the receiver's or the VFO's settings changed during the measure")
        if round(source.get("centre_hz", 0)) not in self.allowed:
            raise Incomplete(f"the centre frequency moved to {source.get('centre_hz')} Hz, not by the measure")
        if self.stream.states["scanner"].get("scan") is not None:
            raise Incomplete("a scan started during the measure")

    def check_filter(self, first: Measure, points: Sequence[Point]) -> None:
        """Refused when the carrier would leave the filter at a prediction."""
        receiver = self.receiver
        moves = [abs(receiver.predicted_hz(computation, point.centre_hz))
                 for computation in PLL.COMPUTATIONS for point in points]
        needed = abs(first.carrier_hz - receiver.vfo_hz) + max(moves) + FILTER_MARGIN_HZ
        if needed > receiver.bandwidth_hz / 2:
            raise Refused(f"the carrier may move by up to {max(moves):.0f} Hz, beyond the filter: widen it to "
                          f"{2 * needed:.0f} Hz at least, or put the VFO on the carrier")

    async def retune(self, centre_hz: int) -> None:
        stream = self.stream
        if round(stream.states["source"]["centre_hz"]) == centre_hz:
            return
        self.allowed = {round(stream.states["source"]["centre_hz"]), centre_hz}
        self.retuned = True
        await stream.send({"type": "source", "centre_hz": centre_hz})
        await stream.until(lambda: round(stream.states["source"]["centre_hz"]) == centre_hz
                           and stream.states["source"].get("status") == "streaming",
                           self.options.timeout_s, f"the receiver tuned to {centre_hz} Hz")
        self.allowed = {centre_hz}

    async def measure_at(self, point: Point, attempts: int) -> Measure:
        """The carrier measured at ``point``; Refused at the first point and
        Incomplete at the others when ``attempts`` measures in a row fail."""
        await self.retune(point.centre_hz)
        reasons = []
        for _ in range(attempts):
            request = self.next_id
            self.next_id += 1
            await self.stream.send({"type": "carrier", "action": "measure", "id": request})
            answer = await self.stream.until(lambda: self.stream.carriers.pop(request, None),
                                             self.options.timeout_s, "the carrier's measure")
            if answer.get("frequency_hz") is not None:
                return Measure(point, float(answer["frequency_hz"]), float(answer.get("margin_db") or 0.0),
                               time.monotonic())
            margin = answer.get("margin_db")
            reasons.append(f"{answer.get('reason')}" + ("" if margin is None else f" (margin {margin} dB)"))
        why = f"the carrier does not measure cleanly at {point.centre_hz} Hz: {', '.join(reasons)}"
        if not self.measures:
            raise Refused(why + "; put the VFO on one steady carrier, alone in the filter, 20 dB above the noise")
        raise Incomplete(why)

    async def restore(self) -> bool:
        """Put the centre frequency back where it was found, through a new
        connection if the stream closed; False, said, when it cannot."""
        centre = self.first_centre_hz
        try:
            if self.stream is None or self.stream.closed:
                await self.disconnect()
                stream = await self.connect()
                await stream.until(lambda: "source" in stream.states, self.options.timeout_s, "the source state")
            stream = self.stream
            stream.guard = None
            stream.errors.clear()
            if round(stream.states["source"]["centre_hz"]) != centre:
                await stream.send({"type": "source", "centre_hz": centre})
            await stream.until(lambda: round(stream.states["source"]["centre_hz"]) == centre,
                               self.options.timeout_s, f"the receiver tuned back to {centre} Hz")
        except (Refused, Incomplete) as problem:
            self.say(f"The centre frequency could not be restored to {centre} Hz: {problem}. Set it again in "
                     "Radiolack.")
            return False
        self.say(f"Centre frequency restored to {centre} Hz.")
        return True


def port_number(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 1 to 65535")
    return port


def positive(kind: type) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        value = kind(text)
        if not value > 0:
            raise argparse.ArgumentTypeError("must be above 0")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_pll.py",
        description="Measure the error of an RTL-SDR tuner's PLL through a Radiolack already running on the "
                    "receiver. It TUNES THE RECEIVER BEHIND THAT RADIOLACK: it moves the centre frequency "
                    "step by step for a few minutes, then restores it. Before: the receiver streams on its tuner, "
                    "without a converter or a scan, its crystal warmed up; the VFO on one steady carrier above "
                    "24 MHz, alone in its filter and 20 dB above the noise, in AM, NFM or CW with a filter of a "
                    "few kHz.",
        epilog="It connects to 127.0.0.1 only, and changes nothing but the centre frequency. Ctrl-C stops it "
               "and restores the centre frequency; a second Ctrl-C leaves at once. Exit status: 0 with a verdict, "
               "1 without one or when the centre frequency could not be restored, 130 after Ctrl-C.")
    parser.add_argument("port", type=port_number,
                        help="the port of that Radiolack on 127.0.0.1 (its address http://127.0.0.1:PORT/)")
    parser.add_argument("--output", type=Path, metavar="FILE",
                        help="also write the table and the verdict to FILE, in Markdown")
    parser.add_argument("--steps", type=positive(int), default=STEPS,
                        help=f"centre frequencies {STEP_HZ} Hz apart, the first one included (default {STEPS})")
    parser.add_argument("--multiples", type=positive(int), default=MULTIPLES,
                        help=f"centre frequencies whose oscillator is a multiple of {MULTIPLE_HZ} Hz "
                             f"(default {MULTIPLES})")
    parser.add_argument("--reference-every", type=positive(int), default=REFERENCE_EVERY,
                        help=f"measure the first centre again after this many points, and at the end "
                             f"(default {REFERENCE_EVERY})")
    parser.add_argument("--attempts", type=positive(int), default=ATTEMPTS,
                        help=f"measures asked at a point before the carrier counts as lost (default {ATTEMPTS})")
    parser.add_argument("--timeout-s", type=positive(float), default=TIMEOUT_S,
                        help=f"longest wait for a retune or a measure, in seconds (default {TIMEOUT_S:g})")
    return parser


async def run(argv: Sequence[str], out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    """Measure as ``argv`` asks; the exit code: 0 with a verdict, 1 without
    one or when the centre frequency could not be restored."""
    options = build_parser().parse_args(argv)
    return await Measurement(options, out, err).run()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
