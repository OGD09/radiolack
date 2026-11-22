// @ts-nocheck -- code from the mockup; it gets JSDoc types as it is rewritten.
/*
 * Radiolack web interface: spectrum and waterfall fed by the engine stream,
 * drawn with WebGL2, the VFO's audio, the menu, and the interactions around
 * them.
 */
import { EngineStream, FINE_COVER } from './stream.js';
import { AudioOutput } from './audio.js';
import { SpectrumView, MAX_MARKERS } from './spectrum-gl.js';
import { WaterfallView } from './waterfall-gl.js';
import { HIST_DB_MIN, HIST_DB_STEP, SpectrumHistory } from './history.js';
import { FINE_BINS, KEPT_SHARE, REMOTE_FINE_BINS, binPosition, coverBins, coveredShare, deepZoomed, fineCover, fineFrameFor,
  fineNarrowest, fineReceived, fitDeepView, readoutDecimals, resolutionNote, wideNarrowest } from './deepzoom.js';
import { loadBandPlan, planMode, rangeAt } from './bandplan.js';
import { antennaLengths, formatLength } from './antenna.js';
import { DIRECT_GAIN_NOTE, shortwaveView } from './shortwave.js';
import { POWER_CONFIRM_MS, powerPress, powerView } from './power.js';
import { OSCILLATOR_NOTE, conversionMoved, convertedFrequency, converterView, oscillatorChange } from './converter.js';
import { formatFrequency, parseFrequency, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ } from './frequency.js';
import { automaticStep } from './tuning.js';
import { PLAN_LIST_MAX, planRows } from './planlist.js';
import { modeAt, nameAt, snapToChannel, spotName, stepChannels, stepSetAt, stripHit, stripLayout } from './channels.js';
import { modeRateNote } from './rates.js';
import { tourNote } from './memorytour.js';
import { centreInUseNote, clearedCentre, driverPllView, gridStepHz } from './tunerpll.js';
import { SOAPY_KIND, deviceAt, devicesView, switchesAtOnce } from './devices.js';
import { RTL_LIST_MS, receiverList, rtlDeviceAt } from './rtldevices.js';
import { advancedView, nearestIndex, renderAdvanced } from './advanced.js';
import { CATEGORY_NAME_MAX, UNCATEGORIZED, bookmarkSections, checkedCategoryName, moveRequest, removeQuestion, sectionName,
  sectionScanChange } from './categories.js';
import { ppmText, typedPpm } from './correction.js';
import { MEASURE_EVERY_MS, calibrateEntry, calibrationView, driverCheckAgain, driverCheckText, driverCheckWanted,
  runView, startingTrueText, tunerNoteShown } from './calibration.js';
import { axisDecimals, bandsInView, beyondReachNote, planEnd, spanMarker, viewExtent, withinReach } from './view.js';

// ------------------------------------------------------------------ helpers
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
// Measurements, such as the signals found and the pointer's place, in MHz to
// a fixed number of decimals; exact frequencies take formatFrequency.
const fmtMHz = (hz, dec = 4) => (hz / 1e6).toFixed(dec);
const fmtBw = (hz) => (hz >= 1000 ? `${+(hz / 1000).toFixed(2)} kHz` : `${Math.round(hz)} Hz`);
const fmtDb = (db) => `${Math.round(db)} dB`;
const fmtAgo = (s) => (s < 60 ? `${Math.max(0, Math.round(s))} s` : `${Math.floor(s / 60)} min`);
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function onMediaChange(query, listener) {
  if (query.addEventListener) query.addEventListener('change', listener);
  else query.addListener(listener);   // Safari before 14
}

// ------------------------------------------------------------------ constants
const HIST_ROWS = 1400;             // waterfall lines kept for re-rendering, in each spectrum's history
const S_ZERO = -104;                // S-meter bar range (mock calibration: S9 = -50 dB)
const S_TOP = -10;
const METER_FLOOR = -140;           // reading when the VFO's channel lies outside the received band
const AXIS_H = 18;                  // frequency scale under the spectrum, CSS px (#spec-axis in style.css)
const PIXEL_BUDGET = 3.4e6;         // GPU drawing buffers WebKitGTK redraws at full rate (scoping.md, spike 1)
const SOURCE_NAMES = { synthetic: 'Synthetic source', rtl_sdr: 'RTL-SDR', rtl_tcp: 'rtl_tcp', soapy: 'SoapySDR' };
const FIRST_MANUAL_GAIN_DB = 30;    // offered when automatic gain is turned off before any manual gain
const DETENT_HOLD = 1.15;           // zooming out holds at the span's width until pushed this much further
const STRIP_SEGMENT_PX = 12;        // the band strip shows a band's ranges while they average this wide, CSS px
const RETUNE_SHARE = 1 / 32;        // a span that moves for the VFO puts it this share of its width above its centre,
const RETUNE_MARGIN_HZ = 5000;      // and this far at least past the channel's lower half: clear of the DC residue
const PENDING_FADE = 0.55;          // how much the span's data fades while the receiver moves to another span
const EDGE_SHARE = 0.1;             // share of the span by each edge that the receiver's filter dims: a step into it moves the span
// Each mode's filter width once tuned to a bookmark (bw) is also the engine's,
// for its memory scan (demod.py, MODE_BANDWIDTHS_HZ): change them together.
const MODES = {
  NFM: { bw: 10000, min: 4000, max: 25000, step: 500 },
  AM: { bw: 8000, min: 3000, max: 15000, step: 500 },
  SAM: { bw: 8000, min: 3000, max: 15000, step: 500 },     // synchronous AM, with AM's filter widths
  USB: { bw: 2700, min: 1500, max: 4000, step: 100 },
  LSB: { bw: 2700, min: 1500, max: 4000, step: 100 },
  CW: { bw: 500, min: 100, max: 2000, step: 50 },
  WFM: { bw: 180000, min: 50000, max: 250000, step: 10000 },
};
const MODE_ORDER = Object.keys(MODES);
const PALETTES = {
  classic: ['#000000', '#00007a', '#0050ff', '#00d0ff', '#40ff80', '#ffff00', '#ff7000', '#ff0000'],
  viridis: ['#440154', '#414487', '#2a788e', '#22a884', '#7ad151', '#fde725'],
  amber: ['#000000', '#2a1200', '#7a3600', '#d97b00', '#ffc04d', '#fff4d6'],
  ice: ['#000000', '#06142e', '#12407a', '#2a86c8', '#7fd3f5', '#f2fbff'],
  gray: ['#000000', '#ffffff'],
};
const DEFAULT_BAND_PLAN = 'europe';     // the plan of an engine that offers no choice
const SQL_WHEEL_PX = 50;            // wheel travel over the S-meter that moves the squelch by 1 dB, as one notch does

// ------------------------------------------------------------------ DOM
const appEl = $('#app');
const specWrap = $('#specwrap');
const wfWrap = $('#wfwrap');
const bandEl = $('#band');
const specCanvas = $('#spec');
const wfCanvas = $('#wf');
const bandCanvas = $('#bandc');
const axisCanvas = $('#spec-axis');
const dbCanvas = $('#spec-db');
const bandCtx = bandCanvas.getContext('2d');
const axisCtx = axisCanvas.getContext('2d');
const dbCtx = dbCanvas.getContext('2d');
const stampsEl = $('#wf-stamps');
const vfoEl = $('#vfo');
const vfoLine = $('#vfo-line');
const xline = $('#xline');
const vfoReturnEl = $('#vfo-return');
const spanFitEl = $('#span-fit');
const tuneTipEl = $('#tune-tip');
const showNextSpan = outline($('#span-next'));       // the span a retune on its way brings
const showPreviewSpan = outline($('#span-preview')); // the span a click would bring
const marksEl = $('#marks');
const freqEl = $('#freq');
const meterFill = $('#smeter .fill');
const meterSql = $('#smeter .sql');
const meterBar = $('#smeter .bar');
const menuEl = $('#menu');
const menuButton = $('#menu-button');
const menuScrim = $('#menu-scrim');
// The menus, in the order of the keys 1 to 8.
const MENUS = $$('#menu nav button[data-menu]').map((b) => b.dataset.menu);
const ctxEl = $('#ctx');
const hoverEl = $('#hoverinfo');
const hintEl = $('#hint');
const noticeEl = $('#notice');
const readoutEl = $('#readout');
const antennaEl = $('#antenna');
const antennaDialog = $('#antenna-dialog');
const calibrateDialog = $('#calibrate-dialog');
const reportDialog = $('#report-dialog');
const legalDialog = $('#legal-dialog');
const confirmDialog = $('#confirm-dialog');
const coarseQuery = window.matchMedia('(pointer: coarse)');   // primary input is a finger
const oneColumnQuery = window.matchMedia('(max-width: 600px)'); // the menu shows its list or a menu (style.css)

// ------------------------------------------------------------------ state
const S = {
  fc: 145.0e6, fs: 2.4e6, nb: 8192,            // the engine's capture, updated by its frames
  vfo: 145.5e6, mode: 'NFM', bw: 10000, autoMode: true, step: 'auto',
  avg: 0.6, fill: true, peakHold: false, grid: true, autoRange: true, top: -30, range: 80,
  palette: 'classic', wfAuto: true, wfMin: -101, wfMax: -57, wfSpeed: 25, timestamps: true,
  showPlan: true, showMarks: true, showChannels: true, split: 0.4,
  sql: -80, sqlAuto: true, notch: false, blanker: false, volume: 70,    // notch, blanker: the engine's, null from an engine without them
  peakMarks: true,
  autoHide: true,
};
const view = { f0: 0, f1: 0 };
let plan = { name: '', ranges: [], bands: [], channels: [], spots: [] };    // band plan, empty until loaded
let planState = 'loading';              // 'loading', 'ready' or 'failed'
let planId = null;                      // the band plan loaded or loading, which the engine names
let bookmarks = [];         // the engine's, in frequency order: { id, f, name, mode, scan, category }
let categories = [];        // the engine's, by name: { id, name }
let categoriesKnown = false;  // whether the engine that runs keeps categories
let namingBookmark = null;  // { name, f, before }: the bookmark Add sent, until the engine's list brings it

const fStart = () => S.fc - S.fs / 2;
const fEnd = () => S.fc + S.fs / 2;
const binHz = () => S.fs / S.nb;
const segmentAt = (f) => rangeAt(plan.ranges, f);
const bandAt = (f) => rangeAt(plan.bands, f);
// Strip tooltip: a segment of a band also names its band.
const rangeTitle = (r) => (r.kind === 'segment' ? `${r.band} · ${r.name}` : r.name);
// The channel set the Automatic step follows at f: the set beside f's
// channels, where the view shows two of them or more (channels.js).
function stepSet(f) {
  if (S.step !== 'auto') return null;
  const set = stepSetAt(plan.channels, f);
  return set && view.f1 - view.f0 >= 2 * set.spacing ? set : null;
}
// A fixed step applies whatever the zoom; Automatic follows the view's
// width, and the channel spacing of f's set or range where the view shows
// two channels or more.
function stepAt(f) {
  if (S.step !== 'auto') return Number(S.step);
  const set = stepSet(f);
  if (set) return set.spacing;
  const seg = segmentAt(f);
  return automaticStep(view.f1 - view.f0, seg ? seg.step : null);
}
// Where a click on f tunes: a set's nearest channel, else the nearest multiple of the step.
function snapAt(f) {
  const set = stepSet(f);
  if (set) return snapToChannel(set, f);
  const st = stepAt(f);
  return Math.round(f / st) * st;
}
// Where the VFO shows: where a retune on its way puts it, else where it is.
const shownVfo = () => (pendingTune ? pendingTune.f : S.vfo);
function passband(f = S.vfo) {
  const bw = S.bw;
  if (S.mode === 'USB') return [f, f + bw];
  if (S.mode === 'LSB') return [f - bw, f];
  return [f - bw / 2, f + bw / 2];
}

// ------------------------------------------------------------------ buffers
// Each spectrum keeps its frames, its trace and its waterfall's lines at its
// own resolution (history.js): the received span's, and the fine spectrum of
// the VFO's channel while the deep zoom asks for it (deepzoom.js). The view
// shows one, `shown`; the wide one goes on taking its frames meanwhile.
const wide = new SpectrumHistory(HIST_ROWS);
const fine = new SpectrumHistory(HIST_ROWS);
const WIDE_LAYER = 0, FINE_LAYER = 1;   // their histories in the waterfall view
let shown = wide;
let autoTop = -30, meterDb = METER_FLOOR, sqlOpen = false;
let meterSqlDb = null;              // the squelch threshold in use, from the frames; null while unknown
const autoWf = { min: -101, max: -57 };
const floorSample = new Float32Array(1024);
let peaks = [];                     // the signals the engine found (stream.js Peak), as last received
let peaksAt = 0;                    // performance.now() when they came: their ages count on from there
let peaksVersion = 0;               // lists received, for the spectrum's marks
let DPR = 1, GLS = 1, appW = 0, specH = 0, wfH = 0, layoutPending = false;
let specVals = null, peakVals = null, specColumns = null;
const markerCorners = new Float32Array(MAX_MARKERS * 6);
let lutDirty = true;

// New capture (centre, width or FFT size): every buffer and the history start again.
function resetBuffers() {
  wide.reset(S.fc, S.fs, S.nb);
  if (waterfallView) waterfallView.setBins(S.nb, WIDE_LAYER);
  if (shown === wide) clearStamps();
}
// The same receiver's span moved by less than its width: the waterfall's
// lines and the peak hold keep their frequencies, what left the span goes and
// the new part starts empty. The next frame starts the averages again.
function slideBuffers() {
  wide.slide(S.fc);
  if (waterfallView) waterfallView.setOffsets(wide.lineOffsets(), WIDE_LAYER);
}

// ------------------------------------------------------------------ deep zoom
// Below the wide spectrum's limit, within the fine spectrum's cover, the view
// asks the engine for the fine frames and shows them once they come, in the
// same place (deepzoom.js). Until the first comes, about 0.17 s later, and
// while those it holds cover less than half the view, the wide spectrum
// shows: never a fine frame where it did not come from.
let deepZoom = false;       // the view is deep enough for the fine spectrum, with its hysteresis
let fineAsked = false;      // this interface asks the engine for the fine frames
let fineFresh = false;      // a fine frame came since it asked
let fineWindow = [0, 0];    // the fine frames' bins that show the air (deepzoom.js coverBins)
const fineLo = () => fine.centreHz - FINE_COVER * fine.widthHz;
const fineHi = () => fine.centreHz + FINE_COVER * fine.widthHz;
// What the shown spectrum measures: the span, or the fine spectrum's cover.
const shownLo = () => (shown === fine ? fineLo() : fStart());
const shownHi = () => (shown === fine ? fineHi() : fEnd());
// The fine spectrum's cover and narrowest view while the engine would send it for the VFO, else null.
function deepLimits() {
  const known = stream.spectra.includes('fine');
  if (!fineReceived({ known, vfoHz: S.vfo, mode: S.mode, span: [fStart(), fEnd()] })) return null;
  const { centreHz, widthHz } = fineFrameFor(S.vfo, S.mode);
  return { cover: fineCover(centreHz, widthHz), narrowestHz: fineNarrowest(widthHz, stream.remote ? REMOTE_FINE_BINS : FINE_BINS) };
}
// Whether the view is deep, and whether to ask: not while the page is hidden, where nobody sees.
function syncDeep() {
  const limits = deepLimits();
  deepZoom = limits !== null && deepZoomed(deepZoom, [view.f0, view.f1], limits.cover, binHz());
  const ask = deepZoom && !document.hidden;
  if (ask !== fineAsked) {
    fineAsked = ask;
    // Each asking starts an empty fine history: lines from an earlier one would
    // stand right under the new ones, with the time between them unseen.
    if (ask) fine.clear();
    fineFresh = false;
    stream.setFine(ask);
  }
  updateShown();
}
// Which spectrum shows: on a switch, the waterfall's timestamps, Display's
// line and the automatic scales follow it.
function updateShown() {
  const next = fineAsked && fineFresh && fine.bins
    && coveredShare([view.f0, view.f1], [fineLo(), fineHi()]) >= KEPT_SHARE ? fine : wide;
  if (next !== shown) {
    shown = next;
    rebuildStamps();
    if (shown.levelsPrimed) primeScales(shown);
  }
  syncResolution();
}
function onFine(frame) {
  if (!fineAsked) return;
  const n = frame.codes.length;
  if (!fine.slides(frame.centreHz, frame.widthHz, n)) {
    fine.reset(frame.centreHz, frame.widthHz, n);
    fineWindow = coverBins(n);
    if (waterfallView) waterfallView.setBins(n, FINE_LAYER);
    if (shown === fine) clearStamps();
  } else if (frame.centreHz !== fine.centreHz) {
    fine.slide(frame.centreHz);   // the VFO moved by less than the channel: the lines keep their frequencies
    if (waterfallView) waterfallView.setOffsets(fine.lineOffsets(), FINE_LAYER);
  }
  fine.add(frame.codes, frame.dbMin, frame.dbStep, S.avg, performance.now());
  fineFresh = true;
  estimateFloor(fine);
  updateShown();
}
// Forget the fine frames, which the next ones replace: another receiver, or a stream that dropped.
function clearFine() {
  fine.clear();
  fineFresh = false;
  updateShown();
}
let resolutionShown = '';
function syncResolution() {
  const text = resolutionNote(shown === fine, shown.binHz || binHz());
  if (text === resolutionShown) return;
  resolutionShown = text;
  $('#ds-resolution').textContent = text;
}

// ------------------------------------------------------------------ engine stream
// The stream closes while the page is hidden, unless the audio plays, and
// the engine idles without clients.
let streamState = 'connecting', frameRate = 0, rateFrames = 0, rateSince = 0;
let streamOpened = false;   // the stream opened at least once on this page
let source = null;          // the engine's source state (stream.js SourceState), once received
let sourceError = '';       // why the engine refused this interface's last change
const typingIn = new Set(); // text fields the user is typing in, which the engine's state leaves as they are
let devices = null;         // the SoapySDR devices (stream.js DevicesState), once received: sources that open them
let soapyPicked = false;    // "SoapySDR device" picked in the Receiver list, no device in use: its part shows
let firstSearchAsked = false;   // this connection asked for the first search of SoapySDR devices
let rtlListed = null;       // the RTL-SDR receivers plugged in (stream.js RtlDevicesState), as the engine answered last
let rtlTimer = 0;           // asks for them again while the Receiver menu shows
let demodError = '';        // why the engine refused this interface's last demodulation change
let bookmarkNote = '';      // why this interface's last bookmark change did not happen
let scanner = null;         // the engine's signal detection (stream.js ScannerState), once received
let scannerNote = '';       // why this interface's last scanner change did not happen
let recording = null;       // the engine's recordings (stream.js RecordingState), once received
let calibration = null;     // the engine's calibration on the stations (stream.js CalibrationState), once received
const calibrationRuns = () => calibration !== null && calibration.running;
let storage = null;         // where the engine saves the settings (stream.js StorageState), once received
let update = null;          // what the engine's update check found (stream.js UpdateState), once received
let updateNote = '';        // why this interface's last update request did not reach the engine
let noticeClosed = '';      // the update notice closed on this page: its status and version
let remoteState = null;     // remote access (stream.js RemoteState), once received: this computer's interfaces only
let remoteNote = '';        // why this interface's last remote access change did not happen
let audioModes = ['NFM'];   // modes the engine demodulates to audio, from its settings
let samLocked = null;       // in SAM, whether the engine holds the carrier, from its settings; null otherwise
let notchLines = null;      // while the engine's notch acts, the lines it removes now; null otherwise
let blankerImpulses = null; // while the engine's blanker is on, the impulses it zeroed in the latest second; null otherwise
let audioState = 'off';     // audio.js AudioState
let audioWanted = false;    // the engine sends this interface audio
let pendingTune = null;     // { centre, before, f, opts, inUse }: where the VFO goes once frames come from
                            // the centre in use for centre, inUse once the engine reported it; before lists
                            // the centres the engine may report on the way
let newSource = false;      // the next frames come from another receiver, or another converter: every buffer starts again

// The wide frames go on feeding the wide spectrum and its waterfall while the
// fine spectrum shows, so that zooming out shows no hole; the S-meter and the
// squelch come from them.
function onFrame(frame) {
  const codes = frame.codes, nb = codes.length;
  if (newSource || frame.centreHz !== S.fc || frame.rateHz !== S.fs || nb !== S.nb) setCapture(frame.centreHz, frame.rateHz, nb);
  wide.add(codes, frame.dbMin, frame.dbStep, S.avg, performance.now());
  updateMeter(frame);
  if (wide.frames % 3 === 0) estimateFloor(wide);
  autoAdjust();
  countFrame(performance.now());
}

// The engine changed its capture: a view that framed the span frames the new
// one, another keeps its frequencies within its new limits. The VFO goes
// where a retune asked for it, or stays inside the new span. A span that
// moved by less than its width keeps what it shows.
function setCapture(centre, rate, bins) {
  const framed = viewFramesSpan(), from = S.fc;
  const slides = !newSource && rate === S.fs && bins === S.nb && Math.abs(centre - from) < rate;
  newSource = false;
  S.fc = centre;
  S.fs = rate;
  S.nb = bins;
  if (slides) slideBuffers(); else resetBuffers();
  if (framed) setView(fStart(), fEnd()); else setView(view.f0, view.f1);
  const pending = pendingTune;
  if (pending && pendingCentre() === centre) {
    pendingTune = null;
    tune(pending.f, pending.opts);
  } else {
    showVfo();          // the engine keeps the VFO inside the new span
  }
  syncSource();
  syncDisplay();
  renderPlanList();
  syncAudioNote();      // a mode may need another sample rate
  updateStatus();
}

// Frames received per second, measured over about one second.
function countFrame(now) {
  if (!rateSince) { rateSince = now; rateFrames = 0; return; }
  rateFrames += 1;
  if (now - rateSince < 1000) return;
  const rate = Math.round((rateFrames * 1000) / (now - rateSince));
  rateSince = now;
  rateFrames = 0;
  if (rate !== frameRate) { frameRate = rate; updateStatus(); }
}

const audio = new AudioOutput((state) => {
  audioState = state;
  syncAudio();
});
const stream = new EngineStream({
  onFrame,
  onAudio: (frame) => audio.play(frame),
  onFine,
  onSource: (state) => {
    const limits = viewLimits(), before = source;
    if (source && (state.kind !== source.kind || state.address !== source.address)) newSource = true;
    // Another converter: the receiver stays where it is tuned, and every
    // frequency moves. The buffers start again, and the view moves with the
    // signals it shows.
    const converted = before !== null && !newSource && conversionMoved(before, state);
    if (converted) newSource = true;
    if (newSource) clearFine();
    if (!before || state.kind !== before.kind) soapyPicked = false;     // another kind in use, from any interface
    // Another receiver, from any interface: the open Receiver menu's list says so at once.
    if (rtlTimer && before && (state.kind !== before.kind || state.status !== before.status)) stream.listRtlDevices();
    source = state;
    sourceError = '';
    // A centre that is neither the requested one nor one reported before it: another change won.
    if (pendingTune && state.centreHz !== pendingTune.centre && !pendingTune.before.includes(state.centreHz)) dropPendingTune();
    else if (pendingTune && state.centreHz === pendingTune.centre && !converted) pendingPlaced(state);
    if (converted) {
      const f0 = convertedFrequency(view.f0, before, state), f1 = convertedFrequency(view.f1, before, state);
      setView(Math.min(f0, f1), Math.max(f0, f1));
    } else if (viewLimits() !== limits) {
      setView(view.f0, view.f1);      // the view's limits follow the source's
    }
    syncSource();
    syncCalibration();    // a measure taken before the change computes nothing
    syncSweep();          // whether the receiver tunes, as the sweep needs,
    syncMem();            // and as the memory scan's list tells
    syncRecording();      // the simulated band is not recorded
    updateStatus();
    if (historyShown()) syncHistorySummary();
  },
  onDemod: applyDemod,
  onBandPlan: (id) => {
    if (id !== planId) {
      if (planId !== null && historyShown()) askHistory();     // the History's channels are the new plan's
      loadPlan(id);
    }
    syncPlanChoice();
  },
  onBookmarks: (list, categoryList) => {
    bookmarks = list.map((b) => ({ id: b.id, f: b.frequencyHz, name: b.name, mode: b.mode, scan: b.scan, category: b.category }));
    categories = categoryList ?? [];
    categoriesKnown = categoryList !== null;
    nameNewBookmark();
    bookmarksChanged();
  },
  onScanner: (state) => {
    // The scans' progress changes the state far more often than the settings
    // do: the controls follow only a change of the settings or of the scan
    // that runs, which an interface made.
    const changed = scanner === null || scannerSettings(scanner) !== scannerSettings(state);
    scanner = state;
    if (!changed) { syncMem(); syncSweep(); return; }
    scannerNote = '';
    syncScanner();
  },
  onPeaks: (list) => {
    peaks = list;
    peaksAt = performance.now();
    peaksVersion += 1;
    if (signalsShown()) renderPeaks();
  },
  onObservations: (list) => {
    observations = list;
    // A channel that went, cleared by another interface, has nothing left to confirm.
    if (typeof historyArmed === 'number' && !list.some((c) => c.frequencyHz === historyArmed)) historyArmed = null;
    if (historyShown()) renderHistory();
  },
  onScans: (list) => {
    keptScans = list;
    const open = openScanId === null ? undefined : list.find((s) => s.id === openScanId);
    if (openScanId !== null && !open) closeScan();      // deleted, here or by another interface
    else if (open && openScan && JSON.stringify(open) !== JSON.stringify(openScan.scan)) stream.askScan(open.id);
    if (typeof scansArmed === 'number' && !list.some((s) => s.id === scansArmed)) scansArmed = null;
    if (rangeShown()) renderScans();
  },
  onScan: (kept) => {
    if (kept.scan.id !== openScanId) return;
    openScan = kept;
    if (rangeShown()) renderScans();
  },
  onPreferences: applyPreferences,
  onRecording: (state) => {
    const status = recordingStatus();
    if (recording && recording.folder !== state.folder) folderNote = '';     // another folder took the refused one's place
    recording = state;
    syncRecording();
    renderCalibration();      // a recording keeps the calibration on the stations from starting
    if (recordingStatus() !== status) updateStatus();
    // A file that came or went, or another folder: the list changed.
    if (recordShown() && recordingKeyNow() !== recordingsKey) askRecordings();
  },
  onRecordings: (list) => {
    recordingList = list;
    recordingsStale = false;
    // A recording that went, deleted by another interface, has nothing left to confirm.
    if (recArmed !== null && !list.files.some((f) => f.name === recArmed)) recArmed = null;
    if (recordShown()) renderRecordings();
  },
  onStorage: (state) => {
    storage = state;
    syncStorage();
    updateStatus();
  },
  onUpdate: (state) => {
    update = state;
    updateNote = '';
    syncUpdate();
  },
  onRemote: (state) => {
    remoteState = state;
    remoteNote = '';
    syncRemote();
    updateStatus();
  },
  onCalibration: (state) => {
    calibration = state;
    runError = '';            // a run that started or ended, from any interface, says more than a refusal before
    syncSource();             // the Receiver menu says while it runs
    syncCalibration();        // the true frequency follows the VFO again once it ends
  },
  onDevices: (state) => {
    devices = state;
    syncSource();
  },
  onRtlDevices: (state) => {
    rtlListed = state;
    syncSource();
  },
  onReport: (report) => {
    $('#ab-report-msg').textContent = '';
    $('#report-msg').textContent = '';
    const text = $('#report-text');
    text.value = report.text;
    text.scrollTop = 0;
    $('#report-issue').href = report.issueUrl;
    if (!reportDialog.open) openReportDialog();
  },
  onCarrier: (measure) => {
    // Only the latest request's answer: an older one may predate a change.
    if (!calibrateDialog.open || measure.id !== carrierId) return;
    carrierMeasure = measure;
    carrierError = '';
    // A steady carrier: the driver's check, if due, runs on it.
    if (measure.frequencyHz !== null && driverCheck.state === 'due' && driverCheckWanted(source, stream.measures)) {
      driverCheckId += 1;
      if (stream.identifyDriver(driverCheckId)) driverCheck = { state: 'running', answer: null };
    }
    renderCalibration();
    clearTimeout(carrierTimer);
    carrierTimer = setTimeout(askCarrier, Math.max(0, MEASURE_EVERY_MS - (performance.now() - carrierAsked)));
  },
  onDriverCheck: (check) => {
    if (!calibrateDialog.open || check.id !== driverCheckId) return;
    // Stopped by a change or the calibration on the stations: checked again on the next carrier measured.
    driverCheck = driverCheckAgain(check.reason) ? { state: 'due', answer: null } : { state: 'done', answer: check };
    // No measure asked before the check's end computes a correction.
    carrierMeasure = null;
    askCarrier();
    renderCalibration();
  },
  onError: (message, command) => {
    if (command === 'demod' || command === 'audio') {
      demodError = message;     // the engine's settings came back before the error
      syncAudioNote();
      return;
    }
    if (command === 'bookmark') {
      namingBookmark = null;    // a refused bookmark never comes
      bookmarkNote = `Change refused: ${message}.`;
      syncBookmarkNote();
      return;
    }
    if (command === 'scanner') {
      scannerNote = `Change refused: ${message}.`;
      syncScanner();        // the controls show the engine's settings again
      return;
    }
    if (command === 'observations') {
      setHistoryNote(`Refused: ${message}.`);
      return;
    }
    if (command === 'scans') {
      setScansNote(`Refused: ${message}.`);
      return;
    }
    if (command === 'preferences') {
      console.error('Radiolack: the engine did not keep the preferences:', message);
      return;
    }
    if (command === 'recording' && recordingAsk === 'folder') {
      folderNote = `Refused: ${message}.`;
      syncRecordingFolder();
      return;
    }
    if (command === 'recording' || command === 'recordings') {
      setRecordNote(`Refused: ${message}.`);
      return;
    }
    if (command === 'update') {
      updateNote = `Refused: ${message}.`;
      syncUpdate();
      return;
    }
    if (command === 'remote') {
      remoteNote = `Refused: ${message}.`;
      syncRemote();
      return;
    }
    if (command === 'report') {
      $('#ab-report-msg').textContent = `Refused: ${message}.`;
      return;
    }
    if (command === 'carrier') {
      carrierError = `Refused: ${message}.`;
      renderCalibration();
      return;
    }
    if (command === 'calibration') {
      runError = `Refused: ${message}.`;
      renderCalibration();
      return;
    }
    if (command === 'devices') {
      sourceError = message;    // shown under the Receiver list, as the source's refusals
      syncSource();
      return;
    }
    // A correction applied from the calibration may be refused too.
    if (command === 'source' && calibrateDialog.open) {
      if (appliedFrom === 'stations') runError = `Refused: ${message}.`;
      else carrierError = `Refused: ${message}.`;
      renderCalibration();
    }
    sourceError = message;
    dropPendingTune();
    syncSource();       // the controls show the engine's settings again
  },
  onState: (state) => {
    // The computer forgot this device: the server now serves the pairing page.
    // A refusal before the stream ever opened would come back at each reload.
    if (state === 'unpaired' && streamOpened) { location.reload(); return; }
    if (state === 'open') streamOpened = true;
    if (state === 'open') firstSearchAsked = false;     // asked again if the engine has still not searched
    if (state !== 'open') rtlListed = null;     // the engine may be another once the stream opens again
    streamState = state;
    followRtlList();
    if (state !== 'open') {
      rateSince = 0; frameRate = 0; audio.reset();
      clearFine();              // asked again once the stream opens, the fine history starting empty
      // The engine's state after the next connection tells what became of a retune on its way.
      if (pendingTune) pendingTune.before = [];
      namingBookmark = null;    // a bookmark added before comes back as any other
    }
    if (state === 'open') {
      refitView();              // the engine's hello may list the fine spectrum, or no longer
      $('#st-remote-client').hidden = !stream.remote;
      $('#ab-remote-client').hidden = !stream.remote;
      $('#ab-report-section').hidden = stream.remote;     // the engine reports to this computer's interfaces only
      syncVersion();
      if (!stream.bandPlans.length && planId === null) loadPlan(DEFAULT_BAND_PLAN);     // an engine without the choice
    }
    syncPlanChoice();     // greyed out until the stream opens again
    syncSource();
    syncScanner();
    syncRecording();
    updateStatus();
    if (state === 'open' && historyShown()) askHistory();
    if (state === 'open') {
      calibration = null;   // the engine sends its own next, if it calibrates
      carrierKey = '';      // a new connection: the measure is asked again
      // The engine stopped the check of a stream that dropped: it runs again.
      if (driverCheck.state === 'running') driverCheck = { state: 'due', answer: null };
    }
    syncCalibration();
    if (state === 'open' && recordShown()) askRecordings();
  },
});
function syncStream() { if (document.hidden && !audioWanted) stream.stop(); else stream.start(); }

