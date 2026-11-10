"""Synthetic I/Q source: a simulated 2 m band for tests and demonstrations.

The band holds FM repeaters and simplex contacts, APRS bursts, CW beacons, an
SSB contact and a satellite beacon with Doppler shift, over white noise, on
generic IARU Region 1 band-plan channels. Samples are computed in memory from
the sample time and paced in real time like a receiver; nothing is recorded.
"""

from __future__ import annotations

import asyncio
import bisect
import cmath
import math
from dataclasses import dataclass

import numpy as np

from .engine import FixedSource

CENTRE_HZ = 145_000_000.0
SAMPLE_RATE_HZ = 2_400_000.0
NOISE_DBFS = -34.0          # noise power over the whole band
RAMP_S = 0.005              # rise and fall time of every transmission
CTCSS_DEVIATION_HZ = 300.0
_NOISE_POOL = 1 << 19       # samples of precomputed noise (0.22 s at 2.4 MS/s)
_MAX_PERIOD = 480_000      # longest modulation table, in samples
_MAX_LAG_S = 0.25           # a reader later than this restarts the pacing from now
_TWO_PI = np.float32(2 * np.pi)
_MORSE = {"V": "...-"}


@dataclass(frozen=True)
class Schedule:
    """Transmissions as sorted, disjoint [start, stop) intervals in seconds,
    repeated every ``period_s``."""

    intervals: tuple[tuple[float, float], ...]
    period_s: float

    def spans(self, t0: float, t1: float) -> list[tuple[float, float]]:
        """Absolute intervals that overlap [t0, t1)."""
        stops = [stop for _, stop in self.intervals]
        found = []
        for cycle in range(math.floor(t0 / self.period_s), math.floor(t1 / self.period_s) + 1):
            base = cycle * self.period_s
            index = bisect.bisect_right(stops, t0 - base)
            while index < len(self.intervals) and base + self.intervals[index][0] < t1:
                start, stop = self.intervals[index]
                found.append((base + start, base + stop))
                index += 1
        return found


@dataclass(frozen=True)
class Emitter:
    """One simulated station.

    ``kind`` is "fm" (``tones`` are audio frequencies and peak deviations in
    Hz), "usb" (``tones`` are audio frequencies and relative amplitudes) or
    "cw" (no tones). ``phases`` holds one start phase per tone, in cycles.
    """

    frequency_hz: float
    level_dbfs: float           # power while transmitting
    schedule: Schedule
    kind: str = "fm"
    tones: tuple[tuple[float, float], ...] = ()
    phases: tuple[float, ...] = ()
    syllabic: bool = False      # voice-like level changes
    ctcss_hz: float | None = None
    doppler_hz: float = 0.0     # shift from +doppler to -doppler over each transmission


