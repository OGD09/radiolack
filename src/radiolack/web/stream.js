/*
 * Engine stream: the WebSocket that brings spectrum frames, audio, the source
 * state, the demodulation settings, the bookmarks, the scanner state, the
 * signals the engine found, the saved preferences, the storage state, the
 * update state and remote access's state from the engine, and the
 * observations when this interface asks, and takes this interface's
 * changes, its preferences, its request for audio, its update requests and
 * its remote access settings to it
 * (docs/design/protocol.md). It reconnects by itself after a disconnection.
 * On a device that remote access serves, the engine sends neither the
 * storage, update nor remote access state, and takes none of their changes;
 * it keeps that device's preferences apart from this computer's.
 */

export const PROTOCOL_VERSION = 6;
const FRAME_KIND_SPECTRUM = 1;
const FRAME_VERSION = 2;
const HEADER_BYTES = 40;
const FLAG_SQUELCH_OPEN = 0x1;
const FRAME_KIND_AUDIO = 2;
const AUDIO_VERSION = 1;
const AUDIO_HEADER_BYTES = 16;
const RETRY_FIRST_MS = 250;
const RETRY_MAX_MS = 4000;
const CLOSE_NOT_PAIRED = 4001;  // a remote device that is not paired, or no longer
const CLOSE_REMOTE_OFF = 4002;  // the computer turned remote access off
const SOURCE_STATUSES = ['connecting', 'streaming', 'waiting', 'idle'];
const SETTINGS = ['address', 'centre_hz', 'sample_rate_hz', 'gain_db', 'ppm', 'dc_removal'];
const COMMANDS = ['source', 'demod', 'audio', 'bookmark', 'scanner', 'observations', 'preferences', 'update', 'remote'];
const UPDATE_STATUSES = ['idle', 'checking', 'current', 'available', 'installed', 'failed', 'unavailable'];

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
 * The engine's scanners, shared by every interface: signal detection, the
 * memory scan and the sweep.
 * @typedef {object} ScannerState
 * @property {boolean} detection    the engine looks for signals in its spectra
 * @property {number} thresholdDb   how far above the local noise a signal rises
 * @property {number[]} ignoredHz   frequencies whose signals are not listed, lowest first
 * @property {number} resumeS       how long the memory scan waits, once the squelch closed, before it watches again
 * @property {string | null} scan   the scan that runs: "memory", "sweep", or null
 * @property {string | null} stopped
 *   why the latest scan stopped: "tuned", "source", "bookmarks" or "range"; null when an interface stopped it, or none ran
 * @property {string | null} stoppedScan  the scan that `stopped` is about: "memory" or "sweep", or null
 * @property {MemoryScanState} memory
 * @property {SweepState | null} sweep  null with an engine older than the sweep
 */

/**
 * The memory scan's state.
 * @typedef {object} MemoryScanState
 * @property {number[]} watched     the ticked bookmarks it watches, those within the receiver's reach, by id
 * @property {number | null} windows  how many windows of the receiver they take; null with an older engine
 * @property {number | null} window   the window the receiver is asked for, from 0; null while it does not run
 * @property {string | null} state  "watching", "checking", "listening" or "waiting"; null while it does not run
 * @property {number | null} bookmarkId  the bookmark tuned to, null while watching
 */

/**
 * The sweep's settings and progress.
 * @typedef {object} SweepState
 * @property {number} lowHz        the range it sweeps, from this frequency
 * @property {number} highHz       to this one
 * @property {number} windowS      the time per window
 * @property {number | null} windows  how many windows the range takes; null while it does not run
 * @property {number | null} window   the window the receiver is asked for, from 0
 * @property {number | null} passes   the passes done
 */

/**
 * Scanner settings to change; the others stay as they are.
 * @typedef {object} ScannerChanges
 * @property {boolean} [detection]
 * @property {number} [thresholdDb]
 * @property {number} [resumeS]
 * @property {number} [sweepLowHz]
 * @property {number} [sweepHighHz]
 * @property {number} [sweepWindowS]
 */

