/*
 * A converter in front of the receiver, in the Receiver menu: an
 * upconverter for shortwave or a downconverter such as a satellite LNB, set
 * by its oscillator's frequency. The engine converts: every frequency the
 * interface shows is the one on the air. Changing either control keeps the
 * receiver where it is tuned: the frequencies shown change, the signals
 * stay.
 */

import { formatFrequency, parseFrequency } from './frequency.js';

/**
 * What may stand in front of the receiver: nothing, an upconverter, a
 * downconverter (an LNB) and a downconverter that mirrors the spectrum.
 * @typedef {'none' | 'up' | 'down' | 'down_inverted'} Converter
 */

/** Every converter, in the order the menu lists them. @type {readonly Converter[]} */
export const CONVERTERS = ['none', 'up', 'down', 'down_inverted'];

/**
 * What each converter does, shown under the list for the one chosen.
 * @type {Readonly<Record<Converter, string>>}
 */
export const CONVERTER_NOTES = {
  none: 'Nothing between the antenna and the receiver.',
  up: 'The receiver tunes the frequency plus the oscillator\'s: shortwave through an upconverter, such as one of 125 MHz.',
  down: 'The receiver tunes the frequency minus the oscillator\'s: a satellite LNB, such as one of 9750 MHz, which needs 13 to 18 V on its cable, more than a dongle\'s antenna power gives.',
  down_inverted: 'The receiver tunes the oscillator\'s frequency minus the frequency: the spectrum arrives mirrored, and Radiolack turns it back.',
};

/** Shown under the oscillator's frequency. */
export const OSCILLATOR_NOTE = 'Its true frequency, such as 125 MHz or 9750 MHz: the correction in ppm is the receiver\'s, not the converter\'s.';

/**
 * The Receiver menu's converter controls for a source state.
 * @typedef {object} ConverterView
 * @property {boolean} shown       the source takes a converter
 * @property {Converter | ''} value the converter chosen, '' without one
 * @property {string} note          what the converter chosen does
 * @property {string} oscillator    the oscillator's frequency as the field shows it, '' without one
 */

/**
 * @param {string | null} value
 * @returns {value is Converter}
 */
const isConverter = (value) => CONVERTERS.some((converter) => converter === value);

/**
 * The converter controls for ``source``: hidden for a source without a
 * converter, such as the simulated band, an engine older than the
 * converter, or while none is known.
 * @param {import('./stream.js').SourceState | null} source
 * @returns {ConverterView}
 */
export function converterView(source) {
  const converter = source ? source.converter : null;
  if (!source || !isConverter(converter) || source.converterLoHz === null) {
    return { shown: false, value: '', note: '', oscillator: '' };
  }
  return {
    shown: true, value: converter, note: CONVERTER_NOTES[converter], oscillator: formatFrequency(source.converterLoHz),
  };
}

/**
 * The oscillator's frequency typed in its field, in whole hertz, as the
 * frequency reader takes it (125, 9750, 9.75 GHz); null when the text is
 * not a frequency, or the one in use, which changes nothing.
 * @param {string} text
 * @param {number | null} inUseHz
 * @returns {number | null}
 */
export function oscillatorChange(text, inUseHz) {
  const hz = parseFrequency(text);
  return hz === null || hz === inUseHz ? null : hz;
}

/**
 * The converter of a source state, as a kind and its oscillator, or null
 * without one: the oscillator does nothing then.
 * @param {import('./stream.js').SourceState} source
 * @returns {{ kind: Converter, loHz: number } | null}
 */
function conversionOf(source) {
  const kind = source.converter;
  if (!isConverter(kind) || kind === 'none' || source.converterLoHz === null) return null;
  return { kind, loHz: source.converterLoHz };
}

/**
 * Whether the frequencies moved from one source state to the next, the
 * same receiver behind another converter or oscillator.
 * @param {import('./stream.js').SourceState} before
 * @param {import('./stream.js').SourceState} after
 * @returns {boolean}
 */
export function conversionMoved(before, after) {
  const a = conversionOf(before), b = conversionOf(after);
  return a?.kind !== b?.kind || a?.loHz !== b?.loHz;
}

/**
 * Where a frequency of ``before`` is in ``after``, through the frequency
 * the receiver tunes, which stays: the same signal, at its new frequency.
 * @param {number} f
 * @param {import('./stream.js').SourceState} before
 * @param {import('./stream.js').SourceState} after
 * @returns {number}
 */
export function convertedFrequency(f, before, after) {
  const a = conversionOf(before), b = conversionOf(after);
  let tuned = f;
  if (a) tuned = a.kind === 'up' ? f + a.loHz : a.kind === 'down' ? f - a.loHz : a.loHz - f;
  if (!b) return tuned;
  return b.kind === 'up' ? tuned - b.loHz : b.kind === 'down' ? tuned + b.loHz : b.loHz - tuned;
}
