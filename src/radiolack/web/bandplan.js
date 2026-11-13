/*
 * Band plan: what each frequency range is used for, read from a JSON file
 * served next to the interface (format in docs/design/band-plan.md). The
 * file's bands and segments become one sorted list of ranges for the band
 * strip, the readouts and the automatic mode and step, and the bands stay
 * whole beside it, for the band strip when a band's segments are too narrow
 * to read.
 */

export const BAND_PLAN_FORMAT = 1;
const COLOUR = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i;

/**
 * One range of the band strip. A band without segments is one range. A band
 * with segments gives one range per segment, and "gap" ranges for the parts
 * of the band its segments leave out.
 * @typedef {object} PlanRange
 * @property {number} from          lower edge, Hz
 * @property {number} to            upper edge, Hz, excluded
 * @property {string} name          what the range is used for
 * @property {string} band          name of the band the range belongs to
 * @property {'band' | 'segment' | 'gap'} kind
 * @property {string} rgb           colour as "r,g,b", for rgb() and rgba()
 * @property {string | null} mode   demodulator the plan names, if any
 * @property {number | null} step   tuning step in Hz, if the plan gives one
 */

/**
 * One band of the file, whole: its ranges are `count` ranges of
 * BandPlan.ranges from `first`, one when it has no segments.
 * @typedef {object} PlanBand
 * @property {number} from          lower edge, Hz
 * @property {number} to            upper edge, Hz, excluded
 * @property {string} name
 * @property {string | null} short  shorter name for where the name does not fit, if the file gives one
 * @property {string} rgb           colour of its type, as "r,g,b"
 * @property {number} first
 * @property {number} count
 */

/**
 * @typedef {object} BandPlan
 * @property {string} name
 * @property {PlanRange[]} ranges   sorted by frequency, without overlaps
 * @property {PlanBand[]} bands     sorted by frequency, without overlaps
 */

/**
 * A band or a segment as the file gives it, once checked.
 * @typedef {object} Entry
 * @property {number} from
 * @property {number} to
 * @property {string} name
 * @property {string | null} short
 * @property {string} rgb
 * @property {string | null} mode
 * @property {number | null} step
 * @property {unknown[]} segments
 */

/**
 * Fetch and read a band plan file.
 * @param {string} url
 * @param {readonly string[]} modes  demodulator modes the interface knows
 * @returns {Promise<BandPlan>}
 */
export async function loadBandPlan(url, modes) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`band plan ${url}: HTTP ${response.status}`);
  return parseBandPlan(await response.json(), modes);
}

/**
 * Check a parsed band plan file and list its ranges. Malformed data throws:
 * the file ships with Radiolack and its tests check it, so an error there is
 * a bug to show, not data to guess around.
 * @param {unknown} data
 * @param {readonly string[]} modes  demodulator modes the interface knows
 * @returns {BandPlan}
 */
export function parseBandPlan(data, modes) {
  if (!isObject(data) || data.format !== BAND_PLAN_FORMAT) fail(`format ${BAND_PLAN_FORMAT} expected`);
  const name = text(data.name, 'plan name');
  const types = data.types;
  if (!isObject(types)) fail('types must be an object');
  /** @type {Map<string, string>} */
  const colours = new Map();
  for (const [type, colour] of Object.entries(types)) {
    const match = typeof colour === 'string' ? COLOUR.exec(colour) : null;
    if (!match) fail(`type ${type}: a #rrggbb colour is expected`);
    colours.set(type, match.slice(1).map((hex) => parseInt(hex, 16)).join(','));
  }
  if (!Array.isArray(data.bands)) fail('bands must be a list');

  /** @type {PlanRange[]} */
  const ranges = [];
  /** @type {PlanBand[]} */
  const bands = [];
  let edge = -Infinity;
  for (const item of data.bands) {
    const band = readEntry(item, colours, modes);
    if (band.from < edge) fail(`${band.name}: bands overlap or are out of order`);
    edge = band.to;
    const first = ranges.length;
    if (band.segments.length === 0) ranges.push(toRange(band, band.name, 'band'));
    let cursor = band.from;
    for (const part of band.segments) {
      const segment = readEntry(part, colours, modes);
      if (segment.segments.length) fail(`${segment.name}: a segment cannot have segments`);
      if (segment.short !== null) fail(`${segment.name}: only bands have a short name`);
      if (segment.from < cursor || segment.to > band.to) fail(`${band.name}: ${segment.name} is out of order or outside the band`);
      if (segment.from > cursor) ranges.push(toRange({ ...band, from: cursor, to: segment.from }, band.name, 'gap'));
      ranges.push(toRange(segment, band.name, 'segment'));
      cursor = segment.to;
    }
    if (band.segments.length && cursor < band.to) ranges.push(toRange({ ...band, from: cursor, to: band.to }, band.name, 'gap'));
    bands.push({ from: band.from, to: band.to, name: band.name, short: band.short, rgb: band.rgb, first, count: ranges.length - first });
  }
  return { name, ranges, bands };
}

/**
 * The range, or the band, that contains a frequency, or null outside the plan.
 * @template {{ from: number, to: number }} R
 * @param {readonly R[]} ranges  sorted, without overlaps
 * @param {number} f  Hz
 * @returns {R | null}
 */
export function rangeAt(ranges, f) {
  let lo = 0, hi = ranges.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, range = ranges[mid];
    if (f < range.from) hi = mid - 1;
    else if (f >= range.to) lo = mid + 1;
    else return range;
  }
  return null;
}

/**
 * @param {unknown} item
 * @param {Map<string, string>} colours  "r,g,b" by type
 * @param {readonly string[]} modes
 * @returns {Entry}
 */
function readEntry(item, colours, modes) {
  if (!isObject(item)) fail('bands and segments must be objects');
  const name = text(item.name, 'name');
  const short = item.short === undefined ? null : text(item.short, `${name}: the short name`);
  const { from, to } = item;
  if (!isHz(from) || !isHz(to) || from >= to) fail(`${name}: from and to must be whole Hz, from below to`);
  const rgb = colours.get(String(item.type));
  if (rgb === undefined) fail(`${name}: unknown type ${String(item.type)}`);
  /** @type {string | null} */
  let mode = null;
  if (item.mode !== undefined) {
    if (typeof item.mode !== 'string' || !modes.includes(item.mode)) fail(`${name}: unknown mode ${String(item.mode)}`);
    mode = item.mode;
  }
  /** @type {number | null} */
  let step = null;
  if (item.step !== undefined) {
    if (typeof item.step !== 'number' || !Number.isFinite(item.step) || item.step <= 0) fail(`${name}: the step must be a positive number`);
    step = item.step;
  }
  const segments = item.segments ?? [];
  if (!Array.isArray(segments)) fail(`${name}: segments must be a list`);
  return { from, to, name, short, rgb, mode, step, segments };
}

/**
 * @param {Entry} entry
 * @param {string} band
 * @param {PlanRange['kind']} kind
 * @returns {PlanRange}
 */
function toRange(entry, band, kind) {
  return { from: entry.from, to: entry.to, name: entry.name, band, kind, rgb: entry.rgb, mode: entry.mode, step: entry.step };
}

/**
 * @param {unknown} value
 * @param {string} what
 * @returns {string}
 */
function text(value, what) {
  if (typeof value !== 'string' || !value.trim()) fail(`${what} must be a non-empty string`);
  return value;
}

/**
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * @param {unknown} value
 * @returns {value is number}
 */
function isHz(value) {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

/**
 * @param {string} message
 * @returns {never}
 */
function fail(message) {
  throw new Error(`band plan: ${message}`);
}