// The engine sends audio while this interface can play it with the volume up.
function syncAudio() {
  const wanted = audioState === 'running' && S.volume > 0;
  if (wanted !== audioWanted) {
    audioWanted = wanted;
    if (!wanted) audio.reset();
    stream.setAudio(wanted);
    syncStream();
  }
  syncAudioNote();
  syncSound();
}

// The speaker, for touch input: crossed out while this interface plays no sound, because the
// browser holds the audio until a tap (Safari, at each page load; a tap on the speaker is one,
// audio.js) or the volume is 0. A tap mutes by setting the volume to 0, as the Radio menu's
// slider would, so that the engine stops sending audio, and unmutes by bringing it back.
const soundEl = $('#sound');
const DEFAULT_VOLUME = S.volume;    // brought back when the speaker did not mute
let volumeBeforeMute = 0;
let soundAtPress = null;            // whether sound played as the latest press on the speaker began
function syncSound() {
  soundEl.setAttribute('aria-pressed', String(audioWanted));
  soundEl.hidden = audioState === 'failed';
}
function setVolume(volume) {
  const input = rangeInputs.get('volume');
  input.value = String(volume);
  input.dispatchEvent(new Event('input'));    // as the slider would: the setting, its label, the audio
}
// The press itself may start the audio, before the click: the click acts on what played before.
soundEl.addEventListener('pointerdown', () => { soundAtPress = audioWanted; });
soundEl.addEventListener('pointercancel', () => { soundAtPress = null; });    // a scroll: no click follows
soundEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') soundAtPress = audioWanted; });
soundEl.addEventListener('click', () => {
  const sounding = soundAtPress ?? audioWanted;
  soundAtPress = null;
  if (sounding) {
    volumeBeforeMute = S.volume;
    setVolume(0);
  } else if (S.volume === 0) {
    setVolume(volumeBeforeMute || DEFAULT_VOLUME);
  }   // otherwise the browser held the audio, and the press asked it to start
});

// The engine measures the VFO's channel: the meter follows its level, fast
// up and slower down, and shows its squelch.
function updateMeter(frame) {
  const db = frame.levelDb === null ? METER_FLOOR : Math.max(METER_FLOOR, frame.levelDb);
  meterDb += (db - meterDb) * (db > meterDb ? 0.6 : 0.15);
  meterSqlDb = frame.squelchDb;
  sqlOpen = frame.squelchOpen;
}

// Noise floor of a spectrum: median level of its central bins, within the
// fine spectrum's cover too. The first estimate of the spectrum shown also
// places the automatic scales, so they need not slide there from afar, and
// so does a switch to the other spectrum: a fine bin holds less noise.
function estimateFloor(h) {
  const nb = h.bins, from = Math.floor(nb * 0.15), to = Math.floor(nb * 0.85);
  const count = Math.min(floorSample.length, to - from), stride = (to - from) / count;
  for (let k = 0; k < count; k += 1) floorSample[k] = h.avgDb[from + Math.floor(k * stride)];
  const median = floorSample.subarray(0, count).sort()[count >> 1];
  if (h.levelsPrimed) {
    h.floorDb += (median - h.floorDb) * 0.2;
    return;
  }
  h.levelsPrimed = true;
  h.floorDb = median;
  if (h === shown) primeScales(h);
}
function primeScales(h) {
  autoTop = h.floorDb - 12 + S.range;
  autoWf.min = h.floorDb - 4;
  autoWf.max = h.floorDb + 40;
  lutDirty = true;
}

// Automatic scales follow the floor of the spectrum shown, once per frame.
function autoAdjust() {
  const floorDb = shown.floorDb;
  if (S.autoRange) autoTop += (floorDb - 12 + S.range - autoTop) * 0.1;
  if (S.wfAuto) {
    const lo = floorDb - 4, hi = floorDb + 40;
    if (Math.abs(lo - autoWf.min) > 0.3 || Math.abs(hi - autoWf.max) > 0.3) {
      autoWf.min += (lo - autoWf.min) * 0.1;
      autoWf.max += (hi - autoWf.max) * 0.1;
      lutDirty = true;
    }
  }
}

// ------------------------------------------------------------------ waterfall
const paletteCache = {};
function paletteRgb(name) {
  if (paletteCache[name]) return paletteCache[name];
  const stops = PALETTES[name].map((h) => [1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16)));
  const out = new Uint8Array(768);
  for (let i = 0; i < 256; i += 1) {
    const x = (i / 255) * (stops.length - 1);
    const k = Math.min(stops.length - 2, Math.floor(x)), fr = x - k;
    for (let c = 0; c < 3; c += 1) out[i * 3 + c] = Math.round(stops[k][c] + (stops[k + 1][c] - stops[k][c]) * fr);
  }
  paletteCache[name] = out;
  return out;
}
const lutBytes = new Uint8Array(256 * 4);       // colour of each history code, RGBA
let lutVersion = 0;
const wfLo = () => (S.wfAuto ? autoWf.min : S.wfMin);
const wfHi = () => (S.wfAuto ? autoWf.max : S.wfMax);
function rebuildLut() {
  const pal = paletteRgb(S.palette), lo = wfLo(), span = Math.max(1, wfHi() - lo);
  for (let q = 0; q < 256; q += 1) {
    const idx = clamp(Math.round(((HIST_DB_MIN + q * HIST_DB_STEP - lo) / span) * 255), 0, 255) * 3;
    lutBytes[q * 4] = pal[idx];
    lutBytes[q * 4 + 1] = pal[idx + 1];
    lutBytes[q * 4 + 2] = pal[idx + 2];
    lutBytes[q * 4 + 3] = 255;
  }
  if (waterfallView) waterfallView.setColours(lutBytes);
  lutVersion += 1;
}

// Timestamps every 10 s: text elements over the waterfall, moved down one
// CSS px per line (one line per CSS px row), from the history shown.
const STAMP_MS = 10000;
const stamps = [];                  // { el, row }, oldest first
let lastStampSlot = -1;
const ageOf = (row) => (shown.head - row + HIST_ROWS) % HIST_ROWS;
const stampLimit = () => Math.min(wfH, shown.filled, HIST_ROWS - 1);
function addStamp(row) {
  const el = document.createElement('div');
  el.className = 'wf-stamp';
  el.textContent = new Date(shown.histTime[row]).toTimeString().slice(0, 8);
  el.style.transform = `translateY(${ageOf(row)}px)`;
  stampsEl.appendChild(el);
  stamps.push({ el, row });
}
function clearStamps() {
  stamps.length = 0;
  stampsEl.replaceChildren();
}
function advanceStamps() {
  const limit = stampLimit();
  for (let i = stamps.length - 1; i >= 0; i -= 1) {
    const { el, row } = stamps[i];
    const age = ageOf(row);
    if (age >= limit) { el.remove(); stamps.splice(i, 1); } else el.style.transform = `translateY(${age}px)`;
  }
}
// From the history: a stamp on the first line of each 10 s slot.
function rebuildStamps() {
  clearStamps();
  if (!S.timestamps) return;
  const h = shown;
  for (let age = stampLimit() - 2; age >= 0; age -= 1) {
    const row = (h.head - age + HIST_ROWS) % HIST_ROWS, older = (row - 1 + HIST_ROWS) % HIST_ROWS;
    if (Math.floor(h.histTime[row] / STAMP_MS) !== Math.floor(h.histTime[older] / STAMP_MS)) addStamp(row);
  }
  if (h.filled) lastStampSlot = Math.floor(h.histTime[h.head] / STAMP_MS);
}

// Waterfall lines: each spectrum makes its own, at the waterfall's speed, from
// the frames since its previous line (history.js). Frames may come slower than
// lines, as to remote devices and as the fine frames do: a line then repeats
// the latest frame, while frames still come, so that the waterfall keeps its
// speed. The fine frames come about ten times a second: a line repeats one
// for a little more than their interval, and once they stop, as while the VFO
// moves at every block, the fine history waits rather than stretch.
const LINE_REPEAT_MS = 250;
const FINE_LINE_REPEAT_MS = 150;
function commitLines(now) {
  const nowMs = performance.now();
  commitLine(wide, WIDE_LAYER, now, nowMs, LINE_REPEAT_MS);
  commitLine(fine, FINE_LAYER, now, nowMs, FINE_LINE_REPEAT_MS);
}
function commitLine(h, layer, now, nowMs, repeatMs) {
  if (!h.commitLine(now, nowMs, repeatMs)) return;
  if (waterfallView) waterfallView.uploadRow(h.head, h.line(h.head), layer);
  if (h !== shown) return;
  const slot = Math.floor(now / STAMP_MS);
  if (S.timestamps) {
    advanceStamps();
    if (slot !== lastStampSlot) addStamp(h.head);
  }
  lastStampSlot = slot;
}

let wfKey = '';                     // what the waterfall shows: drawn again only when it changes
function renderWaterfall() {
  if (!waterfallView) return;
  const h = shown, W = waterfallView.width, finer = h === fine;
  const toX = (f) => ((f - view.f0) / (view.f1 - view.f0)) * W;
  const marker = spanMarker(toX(shownLo()), toX(shownHi()), GLS);
  const key = `${finer} ${h.head} ${h.filled} ${view.f0} ${view.f1} ${W} ${waterfallView.height} ${lutVersion} ${h.bins} ${h.centreHz}`
    + ` ${h.widthHz} ${marker}`;
  if (key === wfKey) return;
  wfKey = key;
  // Bin i spans [i, i + 1) for the view: its middle lies at its centre's frequency.
  waterfallView.draw({
    head: Math.max(0, h.head), lines: h.filled, layer: finer ? FINE_LAYER : WIDE_LAYER, window: finer ? fineWindow : null,
    firstBin: binPosition(view.f0, h.centreHz, h.widthHz, h.bins) + 0.5, binsPerPixel: (view.f1 - view.f0) / h.binHz / W, marker,
  });
}

// ------------------------------------------------------------------ spectrum
function niceStep(x) {
  const p = 10 ** Math.floor(Math.log10(x));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= x) return m * p;
  return 10 * p;
}

// The levels of a spectrum's bins under each column of the view: the highest
// of those a column covers, or between the two nearest bins' centres when a
// bin spans several columns. Bin i spans [i, i + 1) here.
function sample(h, src, out) {
  const W = out.length, nb = h.bins, span = view.f1 - view.f0;
  const binsPerPx = span / h.binHz / W, first = binPosition(view.f0, h.centreHz, h.widthHz, nb) + 0.5;
  for (let x = 0; x < W; x += 1) {
    const ba = first + x * binsPerPx;
    const bb = ba + binsPerPx;
    if (binsPerPx >= 1) {
      let m = -300;
      for (let i = Math.max(0, Math.floor(ba)), e = Math.min(nb, Math.ceil(bb)); i < e; i += 1) if (src[i] > m) m = src[i];
      out[x] = m;
    } else {
      const pos = (ba + bb) / 2 - 0.5;
      const i = Math.floor(pos), fr = pos - i;
      const v0 = src[clamp(i, 0, nb - 1)], v1 = src[clamp(i + 1, 0, nb - 1)];
      out[x] = v0 + (v1 - v0) * fr;
    }
  }
}

// The level scale moves in 0.1 dB steps, so the automatic range does not
// redraw the scales for changes too small to see. Grid and labels share it.
const displayTop = () => Math.round((S.autoRange ? autoTop : S.top) * 10) / 10;
const levelStep = () => (((specH - AXIS_H) / S.range) * 10 >= 24 ? 10 : 20);
const freqStep = () => niceStep(((view.f1 - view.f0) * 110) / appW);

// The band plan's colours for the decor outside the span, one texel per
// column: its band's colour, or nothing outside the plan and while the band
// strip is hidden. Only the bands in view are visited.
let bandGlow = new Uint8Array(0), bandGlowKey = '', bandGlowVersion = 0;
function updateBandGlow(W) {
  const key = `${view.f0} ${view.f1} ${W} ${S.showPlan} ${plan.bands.length}`;
  if (key === bandGlowKey) return;
  bandGlowKey = key;
  bandGlowVersion += 1;
  if (bandGlow.length === 4 * W) bandGlow.fill(0); else bandGlow = new Uint8Array(4 * W);
  if (!S.showPlan) return;
  // Each column takes the band at its middle.
  const span = view.f1 - view.f0, column = (f) => Math.ceil(((f - view.f0) / span) * W - 0.5);
  const [first, end] = bandsInView(plan.bands, view.f0, view.f1);
  for (let i = first; i < end; i += 1) {
    const band = plan.bands[i], rgb = band.rgb.split(',').map(Number);
    for (let x = Math.max(0, column(band.from)), e = Math.min(W, column(band.to)); x < e; x += 1) {
      bandGlow[4 * x] = rgb[0];
      bandGlow[4 * x + 1] = rgb[1];
      bandGlow[4 * x + 2] = rgb[2];
      bandGlow[4 * x + 3] = 255;
    }
  }
}

let specKey = '';                   // what the spectrum shows: drawn again only when it changes
function renderSpectrum() {
  if (!spectrumView || !specColumns) return;
  const W = spectrumView.width, H = spectrumView.height, s = GLS, top = displayTop(), h = shown;
  // Outside what the spectrum shown measures, the span or the fine spectrum's
  // cover, the view draws a decor, and hatches beyond the receiver's reach; a
  // span too narrow to see shows as a marker.
  const toX = (f) => ((f - view.f0) / (view.f1 - view.f0)) * W;
  const [reachLo, reachHi] = reach();
  const lo = shownLo(), hi = shownHi();
  const spanX = [toX(lo), toX(hi)], reachX = [toX(reachLo), toX(reachHi)];
  const marker = spanMarker(spanX[0], spanX[1], s);
  const fade = pendingTune ? PENDING_FADE : 0;
  updateBandGlow(W);
  const key = `${h === fine} ${h.version} ${view.f0} ${view.f1} ${W} ${H} ${top} ${S.range} ${S.fill} ${S.peakHold} ${S.grid} ${S.peakMarks}`
    + ` ${peaksVersion} ${spanX} ${reachX} ${fade} ${bandGlowVersion} ${marker}`;
  if (key === specKey) return;
  specKey = key;
  const plotH = Math.max(2, H - Math.round(AXIS_H * s));
  const yOf = (db) => ((top - db) / S.range) * plotH;
  sample(h, h.avgDb, specVals);
  if (S.peakHold) sample(h, h.peakDb, peakVals);
  for (let x = 0; x < W; x += 1) {
    specColumns[2 * x] = clamp(yOf(specVals[x]), 0, plotH);
    specColumns[2 * x + 1] = S.peakHold ? clamp(yOf(peakVals[x]), 0, plotH) : plotH;
  }
  let grid = null;
  if (S.grid) {
    const span = view.f1 - view.f0, step = freqStep(), dbStep = levelStep();
    grid = [((Math.ceil(view.f0 / step) * step - view.f0) / span) * W, (step / span) * W,
      yOf(Math.floor(top / dbStep) * dbStep), (dbStep / S.range) * plotH];
  }
  let markerCount = 0;
  if (S.peakMarks) {
    const span = view.f1 - view.f0;
    for (const p of peaks) {
      if (markerCount >= MAX_MARKERS) break;
      const f = p.frequencyHz;
      if (!p.active || f < view.f0 || f > view.f1 || f < lo || f > hi) continue;
      const x = ((f - view.f0) / span) * W;
      const bi = clamp(Math.round(binPosition(f, h.centreHz, h.widthHz, h.bins)), 0, h.bins - 1);
      const y = clamp(yOf(h.avgDb[bi]) - 6 * s, 12 * s, plotH);
      markerCorners.set([x - 4 * s, y - 7 * s, x + 4 * s, y - 7 * s, x, y], markerCount * 6);
      markerCount += 1;
    }
  }
  spectrumView.draw({
    columns: specColumns, plotHeight: plotH, fill: S.fill, traceWidth: 1.1 * s, peakWidth: S.peakHold ? s : 0,
    grid, markers: markerCorners, markerCount,
    span: spanX, reach: reachX, marker, scale: s, fade, glow: bandGlow, glowVersion: bandGlowVersion,
  });
}

