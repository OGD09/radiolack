"""Receiver engine: reads a source and shares display frames and audio with
clients.

The engine runs in the server's event loop. It reads blocks of I/Q samples
from its source only while at least one client listens or a scan runs, and in a worker
thread turns each block into a display frame, which carries the VFO's
channel level and squelch, and into audio (demod.py), which goes only to the
clients that asked for it. A client that falls behind loses its oldest
frames instead of slowing the others. The engine also carries the source's
state and the demodulation settings to every client, and the clients'
changes to them: a change reaches every client at once, and the last change
wins. Each client also learns which of its own demodulation changes the
engine has handled, so that it can tell an echo of an older change from the
latest state.

DC removal is the engine's own setting, on by default and shared by every
client like the source's: it takes each block's mean off its samples, so that
a constant offset, such as an RTL-SDR's, does not show as a spike at the
centre frequency. It also removes a signal exactly on the centre frequency.

With the sources it may open (sources.py), the engine switches to another
kind of source when a client asks: the source in use closes, keeping its
settings, and the new one starts from those saved for its receiver. Without
a source to start with, it opens the one used last, the first kind the first
time.

The engine finds the signals in its spectra (scanners.py) while it reads,
unless detection is turned off: every client receives the list of the
signals found, at most once a second, and the scanner settings, which are
shared like the source's and saved with them, ignored frequencies included.
It keeps what it finds, apart from the simulated band's, as observations
(observations.py), which a client receives when it asks, and may clear.
Any client may start a scan, one at a time: the memory scan, which tunes the
VFO to the ticked bookmarks of the span as they become active, and with a
receiver that tunes visits those outside the span too, window after window;
or, with a receiver that tunes, the sweep, which steps the receiver across a
range in windows and brings it back, with the VFO, once a client stops it.
A memory scan that stops while it watches, once it moved the receiver,
brings them back too. A scan keeps the engine reading without
any client, until a client stops it, tunes by hand or chooses another
source. Whether it runs is never saved: while a scan runs, the receiver's
saved settings are those of the scan's start.

The engine also keeps the bookmarks, shared by every client like the
settings, and the interfaces' preferences (preferences.py), which a new
interface starts with: one set for this computer's interfaces, and one for
each paired device once it gave one, until then this computer's. With an
update check (updates.py), it checks for a newer Radiolack when it starts,
unless turned off, and when a client asks, and tells every client what the
check found.

With remote access (remote.py), the engine tells this computer's interfaces
its state, and saves what it keeps, each paired device's preferences
included. Remote clients, the devices that remote access serves, receive
neither that state nor the storage and update states, and may not change
the rtl_tcp server's address. Their links may be slower than this computer's
own: they get one display frame for every two blocks, the mean power of
both, in fewer bins.

With a store (store.py), it starts from what was saved: the settings saved
for its receiver (each receiver, by its ``settings_key``, has its own), the
bookmarks, the preferences, whether to check for updates at start, what
remote access keeps and the observations. It saves them a moment after they
change, as changes come in bursts, and when it stops; the observations,
which change with every signal found, at most every 10 minutes, unless a
client cleared some, and when it stops. A saved setting that the receiver
refuses keeps its default, and one the source was ``given``, such as an
address on the command line, keeps the given value, which is saved in its
place.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import numpy as np

from . import __version__
from .bookmarks import Bookmark, Bookmarks
from .demod import (AUDIO_MODES, MODE_BANDWIDTHS_HZ, MODES, SQUELCH_MARGIN_DB, DemodSettings, Demodulator, Meter,
                    noise_clearance_hz)
from .observations import Observations
from .preferences import check_preferences, saved_preferences
from .remote import REMOTE_FILE, RemoteAccess
from .scanners import (DEFAULT_RESUME_S, DEFAULT_SWEEP_HZ, DEFAULT_THRESHOLD_DB, DEFAULT_WINDOW_S, DETECTION_BLOCKS,
                       MAX_IGNORED, Channel, ChannelWatch, Detection, Detector, MemoryScan, MemoryTour, Signal, Signals,
                       Sweep, SweepWindows, checked_frequency, checked_resume, checked_sweep_range, checked_threshold,
                       checked_window, memory_window, memory_windows, search_blocks, sweep_windows, watched_channels)
from .spectrum import SpectrumAnalyzer, encode_frame, fewer_bins
from .store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store
from .updates import UpdateResult

FRAME_RATE = 30.0       # display frames per second
REMOTE_BLOCKS = 2       # a remote client's display frame: the mean power of this many blocks
REMOTE_BINS = 2048      # at most this many bins in a remote client's display frames
CLIENT_QUEUE = 4        # frames kept for a client that falls behind
CLIENT_REPLIES = 8      # replies kept for a client that falls behind
CLIENT_AUDIO = 15       # audio frames (half a second) kept for a client that falls behind
SAVE_DELAY_S = 1.0      # changes are saved this long after the first of a burst
OBSERVATIONS_SAVE_S = 600.0     # the observations are written at most this often, and when the engine stops
UNRECORDED_KINDS = ("synthetic",)   # sources whose signals are not observations: the simulated band
PEAKS_INTERVAL_S = 1.0  # the signals found go to the clients at most this often
SCANNER_SETTINGS = ("detection", "threshold_db", "resume_s", "sweep_low_hz", "sweep_high_hz", "sweep_window_s",
                    "ignore", "restore", "scan")
OBSERVATIONS_FIELDS = ("clear", "clear_all")
LOCAL_STATES = ("storage", "update", "remote")    # states only this computer's interfaces receive
LOCAL_SETTINGS = ("address",)   # source settings only this computer's interfaces may change

log = logging.getLogger(__name__)


class Source(Protocol):
    """A source of I/Q samples: complex64, in full-scale units.

    ``centre_hz`` and ``sample_rate_hz`` describe the samples ``read`` last
    returned. ``describe`` gives the state the interfaces show
    (docs/design/protocol.md), and the source calls ``on_change`` whenever
    that state changes. ``settings_key`` names the receiver whose settings
    the engine saves: each key has its own. ``given`` names the settings
    chosen when the source was made, which saved ones do not replace.
    ``reopen`` is what opens the same receiver again (sources.py).
    """

    kind: str
    centre_hz: float
    sample_rate_hz: float
    given: frozenset[str]
    on_change: Callable[[], None] | None

    @property
    def settings_key(self) -> str:
        """Such as "rtl_sdr 0": the kind of source and which receiver."""

    @property
    def reopen(self) -> dict[str, Any]:
        """Such as {"kind": "rtl_sdr", "device": "0"}, as JSON values."""

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
    given: frozenset[str] = frozenset()
    on_change: Callable[[], None] | None = None

    @property
    def settings_key(self) -> str:
        return self.kind

    @property
    def reopen(self) -> dict[str, Any]:
        return {"kind": self.kind}

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


class SourceOpener(Protocol):
    """Opens sources by kind (sources.py): ``kinds``, the first being the
    default, are those an engine may switch between."""

    kinds: Sequence[str]

    def open(self, reopen: Mapping[str, object]) -> Source:
        """A new source from what ``Source.reopen`` gave; raises ValueError
        when it cannot be opened."""


class UpdateChecker(Protocol):
    """Checks for a newer Radiolack (updates.py)."""

    def unavailable_reason(self) -> str | None:
        """Why no check can run, in a sentence for the user, or None."""

    async def check(self) -> UpdateResult:
        """What a check finds; it reports a failure in its result."""


class Subscription:
    """What the engine has for one client, in the order it goes out: the
    states that changed (all of them at the start, in the order of
    ``states``), replies to the client's commands, audio frames if the client
    asked for audio, then the latest display frames. A remote client is a
    paired ``device`` that remote access serves, named by its id."""

    def __init__(self, states: Mapping[str, Callable[[Subscription], dict[str, Any]]], frames: int,
                 replies: int = CLIENT_REPLIES, audio: int = CLIENT_AUDIO, device: str | None = None) -> None:
        self._states = dict(states)     # each gives its message for this client
        self._due = set(self._states)
        self._replies: deque[dict[str, Any]] = deque(maxlen=replies)
        self._audio: deque[bytes] = deque(maxlen=audio)
        self._frames: deque[bytes] = deque(maxlen=frames)
        self._ended = False
        self._ready = asyncio.Event()
        self.device = device
        self.audio = False          # the client asked for audio
        self.acknowledged = 0       # id of the client's last demodulation change handled

    @property
    def remote(self) -> bool:
        """Whether the client is a device that remote access serves."""
        return self.device is not None

    def get_nowait(self) -> bytes | dict[str, Any] | None:
        """The next message: an encoded frame or a JSON object, and ``None``
        once the engine stopped. Raises ``asyncio.QueueEmpty`` when none is
        ready."""
        if self._ended:
            return None
        for kind, describe in self._states.items():
            if kind in self._due:
                self._due.discard(kind)
                return describe(self)
        if self._replies:
            return self._replies.popleft()
        if self._audio:
            return self._audio.popleft()
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

    def put_audio(self, frame: bytes) -> None:
        """Add an audio frame if the client wants audio; when it fell behind,
        its oldest audio frame goes."""
        if self.audio:
            self._audio.append(frame)
            self._ready.set()

    def set_audio(self, on: bool) -> None:
        self.audio = on
        if not on:
            self._audio.clear()

    def changed(self, kind: str) -> None:
        """State ``kind`` changed: the client receives it again, unless it
        never receives that state."""
        if kind in self._states:
            self._due.add(kind)
            self._ready.set()

    def reply(self, message: dict[str, Any]) -> None:
        """Answer this client only, such as with the reason for a refused change."""
        self._replies.append(message)
        self._ready.set()

    def end(self) -> None:
        self._ended = True
        self._replies.clear()
        self._audio.clear()
        self._frames.clear()
        self._ready.set()


class Engine:
    """Turn a source into display frames and audio for any number of clients.

    The engine reads ``source``, or without one the source that ``sources``
    opened last, and switches between the kinds of ``sources`` when a client
    asks. It checks for updates with ``updates``; without it, it never
    does. With ``remote``, it shares and saves remote access's state."""

    def __init__(self, source: Source | None = None, *, sources: SourceOpener | None = None,
                 store: Store | None = None, updates: UpdateChecker | None = None,
                 remote: RemoteAccess | None = None,
                 frame_rate: float = FRAME_RATE, analyzer: SpectrumAnalyzer | None = None,
                 queue_size: int = CLIENT_QUEUE, save_delay_s: float = SAVE_DELAY_S,
                 observations_save_s: float = OBSERVATIONS_SAVE_S) -> None:
        if frame_rate <= 0:
            raise ValueError(f"the frame rate must be positive, not {frame_rate}")
        self._sources = sources
        self.analyzer = analyzer if analyzer is not None else SpectrumAnalyzer()
        self._frame_rate = frame_rate
        self._queue_size = queue_size
        self._dc_removal = True
        self._clients: set[Subscription] = set()
        self._wanted = asyncio.Event()     # set while a client listens, or to stop
        self._closed = False
        self._sequence = 0
        self._remote_sequence = 0           # remote clients' frames have their own sequence
        self._remote_blocks: list[np.ndarray] = []  # bin powers of the blocks of their next frame
        self._remote_capture: tuple[float, float] | None = None    # centre and rate of those blocks
        self._demodulator = Demodulator()   # used by one block at a time
        self._capture: tuple[float, float] | None = None    # centre and rate of the last block
        self._closing: set[asyncio.Future] = set()  # sources switched away from, until they release their receiver
        # What each client receives first, in this order, then again whenever
        # it changes; remote clients, none of LOCAL_STATES.
        self._states: dict[str, Callable[[Subscription], dict[str, Any]]] = {
            "source": lambda subscription: self.state(subscription.remote),
            "demod": lambda subscription: self.demod_state(subscription.acknowledged),
            "bookmarks": lambda _: self.bookmarks_state(),
            "scanner": lambda _: self.scanner_state(),
            "peaks": lambda _: self.peaks_state(),
            "preferences": lambda subscription: self.preferences_state(subscription.device),
            "storage": lambda _: self.storage_state(),
            "update": lambda _: self.update_state(),
        }
        self._remote = remote
        if remote is not None:
            if store is not None:
                remote.restore(store.load(REMOTE_FILE))
            self._states["remote"] = lambda _: self.remote_state()
            remote.on_change = self._remote_changed
        # Saving: the store once the saved settings apply, what each file held
        # at its latest write, and the task that writes them after a change.
        self._store: Store | None = None
        self._save_delay_s = save_delay_s
        self._saved: dict[str, dict[str, Any]] = {}
        self._unsaved = False
        self._save_now = asyncio.Event()
        self._saver: asyncio.Future | None = None

        settings = store.load(SETTINGS_FILE) if store is not None else {}
        receivers = settings.get("receivers")
        # The other receivers' settings are written back as they were read.
        self._receivers = ({key: value for key, value in receivers.items() if isinstance(value, dict)}
                           if isinstance(receivers, dict) else {})
        saved_choice = settings.get("source")
        # What opens each kind of source again: the receiver used last of that kind.
        self._reopen: dict[str, dict[str, Any]] = {}
        if isinstance(saved_choice, dict) and isinstance(saved_choice.get("kind"), str):
            self._reopen[saved_choice["kind"]] = saved_choice
        if source is not None:
            self.source = source
        elif sources is not None:
            self.source = _open_saved(sources, saved_choice)
        else:
            raise ValueError("an engine needs a source, or sources to open one")
        self._reopen[self.source.kind] = self.source.reopen
        saved_source = self._receivers.get(self.source.settings_key, {}).get("source")
        self._bookmarks = Bookmarks(store.load(BOOKMARKS_FILE).get("bookmarks", []) if store is not None else [])
        # Observations: what detection found, kept across restarts. Their file
        # is written at the next save once due: observations_save_s after its
        # latest write, or at once after a client cleared some.
        self._observations = Observations(store.load(OBSERVATIONS_FILE) if store is not None else None, time.time())
        self._observations_save_s = observations_save_s
        self._observations_due = False
        self._observations_saved_s = time.monotonic()
        self._observations_timer: asyncio.TimerHandle | None = None
        # Scanners: detection's settings, the signals found and the ignored
        # frequencies; the memory scan's resume time, the bookmarks it
        # watches, the windows they take and the channels of the span; the
        # sweep's range and time per window; the scan, while one runs, and
        # the receiver's centre frequency and the VFO at its start.
        self._detector = Detector()         # used by one block at a time, like the demodulator
        self._detection = True
        self._threshold_db = DEFAULT_THRESHOLD_DB
        self._signals = Signals()
        self._resume_s = DEFAULT_RESUME_S
        self._watch = ChannelWatch()        # used by one block at a time, like the demodulator
        self._memory_ids: tuple[int, ...] = ()      # the ticked bookmarks within reach, in frequency order
        self._windows: tuple[float, ...] = ()       # the centre frequencies of the windows they take
        self._watched: tuple[Channel, ...] = ()     # while the memory scan runs, the ticked bookmarks of the span
        self._bands = np.empty((0, 3))      # their passbands and noise clearances, one row each
        self._sweep_low_hz, self._sweep_high_hz = DEFAULT_SWEEP_HZ
        self._sweep_window_s = DEFAULT_WINDOW_S
        self._memory: MemoryScan | None = None
        self._tour: MemoryTour | None = None        # the memory scan's windows, while it runs
        self._sweep: Sweep | None = None
        self._home: tuple[float, float] | None = None   # centre frequency and VFO at the start of the scan that runs
        # Which scan stopped by itself last, and why; None once a client stopped one, or starts one.
        self._stopped: tuple[str, str] | None = None
        self._restore_scanner(settings.get("scanner"))
        self._peaks_sent_s = -math.inf      # when the clients were last told of the signals
        self._peaks_timer: asyncio.TimerHandle | None = None
        self._adopt()
        self._watch_bookmarks()
        self._preferences = saved_preferences(settings.get("interface")) or {}     # this computer's
        # Updates: whether to check at start, what the latest check found, and the check running.
        self._updates = updates
        self._check_at_start = _saved_check_at_start(settings.get("updates"))
        reason = "This Radiolack does not check for updates." if updates is None else updates.unavailable_reason()
        self._update = UpdateResult("idle") if reason is None else UpdateResult("unavailable", detail=reason)
        self._checking: asyncio.Future | None = None
        if store is not None:
            self._saved = self._contents(observations=True)     # nothing to write until something changes,
            # but a source or a setting given at start replaces the saved one at the next write.
            default_choice = {"kind": self._sources.kinds[0]} if self._sources is not None else None
            if (_given_differ(self.state(), saved_source, self.source.given)
                    or self._sources is not None
                    and (saved_choice if saved_choice is not None else default_choice) != self.source.reopen):
                del self._saved[SETTINGS_FILE]
            self._store = store

    def subscribe(self, device: str | None = None) -> Subscription:
        """The messages for a new client, starting with the source state, the
        demodulation settings, the bookmarks, the scanner state, the signals
        found and the preferences, then, unless
        it is a paired ``device`` that remote access serves, named by its id,
        the storage state, the update state and remote access's state."""
        states = {kind: describe for kind, describe in self._states.items()
                  if device is None or kind not in LOCAL_STATES}
        subscription = Subscription(states, self._queue_size, device=device)
        if self._closed:
            subscription.end()
        else:
            self._clients.add(subscription)
            self._wanted.set()
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._clients.discard(subscription)
        if not self._clients and not self._scanning() and not self._closed:
            self._wanted.clear()

    def state(self, remote: bool = False) -> dict[str, Any]:
        """The source state message (docs/design/protocol.md), with the
        engine's own setting; for a ``remote`` client, without the settings
        it may not change."""
        described = self.source.describe()
        settable = [*described["settable"], "dc_removal"]
        if remote:
            settable = [name for name in settable if name not in LOCAL_SETTINGS]
        return {"type": "source", **described, "dc_removal": self._dc_removal, "settable": settable,
                "kinds": list(self._sources.kinds) if self._sources is not None else []}

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change settings: ``kind`` switches to another kind of source, and
        comes alone; ``dc_removal`` is the engine's own; the others go to the
        source. Raises ValueError, changing nothing, when one is refused.
        Every client then receives the new state. A new centre frequency or
        sample rate stops a scan, before the receiver's samples show it."""
        if "kind" in changes:
            if len(changes) > 1:
                raise ValueError("the kind of source changes on its own, without other settings")
            self._switch(changes["kind"])
            return
        dc_removal = changes.get("dc_removal", self._dc_removal)
        if not isinstance(dc_removal, bool):
            raise ValueError("DC removal must be true or false")
        source_changes = {name: value for name, value in changes.items() if name != "dc_removal"}
        if source_changes:
            span = self._span()
            self.source.configure(source_changes)
            if self._span() != span:
                self._stop_scan("tuned")
        self._dc_removal = dc_removal
        self._source_changed()

    def demod_state(self, acknowledged: int = 0) -> dict[str, Any]:
        """The demodulation state message (docs/design/protocol.md) for a
        client whose changes up to ``acknowledged`` have been handled."""
        return {"type": "demod", **self._demod.describe(), "modes": list(MODES),
                "audio_modes": list(AUDIO_MODES), "acknowledged": acknowledged}

    def configure_demod(self, changes: Mapping[str, object], subscription: Subscription | None = None,
                        change_id: int | None = None) -> None:
        """Change demodulation settings. Raises ValueError, changing nothing,
        when one is refused; otherwise every client receives the new
        settings. ``change_id`` acknowledges the change to ``subscription``,
        the client that asked for it, which receives the settings in either
        case. Tuning the VFO stops a scan."""
        if subscription is not None and change_id is not None:
            subscription.acknowledged = change_id
            subscription.changed("demod")
        demod = self._demod.changed(changes)
        if demod.frequency_hz != self._demod.frequency_hz:
            self._stop_scan("tuned")
        self._demod = demod
        self._demod_changed()

    def set_audio(self, subscription: Subscription, on: bool) -> None:
        """Start or stop sending audio to one client."""
        subscription.set_audio(on)

    def bookmarks_state(self) -> dict[str, Any]:
        """The bookmarks message (docs/design/protocol.md)."""
        return {"type": "bookmarks", "bookmarks": self._bookmarks.describe()}

    def edit_bookmarks(self, command: Mapping[str, object]) -> None:
        """Add, change or remove a bookmark (bookmarks.py). Raises ValueError,
        changing nothing, when refused; otherwise every client receives the
        bookmarks."""
        self._bookmarks.apply(command)
        for subscription in self._clients:
            subscription.changed("bookmarks")
        self._settings_changed()
        self._watch_bookmarks()

    def scanner_state(self) -> dict[str, Any]:
        """The scanner state message (docs/design/protocol.md)."""
        memory, tour, sweep = self._memory, self._tour, self._sweep
        return {"type": "scanner", "detection": self._detection, "threshold_db": self._threshold_db,
                "ignored_hz": self._signals.ignored, "resume_s": self._resume_s,
                "sweep_low_hz": self._sweep_low_hz, "sweep_high_hz": self._sweep_high_hz,
                "sweep_window_s": self._sweep_window_s,
                "scan": "memory" if memory is not None else "sweep" if sweep is not None else None,
                "stopped": None if self._stopped is None else self._stopped[1],
                "stopped_scan": None if self._stopped is None else self._stopped[0],
                "memory": {"watched": list(self._memory_ids), "windows": len(self._windows),
                           "window": None if tour is None else tour.window,
                           "state": None if memory is None else memory.state,
                           "bookmark_id": None if memory is None else memory.bookmark_id},
                "sweep": {"windows": None if sweep is None else sweep.windows.count,
                          "window": None if sweep is None else sweep.window,
                          "passes": None if sweep is None else sweep.passes}}

    def peaks_state(self) -> dict[str, Any]:
        """The peaks message: the signals found, as they stand now."""
        return {"type": "peaks", "peaks": self._signals.describe(time.monotonic())}

    def configure_scanner(self, changes: Mapping[str, object]) -> None:
        """Change detection's settings, ``detection`` (true or false) and
        ``threshold_db``, the memory scan's ``resume_s``, and the sweep's
        range, ``sweep_low_hz`` to ``sweep_high_hz``, and time per window,
        ``sweep_window_s``; or, each alone in its message, ``ignore`` a
        frequency, ``restore`` (true) every ignored one, or start a scan
        (``scan``: "memory" or "sweep") or stop the one that runs (``scan``:
        None). A new range starts a running sweep again from its first
        window. Raises ValueError, changing nothing, when one is refused;
        otherwise every client receives the scanner state, and the signals
        found once they changed."""
        if not changes:
            raise ValueError("a scanner message needs a setting")
        for name in changes:
            if name not in SCANNER_SETTINGS:
                raise ValueError(f"unknown scanner setting: {str(name)[:40]}")
        if any(name in changes for name in ("ignore", "restore", "scan")) and len(changes) > 1:
            raise ValueError("ignore, restore and scan come alone in their message")
        if "ignore" in changes:
            self._signals.ignore(checked_frequency(changes["ignore"], "an ignored frequency"))
        elif "restore" in changes:
            if changes["restore"] is not True:
                raise ValueError("restore takes true")
            self._signals.restore()
        elif "scan" in changes:
            self._start_scan(changes["scan"])
        else:
            detection = changes.get("detection", self._detection)
            if not isinstance(detection, bool):
                raise ValueError("detection must be turned on or off: true or false")
            threshold_db = (checked_threshold(changes["threshold_db"]) if "threshold_db" in changes
                            else self._threshold_db)
            resume_s = checked_resume(changes["resume_s"]) if "resume_s" in changes else self._resume_s
            low_hz, high_hz = self._sweep_low_hz, self._sweep_high_hz
            if "sweep_low_hz" in changes or "sweep_high_hz" in changes:
                low_hz, high_hz = checked_sweep_range(changes.get("sweep_low_hz", low_hz),
                                                      changes.get("sweep_high_hz", high_hz))
            window_s = (checked_window(changes["sweep_window_s"]) if "sweep_window_s" in changes
                        else self._sweep_window_s)
            sweep = self._sweep
            moved = sweep is not None and (low_hz, high_hz) != (self._sweep_low_hz, self._sweep_high_hz)
            windows = self._sweep_windows(low_hz, high_hz) if moved else None
            self._detection, self._threshold_db, self._resume_s = detection, threshold_db, resume_s
            self._sweep_low_hz, self._sweep_high_hz, self._sweep_window_s = low_hz, high_hz, window_s
            if not detection:
                self._signals.clear()
            if sweep is not None:
                sweep.search_blocks = search_blocks(window_s, self._frame_rate)
                if windows is not None:
                    sweep.restart(windows, time.monotonic())
                    self._tune_scan(sweep.centre_hz)
        self._scanner_changed()
        self._settings_changed()
        if self._signals.changed(time.monotonic()):
            self._peaks_changed(at_once=True)

    def observations_state(self) -> dict[str, Any]:
        """The observations message: the channels where detection found
        signals, the latest found first."""
        if self._observations.forget(time.time()):
            self._observations_changed()
        return {"type": "observations", "channels": self._observations.describe()}

    def configure_observations(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Answer a client's observations message, which without any field
        asks for the observations; alone in it, ``clear`` forgets the channel
        within 1.5 kHz of a frequency, if there is one, and ``clear_all``
        (true) forgets them all. The client then receives the observations.
        Raises ValueError, changing nothing, when refused."""
        for name in changes:
            if name not in OBSERVATIONS_FIELDS:
                raise ValueError(f"unknown observations field: {str(name)[:40]}")
        if len(changes) > 1:
            raise ValueError("clear and clear_all come alone in their message")
        if "clear" in changes:
            cleared = self._observations.clear(checked_frequency(changes["clear"], "a channel to clear"))
        elif "clear_all" in changes:
            if changes["clear_all"] is not True:
                raise ValueError("clear_all takes true")
            cleared = self._observations.clear_all()
        else:
            cleared = False
        if cleared:
            self._observations_changed(soon=True)
        subscription.reply(self.observations_state())

    def preferences_state(self, device: str | None = None) -> dict[str, Any]:
        """The preferences message: what a new interface of this computer, or
        of the paired ``device``, starts with. A device that gave none starts
        with this computer's."""
        own = self._remote.preferences(device) if device is not None and self._remote is not None else None
        return {"type": "preferences", "values": dict(self._preferences if own is None else own)}

    def set_preferences(self, values: object, device: str | None = None) -> None:
        """Keep the preferences of an interface of this computer, or of the
        paired ``device``, which the next interfaces of the same computer or
        device start with: names with numbers, true or false, short texts or
        null (preferences.py). Raises ValueError, changing nothing, when one
        is not valid or the device is no longer paired."""
        checked = check_preferences(values)
        if device is None:
            self._preferences = checked
        elif self._remote is None:
            raise ValueError("this device is no longer paired")
        else:
            self._remote.set_preferences(device, checked)
        self._settings_changed()

    def storage_state(self) -> dict[str, Any]:
        """The storage message: where the settings are saved, whether changes
        reach the disk, and what went wrong with the files."""
        store = self._store
        if store is None:
            return {"type": "storage", "directory": None, "saving": False, "problem": ""}
        return {"type": "storage", "directory": store.location, "saving": store.saving,
                "problem": store.problem}

    def update_state(self) -> dict[str, Any]:
        """The update message: what the latest check found, and whether the
        engine checks at start."""
        found = self._update
        return {"type": "update", "status": found.status, "version": __version__, "latest": found.latest,
                "detail": found.detail, "check_at_start": self._check_at_start}

    def configure_updates(self, changes: Mapping[str, object]) -> None:
        """``check_at_start``, true or false, turns the check at the next
        starts on or off; ``check``, true, checks now, unless a check is
        running. Raises ValueError, changing nothing, when one is refused."""
        for name in changes:
            if name not in ("check", "check_at_start"):
                raise ValueError(f"unknown update setting: {str(name)[:40]}")
        check = changes.get("check", False)
        check_at_start = changes.get("check_at_start", self._check_at_start)
        if not isinstance(check, bool) or not isinstance(check_at_start, bool):
            raise ValueError("check and check_at_start must be true or false")
        if check_at_start != self._check_at_start:
            self._check_at_start = check_at_start
            self._update_changed()
            self._settings_changed()
        if check:
            self.check_updates()

    def check_updates(self) -> None:
        """Check for a newer Radiolack in the background, unless a check is
        running: every client learns that it started, then what it found."""
        updates = self._updates
        if updates is None or self._closed or (self._checking is not None and not self._checking.done()):
            return
        self._update = UpdateResult("checking")
        self._update_changed()
        self._checking = asyncio.ensure_future(self._check_updates(updates))

    def remote_state(self) -> dict[str, Any]:
        """The remote access message (docs/design/protocol.md), for this
        computer's interfaces only."""
        if self._remote is None:
            raise RuntimeError("this engine has no remote access")
        return {"type": "remote", **self._remote.describe()}

    def close(self) -> None:
        """Stop for good: every client's messages end with ``None``, the
        source is closed and a running update check stops. Safe to repeat."""
        self._closed = True
        for subscription in self._clients:
            subscription.end()
        self._clients.clear()
        self._wanted.set()
        self.source.close()
        if self._checking is not None:
            self._checking.cancel()
        if self._peaks_timer is not None:
            self._peaks_timer.cancel()
            self._peaks_timer = None
        if self._observations_timer is not None:
            self._observations_timer.cancel()
            self._observations_timer = None

    async def wait_closed(self) -> None:
        """Return once the source, after ``close``, has released its receiver,
        like those switched away from, the update check has stopped its
        programs, and the latest changes are saved."""
        if self._checking is not None:
            await asyncio.gather(self._checking, return_exceptions=True)
        await self.source.wait_closed()
        if self._closing:
            await asyncio.wait(set(self._closing))
        if self._store is None:
            return
        self._save_now.set()
        if self._saver is not None:
            await asyncio.gather(self._saver, return_exceptions=True)
        self._observations_due = True
        await self._save()      # also what a failed write left unsaved, and the observations

    async def run(self) -> None:
        """Produce frames until closed or cancelled, having started the update
        check unless it is turned off at start or cannot run."""
        if self._check_at_start and self._update.status != "unavailable":
            self.check_updates()
        try:
            while True:
                await self._wanted.wait()
                if self._closed:
                    return
                if self._closing:
                    # The new source may need the same receiver: it starts once released.
                    closing = set(self._closing)
                    await asyncio.wait(closing)     # unlike gather, a cancellation leaves them running
                    self._closing -= closing
                    continue
                source = self.source
                block = max(self.analyzer.fft_size, round(source.sample_rate_hz / self._frame_rate))
                samples = await source.read(block)
                if samples is None or source is not self.source:
                    # Waiting, so check that clients still listen, and that
                    # signals no longer found are no longer shown as active.
                    self._check_peaks()
                    continue        # or switched away from
                capture = (source.centre_hz, source.sample_rate_hz)
                if capture != self._capture:
                    self._capture = capture
                    self._keep_vfo_in(*capture)
                    self._watch_bookmarks()     # in case the source retuned by itself, as when it connects
                audio = any(subscription.audio for subscription in self._clients)
                threshold_db = self._threshold_db if self._detection else None
                # The memory scan's channels are those of the span asked of the
                # receiver: none while the samples still come from another.
                channels = self._watched if self._memory is not None and capture == self._span() else None
                sweep = self._sweep
                power, codes, meter, pcm, detections, levels = await asyncio.to_thread(
                    _process_block, self.analyzer, self._demodulator, self._detector, self._watch, samples,
                    *capture, self._dc_removal, self._demod, audio, threshold_db,
                    DETECTION_BLOCKS if sweep is None else sweep.search_blocks,
                    None if channels is None else self._bands)
                self._publish(capture, power, codes, meter, pcm)
                # Detection turned off, or another source, meanwhile: these signals are not news.
                if detections is not None and self._detection and source is self.source:
                    found = self._signals.update(detections, time.monotonic())
                    self._check_peaks()
                    if found and source.kind not in UNRECORDED_KINDS:
                        self._record(found)
                if self._memory is not None and source is self.source:
                    self._step_memory(channels, levels, meter.open, capture[0])
                if sweep is not None and sweep is self._sweep and source is self.source:
                    self._step_sweep(sweep, capture[0])
        except Exception:
            log.exception("the engine stopped")
            self.close()

    def _source_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("source")
        self._settings_changed()
        self._watch_bookmarks()

    def _demod_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("demod")
        self._settings_changed()

    def _update_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("update")

    def _remote_changed(self) -> None:
        """Remote access changed: this computer's interfaces learn it, and what
        it keeps is saved if that changed."""
        for subscription in self._clients:
            subscription.changed("remote")
        self._settings_changed()

    def _check_peaks(self) -> None:
        """Tell the clients of the signals found once the list changed, such
        as when a signal is no longer active."""
        if self._signals.changed(time.monotonic()):
            self._peaks_changed()

    def _peaks_changed(self, at_once: bool = False) -> None:
        """The signals found changed: the clients receive them now when
        ``at_once``, as after an interface's change, or when they last did at
        least PEAKS_INTERVAL_S ago, and otherwise once that long has passed."""
        if self._closed:
            return
        wait = 0.0 if at_once else self._peaks_sent_s + PEAKS_INTERVAL_S - time.monotonic()
        if wait > 0:
            if self._peaks_timer is None:
                self._peaks_timer = asyncio.get_running_loop().call_later(wait, self._send_peaks)
            return
        if self._peaks_timer is not None:
            self._peaks_timer.cancel()
        self._send_peaks()

    def _send_peaks(self) -> None:
        """Every client receives the signals found, as they stand when it does."""
        self._peaks_timer = None
        self._peaks_sent_s = time.monotonic()
        for subscription in self._clients:
            subscription.changed("peaks")

    def _scanner_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("scanner")

    def _record(self, found: Sequence[Signal]) -> None:
        """Keep the signals found in a search as observations, at this
        computer's time and hour."""
        now = time.time()
        self._observations.record(found, now, time.localtime(now).tm_hour)
        self._observations_changed()

    def _observations_changed(self, soon: bool = False) -> None:
        """The observations changed: their file is written once
        ``observations_save_s`` passed since its latest write, or ``soon``,
        with the next save, as after a client cleared some. ``wait_closed``
        writes it in any case."""
        if self._store is None or self._closed:
            return
        if soon:
            if self._observations_timer is not None:
                self._observations_timer.cancel()
            self._observations_save_due()
        elif self._observations_timer is None and not self._observations_due:
            wait = self._observations_saved_s + self._observations_save_s - time.monotonic()
            self._observations_timer = asyncio.get_running_loop().call_later(max(0.0, wait),
                                                                             self._observations_save_due)

    def _observations_save_due(self) -> None:
        self._observations_timer = None
        self._observations_due = True
        self._settings_changed()

    def _start_scan(self, scan: object) -> None:
        """Start a scan, "memory" or "sweep", unless it runs, stopping the
        other, or stop the scan that runs (None). Raises ValueError, changing
        nothing, for another scan, for the memory scan when no ticked bookmark
        lies within the receiver's reach, from the span it is in or comes
        back to, and for the sweep when the receiver cannot tune to every
        window of its range."""
        if scan is None:
            self._stop_scan(None)
        elif scan == "memory":
            if self._memory is None:
                self._start_memory()
        elif scan == "sweep":
            if self._sweep is None:
                self._start_sweep()
        else:
            raise ValueError('a scan is "memory" or "sweep", or null to stop it')

    def _start_memory(self) -> None:
        if not self._memory_ids:    # from the span a sweep brings the receiver back to
            raise ValueError("no ticked bookmark lies within the receiver's reach")
        self._stop_scan(None)       # the sweep, which brings the receiver back
        centre_hz = self._span()[0]
        self._memory = MemoryScan()
        self._home = (centre_hz, self._demod.frequency_hz)
        self._stopped = None
        self._wanted.set()          # the scan goes on without any client
        self._watch_bookmarks()     # the channels of the span
        if self._memory is not None:    # unless the sweep came back where none lies within reach
            self._tour = MemoryTour(self._windows, centre_hz, time.monotonic())

    def _start_sweep(self) -> None:
        if "centre_hz" not in self.source.describe()["settable"]:
            raise ValueError("the sweep needs a receiver that tunes, such as an RTL-SDR")
        windows = self._sweep_windows(self._sweep_low_hz, self._sweep_high_hz)
        self._stop_scan(None)       # the memory scan
        self._sweep = Sweep(windows, search_blocks(self._sweep_window_s, self._frame_rate), time.monotonic())
        self._home = (self._span()[0], self._demod.frequency_hz)
        self._stopped = None
        self._wanted.set()          # the scan goes on without any client
        self._tune_scan(self._sweep.centre_hz)

    def _sweep_windows(self, low_hz: float, high_hz: float) -> SweepWindows:
        """The sweep's windows from ``low_hz`` to ``high_hz`` at the receiver's
        sample rate; raises ValueError when the receiver cannot tune to them
        all."""
        described = self.source.describe()
        windows = sweep_windows(low_hz, high_hz, described["sample_rate_hz"])
        low, high = described["range_hz"]
        if windows.centre(0) < low or windows.centre(windows.count - 1) > high:
            raise ValueError("the receiver cannot tune to every window of the sweep's range: "
                             f"it tunes from {low / 1e6:g} to {high / 1e6:g} MHz")
        return windows

    def _tune_scan(self, centre_hz: float) -> None:
        """Ask the receiver for a window of the scan that runs; one that
        refuses it stops the scan."""
        try:
            self.source.configure({"centre_hz": centre_hz})
        except ValueError as error:
            scan = "memory scan" if self._memory is not None else "sweep"
            log.warning("the %s stopped: the receiver did not tune to %.0f Hz: %s", scan, centre_hz, error)
            self._stop_scan("range")

    def _step_sweep(self, sweep: Sweep, centre_hz: float) -> None:
        """Move the sweep on with a block read at ``centre_hz``: once a window
        is done, the receiver goes to the next one."""
        before = (sweep.window, sweep.passes)
        target = sweep.step(time.monotonic(), centre_hz)
        if target is not None:
            self._tune_scan(target)
        if (sweep.window, sweep.passes) != before:
            self._scanner_changed()

    def _scanning(self) -> bool:
        return self._memory is not None or self._sweep is not None

    def _stop_scan(self, reason: str | None) -> None:
        """Stop the scan that runs, if one does, for ``reason``: "tuned",
        "source", "bookmarks" or "range", or None when a client asked. Unless
        tuned by hand or left for another source, the receiver and the VFO
        come back where the scan found them: after a sweep, and after a
        memory scan that moved the receiver, unless it stays on a channel.
        Without any client, the engine stops reading."""
        if not self._scanning():
            return
        home, memory, tour = self._home, self._memory, self._tour
        scan = "memory" if memory is not None else "sweep"
        self._memory = self._tour = self._sweep = self._home = None
        self._stopped = None if reason is None else (scan, reason)
        if home is not None:
            back = memory is None or (memory.bookmark_id is None and tour is not None and tour.moved)
            if back and reason not in ("tuned", "source"):
                self._go_home(*home)
            self._watch_bookmarks()     # from the span where the receiver stays
        self._scanner_changed()
        if not self._clients and not self._closed:
            self._wanted.clear()

    def _go_home(self, centre_hz: float, vfo_hz: float) -> None:
        """Bring the receiver and the VFO back where the scan found them."""
        if self._span()[0] != centre_hz:
            try:
                self.source.configure({"centre_hz": centre_hz})
            except ValueError as error:
                log.warning("the receiver did not come back to %.0f Hz after the scan: %s", centre_hz, error)
        if vfo_hz != self._demod.frequency_hz:
            self._demod = dataclasses.replace(self._demod, frequency_hz=vfo_hz)
            self._demod_changed()

    def _span(self) -> tuple[float, float]:
        """The centre frequency and sample rate asked of the source, in Hz:
        its samples show them once they come."""
        described = self.source.describe()
        return described["centre_hz"], described["sample_rate_hz"]

    def _watch_bookmarks(self) -> None:
        """Find the ticked bookmarks that a memory scan watches, from the
        span the receiver is in, or while a scan runs the one it started
        from, and the windows they take; while the memory scan runs, also
        the channels of the span asked of the receiver. The clients learn
        when the bookmarks or the windows change: a memory scan then visits
        the new windows, or stops when none is left."""
        centre_hz, rate_hz = self._span()
        start_hz = centre_hz if self._home is None else self._home[0]
        ticked = self._bookmarks.ticked()
        windows, ids = self._memory_plan(ticked, start_hz, rate_hz)
        watched = watched_channels(ticked, centre_hz, rate_hz) if self._memory is not None else ()
        if watched != self._watched:
            self._watched = watched
            self._bands = np.array([(channel.low_hz, channel.high_hz, channel.clearance_hz)
                                    for channel in watched]).reshape(-1, 3)
        if (windows, ids) == (self._windows, self._memory_ids):
            return
        self._windows, self._memory_ids = windows, ids
        if self._memory is not None:
            if ids:
                self._memory.keep(ids)
                if self._tour is not None and self._tour.windows != windows:
                    self._tour.restart(windows, centre_hz, time.monotonic())
            else:
                self._stop_scan("bookmarks")
        self._scanner_changed()

    def _memory_plan(self, ticked: Sequence[tuple[int, Bookmark]], centre_hz: float,
                     rate_hz: float) -> tuple[tuple[float, ...], tuple[int, ...]]:
        """The centre frequencies of the windows that a memory scan visits
        and the ``ticked`` bookmarks it watches there, by id: those of the
        span centred on ``centre_hz`` alone, unless the receiver tunes and
        other ticked bookmarks lie within its reach, which then take as few
        windows as ``memory_windows`` finds."""
        span = tuple(channel.bookmark_id for channel in watched_channels(ticked, centre_hz, rate_hz))
        described = self.source.describe()
        if "centre_hz" in described["settable"]:
            low_hz, high_hz = described["range_hz"]
            reach = [(bookmark_id, bookmark.frequency_hz) for bookmark_id, bookmark in ticked
                     if memory_window(bookmark.frequency_hz, rate_hz, low_hz, high_hz) is not None]
            if not {bookmark_id for bookmark_id, _ in reach} <= set(span):
                windows = memory_windows((frequency for _, frequency in reach), rate_hz, low_hz, high_hz)
                return windows, tuple(bookmark_id for bookmark_id, _ in reach)
        return ((centre_hz,) if span else ()), span

    def _step_memory(self, channels: tuple[Channel, ...] | None, levels: tuple[np.ndarray, np.ndarray] | None,
                     squelch_open: bool, centre_hz: float) -> None:
        """Move the memory scan on with a block read at ``centre_hz``: on its
        ``channels``, those of the span asked of the receiver, unless the
        samples still come from elsewhere (None), then from window to
        window."""
        if channels is not None:
            self._step_scan(channels, levels, squelch_open)
        scan, tour = self._memory, self._tour
        if scan is None or tour is None:
            return
        window = tour.window
        target = tour.step(time.monotonic(), centre_hz, scan.state == "watching", levels is not None)
        if target is not None:
            self._tune_scan(target)
        if tour is self._tour and tour.window != window:
            self._scanner_changed()

    def _step_scan(self, channels: tuple[Channel, ...], levels: tuple[np.ndarray, np.ndarray] | None,
                   squelch_open: bool) -> None:
        """Move the memory scan on with a block: ``levels``, the power and
        the noise of ``channels`` in it, tell which channels would open the
        squelch, with its settings; ``squelch_open``, whether it was open at
        the block's end. Tuned to a channel, the VFO takes its bookmark's
        mode and that mode's filter width."""
        scan = self._memory
        if scan is None or channels is not self._watched:
            return      # stopped, or other channels meanwhile: the next block tells
        active = None
        if levels is not None:
            power_db, noise_db = levels
            margins = power_db - noise_db
            demod = self._demod
            opens = margins >= SQUELCH_MARGIN_DB if demod.squelch_auto else power_db >= demod.squelch_db
            active = [(channel.bookmark_id, float(margin))
                      for channel, margin, on in zip(channels, margins, opens) if on]
        before = (scan.state, scan.bookmark_id)
        target = scan.step(time.monotonic(), active, squelch_open, self._resume_s)
        if target is not None:
            channel = next(channel for channel in channels if channel.bookmark_id == target)
            self._demod = self._demod.changed({"frequency_hz": channel.frequency_hz, "mode": channel.mode,
                                               "bandwidth_hz": MODE_BANDWIDTHS_HZ[channel.mode]})
            self._demod_changed()
        if (scan.state, scan.bookmark_id) != before:
            self._scanner_changed()

    async def _check_updates(self, updates: UpdateChecker) -> None:
        try:
            found = await updates.check()
        except Exception:
            log.exception("the update check failed")
            found = UpdateResult("failed", detail="The check stopped on an error.")
        if found.status == "failed":
            log.warning("the update check failed: %s", found.detail)
        self._update = found
        self._update_changed()

    def _switch(self, kind: object) -> None:
        """Read a source of ``kind`` in place of this one: the receiver used
        last of that kind, with its saved settings. The source in use closes,
        and its settings are kept for when it comes back."""
        sources = self._sources
        if sources is None:
            raise ValueError("this source cannot be changed")
        if not isinstance(kind, str) or kind not in sources.kinds:
            raise ValueError(f"unknown kind of source: {str(kind)[:40]!r}")
        if kind != self.source.kind:
            try:
                new = sources.open(self._reopen.get(kind, {"kind": kind}))
            except ValueError as error:
                log.warning("the saved %s receiver is not used: %s", kind, error)
                new = sources.open({"kind": kind})
            old = self.source
            # Saved before the scan stops: those of the scan's start.
            self._receivers[old.settings_key] = self._receiver_settings()
            self._stop_scan("source")
            old.on_change = None
            old.close()
            self._closing.add(asyncio.ensure_future(self._release(old)))
            self.source = new
            self._reopen[kind] = new.reopen
            self._adopt()
            self._demod_changed()
            self._signals.clear()       # found by another receiver
            if self._signals.changed(time.monotonic()):
                self._peaks_changed(at_once=True)
        self._source_changed()

    @staticmethod
    async def _release(source: Source) -> None:
        """Wait for a source switched away from to release its receiver."""
        try:
            await source.wait_closed()
        except Exception:
            log.exception("the %s source did not close cleanly", source.kind)

    def _adopt(self) -> None:
        """Start the source in use from the settings saved for its receiver,
        apart from those it was given, and the demodulation settings saved
        with them."""
        saved = self._receivers.get(self.source.settings_key, {})
        self._restore_source(saved.get("source"))
        described = self.source.describe()
        # Unless saved, the VFO starts a quarter of the span above the centre, clear of the DC spike.
        self._demod = DemodSettings(
            frequency_hz=float(round(described["centre_hz"] + described["sample_rate_hz"] / 4)))
        self._restore_demod(saved.get("demod"))
        self._capture = None
        self.source.on_change = self._source_changed

    def _receiver_settings(self) -> dict[str, Any]:
        """The settings saved for the receiver in use: while a scan runs,
        with the centre frequency and the VFO at its start."""
        state = self.state()
        source = {name: state[name] for name in state["settable"]}
        demod = self._demod.describe()
        if self._home is not None:
            centre_hz, demod["frequency_hz"] = self._home
            if "centre_hz" in source:       # not with a source that does not tune
                source["centre_hz"] = centre_hz
        return {"source": source, "demod": demod}

    def _restore_source(self, saved: object) -> None:
        """Apply the source settings saved for this receiver one at a time,
        apart from those the source was given: one that the receiver refuses
        keeps its default."""
        if not isinstance(saved, dict):
            return
        settable = self.state()["settable"]
        for name, value in saved.items():
            if name not in settable or name in self.source.given:
                continue
            try:
                self.configure({name: value})
            except ValueError as error:
                log.warning("the saved setting %s is not used: %s", name, error)

    def _restore_demod(self, saved: object) -> None:
        """Apply the saved demodulation settings one at a time: one that is not
        valid keeps its default."""
        if not isinstance(saved, dict):
            return
        for name, value in saved.items():
            try:
                self._demod = self._demod.changed({name: value})
            except ValueError as error:
                log.warning("the saved setting %s is not used: %s", name, error)

    def _restore_scanner(self, saved: object) -> None:
        """Apply the saved scanner settings one at a time: one that is not
        valid keeps its default, and the ignored frequencies beyond the first
        MAX_IGNORED are left out."""
        if saved is None:
            return
        if not isinstance(saved, dict):
            log.warning("the saved scanner settings are not used")
            return
        detection = saved.get("detection", self._detection)
        if isinstance(detection, bool):
            self._detection = detection
        else:
            log.warning("the saved setting detection is not used")
        if "threshold_db" in saved:
            try:
                self._threshold_db = checked_threshold(saved["threshold_db"])
            except ValueError as error:
                log.warning("the saved setting threshold_db is not used: %s", error)
        if "resume_s" in saved:
            try:
                self._resume_s = checked_resume(saved["resume_s"])
            except ValueError as error:
                log.warning("the saved setting resume_s is not used: %s", error)
        if "sweep_low_hz" in saved or "sweep_high_hz" in saved:
            try:
                self._sweep_low_hz, self._sweep_high_hz = checked_sweep_range(saved.get("sweep_low_hz"),
                                                                              saved.get("sweep_high_hz"))
            except ValueError as error:
                log.warning("the saved sweep range is not used: %s", error)
        if "sweep_window_s" in saved:
            try:
                self._sweep_window_s = checked_window(saved["sweep_window_s"])
            except ValueError as error:
                log.warning("the saved setting sweep_window_s is not used: %s", error)
        ignored = saved.get("ignored_hz", [])
        if not isinstance(ignored, list):
            log.warning("the saved ignored frequencies are not used")
            return
        if len(ignored) > MAX_IGNORED:
            log.warning("only the first %d saved ignored frequencies are used", MAX_IGNORED)
        for value in ignored[:MAX_IGNORED]:
            try:
                self._signals.ignore(checked_frequency(value, "an ignored frequency"))
            except ValueError as error:
                log.warning("a saved ignored frequency is not used: %s", error)

    def _contents(self, observations: bool = False) -> dict[str, dict[str, Any]]:
        """What the settings, bookmarks and remote access files hold, as
        things stand, and with ``observations`` the observations file: with
        sources to open, which source opens at the next start."""
        settings: dict[str, Any] = {} if self._sources is None else {"source": self.source.reopen}
        settings["receivers"] = {**self._receivers, self.source.settings_key: self._receiver_settings()}
        settings["scanner"] = {"detection": self._detection, "threshold_db": self._threshold_db,
                               "resume_s": self._resume_s, "sweep_low_hz": self._sweep_low_hz,
                               "sweep_high_hz": self._sweep_high_hz, "sweep_window_s": self._sweep_window_s,
                               "ignored_hz": self._signals.ignored}
        settings["interface"] = dict(self._preferences)
        settings["updates"] = {"check_at_start": self._check_at_start}
        contents = {SETTINGS_FILE: settings, BOOKMARKS_FILE: {"bookmarks": self._bookmarks.stored()}}
        if self._remote is not None:
            contents[REMOTE_FILE] = self._remote.stored()
        if observations:
            contents[OBSERVATIONS_FILE] = self._observations.stored()
        return contents

    def _settings_changed(self) -> None:
        """Save soon. ``wait_closed`` saves what changes after ``close``."""
        if self._store is None or self._closed:
            return
        self._unsaved = True
        if self._saver is None or self._saver.done():
            self._saver = asyncio.ensure_future(self._save_later())

    async def _save_later(self) -> None:
        """Save a moment after a change, and again after the changes made
        while saving."""
        while self._unsaved:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._save_now.wait(), self._save_delay_s)
            self._unsaved = False
            await self._save()

    async def _save(self) -> None:
        """Write the files whose content changed since their latest write, in a
        worker thread, as the disk may be slow, the observations' once due.
        Clients learn when the storage state changes."""
        store = self._store
        if store is None:
            return
        before = self.storage_state()
        observations = self._observations_due
        if observations:
            self._observations_due = False
            self._observations_saved_s = time.monotonic()
            self._observations.forget(time.time())
        try:
            for name, content in self._contents(observations).items():
                if content != self._saved.get(name) and await asyncio.to_thread(store.save, name, content):
                    self._saved[name] = content
        except Exception:
            log.exception("the settings could not be saved")
        if self.storage_state() != before:
            for subscription in self._clients:
                subscription.changed("storage")

    def _keep_vfo_in(self, centre_hz: float, rate_hz: float) -> None:
        """A new span: a VFO left outside it moves to its nearest edge."""
        low, high = math.ceil(centre_hz - rate_hz / 2), math.floor(centre_hz + rate_hz / 2)
        frequency = min(max(self._demod.frequency_hz, low), high)
        if frequency != self._demod.frequency_hz:
            self._demod = dataclasses.replace(self._demod, frequency_hz=float(frequency))
            self._demod_changed()

    def _publish(self, capture: tuple[float, float], power: np.ndarray, codes: np.ndarray, meter: Meter,
                 pcm: np.ndarray | None) -> None:
        if not self._clients:
            return
        frame = encode_frame(self._sequence, *capture, self.analyzer.db_min, self.analyzer.db_step, codes,
                             meter.level_db, meter.squelch_db, meter.open)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        remote_frame = self._remote_frame(capture, power, meter)
        audio = self._demodulator.encode_audio(pcm) if pcm is not None and len(pcm) else None
        for subscription in self._clients:
            if not subscription.remote:
                subscription.put_frame(frame)
            elif remote_frame is not None:
                subscription.put_frame(remote_frame)
            if audio is not None:
                subscription.put_audio(audio)

    def _remote_frame(self, capture: tuple[float, float], power: np.ndarray, meter: Meter) -> bytes | None:
        """The remote clients' frame, once every ``REMOTE_BLOCKS`` blocks of
        the same span: their mean power, in at most ``REMOTE_BINS`` bins, with
        the latest block's meter. None in between, and while no remote client
        listens."""
        if not any(subscription.remote for subscription in self._clients):
            self._remote_blocks.clear()
            return None
        if capture != self._remote_capture:     # blocks of another span do not add up
            self._remote_capture = capture
            self._remote_blocks.clear()
        self._remote_blocks.append(power)
        if len(self._remote_blocks) < REMOTE_BLOCKS:
            return None
        codes = fewer_bins(self.analyzer.codes(np.mean(self._remote_blocks, axis=0)), REMOTE_BINS)
        self._remote_blocks.clear()
        frame = encode_frame(self._remote_sequence, *capture, self.analyzer.db_min, self.analyzer.db_step,
                             codes, meter.level_db, meter.squelch_db, meter.open)
        self._remote_sequence = (self._remote_sequence + 1) & 0xFFFFFFFF
        return frame


