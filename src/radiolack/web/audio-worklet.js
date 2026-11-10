/*
 * Audio playback, in the browser's audio rendering thread: an AudioWorklet
 * processor that buffers the engine's audio frames and resamples them to the
 * device's rate with a windowed-sinc interpolator. The engine's clock runs a
 * little faster or slower than the device's: the playback rate follows the
 * buffer's level, within half a percent, to keep about 150 ms buffered. It
 * plays silence until that much is buffered, and again after running dry.
 *
 * Messages from audio.js: {type: 'frame', position, rateHz, pcm}, the
 * stream's audio frames (stream.js), and {type: 'reset'}, which drops what
 * is buffered.
 */

const PROCESSOR_NAME = 'radiolack-audio';
const TAPS = 16;            // input samples per output sample
const HALF = TAPS / 2;
const PHASES = 256;         // interpolator kernels per input sample, blended linearly
const KAISER_BETA = 7;      // about 70 dB of image rejection
const CUTOFF = 0.9;         // of the lower Nyquist frequency of the two rates
const TARGET_S = 0.15;      // audio buffered when playback starts, and kept on average
const MAX_S = 0.5;          // beyond this, the device fell behind: skip ahead to the target
const MAX_GAP_S = 0.5;      // a longer gap in the frames starts again
const CAPACITY_S = 1.2;     // room for MAX_S, plus a gap of silence and a frame
const MAX_DRIFT = 0.005;    // the playback rate stays within 0.5% of the nominal one
const DRIFT_GAIN = 0.01;    // rate change per relative error of the buffer's level
const SMOOTHING_S = 2;      // the buffer's level is averaged over about this long

/** @type {any} */
const scope = globalThis;
/** @type {new () => { port: MessagePort }} */
const Processor = scope.AudioWorkletProcessor;
/** @type {number} */
const outputRate = scope.sampleRate;

/** @param {number} x */
function besselI0(x) {
  let sum = 1, term = 1;
  for (let k = 1; k < 60 && term > 1e-12 * sum; k += 1) {
    term *= (x / (2 * k)) ** 2;
    sum += term;
  }
  return sum;
}

/**
 * Interpolator kernels: row p holds the taps for an output instant p / PHASES
 * of an input sample after sample n, applied to samples n - HALF + 1 to
 * n + HALF. Each row has unit gain at 0 Hz.
 * @param {number} cutoff  half the passband, relative to the input rate's Nyquist frequency
 */
function kernels(cutoff) {
  const table = new Float32Array((PHASES + 1) * TAPS);
  const norm = besselI0(KAISER_BETA);
  for (let p = 0; p <= PHASES; p += 1) {
    let sum = 0;
    for (let j = 0; j < TAPS; j += 1) {
      const d = p / PHASES + HALF - 1 - j;      // from tap j's sample to the output instant
      const x = d / HALF;
      const window = Math.abs(x) < 1 ? besselI0(KAISER_BETA * Math.sqrt(1 - x * x)) / norm : 0;
      const sinc = d === 0 ? 1 : Math.sin(Math.PI * cutoff * d) / (Math.PI * cutoff * d);
      table[p * TAPS + j] = sinc * window;
      sum += table[p * TAPS + j];
    }
    for (let j = 0; j < TAPS; j += 1) table[p * TAPS + j] /= sum;
  }
  return table;
}

class Playback extends Processor {
  rateHz = 0;                       // of the frames; 0 before the first one
  step = 1;                         // input samples per output sample, at the nominal rate
  table = new Float32Array(0);
  ring = new Float32Array(0);
  mask = 0;
  written = 0;                      // input samples written to the ring since the start
  readAt = 0;                       // the next output instant, in input samples since the start
  /** @type {number | null} */
  expected = null;                  // position of the next frame, if it follows on
  playing = false;
  level = TARGET_S;                 // average buffered audio, seconds
  drift = 0;

  constructor() {
    super();
    this.port.onmessage = (event) => this.receive(event.data);
  }

