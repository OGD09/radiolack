/*
 * The Receiver menu's Calibrate dialog. On the stations: the engine's
 * automatic calibration (calibration.py) tunes to each standard-frequency
 * station within reach and proposes the correction they agree on, which the
 * dialog shows with what it measured at each, for every interface, as the
 * receiver is shared. On a known carrier: the engine measures the carrier
 * at the VFO (carrier.py); the user types its true frequency, and the
 * dialog shows the frequency correction that brings the carrier there,
 * beside the one in use. Either is applied as any change of the
 * correction. On an R820T or R828D, the dialog first has the engine check
 * which computation the rtl-sdr driver follows on that carrier
 * (pllcheck.py), so that the correction it offers comes from a carrier
 * labelled by the computation found.
 */

import { formatFrequency, parseFrequency } from './frequency.js';
import { tunedFrequency } from './converter.js';
import { ppmText } from './correction.js';
import { DRIVER_PLL_NAMES, PLL_TUNERS, isDriverPll } from './tunerpll.js';

/** The corrections a receiver takes, in ppm (correction.py). */
export const PPM_RANGE = /** @type {const} */ ([-100, 100]);
/** A change of the correction larger than this, in ppm, is more than a crystal drifts: the dialog warns. */
export const LARGE_CHANGE_PPM = 10;
/** The dialog asks for a measure this often while it is open, in ms. */
export const MEASURE_EVERY_MS = 2000;
/** A VFO this close to the centre frequency, in Hz, lies where removing the DC spike takes a carrier out. */
const DC_CLEARANCE_HZ = 100;

/**
 * Why the engine gives no frequency, for the user.
 * @type {Readonly<Record<string, string>>}
 */
export const REASONS = {
  silent: 'Nothing is received at the VFO: it lies outside the span, or WFM needs a sample rate of 1.024 MS/s or more.',
  no_signal: 'No steady carrier stands out within the filter: tune the VFO onto one.',
  several: 'Two lines of nearly equal strength within the filter: narrow the filter, or move the VFO onto one.',
  unstable: 'The line moved between two measures: it is not a steady carrier, or the VFO or the receiver changed.',
};

/**
 * The correction, in ppm, that brings a carrier measured at ``measuredHz``,
 * with ``ppmInUse``, to ``trueHz``, both on the air: unrounded, NaN when a
 * frequency the receiver would tune is not above 0 Hz.
 *
 * The derivation. librtlsdr takes a correction c to mean that the crystal
 * runs at its nominal frequency times (1 + c / 10⁶), and every frequency the
 * receiver tunes, and its sample rate, come from the crystal (correction.py).
 * Let the crystal truly run at its nominal frequency times k. With the
 * correction p in use, the receiver asked for T truly tunes T × k / (1 + p),
 * p in parts per one, and a signal truly at T_s, as the receiver tunes it,
 * shows at T_s × (1 + p) / k: every frequency scales alike. So a carrier
 * measured at T_m whose true frequency is T_s gives k = T_s × (1 + p) / T_m,
 * and the correction that makes every frequency true, k / 1 = 1 + p', is
 *
 *     p' = (1 + p) × T_s / T_m − 1.
 *
 * A crystal that runs fast (k > 1) shows a carrier too low (T_m < T_s) and
 * needs a positive correction, as librtlsdr's sign has it. T is the
 * frequency the receiver tunes, not the one on the air: behind a converter,
 * whose oscillator is taken as exact, the crystal scales the receiver's own
 * frequencies only. The receiver corrects its sample rate by the whole part
 * of p only, which moves a carrier off the centre frequency by at most half
 * a ppm of its distance from it: 0.6 Hz at the edge of a 2.4 MS/s span.
 * @param {number} ppmInUse
 * @param {number} measuredHz
 * @param {number} trueHz
 * @param {import('./stream.js').SourceState} source  its converter, as the measure was taken
 * @returns {number}
 */
export function calibratedPpm(ppmInUse, measuredHz, trueHz, source) {
  const measured = tunedFrequency(measuredHz, source), wanted = tunedFrequency(trueHz, source);
  if (!(measured > 0 && wanted > 0)) return NaN;
  return ((1 + ppmInUse / 1e6) * wanted / measured - 1) * 1e6;
}