// Frequency scale under the spectrum and level scale along its left edge:
// 2D text, drawn again only when it changes.
let axisKey = '', levelKey = '';
function renderScales() {
  const d = DPR, span = view.f1 - view.f0;
  const aKey = `${view.f0} ${view.f1} ${appW}`;
  if (aKey !== axisKey) {
    axisKey = aKey;
    const ctx = axisCtx, W = axisCanvas.width, H = axisCanvas.height;
    ctx.clearRect(0, 0, W, H);
    const step = freqStep(), dec = axisDecimals(step);
    ctx.font = `${10 * d}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(215,221,229,0.55)';
    for (let f = Math.ceil(view.f0 / step) * step; f <= view.f1; f += step) {
      ctx.fillText((f / 1e6).toFixed(dec), Math.round(((f - view.f0) / span) * W) + 0.5, H / 2);
    }
  }
  const top = displayTop();
  const lKey = `${top} ${S.range} ${specH} ${labelTopCss} ${markBandCss}`;
  if (lKey === levelKey) return;
  levelKey = lKey;
  // The labels sit just above their grid lines, with a dark halo so the trace never hides them.
  const ctx = dbCtx, W = dbCanvas.width, H = dbCanvas.height;
  ctx.clearRect(0, 0, W, H);
  const dbStep = levelStep(), yOf = (db) => ((top - db) / S.range) * H;
  const labelTop = labelTopCss * d;
  const markBand = markBandCss && [markBandCss[0] * d, markBandCss[1] * d];
  ctx.font = `${10 * d}px system-ui, sans-serif`;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.lineWidth = 3 * d;
  ctx.lineJoin = 'round';
  ctx.strokeStyle = 'rgba(10,12,15,0.85)';
  ctx.fillStyle = 'rgba(215,221,229,0.5)';
  for (let db = Math.ceil((top - S.range) / dbStep) * dbStep; db <= top; db += dbStep) {
    const y = Math.round(yOf(db)) + 0.5;
    if (y <= labelTop || y >= H - 4 * d) continue;
    // The text spans about [y - 14, y - 2] CSS px: skip it where a bookmark label covers the column.
    if (markBand && y - 14 * d < markBand[1] && y - 2 * d > markBand[0]) continue;
    ctx.strokeText(String(db), 6 * d, y - 2 * d);
    ctx.fillText(String(db), 6 * d, y - 2 * d);
  }
}

// ------------------------------------------------------------------ overlays
const fToX = (f) => ((f - view.f0) / (view.f1 - view.f0)) * appW;
const xToF = (x) => view.f0 + (x / appW) * (view.f1 - view.f0);

// The band strip by levels of detail, as a map: a band shows its ranges
// while they average STRIP_SEGMENT_PX on screen, and is one block of its
// type's colour below that.
const showsSegments = (band) => band.count > 1 && (fToX(band.to) - fToX(band.from)) / band.count >= STRIP_SEGMENT_PX;
// What the strip shows at f: a range, or a band drawn as one block.
function stripItemAt(f) {
  const band = bandAt(f);
  return band && !showsSegments(band) ? band : segmentAt(f);
}
// Clipped to the strip, in its px: a range can be thousands of times wider than the view.
const stripExtent = (item) => [Math.max(fToX(item.from) * DPR, 0), Math.min(fToX(item.to) * DPR, bandCanvas.width)];
// Where the ranges' names lie on the strip, px: the channels' numbers and the spots' names keep clear of them.
let stripTaken = [];
// What the strip shows of the channels and spots (channels.js), for the pointer: none while hidden.
let stripMarks = null;
// The first of the names that fits between a and b, if one does; whether one did.
function drawStripLabel(ctx, a, b, names) {
  const name = names.find((n) => n && ctx.measureText(n).width + 8 * DPR < b - a);
  if (!name) return false;
  const w = ctx.measureText(name).width;
  ctx.fillStyle = 'rgba(230,235,240,0.85)';
  ctx.fillText(name, (a + b) / 2, bandCanvas.height / 2 - DPR);
  stripTaken.push([(a + b - w) / 2, (a + b + w) / 2]);
  return true;
}
function drawStripBlock(ctx, item, names) {
  const H = bandCanvas.height, d = DPR, [a, b] = stripExtent(item);
  ctx.fillStyle = `rgba(${item.rgb},0.26)`;
  ctx.fillRect(a, 0, Math.max(1, b - a), H);
  ctx.fillStyle = `rgba(${item.rgb},0.85)`;
  ctx.fillRect(a, H - 2 * d, Math.max(1, b - a), 2 * d);
  return drawStripLabel(ctx, a, b, names);
}
function drawBandStrip() {
  const ctx = bandCtx;
  ctx.clearRect(0, 0, bandCanvas.width, bandCanvas.height);
  stripTaken = [];
  stripMarks = null;
  if (!S.showPlan) return;
  ctx.font = `${10 * DPR}px system-ui, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const [first, end] = bandsInView(plan.bands, view.f0, view.f1);
  for (let i = first; i < end; i += 1) {
    const band = plan.bands[i];
    if (!showsSegments(band)) { drawStripBlock(ctx, band, [band.name, band.short]); continue; }
    let named = false;
    for (let i = band.first; i < band.first + band.count; i += 1) {
      const r = plan.ranges[i];
      if (r.to > view.f0 && r.from < view.f1 && drawStripBlock(ctx, r, [r.name])) named = true;
    }
    // Ranges too narrow for their names: the band's name goes over them.
    if (!named) drawStripLabel(ctx, ...stripExtent(band), [band.name, band.short]);
  }
  if (S.showChannels) drawStripChannels(ctx);
}
// The channels as ticks along the strip's lower edge, with their numbers
// where they fit, and the known frequencies as marks along its upper edge,
// with their names: at most one tick every few px, laid out by channels.js.
function drawStripChannels(ctx) {
  const H = bandCanvas.height, d = DPR;
  stripMarks = stripLayout(plan, view.f0, view.f1, bandCanvas.width, d, (text) => ctx.measureText(text).width, stripTaken);
  ctx.fillStyle = 'rgba(230,235,240,0.6)';
  for (const { x } of stripMarks.ticks) ctx.fillRect(Math.round(x - d / 2), H - 5 * d, d, 3 * d);   // under the names' baseline
  for (const { x, spot } of stripMarks.spots) {
    ctx.fillStyle = `rgb(${spot.rgb})`;
    ctx.beginPath();
    ctx.moveTo(x - 3 * d, 0);
    ctx.lineTo(x + 3 * d, 0);
    ctx.lineTo(x, 4 * d);
    ctx.closePath();
    ctx.fill();
  }
  ctx.fillStyle = 'rgba(230,235,240,0.85)';
  for (const { x, item } of stripMarks.names) ctx.fillText(item.spot.name, x, H / 2 - d);
  ctx.fillStyle = 'rgba(230,235,240,0.6)';
  for (const { x, item } of stripMarks.numbers) ctx.fillText(item.channel.n, x, H / 2 - d);
}
// The spot or channel a press lands on, if the strip shows one there: marks
// answer in its upper half, ticks in its lower half, names and numbers anywhere.
function stripHitAt(e) {
  if (!stripMarks) return null;
  const box = bandEl.getBoundingClientRect(), upper = e.clientY - box.top < box.height / 2;
  return stripHit(plan.bands, stripMarks, e.clientX * DPR, upper, (e.pointerType === 'mouse' ? 4 : 10) * DPR);
}

function renderMarks() {
  marksEl.textContent = '';
  markBandCss = null;
  if (!S.showMarks || specH < 150) return;
  const coarse = coarseQuery.matches;   // touch labels use a larger font and padding (see CSS)
  const items = bookmarks
    .filter((b) => b.f >= view.f0 && b.f <= view.f1)
    .map((b) => ({ b, x: fToX(b.f), w: b.name.length * (coarse ? 7.2 : 6.4) + (coarse ? 22 : 14) }))
    .sort((p, q) => p.x - q.x);
  let right = -Infinity;
  for (const it of items) {
    if (it.x - it.w / 2 < right + 4) continue;
    right = it.x + it.w / 2;
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'mark';
    el.textContent = it.b.name;
    el.title = `${it.b.name} · ${formatFrequency(it.b.f)} MHz · ${it.b.mode}`;
    el.style.left = `${it.x}px`;
    el.addEventListener('pointerdown', (e) => e.stopPropagation());
    el.addEventListener('click', (e) => { e.stopPropagation(); tuneAnywhere(it.b.f, { mode: it.b.mode }); });
    marksEl.appendChild(el);
  }
  // Marks are placed left to right: only the first one can reach the dB label column (7 px tick included).
  const first = marksEl.firstElementChild;
  const r = first && first.getBoundingClientRect();
  if (r && r.left < DB_LABEL_COL) markBandCss = [r.top, r.bottom + 7];
}

function renderFreq() {
  if (editingFreq) return;
  const vfo = shownVfo(), s = String(Math.round(vfo)), n = s.length;
  let html = '';
  for (let i = 0; i < n; i += 1) {
    const pos = n - 1 - i;
    html += `<span class="d${pos < 3 ? ' lo' : ''}" data-m="${10 ** pos}">${s[i]}</span>`;
    if (pos > 0 && pos % 3 === 0) html += '<span class="sep">.</span>';
  }
  freqEl.innerHTML = html;
  freqEl.setAttribute('aria-valuenow', String(Math.round(vfo)));
  freqEl.setAttribute('aria-valuetext', `${formatFrequency(vfo)} MHz`);
}

// The line under the frequency, which a zoom changes with the Automatic step:
// the known frequency or the channel the VFO is on, else its range; in SAM,
// beside the mode, whether it holds the carrier. A text is written only when
// it changes, as rewriting it repaints in WebKitGTK.
const lockEl = $('#m-lock');
const LOCK_TITLES = {
  locked: 'Synchronous AM follows the carrier',
  'no lock': 'No carrier 10 dB over the noise within 1 kHz: the envelope plays, as in AM',
};
function updateMeta() {
  const vfo = shownVfo(), seg = segmentAt(vfo), step = `step ${fmtBw(stepAt(vfo))}`;
  const where = nameAt(plan, vfo) ?? (seg ? seg.name : { ready: 'Outside the plan', failed: 'No band plan' }[planState]);
  for (const [sel, text] of [['#m-mode', S.mode], ['#m-bw', fmtBw(S.bw)], ['#m-seg', where ? `${where} · ${step}` : step]]) {
    const el = $(sel);
    if (el.textContent !== text) el.textContent = text;
  }
  const lock = S.mode !== 'SAM' || samLocked === null ? '' : samLocked ? 'locked' : 'no lock';
  if (lockEl.textContent !== lock) {
    lockEl.textContent = lock;
    lockEl.title = LOCK_TITLES[lock] ?? '';
    lockEl.classList.toggle('on', lock === 'locked');
    lockEl.hidden = !lock;
  }
}

function sUnits(db) {
  const over = db + 50;
  if (over < 0) return `S${clamp(Math.floor(9 + over / 6), 0, 9)}`;
  if (over < 10) return 'S9';
  return `S9+${Math.floor(over / 10) * 10}`;
}

let lastMeterText = 0, shownFill = '', shownOpen = null, shownSql;
function updateMeterUi(ts) {
  // Written only when they change: in WebKitGTK, rewriting a style still repaints.
  const fill = `scaleX(${clamp((meterDb - S_ZERO) / (S_TOP - S_ZERO), 0, 1).toFixed(3)})`;
  if (fill !== shownFill) { shownFill = fill; meterFill.style.transform = fill; }
  if (sqlOpen !== shownOpen) {
    shownOpen = sqlOpen;
    meterFill.classList.toggle('open', sqlOpen);
    vfoEl.classList.toggle('open', sqlOpen);
  }
  const sql = meterSqlDb === null ? null : `${(clamp((meterSqlDb - S_ZERO) / (S_TOP - S_ZERO), 0, 1) * 100).toFixed(1)}%`;
  if (sql !== shownSql) {
    shownSql = sql;
    meterSql.hidden = sql === null;
    if (sql !== null) meterSql.style.left = sql;
  }
  if (ts - lastMeterText > 120) {
    lastMeterText = ts;
    const text = `${sUnits(meterDb)} · ${Math.round(meterDb)} dB`, el = $('#s-text');
    if (el.textContent !== text) el.textContent = text;   // rewriting an unchanged text still repaints
  }
}

function updateOverlays() {
  if (!appW) return;
  const vfo = shownVfo(), [lo, hi] = passband(vfo);
  const x0 = fToX(lo), x1 = fToX(hi);
  const visible = x1 >= -2 && x0 <= appW + 2;
  vfoEl.style.display = visible ? '' : 'none';
  vfoLine.style.display = visible ? '' : 'none';
  vfoEl.style.transform = `translateX(${x0}px)`;
  vfoEl.style.width = `${Math.max(1, x1 - x0)}px`;
  vfoLine.style.transform = `translateX(${Math.round(fToX(vfo))}px)`;
  showNextSpan(pendingTune ? spanAround(pendingCentre()) : null);
  syncVfoReturn(vfo);
  syncSpanFit();
  renderMarks();
  drawBandStrip();
  updateMeta();
  renderFreq();
  syncAntenna();
  if (openName === 'bookmarks') renderPlanList();
}

// The lengths of an antenna for the frequency the VFO shows, while their
// dialog is open: they follow the VFO, written only when they change.
function syncAntenna() {
  if (!antennaDialog.open) return;
  const f = shownVfo(), band = bandAt(f), { wavelength, half, quarter } = antennaLengths(f);
  const texts = [
    ['#antenna-for', `${formatFrequency(f)} MHz${band ? ` · ${band.name}` : ''}`],
    ['#antenna-wave', formatLength(wavelength)],
    ['#antenna-dipole', formatLength(half)],
    ['#antenna-arm', formatLength(quarter)],
    ['#antenna-vertical', formatLength(quarter)],
    ['#antenna-radial', formatLength(quarter)],
  ];
  for (const [sel, text] of texts) {
    const el = $(sel);
    if (el.textContent !== text) el.textContent = text;
  }
}

// An outline over the views around a range of frequencies, written to the
// page only when it moves; null hides it.
function outline(el) {
  let shownKey = '';
  return (range) => {
    const x0 = range ? Math.round(fToX(range[0])) : 0, x1 = range ? Math.round(fToX(range[1])) : 0;
    const key = range && x1 >= 0 && x0 <= appW ? `${x0} ${x1}` : '';
    if (key === shownKey) return;
    shownKey = key;
    el.hidden = !key;
    if (!key) return;
    el.style.transform = `translateX(${x0}px)`;
    el.style.width = `${Math.max(1, x1 - x0)}px`;
  };
}

// When the VFO lies outside the view, a label at that edge of the spectrum
// gives its frequency; clicking it brings the view back to it, at the same zoom.
let vfoReturnText = '';
function syncVfoReturn(vfo) {
  const side = vfo < view.f0 ? 'left' : vfo > view.f1 ? 'right' : '';
  const text = side === 'left' ? `← ${formatFrequency(vfo)} MHz` : side === 'right' ? `${formatFrequency(vfo)} MHz →` : '';
  if (text === vfoReturnText) return;
  vfoReturnText = text;
  vfoReturnEl.hidden = !side;
  if (!side) return;
  vfoReturnEl.textContent = text;
  vfoReturnEl.classList.toggle('right', side === 'right');
  vfoReturnEl.setAttribute('aria-label', `Show the VFO, at ${formatFrequency(vfo)} MHz`);
}
vfoReturnEl.addEventListener('click', () => {
  const span = view.f1 - view.f0, f = shownVfo();
  setView(f - span / 2, f + span / 2);
});

// While the view does not frame the span, a button at the bottom right of the
// spectrum frames it again, as the 0 key does.
let spanFitShown = false;
function syncSpanFit() {
  const shown = !viewFramesSpan();
  if (shown === spanFitShown) return;
  spanFitShown = shown;
  spanFitEl.hidden = !shown;
}
spanFitEl.addEventListener('click', () => setView(fStart(), fEnd()));

// Before a click outside the span, an outline shows where the span would go
// and a tip by the pointer says what the click does; beyond the receiver's
// reach, the tip says that it tunes nothing. A mouse only: a finger has no
// hover, and sees the span on its way just after its tap, or the status
// line's note.
const BEYOND_REACH_TIP = "Beyond the receiver's range: a click tunes nothing";
let tuneTipText = '';
function showTunePreview(at, f) {
  let range = null, tip = '';
  if (at && at.pointerType === 'mouse' && at.area !== 'band' && !(drag && drag.moved)) {
    const [lo, hi] = targetSpan(), target = snapAt(f);     // where the click tunes
    if (beyondReach(target)) {
      tip = BEYOND_REACH_TIP;
    } else if (streamState === 'open' && sourceTunes() && (f < lo || f > hi)) {
      range = spanAround(retuneCentre(target, {}));
      tip = 'Click: tune here';
      if (memoryRuns()) tip += ' · stops the memory scan';
      else if (sweepRuns()) tip += ' · stops the range scan';
    }
  }
  showPreviewSpan(range);
  if (tip !== tuneTipText) {
    tuneTipText = tip;
    tuneTipEl.textContent = tip;
    tuneTipEl.hidden = !tip;
  }
  if (!tip) return;
  // Beside the pointer, on the side with room.
  const x = at.clientX > appW * 0.6 ? `calc(${Math.round(at.clientX) - 12}px - 100%)` : `${Math.round(at.clientX) + 12}px`;
  const y = at.clientY > window.innerHeight - 48 ? `calc(${Math.round(at.clientY) - 10}px - 100%)` : `${Math.round(at.clientY) + 16}px`;
  tuneTipEl.style.transform = `translate(${x}, ${y})`;
}

let hoverAt = null;                 // last pointer position over a view, to refresh the readout when the view moves
function showHover(e, area) {
  hoverAt = { clientX: e.clientX, clientY: e.clientY, area, pointerType: e.pointerType };
  const x = e.clientX, f = xToF(x), h = shown;
  xline.hidden = false;
  xline.style.transform = `translateX(${Math.round(x)}px)`;
  let txt = `${fmtMHz(f, readoutDecimals(view.f1 - view.f0, appW))} MHz`;
  if (f >= shownLo() && f <= shownHi()) {
    if (area === 'spec') {
      const bi = clamp(Math.round(binPosition(f, h.centreHz, h.widthHz, h.bins)), 0, h.bins - 1);
      txt += ` · ${Math.round(h.avgDb[bi])} dB`;
    } else if (area === 'wf') {
      const row = h.rowAt(Math.floor(e.clientY - wfWrap.getBoundingClientRect().top));
      // The line keeps its own frequencies, the span's or the fine spectrum's cover around its centre.
      const half = h === fine ? FINE_COVER * h.widthHz : h.widthHz / 2;
      const lineBin = row < 0 ? -1 : h.lineBinAt(row, f, -half, half);
      if (lineBin >= 0) {
        txt += ` · ${Math.round(HIST_DB_MIN + h.hist[row * h.bins + lineBin] * HIST_DB_STEP)} dB · ${fmtAgo((Date.now() - h.histTime[row]) / 1000)} ago`;
      }
    }
  } else if (h === fine && f >= fStart() && f <= fEnd()) {
    txt += ' · beyond the fine spectrum';
  } else {
    txt += reachable(f) ? ' · outside the span' : ' · out of reach';
  }
  const seg = segmentAt(f);
  if (seg) txt += ` · ${seg.name}`;
  hoverEl.textContent = txt;
  showTunePreview(hoverAt, f);
}
function hideHover() { hoverAt = null; xline.hidden = true; hoverEl.textContent = ''; showTunePreview(null, 0); }

// A note of this interface's own at the head of the status line, for
// STATUS_NOTE_MS: why a typed frequency tuned nothing.
const STATUS_NOTE_MS = 6000;
let statusNote = '', statusNoteTimer = 0;
function showStatusNote(note) {
  statusNote = note;
  clearTimeout(statusNoteTimer);
  statusNoteTimer = setTimeout(() => { statusNote = ''; updateStatus(); }, STATUS_NOTE_MS);
  updateStatus();
}
let shownRateNote = '';      // why the mode plays nothing at this sample rate, as the status line shows it
function updateStatus() {
  let text;
  if (streamState === 'open' && source) {
    const name = SOURCE_NAMES[source.kind] || `Source ${source.kind}`;
    const where = source.address ? ` at ${source.address}` : '';
    if (source.status === 'waiting') {
      text = `Waiting for ${name}${where}: ${source.detail}…`;
    } else if (source.status !== 'streaming') {
      text = `Connecting to ${name}${where}…`;
    } else {
      text = `${name} · ${(S.fs / 1e6).toFixed(3)} MS/s · FFT ${S.nb} (${Math.round(binHz())} Hz)`
        + (frameRate ? ` · ${frameRate} fps` : '');
    }
  } else if (streamState === 'waiting') {
    text = 'Waiting for the engine…';
  } else if (streamState === 'remote-off') {
    text = 'Remote access is off on the computer: waiting for it…';
  } else if (streamState === 'incompatible') {
    text = 'Engine from another version: reload the page';
  } else if (streamState === 'unpaired') {
    text = 'This device is not paired: reload the page';
  } else {
    text = 'Connecting to the engine…';
  }
  if (streamState === 'open' && memoryRuns()) text += ' · memory scan';
  if (streamState === 'open' && sweepRuns()) text += ' · range scan';
  if (streamState === 'open') text += recordingStatus();
  shownRateNote = streamState === 'open' ? modeRateNote(S.mode, S.fs) : '';
  if (shownRateNote) text = `${shownRateNote} · ${text}`;
  if (statusNote) text = `${statusNote} · ${text}`;
  if (gpuError) text = `${gpuError} · ${text}`;
  if (storage && storage.directory !== null && !storage.saving) text += ' · changes not saved';
  $('#status').textContent = `${text}${remoteState?.on ? ' · remote access on' : ''}`;
}

// ------------------------------------------------------------------ layout and view
const bandHeight = () => (coarseQuery.matches ? 26 : 18);   // the band strip doubles as a touch handle
let labelTopCss = 40;               // dB labels start below this line, clear of the menu's icon and the readout
let markBandCss = null;             // [top, bottom] of a bookmark label lying over the dB label column
const DB_LABEL_COL = 40;            // width kept by the dB labels at the left edge, in CSS px (#spec-db in style.css)
// Drawing-buffer scale of the GPU views: the device pixel ratio, lowered on
// large windows to stay within the pixel budget. The spectrum grows with the
// square of the scale; the waterfall, one line per CSS px row, linearly.
function gpuScale(width, specHeight, wfRows) {
  const a = width * specHeight, b = width * wfRows;
  if (a <= 0) return DPR;
  return clamp((-b + Math.sqrt(b * b + 4 * a * PIXEL_BUDGET)) / (2 * a), 1, DPR);
}

function layout() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  appW = window.innerWidth;
  const H = window.innerHeight, bandH = bandHeight();
  specH = Math.round(clamp(S.split, 0.12, 0.8) * (H - bandH));
  appEl.style.gridTemplateRows = `${specH}px ${bandH}px minmax(0, 1fr)`;
  wfH = Math.max(2, H - specH - bandH);
  GLS = gpuScale(appW, specH, wfH);
  if (spectrumView) spectrumView.resize(appW * GLS, specH * GLS);
  if (waterfallView) waterfallView.resize(appW * GLS, wfH);
  const columns = spectrumView ? spectrumView.width : 0;
  specVals = new Float32Array(columns);
  peakVals = new Float32Array(columns);
  specColumns = new Float32Array(2 * columns);
  bandCanvas.width = Math.max(2, Math.round(appW * DPR));
  bandCanvas.height = Math.max(2, Math.round(bandEl.clientHeight * DPR));
  axisCanvas.width = Math.max(2, Math.round(appW * DPR));
  axisCanvas.height = Math.round(AXIS_H * DPR);
  dbCanvas.width = Math.round(DB_LABEL_COL * DPR);
  dbCanvas.height = Math.max(2, Math.round((specH - AXIS_H) * DPR));
  specKey = wfKey = axisKey = levelKey = '';     // resized canvases are blank
  noticeEl.style.top = `${specH + bandH + 8}px`;   // the top of the waterfall
  vfoReturnEl.style.top = `${Math.round((specH - AXIS_H) / 2)}px`;
  spanFitEl.style.top = `${specH - AXIS_H - 6}px`;     // its bottom, just above the frequency scale
  // The labels start below what lies over their column at the top: the menu's icon with a mouse, and
  // the readout on a narrow touch screen. A label's text reaches 14 px above its line.
  const ro = readoutEl.getBoundingClientRect(), icon = menuButton.getBoundingClientRect();
  labelTopCss = Math.max(40, ro.left < DB_LABEL_COL ? Math.ceil(ro.bottom) + 8 : 0,
    icon.left < DB_LABEL_COL && icon.top < specH ? Math.ceil(icon.bottom) + 16 : 0);
  rebuildStamps();
  viewChanged();
}

function viewChanged() {
  syncDeep();
  updateOverlays();
  if (hoverAt) showHover(hoverAt, hoverAt.area);
}

// The view goes from 0 Hz to the end of the band plan, or of the receiver's
// reach when that is higher, whatever the receiver; zooming out holds at the
// span's width first (the detent). Zooming in stops at 16 of the wide
// spectrum's bins, or, within the fine spectrum's cover, of the fine
// spectrum's (deepzoom.js).
const wideMin = () => wideNarrowest(binHz());
function narrowest() {
  const deep = deepLimits();
  return deep ? Math.min(wideMin(), deep.narrowestHz) : wideMin();
}
const extent = () => viewExtent(planEnd(plan.bands), reach(), [fStart(), fEnd()]);
const clampSpan = (span) => { const [lo, hi] = extent(); return clamp(span, narrowest(), hi - lo); };
const viewLimits = () => `${extent()}`;
const viewInSpan = () => view.f0 >= fStart() - 1 && view.f1 <= fEnd() + 1;
const viewFramesSpan = () => Math.abs(view.f0 - fStart()) <= 1 && Math.abs(view.f1 - fEnd()) <= 1;
const atSpanWidth = (span) => Math.abs(span - S.fs) <= S.fs * 1e-9;
// Where a view of this centre and width goes. One kept in the span stays
// inside it while it fits, as with a source that does not tune.
function fitView(centre, span, keepInSpan = false) {
  const limits = keepInSpan && clampSpan(span) <= S.fs ? [fStart(), fEnd()] : extent();
  return fitDeepView(centre, span, limits, wideMin(), deepLimits(), view.f1 - view.f0);
}
function setView(f0, f1, keepInSpan = false) {
  [view.f0, view.f1] = fitView((f0 + f1) / 2, f1 - f0, keepInSpan);
  viewChanged();
}
// The view again within its limits, which the VFO, the mode and the engine's
// fine spectrum move: a view narrower than the wide spectrum allows stays
// within the fine spectrum's cover, and widens without it.
function refitView() {
  const [f0, f1] = fitView((view.f0 + view.f1) / 2, view.f1 - view.f0);
  if (f0 !== view.f0 || f1 !== view.f1) setView(view.f0, view.f1);
  else syncDeep();
}
// The width a step of zoom from span to s gives: zooming in stops once at the
// span's width, zooming out holds there until pushed DETENT_HOLD times further.
let detentPush = 1;
function detentSpan(span, s) {
  if (atSpanWidth(span)) {
    if (s > span) {
      detentPush *= s / span;
      if (detentPush < DETENT_HOLD) return S.fs;
      s = S.fs * (detentPush / DETENT_HOLD);
    }
  } else if ((span < S.fs) !== (s < S.fs)) {
    s = S.fs;
  }
  detentPush = 1;
  return s;
}
// The same detent for a pinch, from the width it started at: past the span's
// width, the view stays there until the fingers spread DETENT_HOLD times
// further, and the other way round.
function pinchSpan(raw, start) {
  const whole = S.fs;
  if (start <= whole || atSpanWidth(start)) return raw <= whole ? raw : raw < whole * DETENT_HOLD ? whole : raw / DETENT_HOLD;
  return raw >= whole ? raw : raw > whole / DETENT_HOLD ? whole : raw * DETENT_HOLD;
}
function zoomAt(factor, anchor) {
  const span = view.f1 - view.f0, r = (anchor - view.f0) / span;
  const s = clampSpan(detentSpan(span, span * factor));
  // At the detent, zooming around a point of the span frames the span.
  if (atSpanWidth(s) && anchor >= fStart() && anchor <= fEnd()) setView(fStart(), fEnd());
  else setView(anchor - r * s, anchor + (1 - r) * s, viewInSpan());
}
// Frame a range or a band; framing it again frames the span.
function zoomToSegment(seg) {
  if (!seg) return;
  const pad = (seg.to - seg.from) * 0.08;
  const [a, b] = fitView((seg.from + seg.to) / 2, seg.to - seg.from + 2 * pad), w = b - a;
  const same = Math.abs(view.f1 - view.f0 - w) < w * 0.05 && Math.abs((view.f0 + view.f1) / 2 - (a + b) / 2) < w * 0.05;
  if (same) setView(fStart(), fEnd()); else setView(a, b);
}

// ------------------------------------------------------------------ tuning
// The VFO, mode, filter width and squelch are the engine's, shared by every
// interface: a change made here shows at once and goes to the engine, and
// the engine's settings show as they come, from any interface.
let lastSeg;
function applyMode(mode) {
  if (!MODES[mode]) return;
  S.mode = mode;
  S.bw = MODES[mode].bw;
}
const demodSnapshot = () => ({
  vfo: S.vfo, mode: S.mode, bw: S.bw, sql: S.sql, sqlAuto: S.sqlAuto, notch: S.notch, blanker: S.blanker,
});
// Send the engine the settings changed since `before`.
function sendDemod(before) {
  const changes = {};
  if (S.vfo !== before.vfo) changes.frequencyHz = S.vfo;
  if (S.mode !== before.mode) changes.mode = S.mode;
  if (S.bw !== before.bw) changes.bandwidthHz = S.bw;
  if (S.sql !== before.sql) changes.squelchDb = S.sql;
  if (S.sqlAuto !== before.sqlAuto) changes.squelchAuto = S.sqlAuto;
  if (S.notch !== before.notch) changes.notch = S.notch;
  if (S.blanker !== before.blanker) changes.blanker = S.blanker;
  if (Object.keys(changes).length) stream.sendDemod(changes);
}
// Show the VFO's settings, bringing the VFO into view unless keepView. A view
// that frames the span stays: the VFO lies outside the span only while the
// span follows it, and the view frames the new span when its frames come.
function showVfo(keepView = false) {
  if (!keepView && !viewFramesSpan() && (S.vfo < view.f0 || S.vfo > view.f1)) {
    const span = view.f1 - view.f0;
    setView(S.vfo - span / 2, S.vfo + span / 2);
  }
  refitView();
  updateOverlays();
  syncDemod();
}
// The VFO stays within the span the engine captures; tuneAnywhere asks for another span.
function tune(f, opts = {}) {
  if (!Number.isFinite(f)) return;
  const before = demodSnapshot();
  f = clamp(Math.round(f), Math.ceil(fStart()), Math.floor(fEnd()));
  const seg = segmentAt(f);
  if (opts.mode) applyMode(opts.mode);
  else if (S.autoMode && seg && seg !== lastSeg && planMode(seg.mode, S.mode) !== S.mode) applyMode(seg.mode);
  lastSeg = seg;
  S.vfo = f;
  sendDemod(before);
  showVfo(opts.keepView);
}
// One step from where the VFO shows. A step into the tenth of the span by an
// edge, which the receiver's filter dims, moves a span that can move, as a
// click outside it would. One move at a time: while the span settles, steps
// add up within the span on its way.
function stepTune(dir, mult = 1) {
  const from = shownVfo(), set = stepSet(from);
  const f = set ? stepChannels(set, from, dir * mult) : snapAt(from) + dir * stepAt(from) * mult;
  if (streamState === 'open' && sourceTunes()) {
    const [lo, hi] = targetSpan(), edge = (hi - lo) * EDGE_SHARE;
    if (pendingTune) {
      pendingTune.f = Math.round(clamp(f, lo, hi));
      updateOverlays();
      return;
    }
    if (((dir > 0 && f > hi - edge) || (dir < 0 && f < lo + edge)) && retuneFor(Math.round(clamp(f, ...reach())), {})) return;
  }
  tune(f);
}
function setMode(mode) {
  const before = demodSnapshot();
  applyMode(mode);
  sendDemod(before);
  showVfo(true);
}
function setBandwidth(bw) {
  const before = demodSnapshot();
  const m = MODES[S.mode];
  S.bw = clamp(Math.round(bw / m.step) * m.step, m.min, m.max);
  sendDemod(before);
  showVfo(true);
}
// The engine's settings, after a change from any interface. The band plan's
// mode applies only when the user tunes into another range.
function applyDemod(state) {
  if (!MODES[state.mode]) return;
  const moved = state.frequencyHz !== S.vfo;
  S.vfo = state.frequencyHz;
  S.mode = state.mode;
  S.bw = state.bandwidthHz;
  S.sql = state.squelchDb;
  S.sqlAuto = state.squelchAuto;
  audioModes = state.audioModes;
  samLocked = state.samLocked;
  S.notch = state.notch;
  notchLines = state.notchLines;
  S.blanker = state.blanker;
  blankerImpulses = state.blankerImpulses;
  demodError = '';
  // While a retune is on its way, the engine keeps the VFO inside the new span
  // until frames come: the view, and the range the user tunes from, stay.
  const settling = pendingTune !== null;
  if (!settling) lastSeg = segmentAt(S.vfo);
  showVfo(settling || !moved);  // a view looking elsewhere stays there, unless the VFO moved
  syncAudioNote();
  if (modeRateNote(S.mode, S.fs) !== shownRateNote) updateStatus();
  syncCalibration();
}

// ------------------------------------------------------------------ scanners
// The engine finds the signals and keeps their list, the same for every
// interface, with the frequency each shows on, its channel in a channelized
// range of the band plan, and its detection settings; this interface shows
// them. The row of the signal that the signals scan is on says so: the one
// nearest its channel, within the 1.5 kHz in which the engine takes two
// detections for one signal.
const SAME_SIGNAL_HZ = 1500;
let listenedPeak = null;            // the signal whose row says that the range scan is on it
let listenedShownHz = null;         // listenedHz() as the rows show it
// The engine sends the list when it changes: the time since each inactive signal was found counts on here.
const peakSeen = (p, now) => (p === listenedPeak ? 'listening' : p.active ? 'active'
  : `${fmtAgo(p.ageS + (now - peaksAt) / 1000)} ago`);
// The engine sends the active signals first, the strongest first, which
// reorders its lists as the margins move and signals start and stop. The
// rows run by the frequency they show, the lowest first: a signal keeps its
// place among the others, and only its margin and time change.
let listedPeaks = [];               // the rows' signals, in the rows' order
let shownPeaks = new Map();         // each row's shown frequency → the engine's, which Ignore sends
function renderPeaks() {
  if (peaksHold.wait()) return;
  const list = $('#pk-body'), refocus = focusedIn(list), now = performance.now();
  const rows = peaks.map((p) => ({ p, f: p.channelHz }))
    .sort((a, b) => a.f - b.f || a.p.frequencyHz - b.p.frequencyHz);
  listedPeaks = rows.map((row) => row.p);
  listenedShownHz = listenedHz();
  listenedPeak = null;
  let nearest = SAME_SIGNAL_HZ;
  for (const { p, f } of listenedShownHz === null ? [] : rows) {
    const away = Math.abs(f - listenedShownHz);
    if (away <= nearest) { nearest = away; listenedPeak = p; }
  }
  shownPeaks = new Map();
  list.innerHTML = rows.length
    ? rows.map(({ p, f }) => {
      shownPeaks.set(String(f), p.frequencyHz);
      const classes = [p.active ? 'active' : '', p === listenedPeak ? 'listening' : ''].filter(Boolean).join(' ');
      return `<tr class="${classes}">`
        + `<td><button type="button" class="freq-link" data-tune="${f}" title="Listen">${fmtMHz(f)}</button></td>`
        + `<td>+${Math.round(p.marginDb)} dB</td><td class="seen">${peakSeen(p, now)}</td>`
        + `<td class="acts"><button type="button" class="link" data-ignore="${f}">Ignore</button></td></tr>`;
    }).join('')
    : `<tr><td colspan="4" class="empty">${scanner && !scanner.detection ? 'Detection is off' : 'No signal detected'}</td></tr>`;
  focusAgain(list, refocus);
}
// Between lists, only the times since the inactive signals were found change.
function updatePeakAges() {
  if (peaksHold.held) return;       // the rows may be an older list's until the click
  const now = performance.now();
  $$('#pk-body td.seen').forEach((cell, i) => {
    const text = listedPeaks[i] ? peakSeen(listedPeaks[i], now) : '';
    if (cell.textContent !== text) cell.textContent = text;
  });
}
const pkOn = $('#pk-on');
// The detection threshold shows in the Signals tab and in the Range tab: one
// setting, the engine's, which both sliders show and either changes.
const thresholds = [[$('#pk-thr'), $('#pk-thr-v')], [$('#sw-thr'), $('#sw-thr-v')]];
function showThreshold(db) {
  for (const [slider, output] of thresholds) {
    slider.value = String(db);
    output.textContent = `${db} dB`;
  }
}
// What interfaces change in the scanner state: the settings and the scan that runs.
const scannerSettings = (s) => JSON.stringify([s.detection, s.thresholdDb, s.ignoredHz, s.resumeS, s.listenS, s.scan,
  s.sweep && [s.sweep.lowHz, s.sweep.highHz, s.sweep.windowS, s.sweep.listen, s.sweep.passes]]);
// The scanners' controls show the engine's settings once it sent them, and
// stay off with an engine that lacks the scanner.
function syncScanner() {
  const known = scanner !== null && stream.scanners.includes('peaks');
  pkOn.disabled = !known;
  for (const [slider] of thresholds) slider.disabled = !known;
  if (scanner) {
    pkOn.checked = scanner.detection;
    showThreshold(scanner.thresholdDb);
  }
  const ignored = scanner ? scanner.ignoredHz.length : 0;
  $('#pk-excluded').textContent = ignored ? `${ignored} ignored ${ignored === 1 ? 'frequency' : 'frequencies'}.` : '';
  $('#pk-restore').hidden = ignored === 0;
  memResume.disabled = !memoryKnown();
  if (memoryKnown()) {
    memResume.value = String(scanner.resumeS);
    memResumeOut.textContent = `${scanner.resumeS} s`;
  }
  syncSweepSettings();
  syncScannerNote();
  if (signalsShown()) renderPeaks();
  if (historyShown()) { syncHistorySummary(); syncHistoryNote(); }
  syncMem();
  syncSweep();
  updateStatus();       // it names the scan that runs
}
// The latest refusal shows in every scan's place, Signals, Range and the
// Bookmarks menu's dock; each tells when the engine that runs lacks its
// scanners, the first it lacks.
const SCANNER_NOTES = [
  ['#pk-note', [['peaks', 'The engine that runs does not find signals: restart Radiolack after its update.']]],
  ['#sw-note', [['sweep', 'The engine that runs does not scan ranges: restart Radiolack after its update.']]],
  ['#mem-note', [['memory', 'The engine that runs does not scan the bookmarks: restart Radiolack after its update.']]],
];
function syncScannerNote() {
  for (const [selector, restarts] of SCANNER_NOTES) {
    let note = scannerNote;
    if (!note && streamState === 'open') note = restarts.find(([name]) => !stream.scanners.includes(name))?.[1] ?? '';
    const el = $(selector);
    el.textContent = note;
    el.hidden = !note;
  }
}
// Send a scanner command: every interface receives the scanner state once
// the engine applies it. One not sent leaves the controls as the engine's
// settings are.
function sendScanner(send) {
  const sent = send();
  scannerNote = sent ? '' : 'Not connected to the engine: the scanner did not change.';
  if (sent) syncScannerNote(); else syncScanner();
  return sent;
}

// The engine's memory scan watches the ticked bookmarks of the receiver's
// span, the same for every interface, and with a receiver that tunes visits
// those outside the span too, window after window: this interface starts
// and stops it, sets its resume time (syncScanner) and shows where it
// stands, in the Bookmarks menu's dock. The bookmarks' list marks the ticked
// ones that the scan leaves out: beyond the receiver's reach, or outside the
// span when it does not visit other windows.
const MEM_STATES = { checking: 'Checking', listening: 'Listening', waiting: 'Waiting' };
const MEM_STOPPED = {
  tuned: 'Stopped: the receiver was tuned by hand.',
  source: 'Stopped: another receiver was chosen.',
  range: 'Stopped: the receiver did not tune to a window of the bookmarks.',
  start: 'Did not start with Radiolack: no ticked bookmark lay within reach.',
};
// Why the latest scan stopped, in the tab of `scan`: the engine's reason when
// it is about that scan, or Stopped. An engine older than the sweep names no
// scan: its reasons are the memory scan's.
function stoppedText(reasons, scan) {
  const about = scanner.stoppedScan ?? 'memory';
  return (about === scan && reasons[scanner.stopped]) || 'Stopped';
}
// While another scan runs, what starting `scan` with `start` does: it stops it.
const SCAN_NAMES = { memory: 'The memory scan', sweep: 'The range scan', signals: 'The range scan' };
function otherScanText(scan, start) {
  const other = scanner.scan !== null && scanner.scan !== scan ? SCAN_NAMES[scanner.scan] : undefined;
  return other ? `${other} runs: ${start} stops it.` : '';
}
const memStart = $('#mem-start'), memResume = $('#mem-resume'), memResumeOut = $('#mem-resume-v');
const memoryKnown = () => scanner !== null && stream.scanners.includes('memory');
const memoryRuns = () => memoryKnown() && scanner.scan === 'memory';
// Whether the engine's memory scan visits the bookmarks outside the span: an
// older engine, which sends no windows, does not.
const memoryVisits = () => memoryKnown() && scanner.memory.windows !== null && sourceTunes();
// What the bookmarks' list shows of the scan: the bookmarks it watches, and why it leaves the others out.
const scanMarksKey = () => (memoryKnown() ? `${memoryVisits()}:${scanner.memory.watched.join(',')}` : '');
let scanMarksShown = '';    // scanMarksKey() as the list shows it
function syncMem() {
  const known = memoryKnown(), runs = memoryRuns(), memory = known ? scanner.memory : null;
  const watched = memory ? memory.watched.length : 0;
  // The scan's progress calls this several times a second: an unchanged text is not rewritten.
  const label = runs ? 'Stop the scan' : 'Scan the ticked bookmarks';
  if (memStart.textContent !== label) memStart.textContent = label;
  memStart.disabled = !known || (!runs && watched === 0);
  let text = '';
  if (runs) {
    const b = bookmarks.find((x) => x.id === memory.bookmarkId);
    text = b && MEM_STATES[memory.state] ? `${MEM_STATES[memory.state]}: ${b.name}`
      : `Watching ${watched} ${watched === 1 ? 'bookmark' : 'bookmarks'}`;
    if (memory.windows > 1 && memory.window !== null) text += `, window ${memory.window + 1} of ${memory.windows}`;
  } else if (known) {
    // While the range scan runs, the span is the one it brings the receiver back to.
    if (watched) text = otherScanText('memory', 'scanning') || stoppedText(MEM_STOPPED, 'memory');
    else if (!bookmarks.some((b) => b.scan)) text = 'Tick the bookmarks to scan.';
    else text = memoryVisits() ? "No ticked bookmark lies within the receiver's reach." : 'No ticked bookmark lies in the span.';
  }
  const el = $('#mem-state');
  if (el.textContent !== text) el.textContent = text;
  // Ticked bookmarks in several windows are watched in turn: the dock says what that misses.
  const tour = memoryVisits() ? tourNote(memory.windows, source.rateHz) : '';
  const tourEl = $('#mem-tour');
  if (tourEl.textContent !== tour) tourEl.textContent = tour;
  tourEl.hidden = !tour;
  if (scanMarksKey() !== scanMarksShown) renderBookmarks();
  syncMenuBadge();
}

