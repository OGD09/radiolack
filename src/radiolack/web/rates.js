/*
 * What a demodulation mode needs of the receiver's sample rate: WFM's
 * channel is 320 kHz wide, more than librtlsdr's lower range of rates gives.
 */

/** WFM's channel rate, Hz (demod.py): a slower receiver plays nothing in WFM. */
export const WFM_CHANNEL_RATE_HZ = 320000;

/** Why WFM plays nothing at a lower rate. */
export const WFM_RATE_NOTE = 'WFM needs a sample rate of 1.024 MS/s or more';

/**
 * Why ``mode`` plays nothing at ``rateHz``, or '' when it plays.
 * @param {string} mode
 * @param {number} rateHz
 * @returns {string}
 */
export function modeRateNote(mode, rateHz) {
  return mode === 'WFM' && rateHz < WFM_CHANNEL_RATE_HZ ? WFM_RATE_NOTE : '';
}
