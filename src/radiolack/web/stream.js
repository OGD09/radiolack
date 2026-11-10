/*
 * Engine stream: the WebSocket that brings spectrum frames, audio, the source
 * state, the demodulation settings, the bookmarks, the saved preferences and
 * the storage state from the engine, and takes this interface's changes, its
 * preferences and its request for audio to it (docs/design/protocol.md). It
 * reconnects by itself after a disconnection.
 */

export const PROTOCOL_VERSION = 4;
const FRAME_KIND_SPECTRUM = 1;
const FRAME_VERSION = 2;
const HEADER_BYTES = 40;
const FLAG_SQUELCH_OPEN = 0x1;
const FRAME_KIND_AUDIO = 2;
const AUDIO_VERSION = 1;
const AUDIO_HEADER_BYTES = 16;
const RETRY_FIRST_MS = 250;
const RETRY_MAX_MS = 4000;
const SOURCE_STATUSES = ['connecting', 'streaming', 'waiting', 'idle'];
const SETTINGS = ['address', 'centre_hz', 'sample_rate_hz', 'gain_db', 'ppm', 'dc_removal'];
const COMMANDS = ['source', 'demod', 'audio', 'bookmark', 'preferences'];

/**
 * @typedef {object} SpectrumFrame
 * @property {number} sequence          wraps at 2^32
 * @property {number} centreHz
 * @property {number} rateHz            sample rate: the width of the frame
 * @property {number} dbMin             level of code 0, dBFS
 * @property {number} dbStep            level step per code, dB
 * @property {number | null} levelDb    power in the VFO's channel, dBFS; null: nothing received there
 * @property {number | null} squelchDb  squelch threshold in use, dBFS; null: not known yet
 * @property {boolean} squelchOpen
 * @property {Uint8Array} codes         one code per bin, lowest frequency first
 */

/**
 * Audio that follows the previous frame unless `position` says otherwise.
 * @typedef {object} AudioFrame
 * @property {number} position  of the first sample, counted at `rateHz`; wraps at 2^32
 * @property {number} rateHz
 * @property {ArrayBuffer} pcm  16-bit signed little-endian mono samples
 */

/**
 * The demodulation settings, shared by every interface.
 * @typedef {object} DemodState
 * @property {number} frequencyHz   the VFO
 * @property {string} mode          one of `modes`
 * @property {number} bandwidthHz   the channel filter's width
 * @property {number} squelchDb     the threshold set by hand, dBFS
 * @property {boolean} squelchAuto  the threshold follows the noise instead
 * @property {string[]} modes       modes the engine takes
 * @property {string[]} audioModes  modes it demodulates to audio; the others are silent
 */

/**
 * Changes to ask of the engine; the others stay as they are.
 * @typedef {object} DemodChanges
 * @property {number} [frequencyHz]
 * @property {string} [mode]
 * @property {number} [bandwidthHz]
 * @property {number} [squelchDb]
 * @property {boolean} [squelchAuto]
 */

/**
 * A named frequency with a mode, shared by every interface. Its id lasts as
 * long as the engine runs.
 * @typedef {object} Bookmark
 * @property {number} id
 * @property {number} frequencyHz
 * @property {string} name
 * @property {string} mode
 * @property {boolean} scan     the memory scan stops here
 */

/**
 * Fields of a bookmark to set; the others stay as they are.
 * @typedef {object} BookmarkFields
 * @property {number} [frequencyHz]
 * @property {string} [name]
 * @property {string} [mode]
 * @property {boolean} [scan]
 */

/**
 * An interface's own settings, by name, which the engine keeps for the next
 * interfaces: numbers, true or false, short texts or null.
 * @typedef {Record<string, number | boolean | string | null>} Preferences
 */

/**
 * Where the engine saves the settings and bookmarks.
 * @typedef {object} StorageState
 * @property {string | null} directory  the settings folder; null: nothing is saved
 * @property {boolean} saving           changes reach the disk
 * @property {string} problem           what went wrong with the files, or ""
 */

