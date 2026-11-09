// @ts-nocheck -- code from the mockup; it gets JSDoc types as it is rewritten.
/*
 * Radiolack web interface: spectrum and waterfall fed by the engine stream,
 * drawn with WebGL2, with the mockup's panels and interactions around them.
 */
import { EngineStream } from './stream.js';
import { SpectrumView, MAX_MARKERS } from './spectrum-gl.js';
import { WaterfallView } from './waterfall-gl.js';

// ------------------------------------------------------------------ helpers
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const fmtMHz = (hz, dec = 4) => (hz / 1e6).toFixed(dec);
const fmtBw = (hz) => (hz >= 1000 ? `${+(hz / 1000).toFixed(2)} kHz` : `${Math.round(hz)} Hz`);
const fmtDb = (db) => `${Math.round(db)} dB`;
const fmtAgo = (s) => (s < 60 ? `${Math.max(0, Math.round(s))} s` : `${Math.floor(s / 60)} min`);
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ------------------------------------------------------------------ constants
const HIST_DB_MIN = -160;           // waterfall history: levels from -160 dBFS in 0.625 dB steps, on 8 bits
const HIST_DB_STEP = 0.625;
const HIST_ROWS = 1400;             // waterfall lines kept for re-rendering
const S_ZERO = -104;                // S-meter bar range (mock calibration: S9 = -50 dB)
const S_TOP = -10;
const METER_FLOOR = -140;           // reading when the passband holds no signal or no bins
const AXIS_H = 18;                  // frequency scale under the spectrum, CSS px (#spec-axis in style.css)
const PIXEL_BUDGET = 3.4e6;         // GPU drawing buffers WebKitGTK redraws at full rate (scoping.md, spike 1)
const SOURCE_NAMES = { synthetic: 'Source synthétique' };
const R820T_GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4,
  28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6];
const MODES = {
  NFM: { bw: 10000, min: 4000, max: 25000, step: 500 },
  AM: { bw: 8000, min: 3000, max: 15000, step: 500 },
  USB: { bw: 2700, min: 1500, max: 4000, step: 100 },
  LSB: { bw: 2700, min: 1500, max: 4000, step: 100 },
  CW: { bw: 500, min: 100, max: 2000, step: 50 },
};
const MODE_ORDER = Object.keys(MODES);
const PALETTES = {
  classic: ['#000000', '#00007a', '#0050ff', '#00d0ff', '#40ff80', '#ffff00', '#ff7000', '#ff0000'],
  viridis: ['#440154', '#414487', '#2a788e', '#22a884', '#7ad151', '#fde725'],
  amber: ['#000000', '#2a1200', '#7a3600', '#d97b00', '#ffc04d', '#fff4d6'],
  ice: ['#000000', '#06142e', '#12407a', '#2a86c8', '#7fd3f5', '#f2fbff'],
  gray: ['#000000', '#ffffff'],
};
// Simplified IARU Region 1 2 m band plan, for the mockup only.
const SEGMENTS = [
  { from: 144.000e6, to: 144.150e6, label: 'CW / MGM', mode: 'CW', step: 100, rgb: '107,138,253' },
  { from: 144.150e6, to: 144.400e6, label: 'SSB / CW / MGM', mode: 'USB', step: 100, rgb: '176,124,255' },
  { from: 144.400e6, to: 144.490e6, label: 'Balises', mode: 'CW', step: 100, rgb: '242,179,61' },
  { from: 144.500e6, to: 144.794e6, label: 'Tous modes', mode: 'NFM', step: 12500, rgb: '125,139,153' },
  { from: 144.794e6, to: 144.990e6, label: 'Numérique', mode: 'NFM', step: 12500, rgb: '60,207,154' },
  { from: 144.990e6, to: 145.194e6, label: 'Entrées relais', mode: 'NFM', step: 12500, rgb: '255,122,122' },
  { from: 145.194e6, to: 145.206e6, label: 'Espace', mode: 'NFM', step: 12500, rgb: '226,230,90' },
  { from: 145.206e6, to: 145.594e6, label: 'FM simplex', mode: 'NFM', step: 12500, rgb: '89,167,255' },
  { from: 145.594e6, to: 145.794e6, label: 'Sorties relais', mode: 'NFM', step: 12500, rgb: '255,159,90' },
  { from: 145.794e6, to: 145.806e6, label: 'ISS', mode: 'NFM', step: 12500, rgb: '226,230,90' },
  { from: 145.806e6, to: 146.000e6, label: 'Satellites', mode: 'USB', step: 100, rgb: '226,230,90' },
];
const PANEL_TITLES = { source: 'Source', demod: 'Démodulation et audio', display: 'Affichage', bands: 'Bandes et favoris', scanners: 'Scanners', settings: 'Réglages' };
const PANEL_ORDER = ['source', 'demod', 'display', 'bands', 'scanners', 'settings'];

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
const marksEl = $('#marks');
const freqEl = $('#freq');
const meterFill = $('#smeter .fill');
const meterSql = $('#smeter .sql');
const panelEl = $('#panel');
const ctxEl = $('#ctx');
const hoverEl = $('#hoverinfo');
const hintEl = $('#hint');
const readoutEl = $('#readout');
const coarseQuery = window.matchMedia('(pointer: coarse)');   // primary input is a finger

// ------------------------------------------------------------------ state
const S = {
  fc: 145.0e6, fs: 2.4e6, nb: 8192,            // the engine's capture, updated by its frames
  vfo: 145.5e6, mode: 'NFM', bw: 10000, autoMode: true, step: 'auto',
  gainIdx: 17, ppm: 0, dcRemove: true, tunerAgc: false, biasT: false,   // receiver settings: shown, not sent yet
  avg: 0.6, fill: true, peakHold: false, grid: true, autoRange: true, top: -30, range: 80,
  palette: 'classic', wfAuto: true, wfMin: -101, wfMax: -57, wfSpeed: 25, timestamps: true,
  showPlan: true, showMarks: true, split: 0.4,
  sql: -80, sqlAuto: true, ctcss: 'detect', agc: 'slow', nr: false, notch: false, blanker: false, volume: 70,
  peakOn: true, peakThr: 12, peakMarks: true, memDwell: 0.25, memResume: 2,
  autoHide: true, keepAwake: false, remote: false,
};
const view = { f0: 0, f1: 0 };
let bookmarks = [
  { f: 144.050e6, name: 'Appel CW', mode: 'CW', scan: false },
  { f: 144.174e6, name: 'FT8', mode: 'USB', scan: false },
  { f: 144.300e6, name: 'Appel SSB', mode: 'USB', scan: true },
  { f: 144.800e6, name: 'APRS', mode: 'NFM', scan: true },
  { f: 145.3375e6, name: 'Simplex', mode: 'NFM', scan: true },
  { f: 145.500e6, name: 'Appel FM', mode: 'NFM', scan: true },
  { f: 145.6125e6, name: 'Relais A', mode: 'NFM', scan: true },
  { f: 145.6625e6, name: 'Relais B', mode: 'NFM', scan: true },
  { f: 145.7375e6, name: 'Relais C', mode: 'NFM', scan: true },
  { f: 145.800e6, name: 'ISS', mode: 'NFM', scan: false },
];
let bookmarkCounter = 0;

