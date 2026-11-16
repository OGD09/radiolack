/*
 * The band plan's named channels and known frequencies (spots): which channel
 * or spot a frequency is on, how the Automatic step moves along a set's
 * channels, and what the band strip draws of them in a view. Sets and spots
 * come sorted by frequency (bandplan.js), and every lookup bisects: nothing
 * here walks the whole plan, and the strip draws at most one channel every
 * TICK_MIN_PX.
 */

import { rangeAt } from './bandplan.js';

/** @typedef {import('./bandplan.js').Channel} Channel */
/** @typedef {import('./bandplan.js').ChannelSet} ChannelSet */
/** @typedef {import('./bandplan.js').Spot} Spot */
/** @typedef {import('./bandplan.js').PlanBand} PlanBand */
/**
 * What these lookups read of the band plan.
 * @typedef {{ bands: readonly PlanBand[], channels: readonly ChannelSet[], spots: readonly Spot[] }} ChannelPlan
 */

/** A frequency within this of a spot is on it, Hz. */
export const SPOT_TOLERANCE_HZ = 50;
/** The strip draws a set's channels as ticks once they lie this far apart, CSS px. */
export const TICK_MIN_PX = 6;
/** And their numbers once they lie this far apart, CSS px. */
export const NUMBER_MIN_PX = 28;
/** A set's channel more than this many spacings from the next one is not followed by the step keys. */
const STEP_GAP_SPACINGS = 2;

/**
 * A channel and its set.
 * @typedef {object} OnChannel
 * @property {ChannelSet} set
 * @property {Channel} channel
 */

/**
 * A channel's label: "ch 3", "RV48", "block 5A".
 * @param {ChannelSet} set
 * @param {Channel} channel
 * @returns {string}
 */
export function channelLabel(set, channel) {
  return set.label.replaceAll('{n}', channel.n);
}

/**
 * A channel's full name: "PMR446 · ch 3".
 * @param {ChannelSet} set
 * @param {Channel} channel
 * @returns {string}
 */
export function channelName(set, channel) {
  return `${set.name} · ${channelLabel(set, channel)}`;
}

/**
 * A known frequency's full name, after its band, as names repeat from band
 * to band: "2 m amateur · SSB centre".
 * @param {readonly PlanBand[]} bands
 * @param {Spot} spot
 * @returns {string}
 */
export function spotName(bands, spot) {
  const band = rangeAt(bands, spot.f);
  return band ? `${band.name} · ${spot.name}` : spot.name;
}

/**
 * The index of the item nearest f, by bisection; -1 for an empty list. Of two
 * at the same distance, the lower one.
 * @param {ReadonlyArray<{ f: number }>} items  sorted by f
 * @param {number} f  Hz
 * @returns {number}
 */
export function nearestIndex(items, f) {
  if (items.length === 0) return -1;
  const above = firstAtOrAbove(items, f);     // the first at or above f: the nearest is it or the one below
  if (above === items.length) return above - 1;
  if (above > 0 && f - items[above - 1].f <= items[above].f - f) return above - 1;
  return above;
}

/**
 * The indices of the items from f0 to f1, both included, by bisection: from
 * `first` to `end`, excluded.
 * @param {ReadonlyArray<{ f: number }>} items  sorted by f
 * @param {number} f0  Hz
 * @param {number} f1  Hz
 * @returns {[number, number]}
 */
export function itemsInView(items, f0, f1) {
  return [firstAtOrAbove(items, f0), firstAbove(items, f1)];
}

/**
 * The channel f is on: the nearest channel of the set where f applies, if it
 * lies within half the set's spacing; null elsewhere.
 * @param {readonly ChannelSet[]} sets  sorted, without overlaps
 * @param {number} f  Hz
 * @returns {OnChannel | null}
 */
