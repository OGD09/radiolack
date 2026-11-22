"""Receiver engine: its radios, and what they share with every client.

The engine runs in the server's event loop. A radio (radio.py) is what the
engine does for one receiver: its source, its reading, its VFO, its
scanners and its recordings. The engine holds its radios and what they
share: the bookmarks, the band plan, the observations, the kept scans, the
interfaces' preferences, remote access's state, the update check, the
search for SoapySDR devices, the recordings folder and the store. Each
client shows one radio, which sends it its frames, its audio and its own
states; the shared states reach every client. A new client shows the
first radio until it chooses another (docs/design/later-milestones.md,
section 5).

The engine has MAX_RADIOS radios at most, each reading a receiver of its
own: a receiver, by its settings (``Source.settings_key``), belongs to one
radio, so that the simulated band, an rtl_tcp server and each RTL-SDR or
SoapySDR device read on one radio at most. Any client may add a radio from
the sources not in use, and remove one, unless it is the last; the
clients of a radio removed show the first. Every client receives the
radios' list, what each reads and runs, and which radio it shows.

The engine keeps the bookmarks, shared by every client like the settings,
the band plan in use (bandplan.py), shared and saved the same way, and the
interfaces' preferences (preferences.py), which a new interface starts
with: one set for this computer's interfaces, and one for each paired
device once it gave one, until then this computer's. It keeps what its
radios find, apart from the simulated band's, as observations
(observations.py), which a client receives when it asks, and may clear,
and each range scan under a name with the channels it found (keptscans.py),
which every client receives, and may rename or delete. With an update
check (updates.py), it checks for a newer Radiolack when it starts, unless
turned off, and when a client asks, and tells every client what the check
found.

With sources that open SoapySDR devices, it searches for them
(soapysearch.py) when this computer's interface asks, never by itself, and
tells every client what the search found, never searching the driver of
the device in use.

With remote access (remote.py), the engine tells this computer's interfaces
its state, and saves what it keeps, each paired device's preferences
included. Remote clients, the devices that remote access serves, receive
neither that state nor the storage and update states, and may not change
the rtl_tcp server's address, search for SoapySDR devices or give device
arguments (server.py refuses them). Their links may be slower than this
computer's own: they get one display frame for every two blocks, the mean
power of both, in fewer bins.

With a recordings folder (recordings.py), its radios record into it: the
clients receive the list of the recordings when they ask, and may delete
one that does not run; this computer's interfaces may also choose another
folder, while no recording runs, which is saved.

With a store (store.py), it starts from what was saved: the source used
last and the settings saved for each receiver (each receiver, by its
``settings_key``, has its own), the scanner settings, the bookmarks, the
preferences, whether to check for updates at start, what remote access
keeps, the recordings folder chosen, the observations and the kept scans.
It saves them a moment after they change, as changes come in bursts, and
when it stops; the observations, which change with every signal found, at
most every 10 minutes, unless a client cleared some, and when it stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from . import __version__
from .bandplan import DEFAULT_PLAN, checked_plan, load_plan
from .bookmarks import Bookmarks
from .keptscans import KeptScans
from .observations import Observations
from .preferences import check_preferences, saved_preferences
from .radio import FRAME_RATE, Radio
from .radioproxy import RadioProcess
from .recordings import check_folder, chosen_folder, delete_recording, is_recording_file, is_recording_name, \
    list_recordings
from .remote import REMOTE_FILE, RemoteAccess
from .rtldevices import RtlDevice
from .soapysearch import DeviceSearch
from .scanners import checked_frequency
from .spectrum import SpectrumAnalyzer
from .devicenames import checked_name, device_key, renamed, saved_names
from .store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SCANS_FILE, SETTINGS_FILE, Store
from .updates import UpdateResult

CLIENT_QUEUE = 4        # frames kept for a client that falls behind
CLIENT_REPLIES = 8      # replies kept for a client that falls behind
CLIENT_AUDIO = 15       # audio frames (half a second) kept for a client that falls behind
CLIENT_FINE = 2         # fine frames (a fifth of a second) kept for a client that falls behind
SAVE_DELAY_S = 1.0      # changes are saved this long after the first of a burst
OBSERVATIONS_SAVE_S = 600.0     # the observations are written at most this often, and when the engine stops
OBSERVATIONS_FIELDS = ("clear", "clear_all")
SCANS_FIELDS = ("get", "rename", "name", "delete", "clear_all")
SCANS_SAVE_S = 10.0     # a running scan is kept on disk and in the clients' list at most this long after a change
RECORDING_CLOSE_S = 10.0        # a stopping engine waits this long for each recording to reach the disk
MAX_RADIOS = 4          # radios at once, each reading a receiver of its own
AnyRadio = Radio | RadioProcess     # the first radio, in Radiolack's process, or another in its own
# A radio's own states, which a client receives again when it shows another radio.
RADIO_STATES = ("source", "demod", "scanner", "peaks", "recording", "devices", "calibration")
LOCAL_STATES = ("storage", "update", "remote")    # states only this computer's interfaces receive
LOCAL_DEVICE_FIELDS = ("search", "args")    # SoapySDR device requests only this computer's interfaces may make
LOCAL_RECORDING_SETTINGS = ("folder",)      # recording settings only this computer's interfaces may change

log = logging.getLogger(__name__)


class Source(Protocol):
    """A source of I/Q samples: complex64, in full-scale units.

    ``centre_hz`` and ``sample_rate_hz`` describe the samples ``read`` last
    returned: the centre asked, which tells the setting they come from,
    with ``exact_centre_hz``, the centre in use that labels them, where the
    receiver sits on the air as far as the source knows, and
    ``rate_scale``, how much faster than ``sample_rate_hz`` the receiver
    truly samples, as the source alone corrects it, which scales the VFO's
    distance from the centre. ``centre_in_use`` gives the centre in use
    for a centre asked, and ``grid_hz`` how far apart those lie for a
    receiver that places its centres on a grid (rtltcp.py), 0 for others;
    ``pll_tuning`` and ``centre_off_grid`` are what the check of the
    driver's computation needs of such a receiver (pllcheck.py).
    ``describe`` gives the state the interfaces show
    (docs/design/protocol.md), and the source calls ``on_change`` whenever
    that state changes. ``settings_key`` names the receiver whose settings
    the engine saves: each key has its own. A source that learns which
    receiver it reads once it opens it, such as a SoapySDR device's serial
    number (soapy.py), changes its key then, and calls ``on_change``: the
    engine then restores the settings saved for that receiver, and
    ``reopen`` names it from then on. ``given`` names the settings
    chosen when the source was made, which saved ones do not replace.
    ``reopen`` is what opens the same receiver again (sources.py).
    ``tap`` hands the receiver's raw samples to an I/Q recording, which
    ``hardware`` names the receiver for, and ``raw_format`` gives the
    format of the samples the tap was handed last, while it is called: as
    SigMF names it (recordings.IQ_DATATYPES), with the full scale of an
    integer format that is not its type's, or None; None for a source
    without raw samples.
    """

    kind: str
    centre_hz: float
    exact_centre_hz: float
    rate_scale: float
    sample_rate_hz: float
    given: frozenset[str]
    on_change: Callable[[], None] | None
    raw_format: tuple[str, float | None] | None

    @property
    def grid_hz(self) -> float:
        """How far apart the centres in use lie, on a grid; 0 without one."""

    def centre_in_use(self, centre_hz: float) -> float:
        """The centre in use that asking for ``centre_hz`` would give."""

    def pll_tuning(self) -> tuple[int, int] | str:
        """The frequency asked of an R820T's or R828D's tuner, and the
        sample rate, where the driver's computation applies; otherwise why
        not: "receiver", "direct" or "range" (rtltcp.RtlTcpSource)."""

    def centre_off_grid(self, request_hz: int) -> float | None:
        """The centre asked for which the tuner, off its grid, is asked for
        ``request_hz``; None when none is."""

    @property
    def settings_key(self) -> str:
        """Such as "rtl_sdr 0": the kind of source and which receiver."""

    @property
    def reopen(self) -> dict[str, Any]:
        """Such as {"kind": "rtl_sdr", "device": "0"}, as JSON values."""

    @property
    def hardware(self) -> str:
        """The receiver, such as "RTL-SDR, Rafael Micro R820T"."""

    def tap(self, receiver: Callable[[bytes, float, float, bool], None] | None) -> None:
        """Hand ``receiver``, in the event loop, the raw samples as they
        arrive, whether read or not: whole I/Q pairs in ``raw_format``, I
        first, with the centre frequency and the sample rate they were
        taken with, and whether they follow those handed before without a
        gap (rtltcp.RtlTcpSource.tap, soapy.SoapySource.tap). None stops.
        Raises ValueError for a source without raw samples."""

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
    raw_format: tuple[str, float | None] | None = None
    rate_scale = 1.0
    grid_hz = 0.0

    @property
    def exact_centre_hz(self) -> float:
        return self.centre_hz

    def centre_in_use(self, centre_hz: float) -> float:
        return centre_hz

    def pll_tuning(self) -> tuple[int, int] | str:
        return "receiver"

    def centre_off_grid(self, request_hz: int) -> float | None:
        return None

    @property
    def settings_key(self) -> str:
        return self.kind

    @property
    def reopen(self) -> dict[str, Any]:
        return {"kind": self.kind}

    @property
    def hardware(self) -> str:
        return self.kind

    def tap(self, receiver: Callable[[bytes, float, float, bool], None] | None) -> None:
        if receiver is not None:
            raise ValueError(f"the {self.kind} source has no raw samples to record")

    def describe(self) -> dict[str, Any]:
        centre = self.centre_hz
        return {"kind": self.kind, "status": "streaming", "detail": "", "address": None,
                "tuner": None, "driver": None, "centre_hz": centre, "centre_in_use_hz": centre,
                "sample_rate_hz": self.sample_rate_hz, "sample_rates_hz": [], "gain_db": None, "ppm": None,
                "gains_db": [],
                "range_hz": [centre, centre], "hf_input": None, "hf_inputs": [], "direct": False, "rtl_agc": None,
                "bias_tee": None, "converter": None, "converter_lo_hz": None, "driver_pll": None, "tuner_grid": None,
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
    asked for audio, the latest fine frames if it asked for the fine
    spectrum, then the latest display frames. A remote client is a paired
    ``device`` that remote access serves, named by its id."""

    def __init__(self, states: Mapping[str, Callable[[Subscription], dict[str, Any]]], frames: int,
                 replies: int = CLIENT_REPLIES, audio: int = CLIENT_AUDIO, device: str | None = None,
                 fine: int = CLIENT_FINE) -> None:
        self._states = dict(states)     # each gives its message for this client
        self._due = set(self._states)
        self._replies: deque[dict[str, Any]] = deque(maxlen=replies)
        self._audio: deque[bytes] = deque(maxlen=audio)
        self._fine: deque[bytes] = deque(maxlen=fine)
        self._frames: deque[bytes] = deque(maxlen=frames)
        self._ended = False
        self._ready = asyncio.Event()
        self.device = device
        self.audio = False          # the client asked for audio
        self.fine = False           # the client asked for the fine spectrum
        self.acknowledged = 0       # id of the client's last demodulation change handled
        self.radio: Radio | None = None     # the radio it shows, once the engine attaches it

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
                message = describe(self)
                if message is not None:     # a radio in its own process sends its states once it can
                    return message
        if self._replies:
            return self._replies.popleft()
        if self._audio:
            return self._audio.popleft()
        if self._fine:
            return self._fine.popleft()
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

    def put_fine(self, frame: bytes) -> None:
        """Add a fine frame if the client wants the fine spectrum; when it
        fell behind, its oldest fine frame goes."""
        if self.fine:
            self._fine.append(frame)
            self._ready.set()

    def set_fine(self, on: bool) -> None:
        self.fine = on
        if not on:
            self._fine.clear()

    def changed(self, kind: str) -> None:
        """State ``kind`` changed: the client receives it again, unless it
        never receives that state."""
        if kind in self._states:
            self._due.add(kind)
            self._ready.set()

    def show(self, radio: AnyRadio) -> None:
        """Show ``radio`` from now on: the previous radio's frames, audio
        and fine frames that wait go, and the client receives its new
        radio's states again."""
        self.radio = radio
        self._frames.clear()
        self._audio.clear()
        self._fine.clear()
        for kind in (*RADIO_STATES, "radios"):
            self.changed(kind)

    def reply(self, message: dict[str, Any]) -> None:
        """Answer this client only, such as with the reason for a refused change."""
        self._replies.append(message)
        self._ready.set()

    def end(self) -> None:
        self._ended = True
        self._replies.clear()
        self._audio.clear()
        self._fine.clear()
        self._frames.clear()
        self._ready.set()