// The engine's range scan (the protocol's sweep) steps a receiver that tunes
// across a range, window after window, or searches the current window alone
// for a range within it, the same for every interface, and may stop to
// listen to each signal it finds, tuning the VFO alone, in the band plan's
// mode: this interface sets its range, time per window, passes and
// listening, the longest listening time and the resume time, the memory
// scan's, starts and stops it, and shows where it stands; Signals' list
// says which signal it is on. A new range starts a running scan again from
// its first window.
const SWEEP_STOPPED = {
  tuned: 'Stopped: the receiver was tuned by hand.',
  source: 'Stopped: another receiver was chosen.',
  range: 'Stopped: the receiver did not tune to a window of the range.',
  detection: 'Stopped: detection was turned off.',
  done: 'Done: every pass set ran.',
  start: 'Did not start with Radiolack: the receiver could not tune to the range, or detection was off.',
};
const swFrom = $('#sw-from'), swTo = $('#sw-to'), swWindow = $('#sw-window'), swWindowOut = $('#sw-window-v');
const swStart = $('#sw-start'), swHere = $('#sw-here'), swPasses = $('#sw-passes'), swEndless = $('#sw-endless');
const swListen = $('#sw-listen'), swName = $('#sw-name');
const sigListen = $('#sig-listen'), sigListenOut = $('#sig-listen-v');
const sigResume = $('#sig-resume'), sigResumeOut = $('#sig-resume-v');
const sweepKnown = () => scanner !== null && scanner.sweep !== null && stream.scanners.includes('sweep');
const sweepRuns = () => sweepKnown() && (scanner.scan === 'sweep' || scanner.scan === 'signals');
// Whether the engine listens and counts passes: an older one does neither.
const listeningKnown = () => sweepKnown() && scanner.sweep.listen !== null && scanner.listenS !== null;
const sourceTunes = () => source !== null && source.settable.includes('centre_hz');
// The part of the receiver's span that detection searches, as "Current
// window" sets the range: its centre plus or minus 0.4 times the rate,
// within it to the kHz.
function currentWindow() {
  if (!source) return null;
  const half = 0.4 * source.rateHz;
  return [Math.ceil((source.centreHz - half) / 1000) * 1000, Math.floor((source.centreHz + half) / 1000) * 1000];
}
// Whether the range lies within that part of the span: the scan then never tunes the receiver.
function rangeHere() {
  const here = currentWindow(), sweep = sweepKnown() ? scanner.sweep : null;
  if (!here || !sweep) return false;
  const half = 0.4 * source.rateHz;
  return source.centreHz - half <= sweep.lowHz && sweep.highHz <= source.centreHz + half;
}
const DEFAULT_PASSES = 3;   // what the passes field offers once Endless is unticked
// The range's fields show the engine's range, except while the user types in
// them (typingIn); the passes field keeps its number while Endless is ticked.
function syncSweepSettings() {
  const sweep = sweepKnown() ? scanner.sweep : null, listening = listeningKnown();
  for (const el of [swFrom, swTo, swWindow]) el.disabled = !sweep;
  swEndless.disabled = !listening;
  swPasses.disabled = !listening || sweep.passes === null;
  swListen.disabled = !listening;
  swName.disabled = !sweep || !scansKnown();
  sigListen.disabled = !listening || !sweep.listen;
  sigResume.disabled = !listening || !sweep.listen;
  if (listening) {
    swEndless.checked = sweep.passes === null;
    if (sweep.passes !== null && !typingIn.has(swPasses)) swPasses.value = String(sweep.passes);
    else if (swPasses.value === '') swPasses.value = String(DEFAULT_PASSES);
    swListen.checked = sweep.listen;
    sigListen.value = String(scanner.listenS);
    sigListenOut.textContent = `${scanner.listenS} s`;
    sigResume.value = String(scanner.resumeS);
    sigResumeOut.textContent = `${scanner.resumeS} s`;
  }
  if (!sweep) return;
  if (!typingIn.has(swFrom)) swFrom.value = formatFrequency(sweep.lowHz);
  if (!typingIn.has(swTo)) swTo.value = formatFrequency(sweep.highHz);
  swWindow.value = String(sweep.windowS);
  swWindowOut.textContent = `${sweep.windowS} s`;
}
// The channel the range scan is on; null while it watches, searches or does not run.
const listenedHz = () => (sweepRuns() && scanner.signals ? scanner.signals.frequencyHz : null);
// What the range scan's state says: the pass, the window and the signal it is on.
function sweepProgress(sweep) {
  if (sweep.windows === null) return 'Scanning';
  let text = `Pass ${sweep.passesDone + 1}`;
  if (sweep.passesSet !== null) text += ` of ${sweep.passesSet}`;
  if (sweep.windows > 1) text += `, window ${sweep.window + 1} of ${sweep.windows}`;
  const signals = scanner.signals;
  if (signals && MEM_STATES[signals.state] && signals.frequencyHz !== null) {
    text += `, ${MEM_STATES[signals.state].toLowerCase()} ${fmtMHz(signals.frequencyHz)} MHz`;
  }
  return text;
}
function syncSweep() {
  const known = sweepKnown(), runs = sweepRuns(), sweep = known ? scanner.sweep : null;
  const listens = known && sweep.listen === true;
  // The scan's progress calls this several times a second: an unchanged text is not rewritten.
  const label = runs ? 'Stop' : 'Start';
  if (swStart.textContent !== label) swStart.textContent = label;
  const tunes = sourceTunes() || rangeHere();
  swStart.disabled = !known || (!runs && (!tunes || (listens && !scanner.detection)));
  const hereOff = !known || source === null || scanner.scan !== null;
  if (swHere.disabled !== hereOff) swHere.disabled = hereOff;
  let text = '';
  if (runs) text = sweepProgress(sweep);
  else if (known) {
    if (!tunes) text = 'The source in use does not tune: choose a range within the current window.';
    else if (listens && !scanner.detection) text = 'Turn detection on to stop and listen.';
    else text = otherScanText('sweep', 'Start') || stoppedText(SWEEP_STOPPED, 'sweep');
  }
  const el = $('#sw-state');
  if (el.textContent !== text) el.textContent = text;
  if (listenedHz() !== listenedShownHz && signalsShown()) renderPeaks();
  syncMenuBadge();
}

// A new end of the range that would leave it empty moves the other end too,
// keeping the range's width: the range moves.
function sendSweepRange(el) {
  const sweep = sweepKnown() ? scanner.sweep : null;
  const hz = parseFrequency(el.value);
  if (!sweep || hz === null || hz === (el === swFrom ? sweep.lowHz : sweep.highHz)) {
    syncSweepSettings();
    return;
  }
  const width = sweep.highHz - sweep.lowHz;
  let low = sweep.lowHz, high = sweep.highHz;
  if (el === swFrom) {
    low = hz;
    if (high <= low) high = low + width;
  } else {
    high = hz;
    if (low >= high) low = high - width;
  }
  sendScanner(() => stream.setScanner({ sweepLowHz: low, sweepHighHz: high }));
}

// The engine keeps the channels where it found signals (stream.js
// Observation) and sends them to the interface that asks: this one asks
// when its History tab shows, then every HISTORY_REFRESH_MS while it does.
// Clear, for a channel or for all, waits for a second press of the same
// button, within HISTORY_CONFIRM_MS: it forgets them for every interface.
const HISTORY_REFRESH_MS = 30000;
const HISTORY_CONFIRM_MS = 5000;
let observations = null;    // the engine's, the latest heard first, once received
let historyNote = '';       // what this interface's last request did, or why it did not happen
let historyArmed = null;    // the Clear awaiting its second press: a channel's frequency, 'all', or null
let historyArmTimer = 0;
let historyHtml = '';       // the table's rows, as last written
const historyKnown = () => stream.scanners.includes('history');
const historyShown = () => openName === 'scan' && scanTab === 'history';
const pad2 = (n) => String(n).padStart(2, '0');
// A time.time() as this device's date and time, such as 2026-11-12 18:05.
function fmtDate(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
// The time since a channel was last heard, in whole minutes, hours or days:
// the list is at most HISTORY_REFRESH_MS old.
function fmtHeardAgo(seconds) {
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
const fmtActive = (s) => (s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
// A channel's tooltip. Its busiest hour shows unless every count is 0, as in
// a file edited by hand: the engine counts every transmission.
function historyTitle(c) {
  const most = Math.max(...c.hours);
  const busiest = most > 0 ? ` · most often from ${pad2(c.hours.indexOf(most))}:00, on the engine's computer` : '';
  return `Listen · first heard ${fmtDate(c.firstS)}, last ${fmtDate(c.lastS)} · up to ${Math.round(c.marginDb)} dB `
    + `above the noise${busiest}`;
}
function askHistory() {
  if (!historyKnown()) return;
  if (!stream.askObservations()) setHistoryNote(notConnected('the history is not up to date'));
}
function setHistoryNote(note) {
  historyNote = note;
  syncHistoryNote();
}
function syncHistoryNote() {
  let note = historyNote;
  if (!note && streamState === 'open' && !historyKnown()) {
    note = 'The engine that runs keeps no history: restart Radiolack after its update.';
  }
  const el = $('#hist-note');
  el.textContent = note;
  el.hidden = !note;
}
// How many channels the list holds, if any (the empty table says so
// otherwise), and why nothing is recorded now, if so.
function syncHistorySummary() {
  const n = observations ? observations.length : 0;
  let text = n ? `${n} ${n === 1 ? 'channel' : 'channels'}.` : '';
  if (source && source.kind === 'synthetic') text += ' The simulated band is not recorded.';
  else if (scanner && !scanner.detection) text += ' Detection is off: nothing is recorded.';
  text = text.trim();
  const el = $('#hist-summary');
  if (el.textContent !== text) el.textContent = text;
  const all = $('#hist-clear-all');
  all.hidden = !historyKnown() || n === 0;
  const label = historyArmed === 'all' ? 'Confirm: clear all' : 'Clear all';
  if (all.textContent !== label) all.textContent = label;
}
function renderHistory() {
  const body = $('#hist-body'), refocus = focusedIn(body), now = Date.now() / 1000;
  const list = observations || [];
  const html = list.length
    ? list.map((c) => {
      const f = c.channelHz;
      const armed = historyArmed === c.frequencyHz;
      return `<tr><td><button type="button" class="freq-link" data-tune="${f}" title="${escapeHtml(historyTitle(c))}">${fmtMHz(f)}</button></td>`
        + `<td>${c.transmissions}×</td><td>${fmtActive(c.activeS)}</td><td>${fmtHeardAgo(now - c.lastS)}</td>`
        + `<td class="acts"><button type="button" class="link" data-mark="${f}">Bookmark</button>`
        + `<button type="button" class="link" data-clear="${c.frequencyHz}"`
        + `${armed ? ` aria-label="Confirm clearing ${fmtMHz(f)} MHz"` : ''}>${armed ? 'Confirm' : 'Clear'}</button></td></tr>`;
    }).join('')
    : `<tr><td colspan="5" class="empty">${observations === null ? 'Not received yet' : 'No channel recorded yet'}</td></tr>`;
  if (html !== historyHtml && !historyHold.wait()) {
    body.innerHTML = html;
    historyHtml = html;
    focusAgain(body, refocus);
  }
  syncHistorySummary();
  syncHistoryNote();
}
// A first press arms Clear; a second one, within HISTORY_CONFIRM_MS, sends it.
function armHistory(what) {
  clearTimeout(historyArmTimer);
  historyArmed = what;
  historyArmTimer = setTimeout(disarmHistory, HISTORY_CONFIRM_MS);
  renderHistory();
}
function disarmHistory() {
  clearTimeout(historyArmTimer);
  historyArmed = null;
  if (historyShown()) renderHistory();
}
function clearHistory(what) {
  disarmHistory();
  const sent = what === 'all' ? stream.clearObservations() : stream.clearObservation(what);
  setHistoryNote(sent ? '' : notConnected('nothing was cleared'));
}

// The engine keeps each range scan under its name, with the channels it
// found (stream.js KeptScan), and sends the list to every interface when it
// changes, at most every 10 s while a scan runs. The Range tab lists them,
// the latest first; one opens with its channels, which the engine sends to
// this interface alone when asked, and again as the scan goes on, with its
// Rename and its Delete. Delete and Clear all wait for a second press of the
// same button within SCANS_CONFIRM_MS: they forget for every interface.
const SCANS_CONFIRM_MS = 5000;
const SCAN_ENDINGS = {
  stopped: 'Stopped', tuned: 'Stopped: tuned by hand', source: 'Stopped: another receiver was chosen',
  range: 'Stopped: the receiver refused a window', detection: 'Stopped: detection was turned off',
  done: 'Done: every pass set ran', closed: 'Ended with Radiolack', interrupted: 'Interrupted',
};
let keptScans = null;       // the engine's, the latest first, once received
let openScanId = null;      // the kept scan whose channels show, or null for the list
let openScan = null;        // its channels, once received
let scansNote = '';         // what this interface's last request did, or why it did not happen
let scansArmed = null;      // the Delete awaiting its second press: a scan's id, 'all', or null
let scansArmTimer = 0;
let scansHtml = '';         // the list's rows, as last written
let scanChannelsHtml = '';  // the open scan's rows, as last written
const scansKnown = () => stream.scanners.includes('scans');
const rangeShown = () => openName === 'scan' && scanTab === 'sweep';
const scanRange = (s) => `${fmtMHz(s.lowHz)}–${fmtMHz(s.highHz)} MHz`;
function setScansNote(note) {
  scansNote = note;
  syncScansNote();
}
function syncScansNote() {
  let note = scansNote;
  if (!note && streamState === 'open' && !scansKnown()) {
    note = 'The engine that runs keeps no scans: restart Radiolack after its update.';
  }
  const el = $('#ks-note');
  if (el.textContent !== note) el.textContent = note;
  el.hidden = !note;
}
// What a kept scan's line says under its name: its start, its range and, while it runs, that it does.
function scanLine(s) {
  return `${fmtDate(s.startS)} · ${scanRange(s)}${s.ended === null ? ' · running' : ''}`;
}
function renderScans() {
  syncScansNote();
  $('#ks-list').hidden = openScanId !== null;
  $('#ks-detail').hidden = openScanId === null;
  if (openScanId === null) renderScanList(); else renderScanDetail();
}
function renderScanList() {
  const body = $('#ks-body'), refocus = focusedIn(body), list = keptScans || [];
  const html = list.length
    ? list.map((s) => `<tr><td><button type="button" class="freq-link" data-open="${s.id}">${escapeHtml(s.name)}</button>`
      + `<div class="note">${escapeHtml(scanLine(s))}</div></td><td>${s.channels}</td></tr>`).join('')
    : `<tr><td colspan="2" class="empty">${keptScans === null ? 'Not received yet' : 'No scan kept yet'}</td></tr>`;
  if (html !== scansHtml) {
    body.innerHTML = html;
    scansHtml = html;
    focusAgain(body, refocus);
  }
  const done = list.filter((s) => s.ended !== null).length;
  const summary = list.length ? `${list.length} ${list.length === 1 ? 'scan' : 'scans'} kept.` : '';
  const el = $('#ks-summary');
  if (el.textContent !== summary) el.textContent = summary;
  const all = $('#ks-clear-all');
  all.hidden = !scansKnown() || done === 0;
  const label = scansArmed === 'all' ? 'Confirm: clear all' : 'Clear all';
  if (all.textContent !== label) all.textContent = label;
}
function renderScanDetail() {
  const summary = (keptScans || []).find((s) => s.id === openScanId);
  if (!summary) return;
  const nameEl = $('#ks-name');
  if (!typingIn.has(nameEl) && nameEl.value !== summary.name) nameEl.value = summary.name;
  const passes = summary.passesSet === null ? `${summary.passes} ${summary.passes === 1 ? 'pass' : 'passes'}`
    : `${summary.passes} of ${summary.passesSet} passes`;
  const info = [`${fmtDate(summary.startS)}${summary.endS === null ? ', running' : ` to ${fmtDate(summary.endS)}`}`,
    scanRange(summary), passes, summary.listen ? 'stopped to listen' : 'did not listen',
    summary.ended === null ? '' : SCAN_ENDINGS[summary.ended] || 'Ended'].filter(Boolean).join(' · ');
  const infoEl = $('#ks-info');
  if (infoEl.textContent !== info) infoEl.textContent = info;
  const body = $('#ks-channels'), refocus = focusedIn(body);
  const channels = openScan ? openScan.channels : null;
  const html = channels && channels.length
    ? channels.map((c) => {
      const f = c.frequencyHz;
      const title = `Listen · first found ${fmtDate(c.firstS)}, last ${fmtDate(c.lastS)}`;
      return `<tr><td><button type="button" class="freq-link" data-tune="${f}" title="${escapeHtml(title)}">${fmtMHz(f)}</button></td>`
        + `<td>${c.searches}×</td><td>+${Math.round(c.marginDb)} dB</td>`
        + `<td>${summary.listen ? fmtActive(c.openS) : ''}</td>`
        + `<td class="acts"><button type="button" class="link" data-mark="${f}">Bookmark</button></td></tr>`;
    }).join('')
    : `<tr><td colspan="5" class="empty">${channels === null ? 'Not received yet' : 'No channel found'}</td></tr>`;
  if (html !== scanChannelsHtml) {
    body.innerHTML = html;
    scanChannelsHtml = html;
    focusAgain(body, refocus);
  }
  const del = $('#ks-delete');
  del.hidden = summary.ended === null;      // the scan that runs is stopped first
  const label = scansArmed === summary.id ? 'Confirm: delete' : 'Delete';
  if (del.textContent !== label) del.textContent = label;
}
function openKeptScan(id) {
  openScanId = id;
  openScan = null;
  scanChannelsHtml = '';
  disarmScans();
  typingIn.delete($('#ks-name'));
  if (!stream.askScan(id)) setScansNote(notConnected('its channels did not come'));
  renderScans();
}
function closeScan() {
  openScanId = null;
  openScan = null;
  disarmScans();
  if (rangeShown()) renderScans();
}
// A first press arms Delete or Clear all; a second one, within SCANS_CONFIRM_MS, sends it.
function armScans(what) {
  clearTimeout(scansArmTimer);
  scansArmed = what;
  scansArmTimer = setTimeout(disarmScans, SCANS_CONFIRM_MS);
  renderScans();
}
function disarmScans() {
  clearTimeout(scansArmTimer);
  scansArmed = null;
  if (rangeShown()) renderScans();
}
$('#ks-body').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b && b.dataset.open) openKeptScan(Number(b.dataset.open));
});
$('#ks-channels').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.tune) tuneAnywhere(Number(b.dataset.tune));
  else if (b.dataset.mark) {
    const f = Number(b.dataset.mark), seg = segmentAt(f);
    const name = addBookmark(f, planMode(modeAt(plan, f) ?? (seg ? seg.mode : null), S.mode));
    setScansNote(name ? `${fmtMHz(f)} MHz added to the bookmarks as ${name}.` : notConnected('no bookmark was added'));
  }
});
$('#ks-back').addEventListener('click', () => { setScansNote(''); closeScan(); });
const ksName = $('#ks-name');
// What the user types stays until Rename, or another scan opens: a press on
// Rename blurs the field first, which must not bring the old name back.
ksName.addEventListener('input', () => typingIn.add(ksName));
function renameKeptScan() {
  typingIn.delete(ksName);
  const name = ksName.value.trim();
  if (!name) { setScansNote('A scan needs a name.'); renderScans(); return; }
  setScansNote(stream.renameScan(openScanId, name) ? '' : notConnected('the scan was not renamed'));
}
$('#ks-rename').addEventListener('click', renameKeptScan);
ksName.addEventListener('keydown', (e) => { if (e.key === 'Enter') renameKeptScan(); });
$('#ks-delete').addEventListener('click', () => {
  if (scansArmed !== openScanId) { armScans(openScanId); return; }
  disarmScans();
  setScansNote(stream.deleteScan(openScanId) ? '' : notConnected('the scan was not deleted'));
});
$('#ks-clear-all').addEventListener('click', () => {
  if (scansArmed !== 'all') { armScans('all'); return; }
  disarmScans();
  setScansNote(stream.clearScans() ? '' : notConnected('nothing was cleared'));
});

// ------------------------------------------------------------------ menu
// Every menu behind one icon. The menu opens as a card under the readout,
// the list of menus beside the chosen one; in one column (oneColumnQuery),
// the list first, then the chosen menu with a back arrow. Escape, a press
// outside it and its close button close it; the keys 1 to 8 open each menu,
// or close it when it shows already. Which menu shows is this page's alone.
let openName = null;        // the menu whose contents show, or null: the menu is closed or shows its list
let lastMenu = MENUS[0];    // the menu that the icon opens again
let scanTab = 'signals';
const menuTitle = (name) => $(`#menu nav button[data-menu="${name}"] span`).textContent;
const signalsShown = () => openName === 'scan' && scanTab === 'signals';
function openMenuCard() {
  if (!menuEl.hidden) return;
  closeCtx();
  menuEl.hidden = false;
  menuScrim.hidden = false;
  menuButton.setAttribute('aria-expanded', 'true');
  document.body.classList.remove('idle');
}
// A name being edited is kept when its menu goes: its field takes no more keys.
function leaveMenu() {
  if (openName !== 'bookmarks') return;
  if (editing) finishEdit(true, false);
  if (naming) finishNaming(true, false);
  if (bookmarkDrag) endDrag();
  if (selecting) setSelecting(false);
}
function showMenu(name) {
  if (openName !== name) leaveMenu();
  openName = name;
  lastMenu = name;
  menuEl.dataset.level = 'content';
  for (const b of $$('#menu nav button[data-menu]')) b.setAttribute('aria-current', String(b.dataset.menu === name));
  for (const s of $$('#menu section[data-menu]')) s.hidden = s.dataset.menu !== name;
  $('#menu-title').textContent = menuTitle(name);
  openMenuCard();
  if (name === 'bookmarks') { renderBookmarks(); renderPlanList(); }
  if (name === 'scan') syncScanTabs();
  if (name === 'record') { renderRecordings(); askRecordings(); }
  if (name === 'receiver') syncDevices();
  followRtlList();
  liveUi();
}
// One column: the list of menus, the last one shown marked as current.
function showMenuList() {
  leaveMenu();
  openName = null;
  menuEl.dataset.level = 'nav';
  openMenuCard();
  followRtlList();
}
// The icon reopens the last menu shown; in one column, the list of menus.
function openMenu() {
  if (oneColumnQuery.matches) showMenuList(); else showMenu(lastMenu);
}
// Closed from the keyboard, the focus that was in the menu returns to its icon.
function closeMenu(keyboard = false) {
  if (menuEl.hidden) return;
  const focused = document.activeElement, hadFocus = menuEl.contains(focused);
  leaveMenu();
  openName = null;
  menuEl.hidden = true;
  menuScrim.hidden = true;
  menuButton.setAttribute('aria-expanded', 'false');
  if (hadFocus && keyboard) menuButton.focus();
  else if (hadFocus) focused.blur();
  followRtlList();
  poke();
}
// The RTL-SDR receivers plugged in: the engine reads them when the Receiver
// menu shows, then every RTL_LIST_MS while it shows, so that a receiver
// plugged in meanwhile appears; never otherwise.
function followRtlList() {
  const wanted = !menuEl.hidden && openName === 'receiver' && streamState === 'open'
    && stream.listings.includes('rtl_sdr');
  if (wanted && !rtlTimer) {
    stream.listRtlDevices();
    rtlTimer = setInterval(() => stream.listRtlDevices(), RTL_LIST_MS);
  } else if (!wanted && rtlTimer) {
    clearInterval(rtlTimer);
    rtlTimer = 0;
  }
}
function toggleMenu(name) { if (openName === name) closeMenu(true); else showMenu(name); }
// The dot on the menu's icon: red while a recording runs, green while a scan does.
function syncMenuBadge() {
  const recordingNow = recordingRuns() || iqRuns(), scanningNow = memoryRuns() || sweepRuns();
  if (menuButton.classList.contains('recording') !== recordingNow) menuButton.classList.toggle('recording', recordingNow);
  if (menuButton.classList.contains('scanning') !== scanningNow) menuButton.classList.toggle('scanning', scanningNow);
}

const syncers = [];
const rangeInputs = new Map();      // key of S → the range input that sets it
function bindCheck(id, key, after) {
  const el = $(`#${id}`);
  const sync = () => { el.checked = Boolean(S[key]); };
  sync();
  syncers.push(sync);
  el.addEventListener('change', () => { S[key] = el.checked; if (after) after(); });
  return el;
}
function bindRange(id, key, fmt, after) {
  const el = $(`#${id}`), out = $(`#${id}-v`);
  const sync = () => { el.value = String(S[key]); if (out) out.textContent = fmt(S[key]); };
  sync();
  syncers.push(sync);
  rangeInputs.set(key, el);
  el.addEventListener('input', () => { S[key] = Number(el.value); if (out) out.textContent = fmt(S[key]); if (after) after(); });
  return el;
}

