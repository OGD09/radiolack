"""Receiver engine: reads a source and shares display frames and audio with
clients.

The engine runs in the server's event loop. It reads blocks of I/Q samples
from its source only while at least one client listens, and in a worker
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

The engine also keeps the bookmarks, shared by every client like the
settings, and the interfaces' preferences, which a new interface starts with.
With a store (store.py), it starts from what was saved: the settings saved
for its receiver (each receiver, by its ``settings_key``, has its own), the
bookmarks and the preferences. It saves them a moment after they change, as
changes come in bursts, and when it stops. A saved setting that the receiver
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
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import numpy as np

from .bookmarks import Bookmarks
from .demod import AUDIO_MODES, MODES, DemodSettings, Demodulator, Meter
from .spectrum import SpectrumAnalyzer, encode_frame
from .store import BOOKMARKS_FILE, SETTINGS_FILE, Store

FRAME_RATE = 30.0       # display frames per second
CLIENT_QUEUE = 4        # frames kept for a client that falls behind
CLIENT_REPLIES = 8      # replies kept for a client that falls behind
CLIENT_AUDIO = 15       # audio frames (half a second) kept for a client that falls behind
SAVE_DELAY_S = 1.0      # changes are saved this long after the first of a burst
MAX_PREFERENCES = 100   # the interfaces' preferences: names and values the engine keeps
_MAX_PREFERENCE_NAME = 40
_MAX_PREFERENCE_TEXT = 100

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


class Subscription:
    """What the engine has for one client, in the order it goes out: the
    states that changed (all of them at the start, in the order of
    ``states``), replies to the client's commands, audio frames if the client
    asked for audio, then the latest display frames."""

    def __init__(self, states: Mapping[str, Callable[[Subscription], dict[str, Any]]], frames: int,
                 replies: int = CLIENT_REPLIES, audio: int = CLIENT_AUDIO) -> None:
        self._states = dict(states)     # each gives its message for this client
        self._due = set(self._states)
        self._replies: deque[dict[str, Any]] = deque(maxlen=replies)
        self._audio: deque[bytes] = deque(maxlen=audio)
        self._frames: deque[bytes] = deque(maxlen=frames)
        self._ended = False
        self._ready = asyncio.Event()
        self.audio = False          # the client asked for audio
        self.acknowledged = 0       # id of the client's last demodulation change handled

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
        """State ``kind`` changed: the client receives it again."""
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
    asks."""

    def __init__(self, source: Source | None = None, *, sources: SourceOpener | None = None,
                 store: Store | None = None, frame_rate: float = FRAME_RATE,
                 analyzer: SpectrumAnalyzer | None = None, queue_size: int = CLIENT_QUEUE,
                 save_delay_s: float = SAVE_DELAY_S) -> None:
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
        self._demodulator = Demodulator()   # used by one block at a time
        self._capture: tuple[float, float] | None = None    # centre and rate of the last block
        self._closing: set[asyncio.Future] = set()  # sources switched away from, until they release their receiver
        # What each client receives first, in this order, then again whenever it changes.
        self._states: dict[str, Callable[[Subscription], dict[str, Any]]] = {
            "source": lambda _: self.state(),
            "demod": lambda subscription: self.demod_state(subscription.acknowledged),
            "bookmarks": lambda _: self.bookmarks_state(),
            "preferences": lambda _: self.preferences_state(),
            "storage": lambda _: self.storage_state(),
        }
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
        self._adopt()
        self._bookmarks = Bookmarks(store.load(BOOKMARKS_FILE).get("bookmarks", []) if store is not None else [])
        self._preferences = _saved_preferences(settings.get("interface"))
        if store is not None:
            self._saved = self._contents()      # nothing to write until something changes,
            # but a source or a setting given at start replaces the saved one at the next write.
            default_choice = {"kind": self._sources.kinds[0]} if self._sources is not None else None
            if (_given_differ(self.state(), saved_source, self.source.given)
                    or self._sources is not None
                    and (saved_choice if saved_choice is not None else default_choice) != self.source.reopen):
                del self._saved[SETTINGS_FILE]
            self._store = store

    def subscribe(self) -> Subscription:
        """The messages for a new client, starting with the source state, the
        demodulation settings, the bookmarks, the preferences and the storage
        state."""
        subscription = Subscription(self._states, self._queue_size)
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
                "settable": [*described["settable"], "dc_removal"],
                "kinds": list(self._sources.kinds) if self._sources is not None else []}

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change settings: ``kind`` switches to another kind of source, and
        comes alone; ``dc_removal`` is the engine's own; the others go to the
        source. Raises ValueError, changing nothing, when one is refused.
        Every client then receives the new state."""
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
            self.source.configure(source_changes)
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
        case."""
        if subscription is not None and change_id is not None:
            subscription.acknowledged = change_id
            subscription.changed("demod")
        self._demod = self._demod.changed(changes)
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

    def preferences_state(self) -> dict[str, Any]:
        """The preferences message: what a new interface starts with."""
        return {"type": "preferences", "values": dict(self._preferences)}

    def set_preferences(self, values: object) -> None:
        """Keep an interface's preferences, which the next interfaces start
        with: names with numbers, true or false, short texts or null. Raises
        ValueError, changing nothing, when one is not valid."""
        if not isinstance(values, dict):
            raise ValueError("the preferences must be a JSON object")
        if len(values) > MAX_PREFERENCES:
            raise ValueError(f"there may be at most {MAX_PREFERENCES} preferences")
        for name, value in values.items():
            if not _preference_valid(name, value):
                raise ValueError(f"the preference {str(name)[:_MAX_PREFERENCE_NAME]!r} must be a number, "
                                 f"true or false, a text of at most {_MAX_PREFERENCE_TEXT} characters or null")
        self._preferences = dict(values)
        self._settings_changed()

    def storage_state(self) -> dict[str, Any]:
        """The storage message: where the settings are saved, whether changes
        reach the disk, and what went wrong with the files."""
        store = self._store
        if store is None:
            return {"type": "storage", "directory": None, "saving": False, "problem": ""}
        return {"type": "storage", "directory": store.location, "saving": store.saving,
                "problem": store.problem}

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
        """Return once the source, after ``close``, has released its receiver,
        like those switched away from, and the latest changes are saved."""
        await self.source.wait_closed()
        if self._closing:
            await asyncio.wait(set(self._closing))
        if self._store is None:
            return
        self._save_now.set()
        if self._saver is not None:
            await asyncio.gather(self._saver, return_exceptions=True)
        await self._save()      # also what a failed write left unsaved

    async def run(self) -> None:
        """Produce frames until closed or cancelled."""
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
                    continue        # waiting, so check that clients still listen; or switched away from
                capture = (source.centre_hz, source.sample_rate_hz)
                if capture != self._capture:
                    self._capture = capture
                    self._keep_vfo_in(*capture)
                audio = any(subscription.audio for subscription in self._clients)
                codes, meter, pcm = await asyncio.to_thread(
                    _process_block, self.analyzer, self._demodulator, samples, *capture,
                    self._dc_removal, self._demod, audio)
                self._publish(capture, codes, meter, pcm)
        except Exception:
            log.exception("the engine stopped")
            self.close()

    def _source_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("source")
        self._settings_changed()

    def _demod_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("demod")
        self._settings_changed()

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
            self._receivers[old.settings_key] = self._receiver_settings()
            old.on_change = None
            old.close()
            self._closing.add(asyncio.ensure_future(self._release(old)))
            self.source = new
            self._reopen[kind] = new.reopen
            self._adopt()
            self._demod_changed()
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
        """The settings saved for the receiver in use."""
        state = self.state()
        return {"source": {name: state[name] for name in state["settable"]}, "demod": self._demod.describe()}

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

    def _contents(self) -> dict[str, dict[str, Any]]:
        """What the settings and bookmarks files hold, as things stand: with
        sources to open, which source opens at the next start."""
        settings: dict[str, Any] = {} if self._sources is None else {"source": self.source.reopen}
        settings["receivers"] = {**self._receivers, self.source.settings_key: self._receiver_settings()}
        settings["interface"] = dict(self._preferences)
        return {SETTINGS_FILE: settings, BOOKMARKS_FILE: {"bookmarks": self._bookmarks.stored()}}

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
        worker thread, as the disk may be slow. Clients learn when the storage
        state changes."""
        store = self._store
        if store is None:
            return
        before = self.storage_state()
        try:
            for name, content in self._contents().items():
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

    def _publish(self, capture: tuple[float, float], codes: np.ndarray, meter: Meter,
                 pcm: np.ndarray | None) -> None:
        if not self._clients:
            return
        frame = encode_frame(self._sequence, *capture, self.analyzer.db_min, self.analyzer.db_step, codes,
                             meter.level_db, meter.squelch_db, meter.open)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        audio = self._demodulator.encode_audio(pcm) if pcm is not None and len(pcm) else None
        for subscription in self._clients:
            subscription.put_frame(frame)
            if audio is not None:
                subscription.put_audio(audio)


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


def _preference_valid(name: object, value: object) -> bool:
    if not isinstance(name, str) or not 0 < len(name) <= _MAX_PREFERENCE_NAME:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= 2 ** 53       # what JavaScript counts exactly
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= _MAX_PREFERENCE_TEXT


def _saved_preferences(saved: object) -> dict[str, Any]:
    """The saved preferences, leaving out those that are not valid."""
    if not isinstance(saved, dict):
        return {}
    valid = {name: value for name, value in saved.items() if _preference_valid(name, value)}
    if len(valid) < len(saved) or len(valid) > MAX_PREFERENCES:
        log.warning("some saved preferences are not used")
    return dict(list(valid.items())[:MAX_PREFERENCES])


def _process_block(analyzer: SpectrumAnalyzer, demodulator: Demodulator, samples: np.ndarray,
                   centre_hz: float, rate_hz: float, dc_removal: bool, settings: DemodSettings,
                   audio: bool) -> tuple[np.ndarray, Meter, np.ndarray | None]:
    """The display codes, the channel meter and, with ``audio``, the audio of
    a block of samples, worked out in a worker thread."""
    if dc_removal:
        # A mean in single precision would leave a trace of a strong offset.
        samples = samples - np.complex64(samples.mean(dtype=np.complex128))
    power = analyzer.power(samples)
    noise = analyzer.noise_density(power, len(samples), settings.frequency_hz - centre_hz, rate_hz)
    meter, pcm = demodulator.process(samples, centre_hz, rate_hz, settings, noise, audio)
    return analyzer.codes(power), meter, pcm