/**
 * A measured frequency in MHz, to the tenth of a hertz: "10.0000013".
 * @param {number} hz
 * @returns {string}
 */
export function measuredText(hz) {
  return (hz / 1e6).toFixed(7);
}

/**
 * The true frequency the dialog starts from: the VFO's, rounded to the
 * kilohertz, as a known carrier usually is.
 * @param {number} vfoHz
 * @returns {string}
 */
export function startingTrueText(vfoHz) {
  return formatFrequency(Math.max(1000, Math.round(vfoHz / 1000) * 1000));
}

/**
 * The Receiver menu's Calibrate entry: whether it opens, and why not.
 * @typedef {object} CalibrateEntry
 * @property {boolean} enabled
 * @property {string} note  why it is disabled, or ""
 */

/**
 * @param {import('./stream.js').SourceState | null} source
 * @param {string[]} measures  what the engine measures, as its hello listed it
 * @param {boolean} open       the stream is open
 * @returns {CalibrateEntry}
 */
export function calibrateEntry(source, measures, open) {
  if (!open || !source) return { enabled: false, note: '' };
  if (!measures.includes('carrier')) return { enabled: false, note: 'Calibrating needs Radiolack to restart.' };
  if (source.ppm === null || !source.settable.includes('ppm')) {
    const note = source.kind === 'synthetic'
      ? 'The simulated band has no frequency correction to calibrate.'
      : 'This receiver has no frequency correction to calibrate.';
    return { enabled: false, note };
  }
  return { enabled: true, note: '' };
}

/**
 * Why a check of the rtl-sdr driver named no computation, for the user.
 * @type {Readonly<Record<string, string>>}
 */
export const DRIVER_CHECK_REASONS = {
  receiver: 'The driver is identified on an RTL-SDR whose tuner is an R820T or R828D.',
  direct: 'The driver is identified on the tuner, not in direct sampling.',
  range: 'The driver is identified where Radiolack places the tuner on its grid: not below about 26 MHz, '
    + 'nor below 28.8 MHz on an R828D.',
  scan: 'A scan runs, and the check would move the centre frequency.',
  iq: 'An I/Q recording runs, and the check would move the centre frequency.',
  position: 'The VFO lies too near the centre frequency or the edge of the span for the check: move the '
    + 'centre frequency.',
  indistinct: 'The two computations cannot be told apart near this frequency.',
  lost: 'The carrier was not measured steadily at every centre frequency, or the VFO receives nothing.',
  neither: 'The carrier followed neither computation: the driver computes otherwise, or the carrier moved.',
  both: 'The carrier followed both computations.',
  stopped: 'The receiver or the VFO changed during the check.',
};

/**
 * Why a driver check answered with ``reason`` is asked again at the next
 * carrier measured rather than shown: the receiver or the VFO changed
 * during it, or the calibration on the stations ran.
 * @param {string | null} reason
 * @returns {boolean}
 */
export function driverCheckAgain(reason) {
  return reason === 'stopped' || reason === 'calibration';
}

/**
 * The dialog's check of the rtl-sdr driver: "idle" while it asks for none;
 * "due" once it will ask, at the first carrier measured; "running" while
 * the answer is on its way; "done" with the answer.
 * @typedef {object} DriverCheckState
 * @property {'idle' | 'due' | 'running' | 'done'} state
 * @property {import('./stream.js').DriverCheck | null} answer  with "done"
 */

/**
 * Whether the dialog checks the rtl-sdr driver for ``source``: an engine
 * that can, and a receiver with an R820T or R828D, on its tuner, whose
 * driver setting the interface may change.
 * @param {import('./stream.js').SourceState | null} source
 * @param {string[]} measures  what the engine measures, as its hello listed it
 * @returns {boolean}
 */
export function driverCheckWanted(source, measures) {
  return measures.includes('driver_pll') && source !== null && PLL_TUNERS.includes(source.tuner ?? '')
    && !source.direct && isDriverPll(source.driverPll) && source.settable.includes('driver_pll');
}