// Source: what the engine captures, and the receiver settings it takes. The
// engine's state is the reference: a change shows once the engine sends it
// back, to every interface. The Receiver list switches between the kinds of
// source the engine opens, each with its own saved settings, and an rtl_tcp
// source can move to another server address. DC removal is the engine's own
// setting for any source.
let manualGainDb = null;    // the last manual gain, offered again when automatic gain is turned off
function sourceNote() {
  if (!source) return '';
  let text = '';
  if (source.kind === 'synthetic') {
    text = source.kinds.length > 1 ? '2 m band simulated by the engine. Choose a receiver in the list above.'
      : '2 m band simulated by the engine. Start Radiolack with --rtl-sdr to use the RTL-SDR receiver '
        + 'plugged into this computer, or with --rtl-tcp for one served by rtl_tcp.';
  } else if (source.kind === 'rtl_sdr' || source.kind === 'rtl_tcp') {
    const where = source.kind === 'rtl_sdr' ? 'on this computer, through rtl_tcp run by Radiolack'
      : `through rtl_tcp at ${source.address}`;
    text = `${source.tuner || 'RTL-SDR receiver'} ${where}.`;
  } else if (source.kind === 'soapy') {
    text = `${source.tuner || 'SoapySDR device'} through SoapySDR${source.driver ? `, driver ${source.driver}` : ''}.`;
  }
  if (soapyPicked) {
    text = stream.remote ? 'Choose a SoapySDR device below.' : 'Choose a SoapySDR device below, or give its arguments.';
  }
  if (['rtl_sdr', 'rtl_tcp', 'soapy'].includes(source.kind)) {
    if (source.status === 'waiting') text += ` Waiting: ${source.detail}. Radiolack tries again by itself.`;
    else if (source.status !== 'streaming') text += ' Connecting…';
  }
  if (sourceError) text += ` Change refused: ${sourceError}.`;
  else if (source.deviceRefusal) text += ` Change refused: ${source.deviceRefusal}.`;
  return text.trim();
}
// The rates the receiver offers, when its state lists them; otherwise the
// list as it stands. Rewritten only when they change.
function offerRates(select, ratesHz) {
  const values = ratesHz.map(String);
  if (!values.length || values.join() === Array.from(select.options, (o) => o.value).join()) return;
  select.replaceChildren(...ratesHz.map((rateHz) => new Option(`${(rateHz / 1e6).toFixed(3)} MS/s`, String(rateHz))));
}
function selectRate(select, rateHz) {
  const value = String(rateHz);
  if (!Array.from(select.options).some((o) => o.value === value)) select.add(new Option(`${(rateHz / 1e6).toFixed(3)} MS/s`, value));
  select.value = value;
}
function syncSource() {
  const settable = new Set(streamState === 'open' && source ? source.settable : []);
  const deviceEl = $('#src-device');
  const kinds = streamState === 'open' && source ? source.kinds : [];
  // No entry is selected before the engine answers; "SoapySDR device" while one is to be chosen.
  const receivers = receiverList(source, rtlListed, soapyPicked, streamState === 'open');
  const entries = receivers.options.map((o) => `${o.value}\n${o.label}\n${o.disabled}`).join('\n');
  if (deviceEl.dataset.options !== entries) {
    deviceEl.replaceChildren(...receivers.options.map((o) => {
      const option = new Option(o.label, o.value);
      option.disabled = o.disabled;
      return option;
    }));
    deviceEl.dataset.options = entries;
  }
  deviceEl.disabled = kinds.length < 2;
  if (deviceEl.value !== receivers.value) deviceEl.value = receivers.value;
  $('#src-note').textContent = [sourceNote(), receivers.note].filter(Boolean).join(' ');
  syncDevices();
  const addressEl = $('#src-address');
  $('#src-address-field').hidden = !source || !source.settable.includes('address');
  if (!typingIn.has(addressEl)) addressEl.value = source?.address ?? '';
  addressEl.disabled = !settable.has('address');
  const centreEl = $('#src-center');
  if (!typingIn.has(centreEl)) centreEl.value = formatFrequency(source ? source.centreHz : S.fc);
  centreEl.disabled = !settable.has('centre_hz');
  const centreNote = centreInUseNote(source);
  const centreNoteEl = $('#src-center-note');
  centreNoteEl.hidden = !centreNote;
  centreNoteEl.textContent = centreNote;
  const rateEl = $('#src-rate');
  if (source) offerRates(rateEl, source.sampleRatesHz);
  selectRate(rateEl, source ? source.rateHz : S.fs);
  rateEl.disabled = !settable.has('sample_rate_hz');
  const shortwave = shortwaveView(source);
  $('#src-hf-field').hidden = !shortwave.shown;
  const hfEl = $('#src-hf');
  for (const option of hfEl.options) option.hidden = option.disabled = !shortwave.offered.includes(option.value);
  hfEl.value = shortwave.value;
  hfEl.disabled = !settable.has('hf_input');
  $('#src-hf-note').textContent = shortwave.note;
  const converter = converterView(source);
  $('#src-converter-field').hidden = $('#src-converter-lo-field').hidden = !converter.shown;
  const converterEl = $('#src-converter');
  converterEl.value = converter.value;
  converterEl.disabled = !settable.has('converter');
  $('#src-converter-note').textContent = converter.note;
  const oscillatorEl = $('#src-converter-lo');
  if (!typingIn.has(oscillatorEl)) oscillatorEl.value = converter.oscillator;
  oscillatorEl.disabled = !settable.has('converter_lo_hz');
  $('#src-converter-lo-note').textContent = converter.shown ? OSCILLATOR_NOTE : '';
  $('#src-rtl-agc-row').hidden = !shortwave.shown;
  const rtlAgcEl = $('#src-rtl-agc');
  rtlAgcEl.checked = shortwave.rtlAgc;
  rtlAgcEl.disabled = !settable.has('rtl_agc') || !shortwave.rtlAgcApplies;
  // While sampling directly the tuner sleeps: its gain waits for it.
  const gainNoteEl = $('#src-gain-note');
  gainNoteEl.hidden = !shortwave.direct;
  gainNoteEl.textContent = shortwave.direct ? DIRECT_GAIN_NOTE : '';
  syncPower();
  const gains = source ? source.gainsDb : [];
  const gainSettable = settable.has('gain_db') && gains.length > 0 && !shortwave.direct;
  const auto = !source || source.gainDb === null;
  if (!auto) manualGainDb = source.gainDb;
  const gainEl = $('#src-gain');
  gainEl.max = String(Math.max(0, gains.length - 1));
  gainEl.value = String(gains.length ? nearestIndex(gains, manualGainDb ?? FIRST_MANUAL_GAIN_DB) : 0);
  gainEl.disabled = !gainSettable || auto;
  $('#src-gain-v').textContent = !source || !source.settable.includes('gain_db') ? '' : auto ? 'automatic' : `${source.gainDb.toFixed(1)} dB`;
  const agcEl = $('#src-agc');
  agcEl.checked = Boolean(source && source.settable.includes('gain_db') && auto);
  // Greyed for a receiver without automatic gain, unless on, so that it can be turned off.
  agcEl.disabled = !gainSettable || (!source.automaticGain && !auto);
  const ppmEl = $('#src-ppm');
  if (!typingIn.has(ppmEl)) ppmEl.value = ppmText(source?.ppm ?? 0);
  ppmEl.disabled = !settable.has('ppm');
  const calibrate = calibrateEntry(source, stream.measures, streamState === 'open');
  $('#src-calibrate').disabled = !calibrate.enabled;
  $('#src-calibrate-note').textContent = calibrate.note || (calibrationRuns()
    ? 'Calibrating on the stations: the receiver and the VFO move until it ends.' : '');
  const driver = driverPllView(source);
  $('#src-driver-field').hidden = !driver.shown;
  const driverEl = $('#src-driver');
  driverEl.value = driver.value;
  driverEl.disabled = !settable.has('driver_pll');
  $('#src-driver-note').textContent = driver.note;
  const dcEl = $('#src-dc');
  dcEl.checked = Boolean(source?.dcRemoval);
  dcEl.disabled = !settable.has('dc_removal');
  syncDeviceControls(settable);
}
// A SoapySDR device's antenna sockets and Advanced section (advanced.js).
const advancedHandlers = {
  onStage: (name, gainDb) => { if (!stream.sendSource({ stageGainsDb: { [name]: gainDb } })) syncSource(); },
  onSetting: (key, value) => { if (!stream.sendSource({ deviceSettings: { [key]: value } })) syncSource(); },
  typing: (el) => document.activeElement === el,
};
function syncDeviceControls(settable) {
  const view = advancedView(source);
  const antennaField = $('#src-antenna-field');
  if (antennaField.hidden !== !view.antennaShown) antennaField.hidden = !view.antennaShown;
  const antennaEl = $('#src-antenna');
  const names = view.antennas.join('\n');
  if (antennaEl.dataset.options !== names) {
    antennaEl.replaceChildren(...view.antennas.map((name) => new Option(name, name)));
    antennaEl.dataset.options = names;
  }
  if (antennaEl.value !== view.antenna) antennaEl.value = view.antenna;
  antennaEl.disabled = !settable.has('antenna');
  const section = $('#src-advanced');
  if (section.hidden !== !view.shown) section.hidden = !view.shown;
  renderAdvanced($('#src-advanced-controls'), view, streamState === 'open',
    { stages: settable.has('stage_gains_db'), settings: settable.has('device_settings') }, advancedHandlers);
}
$('#src-antenna').addEventListener('change', (e) => {
  const antenna = e.target.value;
  if (!source || antenna === source.antenna || !stream.sendSource({ antenna })) syncSource();
});
// SoapySDR devices: the devices the engine's search found, with the one in
// use, Search again and the device arguments, the last two on this
// computer's interfaces only. The engine searches when this computer's
// interface asks, first when the part shows in the open Receiver menu.
function syncDevices() {
  const view = devicesView(source, devices, soapyPicked, stream.remote, streamState === 'open');
  for (const id of ['#src-soapy-field', '#src-soapy-search-row', '#src-soapy-args-field']) {
    const hidden = id === '#src-soapy-field' ? !view.shown : id === '#src-soapy-search-row' ? !(view.shown && view.searchShown)
      : !(view.shown && view.argsShown);
    if ($(id).hidden !== hidden) $(id).hidden = hidden;
  }
  const listEl = $('#src-soapy');
  const values = view.options.map((o) => `${o.value}\n${o.label}`).join('\n');
  if (listEl.dataset.options !== values) {
    listEl.replaceChildren(...view.options.map((o) => new Option(o.label, o.value)));
    listEl.dataset.options = values;
  }
  if (listEl.value !== view.value) listEl.value = view.value;
  listEl.disabled = !view.listEnabled;
  const noteEl = $('#src-soapy-note');
  if (noteEl.textContent !== view.note) noteEl.textContent = view.note;
  $('#src-soapy-search').disabled = !view.searchEnabled;
  const argsEl = $('#src-soapy-args');
  if (!typingIn.has(argsEl) && argsEl.value !== view.args) argsEl.value = view.args;
  argsEl.disabled = !view.argsEnabled;
  if (view.firstSearch && openName === 'receiver' && !firstSearchAsked) {
    firstSearchAsked = true;
    stream.searchDevices();
  }
}
// Antenna power: a first press arms the button, which then says what
// turning it on does; a second one, within POWER_CONFIRM_MS, turns it on.
// Turning it off takes one press.
let powerArmed = false;
let powerArmTimer = 0;
function syncPower() {
  const view = powerView(source, powerArmed);
  $('#src-power-row').hidden = !view.shown;
  $('#src-power-v').textContent = view.state;
  const button = $('#src-power');
  button.textContent = view.button;
  button.setAttribute('aria-label', view.label);
  button.disabled = !(streamState === 'open' && source && source.settable.includes('bias_tee'));
}
function disarmPower() {
  clearTimeout(powerArmTimer);
  powerArmed = false;
}
$('#src-power').addEventListener('click', () => {
  if (!source) return;
  const press = powerPress(source.biasTee === true, powerArmed);
  disarmPower();
  if (press === 'arm') {
    powerArmed = true;
    powerArmTimer = setTimeout(() => { disarmPower(); syncPower(); }, POWER_CONFIRM_MS);
  } else {
    stream.sendSource({ biasTee: press === 'on' });
  }
  syncPower();
});
// Gain the slider points at, in dB, or undefined.
const sliderGain = () => (source ? source.gainsDb[Number($('#src-gain').value)] : undefined);
// A text field shows the engine's value, except while the user types in it:
// from the first keystroke until Enter or leaving the field. A value sent
// then shows as the engine took it, or the one in use if refused, focus or
// not; leaving without a change shows the engine's value again. These run
// before each field's own change handler, which may sync.
for (const el of [$('#src-address'), $('#src-center'), $('#src-ppm'), $('#src-converter-lo'), $('#src-soapy-args')]) {
  el.addEventListener('input', () => typingIn.add(el));
  el.addEventListener('change', () => typingIn.delete(el));
  el.addEventListener('blur', () => { if (typingIn.delete(el)) syncSource(); });
}
// Another receiver: the engine closes the one in use and opens the new one
// with its saved settings, for every interface.
// "SoapySDR device" before any was chosen shows the devices to choose from,
// and switches nothing until one is.
$('#src-device').addEventListener('change', (e) => {
  const kind = e.target.value;
  const wasPicked = soapyPicked;
  soapyPicked = false;
  // An RTL-SDR receiver listed: the engine opens it by its serial number.
  const rtl = rtlDeviceAt(rtlListed, kind);
  if (rtl) {
    if (rtl.inUse || rtl.device === null || !stream.chooseRtlDevice(rtl.device) || wasPicked) syncSource();
    return;
  }
  if (source && kind === SOAPY_KIND && kind !== source.kind && !switchesAtOnce(devices)) {
    soapyPicked = true;
    sourceError = '';
    syncSource();
    return;
  }
  if (!source || kind === source.kind || !stream.sendSource({ kind }) || wasPicked) syncSource();
});
// A device found: the engine opens it, by its driver and serial.
$('#src-soapy').addEventListener('change', (e) => {
  const device = deviceAt(devices, e.target.value);
  if (!device || device.inUse || !stream.chooseDevice(device.driver, device.serial)) syncSource();
});
$('#src-soapy-search').addEventListener('click', () => { stream.searchDevices(); });
// The engine reads the arguments and says why it refuses them; an emptied
// field or the arguments in use change nothing.
$('#src-soapy-args').addEventListener('change', (e) => {
  const el = e.target;
  const args = el.value.trim();
  if (!args || args === devices?.args || !stream.openDevice(args)) el.value = devices?.args ?? '';
});
// The engine reads the address and says why it refuses one; an emptied field
// or the address in use changes nothing.
$('#src-address').addEventListener('change', (e) => {
  const el = e.target;
  const address = el.value.trim();
  if (!source || !address || address === source.address || !stream.sendSource({ address })) el.value = source?.address ?? '';
});
$('#src-center').addEventListener('change', (e) => {
  const hz = parseFrequency(e.target.value);
  if (!source || hz === null || hz === source.centreHz) { syncSource(); return; }
  if (!requestCentre(hz, hz + (S.vfo - S.fc), {})) syncSource();     // the VFO keeps its place in the span
});
$('#src-rate').addEventListener('change', (e) => { if (!stream.sendSource({ rateHz: Number(e.target.value) })) syncSource(); });
$('#src-gain').addEventListener('input', () => { const g = sliderGain(); if (g !== undefined) $('#src-gain-v').textContent = `${g.toFixed(1)} dB`; });
$('#src-gain').addEventListener('change', () => { const g = sliderGain(); if (g === undefined || !stream.sendSource({ gainDb: g })) syncSource(); });
$('#src-agc').addEventListener('change', (e) => {
  const gainDb = e.target.checked ? null : sliderGain();
  if (gainDb === undefined || !stream.sendSource({ gainDb })) syncSource();
});
$('#src-ppm').addEventListener('change', (e) => {
  const el = e.target;
  // Hundredths of a ppm within the field's range, as the engine takes them.
  const ppm = typedPpm(el.valueAsNumber, Number(el.min), Number(el.max));
  if (source && Number.isFinite(ppm) && ppm !== source.ppm && stream.sendSource({ ppm })) {
    el.value = ppmText(ppm);
  } else {
    el.value = ppmText(source?.ppm ?? 0);
    syncSource();
  }
});
$('#src-dc').addEventListener('change', (e) => { if (!stream.sendSource({ dcRemoval: e.target.checked })) syncSource(); });
// Another shortwave input: the engine brings the centre frequency within its reach.
$('#src-hf').addEventListener('change', (e) => {
  const hfInput = e.target.value;
  if (!source || hfInput === source.hfInput || !stream.sendSource({ hfInput })) syncSource();
});
$('#src-rtl-agc').addEventListener('change', (e) => { if (!stream.sendSource({ rtlAgc: e.target.checked })) syncSource(); });
// The driver's computation: the samples' label changes, the receiver stays where it is.
$('#src-driver').addEventListener('change', (e) => {
  const driverPll = e.target.value;
  if (!source || driverPll === source.driverPll || !stream.sendSource({ driverPll })) syncSource();
});
// Another converter or oscillator: the receiver stays where it is tuned, and
// the engine refuses one that would put it beyond its reach on the air.
$('#src-converter').addEventListener('change', (e) => {
  const converter = e.target.value;
  if (!source || converter === source.converter || !stream.sendSource({ converter })) syncSource();
});
$('#src-converter-lo').addEventListener('change', (e) => {
  const hz = source ? oscillatorChange(e.target.value, source.converterLoHz) : null;
  if (hz === null || !stream.sendSource({ converterLoHz: hz })) syncSource();
});

// Tune to f. Outside the span, with a source that tunes, the engine moves the
// span first, and the VFO goes to f once frames come from there. Beyond the
// receiver's reach, the span of a source that does not tune included,
// nothing tunes and the status line says why.
function tuneAnywhere(f, opts = {}) {
  if (!Number.isFinite(f)) return;
  if (beyondReach(f)) { showStatusNote(beyondReachNote(f, reach())); return; }
  if (streamState === 'open' && sourceTunes()) {
    f = Math.round(f);
    const [lo, hi] = targetSpan();
    if (f < lo || f > hi) {
      if (retuneFor(f, opts)) return;
    } else if (pendingTune) {
      pendingTune.f = f;              // within the span on its way: the VFO goes there with it
      pendingTune.opts = opts;
      updateOverlays();
      return;
    }
  }
  tune(f, opts);
}
// What the source can show: its centre range widened by half a span, or the
// span alone when it does not tune.
function reach() {
  if (!sourceTunes()) return [fStart(), fEnd()];
  const half = source.rateHz / 2;
  return [source.rangeHz[0] - half, source.rangeHz[1] + half];
}
function reachable(f) { return withinReach(f, reach()); }
// Whether f lies beyond the reach of the receiver in use: false while none is known.
const beyondReach = (f) => streamState === 'open' && source !== null && !reachable(f);
const spanAround = (centre) => [centre - (source ? source.rateHz : S.fs) / 2, centre + (source ? source.rateHz : S.fs) / 2];
// The span the VFO has to lie in: the one a retune on its way brings, else the one in use.
const targetSpan = () => (pendingTune ? spanAround(pendingCentre()) : [fStart(), fEnd()]);
// Where the span goes for the VFO to sit at f: nearly centred, f a little
// above the centre, clear of the DC residue there and of the channel's lower
// half, within the receiver's range. The mode is the one tune() will pick.
// The VFO's channel at f: how far it reaches below and above f.
function retuneChannel(f, opts) {
  const seg = segmentAt(f), picked = opts.mode || (S.autoMode && seg ? planMode(seg.mode, S.mode) : S.mode);
  const mode = MODES[picked] ? picked : S.mode, bw = mode === S.mode ? S.bw : MODES[mode].bw;
  const below = mode === 'USB' ? 0 : mode === 'LSB' ? bw : bw / 2;
  return { below, above: bw - below };
}
function retuneCentre(f, opts) {
  const offset = Math.max(source.rateHz * RETUNE_SHARE, retuneChannel(f, opts).below + RETUNE_MARGIN_HZ);
  return Math.round(clamp(f - offset, source.rangeHz[0], source.rangeHz[1]));
}
// Move the span for the VFO to sit at f; false when the span would stay where
// it is, or goes, or the engine cannot be asked.
function retuneFor(f, opts) {
  const centre = retuneCentre(f, opts);
  return centre !== (pendingTune ? pendingTune.centre : source.centreHz) && requestCentre(centre, f, opts);
}
// Ask the engine for the span around centre; the VFO goes to f once frames
// come from where the receiver sits for it, the centre in use, which the
// engine reports. A newer request replaces one still on its way.
function requestCentre(centre, f, opts) {
  if (!stream.sendSource({ centreHz: centre })) return false;
  const before = pendingTune ? [...pendingTune.before, pendingTune.centre] : [source.centreHz];
  pendingTune = { centre, before, f, opts, inUse: null };
  viewChanged();      // the outlines, and the tip under a pointer that has not moved
  return true;
}
// The engine reported the centre in use for the retune on its way: a tuner
// placed on its grid may have left the VFO's channel over the DC residue,
// in which case the retune goes to a point of the grid below; frames
// already from there end it at once.
function pendingPlaced(state) {
  const pending = pendingTune;
  pending.inUse = state.centreInUseHz;
  const { below, above } = retuneChannel(pending.f, pending.opts);
  const centre = clearedCentre({
    f: pending.f, asked: pending.centre, inUse: pending.inUse, clearHz: below + RETUNE_MARGIN_HZ, aboveHz: above,
    stepHz: gridStepHz(state), rateHz: state.rateHz, lowestHz: state.rangeHz[0],
  });
  if (centre !== null && centre !== pending.centre && requestCentre(centre, pending.f, pending.opts)) return;
  if (pending.inUse === S.fc && state.rateHz === S.fs && !newSource) {
    pendingTune = null;
    tune(pending.f, pending.opts);
  }
}
// The centre of the span a retune on its way brings: the one in use once known.
const pendingCentre = () => pendingTune.inUse ?? pendingTune.centre;
function dropPendingTune() {
  if (!pendingTune) return;
  pendingTune = null;
  viewChanged();
}

