"""Tests for the bookmarks and their categories: commands, checks, order
and the saved form."""

import unittest

from radiolack.bookmarks import MAX_BOOKMARKS, MAX_CATEGORIES, MAX_CATEGORY_CHARS, MAX_NAME_CHARS, Bookmarks

CALLING = {"frequency_hz": 145_500_000, "name": "FM calling", "mode": "NFM", "scan": True}


class BookmarksTest(unittest.TestCase):
    def setUp(self):
        self.bookmarks = Bookmarks()

    def add(self, **fields):
        self.bookmarks.apply({"action": "add", **fields})
        return self.bookmarks.describe()

    def test_adds_in_frequency_order_with_ids(self):
        self.add(**CALLING)
        listed = self.add(frequency_hz=144_300_000.4, name="  SSB\tcalling ", mode="USB")
        self.assertEqual(listed, [
            {"id": 2, "frequency_hz": 144_300_000.0, "name": "SSB calling", "mode": "USB", "scan": True,
             "category": None},
            {"id": 1, "frequency_hz": 145_500_000.0, "name": "FM calling", "mode": "NFM", "scan": True,
             "category": None},
        ])
        # Without categories, the file is as an older Radiolack wrote it.
        self.assertEqual(self.bookmarks.stored(), {"bookmarks": [
            {key: value for key, value in entry.items() if key not in ("id", "category")} for entry in listed]})

    def test_changes_and_removes_by_id(self):
        self.add(**CALLING)
        self.bookmarks.apply({"action": "change", "id": 1, "name": "Calling", "scan": False})
        self.assertEqual(self.bookmarks.describe(), [
            {"id": 1, "frequency_hz": 145_500_000.0, "name": "Calling", "mode": "NFM", "scan": False,
             "category": None}])
        self.bookmarks.apply({"action": "remove", "id": 1})
        self.assertEqual(self.bookmarks.describe(), [])
        with self.assertRaisesRegex(ValueError, "no such bookmark"):
            self.bookmarks.apply({"action": "remove", "id": 1})

    def test_refuses_invalid_commands_without_changing_anything(self):
        self.add(**CALLING)
        before = self.bookmarks.describe()
        refused = (
            ({"action": "rename", "id": 1}, "a bookmark action is"),
            ({"action": "add", "name": "No frequency", "mode": "NFM"}, "needs a frequency"),
            ({"action": "add", **CALLING, "colour": "red"}, "unknown bookmark field: colour"),
            ({"action": "add", **CALLING, "frequency_hz": 0}, "frequency must be a number"),
            ({"action": "add", **CALLING, "frequency_hz": True}, "frequency must be a number"),
            ({"action": "add", **CALLING, "mode": "DSB"}, "mode must be one of"),
            ({"action": "add", **CALLING, "scan": "yes"}, "scan setting must be true or false"),
            ({"action": "add", **CALLING, "name": " \n "}, "cannot be empty"),
            ({"action": "add", **CALLING, "name": "x" * (MAX_NAME_CHARS + 1)}, "at most"),
            ({"action": "add", **CALLING, "name": "Bell\x07"}, "control characters"),
            ({"action": "add", **CALLING, "name": 7}, "must be text"),
            ({"action": "change", "id": "1", "name": "Calling"}, "id must be a whole number"),
            ({"action": "change", "id": 1, "name": ""}, "cannot be empty"),
            ({"action": "remove", "id": 1, "name": "Calling"}, "unknown bookmark field: name"),
            ({"action": "add", **CALLING, "category": 1}, "no such category"),
            ({"action": "add", **CALLING, "category": "Local"}, "category must be a category id or null"),
            ({"action": "change", "id": 1, "category": True}, "category must be a category id or null"),
            ({"action": "change", "ids": [], "scan": False}, "ids must be a list of 1 to"),
            ({"action": "change", "ids": 1, "scan": False}, "ids must be a list of 1 to"),
            ({"action": "change", "ids": [1, 1], "scan": False}, "names a bookmark twice"),
            ({"action": "change", "ids": [1, 9], "scan": False}, "no such bookmark"),
            ({"action": "change", "ids": [1]}, "sets their category or scan"),
            ({"action": "change", "ids": [1], "name": "Same"}, "category or scan alone, not name"),
            ({"action": "add_category"}, "category's name must be text"),
            ({"action": "add_category", "name": "  "}, "category's name cannot be empty"),
            ({"action": "add_category", "name": "x" * (MAX_CATEGORY_CHARS + 1)}, "at most 40"),
            ({"action": "add_category", "name": "Local", "id": 1}, "unknown bookmark field: id"),
            ({"action": "rename_category", "id": 1, "name": "Local"}, "no such category"),
            ({"action": "remove_category", "id": "1"}, "category id must be a whole number"),
        )
        for command, message in refused:
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, message):
                    self.bookmarks.apply(command)
                self.assertEqual(self.bookmarks.describe(), before)

    def test_keeps_at_most_a_set_number(self):
        bookmarks = Bookmarks([{**CALLING, "name": f"Channel {n}"} for n in range(MAX_BOOKMARKS + 1)])
        self.assertEqual(len(bookmarks.describe()), MAX_BOOKMARKS)
        with self.assertRaisesRegex(ValueError, "already"):
            bookmarks.apply({"action": "add", **CALLING})

    def test_starts_from_the_saved_ones_leaving_out_invalid_entries(self):
        with self.assertLogs("radiolack.bookmarks", "WARNING") as logs:
            bookmarks = Bookmarks([CALLING, {"frequency_hz": 144_050_000, "name": "CW", "mode": "CW", "scan": False},
                                   {"frequency_hz": "145.5", "name": "Text", "mode": "NFM"}, ["not", "an", "object"]])
        self.assertEqual([entry["name"] for entry in bookmarks.describe()], ["CW", "FM calling"])
        self.assertEqual(len(logs.output), 2)
        with self.assertLogs("radiolack.bookmarks", "WARNING"):
            self.assertEqual(Bookmarks({"bookmarks": []}).describe(), [])