/**
 * What the dialog says of the rtl-sdr driver, for ``source`` and the
 * dialog's ``check``: the computation set, and what the check does or found.
 * @param {DriverCheckState} check
 * @param {import('./stream.js').SourceState} source
 * @returns {string}
 */
export function driverCheckText(check, source) {
  const current = isDriverPll(source.driverPll) ? DRIVER_PLL_NAMES[source.driverPll] : '';
  if (check.state === 'running') return 'Identifying: the centre frequency moves for about ten seconds.';
  if (check.state === 'due') {
    return `${current}. Identified once a steady carrier is measured: the centre frequency then moves for about ten `
      + 'seconds.';
  }
  const answer = check.state === 'done' ? check.answer : null;
  if (answer === null) return current ? `${current}.` : '';
  const found = answer.driverPll;
  if (found !== null && isDriverPll(found)) {
    const name = DRIVER_PLL_NAMES[found];
    if (answer.previous === found) return `${name}: confirmed.`;
    if (isDriverPll(answer.previous) && answer.previous !== 'unknown') {
      return `${name}: identified, and saved with the receiver. It changed: it was `
        + `${DRIVER_PLL_NAMES[answer.previous]}, as the driver was upgraded or another one runs.`;
    }
    return `${name}: identified, and saved with the receiver.`;
  }
  const why = answer.reason !== null && Object.hasOwn(DRIVER_CHECK_REASONS, answer.reason)
    ? DRIVER_CHECK_REASONS[answer.reason] : 'The check failed.';
  return current ? `Not identified. ${why} The setting stays: ${current}.` : `Not identified. ${why}`;
}

/**
 * Whether the dialog notes that the result also takes in the tuner's own
 * error: on the tuner of a receiver that could sample directly, unless the
 * tuner is an R820T or R828D whose driver's computation is known, which
 * labels the samples where the tuner truly sits.
 * @param {import('./stream.js').SourceState} source
 * @returns {boolean}
 */
export function tunerNoteShown(source) {
  if (source.direct || !source.hfInputs.some((input) => input === 'q' || input === 'i')) return false;
  const known = PLL_TUNERS.includes(source.tuner ?? '') && isDriverPll(source.driverPll)
    && source.driverPll !== 'unknown';
  return !known;
}

/**
 * What the dialog shows.
 * @typedef {object} CalibrationView
 * @property {string} measured   the carrier measured, or why not
 * @property {string} inUse      the correction in use
 * @property {string} correction the correction that brings the carrier to its true frequency, or "—"
 * @property {string} note       a warning or a refusal, or ""
 * @property {number | null} apply  the correction Apply sends; null: Apply is disabled
 */

/**
 * The dialog for the latest ``measure``, null while the first is on its
 * way, and the true frequency typed, ``trueText``. While ``holding``, as
 * the rtl-sdr driver is being identified, no correction is offered: the
 * measure may still be labelled by the computation set before.
 * @param {object} state
 * @param {import('./stream.js').CarrierMeasure | null} state.measure
 * @param {string} state.trueText
 * @param {import('./stream.js').SourceState} state.source
 * @param {number} state.vfoHz
 * @param {boolean} [state.holding]
 * @returns {CalibrationView}
 */