// Demodulation
const bwEl = $('#dm-bw'), sqlEl = $('#dm-sql');
function syncDemod() {
  for (const b of $$('#mode-group button')) b.setAttribute('aria-checked', String(b.dataset.mode === S.mode));
  const m = MODES[S.mode];
  bwEl.min = String(m.min); bwEl.max = String(m.max); bwEl.step = String(m.step); bwEl.value = String(S.bw);
  $('#dm-bw-v').textContent = fmtBw(S.bw);
  $('#dm-step').value = S.step;
  syncSql();
  syncNotch();
  syncBlanker();
}
// The threshold set by hand, or the automatic one in use. Also refreshed
// while the Radio menu shows: only what changed is written.
function syncSql() {
  const autoEl = $('#dm-sql-auto'), out = $('#dm-sql-v');
  const db = S.sqlAuto ? meterSqlDb : S.sql;
  if (autoEl.checked !== S.sqlAuto) autoEl.checked = S.sqlAuto;
  if (sqlEl.disabled !== S.sqlAuto) sqlEl.disabled = S.sqlAuto;
  if (db !== null && sqlEl.value !== String(Math.round(db))) sqlEl.value = String(Math.round(db));
  const text = db === null ? '—' : fmtDb(db);
  if (out.textContent !== text) out.textContent = text;
}
// The automatic notch, the engine's setting, shared by every interface: it
// acts in AM, SAM, USB and LSB only, and says while it acts how many lines it
// removes. Only what changed is written.
const NOTCH_MODES = ['AM', 'SAM', 'USB', 'LSB'];
const notchEl = $('#dm-notch');
function syncNotch() {
  const known = S.notch !== null, acts = known && NOTCH_MODES.includes(S.mode);
  if (notchEl.checked !== Boolean(S.notch)) notchEl.checked = Boolean(S.notch);
  if (notchEl.disabled !== !acts) notchEl.disabled = !acts;
  let text = '';
  if (!known) text = 'The notch needs Radiolack to restart.';
  else if (!acts) text = `No notch in ${S.mode}: it acts in AM, SAM, USB and LSB.`;
  else if (S.notch && notchLines) text = `Removing ${notchLines} ${notchLines === 1 ? 'line' : 'lines'}.`;
  const el = $('#dm-notch-note');
  if (el.textContent !== text) el.textContent = text;
  if (el.hidden !== !text) el.hidden = !text;
}
notchEl.addEventListener('change', () => {
  const before = demodSnapshot();
  S.notch = notchEl.checked;
  sendDemod(before);
  syncNotch();
});
// The noise blanker, the engine's setting, shared by every interface: it acts
// in every mode, and says how many impulses it zeroed in the latest second.
// Only what changed is written.
const blankerEl = $('#dm-nb');
function syncBlanker() {
  const known = S.blanker !== null;
  if (blankerEl.checked !== Boolean(S.blanker)) blankerEl.checked = Boolean(S.blanker);
  if (blankerEl.disabled !== !known) blankerEl.disabled = !known;
  let text = '';
  if (!known) text = 'The noise blanker needs Radiolack to restart.';
  else if (S.blanker && blankerImpulses) {
    text = `Blanking ${blankerImpulses} ${blankerImpulses === 1 ? 'impulse' : 'impulses'} a second.`;
  }
  const el = $('#dm-nb-note');
  if (el.textContent !== text) el.textContent = text;
  if (el.hidden !== !text) el.hidden = !text;
}
blankerEl.addEventListener('change', () => {
  const before = demodSnapshot();
  S.blanker = blankerEl.checked;
  sendDemod(before);
  syncBlanker();
});
// Why this interface plays nothing, if it does not.
function syncAudioNote() {
  let text = '';
  if (demodError) text = `Change refused: ${demodError}.`;
  else if (!audioModes.includes(S.mode)) text = `${S.mode} is not demodulated yet: only ${audioModes.join(' and ')} plays.`;
  else if (modeRateNote(S.mode, S.fs)) text = `${modeRateNote(S.mode, S.fs)}.`;
  else if (audioState === 'blocked') text = 'Audio starts with a click, a tap or a key press on the page.';
  else if (audioState === 'failed') text = 'Audio cannot play in this browser.';
  const el = $('#dm-audio-note');
  if (el.textContent !== text) el.textContent = text;
  el.hidden = !text;
}
for (const b of $$('#mode-group button')) b.addEventListener('click', () => setMode(b.dataset.mode));
bindCheck('dm-auto', 'autoMode');
bwEl.addEventListener('input', () => setBandwidth(Number(bwEl.value)));
$('#dm-step').addEventListener('change', (e) => { S.step = e.target.value; updateMeta(); });
$('#dm-sql-auto').addEventListener('change', (e) => {
  const before = demodSnapshot();
  S.sqlAuto = e.target.checked;
  // By hand, the threshold starts where the automatic one stood.
  if (!S.sqlAuto && meterSqlDb !== null) S.sql = clamp(Math.round(meterSqlDb), Number(sqlEl.min), Number(sqlEl.max));
  sendDemod(before);
  syncSql();
});
sqlEl.addEventListener('input', () => {
  const before = demodSnapshot();
  S.sql = Number(sqlEl.value);
  sendDemod(before);
  syncSql();
});
// The squelch on the S-meter: the wheel moves the threshold by 1 dB a notch,
// up raising it, and a click or a tap on the bar sets it at that point of the
// meter's scale, both within the Radio menu's range. Either sets it by hand,
// starting from the automatic threshold in use, as unticking Automatic does.
let sqlWheelAcc = 0;
function setSquelchDb(db) {
  const before = demodSnapshot();
  S.sqlAuto = false;
  S.sql = clamp(Math.round(db), Number(sqlEl.min), Number(sqlEl.max));
  sendDemod(before);
  syncSql();
}
function stepSquelch(dir) {
  const from = S.sqlAuto && meterSqlDb !== null ? Math.round(meterSqlDb) : S.sql;
  setSquelchDb(from + dir);
}
meterBar.addEventListener('click', (e) => {
  const r = meterBar.getBoundingClientRect();
  setSquelchDb(S_ZERO + clamp((e.clientX - r.left) / r.width, 0, 1) * (S_TOP - S_ZERO));
});
$('#smeter').addEventListener('wheel', (e) => {
  e.preventDefault();
  poke();
  if (e.ctrlKey || e.metaKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;    // a pinch or a sideways swipe
  // A notch moves it 1 dB, as does each SQL_WHEEL_PX of a touchpad's smaller steps.
  const px = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
  if (Math.abs(px) >= SQL_WHEEL_PX) { sqlWheelAcc = 0; stepSquelch(px < 0 ? 1 : -1); return; }
  sqlWheelAcc += px;
  if (Math.abs(sqlWheelAcc) >= SQL_WHEEL_PX) { stepSquelch(sqlWheelAcc < 0 ? 1 : -1); sqlWheelAcc = 0; }
}, { passive: false });
bindRange('dm-vol', 'volume', (v) => `${v}%`, () => {
  audio.setVolume(S.volume / 100);
  syncAudio();
});

// Display
const avgLabel = (v) => (v === 0 ? 'none' : v < 0.4 ? `light (${v.toFixed(2)})` : v < 0.75 ? `medium (${v.toFixed(2)})` : `strong (${v.toFixed(2)})`);
function drawPalettePreview() { $('#wf-pal-preview').style.background = `linear-gradient(90deg, ${PALETTES[S.palette].join(', ')})`; }
function syncDisplay() {
  for (const s of syncers) s();
  syncResolution();
  $('#wf-pal').value = S.palette;
  drawPalettePreview();
  syncAutoDisplay();
}
function syncAutoDisplay() {
  const top = $('#ds-top'), lo = $('#wf-min'), hi = $('#wf-max');
  top.disabled = S.autoRange;
  lo.disabled = S.wfAuto;
  hi.disabled = S.wfAuto;
  if (S.autoRange) { top.value = String(Math.round(autoTop)); $('#ds-top-v').textContent = fmtDb(autoTop); }
  if (S.wfAuto) {
    lo.value = String(Math.round(autoWf.min)); $('#wf-min-v').textContent = fmtDb(autoWf.min);
    hi.value = String(Math.round(autoWf.max)); $('#wf-max-v').textContent = fmtDb(autoWf.max);
  }
}
bindRange('ds-avg', 'avg', avgLabel);
bindCheck('ds-fill', 'fill');
function clearPeaks() { wide.clearPeaks(); fine.clearPeaks(); }
bindCheck('ds-peak', 'peakHold', () => { if (S.peakHold) clearPeaks(); });
$('#ds-peak-clear').addEventListener('click', clearPeaks);
bindCheck('ds-grid', 'grid');
bindCheck('ds-auto', 'autoRange', () => { if (!S.autoRange) S.top = Math.round(autoTop); syncDisplay(); });
bindRange('ds-top', 'top', fmtDb);
bindRange('ds-range', 'range', (v) => `${v} dB`);
$('#wf-pal').addEventListener('change', (e) => { S.palette = e.target.value; drawPalettePreview(); lutDirty = true; });
bindCheck('wf-auto', 'wfAuto', () => {
  if (!S.wfAuto) { S.wfMin = Math.round(autoWf.min); S.wfMax = Math.round(autoWf.max); }
  syncDisplay();
  lutDirty = true;
});
bindRange('wf-min', 'wfMin', fmtDb, () => { S.wfMin = Math.min(S.wfMin, S.wfMax - 5); lutDirty = true; });
bindRange('wf-max', 'wfMax', fmtDb, () => { S.wfMax = Math.max(S.wfMax, S.wfMin + 5); lutDirty = true; });
bindRange('wf-speed', 'wfSpeed', (v) => `${v} lines/s`);
bindCheck('wf-time', 'timestamps', rebuildStamps);
bindCheck('ov-plan', 'showPlan', drawBandStrip);
bindCheck('ov-marks', 'showMarks', renderMarks);
bindCheck('ov-channels', 'showChannels', drawBandStrip);
bindRange('ov-split', 'split', (v) => `${Math.round(v * 100)}%`, () => { layoutPending = true; });

// Bookmarks and the band plan
// The band plan list shows the ranges the engine captures, segments under their band.
// The band plan under the bookmarks follows the view, or the filter across
// the whole plan (planlist.js); rebuilt only when its rows change.
let planListKey = null, planListRows = [];
function renderPlanList() {
  const list = $('#plan-list'), note = $('#plan-note');
  if (planState !== 'ready') {
    planListKey = null;
    list.textContent = '';
    note.textContent = planState === 'failed' ? 'The band plan could not be loaded.' : '';
    return;
  }
  const query = $('#bm-filter').value.trim();
  const { rows, more } = planRows(plan, view.f0, view.f1, query);
  const key = `${query ? 'q' : 'v'} ${more} ${rows.map(rowKey).join(' ')}`;
  if (key === planListKey) return;
  planListKey = key;
  planListRows = rows;
  const where = query ? 'Ranges, channels and known frequencies that match, across the plan.' : 'Ranges and known frequencies in view.';
  const cut = more ? ` The first ${PLAN_LIST_MAX} rows: ${query ? 'refine the filter' : 'zoom in'} to see the others.` : '';
  note.textContent = `${where}${cut} ${plan.name}, a guide: national tables are the reference.`;
  let html = '';
  const swatches = [];
  rows.forEach((row, i) => {
    if (row.kind === 'group') { html += `<li class="group">${escapeHtml(row.name)}</li>`; return; }
    const [name, num, rgb, cls] = row.kind === 'range'
      ? [query ? rangeTitle(row.range) : row.range.name, `${formatFrequency(row.range.from)}–${formatFrequency(row.range.to)}`, row.range.rgb, '']
      : row.kind === 'set'
        ? [`${row.set.name} · ${row.set.channels.length} channels`, `${formatFrequency(row.set.from)}–${formatFrequency(row.set.to)}`, null, '']
        : [query ? spotName(plan.bands, row.spot) : row.spot.name, formatFrequency(row.spot.f), row.spot.rgb, query ? '' : ' sub'];
    html += `<li><button type="button" class="item${cls}" data-row="${i}"><span class="sw"></span>`
      + `<span class="name">${escapeHtml(name)}</span><span class="num">${num}</span></button></li>`;
    swatches.push(rgb);
  });
  list.innerHTML = html || `<li class="empty">${query ? 'No band plan entry matches' : 'No band plan entry in view'}</li>`;
  // Set through the CSSOM: the Content-Security-Policy blocks inline style attributes.
  list.querySelectorAll('.sw').forEach((sw, i) => { if (swatches[i]) sw.style.background = `rgb(${swatches[i]})`; else sw.classList.add('set'); });
}
const rowKey = (row) => (row.kind === 'group' ? `g${row.name}` : row.kind === 'range' ? `r${row.index}`
  : row.kind === 'set' ? `c${row.set.from}` : `s${row.spot.f}`);
// The band plan that the engine names, from its file: a plan named while
// another loads replaces it.
function loadPlan(id) {
  planId = id;
  loadBandPlan(`bandplan-${id}.json`, MODE_ORDER).then((loaded) => {
    if (id !== planId) return false;
    plan = loaded;
    planState = 'ready';
    return true;
  }, (error) => {
    if (id !== planId) return false;
    console.error(error);
    plan = { name: '', ranges: [], bands: [], channels: [], spots: [] };
    planState = 'failed';
    return true;
  }).then((current) => {
    if (!current) return;
    renderPlanList();
    lastSeg = segmentAt(S.vfo);         // the VFO's range gives its step now
    setView(view.f0, view.f1);          // the view reaches the plan's end now, and redraws the overlays
    if (signalsShown()) renderPeaks();     // signals show on their channels now
  });
}
// The Bookmarks menu's choice of plan, the engine's: the plans its hello
// offers, the one it uses chosen; with one plan alone, it names it.
function syncPlanChoice() {
  const select = $('#bm-plan'), choices = stream.bandPlans;
  const options = choices.map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`).join('');
  if (select.dataset.options !== options) { select.innerHTML = options; select.dataset.options = options; }
  if (planId !== null) select.value = planId;
  select.disabled = streamState !== 'open' || choices.length < 2;
}
$('#bm-plan').addEventListener('change', (e) => {
  if (!stream.setBandPlan(e.target.value)) syncPlanChoice();
});
// Bookmarks belong to the engine: changes go to it, and every interface
// shows them once the engine sends them back. A click on a bookmark tunes
// there with its mode, its box ticks it for the memory scan, and Edit turns
// its row into fields for its name and its frequency, a Category choice and
// a Remove link; leaving the row saves them, unless the frequency is not
// one: the row then stays open with a note. A bookmark that Add makes opens
// so, its name selected. With categories, the list shows the uncategorized
// bookmarks, then each category's, under headings that fold, tick their
// bookmarks for the scan, rename and remove the category
// (docs/design/bookmark-categories.md).
const BOOKMARK_NAME_MAX = 60;       // as the engine's bookmarks.py
const BOOKMARK_FREQUENCY_NOTE = 'Not a frequency: type it in MHz, or with Hz, kHz, MHz or GHz, from 1 Hz to 100 GHz.';
let editing = null;                 // { id, text, freq, invalid } while a bookmark is edited: its fields' texts
let editRow = null;                 // its row: one that a new list replaced changes nothing
let naming = null;                  // { id, text, reason, start } while a category is named: id null for a new one
let nameRow = null;                 // its row, as editRow
const folded = new Set();           // the folded sections' keys: this interface's own, never saved
let selecting = false;              // the Select mode: the rows' boxes select the bookmarks to move
const selected = new Set();         // the bookmarks selected, by id
let bookmarkDrag = null;            // { ids, x, y, ghost, over } from a mouse press on a bookmark until its release
let renderAfterDrag = false;        // a list that came while a bookmark was dragged
const editingOf = (b) => ({ id: b.id, text: b.name, freq: formatFrequency(b.f), invalid: false });
// A category choice's options, and the category an option names.
const categoryOptions = (current) => [`<option value="none"${current === null ? ' selected' : ''}>${UNCATEGORIZED}</option>`,
  ...categories.map((c) => `<option value="${c.id}"${c.id === current ? ' selected' : ''}>${escapeHtml(c.name)}</option>`)].join('');
const optionCategory = (value) => (value === 'none' ? null : Number(value));
function renderBookmarks() {
  if (bookmarkDrag && bookmarkDrag.ghost) { renderAfterDrag = true; return; }     // the rows stay under the pointer until the drop
  if (editing && !bookmarks.some((b) => b.id === editing.id)) editing = null;     // removed meanwhile
  if (naming && naming.id !== null && !categories.some((c) => c.id === naming.id)) naming = null;
  for (const id of selected) if (!bookmarks.some((b) => b.id === id)) selected.delete(id);
  const list = $('#bm-list'), refocus = focusedIn(list);
  // The field that had the focus, or the name's: the new row's takes its place and its selection.
  const active = document.activeElement;
  const oldField = editRow && (editRow.contains(active) && active.dataset.field ? active : editRow.querySelector('input[data-field="name"]'));
  const oldName = nameRow && nameRow.querySelector('input');
  const nameHadFocus = oldName !== null && active === oldName;
  editRow = null;
  nameRow = null;
  const q = $('#bm-filter').value.trim().toLowerCase();
  const edited = (b) => editing !== null && b.id === editing.id;
  const sections = bookmarkSections(bookmarks, categories, q, folded, editing ? editing.id : null);
  const grouped = categories.length > 0;
  // The ticked bookmarks that the memory scan leaves out say why.
  const known = memoryKnown(), watched = new Set(known ? scanner.memory.watched : []);
  const left = memoryVisits() ? 'out of reach · ' : 'outside the span · ';
  scanMarksShown = scanMarksKey();
  const namingField = (label) => `<li class="cat" data-naming><input type="text" data-field="cname" maxlength="${CATEGORY_NAME_MAX}" aria-label="${label}"></li>`;
  const bookmarkRow = (b) => (edited(b)
    ? `<li class="bm${grouped ? ' in' : ''}" data-editing><input type="text" data-field="name" maxlength="${BOOKMARK_NAME_MAX}" aria-label="Name of the bookmark at ${formatFrequency(b.f)} MHz">`
      + `<input type="text" data-field="freq" inputmode="decimal" title="In MHz, or with Hz, kHz, MHz or GHz" aria-label="Frequency of ${escapeHtml(b.name)}">`
      + `<button type="button" class="link" data-del="${b.id}" aria-label="Remove ${escapeHtml(b.name)}">Remove</button>`
      + (grouped ? `<select data-field="category" aria-label="Category of ${escapeHtml(b.name)}">${categoryOptions(b.category)}</select>` : '')
      + '</li>'
    : `<li class="bm${grouped ? ' in' : ''}">${selecting
      ? `<input type="checkbox" data-pick="${b.id}"${selected.has(b.id) ? ' checked' : ''} title="Select this bookmark" aria-label="Select ${escapeHtml(b.name)}">`
      : `<input type="checkbox" data-scan="${b.id}"${b.scan ? ' checked' : ''} title="Scan this bookmark" aria-label="Scan ${escapeHtml(b.name)}">`}`
      + `<button type="button" class="item" data-id="${b.id}" title="${escapeHtml(`${b.name} · ${formatFrequency(b.f)} MHz · ${b.mode}`)}">`
      + `<span class="name">${escapeHtml(b.name)}</span><span class="num">${known && b.scan && !watched.has(b.id) ? left : ''}${formatFrequency(b.f)}</span>`
      + `<span class="tag">${escapeHtml(b.mode)}</span></button>`
      + (selecting ? '' : `<button type="button" class="link" data-edit="${b.id}" title="${grouped ? 'Rename, retune, move or remove' : 'Rename, retune or remove'}" aria-label="Edit ${escapeHtml(b.name)}">Edit</button>`)
      + '</li>');
  const heading = (s) => {
    const name = escapeHtml(sectionName(s.category));
    if (s.category !== null && naming && naming.id === s.category.id) return namingField(`New name of the category ${name}`);
    const count = `${s.total} ${s.total === 1 ? 'bookmark' : 'bookmarks'}`;
    return `<li class="cat" data-key="${s.key}">`
      // In the Select mode the rows' boxes select: the heading's, which ticks for the scan, keeps its place unseen.
      + (selecting ? '<input type="checkbox" class="unseen" disabled aria-hidden="true">'
        : `<input type="checkbox" data-group="${s.key}"${s.ticked === 'all' ? ' checked' : ''}${s.ticked === 'empty' ? ' disabled' : ''}`
          + ` title="Scan this category's bookmarks" aria-label="Scan the bookmarks of ${name}">`)
      + `<button type="button" class="fold" data-fold="${s.key}" aria-expanded="${!s.folded}"${q ? ' disabled' : ''}`
      + ` title="${s.folded ? 'Show' : 'Hide'} its bookmarks" aria-label="${name}, ${count}"><span class="name">${name}</span><span class="num">${s.total}</span></button>`
      + (s.category === null || selecting ? ''
        : `<button type="button" class="link" data-rename="${s.category.id}" aria-label="Rename ${name}">Rename</button>`
          + `<button type="button" class="link" data-delcat="${s.category.id}" aria-label="Remove the category ${name}">Remove</button>`)
      + '</li>';
  };
  let html = naming && naming.id === null ? namingField('Name of the new category') : '';
  for (const s of sections) html += (grouped ? heading(s) : '') + s.bookmarks.map(bookmarkRow).join('');
  list.innerHTML = html || '<li class="empty">No bookmarks</li>';
  for (const box of list.querySelectorAll('input[data-group]')) {
    box.indeterminate = sections.some((s) => s.key === box.dataset.group && s.ticked === 'some');     // the dash: some ticked
  }
  const row = list.querySelector('li[data-editing]');
  let field = null;
  if (row) {
    editRow = row;
    const nameField = row.querySelector('input[data-field="name"]'), freqField = row.querySelector('input[data-field="freq"]');
    const categoryField = row.querySelector('select[data-field="category"]');
    nameField.value = editing.text;
    freqField.value = editing.freq;
    if (editing.invalid) freqField.setAttribute('aria-invalid', 'true');
    nameField.addEventListener('input', () => { if (row === editRow) editing.text = nameField.value; });
    freqField.addEventListener('input', () => { if (row === editRow) editing.freq = freqField.value; });
    // The category changes at once; the row stays open for the other fields.
    if (categoryField) {
      categoryField.addEventListener('change', () => {
        const id = editing.id;
        if (row === editRow && !sendBookmark(() => stream.changeBookmark(id, { category: optionCategory(categoryField.value) }))) renderBookmarks();
      });
    }
    for (const el of [nameField, freqField, categoryField].filter(Boolean)) {
      el.addEventListener('keydown', (e) => {
        e.stopPropagation();      // Escape cancels the changes, and leaves the menu open
        if (e.key === 'Enter') { e.preventDefault(); finishEdit(true, true); }
        else if (e.key === 'Escape') { e.preventDefault(); finishEdit(false, true); }
      });
    }
    row.addEventListener('focusout', (e) => { if (row === editRow && !row.contains(e.relatedTarget)) finishEdit(true, false); });
    const was = oldField ? oldField.dataset.field : 'name';
    field = was === 'freq' ? freqField : was === 'category' && categoryField ? categoryField : nameField;
  }
  const categoryRow = list.querySelector('li[data-naming]');
  let nameField = null;
  if (categoryRow) {
    nameRow = categoryRow;
    nameField = categoryRow.querySelector('input');
    nameField.value = naming.text;
    if (naming.reason) nameField.setAttribute('aria-invalid', 'true');
    nameField.addEventListener('input', () => {
      if (categoryRow !== nameRow) return;
      naming.text = nameField.value;
      if (naming.reason) { naming.reason = ''; nameField.removeAttribute('aria-invalid'); }
    });
    nameField.addEventListener('keydown', (e) => {
      e.stopPropagation();        // Escape leaves the name, and the menu open
      if (e.key === 'Enter') { e.preventDefault(); finishNaming(true, true); }
      else if (e.key === 'Escape') { e.preventDefault(); finishNaming(false, true); }
    });
    nameField.addEventListener('focusout', () => { if (categoryRow === nameRow) finishNaming(true, false); });
  }
  // A row left open on a frequency that is not one leaves the focus where the user took it.
  const focusOutside = document.activeElement !== null && document.activeElement !== document.body && !list.contains(document.activeElement);
  const target = refocus && list.querySelector(refocus);
  // A category's name field takes the focus when its naming starts, wherever
  // the focus was (a pressed button keeps none in WebKit), and keeps it
  // through the lists that come.
  if (nameField && naming.start) {
    nameField.focus();
    nameField.select();
  } else if (target) target.focus();
  else if (field && !(editing.invalid && focusOutside)) {
    field.focus();
    if (!oldField) field.select();
    else if (field instanceof HTMLInputElement && oldField instanceof HTMLInputElement) field.setSelectionRange(oldField.selectionStart, oldField.selectionEnd);
  } else if (nameField && nameHadFocus) {
    nameField.focus();
    nameField.setSelectionRange(oldName.selectionStart, oldName.selectionEnd);
  }
  if (naming) naming.start = false;
}
// The new name, unless empty or unchanged, and the new frequency, unless
// unchanged, go to the engine. A frequency that is not one keeps the row open,
// its field marked, with a note: false. Finished from the keyboard, the focus
// returns to the bookmark's Edit link.
function finishEdit(save, keyboard) {
  const r = editing;
  const b = r && bookmarks.find((x) => x.id === r.id);
  if (save && b) {
    const f = parseFrequency(r.freq);
    if (f === null) {
      r.invalid = true;
      if (editRow) editRow.querySelector('input[data-field="freq"]').setAttribute('aria-invalid', 'true');
      bookmarkNote = BOOKMARK_FREQUENCY_NOTE;
      syncBookmarkNote();
      return false;
    }
    const name = r.text.replace(/\s+/g, ' ').trim(), changes = {};
    if (name && name !== b.name) changes.name = name;
    if (f !== b.f) changes.frequencyHz = f;
    if (Object.keys(changes).length) sendBookmark(() => stream.changeBookmark(b.id, changes));
  }
  editing = null;
  if (bookmarkNote === BOOKMARK_FREQUENCY_NOTE) {
    bookmarkNote = '';
    syncBookmarkNote();
  }
  renderBookmarks();
  const link = keyboard && r && $(`#bm-list [data-edit="${r.id}"]`);
  if (link) link.focus();
  return true;
}
// A rebuilt list keeps the keyboard focus: the control that had it, found
// again by its data attribute.
function focusedIn(list) {
  const el = document.activeElement;
  if (!el || !list.contains(el)) return null;
  const name = ['id', 'scan', 'edit', 'del', 'pick', 'group', 'fold', 'rename', 'delcat', 'get', 'forget', 'tune', 'ignore', 'mark', 'clear']
    .find((n) => el.dataset[n]);
  return name ? `[data-${name}="${el.dataset[name]}"]` : null;
}
function focusAgain(list, selector) {
  const el = selector && list.querySelector(selector);
  if (el) el.focus();
}
// A list rebuilt between the press and the release of a click loses the
// click: the pressed button leaves the page, and the browser drops the click,
// gives it to the list, or after a touch to whatever is then under the
// finger. From a press of the main button on the list until the click that
// follows its release, a rebuild waits; render() then shows the latest. A
// touch's click comes a little after its release, within
// CLICK_AFTER_RELEASE_MS; a press released elsewhere, or that the browser
// cancels for a scroll, makes no click.
const CLICK_AFTER_RELEASE_MS = 500;
function holdUnderPointer(list, render) {
  let held = false, waiting = false, timer = 0;
  const releaseIn = (ms) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      held = false;
      if (waiting) { waiting = false; render(); }
    }, ms);
  };
  list.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    clearTimeout(timer);
    held = true;
  });
  list.addEventListener('click', () => { if (held) releaseIn(0); });     // once every listener of the click ran
  window.addEventListener('pointerup', () => { if (held) releaseIn(CLICK_AFTER_RELEASE_MS); }, true);
  window.addEventListener('pointercancel', () => { if (held) releaseIn(0); }, true);
  window.addEventListener('blur', () => { if (held) releaseIn(0); });     // its release may never come
  return {
    get held() { return held; },
    // Whether a rebuild must wait: render() then follows the release.
    wait() {
      if (held) waiting = true;
      return held;
    },
  };
}
// Send a bookmark command: every interface receives the bookmarks once the engine applies it.
function sendBookmark(send) {
  const sent = send();
  bookmarkNote = sent ? '' : 'Not connected to the engine: the bookmarks did not change.';
  syncBookmarkNote();
  return sent;
}
function syncBookmarkNote() {
  const el = $('#bm-note');
  el.textContent = bookmarkNote;
  el.hidden = !bookmarkNote;
}
function bookmarksChanged() { renderBookmarks(); syncCategoryControls(); renderMarks(); syncMem(); }
// A new bookmark is named "Bookmark" and the first number no other one
// uses: the name, once sent, or null.
// A bookmark on a known frequency or a channel takes its name, else the first free "Bookmark n".
function addBookmark(f, mode) {
  const names = new Set(bookmarks.map((b) => b.name));
  let name = nameAt(plan, f);
  if (name === null || name.length > BOOKMARK_NAME_MAX) {
    let n = 1;
    while (names.has(`Bookmark ${n}`)) n += 1;
    name = `Bookmark ${n}`;
  }
  return sendBookmark(() => stream.addBookmark({ frequencyHz: Math.round(f), name, mode })) ? name : null;
}
// The bookmark that Add made, once the engine's list brings it, opens with
// its name selected, ready to type, while the Bookmarks menu shows and no
// other name is being edited. The engine sorts the list by frequency, then
// by name: the new bookmark is the one that was not there, with its name
// and frequency.
function nameNewBookmark() {
  const n = namingBookmark;
  const b = n && bookmarks.find((x) => !n.before.has(x.id) && x.name === n.name && x.f === n.f);
  if (!b) return;     // not yet: this list came for another change
  namingBookmark = null;
  if (openName === 'bookmarks' && !editing) editing = editingOf(b);
}
// A range or a channel set frames itself; a known frequency tunes there with its mode.
$('#plan-list').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-row]'), row = b && planListRows[Number(b.dataset.row)];
  if (!row) return;
  if (row.kind === 'range') zoomToSegment(row.range);
  else if (row.kind === 'set') zoomToSegment(row.set);
  else if (row.kind === 'spot') tuneAnywhere(row.spot.f, row.spot.mode ? { mode: planMode(row.spot.mode, S.mode) } : {});
});
$('#bm-filter').addEventListener('input', () => { renderBookmarks(); renderPlanList(); });
$('#bm-add').addEventListener('click', () => {
  if (editing) finishEdit(true, false);
  const f = Math.round(S.vfo), before = new Set(bookmarks.map((b) => b.id));
  const name = addBookmark(f, S.mode);
  namingBookmark = name ? { name, f, before } : null;
});
// While a bookmark or a category's name is edited, pressing a button or a
// box of the list leaves the focus where it is: leaving the field would save
// it and rebuild the list under the pointer, and the click would be lost.
$('#bm-list').addEventListener('mousedown', (e) => {
  if ((editing || naming) && e.target.closest('button, input[type="checkbox"]')) e.preventDefault();
});
$('#bm-list').addEventListener('change', (e) => {
  const box = e.target.closest('input[type="checkbox"]');
  if (!box) return;
  if (box.dataset.pick) {
    if (box.checked) selected.add(Number(box.dataset.pick)); else selected.delete(Number(box.dataset.pick));
    syncSelectBar();
    return;
  }
  // A category's box ticks its bookmarks, or unticks them all when all are.
  if (box.dataset.group) {
    const change = sectionScanChange(bookmarks, categories, box.dataset.group);
    if (!change || !change.ids.length || !sendBookmark(() => stream.changeBookmarks(change.ids, { scan: change.scan }))) renderBookmarks();
    return;
  }
  const b = box.dataset.scan && bookmarks.find((x) => String(x.id) === box.dataset.scan);
  // Not sent: the box shows the bookmark's setting again.
  if (b && !sendBookmark(() => stream.changeBookmark(b.id, { scan: box.checked }))) renderBookmarks();
});
$('#bm-list').addEventListener('click', (e) => {
  const button = e.target.closest('button');
  if (!button) return;
  const data = button.dataset;
  if (data.del) {
    const b = bookmarks.find((x) => String(x.id) === data.del);
    if (!b) return;
    editing = null;             // the changes go with the bookmark
    renderBookmarks();
    sendBookmark(() => stream.removeBookmark(b.id));
    if (e.detail === 0) $('#bm-add').focus();     // pressed from the keyboard: the focus stays in the menu
    return;
  }
  if (editing && !finishEdit(true, false)) return;     // the frequency typed is not one: its row stays open
  if (naming && !finishNaming(true, false)) return;
  if (data.fold) {
    if (folded.has(data.fold)) folded.delete(data.fold); else folded.add(data.fold);
    renderBookmarks();
    return;
  }
  const category = categories.find((c) => String(c.id) === (data.rename || data.delcat));
  if (category && data.rename) {
    naming = { id: category.id, text: category.name, reason: '', start: true };
    renderBookmarks();
    return;
  }
  if (category) {
    askConfirm(removeQuestion(bookmarks, category), () => sendBookmark(() => stream.removeCategory(category.id)), $('#bm-new-category'));
    return;
  }
  const b = bookmarks.find((x) => String(x.id) === (data.id || data.edit));
  if (!b) return;
  if (data.id) tuneAnywhere(b.f, { mode: b.mode });
  else { editing = editingOf(b); renderBookmarks(); }
});

// A category's name, new or renamed, goes to the engine unless empty or
// unchanged. A name that cannot be one keeps its field open with the reason,
// from the keyboard: false; left otherwise, the field closes with it.
// Finished from the keyboard, the focus returns to New category or Rename.
function finishNaming(save, keyboard) {
  const n = naming;
  if (!n) return true;
  let reason = '';
  if (save && n.text.trim()) {
    const checked = checkedCategoryName(n.text, categories, n.id);
    const old = categories.find((c) => c.id === n.id);
    if ('reason' in checked) reason = checked.reason;
    else if (n.id === null) sendBookmark(() => stream.addCategory(checked.name));
    else if (old && checked.name !== old.name) sendBookmark(() => stream.renameCategory(old.id, checked.name));
  }
  if (reason && keyboard) {
    n.reason = reason;
    if (nameRow) nameRow.querySelector('input').setAttribute('aria-invalid', 'true');
    bookmarkNote = reason;
    syncBookmarkNote();
    return false;
  }
  naming = null;
  if (reason || (n.reason && bookmarkNote === n.reason)) {
    bookmarkNote = reason;
    syncBookmarkNote();
  }
  renderBookmarks();
  if (keyboard) ($(`#bm-list [data-rename="${n.id}"]`) || $('#bm-new-category')).focus();
  return true;
}
$('#bm-new-category').addEventListener('click', () => {
  if (editing && !finishEdit(true, false)) return;
  if (naming) finishNaming(true, false);
  naming = { id: null, text: '', reason: '', start: true };
  renderBookmarks();
});

// The Select mode: the rows' boxes select bookmarks, and the bar moves them
// into the category chosen, choosing and pressing Move being the
// confirmation. It needs a category, and ends with the menu.
function setSelecting(on) {
  if (on && editing && !finishEdit(true, false)) return;
  if (on && naming) finishNaming(true, false);
  selecting = on;
  selected.clear();
  renderBookmarks();
  syncCategoryControls();
}
let moveOptionsKey = '';
function syncSelectBar() {
  $('#bm-select-bar').hidden = !selecting;
  if (!selecting) return;
  $('#bm-selected').textContent = `${selected.size} selected`;
  const choice = $('#bm-move-to'), key = JSON.stringify(categories);
  if (key !== moveOptionsKey) {
    const value = choice.value;
    moveOptionsKey = key;
    choice.innerHTML = categoryOptions(null);
    if ([...choice.options].some((o) => o.value === value)) choice.value = value;
  }
  $('#bm-move').disabled = selected.size === 0;
}
// New category needs an engine that keeps categories; Select, a category to move to.
function syncCategoryControls() {
  if (selecting && !categories.length) {
    selecting = false;
    selected.clear();
    renderBookmarks();
  }
  const newCategory = $('#bm-new-category');
  newCategory.disabled = !categoriesKnown;
  newCategory.title = categoriesKnown ? '' : 'The engine that runs keeps no categories: restart Radiolack after its update.';
  const select = $('#bm-select');
  select.disabled = !categories.length && !selecting;
  select.setAttribute('aria-pressed', String(selecting));
  select.title = categories.length ? 'Select bookmarks to move them' : 'Make a category first';
  syncSelectBar();
}
$('#bm-select').addEventListener('click', () => setSelecting(!selecting));
$('#bm-select-done').addEventListener('click', (e) => {
  setSelecting(false);
  if (e.detail === 0) $('#bm-select').focus();
});
$('#bm-move').addEventListener('click', () => {
  const target = optionCategory($('#bm-move-to').value);
  const request = moveRequest(bookmarks, categories, [...selected], target);
  if (!request) {
    bookmarkNote = 'The selected bookmarks are already there.';
    syncBookmarkNote();
    return;
  }
  if (!sendBookmark(() => stream.changeBookmarks(request.ids, { category: target }))) return;
  selected.clear();
  renderBookmarks();
  syncSelectBar();
});

// Dragged with a mouse a few pixels, a bookmark's row, or the selection it
// belongs to in the Select mode, follows the pointer; dropped on a
// category's heading, or Uncategorized's, a dialog asks to move it there.
// A press released without dragging is the click that tunes.
const DRAG_START_PX = 5;
let dragEnded = false;      // the click that follows a drop is not one on the bookmark
$('#bm-list').addEventListener('pointerdown', (e) => {
  if (e.pointerType !== 'mouse' || e.button !== 0 || !categories.length || editing || naming) return;
  const item = e.target.closest('button.item[data-id]');
  if (!item) return;
  const id = Number(item.dataset.id);
  bookmarkDrag = { ids: selecting && selected.has(id) ? [...selected] : [id], x: e.clientX, y: e.clientY, ghost: null, over: null };
});
window.addEventListener('pointermove', (e) => {
  if (!bookmarkDrag) return;
  if (!bookmarkDrag.ghost) {
    if (Math.hypot(e.clientX - bookmarkDrag.x, e.clientY - bookmarkDrag.y) < DRAG_START_PX) return;
    const first = bookmarks.find((b) => b.id === bookmarkDrag.ids[0]);
    if (!first) { bookmarkDrag = null; return; }
    bookmarkDrag.ghost = document.createElement('div');
    bookmarkDrag.ghost.className = 'bm-ghost';
    bookmarkDrag.ghost.textContent = bookmarkDrag.ids.length === 1 ? first.name : `${bookmarkDrag.ids.length} bookmarks`;
    document.body.append(bookmarkDrag.ghost);
    document.body.classList.add('dragging');
    for (const id of bookmarkDrag.ids) {
      const row = $(`#bm-list button.item[data-id="${id}"]`);
      if (row) row.closest('li').classList.add('dragged');
    }
  }
  bookmarkDrag.ghost.style.transform = `translate(${e.clientX + 12}px, ${e.clientY + 8}px)`;
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const over = under && under.closest('#bm-list li.cat[data-key]');
  if (over !== bookmarkDrag.over) {
    if (bookmarkDrag.over) bookmarkDrag.over.classList.remove('drop');
    if (over) over.classList.add('drop');
    bookmarkDrag.over = over;
  }
});
window.addEventListener('pointerup', () => {
  if (!bookmarkDrag) return;
  const { ids, ghost, over } = bookmarkDrag;
  endDrag();
  if (!ghost) return;     // a click
  dragEnded = true;
  setTimeout(() => { dragEnded = false; }, 0);
  if (!over) return;
  const target = optionCategory(over.dataset.key);
  const request = moveRequest(bookmarks, categories, ids, target);
  if (!request) return;     // dropped on its own category
  askConfirm(request.question, () => {
    if (!sendBookmark(() => stream.changeBookmarks(request.ids, { category: target })) || !selecting) return;
    for (const id of request.ids) selected.delete(id);
    renderBookmarks();
    syncSelectBar();
  }, null);
}, true);
window.addEventListener('click', (e) => {
  if (dragEnded) { e.stopPropagation(); e.preventDefault(); }
}, true);
window.addEventListener('pointercancel', () => { if (bookmarkDrag) endDrag(); }, true);
window.addEventListener('blur', () => { if (bookmarkDrag) endDrag(); });
function endDrag() {
  const d = bookmarkDrag;
  bookmarkDrag = null;
  if (d.ghost) {
    d.ghost.remove();
    document.body.classList.remove('dragging');
    for (const row of $$('#bm-list li.dragged')) row.classList.remove('dragged');
  }
  if (d.over) d.over.classList.remove('drop');
  if (renderAfterDrag) {
    renderAfterDrag = false;
    renderBookmarks();
  }
}

// A question with Confirm and Cancel: Confirm does what it asks. Answered
// from the keyboard, the focus returns to the control given, or to Add.
let confirmAction = null, confirmReturn = null, confirmKeyboard = false, confirmBackdrop = false;
function askConfirm(question, action, returnTo) {
  $('#confirm-text').textContent = question;
  confirmAction = action;
  confirmReturn = returnTo;
  confirmKeyboard = false;
  confirmDialog.showModal();
}
$('#confirm-ok').addEventListener('click', (e) => {
  const action = confirmAction;
  confirmKeyboard = e.detail === 0;
  confirmDialog.close();
  if (action) action();
});
$('#confirm-cancel').addEventListener('click', (e) => {
  confirmKeyboard = e.detail === 0;
  confirmDialog.close();
});
confirmDialog.addEventListener('cancel', () => { confirmKeyboard = true; });     // Escape
confirmDialog.addEventListener('pointerdown', (e) => { confirmBackdrop = e.target === confirmDialog; });
confirmDialog.addEventListener('click', (e) => {
  if (confirmBackdrop && e.target === confirmDialog && e.detail < 2) confirmDialog.close();
  confirmBackdrop = false;
});
confirmDialog.addEventListener('close', () => {
  const back = confirmReturn;
  confirmAction = null;
  confirmReturn = null;
  if (confirmKeyboard && !menuEl.hidden) (back && back.isConnected && !back.disabled ? back : $('#bm-add')).focus();
});

