"""Monitor loop: FFT detection, band exploration, listens and squelch memory.

The loop runs every 100 ms on the latest captured I/Q frame. It selects a
peak seen in consecutive observations, tunes Gqrx, listens for the profile's
duration while measuring, and stores an observation only after validation.
Interruptions (receiver change, stale stream, Gqrx disconnection, manual
retune) cancel the listen without storing anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

from . import __version__
from .detection import compute_spectrum, extract_peaks
from .errors import GqrxProtocolError, ReceiverError
from .gqrx import GqrxControl
from .listening import LIMITED_COVERAGE_FRACTION, ListeningWindow
from .profiles import Profile
from .receiver import Endpoint
from .scanning import FIXED_WINDOW_SCAN_INFO, BandExplorer, usable_span
from .squelch import FftActivity, SquelchLearner, SquelchSettings


@dataclass(frozen=True)
class MonitorSettings:
    profile: Profile
    full_band: bool = True
    gqrx: Endpoint = Endpoint("127.0.0.1", 7356)
    squelch: SquelchSettings = field(default_factory=SquelchSettings)
    tick_s: float = 0.1
    stale_after_s: float = 0.5
    meter_interval_s: float = 0.2
    first_meter_delay_s: float = 0.3
    reconnect_interval_s: float = 2.0
    stall_warning_s: float = 5.0


@dataclass
class Station:
    frequency: int
    learner: SquelchLearner
    confirmed: bool
    activity: FftActivity


def receiver_context(frame, profile: Profile) -> dict:
    """Settings a squelch memory depends on; any change invalidates the memory."""
    return {"sample_rate_hz": frame.sample_rate, "filter_hz": profile.filter_hz, "mode": profile.mode,
            "receiver_controls": [list(item) for item in frame.receiver_controls]}


def console(text: str):
    print(f"{time.strftime('%H:%M:%S', time.gmtime())}Z {text}", flush=True)


class Monitor:
    def __init__(self, settings: MonitorSettings, capture, bridge, store, *, backend=None,
                 control_factory=None, clock=time.monotonic, sleep=time.sleep, emit=console):
        self.settings = settings
        self.profile = settings.profile
        self.capture = capture
        self.bridge = bridge
        self.store = store
        self.backend = backend
        self.control_factory = control_factory or (lambda: GqrxControl(
            settings.gqrx, mode=self.profile.mode, filter_hz=self.profile.filter_hz))
        self.clock = clock
        self.sleep = sleep
        self.emit = emit
        self.explorer = (BandExplorer(self.profile.grid, self.profile.window_seconds)
                         if settings.full_band else None)
        self.control = None
        self.listening = None
        self.station = None
        self.streaks = {}
        self.cooldown = {}
        self.last_sequence = -1
        self.last_epoch = -1
        self.next_connection = 0.0
        self.next_meter = 0.0
        self.warned_remote = False
        self.waiting_for_source = False
        self.last_fresh = None
        self.stall_warned = False

    def run(self):
        try:
            while True:
                self.tick()
        finally:
            if self.control is not None:
                self.control.close()
                self.control = None

    # -- helpers -----------------------------------------------------------

    def _cancel(self, reason: str):
        if self.listening is not None:
            self.emit(f"Listen cancelled: {reason}; not logged.")
        self.listening = None
        self.station = None

    def _drop_control(self):
        if self.control is not None:
            self.control.close(restore=False)
            self.control = None

    def _lost_control(self, now: float):
        detail = "; listen not logged" if self.listening is not None else ""
        self.emit(f"Gqrx remote control disconnected{detail}. Reconnecting...")
        self._drop_control()
        self.listening = None
        self.station = None
        self.streaks.clear()
        self.next_connection = now + self.settings.reconnect_interval_s

    def _connect(self, now: float) -> bool:
        if now < self.next_connection:
            return False
        try:
            self.control = self.control_factory()
        except OSError:
            if not self.warned_remote:
                self.emit(f"Waiting for Gqrx remote control on {self.settings.gqrx}: enable "
                          "Tools > Remote control in Gqrx (local access only).")
                self.warned_remote = True
            self.next_connection = now + self.settings.reconnect_interval_s
            return False
        self.warned_remote = False
        self.last_fresh = now
        mode = "exploring the full band" if self.explorer else "watching the current window"
        self.emit(f"Gqrx connected; {mode} for peaks.")
        return True

    def _warn_stall(self, now: float):
        if self.last_fresh is None:
            self.last_fresh = now
        if not self.stall_warned and now - self.last_fresh > self.settings.stall_warning_s:
            self.emit(f"No fresh I/Q samples for {self.settings.stall_warning_s:g} s: Gqrx may be paused "
                      "(DSP stopped), or the receiver stalled or was unplugged. Waiting...")
            self.stall_warned = True

    # -- main iteration ----------------------------------------------------

    def tick(self):
        settings = self.settings
        self.sleep(settings.tick_s)
        now = self.clock()
        if self.backend is not None:
            self.backend.check()
        if self.bridge.failure is not None:
            raise ReceiverError(f"I/Q bridge failed: {self.bridge.failure}")
        frame, epoch, connected = self.capture.snapshot()
        if epoch != self.last_epoch:
            self._cancel("receiver settings changed" if connected else "Gqrx input disconnected")
            self.streaks.clear()
            self.last_epoch = epoch
            self.last_fresh = now
        if not connected:
            if self.explorer is not None:
                self.explorer.reset()
            self._drop_control()
            if not self.waiting_for_source:
                self.emit("Waiting for Gqrx to connect to the I/Q bridge; open Gqrx with the RadioLack "
                          "profile and start reception.")
                self.waiting_for_source = True
            return
        self.waiting_for_source = False
        if self.control is None and not self._connect(now):
            return
        if self.explorer is not None:
            self.explorer.check_timeout(now)
        if frame is None or now - frame.received_at > settings.stale_after_s:
            if self.listening is not None:
                self._cancel("I/Q stream interrupted")
            self._warn_stall(now)
            return
        self.last_fresh = now
        self.stall_warned = False
        if self.explorer is not None and self._explore(frame, epoch, now):
            return
        if self.listening is not None and now >= self.listening.deadline:
            self._finish_listen(frame, now)
            return
        if frame.sequence == self.last_sequence:
            return
        self.last_sequence = frame.sequence
        spectrum = compute_spectrum(frame.raw, frame.center, frame.sample_rate)
        peaks = extract_peaks(spectrum, self.profile.grid, self.profile.margin_db)
        if self.station is not None:
            relaxed = extract_peaks(spectrum, self.profile.grid, settings.squelch.relaxed_margin_db)
            nearby = any(abs(peak.frequency_hz - self.station.frequency) <= self.profile.grid.proximity_hz
                         for peak in relaxed)
            self.station.activity.update(nearby, frame.received_at)
            if now >= self.next_meter and not self._meter_tick(now):
                return
        if self.listening is not None:
            self.listening.observe(peaks, frame.received_at)
            return
        self._select_candidate(peaks, frame, epoch, now)

    def _explore(self, frame, epoch, now) -> bool:
        """Drive full-band window moves; True when this tick must end here."""
        explorer = self.explorer
        explorer.sync_rate(frame.sample_rate)
        if explorer.pending is not None:
            if not explorer.confirm(frame, epoch, now):
                return True
            low, high = usable_span(frame.center, frame.sample_rate, self.profile.grid)
            self.emit(f"WINDOW {explorer.current_index + 1}/{len(explorer.centers)} | "
                      f"{low / 1e6:.3f}-{high / 1e6:.3f} MHz | FFT center {frame.center / 1e6:.6f} MHz")
        if self.listening is None and explorer.due(now):
            focus = explorer.next_focus()
            try:
                self.control.move_window(focus, frame.sample_rate)
            except OSError:
                self._lost_control(now)
                return True
            explorer.mark_requested(epoch, self.clock())
            self.station = None
            self.streaks.clear()
            return True
        return False

    def _finish_listen(self, frame, now):
        listening = self.listening
        try:
            # A manual VFO change inside the same window also cancels logging.
            tuned = self.control.frequency() == listening.frequency
            self.control.mute()
        except OSError:
            self._lost_control(now)
            return
        station = self.station
        if tuned and station is not None and station.frequency == listening.frequency:
            event = listening.result(now, frame.center, frame.sample_rate, self.profile.margin_db,
                                     fft_guard_seconds=self.settings.squelch.fft_guard_s)
            if event is not None:
                record = dict(station.learner.record)
                event.update(
                    squelch_dbfs=record["squelch_dbfs"], squelch_source=record["source"],
                    gqrx_noise_floor_dbfs=record.get("noise_floor_dbfs"),
                    squelch_carrier_meter_dbfs=record.get("carrier_meter_dbfs"),
                    squelch_learning_version=record.get("learning_version"),
                    receiver_context=receiver_context(frame, self.profile),
                    quality_flags={
                        "limited_fft_coverage": event["coverage_fraction"] < LIMITED_COVERAGE_FRACTION,
                        "provisional_squelch": record["source"] != "noise"},
                    profile=self.profile.name,
                    confirmation_observations=self.profile.confirm_observations,
                    cooldown_seconds=self.profile.cooldown_seconds,
                    recorded_by=f"radiolack {__version__}")
                # A disk error propagates as StoreWriteError: it is never mistaken for
                # a radio disconnection and the previous store content stays intact.
                self.store.append(event, station=(listening.frequency, record))
                station.confirmed = True
                self.emit(f"SAVED {listening.frequency / 1e6:.6f} MHz | active "
                          f"{listening.active_seconds:.1f}s / {self.profile.listen_seconds:g}s | "
                          f"validated listens {self.store.count(listening.frequency)}")
            else:
                self.emit(f"REJECTED {listening.frequency / 1e6:.6f} MHz | active "
                          f"{listening.active_seconds:.1f}s / {self.profile.listen_seconds:g}s")
        else:
            self.emit("Listen cancelled: VFO changed manually; not logged.")
        self.cooldown[listening.frequency] = now + self.profile.cooldown_seconds
        self.listening = None
        self.streaks.clear()
        if self.explorer is not None:
            self.explorer.after_listen()

    def _meter_tick(self, now) -> bool:
        """Sample Gqrx's meter for the tuned station; False when the tick must end."""
        self.next_meter = now + self.settings.meter_interval_s
        station = self.station
        update = None
        try:
            if self.control.frequency() != station.frequency:
                self.station = None
                return True
            try:
                level = self.control.strength()
            except GqrxProtocolError:
                if self.listening is not None:
                    self.listening.invalid_meter_readings += 1
                return True
            state = station.activity.state(now, self.settings.squelch.fft_guard_s)
            if self.listening is not None:
                self.listening.observe_meter(level, self.control.read_squelch(), state)
            update = station.learner.observe(level, state, now)
            if update is not None and self.listening is not None:
                # Audio is open only while listening; otherwise the value is kept for next time.
                actual = self.control.apply_squelch(update.requested_dbfs)
                station.learner.verified(actual)
                self.listening.record_squelch_change(now, update.requested_dbfs, actual, update.reason)
        except OSError:
            self._lost_control(now)
            return False
        if update is not None:
            if station.confirmed:
                self.store.remember_squelch(station.frequency, station.learner.record)
            record = station.learner.record
            noise = record.get("noise_floor_dbfs")
            label = "SQL GUARD" if update.reason == "carrier_guard" else "SQL LEARNED"
            self.emit(f"{label} {station.frequency / 1e6:.6f} MHz | noise "
                      f"{'unknown' if noise is None else f'{noise:.1f} dBFS'} | squelch "
                      f"{update.requested_dbfs:.1f} dBFS")
        return True

    def _select_candidate(self, peaks, frame, epoch, now):
        self.streaks = {peak.frequency_hz: self.streaks.get(peak.frequency_hz, 0) + 1 for peak in peaks}
        candidate = next((peak for peak in peaks
                          if self.streaks[peak.frequency_hz] >= self.profile.confirm_observations
                          and self.cooldown.get(peak.frequency_hz, 0) <= now), None)
        if candidate is None:
            return
        context = receiver_context(frame, self.profile)
        saved = self.store.squelch_record(candidate.frequency_hz, context)
        try:
            actual = self.control.tune(candidate.frequency_hz, saved["squelch_dbfs"] if saved else None)
            gains = self.control.gain_stages()
        except OSError:
            self._lost_control(now)
            return
        learner = SquelchLearner(context, actual, saved, settings=self.settings.squelch)
        learner.verified(actual)
        activity = FftActivity()
        activity.update(True, frame.received_at)
        self.station = Station(candidate.frequency_hz, learner, saved is not None, activity)
        self.next_meter = self.clock() + self.settings.first_meter_delay_s
        self.listening = ListeningWindow(
            candidate.frequency_hz, epoch, self.clock(), listen_seconds=self.profile.listen_seconds,
            min_active=self.profile.min_active_seconds, min_observed=self.profile.min_observed_seconds,
            mode=self.profile.mode, filter_hz=self.profile.filter_hz)
        self.listening.gain_stages_db = gains
        self.listening.scan_info = (self.explorer.scan_info() if self.explorer is not None
                                    else dict(FIXED_WINDOW_SCAN_INFO))
        self.emit(f"LISTEN {candidate.frequency_hz / 1e6:.6f} MHz | margin {candidate.margin_db:.1f} dB | "
                  f"{self.profile.listen_seconds:g}s | squelch {actual:.1f} dBFS verified | "
                  f"memory {'reapplied' if saved else 'new'}")
