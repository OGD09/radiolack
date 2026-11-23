/*
 * The waterfall's time scale: the time its height shows, which its handle on
 * the right edge and Display's Time shown set, and the timestamps' step. The
 * waterfall draws one line per CSS pixel row, at the speed that the
 * preferences keep (lines per second): the time shown is its height over
 * that speed, and a taller window shows more time, as before.
 */

/** The time shown that the handle and Display's slider reach, s. */
export const MIN_SPAN_S = 10;
export const MAX_SPAN_S = 1200;

/** The speeds a saved preference may take, lines per second. */
export const MIN_SPEED = 0.05;
export const MAX_SPEED = 120;

/** What a notch of the wheel over the handle multiplies the time shown by. */
export const WHEEL_FACTOR = 1.15;
/** Wheel travel in CSS px that one notch stands for. */
export const WHEEL_NOTCH_PX = 100;
/** What each CSS px of a drag down the handle multiplies the time shown by; up divides it. */
export const DRAG_FACTOR = 1.012;

/** Display's slider: 0 to SLIDER_STEPS, along a logarithmic scale of the time shown. */
export const SLIDER_STEPS = 1000;

/** The timestamps' steps, s, and the least room between two, CSS px. */
export const STAMP_STEPS_S = [10, 15, 30, 60, 120, 300, 600];
export const MIN_STAMP_GAP_PX = 40;

const clamp = (/** @type {number} */ v, /** @type {number} */ lo, /** @type {number} */ hi) => Math.min(hi, Math.max(lo, v));

/**
 * The time shown within the handle's reach.
 * @param {number} spanS
 * @returns {number}
 */
export function clampSpan(spanS) {
  return clamp(spanS, MIN_SPAN_S, MAX_SPAN_S);
}

/**
 * A saved speed within the speeds a preference may take.
 * @param {number} speed
 * @returns {number}
 */
export function clampSpeed(speed) {
  return clamp(speed, MIN_SPEED, MAX_SPEED);
}

/**
 * The time that a waterfall `heightPx` tall shows at `speed` lines per second, s.
 * @param {number} heightPx
 * @param {number} speed
 * @returns {number}
 */
export function spanOf(heightPx, speed) {
  return heightPx / speed;
}

/**
 * The speed at which a waterfall `heightPx` tall shows `spanS` seconds,
 * within the handle's reach, in lines per second.
 * @param {number} heightPx
 * @param {number} spanS
 * @returns {number}
 */
export function speedFor(heightPx, spanS) {
  return clampSpeed(heightPx / clampSpan(spanS));
}

/**
 * The time shown after the wheel moved `px` CSS px over the handle, down
 * lengthening it: a notch at most per event.
 * @param {number} spanS
 * @param {number} px
 * @returns {number}
 */
export function wheeledSpan(spanS, px) {
  return clampSpan(spanS * WHEEL_FACTOR ** (clamp(px, -WHEEL_NOTCH_PX, WHEEL_NOTCH_PX) / WHEEL_NOTCH_PX));
}

/**
 * The time shown after a drag of `downPx` CSS px down the handle (negative:
 * up) from a press made while it was `startS`.
 * @param {number} startS
 * @param {number} downPx
 * @returns {number}
 */
export function draggedSpan(startS, downPx) {
  return clampSpan(startS * DRAG_FACTOR ** downPx);
}

/**
 * The time shown at a position of Display's slider.
 * @param {number} value  0 to SLIDER_STEPS
 * @returns {number}
 */
export function sliderSpan(value) {
  return MIN_SPAN_S * (MAX_SPAN_S / MIN_SPAN_S) ** (clamp(value, 0, SLIDER_STEPS) / SLIDER_STEPS);
}

/**
 * The position of Display's slider for a time shown.
 * @param {number} spanS
 * @returns {number}
 */
export function sliderValue(spanS) {
  return Math.round(SLIDER_STEPS * Math.log(clampSpan(spanS) / MIN_SPAN_S) / Math.log(MAX_SPAN_S / MIN_SPAN_S));
}

/**
 * A time shown as the interface writes it: "24 s", "2.5 min", "20 min".
 * @param {number} spanS
 * @returns {string}
 */
export function spanText(spanS) {
  if (spanS < 59.5) return `${Math.round(spanS)} s`;
  const minutes = spanS / 60;
  return `${minutes < 9.75 ? Math.round(minutes * 2) / 2 : Math.round(minutes)} min`;
}

/**
 * The timestamps' step for a time shown: the shortest that leaves at least
 * MIN_STAMP_GAP_PX between two, s.
 * @param {number} spanS
 * @param {number} heightPx
 * @returns {number}
 */
export function stampStep(spanS, heightPx) {
  const pxPerS = heightPx / spanS;
  return STAMP_STEPS_S.find((step) => step * pxPerS >= MIN_STAMP_GAP_PX) ?? STAMP_STEPS_S[STAMP_STEPS_S.length - 1];
}

/**
 * A timestamp's text: with its seconds while the step is shorter than a minute.
 * @param {number} timeMs  ms since the epoch
 * @param {number} stepS
 * @returns {string}
 */
export function stampText(timeMs, stepS) {
  return new Date(timeMs).toTimeString().slice(0, stepS < 60 ? 8 : 5);
}