export function calibrationView({ measure, trueText, source, vfoHz, holding = false }) {
  const ppmInUse = source.ppm ?? 0;
  /** @type {CalibrationView} */
  const view = { measured: 'Measuring…', inUse: `${ppmText(ppmInUse)} ppm`, correction: '—', note: '', apply: null };
  const trueHz = parseFrequency(trueText);
  if (measure !== null) {
    if (measure.frequencyHz === null) {
      let text = measure.reason !== null && Object.hasOwn(REASONS, measure.reason)
        ? REASONS[measure.reason] : 'The carrier could not be measured.';
      if (measure.reason === 'no_signal' && measure.marginDb !== null) {
        text += ` The strongest line stands ${measure.marginDb.toFixed(0)} dB above the noise, 20 dB is needed.`;
      }
      if (measure.reason === 'no_signal' && source.dcRemoval && Math.abs(vfoHz - source.centreHz) < DC_CLEARANCE_HZ) {
        text += ' A carrier on the centre frequency goes with the DC spike: move the centre frequency a little.';
      }
      view.measured = text;
    } else {
      const margin = measure.marginDb === null ? '' : ` · ${measure.marginDb.toFixed(0)} dB above the noise`;
      view.measured = `${measuredText(measure.frequencyHz)} MHz${margin}`;
    }
  }
  if (trueHz === null) {
    view.note = 'Type the carrier\'s true frequency, such as 10 MHz or 9996 kHz.';
    return view;
  }
  if (measure === null || measure.frequencyHz === null) return view;
  if (holding) {
    view.note = 'The correction comes once the rtl-sdr driver is identified.';
    return view;
  }
  const offset = measure.frequencyHz - trueHz;
  view.measured += `, ${Math.abs(offset).toFixed(1)} Hz ${offset >= 0 ? 'above' : 'below'} its true frequency`;
  const exact = calibratedPpm(ppmInUse, measure.frequencyHz, trueHz, source);
  if (!Number.isFinite(exact)) {
    view.note = 'The receiver cannot tune this true frequency behind its converter: check it.';
    return view;
  }
  const ppm = Math.round(exact * 100) / 100 || 0;   // no "-0.00"
  view.correction = `${ppmText(ppm)} ppm`;
  const [low, high] = PPM_RANGE;
  if (ppm < low || ppm > high) {
    view.note = `This needs a correction beyond the ${low} to ${high} ppm a receiver takes: check the true `
      + 'frequency, and that the VFO is on that carrier.';
  } else if (ppm === ppmInUse) {
    view.note = 'The carrier is at its true frequency: nothing to correct.';
  } else {
    view.apply = ppm;
    if (Math.abs(ppm - ppmInUse) > LARGE_CHANGE_PPM) {
      view.note = `This moves the correction by ${ppmText(Math.abs(ppm - ppmInUse))} ppm, more than a crystal `
        + 'drifts: check the true frequency, and that the VFO is on that carrier.';
    }
  }
  return view;
}

/**
 * Why the calibration on the stations proposes nothing, for the user.
 * @type {Readonly<Record<string, string>>}
 */
export const RUN_REASONS = {
  none: 'No station was received: try again at another hour, as propagation allows, or check the antenna.',
  weak: 'One station alone was received, less than 30 dB above the noise: too weak to rely on. Try again at '
    + 'another hour.',
  disagree: 'The stations received do not agree within 0.3 ppm: try again at another hour.',
  range: 'The stations agree on a correction beyond the -100 to 100 ppm a receiver takes: check the antenna.',
};

/**
 * Why the calibration on the stations ended before its last station, for
 * the user; "done" and "stopped" say how many stations it measured.
 * @type {Readonly<Record<string, string>>}
 */
export const RUN_ENDINGS = {
  tuned: 'Stopped: the receiver or the VFO was changed.',
  source: 'Stopped: another receiver was chosen.',
  scan: 'Stopped: a scan started.',
  recording: 'Stopped: a recording started.',
};

/**
 * Why a station gave no carrier, for the stations' table.
 * @type {Readonly<Record<string, string>>}
 */
export const STATION_REASONS = {
  silent: 'nothing received',
  no_signal: 'no carrier',
  several: 'two lines',
  unstable: 'unsteady',
  refused: 'not tuned',
};

/**
 * A station's row in the dialog's table.
 * @typedef {object} StationRow
 * @property {string} station  its frequency, in MHz
 * @property {string} carrier  the carrier's offset from the station, why there is none, "Measuring…" or ""
 * @property {string} margin   "38 dB", or ""
 * @property {string} ppm      the correction it implies, or ""
 * @property {boolean} apart   received, but not among the stations the proposal comes from
 */

/**
 * What the dialog shows of the calibration on the stations.
 * @typedef {object} RunView
 * @property {string} button      "Calibrate on the stations", or "Stop" while it runs
 * @property {boolean} enabled    the button sends
 * @property {string} status      its progress, or how it ended, or ""
 * @property {StationRow[]} rows  one per station, empty before the first calibration
 * @property {string} proposal    the correction proposed, or "—"
 * @property {string} note        why it cannot start, why it proposes nothing, or what to know of the proposal
 * @property {number | null} apply  the correction Apply sends; null: Apply is disabled
 */

