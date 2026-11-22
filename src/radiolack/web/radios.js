/*
 * The Receivers menu's receivers, which the protocol calls radios
 * (later-milestones.md, section 5): one card per receiver, saying what it
 * reads and runs, the one this interface shows framed; the row that adds
 * a receiver from a device that none reads, and the entries it opens; the
 * second press that removes a receiver while a scan or a recording runs
 * on it; the chip beside the frequency, with several receivers; the dot
 * on the menu's icon across the receivers; and the receiver that this
 * device chose last, which it shows again at each connection.
 */

/** @typedef {import('./stream.js').Radio} Radio */
/** @typedef {import('./stream.js').RadiosState} RadiosState */
/** @typedef {import('./stream.js').RtlDevicesState} RtlDevicesState */
/** @typedef {import('./stream.js').DevicesState} DevicesState */
/** @typedef {import('./stream.js').SourceState} SourceState */

import { RTL_KIND, choosable, rtlLabel } from './rtldevices.js';

/** How long the first press on Remove waits for the second, ms. */
export const REMOVE_CONFIRM_MS = 5000;

const KIND_NAMES = /** @type {Record<string, string>} */ ({
  synthetic: 'Synthetic band', rtl_sdr: 'RTL-SDR', rtl_tcp: 'rtl_tcp server', soapy: 'SoapySDR device',
});
const SCAN_NAMES = /** @type {Record<string, string>} */ ({
  memory: 'memory scan', sweep: 'range scan', signals: 'range scan',
});
const RECORDING_NAMES = /** @type {Record<string, string>} */ ({ audio: 'recording audio', iq: 'recording I/Q' });

/**
 * A radio's receiver, as the device remembers the radio it chose: radio
 * numbers change from one start to the next, receivers do not. The names
 * learned for the devices are kept under the same key.
 * @param {Radio} radio
 */
export function radioKey(radio) {
  return `${radio.kind}:${radio.device ?? ''}`;
}

/**
 * What a scan or a recording that runs on a radio is called.
 * @param {Radio} radio
 * @returns {string[]}
 */
function activity(radio) {
  const parts = [];
  if (radio.scan !== null) parts.push(SCAN_NAMES[radio.scan] ?? `${radio.scan} scan`);
  for (const kind of radio.recording) parts.push(RECORDING_NAMES[kind] ?? `recording ${kind}`);
  return parts;
}

/**
 * Learn the devices' names from the RTL-SDR receivers listed and the
 * SoapySDR devices found, under the keys that radioKey gives the radios
 * that read them. The names stay once learned: the list is read only
 * while the Receivers menu shows, and the chip shows without it.
 * @param {Map<string, string>} names
 * @param {RtlDevicesState | null} listed
 * @param {DevicesState | null} devices
 */
export function learnNames(names, listed, devices) {
  if (listed && listed.listing === 'sysfs') {
    for (const device of listed.devices) {
      if (device.found && device.device !== null && device.name) names.set(`${RTL_KIND}:${device.device}`, device.name);
    }
  }
  if (devices) {
    for (const device of devices.devices) {
      if (device.serial && device.label) names.set(`soapy:${device.driver} ${device.serial}`, device.label);
    }
  }
}

/**
 * A receiver's device: its name, as learned, or its kind's, and what
 * tells it apart, an RTL-SDR's or a SoapySDR device's serial number or an
 * rtl_tcp server's address; '' for the simulated band.
 * @param {Radio} radio
 * @param {Map<string, string>} names
 * @returns {{name: string, serial: string}}
 */
export function deviceOf(radio, names) {
  const name = names.get(radioKey(radio)) ?? KIND_NAMES[radio.kind] ?? radio.kind;
  let serial = radio.device ?? '';
  if (radio.kind === 'soapy' && /^[^\s=,]+ \S+$/.test(serial)) serial = serial.slice(serial.indexOf(' ') + 1);
  return { name, serial };
}

