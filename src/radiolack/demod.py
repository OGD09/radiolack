"""Demodulation of one channel: the VFO's signal, its level, the squelch and
its audio at 12 kHz, in NFM, AM, USB, LSB and CW.

The receiver's samples go through three FFT filters (dsp.FftFilter). The
front filter brings the VFO's frequency to 0 Hz, or in CW to the CW pitch,
and the rate down to 48 kHz; the channel filter keeps the mode's passband,
whose power is the channel level; after the mode's detector, the audio
filter keeps the audio band and brings the rate down to 12 kHz. NFM goes
through the FM discriminator, then a de-emphasis and a high-pass filter
against CTCSS tones; AM through an envelope detector that follows the
carrier's level; USB, LSB and CW through the channel's real part, which is
the sideband's audio, under an AGC. The squelch compares the channel level
with a threshold, set by hand or, automatically, 8 dB above the noise that
the spectrum shows around the VFO; it opens and closes in 5 ms fades, in
step with the audio.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsp import FftFilter, lowpass, rate_ratio, shaped

MODES = ("NFM", "AM", "USB", "LSB", "CW")
AUDIO_MODES = MODES                 # every mode plays
CHANNEL_RATE_HZ = 48_000.0
AUDIO_DECIMATION = 4                # 12 kHz audio
BANDWIDTH_RANGE_HZ = (100.0, 25_000.0)
SQUELCH_RANGE_DB = (-160.0, 0.0)
SQUELCH_MARGIN_DB = 8.0             # automatic squelch: noise + 8 dB
SQUELCH_HYSTERESIS_DB = 3.0         # closes this far below the threshold
SQUELCH_WINDOW_S = 0.010            # channel power averaged over 10 ms
FADE_S = 0.005
MAX_DEVIATION_HZ = 5_000.0          # NFM: full-scale audio
DEEMPHASIS_S = 750e-6               # NFM de-emphasis, -6 dB per octave above 212 Hz
DEEMPHASIS_REFERENCE_HZ = 1_000.0   # unit gain there
NFM_HIGH_PASS_HZ = 280.0            # removes CTCSS tones (up to 250.3 Hz)
NFM_LOW_PASS_HZ = 3_500.0
AUDIO_HIGH_PASS_HZ = 100.0          # AM, SSB and CW: no hum, nothing left of a carrier
AUDIO_LOW_PASS_HZ = 5_000.0         # AM, SSB and CW: below the 6 kHz that 12 kHz audio holds
AUDIO_TRANSITION_HZ = 60.0
AUDIO_ATTENUATION_DB = 45.0
FRONT_CUTOFF_HZ = 20_000.0          # flat to 16 kHz, -70 dB from 24 kHz
FRONT_TRANSITION_HZ = 8_000.0
CHANNEL_ATTENUATION_DB = 70.0
CW_PITCH_HZ = 700.0                 # a carrier on the VFO sounds at this pitch in CW
AM_WINDOW_S = 0.010                 # AM: the carrier's level, over the 10 ms before and after each sample
AM_LEVEL = 0.8                      # AM: audio amplitude at 100% modulation
AGC_LEVEL = 0.4                     # SSB and CW: audio amplitude of a steady signal
AGC_DECAY_DB_S = 10.0               # SSB and CW: how fast the gain comes back after a signal (slow AGC)
AGC_FLOOR_DB = -200.0               # SSB and CW: the AGC's level before any signal

# Audio frame header, little-endian: kind, version, reserved, position of the
# first sample (counted at the audio rate, modulo 2^32), audio rate (Hz);
# 16-bit signed samples follow.
AUDIO_HEADER = struct.Struct("<BBHId")
FRAME_KIND_AUDIO = 2
AUDIO_VERSION = 1


@dataclass(frozen=True)
class DemodSettings:
    """The receiver's demodulation settings, shared by every interface."""

    frequency_hz: float
    mode: str = "NFM"
    bandwidth_hz: float = 10_000.0
    squelch_db: float = -80.0       # the threshold set by hand
    squelch_auto: bool = True

    def changed(self, changes: Mapping[str, object]) -> DemodSettings:
        """These settings with ``changes`` applied; raises ValueError, naming
        the first refused one, when any is."""
        values: dict[str, Any] = {}
        for name, value in changes.items():
            if name == "frequency_hz":
                values[name] = float(round(_number(value, "the VFO frequency", 1.0, 1e11)))
            elif name == "mode":
                if value not in MODES:
                    raise ValueError(f"the mode must be one of {', '.join(MODES)}")
                values[name] = value
            elif name == "bandwidth_hz":
                values[name] = _number(value, "the filter width", *BANDWIDTH_RANGE_HZ)
            elif name == "squelch_db":
                values[name] = _number(value, "the squelch threshold", *SQUELCH_RANGE_DB)
            elif name == "squelch_auto":
                if not isinstance(value, bool):
                    raise ValueError("the automatic squelch must be true or false")
                values[name] = value
            else:
                raise ValueError(f"unknown demodulator setting: {name}")
        return dataclasses.replace(self, **values)

    def describe(self) -> dict[str, Any]:
        return {"frequency_hz": self.frequency_hz, "mode": self.mode,
                "bandwidth_hz": self.bandwidth_hz, "squelch_db": self.squelch_db,
                "squelch_auto": self.squelch_auto}


