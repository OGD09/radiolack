/*
 * Engine stream: the WebSocket that brings spectrum frames and the source
 * state from the engine, and takes changes to the source settings to it
 * (docs/design/protocol.md). It reconnects by itself after a disconnection.
 */

export const PROTOCOL_VERSION = 2;
const FRAME_KIND_SPECTRUM = 1;
const FRAME_VERSION = 1;
const HEADER_BYTES = 32;
const RETRY_FIRST_MS = 250;
const RETRY_MAX_MS = 4000;
const SOURCE_STATUSES = ['connecting', 'streaming', 'waiting', 'idle'];
const SETTINGS = ['centre_hz', 'sample_rate_hz', 'gain_db', 'ppm', 'dc_removal'];

/**
 * @typedef {object} SpectrumFrame
 * @property {number} sequence   wraps at 2^32
 * @property {number} centreHz
 * @property {number} rateHz     sample rate: the width of the frame
 * @property {number} dbMin      level of code 0, dBFS
 * @property {number} dbStep     level step per code, dB
 * @property {Uint8Array} codes  one code per bin, lowest frequency first
 */

/**
 * "streaming" while samples arrive; "connecting", "waiting" (to retry, see
 * `detail`) and "idle" (no interface listened) while they do not.
 * @typedef {'connecting' | 'streaming' | 'waiting' | 'idle'} SourceStatus
 */

/**
 * @typedef {'centre_hz' | 'sample_rate_hz' | 'gain_db' | 'ppm' | 'dc_removal'} Setting
 */

/**
 * The engine's source: what it is, whether samples arrive, and the settings
 * asked of the receiver, with the engine's own DC removal. Frames carry the
 * setting they were taken with.
 * @typedef {object} SourceState
 * @property {string} kind              such as "synthetic", "rtl_sdr" or "rtl_tcp"
 * @property {SourceStatus} status
 * @property {string} detail            why the source waits, or ""
 * @property {string | null} address    of a network source, as host:port
 * @property {string | null} tuner      name of the receiver's tuner, once known
 * @property {number} centreHz
 * @property {number} rateHz
 * @property {number | null} gainDb     null: automatic gain, or no gain setting
 * @property {number | null} ppm        frequency correction, whole parts per million; null: none
 * @property {boolean} dcRemoval        the engine takes a constant offset off the samples
 * @property {number[]} gainsDb         manual gains the tuner offers, lowest first
 * @property {[number, number]} rangeHz centre frequencies the source takes
 * @property {Setting[]} settable       settings an interface may change
 */

/**
 * Changes to ask of the engine; the others stay as they are.
 * @typedef {object} SourceChanges
 * @property {number} [centreHz]
 * @property {number} [rateHz]
 * @property {number | null} [gainDb]   null: automatic gain
 * @property {number} [ppm]             a whole number
 * @property {boolean} [dcRemoval]
 */

/**
 * "connecting" before the first answer, "open" once the engine greeted the
 * interface, "waiting" while it retries after a disconnection, and
 * "incompatible" when the engine speaks another protocol version.
 * @typedef {'connecting' | 'open' | 'waiting' | 'incompatible'} StreamState
 */

/**
 * @typedef {object} StreamHandlers
 * @property {(frame: SpectrumFrame) => void} onFrame
 * @property {(source: SourceState) => void} onSource  sent first, then at every change
 * @property {(message: string) => void} onError       why the engine refused a change from this interface
 * @property {(state: StreamState) => void} onState
 */

/**
 * Decode a binary message. Returns null for anything but a well-formed
 * spectrum frame of a version this interface reads.
 * @param {ArrayBuffer} buffer
 * @returns {SpectrumFrame | null}
 */
export function decodeFrame(buffer) {
  if (buffer.byteLength <= HEADER_BYTES) return null;
  const header = new DataView(buffer, 0, HEADER_BYTES);
  if (header.getUint8(0) !== FRAME_KIND_SPECTRUM || header.getUint8(1) !== FRAME_VERSION) return null;
  const frame = {
    sequence: header.getUint32(4, true),
    centreHz: header.getFloat64(8, true),
    rateHz: header.getFloat64(16, true),
    dbMin: header.getFloat32(24, true),
    dbStep: header.getFloat32(28, true),
    codes: new Uint8Array(buffer, HEADER_BYTES),
  };
  const valid = Number.isFinite(frame.centreHz) && Number.isFinite(frame.rateHz) && frame.rateHz > 0
    && Number.isFinite(frame.dbMin) && Number.isFinite(frame.dbStep) && frame.dbStep > 0;
  return valid ? frame : null;
}

/**
 * Decode a source state message. Returns null when a field is missing or
 * malformed; settings this interface does not know are left out.
 * @param {any} message  a parsed JSON message of type "source"
 * @returns {SourceState | null}
 */
