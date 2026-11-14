"""Tests for the settings folder: where it is, reading, atomic writes and
what happens to files that cannot be read or written."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radiolack.store import BOOKMARKS_FILE, FORMAT_VERSION, SETTINGS_FILE, Store, settings_dir


class SettingsDirTest(unittest.TestCase):
    def test_follows_xdg_config_home(self):
        self.assertEqual(settings_dir({"XDG_CONFIG_HOME": "/srv/config", "HOME": "/home/someone"}),
                         Path("/srv/config/radiolack"))

    def test_falls_back_to_the_home_folder(self):
        for environ in ({"HOME": "/home/someone"}, {"XDG_CONFIG_HOME": "", "HOME": "/home/someone"},
                        {"XDG_CONFIG_HOME": "relative/path", "HOME": "/home/someone"}):
            with self.subTest(environ=environ):
                self.assertEqual(settings_dir(environ), Path("/home/someone/.config/radiolack"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "radiolack"
        self.store = Store(self.directory)

    def write(self, name: str, text: str | bytes) -> Path:
        self.directory.mkdir(exist_ok=True)
        path = self.directory / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(self.store.load(SETTINGS_FILE), {})
        self.assertEqual((self.store.saving, self.store.problem), (True, ""))
        self.assertFalse(self.directory.exists())

    def test_saves_whole_files_that_load_back(self):
        content = {"receivers": {"rtl_sdr 0": {"source": {"ppm": -4}}}, "interface": {"volume": 70}}
        self.assertTrue(self.store.save(SETTINGS_FILE, content))
        self.assertTrue(self.store.save(BOOKMARKS_FILE, {"bookmarks": [{"name": "Café net"}]}))
        self.assertEqual(sorted(os.listdir(self.directory)), [BOOKMARKS_FILE, SETTINGS_FILE])    # no temporary file
        written = json.loads((self.directory / SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(written, {"version": FORMAT_VERSION, **content})
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.directory / SETTINGS_FILE).stat().st_mode), 0o600)
        again = Store(self.directory)
        self.assertEqual(again.load(SETTINGS_FILE), content)
        self.assertEqual(again.load(BOOKMARKS_FILE), {"bookmarks": [{"name": "Café net"}]})

    def test_an_unreadable_file_is_kept_aside_and_never_overwritten(self):
        for text in ("{not json", "[1, 2]", '{"bookmarks": []}', '{"version": "1"}', b'{"version": 1, "x": "\xff"}'):
            with self.subTest(text=text):
                path = self.write(BOOKMARKS_FILE, text)
                store = Store(self.directory)
                self.assertEqual(store.load(BOOKMARKS_FILE), {})
                self.assertFalse(path.exists())
                kept = [name for name in os.listdir(self.directory) if name.startswith(f"{BOOKMARKS_FILE}.invalid-")]
                self.assertTrue(kept)
                self.assertIn(f"it was kept as {sorted(kept)[-1]}", store.problem)
                self.assertTrue(store.saving)       # the next save starts a new file
                self.assertTrue(store.save(BOOKMARKS_FILE, {"bookmarks": []}))
        kept = [name for name in os.listdir(self.directory) if name.startswith(f"{BOOKMARKS_FILE}.invalid-")]
        self.assertEqual(len(kept), 5)      # one kept file each: none replaced another

    def test_a_file_from_a_newer_radiolack_is_left_alone(self):
        text = json.dumps({"version": FORMAT_VERSION + 1, "receivers": {}})
        path = self.write(SETTINGS_FILE, text)
        self.assertEqual(self.store.load(SETTINGS_FILE), {})
        self.assertIn("comes from a newer Radiolack", self.store.problem)
        self.assertFalse(self.store.saving)
        self.assertFalse(self.store.save(SETTINGS_FILE, {"receivers": {}}))
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertTrue(self.store.save(BOOKMARKS_FILE, {"bookmarks": []}))     # the other file is saved

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "file permissions as an ordinary user")
    def test_a_file_that_cannot_be_opened_is_left_alone(self):
        path = self.write(SETTINGS_FILE, json.dumps({"version": 1}))
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        self.assertEqual(self.store.load(SETTINGS_FILE), {})
        self.assertIn("settings.json cannot be read", self.store.problem)
        self.assertFalse(self.store.save(SETTINGS_FILE, {}))
        self.assertFalse(self.store.saving)
        path.chmod(0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})

    def test_a_failed_write_is_noted_until_a_write_works(self):
        self.root.joinpath("blocker").write_text("a file where the folder should be")
        store = Store(self.root / "blocker" / "radiolack")
        self.assertFalse(store.save(SETTINGS_FILE, {}))
        self.assertFalse(store.saving)
        self.assertIn("settings.json cannot be saved", store.problem)
        store.directory = self.directory    # as if the disk were fixed
        self.assertTrue(store.save(SETTINGS_FILE, {}))
        self.assertEqual((store.saving, store.problem), (True, ""))

    def test_a_failed_write_leaves_the_previous_file_and_no_temporary_file(self):
        self.assertTrue(self.store.save(SETTINGS_FILE, {"interface": {"volume": 1}}))
        with mock.patch("radiolack.store.os.replace", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(self.store.save(SETTINGS_FILE, {"interface": {"volume": 2}}))
        self.assertEqual(os.listdir(self.directory), [SETTINGS_FILE])
        self.assertEqual(Store(self.directory).load(SETTINGS_FILE), {"interface": {"volume": 1}})
        self.assertEqual(self.store.problem, "settings.json cannot be saved: No space left on device.")

    def test_shows_a_folder_in_the_home_folder_from_there(self):
        with mock.patch("radiolack.store.Path.home", return_value=self.root):
            self.assertEqual(self.store.location, "~/radiolack")
            self.assertEqual(Store(Path("/srv/radiolack")).location, "/srv/radiolack")

    def test_shows_the_whole_folder_to_a_user_without_a_home_folder(self):
        # As the service's user, whose home is the root.
        with mock.patch("radiolack.store.Path.home", return_value=Path("/")):
            self.assertEqual(Store(Path("/var/lib/radiolack")).location, "/var/lib/radiolack")


if __name__ == "__main__":
    unittest.main()