/** @typedef {'source' | 'demod' | 'audio' | 'bookmark' | 'preferences'} Command */

/**
 * "streaming" while samples arrive; "connecting", "waiting" (to retry, see
 * `detail`) and "idle" (no interface listened) while they do not.
 * @typedef {'connecting' | 'streaming' | 'waiting' | 'idle'} SourceStatus
 */

/**
 * @typedef {'address' | 'centre_hz' | 'sample_rate_hz' | 'gain_db' | 'ppm' | 'dc_removal'} Setting
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
 * @property {string} [address]         of an rtl_tcp server: HOST, PORT or HOST:PORT
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
 * @property {(frame: AudioFrame) => void} onAudio     while this interface asks for audio
 * @property {(source: SourceState) => void} onSource  sent first, then at every change
 * @property {(demod: DemodState) => void} onDemod     sent next, then at every change this interface has not overtaken
 * @property {(bookmarks: Bookmark[]) => void} onBookmarks  sent next, then at every change, in frequency order
 * @property {(values: Preferences) => void} onPreferences  sent next, once per connection: the latest saved
 * @property {(storage: StorageState) => void} onStorage    sent next, then at every change
 * @property {(message: string, command: Command | null) => void} onError
 *   why the engine refused a command from this interface, and which kind of command
 * @property {(state: StreamState) => void} onState
 */

/** @param {number} value */
const dbOrNull = (value) => (Number.isFinite(value) ? value : null);

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
    levelDb: dbOrNull(header.getFloat32(32, true)),
    squelchDb: dbOrNull(header.getFloat32(36, true)),
    squelchOpen: (header.getUint16(2, true) & FLAG_SQUELCH_OPEN) !== 0,
    codes: new Uint8Array(buffer, HEADER_BYTES),
  };
  const valid = Number.isFinite(frame.centreHz) && Number.isFinite(frame.rateHz) && frame.rateHz > 0
    && Number.isFinite(frame.dbMin) && Number.isFinite(frame.dbStep) && frame.dbStep > 0;
  return valid ? frame : null;
}

/**
 * Decode a binary message. Returns null for anything but a well-formed
 * audio frame of a version this interface reads.
 * @param {ArrayBuffer} buffer
 * @returns {AudioFrame | null}
 */
export function decodeAudio(buffer) {
  if (buffer.byteLength < AUDIO_HEADER_BYTES || (buffer.byteLength - AUDIO_HEADER_BYTES) % 2) return null;
  const header = new DataView(buffer, 0, AUDIO_HEADER_BYTES);
  if (header.getUint8(0) !== FRAME_KIND_AUDIO || header.getUint8(1) !== AUDIO_VERSION) return null;
  const rateHz = header.getFloat64(8, true);
  if (!(Number.isFinite(rateHz) && rateHz > 0)) return null;
  return { position: header.getUint32(4, true), rateHz, pcm: buffer.slice(AUDIO_HEADER_BYTES) };
}

/**
 * Decode a demodulation state message. Returns null when a field is missing
 * or malformed.
 * @param {any} message  a parsed JSON message of type "demod"
 * @returns {(DemodState & { acknowledged: number }) | null}
 */
export function decodeDemod(message) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const names = (value) => Array.isArray(value) && value.every((name) => typeof name === 'string');
  const m = message;
  const valid = finite(m.frequency_hz) && names(m.modes) && m.modes.includes(m.mode)
    && finite(m.bandwidth_hz) && m.bandwidth_hz > 0 && finite(m.squelch_db) && typeof m.squelch_auto === 'boolean'
    && names(m.audio_modes) && Number.isSafeInteger(m.acknowledged) && m.acknowledged >= 0;
  if (!valid) return null;
  return {
    frequencyHz: m.frequency_hz,
    mode: m.mode,
    bandwidthHz: m.bandwidth_hz,
    squelchDb: m.squelch_db,
    squelchAuto: m.squelch_auto,
    modes: m.modes.slice(),
    audioModes: m.audio_modes.slice(),
    acknowledged: m.acknowledged,
  };
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

