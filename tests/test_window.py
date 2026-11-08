"""Tests for the native window helpers that run without GTK or a display."""

import sys
import unittest
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


class UnavailableReasonTest(unittest.TestCase):
    def test_without_pygobject(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            self.assertEqual(window.unavailable_reason(), "PyGObject is not installed")


if __name__ == "__main__":
    unittest.main()