export function channelOn(sets, f) {
  const set = rangeAt(sets, f);
  if (set === null) return null;
  const channel = set.channels[nearestIndex(set.channels, f)];
  return Math.abs(channel.f - f) <= set.spacing / 2 ? { set, channel } : null;
}

/**
 * The spot f is on: the nearest within SPOT_TOLERANCE_HZ, or null.
 * @param {readonly Spot[]} spots  sorted by f
 * @param {number} f  Hz
 * @returns {Spot | null}
 */
export function spotAt(spots, f) {
  const i = nearestIndex(spots, f);
  return i >= 0 && Math.abs(spots[i].f - f) <= SPOT_TOLERANCE_HZ ? spots[i] : null;
}

/**
 * What the line under the frequency names at f: the spot f is on, after its
 * band, else the channel, after its set; null on neither.
 * @param {ChannelPlan} plan
 * @param {number} f  Hz
 * @returns {string | null}
 */
export function nameAt(plan, f) {
  const spot = spotAt(plan.spots, f);
  if (spot) return spotName(plan.bands, spot);
  const on = channelOn(plan.channels, f);
  return on ? channelName(on.set, on.channel) : null;
}

/**
 * The demodulator a click on f selects from its spot or its channel's set,
 * or null where neither names one.
 * @param {ChannelPlan} plan
 * @param {number} f  Hz
 * @returns {string | null}
 */
export function modeAt(plan, f) {
  const spot = spotAt(plan.spots, f);
  if (spot) return spot.mode;
  const on = channelOn(plan.channels, f);
  return on ? on.set.mode : null;
}

/**
 * The set whose spacing the Automatic step takes at f: the set where f
 * applies, when one of its channels lies less than a spacing from f. Between
 * channels far apart, such as the maritime ship and coast channels, the
 * range's own step applies.
 * @param {readonly ChannelSet[]} sets  sorted, without overlaps
 * @param {number} f  Hz
 * @returns {ChannelSet | null}
 */
export function stepSetAt(sets, f) {
  const set = rangeAt(sets, f);
  if (set === null) return null;
  return Math.abs(set.channels[nearestIndex(set.channels, f)].f - f) < set.spacing ? set : null;
}

/**
 * Where a click on f lands with a set's step: its nearest channel.
 * @param {ChannelSet} set
 * @param {number} f  Hz
 * @returns {number}
 */
export function snapToChannel(set, f) {
  return set.channels[nearestIndex(set.channels, f)].f;
}

/**
 * Where `steps` presses of the step keys go from f along a set's channels:
 * channel by channel, the first press to the nearest channel when it lies
 * ahead. Where the next channel lies more than STEP_GAP_SPACINGS spacings on,
 * or none does, the remaining presses move a spacing each, off the channels.
 * @param {ChannelSet} set
 * @param {number} f      Hz
 * @param {number} steps  presses, negative downwards
 * @returns {number}
 */
export function stepChannels(set, f, steps) {
  const { channels, spacing } = set, dir = Math.sign(steps);
  let left = Math.abs(steps), i = nearestIndex(channels, f);
  if (dir === 0) return f;
  if (channels[i].f !== f && Math.sign(channels[i].f - f) === dir) left -= 1;   // the nearest lies ahead
  while (left > 0) {
    const next = channels[i + dir];
    if (!next || Math.abs(next.f - channels[i].f) > STEP_GAP_SPACINGS * spacing) return channels[i].f + dir * left * spacing;
    i += dir;
    left -= 1;
  }
  return channels[i].f;
}

/**
 * A set as the strip draws it in a view: its channels from `first` to `end`,
 * excluded, as ticks, and their numbers when `numbers`.
 * @typedef {object} StripSet
 * @property {ChannelSet} set
 * @property {number} first
 * @property {number} end
 * @property {boolean} numbers
 */

