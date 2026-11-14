"""Tests for the native window helpers that run without GTK or a display."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from radiolack import window

ORIGIN = window._origin("http://127.0.0.1:8000/")


class OriginTest(unittest.TestCase):
    def test_same_origin(self):
        for uri in ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/x?y#z", "HTTP://127.0.0.1:8000"):
            with self.subTest(uri=uri):
                self.assertEqual(window._origin(uri), ORIGIN)

    def test_other_origins(self):
        for uri in ("http://127.0.0.1:8001/", "https://127.0.0.1:8000/", "http://localhost:8000/",
                    "http://127.0.0.1/", "http://127.0.0.1:99999/", "file:///tmp/a.html",
                    "about:blank", "data:text/html,x"):
            with self.subTest(uri=uri):
                self.assertNotEqual(window._origin(uri), ORIGIN)


class FakeDecision:
    def __init__(self, uri):
        request = SimpleNamespace(get_uri=lambda: uri)
        self._action = SimpleNamespace(get_request=lambda: request)
        self.ignored = False

    def get_navigation_action(self):
        return self._action

    def ignore(self):
        self.ignored = True


DECISION = SimpleNamespace(NAVIGATION_ACTION=0, NEW_WINDOW_ACTION=1, RESPONSE=2)
FAKE_WEBKIT = SimpleNamespace(PolicyDecisionType=DECISION)


class StayOnOriginTest(unittest.TestCase):
    def decide(self, uri, decision_type):
        decision = FakeDecision(uri)
        handled = window._stay_on_origin(None, decision, decision_type, ORIGIN, FAKE_WEBKIT)
        self.assertEqual(handled, decision.ignored)
        return decision.ignored

    def test_allows_navigation_within_interface(self):
        self.assertFalse(self.decide("http://127.0.0.1:8000/", DECISION.NAVIGATION_ACTION))

    def test_refuses_navigation_elsewhere(self):
        for uri in ("https://radiolack.example/", "file:///home/user/picture.png"):
            with self.subTest(uri=uri):
                self.assertTrue(self.decide(uri, DECISION.NAVIGATION_ACTION))

    def test_refuses_new_windows(self):
        self.assertTrue(self.decide("http://127.0.0.1:8000/", DECISION.NEW_WINDOW_ACTION))

    def test_leaves_responses_alone(self):
        decision = SimpleNamespace(ignore=mock.Mock())
        self.assertFalse(window._stay_on_origin(None, decision, DECISION.RESPONSE, ORIGIN, FAKE_WEBKIT))
        decision.ignore.assert_not_called()


class ShowTest(unittest.TestCase):
    def test_keeps_no_browser_data_on_disk(self):
        # A persistent context would fill ~/.local/share/radiolack and ~/.cache/radiolack at each start.
        Gdk, GLib, Gtk, WebKit2 = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(window, "_load_toolkit", return_value=(Gdk, GLib, Gtk, WebKit2)):
            window.show("http://127.0.0.1:8000/")
        WebKit2.WebContext.new_ephemeral.assert_called_once_with()
        self.assertIs(WebKit2.WebView.call_args.kwargs["web_context"], WebKit2.WebContext.new_ephemeral.return_value)
        WebKit2.WebView.return_value.load_uri.assert_called_once_with("http://127.0.0.1:8000/")

    def test_saves_the_downloads(self):
        Gdk, GLib, Gtk, WebKit2 = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(window, "_load_toolkit", return_value=(Gdk, GLib, Gtk, WebKit2)):
            window.show("http://127.0.0.1:8000/")
        WebKit2.WebContext.new_ephemeral.return_value.connect.assert_called_once_with(
            "download-started", window._save_download, WebKit2.WebView.return_value, GLib)


class UnavailableReasonTest(unittest.TestCase):
    def test_without_pygobject(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            self.assertEqual(window.unavailable_reason(), "PyGObject is not installed")


class FakeDownload:
    """A WebKit download: its signals' handlers, which a test emits, and its destination."""

    def __init__(self) -> None:
        self.handlers = {}
        self.destination = None

    def connect(self, name, handler):
        self.handlers[name] = handler

    def emit(self, name, *values):
        return self.handlers[name](self, *values)

    def set_destination(self, uri):
        self.destination = uri


class FakeView:
    """A web view that keeps the scripts it runs in the page."""

    def __init__(self) -> None:
        self.scripts = []

    def evaluate_javascript(self, script, length, world_name, source_uri, cancellable, callback, user_data):
        self.scripts.append(script)


EVENT = re.compile(r"^window\.dispatchEvent\(new CustomEvent\(\"radiolack-download\", \{detail: (.*)\}\)\);$")


class DownloadTest(unittest.TestCase):
    """What the interface downloads goes to the Downloads folder, without
    asking, under a name not taken there, and the page learns where."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        # Not "Downloads", as xdg-user-dirs may name it; the quotes test the event's escaping.
        self.downloads = self.home / 'Saved "files"'
        self.downloads.mkdir()
        self.special = str(self.downloads)
        self.glib = SimpleNamespace(UserDirectory=SimpleNamespace(DIRECTORY_DOWNLOAD="download"),
                                    get_user_special_dir=lambda which: self.special if which == "download" else None,
                                    get_home_dir=lambda: str(self.home),
                                    filename_to_uri=lambda path, hostname: Path(path).as_uri())

    def download(self, suggested: str, view=None, error: str | None = None):
        """Download ``suggested``: returns where it went and what the page learnt."""
        download, view = FakeDownload(), view or FakeView()
        window._save_download(None, download, view, self.glib)
        self.assertTrue(download.emit("decide-destination", suggested))
        if error is not None:
            download.emit("failed", SimpleNamespace(message=error))
        download.emit("finished")
        [script] = view.scripts
        return download.destination, json.loads(EVENT.match(script)[1])

    def test_saves_in_the_downloads_folder_under_a_name_not_taken(self):
        name = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"
        destination, detail = self.download(name)
        self.assertEqual(destination, (self.downloads / name).as_uri())
        self.assertEqual(detail, {"name": name, "path": str(self.downloads / name), "error": None})
        (self.downloads / name).write_bytes(b"saved")
        destination, detail = self.download(name)
        again = "2026-11-06_21-20-00Z_145.500000MHz_NFM (2).wav"
        self.assertEqual((destination, detail["path"]),
                         ((self.downloads / again).as_uri(), str(self.downloads / again)))

    def test_keeps_only_the_name_of_what_the_server_suggests(self):
        destination, detail = self.download("../../.bashrc")
        self.assertEqual((destination, detail["name"]), ((self.downloads / ".bashrc").as_uri(), ".bashrc"))
        self.assertEqual(self.download("..\\x.wav")[0], (self.downloads / "x.wav").as_uri())

    def test_without_a_downloads_folder_saves_in_the_home_folder(self):
        for special in (None, str(self.home / "missing")):
            with self.subTest(special=special):
                self.special = special
                self.assertEqual(self.download("x.wav")[0], (self.home / "x.wav").as_uri())

    def test_tells_the_page_why_a_download_failed(self):
        _, detail = self.download("x.wav", error="Permission denied")
        self.assertEqual(detail["error"], "Permission denied")

    def test_runs_the_script_with_an_older_webkit_too(self):
        view = SimpleNamespace(scripts=[])
        view.run_javascript = lambda script, cancellable, callback, user_data: view.scripts.append(script)
        self.assertEqual(self.download("x.wav", view=view)[1]["name"], "x.wav")


if __name__ == "__main__":
    unittest.main()
