"""Tests for the bookmarks: commands, checks, order and the saved form."""

import unittest

from radiolack.bookmarks import MAX_BOOKMARKS, MAX_NAME_CHARS, Bookmarks

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
            {"id": 2, "frequency_hz": 144_300_000.0, "name": "SSB calling", "mode": "USB", "scan": True},
            {"id": 1, "frequency_hz": 145_500_000.0, "name": "FM calling", "mode": "NFM", "scan": True},
        ])
        self.assertEqual(self.bookmarks.stored(), [{key: value for key, value in entry.items() if key != "id"}
                                                   for entry in listed])

    def test_changes_and_removes_by_id(self):
        self.add(**CALLING)
        self.bookmarks.apply({"action": "change", "id": 1, "name": "Calling", "scan": False})
        self.assertEqual(self.bookmarks.describe(), [
            {"id": 1, "frequency_hz": 145_500_000.0, "name": "Calling", "mode": "NFM", "scan": False}])
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


if __name__ == "__main__":
    unittest.main()
