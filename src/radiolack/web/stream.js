/*
 * Engine stream: the WebSocket that brings spectrum frames from the engine
 * (docs/design/protocol.md). It reconnects by itself after a disconnection.
 */

export const PROTOCOL_VERSION = 1;
const FRAME_KIND_SPECTRUM = 1;
const FRAME_VERSION = 1;
const HEADER_BYTES = 32;
const RETRY_FIRST_MS = 250;
const RETRY_MAX_MS = 4000;

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
 * @typedef {object} Hello
 * @property {string} source  kind of source the engine reads, such as "synthetic"
 */

/**
 * "connecting" before the first answer, "open" once the engine greeted the
 * interface, "waiting" while it retries after a disconnection, and
 * "incompatible" when the engine speaks another protocol version.
 * @typedef {'connecting' | 'open' | 'waiting' | 'incompatible'} StreamState
 */

/**
 * @typedef {object} StreamHandlers
 * @property {(hello: Hello) => void} onHello
 * @property {(frame: SpectrumFrame) => void} onFrame
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
      this.#hello(socket, event.data);
    } else if (this.#greeted && event.data instanceof ArrayBuffer) {
      const frame = decodeFrame(event.data);
      if (frame) this.#handlers.onFrame(frame);
    }
  }

  /**
   * @param {WebSocket} socket
   * @param {string} text
   */
  #hello(socket, text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      return;
    }
    if (this.#greeted || !message || message.type !== 'hello') return;
    if (message.protocol !== PROTOCOL_VERSION) {
      // Reconnecting would meet the same engine: the page must be reloaded.
      this.#incompatible = true;
      socket.close(1000);
      return;
    }
    this.#greeted = true;
    this.#retryMs = RETRY_FIRST_MS;
    this.#handlers.onHello({ source: String(message.source) });
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
