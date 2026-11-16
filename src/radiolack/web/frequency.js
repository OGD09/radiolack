/*
 * Frequencies as the interface writes and reads them: one formatter, in MHz
 * with as many decimals as the frequency has, down to the hertz, and one
 * reader for every field where a frequency is typed.
 */

/** The lowest frequency the engine takes, Hz (demod.py, bookmarks.py). */
export const MIN_FREQUENCY_HZ = 1;
/** The highest frequency the engine takes, Hz. */
export const MAX_FREQUENCY_HZ = 100e9;
/** Below this, a number typed without a unit is in MHz; from it, in Hz. */
export const BARE_MHZ_BELOW = 100000;

/**
 * A frequency in MHz, rounded to the hertz, with 3 to 6 decimals: the zeros
 * at the end beyond the third are dropped: "145.500", "145.6125",
 * "145.61255", "7.074001". Computed on whole hertz, so that no binary
 * rounding shows. A frequency that is not a finite number reads "—".
 * @param {number} hz
 * @returns {string}
 */
export function formatFrequency(hz) {
  if (!Number.isFinite(hz)) return '—';
  const whole = Math.round(Math.abs(hz));
  const sign = hz < 0 && whole > 0 ? '-' : '';
  const mhz = Math.floor(whole / 1e6);
  const decimals = String(whole - mhz * 1e6).padStart(6, '0').replace(/0{1,3}$/, '');
  return `${sign}${mhz}.${decimals}`;
}

/**
 * Each unit a typed frequency may end with, in lower case, and its factor.
 * @type {Readonly<Record<string, number>>}
 */
const UNITS = { hz: 1, k: 1e3, khz: 1e3, m: 1e6, mhz: 1e6, g: 1e9, ghz: 1e9 };
const TYPED = /^\s*(\d+(?:[.,]\d*)?|[.,]\d+)\s*([a-z]*)\s*$/i;

/**
 * Read a typed frequency: a number with a point or a comma for its decimals,
 * then an optional unit, in any case and after optional spaces: Hz, k or
 * kHz, M or MHz, G or GHz. Without a unit, a number below 100,000 is in MHz
 * and another in Hz. The result is rounded to the hertz; null when the text
 * is not a frequency, or one outside 1 Hz to 100 GHz.
 * @param {string} text
 * @returns {number | null}
 */
export function parseFrequency(text) {
  const match = TYPED.exec(text);
  if (!match) return null;
  const value = Number(match[1].replace(',', '.'));
  const unit = match[2].toLowerCase();
  let factor;
  if (unit) {
    if (!Object.hasOwn(UNITS, unit)) return null;
    factor = UNITS[unit];
  } else {
    factor = value < BARE_MHZ_BELOW ? 1e6 : 1;
  }
  const hz = Math.round(value * factor);
  return Number.isFinite(hz) && hz >= MIN_FREQUENCY_HZ && hz <= MAX_FREQUENCY_HZ ? hz : null;
}
