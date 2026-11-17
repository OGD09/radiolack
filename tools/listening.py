"""Prototypes of the listening tools (later-milestones.md, section 2), for
the spike that measures their cost (`tools/measure_listening.py`) and as the
starting point of the pull requests that build them for good.

Each one is written as the design says and as it could move into the
package: vectorised NumPy, block by block, with no loop per sample, and no
knowledge of the engine, the stream or the interface. Synchronous AM and
the automatic notch moved into the package with their pull requests:
demod.py's ``CarrierSearch`` and ``SyncDetector``, the SAM mode, and
``AutoNotch``, the demodulation settings' ``notch``, which the measure takes
from there.

- ``NoiseBlanker``: on the samples that go to the VFO's channel, those over
  six times the block's median magnitude are zeroed, with two samples on
  each side.
- ``FineSpectrum``: FFTs of 8192 bins of the VFO's channel before the
  demodulator, about ten frames a second, each the mean of those since the
  last.

They live in `tools/` until their pull requests move them: the Debian
package and pip take `src/radiolack` alone.
"""

from __future__ import annotations

import math

import numpy as np

from radiolack.dsp import hann

# The noise blanker.
BLANKER_FACTOR = 6.0        # samples over this many times the block's median magnitude are impulses
BLANKER_SIDES = 2           # samples zeroed on each side of one
BLANKER_STRIDE = 16         # the block's median magnitude, from every 16th of its samples

# The fine spectrum.
FINE_SIZE = 8192
FINE_FRAME_RATE = 10.0      # frames a second, about


class NoiseBlanker:
    """Zeroes the impulses of the samples that go to the VFO's channel:
    those whose magnitude exceeds ``BLANKER_FACTOR`` times the block's
    median, with ``BLANKER_SIDES`` samples on each side.

    The median is that of every ``BLANKER_STRIDE``th sample, 5000 of a block
    at 2.4 MS/s, which estimates the block's within about 1% for noise, far
    finer than the factor of 6; a whole block's median took most of the
    blanker's time. The impulses are sought in the samples' real and
    imaginary parts, which NumPy compares far faster than it computes
    magnitudes: a magnitude over the limit has a part over the limit over
    the square root of 2, and only the samples with such a part have their
    magnitude computed.

    An impulse at the end of a block zeroes the first samples of the next;
    one at the start of a block does not reach back into the previous one,
    which has gone. A block whose median magnitude is 0, more than half of
    its samples exact zeros, is left alone. Not thread-safe."""

    def __init__(self) -> None:
        self._pending = 0           # samples to zero at the start of the next block

    def process(self, samples: np.ndarray) -> np.ndarray:
        """``samples`` with their impulses zeroed: the same array when none
        was found, a new one otherwise."""
        count = len(samples)
        pending, self._pending = min(self._pending, count), max(0, self._pending - count)
        if not count:
            return samples
        every = samples[::BLANKER_STRIDE]
        median = float(np.median(every.real.astype(np.float64) ** 2 + every.imag.astype(np.float64) ** 2))
        hits = np.zeros(0, np.intp)
        if median > 0:
            limit = BLANKER_FACTOR ** 2 * median
            parts = np.ascontiguousarray(samples, np.complex64).view(np.float32)
            candidates = np.unique(np.flatnonzero(np.abs(parts) > math.sqrt(limit / 2)) // 2)
            if len(candidates):
                values = samples[candidates]
                hits = candidates[values.real.astype(np.float64) ** 2 + values.imag.astype(np.float64) ** 2 > limit]
        if not len(hits) and not pending:
            return samples
        blanked = samples.copy()
        blanked[:pending] = 0
        if len(hits):
            reach = np.arange(-BLANKER_SIDES, BLANKER_SIDES + 1)
            zeroed = (hits[:, np.newaxis] + reach).ravel()
            blanked[zeroed[(zeroed >= 0) & (zeroed < count)]] = 0
            self._pending = max(self._pending, int(hits[-1]) + BLANKER_SIDES + 1 - count)
        return blanked


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