/**
 * A signal the engine found while it runs.
 * @typedef {object} Peak
 * @property {number} frequencyHz   its centre of power
 * @property {number} widthHz
 * @property {number} marginDb      its highest bin above the local noise, when last found
 * @property {boolean} active       found within the last 1.5 s
 * @property {number} ageS          seconds since it was last found, when the engine sent the list
 */

/**
 * A channel where the engine found signals, which it keeps across restarts.
 * Times are seconds since 1970, UTC.
 * @typedef {object} Observation
 * @property {number} frequencyHz
 * @property {number} firstS          when first found
 * @property {number} lastS           when last found
 * @property {number} transmissions
 * @property {number} activeS         how long it was active, its transmissions together
 * @property {number} marginDb        its highest margin above the local noise
 * @property {number[]} hours
 *   how many of its transmissions began in each hour of the day, from 0 h, on the engine's computer
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

/**
 * What the engine's update check found.
 * @typedef {'idle' | 'checking' | 'current' | 'available' | 'installed' | 'failed' | 'unavailable'} UpdateStatus
 */

/**
 * @typedef {object} UpdateState
 * @property {UpdateStatus} status
 * @property {string} version         the Radiolack that runs
 * @property {string | null} latest   the newer version: in the repository (available) or installed (installed)
 * @property {string} detail          why the check failed or is unavailable, or ""
 * @property {boolean} checkAtStart   the engine checks when it starts
 */

/**
 * A device paired with remote access.
 * @typedef {object} RemoteDevice
 * @property {string} id            names it in forget requests
 * @property {string} label         such as "iPhone · Safari"
 * @property {number} pairedS       when it was paired, in Unix time
 * @property {number} seenS         when it was last used, in Unix time, to the hour
 * @property {boolean} connected    it has a stream open
 */

/**
 * Remote access: other devices on the local network reach Radiolack over
 * HTTPS with an access code. This computer's interfaces only.
 * @typedef {object} RemoteState
 * @property {boolean} on
 * @property {number} port              where it listens, or will
 * @property {string[]} urls            what a device opens, by name then by address; empty while off
 * @property {string | null} code       the access code, while on
 * @property {boolean} blocked          too many wrong codes: the code works again once renewed
 * @property {RemoteDevice[]} devices   paired, in the order they were
 * @property {string | null} fingerprint  SHA-256 of the certificate devices install; null before the first turn-on
 * @property {string} problem           why it could not turn on, or ""
 */

/** @typedef {'source' | 'demod' | 'audio' | 'bookmark' | 'scanner' | 'observations' | 'preferences' | 'update' | 'remote'} Command */

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
 * @property {string[]} kinds           kinds of source the engine may switch to; empty: it reads one only
 */

/**
 * Changes to ask of the engine; the others stay as they are.
 * @typedef {object} SourceChanges
 * @property {string} [kind]            another kind of source, among SourceState.kinds, asked alone
 * @property {string} [address]         of an rtl_tcp server: HOST, PORT or HOST:PORT
 * @property {number} [centreHz]
 * @property {number} [rateHz]
 * @property {number | null} [gainDb]   null: automatic gain
 * @property {number} [ppm]             a whole number
 * @property {boolean} [dcRemoval]
 */

/**
 * "connecting" before the first answer, "open" once the engine greeted the
 * interface, "waiting" while it retries after a disconnection,
 * "remote-off" while it retries after the computer turned remote access
 * off, "unpaired" once the computer no longer knows this device, which must
 * pair again (the stream then stops), and "incompatible" when the engine
 * speaks another protocol version.
 * @typedef {'connecting' | 'open' | 'waiting' | 'remote-off' | 'unpaired' | 'incompatible'} StreamState
 */

