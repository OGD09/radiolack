/*
 * The Mode and filter panel, which the mode and the filter width under the
 * frequency open: the filter widths that its Narrow, Normal and Wide give a
 * mode, and which of them a width is.
 */

/**
 * A mode's filter widths, Hz: the one it opens with, its limits and its step.
 * @typedef {{ bw: number, min: number, max: number, step: number }} ModeWidths
 */

/** @typedef {'narrow' | 'normal' | 'wide'} WidthPreset */

/** Narrow's and Wide's share of the width a mode opens with. */
export const NARROW_SHARE = 0.6;
export const WIDE_SHARE = 1.5;

/**
 * Narrow, Normal and Wide for a mode: the width it opens with, and 0.6 and
 * 1.5 times that width, on the mode's step and within its limits.
 * @param {ModeWidths} widths
 * @returns {Record<WidthPreset, number>}
 */
export function widthPresets(widths) {
  const onStep = (/** @type {number} */ hz) => {
    const snapped = Math.round(hz / widths.step) * widths.step;
    return Math.min(widths.max, Math.max(widths.min, snapped));
  };
  return { narrow: onStep(widths.bw * NARROW_SHARE), normal: onStep(widths.bw), wide: onStep(widths.bw * WIDE_SHARE) };
}

/**
 * The preset that a width is, or '' for any other width.
 * @param {ModeWidths} widths
 * @param {number} bw
 * @returns {WidthPreset | ''}
 */
export function presetAt(widths, bw) {
  const presets = widthPresets(widths);
  for (const name of /** @type {WidthPreset[]} */ (['normal', 'narrow', 'wide'])) {
    if (presets[name] === bw) return name;
  }
  return '';
}