/**
 * One card of the Receivers menu.
 * @typedef {object} ReceiverCard
 * @property {number} id
 * @property {string} name       its device's
 * @property {string} serial     what tells the device apart, or ''
 * @property {boolean} shown     this interface shows it
 * @property {'live' | 'wait' | 'idle'} state
 * @property {string} stateText
 * @property {{kind: 'scan' | 'rec', text: string}[]} tags   the scan and the recordings that run on it
 */

/**
 * The cards, one per receiver, the first first. The centre frequency is
 * known for the receiver shown alone, from its source state.
 * @param {RadiosState} radios
 * @param {Map<string, string>} names
 * @param {number | null} shownCentreHz  the shown receiver's centre frequency, or null
 * @returns {ReceiverCard[]}
 */
export function receiverCards(radios, names, shownCentreHz) {
  return radios.radios.map((radio) => {
    const shown = radio.id === radios.shown;
    /** @type {ReceiverCard['state']} */
    let state = 'idle';
    let stateText = 'Idle';
    if (radio.status === 'streaming') {
      state = 'live';
      stateText = shown && shownCentreHz !== null ? `Receiving · ${(shownCentreHz / 1e6).toFixed(3)} MHz` : 'Receiving';
    } else if (radio.status === 'connecting') {
      state = 'wait';
      stateText = 'Connecting';
    } else if (radio.status === 'waiting') {
      state = 'wait';
      stateText = 'Waiting for its device';
    }
    /** @type {ReceiverCard['tags']} */
    const tags = [];
    if (radio.scan !== null) tags.push({ kind: 'scan', text: SCAN_NAMES[radio.scan] ?? `${radio.scan} scan` });
    for (const kind of radio.recording) tags.push({ kind: 'rec', text: RECORDING_NAMES[kind] ?? `recording ${kind}` });
    return { id: radio.id, ...deviceOf(radio, names), shown, state, stateText, tags };
  });
}

/**
 * The receiver shown, as the heading of its settings and the chip say it.
 * @param {RadiosState | null} radios
 * @param {Map<string, string>} names
 * @returns {{id: number, name: string} | null}
 */
export function shownReceiver(radios, names) {
  const radio = radios ? radios.radios.find((one) => one.id === radios.shown) : undefined;
  return radio ? { id: radio.id, name: deviceOf(radio, names).name } : null;
}

/**
 * Whether a scan or a recording runs on any radio, for the dot on the
 * menu's icon.
 * @param {Radio[]} radios
 * @returns {{scanning: boolean, recording: boolean}}
 */
export function radiosBadge(radios) {
  return {
    scanning: radios.some((radio) => radio.scan !== null),
    recording: radios.some((radio) => radio.recording.length > 0),
  };
}

/**
 * One entry of the list that "Add a receiver" opens: its text, the
 * source that the engine opens for it, as the radio message's `add` takes
 * it, and whether it is a device plugged in.
 * @typedef {object} AddOption
 * @property {string} label
 * @property {Record<string, string>} source
 * @property {boolean} plugged
 */

/**
 * The devices that a new receiver may read: the kinds of source the engine
 * opens, those that no receiver reads, the RTL-SDR receivers plugged in
 * that rtl_tcp opens by their serial numbers and none reads, and the
 * SoapySDR devices that the latest search found, not in use. None once
 * the engine runs its most receivers, or while the stream is closed.
 * @param {RadiosState | null} radios
 * @param {SourceState | null} source       the shown radio's: the kinds of source the engine opens
 * @param {RtlDevicesState | null} listed   the RTL-SDR receivers plugged in, as the engine listed them last
 * @param {DevicesState | null} devices     the SoapySDR devices found
 * @param {boolean} open
 * @returns {AddOption[]}
 */
