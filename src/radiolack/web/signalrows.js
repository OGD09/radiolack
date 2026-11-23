/*
 * The Signals list's rows: one per channel that the signals show on, as the
 * History's rows are, however many of the engine's signals lie there. A
 * broadcast station, for instance, may give a narrow signal and a wide one,
 * farther apart than the engine takes for one, on the same channel.
 */

/** @typedef {import('./stream.js').Peak} Peak */

/**
 * @typedef {object} SignalRow
 * @property {number} channelHz         the frequency the row shows
 * @property {Peak} shown
 *   the signal whose margin and time the row shows: the strongest active one, or else the one found latest
 * @property {number[]} frequenciesHz   the engine's frequencies of the row's signals, lowest first: Ignore sends each
 */

/**
 * Whether signal ``a`` rather than ``b`` speaks for their row.
 * @param {Peak} a
 * @param {Peak} b
 * @returns {boolean}
 */
function speaksBefore(a, b) {
  if (a.active !== b.active) return a.active;
  return a.active ? a.marginDb > b.marginDb : a.ageS < b.ageS;
}

/**
 * The rows of ``peaks``, the lowest channel first.
 * @param {readonly Peak[]} peaks
 * @returns {SignalRow[]}
 */
export function signalRows(peaks) {
  /** @type {Map<number, Peak[]>} */
  const byChannel = new Map();
  for (const peak of peaks) {
    const group = byChannel.get(peak.channelHz);
    if (group) group.push(peak);
    else byChannel.set(peak.channelHz, [peak]);
  }
  return [...byChannel].map(([channelHz, group]) => ({
    channelHz,
    shown: group.reduce((best, peak) => (speaksBefore(peak, best) ? peak : best)),
    frequenciesHz: group.map((peak) => peak.frequencyHz).sort((a, b) => a - b),
  })).sort((a, b) => a.channelHz - b.channelHz);
}
