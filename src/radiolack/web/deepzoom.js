/*
 * The deep zoom (docs/design/later-milestones.md, section 2): below the wide
 * spectrum's limit, the spectrum and the waterfall pass to the fine
 * spectrum of the VFO's channel, in the same place. This module decides
 * when the interface asks the engine for the fine frames, which spectrum
 * shows, where the fine spectrum shows the air (its cover), how narrow the
 * view may go, and where a frequency lies among either spectrum's bins.
 */

import { FINE_COVER } from './stream.js';
import { WFM_CHANNEL_RATE_HZ } from './rates.js';
import { fitWithin } from './view.js';

/** The channel's rate in every mode but WFM, Hz (demod.py): the fine frame's width. */
export const CHANNEL_RATE_HZ = 48000;
/** Where the channel's 0 Hz lies below the VFO in CW, Hz (demod.py): the fine frame's centre. */
export const CW_PITCH_HZ = 700;
/** The fine frames' bins: this computer's interfaces get 8192, a paired device 2048. */
export const FINE_BINS = 8192;
export const REMOTE_FINE_BINS = 2048;
/** A view narrower than this many bins of the wide spectrum, within the cover, asks for the fine frames. */
export const DEEP_BELOW_BINS = 48;
/** A view at least this many wide bins wide goes back to the wide spectrum: the hysteresis, one key's zoom step. */
export const DEEP_LEAVE_BINS = 72;
/** The narrowest view, in bins of the spectrum it shows. */
export const NARROWEST_BINS = 16;
/** The wide spectrum's narrowest view, Hz, whatever its bins. */
export const WIDE_NARROWEST_HZ = 4000;
/** The fine spectrum keeps showing while its cover takes at least this share of the view. */
export const KEPT_SHARE = 0.5;

/** @typedef {readonly [number, number]} Interval  low and high edge, Hz */

/**
 * The fine frame that the engine sends for these settings: its centre, the
 * channel's 0 Hz on the air (the VFO, or in CW the VFO less the CW pitch),
 * and its width, the channel's rate. The engine scales both by the
 * receiver's `rate_scale`, which the interface does not know: within a few
 * hertz of these, which the frames themselves then give exactly.
 * @param {number} vfoHz
 * @param {string} mode
 * @returns {{ centreHz: number, widthHz: number }}
 */
export function fineFrameFor(vfoHz, mode) {
  return {
    centreHz: mode === 'CW' ? vfoHz - CW_PITCH_HZ : vfoHz,
    widthHz: mode === 'WFM' ? WFM_CHANNEL_RATE_HZ : CHANNEL_RATE_HZ,
  };
}

/**
 * The fine spectrum's cover: where its frame shows the air, FINE_COVER of
 * its width on each side of its centre. The bins beyond show the front
 * filter's stopband.
 * @param {number} centreHz
 * @param {number} widthHz
 * @returns {[number, number]}
 */
export function fineCover(centreHz, widthHz) {
  return [centreHz - FINE_COVER * widthHz, centreHz + FINE_COVER * widthHz];
}

/**
 * The bins of a fine frame that show the air, within its cover: the first
 * and the end, excluded.
 * @param {number} bins
 * @returns {[number, number]}
 */
export function coverBins(bins) {
  return [Math.ceil(bins / 2 - FINE_COVER * bins), Math.floor(bins / 2 + FINE_COVER * bins) + 1];
}

/**
 * Whether the engine would send fine frames now: its hello listed them,
 * and the VFO's channel receives, the VFO within the span and the receiver
 * as fast as the channel (WFM's 320 kHz, 48 kHz otherwise).
 * @param {object} state
 * @param {boolean} state.known   the engine's hello lists "fine" among its spectra
 * @param {number} state.vfoHz
 * @param {string} state.mode
 * @param {Interval} state.span   what the receiver captures
 * @returns {boolean}
 */
export function fineReceived({ known, vfoHz, mode, span }) {
  return known && span[1] - span[0] >= fineFrameFor(vfoHz, mode).widthHz && vfoHz >= span[0] && vfoHz <= span[1];
}

/**
 * The share of the view that an interval covers, 0 to 1.
 * @param {Interval} view
 * @param {Interval} cover
 * @returns {number}
 */
export function coveredShare(view, cover) {
  const width = view[1] - view[0];
  if (!(width > 0)) return 0;
  return Math.max(0, Math.min(view[1], cover[1]) - Math.max(view[0], cover[0])) / width;
}