def _number(value: object, what: str, low: float, high: float) -> float:
    # Only a float can be NaN or infinite; a whole number too large for a float
    # fails the range check, which compares it as it is, before any conversion.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f"{what} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{what} must be between {low:g} and {high:g}")
    return float(value)


@dataclass(frozen=True)
class Meter:
    """The channel's state for one block of samples."""

    level_db: float | None      # channel power in dBFS; None: nothing received there
    squelch_db: float | None    # threshold in use; None: no noise estimate yet
    open: bool


def vfo_position_hz(mode: str) -> float:
    """Where the front filter puts the VFO's frequency in the channel: 0 Hz,
    or in CW the CW pitch, so that a carrier on the VFO sounds at that pitch."""
    return CW_PITCH_HZ if mode == "CW" else 0.0


def channel_taps(mode: str, bandwidth_hz: float, rate_hz: float) -> np.ndarray:
    """The channel filter: the mode's passband around the VFO, at ``rate_hz``.
    USB keeps [0, width] above the VFO, LSB [-width, 0] below it, the other
    modes [-width / 2, width / 2] around it; in CW the passband moves with
    the VFO to the CW pitch."""
    transition = min(max(0.1 * bandwidth_hz, 100.0), 1_500.0)
    taps = lowpass(bandwidth_hz / 2, transition, rate_hz, CHANNEL_ATTENUATION_DB).astype(np.complex128)
    centre = vfo_position_hz(mode) + {"USB": bandwidth_hz / 2, "LSB": -bandwidth_hz / 2}.get(mode, 0.0)
    if centre:
        taps *= np.exp(2j * np.pi * centre / rate_hz * (np.arange(len(taps)) - (len(taps) - 1) / 2))
    return taps


def audio_taps(mode: str, rate_hz: float) -> np.ndarray:
    """The audio filter at the channel rate. NFM: de-emphasis with unit gain
    at 1 kHz, high-pass at 280 Hz and low-pass at 3.5 kHz. The other modes:
    high-pass at 100 Hz and low-pass at 5 kHz."""
    if mode == "NFM":
        corner = 1 / (2 * math.pi * DEEMPHASIS_S)

        def response(frequency: np.ndarray) -> np.ndarray:
            deemphasis = np.sqrt((1 + (DEEMPHASIS_REFERENCE_HZ / corner) ** 2) / (1 + (frequency / corner) ** 2))
            return deemphasis * ((frequency >= NFM_HIGH_PASS_HZ) & (frequency <= NFM_LOW_PASS_HZ))
    else:
        def response(frequency: np.ndarray) -> np.ndarray:
            return ((frequency >= AUDIO_HIGH_PASS_HZ) & (frequency <= AUDIO_LOW_PASS_HZ)).astype(np.float64)

    return shaped(response, AUDIO_TRANSITION_HZ, rate_hz, AUDIO_ATTENUATION_DB)


