"""Browser tests: the interface in headless Chromium, Firefox and WebKit,
driven by Playwright against a server on an ephemeral port, with the
simulated band or the fake rtl_tcp program: no receiver.

They launch browsers, so they run only when the RADIOLACK_BROWSER_TESTS
environment variable is 1, as in CI, and then need Playwright and its
browsers (README.md, Tests). Each interface opens in its own browser
context, like a separate device, and any error in its page fails the test.
"""

from __future__ import annotations

import contextlib
import os
import re
import unittest
from collections.abc import Iterator

from radiolack.rtltcp import RtlSdrSource
from radiolack.server import LocalServer
from tests.fakes import FAKE_RTL_TCP, FakeRtlTcpRuns

ENABLED = os.environ.get("RADIOLACK_BROWSER_TESTS") == "1"
BROWSERS = ("chromium", "firefox", "webkit")
TIMEOUT_MS = 10_000     # enough for the first frames on a slow runner
SOURCE_PANEL = '#toolbar button[data-panel="source"]'
DOT = "\N{MIDDLE DOT}"  # the status line's separator
# WebKit 27.2 (Playwright 1.64) refuses its own stylesheet for every <select>
# under the interface's style-src 'self': a WebKit bug without visible effect,
# which WebKitGTK 2.50 does not have. WebKit words a refused inline style the
# same way, but Chromium and Firefox report those in their own words.
WEBKIT_SELECT_REFUSAL = "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"
# Without a GPU, as on a CI runner, Firefox refuses WebGL2 on Mesa's software
# renderer: this preference lifts that refusal. Firefox then also needs a
# display, which xvfb-run gives where there is none.
FIREFOX_PREFS = {"webgl.force-enabled": True}

if ENABLED:     # without Playwright, the tests then fail instead of being skipped
    from playwright.sync_api import Page, expect, sync_playwright


@unittest.skipUnless(ENABLED, "browser tests run only with RADIOLACK_BROWSER_TESTS=1")
class BrowserTest(unittest.TestCase):
    """Each test runs in every browser in turn."""

    @classmethod
    def setUpClass(cls) -> None:
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browsers = {}
        for name in BROWSERS:
            options = {"firefox_user_prefs": FIREFOX_PREFS} if name == "firefox" else {}
            browser = getattr(playwright, name).launch(**options)   # headless
            cls.addClassCleanup(browser.close)
            cls.browsers[name] = browser
        expect.set_options(timeout=TIMEOUT_MS)

    def serve(self, source: RtlSdrSource | None = None) -> str:
        """The address of a server started for this test."""
        server = LocalServer(source=source)
        server.start()
        self.addCleanup(server.stop)
        return server.url

    @contextlib.contextmanager
    def interfaces(self, browser: str, url: str, count: int) -> Iterator[list[Page]]:
        """``count`` interfaces at ``url``, each with the Source panel open."""
        errors: list[str] = []

        def console(message) -> None:
            if message.type != "error":
                return
            if browser == "webkit" and message.text.startswith(WEBKIT_SELECT_REFUSAL):
                return
            errors.append(message.text)

        with contextlib.ExitStack() as stack:
            pages = []
            for _ in range(count):
                context = self.browsers[browser].new_context()
                stack.callback(context.close)
                page = context.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                page.on("console", console)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator(SOURCE_PANEL).click()
                pages.append(page)
            yield pages
        self.assertEqual(errors, [], "errors in the interface")

    def test_shows_the_simulated_band_and_its_settings(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                status = page.locator("#status")
                expect(status).to_contain_text(f"Synthetic source {DOT} 2.400 MS/s")
                expect(status).to_contain_text(re.compile(rf" {DOT} \d+ fps$"))     # frames arrive
                expect(page.locator("#src-device")).to_have_value("synthetic")
                expect(page.locator("#src-note")).to_contain_text("2 m band simulated by the engine.")
                for receiver_setting in ("#src-center", "#src-rate", "#src-gain", "#src-agc", "#src-ppm"):
                    expect(page.locator(receiver_setting)).to_be_disabled()
                expect(page.locator("#src-dc")).to_be_checked()
                expect(page.locator("#src-dc")).to_be_enabled()

    def test_a_change_in_one_interface_shows_in_the_others(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    expect(page.locator("#src-dc")).to_be_checked()
                first.locator("#src-dc").uncheck()
                expect(second.locator("#src-dc")).not_to_be_checked()
                second.locator("#src-dc").check()
                expect(first.locator("#src-dc")).to_be_checked()

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_frequency_correction_is_whole_and_shared(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                field = first.locator("#src-ppm")
                for typed, shown in (("-2.6", "-3"), ("150", "100"), ("0", "0")):
                    field.fill(typed)
                    field.blur()        # the change applies when the field loses the focus
                    for page in (first, second):
                        expect(page.locator("#src-ppm")).to_have_value(shown)


if __name__ == "__main__":
    unittest.main()
