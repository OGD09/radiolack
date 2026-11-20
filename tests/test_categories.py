"""Checks of the Bookmarks menu's categories (categories.js) and of the
bookmarks message's decoding (stream.js), typed modules without a browser:
Node.js runs a script that imports them from the interface's folder and
prints what they give, as JSON. Skipped where Node.js is not installed;
GitHub's runners and the type check's job have it."""

import json
import shutil
import subprocess
import unittest

from radiolack.server import WEB_DIR

NODE = shutil.which("node")

# What categories.js gives, case by case, for a list of five bookmarks in
# two categories, the uncategorized ones first.
CATEGORIES_SCRIPT = """
const c = await import(process.argv[1]);
const repeaters = { id: 1, name: 'Repeaters' }, digital = { id: 2, name: 'Digital' };
const categories = [digital, repeaters];
const b = (id, f, name, scan, category) => ({ id, f, name, mode: 'NFM', scan, category });
const bookmarks = [b(1, 144_300_000, 'SSB calling', false, null), b(2, 144_800_000, 'APRS', true, 2),
  b(3, 145_500_000, 'Calling', false, null), b(4, 145_600_000, 'Repeater A', true, 1), b(5, 145_650_000, 'Repeater B', false, 1),
  b(6, 145_700_000, 'Lost', false, 9)];
const shown = (sections) => sections.map((s) => [s.key, c.sectionName(s.category), s.bookmarks.map((x) => x.id), s.total, s.ticked, s.folded]);
const out = {
  plain: shown(c.bookmarkSections(bookmarks.map((x) => ({ ...x, category: null })), [], '', new Set())),
  plainFiltered: shown(c.bookmarkSections(bookmarks, [], 'repeater', new Set())),
  plainNone: shown(c.bookmarkSections(bookmarks, [], 'nothing', new Set())),
  grouped: shown(c.bookmarkSections(bookmarks, categories, '', new Set())),
  folded: shown(c.bookmarkSections(bookmarks, categories, '', new Set(['1', 'none']), 3)),
  byName: shown(c.bookmarkSections(bookmarks, categories, 'digi', new Set(['2']))),
  byFrequency: shown(c.bookmarkSections(bookmarks, categories, '145.600', new Set(['1']))),
  keep: shown(c.bookmarkSections(bookmarks, categories, 'aprs', new Set(), 4)),
  empty: shown(c.bookmarkSections([], [repeaters], '', new Set())),
  boxes: ['none', '1', '2', '7'].map((key) => c.sectionScanChange(bookmarks, categories, key)),
  allTicked: c.sectionScanChange([b(4, 1, 'A', true, 1), b(5, 2, 'B', true, 1)], [repeaters], '1'),
  emptyBox: c.sectionScanChange([], [repeaters], '1'),
  moves: [
    c.moveRequest(bookmarks, categories, [3], 1), c.moveRequest(bookmarks, categories, [1, 3, 4], 1),
    c.moveRequest(bookmarks, categories, [4, 5], null), c.moveRequest(bookmarks, categories, [4], 1),
    c.moveRequest(bookmarks, categories, [3], 7), c.moveRequest(bookmarks, categories, [99], 1),
  ],
  removes: [c.removeQuestion(bookmarks, repeaters), c.removeQuestion(bookmarks, digital),
    c.removeQuestion(bookmarks, { id: 7, name: 'Empty' })],
  names: [
    c.checkedCategoryName('  Local   nets ', categories), c.checkedCategoryName('   ', categories),
    c.checkedCategoryName('x'.repeat(41), categories), c.checkedCategoryName('x'.repeat(40), categories),
    c.checkedCategoryName('repeaters', categories), c.checkedCategoryName('REPEATERS', categories, 1),
  ],
  keys: [c.sectionKey(null), c.sectionKey(12)],
};
console.log(JSON.stringify(out));
"""