def _open_saved(sources: SourceOpener, saved: object) -> Source:
    """The source used last, or the default kind's when none was saved or it
    cannot be opened."""
    if isinstance(saved, dict):
        try:
            return sources.open(saved)
        except ValueError as error:
            log.warning("the saved source is not used: %s", error)
    return sources.open({"kind": sources.kinds[0]})


def _given_differ(state: Mapping[str, Any], saved: object, given: frozenset[str]) -> bool:
    """Whether a setting the source was given differs from the saved one."""
    saved_values = saved if isinstance(saved, dict) else {}
    return any(name in state["settable"] and saved_values.get(name) != state[name] for name in given)


def _saved_check_at_start(saved: object) -> bool:
    """Whether to check for updates at start, as saved: yes unless saved as
    false."""
    if saved is None:
        return True
    value = saved.get("check_at_start", True) if isinstance(saved, dict) else None
    if not isinstance(value, bool):
        log.warning("the saved update setting is not used")
        return True
    return value


def _process_block(analyzer: SpectrumAnalyzer, demodulator: Demodulator, detector: Detector,
                   watch: ChannelWatch, samples: np.ndarray, centre_hz: float, rate_hz: float,
                   dc_removal: bool, settings: DemodSettings, audio: bool, threshold_db: float | None,
                   search_blocks: int, bands: np.ndarray | None,
                   ) -> tuple[np.ndarray, np.ndarray, Meter, np.ndarray | None, list[Detection] | None,
                              tuple[np.ndarray, np.ndarray] | None]:
    """The bin powers, the display codes, the channel meter, with ``audio``
    the audio, once ``detector`` has averaged ``search_blocks`` blocks the
    signals found, and once ``watch`` knows the noise the power and the
    noise of the memory scan's channels, of a block of samples, worked out in
    a worker thread. Detection is off without ``threshold_db``, and the
    memory scan's channels are not watched without ``bands``, their
    passbands and noise clearances."""
    if dc_removal:
        # A mean in single precision would leave a trace of a strong offset.
        samples = samples - np.complex64(samples.mean(dtype=np.complex128))
    power = analyzer.power(samples)
    noise = analyzer.noise_density(power, len(samples), settings.frequency_hz - centre_hz, rate_hz,
                                   noise_clearance_hz(settings.mode, settings.bandwidth_hz))
    meter, pcm = demodulator.process(samples, centre_hz, rate_hz, settings, noise, audio)
    if threshold_db is None:
        detector.reset()
        detections = None
    else:
        detections = detector.add(power, centre_hz, rate_hz, threshold_db, search_blocks)
    if bands is None:
        watch.reset()
        levels = None
    else:
        levels = watch.levels(power, centre_hz, rate_hz, bands, analyzer.enbw)
    return power, analyzer.codes(power), meter, pcm, detections, levels