// Scanners
function syncScanTabs() {
  for (const b of $$('#scan-tabs button')) b.setAttribute('aria-pressed', String(b.dataset.tab === scanTab));
  for (const p of $$('[data-tab-panel]')) p.hidden = p.dataset.tabPanel !== scanTab;
  if (scanTab === 'signals') renderPeaks();
  if (scanTab === 'history') { renderHistory(); askHistory(); }
  if (scanTab === 'sweep') renderScans();
}
for (const b of $$('#scan-tabs button')) b.addEventListener('click', () => { scanTab = b.dataset.tab; syncScanTabs(); });
pkOn.addEventListener('change', () => sendScanner(() => stream.setScanner({ detection: pkOn.checked })));
for (const [slider] of thresholds) {
  slider.addEventListener('input', () => showThreshold(Number(slider.value)));
  slider.addEventListener('change', () => sendScanner(() => stream.setScanner({ thresholdDb: Number(slider.value) })));
}
bindCheck('pk-marks', 'peakMarks');
// The Signals table, rebuilt by every list, and the History's, by every
// change, keep their rows under a pressed pointer.
const peaksHold = holdUnderPointer($('#pk-body'), () => { if (signalsShown()) renderPeaks(); });
const historyHold = holdUnderPointer($('#hist-body'), () => { if (historyShown()) renderHistory(); });
$('#pk-body').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.tune) tuneAnywhere(Number(b.dataset.tune));
  else if (shownPeaks.has(b.dataset.ignore)) sendScanner(() => stream.ignoreFrequency(shownPeaks.get(b.dataset.ignore)));
});
$('#pk-restore').addEventListener('click', () => sendScanner(() => stream.restoreIgnored()));
$('#hist-body').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.tune) tuneAnywhere(Number(b.dataset.tune));
  else if (b.dataset.mark) {
    const f = Number(b.dataset.mark), seg = segmentAt(f);
    const name = addBookmark(f, planMode(modeAt(plan, f) ?? (seg ? seg.mode : null), S.mode));
    setHistoryNote(name ? `${fmtMHz(f)} MHz added to the bookmarks as ${name}.` : notConnected('no bookmark was added'));
  } else if (b.dataset.clear) {
    const f = Number(b.dataset.clear);
    if (historyArmed === f) clearHistory(f); else armHistory(f);
  }
});
$('#hist-clear-all').addEventListener('click', () => { if (historyArmed === 'all') clearHistory('all'); else armHistory('all'); });
setInterval(() => { if (historyShown() && streamState === 'open' && !document.hidden) askHistory(); }, HISTORY_REFRESH_MS);
memStart.addEventListener('click', () => sendScanner(() => stream.setScan(memoryRuns() ? null : 'memory')));
memResume.addEventListener('input', () => { memResumeOut.textContent = `${memResume.value} s`; });
memResume.addEventListener('change', () => sendScanner(() => stream.setScanner({ resumeS: Number(memResume.value) })));
for (const el of [swFrom, swTo]) {
  el.addEventListener('input', () => typingIn.add(el));
  el.addEventListener('change', () => { typingIn.delete(el); sendSweepRange(el); });
  el.addEventListener('blur', () => { if (typingIn.delete(el)) syncSweepSettings(); });
}
swWindow.addEventListener('input', () => { swWindowOut.textContent = `${swWindow.value} s`; });
swWindow.addEventListener('change', () => sendScanner(() => stream.setScanner({ sweepWindowS: Number(swWindow.value) })));
swStart.addEventListener('click', () => {
  const runs = sweepRuns(), name = swName.value.trim();
  if (sendScanner(() => stream.setScan(runs ? null : 'sweep', runs || !scansKnown() ? undefined : name)) && !runs) {
    swName.value = '';
  }
});
swHere.addEventListener('click', () => {
  const here = currentWindow();
  if (here) sendScanner(() => stream.setScanner({ sweepLowHz: here[0], sweepHighHz: here[1] }));
});
// A number of passes outside 1 to 99, or not whole, goes back to the engine's.
function sendPasses() {
  const n = Number(swPasses.value);
  if (swPasses.value.trim() === '' || !Number.isInteger(n) || n < 1 || n > 99) { syncSweepSettings(); return; }
  sendScanner(() => stream.setScanner({ sweepPasses: n }));
}
swPasses.addEventListener('input', () => typingIn.add(swPasses));
swPasses.addEventListener('change', () => { typingIn.delete(swPasses); sendPasses(); });
swPasses.addEventListener('blur', () => { if (typingIn.delete(swPasses)) syncSweepSettings(); });
swEndless.addEventListener('change', () => {
  if (swEndless.checked) sendScanner(() => stream.setScanner({ sweepPasses: null }));
  else sendPasses();
});
swListen.addEventListener('change', () => sendScanner(() => stream.setScanner({ sweepListen: swListen.checked })));
sigListen.addEventListener('input', () => { sigListenOut.textContent = `${sigListen.value} s`; });
sigListen.addEventListener('change', () => sendScanner(() => stream.setScanner({ listenS: Number(sigListen.value) })));
sigResume.addEventListener('input', () => { sigResumeOut.textContent = `${sigResume.value} s`; });
sigResume.addEventListener('change', () => sendScanner(() => stream.setScanner({ resumeS: Number(sigResume.value) })));

// Recording: the engine records the VFO's audio, and the receiver's raw
// I/Q, when an interface asks, the same for every interface, into WAV and
// SigMF files in its recordings folder, and goes on without any interface.
// It lists the folder's recordings to the interface that asks: this one asks
// when its Record menu shows, when a recording's file comes or goes, when
// the folder changes, and every RECORDINGS_REFRESH_MS while the menu shows.
// Download fetches a file from the engine's computer, as do Data and
// Metadata for an I/Q recording's two files; Delete waits for a second press
// of the same button, within RECORDINGS_CONFIRM_MS. Only this computer's
// interfaces see the folder, and change it in the Record menu.
const RECORDINGS_REFRESH_MS = 30000;
const RECORDINGS_CONFIRM_MS = 5000;
// Why a recording stopped by itself, as the engine's output says it.
const REC_STOPPED = {
  rate: 'the audio rate changed, into or out of WFM',
  source: 'the simulated band was chosen',
  sample_rate: "the receiver's sample rate changed",
  receiver: 'another receiver was chosen',
  converter: 'the converter in front of the receiver changed',
  space: 'less than 2 GiB is free on its disk',
  full: 'its file reached the WAV limit of 4 GiB',
  disk: 'a disk error',
};
const recAudio = $('#rec-audio'), recIq = $('#rec-iq'), recFolderEl = $('#rec-folder');
let recordingList = null;   // the folder's recordings (stream.js RecordingList), once received
let recordingsStale = false; // the latest request for them did not reach the engine
let recordNote = '';        // what this interface's last recording request or download did, or why not
let folderNote = '';        // why this interface's last folder change did not happen
let recordingAsk = '';      // what this interface's last recording command changed: 'audio', 'iq' or 'folder'
let recArmed = null;        // the recording whose Delete awaits its second press, by file, or null
let recArmTimer = 0;
let recordingsHtml = '';    // the list's items, as last written
let recordingsKey = null;   // recordingKeyNow() when the list was last asked for
const recordingKnown = () => recording !== null && stream.recordings.includes('audio');
const iqKnown = () => recording !== null && stream.recordings.includes('iq');
const recordingRuns = () => recordingKnown() && recording.audio !== null;
const iqRuns = () => iqKnown() && recording.iq !== null;
const recordShown = () => openName === 'record';
// The running recordings' files and the folder: when any changes, so does the list.
const recordingKeyNow = () => (recording
  ? [recording.audio && recording.audio.file, recording.iq && recording.iq.file, recording.folder].map((v) => v ?? '').join('\n')
  : '');
const fmtRate = (hz) => `${(hz / 1e6).toFixed(3)} MS/s`;
// A length in seconds as m:ss, or h:mm:ss from an hour on.
function fmtLength(seconds) {
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${m}:${pad2(s % 60)}`;
}
// A size in the binary units of the recordings' limits: 2 GiB free, 4 GiB per file.
function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes / 1024, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
// Why the latest recording stopped by itself, with what went wrong with the disk.
function recordingStop() {
  const stop = recording.stopped;
  const reason = REC_STOPPED[stop.reason] || 'an unknown reason';
  return stop.detail ? `${reason} (${stop.detail})` : reason;
}
// What the status line says of the recordings: those that run, or why the latest stopped by itself.
function recordingStatus() {
  if (!recordingKnown()) return '';
  const runs = [recording.audio && 'audio', recording.iq && 'I/Q'].filter(Boolean);
  if (runs.length) return ` · recording ${runs.join(' and ')}`;
  if (!recording.stopped) return '';
  return ` · ${recording.stopped.kind === 'iq' ? 'I/Q recording' : 'recording'} stopped: ${recordingStop()}`;
}
// What a recording that runs has made so far.
const recordingMade = (r) => (r.file ? `${fmtLength(r.seconds)} · ${fmtBytes(r.bytes)}` : 'starting');
// A Record button: its label, and whether it may start a recording.
function syncRecordButton(button, label, disabled) {
  if (button.textContent !== label) button.textContent = label;
  if (button.disabled !== disabled) button.disabled = disabled;
}
function syncRecording() {
  const known = recordingKnown(), runs = recordingRuns(), a = runs ? recording.audio : null;
  const iqOn = iqKnown(), iqGoes = iqRuns(), q = iqGoes ? recording.iq : null;
  const simulated = source !== null && source.kind === 'synthetic';
  // A recording's state comes every second: an unchanged text is not rewritten.
  syncRecordButton(recAudio, runs ? 'Stop' : 'Record audio', !known || (!runs && simulated));
  syncRecordButton(recIq, iqGoes ? 'Stop' : 'Record I/Q', !iqOn || (!iqGoes && simulated));
  let text = '';
  if (runs) {
    text = `${formatFrequency(a.frequencyHz)} MHz ${a.mode} · ${recordingMade(a)}`;
  } else if (known && simulated) {
    text = 'The simulated band is not recorded.';
  } else if (known && recording.stopped && recording.stopped.kind !== 'iq') {
    text = `Stopped: ${recordingStop()}.`;
  }
  const el = $('#rec-state');
  if (el.textContent !== text) el.textContent = text;
  let iqText = '';
  if (iqGoes) {
    iqText = `${formatFrequency(q.frequencyHz)} MHz · ${fmtRate(q.rateHz)} · ${recordingMade(q)}`;
  } else if (iqOn && recording.stopped && recording.stopped.kind === 'iq') {
    iqText = `Stopped: ${recordingStop()}.`;
  }
  const iqEl = $('#rec-iq-state');
  if (iqEl.textContent !== iqText) iqEl.textContent = iqText;
  syncMenuBadge();
  syncRecordingLive();
  syncRecordingFolder();
}
function setRecordNote(note) {
  recordNote = note;
  syncRecordNote();
}
function syncRecordNote() {
  const parts = [recordNote];
  if (recordingsStale) parts.push(notConnected('the recordings are not up to date'));
  if (recordingList && recordingList.problem) parts.push(recordingList.problem);
  if (streamState === 'open' && !stream.recordings.includes('audio')) {
    parts.push('The engine that runs does not record: restart Radiolack after its update.');
  }
  const note = parts.filter(Boolean).join(' ');
  const el = $('#rec-note');
  if (el.textContent !== note) el.textContent = note;
  el.hidden = !note;
}
function askRecordings() {
  if (!stream.recordings.includes('audio')) return;
  recordingsKey = recordingKeyNow();
  recordingsStale = !stream.askRecordings();
  syncRecordNote();
}
// A link that downloads a recording's file.
function recordingLink(file, text) {
  const name = escapeHtml(file);
  return `<a class="link" href="recordings/${encodeURIComponent(file)}" download="${name}" data-get="${name}">${text}</a>`;
}
function recordingItem(f) {
  const name = escapeHtml(f.name), iq = f.kind === 'iq';
  let facts = fmtDate(f.startedS), acts;
  if (iq && f.rateHz !== null) facts += ` · ${fmtRate(f.rateHz)}`;
  if (f.running) {
    facts += ` · <span data-live="${name}"></span>`;     // the state's, every second
    acts = '<span class="num">Recording</span>';
  } else {
    if (f.seconds !== null) facts += ` · ${fmtLength(f.seconds)}`;
    if (f.bytes !== null) facts += ` · ${fmtBytes(f.bytes)}`;
    const armed = recArmed === f.name;
    // An I/Q recording's two files: the samples, and the metadata that tells how to read them.
    acts = (iq ? recordingLink(f.name, 'Data') + (f.meta ? recordingLink(f.meta, 'Metadata') : '')
      : recordingLink(f.name, 'Download'))
      + `<button type="button" class="link" data-del="${name}"${armed ? ` aria-label="Confirm deleting ${name}"` : ''}>`
      + `${armed ? 'Confirm' : 'Delete'}</button>`;
  }
  return `<li class="rec" title="${name}"><span class="name">${formatFrequency(f.frequencyHz)} MHz ${escapeHtml(f.mode)}`
    + `<span class="facts">${facts}</span></span>${acts}</li>`;
}
function renderRecordings() {
  const list = $('#rec-list'), refocus = focusedIn(list);
  const files = recordingList ? recordingList.files : [];
  const html = files.map(recordingItem).join('')
    || `<li class="empty">${recordingList === null ? 'Not received yet' : 'No recording yet'}</li>`;
  if (html !== recordingsHtml && !recordingsHold.wait()) {
    list.innerHTML = html;
    recordingsHtml = html;
    focusAgain(list, refocus);
  }
  syncRecordingLive();
  syncRecordingSummary();
  syncRecordNote();
}
// The running recordings' lengths and sizes, which their state brings every
// second, each found by its file: rows that a pressed pointer keeps from an
// older list get their own.
function syncRecordingLive() {
  for (const el of document.querySelectorAll('#rec-list [data-live]')) {
    const r = [recordingRuns() && recording.audio, iqRuns() && recording.iq].find((r) => r && r.file === el.dataset.live);
    const text = r ? `${fmtLength(r.seconds)} · ${fmtBytes(r.bytes)}` : '';
    if (el.textContent !== text) el.textContent = text;
  }
}
// Where the recordings are, on this computer, the space left, and the older ones the list leaves out.
function syncRecordingSummary() {
  const l = recordingList, folder = recording && recording.folder;
  let text = '';
  if (l) {
    const free = l.freeBytes === null ? '' : `${fmtBytes(l.freeBytes)} free${folder ? '' : " on the computer's disk"}`;
    text = [folder ? `In ${folder}` : '', free].filter(Boolean).join(', ');
    if (text) text += '.';
    if (l.more) text += ` ${l.more} older ${l.more === 1 ? 'recording is' : 'recordings are'} not listed.`;
  }
  const el = $('#rec-summary');
  if (el.textContent !== text.trim()) el.textContent = text.trim();
}
// A first press arms Delete; a second one, within RECORDINGS_CONFIRM_MS, sends it.
function armRecording(name) {
  clearTimeout(recArmTimer);
  recArmed = name;
  recArmTimer = setTimeout(disarmRecording, RECORDINGS_CONFIRM_MS);
  renderRecordings();
}
function disarmRecording() {
  clearTimeout(recArmTimer);
  recArmed = null;
  if (recordShown()) renderRecordings();
}
recAudio.addEventListener('click', () => {
  const on = !recordingRuns();
  recordingAsk = 'audio';
  setRecordNote(stream.setRecording(on) ? '' : notConnected(on ? 'nothing is recorded' : 'the recording goes on'));
});
recIq.addEventListener('click', () => {
  const on = !iqRuns();
  recordingAsk = 'iq';
  setRecordNote(stream.setIqRecording(on) ? '' : notConnected(on ? 'nothing is recorded' : 'the recording goes on'));
});
// The list, rebuilt by every change, keeps its rows under a pressed pointer.
const recordingsHold = holdUnderPointer($('#rec-list'), () => { if (recordShown()) renderRecordings(); });
$('#rec-list').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-del]');
  if (!b) return;
  const name = b.dataset.del;
  if (recArmed !== name) { armRecording(name); return; }
  disarmRecording();
  setRecordNote(stream.deleteRecording(name) ? '' : notConnected('nothing was deleted'));
});
setInterval(() => { if (recordShown() && streamState === 'open' && !document.hidden) askRecordings(); }, RECORDINGS_REFRESH_MS);
// The desktop window saves what the interface downloads in the Downloads
// folder without asking, then tells where (window.py).
window.addEventListener('radiolack-download', (e) => {
  const d = e.detail;
  if (!d || typeof d.name !== 'string') return;
  if (typeof d.error === 'string' && d.error) setRecordNote(`${d.name} was not saved: ${d.error}.`);
  else setRecordNote(typeof d.path === 'string' ? `Saved as ${d.path}.` : `${d.name} was saved.`);
});

// Settings
bindCheck('st-autohide', 'autoHide', poke);

// About's updates: the engine checks for a newer Radiolack at start, unless
// turned off, and on request. The box and the notice follow the engine's state.
$('#ab-update-start').addEventListener('change', (e) => {
  if (stream.setCheckAtStart(e.target.checked)) return;
  updateNote = 'Not connected to the engine: the setting did not change.';
  syncUpdate();
});
$('#ab-update-check').addEventListener('click', () => {
  updateNote = stream.checkUpdates() ? '' : 'Not connected to the engine.';
  syncUpdate();
});
$('#notice-close').addEventListener('click', () => {
  noticeClosed = noticeKey();
  syncUpdate();
});
const UPDATE_TEXTS = {
  idle: (u) => `Radiolack ${u.version}.`,
  checking: () => 'Checking for a newer version…',
  current: (u) => `Radiolack ${u.version} is up to date.`,
  available: (u) => `Radiolack ${u.latest} is available: install it with the system's software updates.`,
  installed: (u) => `Radiolack ${u.latest} is installed: restart Radiolack to use it.`,
  failed: (u) => `The check failed: ${u.detail}`,
  unavailable: (u) => u.detail,
};
// The notice shows a newer version until closed on this page.
function noticeKey() {
  return update && (update.status === 'available' || update.status === 'installed') ? `${update.status} ${update.latest}` : '';
}
function syncUpdate() {
  $('#ab-update-section').hidden = !update;     // this computer's interfaces only
  const text = update ? UPDATE_TEXTS[update.status](update) : '';
  $('#ab-update').textContent = [text, updateNote].filter(Boolean).join(' ');
  const start = $('#ab-update-start');
  start.checked = update ? update.checkAtStart : false;
  start.disabled = !update;
  $('#ab-update-check').disabled = !update || update.status === 'checking';
  const key = noticeKey();
  if (key) $('#notice-text').textContent = text;
  noticeEl.hidden = !key || key === noticeClosed;
}
// The version that runs opens About, on every interface: the engine's hello gives it.
function syncVersion() {
  const version = $('#ab-version');
  version.textContent = stream.version ? `Radiolack ${stream.version}` : '';
  version.hidden = !stream.version;
}

// Remote access: devices on the local network reach the engine over HTTPS
// with the access code. Only this computer's interfaces receive its state
// and change it. Turning on goes on in the engine: the state then says
// whether it listens, or why not.
const remotePortEl = $('#st-remote-port');
const notConnected = (what) => `Not connected to the engine: ${what}.`;
const setRemoteNote = (text) => { remoteNote = text; syncRemote(); };
$('#st-remote').addEventListener('change', (e) => {
  if (!stream.setRemoteAccess(e.target.checked)) setRemoteNote(notConnected('remote access did not change'));
});
remotePortEl.addEventListener('input', () => typingIn.add(remotePortEl));
remotePortEl.addEventListener('blur', () => { if (typingIn.delete(remotePortEl)) syncRemote(); });
remotePortEl.addEventListener('change', () => {
  typingIn.delete(remotePortEl);
  const port = Number(remotePortEl.value);
  if (!Number.isInteger(port) || port < Number(remotePortEl.min) || port > Number(remotePortEl.max)) {
    setRemoteNote(`The port must be a whole number from ${remotePortEl.min} to ${remotePortEl.max}.`);
  } else if (!stream.setRemotePort(port)) {
    setRemoteNote(notConnected('the port did not change'));
  }
});
$('#st-remote-renew').addEventListener('click', () => {
  if (!stream.renewRemoteCode()) setRemoteNote(notConnected('the code did not change'));
});
// The paired devices, rebuilt by every state of remote access, keep their
// rows under a pressed pointer. The list follows the state while Settings
// is closed too: opening it does not rebuild the list.
const devicesHold = holdUnderPointer($('#st-remote-devices'), () => { if (remoteState) renderRemoteDevices(); });
$('#st-remote-devices').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-forget]');
  if (b && !stream.forgetRemoteDevice(b.dataset.forget)) setRemoteNote(notConnected('the device was not forgotten'));
});
$('#st-remote-copy').addEventListener('click', () => {
  const urlEl = $('#st-remote-url');
  const selectUrl = () => {
    const range = document.createRange();
    range.selectNodeContents(urlEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    setRemoteNote('Address selected: copy it from the browser menu.');
  };
  if (!navigator.clipboard) { selectUrl(); return; }
  navigator.clipboard.writeText(urlEl.textContent).then(() => setRemoteNote('Address copied.'), selectUrl);
});
// When a paired device last used remote access, which the engine notes to the hour.
function fmtSeen(seconds) {
  const age = Date.now() / 1000 - seconds;
  if (age < 3600) return 'used in the last hour';
  if (age < 86400) return `used ${Math.floor(age / 3600)} h ago`;
  const days = Math.floor(age / 86400);
  return `used ${days} ${days === 1 ? 'day' : 'days'} ago`;
}
function renderRemoteDevices() {
  if (devicesHold.wait()) return;
  const list = $('#st-remote-devices'), refocus = focusedIn(list);
  list.innerHTML = remoteState.devices.map((d) => `<li class="device"><span class="name">${escapeHtml(d.label)}</span>`
    + `<span class="num">${d.connected ? 'connected' : fmtSeen(d.seenS)}</span>`
    + `<button type="button" class="link" data-forget="${escapeHtml(d.id)}" aria-label="Forget ${escapeHtml(d.label)}">Forget</button></li>`).join('')
    || '<li class="empty">No paired device</li>';
  focusAgain(list, refocus);
}
function syncRemote() {
  const r = remoteState;
  $('#st-remote-section').hidden = !r;
  if (!r) return;
  $('#st-remote').checked = r.on;
  if (!typingIn.has(remotePortEl)) remotePortEl.value = String(r.port);
  remotePortEl.disabled = r.on;     // the engine changes it only while off
  $('#st-remote-info').hidden = !r.on;
  $('#st-remote-url-field').hidden = !r.urls.length;     // on, but listening nowhere: the message says why
  $('#st-remote-url').textContent = r.urls[0] ?? '';
  $('#st-remote-urls').textContent = r.urls.length > 1 ? `Or: ${r.urls.slice(1).join(', ')}` : '';
  $('#st-remote-code').textContent = r.code ?? '';
  const blocked = r.blocked ? 'Too many wrong codes: the code no longer works. Make a new code.' : '';
  $('#st-remote-msg').textContent = [r.problem, blocked, remoteNote].filter(Boolean).join(' ');
  renderRemoteDevices();
  $('#st-remote-fp-field').hidden = !r.fingerprint;
  $('#st-remote-fingerprint').textContent = r.fingerprint ?? '';
}

// The recordings folder, which this computer's interfaces alone receive and
// change, while nothing records: the engine checks it, then every one of
// them receives it, or this one learns why not.
function syncRecordingFolder() {
  const r = recording, local = r !== null && r.defaultFolder !== null;
  const section = $('#rec-folder-section');
  if (section.hidden === local) section.hidden = !local;
  if (!local) return;
  const runs = r.audio !== null || r.iq !== null, value = r.folder ?? '';
  if (!typingIn.has(recFolderEl) && recFolderEl.value !== value) recFolderEl.value = value;
  if (recFolderEl.disabled !== runs) recFolderEl.disabled = runs;
  const fixed = runs || r.folder === r.defaultFolder, use = $('#rec-default');
  if (use.disabled !== fixed) use.disabled = fixed;
  const text = folderNote || (runs ? 'A recording runs: stop it to change the folder.' : '');
  const el = $('#rec-folder-msg');
  if (el.textContent !== text) el.textContent = text;
}
function sendFolder(folder) {
  recordingAsk = 'folder';
  folderNote = stream.setRecordingFolder(folder) ? '' : notConnected('the folder did not change');
  syncRecordingFolder();
}
recFolderEl.addEventListener('input', () => typingIn.add(recFolderEl));
recFolderEl.addEventListener('blur', () => { if (typingIn.delete(recFolderEl)) syncRecordingFolder(); });
recFolderEl.addEventListener('change', () => {
  typingIn.delete(recFolderEl);
  const folder = recFolderEl.value.trim();
  sendFolder(folder || null);       // an empty field: the default folder
});
$('#rec-default').addEventListener('click', () => sendFolder(null));

// Where the engine saves the settings, bookmarks and history, and what went wrong with its files.
function syncStorage() {
  $('#st-storage-section').hidden = !storage;   // this computer's interfaces only
  let text = '';
  if (storage && storage.directory === null) text = 'Settings, bookmarks and history are not saved.';
  else if (storage) text = `Settings, bookmarks and history are saved in ${storage.directory}.`;
  if (storage && storage.problem) text += ` ${storage.problem}`;
  $('#st-storage').textContent = text;
}

// ------------------------------------------------------------------ preferences
// This interface's own settings. The engine keeps them for the next start,
// as the page's address, and with it the browser's storage, changes with
// the port: one set for this computer's interfaces, and one for each device
// that remote access serves, which starts from this computer's until it
// gives its own. They leave out the engine's settings (VFO, mode, filter,
// squelch, receiver, remote access) and which menu shows.
const PREFERENCES = ['autoMode', 'step', 'avg', 'fill', 'peakHold', 'grid', 'autoRange', 'top', 'range', 'palette',
  'wfAuto', 'wfMin', 'wfMax', 'wfSpeed', 'timestamps', 'showPlan', 'showMarks', 'showChannels', 'split', 'volume',
  'peakMarks', 'autoHide'];
const PREFERENCE_SELECTS = { step: '#dm-step', palette: '#wf-pal' };
const PREFERENCES_SAVE_MS = 1000;
let preferencesLoaded = false;      // the engine's applied: nothing is saved before
let savedPreferences = '';          // as last sent, in JSON

function preferencesNow() { return Object.fromEntries(PREFERENCES.map((key) => [key, S[key]])); }
// A saved value replaces the default when it has the same type and its control takes it.
function preferenceValue(key, value) {
  if (typeof value !== typeof S[key]) return undefined;
  if (typeof value === 'number') {
    const el = rangeInputs.get(key);
    return el && Number.isFinite(value) ? clamp(value, Number(el.min), Number(el.max)) : undefined;
  }
  if (typeof value === 'string') {
    const el = PREFERENCE_SELECTS[key] && $(PREFERENCE_SELECTS[key]);
    return el && Array.from(el.options).some((o) => o.value === value) ? value : undefined;
  }
  return value;
}
// The engine sends its preferences at each connection: only the first ones
// apply, as this interface's own then stand.
function applyPreferences(values) {
  if (preferencesLoaded) return;
  preferencesLoaded = true;
  for (const key of PREFERENCES) {
    const value = Object.hasOwn(values, key) ? preferenceValue(key, values[key]) : undefined;
    if (value !== undefined) S[key] = value;
  }
  S.wfMin = Math.min(S.wfMin, S.wfMax - 5);     // as the waterfall controls keep them
  savedPreferences = JSON.stringify(preferencesNow());
  syncDisplay();
  syncDemod();
  updateMeta();
  lutDirty = true;
  layoutPending = true;
  rebuildStamps();
  drawBandStrip();
  renderMarks();
  audio.setVolume(S.volume / 100);
  syncAudio();
  poke();
}
// Changed preferences go to the engine, which writes them to the disk soon after.
function savePreferences() {
  if (!preferencesLoaded) return;
  const values = preferencesNow(), json = JSON.stringify(values);
  if (json !== savedPreferences && stream.savePreferences(values)) savedPreferences = json;
}

// A press from the keyboard (detail 0) moves the focus along when what it
// pressed goes: in one column, between the list of menus and the menu.
menuButton.addEventListener('click', (e) => { if (menuEl.hidden) openMenu(); else closeMenu(e.detail === 0); });
$('#menu nav').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-menu]');
  if (!b) return;
  showMenu(b.dataset.menu);
  if (e.detail === 0 && oneColumnQuery.matches) $('#menu .back').focus();
});
$('#menu .back').addEventListener('click', (e) => {
  showMenuList();
  if (e.detail === 0) $(`#menu nav button[data-menu="${lastMenu}"]`).focus();
});
$('#menu [data-close]').addEventListener('click', (e) => closeMenu(e.detail === 0));
// A press outside the menu closes it, and does nothing else.
menuScrim.addEventListener('click', () => closeMenu());
menuScrim.addEventListener('contextmenu', (e) => { e.preventDefault(); closeMenu(); });
// Widened to two columns while it shows the list of menus: the last menu shows beside it.
onMediaChange(oneColumnQuery, () => { if (!menuEl.hidden && !oneColumnQuery.matches && openName === null) showMenu(lastMenu); });

function liveUi() {
  if (signalsShown()) updatePeakAges();
  if (openName === 'radio') syncSql();
  if (openName === 'display') syncAutoDisplay();
}

// ------------------------------------------------------------------ context menu
let ctxF = 0, ctxGuardUntil = 0;
// A touch menu opens beside the finger so the finger does not hide it: above, else below, else to one side.
function touchMenuOrigin(x, y, w, h) {
  const gap = 24, margin = 8;
  if (y - gap - h >= margin) return [x - w / 2, y - gap - h];
  if (y + gap + h <= window.innerHeight - margin) return [x - w / 2, y + gap];
  return [x + gap + w <= window.innerWidth - margin ? x + gap : x - gap - w, y - h / 2];
}
function openCtx(x, y, f, touch = false) {
  ctxF = f;
  const seg = segmentAt(f);
  $('#ctx-title').textContent = `${formatFrequency(snapAt(f))} MHz${seg ? ` · ${seg.name}` : ''}`;   // where Tune and Bookmark go
  ctxEl.querySelector('[data-cmd="segment"]').disabled = !seg;
  // Beyond the receiver's reach nothing tunes: Bookmark stays, for another receiver.
  for (const cmd of ['tune', 'ignore']) ctxEl.querySelector(`[data-cmd="${cmd}"]`).disabled = beyondReach(snapAt(f));
  ctxEl.hidden = false;
  const r = ctxEl.getBoundingClientRect();
  const [left, top] = touch ? touchMenuOrigin(x, y, r.width, r.height) : [x, y];
  ctxEl.style.left = `${clamp(left, 8, window.innerWidth - r.width - 8)}px`;
  ctxEl.style.top = `${clamp(top, 8, window.innerHeight - r.height - 8)}px`;
  if (!touch) ctxEl.querySelector('button:not(:disabled)').focus();   // keyboard use starts on the first item; a finger needs no focus ring
}
function closeCtx() { ctxEl.hidden = true; }
// Lifting the finger that opened the menu also fires a click where it lifts: that click is ignored.
function guardCtxRelease(e) { if (e.pointerType !== 'mouse' && !ctxEl.hidden) ctxGuardUntil = performance.now() + 350; }
ctxEl.addEventListener('click', (e) => {
  if (performance.now() < ctxGuardUntil) return;
  const b = e.target.closest('button[data-cmd]');
  if (!b || b.disabled) return;
  const f = snapAt(ctxF), seg = segmentAt(ctxF);
  if (b.dataset.cmd === 'tune') tuneAnywhere(f);
  else if (b.dataset.cmd === 'bookmark') addBookmark(f, planMode(modeAt(plan, f) ?? (seg ? seg.mode : null), S.mode));
  else if (b.dataset.cmd === 'center') { const span = view.f1 - view.f0; setView(ctxF - span / 2, ctxF + span / 2); }
  else if (b.dataset.cmd === 'segment') zoomToSegment(seg);
  else if (b.dataset.cmd === 'ignore') sendScanner(() => stream.ignoreFrequency(f));
  closeCtx();
});
// A press outside the menu only dismisses it: on the views it must not also tune or start a drag.
window.addEventListener('pointerdown', (e) => {
  if (ctxEl.hidden || ctxEl.contains(e.target)) return;
  closeCtx();
  if (specWrap.contains(e.target) || wfWrap.contains(e.target)) { poke(); e.stopPropagation(); }
}, true);

