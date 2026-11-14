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
import json
import os
import re
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LocalServer
from radiolack.sources import Sources
from radiolack.store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcpRuns, FakeUpdates, free_port

ENABLED = os.environ.get("RADIOLACK_BROWSER_TESTS") == "1"
BROWSERS = ("chromium", "firefox", "webkit")
TIMEOUT_MS = 10_000     # enough for the first frames on a slow runner
SOURCE_PANEL = '#toolbar button[data-panel="source"]'
DEMOD_PANEL = '#toolbar button[data-panel="demod"]'
DISPLAY_PANEL = '#toolbar button[data-panel="display"]'
BANDS_PANEL = '#toolbar button[data-panel="bands"]'
SCANNERS_PANEL = '#toolbar button[data-panel="scanners"]'
SETTINGS_PANEL = '#toolbar button[data-panel="settings"]'
PREFERENCES_SAVE_MS = 1000  # how often an interface gives the engine its changed preferences (main.js)
AUDIO_FRAME = 2         # the first byte of an audio frame (docs/design/protocol.md)
DOT = "\N{MIDDLE DOT}"  # the status line's separator
PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True}     # a phone in portrait, with touch input
# An init script that keeps the audio held, as Safari does until a gesture:
# the audio context reads suspended, whatever the browser does. Headless
# browsers start it earlier, Firefox and WebKit at times without any gesture.
HELD_AUDIO = "Object.defineProperty(AudioContext.prototype, 'state', { get: () => 'suspended', configurable: true });"
FRAME_RATE = re.compile(rf" {DOT} \d+ fps$")     # the frames an interface receives per second, in its status line
# A remote device's: about 15. A page that loads late may take a backlog in
# a burst, so the test waits until the rate settles below 21.
REMOTE_FRAME_RATE = re.compile(rf" {DOT} (?:1?\d|20) fps$")
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
# Draws lines in a waterfall view of its own, 8 bins wide and 2 lines high,
# whose colour table gives each level code as its red: the red of each pixel,
# newest line first, after each move of the span.
WATERFALL_PROBE = """async () => {
  const { WaterfallView } = await import(new URL('waterfall-gl.js', document.baseURI).href);
  const canvas = document.createElement('canvas');
  const view = new WaterfallView(canvas, 4, () => {});
  view.resize(8, 2);
  view.setBins(8);
  const colours = new Uint8Array(256 * 4);
  for (let code = 0; code < 256; code += 1) colours.set([code, code, code, 255], code * 4);
  view.setColours(colours);
  const gl = canvas.getContext('webgl2'), pixels = new Uint8Array(8 * 2 * 4);
  const drawn = () => {
    view.draw({ head: 1, lines: 2, firstBin: 0, binsPerPixel: 1 });
    gl.readPixels(0, 0, 8, 2, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return [1, 0].map((y) => Array.from({ length: 8 }, (_, x) => pixels[(y * 8 + x) * 4]));
  };
  view.uploadRow(0, Uint8Array.of(10, 20, 30, 40, 50, 60, 70, 80));
  view.setOffsets(Float32Array.of(-3, 0, 0, 0));      // the span moved 3 bins up
  view.uploadRow(1, Uint8Array.of(1, 2, 3, 4, 5, 6, 7, 8));
  const up = drawn();
  view.setOffsets(Float32Array.of(-1, 2, 0, 0));      // then 2 bins down
  const down = drawn();
  view.setOffsets(Float32Array.of(-2.5, 0, 0, 0));    // and by half a bin
  return [up, down, drawn()];
}"""

if ENABLED:     # without Playwright, the tests then fail instead of being skipped
    from playwright.sync_api import Page, expect, sync_playwright