/**
 * Whether the view shows the fine spectrum, from whether it did: it starts
 * once the view is narrower than DEEP_BELOW_BINS wide bins and lies within
 * the cover, and stops once the view is DEEP_LEAVE_BINS wide bins wide or
 * the cover takes less than KEPT_SHARE of it. Between the two, the view
 * stays with the spectrum it shows, so that it never flips back and forth
 * at one width or one edge.
 * @param {boolean} was        the view showed the fine spectrum
 * @param {Interval} view
 * @param {Interval} cover     the fine spectrum's
 * @param {number} wideBinHz   the width of the wide spectrum's bins
 * @returns {boolean}
 */
export function deepZoomed(was, view, cover, wideBinHz) {
  const width = view[1] - view[0], share = coveredShare(view, cover);
  if (was) return width < DEEP_LEAVE_BINS * wideBinHz && share >= KEPT_SHARE;
  return width < DEEP_BELOW_BINS * wideBinHz && share >= 1;
}

/**
 * The narrowest view of the wide spectrum, Hz: NARROWEST_BINS of its bins,
 * and WIDE_NARROWEST_HZ at the least.
 * @param {number} wideBinHz
 * @returns {number}
 */
export function wideNarrowest(wideBinHz) {
  return Math.max(WIDE_NARROWEST_HZ, NARROWEST_BINS * wideBinHz);
}

/**
 * The narrowest view of the fine spectrum, Hz: NARROWEST_BINS of its bins,
 * 94 Hz at 48 kHz in 8192 bins.
 * @param {number} widthHz  the fine frame's
 * @param {number} bins
 * @returns {number}
 */
export function fineNarrowest(widthHz, bins) {
  return (NARROWEST_BINS * widthHz) / bins;
}

/**
 * Where a view of this centre and width goes. Without the fine spectrum,
 * within `limits` and no narrower than `wideMin`, as before the deep zoom.
 * With it, a view narrower than `wideMin` lies within the cover: zooming in
 * from a wider view elsewhere stops at `wideMin`, and a narrow view moved
 * past the cover's edge stops there, so that the view never jumps to the
 * cover nor widens by itself.
 * @param {number} centre        Hz
 * @param {number} width         Hz
 * @param {Interval} limits      where the view may go
 * @param {number} wideMin       the wide spectrum's narrowest view, Hz
 * @param {{ cover: Interval, narrowestHz: number } | null} deep
 *   the fine spectrum's cover and narrowest view, or null while the engine would send none
 * @param {number} fromWidth     the width of the view before, Hz
 * @returns {[number, number]}
 */
export function fitDeepView(centre, width, limits, wideMin, deep, fromWidth) {
  if (!deep) return fitWithin(centre, width, limits, wideMin);
  const [a, b] = fitWithin(centre, width, limits, Math.min(wideMin, deep.narrowestHz));
  if (b - a >= wideMin) return [a, b];
  const inside = /** @type {const} */ ([Math.max(limits[0], deep.cover[0]), Math.min(limits[1], deep.cover[1])]);
  if (a >= inside[0] && b <= inside[1]) return [a, b];
  if (fromWidth >= wideMin || inside[1] - inside[0] < b - a) return fitWithin(centre, wideMin, limits, wideMin);
  return fitWithin((a + b) / 2, b - a, inside, deep.narrowestHz);
}

/**
 * Where f lies among a frame's bins, bin i at i: with `bins` bins over
 * `widthHz` around `centreHz`, bin i is centred on
 * `centreHz - widthHz / 2 + i * widthHz / bins` (docs/design/protocol.md).
 * @param {number} f
 * @param {number} centreHz
 * @param {number} widthHz
 * @param {number} bins
 * @returns {number}
 */
export function binPosition(f, centreHz, widthHz, bins) {
  return ((f - centreHz) / widthHz) * bins + bins / 2;
}

/**
 * Display's resolution line: which spectrum shows, and its hertz per bin.
 * @param {boolean} fine
 * @param {number} binHz
 * @returns {string}
 */
export function resolutionNote(fine, binHz) {
  const hz = binHz < 10 ? binHz.toFixed(1) : String(Math.round(binHz));
  return fine ? `Fine spectrum of the VFO's channel: ${hz} Hz per bin.` : `Wide spectrum: ${hz} Hz per bin.`;
}

/**
 * The decimals, in MHz, of the frequency that the readout gives under the
 * pointer: tens of hertz, or the hertz once a pixel spans less than 10 Hz.
 * @param {number} viewHz    the view's width
 * @param {number} widthPx   its width on screen, CSS px
 * @returns {number}
 */
export function readoutDecimals(viewHz, widthPx) {
  return viewHz / widthPx < 10 ? 6 : 5;
}