/**
 * Decode a bookmarks message. Returns null when an entry is missing a field
 * or holds a malformed one.
 * @param {any} message  a parsed JSON message of type "bookmarks"
 * @returns {Bookmark[] | null}
 */
export function decodeBookmarks(message) {
  if (!Array.isArray(message.bookmarks)) return null;
  /** @type {Bookmark[]} */
  const bookmarks = [];
  for (const b of message.bookmarks) {
    const valid = b !== null && typeof b === 'object' && Number.isSafeInteger(b.id) && b.id > 0
      && typeof b.frequency_hz === 'number' && Number.isFinite(b.frequency_hz)
      && typeof b.name === 'string' && typeof b.mode === 'string' && typeof b.scan === 'boolean';
    if (!valid) return null;
    bookmarks.push({ id: b.id, frequencyHz: b.frequency_hz, name: b.name, mode: b.mode, scan: b.scan });
  }
  return bookmarks;
}

/**
 * Decode a preferences message. Returns null unless its values are a JSON
 * object; values that are not numbers, true or false, texts or null are left
 * out.
 * @param {any} message  a parsed JSON message of type "preferences"
 * @returns {Preferences | null}
 */
export function decodePreferences(message) {
  const values = message.values;
  if (values === null || typeof values !== 'object' || Array.isArray(values)) return null;
  // fromEntries defines own properties: a "__proto__" entry cannot change the prototype.
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value === null
    || typeof value === 'boolean' || typeof value === 'string'
    || (typeof value === 'number' && Number.isFinite(value))));
}

/**
 * Decode a storage message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "storage"
 * @returns {StorageState | null}
 */
export function decodeStorage(message) {
  const m = message;
  const valid = (m.directory === null || typeof m.directory === 'string') && typeof m.saving === 'boolean'
    && typeof m.problem === 'string';
  return valid ? { directory: m.directory, saving: m.saving, problem: m.problem } : null;
}

/**
 * The fields of a bookmark command, named as the engine names them.
 * @param {BookmarkFields} fields
 */