class CategoriesTest(unittest.TestCase):
    def setUp(self):
        self.bookmarks = Bookmarks()
        for name, frequency_hz in (("Calling", 145_500_000), ("Repeater", 145_600_000), ("Tower", 118_100_000)):
            self.bookmarks.apply({"action": "add", **CALLING, "name": name, "frequency_hz": frequency_hz})

    def category_of(self) -> dict[str, int | None]:
        return {entry["name"]: entry["category"] for entry in self.bookmarks.describe()}

    def test_makes_renames_and_removes_categories_in_name_order(self):
        self.bookmarks.apply({"action": "add_category", "name": " local  repeaters "})
        self.bookmarks.apply({"action": "add_category", "name": "Airport"})
        self.assertEqual(self.bookmarks.categories(), [{"id": 2, "name": "Airport"}, {"id": 1, "name": "local repeaters"}])
        with self.assertRaisesRegex(ValueError, "already named"):
            self.bookmarks.apply({"action": "add_category", "name": "AIRPORT"})
        self.bookmarks.apply({"action": "rename_category", "id": 1, "name": "Local repeaters"})     # its own name, recased
        with self.assertRaisesRegex(ValueError, "already named"):
            self.bookmarks.apply({"action": "rename_category", "id": 1, "name": "airport"})
        self.assertEqual(self.bookmarks.categories(), [{"id": 2, "name": "Airport"}, {"id": 1, "name": "Local repeaters"}])
        self.bookmarks.apply({"action": "change", "id": 1, "category": 1})
        self.bookmarks.apply({"action": "remove_category", "id": 1})      # its bookmarks stay, uncategorized
        self.assertEqual(self.bookmarks.categories(), [{"id": 2, "name": "Airport"}])
        self.assertEqual(self.category_of(), {"Tower": None, "Calling": None, "Repeater": None})

    def test_moves_and_ticks_several_bookmarks_at_once(self):
        self.bookmarks.apply({"action": "add_category", "name": "Local"})
        self.bookmarks.apply({"action": "change", "ids": [1, 2], "category": 1, "scan": False})
        self.assertEqual(self.category_of(), {"Tower": None, "Calling": 1, "Repeater": 1})
        self.assertEqual([entry["name"] for entry in self.bookmarks.describe() if entry["scan"]], ["Tower"])
        self.bookmarks.apply({"action": "change", "ids": [2], "category": None})
        self.bookmarks.apply({"action": "add", **CALLING, "name": "Simplex", "category": 1})
        self.assertEqual(self.category_of(), {"Tower": None, "Calling": 1, "Simplex": 1, "Repeater": None})

    def test_at_most_a_set_number_of_categories(self):
        for index in range(MAX_CATEGORIES):
            self.bookmarks.apply({"action": "add_category", "name": f"Category {index}"})
        with self.assertRaisesRegex(ValueError, "already 100 categories"):
            self.bookmarks.apply({"action": "add_category", "name": "One more"})

    def test_the_file_keeps_the_bookmarks_as_before_and_the_categories_beside_them(self):
        self.bookmarks.apply({"action": "add_category", "name": "Local"})
        self.bookmarks.apply({"action": "add_category", "name": "Empty"})
        self.bookmarks.apply({"action": "change", "ids": [1, 2], "category": 1})
        stored = self.bookmarks.stored()
        self.assertEqual([entry["name"] for entry in stored["bookmarks"]], ["Tower", "Calling", "Repeater"])
        self.assertTrue(all(set(entry) == {"frequency_hz", "name", "mode", "scan"} for entry in stored["bookmarks"]))
        self.assertEqual(stored["categories"], [{"name": "Empty", "bookmarks": []}, {"name": "Local", "bookmarks": [1, 2]}])
        again = Bookmarks(stored["bookmarks"], stored["categories"])
        self.assertEqual(again.stored(), stored)
        self.assertEqual({entry["name"]: entry["category"] for entry in again.describe()},
                         {"Tower": None, "Calling": 2, "Repeater": 2})
        # An older Radiolack reads the same bookmarks and writes back none of the categories.
        self.assertEqual(Bookmarks(stored["bookmarks"]).stored(), {"bookmarks": stored["bookmarks"]})

    def test_leaves_out_saved_categories_that_are_not_valid(self):
        saved = [CALLING, {**CALLING, "name": "Bad", "frequency_hz": "x"}, {**CALLING, "name": "Other"},
                 {**CALLING, "name": "Tagged", "category": "Local"}]
        categories = [
            {"name": "Local", "bookmarks": [0, 1, 2, 9, True, "0"]},     # 1 left out, 9 and the others name none
            {"name": "local", "bookmarks": []},                         # a name taken
            {"name": "Twice", "bookmarks": [0]},                        # already in Local
            {"name": "", "bookmarks": []}, {"name": "No list", "bookmarks": 3}, {"name": "Extra", "bookmarks": [], "x": 1},
            "Text",
        ]
        with self.assertLogs("radiolack.bookmarks", "WARNING") as logs:
            bookmarks = Bookmarks(saved, categories)
        self.assertEqual(len(logs.output), 2 + 4 + 6)
        self.assertEqual(bookmarks.categories(), [{"id": 1, "name": "Local"}, {"id": 2, "name": "Twice"}])
        self.assertEqual({entry["name"]: entry["category"] for entry in bookmarks.describe()},
                         {"FM calling": 1, "Other": 1})
        with self.assertLogs("radiolack.bookmarks", "WARNING"):
            self.assertEqual(Bookmarks([CALLING], {"Local": [0]}).categories(), [])


if __name__ == "__main__":
    unittest.main()
