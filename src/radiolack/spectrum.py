"""Display spectrum: FFT frames of I/Q samples, quantized for the interface.

A frame averages the power of the last few Blackman-Harris windowed FFTs of a
block of samples and expresses it in dBFS: a full-scale complex tone centred
on a bin reads 0 dBFS. Like other SDR programs, the display does not need
every sample: the interface averages successive frames. Frames travel to the
interface as one unsigned 8-bit code per bin, behind a small binary header
(docs/design/protocol.md).
"""

from __future__ import annotations

import struct

import numpy as np

FFT_SIZE = 8192
FFTS_PER_FRAME = 4
DB_MIN = -160.0      # dBFS of code 0; lower levels read as code 0
DB_STEP = 0.625      # dB per code: code 255 is about -0.6 dBFS

# Spectrum frame header, little-endian: kind, version, reserved, sequence
# number, centre frequency (Hz), sample rate (Hz), dB of code 0, dB per code.
FRAME_HEADER = struct.Struct("<BBHIddff")
FRAME_KIND_SPECTRUM = 1
FRAME_VERSION = 1


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
        # Scales |X|^2 so that a full-scale tone on a bin centre reads 1 (0 dBFS).
        self._scale = 1.0 / float(self._window.sum(dtype=np.float64)) ** 2

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

    def frame(self, iq: np.ndarray) -> np.ndarray:
        """Codes of the frame computed from ``iq``."""
        return self.codes(self.power(iq))


def encode_frame(sequence: int, centre_hz: float, sample_rate_hz: float,
                 db_min: float, db_step: float, codes: np.ndarray) -> bytes:
    """Binary spectrum frame: the header, then one code per bin."""
    header = FRAME_HEADER.pack(FRAME_KIND_SPECTRUM, FRAME_VERSION, 0, sequence & 0xFFFFFFFF,
                               centre_hz, sample_rate_hz, db_min, db_step)
    return header + np.ascontiguousarray(codes, dtype=np.uint8).tobytes()
