"""Prototype of the last listening tool still to build (later-milestones.md,
section 2), for the spike that measures the tools' cost
(`tools/measure_listening.py`) and as the starting point of the pull request
that builds it for good.

It is written as the design says and as it could move into the package:
vectorised NumPy, block by block, with no loop per sample, and no knowledge
of the engine, the stream or the interface. Synchronous AM, the automatic
notch and the noise blanker moved into the package with their pull
requests: demod.py's ``CarrierSearch`` and ``SyncDetector``, the SAM mode,
``AutoNotch``, the demodulation settings' ``notch``, and ``NoiseBlanker``,
their ``blanker``, which the measure takes from there.

- ``FineSpectrum``: FFTs of 8192 bins of the VFO's channel before the
  demodulator, about ten frames a second, each the mean of those since the
  last.

It lives in `tools/` until its pull request moves it: the Debian package
and pip take `src/radiolack` alone.
"""

from __future__ import annotations

import numpy as np

from radiolack.dsp import hann

# The fine spectrum.
FINE_SIZE = 8192
FINE_FRAME_RATE = 10.0      # frames a second, about


class FineSpectrum:
    """The fine spectrum of the VFO's channel: Hann-windowed FFTs of
    ``size`` samples, one every ``hop`` samples, the frame's length at
    most, and a frame every ``rate / FINE_FRAME_RATE`` samples, the mean
    power of the FFTs since the last, in full-scale units (a full-scale
    tone centred on a bin reads 1), the lowest frequency first: bin ``i``
    lies ``(i - size / 2) * rate / size`` from the channel's 0 Hz. At
    48 kHz an FFT spans 0.17 s and FFTs overlap; at WFM's 320 kHz they
    follow each other and a frame averages about four. Not thread-safe."""

    def __init__(self, size: int = FINE_SIZE) -> None:
        if size < 16 or size & (size - 1):
            raise ValueError(f"the FFT size must be a power of two of at least 16, not {size}")
        self.size = size
        self._window = hann(size)
        self._scale = 1.0 / float(self._window.astype(np.float64).sum()) ** 2
        self._rate_hz: float | None = None

    def _start(self, rate_hz: float) -> None:
        self._rate_hz = rate_hz
        self._frame = max(1, round(rate_hz / FINE_FRAME_RATE))
        self._hop = min(self.size, self._frame)
        self._recent = np.zeros(0, np.complex64)
        self._total = 0                 # samples received
        self._next_frame = self._frame  # where the next frame ends, in samples received
        self._sum = np.zeros(self.size)
        self._count = 0

    def add(self, channel: np.ndarray, rate_hz: float) -> list[np.ndarray]:
        """Take a block of the channel, complex at ``rate_hz``: the frames
        it completes, usually none or one. Another rate starts again."""
        if rate_hz != self._rate_hz:
            self._start(rate_hz)
        if not len(channel):
            return []
        size, hop = self.size, self._hop
        data = np.concatenate((self._recent, channel))
        first = self._total - len(self._recent)            # where ``data`` starts, in samples received
        self._total += len(channel)
        self._recent = data[-size:]
        # The FFTs that end in this block: every ``hop`` samples, once ``size`` came.
        ends = np.arange(max(size, (self._total - len(channel)) // hop * hop + hop), self._total + 1, hop)
        if not len(ends):
            return []
        starts = ends - size - first
        segments = data[starts[:, np.newaxis] + np.arange(size)]
        spectra = np.fft.fft(segments * self._window, axis=1)
        powers = (spectra.real ** 2 + spectra.imag ** 2) * self._scale
        frames = []
        for end, power in zip(ends, powers):
            self._sum += power
            self._count += 1
            if end >= self._next_frame:
                frames.append(np.fft.fftshift(self._sum / self._count).astype(np.float32))
                self._sum = np.zeros(size)
                self._count = 0
                self._next_frame += (end - self._next_frame) // self._frame * self._frame + self._frame
        return frames