class SyntheticBand(FixedSource):
    """Simulated band read like a receiver: complex64 samples in full-scale units.
    Its centre frequency and sample rate cannot change."""

    kind = "synthetic"

    def __init__(self, seed: int | None = None, *, centre_hz: float = CENTRE_HZ,
                 sample_rate_hz: float = SAMPLE_RATE_HZ,
                 emitters: list[Emitter] | None = None,
                 noise_dbfs: float | None = NOISE_DBFS) -> None:
        self.centre_hz = float(centre_hz)
        self.sample_rate_hz = float(sample_rate_hz)
        self._rng = np.random.default_rng(seed)
        self.emitters = list(emitters) if emitters is not None else default_scene(self._rng)
        self._noise: np.ndarray | None = None   # None: no noise at all
        if noise_dbfs is not None:
            sigma = math.sqrt(10 ** (noise_dbfs / 10) / 2)
            noise = self._rng.standard_normal(2 * _NOISE_POOL, dtype=np.float32) * np.float32(sigma)
            self._noise = noise.view(np.complex64)
        ramp = max(1, round(RAMP_S * self.sample_rate_hz))
        self._rise = (0.5 - 0.5 * np.cos(np.pi * (np.arange(ramp) + 0.5) / ramp)).astype(np.float32)
        self._fall = self._rise[::-1].copy()
        self._size = 0                          # block size the tables below are for
        self._seconds = self._seconds32 = np.empty(0)
        self._tables: list[_Tables] = []
        self._position = 0                      # samples produced since the start
        self._anchor: float | None = None       # event-loop time of sample 0

    def generate(self, count: int) -> np.ndarray:
        """The next ``count`` samples, continuing the previous ones."""
        if not 0 < count <= _NOISE_POOL:
            raise ValueError(f"cannot generate {count} samples at once (1 to {_NOISE_POOL})")
        if count != self._size:
            self._seconds = np.arange(count) / self.sample_rate_hz
            self._seconds32 = self._seconds.astype(np.float32)
            self._tables = [self._prepare(emitter, count) for emitter in self.emitters]
            self._size = count
        if self._noise is None:
            block = np.zeros(count, np.complex64)
        else:
            start = int(self._rng.integers(0, _NOISE_POOL - count + 1))
            block = self._noise[start:start + count].copy()
        for emitter, tables in zip(self.emitters, self._tables):
            self._render(emitter, tables, block)
        self._position += count
        return block

    async def read(self, count: int) -> np.ndarray:
        """Like ``generate``, returning once a receiver would have delivered those samples."""
        now = asyncio.get_running_loop().time()
        if self._anchor is None or now - self._anchor - self._position / self.sample_rate_hz > _MAX_LAG_S:
            self._anchor = now - self._position / self.sample_rate_hz
        due = self._anchor + (self._position + count) / self.sample_rate_hz
        if due > now:
            await asyncio.sleep(due - now)
        return await asyncio.to_thread(self.generate, count)

    # ------------------------------------------------------------------ rendering

    def _prepare(self, emitter: Emitter, count: int) -> _Tables:
        """Precompute the emitter's carrier and modulation for blocks of ``count`` samples."""
        tables = _Tables()
        rate = self.sample_rate_hz
        if emitter.doppler_hz or abs(emitter.frequency_hz - self.centre_hz) >= rate / 2:
            return tables                       # computed for each block, or never heard
        cycles = (np.arange(count) * ((emitter.frequency_hz - self.centre_hz) / rate)) % 1.0
        if emitter.kind == "fm":
            tables.phase = (2 * np.pi * cycles).astype(np.float32)
        else:
            tables.carrier = np.exp(2j * np.pi * cycles).astype(np.complex64)
        period = _period([frequency for frequency, _ in emitter.tones], rate)
        if period and emitter.kind in ("fm", "usb"):
            # One period of the modulation, extended by a block so that any
            # block is a contiguous slice.
            seconds = np.arange(period + count) / rate
            if emitter.kind == "fm":
                tables.tones = _sines(_tone_phases(emitter), seconds, 0.0, _indices(emitter))
            else:
                tables.tones = _sideband(emitter, seconds, 0.0)
            tables.period = period
        return tables

    def _render(self, emitter: Emitter, tables: _Tables, block: np.ndarray) -> None:
        rate = self.sample_rate_hz
        offset_hz = emitter.frequency_hz - self.centre_hz
        if abs(offset_hz) >= rate / 2:
            return
        n = len(block)
        t0 = self._position / rate
        spans = emitter.schedule.spans(t0, t0 + n / rate)
        if not spans:
            return
        gain = self._envelope(spans, t0, n) * np.float32(10 ** (emitter.level_dbfs / 20))
        start = math.fmod(offset_hz * t0, 1.0)  # carrier phase at the block start, in cycles
        if emitter.doppler_hz:
            # Drifts linearly from +doppler to -doppler across the transmission.
            first, last = spans[0]
            elapsed = self._seconds + (t0 - first)
            cycles = self._seconds * offset_hz + start
            cycles += emitter.doppler_hz * (elapsed - elapsed * elapsed / (last - first))
            cycles -= np.floor(cycles)
            phase = (2 * np.pi * cycles).astype(np.float32)
        elif emitter.kind == "fm":
            phase = tables.phase + np.float32(2 * np.pi * start)
            phase += self._modulation(emitter, tables, t0)
        else:
            wave = tables.carrier * np.complex64(cmath.exp(2j * math.pi * start))
            if emitter.kind == "usb":
                wave *= self._sideband(emitter, tables, t0)
            block += gain * wave
            return
        block.real += gain * np.cos(phase)
        block.imag += gain * np.sin(phase)

    def _envelope(self, spans: list[tuple[float, float]], t0: float, n: int) -> np.ndarray | np.float32:
        """Keying envelope of the block: raised-cosine edges, 1 while on."""
        rate, ramp = self.sample_rate_hz, len(self._rise)
        edges = [(round((start - t0) * rate), round((stop - t0) * rate)) for start, stop in spans]
        if len(edges) == 1 and edges[0][0] + ramp <= 0 and edges[0][1] - ramp >= n:
            return np.float32(1)
        envelope = np.zeros(n, np.float32)
        for start, stop in edges:
            lo, hi = max(start, 0), min(stop, n)
            if lo >= hi:
                continue
            envelope[lo:hi] = 1
            a, b = lo, min(start + ramp, hi)
            if a < b:
                envelope[a:b] = self._rise[a - start:b - start]
            a, b = max(stop - ramp, lo), hi
            if a < b:
                fall = self._fall[a - stop + ramp:b - stop + ramp]
                envelope[a:b] = np.minimum(envelope[a:b], fall)
        return envelope

    def _slice(self, tables: _Tables) -> np.ndarray:
        """The current block's part of a precomputed modulation (a read-only view)."""
        first = self._position % tables.period
        return tables.tones[first:first + self._size]

    def _modulation(self, emitter: Emitter, tables: _Tables, t0: float) -> np.ndarray:
        """FM phase deviation in radians: the voice tones, then the CTCSS tone."""
        if tables.period:
            phase = self._slice(tables)
        else:
            phase = _sines(_tone_phases(emitter), self._seconds32, t0, _indices(emitter))
        if emitter.syllabic:
            phase = phase * self._syllables(emitter, t0)
        if emitter.ctcss_hz:
            phase = phase + _sines([(emitter.ctcss_hz, 0.0)], self._seconds32, t0,
                                   [CTCSS_DEVIATION_HZ / emitter.ctcss_hz])
        return phase

    def _sideband(self, emitter: Emitter, tables: _Tables, t0: float) -> np.ndarray:
        """Complex baseband of the upper sideband carrying the tones."""
        base = self._slice(tables) if tables.period else _sideband(emitter, self._seconds32, t0)
        if emitter.syllabic:
            base = base * self._syllables(emitter, t0)
        return base

    def _syllables(self, emitter: Emitter, t0: float) -> np.ndarray:
        """Slow voice-like gain between 0.24 and 1, linear across the block."""
        def gain(t: float) -> float:
            seed = emitter.frequency_hz * 1e-3
            return 0.62 + 0.38 * math.sin(2 * math.pi * 2.1 * t + seed) * math.sin(2 * math.pi * 0.43 * t + 2 * seed)

        duration = self._size / self.sample_rate_hz
        first, last = gain(t0), gain(t0 + duration)
        return np.float32(first) + self._seconds32 * np.float32((last - first) / duration)