/**
 * @typedef {object} StreamHandlers
 * @property {(frame: SpectrumFrame) => void} onFrame
 * @property {(frame: AudioFrame) => void} onAudio     while this interface asks for audio
 * @property {(source: SourceState) => void} onSource  sent first, then at every change
 * @property {(demod: DemodState) => void} onDemod     sent next, then at every change this interface has not overtaken
 * @property {(bookmarks: Bookmark[]) => void} onBookmarks  sent next, then at every change, in frequency order
 * @property {(scanner: ScannerState) => void} onScanner    sent next, then at every change
 * @property {(peaks: Peak[]) => void} onPeaks
 *   sent next, then when the list changes, at most about once a second: active signals first, strongest first
 * @property {(observations: Observation[]) => void} onObservations
 *   sent when this interface asks for them, or cleared some: the latest found first
 * @property {(values: Preferences) => void} onPreferences
 *   sent next, once per connection: the latest saved for this computer's interfaces, or for this device
 * @property {(storage: StorageState) => void} onStorage    sent next, then at every change
 * @property {(update: UpdateState) => void} onUpdate       sent next, then at every change
 * @property {(remote: RemoteState) => void} onRemote       sent next, then at every change
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
    kinds: Array.isArray(m.kinds) ? m.kinds.filter((/** @type {unknown} */ kind) => typeof kind === 'string') : [],
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
 * Decode a scanner state message. Returns null when a field is missing or
 * malformed, apart from the sweep's, which an engine older than the sweep
 * does not send: without them, or with one malformed, the sweep is null;
 * and the memory scan's windows, which an engine older than them does not
 * send: missing or malformed, they are null.
 * @param {any} message  a parsed JSON message of type "scanner"
 * @returns {ScannerState | null}
 */
export function decodeScanner(message) {
  const m = message;
  const memory = m.memory;
  const valid = typeof m.detection === 'boolean' && typeof m.threshold_db === 'number'
    && Number.isFinite(m.threshold_db) && Array.isArray(m.ignored_hz)
    && m.ignored_hz.every((/** @type {unknown} */ f) => typeof f === 'number' && Number.isFinite(f))
    && typeof m.resume_s === 'number' && Number.isFinite(m.resume_s)
    && (m.scan === null || typeof m.scan === 'string') && (m.stopped === null || typeof m.stopped === 'string')
    && memory !== null && typeof memory === 'object' && Array.isArray(memory.watched)
    && memory.watched.every((/** @type {unknown} */ id) => Number.isSafeInteger(id))
    && (memory.state === null || typeof memory.state === 'string')
    && (memory.bookmark_id === null || Number.isSafeInteger(memory.bookmark_id));
  if (!valid) return null;
  return {
    detection: m.detection,
    thresholdDb: m.threshold_db,
    ignoredHz: m.ignored_hz.slice(),
    resumeS: m.resume_s,
    scan: m.scan,
    stopped: m.stopped,
    stoppedScan: typeof m.stopped_scan === 'string' ? m.stopped_scan : null,
    memory: {
      watched: memory.watched.slice(),
      windows: countOrNull(memory.windows),
      window: countOrNull(memory.window),
      state: memory.state,
      bookmarkId: memory.bookmark_id,
    },
    sweep: decodeSweep(m),
  };
}

/**
 * A count in a message: a whole number from 0, or null when it is missing or
 * malformed.
 * @param {unknown} n
 * @returns {number | null}
 */
function countOrNull(n) {
  return typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 ? n : null;
}

/**
 * The sweep's settings and progress in a scanner state message, or null when
 * one is missing or malformed.
 * @param {any} m  a parsed JSON message of type "scanner"
 * @returns {SweepState | null}
 */
function decodeSweep(m) {
  const progress = m.sweep;
  if (progress === null || typeof progress !== 'object') return null;
  const counts = [progress.windows, progress.window, progress.passes];
  const valid = [m.sweep_low_hz, m.sweep_high_hz, m.sweep_window_s].every(
    (/** @type {unknown} */ v) => typeof v === 'number' && Number.isFinite(v) && v > 0)
    && (counts.every((n) => n === null) || counts.every((n) => Number.isSafeInteger(n) && n >= 0));
  if (!valid) return null;
  return {
    lowHz: m.sweep_low_hz,
    highHz: m.sweep_high_hz,
    windowS: m.sweep_window_s,
    windows: progress.windows,
    window: progress.window,
    passes: progress.passes,
  };
}