export function addOptions(radios, source, listed, devices, open) {
  if (!open || !radios || !source || radios.radios.length >= radios.max) return [];
  const kinds = source.kinds;
  /** @param {string} kind */
  const unread = (kind) => kinds.includes(kind) && !radios.radios.some((radio) => radio.kind === kind);
  /** @type {AddOption[]} */
  const options = [];
  if (unread('synthetic')) options.push({ label: 'Synthetic band (2 m)', source: { kind: 'synthetic' }, plugged: false });
  const plugged = listed && listed.listing === 'sysfs' ? listed.devices : [];
  if (kinds.includes(RTL_KIND)) {
    if (plugged.some(choosable)) {
      for (const device of plugged) {
        if (choosable(device) && device.radio === null && device.device !== null) {
          options.push({ label: rtlLabel(device), source: { kind: RTL_KIND, device: device.device }, plugged: true });
        }
      }
    } else if (unread(RTL_KIND)) {    // without a list: the first receiver, as --rtl-sdr opens it
      options.push({ label: 'RTL-SDR · this computer', source: { kind: RTL_KIND, device: '0' }, plugged: false });
    }
  }
  if (unread('rtl_tcp')) options.push({ label: 'RTL-SDR · rtl_tcp', source: { kind: 'rtl_tcp' }, plugged: false });
  if (kinds.includes('soapy') && devices) {
    for (const device of devices.devices) {
      if (!device.inUse) {
        options.push({ label: `SoapySDR · ${device.label}`, source: { kind: 'soapy', driver: device.driver, serial: device.serial },
          plugged: true });
      }
    }
  }
  return options;
}

/**
 * The "Add a receiver" row: whether it opens a list, and what it says of
 * the devices plugged in that no receiver reads and of the most receivers.
 * @param {RadiosState} radios
 * @param {AddOption[]} options
 * @returns {{enabled: boolean, detail: string}}
 */
export function addRow(radios, options) {
  if (radios.radios.length >= radios.max) return { enabled: false, detail: `${radios.max} at most: remove one first` };
  const plugged = options.filter((option) => option.plugged).length;
  const more = plugged ? `${plugged} more plugged in` : 'No other device plugged in';
  return { enabled: options.length > 0, detail: `${more} · up to ${radios.max}` };
}

/**
 * The Remove button for the receiver shown: the first stays; one on which
 * a scan or a recording runs takes a second press, which says what stops.
 * @typedef {object} RemoveView
 * @property {boolean} shown    with several receivers
 * @property {boolean} enabled
 * @property {string} button    what the button says
 * @property {string} title     why it cannot be pressed, or ''
 */

/**
 * @param {RadiosState | null} radios
 * @param {boolean} armed  a first press waits for the second
 * @returns {RemoveView}
 */
export function removeView(radios, armed) {
  const shown = radios ? radios.radios.find((radio) => radio.id === radios.shown) : undefined;
  if (!radios || !shown) return { shown: false, enabled: false, button: 'Remove', title: '' };
  const several = radios.radios.length > 1;
  if (shown === radios.radios[0]) {
    return { shown: several, enabled: false, button: 'Remove', title: 'Receiver 1 stays: change its device instead' };
  }
  const running = activity(shown);
  if (armed && running.length) return { shown: several, enabled: true, button: `Confirm: stops ${running.join(', ')}`, title: '' };
  return { shown: several, enabled: true, button: 'Remove', title: '' };
}

/**
 * What a press on Remove does: "arm" for a first press on a radio on which
 * something runs, "remove" otherwise.
 * @param {RadiosState | null} radios
 * @param {boolean} armed
 * @returns {'arm' | 'remove' | 'none'}
 */
export function removePress(radios, armed) {
  const shown = radios ? radios.radios.find((radio) => radio.id === radios.shown) : undefined;
  if (!radios || !shown || shown === radios.radios[0]) return 'none';
  return activity(shown).length && !armed ? 'arm' : 'remove';
}

/**
 * The radio to show again at a connection: the one that reads the
 * receiver this device chose last, when the engine shows another; null
 * otherwise.
 * @param {RadiosState} radios
 * @param {string} remembered  the receiver of the radio this device chose last, radioKey's, or ''
 * @returns {number | null}
 */
export function radioToRestore(radios, remembered) {
  if (!remembered) return null;
  const radio = radios.radios.find((one) => radioKey(one) === remembered);
  return radio && radio.id !== radios.shown ? radio.id : null;
}
