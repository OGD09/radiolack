"""Demodulation of one channel: the VFO's signal, its level, the squelch and
NFM audio at 12 kHz.

The receiver's samples go through three FFT filters (dsp.FftFilter). The
front filter brings the VFO's frequency to 0 Hz and the rate down to 48 kHz;
the channel filter keeps the mode's passband, whose power is the channel
level; after the FM discriminator, the audio filter applies the de-emphasis,
removes the CTCSS tones below 300 Hz and the noise above 3.5 kHz, and brings
the rate down to 12 kHz. The squelch compares the channel level with a
threshold, set by hand or, automatically, 8 dB above the noise that the
spectrum shows around the VFO; it opens and closes in 5 ms fades, in step
with the audio.
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
AUDIO_MODES = ("NFM",)              # modes demodulated so far; the others are silent
CHANNEL_RATE_HZ = 48_000.0
AUDIO_DECIMATION = 4                # 12 kHz audio
BANDWIDTH_RANGE_HZ = (100.0, 25_000.0)
SQUELCH_RANGE_DB = (-160.0, 0.0)
SQUELCH_MARGIN_DB = 8.0             # automatic squelch: noise + 8 dB
SQUELCH_HYSTERESIS_DB = 3.0         # closes this far below the threshold
SQUELCH_WINDOW_S = 0.010            # channel power averaged over 10 ms
FADE_S = 0.005
MAX_DEVIATION_HZ = 5_000.0          # full-scale audio
DEEMPHASIS_S = 750e-6               # NFM de-emphasis, -6 dB per octave above 212 Hz
DEEMPHASIS_REFERENCE_HZ = 1_000.0   # unit gain there
HIGH_PASS_HZ = 280.0                # removes CTCSS tones (up to 250.3 Hz)
LOW_PASS_HZ = 3_500.0
AUDIO_TRANSITION_HZ = 60.0
AUDIO_ATTENUATION_DB = 45.0
FRONT_CUTOFF_HZ = 20_000.0          # flat to 16 kHz, -70 dB from 24 kHz
FRONT_TRANSITION_HZ = 8_000.0
CHANNEL_ATTENUATION_DB = 70.0

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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
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


def channel_taps(mode: str, bandwidth_hz: float, rate_hz: float) -> np.ndarray:
    """The channel filter: the mode's passband around the VFO, at ``rate_hz``.
    USB keeps [0, width] above the VFO, LSB [-width, 0] below it, the other
    modes [-width / 2, width / 2]."""
    transition = min(max(0.1 * bandwidth_hz, 100.0), 1_500.0)
    taps = lowpass(bandwidth_hz / 2, transition, rate_hz, CHANNEL_ATTENUATION_DB).astype(np.complex128)
    if mode in ("USB", "LSB"):
        centre = bandwidth_hz / 2 if mode == "USB" else -bandwidth_hz / 2
        taps *= np.exp(2j * np.pi * centre / rate_hz * (np.arange(len(taps)) - (len(taps) - 1) / 2))
    return taps


def audio_taps(rate_hz: float) -> np.ndarray:
    """The NFM audio filter at the channel rate: de-emphasis with unit gain at
    1 kHz, high-pass at 280 Hz and low-pass at 3.5 kHz."""
    corner = 1 / (2 * math.pi * DEEMPHASIS_S)

    def response(frequency: np.ndarray) -> np.ndarray:
        deemphasis = np.sqrt((1 + (DEEMPHASIS_REFERENCE_HZ / corner) ** 2) / (1 + (frequency / corner) ** 2))
        return deemphasis * ((frequency >= HIGH_PASS_HZ) & (frequency <= LOW_PASS_HZ))

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
        self._front.set_shift(shift_hz)
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
        if self._audio is None:
            self._audio = _AudioPath(self.channel_rate_hz)
        sound = self._audio.process(channel, gate, settings.mode in AUDIO_MODES)
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
    """FM discriminator, squelch gate and audio filter: the channel rate in,
    the audio rate out.

    The gate acts on the discriminator's output, before the audio filter,
    whose long response would otherwise spread the noise around a closing
    squelch. Its decision averages the last 10 ms, so the discriminator's
    output waits that long, plus a fade, for the gate to look ahead: a
    sample passes only if the squelch stays open until the decisions about
    it are in, and the gate fades out before the noise that follows a
    carrier."""

    def __init__(self, channel_rate_hz: float) -> None:
        self._filter = FftFilter(audio_taps(channel_rate_hz), channel_rate_hz, up=1, down=AUDIO_DECIMATION)
        self._scale = channel_rate_hz / (2 * math.pi * MAX_DEVIATION_HZ)
        self._previous = np.complex64(0)
        self._fade = max(1, round(FADE_S * channel_rate_hz))
        self._span = max(1, round(SQUELCH_WINDOW_S * channel_rate_hz)) + self._fade
        self._held = np.zeros(self._span)           # discriminator output waiting for the gate
        self._decisions = np.zeros(self._span)      # the squelch's last decisions
        self._passed = np.zeros(self._fade - 1)     # the gate's last values, for the fades

    def process(self, channel: np.ndarray, gate: np.ndarray, demodulate: bool) -> np.ndarray:
        """Audio for ``channel``, silent where ``gate``, the squelch's
        decision at each channel sample, is closed, or everywhere unless
        ``demodulate``."""
        previous = np.concatenate(([self._previous], channel[:-1])) if len(channel) else channel
        if len(channel):
            self._previous = channel[-1]
        frequency = np.angle(channel * np.conj(previous)) * self._scale if demodulate \
            else np.zeros(len(channel))
        held = np.concatenate((self._held, frequency))
        self._held = held[len(frequency):]
        # A held sample passes if every decision from its own to the latest is open.
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
        gated = held[:len(frequency)] * fades
        return self._filter.process(gated.astype(np.complex64)).real.astype(np.float64)