class Engine:
    """Hold the radios and what they share, for any number of clients.

    The engine's first radio reads ``source``, and one more radio reads
    each of ``more_sources``, in order. Without either, the first radio
    reads the source that ``sources`` opened last and the others reopen
    the radios used last. Each radio switches between the kinds of
    ``sources`` when a client asks, and a client may add a radio reading
    any of them. The engine checks for updates with ``updates``; without
    it, it never does. With ``remote``, it shares and saves remote access's
    state. Its radios record into ``recordings``, unless the user chose
    another folder; without it, they never do. ``frame_rate`` and
    ``analyzer`` are the radios' (radio.py), the analyzer the first's.
    Raises ValueError for more than MAX_RADIOS sources, or two that read
    one receiver."""

    def __init__(self, source: Source | None = None, *, more_sources: Sequence[Source] = (),
                 sources: SourceOpener | None = None,
                 store: Store | None = None, updates: UpdateChecker | None = None,
                 remote: RemoteAccess | None = None, recordings: Path | None = None,
                 frame_rate: float = FRAME_RATE, analyzer: SpectrumAnalyzer | None = None,
                 queue_size: int = CLIENT_QUEUE, save_delay_s: float = SAVE_DELAY_S,
                 observations_save_s: float = OBSERVATIONS_SAVE_S, radio_processes: bool = False) -> None:
        if frame_rate <= 0:
            raise ValueError(f"the frame rate must be positive, not {frame_rate}")
        self.sources = sources
        # The radios after the first each in a process of their own, which
        # opens sources as ``sources`` would (Sources.recipe).
        self._radio_processes = radio_processes and hasattr(sources, "recipe")
        self._queue_size = queue_size
        self._clients: set[Subscription] = set()
        self._closed = False
        self._stopped = asyncio.Event()     # set once closed, which ends ``run``
        self._tasks: dict[AnyRadio, asyncio.Future] = {}  # each radio's reading, while the engine runs
        self._frame_rate = frame_rate
        self._next_radio = 1        # the id of the next radio added
        self._radios_sent: list[dict[str, Any]] | None = None   # the radios' list as the clients last learnt it
        self._receivers_shared: list[dict[str, Any]] | None = None  # the radios' receivers as their processes learnt them
        self.closing: set[asyncio.Future] = set()   # sources switched away from, until they release their receiver
        # What each client receives first, in this order, then again whenever
        # it changes: its radio's states and the shared ones; remote
        # clients, none of LOCAL_STATES.
        self._states: dict[str, Callable[[Subscription], dict[str, Any]]] = {
            "source": lambda subscription: subscription.radio.state(subscription.remote),
            "demod": lambda subscription: subscription.radio.demod_state(subscription.acknowledged),
            "bookmarks": lambda _: self.bookmarks_state(),
            "band_plan": lambda _: self.band_plan_state(),
            "scanner": lambda subscription: subscription.radio.scanner_state(),
            "peaks": lambda subscription: subscription.radio.peaks_state(),
            "preferences": lambda subscription: self.preferences_state(subscription.device),
            "recording": lambda subscription: subscription.radio.recording_state(subscription.remote),
            "radios": lambda subscription: self.radios_state(subscription),
        }
        # The search for SoapySDR devices, with sources that open them.
        self.device_search: DeviceSearch | None = None
        self._search_task: asyncio.Future | None = None
        if sources is not None and "soapy" in sources.kinds:
            self.device_search = DeviceSearch()
            self.device_search.on_change = self._follow_devices
            self._states["devices"] = lambda subscription: subscription.radio.devices_state(subscription.remote)
        # The RTL-SDR receivers plugged in, read from sysfs when a client asks, never otherwise.
        self.rtl_listing: Callable[[], Sequence[RtlDevice] | None] | None = None
        if sources is not None and "rtl_sdr" in sources.kinds:
            # Sources lists them (rtldevices.py); a test's opener may not.
            self.rtl_listing = getattr(sources, "rtl_devices", None)
        self._states["scans"] = lambda _: self.scans_state()
        self._states["calibration"] = lambda subscription: subscription.radio.calibration_state()
        self._states["storage"] = lambda _: self.storage_state()
        self._states["update"] = lambda _: self.update_state()
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
        # Each receiver's saved settings, by its key; the other receivers'
        # are written back as they were read.
        self.receivers: dict[str, dict[str, Any]] = (
            {key: value for key, value in receivers.items() if isinstance(value, dict)}
            if isinstance(receivers, dict) else {})
        # The names the user gave the devices (devicenames.py).
        self.device_names = saved_names(settings.get("device_names"))
        saved_choice = settings.get("source")
        # What opens each kind of source again: the receiver used last of that kind.
        self.reopen: dict[str, dict[str, Any]] = {}
        if isinstance(saved_choice, dict) and isinstance(saved_choice.get("kind"), str):
            self.reopen[saved_choice["kind"]] = saved_choice
        saved_bookmarks = store.load(BOOKMARKS_FILE) if store is not None else {}
        self.bookmarks = Bookmarks(saved_bookmarks.get("bookmarks", []), saved_bookmarks.get("categories", []))
        # Observations: what detection found, kept across restarts. Their file
        # is written at the next save once due: observations_save_s after its
        # latest write, or at once after a client cleared some.
        self.observations = Observations(store.load(OBSERVATIONS_FILE) if store is not None else None, time.time())
        self._observations_save_s = observations_save_s
        self._observations_due = False
        self._observations_saved_s = time.monotonic()
        self._observations_timer: asyncio.TimerHandle | None = None
        # Kept scans: each range scan under a name with the channels it
        # found. Their file is written, and the clients' list sent, at most
        # SCANS_SAVE_S after a change while a scan runs, at once otherwise.
        self.kept = KeptScans(store.load(SCANS_FILE) if store is not None else None)
        self._scans_due = False
        self._scans_timer: asyncio.TimerHandle | None = None
        # The band plan in use, which gives the channels the signals show on,
        # the modes the range scan takes and WFM's de-emphasis.
        self._band_plan = _saved_band_plan(settings.get("band_plan"))
        self.plan = load_plan(self._band_plan)
        # Recording: the default folder and the one chosen, if any; the
        # recordings whose thread may still write, and the clients'
        # requests that wait for the disk.
        self.default_recordings = recordings
        self._chosen_recordings = _saved_recordings_folder(settings.get("recordings"))
        self.unfinished: set[Any] = set()
        self._requests: set[asyncio.Future] = set()
        if len(more_sources) >= MAX_RADIOS:
            raise ValueError(f"{MAX_RADIOS} radios at most")
        self.radios: list[AnyRadio] = []
        radio = self._new_radio(source, saved_choice=saved_choice, saved_scanner=settings.get("scanner"),
                                analyzer=analyzer)
        saved_radios = settings.get("radios")
        if source is None and not more_sources and sources is not None and isinstance(saved_radios, list):
            self._reopen_radios(sources, saved_radios)
        for more in more_sources:
            self.check_free(more)
            self._new_radio(more, saved_scanner=_saved_scanner(saved_radios, more.reopen))
        self._radios_sent = [radio.summary() for radio in self.radios]
        self._preferences = saved_preferences(settings.get("interface")) or {}     # this computer's
        # Updates: whether to check at start, what the latest check found, and the check running.
        self._updates = updates
        self._check_at_start = _saved_check_at_start(settings.get("updates"))
        reason = "This Radiolack does not check for updates." if updates is None else updates.unavailable_reason()
        self._update = UpdateResult("idle") if reason is None else UpdateResult("unavailable", detail=reason)
        self._checking: asyncio.Future | None = None
        if store is not None:
            self._saved = self._contents(observations=True, scans=True)     # nothing to write until a change,
            # but a source or a setting given at start replaces the saved one at the next write.
            saved_source = self.receivers.get(radio.source.settings_key, {}).get("source")
            default_choice = {"kind": self.sources.kinds[0]} if self.sources is not None else None
            if (_given_differ(radio.state(), saved_source, radio.source.given)
                    or self.sources is not None
                    and (saved_choice if saved_choice is not None else default_choice) != radio.source.reopen
                    or self._saved[SETTINGS_FILE].get("radios") != saved_radios):
                del self._saved[SETTINGS_FILE]
            self._store = store

    def subscribe(self, device: str | None = None) -> Subscription:
        """The messages for a new client, which shows the first radio:
        starting with the source state, the demodulation settings, the
        bookmarks, the scanner state, the signals found, the preferences,
        the recording state, the radios' list and, with sources that open
        SoapySDR devices, the devices state, the kept scans and the
        calibration state, then, unless it is a paired ``device`` that
        remote access serves, named by its id, the storage state, the update
        state and remote access's state."""
        states = {kind: describe for kind, describe in self._states.items()
                  if device is None or kind not in LOCAL_STATES}
        subscription = Subscription(states, self._queue_size, device=device)
        radio = self.radios[0]
        subscription.radio = radio
        if self._closed:
            subscription.end()
        else:
            self._clients.add(subscription)
            radio.attach(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._clients.discard(subscription)
        if subscription.radio is not None:
            subscription.radio.detach(subscription)

    @property
    def lists_rtl_devices(self) -> bool:
        """Whether the engine lists the RTL-SDR receivers plugged in (the
        hello's ``listings``)."""
        return self.rtl_listing is not None

    def radios_state(self, subscription: Subscription | None = None) -> dict[str, Any]:
        """The radios message (docs/design/protocol.md): each radio, what it
        reads and runs, and which one ``subscription`` shows."""
        shown = None if subscription is None or subscription.radio is None else subscription.radio.id
        return {"type": "radios", "radios": [radio.summary() for radio in self.radios], "shown": shown,
                "max": MAX_RADIOS, "names": dict(self.device_names)}

    def radios_changed(self) -> None:
        """What a radio reads or runs may have changed: every client
        receives the radios' list again, if it changed."""
        radios = [radio.summary() for radio in self.radios]
        if radios == self._radios_sent:
            return
        self._radios_sent = radios
        for subscription in self._clients:
            subscription.changed("radios")
        # The radios' processes learn the others' receivers, which the
        # centre frequency's moves, in the summaries, leave as they are.
        receivers = self.others(None)
        if receivers == self._receivers_shared:
            return
        self._receivers_shared = receivers
        for radio in self.radios:
            if isinstance(radio, RadioProcess):
                radio.share(radios=self.others(radio))

    def rename_device(self, radio: AnyRadio, value: object) -> None:
        """Give the device that ``radio`` reads the name ``value``, or
        forget its name for "". Raises ValueError, changing nothing, for a
        name that is not one (devicenames.py)."""
        name = checked_name(value)
        key = device_key(radio.summary())
        if self.device_names.get(key, "") == name:
            return
        self.device_names = renamed(self.device_names, key, name)
        self._radios_sent = None        # sent again with the names
        self.radios_changed()
        self.settings_changed()

    def configure_radios(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Answer a client's radio message, which asks for one thing: show
        the radio whose ``id`` it gives, ``add`` a radio reading the source
        that it describes, which the client then shows, ``remove`` the
        radio whose id it gives, or ``rename`` the device that a radio reads. Raises ValueError, changing nothing, when
        refused. Only this computer's interfaces may give a SoapySDR
        device's arguments (server.py)."""
        if len(changes) != 1:
            raise ValueError("a radio message asks for one thing: id, add, remove or rename")
        name, value = next(iter(changes.items()))
        if name == "id":
            self._show(subscription, self._radio(value))
        elif name == "add":
            self._show(subscription, self.add_radio(value))
        elif name == "remove":
            self.remove_radio(self._radio(value))
        elif name == "rename":
            if not isinstance(value, dict) or set(value) != {"id", "name"}:
                raise ValueError("rename gives a radio's id and its device's name")
            self.rename_device(self._radio(value["id"]), value["name"])
        else:
            raise ValueError(f"unknown radio field: {str(name)[:40]}")

    def add_radio(self, described: object) -> AnyRadio:
        """A new radio, reading the source that ``described`` gives as
        ``sources.open`` takes it, such as {"kind": "rtl_sdr", "device":
        "1"}, or for a SoapySDR device among those found, its ``driver``
        and ``serial``. It starts from the settings saved for its receiver.
        Raises ValueError, adding none, for MAX_RADIOS radios, a source
        that cannot open, or a receiver that another radio reads."""
        if self._closed:
            raise ValueError("Radiolack is stopping")
        if self.sources is None:
            raise ValueError("this Radiolack reads one receiver")
        if len(self.radios) >= MAX_RADIOS:
            raise ValueError(f"{MAX_RADIOS} receivers at most: remove one first")
        if not isinstance(described, dict) or not isinstance(described.get("kind"), str):
            raise ValueError("add describes a source, starting with its kind")
        if described["kind"] not in self.sources.kinds:
            raise ValueError(f"unknown kind of source: {described['kind'][:40]!r}")
        if described["kind"] == "soapy":
            self.check_no_search()
            described = self._soapy_found(described)
        source = self.sources.open(described)
        try:
            self.check_free(source)
        except ValueError:
            source.close()      # it never read: nothing to release
            raise
        radio = self._new_radio(source)
        self.reopen[source.kind] = source.reopen
        log.info("radio %d added", radio.id)
        self.radios_changed()
        self.settings_changed()
        return radio

    def remove_radio(self, radio: AnyRadio) -> None:
        """Remove ``radio``, unless it is the first: its scan and its
        recordings stop and its receiver is released, its settings saved
        for when it comes back; its clients show the first radio."""
        if radio is self.radios[0]:
            raise ValueError("the first receiver stays: change its device instead")
        self.receivers[radio.source.settings_key] = radio.receiver_settings()
        self.radios.remove(radio)
        for subscription in [subscription for subscription in self._clients if subscription.radio is radio]:
            self._show(subscription, self.radios[0])
        if radio.end_kept_scan("closed"):
            self.scans_changed(soon=True)
        radio.close()
        self.closing.add(asyncio.ensure_future(radio.wait_closed()))
        task = self._tasks.pop(radio, None)
        if task is not None:
            task.cancel()
        log.info("radio %d removed", radio.id)
        self.radios_changed()
        self.settings_changed()

    def check_free(self, source: Source, radio: AnyRadio | None = None) -> None:
        """Raises ValueError when a radio other than ``radio`` reads the
        receiver that ``source`` would read."""
        for other in self.radios:
            if other is not radio and (other.source.settings_key == source.settings_key
                                       or other.source.reopen == source.reopen):
                raise ValueError(f"receiver {other.id} reads this device already")

    def bookmarks_state(self) -> dict[str, Any]:
        """The bookmarks message (docs/design/protocol.md)."""
        return {"type": "bookmarks", "bookmarks": self.bookmarks.describe(), "categories": self.bookmarks.categories()}

    def edit_bookmarks(self, command: Mapping[str, object]) -> None:
        """Add, change or remove bookmarks, or their categories
        (bookmarks.py). Raises ValueError, changing nothing, when refused;
        otherwise every client receives the bookmarks, and each radio's
        memory scan watches them."""
        self.bookmarks.apply(command)
        for subscription in self._clients:
            subscription.changed("bookmarks")
        self.settings_changed()
        for radio in self.radios:
            radio.watch_bookmarks()

    def band_plan_state(self) -> dict[str, Any]:
        """The band plan message (docs/design/protocol.md): the plan in use."""
        return {"type": "band_plan", "plan": self._band_plan}

    def configure_band_plan(self, changes: Mapping[str, object]) -> None:
        """Change the band plan in use, ``plan``, one of bandplan.PLANS, for
        every client: from now on, the signals show on its channels, the
        range scan takes its modes and WFM its de-emphasis. Raises
        ValueError, changing nothing, for another field or plan."""
        for name in changes:
            if name != "plan":
                raise ValueError(f"unknown band plan field: {str(name)[:40]}")
        plan = checked_plan(changes.get("plan"))
        if plan == self._band_plan:
            return
        self._band_plan, self.plan = plan, load_plan(plan)
        for subscription in self._clients:
            subscription.changed("band_plan")
            subscription.changed("peaks")       # on the new plan's channels
        self._share(band_plan=plan)
        self.settings_changed()

    def observations_state(self) -> dict[str, Any]:
        """The observations message: the channels where detection found
        signals, the latest found first, each with the channel of the band
        plan it shows on."""
        if self.observations.forget(time.time()):
            self.observations_changed()
        channels = self.observations.describe()
        for channel in channels:
            channel["channel_hz"] = self.plan.channel(channel["frequency_hz"], 0.0)
        return {"type": "observations", "channels": channels}

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
            cleared = self.observations.clear(checked_frequency(changes["clear"], "a channel to clear"))
        elif "clear_all" in changes:
            if changes["clear_all"] is not True:
                raise ValueError("clear_all takes true")
            cleared = self.observations.clear_all()
        else:
            cleared = False
        if cleared:
            self.observations_changed(soon=True)
        subscription.reply(self.observations_state())

    def scans_state(self) -> dict[str, Any]:
        """The scans message: the kept scans, the latest first."""
        return {"type": "scans", "scans": self.kept.summaries()}

    def configure_scans(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Answer a client's scans message: ``get`` a kept scan's channels,
        which the client alone receives; ``rename`` one, with its new
        ``name``; ``delete`` one, not the scan that runs; or ``clear_all``
        (true) but the scan that runs. Every client then receives the kept
        scans. Raises ValueError, changing nothing, when refused."""
        for name in changes:
            if name not in SCANS_FIELDS:
                raise ValueError(f"unknown scans field: {str(name)[:40]}")
        if set(changes) not in ({"get"}, {"rename", "name"}, {"delete"}, {"clear_all"}):
            raise ValueError("a scans message is get, rename with name, delete or clear_all, alone")
        if "get" in changes:
            subscription.reply({"type": "scan", **self.kept.channels(changes["get"])})
            return
        if "rename" in changes:
            self.kept.rename(changes["rename"], changes["name"])
        elif "delete" in changes:
            self.kept.delete(changes["delete"])
        else:
            if changes["clear_all"] is not True:
                raise ValueError("clear_all takes true")
            self.kept.clear_all()
        self.scans_changed(soon=True)

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
        self.settings_changed()

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
            self.settings_changed()
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

    def configure_recordings(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Answer a client's recordings message, which without any field asks
        for the list of the recordings; ``delete``, a recording as the list
        names it, which must not be one that runs, deletes it first, with
        an I/Q recording's metadata. Once the disk answered, the client
        receives the list, or an error when the recording could not be
        deleted. Raises ValueError, changing nothing, when refused."""
        for name in changes:
            if name != "delete":
                raise ValueError(f"unknown recordings field: {str(name)[:40]}")
        self.check_recordings()
        delete = changes.get("delete")
        if "delete" in changes:
            if not is_recording_name(delete):
                raise ValueError("delete takes the file of a recording")
            if delete in self._running_files():
                raise ValueError("this recording runs: stop it before deleting it")
        self.request(self._answer_recordings(self.recordings_folder(), delete, subscription))

    def download_folder(self, name: str) -> Path | None:
        """The folder of the recording's file ``name``, to download it; None
        unless the name is a recording's file, or for a recording that
        runs."""
        folder = self.recordings_folder()
        if folder is None or not is_recording_file(name) or name in self._running_files():
            return None
        return folder

    def choose_recordings_folder(self, value: object, subscription: Subscription | None) -> None:
        """Record into the folder ``value``, a full path, or null for the
        default, which this computer's interfaces alone may change: once
        checked, as the disk may be slow, it applies, or ``subscription``
        learns why not. Raises ValueError, changing nothing, without a
        recordings folder, for a value that is not a full path, and while a
        recording runs."""
        self.check_recordings()
        folder = chosen_folder(value)
        if self._recording_runs():
            raise ValueError("a recording runs: stop it before choosing another folder")
        self.request(self._choose_folder(folder, subscription))

    def close(self) -> None:
        """Stop for good: each radio closes, its source, its recordings, a
        check of the driver's computation or a calibration that runs
        bringing its receiver back first; every client's messages end with
        ``None``, and a running update check stops. Safe to repeat."""
        for radio in self.radios:
            radio.close()
        self._closed = True
        self._stopped.set()
        for subscription in self._clients:
            subscription.end()
        self._clients.clear()
        for request in self._requests:
            request.cancel()
        if self._checking is not None:
            self._checking.cancel()
        if self.device_search is not None:
            self.device_search.close()
        if self._search_task is not None:
            self._search_task.cancel()
        if self._observations_timer is not None:
            self._observations_timer.cancel()
            self._observations_timer = None
        if self._scans_timer is not None:
            self._scans_timer.cancel()
            self._scans_timer = None

    async def wait_closed(self) -> None:
        """Return once the radios' sources, after ``close``, have released
        their receivers, like those switched away from, the update check
        has stopped its programs, the recordings have reached the disk, or
        RECORDING_CLOSE_S has passed for each, and the latest changes are
        saved."""
        if self._checking is not None:
            await asyncio.gather(self._checking, return_exceptions=True)
        if self.device_search is not None:
            await self.device_search.wait_closed()      # its thread no longer calls the modules
        for radio in self.radios:
            await radio.wait_closed()
        if self.closing:
            await asyncio.wait(set(self.closing))
        for recording in list(self.unfinished):
            if not await asyncio.to_thread(recording.join, RECORDING_CLOSE_S):
                log.warning("the recording %s did not reach the disk in time", recording.file or "just started")
        if self._store is None:
            return
        self._save_now.set()
        if self._saver is not None:
            await asyncio.gather(self._saver, return_exceptions=True)
        self._observations_due = True
        self.kept.end_all(time.time(), "closed")
        self._scans_due = True
        await self._save()      # also what a failed write left unsaved, the observations and the kept scans

    async def run(self) -> None:
        """Run the radios, those added meanwhile too, until closed or
        cancelled, having started the update check unless it is turned off
        at start or cannot run."""
        if self._check_at_start and self._update.status != "unavailable":
            self.check_updates()
        for radio in self.radios:
            self._tasks[radio] = asyncio.ensure_future(radio.run())
        try:
            await self._stopped.wait()
        finally:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def settings_changed(self) -> None:
        """Save soon. ``wait_closed`` saves what changes after ``close``."""
        if self._store is None or self._closed:
            return
        self._unsaved = True
        if self._saver is None or self._saver.done():
            self._saver = asyncio.ensure_future(self._save_later())

    def observations_changed(self, soon: bool = False) -> None:
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

    def scans_changed(self, soon: bool = False) -> None:
        """The kept scans changed: every client receives them, and their file
        is written, at once when ``soon``, as when a scan starts or ends or
        a client renamed or deleted one, otherwise SCANS_SAVE_S later, as
        while a scan finds channels. ``wait_closed`` writes it in any case."""
        if self._closed:
            return
        if soon:
            if self._scans_timer is not None:
                self._scans_timer.cancel()
            self._scans_save_due()
        elif self._scans_timer is None:
            self._scans_timer = asyncio.get_running_loop().call_later(SCANS_SAVE_S, self._scans_save_due)

    def recordings_folder(self) -> Path | None:
        """Where recordings go: the folder chosen, or the default; None for
        an engine that records nothing."""
        if self.default_recordings is None:
            return None
        return self._chosen_recordings if self._chosen_recordings is not None else self.default_recordings

    def check_recordings(self) -> None:
        if self.recordings_folder() is None:
            raise ValueError("this Radiolack does not record")

    def request(self, work: Coroutine[Any, Any, None]) -> None:
        """Run a client's request that waits for the disk, unless closed: the
        engine stops it when it closes."""
        if self._closed:
            work.close()
            return
        request = asyncio.ensure_future(work)
        self._requests.add(request)
        request.add_done_callback(self._request_done)

    def check_no_search(self) -> None:
        """Raise ValueError while a search for SoapySDR devices runs: it
        could disturb a device that opens meanwhile (soapysearch.py)."""
        if self.device_search is not None and self.device_search.running:
            raise ValueError("a search for SoapySDR devices is running: choose once it ends")

    def start_search(self) -> None:
        """Start a search for SoapySDR devices, unless one runs: it waits
        for the receivers switched away from to be released, then searches
        every listed driver but that of the SoapySDR device in use."""
        search = self.device_search
        if search is None or self._closed or not search.begin():
            return
        self._search_task = asyncio.ensure_future(self._search_devices(search))

    def _update_changed(self) -> None:
        for subscription in self._clients:
            subscription.changed("update")

    def _remote_changed(self) -> None:
        """Remote access changed: this computer's interfaces learn it, and what
        it keeps is saved if that changed."""
        for subscription in self._clients:
            subscription.changed("remote")
        self.settings_changed()

    # The radios in processes of their own (radioproxy.py).

    def shared_copy(self, radio: RadioProcess) -> dict[str, Any]:
        """What a radio's process needs of what the radios share, as it
        starts."""
        return {"sources": self.sources.recipe(),   # type: ignore[union-attr]
                "receivers": self.receivers, "reopen": self.reopen, "band_plan": self._band_plan,
                "bookmarks": self.ticked_copy(),
                "radios": self.others(radio), "search": self.search_copy(), "recordings": self._recordings_copy()}

    def ticked_copy(self) -> list[dict[str, Any]]:
        """The bookmarks that the memory scan goes through, with their ids,
        for the radios' processes."""
        return [{"id": bookmark_id, **bookmark.describe()} for bookmark_id, bookmark in self.bookmarks.ticked()]

    def others(self, radio: AnyRadio | None) -> list[dict[str, Any]]:
        """The receivers of the radios but ``radio``, for its process; of
        every radio for None."""
        return [{"id": other.id, "kind": other.source.kind, "reopen": other.source.reopen,
                 "settings_key": other.source.settings_key} for other in self.radios if other is not radio]

    def search_copy(self) -> dict[str, Any] | None:
        """The search for SoapySDR devices, for the radios' processes."""
        search = self.device_search
        if search is None:
            return None
        return {"drivers": list(search.drivers), "status": search.status, "detail": search.detail,
                "devices": [[device.driver, device.serial, device.label] for device in search.devices]}

    def _recordings_copy(self) -> list[str | None]:
        return [None if self.default_recordings is None else str(self.default_recordings),
                None if self._chosen_recordings is None else str(self._chosen_recordings)]

    def _share(self, **shared: Any) -> None:
        for radio in self.radios:
            if isinstance(radio, RadioProcess):
                radio.share(**shared)

    def follow_settings(self, radio: RadioProcess, receivers: Mapping[str, Any], reopen: Mapping[str, Any]) -> None:
        """A radio's process changed its settings, those saved for
        receivers and the receivers used last: save them."""
        for key, value in receivers.items():
            if value is None:
                self.receivers.pop(key, None)
            elif isinstance(value, dict):
                self.receivers[key] = value
        for kind, value in reopen.items():
            if isinstance(value, dict):
                self.reopen[kind] = value
        self.settings_changed()

    def follow_kept_scan(self, radio: RadioProcess, scan: object, soon: bool) -> None:
        """A radio's process changed its kept scan."""
        key = (radio.id, scan.get("id") if isinstance(scan, dict) else None)
        if self.kept.follow(key, scan):
            self.scans_changed(soon=soon)

    def follow_observations(self, found: Sequence[Any], now: float, hour: int) -> None:
        """A radio's process found signals: they join the observations."""
        self.observations.record(found, now, hour)
        self.observations_changed()

    def radio_failed(self, radio: RadioProcess, reason: str) -> None:
        """A radio's process ended unasked: the radio goes, its clients
        showing the first radio."""
        if radio not in self.radios:
            return
        self.radios.remove(radio)
        self._tasks.pop(radio, None)
        for subscription in [subscription for subscription in self._clients if subscription.radio is radio]:
            self._show(subscription, self.radios[0])
            subscription.reply({"type": "error", "command": "radio", "message": f"receiver {radio.id} stopped: {reason}"})
        self.radios_changed()
        self.settings_changed()

    def _new_radio(self, source: Source | None, *, saved_choice: object = None, saved_scanner: object = None,
                   analyzer: SpectrumAnalyzer | None = None) -> AnyRadio:
        """A new radio reading ``source``, which runs at once if the engine
        does."""
        radio: AnyRadio
        if self._radio_processes and self.radios and source is not None:
            radio = RadioProcess(self, source, radio_id=self._next_radio, saved_scanner=saved_scanner,
                                 frame_rate=self._frame_rate)
        else:
            radio = Radio(self, source, radio_id=self._next_radio, saved_choice=saved_choice,
                          saved_scanner=saved_scanner, frame_rate=self._frame_rate, analyzer=analyzer)
        self._next_radio += 1
        self.radios.append(radio)
        if self._tasks or (self.radios and self.radios[0] in self._tasks):
            self._tasks[radio] = asyncio.ensure_future(radio.run())
        return radio

    def _reopen_radios(self, sources: SourceOpener, saved: list[object]) -> None:
        """Reopen the radios used last after the first, as saved, leaving
        out those that cannot open or read a receiver that another reads."""
        for entry in saved:
            if len(self.radios) >= MAX_RADIOS:
                log.warning("only %d saved radios are used", MAX_RADIOS)
                return
            if not isinstance(entry, dict):
                log.warning("a saved radio is not used: it is not described")
                continue
            try:
                source = sources.open(entry.get("source") if isinstance(entry.get("source"), dict) else {})
                self.check_free(source)
            except ValueError as error:
                log.warning("a saved radio is not used: %s", error)
                continue
            self._new_radio(source, saved_scanner=entry.get("scanner"))

    def _radio(self, radio_id: object) -> AnyRadio:
        if isinstance(radio_id, bool) or not isinstance(radio_id, int):
            raise ValueError("a radio's id must be a whole number")
        for radio in self.radios:
            if radio.id == radio_id:
                return radio
        raise ValueError("no such receiver: it may have been removed")

    def _show(self, subscription: Subscription, radio: AnyRadio) -> None:
        """``subscription`` shows ``radio`` from now on."""
        old = subscription.radio
        if old is radio or self._closed:
            return
        if old is not None:
            old.detach(subscription)
        subscription.show(radio)
        radio.attach(subscription)

    def _soapy_found(self, described: Mapping[str, object]) -> dict[str, Any]:
        """A SoapySDR device to add, as ``sources.open`` takes it: by its
        device arguments, or among those found by its driver and serial."""
        if set(described) == {"kind", "args"}:
            return dict(described)
        if set(described) != {"kind", "driver", "serial"}:
            raise ValueError("a SoapySDR device to add is one found, by its driver and serial, or its arguments")
        search = self.device_search
        found = None if search is None else search.find(described["driver"], described["serial"])
        if found is None:
            raise ValueError("this device is not among those found: search again")
        return {"kind": "soapy", "args": found.args}

    def _follow_devices(self) -> None:
        """The search's state changed: each radio's clients learn it."""
        for radio in self.radios:
            radio.follow_devices()

    def _scans_save_due(self) -> None:
        self._scans_timer = None
        self._scans_due = True
        for subscription in self._clients:
            subscription.changed("scans")
        self.settings_changed()

    def _observations_save_due(self) -> None:
        self._observations_timer = None
        self._observations_due = True
        self.settings_changed()

    def _recording_runs(self) -> bool:
        return any(radio.recording_runs() for radio in self.radios)

    def _running_files(self) -> set[str]:
        """The files of the recordings that run, which their threads write."""
        return set().union(*(radio.running_files() for radio in self.radios))

    async def _choose_folder(self, folder: Path | None, subscription: Subscription | None) -> None:
        """Record into ``folder``, or into the default one (None), once
        checked: unless it is no folder that Radiolack may write into, or a
        recording started meanwhile, which ``subscription`` then learns."""
        try:
            if folder is not None:
                await asyncio.to_thread(check_folder, folder)
            if self._recording_runs():
                raise ValueError("a recording runs: stop it before choosing another folder")
        except ValueError as error:
            if subscription is not None:
                subscription.reply({"type": "error", "command": "recording", "message": str(error)})
            return
        if folder != self._chosen_recordings:
            self._chosen_recordings = folder
            for client in self._clients:
                client.changed("recording")
            self._share(recordings=self._recordings_copy())
            self.settings_changed()

    async def _answer_recordings(self, folder: Path, delete: str | None, subscription: Subscription) -> None:
        """Delete the recording ``delete``, if given, then send the list of
        the recordings to ``subscription``, or an error when the recording
        could not be deleted. The disk is read in a worker thread."""
        if delete is not None:
            # A recording stopped a moment ago may still be closing its files,
            # which would bring an I/Q recording's metadata back: once done.
            for recording in [recording for recording in self.unfinished if recording.file == delete]:
                await asyncio.to_thread(recording.join, RECORDING_CLOSE_S)
            try:
                await asyncio.to_thread(delete_recording, folder, delete)
            except FileNotFoundError:
                pass        # deleted meanwhile
            except (OSError, ValueError) as error:
                reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
                subscription.reply({"type": "error", "command": "recordings",
                                    "message": f"{delete} cannot be deleted: {reason}"})
        listed = await asyncio.to_thread(list_recordings, folder, self._running_files())
        subscription.reply({"type": "recordings", **listed})

    def _request_done(self, request: asyncio.Future) -> None:
        self._requests.discard(request)
        if not request.cancelled() and request.exception() is not None:
            log.error("a request from an interface failed", exc_info=request.exception())

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

    async def _search_devices(self, search: DeviceSearch) -> None:
        try:
            if self.closing:
                await asyncio.wait(set(self.closing))       # a device still closing is not searched
            await search.run({device.driver for radio in self.radios
                              if (device := radio.soapy_in_use()) is not None})
        except asyncio.CancelledError:
            pass        # the engine stops
        except Exception:
            log.exception("the SoapySDR device search stopped")
            search.abandon()        # Search again works again

    def _contents(self, observations: bool = False, scans: bool = False) -> dict[str, dict[str, Any]]:
        """What the settings, bookmarks and remote access files hold, as
        things stand, with ``observations`` the observations file and with
        ``scans`` the kept scans' file: with
        sources to open, which sources open at the next start."""
        radio = self.radios[0]
        settings: dict[str, Any] = {} if self.sources is None else {"source": radio.source.reopen}
        # Every radio's receiver as it stands, which that radio starts from again.
        settings["receivers"] = {**self.receivers, **{each.source.settings_key: each.receiver_settings()
                                                      for each in self.radios}}
        settings["scanner"] = radio.scanner_settings()
        if self.sources is not None and len(self.radios) > 1:
            # The radios after the first, which an older Radiolack leaves out.
            settings["radios"] = [{"source": other.source.reopen, "scanner": other.scanner_settings()}
                                  for other in self.radios[1:]]
        settings["band_plan"] = self._band_plan
        settings["device_names"] = dict(self.device_names)
        settings["interface"] = dict(self._preferences)
        settings["updates"] = {"check_at_start": self._check_at_start}
        chosen = self._chosen_recordings
        settings["recordings"] = {"folder": None if chosen is None else str(chosen)}
        contents = {SETTINGS_FILE: settings, BOOKMARKS_FILE: self.bookmarks.stored()}
        if self._remote is not None:
            contents[REMOTE_FILE] = self._remote.stored()
        if observations:
            contents[OBSERVATIONS_FILE] = self.observations.stored()
        if scans:
            contents[SCANS_FILE] = self.kept.stored()
        return contents

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
        worker thread, as the disk may be slow, the observations' and the
        kept scans' once due.
        Clients learn when the storage state changes."""
        store = self._store
        if store is None:
            return
        before = self.storage_state()
        observations = self._observations_due
        if observations:
            self._observations_due = False
            # Counted from here while the file is written, so that a change
            # made meanwhile does not ask for another write at once.
            self._observations_saved_s = time.monotonic()
            self.observations.forget(time.time())
        scans, self._scans_due = self._scans_due, False
        try:
            for name, content in self._contents(observations, scans).items():
                if content != self._saved.get(name) and await asyncio.to_thread(store.save, name, content):
                    self._saved[name] = content
        except Exception:
            log.exception("the settings could not be saved")
        if observations:
            # The write may have taken a moment: the time to the next one
            # counts from its end, for a write asked for meanwhile too.
            self._observations_saved_s = time.monotonic()
            if self._observations_timer is not None:
                self._observations_timer.cancel()
                self._observations_timer = asyncio.get_running_loop().call_later(
                    self._observations_save_s, self._observations_save_due)
        if self.storage_state() != before:
            for subscription in self._clients:
                subscription.changed("storage")


def _given_differ(state: Mapping[str, Any], saved: object, given: frozenset[str]) -> bool:
    """Whether a setting the source was given differs from the saved one."""
    saved_values = saved if isinstance(saved, dict) else {}
    return any(name in state["settable"] and saved_values.get(name) != state[name] for name in given)


def _saved_scanner(saved_radios: object, reopen: Mapping[str, Any]) -> object:
    """The scanner settings saved for the radio that read the receiver
    that ``reopen`` opens, if any."""
    for entry in saved_radios if isinstance(saved_radios, list) else ():
        if isinstance(entry, dict) and entry.get("source") == reopen:
            return entry.get("scanner")
    return None


def _saved_recordings_folder(saved: object) -> Path | None:
    """The recordings folder chosen, as saved: None for the default."""
    if saved is None:
        return None
    try:
        if not isinstance(saved, dict):
            raise ValueError("not an object")
        return chosen_folder(saved.get("folder"))
    except ValueError:
        log.warning("the saved recordings folder is not used")
        return None


def _saved_band_plan(saved: object) -> str:
    """The band plan in use, as saved: Europe's unless another was saved."""
    if saved is None:
        return DEFAULT_PLAN
    try:
        return checked_plan(saved)
    except ValueError as error:
        log.warning("the saved band plan is not used: %s", error)
        return DEFAULT_PLAN


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

