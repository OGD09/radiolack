"""Fine spectrum of the VFO's channel, for the deep zoom (later-milestones.md,
section 2): Hann-windowed FFTs of 8192 bins of the channel before the
demodulator, the front filter's output, about ten frames a second, each the
mean power of the FFTs since the last.

At the channel's 48 kHz a bin is 5.9 Hz wide, an FFT spans 0.17 s and FFTs
overlap, one every 4800 samples, so that each frame holds one; at WFM's
320 kHz a bin is 39 Hz wide and a frame averages about four FFTs. The cover,
where the front filter passes the signal, is 0.4 times the channel's rate on
each side of the frame's centre (19.2 kHz at 48 kHz): flat to 16 kHz, 0.8 dB
down at 18 kHz and 3.1 dB at its edge; the bins beyond it show the filter's
stopband.

A frame never mixes two settings: the engine gives each block a key, its
VFO, mode and receiver's settings, and another key drops the partial mean
and the samples kept, and starts again. The frames go to the interfaces
that asked, as binary frames with the spectrum frames' header
(docs/design/protocol.md). The module knows nothing of the engine, the
stream or the interface.
"""

from __future__ import annotations

import math
import numpy as np

from .dsp import hann
from .spectrum import FRAME_HEADER

FINE_SIZE = 8192
FINE_FRAME_RATE = 10.0      # frames a second, about
FINE_COVER = 0.4            # the cover, on each side of the frame's centre, in channel rates
REMOTE_FINE_BINS = 2048     # a remote client's fine frames: each bin the highest of those it groups
FRAME_KIND_FINE = 3
FINE_VERSION = 1


class FineSpectrum:
    """The fine spectrum of the VFO's channel: Hann-windowed FFTs of
    ``size`` samples, one every ``hop`` samples, the frame's length at
    most, and a frame every ``rate / FINE_FRAME_RATE`` samples, the mean
    power of the FFTs since the last, in full-scale units (a full-scale
    tone centred on a bin reads 1), the lowest frequency first: bin ``i``
    lies ``(i - size / 2) * rate / size`` from the channel's 0 Hz. Not
    thread-safe: one block at a time."""

    def __init__(self, size: int = FINE_SIZE) -> None:
        if size < 16 or size & (size - 1):
            raise ValueError(f"the FFT size must be a power of two of at least 16, not {size}")
        self.size = size
        self._window = hann(size)
        self._scale = 1.0 / float(self._window.astype(np.float64).sum()) ** 2
        self._setting: tuple[float, object] | None = None
        self.rate_hz = 0.0

    def reset(self) -> None:
        """Forget the samples kept and the partial mean: the next block
        starts again."""
        self._setting = None

    def _start(self, rate_hz: float, key: object) -> None:
        self._setting = (rate_hz, key)
        self.rate_hz = rate_hz
        self._frame = max(1, round(rate_hz / FINE_FRAME_RATE))
        self._hop = min(self.size, self._frame)
        self._recent = np.zeros(0, np.complex64)
        self._total = 0                 # samples received
        self._next_frame = self._frame  # where the next frame ends, in samples received
        self._sum = np.zeros(self.size)
        self._count = 0

    def add(self, channel: np.ndarray, rate_hz: float, key: object = None) -> list[np.ndarray]:
        """Take a block of the channel, complex at ``rate_hz``, from the
        setting ``key``: the frames it completes, usually none or one.
        Another rate or key starts again."""
        if self._setting != (rate_hz, key):
            self._start(rate_hz, key)
        if not len(channel):
            return []
        size, hop = self.size, self._hop
        data = np.concatenate((self._recent, np.asarray(channel, np.complex64)))
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


def encode_fine(sequence: int, centre_hz: float, width_hz: float, db_min: float, db_step: float,
                codes: np.ndarray) -> bytes:
    """Binary fine frame: the spectrum frames' header, of kind 3, whose
    centre is the frequency on the air of bin ``len(codes) / 2`` and whose
    rate is the frame's width on the air, without a channel level or a
    squelch (NaN), then one code per bin, lowest frequency first."""
    header = FRAME_HEADER.pack(FRAME_KIND_FINE, FINE_VERSION, 0, sequence & 0xFFFFFFFF, centre_hz, width_hz,
                               db_min, db_step, math.nan, math.nan)
    return header + np.ascontiguousarray(codes, dtype=np.uint8).tobytes()