@dataclass
class _Tables:
    """Waveforms of one emitter precomputed for one block size."""

    phase: np.ndarray | None = None     # FM carrier phase from the block start, radians
    carrier: np.ndarray | None = None   # exp(j phase), for the other kinds
    tones: np.ndarray | None = None     # modulation over one period plus a block
    period: int = 0                     # samples after which the modulation repeats


def _period(frequencies: list[float], rate: float) -> int:
    """Samples after which tones at whole frequencies repeat, if few enough; else 0."""
    if not frequencies or not all(frequency > 0 and float(frequency).is_integer() for frequency in frequencies):
        return 0
    common = 0
    for frequency in frequencies:
        common = math.gcd(common, int(frequency))
    period = rate / common
    return int(period) if period.is_integer() and period <= _MAX_PERIOD else 0


def _sines(tones, seconds: np.ndarray, t0: float, weights, shift: float = 0.0) -> np.ndarray:
    """Sum of ``weight * sin(2 pi (frequency (t0 + t) + phase + shift))`` over
    (frequency, phase) tones, at the times ``t`` of ``seconds`` (float32 for a
    block, float64 for a long table)."""
    total = np.zeros(len(seconds), np.float32)
    for (frequency, start), weight in zip(tones, weights):
        cycles = seconds * frequency + math.fmod(frequency * t0 + start + shift, 1.0)
        if cycles.dtype != np.float32:
            cycles = (cycles % 1.0).astype(np.float32)
        total += np.float32(weight) * np.sin(_TWO_PI * cycles)
    return total


