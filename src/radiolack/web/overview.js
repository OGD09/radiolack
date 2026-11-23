/*
 * The overview bar (later-milestones.md, section 7): a thin bar above the
 * band strip that shows the whole band plan, from 1 kHz to its end, on a
 * logarithmic scale, with the receiver's reach, the span, the view and the
 * VFO. The spectrum and the waterfall stay linear. Here, without the DOM:
 * where a frequency lies on the bar and the other way round, the band under
 * a point, the bar's tooltip and what it draws.
 */

import { rangeAt } from './bandplan.js';
import { formatFrequency } from './frequency.js';

/** The bar's lower end, Hz: nothing below shows on it. */
export const OVERVIEW_LOW_HZ = 1000;
/** The bar's height with a mouse and on a touch screen, CSS px. */
export const OVERVIEW_HEIGHT_PX = /** @type {const} */ ({ mouse: 10, touch: 14 });
/** The narrowest span and view drawn, CSS px: narrower ones widen around their middle. */
export const OVERVIEW_SPAN_MIN_PX = 2;
export const OVERVIEW_VIEW_MIN_PX = 5;

/** @typedef {readonly [number, number]} Interval  low and high edge, Hz */

/**
 * The bar's logarithmic scale: the decimal logarithms of its ends, from
 * 1 kHz to the plan's end, a decade at least.
 * @typedef {object} OverviewScale
 * @property {number} low   log10 of the lower end
 * @property {number} high  log10 of the upper end
 */

/**
 * The scale of a bar that ends at the plan's end.
 * @param {number} endHz  upper edge of the plan's last band
 * @returns {OverviewScale}
 */
export function overviewScale(endHz) {
  const low = Math.log10(OVERVIEW_LOW_HZ);
  const high = Number.isFinite(endHz) && endHz > OVERVIEW_LOW_HZ * 10 ? Math.log10(endHz) : low + 1;
  return { low, high };
}

/**
 * The bar's height, CSS px.
 * @param {boolean} coarse  whether the primary pointer is a finger
 * @returns {number}
 */
export function overviewHeight(coarse) {
  return coarse ? OVERVIEW_HEIGHT_PX.touch : OVERVIEW_HEIGHT_PX.mouse;
}

/**
 * Where a frequency lies along the bar, from 0 at its lower end to 1 at its
 * upper end; below 1 kHz, 0 Hz included, at 0, and beyond its end at 1.
 * @param {number} f  Hz
 * @param {OverviewScale} scale
 * @returns {number}
 */
export function positionOf(f, scale) {
  if (!(f > OVERVIEW_LOW_HZ)) return 0;     // NaN too
  return Math.min(1, (Math.log10(f) - scale.low) / (scale.high - scale.low));
}

/**
 * The frequency at a share of the bar, which is kept within it.
 * @param {number} share  0 at its lower end, 1 at its upper end
 * @param {OverviewScale} scale
 * @returns {number} Hz
 */
export function frequencyAt(share, scale) {
  const s = Number.isFinite(share) ? Math.min(1, Math.max(0, share)) : 0;
  return 10 ** (scale.low + s * (scale.high - scale.low));
}

/**
 * The band under a share of the bar, or null where the plan has none.
 * @template {{ from: number, to: number }} B
 * @param {readonly B[]} bands  sorted, without overlaps
 * @param {number} share
 * @param {OverviewScale} scale
 * @returns {B | null}
 */
export function bandUnder(bands, share, scale) {
  return rangeAt(bands, frequencyAt(share, scale));
}

/**
 * The bar's tooltip over a band: its name and its edges, in MHz.
 * @param {{ from: number, to: number, name: string }} band
 * @returns {string}
 */
export function overviewTitle(band) {
  return `${band.name} · ${formatFrequency(band.from)}–${formatFrequency(band.to)} MHz`;
}

/**
 * The decades within the bar, Hz: 10 kHz, 100 kHz and on, each one that
 * lies inside the bar, its ends excluded.
 * @param {OverviewScale} scale
 * @returns {number[]}
 */
export function decades(scale) {
  const found = [];
  for (let d = Math.floor(scale.low) + 1; d < scale.high; d += 1) found.push(10 ** d);
  return found;
}

/**
 * A piece of a band on the bar: within the receiver's reach or not.
 * @typedef {object} OverviewPiece
 * @property {number} x0     px
 * @property {number} x1     px
 * @property {string} rgb    the band's colour, as "r,g,b"
 * @property {boolean} reach  within the receiver's reach, drawn brighter
 */

/**
 * What the bar draws, in its px.
 * @typedef {object} OverviewDrawing
 * @property {OverviewPiece[]} pieces  the bands, cut at the reach's edges, in frequency order
 * @property {[number, number]} reach  the receiver's reach
 * @property {[number, number]} span   what it captures now, OVERVIEW_SPAN_MIN_PX CSS px wide at least
 * @property {[number, number]} view   what the view shows, drawn as a bracket, OVERVIEW_VIEW_MIN_PX wide at least
 * @property {number} vfo              the VFO, a line
 * @property {number[]} ticks          the decades
 */

/**
 * What the bar draws: its bands, brighter within the receiver's reach, the
 * span, the view and the VFO, and a tick at each decade.
 * @param {object} state
 * @param {ReadonlyArray<{ from: number, to: number, rgb: string }>} state.bands  sorted, without overlaps
 * @param {OverviewScale} state.scale
 * @param {number} state.width     the bar's width, px
 * @param {number} state.pxPerCss  px per CSS px
 * @param {Interval} state.reach   what the receiver can show
 * @param {Interval} state.span    what it captures now
 * @param {Interval} state.view    what the view shows
 * @param {number} state.vfo       Hz
 * @returns {OverviewDrawing}
 */
export function overviewDrawing({ bands, scale, width, pxPerCss, reach, span, view, vfo }) {
  const x = (/** @type {number} */ f) => positionOf(f, scale) * width;
  const reachX = /** @type {[number, number]} */ ([x(reach[0]), x(reach[1])]);
  /** @type {OverviewPiece[]} */
  const pieces = [];
  for (const band of bands) {
    const a = x(band.from), b = x(band.to);
    if (b <= a) continue;                   // below 1 kHz or beyond the bar's end
    // Cut at the reach's edges: the part within it is drawn brighter.
    const cuts = [a, Math.min(b, Math.max(a, reachX[0])), Math.min(b, Math.max(a, reachX[1])), b];
    for (let i = 0; i < 3; i += 1) {
      if (cuts[i + 1] > cuts[i]) pieces.push({ x0: cuts[i], x1: cuts[i + 1], rgb: band.rgb, reach: i === 1 });
    }
  }
  return {
    pieces,
    reach: reachX,
    span: widened(x(span[0]), x(span[1]), OVERVIEW_SPAN_MIN_PX * pxPerCss),
    view: widened(x(view[0]), x(view[1]), OVERVIEW_VIEW_MIN_PX * pxPerCss),
    vfo: x(vfo),
    ticks: decades(scale).map(x),
  };
}

/**
 * An interval, widened around its middle to `least` px when narrower.
 * @param {number} x0
 * @param {number} x1
 * @param {number} least
 * @returns {[number, number]}
 */
function widened(x0, x1, least) {
  if (x1 - x0 >= least) return [x0, x1];
  const middle = (x0 + x1) / 2;
  return [middle - least / 2, middle + least / 2];
}
