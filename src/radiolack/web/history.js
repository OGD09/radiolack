/*
 * A spectrum's frames as the interface keeps them: the latest frame, the
 * averaged trace, the peak hold, and the waterfall's lines, each with its
 * time and the centre of the frames it comes from, so that a capture that
 * moves keeps every line at its own frequencies. The wide spectrum and the
 * fine spectrum of the VFO's channel each keep one, at their own
 * resolution (deepzoom.js).
 */

/** The level of history code 0, dBFS: the waterfall's lines take 8 bits. */
export const HIST_DB_MIN = -160;
/** The level step of the history's codes, dB. */
export const HIST_DB_STEP = 0.625;
/** The trace's level before any frame, dBFS. */
const EMPTY_DB = -140;
/** The peak hold's level before any frame, dBFS: under any level drawn. */
const NO_PEAK_DB = -300;
/**
 * The frames a waterfall line must span, on average, to keep their highest
 * power rather than their mean: with fewer, lines that take one frame and
 * lines that take two would differ by about 2 dB of noise, in stripes.
 */
export const PEAK_LINE_FRAMES = 4;

export class SpectrumHistory {
  /** Lines the waterfall keeps. */
  rows;
  /** The capture of the frames kept: the frequency on the air of bin `bins / 2`, Hz. */
  centreHz = 0;
  /** Its width on the air, Hz. */
  widthHz = 0;
  /** Its bins, lowest frequency first; 0 before any frame. */
  bins = 0;
  /** The latest frame, linear power per bin. */
  frameLin = new Float32Array(0);
  /** The averaged trace, linear and in dBFS. */
  avgLin = new Float32Array(0);
  avgDb = new Float32Array(0);
  /** The peak hold, dBFS. */
  peakDb = new Float32Array(0);
  /** The frames' power since the latest waterfall line: their sum and their highest, per bin. */
  wfAcc = new Float32Array(0);
  wfMax = new Float32Array(0);
  wfCount = 0;
  /** The frames' interval, ms, averaged; 0 before two frames of this capture. */
  frameMs = 0;
  /** The waterfall's lines, `bins` codes each, row after row. */
  hist = new Uint8Array(0);
  /** When each line was made, ms since the epoch. */
  histTime;
  /** The centre of the capture each line comes from, Hz. */
  histCentre;
  /** The row of the newest line, -1 before any. */
  head = -1;
  /** Lines kept. */
  filled = 0;
  /** The average holds a frame: the next one is averaged with it. */
  avgPrimed = false;
  /** performance.now() at the latest frame of this capture. */
  lastFrameMs = -Infinity;
  /** Frames received since the capture last started again. */
  frames = 0;
  /** Counts every change of what is kept: what is drawn changes with it. */
  version = 0;
  /** The noise floor, dBFS, once estimated. */
  floorDb = -97;
  levelsPrimed = false;
  #codeDbMin = NaN;
  #codeDbStep = NaN;
  #codeToLin = new Float32Array(256);
  /** The lines' buffer that a rescale writes into, then swaps with `hist`. */
  #spare = new Uint8Array(0);

  /** @param {number} rows  lines the waterfall keeps */
  constructor(rows) {
    this.rows = rows;
    this.histTime = new Float64Array(rows);
    this.histCentre = new Float64Array(rows);
  }

  /** The width of a bin, Hz; 0 before any frame. */
  get binHz() {
    return this.bins ? this.widthHz / this.bins : 0;
  }

  /**
   * Start again for another capture: every buffer and the history are
   * cleared, kept in memory while the number of bins stays.
   * @param {number} centreHz
   * @param {number} widthHz
   * @param {number} bins
   */
  reset(centreHz, widthHz, bins) {
    if (bins !== this.bins) {
      this.frameLin = new Float32Array(bins);
      this.avgLin = new Float32Array(bins);
      this.avgDb = new Float32Array(bins);
      this.peakDb = new Float32Array(bins);
      this.wfAcc = new Float32Array(bins);
      this.wfMax = new Float32Array(bins);
      this.hist = new Uint8Array(bins * this.rows);
    }
    this.centreHz = centreHz;
    this.widthHz = widthHz;
    this.bins = bins;
    this.avgDb.fill(EMPTY_DB);
    this.peakDb.fill(NO_PEAK_DB);
    this.wfAcc.fill(0);
    this.wfMax.fill(0);
    this.wfCount = 0;
    this.frameMs = 0;
    this.head = -1;
    this.filled = 0;
    this.avgPrimed = false;
    this.levelsPrimed = false;
    this.lastFrameMs = -Infinity;
    this.frames = 0;
    this.version += 1;
  }

