"""Browser tests: the interface in headless Chromium, Firefox and WebKit,
driven by Playwright against a server on an ephemeral port, with the
simulated band or the fake rtl_tcp program: no receiver. Remote access
listens on 127.0.0.1 only, on another ephemeral port.

They launch browsers, so they run only when the RADIOLACK_BROWSER_TESTS
environment variable is 1, as in CI, and then need Playwright and its
browsers (README.md, Tests). Each interface opens in its own browser
context, like a separate device, and any error in its page fails the test.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path

from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LocalServer
from radiolack.sources import Sources
from radiolack.store import SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcpRuns, FakeUpdates, free_port

ENABLED = os.environ.get("RADIOLACK_BROWSER_TESTS") == "1"
BROWSERS = ("chromium", "firefox", "webkit")
TIMEOUT_MS = 10_000     # enough for the first frames on a slow runner
SOURCE_PANEL = '#toolbar button[data-panel="source"]'
DEMOD_PANEL = '#toolbar button[data-panel="demod"]'
DISPLAY_PANEL = '#toolbar button[data-panel="display"]'
BANDS_PANEL = '#toolbar button[data-panel="bands"]'
SETTINGS_PANEL = '#toolbar button[data-panel="settings"]'
PREFERENCES_SAVE_MS = 1000  # how often an interface gives the engine its changed preferences (main.js)
AUDIO_FRAME = 2         # the first byte of an audio frame (docs/design/protocol.md)
DOT = "\N{MIDDLE DOT}"  # the status line's separator
# WebKit 27.2 (Playwright 1.64) refuses its own stylesheet for every <select>
# under the interface's style-src 'self': a WebKit bug without visible effect,
# which WebKitGTK 2.50 does not have. WebKit words a refused inline style the
# same way, but Chromium and Firefox report those in their own words.
WEBKIT_SELECT_REFUSAL = "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"
# Chromium and WebKit report a refused request in the console, such as the
# answer to a wrong access code.
WRONG_CODE_REFUSAL = "Failed to load resource: the server responded with a status of 403 (Forbidden)"
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

    def serve(self, source: RtlTcpSource | None = None, store: Store | None = None,
              sources: Sources | None = None, updates: FakeUpdates | None = None,
              remote_addresses: Callable[[], list[str]] | None = None, host: str | None = None) -> str:
        """The address of a server started for this test."""
        server = LocalServer(source=source, store=store, sources=sources, updates=updates,
                             remote_addresses=remote_addresses, host=host)
        server.start()
        self.addCleanup(server.stop)
        return server.url

    @contextlib.contextmanager
    def interfaces(self, browser: str, url: str, count: int) -> Iterator[list[Page]]:
        """``count`` interfaces at ``url``, each with the Source panel open:
        a click, which lets the audio start. ``self.audio[page]`` counts the
        audio frames each one receives."""
        errors: list[str] = []
        self.audio: dict[Page, list[int]] = {}
        with contextlib.ExitStack() as stack:
            pages = []
            for _ in range(count):
                context = self.browsers[browser].new_context()
                stack.callback(context.close)
                page = self.watched_page(context, browser, errors)
                self.audio[page] = received = []
                page.on("websocket", lambda socket, received=received: socket.on(
                    "framereceived",
                    lambda payload: received.append(1) if isinstance(payload, bytes) and payload[:1] == bytes(
                        [AUDIO_FRAME]) else None))
                page.goto(url)
                page.locator(SOURCE_PANEL).click()
                pages.append(page)
            yield pages
        self.assertEqual(errors, [], "errors in the interface")

    @contextlib.contextmanager
    def remote_device(self, browser: str, allowed: tuple[str, ...] = ()) -> Iterator[Page]:
        """A page in a browser context of its own, like a phone on the local
        network, which accepts remote access's certificate without having
        installed it. Console errors other than the ``allowed`` ones fail
        the test."""
        errors: list[str] = []
        context = self.browsers[browser].new_context(ignore_https_errors=True)
        try:
            yield self.watched_page(context, browser, errors)
        finally:
            context.close()
        self.assertEqual([error for error in errors if error not in allowed], [], "errors in the remote device's pages")

    @staticmethod
    def watched_page(context, browser: str, errors: list[str]) -> Page:
        """A new page whose console errors and uncaught exceptions go to ``errors``."""

        def console(message) -> None:
            if message.type != "error":
                return
            if browser == "webkit" and message.text.startswith(WEBKIT_SELECT_REFUSAL):
                return
            errors.append(message.text)

        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        page.on("console", console)
        page.on("pageerror", lambda error: errors.append(str(error)))
        return page

    def test_shows_the_simulated_band_and_its_settings(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                status = page.locator("#status")
                expect(status).to_contain_text(f"Synthetic source {DOT} 2.400 MS/s")
                expect(status).to_contain_text(re.compile(rf" {DOT} \d+ fps$"))     # frames arrive
                expect(page.locator("#src-device")).to_have_value("synthetic")
                expect(page.locator("#src-note")).to_contain_text("2 m band simulated by the engine.")
                expect(page.locator("#src-address")).to_be_hidden()
                for receiver_setting in ("#src-center", "#src-rate", "#src-gain", "#src-agc", "#src-ppm"):
                    expect(page.locator(receiver_setting)).to_be_disabled()
                expect(page.locator("#src-dc")).to_be_checked()
                expect(page.locator("#src-dc")).to_be_enabled()

    def test_a_newer_version_shows_until_closed_and_settings_check_again(self):
        updates = FakeUpdates(UpdateResult("available", latest="9.9.9"))
        url = self.serve(updates=updates)
        text = "Radiolack 9.9.9 is available: install it with the system's software updates."
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    expect(page.locator("#notice-text")).to_have_text(text)
                first.locator("#notice-close").click()
                expect(first.locator("#notice")).to_be_hidden()
                expect(second.locator("#notice")).to_be_visible()       # closed on its own page only
                for page in (first, second):
                    page.locator(SETTINGS_PANEL).click()
                    expect(page.locator("#st-update")).to_have_text(text)
                    expect(page.locator("#st-update-start")).to_be_checked()
                first.locator("#st-update-start").uncheck()
                expect(second.locator("#st-update-start")).not_to_be_checked()
                second.locator("#st-update-start").check()
                expect(first.locator("#st-update-start")).to_be_checked()
                checks = updates.checks
                first.locator("#st-update-check").click()
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                while updates.checks == checks:
                    self.assertLess(time.monotonic(), deadline, "Check now did not check")
                    time.sleep(0.05)
                expect(first.locator("#st-update")).to_have_text(text)
                expect(first.locator("#st-update-check")).to_be_enabled()
                expect(first.locator("#notice")).to_be_hidden()         # the same version stays closed

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

    def wait_for_audio(self, page: Page, frames: int = 3) -> None:
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while len(self.audio[page]) < frames:
            self.assertLess(time.monotonic(), deadline,
                            "no audio arrived (Firefox plays none without a sound server: README.md, Tests)")
            page.wait_for_timeout(100)      # lets Playwright deliver the page's events

    def test_audio_starts_with_a_click_and_the_vfo_is_shared(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    self.wait_for_audio(page)
                    expect(page.locator("#m-mode")).to_have_text("NFM")
                first.locator(DEMOD_PANEL).click()
                first.locator('#mode-group button[data-mode="AM"]').click()
                for page in (first, second):
                    expect(page.locator("#m-mode")).to_have_text("AM")
                    expect(page.locator("#m-bw")).to_have_text("8 kHz")
                expect(first.locator("#dm-audio-note")).to_be_hidden()     # AM plays too
                first.locator('#mode-group button[data-mode="NFM"]').click()
                expect(second.locator("#m-mode")).to_have_text("NFM")

    def test_the_end_of_a_touch_asks_the_audio_to_start(self):
        # A tap activates the page only once the finger leaves the screen, and
        # only then does Safari on the iPhone let the audio start. Headless
        # browsers start it earlier, Firefox at times without any gesture, so
        # the audio stays suspended here and the test counts the page's
        # requests to start it.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                context = self.browsers[browser].new_context()
                self.addCleanup(context.close)
                context.add_init_script(   # before the interface's scripts
                    "window.resumes = 0;"
                    " AudioContext.prototype.resume = () => { window.resumes += 1; return Promise.resolve(); };")
                errors: list[str] = []
                page = self.watched_page(context, browser, errors)
                page.goto(url)
                expect(page.locator("#dm-audio-note")).to_have_text(
                    "Audio starts with a click, a tap or a key press on the page.")
                for event in ("new PointerEvent('pointerup', { pointerType: 'touch' })", "new Event('touchend')"):
                    before = page.evaluate("() => window.resumes")
                    page.evaluate(f"() => window.dispatchEvent({event})")
                    self.assertGreater(page.evaluate("() => window.resumes"), before, event)
                self.assertEqual(errors, [])

    def test_bookmarks_are_shared(self):
        for browser in BROWSERS:
            url = self.serve()      # no bookmark yet
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                first.locator(BANDS_PANEL).click()
                expect(first.locator("#bm-list .empty")).to_have_text("No bookmarks")
                first.locator("#bm-add").click()
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["Bookmark 1"])
                first.locator("#bm-list button[data-edit]").click()
                name = first.locator("#bm-list input")
                expect(name).to_be_focused()
                name.fill("Not kept")
                name.press("Escape")        # cancels the new name, and leaves the panel open
                expect(first.locator("#bm-list .item .name")).to_have_text(["Bookmark 1"])
                expect(first.locator("#panel")).to_be_visible()
                expect(first.locator("#bm-list button[data-edit]")).to_be_focused()
                first.locator("#bm-list button[data-edit]").click()
                name.fill("  Calling ")
                name.press("Enter")
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["Calling"])
                    expect(page.locator("#marks .mark")).to_have_text(["Calling"])
                expect(first.locator("#bm-list button[data-edit]")).to_be_focused()
                second.locator(BANDS_PANEL).click()
                second.locator("#bm-list button[data-edit]").click()
                second.locator("#bm-list input").fill("FM calling")
                second.locator("#bm-filter").click()       # leaving the row saves the name
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["FM calling"])
                second.locator("#bm-list button[data-edit]").click()
                second.locator("#bm-list button[data-del]").click()     # while the name's field has the focus
                for page in (first, second):
                    expect(page.locator("#bm-list .empty")).to_have_text("No bookmarks")
                    expect(page.locator("#marks .mark")).to_have_count(0)
                expect(first.locator("#bm-note")).to_be_hidden()

    def test_preferences_are_kept_for_the_next_interfaces(self):
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            url = self.serve(store=Store(directory))    # no preference yet
            with self.subTest(browser=browser):
                with self.interfaces(browser, url, 1) as [page]:
                    page.locator(SETTINGS_PANEL).click()
                    expect(page.locator("#st-storage")).to_have_text(
                        f"Settings and bookmarks are saved in {Store(directory).location}.")
                    page.locator(DISPLAY_PANEL).click()
                    page.locator("#wf-pal").select_option("gray")
                    page.locator("#ds-grid").uncheck()
                    page.wait_for_timeout(2 * PREFERENCES_SAVE_MS)
                with self.interfaces(browser, url, 1) as [page]:
                    expect(page.locator("#wf-pal")).to_have_value("gray")
                    expect(page.locator("#ds-grid")).not_to_be_checked()

    def test_remote_access_says_why_it_cannot_turn_on(self):
        url = self.serve()      # which finds no address on a local network
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                page.locator(SETTINGS_PANEL).click()
                expect(page.locator("#st-remote")).not_to_be_checked()
                expect(page.locator("#st-remote-client")).to_be_hidden()
                page.locator("#st-remote").click()
                expect(page.locator("#st-remote-msg")).to_have_text(
                    "This computer has no address on a local network: connect it to one, "
                    "then turn remote access on.")
                expect(page.locator("#st-remote")).not_to_be_checked()
                expect(page.locator("#st-remote-info")).to_be_hidden()
                expect(page.locator("#st-remote-devices")).to_have_text("No paired device")
                expect(page.locator("#status")).not_to_contain_text("remote access")

    def test_a_device_pairs_with_the_code_then_shares_the_receiver_until_forgotten(self):
        for browser in BROWSERS:
            url = self.serve(remote_addresses=lambda: [LOOPBACK], host="radiolack-test")
            port = free_port()
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [computer]:
                computer.locator(SETTINGS_PANEL).click()
                computer.locator("#st-remote-port").fill(str(port))
                computer.locator("#st-remote-port").blur()
                computer.locator("#st-remote").click()
                expect(computer.locator("#st-remote-url")).to_have_text(f"https://radiolack-test.local:{port}/")
                expect(computer.locator("#st-remote-urls")).to_have_text(f"Or: https://{LOOPBACK}:{port}/")
                expect(computer.locator("#st-remote-code")).to_have_text(re.compile(r"^[2-9A-Z]{4}-[2-9A-Z]{4}$"))
                expect(computer.locator("#st-remote-port")).to_be_disabled()
                expect(computer.locator("#status")).to_contain_text("remote access on")
                code = computer.locator("#st-remote-code").inner_text()
                with self.remote_device(browser, allowed=(WRONG_CODE_REFUSAL,)) as device:
                    device.goto(f"https://{LOOPBACK}:{port}/")
                    expect(device.locator('a[href="certificate"]')).to_be_visible()
                    device.locator("#pair-code").fill("ABCD-EFGH")
                    device.locator("#pair-submit").click()
                    expect(device.locator("#pair-msg")).to_have_text("Wrong code.")
                    device.locator("#pair-code").fill(code.lower())
                    device.locator("#pair-submit").click()
                    expect(device.locator("#status")).to_contain_text("MS/s")      # the interface, with frames
                    device.locator(SETTINGS_PANEL).click()
                    expect(device.locator("#st-remote-client")).to_be_visible()
                    for section in ("#st-storage-section", "#st-update-section", "#st-remote-section"):
                        expect(device.locator(section)).to_be_hidden()
                    expect(device.locator("#status")).not_to_contain_text("remote access")
                    expect(computer.locator("#st-remote-devices li.device")).to_contain_text("connected")
                    computer.locator("#st-remote-devices button[data-forget]").click()
                    expect(device.locator("#pair-form")).to_be_visible()    # reloaded: the pairing page again
                    expect(computer.locator("#st-remote-devices")).to_have_text("No paired device")
                computer.locator("#st-remote").click()
                expect(computer.locator("#st-remote-info")).to_be_hidden()
                expect(computer.locator("#st-remote-port")).to_be_enabled()

    def test_a_stream_refused_before_it_opened_asks_for_a_reload_instead_of_reloading(self):
        # As if the browser sent the session cookie with the page but not with
        # the stream: reloading would bring the same page back, again and again.
        # Every stream is refused before its hello, as remote access would.
        def refuse(socket) -> None:
            server = socket.connect_to_server()
            server.on_message(lambda _message: socket.close(code=4001, reason="This device is not paired."))

        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                context = self.browsers[browser].new_context()
                self.addCleanup(context.close)
                errors: list[str] = []
                page = self.watched_page(context, browser, errors)
                page.route_web_socket(re.compile(r"/stream$"), refuse)
                loaded: list[str] = []
                page.on("request", lambda request: loaded.append(request.url)
                        if request.is_navigation_request() else None)
                page.goto(url)
                expect(page.locator("#status")).to_contain_text("This device is not paired: reload the page")
                self.assertEqual(loaded, [url])
                self.assertEqual(errors, [])

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_frequency_correction_is_whole_and_shared(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                expect(first.locator("#src-address")).to_be_hidden()   # the source picks its own address
                field = first.locator("#src-ppm")
                for typed, shown in (("-2.6", "-3"), ("150", "100"), ("0", "0")):
                    field.fill(typed)
                    field.blur()        # the change applies when the field loses the focus
                    for page in (first, second):
                        expect(page.locator("#src-ppm")).to_have_value(shown)

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_receiver_list_switches_the_source_for_every_interface(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
        address = f"{LOOPBACK}:{free_port()}"     # the rtl_tcp server used last: nothing listens there
        Store(directory).save(SETTINGS_FILE, {"receivers": {"rtl_tcp": {"source": {"address": address}}}})
        url = self.serve(sources=Sources(rtl_tcp_program=FAKE_RTL_TCP), store=Store(directory))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                receiver = first.locator("#src-device")
                expect(receiver).to_be_enabled()
                expect(receiver).to_have_value("synthetic")
                expect(first.locator('#src-device option[value="soapy"]')).to_be_disabled()
                expect(first.locator("#src-note")).to_contain_text("Choose a receiver in the list above.")
                receiver.select_option("rtl_sdr")
                for page in (first, second):
                    expect(page.locator("#src-device")).to_have_value("rtl_sdr")
                    expect(page.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                receiver.select_option("rtl_tcp")
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(
                        f"Waiting for rtl_tcp at {address}: connection refused\N{HORIZONTAL ELLIPSIS}")
                    expect(page.locator("#src-address")).to_have_value(address)
                second.locator("#src-device").select_option("synthetic")
                for page in (first, second):
                    expect(page.locator("#src-device")).to_have_value("synthetic")
                    expect(page.locator("#src-address")).to_be_hidden()

    def test_the_rtl_tcp_server_address_changes_in_the_source_panel(self):
        # Nothing listens on these ports: the source waits, refused.
        url = self.serve(RtlTcpSource(LOOPBACK, free_port()))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text("connection refused")
                field = first.locator("#src-address")
                expect(field).to_be_enabled()
                port = free_port()
                address = f"{LOOPBACK}:{port}"
                field.fill(str(port))           # a port alone: on this computer
                field.press("Enter")
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(
                        f"Waiting for rtl_tcp at {address}: connection refused\N{HORIZONTAL ELLIPSIS}")
                    expect(page.locator("#src-address")).to_have_value(address)    # as the engine reads it
                expect(field).to_be_focused()
                # What the user is typing stays while another interface changes a setting.
                field.fill(f"{LOOPBACK}:")
                second.locator("#src-dc").uncheck()
                expect(first.locator("#src-dc")).not_to_be_checked()
                expect(field).to_have_value(f"{LOOPBACK}:")
                field.press("Enter")
                expect(first.locator("#src-note")).to_contain_text(
                    "Change refused: the port must be a whole number from 1 to 65535.")
                expect(field).to_have_value(address)        # the address in use, the field still focused
                expect(field).to_be_focused()
                expect(second.locator("#status")).to_contain_text(address)


if __name__ == "__main__":
    unittest.main()