/**
 * The sets whose channels the strip draws in a view: those in view whose
 * spacing reaches TICK_MIN_PX, with the channels in view. Sets that overlap
 * the view are found by bisection.
 * @param {readonly ChannelSet[]} sets  sorted, without overlaps
 * @param {number} f0        the view's lower edge, Hz
 * @param {number} f1        its upper edge, Hz
 * @param {number} pxPerHz   CSS px per Hz
 * @returns {StripSet[]}
 */
export function stripSets(sets, f0, f1, pxPerHz) {
  /** @type {StripSet[]} */
  const out = [];
  let lo = 0, hi = sets.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sets[mid].to > f0) hi = mid; else lo = mid + 1;
  }
  for (let i = lo; i < sets.length && sets[i].from < f1; i += 1) {
    const set = sets[i], px = set.spacing * pxPerHz;
    if (px < TICK_MIN_PX) continue;
    const [first, end] = itemsInView(set.channels, f0, f1);
    if (first < end) out.push({ set, first, end, numbers: px >= NUMBER_MIN_PX });
  }
  return out;
}

/**
 * Lays out labels left to right, as the bookmarks' marks are: each label,
 * centred on its x, shows unless it reaches within `gap` of the one before
 * or of a place already taken.
 * @template T
 * @param {ReadonlyArray<{ x: number, width: number, item: T }>} labels  sorted by x
 * @param {ReadonlyArray<readonly [number, number]>} taken  places already taken, [left, right]
 * @param {number} gap
 * @returns {Array<{ x: number, width: number, item: T }>}
 */
export function fitLabels(labels, taken, gap) {
  const shown = [];
  let right = -Infinity;
  for (const label of labels) {
    const a = label.x - label.width / 2, b = label.x + label.width / 2;
    if (a < right + gap) continue;
    if (taken.some(([l, r]) => a < r + gap && b > l - gap)) continue;
    shown.push(label);
    right = b;
  }
  return shown;
}

/**
 * A channel's tick on the strip.
 * @typedef {object} StripTick
 * @property {number} x  px
 * @property {ChannelSet} set
 * @property {Channel} channel
 */

/**
 * A known frequency's mark on the strip.
 * @typedef {object} StripSpot
 * @property {number} x  px
 * @property {Spot} spot
 */

/**
 * What the band strip draws of the channels and known frequencies in a view,
 * in the strip's px: ticks for the channels, their numbers where they fit, a
 * mark for each spot and its name where it fits. Names take their places
 * before numbers, after the ranges' names (`taken`), left to right.
 * @typedef {object} StripLayout
 * @property {StripTick[]} ticks
 * @property {Array<{ x: number, width: number, item: StripTick }>} numbers
 * @property {StripSpot[]} spots
 * @property {Array<{ x: number, width: number, item: StripSpot }>} names
 * @property {number} tickGap  px between the closest ticks drawn, Infinity without ticks
 */

/**
 * Lays out the channels and spots in the view [f0, f1] on a strip `width` px
 * wide. At most one channel every TICK_MIN_PX across the strip, and the spots
 * in view, are visited.
 * @param {ChannelPlan} plan
 * @param {number} f0      the view's lower edge, Hz
 * @param {number} f1      its upper edge, Hz
 * @param {number} width   the strip's width, px
 * @param {number} scale   px per CSS px
 * @param {(text: string) => number} measure  a text's width, px
 * @param {ReadonlyArray<readonly [number, number]>} taken  places the ranges' names take, px
 * @returns {StripLayout}
 */
