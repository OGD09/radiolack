/*
 * The memory scan's tour across the receiver's windows, as the Bookmarks
 * menu's dock tells it: with ticked bookmarks in several windows, the scan
 * watches each window in turn, and a transmission in the others meanwhile
 * goes unheard (README.md, Settings and bookmarks).
 */

/**
 * A window's share of the tour, s: the receiver's settling, about 0.4 s,
 * the noise's estimate, 0.33 s, and the watch, 1 s (scanners.VISIT_S).
 */
export const WINDOW_TOUR_S = 1.75;

/** The part of the span that detection searches, as a fraction of the sample rate (scanners.USABLE_FRACTION, twice). */
export const SEARCHED_FRACTION = 0.8;

/**
 * What the dock says of a tour of ``windows`` windows at ``rateHz``, or ''
 * when the bookmarks take one window at most: the tour's length, the
 * shortest transmission it cannot miss, and how close the bookmarks must
 * lie to be watched without a break.
 * @param {number} windows
 * @param {number} rateHz
 * @returns {string}
 */
export function tourNote(windows, rateHz) {
  if (!(windows > 1) || !(rateHz > 0)) return '';
  const tour = Math.round(WINDOW_TOUR_S * windows);
  const missed = Math.round(WINDOW_TOUR_S * windows - 1);
  const width = (SEARCHED_FRACTION * rateHz / 1e6).toFixed(2);
  return `${windows} windows, watched in turn about 1 s each: a tour takes about ${tour} s, more while listening, `
    + `and may miss a transmission shorter than about ${missed} s. Bookmarks within ${width} MHz take one window, `
    + 'watched without a break.';
}
