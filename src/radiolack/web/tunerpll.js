/*
 * The driver's computation of an R820T's or R828D's PLL, in the Receiver
 * menu: the choice that tells Radiolack where the tuner truly sits on its
 * grid, and the centre in use that the grid gives for the centre asked.
 */

import { formatFrequency } from './frequency.js';

/**
 * How the driver behind rtl_tcp computes the tuner's PLL: not known yet,
 * as rtl-sdr up to 2.0.1 and the RTL-SDR Blog fork do (a constant low on
 * the grid), or as rtl-sdr 2.0.2 and later do (exact on the grid).
 * @typedef {'unknown' | 'truncating' | 'rounding'} DriverPll
 */

/** Every choice, in the order the menu lists them. @type {readonly DriverPll[]} */
export const DRIVER_PLLS = ['unknown', 'truncating', 'rounding'];

/** The tuners whose PLL the setting describes, as rtl_tcp names them. */
export const PLL_TUNERS = ['Rafael Micro R820T', 'Rafael Micro R828D'];

/**
 * Each choice as the Receiver menu's list names it.
 * @type {Readonly<Record<DriverPll, string>>}
 */
export const DRIVER_PLL_NAMES = {
  unknown: 'Unknown',
  truncating: 'rtl-sdr up to 2.0.1, RTL-SDR Blog',
  rounding: 'rtl-sdr 2.0.2 and later',
};

/**
 * What each choice does, shown under the list for the one chosen.
 * @type {Readonly<Record<DriverPll, string>>}
 */
export const DRIVER_PLL_NOTES = {
  unknown: 'Radiolack places the tuner where its error is the same at every centre frequency, and takes the '
    + 'tuner as exact there. Calibrate identifies the driver on a steady carrier; choose it here if you know it.',
  truncating: 'rtl-sdr up to 2.0.1 (Debian 12\'s 0.6.0) and the RTL-SDR Blog driver: the tuner sits a little '
    + 'low, by the same amount at every centre frequency of its range, and Radiolack takes it off.',
  rounding: 'rtl-sdr 2.0.2 and later: the tuner sits exactly where Radiolack places it.',
};

/**
 * The Receiver menu's driver controls for a source state.
 * @typedef {object} DriverPllView
 * @property {boolean} shown        the receiver has an R820T or R828D and the setting
 * @property {DriverPll | ''} value the choice made, '' without one
 * @property {string} note          what the choice does
 */

/**
 * @param {string | null | undefined} value
 * @returns {value is DriverPll}
 */
export const isDriverPll = (value) => DRIVER_PLLS.some((choice) => choice === value);

/**
 * The driver controls for ``source``: hidden for a source without the
 * setting, such as the simulated band, and for a tuner other than an R820T
 * or R828D, which is never placed on a grid.
 * @param {import('./stream.js').SourceState | null} source
 * @returns {DriverPllView}
 */
export function driverPllView(source) {
  const value = source ? source.driverPll : null;
  if (!source || !isDriverPll(value) || !PLL_TUNERS.includes(source.tuner ?? '')) {
    return { shown: false, value: '', note: '' };
  }
  return { shown: true, value, note: DRIVER_PLL_NOTES[value] };
}

/** How far apart the tuner's grid puts the centres in use, on a crystal without error. */
export const GRID_HZ = 112_500;

/**
 * How far apart, on the air, the centres in use lie where ``source``'s
 * tuner is placed on its grid: its crystal's correction moves them; 0 for
 * a receiver that is never placed, or samples directly.
 * @param {import('./stream.js').SourceState | null} source
 * @returns {number}
 */
export function gridStepHz(source) {
  if (!source || source.direct || !PLL_TUNERS.includes(source.tuner ?? '')) return 0;
  return GRID_HZ * (1 + (source.ppm ?? 0) / 1e6);
}

/**
 * A retune for the VFO to sit at ``f``, ``clearHz`` above the centre at
 * least, asked ``asked``, and the receiver placed at ``inUse``, which may
 * leave the VFO's channel over the centre's DC residue: the centre to ask
 * instead, the grid's highest point that clears it, or null when the VFO
 * clears it already, when the retune did not put the VFO above the centre
 * (at the bottom of the receiver's range), or when no such point keeps the
 * channel, ``aboveHz`` above the VFO, within the span or the range.
 * @param {{ f: number, asked: number, inUse: number, clearHz: number, aboveHz: number,
 *   stepHz: number, rateHz: number, lowestHz: number }} retune
 * @returns {number | null}
 */
export function clearedCentre({ f, asked, inUse, clearHz, aboveHz, stepHz, rateHz, lowestHz }) {
  if (stepHz <= 0 || f - inUse >= clearHz || f - asked < clearHz) return null;
  const steps = Math.ceil((clearHz - (f - inUse)) / stepHz);
  const centre = Math.round(inUse - steps * stepHz);
  if (centre < lowestHz || f + aboveHz - centre > rateHz / 2) return null;
  return centre;
}

/**
 * Where the receiver sits for the centre asked, when that is a whole hertz
 * or more away from it, as an R820T placed on its grid is; '' otherwise.
 * @param {import('./stream.js').SourceState | null} source
 * @returns {string}
 */
export function centreInUseNote(source) {
  if (!source || Math.abs(source.centreInUseHz - source.centreHz) < 1) return '';
  return `In use: ${formatFrequency(source.centreInUseHz)} MHz, where the tuner sits.`;
}