export function stripLayout(plan, f0, f1, width, scale, measure, taken) {
  /** @type {StripLayout} */
  const out = { ticks: [], numbers: [], spots: [], names: [], tickGap: Infinity };
  if (!(f1 > f0) || !(width > 0)) return out;
  const pxPerHz = width / (f1 - f0), xOf = (/** @type {number} */ f) => (f - f0) * pxPerHz;
  const gap = 4 * scale;
  /** @type {Array<{ x: number, width: number, item: StripTick }>} */
  const numbers = [];
  for (const { set, first, end, numbers: numbered } of stripSets(plan.channels, f0, f1, pxPerHz / scale)) {
    out.tickGap = Math.min(out.tickGap, set.spacing * pxPerHz);
    for (let i = first; i < end; i += 1) {
      const tick = { x: xOf(set.channels[i].f), set, channel: set.channels[i] };
      out.ticks.push(tick);
      if (numbered) numbers.push({ x: tick.x, width: measure(tick.channel.n), item: tick });
    }
  }
  const [first, end] = itemsInView(plan.spots, f0, f1);
  /** @type {Array<{ x: number, width: number, item: StripSpot }>} */
  const names = [];
  for (let i = first; i < end; i += 1) {
    const mark = { x: xOf(plan.spots[i].f), spot: plan.spots[i] };
    out.spots.push(mark);
    names.push({ x: mark.x, width: measure(mark.spot.name), item: mark });
  }
  out.names = fitLabels(names, taken, gap);
  const named = out.names.map(({ x, width: w }) => /** @type {[number, number]} */ ([x - w / 2, x + w / 2]));
  out.numbers = fitLabels(numbers, [...taken, ...named], gap);
  return out;
}

/**
 * What a press at x on the strip lands on, as drawn there: in the strip's
 * upper half, a spot's mark within `tolerance` px; in its lower half, a
 * channel's tick within it, while ticks lie at least twice the tolerance
 * apart, so that a press never falls between two; else, in either half, a
 * spot's name or a channel's number drawn under the press. Null elsewhere:
 * the press is the range's.
 * @param {readonly PlanBand[]} bands  the plan's, for the spots' names
 * @param {StripLayout} layout
 * @param {number} x          px
 * @param {boolean} upper     whether the press is in the strip's upper half
 * @param {number} tolerance  px
 * @returns {{ f: number, name: string, mode: string | null, note: string | null } | null}
 */
export function stripHit(bands, layout, x, upper, tolerance) {
  const near = (/** @type {{ x: number }} */ item) => Math.abs(item.x - x) <= tolerance;
  const on = (/** @type {{ x: number, width: number }} */ label) => Math.abs(label.x - x) <= label.width / 2;
  /** @type {Spot | null} */
  let spot = null;
  /** @type {StripTick | null} */
  let tick = null;
  if (upper) spot = nearestTo(layout.spots.filter(near), x)?.spot ?? null;
  else if (layout.tickGap >= 2 * tolerance) tick = layout.ticks.find(near) ?? null;   // at most one so near
  if (!spot && !tick) spot = layout.names.find(on)?.item.spot ?? null;
  if (!spot && !tick) tick = layout.numbers.find(on)?.item ?? null;
  if (spot) return { f: spot.f, name: spotName(bands, spot), mode: spot.mode, note: null };
  if (tick) return { f: tick.channel.f, name: channelName(tick.set, tick.channel), mode: tick.set.mode, note: tick.channel.note };
  return null;
}

/**
 * The item nearest x, or undefined for none.
 * @template {{ x: number }} T
 * @param {readonly T[]} items
 * @param {number} x
 * @returns {T | undefined}
 */
function nearestTo(items, x) {
  let best;
  for (const item of items) if (!best || Math.abs(item.x - x) < Math.abs(best.x - x)) best = item;
  return best;
}

/**
 * The first index whose item's f is at or above f, by bisection.
 * @param {ReadonlyArray<{ f: number }>} items  sorted by f
 * @param {number} f
 * @returns {number}
 */
function firstAtOrAbove(items, f) {
  let lo = 0, hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].f < f) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/**
 * The first index whose item's f lies above f, by bisection.
 * @param {ReadonlyArray<{ f: number }>} items  sorted by f
 * @param {number} f
 * @returns {number}
 */
function firstAbove(items, f) {
  let lo = 0, hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].f <= f) lo = mid + 1; else hi = mid;
  }
  return lo;
}
