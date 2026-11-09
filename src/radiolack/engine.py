"""Receiver engine: reads a source and shares display frames with clients.

The engine runs in the server's event loop. It reads blocks of I/Q samples
from its source only while at least one client listens, turns each block into
a display frame in a worker thread, and offers the frame to every client. A
client that falls behind loses its oldest frames instead of slowing the
others. The engine also carries the source's state to every client, and the
clients' changes to the source: a change reaches every client at once, and
the last change wins.

DC removal is the engine's own setting, on by default and shared by every
client like the source's: it takes each block's mean off its samples, so that
a constant offset, such as an RTL-SDR's, does not show as a spike at the
centre frequency. It also removes a signal exactly on the centre frequency.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import numpy as np

from .spectrum import SpectrumAnalyzer, encode_frame

FRAME_RATE = 30.0       # display frames per second
CLIENT_QUEUE = 4        # frames kept for a client that falls behind
CLIENT_REPLIES = 8      # replies kept for a client that falls behind

log = logging.getLogger(__name__)


class Source(Protocol):
    """A source of I/Q samples: complex64, in full-scale units.

    ``centre_hz`` and ``sample_rate_hz`` describe the samples ``read`` last
    returned. ``describe`` gives the state the interfaces show
    (docs/design/protocol.md), and the source calls ``on_change`` whenever
    that state changes.
    """

    kind: str
    centre_hz: float
    sample_rate_hz: float
    on_change: Callable[[], None] | None

    async def read(self, count: int) -> np.ndarray | None:
        """The next ``count`` samples once they have arrived, or ``None`` when
        none arrived within a short wait: the source is waiting for its
        receiver."""

    def describe(self) -> dict[str, Any]:
        """The source's state, as JSON values."""

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change settings; raises ValueError, changing nothing, when refused."""

    def close(self) -> None:
        """Release the receiver for good. Safe to repeat."""

    async def wait_closed(self) -> None:
        """Return once ``close`` has released the receiver, which may take a
        moment, such as stopping a program."""


class FixedSource:
    """Mixin for sources whose settings cannot change."""

    kind: str
    centre_hz: float
    sample_rate_hz: float
    on_change: Callable[[], None] | None = None

    def describe(self) -> dict[str, Any]:
        centre = self.centre_hz
        return {"kind": self.kind, "status": "streaming", "detail": "", "address": None,
                "tuner": None, "centre_hz": centre, "sample_rate_hz": self.sample_rate_hz,
                "gain_db": None, "ppm": None, "gains_db": [], "range_hz": [centre, centre],
                "settable": []}

    def configure(self, changes: Mapping[str, object]) -> None:
        raise ValueError(f"the {self.kind} source cannot change its settings")

    def close(self) -> None:
        """Nothing to release."""

    async def wait_closed(self) -> None:
        """Nothing to wait for."""


class Subscription:
    """What the engine has for one client, in the order it goes out: the
    source state when it changed (first of all at the start), replies to
    the client's commands, then the latest frames."""

    def __init__(self, state: Callable[[], dict[str, Any]], frames: int,
                 replies: int = CLIENT_REPLIES) -> None:
        self._state = state
        self._state_due = True
        self._replies: deque[dict[str, Any]] = deque(maxlen=replies)
        self._frames: deque[bytes] = deque(maxlen=frames)
        self._ended = False
        self._ready = asyncio.Event()

    def get_nowait(self) -> bytes | dict[str, Any] | None:
        """The next message: an encoded frame or a JSON object, and ``None``
        once the engine stopped. Raises ``asyncio.QueueEmpty`` when none is
        ready."""
        if self._ended:
            return None
        if self._state_due:
            self._state_due = False
            return self._state()
        if self._replies:
            return self._replies.popleft()
        if self._frames:
            return self._frames.popleft()
        raise asyncio.QueueEmpty

    async def get(self) -> bytes | dict[str, Any] | None:
        """Like ``get_nowait``, waiting for a message when none is ready."""
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                self._ready.clear()
                await self._ready.wait()

    def put_frame(self, frame: bytes) -> None:
        """Add a frame; when the client fell behind, its oldest frame goes."""
        self._frames.append(frame)
        self._ready.set()

    def state_changed(self) -> None:
        self._state_due = True
        self._ready.set()

    def reply(self, message: dict[str, Any]) -> None:
        """Answer this client only, such as with the reason for a refused change."""
        self._replies.append(message)
        self._ready.set()

    def end(self) -> None:
        self._ended = True
        self._replies.clear()
        self._frames.clear()
        self._ready.set()