// ------------------------------------------------------------------ pointer and keyboard
let drag = null, wheelAcc = 0, pinch = null, longPress = 0;
const touchX = new Map();           // touch or pen contacts on the views: pointerId -> clientX
const LONG_PRESS_MS = 550;
function hitTest(x, touch = false) {
  const [lo, hi] = passband();
  const xl = fToX(lo), xh = fToX(hi);
  const edge = touch ? 12 : 5, near = touch ? 16 : 6;   // fingers need wider handles than a mouse
  if (xh - xl >= 3 * edge) {
    if (Math.abs(x - xl) <= edge) return 'lo';
    if (Math.abs(x - xh) <= edge) return 'hi';
    if (x > xl && x < xh) return 'move';
  } else if (Math.abs(x - fToX(S.vfo)) <= near) {
    return 'move';
  }
  return 'free';
}
const cursorFor = (hit) => (hit === 'lo' || hit === 'hi' ? 'ew-resize' : hit === 'move' ? 'grab' : 'crosshair');

function cancelLongPress() { clearTimeout(longPress); longPress = 0; }
// Two fingers zoom around their midpoint and follow it, like a map.
function startPinch() {
  cancelLongPress();
  drag = null;
  const [a, b] = [...touchX.values()];
  pinch = { d0: Math.max(24, Math.abs(a - b)), mid0: (a + b) / 2, f0: view.f0, f1: view.f1, inSpan: viewInSpan() };
}
function updatePinch() {
  const [a, b] = [...touchX.values()];
  const span0 = pinch.f1 - pinch.f0;
  const span = clampSpan(pinchSpan(span0 * (pinch.d0 / Math.max(24, Math.abs(a - b))), span0));
  const anchor = pinch.f0 + (pinch.mid0 / appW) * span0;   // frequency under the initial midpoint
  const start = anchor - (((a + b) / 2) / appW) * span;
  setView(start, start + span, pinch.inSpan);
}
function releaseTouch(e) {
  touchX.delete(e.pointerId);
  if (pinch && touchX.size < 2) { pinch = null; drag = null; }
}

function onDown(e) {
  if (e.button !== 0) return;
  hintEl.classList.add('gone');
  e.currentTarget.setPointerCapture(e.pointerId);
  e.preventDefault();
  const touch = e.pointerType !== 'mouse';
  if (touch) {
    touchX.set(e.pointerId, e.clientX);
    if (touchX.size === 2) { startPinch(); return; }
    if (touchX.size > 2) return;
  }
  const x = e.clientX;
  drag = { hit: hitTest(x, touch), x0: x, y0: e.clientY, f0: xToF(x), vfo0: S.vfo, view0: view.f0, moved: false, touch };
  if (touch && drag.hit === 'free') {
    longPress = setTimeout(() => {
      longPress = 0;
      if (!drag || drag.moved) return;
      const { x0, y0 } = drag;
      drag = null;
      openCtx(x0, y0, xToF(x0), true);
    }, LONG_PRESS_MS);
  }
}
function onMove(e, area) {
  poke();
  if (touchX.has(e.pointerId)) {
    touchX.set(e.pointerId, e.clientX);
    if (pinch) { if (touchX.size >= 2) updatePinch(); return; }
  }
  showHover(e, area);
  const x = e.clientX;
  if (!drag) { if (e.pointerType === 'mouse') e.currentTarget.style.cursor = cursorFor(hitTest(x)); return; }
  const dx = x - drag.x0;
  // A finger sliding vertically is a swipe, not a tap: it must not tune on release.
  const slop = drag.touch ? 8 : 3;
  if (!drag.moved && (Math.abs(dx) > slop || (drag.touch && Math.abs(e.clientY - drag.y0) > slop))) {
    drag.moved = true;
    cancelLongPress();
  }
  const hzPerPx = (view.f1 - view.f0) / appW;
  if (drag.hit === 'lo' || drag.hit === 'hi') {
    const f = xToF(x);
    if (S.mode === 'USB') { if (drag.hit === 'hi') setBandwidth(f - S.vfo); }
    else if (S.mode === 'LSB') { if (drag.hit === 'lo') setBandwidth(S.vfo - f); }
    else setBandwidth(2 * Math.abs(f - S.vfo));
  } else if (drag.hit === 'move') {
    if (drag.moved) tune(snapAt(drag.vfo0 + dx * hzPerPx), { keepView: true });
  } else if (drag.moved) {
    const span = view.f1 - view.f0, a = drag.view0 - dx * hzPerPx;
    setView(a, a + span);
  }
}
function onUp(e) {
  cancelLongPress();
  guardCtxRelease(e);
  releaseTouch(e);
  if (!drag) return;
  if (drag.hit === 'free' && !drag.moved) tuneAnywhere(snapAt(drag.f0));
  drag = null;
}
function onCancel(e) {
  cancelLongPress();
  guardCtxRelease(e);
  releaseTouch(e);
  drag = null;
}
function onWheel(e) {
  e.preventDefault();
  poke();
  hintEl.classList.add('gone');
  if (e.ctrlKey || e.metaKey) { zoomAt(Math.exp(clamp(e.deltaY, -100, 100) * 0.004), xToF(e.clientX)); return; }
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    const span = view.f1 - view.f0, a = view.f0 + e.deltaX * (span / appW);
    setView(a, a + span);
    return;
  }
  const px = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
  if (Math.abs(px) >= 50) { wheelAcc = 0; stepTune(px > 0 ? -1 : 1); return; }
  wheelAcc += px;
  if (Math.abs(wheelAcc) >= 50) { stepTune(wheelAcc > 0 ? -1 : 1); wheelAcc = 0; }
}
for (const [el, area] of [[specWrap, 'spec'], [wfWrap, 'wf']]) {
  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', (e) => onMove(e, area));
  el.addEventListener('pointerup', onUp);
  el.addEventListener('pointercancel', onCancel);
  el.addEventListener('pointerleave', hideHover);
  el.addEventListener('wheel', onWheel, { passive: false });
  el.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    cancelLongPress();
    drag = null;                    // Android also fires this on a long press: never tune on release
    openCtx(e.clientX, e.clientY, xToF(e.clientX), e.pointerType === 'touch' || e.pointerType === 'pen');
  });
  el.addEventListener('gesturestart', (e) => e.preventDefault());   // Safari: the pinch belongs to the views
}

let bandDrag = null;
bandEl.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  bandDrag = { y0: e.clientY, h0: specH, moved: false };
  bandEl.setPointerCapture(e.pointerId);
  e.preventDefault();
});
bandEl.addEventListener('pointermove', (e) => {
  poke();
  showHover(e, 'band');
  const hit = stripHitAt(e), item = stripItemAt(xToF(e.clientX));
  const title = hit ? [hit.name, `${formatFrequency(hit.f)} MHz`, hit.note, hit.mode].filter(Boolean).join(' · ')
    : item ? `${rangeTitle(item)} · ${formatFrequency(item.from)}–${formatFrequency(item.to)} MHz · drag: spectrum height` : 'Drag: spectrum height';
  if (bandEl.title !== title) bandEl.title = title;
  bandEl.classList.toggle('on-mark', hit !== null);
  if (!bandDrag) return;
  const dy = e.clientY - bandDrag.y0;
  if (Math.abs(dy) > 3) bandDrag.moved = true;
  if (bandDrag.moved) { S.split = clamp((bandDrag.h0 + dy) / (window.innerHeight - bandHeight()), 0.12, 0.8); layoutPending = true; }
});
bandEl.addEventListener('pointerup', (e) => {
  if (bandDrag && !bandDrag.moved) {
    const hit = stripHitAt(e);
    if (hit) tuneAnywhere(hit.f, hit.mode ? { mode: planMode(hit.mode, S.mode) } : {});
    else zoomToSegment(stripItemAt(xToF(e.clientX)));
  }
  bandDrag = null;
});
bandEl.addEventListener('pointercancel', () => { bandDrag = null; });
bandEl.addEventListener('pointerleave', () => { hideHover(); bandEl.classList.remove('on-mark'); });

let editingFreq = false;
function editFreq() {
  if (editingFreq) return;
  editingFreq = true;
  const input = document.createElement('input');
  input.type = 'text';
  input.inputMode = 'decimal';
  input.className = 'freq-input';
  const shown = Math.round(shownVfo());
  input.value = formatFrequency(shown);
  input.setAttribute('aria-label', 'Frequency, in MHz or with a unit');
  freqEl.replaceChildren(input);
  input.focus();
  input.select();
  let closed = false;
  const finish = (apply) => {
    if (closed) return;
    closed = true;
    editingFreq = false;
    // Only a frequency other than the VFO's tunes: an entry left as it opened moves nothing.
    const hz = apply ? parseFrequency(input.value) : null;
    if (apply && hz === null && input.value.trim()) showStatusNote('Not a frequency: type it in MHz, or with Hz, kHz, MHz or GHz');
    else if (hz !== null && hz !== shown) tuneAnywhere(hz);     // beyond the receiver's reach, it says why
    renderFreq();
    freqEl.focus();
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}
// A modal dialog, which Escape, its close button or a press on the backdrop
// closes. Closed from the keyboard, the dialog gives the focus back to the
// control that opened it; closed with a pointer, it leaves that control
// unfocused: a focus there, even the dialog's own restoring one, would ring it
// in WebKit. Returns the function that opens the dialog.
function bindDialog(dialog, opener) {
  let backdropPress = false;
  let keyClose = false;
  dialog.addEventListener('cancel', () => { keyClose = true; });     // Escape
  dialog.querySelector('.close').addEventListener('click', (e) => {
    keyClose = e.detail === 0;     // pressed from the keyboard
    dialog.close();
  });
  // A press that starts in the dialog and ends on the backdrop, as when selecting text, keeps it open,
  // and so does the second click of a double click on its opener, which lands on the backdrop.
  dialog.addEventListener('pointerdown', (e) => { backdropPress = e.target === dialog; });
  dialog.addEventListener('click', (e) => {
    if (backdropPress && e.target === dialog && e.detail < 2) dialog.close();
    backdropPress = false;
  });
  dialog.addEventListener('close', () => {
    if (keyClose) opener.focus();
    else opener.blur();
  });
  return () => {
    keyClose = false;
    dialog.showModal();
  };
}

// The icon beside the frequency opens the antenna lengths.
const openAntennaDialog = bindDialog(antennaDialog, antennaEl);
antennaEl.addEventListener('click', () => {
  openAntennaDialog();
  syncAntenna();
});

// The Receiver menu's Calibrate dialog (calibration.js). On the stations:
// the engine's calibration, which any interface starts or stops and every
// interface follows, shown with what it measured at each station and the
// correction it proposes, which Apply the proposal sends as any change of
// the correction; the run goes on when the dialog closes. On a known
// carrier: while the dialog is open,
// the engine measures the carrier at the VFO, asked again as each measure
// comes, every MEASURE_EVERY_MS at most; the user types the carrier's true
// frequency, and Apply sends the correction that brings the carrier there,
// as any change of the correction, and tunes the VFO there, where the
// carrier then lies, keeping the dialog open on the next measures. Only the latest request's answer counts, and a change of the
// receiver's settings or of the VFO asks again: no measure taken before the
// change computes a correction. On an R820T or R828D, the first steady
// carrier measured also has the engine identify the rtl-sdr driver, once
// per opening: the correction waits for its answer, and the measures asked
// after it. While the calibration on the stations runs, the engine's
// measure waits for its end, and the true frequency stays as it is.
const CARRIER_LOST_MS = 10000;  // an answer lost with a dropped stream is asked again after this
const calibrateButton = $('#src-calibrate');
const openCalibrateDialog = bindDialog(calibrateDialog, calibrateButton);
let carrierId = 0;              // the latest request's id
let carrierMeasure = null;      // its answer; null while it is on its way
let carrierAsked = 0;           // when it went, performance.now()
let carrierTimer = 0;
let carrierError = '';          // why the engine refused a request or a correction, or ''
let carrierKey = '';            // the settings the measure depends on: another asks again
let trueTyped = false;          // the user typed the true frequency: it no longer follows the VFO
let calibrationApplied = false; // a correction went: Cancel reads Close
let runError = '';              // why the engine refused to start or stop the run, or its proposal; or ''
let appliedFrom = '';           // the latest correction applied: 'stations' or 'carrier'
let runRowsKey = '';            // the stations' table as last drawn
let driverCheckId = 0;          // the latest driver check's id
/** @type {import('./calibration.js').DriverCheckState} */
let driverCheck = { state: 'idle', answer: null };
// What a measure depends on: the receiver, its correction, converter, span
// and the driver's computation, which labels the samples, and the VFO's channel.
function carrierSetting() {
  if (!source) return '';
  return JSON.stringify([source.kind, source.address, source.ppm, source.converter, source.converterLoHz,
    source.centreHz, source.centreInUseHz, source.driverPll, source.rateHz, source.dcRemoval, S.vfo, S.mode, S.bw]);
}
// The driver's check holds the correction back until it answers.
function driverCheckHolds() {
  return (driverCheck.state === 'due' || driverCheck.state === 'running') && driverCheckWanted(source, stream.measures);
}
function askCarrier() {
  clearTimeout(carrierTimer);
  carrierTimer = 0;
  if (!calibrateDialog.open) return;
  carrierId += 1;
  carrierAsked = performance.now();
  stream.measureCarrier(carrierId);     // not connected: asked again once the stream opens
  carrierTimer = setTimeout(askCarrier, CARRIER_LOST_MS);
}
// The dialog follows a change: a new measure for new settings, and the VFO's
// frequency, until the user types another.
function syncCalibration() {
  if (!calibrateDialog.open) return;
  if (driverCheck.state === 'idle' && driverCheckWanted(source, stream.measures)) {
    driverCheck = { state: 'due', answer: null };
  }
  const key = carrierSetting();
  if (key !== carrierKey && streamState === 'open') {
    carrierKey = key;
    carrierMeasure = null;
    askCarrier();
  }
  if (!trueTyped && !calibrationRuns()) setValue($('#calibrate-true'), startingTrueText(S.vfo));
  renderCalibration();
}
// Writes only what changed.
function setValue(el, value) { if (el.value !== value) el.value = value; }
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
function renderCalibration() {
  if (!calibrateDialog.open || !source) return;
  const entry = calibrateEntry(source, stream.measures, streamState === 'open');
  renderRun(entry);
  const view = calibrationView({ measure: carrierMeasure, trueText: $('#calibrate-true').value, source, vfoHz: S.vfo,
    holding: driverCheckHolds() });
  if (calibrationRuns()) {
    view.measured = 'Measured once the calibration on the stations ends.';
    view.apply = null;
  }
  setText($('#calibrate-measured'), streamState === 'open' ? view.measured : notConnected('nothing is measured'));
  $('#calibrate-driver-field').hidden = !driverPllView(source).shown;
  setText($('#calibrate-driver'), driverCheckText(driverCheck, source));
  setText($('#calibrate-in-use'), view.inUse);
  setText($('#calibrate-correction'), view.correction);
  setText($('#calibrate-note'), carrierError || entry.note || view.note);
  $('#calibrate-apply').disabled = !entry.enabled || view.apply === null;
  $('#calibrate-converter').hidden = !source.converter || source.converter === 'none';
  $('#calibrate-tuner').hidden = !tunerNoteShown(source);
  setText($('#calibrate-cancel'), calibrationApplied ? 'Close' : 'Cancel');
}
// The calibration on the stations: its button, its table, its proposal.
function currentRunView() {
  return runView({ state: calibration, source, measures: stream.measures, open: streamState === 'open',
    recording: recordingRuns() || iqRuns() });
}
function renderRun(entry) {
  const run = currentRunView();
  const runButton = $('#calibrate-run');
  setText(runButton, run.button);
  runButton.disabled = !run.enabled || (!calibrationRuns() && !entry.enabled);
  setText($('#calibrate-run-status'), run.status);
  const key = JSON.stringify(run.rows);
  if (key !== runRowsKey) {
    runRowsKey = key;
    $('#calibrate-stations').hidden = run.rows.length === 0;
    $('#calibrate-stations-body').replaceChildren(...run.rows.map((row) => {
      const tr = document.createElement('tr');
      tr.classList.toggle('apart', row.apart);
      for (const text of [row.station, row.carrier, row.margin, row.ppm]) {
        const td = document.createElement('td');
        td.textContent = text;
        tr.append(td);
      }
      return tr;
    }));
  }
  setText($('#calibrate-proposal'), run.proposal);
  setText($('#calibrate-run-note'), runError || run.note);
  $('#calibrate-run-apply').disabled = !entry.enabled || run.apply === null;
}
$('#calibrate-run').addEventListener('click', () => {
  const action = calibrationRuns() ? 'stop' : 'start';
  runError = stream.calibrate(action) ? '' : notConnected(`the calibration did not ${action}`);
  renderCalibration();
});
$('#calibrate-run-apply').addEventListener('click', () => {
  const run = currentRunView();
  if (run.apply === null) return;
  if (!stream.sendSource({ ppm: run.apply })) {
    runError = notConnected('no correction was applied');
  } else {
    calibrationApplied = true;
    appliedFrom = 'stations';
    runError = carrierError = '';
    carrierMeasure = null;
    askCarrier();               // measured with the new correction
  }
  renderCalibration();
});
calibrateButton.addEventListener('click', () => {
  if (!calibrateEntry(source, stream.measures, streamState === 'open').enabled) return;
  trueTyped = calibrationApplied = false;
  carrierError = carrierKey = runError = appliedFrom = '';
  carrierMeasure = null;
  driverCheck = { state: 'idle', answer: null };      // every calibration checks the driver again
  setValue($('#calibrate-true'), startingTrueText(S.vfo));
  openCalibrateDialog();
  syncCalibration();
});
calibrateDialog.addEventListener('close', () => {
  clearTimeout(carrierTimer);
  carrierTimer = 0;
  carrierId += 1;               // an answer still on its way is ignored
  driverCheckId += 1;           // so is a driver check's, which the engine still saves
  carrierMeasure = null;
  driverCheck = { state: 'idle', answer: null };
});
$('#calibrate-true').addEventListener('input', () => {
  trueTyped = true;
  renderCalibration();
});
$('#calibrate-apply').addEventListener('click', () => {
  const trueText = $('#calibrate-true').value, trueHz = parseFrequency(trueText);
  const view = calibrationView({ measure: carrierMeasure, trueText, source, vfoHz: S.vfo, holding: driverCheckHolds() });
  if (view.apply === null || trueHz === null) return;
  if (!stream.sendSource({ ppm: view.apply })) {
    carrierError = notConnected('no correction was applied');
  } else {
    // The carrier moves by the change, a few kilohertz on the tuner: the VFO follows it, in its mode.
    if (trueHz !== S.vfo) stream.sendDemod({ frequencyHz: trueHz });
    calibrationApplied = true;
    appliedFrom = 'carrier';
    carrierError = runError = '';
    carrierMeasure = null;
    askCarrier();               // after the change, on the stream: measured with the new correction
  }
  renderCalibration();
});
// Cancel closes as the close button does, from the keyboard or with a pointer.
$('#calibrate-cancel').addEventListener('click', (e) => {
  calibrateDialog.querySelector('.close').dispatchEvent(new MouseEvent('click', { detail: e.detail }));
});

// About's problem report: the engine makes it, with what the interface tells
// of itself; a modal dialog shows it, to open as a new GitHub issue, which
// the user's web browser shows, or to copy.
const reportButton = $('#ab-report');
const openReportDialog = bindDialog(reportDialog, reportButton);
// The web engine, whether the views draw, the window's size and the audio
// lost since the page loaded: 300 characters at most, the audio's kept.
function pageFacts() {
  const views = spectrumView ? 'WebGL2 views' : gpuError;
  const lost = audio.losses;
  const losses = `audio since load: ran dry ${lost.dry}, ${lost.missingS.toFixed(1)} s dropped by the engine, `
    + `${lost.skippedS.toFixed(1)} s skipped`;
  const rest = `; ${views}; ${innerWidth}x${innerHeight} at ${devicePixelRatio}x; ${losses}`;
  return (navigator.userAgent.slice(0, Math.max(0, 300 - rest.length)) + rest).slice(0, 300);
}
reportButton.addEventListener('click', () => {
  $('#ab-report-msg').textContent = '';
  if (!stream.askReport(pageFacts())) $('#ab-report-msg').textContent = notConnected('no report was made');
});
$('#report-copy').addEventListener('click', () => {
  const text = $('#report-text');
  const note = $('#report-msg');
  const select = () => {
    text.focus();
    text.select();
    note.textContent = 'Report selected: copy it with Ctrl+C.';
  };
  if (!navigator.clipboard) { select(); return; }
  navigator.clipboard.writeText(text.value).then(() => { note.textContent = 'Report copied.'; }, select);
});

// About's legal notice, on every interface, from its start each time.
const legalButton = $('#ab-legal');
const openLegalDialog = bindDialog(legalDialog, legalButton);
legalButton.addEventListener('click', () => {
  openLegalDialog();
  $('#legal-text').scrollTop = 0;
});

// A digit of the readout changes with the wheel over it, or dragged up or
// down by any pointer: by one for each DIGIT_DRAG_PX, once the pointer moved
// DIGIT_DRAG_SLOP_PX, so that a shorter press stays a click, or a tap that
// opens the keypad. The VFO goes there, off the step's grid, and the span
// follows it as after a click outside the span.
const DIGIT_DRAG_PX = 16;
const DIGIT_DRAG_SLOP_PX = 8;
const DRAG_CLICK_MS = 400;          // the click that ends a drag comes within this
let digitDrag = null;               // { id, y0, base, m, steps, moved } while a pointer holds a digit
let digitDragEnd = -Infinity;       // performance.now() when the last drag ended
function tuneDigit(f) {
  if (f >= MIN_FREQUENCY_HZ && f <= MAX_FREQUENCY_HZ) tuneAnywhere(f);
}
const afterDrag = () => performance.now() - digitDragEnd < DRAG_CLICK_MS;
freqEl.addEventListener('dblclick', () => { if (!afterDrag()) editFreq(); });
freqEl.addEventListener('click', () => { if (coarseQuery.matches && !afterDrag()) editFreq(); });   // touch: one tap opens the keypad
freqEl.addEventListener('wheel', (e) => {
  const digit = e.target.closest('.d');
  if (!digit) return;
  e.preventDefault();
  if (Math.abs(e.deltaY) < 2) return;
  tuneDigit(Math.round(shownVfo()) + (e.deltaY < 0 ? 1 : -1) * Number(digit.dataset.m));
}, { passive: false });
freqEl.addEventListener('pointerdown', (e) => {
  const digit = !editingFreq && e.button === 0 && e.target.closest('.d');
  if (!digit) return;
  digitDrag = { id: e.pointerId, y0: e.clientY, base: Math.round(shownVfo()), m: Number(digit.dataset.m), steps: 0, moved: false };
  freqEl.setPointerCapture(e.pointerId);    // the digits are written again as the VFO moves
});
freqEl.addEventListener('pointermove', (e) => {
  if (!digitDrag || e.pointerId !== digitDrag.id) return;
  const rise = digitDrag.y0 - e.clientY;
  if (!digitDrag.moved && Math.abs(rise) < DIGIT_DRAG_SLOP_PX) return;
  digitDrag.moved = true;
  const steps = Math.trunc(rise / DIGIT_DRAG_PX);
  if (steps === digitDrag.steps) return;
  digitDrag.steps = steps;
  tuneDigit(digitDrag.base + steps * digitDrag.m);
});
function endDigitDrag(e) {
  if (!digitDrag || e.pointerId !== digitDrag.id) return;
  if (digitDrag.moved) digitDragEnd = performance.now();
  digitDrag = null;
}
for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) freqEl.addEventListener(type, endDigitDrag);

// Touch step buttons: a press steps once, holding repeats.
function bindStepButton(btn, dir) {
  let delay = 0, repeat = 0, lastPointer = -Infinity;
  const stop = () => { clearTimeout(delay); clearInterval(repeat); delay = 0; repeat = 0; };
  btn.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    stop();
    btn.setPointerCapture(e.pointerId);
    stepTune(dir);
    delay = setTimeout(() => { repeat = setInterval(() => stepTune(dir), 120); }, 400);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    btn.addEventListener(type, () => { stop(); lastPointer = performance.now(); });
  }
  window.addEventListener('blur', stop);
  // Keyboard and assistive technologies activate the button with a click and no pointer press.
  btn.addEventListener('click', () => { if (performance.now() - lastPointer > 300) stepTune(dir); });
}
bindStepButton($('#tune-down'), -1);
bindStepButton($('#tune-up'), 1);

window.addEventListener('keydown', (e) => {
  poke();
  // A dialog keeps its keys: Escape closes it alone, and nothing tunes behind it.
  if (antennaDialog.open || calibrateDialog.open || reportDialog.open || legalDialog.open || confirmDialog.open) return;
  if (e.key === 'Escape') {
    if (!ctxEl.hidden) { closeCtx(); e.preventDefault(); }
    else if (!menuEl.hidden) { closeMenu(true); e.preventDefault(); }
    return;
  }
  const tag = e.target && e.target.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target === freqEl && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
    stepTune(e.key === 'ArrowUp' ? 1 : -1, e.shiftKey ? 10 : 1);
    e.preventDefault();
    return;
  }
  if (e.target === freqEl && e.key === 'Enter') { editFreq(); e.preventDefault(); return; }
  switch (e.key) {
    case 'ArrowRight': stepTune(1, e.shiftKey ? 10 : 1); break;
    case 'ArrowLeft': stepTune(-1, e.shiftKey ? 10 : 1); break;
    case '+': case '=': zoomAt(1 / 1.5, shownVfo()); break;
    case '-': case '_': zoomAt(1.5, shownVfo()); break;
    case '0': setView(fStart(), fEnd()); break;
    case 'm': case 'M': setMode(MODE_ORDER[(MODE_ORDER.indexOf(S.mode) + 1) % MODE_ORDER.length]); break;
    default:
      if (/^[1-9]$/.test(e.key) && Number(e.key) <= MENUS.length) toggleMenu(MENUS[Number(e.key) - 1]);
      else return;
  }
  e.preventDefault();
});

let idleTimer = 0;
function poke() {
  document.body.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (S.autoHide && menuEl.hidden) document.body.classList.add('idle'); }, 3000);
}
window.addEventListener('pointermove', poke, { passive: true });
window.addEventListener('pointerdown', poke, { passive: true });   // a tap wakes the interface too
window.addEventListener('resize', () => { layoutPending = true; });

const HINTS = {
  mouse: 'Click: tune · Wheel: step · Ctrl + wheel: zoom · Right-click: menu · 1 to 8: menus',
  touch: 'Tap: tune · Pinch: zoom · Drag: scroll · Long press: menu',
};
function syncPointerMode() { hintEl.textContent = coarseQuery.matches ? HINTS.touch : HINTS.mouse; }
onMediaChange(coarseQuery, () => { syncPointerMode(); layoutPending = true; });

// ------------------------------------------------------------------ GPU views
// Without WebGL2 the rest of the interface still works; the status line says why the views stay empty.
let spectrumView = null, waterfallView = null, gpuError = '';
try {
  spectrumView = new SpectrumView(specCanvas, () => { specKey = ''; });
  waterfallView = new WaterfallView(wfCanvas, HIST_ROWS, () => {
    waterfallView.uploadAll(wide.hist, WIDE_LAYER);
    if (fine.bins) waterfallView.uploadAll(fine.hist, FINE_LAYER);
    waterfallView.setColours(lutBytes);
    wfKey = '';
  });
} catch (error) {
  spectrumView = null;
  waterfallView = null;
  gpuError = 'WebGL2 unavailable: spectrum and waterfall not shown';
  console.error('Radiolack: the spectrum and waterfall cannot be drawn:', error);
}

// ------------------------------------------------------------------ main loop
// Frames are integrated as they arrive; each animation frame commits the
// waterfall lines that are due and redraws only the views that changed.
let lastTs = performance.now(), lineAcc = 0, lastUi = 0;
function loop(ts) {
  const dt = Math.min(0.1, Math.max(0, (ts - lastTs) / 1000));
  lastTs = ts;
  if (layoutPending) { layoutPending = false; layout(); }
  if (lutDirty) { lutDirty = false; rebuildLut(); }
  lineAcc += dt * S.wfSpeed;
  let lines = 0;
  while (lineAcc >= 1 && lines < 6) { lineAcc -= 1; lines += 1; commitLines(Date.now()); }
  if (lineAcc > 6) lineAcc = 0;
  renderSpectrum();
  renderWaterfall();
  renderScales();
  updateMeterUi(ts);
  if (ts - lastUi > 400) { lastUi = ts; liveUi(); }
  requestAnimationFrame(loop);
}

// ------------------------------------------------------------------ start
resetBuffers();
setView(fStart(), fEnd());
layout();
syncSource();
syncDisplay();
renderBookmarks();
syncCategoryControls();
syncScanTabs();
syncScanner();
syncUpdate();
updateStatus();
showVfo();
syncPointerMode();
poke();
setTimeout(() => hintEl.classList.add('gone'), 9000);
document.addEventListener('visibilitychange', () => { savePreferences(); syncStream(); syncDeep(); });
window.addEventListener('pagehide', savePreferences);
setInterval(savePreferences, PREFERENCES_SAVE_MS);
syncStream();
audio.setVolume(S.volume / 100);
audio.start();
requestAnimationFrame(loop);