# What stream.js decodes of bookmarks messages, valid or not.
DECODE_SCRIPT = """
const s = await import(process.argv[1]);
const bookmark = { id: 1, frequency_hz: 145500000, name: 'Calling', mode: 'NFM', scan: true };
const valid = { type: 'bookmarks', bookmarks: [{ ...bookmark, category: 2 }, { ...bookmark, id: 3, category: null }],
  categories: [{ id: 2, name: 'Local' }] };
const out = {
  valid: s.decodeBookmarks(valid),
  older: s.decodeBookmarks({ type: 'bookmarks', bookmarks: [bookmark] }),
  refused: [
    { ...valid, categories: 'Local' }, { ...valid, categories: [{ id: 0, name: 'Local' }] },
    { ...valid, categories: [{ id: 2 }] }, { ...valid, bookmarks: [bookmark] },
    { ...valid, bookmarks: [{ ...bookmark, category: '2' }] }, { ...valid, bookmarks: [{ ...bookmark, category: -1 }] },
    { type: 'bookmarks', bookmarks: [{ ...bookmark, category: 'x' }] }, { ...valid, bookmarks: null },
  ].map(s.decodeBookmarks),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js runs the interface's modules")
class CategoriesModuleTest(unittest.TestCase):
    """categories.js and stream.js's decoding, run in Node.js."""

    @staticmethod
    def run_script(script: str, module: str) -> dict:
        """What ``script`` prints, as JSON, given the interface's ``module``
        as its first argument, a file URL."""
        done = subprocess.run([NODE, "--input-type=module", "-e", script, (WEB_DIR / module).as_uri()],
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_the_list_groups_the_bookmarks_under_their_categories(self):
        found = self.run_script(CATEGORIES_SCRIPT, "categories.js")
        # Without categories, one section: the list as before them.
        self.assertEqual(found["plain"], [["none", "Uncategorized", [1, 2, 3, 4, 5, 6], 6, "some", False]])
        self.assertEqual(found["plainFiltered"], [["none", "Uncategorized", [4, 5], 6, "some", False]])
        self.assertEqual(found["plainNone"], [])
        # Uncategorized first, then the categories in the engine's order;
        # a bookmark in a category the list does not know shows uncategorized.
        self.assertEqual(found["grouped"], [
            ["none", "Uncategorized", [1, 3, 6], 3, "none", False],
            ["2", "Digital", [2], 1, "all", False],
            ["1", "Repeaters", [4, 5], 2, "some", False],
        ])
        # A folded section shows its heading alone, and the bookmark being edited.
        self.assertEqual(found["folded"], [
            ["none", "Uncategorized", [3], 3, "none", True],
            ["2", "Digital", [2], 1, "all", False],
            ["1", "Repeaters", [], 2, "some", True],
        ])
        # A filter unfolds what it shows: a category whose name matches shows
        # all its bookmarks; a section without a match is left out.
        self.assertEqual(found["byName"], [["2", "Digital", [2], 1, "all", False]])
        self.assertEqual(found["byFrequency"], [["1", "Repeaters", [4], 2, "some", False]])
        self.assertEqual(found["keep"], [["2", "Digital", [2], 1, "all", False], ["1", "Repeaters", [4], 2, "some", False]])
        self.assertEqual(found["empty"], [["none", "Uncategorized", [], 0, "empty", False],
                                          ["1", "Repeaters", [], 0, "empty", False]])
        self.assertEqual(found["keys"], ["none", "12"])

    def test_a_category_s_box_ticks_its_bookmarks_or_unticks_them_all(self):
        found = self.run_script(CATEGORIES_SCRIPT, "categories.js")
        self.assertEqual(found["boxes"], [
            {"ids": [1, 3, 6], "scan": True},     # none ticked
            {"ids": [5], "scan": True},           # some: the others join them
            {"ids": [2], "scan": False},          # all: unticked
            None,                                 # an unknown category
        ])
        self.assertEqual(found["allTicked"], {"ids": [4, 5], "scan": False})
        self.assertIsNone(found["emptyBox"])

    def test_moves_and_removals_ask_their_question(self):
        found = self.run_script(CATEGORIES_SCRIPT, "categories.js")
        self.assertEqual(found["moves"], [
            {"ids": [3], "question": 'Move "Calling" to "Repeaters"?'},
            {"ids": [1, 3], "question": 'Move 2 bookmarks to "Repeaters"?'},    # Repeater A is there already
            {"ids": [4, 5], "question": "Move 2 bookmarks to Uncategorized?"},
            None, None, None,       # already there, an unknown category, an unknown bookmark
        ])
        self.assertEqual(found["removes"], [
            'Remove the category "Repeaters"? Its 2 bookmarks become uncategorized.',
            'Remove the category "Digital"? Its bookmark becomes uncategorized.',
            'Remove the category "Empty"? It holds no bookmark.',
        ])

    def test_a_category_s_name_is_checked_as_the_engine_checks_it(self):
        found = self.run_script(CATEGORIES_SCRIPT, "categories.js")
        self.assertEqual(found["names"], [
            {"name": "Local nets"},
            {"reason": "A category needs a name."},
            {"reason": "A category's name is at most 40 characters."},
            {"name": "x" * 40},
            {"reason": "A category is already named repeaters."},
            {"name": "REPEATERS"},      # its own name, in another case
        ])

    def test_the_bookmarks_message_brings_the_categories(self):
        found = self.run_script(DECODE_SCRIPT, "stream.js")
        bookmark = {"id": 1, "frequencyHz": 145500000, "name": "Calling", "mode": "NFM", "scan": True}
        self.assertEqual(found["valid"], {
            "bookmarks": [{**bookmark, "category": 2}, {**bookmark, "id": 3, "category": None}],
            "categories": [{"id": 2, "name": "Local"}],
        })
        # An engine without categories: its bookmarks are uncategorized.
        self.assertEqual(found["older"], {"bookmarks": [{**bookmark, "category": None}], "categories": None})
        self.assertEqual(found["refused"], [None] * 8)


if __name__ == "__main__":
    unittest.main()