class Demodulator:
    """Turns blocks of receiver samples into a channel level, a squelch
    state and audio. Not thread-safe: one block at a time."""

    def __init__(self) -> None:
        self._rate_hz: float | None = None
        self._front: FftFilter | None = None
        self._channel: FftFilter | None = None
        self._channel_key: tuple[str, float, float] | None = None
        self._noise_bandwidth_hz = 0.0
        self.channel_rate_hz = CHANNEL_RATE_HZ
        self._window = 1
        self._powers = np.zeros(0)          # the last channel powers, for the moving average
        self._open = False
        self._audio: _AudioPath | None = None
        self.position = 0                   # audio samples sent, modulo 2^32

    @property
    def audio_rate_hz(self) -> float:
        return self.channel_rate_hz / AUDIO_DECIMATION

    def process(self, samples: np.ndarray, centre_hz: float, rate_hz: float, settings: DemodSettings,
                noise_density: float | None, audio: bool) -> tuple[Meter, np.ndarray | None]:
        """The meter for ``samples``, received at ``centre_hz`` and ``rate_hz``,
        and with ``audio`` the 16-bit audio they make. ``noise_density`` is
        the noise power per hertz around the VFO, for the automatic squelch.
        A VFO outside the received band reads nothing and stays silent; a
        receiver slower than the channel rate gives no meter and no audio."""
        if rate_hz != self._rate_hz:
            self._start(rate_hz)
        if self._front is None or self._channel is None:
            return Meter(None, None, False), None       # too slow a receiver for a channel
        shift_hz = settings.frequency_hz - centre_hz
        self._front.set_shift(shift_hz - vfo_position_hz(settings.mode))
        key = (settings.mode, settings.bandwidth_hz, self.channel_rate_hz)
        if key != self._channel_key:
            taps = channel_taps(settings.mode, settings.bandwidth_hz, self.channel_rate_hz)
            self._channel.set_taps(taps)
            self._channel_key = key
            self._noise_bandwidth_hz = float(np.sum(np.abs(taps) ** 2)) * self.channel_rate_hz
        channel = self._channel.process(self._front.process(samples))
        if not abs(shift_hz) <= rate_hz / 2:
            channel = np.zeros_like(channel)    # the VFO lies outside the received band

        power = channel.real.astype(np.float64) ** 2 + channel.imag.astype(np.float64) ** 2
        mean = float(power.mean()) if len(power) else 0.0
        level_db = 10 * math.log10(mean) if mean > 1e-20 else None
        if settings.squelch_auto:
            threshold = (10 * math.log10(noise_density * self._noise_bandwidth_hz) + SQUELCH_MARGIN_DB
                         if noise_density else None)
        else:
            threshold = settings.squelch_db
        gate = self._squelch(power, threshold)
        meter = Meter(level_db, threshold, self._open)

        if not audio:
            self._audio = None
            return meter, None
        if self._audio is None or self._audio.mode != settings.mode:
            self._audio = _AudioPath(settings.mode, self.channel_rate_hz)
        sound = self._audio.process(channel, power, gate)
        pcm = np.clip(np.rint(sound * 32767), -32768, 32767).astype("<i2")
        return meter, pcm

    def encode_audio(self, pcm: np.ndarray) -> bytes:
        """An audio frame (docs/design/protocol.md) for samples that follow the previous ones."""
        frame = AUDIO_HEADER.pack(FRAME_KIND_AUDIO, AUDIO_VERSION, 0, self.position, self.audio_rate_hz)
        self.position = (self.position + len(pcm)) & 0xFFFFFFFF
        return frame + pcm.tobytes()

    def _start(self, rate_hz: float) -> None:
        """New receiver rate: a new front filter, and new channel and audio
        filters if the channel rate changes with it. A receiver slower than
        the channel rate gives no channel at all."""
        self._rate_hz = rate_hz
        if not rate_hz >= CHANNEL_RATE_HZ:
            self._front = None
            self._open = False
            self._audio = None
            return
        up, down = rate_ratio(rate_hz, CHANNEL_RATE_HZ)
        front = lowpass(FRONT_CUTOFF_HZ, FRONT_TRANSITION_HZ, rate_hz, CHANNEL_ATTENUATION_DB)
        self._front = FftFilter(front, rate_hz, up=up, down=down)
        channel_rate = self._front.output_rate_hz
        if self._channel is None or channel_rate != self.channel_rate_hz:
            self.channel_rate_hz = channel_rate
            self._channel = FftFilter(np.ones(1), channel_rate)
            self._channel_key = None
            self._window = max(1, round(SQUELCH_WINDOW_S * channel_rate))
            self._powers = np.zeros(self._window - 1)
            self._audio = None

    def _squelch(self, power: np.ndarray, threshold_db: float | None) -> np.ndarray:
        """Open (1) or closed (0) at each channel sample: it opens once the
        power averaged over 10 ms reaches the threshold, and closes once that
        average falls ``SQUELCH_HYSTERESIS_DB`` below it. Without a
        threshold it stays closed."""
        window = self._window
        history = np.concatenate((self._powers, power))
        self._powers = history[len(history) - (window - 1):]
        if threshold_db is None:
            self._open = False
        if threshold_db is None or not len(power):
            return np.full(len(power), float(self._open))
        sums = np.cumsum(np.concatenate(([0.0], history)))
        averages = (sums[window:] - sums[:-window]) / window
        # +1 where the average opens the squelch, 0 where it closes it,
        # -1 in between, where the previous state holds.
        events = np.full(len(power), -1)
        events[averages >= 10 ** (threshold_db / 10)] = 1
        events[averages < 10 ** ((threshold_db - SQUELCH_HYSTERESIS_DB) / 10)] = 0
        latest = np.maximum.accumulate(np.where(events >= 0, np.arange(len(power)), -1))
        gate = np.where(latest >= 0, events[latest], int(self._open)).astype(np.float64)
        self._open = bool(gate[-1])
        return gate


