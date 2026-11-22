/*
 * The Receiver list's entries: the simulated band, one entry per RTL-SDR
 * receiver plugged into the computer, as the engine read them from sysfs
 * without opening them, an rtl_tcp server and SoapySDR devices. The
 * interface asks for the RTL-SDR receivers each time the Receiver menu
 * shows, then every RTL_LIST_MS while it shows, never otherwise. Without
 * a list (an older engine, a system without sysfs, or nothing that can be
 * chosen by its serial number), one entry opens the RTL-SDR used last, as
 * "RTL-SDR · this computer" always did.
 */

/** @typedef {import('./stream.js').RtlDevice} RtlDevice */
/** @typedef {import('./stream.js').RtlDevicesState} RtlDevicesState */
/** @typedef {import('./stream.js').SourceState} SourceState */

export const RTL_KIND = 'rtl_sdr';
export const RTL_LIST_MS = 2000;     // how often the open Receiver menu asks for the receivers again
const RTL_PREFIX = 'rtl_sdr:';       // an entry's value: this, then its index in the list

/**
 * One entry of the Receiver list: its value, a kind of source or an
 * RTL-SDR receiver listed, its text and whether it may be chosen.
 * @typedef {object} ReceiverOption
 * @property {string} value
 * @property {string} label
 * @property {boolean} disabled
 */

/**
 * The Receiver list for the states as they are.
 * @typedef {object} ReceiverList
 * @property {ReceiverOption[]} options
 * @property {string} value   the entry selected, or '' before the engine answers
 * @property {string} note    what the list says of the receivers that cannot be chosen, or ''
 */

/**
 * Whether rtl_tcp opens this receiver, and it alone, by its serial number.
 * @param {RtlDevice} device
 */
export function choosable(device) {
  return device.found && device.device !== null && !device.shared && !device.readsAsIndex;
}

/**
 * Who holds a receiver not in use here: another of Radiolack's radios, or
 * from the drivers bound to it; or ''.
 * @param {RtlDevice} device
 */
function holder(device) {
  if (device.inUse) return '';
  if (device.radio !== null) return `read by radio ${device.radio}`;
  if (device.drivers.includes('usbfs')) return 'in use by another program';
  const dvb = device.drivers.find((driver) => driver.startsWith('dvb_usb'));
  if (dvb) return 'held by the DVB driver';
  return device.drivers.length ? `held by ${device.drivers[0]}` : '';
}

/**
 * An RTL-SDR receiver's entry text.
 * @param {RtlDevice} device
 */
export function rtlLabel(device) {
  if (!device.found) {
    return device.readsAsIndex ? `RTL-SDR · receiver ${device.device} by its index` : `RTL-SDR · ${device.device} · not found`;
  }
  const parts = ['RTL-SDR', device.name, device.device ?? 'no serial number'];
  const held = holder(device);
  if (held) parts.push(held);
  return parts.join(' · ');
}

/**
 * What the note says of the receivers listed that cannot be chosen.
 * @param {RtlDevice[]} devices
 */
export function rtlNote(devices) {
  const notes = [];
  const shared = [...new Set(devices.filter((d) => d.found && d.shared).map((d) => d.device))];
  for (const serial of shared) {
    notes.push(`Several receivers share the serial number ${serial}: rtl_tcp cannot tell them apart.`);
  }
  for (const d of devices.filter((one) => one.found && one.readsAsIndex && !one.shared)) {
    notes.push(`rtl_tcp reads the serial number ${d.device} as a receiver's index.`);
  }
  if (notes.length) notes.push('Give each receiver its own serial number with rtl_eeprom -s, one plugged in at a time.');
  if (devices.some((d) => d.found && d.device === null)) {
    notes.push('A receiver without a serial number opens with --rtl-sdr and its index.');
  }
  if (devices.some((d) => !d.found && d.readsAsIndex)) {
    notes.push('The receiver in use was opened by its index, which need not follow this list\'s order.');
  }
  return notes.join(' ');
}

/**
 * The Receiver list for the source in use, the RTL-SDR receivers the
 * engine listed last (null before it answered, or for an engine that lists
 * none), whether this interface picked "SoapySDR device" with no device
 * in use, whether the stream is open, and the other radios, which the
 * simulated band and the rtl_tcp server belong to when one reads them.
 * Kinds the engine does not open cannot be chosen, nor what another radio
 * reads, nor anything while the stream is closed.
 * @param {SourceState | null} source
 * @param {RtlDevicesState | null} listed
 * @param {boolean} soapyPicked
 * @param {boolean} open
 * @param {{id: number, kind: string}[]} [others]
 * @returns {ReceiverList}
 */
export function receiverList(source, listed, soapyPicked, open, others = []) {
  const kinds = open && source ? source.kinds : [];
  /** @param {string} kind */
  const kindOption = (kind, /** @type {string} */ label) => {
    const reader = kind === 'synthetic' || kind === 'rtl_tcp' ? others.find((other) => other.kind === kind) : undefined;
    return reader ? { value: kind, label: `${label} · read by radio ${reader.id}`, disabled: true }
      : { value: kind, label, disabled: !kinds.includes(kind) };
  };
  const devices = listed && listed.listing === 'sysfs' ? listed.devices : [];
  const fallback = !devices.some(choosable);
  /** @type {ReceiverOption[]} */
  const rtl = devices.map((device, index) => ({
    value: `${RTL_PREFIX}${index}`,
    label: rtlLabel(device),
    disabled: !kinds.includes(RTL_KIND) || (!device.inUse && (!choosable(device) || device.radio !== null)),
  }));
  if (fallback) rtl.unshift(kindOption(RTL_KIND, 'RTL-SDR · this computer'));
  const options = [kindOption('synthetic', 'Synthetic band (2 m)'), ...rtl,
    kindOption('rtl_tcp', 'RTL-SDR · rtl_tcp'), kindOption('soapy', 'SoapySDR device')];
  let value = soapyPicked ? 'soapy' : source ? source.kind : '';
  if (!soapyPicked && source && source.kind === RTL_KIND) {
    const inUse = devices.findIndex((device) => device.inUse);
    if (inUse >= 0) value = `${RTL_PREFIX}${inUse}`;
    else if (!fallback) value = '';
  }
  return { options, value, note: rtlNote(devices) };
}

/**
 * The RTL-SDR receiver that an entry names, or null for a kind of source,
 * or an entry the list no longer holds.
 * @param {RtlDevicesState | null} listed
 * @param {string} value
 * @returns {RtlDevice | null}
 */
export function rtlDeviceAt(listed, value) {
  if (!listed || !value.startsWith(RTL_PREFIX)) return null;
  const index = value.slice(RTL_PREFIX.length);
  if (!/^\d+$/.test(index)) return null;
  return listed.devices[Number(index)] ?? null;
}
