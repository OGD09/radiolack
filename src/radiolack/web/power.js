/*
 * Antenna power (bias tee) in the Receiver menu: 4.5 V on the antenna
 * socket of an RTL-SDR Blog V3 or V4. Turning it on takes a second press,
 * which says what it does; turning it off is immediate.
 */

/** How long the first press waits for the second, ms. */
export const POWER_CONFIRM_MS = 5000;

/** What turning the power on does, shown on the button that waits for the second press. */
export const POWER_WARNING = '4.5 V on the antenna socket';

/**
 * The antenna power's control for a source state.
 * @typedef {object} PowerView
 * @property {boolean} shown    the source has antenna power
 * @property {boolean} on
 * @property {string} state     "on" or "off", beside the control's name
 * @property {string} button    what the button does
 * @property {string} label     the button's accessible name
 */

/**
 * @param {import('./stream.js').SourceState | null} source
 * @param {boolean} armed  a first press waits for the second
 * @returns {PowerView}
 */
export function powerView(source, armed) {
  if (!source || source.biasTee === null) {
    return { shown: false, on: false, state: '', button: '', label: '' };
  }
  if (source.biasTee) {
    return { shown: true, on: true, state: 'on', button: 'Turn off', label: 'Turn the antenna power off' };
  }
  if (armed) {
    return {
      shown: true, on: false, state: 'off', button: `Confirm: ${POWER_WARNING}`,
      label: `Confirm turning the antenna power on: ${POWER_WARNING}`,
    };
  }
  return { shown: true, on: false, state: 'off', button: 'Turn on', label: 'Turn the antenna power on' };
}

/**
 * What a press on the button does: "off" when the power is on, "on" when a
 * first press waits, "arm" otherwise.
 * @param {boolean} on
 * @param {boolean} armed
 * @returns {'off' | 'on' | 'arm'}
 */
export function powerPress(on, armed) {
  if (on) return 'off';
  return armed ? 'on' : 'arm';
}