const gainDb = () => R820T_GAINS[S.gainIdx];
const fStart = () => S.fc - S.fs / 2;
const fEnd = () => S.fc + S.fs / 2;
const binHz = () => S.fs / S.nb;
const segmentAt = (f) => SEGMENTS.find((s) => f >= s.from && f < s.to) || null;
function stepAt(f) {
  if (S.step !== 'auto') return Number(S.step);
  const seg = segmentAt(f);
  return seg ? seg.step : 100;
}
const snapAt = (f) => { const st = stepAt(f); return Math.round(f / st) * st; };
function passband() {
  const f = S.vfo, bw = S.bw;
  if (S.mode === 'USB') return [f, f + bw];
  if (S.mode === 'LSB') return [f - bw, f];
  return [f - bw / 2, f + bw / 2];
}

// ------------------------------------------------------------------ buffers
let frameLin, avgLin, avgDb, peakDb, wfAcc, hist, histTime;
let wfCount = 0, histHead = -1, histFilled = 0, avgPrimed = false, levelsPrimed = false, frameCounter = 0;
let floorDb = -97, autoTop = -30, meterDb = METER_FLOOR, sqlOpen = false;
const autoWf = { min: -101, max: -57 };
const floorSample = new Float32Array(1024);
const PEAK_MERGE_HZ = 1500;         // detections closer than this update the same entry
const PEAK_LIST_MAX = 200;
const peaks = [];                   // { f, ch, first, last, hits, margin }
const excluded = [];                // frequencies ignored by the peak scanner
const isExcluded = (f) => excluded.some((x) => Math.abs(x - f) <= PEAK_MERGE_HZ);
let DPR = 1, GLS = 1, appW = 0, specH = 0, wfH = 0, layoutPending = false;
let specVals = null, peakVals = null, specColumns = null;
const markerCorners = new Float32Array(MAX_MARKERS * 6);
let lutDirty = true;

// New capture (centre, width or FFT size): every buffer and the history start again.
function resetBuffers() {
  const nb = S.nb;
  frameLin = new Float32Array(nb);
  avgLin = new Float32Array(nb);
  avgDb = new Float32Array(nb).fill(-140);
  peakDb = new Float32Array(nb).fill(-300);
  wfAcc = new Float32Array(nb);
  hist = new Uint8Array(nb * HIST_ROWS);
  histTime = new Float64Array(HIST_ROWS);
  wfCount = 0; histHead = -1; histFilled = 0; avgPrimed = false; levelsPrimed = false;
  if (waterfallView) waterfallView.setBins(nb);
  clearStamps();
  const lo = fStart(), hi = fEnd();
  if (!(view.f1 > view.f0 && view.f0 >= lo && view.f1 <= hi)) { view.f0 = lo; view.f1 = hi; }
  viewChanged();
}

// ------------------------------------------------------------------ engine stream
// The display is the stream's only use for now: it closes while the page is
// hidden, and the engine idles without clients.
let streamState = 'connecting', sourceKind = '', frameRate = 0, rateFrames = 0, rateSince = 0;
let codeDbMin = NaN, codeDbStep = NaN;
const codeToLin = new Float32Array(256);

function onFrame(frame) {
  const codes = frame.codes, nb = codes.length;
  if (frame.centreHz !== S.fc || frame.rateHz !== S.fs || nb !== S.nb) setCapture(frame.centreHz, frame.rateHz, nb);
  if (frame.dbMin !== codeDbMin || frame.dbStep !== codeDbStep) {
    codeDbMin = frame.dbMin;
    codeDbStep = frame.dbStep;
    for (let c = 0; c < 256; c += 1) codeToLin[c] = 10 ** ((codeDbMin + c * codeDbStep) / 10);
  }
  for (let i = 0; i < nb; i += 1) frameLin[i] = codeToLin[codes[i]];
  integrateFrame();
  frameCounter += 1;
  if (frameCounter % 3 === 0) estimateFloor();
  autoAdjust();
  countFrame(performance.now());
}