/**
 * Decode a peaks message. Returns null when an entry is missing a field or
 * holds a malformed one.
 * @param {any} message  a parsed JSON message of type "peaks"
 * @returns {Peak[] | null}
 */
export function decodePeaks(message) {
  if (!Array.isArray(message.peaks)) return null;
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @type {Peak[]} */
  const peaks = [];
  for (const p of message.peaks) {
    const valid = p !== null && typeof p === 'object' && finite(p.frequency_hz) && finite(p.width_hz)
      && finite(p.margin_db) && typeof p.active === 'boolean' && finite(p.age_s);
    if (!valid) return null;
    peaks.push({ frequencyHz: p.frequency_hz, widthHz: p.width_hz, marginDb: p.margin_db, active: p.active, ageS: p.age_s });
  }
  return peaks;
}

/**
 * Decode an observations message. Returns null when it holds no list, or
 * holds a malformed channel.
 * @param {any} message  a parsed JSON message of type "observations"
 * @returns {Observation[] | null}
 */
export function decodeObservations(message) {
  if (!Array.isArray(message.channels)) return null;
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @type {Observation[]} */
  const channels = [];
  for (const c of message.channels) {
    const valid = c !== null && typeof c === 'object' && finite(c.frequency_hz) && finite(c.first_s)
      && finite(c.last_s) && Number.isSafeInteger(c.transmissions) && finite(c.active_s) && finite(c.margin_db)
      && Array.isArray(c.hours) && c.hours.length === 24
      && c.hours.every((/** @type {unknown} */ n) => Number.isSafeInteger(n));
    if (!valid) return null;
    channels.push({ frequencyHz: c.frequency_hz, firstS: c.first_s, lastS: c.last_s, transmissions: c.transmissions,
      activeS: c.active_s, marginDb: c.margin_db, hours: c.hours.slice() });
  }
  return channels;
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
 * Decode an update message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "update"
 * @returns {UpdateState | null}
 */
export function decodeUpdate(message) {
  const m = message;
  const valid = UPDATE_STATUSES.includes(m.status) && typeof m.version === 'string'
    && (m.latest === null || typeof m.latest === 'string') && typeof m.detail === 'string'
    && typeof m.check_at_start === 'boolean';
  return valid
    ? { status: m.status, version: m.version, latest: m.latest, detail: m.detail, checkAtStart: m.check_at_start }
    : null;
}

/**
 * Decode a remote access message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "remote"
 * @returns {RemoteState | null}
 */
export function decodeRemote(message) {
  /** @param {unknown} value */
  const textOrNull = (value) => value === null || typeof value === 'string';
  /** @param {any} d */
  const device = (d) => d !== null && typeof d === 'object' && typeof d.id === 'string'
    && typeof d.label === 'string' && Number.isSafeInteger(d.paired) && Number.isSafeInteger(d.seen)
    && typeof d.connected === 'boolean';
  const m = message;
  const valid = typeof m.on === 'boolean' && Number.isSafeInteger(m.port) && Array.isArray(m.urls)
    && m.urls.every((/** @type {unknown} */ url) => typeof url === 'string') && textOrNull(m.code)
    && typeof m.blocked === 'boolean' && Array.isArray(m.devices) && m.devices.every(device)
    && textOrNull(m.fingerprint) && typeof m.problem === 'string';
  if (!valid) return null;
  return {
    on: m.on,
    port: m.port,
    urls: m.urls.slice(),
    code: m.code,
    blocked: m.blocked,
    devices: m.devices.map((/** @type {any} */ d) => (
      { id: d.id, label: d.label, pairedS: d.paired, seenS: d.seen, connected: d.connected })),
    fingerprint: m.fingerprint,
    problem: m.problem,
  };
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
  #remote = false;        // the engine serves this interface through remote access
  /** @type {string[]} */
  #scanners = [];         // the scanners the engine runs, as its last hello listed them
  #remoteOff = false;     // the computer turned remote access off: retrying until it is on again
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

  /**
   * Whether the engine serves this interface through remote access, as its
   * last hello said: it then sends neither the storage, update nor remote
   * access state, and refuses their changes.
   */
  get remote() {
    return this.#remote;
  }

  /**
   * The scanners the engine runs, as its last hello listed them, such as
   * "peaks" and "memory": an older engine runs fewer, or none.
   * @returns {string[]}
   */
  get scanners() {
    return this.#scanners.slice();
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
    if (changes.kind !== undefined) message.kind = changes.kind;
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
   * Change the engine's scanner settings: every interface then receives the
   * scanner state, or this one an error. Returns false, sending nothing,
   * when the stream is not open.
   * @param {ScannerChanges} changes
   * @returns {boolean}
   */
  setScanner(changes) {
    /** @type {Record<string, boolean | number>} */
    const message = {};
    if (changes.detection !== undefined) message.detection = changes.detection;
    if (changes.thresholdDb !== undefined) message.threshold_db = changes.thresholdDb;
    if (changes.resumeS !== undefined) message.resume_s = changes.resumeS;
    if (changes.sweepLowHz !== undefined) message.sweep_low_hz = changes.sweepLowHz;
    if (changes.sweepHighHz !== undefined) message.sweep_high_hz = changes.sweepHighHz;
    if (changes.sweepWindowS !== undefined) message.sweep_window_s = changes.sweepWindowS;
    return this.#send({ type: 'scanner', ...message });
  }

  /**
   * Start a scan, the memory scan ("memory") or the sweep ("sweep"), or stop
   * the scan that runs (null): every interface then receives the scanner
   * state, or this one an error, such as when no ticked bookmark lies in the
   * receiver's span, or when the receiver does not tune. Returns false,
   * sending nothing, when the stream is not open.
   * @param {'memory' | 'sweep' | null} scan
   * @returns {boolean}
   */
  setScan(scan) {
    return this.#send({ type: 'scanner', scan });
  }

  /**
   * Ask the engine to ignore a frequency: its signals leave the list, for
   * every interface, and stay out of it. Returns false, sending nothing,
   * when the stream is not open.
   * @param {number} frequencyHz
   * @returns {boolean}
   */
  ignoreFrequency(frequencyHz) {
    return this.#send({ type: 'scanner', ignore: frequencyHz });
  }

  /**
   * Ask the engine to ignore no frequency any more, as `ignoreFrequency`.
   * @returns {boolean}
   */
  restoreIgnored() {
    return this.#send({ type: 'scanner', restore: true });
  }

  /**
   * Ask the engine for the channels where it found signals, which come to
   * this interface only (`onObservations`). Returns false, sending nothing,
   * when the stream is not open.
   * @returns {boolean}
   */
  askObservations() {
    return this.#send({ type: 'observations' });
  }

  /**
   * Ask the engine to forget the channel within 1.5 kHz of a frequency, for
   * every interface; this one then receives the channels left. Returns
   * false, sending nothing, when the stream is not open.
   * @param {number} frequencyHz
   * @returns {boolean}
   */
  clearObservation(frequencyHz) {
    return this.#send({ type: 'observations', clear: frequencyHz });
  }

  /**
   * Ask the engine to forget every channel, as `clearObservation`.
   * @returns {boolean}
   */
  clearObservations() {
    return this.#send({ type: 'observations', clear_all: true });
  }

  /**
   * Give the engine this interface's preferences, which the next interfaces
   * of this computer, or of this device through remote access, start with;
   * open interfaces keep theirs. Returns false, sending nothing, when the
   * stream is not open.
   * @param {Preferences} values
   * @returns {boolean}
   */
  savePreferences(values) {
    return this.#send({ type: 'preferences', values });
  }

  /**
   * Ask the engine to check for a newer Radiolack now, unless a check is
   * running: every interface receives the update state as it changes.
   * Returns false, sending nothing, when the stream is not open.
   * @returns {boolean}
   */
  checkUpdates() {
    return this.#send({ type: 'update', check: true });
  }

  /**
   * Turn the engine's update check at start on or off, for the next starts.
   * Returns false, sending nothing, when the stream is not open.
   * @param {boolean} on
   * @returns {boolean}
   */
  setCheckAtStart(on) {
    return this.#send({ type: 'update', check_at_start: on });
  }

  /**
   * Turn remote access on or off: this computer's interfaces receive its
   * state as it changes, which says why it could not turn on. Returns false,
   * sending nothing, when the stream is not open.
   * @param {boolean} on
   * @returns {boolean}
   */
  setRemoteAccess(on) {
    return this.#send({ type: 'remote', on });
  }

  /**
   * Change the port remote access listens on, while it is off. Returns
   * false, sending nothing, when the stream is not open.
   * @param {number} port
   * @returns {boolean}
   */
  setRemotePort(port) {
    return this.#send({ type: 'remote', port });
  }

  /**
   * Make a new access code: every paired device is forgotten and must enter
   * it. Returns false, sending nothing, when the stream is not open.
   * @returns {boolean}
   */
  renewRemoteCode() {
    return this.#send({ type: 'remote', new_code: true });
  }

  /**
   * Forget a paired device, which must enter the code again. Returns false,
   * sending nothing, when the stream is not open.
   * @param {string} id
   * @returns {boolean}
   */
  forgetRemoteDevice(id) {
    return this.#send({ type: 'remote', forget: id });
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
    socket.addEventListener('close', (event) => this.#closed(socket, event.code));
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
    } else if (message.type === 'scanner') {
      const scanner = decodeScanner(message);
      if (scanner) this.#handlers.onScanner(scanner);
    } else if (message.type === 'peaks') {
      const peaks = decodePeaks(message);
      if (peaks) this.#handlers.onPeaks(peaks);
    } else if (message.type === 'observations') {
      const observations = decodeObservations(message);
      if (observations) this.#handlers.onObservations(observations);
    } else if (message.type === 'preferences') {
      const values = decodePreferences(message);
      if (values) this.#handlers.onPreferences(values);
    } else if (message.type === 'storage') {
      const storage = decodeStorage(message);
      if (storage) this.#handlers.onStorage(storage);
    } else if (message.type === 'update') {
      const update = decodeUpdate(message);
      if (update) this.#handlers.onUpdate(update);
    } else if (message.type === 'remote') {
      const remote = decodeRemote(message);
      if (remote) this.#handlers.onRemote(remote);
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
    this.#remote = message.remote === true;
    this.#scanners = Array.isArray(message.scanners)
      ? message.scanners.filter((/** @type {unknown} */ name) => typeof name === 'string') : [];
    this.#remoteOff = false;
    this.#retryMs = RETRY_FIRST_MS;
    if (this.#audio) socket.send(JSON.stringify({ type: 'audio', on: true }));
    this.#handlers.onState('open');
  }

  /**
   * @param {WebSocket} socket
   * @param {number} code  the close code
   */
  #closed(socket, code) {
    if (socket !== this.#socket) return;
    this.#socket = null;
    if (!this.#running) return;
    if (this.#incompatible) {
      this.#running = false;
      this.#handlers.onState('incompatible');
      return;
    }
    if (code === CLOSE_NOT_PAIRED) {
      // Reconnecting would meet the same refusal: the page must pair this device again.
      this.#running = false;
      this.#handlers.onState('unpaired');
      return;
    }
    if (code === CLOSE_REMOTE_OFF) this.#remoteOff = true;
    this.#retry();
  }

  #retry() {
    this.#handlers.onState(this.#remoteOff ? 'remote-off' : 'waiting');
    const delay = this.#retryMs;
    this.#retryMs = Math.min(RETRY_MAX_MS, delay * 2);
    this.#timer = window.setTimeout(() => { if (this.#running) this.#connect(); }, delay);
  }
}
