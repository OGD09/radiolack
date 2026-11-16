/*
 * Shortwave with an RTL-SDR, in the Receiver menu: the input a receiver
 * uses below its tuner's range, what each one needs, and what direct
 * sampling changes in the menu while it runs.
 */

/**
 * What a receiver may use for shortwave: nothing, direct sampling on the
 * RTL2832's Q or I input, or a tuner that covers it.
 * @typedef {'none' | 'q' | 'i' | 'tuner'} HfInput
 */

/** Every input, in the order the menu lists them. @type {readonly HfInput[]} */
export const HF_INPUTS = ['none', 'q', 'i', 'tuner'];

/**
 * What each input needs, shown under the list for the one chosen.
 * @type {Readonly<Record<HfInput, string>>}
 */
export const HF_INPUT_NEEDS = {
  none: 'The tuner alone: nothing below its range, 24 MHz for most dongles.',
  q: 'Below the tuner\'s range, samples the antenna socket directly: an RTL-SDR Blog V3, or a dongle modified for it. Nothing below about 500 kHz.',
  i: 'Below the tuner\'s range, samples the I input directly: a dongle modified for it.',
  tuner: 'A tuner that covers shortwave by itself: an RTL-SDR Blog V4, with rtl-sdr 2.0 or later (not Debian 12\'s 0.6.0). A wrong choice leaves the band silent.',
};

/** Shown in place of the gain while the receiver samples directly. */
export const DIRECT_GAIN_NOTE = 'Direct sampling: no gain control';

/** Shown under the input while the receiver samples directly. */
export const MIRROR_NOTE = 'Sampling directly: signals above and below 14.4 MHz mirror each other.';

/**
 * The Receiver menu's shortwave controls for a source state.
 * @typedef {object} ShortwaveView
 * @property {boolean} shown        the source has a shortwave input
 * @property {HfInput[]} offered    the inputs the receiver offers
 * @property {HfInput | ''} value   the input chosen, '' without one
 * @property {string} note          what the input chosen needs, and the mirror while sampling directly
 * @property {boolean} direct       the receiver samples directly: no gain control
 * @property {boolean} rtlAgc       the RTL2832's AGC is on
 * @property {boolean} rtlAgcApplies the input chosen samples directly: the RTL2832's AGC applies to it
 */

/**
 * @param {string | null} value
 * @returns {value is HfInput}
 */
const isHfInput = (value) => HF_INPUTS.some((input) => input === value);

/**
 * The shortwave controls for ``source``: hidden for a source without a
 * shortwave input, such as the simulated band, or while none is known.
 * @param {import('./stream.js').SourceState | null} source
 * @returns {ShortwaveView}
 */
export function shortwaveView(source) {
  const hfInput = source ? source.hfInput : null;
  if (!source || !isHfInput(hfInput)) {
    return { shown: false, offered: [], value: '', note: '', direct: false, rtlAgc: false, rtlAgcApplies: false };
  }
  const offered = HF_INPUTS.filter((input) => source.hfInputs.includes(input));
  const note = source.direct ? `${HF_INPUT_NEEDS[hfInput]} ${MIRROR_NOTE}` : HF_INPUT_NEEDS[hfInput];
  return {
    shown: true, offered, value: hfInput, note, direct: source.direct, rtlAgc: source.rtlAgc === true,
    rtlAgcApplies: hfInput === 'q' || hfInput === 'i',
  };
}