// The engine changed its capture: the VFO stays inside the new span.
function setCapture(centre, rate, bins) {
  S.fc = centre;
  S.fs = rate;
  S.nb = bins;
  resetBuffers();
  tune(S.vfo);
  syncSource();
  syncDisplay();
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

const stream = new EngineStream({
  onHello: (hello) => { sourceKind = hello.source; syncSource(); updateStatus(); },
  onFrame,
  onState: (state) => {
    streamState = state;
    if (state !== 'open') { rateSince = 0; frameRate = 0; }
    updateStatus();
  },
});
function syncStream() { if (document.hidden) stream.stop(); else stream.start(); }

function integrateFrame() {
  const nb = S.nb, a = avgPrimed ? S.avg : 0;
  for (let i = 0; i < nb; i += 1) {
    const v = frameLin[i];
    const m = a * avgLin[i] + (1 - a) * v;
    avgLin[i] = m;
    const db = 10 * Math.log10(m + 1e-30);
    avgDb[i] = db;
    if (db > peakDb[i]) peakDb[i] = db;
    wfAcc[i] += v;
  }
  wfCount += 1;
  avgPrimed = true;
  updateMeter();
}

function updateMeter() {
  const [lo, hi] = passband();
  const bin = binHz(), f0 = fStart();
  const a = clamp(Math.floor((lo - f0) / bin), 0, S.nb);
  const b = clamp(Math.ceil((hi - f0) / bin), 0, S.nb);
  if (b <= a) {                     // passband outside the captured span
    meterDb = METER_FLOOR;
    sqlOpen = false;
    return;
  }
  let p = 0;
  for (let i = a; i < b; i += 1) p += frameLin[i];
  const db = Math.max(METER_FLOOR, 10 * Math.log10(p + 1e-30));
  meterDb += (db - meterDb) * (db > meterDb ? 0.6 : 0.15);
  if (S.sqlAuto) S.sql = Math.round(floorDb + 10 * Math.log10(Math.max(1, b - a)) + 8);
  sqlOpen = meterDb > S.sql;
}

// Noise floor: median level of the central bins. The first estimate also
// places the automatic scales, so they need not slide there from afar.
function estimateFloor() {
  const nb = S.nb, from = Math.floor(nb * 0.15), to = Math.floor(nb * 0.85);
  const count = Math.min(floorSample.length, to - from), stride = (to - from) / count;
  for (let k = 0; k < count; k += 1) floorSample[k] = avgDb[from + Math.floor(k * stride)];
  const median = floorSample.subarray(0, count).sort()[count >> 1];
  if (levelsPrimed) {
    floorDb += (median - floorDb) * 0.2;
    return;
  }
  levelsPrimed = true;
  floorDb = median;
  autoTop = floorDb - 12 + S.range;
  autoWf.min = floorDb - 4;
  autoWf.max = floorDb + 40;
  lutDirty = true;
}

// Automatic scales follow the floor, once per frame.
function autoAdjust() {
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
// CSS px per line (one line per CSS px row).
const STAMP_MS = 10000;
const stamps = [];                  // { el, row }, oldest first
let lastStampSlot = -1;
const ageOf = (row) => (histHead - row + HIST_ROWS) % HIST_ROWS;
const stampLimit = () => Math.min(wfH, histFilled, HIST_ROWS - 1);
function addStamp(row) {
  const el = document.createElement('div');
  el.className = 'wf-stamp';
  el.textContent = new Date(histTime[row]).toTimeString().slice(0, 8);
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
  for (let age = stampLimit() - 2; age >= 0; age -= 1) {
    const row = (histHead - age + HIST_ROWS) % HIST_ROWS, older = (row - 1 + HIST_ROWS) % HIST_ROWS;
    if (Math.floor(histTime[row] / STAMP_MS) !== Math.floor(histTime[older] / STAMP_MS)) addStamp(row);
  }
  if (histFilled) lastStampSlot = Math.floor(histTime[histHead] / STAMP_MS);
}

// One waterfall line: the average power of the frames since the previous line.
function commitWfLine(now) {
  if (!wfCount) return;
  const nb = S.nb;
  histHead = (histHead + 1) % HIST_ROWS;
  histFilled = Math.min(HIST_ROWS, histFilled + 1);
  const base = histHead * nb, inv = 1 / wfCount;
  for (let i = 0; i < nb; i += 1) {
    const q = (10 * Math.log10(wfAcc[i] * inv + 1e-30) - HIST_DB_MIN) / HIST_DB_STEP;
    hist[base + i] = q <= 0 ? 0 : q >= 255 ? 255 : q + 0.5;
    wfAcc[i] = 0;
  }
  wfCount = 0;
  histTime[histHead] = now;
  if (waterfallView) waterfallView.uploadRow(histHead, hist.subarray(base, base + nb));
  const slot = Math.floor(now / STAMP_MS);
  if (S.timestamps) {
    advanceStamps();
    if (slot !== lastStampSlot) addStamp(histHead);
  }
  lastStampSlot = slot;
}

let wfKey = '';                     // what the waterfall shows: drawn again only when it changes
function renderWaterfall() {
  if (!waterfallView) return;
  const W = waterfallView.width;
  const key = `${histHead} ${histFilled} ${view.f0} ${view.f1} ${W} ${waterfallView.height} ${lutVersion} ${S.nb}`;
  if (key === wfKey) return;
  wfKey = key;
  const bin = binHz();
  waterfallView.draw({
    head: Math.max(0, histHead), lines: histFilled,
    firstBin: (view.f0 - fStart()) / bin, binsPerPixel: (view.f1 - view.f0) / bin / W,
  });
}

// ------------------------------------------------------------------ spectrum
function niceStep(x) {
  const p = 10 ** Math.floor(Math.log10(x));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= x) return m * p;
  return 10 * p;
}

function sample(src, out) {
  const W = out.length, nb = S.nb, bin = binHz(), f0 = fStart(), span = view.f1 - view.f0;
  const binsPerPx = span / bin / W;
  for (let x = 0; x < W; x += 1) {
    const ba = (view.f0 + (x / W) * span - f0) / bin;
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

let specKey = '';                   // what the spectrum shows: drawn again only when it changes
function renderSpectrum() {
  if (!spectrumView || !specColumns) return;
  const W = spectrumView.width, H = spectrumView.height, s = GLS, top = displayTop();
  const key = `${frameCounter} ${view.f0} ${view.f1} ${W} ${H} ${top} ${S.range} ${S.fill} ${S.peakHold} ${S.grid} ${S.peakOn && S.peakMarks}`;
  if (key === specKey) return;
  specKey = key;
  const plotH = Math.max(2, H - Math.round(AXIS_H * s));
  const yOf = (db) => ((top - db) / S.range) * plotH;
  sample(avgDb, specVals);
  if (S.peakHold) sample(peakDb, peakVals);
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
  if (S.peakOn && S.peakMarks) {
    const now = performance.now(), span = view.f1 - view.f0;
    for (const p of peaks) {
      if (markerCount >= MAX_MARKERS) break;
      if (now - p.last > 1500 || isExcluded(p.f) || p.f < view.f0 || p.f > view.f1) continue;
      const x = ((p.f - view.f0) / span) * W;
      const bi = clamp(Math.round((p.f - fStart()) / binHz()), 0, S.nb - 1);
      const y = clamp(yOf(avgDb[bi]) - 6 * s, 12 * s, plotH);
      markerCorners.set([x - 4 * s, y - 7 * s, x + 4 * s, y - 7 * s, x, y], markerCount * 6);
      markerCount += 1;
    }
  }
  spectrumView.draw({
    columns: specColumns, plotHeight: plotH, fill: S.fill, traceWidth: 1.1 * s, peakWidth: S.peakHold ? s : 0,
    grid, markers: markerCorners, markerCount,
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
    const step = freqStep(), dec = clamp(Math.ceil(-Math.log10(step / 1e6) - 1e-9), 3, 6);
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

function drawBandStrip() {
  const ctx = bandCtx, W = bandCanvas.width, H = bandCanvas.height, d = DPR;
  ctx.clearRect(0, 0, W, H);
  if (!S.showPlan) return;
  ctx.font = `${10 * d}px system-ui, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const s of SEGMENTS) {
    const x0 = fToX(s.from) * d, x1 = fToX(s.to) * d;
    if (x1 < 0 || x0 > W) continue;
    ctx.fillStyle = `rgba(${s.rgb},0.26)`;
    ctx.fillRect(x0, 0, Math.max(1, x1 - x0), H);
    ctx.fillStyle = `rgba(${s.rgb},0.85)`;
    ctx.fillRect(x0, H - 2 * d, Math.max(1, x1 - x0), 2 * d);
    const a = Math.max(x0, 0), b = Math.min(x1, W);
    if (ctx.measureText(s.label).width + 8 * d < b - a) {
      ctx.fillStyle = 'rgba(230,235,240,0.85)';
      ctx.fillText(s.label, (a + b) / 2, H / 2 - d);
    }
  }
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
    el.title = `${it.b.name} · ${fmtMHz(it.b.f)} MHz · ${it.b.mode}`;
    el.style.left = `${it.x}px`;
    el.addEventListener('pointerdown', (e) => e.stopPropagation());
    el.addEventListener('click', (e) => { e.stopPropagation(); userTune(); tune(it.b.f, { mode: it.b.mode }); });
    marksEl.appendChild(el);
  }
  // Marks are placed left to right: only the first one can reach the dB label column (7 px tick included).
  const first = marksEl.firstElementChild;
  const r = first && first.getBoundingClientRect();
  if (r && r.left < DB_LABEL_COL) markBandCss = [r.top, r.bottom + 7];
}

function renderFreq() {
  if (editingFreq) return;
  const s = String(Math.round(S.vfo)), n = s.length;
  let html = '';
  for (let i = 0; i < n; i += 1) {
    const pos = n - 1 - i;
    html += `<span class="d${pos < 3 ? ' lo' : ''}" data-m="${10 ** pos}">${s[i]}</span>`;
    if (pos > 0 && pos % 3 === 0) html += '<span class="sep">.</span>';
  }
  freqEl.innerHTML = html;
  freqEl.setAttribute('aria-valuenow', String(Math.round(S.vfo)));
  freqEl.setAttribute('aria-valuetext', `${fmtMHz(S.vfo)} MHz`);
}

function updateMeta() {
  $('#m-mode').textContent = S.mode;
  $('#m-bw').textContent = fmtBw(S.bw);
  const seg = segmentAt(S.vfo);
  $('#m-seg').textContent = `${seg ? seg.label : 'Hors plan'} · pas ${fmtBw(stepAt(S.vfo))}`;
}

function sUnits(db) {
  const over = db + 50;
  if (over < 0) return `S${clamp(Math.floor(9 + over / 6), 0, 9)}`;
  if (over < 10) return 'S9';
  return `S9+${Math.floor(over / 10) * 10}`;
}

let lastMeterText = 0;
function updateMeterUi(ts) {
  const frac = clamp((meterDb - S_ZERO) / (S_TOP - S_ZERO), 0, 1);
  meterFill.style.transform = `scaleX(${frac})`;
  meterFill.classList.toggle('open', sqlOpen);
  meterSql.style.left = `${clamp((S.sql - S_ZERO) / (S_TOP - S_ZERO), 0, 1) * 100}%`;
  vfoEl.classList.toggle('open', sqlOpen);
  if (ts - lastMeterText > 120) {
    lastMeterText = ts;
    const text = `${sUnits(meterDb)} · ${Math.round(meterDb)} dB`, el = $('#s-text');
    if (el.textContent !== text) el.textContent = text;   // rewriting an unchanged text still repaints
  }
}

function updateOverlays() {
  if (!appW) return;
  const [lo, hi] = passband();
  const x0 = fToX(lo), x1 = fToX(hi);
  const visible = x1 >= -2 && x0 <= appW + 2;
  vfoEl.style.display = visible ? '' : 'none';
  vfoLine.style.display = visible ? '' : 'none';
  vfoEl.style.transform = `translateX(${x0}px)`;
  vfoEl.style.width = `${Math.max(1, x1 - x0)}px`;
  vfoLine.style.transform = `translateX(${Math.round(fToX(S.vfo))}px)`;
  renderMarks();
  drawBandStrip();
  updateMeta();
  renderFreq();
}

let hoverAt = null;                 // last pointer position over a view, to refresh the readout when the view moves
function showHover(e, area) {
  hoverAt = { clientX: e.clientX, clientY: e.clientY, area };
  const x = e.clientX, f = xToF(x);
  xline.hidden = false;
  xline.style.transform = `translateX(${Math.round(x)}px)`;
  let txt = `${fmtMHz(f, 5)} MHz`;
  const bi = Math.floor((f - fStart()) / binHz());
  if (bi >= 0 && bi < S.nb) {
    if (area === 'spec') {
      txt += ` · ${Math.round(avgDb[bi])} dB`;
    } else if (area === 'wf') {
      const age = Math.floor(e.clientY - wfWrap.getBoundingClientRect().top);
      if (age >= 0 && age < histFilled) {
        const row = (histHead - age + HIST_ROWS) % HIST_ROWS;
        txt += ` · ${Math.round(HIST_DB_MIN + hist[row * S.nb + bi] * HIST_DB_STEP)} dB · il y a ${fmtAgo((Date.now() - histTime[row]) / 1000)}`;
      }
    }
  }
  const seg = segmentAt(f);
  if (seg) txt += ` · ${seg.label}`;
  hoverEl.textContent = txt;
}
function hideHover() { hoverAt = null; xline.hidden = true; hoverEl.textContent = ''; }

function updateStatus() {
  let text;
  if (streamState === 'open') {
    text = `${SOURCE_NAMES[sourceKind] || `Source ${sourceKind}`} · ${(S.fs / 1e6).toFixed(3)} MS/s · FFT ${S.nb} (${Math.round(binHz())} Hz)`
      + (frameRate ? ` · ${frameRate} i/s` : '');
  } else if (streamState === 'waiting') {
    text = 'En attente du moteur…';
  } else if (streamState === 'incompatible') {
    text = 'Moteur d’une autre version : rechargez la page';
  } else {
    text = 'Connexion au moteur…';
  }
  if (gpuError) text = `${gpuError} · ${text}`;
  $('#status').textContent = `${text}${S.remote ? ' · accès à distance actif' : ''}`;
}

// ------------------------------------------------------------------ layout and view
const bandHeight = () => (coarseQuery.matches ? 26 : 18);   // the band strip doubles as a touch handle
let labelTopCss = 40;               // dB labels start below this line, clear of the brand and the readout
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
  // On narrow screens the readout sits over the dB label column: start the labels below it.
  const ro = readoutEl.getBoundingClientRect();
  labelTopCss = ro.left < 60 ? Math.ceil(ro.bottom) + 8 : 40;
  rebuildStamps();
  viewChanged();
}

function viewChanged() {
  updateOverlays();
  if (hoverAt) showHover(hoverAt, hoverAt.area);
}

const minSpan = () => Math.max(4000, binHz() * 16);
function setView(f0, f1) {
  const lo = fStart(), hi = fEnd();
  const span = clamp(f1 - f0, minSpan(), hi - lo);
  const a = clamp((f0 + f1) / 2 - span / 2, lo, hi - span);
  view.f0 = a;
  view.f1 = a + span;
  viewChanged();
}
function zoomAt(factor, anchor) {
  const span = view.f1 - view.f0, r = (anchor - view.f0) / span;
  const s = clamp(span * factor, minSpan(), fEnd() - fStart());
  const a = clamp(anchor - r * s, fStart(), fEnd() - s);
  view.f0 = a;
  view.f1 = a + s;
  viewChanged();
}
function zoomToSegment(seg) {
  if (!seg) return;
  const pad = (seg.to - seg.from) * 0.08, a = seg.from - pad, b = seg.to + pad, w = b - a;
  const same = Math.abs(view.f1 - view.f0 - w) < w * 0.05 && Math.abs((view.f0 + view.f1) / 2 - (a + b) / 2) < w * 0.05;
  if (same) setView(fStart(), fEnd()); else setView(a, b);
}

// ------------------------------------------------------------------ tuning
let lastSeg;
function applyMode(mode) {
  if (!MODES[mode]) return;
  S.mode = mode;
  S.bw = MODES[mode].bw;
}
// The engine cannot retune yet: the VFO stays within the span it captures.
function tune(f, opts = {}) {
  if (!Number.isFinite(f)) return;
  f = clamp(Math.round(f), Math.ceil(fStart()), Math.floor(fEnd()));
  const seg = segmentAt(f);
  if (opts.mode) applyMode(opts.mode);
  else if (S.autoMode && seg && seg !== lastSeg && seg.mode !== S.mode) applyMode(seg.mode);
  lastSeg = seg;
  S.vfo = f;
  if (!opts.keepView && (f < view.f0 || f > view.f1)) {
    const span = view.f1 - view.f0;
    setView(f - span / 2, f + span / 2);
  }
  updateOverlays();
  syncDemod();
}
function stepTune(dir, mult = 1) {
  userTune();
  tune(snapAt(S.vfo) + dir * stepAt(S.vfo) * mult);
}
function setMode(mode) {
  applyMode(mode);
  syncDemod();
  updateOverlays();
}
function setBandwidth(bw) {
  const m = MODES[S.mode];
  S.bw = clamp(Math.round(bw / m.step) * m.step, m.min, m.max);
  syncDemod();
  updateOverlays();
}

// ------------------------------------------------------------------ scanners
// Channelized FM snaps to the 12.5 kHz raster. Narrow signals keep 100 Hz
// precision and merge with the nearest entry, so bin jitter does not split
// one transmitter into several rows and slow drift (Doppler) is followed.
function registerPeak(f, width, margin, now) {
  const seg = segmentAt(f);
  const ch = width >= 4000 && Boolean(seg) && seg.step >= 12500;
  const fq = ch ? Math.round(f / 12500) * 12500 : Math.round(f / 100) * 100;
  if (isExcluded(fq)) return;
  let e = null, best = PEAK_MERGE_HZ;
  for (const p of peaks) {
    const dist = Math.abs(p.f - fq);
    if (dist <= best) { best = dist; e = p; }
  }
  if (!e) {
    e = { f: fq, ch, first: now, last: now, hits: 0, margin };
    peaks.push(e);
    if (peaks.length > PEAK_LIST_MAX) {
      peaks.sort((a, b) => b.last - a.last);
      peaks.length = PEAK_LIST_MAX;
    }
  } else if (ch) {
    e.f = fq;
    e.ch = true;
  } else if (!e.ch) {
    e.f = Math.round((e.f + (fq - e.f) * 0.3) / 100) * 100;
  }
  e.last = now;
  e.hits += 1;
  e.margin = margin;
}
function scanPeaks(now) {
  if (!S.peakOn || !avgPrimed) return;
  const nb = S.nb, bin = binHz(), f0 = fStart(), thr = floorDb + S.peakThr;
  const lo = Math.floor(nb * 0.05), hi = Math.ceil(nb * 0.95);
  let i = lo;
  while (i < hi) {
    if (avgDb[i] <= thr) { i += 1; continue; }
    let j = i, maxDb = -300, wsum = 0, fsum = 0;
    while (j < hi && avgDb[j] > thr - 4) {
      wsum += avgLin[j];
      fsum += avgLin[j] * j;
      if (avgDb[j] > maxDb) maxDb = avgDb[j];
      j += 1;
    }
    registerPeak(f0 + (fsum / wsum + 0.5) * bin, (j - i) * bin, maxDb - floorDb, now);
    i = j + 1;
  }
}
function renderPeaks() {
  const now = performance.now();
  const rows = peaks.filter((p) => !isExcluded(p.f));
  rows.sort((a, b) => {
    const aa = now - a.last < 1500, ba = now - b.last < 1500;
    if (aa !== ba) return aa ? -1 : 1;
    return aa ? b.margin - a.margin : b.last - a.last;
  });
  $('#pk-body').innerHTML = rows.length
    ? rows.slice(0, 40).map((p) => {
      const ago = (now - p.last) / 1000, active = ago < 1.5;
      return `<tr class="${active ? 'active' : ''}">`
        + `<td><button type="button" class="freq-link" data-act="tune" data-f="${p.f}" title="Écouter">${fmtMHz(p.f)}</button></td>`
        + `<td>+${Math.round(p.margin)} dB</td><td>${active ? 'actif' : `il y a ${fmtAgo(ago)}`}</td>`
        + `<td class="acts"><button type="button" class="link" data-act="ignore" data-f="${p.f}">Ignorer</button></td></tr>`;
    }).join('')
    : '<tr><td colspan="4" class="empty">Aucun pic détecté</td></tr>';
  $('#pk-excluded').textContent = excluded.length ? `${excluded.length} fréquence(s) ignorée(s).` : '';
  $('#pk-restore').hidden = excluded.length === 0;
}

const mem = { running: false, idx: -1, state: 'idle', until: 0, cur: null };
function memNext(now) {
  const list = bookmarks.filter((b) => b.scan);
  if (!list.length) { memStop(); return; }
  mem.idx = (mem.idx + 1) % list.length;
  mem.cur = list[mem.idx];
  tune(mem.cur.f, { mode: mem.cur.mode, keepView: true });
  meterDb = METER_FLOOR;
  sqlOpen = false;
  mem.state = 'settle';
  mem.until = now + S.memDwell;
  syncMem();
}
function memStart() {
  if (!bookmarks.some((b) => b.scan)) { $('#mem-state').textContent = 'Aucun favori coché.'; return; }
  mem.running = true;
  mem.idx = -1;
  memNext(performance.now() / 1000);
  syncMem();
}
function memStop() {
  mem.running = false;
  mem.state = 'idle';
  syncMem();
}
function memTick(now) {
  if (!mem.running) return;
  const before = mem.state;
  if (mem.state === 'settle') {
    if (now >= mem.until) { if (sqlOpen) mem.state = 'listen'; else memNext(now); }
  } else if (mem.state === 'listen') {
    if (!sqlOpen) { mem.state = 'hang'; mem.until = now + S.memResume; }
  } else if (mem.state === 'hang') {
    if (sqlOpen) mem.state = 'listen';
    else if (now >= mem.until) memNext(now);
  }
  if (mem.running && mem.state !== before) syncMem();
}
function userTune() { if (mem.running) memStop(); }
function syncMem() {
  $('#mem-start').textContent = mem.running ? 'Arrêter' : 'Démarrer';
  const name = mem.cur ? mem.cur.name : '';
  let txt = 'Arrêté';
  if (mem.running) txt = mem.state === 'listen' ? `Écoute : ${name}` : mem.state === 'hang' ? `Attente : ${name}` : `Balayage : ${name}`;
  $('#mem-state').textContent = txt;
}
function renderMemList() {
  $('#mem-list').innerHTML = bookmarks.map((b, i) => `<li><label class="check"><input type="checkbox" data-i="${i}"${b.scan ? ' checked' : ''}>`
    + `<span>${escapeHtml(b.name)}</span><span class="num">${fmtMHz(b.f)}</span></label></li>`).join('');
}

// ------------------------------------------------------------------ panels
let openName = null, scanTab = 'peaks';
function openPanel(name) {
  openName = name;
  panelEl.hidden = false;
  for (const s of $$('#panel section[data-panel]')) s.hidden = s.dataset.panel !== name;
  $('#panel-title').textContent = PANEL_TITLES[name];
  for (const b of $$('#toolbar button')) b.setAttribute('aria-expanded', String(b.dataset.panel === name));
  document.body.classList.remove('idle');
  if (name === 'bands') renderBookmarks();
  if (name === 'scanners') syncScanTabs();
  liveUi();
}
function closePanel() {
  if (!openName) return;
  const btn = $(`#toolbar button[data-panel="${openName}"]`);
  const hadFocus = panelEl.contains(document.activeElement);
  openName = null;
  panelEl.hidden = true;
  for (const b of $$('#toolbar button')) b.setAttribute('aria-expanded', 'false');
  if (hadFocus && btn) btn.focus();
}
function togglePanel(name) { if (openName === name) closePanel(); else openPanel(name); }

const syncers = [];
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
  el.addEventListener('input', () => { S[key] = Number(el.value); if (out) out.textContent = fmt(S[key]); if (after) after(); });
  return el;
}

// Source: what the engine captures. The receiver settings are shown read-only
// until the engine takes commands.
function syncSource() {
  $('#src-device').value = sourceKind;   // no entry is selected before the engine answers
  $('#src-note').textContent = sourceKind === 'synthetic'
    ? 'Bande 2 m simulée par le moteur. Les réglages du récepteur viendront avec la source rtl_tcp.' : '';
  $('#src-center').value = fmtMHz(S.fc);
  $('#src-rate').value = String(S.fs);
  $('#src-gain').max = String(R820T_GAINS.length - 1);
  $('#src-gain').value = String(S.gainIdx);
  $('#src-gain-v').textContent = `${gainDb().toFixed(1)} dB`;
  $('#src-ppm').value = String(S.ppm);
  $('#src-dc').checked = S.dcRemove;
  $('#src-agc').checked = S.tunerAgc;
  $('#src-bias').checked = S.biasT;
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
}
function syncSql() {
  sqlEl.value = String(S.sql);
  sqlEl.disabled = S.sqlAuto;
  $('#dm-sql-v').textContent = fmtDb(S.sql);
}
for (const b of $$('#mode-group button')) b.addEventListener('click', () => setMode(b.dataset.mode));
bindCheck('dm-auto', 'autoMode');
bwEl.addEventListener('input', () => setBandwidth(Number(bwEl.value)));
$('#dm-step').addEventListener('change', (e) => { S.step = e.target.value; updateMeta(); });
bindCheck('dm-sql-auto', 'sqlAuto', syncSql);
sqlEl.addEventListener('input', () => { S.sql = Number(sqlEl.value); $('#dm-sql-v').textContent = fmtDb(S.sql); });
$('#dm-ctcss').value = S.ctcss;
$('#dm-ctcss').addEventListener('change', (e) => { S.ctcss = e.target.value; });
$('#dm-agc').value = S.agc;
$('#dm-agc').addEventListener('change', (e) => { S.agc = e.target.value; });
bindCheck('dm-nr', 'nr');
bindCheck('dm-notch', 'notch');
bindCheck('dm-nb', 'blanker');
bindRange('dm-vol', 'volume', (v) => `${v} %`);

// Display
const avgLabel = (v) => (v === 0 ? 'aucun' : v < 0.4 ? `léger (${v.toFixed(2)})` : v < 0.75 ? `moyen (${v.toFixed(2)})` : `fort (${v.toFixed(2)})`);
function drawPalettePreview() { $('#wf-pal-preview').style.background = `linear-gradient(90deg, ${PALETTES[S.palette].join(', ')})`; }
function syncDisplay() {
  for (const s of syncers) s();
  $('#ds-fft').value = String(S.nb);
  $('#ds-fft-note').textContent = `Résolution : ${Math.round(binHz())} Hz par point. Taille, fenêtre et cadence sont celles du moteur pour l’instant.`;
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
bindCheck('ds-peak', 'peakHold', () => { if (S.peakHold) peakDb.fill(-300); });
$('#ds-peak-clear').addEventListener('click', () => peakDb.fill(-300));
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
bindRange('wf-speed', 'wfSpeed', (v) => `${v} lignes/s`);
bindCheck('wf-time', 'timestamps', rebuildStamps);
bindCheck('ov-plan', 'showPlan', drawBandStrip);
bindCheck('ov-marks', 'showMarks', renderMarks);
bindRange('ov-split', 'split', (v) => `${Math.round(v * 100)} %`, () => { layoutPending = true; });

// Bands and bookmarks
function renderPlanList() {
  const list = $('#plan-list');
  list.innerHTML = SEGMENTS.map((s, i) => `<li><button type="button" class="item" data-seg="${i}"><span class="sw"></span>`
    + `<span class="name">${s.label}</span><span class="num">${fmtMHz(s.from, 3)}–${fmtMHz(s.to, 3)}</span></button></li>`).join('');
  // Set through the CSSOM: the Content-Security-Policy blocks inline style attributes.
  list.querySelectorAll('.sw').forEach((sw, i) => { sw.style.background = `rgb(${SEGMENTS[i].rgb})`; });
}
function renderBookmarks() {
  const q = $('#bm-filter').value.trim().toLowerCase();
  const items = bookmarks.map((b, i) => ({ b, i })).filter(({ b }) => !q || b.name.toLowerCase().includes(q) || fmtMHz(b.f).includes(q));
  $('#bm-list').innerHTML = items.length
    ? items.map(({ b, i }) => `<li class="bm"><button type="button" class="item" data-i="${i}"><span class="name">${escapeHtml(b.name)}</span>`
      + `<span class="num">${fmtMHz(b.f)}</span><span class="tag">${b.mode}</span></button>`
      + `<button type="button" class="link" data-del="${i}" aria-label="Retirer ${escapeHtml(b.name)}">Retirer</button></li>`).join('')
    : '<li class="empty">Aucun favori</li>';
}
function bookmarksChanged() { renderBookmarks(); renderMarks(); renderMemList(); }
function addBookmark(f, mode) {
  bookmarkCounter += 1;
  bookmarks.push({ f, name: `Favori ${bookmarkCounter}`, mode, scan: true });
  bookmarks.sort((a, b) => a.f - b.f);
  bookmarksChanged();
}
$('#plan-list').addEventListener('click', (e) => { const b = e.target.closest('button[data-seg]'); if (b) zoomToSegment(SEGMENTS[Number(b.dataset.seg)]); });
$('#bm-filter').addEventListener('input', renderBookmarks);
$('#bm-add').addEventListener('click', () => addBookmark(S.vfo, S.mode));
$('#bm-list').addEventListener('click', (e) => {
  const go = e.target.closest('button[data-i]');
  const del = e.target.closest('button[data-del]');
  if (go) { const b = bookmarks[Number(go.dataset.i)]; if (b) { userTune(); tune(b.f, { mode: b.mode }); } }
  if (del) { bookmarks.splice(Number(del.dataset.del), 1); bookmarksChanged(); }
});

// Scanners
function syncScanTabs() {
  for (const b of $$('#scan-tabs button')) b.setAttribute('aria-pressed', String(b.dataset.tab === scanTab));
  for (const p of $$('[data-tab-panel]')) p.hidden = p.dataset.tabPanel !== scanTab;
  if (scanTab === 'peaks') renderPeaks();
  if (scanTab === 'memory') { renderMemList(); syncMem(); }
}
for (const b of $$('#scan-tabs button')) b.addEventListener('click', () => { scanTab = b.dataset.tab; syncScanTabs(); });
bindCheck('pk-on', 'peakOn');
bindRange('pk-thr', 'peakThr', (v) => `${v} dB`);
bindCheck('pk-marks', 'peakMarks');
$('#pk-body').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-act]');
  if (!b) return;
  const f = Number(b.dataset.f);
  if (b.dataset.act === 'tune') { userTune(); tune(f); } else { excluded.push(f); renderPeaks(); }
});
$('#pk-restore').addEventListener('click', () => { excluded.length = 0; renderPeaks(); });
bindRange('mem-dwell', 'memDwell', (v) => `${Math.round(v * 1000)} ms`);
bindRange('mem-resume', 'memResume', (v) => `${v} s`);
$('#mem-list').addEventListener('change', (e) => { const b = bookmarks[Number(e.target.dataset.i)]; if (b) b.scan = e.target.checked; });
$('#mem-start').addEventListener('click', () => { if (mem.running) memStop(); else memStart(); });

// Settings
bindCheck('st-autohide', 'autoHide', poke);
bindCheck('st-wake', 'keepAwake');

// Remote access (mock): the desktop app would open its web server to the local network.
const REMOTE_CODE_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';   // no 0/O or 1/I to misread
function newRemoteCode() {
  const s = Array.from(crypto.getRandomValues(new Uint8Array(8)), (n) => REMOTE_CODE_CHARS[n % REMOTE_CODE_CHARS.length]).join('');
  return `${s.slice(0, 4)}-${s.slice(4)}`;
}
const remoteMsg = (text) => { $('#st-remote-msg').textContent = text; };
bindCheck('st-remote', 'remote', () => {
  $('#st-remote-info').hidden = !S.remote;
  if (S.remote && !$('#st-remote-code').textContent) $('#st-remote-code').textContent = newRemoteCode();
  remoteMsg('');
  updateStatus();
});
$('#st-remote-renew').addEventListener('click', () => {
  $('#st-remote-code').textContent = newRemoteCode();
  remoteMsg('Nouveau code : les appareils déjà connectés devront le saisir.');
});
$('#st-remote-copy').addEventListener('click', () => {
  const urlEl = $('#st-remote-url');
  const selectUrl = () => {
    const range = document.createRange();
    range.selectNodeContents(urlEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    remoteMsg('Adresse sélectionnée : copiez-la depuis le menu du navigateur.');
  };
  if (!navigator.clipboard) { selectUrl(); return; }
  navigator.clipboard.writeText(urlEl.textContent).then(() => remoteMsg('Adresse copiée.'), selectUrl);
});

for (const b of $$('#toolbar button')) b.addEventListener('click', () => togglePanel(b.dataset.panel));
$('#panel .close').addEventListener('click', closePanel);

function liveUi() {
  if (openName === 'scanners') { if (scanTab === 'peaks') renderPeaks(); else if (scanTab === 'memory') syncMem(); }
  if (openName === 'demod') syncSql();
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
  $('#ctx-title').textContent = `${fmtMHz(f, 4)} MHz${seg ? ` · ${seg.label}` : ''}`;
  ctxEl.querySelector('[data-cmd="segment"]').disabled = !seg;
  ctxEl.hidden = false;
  const r = ctxEl.getBoundingClientRect();
  const [left, top] = touch ? touchMenuOrigin(x, y, r.width, r.height) : [x, y];
  ctxEl.style.left = `${clamp(left, 8, window.innerWidth - r.width - 8)}px`;
  ctxEl.style.top = `${clamp(top, 8, window.innerHeight - r.height - 8)}px`;
  if (!touch) ctxEl.querySelector('button').focus();   // keyboard use starts on the first item; a finger needs no focus ring
}
function closeCtx() { ctxEl.hidden = true; }
// Lifting the finger that opened the menu also fires a click where it lifts: that click is ignored.
function guardCtxRelease(e) { if (e.pointerType !== 'mouse' && !ctxEl.hidden) ctxGuardUntil = performance.now() + 350; }
ctxEl.addEventListener('click', (e) => {
  if (performance.now() < ctxGuardUntil) return;
  const b = e.target.closest('button[data-cmd]');
  if (!b || b.disabled) return;
  const f = snapAt(ctxF), seg = segmentAt(ctxF);
  if (b.dataset.cmd === 'tune') { userTune(); tune(f); }
  else if (b.dataset.cmd === 'bookmark') addBookmark(f, seg ? seg.mode : S.mode);
  else if (b.dataset.cmd === 'center') { const span = view.f1 - view.f0; setView(ctxF - span / 2, ctxF + span / 2); }
  else if (b.dataset.cmd === 'segment') zoomToSegment(seg);
  else if (b.dataset.cmd === 'ignore') { excluded.push(f); renderPeaks(); }
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
  pinch = { d0: Math.max(24, Math.abs(a - b)), mid0: (a + b) / 2, f0: view.f0, f1: view.f1 };
}
function updatePinch() {
  const [a, b] = [...touchX.values()];
  const span0 = pinch.f1 - pinch.f0;
  const span = span0 * (pinch.d0 / Math.max(24, Math.abs(a - b)));
  const anchor = pinch.f0 + (pinch.mid0 / appW) * span0;   // frequency under the initial midpoint
  const start = anchor - (((a + b) / 2) / appW) * span;
  setView(start, start + span);
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
  if (drag.hit !== 'free') userTune();
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
  if (drag.hit === 'free' && !drag.moved) { userTune(); tune(snapAt(drag.f0)); }
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
  const seg = segmentAt(xToF(e.clientX));
  bandEl.title = seg ? `${seg.label} · ${fmtMHz(seg.from, 3)}–${fmtMHz(seg.to, 3)} MHz · glisser : hauteur du spectre` : 'Glisser : hauteur du spectre';
  if (!bandDrag) return;
  const dy = e.clientY - bandDrag.y0;
  if (Math.abs(dy) > 3) bandDrag.moved = true;
  if (bandDrag.moved) { S.split = clamp((bandDrag.h0 + dy) / (window.innerHeight - bandHeight()), 0.12, 0.8); layoutPending = true; }
});
bandEl.addEventListener('pointerup', (e) => {
  if (bandDrag && !bandDrag.moved) zoomToSegment(segmentAt(xToF(e.clientX)));
  bandDrag = null;
});
bandEl.addEventListener('pointercancel', () => { bandDrag = null; });
bandEl.addEventListener('pointerleave', hideHover);

let editingFreq = false;
function editFreq() {
  if (editingFreq) return;
  editingFreq = true;
  const input = document.createElement('input');
  input.type = 'text';
  input.inputMode = 'decimal';
  input.className = 'freq-input';
  input.value = fmtMHz(S.vfo);
  input.setAttribute('aria-label', 'Fréquence en MHz');
  freqEl.replaceChildren(input);
  input.focus();
  input.select();
  let closed = false;
  const finish = (apply) => {
    if (closed) return;
    closed = true;
    editingFreq = false;
    if (apply) {
      const v = parseFloat(input.value.replace(',', '.'));
      const hz = v < 1e5 ? v * 1e6 : v;
      if (Number.isFinite(hz) && hz >= 24e6 && hz <= 1766e6) { userTune(); tune(hz); }
    }
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
freqEl.addEventListener('dblclick', editFreq);
freqEl.addEventListener('click', () => { if (coarseQuery.matches) editFreq(); });   // touch: one tap opens the keypad
freqEl.addEventListener('wheel', (e) => {
  const digit = e.target.closest('.d');
  if (!digit) return;
  e.preventDefault();
  if (Math.abs(e.deltaY) < 2) return;
  userTune();
  tune(S.vfo + (e.deltaY < 0 ? 1 : -1) * Number(digit.dataset.m));
}, { passive: false });

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
  if (e.key === 'Escape') {
    if (!ctxEl.hidden) { closeCtx(); e.preventDefault(); }
    else if (openName) { closePanel(); e.preventDefault(); }
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
    case '+': case '=': zoomAt(1 / 1.5, S.vfo); break;
    case '-': case '_': zoomAt(1.5, S.vfo); break;
    case '0': setView(fStart(), fEnd()); break;
    case 'm': case 'M': setMode(MODE_ORDER[(MODE_ORDER.indexOf(S.mode) + 1) % MODE_ORDER.length]); break;
    default:
      if (e.key >= '1' && e.key <= '6') togglePanel(PANEL_ORDER[Number(e.key) - 1]);
      else return;
  }
  e.preventDefault();
});

let idleTimer = 0;
function poke() {
  document.body.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (S.autoHide && !openName) document.body.classList.add('idle'); }, 3000);
}
window.addEventListener('pointermove', poke, { passive: true });
window.addEventListener('pointerdown', poke, { passive: true });   // a tap wakes the interface too
window.addEventListener('resize', () => { layoutPending = true; });

const HINTS = {
  mouse: 'Clic : accorder · Molette : pas · Ctrl + molette : zoom · Clic droit : menu · 1 à 6 : panneaux',
  touch: 'Toucher : accorder · Pincer : zoom · Glisser : défiler · Appui long : menu',
};
function syncPointerMode() { hintEl.textContent = coarseQuery.matches ? HINTS.touch : HINTS.mouse; }
const onPointerModeChange = () => { syncPointerMode(); layoutPending = true; };
if (coarseQuery.addEventListener) coarseQuery.addEventListener('change', onPointerModeChange);
else coarseQuery.addListener(onPointerModeChange);   // Safari before 14

// ------------------------------------------------------------------ GPU views
// Without WebGL2 the rest of the interface still works; the status line says why the views stay empty.
let spectrumView = null, waterfallView = null, gpuError = '';
try {
  spectrumView = new SpectrumView(specCanvas, () => { specKey = ''; });
  waterfallView = new WaterfallView(wfCanvas, HIST_ROWS, () => {
    waterfallView.uploadAll(hist);
    waterfallView.setColours(lutBytes);
    wfKey = '';
  });
} catch (error) {
  spectrumView = null;
  waterfallView = null;
  gpuError = 'WebGL2 indisponible : spectre et cascade non affichés';
  console.error('Radiolack: the spectrum and waterfall cannot be drawn:', error);
}

// ------------------------------------------------------------------ main loop
// Frames are integrated as they arrive; each animation frame commits the
// waterfall lines that are due and redraws only the views that changed.
let lastTs = performance.now(), lineAcc = 0, lastUi = 0, lastPeakScan = 0;
function loop(ts) {
  const dt = Math.min(0.1, Math.max(0, (ts - lastTs) / 1000));
  lastTs = ts;
  if (layoutPending) { layoutPending = false; layout(); }
  if (lutDirty) { lutDirty = false; rebuildLut(); }
  lineAcc += dt * S.wfSpeed;
  let lines = 0;
  while (lineAcc >= 1 && lines < 6) { lineAcc -= 1; lines += 1; commitWfLine(Date.now()); }
  if (lineAcc > 6) lineAcc = 0;
  if (ts - lastPeakScan > 300) { lastPeakScan = ts; scanPeaks(ts); }
  memTick(ts / 1000);
  renderSpectrum();
  renderWaterfall();
  renderScales();
  updateMeterUi(ts);
  if (ts - lastUi > 400) { lastUi = ts; liveUi(); }
  requestAnimationFrame(loop);
}

// ------------------------------------------------------------------ start
resetBuffers();
layout();
syncSource();
syncDisplay();
renderPlanList();
renderBookmarks();
renderMemList();
syncScanTabs();
updateStatus();
tune(S.vfo);
syncPointerMode();
poke();
setTimeout(() => hintEl.classList.add('gone'), 9000);
document.addEventListener('visibilitychange', syncStream);
syncStream();
requestAnimationFrame(loop);
