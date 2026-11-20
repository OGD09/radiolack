# Bookmark categories

Designed with the owner on 2026-11-20, after 0.19.0. [`scoping.md`](scoping.md)
holds its summary among the milestones, and its decision of 2026-11-20.

Goal: sort the bookmarks into categories that the owner makes, names and
removes, one level deep, and move bookmarks into them by dragging them
there, by choosing a category, or several at once.

Why: the Bookmarks menu shows one list in frequency order, up to 2000
bookmarks, ticked or not for the memory scan. Repeaters, an airport's
frequencies and stations to watch mix there, and the memory scan's ticks
are set one bookmark at a time.

Rules it keeps (later-milestones.md, "Rules they share"): receive-only;
tests need no receiver and reach no network; protocol version 6 stays,
an older page ignoring what it does not know; each pull request brings its
documentation and tests.

## Decisions

- **Categories, one level, the owner's own.** A category is a name the
  owner gives; it holds bookmarks and nothing else: no category within a
  category. A bookmark is in one category at most; without one, it is
  uncategorized. Categories are not taken from the band plan: the band
  plan list stays under the bookmarks, as it is.
- **Named "Category" in the interface.**
- **Drag and drop with a confirmation.** Dropping a bookmark on a category
  asks "Move … to …?" with Confirm and Cancel, as the owner chose over an
  Undo after the move.
- **Without dragging too.** Edit gains a Category choice, and a Select mode
  moves several bookmarks at once: the iPhone, where Safari's drag and
  drop does not follow a finger, and the keyboard need them.
- **A category ticks its bookmarks.** A box on each category's heading
  ticks or unticks all its bookmarks for the memory scan at once.
- **Removing a category keeps its bookmarks**, which become uncategorized.

## Design

### Data

- **A category**: a name, 1 to 40 characters on one line, unique ignoring
  case; at most 100 categories. The engine numbers them, as it numbers
  the bookmarks, again at each start.
- **A bookmark** gains `category`: a category's id, or null.
- **Order**: the categories by name, ignoring case; the uncategorized
  bookmarks first, under "Uncategorized"; within each, the bookmarks in
  frequency order, then by name, as today.
- **The file**: `bookmarks.json` keeps its bookmarks as they are, and
  gains a list of categories, each with its name and the positions of its
  bookmarks in the file's list:

  ```json
  {"version": 1,
   "bookmarks": [{"frequency_hz": 145500000.0, "name": "FM calling", "mode": "NFM", "scan": true}],
   "categories": [{"name": "Local", "bookmarks": [0]}]}
  ```

  An older Radiolack reads the bookmarks and ignores the categories,
  which it does not write back: going back to it loses the categories,
  never a bookmark. A bookmark field it did not know would have cost the
  bookmark itself. A category whose name is not valid, a position that
  names no bookmark or one already placed are left out with a warning, as
  a saved bookmark that is not valid is today.

### Protocol

- **The bookmarks message** gains `categories`, `[{"id": 1, "name":
  "Local"}]` in name order, and each bookmark `category`, an id or null.
  Bookmarks keep their order, frequency then name: the interface groups
  them.
- **The bookmark message**:
  - `add` and `change` take `category`, an id or null;
  - `change` takes `ids`, a list of 1 to 2000 bookmark ids, in place of
    `id`, with `category` or `scan` alone: what the Select mode, a drop of
    several and a category's box send, in one message, one change;
  - `add_category` with `name`, `rename_category` with `id` and `name`,
    `remove_category` with `id`, whose bookmarks become uncategorized.
- **Refusals**, the whole message changing nothing: a name already taken,
  empty, too long or with control characters, a 101st category, an
  unknown id, as for the bookmarks today.
- **Compatibility**: an older interface ignores `categories` and
  `category`, shows one list as today, and adds bookmarks uncategorized;
  its changes leave a bookmark's category as it is. A newer interface
  facing an older engine, whose bookmarks message has no `categories`,
  shows no category and offers none.

### Interface

The Bookmarks menu:

- **The toolbar**: Filter, "Add current frequency", then "New category"
  and "Select".
- **The list**: with no category, one list as today. With categories,
  "Uncategorized" first, then each category by name, each a heading with
  its box, its name, how many bookmarks it holds, a fold toggle, Rename
  and Remove, then its bookmarks as today. The filter keeps the headings
  of the categories where a bookmark matches, and a category whose name
  matches shows all its bookmarks. Folded categories are each interface's
  own, not saved.
- **The category's box**: ticked when all its bookmarks are ticked,
  mixed when some are, a press ticking them all, or unticking them all
  when all are.
- **New category and Rename**: an inline field, as a bookmark's name is
  edited, Enter to keep, Escape to leave.
- **Remove**: a dialog, "Remove the category …? Its N bookmarks become
  uncategorized." with Confirm and Cancel.
- **Drag and drop**, with a mouse: a bookmark's row dragged a few pixels
  follows the pointer; a category's heading, or "Uncategorized", lights
  up under it; dropped there, a dialog asks "Move … to …?", or "Move 3
  bookmarks to …?" in the Select mode, with Confirm and Cancel. Dropped
  elsewhere, or on its own category, nothing happens. A click without
  dragging tunes, as today.
- **Edit**: a Category choice beside the name and the frequency, which
  moves the bookmark at once, as the other fields change it.
- **Select**: each row's box selects the bookmark instead of ticking it
  for the scan, and a bar shows "3 selected", a Category choice, Move and
  Done. Move moves them without a dialog: choosing the category and
  pressing Move is the confirmation.
- Every interface, a paired device's included, makes, renames and removes
  categories and moves bookmarks, as it changes the bookmarks today.

## Pull requests

1. This design.
2. The engine: categories in `bookmarks.py`, their file and their
   messages, `change` with `ids`; protocol.md; tests of the file's
   checks, the downgrade's layout and the refusals.
3. The interface: the grouped list, the category's box, New category,
   Rename and Remove, Edit's choice, Select, and the drag and drop with
   its dialog; README.md; browser tests.

## Done when

On roamer: three categories made, bookmarks dragged into them with
Confirm, others moved by Select on the iPhone; a category's box ticks its
bookmarks and the memory scan watches them; a category renamed and one
removed, its bookmarks uncategorized; everything shown again after a
restart; and every test passes in CI.

## Limits

- One level: a category holds no category.
- One category per bookmark.
- Drag and drop with a mouse only; on a touch screen, Edit and Select.
- The categories' order is their names': no manual order.
- Going back to an older Radiolack loses the categories, not the
  bookmarks.