export function decodeSource(message) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const textOrNull = (value) => value === null || typeof value === 'string';
  const m = message;
  const valid = typeof m.kind === 'string' && SOURCE_STATUSES.includes(m.status) && typeof m.detail === 'string'
    && textOrNull(m.address) && textOrNull(m.tuner)
    && finite(m.centre_hz) && finite(m.sample_rate_hz) && m.sample_rate_hz > 0
    && (m.gain_db === null || finite(m.gain_db))
    && (m.ppm === null || Number.isInteger(m.ppm)) && typeof m.dc_removal === 'boolean'
    && Array.isArray(m.gains_db) && m.gains_db.every(finite)
    && Array.isArray(m.range_hz) && m.range_hz.length === 2 && m.range_hz.every(finite)
    && m.range_hz[0] <= m.range_hz[1]
    && Array.isArray(m.settable) && m.settable.every((/** @type {unknown} */ name) => typeof name === 'string');
  if (!valid) return null;
  return {
    kind: m.kind,
    status: m.status,
    detail: m.detail,
    address: m.address,
    tuner: m.tuner,
    centreHz: m.centre_hz,
    rateHz: m.sample_rate_hz,
    gainDb: m.gain_db,
    ppm: m.ppm,
    dcRemoval: m.dc_removal,
    gainsDb: m.gains_db.slice(),
    rangeHz: [m.range_hz[0], m.range_hz[1]],
    settable: m.settable.filter((/** @type {string} */ name) => SETTINGS.includes(name)),
  };
}

/** The address of the stream on the server that served this page. */
function streamUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/stream`;
}

/** One WebSocket to the engine at a time, opened again after each disconnection. */
export class EngineStream {
  /** @type {StreamHandlers} */
  #handlers;
  #url;
  /** @type {WebSocket | null} */
  #socket = null;
  #running = false;
  #greeted = false;
  #incompatible = false;
  #retryMs = RETRY_FIRST_MS;
  #timer = 0;

  /**
   * @param {StreamHandlers} handlers
   * @param {string} [url]
   */
  constructor(handlers, url = streamUrl()) {
    this.#handlers = handlers;
    this.#url = url;
  }

  /** Connect, unless already connected or connecting. */
  start() {
    if (this.#running) return;
    this.#running = true;
    this.#incompatible = false;
    this.#retryMs = RETRY_FIRST_MS;
    this.#handlers.onState('connecting');
    this.#connect();
  }

  /**
   * Ask the engine to change source settings. Every interface then receives
   * the new state, or this one an error. Returns false, sending nothing,
   * when the stream is not open.
   * @param {SourceChanges} changes
   * @returns {boolean}
   */
  sendSource(changes) {
    const socket = this.#socket;
    if (!socket || !this.#greeted || socket.readyState !== WebSocket.OPEN) return false;
    /** @type {Record<string, string | number | boolean | null>} */
    const message = { type: 'source' };
    if (changes.centreHz !== undefined) message.centre_hz = changes.centreHz;
    if (changes.rateHz !== undefined) message.sample_rate_hz = changes.rateHz;
    if (changes.gainDb !== undefined) message.gain_db = changes.gainDb;
    if (changes.ppm !== undefined) message.ppm = changes.ppm;
    if (changes.dcRemoval !== undefined) message.dc_removal = changes.dcRemoval;
    socket.send(JSON.stringify(message));
    return true;
  }

  /** Disconnect and stop retrying; the engine idles once no client listens. */
  stop() {
    this.#running = false;
    clearTimeout(this.#timer);
    const socket = this.#socket;
    this.#socket = null;
    if (socket) socket.close(1000);
  }

  #connect() {
    let socket;
    try {
      socket = new WebSocket(this.#url);
    } catch (error) {
      // Some browsers throw at once when a policy blocks the address: retry later like any failure.
      console.error('Radiolack: cannot open the engine stream:', error);
      this.#retry();
      return;
    }
    socket.binaryType = 'arraybuffer';
    this.#socket = socket;
    this.#greeted = false;
    socket.addEventListener('message', (event) => this.#receive(socket, event));
    // A failed connection fires "error" then "close": the retry is scheduled on "close" only.
    socket.addEventListener('close', () => this.#closed(socket));
  }

  /**
   * @param {WebSocket} socket
   * @param {MessageEvent} event
   */
  #receive(socket, event) {
    if (socket !== this.#socket) return;
    if (typeof event.data === 'string') {
      this.#text(socket, event.data);
    } else if (this.#greeted && event.data instanceof ArrayBuffer) {
      const frame = decodeFrame(event.data);
      if (frame) this.#handlers.onFrame(frame);
    }
  }

  /**
   * A JSON message: the hello first, then source states and errors.
   * @param {WebSocket} socket
   * @param {string} text
   */
  #text(socket, text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      return;
    }
    if (!message || typeof message !== 'object') return;
    if (!this.#greeted) {
      if (message.type === 'hello') this.#hello(socket, message);
    } else if (message.type === 'source') {
      const source = decodeSource(message);
      if (source) this.#handlers.onSource(source);
    } else if (message.type === 'error') {
      this.#handlers.onError(String(message.message));
    }
  }

  /**
   * @param {WebSocket} socket
   * @param {any} message  a parsed hello
   */
  #hello(socket, message) {
    if (message.protocol !== PROTOCOL_VERSION) {
      // Reconnecting would meet the same engine: the page must be reloaded.
      this.#incompatible = true;
      socket.close(1000);
      return;
    }
    this.#greeted = true;
    this.#retryMs = RETRY_FIRST_MS;
    this.#handlers.onState('open');
  }

  /** @param {WebSocket} socket */
  #closed(socket) {
    if (socket !== this.#socket) return;
    this.#socket = null;
    if (!this.#running) return;
    if (this.#incompatible) {
      this.#running = false;
      this.#handlers.onState('incompatible');
      return;
    }
    this.#retry();
  }

  #retry() {
    this.#handlers.onState('waiting');
    const delay = this.#retryMs;
    this.#retryMs = Math.min(RETRY_MAX_MS, delay * 2);
    this.#timer = window.setTimeout(() => { if (this.#running) this.#connect(); }, delay);
  }
}
