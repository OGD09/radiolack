/*
 * Antenna lengths for a frequency, shown beside the VFO's readout: the
 * wavelength, and the lengths to cut for a half-wave dipole and a
 * quarter-wave vertical.
 */

/** Speed of light in vacuum, m/s. */
export const SPEED_OF_LIGHT = 299792458;

/**
 * A resonant element is shorter than its share of the free-space wavelength,
 * by about 5% for a thin wire (end effect): the classic 143/f metres for a
 * half-wave dipole and 71.3/f metres for a quarter-wave element, f in MHz.
 */
export const SHORTENING = 0.95;

/**
 * Lengths for an antenna resonant at one frequency, in metres.
 * @typedef {object} AntennaLengths
 * @property {number} wavelength  in free space, without shortening
 * @property {number} half        a half-wave element, shortened: a dipole end to end
 * @property {number} quarter     a quarter-wave element, shortened: a dipole's arm, a
 *                                vertical's radiator and, by the classic formula, each
 *                                of a ground plane's radials
 */

/**
 * @param {number} hz  frequency, above 0
 * @returns {AntennaLengths}
 */
export function antennaLengths(hz) {
  const wavelength = SPEED_OF_LIGHT / hz;
  return { wavelength, half: (wavelength / 2) * SHORTENING, quarter: (wavelength / 4) * SHORTENING };
}

/** @type {ReadonlyArray<readonly [string, number]>} */
const UNITS = [['km', 1000], ['m', 1], ['cm', 0.01], ['mm', 0.001]];

/**
 * A length to 3 significant digits, in the largest unit that keeps it at 1 or
 * more: "2.06 m", "48.9 cm", "11.1 mm". More digits would claim a precision
 * that the conductor's thickness, its height and what stands near it take
 * away. A length that is not a positive number reads "—".
 * @param {number} metres
 * @returns {string}
 */
export function formatLength(metres) {
  if (!(metres > 0 && Number.isFinite(metres))) return '—';
  for (const [unit, scale] of UNITS) {
    // Rounded first, so that 0.9996 m reads "1.00 m" rather than "100 cm".
    const value = Number((metres / scale).toPrecision(3));
    if (value >= 1 || unit === 'mm') return `${value >= 1000 ? Math.round(value) : value.toPrecision(3)} ${unit}`;
  }
  return '—';
}
