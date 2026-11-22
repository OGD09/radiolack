/*
 * The Receiver menu's radios (later-milestones.md, section 5): the radio
 * list, which says what each radio reads and runs, the entries that add a
 * radio from a receiver that no radio reads, the second press that removes
 * a radio while a scan or a recording runs on it, the dot on the menu's
 * icon across the radios, and the radio that this device showed last,
 * which it shows again at each connection.
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
  synthetic: 'Synthetic band', rtl_sdr: 'RTL-SDR', rtl_tcp: 'rtl_tcp', soapy: 'SoapySDR',
});
const SCAN_NAMES = /** @type {Record<string, string>} */ ({
  memory: 'memory scan', sweep: 'range scan', signals: 'range scan',
});
const RECORDING_NAMES = /** @type {Record<string, string>} */ ({ audio: 'recording audio', iq: 'recording I/Q' });

/**
 * A radio's receiver, as the device remembers the radio it showed: radio
 * numbers change from one start to the next, receivers do not.
 * @param {Radio} radio
 */
export function radioKey(radio) {
  return `${radio.kind}:${radio.device ?? ''}`;
}

/**
 * What a scan or a recording that runs on a radio is called in its entry.
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
 * A radio's entry in the radio list: its number, what it reads, whether it
 * waits for its receiver, and what runs on it.
 * @param {Radio} radio
 */
export function radioLabel(radio) {
  const parts = [`Radio ${radio.id}`, KIND_NAMES[radio.kind] ?? radio.kind];
  if (radio.device) parts.push(radio.device);
  if (radio.status === 'waiting') parts.push('waiting');
  parts.push(...activity(radio));
  return parts.join(' · ');
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
 * One entry of the "Add a radio" list: its text, and the source that the
 * engine opens for it, as the radio message's `add` takes it.
 * @typedef {object} AddOption
 * @property {string} label
 * @property {Record<string, string>} source
 */

/**
 * The receivers that a new radio may read: the kinds of source the engine
 * opens, those that no radio reads, the RTL-SDR receivers plugged in that
 * rtl_tcp opens by their serial numbers and no radio reads, and the
 * SoapySDR devices that the latest search found, not in use. None once
 * the engine runs its most radios, or while the stream is closed.
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
  if (unread('synthetic')) options.push({ label: 'Synthetic band (2 m)', source: { kind: 'synthetic' } });
  const plugged = listed && listed.listing === 'sysfs' ? listed.devices : [];
  if (kinds.includes(RTL_KIND)) {
    if (plugged.some(choosable)) {
      for (const device of plugged) {
        if (choosable(device) && device.radio === null && device.device !== null) {
          options.push({ label: rtlLabel(device), source: { kind: RTL_KIND, device: device.device } });
        }
      }
    } else if (unread(RTL_KIND)) {    // without a list: the first receiver, as --rtl-sdr opens it
      options.push({ label: 'RTL-SDR · this computer', source: { kind: RTL_KIND, device: '0' } });
    }
  }
  if (unread('rtl_tcp')) options.push({ label: 'RTL-SDR · rtl_tcp', source: { kind: 'rtl_tcp' } });
  if (kinds.includes('soapy') && devices) {
    for (const device of devices.devices) {
      if (!device.inUse) {
        options.push({ label: `SoapySDR · ${device.label}`, source: { kind: 'soapy', driver: device.driver, serial: device.serial } });
      }
    }
  }
  return options;
}

/**
 * The Remove button for the radio shown: the first radio stays; a radio
 * on which a scan or a recording runs takes a second press, which says
 * what stops.
 * @typedef {object} RemoveView
 * @property {boolean} enabled
 * @property {string} button   what the button says
 */

/**
 * @param {RadiosState | null} radios
 * @param {boolean} armed  a first press waits for the second
 * @returns {RemoveView}
 */
export function removeView(radios, armed) {
  const shown = radios ? radios.radios.find((radio) => radio.id === radios.shown) : undefined;
  if (!radios || !shown || shown === radios.radios[0]) return { enabled: false, button: 'Remove this radio' };
  const running = activity(shown);
  if (armed && running.length) return { enabled: true, button: `Confirm: stops ${running.join(', ')}` };
  return { enabled: true, button: 'Remove this radio' };
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
 * receiver this device showed last, when the engine shows another; null
 * otherwise.
 * @param {RadiosState} radios
 * @param {string} remembered  the receiver of the radio this device showed last, radioKey's, or ''
 * @returns {number | null}
 */
export function radioToRestore(radios, remembered) {
  if (!remembered) return null;
  const radio = radios.radios.find((one) => radioKey(one) === remembered);
  return radio && radio.id !== radios.shown ? radio.id : null;
}