  /** Forget every frame: the next one starts a capture of its own. */
  clear() {
    this.reset(0, 0, this.bins);
  }

  /**
   * Whether a frame of this capture follows the frames kept, moved by less
   * than its width: the history then slides rather than starts again.
   * @param {number} centreHz
   * @param {number} widthHz
   * @param {number} bins
   * @returns {boolean}
   */
  slides(centreHz, widthHz, bins) {
    return this.widthHz > 0 && widthHz === this.widthHz && bins === this.bins && Math.abs(centreHz - this.centreHz) < widthHz;
  }

  /**
   * The same capture moved by less than its width: the waterfall's lines and
   * the peak hold keep their frequencies, what left the capture goes and the
   * new part starts empty. The next frame starts the average again.
   * @param {number} centreHz
   */
  slide(centreHz) {
    const bins = this.bins, moved = Math.round((this.centreHz - centreHz) / this.binHz);   // bin i of the old capture is bin i + moved
    const peak = this.peakDb;
    if (moved >= bins || moved <= -bins) {
      peak.fill(NO_PEAK_DB);
    } else if (moved > 0) {
      peak.copyWithin(moved, 0, bins - moved);
      peak.fill(NO_PEAK_DB, 0, moved);
    } else if (moved < 0) {
      peak.copyWithin(0, -moved);
      peak.fill(NO_PEAK_DB, bins + moved);
    }
    this.centreHz = centreHz;
    this.wfAcc.fill(0);
    this.wfMax.fill(0);
    this.wfCount = 0;
    this.avgPrimed = false;
    this.lastFrameMs = -Infinity;
    this.version += 1;
  }

  /** Start the peak hold again. */
  clearPeaks() {
    this.peakDb.fill(NO_PEAK_DB);
    this.version += 1;
  }

  /**
   * Add a frame of this capture: its codes stand for `dbMin + c * dbStep`
   * dBFS. The trace averages it with the frames before by `smoothing`, 0
   * for none; the peak hold and the next waterfall line take it too.
   * @param {Uint8Array} codes      one per bin
   * @param {number} dbMin
   * @param {number} dbStep
   * @param {number} smoothing      0 to 1
   * @param {number} nowMs          performance.now()
   */
  add(codes, dbMin, dbStep, smoothing, nowMs) {
    if (dbMin !== this.#codeDbMin || dbStep !== this.#codeDbStep) {
      this.#codeDbMin = dbMin;
      this.#codeDbStep = dbStep;
      for (let c = 0; c < 256; c += 1) this.#codeToLin[c] = 10 ** ((dbMin + c * dbStep) / 10);
    }
    const bins = this.bins, toLin = this.#codeToLin, a = this.avgPrimed ? smoothing : 0;
    const frame = this.frameLin, avg = this.avgLin, avgDb = this.avgDb, peak = this.peakDb, acc = this.wfAcc, top = this.wfMax;
    for (let i = 0; i < bins; i += 1) {
      const v = toLin[codes[i]];
      frame[i] = v;
      const m = a * avg[i] + (1 - a) * v;
      avg[i] = m;
      const db = 10 * Math.log10(m + 1e-30);
      avgDb[i] = db;
      if (db > peak[i]) peak[i] = db;
      acc[i] += v;
      if (v > top[i]) top[i] = v;
    }
    if (Number.isFinite(this.lastFrameMs) && nowMs > this.lastFrameMs) {
      const interval = nowMs - this.lastFrameMs;
      this.frameMs = this.frameMs ? 0.9 * this.frameMs + 0.1 * interval : interval;
    }
    this.wfCount += 1;
    this.avgPrimed = true;
    this.lastFrameMs = nowMs;
    this.frames += 1;
    this.version += 1;
  }