class _AudioPath:
    """A mode's detector, the squelch gate and the audio filter: the channel
    rate in, the audio rate out.

    The gate acts on the detector's output, before the audio filter, whose
    long response would otherwise spread the noise around a closing squelch.
    Its decision averages the last 10 ms, so the detector's output waits that
    long, plus a fade, for the gate to look ahead: a sample passes only if
    the squelch stays open until the decisions about it are in, and the gate
    fades out before the noise that follows a carrier. The AM detector and
    the AGC use that wait to look ahead too."""

    def __init__(self, mode: str, channel_rate_hz: float) -> None:
        self.mode = mode
        window = max(1, round(SQUELCH_WINDOW_S * channel_rate_hz))
        self._fade = max(1, round(FADE_S * channel_rate_hz))
        self._span = window + self._fade
        self._detector: _FmDetector | _AmDetector | _SidebandDetector
        if mode == "NFM":
            self._detector = _FmDetector(channel_rate_hz, self._span)
        elif mode == "AM":
            self._detector = _AmDetector(channel_rate_hz, self._span)
        else:
            self._detector = _SidebandDetector(channel_rate_hz, self._span, window)
        self._filter = FftFilter(audio_taps(mode, channel_rate_hz), channel_rate_hz, up=1, down=AUDIO_DECIMATION)
        self._decisions = np.zeros(self._span)      # the squelch's last decisions
        self._passed = np.zeros(self._fade - 1)     # the gate's last values, for the fades

    def process(self, channel: np.ndarray, power: np.ndarray, gate: np.ndarray) -> np.ndarray:
        """Audio for ``channel``, whose power at each sample is ``power``,
        silent where ``gate``, the squelch's decision at each channel sample,
        is closed."""
        detected = self._detector.process(channel, power)
        # A detected sample passes if every decision from its own to the latest is open.
        span = self._span
        decisions = np.concatenate((self._decisions, gate))
        self._decisions = decisions[len(gate):]
        sums = np.cumsum(np.concatenate(([0.0], decisions)))
        passed = (sums[span + 1:] - sums[:-(span + 1)]) > span + 0.5
        # A moving average of the gate turns its steps into linear fades.
        recent = np.concatenate((self._passed, passed))
        self._passed = recent[len(recent) - (self._fade - 1):]
        sums = np.cumsum(np.concatenate(([0.0], recent)))
        fades = (sums[self._fade:] - sums[:-self._fade]) / self._fade
        gated = detected * fades
        return self._filter.process(gated.astype(np.complex64)).real.astype(np.float64)


class _Delay:
    """Real samples, given back ``samples`` later."""

    def __init__(self, samples: int) -> None:
        self._waiting = np.zeros(samples)

    def process(self, values: np.ndarray) -> np.ndarray:
        both = np.concatenate((self._waiting, values))
        self._waiting = both[len(values):]
        return both[:len(values)]


