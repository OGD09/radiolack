"""FFT peak detection on raw rtl_tcp I/Q frames.

The detector inspects every bin of the received window. Levels are FFT-bin
powers relative to full scale ("dBFS per bin"). They are not calibrated dBm,
not an antenna-input SNR, and not comparable with Gqrx's integrated S-meter.
A peak's margin is a contrast against the local spectral noise estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

import numpy as np

FFT_SIZE = 8192
FFT_AVERAGES = 8
WINDOW_NAME = "hann"
# rtl_tcp sends unsigned 8-bit I and Q per complex sample.
FRAME_BYTES = FFT_SIZE * FFT_AVERAGES * 2
# RTL-SDR roll-off makes the outer spectrum unreliable; keep +/-32 % of the rate.
USABLE_FRACTION = 0.32
# The DC spike around the hardware center is excluded.
DC_EXCLUSION_HZ = 3000
LOCAL_NOISE_RADIUS_HZ = 50000
MIN_LOCAL_BINS = 16
MIN_SAMPLE_RATE = 225000
MAX_SAMPLE_RATE = 3200000

_WINDOW = np.hanning(FFT_SIZE)
_WINDOW_POWER_GAIN = _WINDOW.sum() ** 2


@dataclass(frozen=True)
class ChannelGrid:
    """Carrier frequencies ``origin + k * spacing`` (rounded to 1 Hz) in ``[low, high)``.

    The spacing is an exact fraction so that grids such as 25000/3 Hz
    (8.333 kHz) do not accumulate rounding errors across a band.
    """

    low_hz: int
    high_hz: int
    origin_hz: int
    spacing_hz: Fraction
    snap_tolerance_hz: float

    def __post_init__(self):
        if not 0 < self.low_hz < self.high_hz:
            raise ValueError("Band limits must satisfy 0 < low < high")
        if self.spacing_hz <= 0:
            raise ValueError("Channel spacing must be positive")
        if not 0 <= self.snap_tolerance_hz < self.spacing_hz / 2:
            raise ValueError("Snap tolerance must be below half the channel spacing")

    def contains(self, frequency) -> bool:
        return self.low_hz <= frequency < self.high_hz

    def nearest(self, frequency: float) -> int:
        """Nearest grid carrier; arithmetic matches the prototype's 8.333 kHz snapping."""
        numerator, denominator = self.spacing_hz.numerator, self.spacing_hz.denominator
        index = round((frequency - self.origin_hz) * denominator / numerator)
        return self.origin_hz + round(index * numerator / denominator)

    def snap(self, frequency: float) -> int | None:
        """Grid carrier for a measured frequency, or None if off-grid or out of band."""
        channel = self.nearest(frequency)
        if not self.contains(channel) or abs(frequency - channel) > self.snap_tolerance_hz:
            return None
        return channel

    def channels(self) -> list[int]:
        """Every grid carrier inside the band, in increasing order."""
        numerator, denominator = self.spacing_hz.numerator, self.spacing_hz.denominator
        first = math.floor((self.low_hz - self.origin_hz) / self.spacing_hz) - 1
        last = math.ceil((self.high_hz - self.origin_hz) / self.spacing_hz) + 1
        result = []
        for index in range(first, last + 1):
            # Same arithmetic as nearest(), so snapped peaks are always list members.
            channel = self.origin_hz + round(index * numerator / denominator)
            if self.contains(channel) and (not result or channel != result[-1]):
                result.append(channel)
        return result

    @property
    def spacing_label(self) -> str:
        spacing = self.spacing_hz
        return str(spacing.numerator) if spacing.denominator == 1 else f"{spacing.numerator}/{spacing.denominator}"

    @property
    def proximity_hz(self) -> int:
        """Distance within which another peak counts as the same channel."""
        return math.ceil(self.spacing_hz)


@dataclass(frozen=True)
class Peak:
    frequency_hz: int
    power_dbfs: float
    noise_dbfs: float
    bin_frequency_hz: float | None = None

    @property
    def margin_db(self) -> float:
        return self.power_dbfs - self.noise_dbfs


@dataclass(frozen=True)
class Spectrum:
    """Averaged power spectrum of one I/Q frame, ordered by frequency."""

    center_hz: int
    sample_rate: int
    db: np.ndarray
    frequencies: np.ndarray
    offsets: np.ndarray


def compute_spectrum(raw: bytes, center_hz: int, sample_rate: int) -> Spectrum:
    """Average eight Hann-windowed 8192-point FFTs of unsigned interleaved I/Q."""
    if len(raw) != FRAME_BYTES or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise ValueError("Invalid I/Q frame length or sample rate")
    values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 128
    iq = (values[0::2] + 1j * values[1::2]).reshape(-1, FFT_SIZE)
    iq -= iq.mean(axis=1, keepdims=True)
    fft = np.fft.fftshift(np.fft.fft(iq * _WINDOW, axis=1), axes=1)
    power = (np.abs(fft) ** 2).mean(axis=0) / _WINDOW_POWER_GAIN
    db = 10 * np.log10(np.maximum(power, 1e-20))
    offsets = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, 1 / sample_rate))
    return Spectrum(center_hz, sample_rate, db, center_hz + offsets, offsets)


def extract_peaks(spectrum: Spectrum, grid: ChannelGrid, margin_db: float) -> list[Peak]:
    """Grid carriers at least ``margin_db`` above their local spectral noise.

    The unreliable outer spectrum, the DC region and out-of-band bins are
    excluded. Results are sorted by decreasing margin, one per channel.
    """
    if not math.isfinite(margin_db) or margin_db <= 0:
        raise ValueError("Detection margin must be a positive number of dB")
    db, offsets, frequencies = spectrum.db, spectrum.offsets, spectrum.frequencies
    rate = spectrum.sample_rate
    usable = ((np.abs(offsets) < rate * USABLE_FRACTION) & (np.abs(offsets) > DC_EXCLUSION_HZ)
              & (frequencies >= grid.low_hz) & (frequencies < grid.high_hz))
    if not usable.any():
        return []
    coarse_floor = float(np.median(db[usable]))
    maxima = np.zeros(len(db), dtype=bool)
    maxima[1:-1] = (db[1:-1] > db[:-2]) & (db[1:-1] >= db[2:])
    indices = np.flatnonzero(usable & maxima & (db >= coarse_floor + margin_db))
    radius = max(MIN_LOCAL_BINS, round(LOCAL_NOISE_RADIUS_HZ * len(db) / rate))
    found: dict[int, Peak] = {}
    for index in indices:
        low, high = max(0, index - radius), min(len(db), index + radius + 1)
        local = db[low:high][usable[low:high]]
        if len(local) < MIN_LOCAL_BINS:
            continue
        noise = float(np.median(local))
        frequency = float(frequencies[index])
        # Gqrx expects real carrier frequencies, not 8.33 kHz channel labels.
        channel = grid.snap(frequency)
        if channel is None:
            continue
        peak = Peak(channel, float(db[index]), noise, frequency)
        if peak.margin_db >= margin_db and (
                channel not in found or peak.margin_db > found[channel].margin_db):
            found[channel] = peak
    return sorted(found.values(), key=lambda item: item.margin_db, reverse=True)


def find_peaks(raw: bytes, center_hz: int, sample_rate: int, grid: ChannelGrid,
               margin_db: float = 15.0) -> list[Peak]:
    """Convenience wrapper: spectrum of one frame, then peak extraction."""
    return extract_peaks(compute_spectrum(raw, center_hz, sample_rate), grid, margin_db)