  /** @param {any} message */
  receive(message) {
    if (message.type === 'reset') {
      if (this.rateHz) this.restart(this.rateHz);
      return;
    }
    const { position, rateHz, pcm } = message;
    if (!(rateHz > 0) || !(pcm instanceof ArrayBuffer)) return;
    if (rateHz !== this.rateHz) {
      this.restart(rateHz);
    } else if (this.expected !== null) {
      const gap = (position - this.expected) >>> 0;     // positions wrap at 2^32
      if (gap > MAX_GAP_S * rateHz) this.restart(rateHz);
      else this.silence(gap);                            // frames the engine dropped for this client
    }
    this.append(pcm);
    this.expected = (position + pcm.byteLength / 2) >>> 0;
    if (this.written - this.readAt > MAX_S * rateHz) this.readAt = this.written - TARGET_S * rateHz;
  }

  /**
   * Start again at `rateHz`, with nothing buffered.
   * @param {number} rateHz
   */
  restart(rateHz) {
    if (rateHz !== this.rateHz) {
      this.rateHz = rateHz;
      this.step = rateHz / outputRate;
      this.table = kernels(CUTOFF * Math.min(1, outputRate / rateHz));
      let capacity = 1;
      while (capacity < CAPACITY_S * rateHz + TAPS) capacity *= 2;
      this.ring = new Float32Array(capacity);
      this.mask = capacity - 1;
    } else {
      this.ring.fill(0);
    }
    this.written = 0;
    this.readAt = 0;
    this.expected = null;
    this.playing = false;
    this.level = TARGET_S;
    this.drift = 0;
  }

  /** @param {ArrayBuffer} pcm  16-bit signed little-endian samples */
  append(pcm) {
    const view = new DataView(pcm), count = pcm.byteLength >> 1;
    for (let i = 0; i < count; i += 1) this.ring[(this.written + i) & this.mask] = view.getInt16(2 * i, true) / 32768;
    this.written += count;
  }

  /** @param {number} count */
  silence(count) {
    for (let i = 0; i < count; i += 1) this.ring[(this.written + i) & this.mask] = 0;
    this.written += count;
  }

  /**
   * One output sample at `at`, in input samples since the start.
   * @param {number} at
   */
  sample(at) {
    const n = Math.floor(at), scaled = (at - n) * PHASES, p = Math.floor(scaled), blend = scaled - p;
    const table = this.table, ring = this.ring, mask = this.mask, row = p * TAPS;
    let k = n - HALF + 1, sum = 0;
    for (let j = 0; j < TAPS; j += 1, k += 1) {
      const tap = table[row + j];
      sum += ring[k & mask] * (tap + blend * (table[row + TAPS + j] - tap));
    }
    return sum;
  }

  /**
   * @param {Float32Array[][]} _inputs
   * @param {Float32Array[][]} outputs
   * @returns {boolean}
   */
  process(_inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    let i = 0;
    if (this.playing) {
      const step = this.step * (1 + this.drift);
      for (; i < out.length; i += 1) {
        if (Math.floor(this.readAt) + HALF >= this.written) {
          this.playing = false;     // ran dry: buffer again
          break;
        }
        out[i] = this.sample(this.readAt);
        this.readAt += step;
      }
      this.follow(out.length);
    }
    out.fill(0, i);
    if (!this.playing && this.rateHz && this.written - this.readAt >= TARGET_S * this.rateHz) {
      this.playing = true;
      this.level = TARGET_S;
      this.drift = 0;
    }
    return true;
  }

  /**
   * Nudge the playback rate towards keeping TARGET_S buffered.
   * @param {number} count  output samples just played
   */
  follow(count) {
    const level = (this.written - this.readAt) / this.rateHz;
    this.level += (level - this.level) * Math.min(1, count / (outputRate * SMOOTHING_S));
    const drift = ((this.level - TARGET_S) / TARGET_S) * DRIFT_GAIN;
    this.drift = Math.min(MAX_DRIFT, Math.max(-MAX_DRIFT, drift));
  }
}

scope.registerProcessor(PROCESSOR_NAME, Playback);