class _FmDetector:
    """FM discriminator: the phase step from one channel sample to the next,
    full scale at 5 kHz of deviation, ``delay`` samples late."""

    def __init__(self, channel_rate_hz: float, delay: int) -> None:
        self._scale = channel_rate_hz / (2 * math.pi * MAX_DEVIATION_HZ)
        self._previous = np.complex64(0)
        self._delay = _Delay(delay)

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        previous = np.concatenate(([self._previous], channel[:-1])) if len(channel) else channel
        if len(channel):
            self._previous = channel[-1]
        return self._delay.process(np.angle(channel * np.conj(previous)) * self._scale)


class _AmDetector:
    """Envelope detector, ``delay`` samples late. The envelope over the
    carrier's level, less one, is the modulation, whatever the signal's
    strength: AM needs no other AGC, and the carrier never reaches the
    squelch's gate, which would make it thump. The carrier's level is the
    higher of the envelope's averages over the 10 ms before and the 10 ms
    after each sample, which the delay makes available: where a carrier
    starts or stops, one of them still holds all of it."""

    def __init__(self, channel_rate_hz: float, delay: int) -> None:
        self._window = min(max(1, round(AM_WINDOW_S * channel_rate_hz)), delay)
        self._before = np.zeros(self._window + delay)   # the envelope's last samples

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        count, window = len(channel), self._window
        envelope = np.concatenate((self._before, np.abs(channel).astype(np.float64)))
        self._before = envelope[count:]
        # Output i is envelope[window + i], between envelope[i:window + i]
        # and envelope[window + i + 1:2 * window + i + 1].
        sums = np.cumsum(np.concatenate(([0.0], envelope)))
        before = sums[window:window + count] - sums[:count]
        after = sums[2 * window + 1:2 * window + 1 + count] - sums[window + 1:window + 1 + count]
        carrier = np.maximum(before, after) / window
        audio = np.zeros(count)
        heard = carrier > 1e-9                  # -180 dBFS: no carrier at all, silence
        audio[heard] = envelope[window:window + count][heard] / carrier[heard] - 1
        return audio * AM_LEVEL


class _SidebandDetector:
    """USB, LSB and CW: the channel's real part, which is the sideband's
    audio (in CW, the front filter put the VFO at the CW pitch), ``delay``
    samples late, under an AGC that brings a steady signal to the same level
    whatever its strength. The AGC follows the channel's power averaged over
    ``window`` samples: it takes the gain down as soon as that average rises,
    ``window`` samples ahead of the audio thanks to the delay, and lets it
    come back at 10 dB per second."""

    def __init__(self, channel_rate_hz: float, delay: int, window: int) -> None:
        self._window = window
        self._delay = _Delay(delay)
        self._target_db = 20 * math.log10(AGC_LEVEL)
        self._decay_db = AGC_DECAY_DB_S / channel_rate_hz       # per sample
        self._powers = np.zeros(window - 1)                     # the last powers, for the average
        self._levels = np.full(delay - window, AGC_FLOOR_DB)    # levels that the next outputs wait for
        self._level = AGC_FLOOR_DB                              # the latest level

    def process(self, channel: np.ndarray, power: np.ndarray) -> np.ndarray:
        count, window = len(channel), self._window
        audio = self._delay.process(channel.real.astype(np.float64))
        history = np.concatenate((self._powers, power))
        self._powers = history[len(history) - (window - 1):]
        sums = np.cumsum(np.concatenate(([0.0], history)))
        averages = (sums[window:] - sums[:-window]) / window
        averages_db = 10 * np.log10(np.maximum(averages, 10 ** (AGC_FLOOR_DB / 10)))
        # The level follows each rise at once and falls no faster than the
        # decay: the running maximum of the averages, each less the decay
        # since it.
        decay = np.arange(count) * self._decay_db
        levels = np.maximum.accumulate(np.concatenate(([self._level - self._decay_db], averages_db + decay)))
        levels = levels[1:] - decay
        if count:
            self._level = float(levels[-1])
        # Output i takes its gain from the level ``window`` samples after it.
        levels = np.concatenate((self._levels, levels))
        self._levels = levels[count:]
        return audio * 10 ** ((self._target_db - levels[:count]) / 20)