def _sideband(emitter: Emitter, seconds: np.ndarray, t0: float) -> np.ndarray:
    """Upper sideband baseband carrying the emitter's tones, unit total power."""
    tones = _tone_phases(emitter)
    norm = 1 / math.sqrt(sum(value * value for _, value in emitter.tones))
    amplitudes = [value * norm for _, value in emitter.tones]
    base = np.empty(len(seconds), np.complex64)
    # exp(j 2 pi f t): the cosine is the sine a quarter cycle ahead.
    base.real = _sines(tones, seconds, t0, amplitudes, shift=0.25)
    base.imag = _sines(tones, seconds, t0, amplitudes)
    return base


def _tone_phases(emitter: Emitter) -> list[tuple[float, float]]:
    """(frequency, start phase in cycles) of each tone."""
    return [(frequency, start) for (frequency, _), start in zip(emitter.tones, emitter.phases)]


def _indices(emitter: Emitter) -> list[float]:
    """FM modulation index of each tone: peak deviation over frequency."""
    return [deviation / frequency for frequency, deviation in emitter.tones]


# ---------------------------------------------------------------------- scene

def morse(text: str, wpm: float, carrier_s: float, pause_s: float) -> Schedule:
    """A beacon: ``text`` in Morse code, a long carrier, then a pause."""
    unit = 1.2 / wpm
    intervals = []
    t = 0.0
    for index, letter in enumerate(text):
        if index:
            t += 2 * unit       # three units between letters
        for element in _MORSE[letter]:
            length = unit if element == "." else 3 * unit
            intervals.append((t, t + length))
            t += length + unit
    t += 2 * unit
    intervals.append((t, t + carrier_s))
    return Schedule(tuple(intervals), t + carrier_s + pause_s)


def traffic(rng: np.random.Generator, period_s: float, *, overs=(2, 7), over_s=(4.0, 18.0),
            gap_s=(1.5, 5.0), quiet_s=(15.0, 80.0)) -> Schedule:
    """Contacts: a few overs separated by short gaps, then a quiet spell."""
    intervals = []
    t = float(rng.uniform(1.0, quiet_s[1]))
    while True:
        for _ in range(int(rng.integers(overs[0], overs[1] + 1))):
            length = float(rng.uniform(*over_s))
            if t + length > period_s - 1.0:
                return Schedule(tuple(intervals), period_s)
            intervals.append((t, t + length))
            t += length + float(rng.uniform(*gap_s))
        t += float(rng.uniform(*quiet_s))


def default_scene(rng: np.random.Generator) -> list[Emitter]:
    """The simulated band: generic channels of the IARU Region 1 2 m band plan."""
    def voice(frequency, level, schedule, ctcss=None):
        tones = ((330.0, 900.0), (570.0, 700.0), (910.0, 450.0))
        return Emitter(frequency, level, schedule, "fm", tones, tuple(rng.random(3)), True, ctcss)

    def beacon(frequency, level, wpm, carrier_s):
        return Emitter(frequency, level, morse("VVV", wpm, carrier_s, 1.0), "cw")

    return [
        voice(145_600_000.0, -25.0, traffic(rng, 900.0), 123.0),     # repeater outputs
        voice(145_637_500.0, -36.0, traffic(rng, 900.0), 67.0),
        voice(145_712_500.0, -47.0, traffic(rng, 900.0), 88.5),
        voice(145_500_000.0, -40.0, traffic(rng, 900.0, quiet_s=(40.0, 160.0))),   # simplex
        voice(145_400_000.0, -45.0, traffic(rng, 900.0, quiet_s=(60.0, 200.0))),
        Emitter(144_800_000.0, -35.0,                                             # APRS
                traffic(rng, 600.0, overs=(1, 1), over_s=(0.4, 0.8), quiet_s=(6.0, 30.0)),
                "fm", ((1200.0, 1500.0), (2200.0, 1000.0)), tuple(rng.random(2))),
        beacon(144_415_000.0, -56.0, 12.0, 6.0),                                  # beacons
        beacon(144_440_000.0, -63.0, 15.0, 8.0),
        beacon(144_465_000.0, -52.0, 10.0, 5.0),
        Emitter(144_300_000.0, -44.0,                                             # SSB contact
                traffic(rng, 900.0, over_s=(6.0, 20.0), quiet_s=(30.0, 120.0)),
                "usb", ((450.0, 1.0), (900.0, 0.8), (1500.0, 0.6), (2100.0, 0.4)),
                tuple(rng.random(4)), True),
        Emitter(145_950_000.0, -58.0, Schedule(((60.0, 540.0),), 1200.0), "cw",   # satellite
                doppler_hz=3000.0),
    ]
