// The Bookmarks menu's categories (docs/design/bookmark-categories.md): how
// the list groups the bookmarks under their categories, what a category's
// box shows and changes, and what moving bookmarks sends and asks. The
// engine keeps the categories, one level deep; a bookmark is in one at most.

import { formatFrequency } from './frequency.js';

/** The heading of the bookmarks in no category. */
export const UNCATEGORIZED = 'Uncategorized';
/** The longest name of a category, as the engine's bookmarks.py. */
export const CATEGORY_NAME_MAX = 40;

/**
 * A bookmark as the menu lists it.
 * @typedef {object} ListedBookmark
 * @property {number} id
 * @property {number} f  its frequency, in Hz
 * @property {string} name
 * @property {string} mode
 * @property {boolean} scan  whether the memory scan watches it
 * @property {number | null} category  its category's id, or null
 */

/**
 * @typedef {object} Category
 * @property {number} id
 * @property {string} name
 */

/**
 * A category's part of the list, or the uncategorized bookmarks'.
 * @typedef {object} Section
 * @property {Category | null} category  null for the uncategorized bookmarks
 * @property {string} key  the section's key: its category's id, or "none"
 * @property {ListedBookmark[]} bookmarks  those it shows, in the list's order
 * @property {number} total  how many bookmarks it holds, the filter aside
 * @property {'all' | 'some' | 'none' | 'empty'} ticked  how many of them the memory scan watches
 * @property {boolean} folded  whether it shows its heading alone
 */

/**
 * The key of a category's section, or the uncategorized bookmarks'.
 * @param {number | null} category
 * @returns {string}
 */
export const sectionKey = (category) => (category === null ? 'none' : String(category));

/**
 * The name a category's section shows.
 * @param {Category | null} category
 * @returns {string}
 */
export const sectionName = (category) => (category === null ? UNCATEGORIZED : category.name);

/**
 * Whether a bookmark matches the filter typed: its name or its frequency.
 * @param {ListedBookmark} b
 * @param {string} query  lower case, trimmed; empty matches every bookmark
 * @returns {boolean}
 */
export const matches = (b, query) => !query || b.name.toLowerCase().includes(query) || formatFrequency(b.f).includes(query);

/**
 * The list's sections. Without categories, one section, whose heading the
 * menu leaves out: the list as before categories. With some, the
 * uncategorized bookmarks first, then each category in the engine's order,
 * by name. While a filter is typed, a section shows the bookmarks that
 * match, all of them when its category's name matches, unfolded; a section
 * with none is left out. The bookmark being edited always shows.
 * @param {ListedBookmark[]} bookmarks  in the engine's order
 * @param {Category[]} categories  in the engine's order
 * @param {string} query  the filter, lower case and trimmed
 * @param {Set<string>} folded  the keys of the folded sections
 * @param {number | null} [keep]  the bookmark being edited
 * @returns {Section[]}
 */
export function bookmarkSections(bookmarks, categories, query, folded, keep = null) {
  const known = new Set(categories.map((c) => c.id));
  /** @type {(Category | null)[]} */
  const order = categories.length ? [null, ...categories] : [null];
  return order.flatMap((category) => {
    const id = category === null ? null : category.id;
    // A bookmark whose category is unknown, from a list sent mid-change, shows uncategorized.
    const held = bookmarks.filter((b) => (b.category !== null && known.has(b.category) ? b.category : null) === id);
    const named = query !== '' && category !== null && category.name.toLowerCase().includes(query);
    const shown = held.filter((b) => named || matches(b, query) || b.id === keep);
    if (query && !shown.length && !named) return [];
    const key = sectionKey(id);
    const watched = held.filter((b) => b.scan).length;
    return [{
      category, key, total: held.length, folded: !query && folded.has(key),
      bookmarks: !query && folded.has(key) ? held.filter((b) => b.id === keep) : shown,
      ticked: !held.length ? 'empty' : watched === held.length ? 'all' : watched ? 'some' : 'none',
    }];
  });
}

/**
 * What a press on a section's box sends: every bookmark it holds unticked
 * when all are ticked, else the unticked ones ticked; null for an empty
 * section.
 * @param {ListedBookmark[]} bookmarks  all of them, in the engine's order
 * @param {Category[]} categories
 * @param {string} key  the section's
 * @returns {{ ids: number[], scan: boolean } | null}
 */
export function sectionScanChange(bookmarks, categories, key) {
  const [section] = bookmarkSections(bookmarks, categories, '', new Set()).filter((s) => s.key === key);
  if (!section || !section.total) return null;
  // bookmarkSections without a filter nor folds shows every bookmark the section holds.
  const scan = section.ticked !== 'all';
  return { ids: section.bookmarks.filter((b) => b.scan !== scan).map((b) => b.id), scan };
}

/**
 * Bookmarks to move into a category: those not already there, and the
 * question the move asks, or null when none would move.
 * @param {ListedBookmark[]} bookmarks
 * @param {Category[]} categories
 * @param {number[]} ids  the bookmarks moved
 * @param {number | null} target  the category, or null to uncategorize them
 * @returns {{ ids: number[], question: string } | null}
 */
export function moveRequest(bookmarks, categories, ids, target) {
  const category = target === null ? null : categories.find((c) => c.id === target);
  if (category === undefined) return null;
  const moving = bookmarks.filter((b) => ids.includes(b.id) && b.category !== target);
  if (!moving.length) return null;
  const what = moving.length === 1 ? `"${moving[0].name}"` : `${moving.length} bookmarks`;
  const where = category === null ? 'Uncategorized' : `"${category.name}"`;
  return { ids: moving.map((b) => b.id), question: `Move ${what} to ${where}?` };
}

/**
 * The question that removing a category asks.
 * @param {ListedBookmark[]} bookmarks
 * @param {Category} category
 * @returns {string}
 */
export function removeQuestion(bookmarks, category) {
  const held = bookmarks.filter((b) => b.category === category.id).length;
  const fate = held === 0 ? 'It holds no bookmark.'
    : `Its ${held === 1 ? 'bookmark becomes' : `${held} bookmarks become`} uncategorized.`;
  return `Remove the category "${category.name}"? ${fate}`;
}

/**
 * A category's name as typed, with its spaces as the engine keeps them, or
 * why it cannot be one: a text, the reason, for an empty name, a name too
 * long or one that another category has, ignoring case.
 * @param {string} typed
 * @param {Category[]} categories
 * @param {number | null} [renamed]  the category renamed, which may keep its own name
 * @returns {{ name: string } | { reason: string }}
 */
export function checkedCategoryName(typed, categories, renamed = null) {
  const name = typed.replace(/\s+/g, ' ').trim();
  if (!name) return { reason: 'A category needs a name.' };
  if (name.length > CATEGORY_NAME_MAX) return { reason: `A category's name is at most ${CATEGORY_NAME_MAX} characters.` };
  const folded = name.toLowerCase();
  if (categories.some((c) => c.id !== renamed && c.name.toLowerCase() === folded)) {
    return { reason: `A category is already named ${name}.` };
  }
  return { name };
}
