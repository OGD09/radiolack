"""A radio: what the engine does for one receiver (engine.py).

A radio reads blocks of I/Q samples from its source only while at least one
of its clients listens, or one of its scans, its calibration or its
recordings runs, and in a worker thread turns each block into a display
frame, which carries the VFO's channel level and squelch, and into audio
(demod.py), which goes only to the clients that asked for it. A client that
falls behind loses its oldest frames instead of slowing the others. The
radio also carries the source's state and the demodulation settings to its
clients, and the clients' changes to them: a change reaches every client at
once, and the last change wins. Each client also learns which of its own
demodulation changes the radio has handled, so that it can tell an echo of
an older change from the latest state. The noise blanker, a demodulation
setting, acts on the samples that make the VFO's channel alone: the display
frames, detection and the scans' search take every block as received.

DC removal is the radio's own setting, on by default and shared by its
clients like the source's: it takes each block's mean off its samples, so
that a constant offset, such as an RTL-SDR's, does not show as a spike at
the centre frequency. It also removes a signal exactly on the centre
frequency.

With the sources that the engine may open (sources.py), the radio switches
to another kind of source when a client asks: the source in use closes,
keeping its settings, and the new one starts from those saved for its
receiver. Without a source to start with, it opens the one used last, the
first kind the first time. With sources that open SoapySDR devices, a
client may open one of the devices that the engine's search found, and
this computer's interfaces any other, by its device arguments, as another
source.

The radio finds the signals in its spectra (scanners.py) while it reads,
unless detection is turned off: its clients receive the list of the
signals found, at most once a second, each with the frequency it shows on,
its band plan channel or its frequency to the 100 Hz (bandplan.py), and the
scanner settings, which are shared like the source's and saved with them,
ignored frequencies included. What it finds, apart from the simulated
band's, goes to the engine's observations (observations.py). Any client
may start a scan, one at a time: the memory scan, which tunes the VFO to
the ticked bookmarks of the span as they become active, and with a
receiver that tunes visits those outside the span too, window after
window; with a receiver that tunes, the sweep, which steps the receiver
across a range in windows and brings it back, with the VFO, once a client
stops it, unless it stops on a signal it listens to: the range scan
(docs/design/range-scan.md), which may stop to listen to the active
signals of each window in turn with the VFO alone, in the band plan's mode,
may stop by itself after a number of passes, and never tunes the receiver
for a range within the part of the span that detection searches, and
which the engine keeps under a name (keptscans.py); "signals", which an
older page sends, starts it over that part of the span, stopping to
listen. A memory scan that stops while it watches, once it moved the
receiver, brings them back too. A scan keeps the radio reading without any
client, until a client stops it, tunes by hand or chooses another source.
Whether it runs is never saved: while a scan runs, the receiver's saved
settings are those of the scan's start.

With a recordings folder (recordings.py), any client may record what the
VFO plays, and the receiver's raw I/Q, at the same time or not: a recording
keeps the radio reading without any client, like a scan, until a client
stops it. The audio's goes on when the VFO moves, a scan included, or
another receiver replaces this one, and stops by itself once the audio rate
changes, into or out of WFM, and on the simulated band, which is never
recorded. The I/Q's takes the samples as the source receives them, before
the DC removal, those the radio leaves when it falls behind included; it
goes on when the receiver retunes, a scan included, and after a gap in the
samples, and stops by itself once the sample rate changes or another
receiver is chosen. Both stop by themselves when their disk falls short of
space, the audio's file is full or the disk fails; every client then learns
why. Neither starts by itself nor is saved.

This computer's interfaces may ask for a problem report (report.py), which
the radio sends to the one that asked, for its user to share or not. Any
interface may ask for the measure of the carrier at the VFO (carrier.py),
to calibrate the receiver's frequency correction: the radio gathers the
VFO's channel, before the demodulator and unblanked whether or not the
noise blanker is on, only while an interface waits for a measure, which
every interface that asked meanwhile shares and receives, each alone. On
an R820T or R828D, an interface may also ask which of the rtl-sdr
driver's computations its PLL follows (pllcheck.py): the radio measures
that carrier at the centre in use and at centres off the tuner's grid, for
every interface, brings the receiver back whatever ends the check, and
saves the computation found with the receiver.

Any client may also start the automatic calibration (calibration.py), and
stop it: like a scan, the radio tunes the receiver and the VFO, for every
client, to each standard-frequency station of the band plan within its
reach, measures its carrier, then brings them back, and every client
learns the run's progress, its results and the correction it proposes,
which a client applies as any change of the correction. It stops a scan,
a check of the driver's computation and the carrier's measures wait for
it, and a scan, a recording, another receiver, or a change of the source's
settings or of the VFO's frequency, mode or filter stops it, the receiver
and the VFO coming back first; it starts neither while a recording runs,
nor behind a converter, nor on a source without a correction, and never
by itself.

The radio starts from the settings saved for its receiver (each receiver,
by its ``settings_key``, has its own) and its scanner settings, which the
engine reads and saves. A saved setting that the receiver refuses keeps
its default, and one the source was ``given``, such as an address on the
command line, keeps the given value, which is saved in its place.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import functools
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from . import calibration
from .bandplan import plan_mode
from .bookmarks import Bookmark
from .carrier import SILENT, UNSTABLE, Carrier, CarrierMeasure
from .converter import HIGHEST_HZ, Conversion
from .demod import (AUDIO_MODES, MODE_BANDWIDTHS_HZ, MODES, SQUELCH_MARGIN_DB, DemodSettings, Demodulator, Meter,
                    WFM_DEEMPHASIS_S, has_channel, noise_clearance_hz, passband_hz, vfo_position_hz)
from .fine import REMOTE_FINE_BINS, FineSpectrum, encode_fine
from .keptscans import KeptScan, KeptScans, checked_name
from .pllcheck import LOST, MAX_MOVE_HZ, Point, choose_points, identify
from .recordings import WAV_HEADER, AudioRecording, IQRecording, Recording, meta_name
from .report import Report, check_page, receiver_line
from .rtldevices import RtlDevice, reads_as_index
from .soapy import SoapySource
from .soapysearch import Device, device as soapy_device
from .scanners import (DC_GAP_HZ, DEFAULT_LISTEN_S, DEFAULT_RESUME_S, DEFAULT_SWEEP_HZ, DEFAULT_THRESHOLD_DB,
                       DEFAULT_WINDOW_S, DETECTION_BLOCKS, MAX_IGNORED, SCAN_NAMES, SCANS, USABLE_FRACTION, Channel,
                       ChannelWatch, Detection, Detector, MemoryScan, MemoryTour, Placement, Signal, Signals,
                       SignalScan, Sweep, SweepWindows, checked_frequency, checked_listen, checked_passes,
                       checked_resume, checked_sweep_range, checked_threshold, checked_window, memory_window,
                       memory_windows, range_window, same_signal, search_blocks, sweep_windows, watched_channels)
from .spectrum import SpectrumAnalyzer, encode_frame, fewer_bins
from .store import shown_path

if TYPE_CHECKING:
    from .engine import Engine, Source, SourceOpener, Subscription

FRAME_RATE = 30.0       # display frames per second
REMOTE_BLOCKS = 2       # a remote client's display frame: the mean power of this many blocks
REMOTE_BINS = 2048      # at most this many bins in a remote client's display frames
UNRECORDED_KINDS = ("synthetic",)   # sources neither observed nor recorded: the simulated band
PEAKS_INTERVAL_S = 1.0  # the signals found go to the clients at most this often
LISTEN_NEEDS_DETECTION = "the range scan stops to listen only while detection is on: turn it on first"
SCANNER_SETTINGS = ("detection", "threshold_db", "resume_s", "listen_s", "sweep_low_hz", "sweep_high_hz",
                    "sweep_window_s", "sweep_listen", "sweep_passes", "ignore", "restore", "scan", "name")
CARRIER_TIMEOUT_S = 6.0     # a carrier's measure that settings kept starting again ends this long after it began
MAX_REQUEST_ID = 2 ** 53 - 1    # the largest whole number JavaScript counts exactly
CHECK_POINT_TIMEOUT_S = 6.0     # a point of the driver's check whose carrier is not measured within this ends it
CHECK_CLEARANCE_HZ = 1_000.0    # the VFO stays this far from the centre at every point of the driver's check
STOPPED = "stopped"     # a driver's check that an interface's change, or the engine's stop, ended
STATION_TIMEOUT_S = 6.0     # a calibration's station whose carrier is not measured within this gives none
# Source settings that leave a calibration running: they change what the
# receiver hears, not where it tunes nor how its samples are labelled.
CALIBRATION_KEEPS = ("gain_db", "stage_gains_db", "rtl_agc", "bias_tee", "antenna", "dc_removal")
RECORDING_PROGRESS_S = 1.0      # a recording's length and size go to the clients this often
LOCAL_SETTINGS = ("address",)   # source settings only this computer's interfaces may change
# Source settings that start at their default at each start, never saved:
# the antenna power, off, and the tuner's grid, on.
UNSAVED_SETTINGS = ("bias_tee", "tuner_grid")
# Why a recording stopped by itself, as the output tells it.
_RECORDING_STOPS = {"rate": "the audio rate changed, into or out of WFM", "source": "the simulated band was chosen",
                    "sample_rate": "the receiver's sample rate changed", "receiver": "another receiver was chosen",
                    "converter": "the converter in front of the receiver changed",
                    "space": "less than 2 GiB is free on its disk", "full": "its file reached the WAV limit of 4 GiB",
                    "disk": "a disk error"}

log = logging.getLogger(__name__)


class Radio:
    """Turn one source into display frames and audio for its clients, the
    engine's subscriptions that show it (``clients``), which the engine
    attaches and detaches. ``radio_id`` names it to the clients.

    The radio reads ``source``, or without one the source that the engine's
    sources open from ``saved_choice``, the source saved last, and starts
    from ``saved_scanner``, the scanner settings saved. Its ``engine``
    holds what radios share: the bookmarks, the band plan, the
    observations, the kept scans, the receivers' saved settings, the
    sources and the recordings folder, and saves them."""

    def __init__(self, engine: Engine, source: Source | None, *, radio_id: int = 1, saved_choice: object = None,
                 saved_scanner: object = None, frame_rate: float = FRAME_RATE,
                 analyzer: SpectrumAnalyzer | None = None) -> None:
        self.id = radio_id
        self._engine = engine
        self.analyzer = analyzer if analyzer is not None else SpectrumAnalyzer()
        self._frame_rate = frame_rate
        self._dc_removal = True
        self.clients: set[Subscription] = set()
        self._wanted = asyncio.Event()     # set while a client listens, or to stop
        self._closed = False
        self._sequence = 0
        self._remote_sequence = 0           # remote clients' frames have their own sequence
        self._remote_blocks: list[np.ndarray] = []  # bin powers of the blocks of their next frame
        self._remote_capture: tuple[float, float] | None = None    # centre and rate of those blocks
        self._demodulator = Demodulator()   # used by one block at a time
        self._sam_locked = False            # whether SAM held its carrier after the last block
        self._notch_lines = 0               # the lines that the notch removed after the last block
        self._blanker_impulses = 0          # the impulses that the blanker zeroed in the latest second
        # The fine spectrum, while an interface asks for it, and its frames' sequence.
        self._fine: FineSpectrum | None = None
        self._fine_sequence = 0
        # The centre asked and the rate of the last block, and its centre in use and rate.
        self._capture: tuple[tuple[float, float], tuple[float, float]] | None = None
        # The span asked when a client last tuned the VFO: its samples leave the VFO where the
        # client set it, even outside it (protocol.md, the demodulation state's frequency_hz).
        self._vfo_set_in: tuple[float, float] | None = None
        self._devices_sent: dict[str, Any] | None = None    # the devices state as the clients last learnt it
        if source is not None:
            self.source = source
        elif engine.sources is not None:
            self.source = _open_saved(engine.sources, saved_choice)
        else:
            raise ValueError("an engine needs a source, or sources to open one")
        engine.reopen[self.source.kind] = self.source.reopen
        self._kept_name: str | None = None      # the name given to the range scan about to start
        self._kept: KeptScan | None = None      # the kept scan of this radio's range scan, if it ran
        self._kept_resumed = False      # the range scan starting again goes on with the kept scan that runs
        self._kept_step_s = 0.0         # the latest block of the range scan, in time.monotonic()
        # Scanners: detection's settings, the signals found and the ignored
        # frequencies; the memory scan's resume time, which the range scan
        # takes too, the bookmarks it watches, the windows they take and the
        # channels of the span; the range scan's (the sweep's) range, time
        # per window, listening and passes; its longest listening time; the
        # scan, while one runs, whether the range scan runs over the current
        # window that "signals" gave it, and the receiver's centre frequency
        # and the VFO's settings at its start.
        self._detector = Detector()         # used by one block at a time, like the demodulator
        self._detection = True
        self._threshold_db = DEFAULT_THRESHOLD_DB
        self._signals = Signals()
        self._resume_s = DEFAULT_RESUME_S
        self._listen_s = DEFAULT_LISTEN_S
        self._watch = ChannelWatch()        # used by one block at a time, like the demodulator
        self._memory_ids: tuple[int, ...] = ()      # the ticked bookmarks within reach, in frequency order
        self._windows: tuple[float, ...] = ()       # the centre frequencies of the windows they take
        self._watched: tuple[Channel, ...] = ()     # while the memory scan runs, the ticked bookmarks of the span
        self._bands = np.empty((0, 3))      # their passbands and noise clearances, one row each
        self._sweep_low_hz, self._sweep_high_hz = DEFAULT_SWEEP_HZ
        self._sweep_window_s = DEFAULT_WINDOW_S
        self._sweep_listen = False
        self._sweep_passes: int | None = None     # endless
        self._memory: MemoryScan | None = None
        self._tour: MemoryTour | None = None        # the memory scan's windows, while it runs
        self._sweep: Sweep | None = None
        self._sweep_here = False    # the range scan runs over the current window, as "signals" started it
        self._signal_tuned = (0.0, 0.0)    # the frequency and width of the signal a scan tuned to last
        self._home: tuple[float, DemodSettings] | None = None   # at the start of the scan that runs
        # Which scan stopped by itself last, or could not start as given at
        # start, and why; None once a client stopped one, or starts one.
        self._stopped: tuple[str, str] | None = None
        self._restore_scanner(saved_scanner)
        self._peaks_sent_s = -math.inf      # when the clients were last told of the signals
        self._peaks_timer: asyncio.TimerHandle | None = None
        # Recording: the audio recording that runs, the rate of its audio
        # once it came and how many samples it was given; the I/Q recording
        # that runs and how many bytes it was given; why the latest stopped
        # by itself.
        self._audio: AudioRecording | None = None
        self._audio_rate: float | None = None
        self._audio_samples = 0
        self._iq: IQRecording | None = None
        self._iq_bytes = 0
        self._recording_stopped: dict[str, str] | None = None
        self._progress_timer: asyncio.TimerHandle | None = None
        # The carrier's measure while an interface waits for it: the
        # interfaces waiting, with the id each gave its latest request, when
        # it began, and the count of the source's changes, which start it
        # again.
        self._carrier: CarrierMeasure | None = None
        self._carrier_waiting: dict[Subscription, int | None] = {}
        self._carrier_began = 0.0
        self._source_changes = 0
        # The check of the driver's computation while one runs (pllcheck.py).
        self._driver_check: _DriverCheck | None = None
        # The automatic calibration that runs, or the latest, until another
        # starts: it runs while its ``ended`` is None.
        self._calibration: _CalibrationRun | None = None
        self._adopt()
        self.watch_bookmarks()

    def attach(self, subscription: Subscription) -> None:
        """A client shows this radio from now on: it reads for it, and
        computes the fine spectrum if the client asked for it."""
        self.clients.add(subscription)
        self._follow_fine()
        self._wanted.set()

    def summary(self) -> dict[str, Any]:
        """What the radios message says of this radio: what it reads, and
        the scan and the recordings that run."""
        return {"id": self.id, **source_summary(self.source), "scan": self._running_scan(),
                "recording": [kind for kind, running in (("audio", self._audio), ("iq", self._iq))
                              if running is not None]}

    @property
    def kept_scan(self) -> KeptScan | None:
        """The kept scan of this radio's range scan, the latest it ran."""
        return self._kept

    def end_kept_scan(self, ended: str) -> bool:
        """End this radio's kept scan, if it runs, for ``ended``, as when
        the radio is removed. Returns whether it ran."""
        return KeptScans.end(self._kept, time.time(), ended)

    def detach(self, subscription: Subscription) -> None:
        """A client no longer shows this radio: the measures and the check
        it alone waited for end, and without any client, scan, calibration
        or recording, the radio stops reading."""
        self.clients.discard(subscription)
        self._follow_fine()
        self._carrier_waiting.pop(subscription, None)
        if not self._carrier_waiting:
            self._carrier = None        # nobody waits: nothing more to measure
        check = self._driver_check
        if check is not None:
            check.waiting.pop(subscription, None)
            if not check.waiting:
                self._end_driver_check(STOPPED)     # nobody waits: the receiver comes back
        if self._idle():
            self._wanted.clear()

    def scanner_settings(self) -> dict[str, Any]:
        """The scanner settings as the engine saves them."""
        return {"detection": self._detection, "threshold_db": self._threshold_db,
                "resume_s": self._resume_s, "listen_s": self._listen_s,
                "sweep_low_hz": self._sweep_low_hz, "sweep_high_hz": self._sweep_high_hz,
                "sweep_window_s": self._sweep_window_s, "sweep_listen": self._sweep_listen,
                "sweep_passes": self._sweep_passes, "ignored_hz": self._signals.ignored}

    def close(self) -> None:
        """Stop for good: the source is closed and the recordings that run
        stop; a check of the driver's computation brings the receiver back
        first, as does a calibration that runs. The engine ends the
        clients' messages. Safe to repeat."""
        self._end_driver_check(STOPPED)
        self._end_calibration("closed")
        self._closed = True
        self.clients.clear()
        self._wanted.set()
        self.source.close()
        if self._iq is not None:
            self.source.tap(None)
        for recording in (self._audio, self._iq):
            if recording is not None:
                recording.stop()        # its thread writes what came, then closes its files
        self._audio = self._iq = None
        if self._progress_timer is not None:
            self._progress_timer.cancel()
            self._progress_timer = None
        if self._peaks_timer is not None:
            self._peaks_timer.cancel()
            self._peaks_timer = None
        self._carrier = None
        self._carrier_waiting.clear()
        self._fine = None

    async def wait_closed(self) -> None:
        """Return once the source, after ``close``, has released its receiver."""
        await self.source.wait_closed()

    async def run(self) -> None:
        """Produce frames until closed or cancelled; an error closes the
        engine."""
        try:
            while True:
                await self._wanted.wait()
                if self._closed:
                    return
                if self._engine.closing:
                    # The new source may need the same receiver: it starts once released.
                    closing = set(self._engine.closing)
                    await asyncio.wait(closing)     # unlike gather, a cancellation leaves them running
                    self._engine.closing -= closing
                    continue
                source = self.source
                block = max(self.analyzer.fft_size, round(source.sample_rate_hz / self._frame_rate))
                samples = await source.read(block)
                if samples is None or source is not self.source:
                    # Waiting, so check that clients still listen, and that
                    # signals no longer found are no longer shown as active;
                    # SAM holds no carrier while nothing comes.
                    self._follow_sam_lock(False)
                    self._check_peaks()
                    self._check_carrier()
                    continue        # or switched away from
                # The centre asked tells the setting the samples come from;
                # the one in use labels them.
                capture = (source.centre_hz, source.sample_rate_hz)
                label = (source.exact_centre_hz, source.sample_rate_hz)
                if (capture, label) != self._capture:
                    self._capture = (capture, label)
                    if capture != self._vfo_set_in:
                        self._keep_vfo_in(*label)
                    self.watch_bookmarks()      # in case the source retuned by itself, as when it connects
                audio = self._audio is not None or any(subscription.audio for subscription in self.clients)
                threshold_db = self._threshold_db if self._detection else None
                # The memory scan's channels are those of the span asked of the
                # receiver: none while the samples still come from another.
                channels = self._watched if self._memory is not None and capture == self._span() else None
                sweep = self._sweep
                measure = self._carrier_measure(capture, label)
                fine = self._fine_measure(label, source.rate_scale)
                power, codes, meter, pcm, detections, levels, carrier, fine_codes = await asyncio.to_thread(
                    _process_block, self.analyzer, self._demodulator, self._detector, self._watch, samples,
                    *label, self._dc_removal, self._demod, audio, threshold_db,
                    DETECTION_BLOCKS if sweep is None else sweep.search_blocks,
                    None if channels is None else self._bands, measure, source.rate_scale, fine,
                    self._engine.plan.wfm_deemphasis_s)
                self._publish(label, power, codes, meter, pcm)
                if fine is not None and fine_codes:
                    self._publish_fine(fine, fine_codes)
                self._follow_sam_lock(bool(meter.sam_locked))
                self._follow_notch(meter.notch_lines or 0)
                self._follow_blanker(meter.blanker_impulses or 0)
                if measure is not None and carrier is not None:
                    self._measured(measure, carrier, label[0])
                self._check_carrier()
                if self._audio is not None and pcm is not None and len(pcm):
                    self._record_audio(pcm)
                # Detection turned off, or another source, meanwhile: these signals are not news.
                if detections is not None and self._detection and source is self.source:
                    found = self._signals.update(detections, time.monotonic())
                    self._check_peaks()
                    if found and source.kind not in UNRECORDED_KINDS:
                        self._record(found)
                if self._memory is not None and source is self.source:
                    self._step_memory(channels, levels, meter.open, capture[0])
                if sweep is not None and sweep is self._sweep and source is self.source:
                    self._step_sweep(sweep, capture[0], meter.open, label)
        except Exception:
            log.exception("the engine stopped")
            self._engine.close()

    def state(self, remote: bool = False) -> dict[str, Any]:
        """The source state message (docs/design/protocol.md), with the
        radio's own setting; for a ``remote`` client, without the settings
        it may not change."""
        described = self.source.describe()
        settable = [*described["settable"], "dc_removal"]
        if remote:
            settable = [name for name in settable if name not in LOCAL_SETTINGS]
        return {"type": "source", **described, "dc_removal": self._dc_removal, "settable": settable,
                "kinds": list(self._engine.sources.kinds) if self._engine.sources is not None else []}

    def configure(self, changes: Mapping[str, object]) -> None:
        """Change settings: ``kind`` switches to another kind of source, and
        comes alone; ``dc_removal`` is the radio's own; the others go to the
        source. Raises ValueError, changing nothing, when one is refused.
        Every client then receives the new state. A new centre frequency or
        sample rate stops a scan, before the receiver's samples show it; a
        new sample rate or address stops an I/Q recording. Another converter
        in front of the receiver does all of these, which its change of
        frequencies calls for, and the VFO and the signals found follow it:
        the VFO stays on the signal it was on, and the signals found
        through the previous converter are forgotten. A check of the
        driver's computation that runs stops first, bringing the receiver
        back, so that the change applies to the receiver as it was; so does
        a calibration that runs, unless the changes are all among
        CALIBRATION_KEEPS."""
        self._end_driver_check(STOPPED)
        if not set(changes) <= set(CALIBRATION_KEEPS):
            self._end_calibration("source" if "kind" in changes else "tuned")
        self._configure(changes, restoring=False)

    def _configure(self, changes: Mapping[str, object], *, restoring: bool) -> None:
        """``configure``; when ``restoring`` the settings saved for a
        receiver, the VFO, whose own saved value comes next, and the signals
        found do not follow a converter."""
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
            before = self.source.describe()
            span, address, conversion = self._span(), before["address"], _conversion(before)
            self.source.configure(source_changes)
            converted = _conversion(self.source.describe()).key != conversion.key
            if self._span() != span or converted:
                self._stop_scan("tuned")
            # An I/Q recording keeps one sample rate, from one receiver,
            # behind one converter, which its metadata describes.
            if self._span()[1] != span[1]:
                self._stop_recording("iq", "sample_rate")
            elif self.source.describe()["address"] != address:
                self._stop_recording("iq", "receiver")
            elif converted:
                self._stop_recording("iq", "converter")
            if converted and not restoring:
                self._follow_conversion(conversion, _conversion(self.source.describe()))
        self._dc_removal = dc_removal
        self._source_changed()

    def demod_state(self, acknowledged: int = 0) -> dict[str, Any]:
        """The demodulation state message (docs/design/protocol.md) for a
        client whose changes up to ``acknowledged`` have been handled."""
        return {"type": "demod", **self._demod.describe(),
                "sam_locked": self._sam_locked if self._demod.mode == "SAM" else None,
                "notch_lines": self._notch_lines if self._demod.notching else None,
                "blanker_impulses": self._blanker_impulses if self._demod.blanker else None,
                "modes": list(MODES), "audio_modes": list(AUDIO_MODES), "acknowledged": acknowledged}

    def configure_demod(self, changes: Mapping[str, object], subscription: Subscription | None = None,
                        change_id: int | None = None) -> None:
        """Change demodulation settings. Raises ValueError, changing nothing,
        when one is refused; otherwise every client receives the new
        settings. ``change_id`` acknowledges the change to ``subscription``,
        the client that asked for it, which receives the settings in either
        case. Tuning the VFO stops a scan; tuning it, or another mode or
        filter, stops a check of the driver's computation, whose carrier it
        moves, and a calibration that runs, whose VFO comes back first: the
        changes then apply to the VFO as it was."""
        if subscription is not None and change_id is not None:
            subscription.acknowledged = change_id
            subscription.changed("demod")
        demod = self._demod.changed(changes)
        if (demod.frequency_hz, demod.mode, demod.bandwidth_hz) != (
                self._demod.frequency_hz, self._demod.mode, self._demod.bandwidth_hz):
            self._end_driver_check(STOPPED)
            if self._running_calibration() is not None:
                self._end_calibration("tuned")
                demod = self._demod.changed(changes)
        if demod.frequency_hz != self._demod.frequency_hz:
            self._stop_scan("tuned")
            self._vfo_set_in = self._span()
        self._demod = demod
        self._demod_changed()

    def set_audio(self, subscription: Subscription, on: bool) -> None:
        """Start or stop sending audio to one client."""
        subscription.set_audio(on)

    def set_fine(self, subscription: Subscription, on: bool) -> None:
        """Start or stop sending the fine spectrum's frames to one client:
        the radio computes it only while at least one client asks."""
        subscription.set_fine(on)
        self._follow_fine()

    def scanner_state(self) -> dict[str, Any]:
        """The scanner state message (docs/design/protocol.md)."""
        memory, tour, sweep, signals = self._memory, self._tour, self._sweep, self._listening()
        return {"type": "scanner", "detection": self._detection, "threshold_db": self._threshold_db,
                "ignored_hz": self._signals.ignored, "resume_s": self._resume_s, "listen_s": self._listen_s,
                "sweep_low_hz": self._sweep_low_hz, "sweep_high_hz": self._sweep_high_hz,
                "sweep_window_s": self._sweep_window_s, "sweep_listen": self._sweep_listen,
                "sweep_passes": self._sweep_passes, "scan": self._running_scan(),
                "stopped": None if self._stopped is None else self._stopped[1],
                "stopped_scan": None if self._stopped is None else self._stopped[0],
                "memory": {"watched": list(self._memory_ids), "windows": len(self._windows),
                           "window": None if tour is None else tour.window,
                           "state": None if memory is None else memory.state,
                           "bookmark_id": None if memory is None else memory.bookmark_id},
                "sweep": {"windows": None if sweep is None else sweep.windows.count,
                          "window": None if sweep is None else sweep.window,
                          "passes": None if sweep is None else sweep.passes,
                          "passes_set": None if sweep is None else sweep.passes_set},
                "signals": {"state": None if signals is None else signals.state,
                            "frequency_hz": None if signals is None else signals.frequency_hz}}

    def peaks_state(self) -> dict[str, Any]:
        """The peaks message: the signals found, as they stand now, each
        with the channel it shows on."""
        peaks = self._signals.describe(time.monotonic())
        for peak in peaks:
            peak["channel_hz"] = self._engine.plan.channel(peak["frequency_hz"], peak["width_hz"])
        return {"type": "peaks", "peaks": peaks}

    def configure_scanner(self, changes: Mapping[str, object]) -> None:
        """Change detection's settings, ``detection`` (true or false) and
        ``threshold_db``, the memory scan's and the range scan's
        ``resume_s``, the range scan's ``listen_s``, and its range,
        ``sweep_low_hz`` to
        ``sweep_high_hz``, time per window, ``sweep_window_s``, listening,
        ``sweep_listen``, and passes, ``sweep_passes``; or, each alone in its
        message, ``ignore`` a frequency, ``restore`` (true) every ignored
        one, or start a scan (``scan``: "memory", "sweep", the range scan,
        or "signals", the range scan over the current window, stopping to
        listen) or stop the one that runs (``scan``: None). A new range
        starts a running range scan again from its first window, and its
        listening and passes, when given, apply at once, a pass that ends
        from then on counting against the passes set; detection turned off
        stops a range scan that listens, and an ignored frequency takes it
        away from the signal there. Raises
        ValueError, changing nothing, when one is refused; otherwise every
        client receives the scanner state, and the signals found once they
        changed."""
        if not changes:
            raise ValueError("a scanner message needs a setting")
        for name in changes:
            if name not in SCANNER_SETTINGS:
                raise ValueError(f"unknown scanner setting: {str(name)[:40]}")
        if (any(name in changes for name in ("ignore", "restore", "scan")) and len(changes) > 1
                and set(changes) != {"scan", "name"}):
            raise ValueError("ignore, restore and scan come alone in their message")
        if "name" in changes and changes.get("scan") not in ("sweep", "signals"):
            raise ValueError("a name goes with the range scan's start alone")
        if "ignore" in changes:
            ignored = checked_frequency(changes["ignore"], "an ignored frequency")
            self._signals.ignore(ignored)
            scan = self._listening()
            if scan is not None and scan.frequency_hz is not None and same_signal(*self._signal_tuned, ignored, 0.0):
                scan.leave()
        elif "restore" in changes:
            if changes["restore"] is not True:
                raise ValueError("restore takes true")
            self._signals.restore()
        elif "scan" in changes:
            self._kept_name = checked_name(changes.get("name"))
            try:
                self._start_scan(changes["scan"])
            finally:
                self._kept_name = None
        else:
            detection = changes.get("detection", self._detection)
            if not isinstance(detection, bool):
                raise ValueError("detection must be turned on or off: true or false")
            threshold_db = (checked_threshold(changes["threshold_db"]) if "threshold_db" in changes
                            else self._threshold_db)
            resume_s = checked_resume(changes["resume_s"]) if "resume_s" in changes else self._resume_s
            listen_s = checked_listen(changes["listen_s"]) if "listen_s" in changes else self._listen_s
            low_hz, high_hz = self._sweep_low_hz, self._sweep_high_hz
            if "sweep_low_hz" in changes or "sweep_high_hz" in changes:
                low_hz, high_hz = checked_sweep_range(changes.get("sweep_low_hz", low_hz),
                                                      changes.get("sweep_high_hz", high_hz))
            window_s = (checked_window(changes["sweep_window_s"]) if "sweep_window_s" in changes
                        else self._sweep_window_s)
            listen = changes.get("sweep_listen", self._sweep_listen)
            if not isinstance(listen, bool):
                raise ValueError("sweep_listen must be turned on or off: true or false")
            passes = checked_passes(changes["sweep_passes"]) if "sweep_passes" in changes else self._sweep_passes
            sweep = self._sweep
            if sweep is not None and changes.get("sweep_listen") is True and not detection:
                raise ValueError(LISTEN_NEEDS_DETECTION)
            moved = sweep is not None and (low_hz, high_hz) != (self._sweep_low_hz, self._sweep_high_hz)
            windows = self._sweep_windows(low_hz, high_hz) if moved else None
            self._detection, self._threshold_db, self._resume_s = detection, threshold_db, resume_s
            self._listen_s = listen_s
            self._sweep_low_hz, self._sweep_high_hz, self._sweep_window_s = low_hz, high_hz, window_s
            self._sweep_listen, self._sweep_passes = listen, passes
            if sweep is not None:
                # Given alone: a range scan over the current window listens whatever the saved switch says.
                if "sweep_listen" in changes:
                    sweep.listen = listen
                if "sweep_passes" in changes:
                    sweep.passes_set = passes
            if not detection:
                self._signals.clear()
                if sweep is not None and sweep.listen:
                    self._stop_scan("detection")
            if sweep is not None and sweep is self._sweep:
                sweep.search_blocks = search_blocks(window_s, self._frame_rate)
                if windows is not None:
                    if self._sweep_here:    # from now on, the range scan of the saved settings
                        sweep.listen, sweep.passes_set = listen, passes
                        self._sweep_here = False
                    sweep.restart(windows, time.monotonic())
                    if sweep.centre_hz != self._span()[0]:      # a range within the span stays there
                        self._tune_scan(sweep.centre_hz)
        self._scanner_changed()
        self._engine.settings_changed()
        if self._signals.changed(time.monotonic()):
            self._peaks_changed(at_once=True)

    def start_given_scan(self, scan: str) -> None:
        """Start ``scan``, "memory", "sweep" or "signals", given at start
        (cli's --scan), as an interface would. Raises ValueError, saying why,
        for another scan, and when it cannot start: unless another scan
        runs, which goes on, the scanner state then tells the clients,
        ``start`` being why it stopped."""
        if scan not in SCANS:
            raise ValueError(f'a scan is "memory", "sweep" or "signals", not {str(scan)[:40]!r}')
        try:
            self.configure_scanner({"scan": scan})
        except ValueError:
            if not self._scanning():
                self._stopped = ("sweep" if scan == "signals" else scan, "start")     # the range scan
                self._scanner_changed()
            raise

    def power_antenna(self) -> None:
        """Turn the antenna power on for the receiver in use at start (cli's
        --bias-tee). Raises ValueError, saying why, for a source without it.
        A SoapySDR device not yet opened takes it, and turns it on once it
        opens if it has antenna power, or says that it ignores it."""
        if "bias_tee" not in self.source.describe()["settable"]:
            raise ValueError(f"the {self.source.kind} source has no antenna power")
        self.configure({"bias_tee": True})

    def devices_state(self, remote: bool = False) -> dict[str, Any]:
        """The SoapySDR devices message (docs/design/protocol.md): the
        search's state, the devices it found and the device in use; for a
        ``remote`` client, without the device arguments in use."""
        search = self._engine.device_search
        if search is None:
            raise RuntimeError("this engine opens no SoapySDR device")
        chosen = self._engine.reopen.get("soapy", {}).get("args")
        return {"type": "devices", "search": search.status, "detail": search.detail, "drivers": list(search.drivers),
                "devices": search.describe(self.soapy_in_use()), "chosen": isinstance(chosen, str),
                "args": None if remote or not isinstance(self.source, SoapySource) else self.source.args}

    def configure_devices(self, changes: Mapping[str, object]) -> None:
        """Search for SoapySDR devices (``search``: true), unless a search
        runs, or open one: among those found, by its driver and serial
        number (``choose``), or by its device arguments (``args``), one at a
        time. Opening one switches to it as to another kind of source, and is
        refused while a search runs, which could disturb it. Raises
        ValueError, changing nothing, when refused. Only this computer's
        interfaces may search or give arguments (server.py)."""
        search = self._engine.device_search
        if search is None:
            raise ValueError("this Radiolack does not open SoapySDR devices")
        if len(changes) != 1:
            raise ValueError("a devices message asks for one thing: search, choose or args")
        name, value = next(iter(changes.items()))
        if name == "search":
            if value is not True:
                raise ValueError("search must be true")
            self._engine.start_search()
        elif name == "choose":
            if not (isinstance(value, dict) and set(value) == {"driver", "serial"}
                    and all(isinstance(text, str) for text in value.values())):
                raise ValueError("choose names a device found, by its driver and serial")
            in_use = self.soapy_in_use()
            if in_use is not None and (in_use.driver, in_use.serial) == (value["driver"], value["serial"]):
                return      # in use already
            found = search.find(value["driver"], value["serial"])
            if found is None:
                raise ValueError("this device is not among those found: search again")
            self._open_soapy(found.args)
        elif name == "args":
            if not isinstance(value, str):
                raise ValueError("the device arguments must be text")
            self._open_soapy(value)
        else:
            raise ValueError(f"unknown devices field: {str(name)[:40]}")

    def rtl_devices_state(self) -> dict[str, Any]:
        """The RTL-SDR receivers message (docs/design/protocol.md), read
        from sysfs now: the receivers plugged in, none opened, and the one
        that the source in use reads, listed even when not found; each
        with the radio that reads it, this one or another."""
        listing = self._engine.rtl_listing
        if listing is None:
            raise RuntimeError("this engine lists no RTL-SDR receivers")
        listed = listing()

        def reads(radio: Radio, device: RtlDevice) -> bool:
            in_use = radio.source.reopen.get("device") if radio.source.kind == "rtl_sdr" else None
            if not isinstance(in_use, str):
                return False
            # Opened by its index, the first, and alone plugged in: that one.
            alone = (listed is not None and len(listed) == 1 and bool(listed[0].serial)
                     and in_use.isdigit() and int(in_use) == 0)
            return alone or device.serial == in_use

        devices = []
        for device in listed or ():
            reader = next((radio for radio in self._engine.radios if reads(radio, device)), None)
            devices.append({"device": device.serial or None, "name": device.name,
                            "manufacturer": device.manufacturer, "drivers": list(device.drivers),
                            "shared": device.shared, "reads_as_index": device.reads_as_index,
                            "in_use": reader is self, "radio": None if reader is None else reader.id,
                            "found": True})
        in_use = self.source.reopen.get("device") if self.source.kind == "rtl_sdr" else None
        if isinstance(in_use, str) and not any(device["in_use"] for device in devices):
            devices.append({"device": in_use, "name": "", "manufacturer": "", "drivers": [], "shared": False,
                            "reads_as_index": reads_as_index(in_use, len(listed or ())),
                            "in_use": True, "radio": self.id, "found": False})
        return {"type": "rtl_devices", "listing": "unavailable" if listed is None else "sysfs", "devices": devices}

    def configure_rtl_devices(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """List the RTL-SDR receivers plugged in (``list``: true), or read
        one of them by its serial number (``choose``) in place of the source
        in use, as a switch to another kind of source does; then reply with
        the list. Raises ValueError, changing nothing, when refused: a
        serial number that no receiver plugged in has, or that rtl_tcp
        would not read as this receiver's alone (rtldevices.py). The first time a
        receiver is chosen by its serial number, alone plugged in, it starts
        from the settings saved for the first receiver, opened by its index,
        if any: they were its own."""
        if self._engine.rtl_listing is None:
            raise ValueError("this Radiolack does not list RTL-SDR receivers")
        if len(changes) != 1:
            raise ValueError("an rtl_devices message asks for one thing: list or choose")
        name, value = next(iter(changes.items()))
        if name == "list":
            if value is not True:
                raise ValueError("list must be true")
        elif name == "choose":
            if not isinstance(value, str) or not value:
                raise ValueError("choose names a receiver plugged in, by its serial number")
            self._choose_rtl_device(value)
        else:
            raise ValueError(f"unknown rtl_devices field: {str(name)[:40]}")
        subscription.reply(self.rtl_devices_state())

    def _choose_rtl_device(self, serial: str) -> None:
        sources, listing = self._engine.sources, self._engine.rtl_listing
        listed = listing() if listing is not None else None
        found = [device for device in listed or () if device.serial == serial]
        if sources is None or not found:
            raise ValueError("no RTL-SDR receiver plugged in has this serial number: plug it in, or choose another")
        if not found[0].choosable:
            raise ValueError("rtl_tcp cannot tell this receiver by its serial number: give it its own with rtl_eeprom -s")
        if self.source.kind == "rtl_sdr" and any(
                device["in_use"] and device["device"] == serial for device in self.rtl_devices_state()["devices"]):
            return      # in use already
        new = sources.open({"kind": "rtl_sdr", "device": serial})
        try:
            self._engine.check_free(new, self)
        except ValueError:
            new.close()         # it never read: nothing to release
            raise
        receivers = self._engine.receivers
        if new.settings_key not in receivers and listed is not None and len(listed) == 1:
            first = sources.open({"kind": "rtl_sdr", "device": "0"})
            first.close()       # it never read: nothing to release
            saved = receivers.get(first.settings_key)
            if saved is not None:
                receivers[new.settings_key] = copy.deepcopy(saved)
        self._end_driver_check(STOPPED)
        self._end_calibration("source")
        self._replace(new)
        self._source_changed()

    def recording_state(self, remote: bool = False) -> dict[str, Any]:
        """The recording state message (docs/design/protocol.md): the audio
        and I/Q recordings that run, if any, and why the latest stopped by
        itself; unless for a ``remote`` client, the recordings folder too."""
        audio = iq = None
        if (recording := self._audio) is not None:
            rate, samples = self._audio_rate, self._audio_samples
            audio = {"file": recording.file, "started": round(recording.started),
                     "frequency_hz": recording.frequency_hz, "mode": recording.mode,
                     "rate_hz": None if rate is None else round(rate),
                     "seconds": round(samples / rate, 1) if rate else 0.0,
                     "bytes": WAV_HEADER.size + 2 * samples if recording.file is not None else 0}
        if (raw := self._iq) is not None:
            pair_bytes = raw.pair_bytes
            iq = {"file": raw.file, "started": round(raw.started), "frequency_hz": raw.frequency_hz,
                  "rate_hz": round(raw.rate_hz),
                  "seconds": round(self._iq_bytes / pair_bytes / raw.rate_hz, 1) if pair_bytes else 0.0,
                  "bytes": self._iq_bytes if raw.file is not None else 0}
        state: dict[str, Any] = {"type": "recording", "audio": audio, "iq": iq,
                                 "stopped": None if self._recording_stopped is None else dict(self._recording_stopped)}
        if not remote:
            folder, default = self._engine.recordings_folder(), self._engine.default_recordings
            state["folder"] = None if folder is None else shown_path(folder)
            state["default_folder"] = None if default is None else shown_path(default)
        return state

    def configure_recording(self, changes: Mapping[str, object], subscription: Subscription | None = None) -> None:
        """Start (``audio``: true) or stop (false) recording the audio, and
        the same with ``iq`` for the receiver's raw I/Q; or choose the
        recordings ``folder``, a full path, or null for the default, which
        this computer's interfaces alone may change: once checked, as the
        disk may be slow, it applies, or ``subscription`` learns why not.
        Each comes alone in its message. Raises ValueError, changing
        nothing, when refused."""
        if len(changes) != 1:
            raise ValueError("a recording message changes one setting at a time")
        [(name, value)] = changes.items()
        if name in ("audio", "iq"):
            if not isinstance(value, bool):
                raise ValueError(f"the {'audio' if name == 'audio' else 'I/Q'} is recorded or not: true or false")
            if not value:
                self._stop_recording(name, None)
            elif name == "audio":
                self._start_audio()
            else:
                self._start_iq()
        elif name == "folder":
            self._engine.choose_recordings_folder(value, subscription)
        else:
            raise ValueError(f"unknown recording setting: {str(name)[:40]}")

    def report(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Send ``subscription`` a problem report, with what the interface
        tells of itself, ``page``, a line of text, if given. Raises
        ValueError when refused."""
        for name in changes:
            if name != "page":
                raise ValueError(f"unknown report field: {str(name)[:40]}")
        made = Report.now(receiver_line(self.source.describe()), check_page(changes.get("page", "")))
        subscription.reply({"type": "report", "text": made.text(), "issue_url": made.issue_url()})

    def measure_carrier(self, changes: Mapping[str, object], subscription: Subscription) -> None:
        """Measure the carrier at the VFO for ``subscription``, which then
        receives the result alone: ``action`` must be ``measure``, or
        ``identify`` for a check of the driver's computation on that
        carrier (``identify_driver``), and ``id``, if given, a whole number
        that the result repeats. A client that asks again while a measure
        runs waits for that one, with its new id; a measure asked while the
        check runs waits for the check's end. Raises ValueError when
        refused."""
        for name in changes:
            if name not in ("action", "id"):
                raise ValueError(f"unknown carrier field: {str(name)[:40]}")
        action = changes.get("action")
        if action not in ("measure", "identify"):
            raise ValueError("a carrier message takes the action measure or identify")
        request_id = changes.get("id")
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, int)
                                       or not 0 < request_id <= MAX_REQUEST_ID):
            raise ValueError("a request id must be a whole number from 1")
        if action == "identify":
            self.identify_driver(subscription, request_id)
            return
        self._carrier_waiting[subscription] = request_id
        if self._carrier is None:
            self._carrier = CarrierMeasure()
            self._carrier_began = time.monotonic()

    def identify_driver(self, subscription: Subscription, request_id: int | None) -> None:
        """Check which of the driver's computations an R820T's or R828D's
        PLL follows, on the carrier at the VFO (pllcheck.py), for
        ``subscription``, which then receives the result alone, with
        ``request_id``; a client that asks while a check runs waits for that
        one. The receiver's tuner moves off its grid, to centre frequencies
        within MAX_MOVE_HZ, for every interface, then comes back, whatever
        ends the check. A computation found is saved with the receiver,
        replacing the one set. Refused, with the reason in the answer and
        nothing moved, with another receiver, in direct sampling, below
        where the PLL is computed, while a scan, a calibration or an I/Q
        recording runs, with a VFO whose channel receives nothing at the sample rate,
        or too near the centre or the span's edge for the moves, and where
        the computations cannot be told apart."""
        check = self._driver_check
        if check is not None:
            check.waiting[subscription] = request_id
            return
        described = self.source.describe()
        tuning = self.source.pll_tuning()
        points: tuple[Point, ...] = ()
        if self._running_calibration() is not None:
            reason: str | None = "calibration"     # whose receiver is not where it comes back
        elif isinstance(tuning, str):
            reason = tuning
        elif self._scanning():
            reason = "scan"
        elif self._iq is not None:
            reason = "iq"
        elif not has_channel(self._demod.mode, tuning[1]):
            reason = LOST       # the VFO's channel receives nothing at this rate: no carrier to measure
        else:
            request_hz, rate_hz = tuning
            distance = abs(self._demod.frequency_hz - described["centre_in_use_hz"])
            if (distance < MAX_MOVE_HZ + CHECK_CLEARANCE_HZ
                    or distance + MAX_MOVE_HZ + self._demod.bandwidth_hz > rate_hz / 2):
                reason = "position"
            else:
                points = choose_points(request_hz, rate_hz, self.source.centre_off_grid)
                reason = None if points else "indistinct"
        if reason is not None:
            subscription.reply(_driver_answer(request_id, None, described["driver_pll"], reason))
            return
        centre_hz = described["centre_hz"]
        self._driver_check = _DriverCheck(
            {subscription: request_id}, rate_hz, (Point(centre_hz, request_hz), *points),
            (centre_hz, described["tuner_grid"]), described["driver_pll"],
            (centre_hz, described["centre_in_use_hz"]), time.monotonic())
        log.info("checking the driver's computation at %d Hz, then off the grid at %s Hz", request_hz,
                 ", ".join(str(point.request_hz) for point in points))

    def calibration_state(self) -> dict[str, Any]:
        """The calibration state message (docs/design/protocol.md): the
        calibration that runs, or the latest, with its stations, what it
        measured at each so far and, once it ended, why, and the correction
        it proposes, or why none."""
        run = self._calibration
        if run is None:
            return {"type": "calibration", "running": False, "stations": [], "results": [], "proposal": None,
                    "reason": None, "ended": None}
        proposal = run.proposal
        agreeing = () if proposal is None else proposal.agreeing
        results = [{"frequency_hz": result.frequency_hz,
                    "carrier_hz": None if result.carrier_hz is None else round(result.carrier_hz, 1),
                    "margin_db": result.margin_db,
                    "ppm": None if result.ppm is None else round(result.ppm, 2) + 0.0,     # no -0.0
                    "agrees": index in agreeing, "reason": result.reason}
                   for index, result in enumerate(run.results)]
        return {"type": "calibration", "running": run.ended is None,
                "stations": [station.frequency_hz for station in run.stations], "results": results,
                "proposal": (None if proposal is None or proposal.ppm is None
                             else {"ppm": proposal.ppm, "stations": len(agreeing)}),
                "reason": None if proposal is None else proposal.reason, "ended": run.ended}

    def calibrate(self, changes: Mapping[str, object]) -> None:
        """Start the automatic calibration, ``action`` ``start``, unless one
        runs, or stop the one that runs, ``stop``, if one does: for every
        client, which learn its progress, its results and its proposal.
        Starting stops a scan and a check of the driver's computation, which
        bring the receiver back first. Raises ValueError, changing nothing,
        on a source without a frequency correction or that does not tune,
        behind a converter, whose oscillator would pass for the crystal's
        error, while a recording runs, and when no standard-frequency
        station of the band plan lies within the receiver's reach."""
        for name in changes:
            if name != "action":
                raise ValueError(f"unknown calibration field: {str(name)[:40]}")
        action = changes.get("action")
        if action == "stop":
            self._end_calibration("stopped")
        elif action == "start":
            if self._running_calibration() is None:
                self._start_calibration()
        else:
            raise ValueError("a calibration message takes the action start or stop")

    def _source_changed(self) -> None:
        if self.source.settings_key != self._settings_key:
            self._identified()
        self._source_changes += 1       # a carrier's measure starts again
        for subscription in self.clients:
            subscription.changed("source")
        self._engine.radios_changed()
        self._engine.settings_changed()
        self.watch_bookmarks()
        self.follow_devices()          # the device in use, or the device chosen, may change

    def _demod_changed(self) -> None:
        for subscription in self.clients:
            subscription.changed("demod")
        self._engine.settings_changed()

    def _follow_sam_lock(self, locked: bool) -> None:
        """SAM gained or lost its carrier: in SAM, every client learns it
        with the demodulation state, which saves nothing."""
        if locked == self._sam_locked:
            return
        self._sam_locked = locked
        if self._demod.mode == "SAM":
            for subscription in self.clients:
                subscription.changed("demod")

    def _follow_notch(self, lines: int) -> None:
        """The notch removes another number of lines: while it acts, every
        client learns it with the demodulation state, which saves nothing."""
        if lines == self._notch_lines:
            return
        self._notch_lines = lines
        if self._demod.notching:
            for subscription in self.clients:
                subscription.changed("demod")

    def _follow_blanker(self, impulses: int) -> None:
        """The blanker zeroed another number of impulses in the latest
        second, which changes at most once a second: while it is on, every
        client learns it with the demodulation state, which saves nothing."""
        if impulses == self._blanker_impulses:
            return
        self._blanker_impulses = impulses
        if self._demod.blanker:
            for subscription in self.clients:
                subscription.changed("demod")

    def _follow_fine(self) -> None:
        """The fine spectrum runs while a client asks for it: the first
        starts it, and once none asks, its partial mean goes."""
        if not any(subscription.fine for subscription in self.clients):
            self._fine = None
        elif self._fine is None:
            self._fine = FineSpectrum()

    def _fine_setting(self) -> tuple[Any, ...]:
        """What a fine frame must not mix: the source's state, through the
        count of its changes, which a switch of source counts too, the
        VFO, the mode and DC removal."""
        demod = self._demod
        return self._source_changes, demod.frequency_hz, demod.mode, self._dc_removal

    def _fine_measure(self, label: tuple[float, float], rate_scale: float
                      ) -> tuple[FineSpectrum, tuple[Any, ...], float] | None:
        """What a block gives the fine spectrum while a client asks for it:
        the spectrum, the setting of the block's samples, which starts it
        again when it changes, and ``rate_scale``, by which the receiver
        samples faster than their rate. The setting holds their centre in
        use and rate, ``label``, and ``rate_scale`` too. None while no
        client asks."""
        if self._fine is None:
            return None
        return self._fine, (self._fine_setting(), label, rate_scale), rate_scale

    def _publish_fine(self, fine: tuple[FineSpectrum, tuple[Any, ...], float], frames: Sequence[np.ndarray]) -> None:
        """Send the fine frames, as codes, that a block completed to the
        clients that ask, unless the setting changed while the block was
        processed, or none asks any more: each frame's centre is the
        frequency on the air of the channel's 0 Hz, the VFO less the CW
        pitch in CW, and its width the channel's rate, both scaled by the
        block's ``rate_scale``, as the receiver samples that much faster.
        Remote clients get frames of at most REMOTE_FINE_BINS bins, each
        the highest of those it groups; every client's frames take the same
        sequence."""
        spectrum, key, rate_scale = fine
        if spectrum is not self._fine or key[0] != self._fine_setting():
            return
        _, frequency_hz, mode, _ = key[0]
        centre_hz = frequency_hz - vfo_position_hz(mode) * rate_scale
        width_hz = spectrum.rate_hz * rate_scale
        db_min, db_step = self.analyzer.db_min, self.analyzer.db_step
        remote = any(subscription.fine and subscription.remote for subscription in self.clients)
        for codes in frames:
            sequence, self._fine_sequence = self._fine_sequence, (self._fine_sequence + 1) & 0xFFFFFFFF
            frame = encode_fine(sequence, centre_hz, width_hz, db_min, db_step, codes)
            remote_frame = (encode_fine(sequence, centre_hz, width_hz, db_min, db_step,
                                        fewer_bins(codes, REMOTE_FINE_BINS)) if remote else frame)
            for subscription in self.clients:
                subscription.put_fine(remote_frame if subscription.remote else frame)

    def _carrier_measure(self, capture: tuple[float, float], label: tuple[float, float]
                         ) -> tuple[CarrierMeasure, tuple[Any, ...], float, tuple[float, float]] | None:
        """What a block measures of the carrier, while an interface waits for
        it, from the centre asked and the rate of its samples, ``capture``,
        and their centre in use, ``label``: the measure, the setting its
        samples come from, which starts it again when it changes, the
        frequency on the air of the channel's 0 Hz, and where the measure
        looks, from that 0 Hz: within the VFO's filter. A VFO whose channel
        receives nothing, outside the span or in a mode that the sample rate
        cannot hold, answers at once: there is nothing to measure. While a
        calibration or a check of the driver's computation runs, it alone
        measures, from the samples of the station or the point it waits
        for: a calibration within the station's window."""
        demod = self._demod
        origin_hz = demod.frequency_hz - vfo_position_hz(demod.mode)
        run = self._running_calibration()
        if run is not None:
            if (capture[0], label[0]) != run.expected:
                return None         # the receiver still settles on the station
            window = calibration.window_hz(run.stations[len(run.results)].frequency_hz)
            key = (len(run.results), capture, label, self._dc_removal)
            return run.gatherer, key, origin_hz, (-window, window)
        check = self._driver_check
        if check is not None:
            if (capture[0], label[0]) != check.expected:
                return None         # the receiver still settles on the point
            key = (len(check.offsets), capture, label, self._dc_removal)
            return check.gatherer, key, origin_hz, _filter_band(demod)
        if self._carrier is None:
            return None
        centre_hz, rate_hz = label
        if not has_channel(demod.mode, rate_hz) or not abs(demod.frequency_hz - centre_hz) <= rate_hz / 2:
            self._answer_carrier(Carrier(None, None, SILENT), 0.0)
            return None
        key = (self._source_changes, demod.frequency_hz, demod.mode, demod.bandwidth_hz, label, self._dc_removal)
        return self._carrier, key, origin_hz, _filter_band(demod)

    def _measured(self, measure: tuple[CarrierMeasure, tuple[Any, ...], float, tuple[float, float]],
                  carrier: Carrier, label_hz: float) -> None:
        """A measure ended with ``carrier``, from samples labelled
        ``label_hz``: for the station of the calibration that waited for it,
        the point of the driver's check that waited for it, or the
        interfaces waiting for a carrier's measure, unless it no longer
        counts."""
        run = self._running_calibration()
        if run is not None:
            if measure[0] is run.gatherer:
                self._station_measured(run, carrier, measure[2])
            return
        check = self._driver_check
        if check is not None:
            if measure[0] is check.gatherer:
                measured_hz = None if carrier.frequency_hz is None else measure[2] + carrier.frequency_hz
                self._driver_point(check, measured_hz, label_hz)
        elif measure[0] is self._carrier:
            self._answer_carrier(carrier, measure[2])

    def _check_carrier(self) -> None:
        """End a carrier's measure that settings kept starting again, or for
        which no samples came, once it ran for CARRIER_TIMEOUT_S, a point of
        the driver's check once it ran for CHECK_POINT_TIMEOUT_S, and a
        calibration's station once it ran for STATION_TIMEOUT_S: the
        measures asked meanwhile wait for the check's or the calibration's
        end."""
        now = time.monotonic()
        run = self._running_calibration()
        if run is not None:
            if now - run.began >= STATION_TIMEOUT_S:
                self._station_measured(run, Carrier(None, None, UNSTABLE if run.gatherer.received else SILENT), 0.0)
            return
        check = self._driver_check
        if check is not None:
            if now - check.began >= CHECK_POINT_TIMEOUT_S:
                self._end_driver_check(LOST)
        elif self._carrier is not None and now - self._carrier_began >= CARRIER_TIMEOUT_S:
            self._answer_carrier(Carrier(None, None, UNSTABLE if self._carrier.received else SILENT), 0.0)

    def _driver_point(self, check: _DriverCheck, measured_hz: float | None, label_hz: float) -> None:
        """The carrier measured at ``measured_hz`` on the air, None when it
        was not, at the point of the driver's check that ``check`` waited
        for, from samples labelled ``label_hz``: the receiver goes to the
        next point, or the check ends."""
        if measured_hz is None:
            self._end_driver_check(LOST)
            return
        conversion = _conversion(self.source.describe())
        check.offsets.append(conversion.tuned(measured_hz) - conversion.tuned(label_hz))
        if len(check.offsets) == len(check.points):
            self._end_driver_check(None)
            return
        point = check.points[len(check.offsets)]
        try:
            self.source.configure({"tuner_grid": False, "centre_hz": point.centre_hz})
        except ValueError as error:
            log.warning("the driver's check did not move the receiver to %.0f Hz: %s", point.centre_hz, error)
            self._end_driver_check(STOPPED)
            return
        check.expected = (point.centre_hz, self.source.describe()["centre_in_use_hz"])
        check.gatherer = CarrierMeasure()
        check.began = time.monotonic()

    def _end_driver_check(self, reason: str | None) -> None:
        """End the check of the driver's computation that runs, if one does:
        with every point measured when ``reason`` is None, otherwise for
        ``reason``. The receiver comes back to the centre and the grid of
        the check's start, and takes the computation found, unless already
        set; the interfaces waiting learn what was found, and the carrier's
        measures that waited start from the samples labelled as now."""
        check, self._driver_check = self._driver_check, None
        if check is None:
            return
        found = None
        if reason is None:
            identification = identify(check.rate_hz, check.points, check.offsets)
            found, reason = identification.computation, identification.reason
            log.info("the driver's check found %s; each move from its prediction: %s", found or reason,
                     "; ".join(f"{name} " + ", ".join(f"{value:+.1f} Hz" for value in residuals)
                               for name, residuals in identification.residuals_hz.items()))
        else:
            log.info("the driver's check ended without a result: %s", reason)
        described = self.source.describe()
        centre_hz, grid = check.home
        back: dict[str, object] = {}
        if described["tuner_grid"] != grid:
            back["tuner_grid"] = grid
        if described["centre_hz"] != centre_hz:
            back["centre_hz"] = centre_hz
        changes = dict(back)
        if found is not None and found != described["driver_pll"]:
            changes["driver_pll"] = found
        if changes:
            try:
                self.source.configure(changes)
            except ValueError as error:
                log.warning("the receiver did not come back to %.0f Hz after the driver's check: %s",
                            centre_hz, error)
                if "tuner_grid" in back:
                    try:
                        self.source.configure({"tuner_grid": grid})
                    except ValueError as again:
                        log.warning("the receiver did not come back to its grid either: %s", again)
        for subscription, request_id in check.waiting.items():
            subscription.reply(_driver_answer(request_id, found, check.previous, reason))
        if self._carrier is not None:
            self._carrier = CarrierMeasure()
            self._carrier_began = time.monotonic()

    def _answer_carrier(self, carrier: Carrier, origin_hz: float) -> None:
        """Send the measure's result to every interface waiting for it, each
        alone, with the frequency on the air, from ``origin_hz``, that of the
        channel's 0 Hz, to the tenth of a hertz; the next request starts
        another."""
        frequency = None if carrier.frequency_hz is None else round(origin_hz + carrier.frequency_hz, 1)
        for subscription, request_id in self._carrier_waiting.items():
            subscription.reply({"type": "carrier", "id": request_id, "frequency_hz": frequency,
                                "margin_db": carrier.margin_db, "reason": carrier.reason})
        self._carrier_waiting.clear()
        self._carrier = None

    def _running_calibration(self) -> _CalibrationRun | None:
        """The calibration that runs, if one does."""
        run = self._calibration
        return run if run is not None and run.ended is None else None

    def _calibration_changed(self) -> None:
        for subscription in self.clients:
            subscription.changed("calibration")

    def _start_calibration(self) -> None:
        """Start a calibration (``calibrate``), from the receiver and the VFO
        as a scan or a check of the driver's computation that runs leaves
        them once stopped."""
        described = self.source.describe()
        if described["ppm"] is None or "ppm" not in described["settable"]:
            raise ValueError("this receiver has no frequency correction to calibrate")
        if "centre_hz" not in described["settable"]:
            raise ValueError("calibrating needs a receiver that tunes, such as an RTL-SDR")
        if _conversion(described).active:
            raise ValueError("a converter is set in front of the receiver: its oscillator would pass for the "
                             "crystal's error")
        if self.recording_runs():
            raise ValueError("a recording runs: stop it before calibrating")
        low_hz, high_hz = described["range_hz"]
        stations = calibration.stations(self._engine.plan.references, described["sample_rate_hz"], (low_hz, high_hz),
                                        self.source.centre_in_use)
        if not stations:
            raise ValueError("no standard-frequency station of the band plan lies within the receiver's reach")
        self._end_driver_check(STOPPED)     # which brings the receiver back
        self._stop_scan(None)       # likewise, unless on a signal or a bookmark
        run = _CalibrationRun(stations, (self._span()[0], self._demod), described["ppm"])
        self._calibration = run
        log.info("calibrating on %s Hz", ", ".join(str(station.frequency_hz) for station in stations))
        self._wanted.set()          # it goes on without any client
        self._calibration_changed()
        self._next_station(run)

    def _next_station(self, run: _CalibrationRun) -> None:
        """Tune to the first station of ``run`` not yet measured, for every
        client: the receiver to its centre, the VFO onto it in
        calibration.MODE with a filter that holds its window; a station
        whose centre the receiver refuses gives no carrier. Once every
        station is measured, the calibration ends."""
        while len(run.results) < len(run.stations):
            station = run.stations[len(run.results)]
            if self._span()[0] != station.centre_hz:
                try:
                    self.source.configure({"centre_hz": station.centre_hz})
                except ValueError as error:
                    log.warning("the calibration skipped %d Hz: the receiver did not tune to %.0f Hz: %s",
                                station.frequency_hz, station.centre_hz, error)
                    run.results.append(calibration.Result(station.frequency_hz, None, None, None,
                                                          calibration.REFUSED))
                    self._calibration_changed()
                    continue
            self._demod = self._demod.changed({"frequency_hz": station.frequency_hz, "mode": calibration.MODE,
                                               "bandwidth_hz": calibration.filter_width_hz(station.frequency_hz)})
            self._demod_changed()
            run.expected = (station.centre_hz, self.source.describe()["centre_in_use_hz"])
            run.gatherer = CarrierMeasure()
            run.began = time.monotonic()
            return
        self._end_calibration("done")

    def _station_measured(self, run: _CalibrationRun, carrier: Carrier, origin_hz: float) -> None:
        """The measure of the station that ``run`` waits for ended with
        ``carrier``, whose frequency counts from ``origin_hz``, that of the
        channel's 0 Hz on the air: with the correction it implies; then the
        next station."""
        station = run.stations[len(run.results)]
        if carrier.frequency_hz is None:
            result = calibration.Result(station.frequency_hz, None, carrier.margin_db, None, carrier.reason)
        else:
            carrier_hz = origin_hz + carrier.frequency_hz
            result = calibration.Result(station.frequency_hz, carrier_hz, carrier.margin_db,
                                        calibration.implied_ppm(run.ppm, carrier_hz, station.frequency_hz))
        run.results.append(result)
        self._calibration_changed()
        self._next_station(run)

    def _end_calibration(self, reason: str, back: bool = True) -> None:
        """End the calibration that runs, if one does, for ``reason``:
        ``done``, every station measured; ``stopped``, a client stopped it;
        ``tuned``, a client changed the source's settings or the VFO's
        frequency, mode or filter; ``source``, another receiver; ``scan``,
        ``recording`` or ``closed``, the engine stopping. With ``done`` and
        ``stopped`` it proposes a correction from what it measured. With
        ``back``, the receiver and the VFO come back where it found them;
        the carrier's measures that waited start from the receiver as it is
        then, and every client learns how it ended."""
        run = self._running_calibration()
        if run is None:
            return
        run.ended = reason
        if reason in ("done", "stopped"):
            run.proposal = calibration.propose(run.results)
        if back:
            centre_hz, start = run.home
            if self._span()[0] != centre_hz:
                try:
                    self.source.configure({"centre_hz": centre_hz})
                except ValueError as error:
                    log.warning("the receiver did not come back to %.0f Hz after the calibration: %s", centre_hz,
                                error)
            vfo = dataclasses.replace(self._demod, frequency_hz=start.frequency_hz, mode=start.mode,
                                      bandwidth_hz=start.bandwidth_hz)
            if vfo != self._demod:
                self._demod = vfo
                self._demod_changed()
        proposal = run.proposal
        log.info("the calibration ended (%s) with %d of %d stations received%s", reason,
                 sum(result.ppm is not None for result in run.results), len(run.stations),
                 "" if proposal is None else f", proposing {proposal.ppm} ppm" if proposal.ppm is not None
                 else f", proposing nothing ({proposal.reason})")
        self._calibration_changed()
        if self._carrier is not None:
            self._carrier = CarrierMeasure()
            self._carrier_began = time.monotonic()
        if self._idle():
            self._wanted.clear()

    def _forget_calibration(self, back: bool = True) -> None:
        """Another receiver: the calibration that runs ends with ``source``,
        bringing the receiver back with ``back``, and stays, to say so; an
        earlier one goes, as its proposal was for the receiver before."""
        if self._running_calibration() is not None:
            self._end_calibration("source", back=back)
        elif self._calibration is not None and self._calibration.ended != "source":
            self._calibration = None
            self._calibration_changed()

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
        for subscription in self.clients:
            subscription.changed("peaks")

    def _scanner_changed(self) -> None:
        for subscription in self.clients:
            subscription.changed("scanner")
        self._engine.radios_changed()

    def _record(self, found: Sequence[Signal]) -> None:
        """Keep the signals found in a search as observations, at this
        computer's time and hour, and those within its range for the range
        scan that runs."""
        now = time.time()
        self._engine.observations.record(found, now, time.localtime(now).tm_hour)
        self._engine.observations_changed()
        running = self._kept
        if self._sweep is not None and running is not None and running.ended is None:
            plan = self._engine.plan
            channels = [(plan.channel(round(signal.frequency_hz), round(signal.width_hz)), signal.margin_db)
                        for signal in found if running.low_hz <= signal.frequency_hz <= running.high_hz]
            if KeptScans.record(running, channels, now):
                self._engine.scans_changed()

    def _recording_changed(self) -> None:
        for subscription in self.clients:
            subscription.changed("recording")
        self._engine.radios_changed()

    def _start_audio(self) -> None:
        """Start recording the audio, unless such a recording runs: from the
        next block, which also gives the audio rate, into a file that its
        thread makes. Raises ValueError, starting nothing, without a
        recordings folder and on the simulated band."""
        if self._audio is not None or self._closed:
            return
        folder = self._recording_folder()
        self._end_calibration("recording")      # the recording starts where the VFO comes back
        recording = AudioRecording(folder, time.time(), self._demod.frequency_hz, self._demod.mode,
                                   make_folder=folder == self._engine.default_recordings, report=self._reporter())
        self._audio, self._audio_rate, self._audio_samples = recording, None, 0
        self._started(recording)

    def _start_iq(self) -> None:
        """Start recording the receiver's raw I/Q, unless such a recording
        runs: from the samples that arrive next, at the sample rate asked of
        the receiver, into files that its thread makes. Raises ValueError,
        starting nothing, without a recordings folder, on the simulated band
        and for a source without raw samples."""
        if self._iq is not None or self._closed:
            return
        folder = self._recording_folder()
        self._end_driver_check(STOPPED)     # the recording starts where the receiver comes back
        self._end_calibration("recording")
        source = self.source
        # Named after the centre asked; its metadata gives the centre in use, as the samples are labelled,
        # and their format, which the first samples give.
        centre_hz, rate_hz = self._span()
        recording = IQRecording(folder, time.time(), centre_hz, rate_hz, hardware=source.hardware,
                                make_folder=folder == self._engine.default_recordings, report=self._reporter(),
                                datatype=None)
        source.tap(functools.partial(self._record_iq, recording))
        self._iq, self._iq_bytes = recording, 0
        self._started(recording)

    def _recording_folder(self) -> Path:
        """The folder a new recording goes to. Raises ValueError without a
        recordings folder and on the simulated band."""
        self._engine.check_recordings()
        if self.source.kind in UNRECORDED_KINDS:
            raise ValueError("the simulated band is not recorded")
        return self._engine.recordings_folder()

    def _reporter(self) -> Callable[[Recording, str, Any], None]:
        """What a recording's thread tells, brought to the event loop."""
        loop = asyncio.get_running_loop()

        def report(recording: Recording, event: str, value: Any) -> None:
            with contextlib.suppress(RuntimeError):     # the loop closed meanwhile
                loop.call_soon_threadsafe(self._recording_event, recording, event, value)

        return report

    def _started(self, recording: Recording) -> None:
        """Start the thread of ``recording``, which now runs, and keep the
        engine reading for it; the clients learn of it every second."""
        if self._recording_stopped is not None and self._recording_stopped["kind"] == recording.kind:
            self._recording_stopped = None
        self._engine.unfinished.add(recording)
        recording.start()
        self._wanted.set()          # the recording goes on without any client
        if self._progress_timer is None:
            self._progress_timer = asyncio.get_running_loop().call_later(RECORDING_PROGRESS_S,
                                                                         self._recording_progress)
        self._recording_changed()

    def recording_runs(self) -> bool:
        return self._audio is not None or self._iq is not None

    def running_files(self) -> set[str]:
        """The files of the recordings that run, which their threads write."""
        files = set()
        if self._audio is not None and self._audio.file is not None:
            files.add(self._audio.file)
        if self._iq is not None and self._iq.file is not None:
            files |= {self._iq.file, meta_name(self._iq.file)}
        return files

    def _stop_recording(self, kind: str, reason: str | None, detail: str = "") -> None:
        """Stop the recording of ``kind`` that runs, "audio" or "iq", if one
        does, for ``reason``: "rate", "source", "sample_rate", "receiver",
        "space", "full" or "disk", with ``detail`` for the last, or None
        when a client asked. Its thread writes what came, then closes its
        files. Without any client, scan and recording, the radio stops
        reading. The output tells of a stop by itself, as no interface may
        be open to show it."""
        recording: Recording | None
        if kind == "audio":
            recording, self._audio = self._audio, None
        else:
            recording, self._iq = self._iq, None
            if recording is not None:
                self.source.tap(None)
        if recording is None:
            return
        recording.stop()
        if self._progress_timer is not None and not self.recording_runs():
            self._progress_timer.cancel()
            self._progress_timer = None
        if reason is not None:
            self._recording_stopped = {"kind": kind, "reason": reason, "detail": detail}
            log.warning("the recording %s stopped: %s%s", recording.file or f"of {kind} just started",
                        _RECORDING_STOPS.get(reason, reason), f" ({detail})" if detail else "")
        self._recording_changed()
        if self._idle():
            self._wanted.clear()

    def _recording_event(self, recording: Recording, event: str, value: Any) -> None:
        """What a recording's thread told: its file was made ("opened", its
        name), or it stopped ("stopped", the reason and its detail)."""
        running = recording is self._audio or recording is self._iq
        if event == "opened":
            recording.file = value
            if running:
                self._recording_changed()
            return
        self._engine.unfinished.discard(recording)
        reason, detail = value
        if running:                             # by itself
            self._stop_recording(recording.kind, reason or "disk", detail)
        elif reason is not None:                # while closing its files, once stopped
            log.warning("the recording %s may be incomplete: %s%s", recording.file or "just started",
                        _RECORDING_STOPS.get(reason, reason), f" ({detail})" if detail else "")

    def _recording_progress(self) -> None:
        """Every RECORDING_PROGRESS_S while a recording runs, the clients
        learn its length and size."""
        self._progress_timer = None
        if not self.recording_runs() or self._closed:
            return
        self._recording_changed()
        self._progress_timer = asyncio.get_running_loop().call_later(RECORDING_PROGRESS_S,
                                                                     self._recording_progress)

    def _record_audio(self, pcm: np.ndarray) -> None:
        """Hand a block's audio to the audio recording that runs: the first
        sets its rate, and another rate stops it, as does a disk that does
        not keep up."""
        recording = self._audio
        rate = self._demodulator.audio_rate_hz
        if self._audio_rate is None:
            self._audio_rate = rate
        elif rate != self._audio_rate:
            self._stop_recording("audio", "rate")
            return
        if not recording.write(pcm.tobytes(), round(rate)):
            self._stop_recording("audio", "disk", "the disk did not keep up")
            return
        self._audio_samples += len(pcm)

    def _record_iq(self, recording: IQRecording, data: bytes, centre_hz: float, rate_hz: float,
                   joined: bool) -> None:
        """Hand raw samples, as they arrive, to ``recording`` while it runs:
        the first set its format; another sample rate stops it, as do
        another format, which another receiver gives, and a disk that does
        not keep up. Samples that follow a gap may come from a receiver that
        connected again: the metadata names it as it is then."""
        if recording is not self._iq:
            return
        try:
            if rate_hz != recording.rate_hz:
                self._stop_recording("iq", "sample_rate")
                return
            raw_format = self.source.raw_format
            if raw_format is None or not recording.take_format(*raw_format):
                self._stop_recording("iq", "receiver")
                return
            if not joined:
                recording.hardware = self.source.hardware
            if not recording.write(data, centre_hz, time.time(), joined):
                self._stop_recording("iq", "disk", "the disk did not keep up")
                return
            self._iq_bytes += len(data)
        except Exception:
            log.exception("the I/Q recording stopped on an error")
            self._stop_recording("iq", "disk", "an unexpected error, which Radiolack's error output gives")

    def _start_scan(self, scan: object) -> None:
        """Start a scan, "memory", "sweep", the range scan with its saved
        settings, or "signals", the range scan over the current window,
        stopping to listen, unless it runs, stopping the one that runs, or
        stop the scan that runs (None). Raises ValueError, changing nothing,
        for another scan, for the memory scan when no ticked bookmark lies
        within the receiver's reach, from the span it is in or comes back
        to, and for the range scan when the receiver cannot tune to every
        window of its range, or when it would listen while detection is
        off."""
        if scan is None:
            self._stop_scan(None)
        elif scan == "memory":
            if self._memory is None:
                self._start_memory()
        elif scan == "sweep":
            if self._sweep is None or self._sweep_here:
                self._start_sweep(here=False)
        elif scan == "signals":
            if self._sweep is None or not self._sweep_here:
                self._start_sweep(here=True)
        else:
            raise ValueError('a scan is "memory", "sweep" or "signals", or null to stop it')

    def _start_memory(self) -> None:
        if not self._memory_ids:    # from the span a sweep brings the receiver back to
            raise ValueError("no ticked bookmark lies within the receiver's reach")
        self._end_driver_check(STOPPED)     # which brings the receiver back too
        self._end_calibration("scan")       # likewise
        self._stop_scan(None)       # the range scan, which brings the receiver back unless on a signal
        centre_hz = self._span()[0]
        self._memory = MemoryScan()
        self._home = (centre_hz, self._demod)
        self._stopped = None
        self._wanted.set()          # the scan goes on without any client
        self.watch_bookmarks()     # the channels of the span
        if self._memory is not None:    # unless the sweep came back where none lies within reach
            self._tour = MemoryTour(self._windows, centre_hz, time.monotonic())

    def _start_sweep(self, here: bool) -> None:
        """Start the range scan: with its saved settings, or ``here``, over
        the part of the current span that detection searches, stopping to
        listen, without end, the saved settings left as they are."""
        listen, passes = (True, None) if here else (self._sweep_listen, self._sweep_passes)
        if listen and not self._detection:
            raise ValueError(LISTEN_NEEDS_DETECTION)
        if not here:
            self._sweep_windows(self._sweep_low_hz, self._sweep_high_hz)     # refused, it changes nothing
        self._end_driver_check(STOPPED)     # which brings the receiver back
        self._end_calibration("scan")       # likewise
        self._stop_scan(None)       # the memory scan or the range scan, which bring the receiver back
        # From the span the receiver came back to.
        centre_hz = self._span()[0]
        windows = (SweepWindows(centre_hz, 0.0, 1) if here
                   else self._sweep_windows(self._sweep_low_hz, self._sweep_high_hz))
        self._sweep = Sweep(windows, search_blocks(self._sweep_window_s, self._frame_rate), time.monotonic(),
                            listen, passes)
        self._sweep_here = here
        self._home = (centre_hz, self._demod)
        self._keep_sweep(here, windows, listen, passes)
        self._stopped = None
        self._wanted.set()          # the scan goes on without any client
        if self._sweep.centre_hz != self._span()[0]:    # a range within the span stays there
            self._tune_scan(self._sweep.centre_hz)

    def _keep_sweep(self, here: bool, windows: SweepWindows, listen: bool, passes: int | None) -> None:
        """Keep the range scan that starts, under the name given or its range
        and start, unless it reads the simulated band, or goes on with the
        kept scan that runs, after a new receiver's settings."""
        self._kept_step_s = time.monotonic()
        if self.source.kind in UNRECORDED_KINDS:
            if KeptScans.end(self._kept, time.time(), "source"):      # one that ran on a receiver stops there
                self._engine.scans_changed(soon=True)
            return
        if self._kept_resumed and self._kept is not None and self._kept.ended is None:
            return
        KeptScans.end(self._kept, time.time(), "stopped")      # never in practice: a scan stops first
        if here:
            rate_hz = self._span()[1]
            low_hz, high_hz = windows.centre(0) - USABLE_FRACTION * rate_hz, windows.centre(0) + USABLE_FRACTION * rate_hz
        else:
            low_hz, high_hz = self._sweep_low_hz, self._sweep_high_hz
        self._kept = self._engine.kept.start(self._kept_name, time.time(), max(1.0, low_hz), high_hz, listen, passes)
        self._engine.scans_changed(soon=True)

    def _sweep_windows(self, low_hz: float, high_hz: float) -> SweepWindows:
        """The sweep's windows from ``low_hz`` to ``high_hz`` at the receiver's
        sample rate: one, the span itself, for a range within the part of the
        span that detection searches, the span a scan started from while one
        runs; raises ValueError for a wider range when the receiver does not
        tune, or cannot tune to every window."""
        described = self.source.describe()
        centre_hz = described["centre_hz"] if self._home is None else self._home[0]
        one = range_window(low_hz, high_hz, centre_hz, described["sample_rate_hz"])
        if one is not None:
            return one
        if "centre_hz" not in described["settable"]:
            raise ValueError("a range wider than the current window needs a receiver that tunes, such as an RTL-SDR")
        windows = sweep_windows(low_hz, high_hz, described["sample_rate_hz"])
        low, high = described["range_hz"]
        if windows.centre(0) < low or windows.centre(windows.count - 1) > high:
            raise ValueError("the receiver cannot tune to every window of the range scan's range: "
                             f"it tunes from {low / 1e6:g} to {high / 1e6:g} MHz")
        return windows

    def _tune_scan(self, centre_hz: float) -> None:
        """Ask the receiver for a window of the scan that runs; one that
        refuses it stops the scan."""
        try:
            self.source.configure({"centre_hz": centre_hz})
        except ValueError as error:
            log.warning("the %s stopped: the receiver did not tune to %.0f Hz: %s",
                        SCAN_NAMES[self._running_scan() or "sweep"], centre_hz, error)
            self._stop_scan("range")

    def _step_sweep(self, sweep: Sweep, centre_hz: float, squelch_open: bool, label: tuple[float, float]) -> None:
        """Move the sweep on with a block read at ``centre_hz``, which
        ``label``, the centre in use and the rate, labels; ``squelch_open``
        tells whether the squelch was open at the block's end. Once a
        window is done, the receiver goes to the next one, unless the sweep
        listens there first to the window's active signals; once the passes
        set are done, it stops."""
        now = time.monotonic()
        signals = self._span_signals(label, now)
        listening = sweep.listening
        before = (sweep.window, sweep.passes, None if listening is None else (listening.state, listening.frequency_hz))
        target, channel = None, None
        if sweep.listening is None:
            target = sweep.step(now, centre_hz, list(signals))
        if sweep.listening is not None:     # since this block too, once the window's searches ended
            target, channel = sweep.listen_step(now, list(signals), squelch_open, self._resume_s, self._listen_s)
        if channel is not None:
            self._tune_signal(channel, signals)
        self._keep_progress(sweep, now, squelch_open)
        if sweep.done:
            self._stop_scan("done")
            return
        if target is not None and target != self._span()[0]:
            self._tune_scan(target)
        listening = sweep.listening
        if (sweep.window, sweep.passes,
                None if listening is None else (listening.state, listening.frequency_hz)) != before:
            self._scanner_changed()

    def _keep_progress(self, sweep: Sweep, now: float, squelch_open: bool) -> None:
        """Keep the range scan's passes done and, while it listens with the
        squelch open, the time since its latest block on the signal heard."""
        elapsed = min(max(0.0, now - self._kept_step_s), 1.0)     # a block longer than a second counts for one
        self._kept_step_s = now
        listening = sweep.listening
        if (listening is not None and listening.state == "listening" and listening.frequency_hz is not None
                and squelch_open):
            KeptScans.listened(self._kept, listening.frequency_hz, elapsed)
            self._engine.scans_changed()
        if KeptScans.progress(self._kept, sweep.passes):
            self._engine.scans_changed()

    def _span_signals(self, label: tuple[float, float], now: float) -> dict[int, tuple[float, float]]:
        """The active signals of the part of the span that ``label``, the
        centre in use and the rate, labels and that detection searches, by
        the channel that the list of signals shows each on: its frequency
        and width."""
        centre_hz, rate_hz = label
        signals: dict[int, tuple[float, float]] = {}
        for frequency_hz, width_hz in self._signals.active(now):
            if DC_GAP_HZ < abs(frequency_hz - centre_hz) <= USABLE_FRACTION * rate_hz:
                # As the list shows it: its frequency and width in whole Hz.
                channel = self._engine.plan.channel(round(frequency_hz), round(width_hz))
                signals.setdefault(channel, (frequency_hz, width_hz))
        return signals

    def _tune_signal(self, channel: int, signals: Mapping[int, tuple[float, float]]) -> None:
        """Tune the VFO to a scan's ``channel``, one of ``signals``, for every
        client: with the band plan's mode there, and that mode's filter
        width when the mode changes."""
        self._signal_tuned = signals[channel]
        mode = plan_mode(self._engine.plan.mode_at(channel), self._demod.mode)
        changes: dict[str, object] = {"frequency_hz": channel, "mode": mode}
        if mode != self._demod.mode:
            changes["bandwidth_hz"] = MODE_BANDWIDTHS_HZ[mode]
        self._demod = self._demod.changed(changes)
        self._demod_changed()

    def _listening(self) -> SignalScan | None:
        """The range scan's listening while it listens in a window; None
        otherwise."""
        return None if self._sweep is None else self._sweep.listening

    def _running_scan(self) -> str | None:
        """The scan that runs, "memory" or "sweep", the range scan, or None."""
        if self._memory is not None:
            return "memory"
        return None if self._sweep is None else "sweep"

    def _scanning(self) -> bool:
        return self._running_scan() is not None

    def _idle(self) -> bool:
        """Whether nothing needs the source's samples: no client, no scan, no
        calibration and no recording, while the radio runs."""
        return (not self.clients and not self._scanning() and self._running_calibration() is None
                and not self.recording_runs() and not self._closed)

    def _stop_scan(self, reason: str | None) -> None:
        """Stop the scan that runs, if one does, for ``reason``: "tuned",
        "source", "bookmarks", "range", "detection" or "done", or None when
        a client asked. Unless tuned by hand or left for another source, the
        receiver and the VFO come back where the scan found them: after a
        range scan, unless it stays on a signal it listens to, and after a
        memory scan that moved the receiver, unless it stays on a channel.
        Without any client, the radio stops reading. The
        output tells of a stop for want of bookmarks, as ``_tune_scan`` does
        of a refused window: no interface may be open to show it."""
        scan = self._running_scan()
        if scan is None:
            return
        home, memory, tour, sweep = self._home, self._memory, self._tour, self._sweep
        if reason == "bookmarks":
            log.warning("the memory scan stopped: no ticked bookmark lies within the receiver's reach any more")
        self._memory = self._tour = self._sweep = self._home = None
        self._sweep_here = False
        if sweep is not None and not self._kept_resumed and KeptScans.end(self._kept, time.time(), reason or "stopped"):
            self._engine.scans_changed(soon=True)
        self._stopped = None if reason is None else (scan, reason)
        if home is not None:
            back = ((sweep is not None and (sweep.listening is None or sweep.listening.frequency_hz is None))
                    or (memory is not None and memory.bookmark_id is None and tour is not None and tour.moved))
            if back and reason not in ("tuned", "source"):
                self._go_home(home[0], home[1].frequency_hz)
            self.watch_bookmarks()     # from the span where the receiver stays
        self._scanner_changed()
        if self._idle():
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

    def watch_bookmarks(self) -> None:
        """Find the ticked bookmarks that a memory scan watches, from the
        span the receiver is in, or while a scan runs the one it started
        from, and the windows they take; while the memory scan runs, also
        the channels of the span asked of the receiver. The clients learn
        when the bookmarks or the windows change: a memory scan then visits
        the new windows, or stops when none is left."""
        centre_hz, rate_hz = self._span()
        start_hz = centre_hz if self._home is None else self._home[0]
        ticked = self._engine.bookmarks.ticked()
        windows, ids = self._memory_plan(ticked, start_hz, rate_hz)
        in_use = self.source.describe()["centre_in_use_hz"]
        watched = watched_channels(ticked, in_use, rate_hz) if self._memory is not None else ()
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
        span that asking for ``centre_hz`` gives alone, unless the receiver
        tunes and other ticked bookmarks lie within its reach, which then
        take as few windows as ``memory_windows`` finds, where the receiver
        places them."""
        source = self.source
        span = tuple(channel.bookmark_id
                     for channel in watched_channels(ticked, source.centre_in_use(centre_hz), rate_hz))
        described = source.describe()
        if "centre_hz" in described["settable"]:
            low_hz, high_hz = described["range_hz"]
            placement = Placement(source.centre_in_use, source.grid_hz)
            reach = [(bookmark_id, bookmark.frequency_hz) for bookmark_id, bookmark in ticked
                     if memory_window(bookmark.frequency_hz, rate_hz, low_hz, high_hz, placement) is not None]
            if not {bookmark_id for bookmark_id, _ in reach} <= set(span):
                windows = memory_windows((frequency for _, frequency in reach), rate_hz, low_hz, high_hz,
                                         placement)
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

    def _switch(self, kind: object) -> None:
        """Read a source of ``kind`` in place of this one: the receiver used
        last of that kind, with its saved settings. The source in use closes,
        and its settings are kept for when it comes back."""
        sources = self._engine.sources
        if sources is None:
            raise ValueError("this source cannot be changed")
        if not isinstance(kind, str) or kind not in sources.kinds:
            raise ValueError(f"unknown kind of source: {str(kind)[:40]!r}")
        if kind != self.source.kind:
            if kind == "soapy":
                self._engine.check_no_search()
            try:
                new = sources.open(self._engine.reopen.get(kind, {"kind": kind}))
            except ValueError as error:
                log.warning("the saved %s receiver is not used: %s", kind, error)
                new = sources.open({"kind": kind})
            try:
                self._engine.check_free(new, self)
            except ValueError:
                new.close()         # it never read: nothing to release
                raise
            self._replace(new)
        self._source_changed()

    def _replace(self, new: Source) -> None:
        """Read ``new`` in place of the source in use, which closes, its
        settings kept for when it comes back; ``new`` starts from those
        saved for its receiver."""
        old = self.source
        # Saved before the scan or the calibration stops: those of its start.
        self._engine.receivers[old.settings_key] = self.receiver_settings()
        self._forget_calibration(back=False)
        self._stop_scan("source")
        self._stop_recording("iq", "receiver")      # while the source in use still holds its tap
        if new.kind in UNRECORDED_KINDS:
            self._stop_recording("audio", "source")
        old.on_change = None
        old.close()
        self._engine.closing.add(asyncio.ensure_future(self._release(old)))
        self.source = new
        self._engine.reopen[new.kind] = new.reopen
        self._adopt()
        self._demod_changed()
        self._signals.clear()       # found by another receiver
        if self._signals.changed(time.monotonic()):
            self._peaks_changed(at_once=True)

    def _open_soapy(self, args: str) -> None:
        """Read the SoapySDR device that ``args`` name in place of the
        source in use, unless it is that device already. Raises ValueError,
        changing nothing, for arguments that ``soapy.parse_args`` refuses,
        and while a search runs. A check of the driver's computation that
        runs stops first, bringing the receiver back."""
        sources = self._engine.sources
        if sources is None or "soapy" not in sources.kinds:
            raise ValueError("this Radiolack does not open SoapySDR devices")
        self._engine.check_no_search()
        new = sources.open({"kind": "soapy", "args": args})
        if self.source.kind == "soapy" and new.reopen == self.source.reopen:
            new.close()         # it never read: nothing to release
            return
        try:
            self._engine.check_free(new, self)
        except ValueError:
            new.close()         # it never read: nothing to release
            raise
        self._end_driver_check(STOPPED)
        self._end_calibration("source")
        self._replace(new)
        self._source_changed()

    def soapy_in_use(self) -> Device | None:
        """The SoapySDR device that the source in use reads, if it is one."""
        if not isinstance(self.source, SoapySource):
            return None
        driver, serial, label = self.source.identity
        return soapy_device(driver, serial, label or self.source.hardware)

    def follow_devices(self) -> None:
        """Every client receives the devices state again, if it changed:
        the search's, the device in use or the device chosen."""
        if self._engine.device_search is None:
            return
        state = self.devices_state()
        if state == self._devices_sent:
            return
        self._devices_sent = state
        for subscription in self.clients:
            subscription.changed("devices")

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
        self._settings_key = self.source.settings_key
        saved = self._engine.receivers.get(self._settings_key, {})
        self._restore_source(saved.get("source"))
        described = self.source.describe()
        # Unless saved, the VFO starts a quarter of the span above the centre, clear of the DC spike.
        self._demod = DemodSettings(
            frequency_hz=float(round(described["centre_hz"] + described["sample_rate_hz"] / 4)))
        self._restore_demod(saved.get("demod"))
        self._capture = self._vfo_set_in = None
        self.source.on_change = self._source_changed

    def _identified(self) -> None:
        """The source in use has learnt which receiver it reads (``Source``):
        what was kept under its former key, which named no receiver, goes;
        the settings saved for that receiver, if any, apply, as at a switch,
        and the receiver opens by its new ``reopen`` from now on."""
        former, self._settings_key = self._settings_key, self.source.settings_key
        self._engine.receivers.pop(former, None)
        self._engine.reopen[self.source.kind] = self.source.reopen
        saved = self._engine.receivers.get(self._settings_key)
        log.info("the %s source reads %s", self.source.kind, self._settings_key)
        self._forget_calibration()
        if not isinstance(saved, dict):
            return
        # A scan started before, such as --scan's, which the receiver's
        # first opening follows at once, starts again from these settings.
        scan = "signals" if self._sweep_here else self._running_scan()
        self._kept_resumed = True       # a range scan's kept scan goes on
        try:
            self._stop_scan(None)
            self._restore_source(saved.get("source"))
            self._restore_demod(saved.get("demod"))
            self._demod_changed()
            if scan is not None:
                try:
                    self._start_scan(scan)
                except ValueError as error:
                    log.warning("the %s stopped: %s", SCAN_NAMES[scan], error)
        finally:
            self._kept_resumed = False
        if self._sweep is None and KeptScans.end(self._kept, time.time(), "stopped"):
            self._engine.scans_changed(soon=True)

    def receiver_settings(self) -> dict[str, Any]:
        """The settings saved for the receiver in use: while a scan or a
        calibration runs, with the centre frequency, and the VFO's
        frequency, mode and filter width, at its start, and while a check of
        the driver's computation runs, with the centre frequency at its
        start."""
        state = self.state()
        source = {name: state[name] for name in state["settable"] if name not in UNSAVED_SETTINGS}
        demod = self._demod.describe()
        run = self._running_calibration()
        home = run.home if run is not None else self._home
        if home is not None:
            centre_hz, start = home
            demod.update(frequency_hz=start.frequency_hz, mode=start.mode, bandwidth_hz=start.bandwidth_hz)
            if "centre_hz" in source:       # not with a source that does not tune
                source["centre_hz"] = centre_hz
        if self._driver_check is not None and "centre_hz" in source:
            source["centre_hz"] = self._driver_check.home[0]
        return {"source": source, "demod": demod}

    def _restore_source(self, saved: object) -> None:
        """Apply the source settings saved for this receiver, apart from
        those the source was given: all at once, as they hold together, such
        as a centre frequency beyond a converter; when the receiver refuses
        that, one at a time, in the order of ``settable``, and one that the
        receiver refuses keeps its default."""
        if not isinstance(saved, dict):
            return
        settable = self.state()["settable"]
        restored = {name: saved[name] for name in settable
                    if name in saved and name not in self.source.given and name not in UNSAVED_SETTINGS}
        if not restored:
            return
        try:
            self._configure(restored, restoring=True)
            return
        except ValueError:
            pass
        for name, value in restored.items():
            try:
                self._configure({name: value}, restoring=True)
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
        if "listen_s" in saved:
            try:
                self._listen_s = checked_listen(saved["listen_s"])
            except ValueError as error:
                log.warning("the saved setting listen_s is not used: %s", error)
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
        listen = saved.get("sweep_listen", self._sweep_listen)
        if isinstance(listen, bool):
            self._sweep_listen = listen
        else:
            log.warning("the saved setting sweep_listen is not used")
        if "sweep_passes" in saved:
            try:
                self._sweep_passes = checked_passes(saved["sweep_passes"])
            except ValueError as error:
                log.warning("the saved setting sweep_passes is not used: %s", error)
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

    def _follow_conversion(self, old: Conversion, new: Conversion) -> None:
        """Another converter in front of the receiver, which stays where it
        is tuned: the VFO moves with the frequencies so as to stay on the
        same signal, and the signals found through the old one are
        forgotten. A VFO that would leave Radiolack's frequencies stays,
        and the span's edge takes it once the new samples come."""
        frequency = new.air(old.tuned(self._demod.frequency_hz))
        if 1 <= frequency <= HIGHEST_HZ:
            self._demod = dataclasses.replace(self._demod, frequency_hz=float(frequency))
            self._demod_changed()
        self._signals.clear()
        if self._signals.changed(time.monotonic()):
            self._peaks_changed(at_once=True)

    def _keep_vfo_in(self, centre_hz: float, rate_hz: float) -> None:
        """A new span: a VFO left outside it moves to its nearest edge."""
        low, high = math.ceil(centre_hz - rate_hz / 2), math.floor(centre_hz + rate_hz / 2)
        frequency = min(max(self._demod.frequency_hz, low), high)
        if frequency != self._demod.frequency_hz:
            self._demod = dataclasses.replace(self._demod, frequency_hz=float(frequency))
            self._demod_changed()

    def _publish(self, capture: tuple[float, float], power: np.ndarray, codes: np.ndarray, meter: Meter,
                 pcm: np.ndarray | None) -> None:
        if not self.clients:
            return
        frame = encode_frame(self._sequence, *capture, self.analyzer.db_min, self.analyzer.db_step, codes,
                             meter.level_db, meter.squelch_db, meter.open)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        remote_frame = self._remote_frame(capture, power, meter)
        audio = self._demodulator.encode_audio(pcm) if pcm is not None and len(pcm) else None
        for subscription in self.clients:
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
        if not any(subscription.remote for subscription in self.clients):
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


@dataclasses.dataclass
class _DriverCheck:
    """A check of the driver's computation as it runs (pllcheck.py): the
    interfaces waiting for it, with the id each gave; the sample rate; the
    points, the centre in use first; the centre asked and the grid's
    setting at its start, which come back at its end, and the computation
    set then; the centre asked and the centre in use of the samples that the
    point it waits for takes, its measure and when it began; and where the
    carrier lay at each point measured, on the tuner's frequencies, from
    the samples' label."""

    waiting: dict[Subscription, int | None]
    rate_hz: int
    points: tuple[Point, ...]
    home: tuple[float, bool]
    previous: str
    expected: tuple[float, float]
    began: float
    gatherer: CarrierMeasure = dataclasses.field(default_factory=CarrierMeasure)
    offsets: list[float] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _CalibrationRun:
    """An automatic calibration (calibration.py): its stations; the centre
    asked and the demodulation settings at its start, whose VFO comes back
    at its end; the correction in use while it runs; what it measured at
    each station so far, in order; the centre asked and the centre in use
    of the samples that the station it waits for takes, its measure and
    when it began; and once it ended, why, and with ``done`` or
    ``stopped`` the correction it proposes, or why none."""

    stations: tuple[calibration.Station, ...]
    home: tuple[float, DemodSettings]
    ppm: float
    results: list[calibration.Result] = dataclasses.field(default_factory=list)
    expected: tuple[float, float] | None = None
    began: float = 0.0
    gatherer: CarrierMeasure = dataclasses.field(default_factory=CarrierMeasure)
    ended: str | None = None
    proposal: calibration.Proposal | None = None


def _driver_answer(request_id: int | None, found: str | None, previous: object, reason: str | None
                   ) -> dict[str, Any]:
    """The answer to a check of the driver's computation (docs/design/protocol.md)."""
    return {"type": "driver_pll", "id": request_id, "driver_pll": found, "previous": previous, "reason": reason}


def source_summary(source: Source) -> dict[str, Any]:
    """What the radios message says of a radio's source: its kind, the
    receiver it reads, the tuner and the status."""
    described = source.describe()
    if source.kind == "rtl_sdr":
        device = source.reopen.get("device")
    elif source.kind == "rtl_tcp":
        device = described.get("address")
    elif source.kind == "soapy":
        device = source.settings_key.removeprefix("soapy ")
    else:
        device = None
    return {"kind": source.kind, "device": device, "tuner": described.get("tuner"), "status": described.get("status")}


def _open_saved(sources: SourceOpener, saved: object) -> Source:
    """The source used last, or the default kind's when none was saved or it
    cannot be opened."""
    if isinstance(saved, dict):
        try:
            return sources.open(saved)
        except ValueError as error:
            log.warning("the saved source is not used: %s", error)
    return sources.open({"kind": sources.kinds[0]})


def _process_block(analyzer: SpectrumAnalyzer, demodulator: Demodulator, detector: Detector,
                   watch: ChannelWatch, samples: np.ndarray, centre_hz: float, rate_hz: float,
                   dc_removal: bool, settings: DemodSettings, audio: bool, threshold_db: float | None,
                   search_blocks: int, bands: np.ndarray | None,
                   measure: tuple[CarrierMeasure, tuple[Any, ...], float, tuple[float, float]] | None = None,
                   rate_scale: float = 1.0,
                   fine: tuple[FineSpectrum, tuple[Any, ...], float] | None = None,
                   wfm_deemphasis_s: float = WFM_DEEMPHASIS_S,
                   ) -> tuple[np.ndarray, np.ndarray, Meter, np.ndarray | None, list[Detection] | None,
                              tuple[np.ndarray, np.ndarray] | None, Carrier | None, list[np.ndarray]]:
    """The bin powers, the display codes, the channel meter, with ``audio``
    the audio, once ``detector`` has averaged ``search_blocks`` blocks the
    signals found, once ``watch`` knows the noise the power and the noise of
    the memory scan's channels, once ``measure``, a carrier's measure, the
    setting of these samples and where it looks in the channel, from its
    0 Hz, has one its result, and with ``fine``,
    the fine spectrum and the setting of these samples, the codes of the
    fine frames they complete, of a block of samples, worked out in a
    worker thread. Detection is off without ``threshold_db``, and the
    memory scan's channels are not watched without ``bands``, their
    passbands and noise clearances. The VFO's distance from ``centre_hz``
    shrinks by ``rate_scale``, the factor by which the receiver samples
    faster than ``rate_hz``: the samples show it that much nearer. The
    noise blanker acts within the demodulator, on the VFO's channel alone:
    the bin powers, detection, the memory scan's channels, the measure and
    the fine spectrum take the samples as received. Without ``fine`` the
    demodulator's front filter is not tapped. WFM takes the de-emphasis of
    time constant ``wfm_deemphasis_s``."""
    if dc_removal:
        # A mean in single precision would leave a trace of a strong offset.
        samples = samples - np.complex64(samples.mean(dtype=np.complex128))
    power = analyzer.power(samples)
    shift_hz = (settings.frequency_hz - centre_hz) / rate_scale
    noise = analyzer.noise_density(power, len(samples), shift_hz, rate_hz,
                                   noise_clearance_hz(settings.mode, settings.bandwidth_hz))
    carrier: list[Carrier | None] = [None]     # what the measure gives, if any
    tap: Callable[[np.ndarray], None] | None = None
    if measure is not None:
        gatherer, key, _, (low_hz, high_hz) = measure

        def tap(channel: np.ndarray) -> None:
            carrier[0] = gatherer.add(channel, demodulator.channel_rate_hz, low_hz, high_hz, key)
    fine_codes: list[np.ndarray] = []
    front_tap: Callable[[np.ndarray], None] | None = None
    if fine is not None:
        spectrum, fine_key, _ = fine

        def front_tap(front: np.ndarray) -> None:
            fine_codes.extend(analyzer.codes(power) for power in
                              spectrum.add(front, demodulator.channel_rate_hz, fine_key))
    meter, pcm = demodulator.process(samples, settings.frequency_hz - shift_hz, rate_hz, settings, noise, audio, tap,
                                     front_tap, wfm_deemphasis_s)
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
    return power, analyzer.codes(power), meter, pcm, detections, levels, carrier[0], fine_codes


def _filter_band(demod: DemodSettings) -> tuple[float, float]:
    """Where the VFO's filter passes, from its channel's 0 Hz: where a
    carrier's measure looks."""
    low_hz, high_hz = passband_hz(demod.mode, demod.bandwidth_hz)
    position = vfo_position_hz(demod.mode)
    return position + low_hz, position + high_hz


def _conversion(described: Mapping[str, Any]) -> Conversion:
    """The converter in front of a source, from its state: none for a
    source without one."""
    kind, lo_hz = described.get("converter"), described.get("converter_lo_hz")
    return Conversion(kind, lo_hz) if isinstance(kind, str) and isinstance(lo_hz, float) else Conversion()