  /**
   * Make a waterfall line from the frames since the previous line: the
   * highest power of each bin when a line spans PEAK_LINE_FRAMES frames or
   * more at `periodMs` a line, so that a slow waterfall keeps brief
   * signals, and their mean otherwise. Frames may come slower than lines: a line then repeats the latest
   * frame while it is at most `repeatMs` old, so that the waterfall keeps its
   * speed; once frames stop, lines stop too, rather than stretch the last.
   * @param {number} nowTime  ms since the epoch, the line's time
   * @param {number} nowMs    performance.now()
   * @param {number} repeatMs
   * @param {number} [periodMs]  the time between two lines; 0, the default, for the mean
   * @returns {boolean} whether a line was made, at row `head`
   */
  commitLine(nowTime, nowMs, repeatMs, periodMs = 0) {
    const bins = this.bins;
    if (!bins) return false;
    const highest = this.frameMs > 0 && periodMs >= PEAK_LINE_FRAMES * this.frameMs;
    let levels = highest ? this.wfMax : this.wfAcc, inv = highest ? 1 : 1 / this.wfCount;
    if (!this.wfCount) {
      if (nowMs - this.lastFrameMs > repeatMs) return false;
      levels = this.frameLin;
      inv = 1;
    }
    this.head = (this.head + 1) % this.rows;
    this.filled = Math.min(this.rows, this.filled + 1);
    const hist = this.hist, acc = this.wfAcc, top = this.wfMax, base = this.head * bins;
    for (let i = 0; i < bins; i += 1) {
      const q = (10 * Math.log10(levels[i] * inv + 1e-30) - HIST_DB_MIN) / HIST_DB_STEP;
      hist[base + i] = q <= 0 ? 0 : q >= 255 ? 255 : q + 0.5;
      acc[i] = 0;
      top[i] = 0;
    }
    this.wfCount = 0;
    this.histTime[this.head] = nowTime;
    this.histCentre[this.head] = this.centreHz;
    return true;
  }

  /**
   * Draw the waterfall's lines again for another speed: `ratio` lines kept
   * make each new line, newest first, a new line taking the highest code of
   * each bin among them, with the time and the capture's centre of the
   * newest; lines of another centre than the newest's are left out of it.
   * Below 1, a line kept makes several new ones: going back to a faster
   * speed leaves the past as coarse as it was. Keeps at most `rows` lines.
   * @param {number} ratio  the speed before over the speed after
   */
  rescale(ratio) {
    const bins = this.bins, filled = this.filled;
    if (!(ratio > 0) || !Number.isFinite(ratio) || ratio === 1 || !bins || !filled) return;
    const count = Math.max(1, Math.min(this.rows, Math.floor(filled / ratio)));
    if (this.#spare.length !== this.hist.length) this.#spare = new Uint8Array(this.hist.length);
    const from = this.hist, to = this.#spare;
    const times = new Float64Array(this.rows), centres = new Float64Array(this.rows);
    for (let age = 0; age < count; age += 1) {
      const a = Math.min(filled - 1, Math.floor(age * ratio));
      const b = Math.min(filled, Math.max(a + 1, Math.floor((age + 1) * ratio)));
      const newest = this.rowAt(a), centre = this.histCentre[newest], row = count - 1 - age, base = row * bins;
      to.set(from.subarray(newest * bins, (newest + 1) * bins), base);
      for (let older = a + 1; older < b; older += 1) {
        const r = this.rowAt(older);
        if (this.histCentre[r] !== centre) continue;
        for (let i = 0, src = r * bins; i < bins; i += 1) if (from[src + i] > to[base + i]) to[base + i] = from[src + i];
      }
      times[row] = this.histTime[newest];
      centres[row] = centre;
    }
    this.#spare = from;
    this.hist = to;
    this.histTime = times;
    this.histCentre = centres;
    this.head = count - 1;
    this.filled = count;
    this.version += 1;
  }

  /**
   * One waterfall line's codes.
   * @param {number} row
   * @returns {Uint8Array}
   */
  line(row) {
    return this.hist.subarray(row * this.bins, (row + 1) * this.bins);
  }

  /**
   * Where each waterfall line's first bin lies, in bins from the capture's
   * first bin, for the waterfall view.
   * @returns {Float32Array}
   */
  lineOffsets() {
    const offsets = new Float32Array(this.rows), bin = this.binHz;
    if (!bin) return offsets;
    for (let row = 0; row < this.rows; row += 1) offsets[row] = (this.histCentre[row] - this.centreHz) / bin;
    return offsets;
  }

  /**
   * The row of the line `age` lines below the newest, or -1 when the history
   * holds no such line.
   * @param {number} age
   * @returns {number}
   */
  rowAt(age) {
    return age >= 0 && age < this.filled ? (this.head - age + this.rows) % this.rows : -1;
  }

  /**
   * The bin of line `row` nearest f, or -1 when f lies outside [lo, hi],
   * frequencies on the air taken where that line's capture was.
   * @param {number} row
   * @param {number} f       Hz
   * @param {number} loHz    of the part of the line that shows the air, relative to its centre
   * @param {number} hiHz
   * @returns {number}
   */
  lineBinAt(row, f, loHz, hiHz) {
    const offset = f - this.histCentre[row];
    if (!this.bins || offset < loHz || offset > hiHz) return -1;
    const bin = Math.round((offset + this.widthHz / 2) / this.binHz);
    return Math.min(this.bins - 1, Math.max(0, bin));
  }
}
