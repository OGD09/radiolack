/*
 * The Bookmarks menu's band plan list: the ranges in the view, each with the
 * known frequencies (spots) inside it, grouped by band; or, filtered, the
 * ranges, channel sets and spots of the whole plan whose names match, in
 * frequency order. At most PLAN_LIST_MAX rows: the view is found by
 * bisection and the walk stops there, however wide the view.
 */

import { itemsInView, spotName } from './channels.js';

/** @typedef {import('./bandplan.js').PlanRange} PlanRange */
/** @typedef {import('./bandplan.js').ChannelSet} ChannelSet */
/** @typedef {import('./bandplan.js').Spot} Spot */
/** @typedef {import('./bandplan.js').PlanBand} PlanBand */

/** The most rows the list shows. */
export const PLAN_LIST_MAX = 200;

/**
 * A row of the list: a band's name over its ranges, a range (by its index in
 * the plan), a channel set or a spot.
 * @typedef {{ kind: 'group', name: string }
 *   | { kind: 'range', index: number, range: PlanRange }
 *   | { kind: 'set', set: ChannelSet }
 *   | { kind: 'spot', spot: Spot }} PlanRow
 */

/**
 * The list's rows for the view [f0, f1], or for the filter `query` when it
 * is not empty; `more` when rows were left out past PLAN_LIST_MAX.
 * @param {{ ranges: readonly PlanRange[], bands: readonly PlanBand[], channels: readonly ChannelSet[], spots: readonly Spot[] }} plan
 * @param {number} f0     the view's lower edge, Hz
 * @param {number} f1     its upper edge, Hz
 * @param {string} query  the filter, as typed
 * @returns {{ rows: PlanRow[], more: boolean }}
 */
export function planRows(plan, f0, f1, query) {
  const q = query.trim().toLowerCase();
  return q ? matchingRows(plan, q) : rowsInView(plan, f0, f1);
}

/**
 * The ranges in view, a band's name over its segments, each followed by the
 * spots inside it and in view. A gap shows its spots alone.
 * @param {{ ranges: readonly PlanRange[], spots: readonly Spot[] }} plan
 * @param {number} f0
 * @param {number} f1
 * @returns {{ rows: PlanRow[], more: boolean }}
 */
function rowsInView({ ranges, spots }, f0, f1) {
  /** @type {PlanRow[]} */
  const rows = [];
  let group = null, lo = 0, hi = ranges.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ranges[mid].to > f0) hi = mid; else lo = mid + 1;
  }
  for (let i = lo; i < ranges.length && ranges[i].from < f1; i += 1) {
    const range = ranges[i];
    const [first, end] = itemsInView(spots, Math.max(range.from, f0), Math.min(range.to, f1));
    const inside = spots.slice(first, end).filter((spot) => spot.f < range.to);   // `to` is excluded
    if (range.kind === 'gap' && inside.length === 0) continue;
    /** @type {PlanRow[]} */
    const add = [];
    const g = range.kind === 'band' ? null : range.band;
    if (g !== null && g !== group) add.push({ kind: 'group', name: g });
    if (range.kind !== 'gap') add.push({ kind: 'range', index: i, range });
    for (const spot of inside) add.push({ kind: 'spot', spot });
    if (rows.length + add.length > PLAN_LIST_MAX) return { rows, more: true };
    rows.push(...add);
    group = g;
  }
  return { rows, more: false };
}

/**
 * The ranges and spots whose names or whose bands' hold q, and the channel
 * sets whose names do, in frequency order.
 * @param {{ ranges: readonly PlanRange[], bands: readonly PlanBand[], channels: readonly ChannelSet[], spots: readonly Spot[] }} plan
 * @param {string} q  lower case
 * @returns {{ rows: PlanRow[], more: boolean }}
 */
function matchingRows({ ranges, bands, channels, spots }, q) {
  const holds = (/** @type {string} */ name) => name.toLowerCase().includes(q);
  /** @type {Array<{ f: number, row: PlanRow }>} */
  const found = [];
  ranges.forEach((range, index) => {
    if (range.kind !== 'gap' && (holds(range.name) || holds(range.band))) found.push({ f: range.from, row: { kind: 'range', index, range } });
  });
  for (const set of channels) if (holds(set.name)) found.push({ f: set.from, row: { kind: 'set', set } });
  for (const spot of spots) if (holds(spotName(bands, spot))) found.push({ f: spot.f, row: { kind: 'spot', spot } });
  found.sort((a, b) => a.f - b.f);      // stable: a range before the set or spot at its edge
  return { rows: found.slice(0, PLAN_LIST_MAX).map(({ row }) => row), more: found.length > PLAN_LIST_MAX };
}