/**
 * The calibration on the stations, from the engine's ``state``, null until
 * it comes, for ``source``; ``recording`` while an audio or I/Q recording
 * runs, which keeps it from starting.
 * @param {object} context
 * @param {import('./stream.js').CalibrationState | null} context.state
 * @param {import('./stream.js').SourceState} context.source
 * @param {string[]} context.measures  what the engine measures, as its hello listed it
 * @param {boolean} context.open       the stream is open
 * @param {boolean} context.recording
 * @returns {RunView}
 */
export function runView({ state, source, measures, open, recording }) {
  const running = state !== null && state.running;
  /** @type {RunView} */
  const view = { button: running ? 'Stop' : 'Calibrate on the stations', enabled: open, status: '', rows: [],
    proposal: '—', note: '', apply: null };
  if (!running) {
    if (!measures.includes('calibration')) view.note = 'Calibrating on the stations needs Radiolack to restart.';
    else if (!source.settable.includes('centre_hz')) {
      view.note = 'Calibrating on the stations needs a receiver that tunes.';
    } else if (source.converter && source.converter !== 'none') {
      view.note = 'A converter is set: its oscillator would pass for the crystal\'s error. Calibrate on a known '
        + 'carrier, or set the converter to None first.';
    } else if (recording) view.note = 'A recording runs: stop it before calibrating on the stations.';
    if (view.note) view.enabled = false;
  }
  if (state === null) return view;
  const { stations, results } = state;
  view.rows = stations.map((frequencyHz, index) => stationRow(frequencyHz, results[index] ?? null,
    running && index === results.length, state.proposal !== null));
  const received = results.filter((result) => result.ppm !== null).length;
  if (running) {
    view.status = `Station ${Math.min(results.length + 1, stations.length)} of ${stations.length}: the receiver `
      + 'and the VFO come back at the end.';
  } else if (state.ended === 'done') {
    view.status = `Done: ${received} of ${stations.length} stations received.`;
  } else if (state.ended === 'stopped') {
    view.status = `Stopped after ${results.length} of ${stations.length} stations: ${received} received.`;
  } else if (state.ended !== null) {
    view.status = Object.hasOwn(RUN_ENDINGS, state.ended) ? RUN_ENDINGS[state.ended] : 'Stopped.';
  }
  if (view.note) return view;
  if (state.proposal !== null) {
    const { ppm, stations: agreeing } = state.proposal;
    view.proposal = `${ppmText(ppm)} ppm`;
    if (ppm === source.ppm) {
      view.note = 'This is the correction in use: nothing to change.';
    } else {
      view.apply = ppm;
      view.note = agreeing === 1
        ? 'From one station alone, though well above the noise: confirm it with a second calibration if you can.'
        : `${agreeing} stations agree on it.`;
    }
  } else if (state.reason !== null) {
    view.note = Object.hasOwn(RUN_REASONS, state.reason) ? RUN_REASONS[state.reason] : 'Nothing is proposed.';
  }
  return view;
}

/**
 * @param {number} frequencyHz
 * @param {import('./stream.js').CalibrationResult | null} result  null: not measured yet
 * @param {boolean} measuring  the station being measured
 * @param {boolean} proposed   a proposal came, from the stations that agree
 * @returns {StationRow}
 */
function stationRow(frequencyHz, result, measuring, proposed) {
  /** @type {StationRow} */
  const row = { station: formatFrequency(frequencyHz), carrier: measuring ? 'Measuring…' : '', margin: '', ppm: '',
    apart: false };
  if (result === null) return row;
  row.margin = result.marginDb === null ? '' : `${result.marginDb.toFixed(0)} dB`;
  if (result.carrierHz === null || result.ppm === null) {
    row.carrier = result.reason !== null && Object.hasOwn(STATION_REASONS, result.reason)
      ? STATION_REASONS[result.reason] : 'not measured';
    return row;
  }
  const offset = result.carrierHz - frequencyHz;
  row.carrier = `${offset < 0 ? '−' : '+'}${Math.abs(offset).toFixed(1)} Hz`;
  row.ppm = ppmText(result.ppm);
  row.apart = proposed && !result.agrees;
  return row;
}
