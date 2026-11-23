/*
 * The volume at hand: what a drag on the speaker, a notch of the wheel over
 * it or an arrow key gives, and what its gauge says.
 */

/** Percent per notch of the wheel and per arrow key, on a grid of that step. */
export const VOLUME_STEP = 5;

/** CSS pixels of a drag per percent: the gauge, 160 px tall, follows the finger. */
export const DRAG_PX_PER_PERCENT = 1.6;

/** How far a press moves before it drags the volume rather than taps. */
export const VOLUME_DRAG_START_PX = 6;

const clampVolume = (/** @type {number} */ v) => Math.min(100, Math.max(0, Math.round(v)));

/**
 * The volume after a drag of ``upPx`` CSS pixels upwards (negative: down)
 * from a press made at ``start``.
 * @param {number} start
 * @param {number} upPx
 * @returns {number}
 */
export function draggedVolume(start, upPx) {
  return clampVolume(start + upPx / DRAG_PX_PER_PERCENT);
}

/**
 * The volume ``steps`` steps up (negative: down) from ``volume``: the first
 * step lands on the grid of VOLUME_STEP.
 * @param {number} volume
 * @param {number} steps
 * @returns {number}
 */
export function steppedVolume(volume, steps) {
  if (!steps) return clampVolume(volume);
  const onGrid = steps > 0 ? Math.floor(volume / VOLUME_STEP) : Math.ceil(volume / VOLUME_STEP);
  return clampVolume((onGrid + steps) * VOLUME_STEP);
}

/**
 * What the gauge says of a volume.
 * @param {number} volume
 * @returns {string}
 */
export function volumeText(volume) {
  return volume > 0 ? `${volume}%` : 'Muted';
}
