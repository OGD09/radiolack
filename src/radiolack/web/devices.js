/*
 * The Receiver menu's SoapySDR devices (later-milestones.md, section 1,
 * pull request 3): when its part shows, the list of the devices that the
 * engine's search found with the device in use, Search again and the
 * device arguments field, and what the part's note says. The engine
 * searches only when this computer's interface asks, the first time once
 * the part shows in the Receiver menu; a device that remote access serves
 * may choose among the devices found, but neither searches nor gives
 * arguments.
 */

/** @typedef {import('./stream.js').DevicesState} DevicesState */
/** @typedef {import('./stream.js').SoapyDevice} SoapyDevice */
/** @typedef {import('./stream.js').SourceState} SourceState */

/** The Receiver list's entry for SoapySDR devices. */
export const SOAPY_KIND = 'soapy';

/**
 * One entry of the list of devices: its value, '' for the line shown when
 * no device in use is selected, and its text.
 * @typedef {object} DeviceOption
 * @property {string} value
 * @property {string} label
 */

/**
 * The SoapySDR part of the Receiver menu for the states as they are.
 * @typedef {object} DevicesView
 * @property {boolean} shown          the part shows
 * @property {DeviceOption[]} options the list's entries
 * @property {string} value           the entry selected: the device in use, or ''
 * @property {boolean} listEnabled    a device found may be chosen
 * @property {string} note            the search's state, under the list
 * @property {boolean} searchShown    Search again: this computer's interfaces only
 * @property {boolean} searchEnabled
 * @property {boolean} argsShown      the arguments field: this computer's interfaces only
 * @property {boolean} argsEnabled
 * @property {string} args            the device arguments in use, or ''
 * @property {boolean} firstSearch    no search has run: this computer's interface asks for one now
 */

/**
 * Whether choosing "SoapySDR device" in the Receiver list switches to a
 * device at once, the one chosen last, or shows the SoapySDR part alone, a
 * device to be chosen there; at once too for an engine that sends no
 * devices state, which then says why it refuses.
 * @param {DevicesState | null} devices
 * @returns {boolean}
 */
export function switchesAtOnce(devices) {
  return devices === null || devices.chosen;
}

/**
 * Names in a sentence: "a", "a and b", "a, b and c".
 * @param {string[]} names
 */
export function nameList(names) {
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** @param {string} text */
const sentence = (text) => (text ? `${text[0].toUpperCase()}${text.slice(1)}${/[.…]$/.test(text) ? '' : '.'}` : '');

/**
 * What the note under the list says of the search.
 * @param {DevicesState} devices
 * @param {boolean} remote  a device that remote access serves, which does not search
 */
export function searchNote(devices, remote) {
  const drivers = nameList(devices.drivers);
  const found = devices.devices.some((device) => !device.inUse);
  const none = devices.devices.length ? 'No other device found.' : 'No device found.';
  switch (devices.search) {
    case 'never':
      return remote ? 'Not searched yet: the computer searches.' : 'Not searched yet.';
    case 'searching':
      return `Searching ${drivers}…`;
    case 'unavailable':
      return sentence(devices.detail);
    case 'failed':
      return `${sentence(devices.detail)} Searched: ${drivers}.`;
    default: {
      const other = remote ? '' : ' Any other device opens by its arguments.';
      return found ? `Searched: ${drivers}.` : `${none} Searched: ${drivers}.${other}`;
    }
  }
}

/**
 * The SoapySDR part for the source in use, the devices state, whether this
 * interface picked "SoapySDR device" in the Receiver list without a device
 * in use, whether a device that remote access serves shows it, and whether
 * the stream is open. Hidden until the engine sends a devices state, and
 * while another kind of source is in use, unless picked.
 * @param {SourceState | null} source
 * @param {DevicesState | null} devices
 * @param {boolean} picked
 * @param {boolean} remote
 * @param {boolean} open
 * @param {Record<string, string>} [names]  the names given to the devices, which the list shows
 * @returns {DevicesView}
 */
export function devicesView(source, devices, picked, remote, open, names = {}) {
  const shown = Boolean(source && devices && source.kinds.includes(SOAPY_KIND)
    && (source.kind === SOAPY_KIND || picked));
  if (!shown || !source || !devices) {
    return { shown: false, options: [], value: '', listEnabled: false, note: '', searchShown: false,
      searchEnabled: false, argsShown: false, argsEnabled: false, args: '', firstSearch: false };
  }
  const searching = devices.search === 'searching';
  const inUse = source.kind === SOAPY_KIND ? devices.devices.findIndex((device) => device.inUse) : -1;
  /** @type {DeviceOption[]} */
  const options = devices.devices.map((device, index) => ({ value: String(index),
    label: names[`${SOAPY_KIND}:${device.driver} ${device.serial}`] ?? device.label }));
  if (inUse < 0) {
    const label = searching ? 'Searching…' : options.length ? 'Choose a device' : 'No device found';
    options.unshift({ value: '', label });
  }
  return {
    shown,
    options,
    value: inUse < 0 ? '' : String(inUse),
    listEnabled: open && !searching && devices.devices.length > 0,
    note: searchNote(devices, remote),
    searchShown: !remote,
    searchEnabled: open && !searching && !remote,
    argsShown: !remote,
    argsEnabled: open && !searching && !remote,
    args: devices.args ?? '',
    firstSearch: open && !remote && devices.search === 'never',
  };
}

/**
 * The device that an entry of the list names, or null for the line without
 * one, or for an entry the list no longer holds.
 * @param {DevicesState | null} devices
 * @param {string} value
 * @returns {SoapyDevice | null}
 */
export function deviceAt(devices, value) {
  if (!devices || !/^\d+$/.test(value)) return null;
  return devices.devices[Number(value)] ?? null;
}
