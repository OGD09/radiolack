"""Signal processing building blocks: FIR filter design and FFT filtering.

Filters run by fast convolution (overlap-save): each segment of samples, with
the end of the previous one, goes through an FFT, is multiplied by the
filter's frequency response and comes back through a smaller inverse FFT,
which also shifts it in frequency and lowers its sample rate. NumPy computes
this far faster than a convolution sample by sample, and any filter length
costs about the same.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_SEGMENT_FACTOR = 3     # FFT segments hold at least 3 times the filter's memory


def kaiser_beta(attenuation_db: float) -> float:
    """Kaiser window parameter for a stopband attenuation (Kaiser's formula)."""
    if attenuation_db > 50:
        return 0.1102 * (attenuation_db - 8.7)
    if attenuation_db >= 21:
        excess = attenuation_db - 21
        return 0.5842 * excess ** 0.4 + 0.07886 * excess
    return 0.0


def kaiser_length(attenuation_db: float, transition_hz: float, rate_hz: float) -> int:
    """Odd number of taps of a Kaiser-window filter with this transition band."""
    if not 0 < transition_hz < rate_hz / 2:
        raise ValueError(f"the transition band must be between 0 and {rate_hz / 2} Hz, not {transition_hz}")
    width = 2 * math.pi * transition_hz / rate_hz
    taps = math.ceil(max(attenuation_db - 7.95, 0.0) / (2.285 * width)) + 1
    return taps | 1             # odd: the delay is a whole number of samples


def lowpass(cutoff_hz: float, transition_hz: float, rate_hz: float,
            attenuation_db: float = 70.0) -> np.ndarray:
    """Linear-phase low-pass FIR filter with unit gain at 0 Hz: half gain at
    ``cutoff_hz``, full attenuation from ``cutoff_hz + transition_hz / 2``."""
    taps = kaiser_length(attenuation_db, transition_hz, rate_hz)
    t = np.arange(taps) - (taps - 1) / 2
    band = 2 * cutoff_hz / rate_hz
    h = band * np.sinc(band * t) * np.kaiser(taps, kaiser_beta(attenuation_db))
    return h / h.sum()


def shaped(response: Callable[[np.ndarray], np.ndarray], transition_hz: float, rate_hz: float,
           attenuation_db: float) -> np.ndarray:
    """Linear-phase FIR filter whose gain follows ``response``, a function of
    the frequency in Hz (0 to rate / 2): window method, where each step of
    the response spreads over ``transition_hz``."""
    taps = kaiser_length(attenuation_db, transition_hz, rate_hz)
    grid = 1 << max(14, (16 * taps).bit_length())
    gain = response(np.fft.rfftfreq(grid, 1 / rate_hz))
    ideal = np.fft.irfft(gain, grid)                    # zero phase: centred on sample 0
    return np.roll(ideal, taps // 2)[:taps] * np.kaiser(taps, kaiser_beta(attenuation_db))


class FftFilter:
    """FIR filter that runs on a stream of samples by overlap-save FFTs.

    ``process`` takes any number of samples and returns the filtered ones,
    ``up / down`` times as many: samples that do not make up a whole output
    sample wait for the next call. ``set_shift`` moves the signal down in
    frequency on the way, so that ``shift_hz`` comes out at 0 Hz; the taps
    filter the shifted signal. The output lags the input by the filter's
    delay, ``(len(taps) - 1) / 2`` input samples for a linear-phase filter.

    The filter's response must be negligible beyond the output's Nyquist
    frequency, which the output cannot hold. Like any mixer on sampled
    signals, the shift wraps around: a passband that reaches past the
    input's Nyquist frequency picks up the opposite edge of the input band.
    """

    def __init__(self, taps: np.ndarray, rate_hz: float, *, up: int = 1, down: int = 1) -> None:
        if not 1 <= up <= down:
            raise ValueError(f"cannot change the rate by {up}/{down}: only by 1 or less")
        common = math.gcd(up, down)
        self.up, self.down = up // common, down // common
        self.rate_hz = float(rate_hz)
        self.output_rate_hz = self.rate_hz * self.up / self.down
        self._shift_hz = 0.0
        self._bin_shift = 0
        self._fine_hz = 0.0     # what the whole-bin shift leaves, removed after the inverse FFT
        self._fine_phase = 0.0  # cycles
        self._start = 0         # input index of the buffer's first sample
        self._buffer = np.zeros(0, np.complex64)
        self._overlap = 0
        self.set_taps(taps)

    @property
    def shift_hz(self) -> float:
        return self._shift_hz

    def set_taps(self, taps: np.ndarray) -> None:
        """Change the filter, keeping the samples it remembers."""
        taps = np.asarray(taps, np.complex128)
        if taps.ndim != 1 or not len(taps):
            raise ValueError("a filter needs at least one tap")
        q = self.down
        overlap = q * math.ceil((len(taps) - 1) / q)
        size = q
        while size < max(_SEGMENT_FACTOR * overlap, 16 * q):
            size *= 2
        # Keep the remembered samples: pad with silence before them, or drop the oldest.
        grow = overlap - self._overlap
        if grow > 0:
            self._buffer = np.concatenate((np.zeros(grow, np.complex64), self._buffer))
        elif grow < 0:
            self._buffer = self._buffer[-grow:]
        self._start -= grow
        self._overlap = overlap
        self._size = size
        self._outputs = size * self.up // self.down
        self._response = np.fft.fft(taps, size)
        self._plan()

    def set_shift(self, shift_hz: float) -> None:
        """Bring ``shift_hz`` down to 0 Hz from now on."""
        shift_hz = float(shift_hz)
        if shift_hz != self._shift_hz:
            self._shift_hz = shift_hz
            self._plan()

    def _plan(self) -> None:
        """Which input bins make the output bins, and with what gain."""
        n, m = self._size, self._outputs
        bin_hz = self.rate_hz / n
        self._bin_shift = round(self._shift_hz / bin_hz)
        self._fine_hz = self._shift_hz - self._bin_shift * bin_hz
        signed = np.arange(m)
        signed[(m + 1) // 2:] -= m                      # output bins in FFT order: 0, 1, ..., -1
        self._bins = (self._bin_shift + signed) % n     # the input bins they come from
        # Scaled so that the smaller inverse FFT gives the input's units.
        self._weights = self._response[signed % n] * (m / n)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Filter ``samples`` (complex), continuing the previous ones."""
        buffer = np.concatenate((self._buffer, np.asarray(samples, np.complex64)))
        q, n, overlap = self.down, self._size, self._overlap
        usable = (len(buffer) - overlap) // q * q       # new samples processed now
        if usable <= 0:
            self._buffer = buffer
            return np.zeros(0, np.complex64)
        hop = n - overlap
        full = usable // hop                            # whole segments; a shorter one may follow
        rest = usable - full * hop
        spectra = []
        if full:
            # A strided view: NumPy reads the segments in place, which is much faster.
            spectra.append(np.fft.fft(sliding_window_view(buffer, n)[:full * hop:hop], axis=1))
        if rest:
            # The zeros after the short segment reach none of its valid outputs.
            last = np.zeros(n, np.complex64)
            last[:overlap + rest] = buffer[full * hop:full * hop + overlap + rest]
            spectra.append(np.fft.fft(last)[np.newaxis])
        both = spectra[0] if len(spectra) == 1 else np.concatenate(spectra)
        filtered = np.fft.ifft(both[:, self._bins] * self._weights, axis=1)
        # The whole-bin shift counts from each segment's first sample: carry its phase on.
        turns = [(self._bin_shift * (self._start + row * hop)) % n for row in range(len(filtered))]
        filtered *= np.exp(-2j * np.pi * np.array(turns) / n)[:, np.newaxis]
        first = overlap * self.up // q                  # outputs before it are wrapped around
        per_hop = hop * self.up // q
        output = filtered[:full, first:first + per_hop].reshape(-1)
        if rest:
            output = np.concatenate((output, filtered[-1, first:first + rest * self.up // q]))
        if self._fine_hz:
            cycles = self._fine_phase + np.arange(len(output)) * (self._fine_hz / self.output_rate_hz)
            output *= np.exp(-2j * np.pi * cycles)
            self._fine_phase = (self._fine_phase + len(output) * self._fine_hz / self.output_rate_hz) % 1.0
        self._buffer = buffer[usable:]
        self._start += usable
        return output.astype(np.complex64)


def smooth_numbers(limit: int) -> list[int]:
    """Numbers up to ``limit`` with no prime factor above 7, in order: FFT
    sizes that NumPy computes quickly."""
    numbers = {1}
    for prime in (2, 3, 5, 7):
        for value in list(numbers):
            value *= prime
            while value <= limit:
                numbers.add(value)
                value *= prime
    return sorted(numbers)


def rate_ratio(rate_hz: float, target_hz: float, max_down: int = 1024) -> tuple[int, int]:
    """(up, down), with ``up <= down`` and ``down`` a smooth number up to
    ``max_down``, such that ``rate_hz * up / down`` comes closest to
    ``target_hz``: exactly for the usual receiver rates."""
    if not 0 < target_hz <= rate_hz:
        raise ValueError(f"cannot bring {rate_hz} Hz to {target_hz} Hz")
    best = (1, 1)
    best_error = math.inf
    for down in smooth_numbers(max_down):
        up = min(down, max(1, round(target_hz * down / rate_hz)))
        error = abs(rate_hz * up / down - target_hz)
        if error < best_error - 1e-9 * target_hz:
            best, best_error = (up, down), error
    up, down = best
    common = math.gcd(up, down)
    return up // common, down // common