def kept_channel(frequency_hz: int, last_s: float) -> dict:
    """A channel of observations.json, first heard a day before ``last_s``,
    twice, both times at noon."""
    hours = [0] * 24
    hours[12] = 2
    return {"frequency_hz": frequency_hz, "first_s": last_s - 86_400, "last_s": last_s, "transmissions": 2,
            "active_s": 30.0, "margin_db": 20.0, "hours": hours}


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
    def interfaces(self, browser: str, url: str, count: int, **options) -> Iterator[list[Page]]:
        """``count`` interfaces at ``url``, each with the Source panel open:
        a click, which lets the audio start. ``self.audio[page]`` counts the
        audio frames each one receives, and ``self.sent[page]`` lists the
        messages each one sends, as text. ``options`` go to each browser
        context, such as a phone's viewport and touch input."""
        errors: list[str] = []
        self.audio: dict[Page, list[int]] = {}
        self.sent: dict[Page, list[str]] = {}
        with contextlib.ExitStack() as stack:
            pages = []
            for _ in range(count):
                context = self.browsers[browser].new_context(**options)
                stack.callback(context.close)
                page = self.watched_page(context, browser, errors)
                self.audio[page] = received = []
                self.sent[page] = sent = []

                def watch(socket, received=received, sent=sent) -> None:
                    socket.on("framereceived", lambda payload: received.append(1) if isinstance(
                        payload, bytes) and payload[:1] == bytes([AUDIO_FRAME]) else None)
                    socket.on("framesent", lambda payload: sent.append(payload) if isinstance(payload, str) else None)

                page.on("websocket", watch)
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
                expect(status).to_contain_text(FRAME_RATE)     # frames arrive
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
                    expect(page.locator("#sound")).to_be_hidden()      # the speaker is for touch input
                first.locator(DEMOD_PANEL).click()
                first.locator('#mode-group button[data-mode="AM"]').click()
                for page in (first, second):
                    expect(page.locator("#m-mode")).to_have_text("AM")
                    expect(page.locator("#m-bw")).to_have_text("8 kHz")
                expect(first.locator("#dm-audio-note")).to_be_hidden()     # AM plays too
                first.locator('#mode-group button[data-mode="NFM"]').click()
                expect(second.locator("#m-mode")).to_have_text("NFM")

    def test_held_audio_shows_a_speaker_and_a_touch_asks_it_to_start(self):
        # A tap activates the page only once the finger leaves the screen, and
        # only then does Safari on the iPhone let the audio start. The audio
        # stays held here, and the test counts the page's requests to start
        # it. On a touch screen, the speaker shows it held: nothing else does
        # out of the Demod panel.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                context = self.browsers[browser].new_context(**PHONE)
                self.addCleanup(context.close)
                context.add_init_script(   # before the interface's scripts
                    HELD_AUDIO + " window.resumes = 0;"
                    " AudioContext.prototype.resume = () => { window.resumes += 1; return Promise.resolve(); };")
                errors: list[str] = []
                page = self.watched_page(context, browser, errors)
                page.goto(url)
                expect(page.locator("#dm-audio-note")).to_have_text(
                    "Audio starts with a click, a tap or a key press on the page.")
                speaker = page.get_by_role("button", name="Audio")
                expect(speaker).to_be_visible()
                expect(speaker).to_have_attribute("aria-pressed", "false")      # crossed out
                for event in ("new PointerEvent('pointerup', { pointerType: 'touch' })", "new Event('touchend')"):
                    before = page.evaluate("() => window.resumes")
                    page.evaluate(f"() => window.dispatchEvent({event})")
                    self.assertGreater(page.evaluate("() => window.resumes"), before, event)
                before = page.evaluate("() => window.resumes")
                speaker.tap()
                self.assertGreater(page.evaluate("() => window.resumes"), before, "a tap on the speaker")
                expect(page.locator("#dm-vol")).to_have_value("70")    # which the tap leaves as it was
                self.assertEqual(errors, [])

    def test_the_speaker_mutes_and_unmutes_on_a_touch_screen(self):
        # Muting sets the volume to 0, so that the engine stops sending audio,
        # and unmuting brings the volume back. A server for each browser: the
        # page saves its preferences each second, so the engine may still
        # hold the muted volume when it closes.
        for browser in BROWSERS:
            url = self.serve()
            with self.subTest(browser=browser), self.interfaces(browser, url, 1, **PHONE) as [page]:
                self.wait_for_audio(page)
                speaker = page.get_by_role("button", name="Audio")
                expect(speaker).to_have_attribute("aria-pressed", "true")
                speaker.tap()
                expect(speaker).to_have_attribute("aria-pressed", "false")
                expect(page.locator("#dm-vol")).to_have_value("0")
                page.wait_for_timeout(500)      # audio frames already on their way
                received = len(self.audio[page])
                page.wait_for_timeout(1000)     # about 30 more if the engine still sent them
                self.assertEqual(len(self.audio[page]), received)
                speaker.tap()
                expect(speaker).to_have_attribute("aria-pressed", "true")
                expect(page.locator("#dm-vol")).to_have_value("70")
                self.wait_for_audio(page, received + 3)

    def test_the_audio_asks_safari_for_its_playback_session(self):
        # Safari plays the "playback" session with the ringer switch on
        # silent. The other browsers lack the API, which the test stands in
        # for, and the audio stays held.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                context = self.browsers[browser].new_context()
                self.addCleanup(context.close)
                context.add_init_script(   # before the interface's scripts
                    HELD_AUDIO + " Object.defineProperty(Navigator.prototype, 'audioSession',"
                    " { value: { type: 'auto' }, configurable: true });")
                errors: list[str] = []
                page = self.watched_page(context, browser, errors)
                page.goto(url)
                expect(page.locator("#dm-audio-note")).to_have_text(
                    "Audio starts with a click, a tap or a key press on the page.")
                self.assertEqual(page.evaluate("() => navigator.audioSession.type"), "playback")
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
                        f"Settings, bookmarks and history are saved in {Store(directory).location}.")
                    page.locator(DISPLAY_PANEL).click()
                    page.locator("#wf-pal").select_option("gray")
                    page.locator("#ds-grid").uncheck()
                    page.wait_for_timeout(2 * PREFERENCES_SAVE_MS)
                with self.interfaces(browser, url, 1) as [page]:
                    expect(page.locator("#wf-pal")).to_have_value("gray")
                    expect(page.locator("#ds-grid")).not_to_be_checked()

    def test_every_interface_lists_the_signals_the_engine_finds(self):
        url = self.serve()      # the simulated band, whose beacons are always on the air
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    page.locator(SCANNERS_PANEL).click()
                    expect(page.locator("#pk-on")).to_be_checked()
                    expect(page.locator("#pk-body button[data-tune]").first).to_be_visible()
                frequency = first.locator("#pk-body button[data-tune]").first.get_attribute("data-tune")
                first.locator(f'#pk-body button[data-ignore="{frequency}"]').click()
                for page in (first, second):
                    expect(page.locator("#pk-excluded")).to_have_text("1 ignored frequency.")
                    expect(page.locator(f'#pk-body button[data-tune="{frequency}"]')).to_have_count(0)
                second.locator("#pk-restore").click()
                for page in (first, second):    # the engine's state after the restore still has detection on
                    expect(page.locator("#pk-restore")).to_be_hidden()
                second.locator("#pk-on").uncheck()
                for page in (first, second):
                    expect(page.locator("#pk-on")).not_to_be_checked()
                    expect(page.locator("#pk-body .empty")).to_have_text("Detection is off")
                first.locator("#pk-on").check()
                for page in (first, second):
                    expect(page.locator("#pk-body button[data-tune]").first).to_be_visible()
                expect(first.locator("#pk-note")).to_be_hidden()

    def test_every_interface_follows_the_memory_scan_until_one_tunes_by_hand(self):
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            # A beacon of the simulated band, always on the air, and a bookmark outside the span.
            Store(directory).save(BOOKMARKS_FILE, {"bookmarks": [
                {"frequency_hz": 144_465_000, "name": "Beacon", "mode": "CW", "scan": True},
                {"frequency_hz": 433_500_000, "name": "Far away", "mode": "NFM", "scan": True}]})
            url = self.serve(store=Store(directory))
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    page.locator(SCANNERS_PANEL).click()
                    page.locator('#scan-tabs button[data-tab="memory"]').click()
                    expect(page.locator("#mem-list li")).to_have_text(
                        [re.compile(r"^Beacon"), re.compile(r"^Far away.*outside the span")])
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                first.locator("#mem-start").click()
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text(re.compile(r"^(Listening|Waiting): Beacon$"))
                    expect(page.locator("#mem-start")).to_have_text("Stop")
                    expect(page.locator("#m-mode")).to_have_text("CW")
                    expect(page.locator("#status")).to_contain_text(f"{DOT} memory scan")
                second.keyboard.press("ArrowRight")     # one step up: tuning by hand stops the scan
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text("Stopped: the receiver was tuned by hand.")
                    expect(page.locator("#mem-start")).to_have_text("Start")
                    expect(page.locator("#status")).not_to_contain_text("memory scan")
                expect(first.locator("#mem-note")).to_be_hidden()

    def test_every_interface_follows_the_memory_scan_across_windows_until_one_stops_it(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            # The receiver starts on 145 MHz: a bookmark in its span, one far away, one beyond its reach.
            Store(directory).save(BOOKMARKS_FILE, {"bookmarks": [
                {"frequency_hz": 10_000_000, "name": "Low", "mode": "AM", "scan": True},
                {"frequency_hz": 145_500_000, "name": "Near", "mode": "NFM", "scan": True},
                {"frequency_hz": 433_500_000, "name": "Far", "mode": "NFM", "scan": True}]})
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP), store=Store(directory))
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                home = first.locator("#src-center").input_value()
                for page in (first, second):
                    page.locator(SCANNERS_PANEL).click()
                    page.locator('#scan-tabs button[data-tab="memory"]').click()
                    expect(page.locator("#mem-list li")).to_have_text(
                        [re.compile(r"^Low.*out of reach"), re.compile(r"^Near[^·]*$"), re.compile(r"^Far[^·]*$")])
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                first.locator("#mem-start").click()
                for page in (first, second):    # Near and Far take a window each
                    expect(page.locator("#mem-state")).to_have_text("Watching 2 bookmarks, window 2 of 2")
                    expect(page.locator("#mem-start")).to_have_text("Stop")
                    expect(page.locator("#status")).to_contain_text(f"{DOT} memory scan")
                second.locator("#mem-start").click()
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                    expect(page.locator("#mem-start")).to_have_text("Start")
                    expect(page.locator("#src-center")).to_have_value(home)     # the receiver came back
                expect(first.locator("#mem-note")).to_be_hidden()

    def test_every_interface_follows_the_sweep_until_one_stops_it(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                home = first.locator("#src-center").input_value()
                for page in (first, second):
                    page.locator(SCANNERS_PANEL).click()
                    page.locator('#scan-tabs button[data-tab="sweep"]').click()
                    expect(page.locator("#sw-to")).to_have_value("440.0000")
                    expect(page.locator("#sw-state")).to_have_text("Stopped")
                field = first.locator("#sw-from")
                field.fill("1240")      # above the range's end: the whole range moves
                field.press("Enter")
                for page in (first, second):
                    expect(page.locator("#sw-from")).to_have_value("1240.0000")
                    expect(page.locator("#sw-to")).to_have_value("1250.0000")
                first.locator("#sw-window").press("ArrowRight")
                for page in (first, second):
                    expect(page.locator("#sw-window-v")).to_have_text("0.75 s")
                first.locator("#sw-start").click()
                for page in (first, second):     # 17 windows of 2.4 MHz, a quarter of the rate apart at most
                    expect(page.locator("#sw-state")).to_have_text(re.compile(r"^Pass 1, window ([2-9]|1\d) of 17$"))
                    expect(page.locator("#sw-start")).to_have_text("Stop")
                    expect(page.locator("#status")).to_contain_text(f"{DOT} sweep")
                second.locator("#sw-start").click()
                for page in (first, second):
                    expect(page.locator("#sw-state")).to_have_text("Stopped")
                    expect(page.locator("#sw-start")).to_have_text("Start")
                    expect(page.locator("#status")).not_to_contain_text("sweep")
                    expect(page.locator("#src-center")).to_have_value(home)     # the receiver came back
                expect(first.locator("#sw-note")).to_be_hidden()

    def test_the_history_tunes_bookmarks_and_clears_the_channels_kept(self):
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            heard = time.time()
            Store(directory).save(OBSERVATIONS_FILE, {"channels": [
                kept_channel(433_500_000, heard - 7200), kept_channel(145_500_000, heard - 600)]})
            url = self.serve(store=Store(directory))    # the simulated band, which is not recorded
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    page.locator(SCANNERS_PANEL).click()
                    page.locator('#scan-tabs button[data-tab="history"]').click()
                    # The latest heard first.
                    expect(page.locator("#hist-body button[data-tune]")).to_have_text(["145.5000", "433.5000"])
                    expect(page.locator("#hist-summary")).to_have_text(
                        "2 channels. The simulated band is not recorded.")
                expect(first.locator("#hist-body tr").first).to_contain_text("10 min ago")
                tune = first.locator('#hist-body button[data-tune="145500000"]')
                expect(tune).to_have_attribute("title", re.compile(
                    rf"^Listen {DOT} first heard .* most often from 12:00, on the engine's computer$"))
                tune.click()
                for page in (first, second):    # the VFO is shared
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145500000")
                first.locator('#hist-body button[data-mark="145500000"]').click()
                expect(first.locator("#hist-note")).to_have_text("145.5000 MHz added to the bookmarks as Bookmark 1.")
                expect(first.locator("#marks .mark")).to_have_text(["Bookmark 1"])
                clear = first.locator('#hist-body button[data-clear="433500000"]')
                clear.click()       # a first press asks for a second one
                expect(clear).to_have_text("Confirm")
                clear.click()
                expect(first.locator("#hist-body button[data-tune]")).to_have_text(["145.5000"])
                expect(first.locator("#hist-note")).to_be_hidden()
                # The other interface sees the change once it asks again, as when its tab shows.
                second.locator('#scan-tabs button[data-tab="peaks"]').click()
                second.locator('#scan-tabs button[data-tab="history"]').click()
                expect(second.locator("#hist-body button[data-tune]")).to_have_text(["145.5000"])
                clear_all = second.locator("#hist-clear-all")
                clear_all.click()
                expect(clear_all).to_have_text("Confirm: clear all")
                clear_all.click()
                expect(second.locator("#hist-body .empty")).to_have_text("No channel recorded yet")
                expect(second.locator("#hist-summary")).to_have_text("The simulated band is not recorded.")
                expect(clear_all).to_be_hidden()
                expect(second.locator("#hist-note")).to_be_hidden()

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

    def test_remote_access_stays_on_while_this_computer_has_no_address(self):
        self.enterContext(mock.patch("radiolack.server.ADDRESS_CHECK_S", 0.05))
        addresses: list[str] = []
        for browser in BROWSERS:
            addresses[:] = [LOOPBACK]
            url = self.serve(remote_addresses=lambda: list(addresses), host="radiolack-test")
            port = free_port()
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page], \
                    self.assertLogs("radiolack.server", "WARNING"):
                page.locator(SETTINGS_PANEL).click()
                page.locator("#st-remote-port").fill(str(port))
                page.locator("#st-remote-port").blur()
                page.locator("#st-remote").click()
                expect(page.locator("#st-remote-url")).to_have_text(f"https://{LOOPBACK}:{port}/")
                addresses.clear()
                expect(page.locator("#st-remote-msg")).to_have_text(
                    "This computer has no address on a local network for now: remote access "
                    "listens again once it has one.")
                expect(page.locator("#st-remote")).to_be_checked()
                expect(page.locator("#st-remote-url-field")).to_be_hidden()
                expect(page.locator("#st-remote-code")).to_have_text(re.compile(r"^[2-9A-Z]{4}-[2-9A-Z]{4}$"))
                addresses.append(LOOPBACK)
                expect(page.locator("#st-remote-url")).to_have_text(f"https://{LOOPBACK}:{port}/")
                expect(page.locator("#st-remote-url-field")).to_be_visible()
                expect(page.locator("#st-remote-msg")).to_be_empty()

    def test_a_device_pairs_with_the_code_then_shares_the_receiver_until_forgotten(self):
        for browser in BROWSERS:
            url = self.serve(remote_addresses=lambda: [LOOPBACK], host="radiolack-test")
            port = free_port()
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [computer]:
                computer.locator(SETTINGS_PANEL).click()
                computer.locator("#st-remote-port").fill(str(port))
                computer.locator("#st-remote-port").blur()
                computer.locator("#st-remote").click()
                expect(computer.locator("#st-remote-url")).to_have_text(f"https://{LOOPBACK}:{port}/")
                expect(computer.locator("#st-remote-urls")).to_have_text(f"Or: https://radiolack-test.local:{port}/")
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
                    expect(device.locator("#status")).to_contain_text(REMOTE_FRAME_RATE)     # half the frames
                    device.locator(SETTINGS_PANEL).click()
                    expect(device.locator("#st-remote-client")).to_be_visible()
                    for section in ("#st-storage-section", "#st-update-section", "#st-remote-section"):
                        expect(device.locator(section)).to_be_hidden()
                    expect(device.locator("#status")).not_to_contain_text("remote access")
                    # The device keeps its own settings when its page reloads, as Safari's often do.
                    device.locator(DISPLAY_PANEL).click()
                    device.locator("#wf-pal").select_option("gray")
                    device.wait_for_timeout(2 * PREFERENCES_SAVE_MS)
                    device.reload()
                    expect(device.locator("#wf-pal")).to_have_value("gray")
                    expect(computer.locator("#st-remote-devices li.device")).to_contain_text("connected")
                    computer.locator("#st-remote-devices button[data-forget]").click()
                    expect(device.locator("#pair-form")).to_be_visible()    # reloaded: the pairing page again
                    expect(computer.locator("#st-remote-devices")).to_have_text("No paired device")
                computer.locator("#st-remote").click()
                expect(computer.locator("#st-remote-info")).to_be_hidden()
                expect(computer.locator("#st-remote-port")).to_be_enabled()
                computer.reload()       # this computer's interfaces keep their own settings
                expect(computer.locator("#status")).to_contain_text("fps")     # frames, which follow the preferences
                expect(computer.locator("#wf-pal")).to_have_value("classic")

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

    @staticmethod
    def readout_at(page: Page, fraction: float) -> tuple[float, str]:
        """The readout under the pointer, moved over the spectrum at ``fraction``
        of the way from its first pixel to its last: its frequency in MHz, and
        its text."""
        box = page.locator("#specwrap").bounding_box()
        page.mouse.move(box["x"] + 1 + (box["width"] - 2) * fraction, box["y"] + box["height"] * 0.3)
        text = page.locator("#hoverinfo").inner_text()
        return float(text.split(" ", 1)[0]), text

    def view_edges(self, page: Page) -> tuple[float, float]:
        """The frequencies, in MHz, at the spectrum's first and last pixels."""
        return self.readout_at(page, 0)[0], self.readout_at(page, 1)[0]

    @staticmethod
    def band_title_at(page: Page, fraction: float) -> str:
        """The band strip's title with the pointer over it at ``fraction`` of
        the way from its first pixel to its last."""
        box = page.locator("#band").bounding_box()
        page.mouse.move(box["x"] + 1 + (box["width"] - 2) * fraction, box["y"] + box["height"] / 2)
        return page.locator("#band").get_attribute("title")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_view_goes_past_the_span_of_a_receiver_that_tunes(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # 2.4 MHz around 145 MHz, the VFO on 145.6
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                page.keyboard.press("Escape")       # the Source panel lies over the spectrum
                (low, low_text), (high, high_text) = self.readout_at(page, 0), self.readout_at(page, 1)
                self.assertAlmostEqual(low, 143.8, delta=0.01)
                self.assertAlmostEqual(high, 146.2, delta=0.01)
                self.assertRegex(low_text, rf" MHz {DOT} -?\d+ dB")      # levels: in the span
                self.assertTrue(self.band_title_at(page, 0.75).startswith(
                    f"2 m amateur {DOT} Repeater outputs {DOT} 145.575\N{EN DASH}145.794 MHz"))
                page.keyboard.press("-")    # one step out passes the detent at the span's width
                (low, low_text), (high, high_text) = self.readout_at(page, 0), self.readout_at(page, 1)
                self.assertAlmostEqual(high - low, 2.4 * 1.5 / 1.15, delta=0.01)
                for text in (low_text, high_text):
                    self.assertIn(f" MHz {DOT} outside the span", text)
                page.keyboard.press("+")    # one step in stops at the span, which it frames
                low, high = self.view_edges(page)
                self.assertAlmostEqual(low, 143.8, delta=0.01)
                self.assertAlmostEqual(high, 146.2, delta=0.01)
                for _ in range(9):
                    page.keyboard.press("-")
                low, high = self.view_edges(page)
                self.assertAlmostEqual(high - low, 24, delta=0.05)     # ten spans at most
                expect(page.locator("#span-fit")).to_be_visible()
                # Zoomed out, a band whose ranges would be too narrow shows as one block.
                where = (145 - low) / (high - low)
                self.assertEqual(self.band_title_at(page, where),
                                 f"2 m amateur {DOT} 144.000\N{EN DASH}146.000 MHz {DOT} drag: spectrum height")
                page.mouse.down()
                page.mouse.up()             # a click frames the band
                low, high = self.view_edges(page)
                self.assertAlmostEqual(low, 143.84, delta=0.01)
                self.assertAlmostEqual(high, 146.16, delta=0.01)
                page.keyboard.press("0")    # frames the span
                low, high = self.view_edges(page)
                self.assertAlmostEqual(low, 143.8, delta=0.01)
                self.assertAlmostEqual(high, 146.2, delta=0.01)
                expect(page.locator("#span-fit")).to_be_hidden()
                expect(page.locator("#src-center")).to_have_value("145.0000")    # the view alone moved
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145600000")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_click_outside_the_span_moves_it_for_every_interface(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # each browser starts on 145 MHz
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                for page in (first, second):
                    page.keyboard.press("Escape")   # the Source panel lies over the spectrum
                for _ in range(9):
                    first.keyboard.press("-")       # ten spans, from 127.6 to 151.6 MHz
                view = self.view_edges(first)
                _, text = self.readout_at(first, 0.08)
                self.assertRegex(text, rf"^\d+\.\d+ MHz {DOT} outside the span {DOT} Airband$")
                expect(first.locator("#tune-tip")).to_have_text("Click: tune here")
                expect(first.locator("#span-preview")).to_be_visible()
                first.mouse.down()
                first.mouse.up()
                expect(first.locator("#tune-tip")).to_be_hidden()      # at once, with the preview
                expect(first.locator("#span-preview")).to_be_hidden()
                for page in (first, second):        # once frames come from there, the airband's mode
                    expect(page.locator("#m-mode")).to_have_text("AM")
                vfo = int(first.locator("#freq").get_attribute("aria-valuenow"))
                centre = (vfo - 75_000) / 1e6       # a 32nd of the span below the VFO
                for page in (first, second):
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", str(vfo))
                    expect(page.locator("#src-center")).to_have_value(f"{centre:.4f}")
                expect(first.locator("#span-next")).to_be_hidden()
                low, high = self.view_edges(first)  # a view looking elsewhere stays
                self.assertAlmostEqual(low, view[0], delta=0.001)
                self.assertAlmostEqual(high, view[1], delta=0.001)
                low, high = self.view_edges(second)     # one that framed the span frames the new one
                self.assertAlmostEqual(low, centre - 1.2, delta=0.01)
                self.assertAlmostEqual(high, centre + 1.2, delta=0.01)

    def waterfall_readout_at(self, page: Page, frequency_mhz: float, age: int) -> str:
        """The readout under the pointer, moved over the waterfall at
        ``frequency_mhz``, which the view shows, on the line ``age`` lines
        below the newest."""
        low, high = self.view_edges(page)
        box = page.locator("#wfwrap").bounding_box()
        page.mouse.move(box["x"] + 1 + (box["width"] - 2) * (frequency_mhz - low) / (high - low), box["y"] + age + 0.5)
        return page.locator("#hoverinfo").inner_text()

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_step_into_the_edge_moves_the_span_once_and_the_waterfall_slides_with_it(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        age = 60                # a waterfall line older than the move: 2.4 s at 25 lines a second
        for browser in BROWSERS:
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # each browser starts on 145 MHz, the VFO on 145.6
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                page.locator(DEMOD_PANEL).click()
                page.locator("#dm-step").select_option("25000")
                page.locator(DEMOD_PANEL).click()   # closes the panel, which lies over the spectrum
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                while " ago" not in self.waterfall_readout_at(page, 145.5, age):
                    self.assertLess(time.monotonic(), deadline, f"the waterfall has no line {age} lines down")
                    page.wait_for_timeout(100)
                freq = page.locator("#freq")
                freq.focus()
                for _ in range(4):      # steps of 250 kHz: the second goes past 145.96 MHz, into the span's upper tenth
                    freq.press("Shift+ArrowUp")
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                while abs(self.view_edges(page)[0] - 144.825) > 0.01:     # the view frames the new span
                    self.assertLess(time.monotonic(), deadline, "the span did not move")
                    page.wait_for_timeout(50)
                # The waterfall's older lines keep their frequencies, within the new span only.
                self.assertNotIn(" dB", self.waterfall_readout_at(page, 146.7, age))
                self.assertRegex(self.waterfall_readout_at(page, 145.5, age), rf"^145\.\d+ MHz {DOT} -?\d+ dB {DOT} \d+ s ago")
                # One move, 75 kHz below the step that went past: the steps made meanwhile add up.
                expect(page.locator("#src-center")).to_have_value("146.0250")
                expect(freq).to_have_attribute("aria-valuenow", "146600000")
                centres = [message["centre_hz"] for message in map(json.loads, self.sent[page]) if "centre_hz" in message]
                self.assertEqual(centres, [146_025_000])
                low, high = self.view_edges(page)
                self.assertAlmostEqual(high, 147.225, delta=0.01)

    def test_the_waterfall_draws_each_line_at_its_frequencies(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                up, down, half = page.evaluate(WATERFALL_PROBE)
                self.assertEqual(up, [[1, 2, 3, 4, 5, 6, 7, 8], [40, 50, 60, 70, 80, 0, 0, 0]])
                self.assertEqual(down, [[0, 0, 1, 2, 3, 4, 5, 6], [20, 30, 40, 50, 60, 70, 80, 0]])
                self.assertEqual(half[1], [30, 40, 50, 60, 70, 80, 0, 0])

    def test_steps_stop_at_the_edge_of_the_simulated_span(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.locator(DEMOD_PANEL).click()
                page.locator("#dm-step").select_option("25000")
                page.locator(DEMOD_PANEL).click()   # closes the panel, which lies over the spectrum
                span = self.view_edges(page)
                freq = page.locator("#freq")
                freq.focus()
                for _ in range(8):      # steps of 250 kHz, from 145.6 MHz: a source that does not tune keeps its span
                    freq.press("Shift+ArrowUp")
                expect(freq).to_have_attribute("aria-valuenow", "146200000")
                self.assertEqual(self.view_edges(page), span)
                self.assertEqual([message for message in self.sent[page] if "centre_hz" in message], [])

    def test_the_view_stays_in_the_simulated_span_and_comes_back_to_the_vfo_or_the_span(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Source panel lies over the spectrum
                fit = page.get_by_role("button", name="Show the whole span")
                expect(fit).to_be_hidden()          # the view frames the span
                span = self.view_edges(page)
                for _ in range(3):
                    page.keyboard.press("-")        # a source that does not tune shows its span at most
                self.assertEqual(self.view_edges(page), span)
                for _ in range(3):
                    page.keyboard.press("+")
                box = page.locator("#specwrap").bounding_box()
                y = box["y"] + box["height"] * 0.3
                page.mouse.move(box["x"] + box["width"] * 0.1, y)
                page.mouse.down()
                page.mouse.move(box["x"] + box["width"] * 0.9, y, steps=10)     # the view goes down, the VFO stays
                page.mouse.up()
                vfo = int(page.locator("#freq").get_attribute("aria-valuenow"))
                back = page.locator("#vfo-return")
                expect(back).to_have_text(f"{vfo / 1e6:.4f} MHz \N{RIGHTWARDS ARROW}")
                expect(back).to_have_attribute("aria-label", f"Show the VFO, at {vfo / 1e6:.4f} MHz")
                back.click()
                expect(back).to_be_hidden()
                low, high = self.view_edges(page)   # around the VFO, at the same zoom
                self.assertAlmostEqual((low + high) / 2, vfo / 1e6, delta=0.001)
                self.assertAlmostEqual(high - low, (span[1] - span[0]) / 1.5 ** 3, delta=0.01)
                fit.click()
                expect(fit).to_be_hidden()
                self.assertEqual(self.view_edges(page), span)


if __name__ == "__main__":
    unittest.main()
