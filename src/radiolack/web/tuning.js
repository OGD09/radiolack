/*
 * The Automatic tuning step: the band plan's channel spacing where the view
 * shows two channels or more, otherwise a step that follows the view's width,
 * by decades from 100 kHz down to 1 Hz.
 */

/**
 * Each view width, in Hz, from which a step applies, widest first, with that
 * step in Hz; a narrower view steps by FINEST_STEP_HZ.
 * @type {ReadonlyArray<readonly [number, number]>}
 */
export const ZOOM_STEPS = [[200e6, 100e3], [20e6, 10e3], [2e6, 1e3], [200e3, 100], [20e3, 10]];
/** The step of the narrowest views, Hz. */
export const FINEST_STEP_HZ = 1;

/**
 * The Automatic step for a view `viewHz` wide on a range whose channels are
 * `spacingHz` apart, or null where the band plan gives no spacing.
 * @param {number} viewHz       width of the view, Hz
 * @param {number | null} spacingHz
 * @returns {number}
 */
export function automaticStep(viewHz, spacingHz) {
  if (spacingHz !== null && spacingHz > 0 && viewHz >= 2 * spacingHz) return spacingHz;
  for (const [width, step] of ZOOM_STEPS) if (viewHz >= width) return step;
  return FINEST_STEP_HZ;
}
