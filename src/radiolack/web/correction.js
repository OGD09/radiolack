/*
 * The Receiver menu's frequency correction: parts per million to the
 * hundredth, as the engine takes them (correction.py).
 */

/**
 * A correction as the field shows it, with two decimals: "37.42", "-3.00".
 * @param {number} ppm
 * @returns {string}
 */
export function ppmText(ppm) {
  return ppm.toFixed(2);
}

/**
 * A typed correction rounded to the hundredth and kept from ``min`` to
 * ``max``; NaN when nothing valid was typed.
 * @param {number} typed  the field's value as a number, NaN when empty
 * @param {number} min
 * @param {number} max
 * @returns {number}
 */
export function typedPpm(typed, min, max) {
  return Math.min(max, Math.max(min, Math.round(typed * 100) / 100));
}
