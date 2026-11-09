"""Receiver engine: reads a source and shares display frames with clients.

The engine runs in the server's event loop. It reads blocks of I/Q samples
from its source only while at least one client listens, turns each block into
a display frame in a worker thread, and offers the frame to every client. A
client that falls behind loses its oldest frames instead of slowing the
others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import numpy as np

from .spectrum import SpectrumAnalyzer, encode_frame

FRAME_RATE = 30.0       # display frames per second
CLIENT_QUEUE = 4        # frames kept for a client that falls behind

log = logging.getLogger(__name__)


class Source(Protocol):
    """A source of I/Q samples: complex64, in full-scale units."""

    kind: str
    centre_hz: float
    sample_rate_hz: float

    async def read(self, count: int) -> np.ndarray:
        """The next ``count`` samples, once they have arrived."""


class Engine:
    """Turn a source into display frames for any number of clients."""

    def __init__(self, source: Source, *, frame_rate: float = FRAME_RATE,
                 analyzer: SpectrumAnalyzer | None = None,
                 queue_size: int = CLIENT_QUEUE) -> None:
        if frame_rate <= 0:
            raise ValueError(f"the frame rate must be positive, not {frame_rate}")
        self.source = source
        self.analyzer = analyzer if analyzer is not None else SpectrumAnalyzer()
        self._block = max(self.analyzer.fft_size, round(source.sample_rate_hz / frame_rate))
        self._queue_size = queue_size
        self._clients: set[asyncio.Queue[bytes | None]] = set()
        self._wanted = asyncio.Event()     # set while a client listens, or to stop
        self._closed = False
        self._sequence = 0

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """A queue of encoded frames for a new client; ``None`` means the engine stopped."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(self._queue_size)
        if self._closed:
            queue.put_nowait(None)
        else:
            self._clients.add(queue)
            self._wanted.set()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._clients.discard(queue)
        if not self._clients and not self._closed:
            self._wanted.clear()

    def close(self) -> None:
        """Stop for good: every client's queue ends with ``None``. Safe to repeat."""
        self._closed = True
        for queue in self._clients:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self._clients.clear()
        self._wanted.set()

    async def run(self) -> None:
        """Produce frames until closed or cancelled."""
        try:
            while True:
                await self._wanted.wait()
                if self._closed:
                    return
                samples = await self.source.read(self._block)
                codes = await asyncio.to_thread(self.analyzer.frame, samples)
                self._publish(codes)
        except Exception:
            log.exception("the display engine stopped")
            self.close()

    def _publish(self, codes: np.ndarray) -> None:
        if not self._clients:
            return
        frame = encode_frame(self._sequence, self.source.centre_hz, self.source.sample_rate_hz,
                             self.analyzer.db_min, self.analyzer.db_step, codes)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        for queue in self._clients:
            if queue.full():
                queue.get_nowait()      # the client fell behind: drop its oldest frame
            queue.put_nowait(frame)
