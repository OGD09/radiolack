"""Display spectrum: FFT frames of I/Q samples, quantized for the interface.

A frame averages the power of the last few Blackman-Harris windowed FFTs of a
block of samples and expresses it in dBFS: a full-scale complex tone centred
on a bin reads 0 dBFS. Like other SDR programs, the display does not need
every sample: the interface averages successive frames. Frames travel to the
interface as one unsigned 8-bit code per bin, behind a small binary header
that also carries the VFO's channel level and squelch
(docs/design/protocol.md). The bins around the VFO also tell the noise level
there, for the automatic squelch.
"""

from __future__ import annotations

import math
import struct

import numpy as np

FFT_SIZE = 8192
FFTS_PER_FRAME = 4
DB_MIN = -160.0      # dBFS of code 0; lower levels read as code 0
DB_STEP = 0.625      # dB per code: code 255 is about -0.6 dBFS
NOISE_SPAN_HZ = 200_000.0   # the noise level is the median of the bins this wide around the VFO
MIN_NOISE_BINS = 16

# Spectrum frame header, little-endian: kind, version, flags, sequence
# number, centre frequency (Hz), sample rate (Hz), dB of code 0, dB per code,
# then the VFO's channel level and squelch threshold (dBFS, NaN when unknown).
FRAME_HEADER = struct.Struct("<BBHIddffff")
FRAME_KIND_SPECTRUM = 1
FRAME_VERSION = 2
FLAG_SQUELCH_OPEN = 0x1


def blackman_harris(size: int) -> np.ndarray:
    """Periodic 4-term Blackman-Harris window (sidelobes below -92 dB)."""
    phase = 2 * np.pi * np.arange(size) / size
    window = (0.35875 - 0.48829 * np.cos(phase) + 0.14128 * np.cos(2 * phase)
              - 0.01168 * np.cos(3 * phase))
    return window.astype(np.float32)


class SpectrumAnalyzer:
    """Turn blocks of I/Q samples into display frames.

    Bin ``i`` of a frame is centred on ``centre - rate / 2 + i * rate / size``:
    the lowest frequency comes first and the centre frequency is bin
    ``size / 2``.
    """

    def __init__(self, fft_size: int = FFT_SIZE, ffts_per_frame: int = FFTS_PER_FRAME,
                 db_min: float = DB_MIN, db_step: float = DB_STEP) -> None:
        if fft_size < 16 or fft_size & (fft_size - 1):
            raise ValueError(f"the FFT size must be a power of two of at least 16, not {fft_size}")
        if ffts_per_frame < 1:
            raise ValueError(f"a frame needs at least one FFT, not {ffts_per_frame}")
        if db_step <= 0:
            raise ValueError(f"the dB step must be positive, not {db_step}")
        self.fft_size = fft_size
        self.ffts_per_frame = ffts_per_frame
        self.db_min = float(db_min)
        self.db_step = float(db_step)
        self._window = blackman_harris(fft_size)
        window = self._window.astype(np.float64)
        # Scales |X|^2 so that a full-scale tone on a bin centre reads 1 (0 dBFS).
        self._scale = 1.0 / float(window.sum()) ** 2
        # Equivalent noise bandwidth of the window, in bins: noise of power p
        # reads p * enbw / fft_size in each bin.
        self.enbw = fft_size * float((window ** 2).sum()) / float(window.sum()) ** 2

    def power(self, iq: np.ndarray) -> np.ndarray:
        """Mean bin power of the last FFT windows of ``iq``, in full-scale units."""
        count = min(len(iq) // self.fft_size, self.ffts_per_frame)
        if count == 0:
            raise ValueError(f"a frame needs at least {self.fft_size} samples, got {len(iq)}")
        blocks = np.asarray(iq[len(iq) - count * self.fft_size:]).reshape(count, self.fft_size)
        spectra = np.fft.fft(blocks * self._window, axis=1)
        power = (spectra.real ** 2 + spectra.imag ** 2).mean(axis=0) * self._scale
        return np.fft.fftshift(power)

    def codes(self, power: np.ndarray) -> np.ndarray:
        """Quantize bin powers to 8-bit codes: ``dB = db_min + code * db_step``."""
        db = 10 * np.log10(np.maximum(power, 1e-30).astype(np.float32))
        return np.clip(np.rint((db - self.db_min) / self.db_step), 0, 255).astype(np.uint8)

    def noise_density(self, power: np.ndarray, samples: int, offset_hz: float,
                      rate_hz: float) -> float | None:
        """Noise power per hertz ``offset_hz`` from the centre frequency,
        estimated from ``power``, the bins that ``power`` computed from
        ``samples`` samples: the median of the bins within
        ``NOISE_SPAN_HZ / 2``, which the signals among them barely move.
        None when too few of these bins lie in the span."""
        bin_hz = rate_hz / self.fft_size
        middle = self.fft_size / 2 + offset_hz / bin_hz
        half = NOISE_SPAN_HZ / 2 / bin_hz
        low, high = max(0, math.floor(middle - half)), min(self.fft_size, math.ceil(middle + half) + 1)
        if high - low < MIN_NOISE_BINS:
            return None
        averaged = max(1, min(samples // self.fft_size, self.ffts_per_frame))
        # The median of K averaged periodograms of noise lies below their mean
        # (Wilson-Hilferty approximation of the chi-squared median).
        mean = float(np.median(power[low:high])) / (1 - 1 / (9 * averaged)) ** 3
        return mean * self.fft_size / (self.enbw * rate_hz)


def fewer_bins(codes: np.ndarray, bins: int) -> np.ndarray:
    """``codes`` in at most ``bins`` bins, each the highest code of the
    consecutive bins it groups, so that a narrow signal stays visible. Both
    counts are powers of two; fewer codes than ``bins`` stay as they are."""
    group = len(codes) // bins
    if group <= 1:
        return codes
    return codes.reshape(-1, group).max(axis=1)


def encode_frame(sequence: int, centre_hz: float, sample_rate_hz: float, db_min: float, db_step: float,
                 codes: np.ndarray, level_db: float | None = None, squelch_db: float | None = None,
                 squelch_open: bool = False) -> bytes:
    """Binary spectrum frame: the header, then one code per bin."""
    header = FRAME_HEADER.pack(
        FRAME_KIND_SPECTRUM, FRAME_VERSION, FLAG_SQUELCH_OPEN if squelch_open else 0,
        sequence & 0xFFFFFFFF, centre_hz, sample_rate_hz, db_min, db_step,
        math.nan if level_db is None else level_db, math.nan if squelch_db is None else squelch_db)
    return header + np.ascontiguousarray(codes, dtype=np.uint8).tobytes()