class Engine:
    """Turn a source into display frames for any number of clients."""

    def __init__(self, source: Source, *, frame_rate: float = FRAME_RATE,
                 analyzer: SpectrumAnalyzer | None = None,
                 queue_size: int = CLIENT_QUEUE) -> None:
        if frame_rate <= 0:
            raise ValueError(f"the frame rate must be positive, not {frame_rate}")
        self.source = source
        self.analyzer = analyzer if analyzer is not None else SpectrumAnalyzer()
        self._frame_rate = frame_rate
        self._queue_size = queue_size
        self._dc_removal = True
        self._clients: set[Subscription] = set()
        self._wanted = asyncio.Event()     # set while a client listens, or to stop
        self._closed = False
        self._sequence = 0
        source.on_change = self._source_changed

    def subscribe(self) -> Subscription:
        """The messages for a new client, starting with the source state."""
        subscription = Subscription(self.state, self._queue_size)
        if self._closed:
            subscription.end()
        else:
            self._clients.add(subscription)
            self._wanted.set()
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._clients.discard(subscription)
        if not self._clients and not self._closed:
            self._wanted.clear()

    def state(self) -> dict[str, Any]:
        """The source state message (docs/design/protocol.md), with the
        engine's own setting."""
        described = self.source.describe()
        return {"type": "source", **described, "dc_removal": self._dc_removal,
                "settable": [*described["settable"], "dc_removal"]}

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change settings: ``dc_removal`` is the engine's own, the others go
        to the source. Raises ValueError, changing nothing, when one is
        refused. Every client then receives the new state."""
        dc_removal = changes.get("dc_removal", self._dc_removal)
        if not isinstance(dc_removal, bool):
            raise ValueError("DC removal must be true or false")
        source_changes = {name: value for name, value in changes.items() if name != "dc_removal"}
        if source_changes:
            self.source.configure(source_changes)
        self._dc_removal = dc_removal
        self._source_changed()

    def close(self) -> None:
        """Stop for good: every client's messages end with ``None``, and the
        source is closed. Safe to repeat."""
        self._closed = True
        for subscription in self._clients:
            subscription.end()
        self._clients.clear()
        self._wanted.set()
        self.source.close()

    async def wait_closed(self) -> None:
        """Return once the source, after ``close``, has released its receiver."""
        await self.source.wait_closed()

    async def run(self) -> None:
        """Produce frames until closed or cancelled."""
        try:
            while True:
                await self._wanted.wait()
                if self._closed:
                    return
                block = max(self.analyzer.fft_size, round(self.source.sample_rate_hz / self._frame_rate))
                samples = await self.source.read(block)
                if samples is None:
                    continue        # the source is waiting: check that clients still listen
                codes = await asyncio.to_thread(_display_codes, self.analyzer, samples,
                                                self._dc_removal)
                self._publish(codes)
        except Exception:
            log.exception("the display engine stopped")
            self.close()

    def _source_changed(self) -> None:
        for subscription in self._clients:
            subscription.state_changed()

    def _publish(self, codes: np.ndarray) -> None:
        if not self._clients:
            return
        frame = encode_frame(self._sequence, self.source.centre_hz, self.source.sample_rate_hz,
                             self.analyzer.db_min, self.analyzer.db_step, codes)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        for subscription in self._clients:
            subscription.put_frame(frame)


def _display_codes(analyzer: SpectrumAnalyzer, samples: np.ndarray, dc_removal: bool) -> np.ndarray:
    """The display codes of a block of samples, worked out in a worker thread."""
    if dc_removal:
        # A mean in single precision would leave a trace of a strong offset.
        samples = samples - np.complex64(samples.mean(dtype=np.complex128))
    return analyzer.frame(samples)