function bookmarkFields(fields) {
  /** @type {Record<string, string | number | boolean>} */
  const message = {};
  if (fields.frequencyHz !== undefined) message.frequency_hz = fields.frequencyHz;
  if (fields.name !== undefined) message.name = fields.name;
  if (fields.mode !== undefined) message.mode = fields.mode;
  if (fields.scan !== undefined) message.scan = fields.scan;
  return message;
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
  #audio = false;         // this interface wants audio
  #lastChangeId = 0;      // ids of demodulation changes, counted for the whole page
  #awaitedId = 0;         // the last one sent on this connection: older echoes are stale

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
    /** @type {Record<string, string | number | boolean | null>} */
    const message = { type: 'source' };
    if (changes.address !== undefined) message.address = changes.address;
    if (changes.centreHz !== undefined) message.centre_hz = changes.centreHz;
    if (changes.rateHz !== undefined) message.sample_rate_hz = changes.rateHz;
    if (changes.gainDb !== undefined) message.gain_db = changes.gainDb;
    if (changes.ppm !== undefined) message.ppm = changes.ppm;
    if (changes.dcRemoval !== undefined) message.dc_removal = changes.dcRemoval;
    return this.#send(message);
  }

  /**
   * Ask the engine to change demodulation settings. Every interface then
   * receives the new settings; until they include this change, this
   * interface ignores older settings, which would undo it on screen.
   * Returns false, sending nothing, when the stream is not open.
   * @param {DemodChanges} changes
   * @returns {boolean}
   */
  sendDemod(changes) {
    /** @type {Record<string, string | number | boolean>} */
    const message = { type: 'demod', id: this.#lastChangeId + 1 };
    if (changes.frequencyHz !== undefined) message.frequency_hz = changes.frequencyHz;
    if (changes.mode !== undefined) message.mode = changes.mode;
    if (changes.bandwidthHz !== undefined) message.bandwidth_hz = changes.bandwidthHz;
    if (changes.squelchDb !== undefined) message.squelch_db = changes.squelchDb;
    if (changes.squelchAuto !== undefined) message.squelch_auto = changes.squelchAuto;
    if (!this.#send(message)) return false;
    this.#lastChangeId += 1;
    this.#awaitedId = this.#lastChangeId;
    return true;
  }

  /**
   * Ask the engine to add a bookmark. Every interface then receives the
   * bookmarks, or this one an error. Returns false, sending nothing, when
   * the stream is not open.
   * @param {{frequencyHz: number, name: string, mode: string, scan?: boolean}} bookmark
   * @returns {boolean}
   */
  addBookmark(bookmark) {
    return this.#send({ type: 'bookmark', action: 'add', ...bookmarkFields(bookmark) });
  }

  /**
   * Ask the engine to change some fields of a bookmark, as `addBookmark`.
   * @param {number} id
   * @param {BookmarkFields} changes
   * @returns {boolean}
   */
  changeBookmark(id, changes) {
    return this.#send({ type: 'bookmark', action: 'change', id, ...bookmarkFields(changes) });
  }

  /**
   * Ask the engine to remove a bookmark, as `addBookmark`.
   * @param {number} id
   * @returns {boolean}
   */
  removeBookmark(id) {
    return this.#send({ type: 'bookmark', action: 'remove', id });
  }

  /**
   * Give the engine this interface's preferences, which the next interfaces
   * start with; open interfaces keep theirs. Returns false, sending nothing,
   * when the stream is not open.
   * @param {Preferences} values
   * @returns {boolean}
   */
  savePreferences(values) {
    return this.#send({ type: 'preferences', values });
  }

  /**
   * Whether this interface wants audio: the engine sends audio frames only
   * then, on this connection and the next ones.
   * @param {boolean} on
   */
  setAudio(on) {
    if (on === this.#audio) return;
    this.#audio = on;
    this.#send({ type: 'audio', on });     // when not open, #hello sends it once greeted
  }

  /** Disconnect and stop retrying; the engine idles once no client listens. */
  stop() {
    this.#running = false;
    clearTimeout(this.#timer);
    const socket = this.#socket;
    this.#socket = null;
    if (socket) socket.close(1000);
  }

  /**
   * Send a command, once the engine greeted this interface.
   * @param {Record<string, unknown>} command
   * @returns {boolean} false, sending nothing, when the stream is not open
   */
  #send(command) {
    const socket = this.#socket;
    if (!socket || !this.#greeted || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(command));
    return true;
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
    this.#awaitedId = 0;
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
    } else if (this.#greeted && event.data instanceof ArrayBuffer && event.data.byteLength) {
      const kind = new Uint8Array(event.data, 0, 1)[0];
      if (kind === FRAME_KIND_SPECTRUM) {
        const frame = decodeFrame(event.data);
        if (frame) this.#handlers.onFrame(frame);
      } else if (kind === FRAME_KIND_AUDIO && this.#audio) {
        const frame = decodeAudio(event.data);
        if (frame) this.#handlers.onAudio(frame);
      }
    }
  }

  /**
   * A JSON message: the hello first, then states and errors.
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
    } else if (message.type === 'demod') {
      const demod = decodeDemod(message);
      if (demod && demod.acknowledged >= this.#awaitedId) this.#handlers.onDemod(demod);
    } else if (message.type === 'bookmarks') {
      const bookmarks = decodeBookmarks(message);
      if (bookmarks) this.#handlers.onBookmarks(bookmarks);
    } else if (message.type === 'preferences') {
      const values = decodePreferences(message);
      if (values) this.#handlers.onPreferences(values);
    } else if (message.type === 'storage') {
      const storage = decodeStorage(message);
      if (storage) this.#handlers.onStorage(storage);
    } else if (message.type === 'error') {
      const command = COMMANDS.includes(message.command) ? message.command : null;
      this.#handlers.onError(String(message.message), command);
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
    if (this.#audio) socket.send(JSON.stringify({ type: 'audio', on: true }));
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
