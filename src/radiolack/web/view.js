/*
 * The view's limits, whatever the receiver: from 0 Hz to the end of the
 * band plan, or of the receiver's reach when that is higher. Beyond the
 * receiver's reach nothing tunes, and the view says why. Also what the view
 * draws as it widens: the bands in view, the frequency axis's decimals, and
 * a marker where the span is too narrow to see.
 */

import { formatFrequency } from './frequency.js';

/** A span narrower than this, in CSS px, shows as a marker. */
export const SPAN_MARKER_BELOW_PX = 3;
/** The marker's width, CSS px. */
export const SPAN_MARKER_PX = 2;
/** The fewest and most decimals of the frequency axis's labels, in MHz. */
export const AXIS_DECIMALS = /** @type {const} */ ([0, 6]);

/** @typedef {readonly [number, number]} Interval  low and high edge, Hz */

/**
 * Where the view may go: from 0 Hz, or the span's lower edge where it lies
 * below 0 Hz, to the end of the band plan, the receiver's reach or the
 * span, whichever is highest. A plan that is loading or failed to load ends
 * at 0 Hz: the receiver's reach then sets the end.
 * @param {number} planEndHz   upper edge of the plan's last band, 0 without a plan
 * @param {Interval} reach     what the receiver can show
 * @param {Interval} span      what it captures now
 * @returns {[number, number]}
 */
export function viewExtent(planEndHz, reach, span) {
  return [Math.min(0, span[0]), Math.max(planEndHz, reach[1], span[1])];
}

/**
 * The upper edge of a band plan's last band, Hz, or 0 for an empty plan.
 * @param {ReadonlyArray<{ to: number }>} bands  sorted by frequency
 * @returns {number}
 */
export function planEnd(bands) {
  return bands.length ? bands[bands.length - 1].to : 0;
}

/**
 * A view of this centre and width brought within limits: its width from
 * `minWidth` to the limits' width, its centre so that it stays inside them.
 * @param {number} centre    Hz
 * @param {number} width     Hz
 * @param {Interval} limits  where the view may go
 * @param {number} minWidth  the narrowest view, Hz
 * @returns {[number, number]}
 */
export function fitWithin(centre, width, limits, minWidth) {
  const [lo, hi] = limits;
  const w = Math.min(hi - lo, Math.max(minWidth, width));
  const c = Math.min(hi - w / 2, Math.max(lo + w / 2, centre));
  return [c - w / 2, c + w / 2];
}

/**
 * Whether the receiver reaches f.
 * @param {number} f       Hz
 * @param {Interval} reach
 * @returns {boolean}
 */
export function withinReach(f, reach) {
  return f >= reach[0] && f <= reach[1];
}

/**
 * Why a frequency tunes nothing, for the status line: it lies beyond the
 * receiver's range, given from 0 Hz at the lowest.
 * @param {number} f       Hz
 * @param {Interval} reach
 * @returns {string}
 */
export function beyondReachNote(f, reach) {
  const lo = formatFrequency(Math.max(0, reach[0])), hi = formatFrequency(reach[1]);
  return `${formatFrequency(f)} MHz is beyond this receiver's range (${lo} to ${hi} MHz)`;
}

/**
 * The bands that overlap the view, found by bisection: the indices from
 * `first` to `end`, excluded.
 * @param {ReadonlyArray<{ from: number, to: number }>} bands  sorted, without overlaps
 * @param {number} f0  the view's lower edge, Hz
 * @param {number} f1  its upper edge, Hz
 * @returns {[number, number]}
 */
export function bandsInView(bands, f0, f1) {
  const first = firstIndex(bands, (band) => band.to > f0);
  return [first, Math.max(first, firstIndex(bands, (band) => band.from >= f1))];
}

/**
 * The first index whose band passes a test that, along the sorted bands,
 * fails and then passes; the length when none passes.
 * @template T
 * @param {readonly T[]} bands
 * @param {(band: T) => boolean} passes
 * @returns {number}
 */
function firstIndex(bands, passes) {
  let lo = 0, hi = bands.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (passes(bands[mid])) hi = mid; else lo = mid + 1;
  }
  return lo;
}

/**
 * The frequency axis's decimals, in MHz, for labels `stepHz` apart: as many
 * as a multiple of the step needs, from 0 for the widest views to 6, the
 * hertz: 0.25 MHz takes 2.
 * @param {number} stepHz
 * @returns {number}
 */
export function axisDecimals(stepHz) {
  const mhz = stepHz / 1e6;
  for (let d = AXIS_DECIMALS[0]; d < AXIS_DECIMALS[1]; d += 1) {
    const scaled = mhz * 10 ** d, whole = Math.round(scaled);
    if (whole >= 1 && Math.abs(scaled - whole) < 1e-6 * scaled) return d;
  }
  return AXIS_DECIMALS[1];
}

/**
 * The marker that stands for a span too narrow to see, in the same px as
 * its edges, or null for a span wide enough: SPAN_MARKER_PX CSS px around
 * its middle.
 * @param {number} x0     the span's lower edge, px
 * @param {number} x1     its upper edge, px
 * @param {number} scale  px per CSS px
 * @returns {[number, number] | null}
 */
export function spanMarker(x0, x1, scale) {
  if (x1 - x0 >= SPAN_MARKER_BELOW_PX * scale) return null;
  const middle = (x0 + x1) / 2, half = (SPAN_MARKER_PX * scale) / 2;
  return [middle - half, middle + half];
}
