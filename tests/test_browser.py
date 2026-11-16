"""Browser tests: the interface in headless Chromium, Firefox and WebKit,
driven by Playwright against a server on an ephemeral port, with the
simulated band or the fake rtl_tcp program: no receiver. Remote access
listens on 127.0.0.1 only, on another ephemeral port. Recordings go to
temporary folders.

They launch browsers, so they run only when the RADIOLACK_BROWSER_TESTS
environment variable is 1, as in CI, and then need Playwright and its
browsers (README.md, Tests). Each interface opens in its own browser
context, like a separate device, and any error in its page fails the test.
RADIOLACK_BROWSERS, such as "firefox" or "chromium,webkit", keeps them to
some of the browsers, as CI runs one job per browser.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from unittest import mock

import radiolack
from radiolack import PROJECT_URL, __version__, r82xx
from radiolack.recordings import recording_name, wav_header
from radiolack.remote import REMOTE_FILE, RemoteAccess
from radiolack.report import ISSUES_URL
from radiolack.rtltcp import RtlSdrSource, RtlTcpSource
from radiolack.server import LocalServer
from radiolack.sources import Sources
from radiolack.store import BOOKMARKS_FILE, OBSERVATIONS_FILE, SETTINGS_FILE, Store, shown_path
from radiolack.updates import UpdateResult
from tests.fakes import FAKE_RTL_TCP, LOOPBACK, FakeRtlTcpRuns, FakeUpdates, free_port

ENABLED = os.environ.get("RADIOLACK_BROWSER_TESTS") == "1"
DRIVEN = ("chromium", "firefox", "webkit")      # the browsers that the tests drive


def chosen_browsers(names: str) -> tuple[str, ...]:
    """The browsers that ``names`` lists, separated by commas, each once and
    in its order; all those the tests drive when it lists none. A name that
    the tests do not drive fails, rather than leave a browser untested."""
    chosen = tuple(dict.fromkeys(name.strip() for name in names.split(",") if name.strip()))
    unknown = [name for name in chosen if name not in DRIVEN]
    if unknown:
        raise ValueError(f"RADIOLACK_BROWSERS lists browsers that the tests do not drive: {', '.join(unknown)}")
    return chosen or DRIVEN


BROWSERS = chosen_browsers(os.environ.get("RADIOLACK_BROWSERS", ""))
TIMEOUT_MS = 10_000     # enough for the first frames on a slow runner
MENU_BUTTON = "#menu-button"
# The classes that show a dot on the menu's icon: red while a recording runs,
# green while the memory scan or the sweep does.
RECORDING = re.compile(r"(^|\s)recording(\s|$)")
SCANNING = re.compile(r"(^|\s)scanning(\s|$)")
# The menus, in the order of the keys 1 to 8, with their titles (index.html).
MENUS = {"radio": "Radio", "bookmarks": "Bookmarks", "scan": "Scan", "record": "Record", "receiver": "Receiver",
         "display": "Display", "settings": "Settings", "about": "About"}
# The controls that did nothing, which the menu left out on 2026-11-15 (docs/design/scoping.md).
REMOVED_CONTROLS = ("#dm-ctcss", "#dm-agc", "#dm-nr", "#dm-notch", "#dm-nb", '#src-device option[value="soapy"]',
                    '#src-device option[value="file"]', "#src-bias", "#ds-fft", "#ds-win", "#ds-rate", "#band-group",
                    "#st-wake", "#brand", "#toolbar", "#panel", "#mem-list")
S_ZERO, S_TOP = -104, -10   # the S-meter's scale, in dB, from its bar's left end to its right end (main.js)
SQUELCH_RANGE = (-130, -20)  # the squelch threshold's range, in dB (#dm-sql)
MID_SCALE = re.compile(r"^-5[6-8]$")    # the threshold that a press halfway along the S-meter sets: -57 dB, to a pixel
PREFERENCES_SAVE_MS = 1000  # how often an interface gives the engine its changed preferences (main.js)
HISTORY_CONFIRM_MS = 5000   # how long a first press of a Clear in the History waits for the second (main.js)
RECORDINGS_CONFIRM_MS = 5000  # how long a first press of a recording's Delete waits for the second (main.js)
AUDIO_FRAME = 2         # the first byte of an audio frame (docs/design/protocol.md)
DOT = "\N{MIDDLE DOT}"  # the status line's separator
BOOKMARK_NAME = '#bm-list input[data-field="name"]'     # the name's field of the bookmark being edited
# A new bookmark on a channel or a known frequency takes its name.
RV48_NAME = f"2 m FM {DOT} RV48"                    # 145.6 MHz, where the simulated band starts
V40_NAME = f"2 m FM {DOT} V40"                      # 145.5 MHz
SSB_CENTRE_NAME = f"2 m amateur {DOT} SSB centre"   # 144.3 MHz
BOOKMARK_FREQUENCY = '#bm-list input[data-field="freq"]'     # and its frequency's
BOOKMARK_FREQUENCY_NOTE = "Not a frequency: type it in MHz, or with Hz, kHz, MHz or GHz, from 1 Hz to 100 GHz."
# The Radio menu's tuning steps, in their order (index.html).
STEPS = ["Automatic", "1 Hz", "10 Hz", "50 Hz", "100 Hz", "500 Hz", "1 kHz", "5 kHz", "6.25 kHz", "8.33 kHz",
         "9 kHz", "10 kHz", "12.5 kHz", "25 kHz", "50 kHz", "100 kHz"]
PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True}     # a phone in portrait, with touch input
PHONE_ON_ITS_SIDE = {"viewport": {"width": 844, "height": 390}, "has_touch": True}    # the same, in landscape
NARROW = {"viewport": {"width": 500, "height": 800}}       # a narrow window, with a mouse: the menu in one column
NARROWEST = {"viewport": {"width": 390, "height": 800}}    # narrower: the frequency follows the menu's icon
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
# Where the band plan ends, in MHz: the view goes that far whatever the receiver.
PLAN_END_MHZ = max(band["to"] for band in json.loads(
    (Path(radiolack.__file__).parent / "web" / "bandplan-europe.json").read_text(encoding="utf-8"))["bands"]) / 1e6
WEBKIT_SELECT_REFUSAL = "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'"
# Chromium and WebKit report a refused request in the console, such as the
# answer to a wrong access code.
WRONG_CODE_REFUSAL = "Failed to load resource: the server responded with a status of 403 (Forbidden)"
# Without a GPU, as on a CI runner, Firefox refuses WebGL2 on Mesa's software
# renderer: this preference lifts that refusal. Firefox then also needs a
# display, which xvfb-run gives where there is none.
FIREFOX_PREFS = {"webgl.force-enabled": True}
# Chromium draws WebGL2 through the system's EGL: on the GPU where there is
# one, otherwise with Mesa's software renderer, llvmpipe, which drew two
# interfaces three times as many frames a second as Chromium's own,
# SwiftShader (2026-11-16), and Playwright waits for frames before its
# actions. Chromium refuses WebGL2 on llvmpipe unless it ignores its GPU
# blocklist, and its EGL did not start with the display variables of a
# desktop session, which a browser without a window does not need. Without
# EGL, Chromium falls back to SwiftShader.
CHROMIUM_ARGS = ["--use-angle=gl-egl", "--ignore-gpu-blocklist"]
DISPLAY_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY", "EGL_PLATFORM")
# A page's WebGL2 renderer as the page reads it, which the log gives for each
# browser, as the tests' durations depend on it. In WebKit, a page reads
# Apple GPU whatever draws.
WEBGL_RENDERER = """() => {
  const gl = document.createElement('canvas').getContext('webgl2');
  if (!gl) return 'nothing: no WebGL2';
  const info = gl.getExtension('WEBGL_debug_renderer_info');
  return gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER);
}"""
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
# A span too narrow to see, as the spectrum and the waterfall draw it on
# canvases 8 px wide: a marker 2 px wide around its middle, the spectrum's
# over its decor, the waterfall's over black; and what view.js gives for
# the view's limits, the bands in view and the axis's decimals.
SPAN_MARKER_PROBE = """async () => {
  const base = document.baseURI;
  const { SpectrumView, MAX_MARKERS } = await import(new URL('spectrum-gl.js', base).href);
  const { WaterfallView } = await import(new URL('waterfall-gl.js', base).href);
  const view = await import(new URL('view.js', base).href);
  const marker = view.spanMarker(3.8, 4.2, 1);
  const row = (canvas) => {
    const gl = canvas.getContext('webgl2'), pixels = new Uint8Array(8 * 4);
    gl.readPixels(0, 0, 8, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return Array.from({ length: 8 }, (_, x) => pixels[x * 4]);
  };
  const specCanvas = document.createElement('canvas');
  const spectrum = new SpectrumView(specCanvas, () => {});
  spectrum.resize(8, 4);
  const scene = {
    columns: new Float32Array(16).fill(4), plotHeight: 4, fill: false, traceWidth: 1, peakWidth: 0, grid: null,
    markers: new Float32Array(MAX_MARKERS * 6), markerCount: 0, span: [100, 101], reach: [-1e9, 1e9],
    scale: 1, fade: 0, glow: new Uint8Array(32), glowVersion: 1,
  };
  spectrum.draw({ ...scene, marker: null });
  const plain = row(specCanvas);
  spectrum.draw({ ...scene, marker });
  const marked = row(specCanvas);
  const wfCanvas = document.createElement('canvas');
  const waterfall = new WaterfallView(wfCanvas, 4, () => {});
  waterfall.resize(8, 2);
  waterfall.setBins(8);
  waterfall.draw({ head: 0, lines: 0, firstBin: 0, binsPerPixel: 1, marker });
  const bands = [{ from: 10, to: 20 }, { from: 20, to: 30 }, { from: 40, to: 50 }];
  return {
    marker, wide: view.spanMarker(3, 6, 1), doubled: view.spanMarker(3, 4, 2),
    spectrum: [plain, marked], waterfall: row(wfCanvas),
    extents: [view.viewExtent(6.425e9, [-0.7e6, 1767.2e6], [-0.7e6, 1.7e6]), view.viewExtent(0, [143.8e6, 146.2e6], [143.8e6, 146.2e6]),
      view.viewExtent(6.425e9, [23.6e6, 4295.3e6], [143.8e6, 146.2e6])],
    fitted: [view.fitWithin(-5, 4, [0, 100], 2), view.fitWithin(50, 500, [0, 100], 2), view.fitWithin(99, 1, [0, 100], 2)],
    inView: [[0, 5], [15, 25], [30, 40], [25, 45], [55, 60], [0, 100]].map(([a, b]) => view.bandsInView(bands, a, b)),
    decimals: [500e6, 100e6, 1e6, 0.5e6, 0.25e6, 25e3, 1, 0.1].map(view.axisDecimals),
    note: view.beyondReachNote(5760e6, [-0.7e6, 1767.2e6]),
  };
}"""
# The lengths antenna.js gives at a few frequencies (the wavelength, a
# half-wave and a quarter-wave element), and how it writes lengths at the
# edges of its units and lengths that are not positive numbers.
ANTENNA_PROBE = """async () => {
  const { antennaLengths, formatLength } = await import(new URL('antenna.js', document.baseURI).href);
  const at = (hz) => { const l = antennaLengths(hz); return [l.wavelength, l.half, l.quarter].map(formatLength); };
  return {
    lengths: [9e3, 7.1e6, 100e6, 433.5e6, 6.425e9].map(at),
    edges: [0, -1, NaN, Infinity, 0.99996, 999.96, 0.0004].map(formatLength),
  };
}"""
# How frequency.js writes frequencies, and what it reads in typed ones: a
# frequency in Hz, or null for a text that is not one.
FREQUENCY_PROBE = """async () => {
  const { formatFrequency, parseFrequency } = await import(new URL('frequency.js', document.baseURI).href);
  return {
    written: [145.5e6, 145612500, 145612550, 7074001, 1, 100e9, 145600000.4, 25000 / 3, NaN].map(formatFrequency),
    read: ['145.5', '145,6125', ' 145.61255 MHz ', '145612550', '7074 k', '7.074kHz', '1 GHz', '10489.5M', '12 hz',
      '.5', '99999', '100000', '0', '0.4 Hz', '100.1 GHz', '', 'abc', '145.5 x', '1e6', '-1', '10.489,5 M'].map(parseFrequency),
  };
}"""
# The fill and the stroke of each path of a logo, its text's colour where
# a browser gives the keyword currentcolor.
LOGO_PAINT = """(logo) => [...logo.querySelectorAll('path')].map((path) => {
  const style = getComputedStyle(path), paint = (value) => (/^currentcolor$/i.test(value) ? style.color : value);
  return [paint(style.fill), paint(style.stroke)];
})"""
# Loads each icon that the page links: the PNG's size, or whether the SVG,
# whose size each browser gives in its own way, loaded.
ICONS_PROBE = """async () => {
  const links = document.querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]');
  return Object.fromEntries(await Promise.all([...links].map((link) => new Promise((resolve) => {
    const image = new Image(), name = link.getAttribute('href');
    image.onload = () => resolve([name, name.endsWith('.png') ? [image.naturalWidth, image.naturalHeight] : 'loaded']);
    image.onerror = () => resolve([name, 'broken']);
    image.src = link.href;
  }))));
}"""
# What is wider than the window, and which of the menu's parts that show are
# wider than their place in it: none should be.
MENU_OVERFLOW = """() => {
  const wide = [document.documentElement, document.body].filter((el) => el.scrollWidth > innerWidth).map((el) => el.tagName);
  for (const el of document.querySelectorAll('#menu nav, #menu .menu-head, #menu .menu-scroll, #menu .menu-dock')) {
    if (el.offsetParent !== null && el.scrollWidth > el.clientWidth + 1) wide.push(el.className || el.tagName);
  }
  return wide;
}"""

if ENABLED:     # without Playwright, the tests then fail instead of being skipped
    from playwright.sync_api import Browser, Error as PlaywrightError, Locator, Page, expect, sync_playwright


def launch_options(name: str, environment: Mapping[str, str]) -> dict:
    """Playwright's options to launch the browser ``name`` from
    ``environment``: Chromium draws WebGL2 through EGL, in that environment
    without its display variables; Firefox forces WebGL2 on, and like WebKit
    keeps the environment, display included."""
    if name == "chromium":
        return {"args": CHROMIUM_ARGS,
                "env": {key: value for key, value in environment.items() if key not in DISPLAY_VARIABLES}}
    if name == "firefox":
        return {"firefox_user_prefs": FIREFOX_PREFS}
    return {}


def overlap(first: dict, second: dict) -> bool:
    """Whether two boxes, as Playwright gives them, cover a common area."""
    return (first["x"] < second["x"] + second["width"] and second["x"] < first["x"] + first["width"]
            and first["y"] < second["y"] + second["height"] and second["y"] < first["y"] + first["height"])


def mhz(frequency_hz: int) -> str:
    """A frequency as the interface writes it (frequency.js): in MHz, with 3
    to 6 decimals, the zeros at the end beyond the third dropped."""
    return re.sub(r"0{1,3}\Z", "", f"{frequency_hz // 1_000_000}.{frequency_hz % 1_000_000:06d}")


def in_use(centre_hz: int, rate_hz: int = 2_400_000) -> int:
    """Where the fake rtl_tcp's R820T sits for ``centre_hz``: on its grid,
    the driver's computation unknown and no correction (rtltcp.py)."""
    return r82xx.on_grid(centre_hz, r82xx.rate_if_hz(rate_hz), 24e6, 1766e6)


def kept_channel(frequency_hz: int, last_s: float) -> dict:
    """A channel of observations.json, first heard a day before ``last_s``,
    twice, both times at noon."""
    hours = [0] * 24
    hours[12] = 2
    return {"frequency_hz": frequency_hz, "first_s": last_s - 86_400, "last_s": last_s, "transmissions": 2,
            "active_s": 30.0, "margin_db": 20.0, "hours": hours}


class ChosenBrowsersTest(unittest.TestCase):
    """RADIOLACK_BROWSERS, read without launching any browser."""

    def test_every_browser_unless_some_are_listed(self):
        for names in ("", " ", ",", " , "):
            self.assertEqual(chosen_browsers(names), ("chromium", "firefox", "webkit"), repr(names))

    def test_the_browsers_listed_each_once_in_their_order(self):
        self.assertEqual(chosen_browsers("firefox"), ("firefox",))
        self.assertEqual(chosen_browsers(" webkit, chromium ,webkit,"), ("webkit", "chromium"))

    def test_a_browser_that_the_tests_do_not_drive_fails(self):
        for names in ("safari", "firefox,Webkit", "chrome"):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "do not drive"):
                chosen_browsers(names)


class LaunchOptionsTest(unittest.TestCase):
    """Each browser's launch options, read without launching any browser."""

    def test_chromium_draws_through_egl_without_the_display_variables(self):
        environment = {"DISPLAY": ":99", "WAYLAND_DISPLAY": "wayland-0", "EGL_PLATFORM": "wayland",
                       "HOME": "/home/user", "XDG_CONFIG_HOME": "/tmp/config"}
        options = launch_options("chromium", environment)
        self.assertEqual(options["args"], ["--use-angle=gl-egl", "--ignore-gpu-blocklist"])
        self.assertEqual(options["env"], {"HOME": "/home/user", "XDG_CONFIG_HOME": "/tmp/config"})
        self.assertEqual(environment["DISPLAY"], ":99")     # the tests' own environment stays as it is

    def test_firefox_and_webkit_keep_the_display(self):
        # Without a GPU, Firefox draws WebGL2 only on a display, such as xvfb-run's.
        self.assertEqual(launch_options("firefox", {"DISPLAY": ":99"}), {"firefox_user_prefs": {"webgl.force-enabled": True}})
        self.assertEqual(launch_options("webkit", {"DISPLAY": ":99"}), {})


@unittest.skipUnless(ENABLED, "browser tests run only with RADIOLACK_BROWSER_TESTS=1")
class BrowserTest(unittest.TestCase):
    """Each test runs in every browser in turn, or in those of
    RADIOLACK_BROWSERS."""

    @classmethod
    def setUpClass(cls) -> None:
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browsers = {}
        for name in BROWSERS:
            browser = getattr(playwright, name).launch(**launch_options(name, os.environ))   # headless
            cls.addClassCleanup(browser.close)
            cls.browsers[name] = browser
            renderer = cls.webgl_renderer(browser)
            print(f"{name} {browser.version}: WebGL2 renderer as a page reads it: {renderer}", file=sys.stderr, flush=True)
        expect.set_options(timeout=TIMEOUT_MS)

    @staticmethod
    def webgl_renderer(browser: Browser) -> str:
        """The WebGL2 renderer that a blank page in ``browser`` reads."""
        page = browser.new_page()
        try:
            return page.evaluate(WEBGL_RENDERER)
        except PlaywrightError as error:
            return f"unknown ({error.message})"
        finally:
            page.close()

    def serve(self, source: RtlTcpSource | None = None, store: Store | None = None,
              sources: Sources | None = None, updates: FakeUpdates | None = None,
              remote_addresses: Callable[[], list[str]] | None = None, host: str | None = None,
              recordings: Path | None = None) -> str:
        """The address of a server started for this test, which records
        into ``recordings``, if given."""
        server = LocalServer(source=source, store=store, sources=sources, updates=updates,
                             remote_addresses=remote_addresses, host=host, recordings=recordings)
        server.start()
        self.addCleanup(server.stop)
        return server.url

    @contextlib.contextmanager
    def interfaces(self, browser: str, url: str, count: int, **options) -> Iterator[list[Page]]:
        """``count`` interfaces at ``url``, each with the Receiver menu open:
        clicks, which let the audio start. ``self.audio[page]`` counts the
        audio frames each one receives, ``self.signals[page]`` the lists of
        signals found, and ``self.sent[page]`` lists the messages each one
        sends, as text. ``options`` go to each browser context, such as a
        phone's viewport and touch input."""
        errors: list[str] = []
        self.audio: dict[Page, list[int]] = {}
        self.signals: dict[Page, list[int]] = {}
        self.sent: dict[Page, list[str]] = {}
        with contextlib.ExitStack() as stack:
            pages = []
            for _ in range(count):
                context = self.browsers[browser].new_context(**options)
                stack.callback(context.close)
                page = self.watched_page(context, browser, errors)
                self.audio[page] = received = []
                self.signals[page] = lists = []
                self.sent[page] = sent = []

                def watch(socket, received=received, lists=lists, sent=sent) -> None:
                    socket.on("framereceived", lambda payload: received.append(1) if isinstance(
                        payload, bytes) and payload[:1] == bytes([AUDIO_FRAME]) else None)
                    socket.on("framereceived", lambda payload: lists.append(1) if isinstance(
                        payload, str) and json.loads(payload).get("type") == "peaks" else None)
                    socket.on("framesent", lambda payload: sent.append(payload) if isinstance(payload, str) else None)

                page.on("websocket", watch)
                page.goto(url)
                self.open_menu(page, "receiver")
                pages.append(page)
            yield pages
        self.assertEqual(errors, [], "errors in the interface")

    @staticmethod
    def open_menu(page: Page, name: str) -> None:
        """Show the menu ``name`` with the pointer: the icon opens the menu,
        and in one column its arrow goes back to the list of menus. The
        menu covers the views, the readout and the speaker until it closes:
        Escape closes it."""
        if page.locator("#menu").is_hidden():
            page.locator(MENU_BUTTON).click()
        back = page.get_by_role("button", name="Back to the menus", exact=True)
        if back.is_visible():
            back.click()
        page.locator(f'#menu nav button[data-menu="{name}"]').click()
        expect(page.locator(f'#menu section[data-menu="{name}"]')).to_be_visible()
        expect(page.locator("#menu-title")).to_have_text(MENUS[name])

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

    def logo_box(self, page: Page, em_px: float) -> dict:
        """Check the logo in the page's heading, which it names Radiolack:
        the drawing's proportions, the name ``em_px`` pixels to the em, in
        the heading's colour. Return its box."""
        heading = page.get_by_role("heading", name="Radiolack", exact=True)
        expect(heading).to_be_visible()
        logo = heading.get_by_role("img", name="Radiolack", exact=True)
        _, _, width, height = (float(value) for value in logo.get_attribute("viewBox").split())
        box = logo.bounding_box()
        self.assertAlmostEqual(box["height"], height * em_px / 1000, delta=0.05)
        self.assertAlmostEqual(box["width"], box["height"] * width / height, delta=0.1)
        colour = heading.evaluate("(heading) => getComputedStyle(heading).color")
        self.assertEqual(logo.evaluate(LOGO_PAINT), [["none", colour], [colour, "none"]])   # the mark, the name
        return box

    def test_the_menu_leaves_the_frequency_clear_at_every_width_and_the_icons_load(self):
        # The interface names Radiolack in its title alone, so that nothing
        # pushes the frequency aside. Each menu, the menu's icon and the
        # speaker stay clear of the readout, and nothing is wider than the
        # window or than the menu, on a computer, in narrow windows, on a
        # phone and on a phone on its side. On a touch screen the icon lies
        # between the step buttons. The antenna icon's target, a finger's on
        # a touch screen, stays below the window's top edge.
        url = self.serve()
        readout = ("#freq", "#antenna", "#meta", "#smeter")
        for browser in BROWSERS:
            for options in ({}, NARROW, NARROWEST, PHONE, PHONE_ON_ITS_SIDE):
                with (self.subTest(browser=browser, viewport=options.get("viewport")),
                      self.interfaces(browser, url, 1, **options) as [page]):
                    expect(page).to_have_title("Radiolack")
                    expect(page.get_by_role("heading", name="Radiolack")).to_have_count(0)
                    width = page.viewport_size["width"]
                    for key, name in enumerate(MENUS, start=1):
                        page.keyboard.press(str(key))
                        expect(page.locator("#menu-title")).to_have_text(MENUS[name])
                        menu = page.locator("#menu").bounding_box()
                        self.assertTrue(menu["x"] >= 0 and menu["x"] + menu["width"] <= width, (name, menu))
                        for part in readout:
                            self.assertFalse(overlap(menu, page.locator(part).bounding_box()), (name, part))
                        self.assertEqual(page.evaluate(MENU_OVERFLOW), [], name)
                    page.keyboard.press("Escape")
                    expect(page.locator("#menu")).to_be_hidden()
                    self.assertEqual(page.evaluate(MENU_OVERFLOW), [])
                    antenna = page.locator("#antenna").bounding_box()
                    self.assertGreaterEqual(antenna["y"], -0.01, antenna)      # to within rounding
                    for control in (MENU_BUTTON, "#sound", "#hint"):
                        box = page.locator(control).bounding_box()      # None while hidden
                        for part in readout:
                            self.assertFalse(box is not None and overlap(box, page.locator(part).bounding_box()),
                                             (control, part))
                    icon = page.locator(MENU_BUTTON).bounding_box()
                    if options.get("has_touch"):
                        down, up = (page.locator(step).bounding_box() for step in ("#tune-down", "#tune-up"))
                        self.assertTrue(down["x"] + down["width"] <= icon["x"] and icon["x"] + icon["width"] <= up["x"])
                        self.assertGreaterEqual(min(icon["width"], icon["height"]), 44)
                    else:       # in the top left corner, at every width
                        for got, wanted in zip((icon["x"], icon["y"], icon["width"], icon["height"]), (10, 8, 34, 32)):
                            self.assertAlmostEqual(got, wanted, delta=0.5)
                    if not options:
                        self.assertEqual(page.evaluate(ICONS_PROBE),
                                         {"favicon.svg": "loaded", "apple-touch-icon.png": [180, 180]})

    def test_a_newer_version_shows_until_closed_and_about_checks_again(self):
        updates = FakeUpdates(UpdateResult("available", latest="9.9.9"))
        url = self.serve(updates=updates)
        text = "Radiolack 9.9.9 is available: install it with the system's software updates."
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    expect(page.locator("#notice-text")).to_have_text(text)
                first.keyboard.press("Escape")      # the menu lies over the notice
                first.locator("#notice-close").click()
                expect(first.locator("#notice")).to_be_hidden()
                expect(second.locator("#notice")).to_be_visible()       # closed on its own page only
                for page in (first, second):
                    self.open_menu(page, "about")
                    expect(page.locator("#ab-update")).to_have_text(text)
                    expect(page.locator("#ab-update-start")).to_be_checked()
                first.locator("#ab-update-start").uncheck()
                expect(second.locator("#ab-update-start")).not_to_be_checked()
                second.locator("#ab-update-start").check()
                expect(first.locator("#ab-update-start")).to_be_checked()
                checks = updates.checks
                first.locator("#ab-update-check").click()
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                while updates.checks == checks:
                    self.assertLess(time.monotonic(), deadline, "Check now did not check")
                    time.sleep(0.05)
                expect(first.locator("#ab-update")).to_have_text(text)
                expect(first.locator("#ab-update-check")).to_be_enabled()
                expect(first.locator("#notice")).to_be_hidden()         # the same version stays closed

    def test_about_reports_a_problem_to_read_then_share_or_copy(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "about")
                page.locator("#ab-report").click()
                dialog = page.locator("#report-dialog")
                expect(dialog).to_be_visible()
                text = page.locator("#report-text")
                expect(text).to_have_value(re.compile(rf"^Radiolack {re.escape(__version__)}( \(Debian package\))?\n"))
                expect(text).to_have_value(re.compile(r"\nInterface: \S.*\nReceiver: synthetic, streaming, "))
                self.assertTrue(page.locator("#report-issue").get_attribute("href").startswith(f"{ISSUES_URL}?body="))
                page.locator("#report-copy").click()
                # Without the clipboard, which a headless browser may refuse, the report is selected.
                expect(page.locator("#report-msg")).to_have_text(
                    re.compile(r"^Report (copied|selected: copy it with Ctrl\+C)\.$"))
                page.keyboard.press("Escape")       # closes the dialog alone
                expect(dialog).to_be_hidden()
                expect(page.locator('#menu section[data-menu="about"]')).to_be_visible()

    def test_about_shows_the_version_the_links_and_the_legal_notice(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "about")
                expect(page.locator("#ab-version")).to_have_text(f"Radiolack {__version__}")
                # Radiolack's pages on GitHub, which the desktop window opens in the web browser: never followed here.
                for link, href in (("#ab-idea", f"{PROJECT_URL}/issues/new"), ("#ab-project", PROJECT_URL)):
                    expect(page.locator(link)).to_have_attribute("href", href)
                    expect(page.locator(link)).to_have_attribute("target", "_blank")
                button, dialog = page.locator("#ab-legal"), page.locator("#legal-dialog")
                text = page.locator("#legal-text")
                button.click()
                expect(dialog).to_be_visible()
                expect(text).to_contain_text("Radiolack is free software, copyright 2026 OGD09.")
                expect(dialog.locator(f'a[href="{PROJECT_URL}/blob/main/LICENSE"]')).to_have_attribute("target", "_blank")
                # The notice scrolls under its title, whose close button stays in sight.
                self.assertGreater(text.evaluate("e => e.scrollHeight - e.clientHeight"), 0)
                text.evaluate("e => { e.scrollTop = e.scrollHeight; }")
                expect(text.get_by_text("If a court holds part of this notice unenforceable")).to_be_in_viewport()
                expect(dialog.locator(".close")).to_be_in_viewport()
                page.keyboard.press("Escape")       # closes the dialog alone, the focus back on its button
                expect(dialog).to_be_hidden()
                expect(page.locator('#menu section[data-menu="about"]')).to_be_visible()
                expect(button).to_be_focused()
                button.click()                      # again from its start
                expect(dialog).to_be_visible()
                self.assertEqual(text.evaluate("e => e.scrollTop"), 0)
                dialog.locator(".close").click()
                expect(dialog).to_be_hidden()
                expect(button).not_to_be_focused()  # closed with the pointer

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

    def wait_for_signals(self, page: Page, lists: int) -> None:
        """Wait until ``page`` has received ``lists`` lists of signals found."""
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while len(self.signals[page]) < lists:
            self.assertLess(time.monotonic(), deadline, "no new list of signals came")
            page.wait_for_timeout(100)      # lets Playwright deliver the page's events

    def press(self, page: Page, target: Locator) -> list[float]:
        """Press the mouse's main button at the centre of ``target`` and keep
        it pressed: the point pressed, [x, y]. A list rebuilt while the point
        is sought takes the element found out of the page: it is then found
        again, and a list rebuilt after that leaves the press to the element
        in its place."""
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while True:
            try:
                target.scroll_into_view_if_needed()
                box = target.bounding_box()     # None once out of the page
            except PlaywrightError as error:
                if "not attached to the DOM" not in error.message:
                    raise
                box = None
            if box is not None:
                break
            self.assertLess(time.monotonic(), deadline, "the element to press kept leaving the page")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        return [x, y]

    def test_audio_starts_with_a_click_and_the_vfo_is_shared(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    self.wait_for_audio(page)
                    expect(page.locator("#m-mode")).to_have_text("NFM")
                    expect(page.locator("#sound")).to_be_hidden()      # the speaker is for touch input
                self.open_menu(first, "radio")
                first.locator('#mode-group button[data-mode="AM"]').click()
                for page in (first, second):
                    expect(page.locator("#m-mode")).to_have_text("AM")
                    expect(page.locator("#m-bw")).to_have_text("8 kHz")
                expect(first.locator("#dm-audio-note")).to_be_hidden()     # AM plays too
                first.locator('#mode-group button[data-mode="NFM"]').click()
                expect(second.locator("#m-mode")).to_have_text("NFM")

    def test_the_icon_beside_the_frequency_opens_the_antenna_lengths(self):
        # The lengths follow the VFO while they show, as when another
        # interface tunes, and the dialog keeps the keys: nothing tunes behind
        # it, and Escape closes it alone, giving the focus back to the icon.
        # Closed with a pointer, it leaves the icon unfocused, so that no
        # focus ring stays on it. A server for each browser, as the test moves
        # the VFO.
        for browser in BROWSERS:
            url = self.serve()      # the VFO on 145.6 MHz
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                icon = first.get_by_role("button", name="Antenna length", exact=True)
                expect(icon).to_have_attribute("title", "Antenna length for this frequency")
                dialog = first.locator("#antenna-dialog")
                expect(dialog).to_be_hidden()
                icon.focus()        # from the keyboard, as the Receiver menu lies over the readout
                first.keyboard.press("Enter")
                expect(dialog).to_be_visible()
                expect(first.locator("#antenna-for")).to_have_text(f"145.600 MHz {DOT} 2 m amateur")
                expect(dialog.locator(".lengths dd")).to_have_text(["2.06 m", "97.8 cm", "48.9 cm", "48.9 cm", "48.9 cm"])
                first.keyboard.press("ArrowRight")
                expect(first.locator("#freq")).to_have_attribute("aria-valuenow", "145600000")
                second.keyboard.press("ArrowRight")     # a step of 12.5 kHz, from the other interface
                expect(first.locator("#antenna-for")).to_have_text(f"145.6125 MHz {DOT} 2 m amateur")
                first.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                expect(icon).to_be_focused()
                expect(first.locator("#menu")).to_be_visible()      # the Receiver menu, open behind it
                first.keyboard.press("Escape")      # closes the menu in turn
                expect(first.locator("#menu")).to_be_hidden()
                icon.click()
                dialog.get_by_role("button", name="Close the antenna lengths").click()
                expect(dialog).to_be_hidden()
                expect(icon).not_to_be_focused()
                icon.click()
                first.mouse.click(4, 4)     # on the backdrop, away from the dialog
                expect(dialog).to_be_hidden()
                expect(icon).not_to_be_focused()
                icon.dblclick()             # whose second click lands on the backdrop
                expect(dialog).to_be_visible()
                first.keyboard.press("Escape")
                expect(dialog).to_be_hidden()

    def test_antenna_lengths_take_the_unit_that_fits_them(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.assertEqual(page.evaluate(ANTENNA_PROBE), {
                    "lengths": [
                        ["33.3 km", "15.8 km", "7.91 km"],    # 9 kHz
                        ["42.2 m", "20.1 m", "10.0 m"],       # 7.1 MHz
                        ["3.00 m", "1.42 m", "71.2 cm"],      # 100 MHz
                        ["69.2 cm", "32.8 cm", "16.4 cm"],    # 433.5 MHz
                        ["4.67 cm", "2.22 cm", "1.11 cm"],    # 6.425 GHz
                    ],
                    "edges": ["\N{EM DASH}"] * 4 + ["1.00 m", "1.00 km", "0.400 mm"],
                })

    def test_frequencies_are_written_exactly_and_typed_with_a_unit(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.assertEqual(page.evaluate(FREQUENCY_PROBE), {
                    "written": ["145.500", "145.6125", "145.61255", "7.074001", "0.000001", "100000.000", "145.600",
                                "0.008333", "\N{EM DASH}"],
                    "read": [145_500_000, 145_612_500, 145_612_550, 145_612_550, 7_074_000, 7_074, 1_000_000_000,
                             10_489_500_000, 12, 500_000, 99_999_000_000, 100_000] + [None] * 9,
                })
                self.assertEqual([mhz(hz) for hz in (145_500_000, 145_612_500, 145_612_550, 7_074_001)],
                                 ["145.500", "145.6125", "145.61255", "7.074001"])     # as this test writes them
                self.open_menu(page, "radio")
                expect(page.locator("#dm-step option")).to_have_text(STEPS)

    def test_a_typed_frequency_tunes_exactly_and_one_left_unchanged_moves_nothing(self):
        # The entry opens with the whole frequency; Enter tunes there, off the
        # step's grid, and leaving it unchanged sends nothing. A frequency
        # beyond the receiver's reach, here the simulated band's span, or a
        # text that is not one, tunes nothing and the status line says why.
        url = self.serve()      # the simulated band, from 143.8 to 146.2 MHz, the VFO on 145.6 MHz
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                freq, entry = page.locator("#freq"), page.locator("#freq input")
                freq.dblclick()
                expect(entry).to_have_value("145.600")
                expect(entry).to_be_focused()
                entry.fill("145.61255")
                entry.press("Enter")
                expect(freq).to_have_attribute("aria-valuenow", "145612550")
                expect(freq).to_have_attribute("aria-valuetext", "145.61255 MHz")
                tuned = len([m for m in map(json.loads, self.sent[page]) if m.get("type") == "demod"])
                freq.dblclick()
                expect(entry).to_have_value("145.61255")
                entry.blur()                        # unchanged: nothing moves, nothing is sent
                expect(entry).to_have_count(0)
                expect(freq).to_have_attribute("aria-valuenow", "145612550")
                freq.dblclick()
                entry.fill("144,3 M")
                entry.press("Enter")
                expect(freq).to_have_attribute("aria-valuenow", "144300000")
                self.assertEqual(len([m for m in map(json.loads, self.sent[page]) if m.get("type") == "demod"]), tuned + 1)
                freq.dblclick()
                entry.fill("5 GHz")
                entry.press("Enter")
                expect(page.locator("#status")).to_contain_text(
                    f"5000.000 MHz is beyond this receiver's range (143.800 to 146.200 MHz) {DOT} Synthetic source")
                expect(freq).to_have_attribute("aria-valuenow", "144300000")
                freq.dblclick()
                entry.fill("abc")
                entry.press("Enter")
                expect(page.locator("#status")).to_contain_text(
                    f"Not a frequency: type it in MHz, or with Hz, kHz, MHz or GHz {DOT} Synthetic source")
                expect(freq).to_have_attribute("aria-valuenow", "144300000")

    def test_a_digit_dragged_or_under_the_wheel_changes_and_opens_no_entry(self):
        # Dragged up or down, a digit changes by one for each 16 px, off the
        # step's grid; a drag never opens the entry. Here the 1 kHz digit,
        # from 145.6 MHz. The drag stays within the page, which starts some
        # 20 px above the digits: beyond it, Firefox under Playwright reports
        # the pointer at another height.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                freq = page.locator("#freq")
                box = page.locator('#freq .d[data-m="1000"]').bounding_box()
                x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x, y + 56, steps=8)        # three steps down
                expect(freq).to_have_attribute("aria-valuenow", "145597000")
                page.mouse.move(x, y - 20, steps=8)        # then one above where it started
                expect(freq).to_have_attribute("aria-valuenow", "145601000")
                page.mouse.up()
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x, y - 5)                  # a press that moves less than 8 px changes nothing
                page.mouse.up()
                expect(freq).to_have_attribute("aria-valuenow", "145601000")
                expect(page.locator("#freq input")).to_have_count(0)
                box = page.locator('#freq .d[data-m="10"]').bounding_box()
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, -100)                  # a notch up: 10 Hz more
                expect(freq).to_have_attribute("aria-valuenow", "145601010")

    def test_the_automatic_step_follows_the_zoom_and_the_channels(self):
        # On the 2 m repeaters, 12.5 kHz apart, the step is their spacing while
        # the view shows two channels or more; zoomed in further it follows the
        # view's width. Where the plan gives no spacing, the width alone sets
        # it, and a fixed step holds whatever the zoom.
        url = self.serve()      # the simulated band, the VFO on 145.6 MHz, the view 2.4 MHz wide
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                meta, freq = page.locator("#m-seg"), page.locator("#freq")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 12\.5 kHz$"))
                for _ in range(11):                 # 27.7 kHz wide: still two channels
                    page.keyboard.press("+")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 12\.5 kHz$"))
                page.keyboard.press("+")            # 18.5 kHz wide
                expect(meta).to_have_text(re.compile(rf" {DOT} step 1 Hz$"))
                freq.focus()
                freq.press("ArrowUp")
                expect(freq).to_have_attribute("aria-valuenow", "145600001")
                page.keyboard.press("0")            # the whole span, 2.4 MHz
                freq.dblclick()
                page.locator("#freq input").fill("144.05")     # the CW segment: no channels
                page.locator("#freq input").press("Enter")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 1 kHz$"))
                for _ in range(3):                  # 711 kHz wide
                    page.keyboard.press("+")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 100 Hz$"))
                self.open_menu(page, "radio")
                page.locator("#dm-step").select_option("25000")
                page.keyboard.press("Escape")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 25 kHz$"))
                page.keyboard.press("0")
                expect(meta).to_have_text(re.compile(rf" {DOT} step 25 kHz$"))

    def strip_point(self, page: Page, frequency_mhz: float, part: float) -> tuple[float, float]:
        """The point of the band strip over ``frequency_mhz``, which the view
        shows, at ``part`` of its height from its top."""
        low, high = self.view_edges(page)
        spec, band = page.locator("#specwrap").bounding_box(), page.locator("#band").bounding_box()
        x = spec["x"] + 1 + (spec["width"] - 2) * (frequency_mhz - low) / (high - low)
        return x, band["y"] + band["height"] * part

    def test_the_channels_and_known_frequencies_show_on_the_line_and_the_strip(self):
        # The line under the frequency names the channel the VFO is on, and
        # the step keys go from channel to channel. On the strip, a channel's
        # tick, in its lower half, tunes there, and a known frequency's mark,
        # in its upper half, tunes there in its mode; Display hides them.
        url = self.serve()      # the simulated band, the VFO on 145.6 MHz in NFM
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                meta, freq, band = page.locator("#m-seg"), page.locator("#freq"), page.locator("#band")
                expect(meta).to_have_text(f"{RV48_NAME} {DOT} step 12.5 kHz")
                freq.focus()
                freq.press("ArrowUp")
                expect(freq).to_have_attribute("aria-valuenow", "145612500")
                expect(meta).to_have_text(f"2 m FM {DOT} RV49 {DOT} step 12.5 kHz")
                for _ in range(4):                  # 474 kHz wide: the channels lie 33 px apart
                    page.keyboard.press("+")
                x, y = self.strip_point(page, 145.65, 0.8)
                page.mouse.move(x, y)
                expect(band).to_have_attribute("title", f"2 m FM {DOT} RV52 {DOT} 145.650 MHz {DOT} NFM")
                page.mouse.down()
                page.mouse.up()
                expect(freq).to_have_attribute("aria-valuenow", "145650000")
                expect(meta).to_have_text(f"2 m FM {DOT} RV52 {DOT} step 12.5 kHz")
                page.keyboard.press("0")            # the whole span, 2.4 MHz
                x, y = self.strip_point(page, 144.75, 0.2)
                page.mouse.move(x, y)
                expect(band).to_have_attribute("title", f"2 m amateur {DOT} ATV talkback {DOT} 144.750 MHz {DOT} USB")
                page.mouse.down()
                page.mouse.up()
                expect(freq).to_have_attribute("aria-valuenow", "144750000")
                expect(page.locator("#m-mode")).to_have_text("USB")
                expect(meta).to_have_text(re.compile(rf"^2 m amateur {DOT} ATV talkback {DOT} step "))
                self.open_menu(page, "display")
                page.locator("#ov-channels").uncheck()
                page.keyboard.press("Escape")
                x, y = self.strip_point(page, 144.75, 0.2)
                page.mouse.move(x, y)
                expect(band).to_have_attribute("title", re.compile(rf"^2 m amateur {DOT} .* {DOT} drag: spectrum height$"))
                expect(meta).to_have_text(re.compile(rf"^2 m amateur {DOT} ATV talkback {DOT} step "))   # the line still names it

    def test_the_band_plan_list_follows_the_view_and_the_filter(self):
        # Under the bookmarks, the band plan lists the ranges in view with the
        # known frequencies inside them, at most 200 rows; filtered, the
        # ranges, channel sets and known frequencies of the whole plan. A set
        # frames itself, a known frequency tunes there.
        url = self.serve()      # the simulated band, the VFO on 145.6 MHz
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                plan, note = page.locator("#plan-list"), page.locator("#plan-note")
                self.open_menu(page, "bookmarks")
                expect(plan.locator(".item.sub .name", has_text="APRS")).to_have_count(1)
                page.keyboard.press("Escape")
                for _ in range(4):                  # 474 kHz around the VFO: 144.8 MHz out of view
                    page.keyboard.press("+")
                self.open_menu(page, "bookmarks")
                expect(plan.locator(".item .name", has_text="Repeater outputs")).to_have_count(1)
                expect(plan.locator(".item .name", has_text="APRS")).to_have_count(0)
                page.keyboard.press("Escape")
                for _ in range(30):                 # the whole plan
                    page.keyboard.press("-")
                self.open_menu(page, "bookmarks")
                expect(note).to_contain_text("The first 200 rows: zoom in to see the others.")
                self.assertLessEqual(plan.locator("li").count(), 200)     # a range comes with its known frequencies, or not at all
                page.locator("#bm-filter").fill("APRS")
                expect(plan.locator(".item .name")).to_have_text([f"2 m amateur {DOT} APRS", f"70 cm amateur {DOT} APRS"])
                plan.locator(".item", has_text=f"2 m amateur {DOT} APRS").click()
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "144800000")
                page.locator("#bm-filter").fill("PMR446")
                plan.locator("button.item", has=page.locator(".sw.set")).click()
                page.keyboard.press("Escape")
                low, high = self.view_edges(page)
                self.assertAlmostEqual(low, 446.0 - 0.016, delta=0.002)
                self.assertAlmostEqual(high, 446.2 + 0.016, delta=0.002)

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_digit_that_leaves_the_span_moves_it(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # 2.4 MHz around 145 MHz, the VFO on 145.6
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                freq = page.locator("#freq")
                for expected in ("146600000", "147600000"):
                    box = page.locator('#freq .d[data-m="1000000"]').bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.mouse.wheel(0, -100)
                    expect(freq).to_have_attribute("aria-valuenow", expected)
                self.open_menu(page, "receiver")    # the span moved once, for 146.6 MHz, and holds 147.6 MHz
                expect(page.locator("#src-center")).to_have_value("146.525")
                expect(freq).to_have_attribute("aria-valuenow", "147600000")

    def test_the_antenna_icon_takes_a_finger_beside_the_frequency(self):
        # On a touch screen the icon's target is at least 40 px across and
        # lies past the frequency, which a tap edits; the dialog fits a phone.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1, **PHONE) as [page]:
                icon = page.get_by_role("button", name="Antenna length", exact=True)
                target, freq = icon.bounding_box(), page.locator("#freq").bounding_box()
                # 40 px, to within the rounding of edges that fall between pixels (Firefox: 40 - 2**-20)
                self.assertGreaterEqual(min(target["width"], target["height"]), 40 - 0.01)
                self.assertGreaterEqual(target["x"], freq["x"] + freq["width"] - 0.5)
                page.keyboard.press("Escape")       # the menu lies over the readout
                icon.tap()
                dialog = page.locator("#antenna-dialog")
                expect(dialog).to_be_visible()
                box = dialog.bounding_box()
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"] + box["width"], PHONE["viewport"]["width"])
                dialog.get_by_role("button", name="Close the antenna lengths").tap()
                expect(dialog).to_be_hidden()
                expect(icon).not_to_be_focused()

    def test_held_audio_shows_a_speaker_and_a_touch_asks_it_to_start(self):
        # A tap activates the page only once the finger leaves the screen, and
        # only then does Safari on the iPhone let the audio start. The audio
        # stays held here, and the test counts the page's requests to start
        # it. On a touch screen, the speaker shows it held: nothing else does
        # out of the Radio menu.
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
                page.keyboard.press("Escape")       # the menu lies over the speaker
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
        # Add opens the new bookmark's name in the interface that added it;
        # Edit opens a bookmark's name again.
        for browser in BROWSERS:
            url = self.serve()      # no bookmark yet
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                self.open_menu(first, "bookmarks")
                expect(first.locator("#bm-list .empty")).to_have_text("No bookmarks")
                first.locator("#bm-add").click()
                expect(second.locator("#bm-list .item .name")).to_have_text([RV48_NAME])
                name = first.locator(BOOKMARK_NAME)
                expect(name).to_be_focused()
                expect(name).to_have_value(RV48_NAME)
                name.fill("Not kept")
                name.press("Escape")        # cancels the new name, and leaves the menu open
                expect(first.locator("#bm-list .item .name")).to_have_text([RV48_NAME])
                expect(first.locator("#menu")).to_be_visible()
                edit = first.locator("#bm-list button[data-edit]")
                expect(edit).to_be_focused()
                edit.click()
                expect(name).to_be_focused()
                name.fill("  Calling ")
                name.press("Enter")
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["Calling"])
                    expect(page.locator("#marks .mark")).to_have_text(["Calling"])
                expect(edit).to_be_focused()
                self.open_menu(second, "bookmarks")
                second.locator("#bm-list button[data-edit]").click()
                second.locator(BOOKMARK_NAME).fill("FM calling")
                second.locator("#bm-filter").click()       # leaving the row saves the name
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["FM calling"])
                second.locator("#bm-list button[data-edit]").click()
                second.locator("#bm-list button[data-del]").click()     # while the name's field has the focus
                for page in (first, second):
                    expect(page.locator("#bm-list .empty")).to_have_text("No bookmarks")
                    expect(page.locator("#marks .mark")).to_have_count(0)
                expect(first.locator("#bm-note")).to_be_hidden()

    def test_a_bookmark_tunes_with_its_mode_and_a_new_one_opens_with_its_name_selected(self):
        # A click on a bookmark tunes every interface there, in its mode, and
        # its box ticks it for the memory scan. The engine sorts the bookmarks
        # by frequency, then by name: the one that Add makes opens wherever it
        # lands, here before another one at the same frequency.
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            Store(directory).save(BOOKMARKS_FILE, {"bookmarks": [
                {"frequency_hz": 144_300_000, "name": "SSB calling", "mode": "USB", "scan": False},
                {"frequency_hz": 145_500_000, "name": "FM calling", "mode": "NFM", "scan": True}]})
            url = self.serve(store=Store(directory))    # the simulated band, the VFO on 145.6 MHz in NFM
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                self.open_menu(first, "bookmarks")
                expect(first.locator("#bm-list .item .name")).to_have_text(["SSB calling", "FM calling"])
                first.locator("#bm-list button.item", has_text="SSB calling").click()
                for page in (first, second):    # the VFO is shared
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "144300000")
                    expect(page.locator("#m-mode")).to_have_text("USB")
                expect(first.locator("#menu")).to_be_visible()      # which stays open
                tick = first.get_by_role("checkbox", name="Scan SSB calling", exact=True)
                expect(tick).not_to_be_checked()
                tick.check()
                expect(second.locator('#bm-list input[aria-label="Scan SSB calling"]')).to_be_checked()
                first.locator("#bm-add").click()        # at the VFO, in its mode
                name = first.locator(BOOKMARK_NAME)
                expect(name).to_be_focused()
                expect(name).to_have_value(SSB_CENTRE_NAME)
                self.assertEqual(name.evaluate("(field) => [field.selectionStart, field.selectionEnd]"), [0, len(SSB_CENTRE_NAME)])
                expect(first.locator("#bm-list li").first.locator('input[data-field="name"]')).to_have_count(1)
                expect(second.locator("#bm-list .item .name")).to_have_text([SSB_CENTRE_NAME, "SSB calling", "FM calling"])
                expect(second.locator(BOOKMARK_NAME)).to_have_count(0)     # open where added only
                first.keyboard.type("Net")      # in place of the selected name
                first.keyboard.press("Enter")
                for page in (first, second):
                    expect(page.locator("#bm-list .item .name")).to_have_text(["Net", "SSB calling", "FM calling"])
                    expect(page.locator("#bm-list .item .tag")).to_have_text(["USB", "USB", "NFM"])
                expect(first.locator("#bm-list button[data-edit]").first).to_be_focused()

    def test_a_bookmark_s_frequency_is_edited_beside_its_name(self):
        # Edit opens the frequency, whole, beside the name: a new one moves
        # the bookmark for every interface, exactly, and one that is not a
        # frequency keeps the row open, its field marked, with a note.
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            Store(directory).save(BOOKMARKS_FILE, {"bookmarks": [
                {"frequency_hz": 145_500_000, "name": "FM calling", "mode": "NFM", "scan": False}]})
            url = self.serve(store=Store(directory))    # the simulated band
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    self.open_menu(page, "bookmarks")
                    expect(page.locator("#bm-list .item .num")).to_have_text(["145.500"])
                first.locator("#bm-list button[data-edit]").click()
                field = first.locator(BOOKMARK_FREQUENCY)
                expect(field).to_have_value("145.500")
                expect(field).to_have_accessible_name("Frequency of FM calling")
                field.fill("145.61255")
                field.press("Enter")
                for page in (first, second):
                    expect(page.locator("#bm-list .item .num")).to_have_text(["145.61255"])
                    expect(page.locator("#bm-list .item .name")).to_have_text(["FM calling"])
                first.locator("#bm-list button.item").click()     # a bookmark tunes off the step's grid
                for page in (first, second):
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145612550")
                first.locator("#bm-list button[data-edit]").click()
                field.fill("145.6 MHz and more")
                field.press("Enter")
                expect(first.locator("#bm-list li[data-editing]")).to_have_count(1)
                expect(field).to_have_attribute("aria-invalid", "true")
                expect(first.locator("#bm-note")).to_have_text(BOOKMARK_FREQUENCY_NOTE)
                first.locator(BOOKMARK_NAME).fill("Calling")
                field.fill("145,5")
                first.locator("#bm-filter").click()       # leaving the row saves both
                for page in (first, second):
                    expect(page.locator("#bm-list .item .num")).to_have_text(["145.500"])
                    expect(page.locator("#bm-list .item .name")).to_have_text(["Calling"])
                expect(first.locator("#bm-note")).to_be_hidden()
                first.locator("#bm-list button[data-edit]").click()
                field.fill("0")
                field.press("Escape")                     # cancels, note and all
                expect(first.locator("#bm-list li[data-editing]")).to_have_count(0)
                expect(first.locator("#bm-note")).to_be_hidden()
                expect(second.locator("#bm-list .item .num")).to_have_text(["145.500"])

    def test_the_s_meter_sets_the_squelch_with_the_wheel_a_click_or_a_tap(self):
        # A notch of the wheel moves the threshold 1 dB, up raising it, and a
        # click or a tap on the bar sets it at that point of the meter's
        # scale, both within the threshold's range. Either turns the automatic
        # threshold off, the wheel starting from it. The squelch is shared.
        low, high = SQUELCH_RANGE
        for browser in BROWSERS:
            url = self.serve()      # a server for each browser, as the test changes the squelch
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                threshold, automatic = first.locator("#dm-sql"), first.locator("#dm-sql-auto")
                self.open_menu(first, "radio")
                expect(automatic).to_be_checked()
                expect(first.locator("#dm-sql-v")).to_have_text(re.compile(r"^-\d+ dB"))    # from the frames
                automatic.uncheck()
                threshold.fill(str(low + 5))    # a threshold by hand, far below the automatic one
                automatic.check()
                first.keyboard.press("Escape")      # the menu lies over the readout
                meter, bar = first.locator("#smeter"), first.locator("#smeter .bar")
                expect(meter).to_have_attribute("title", "Squelch: wheel or click")
                bar.hover()
                # The automatic threshold in use, where the S-meter marks it: it follows the noise.
                noise_based = S_ZERO + bar.locator(".sql").evaluate(
                    "(mark) => parseFloat(mark.style.left)") / 100 * (S_TOP - S_ZERO)
                first.mouse.wheel(0, -100)      # a notch up
                expect(automatic).not_to_be_checked()
                by_hand = int(threshold.input_value())
                self.assertAlmostEqual(by_hand, noise_based + 1, delta=1.5)    # not from the threshold by hand
                first.mouse.wheel(0, -100)
                expect(threshold).to_have_value(str(by_hand + 1))
                first.mouse.wheel(0, 100)       # a notch down
                expect(threshold).to_have_value(str(by_hand))
                self.open_menu(second, "radio")
                expect(second.locator("#dm-sql-auto")).not_to_be_checked()
                expect(second.locator("#dm-sql")).to_have_value(str(by_hand))
                box = bar.bounding_box()
                bar.click(position={"x": box["width"] - 1, "y": box["height"] / 2})     # past the range's top
                expect(threshold).to_have_value(str(high))
                first.mouse.wheel(0, -100)
                first.mouse.wheel(0, 100)
                expect(threshold).to_have_value(str(high - 1))      # the notch up went nowhere
                bar.click(position={"x": box["width"] / 2, "y": box["height"] / 2})
                expect(threshold).to_have_value(MID_SCALE)
                self.open_menu(first, "radio")
                threshold.fill(str(low + 1))
                first.keyboard.press("Escape")
                bar.hover()
                first.mouse.wheel(0, 100)
                first.mouse.wheel(0, 100)
                first.mouse.wheel(0, -100)
                expect(threshold).to_have_value(str(low + 1))       # the second notch down went nowhere
                self.open_menu(first, "radio")
                automatic.check()
                first.keyboard.press("Escape")
                bar.click(position={"x": 1, "y": box["height"] / 2})
                expect(automatic).not_to_be_checked()
                expect(threshold).to_have_value(re.compile(rf"^({S_ZERO}|{S_ZERO + 1})$"))     # the scale's start
            with self.subTest(browser=browser, touch=True), self.interfaces(browser, url, 1, **PHONE) as [phone]:
                phone.keyboard.press("Escape")      # the menu lies over the readout
                bar = phone.locator("#smeter .bar")
                box = bar.bounding_box()
                bar.tap(position={"x": box["width"] / 2, "y": box["height"] / 2})
                self.open_menu(phone, "radio")
                expect(phone.locator("#dm-sql")).to_have_value(MID_SCALE)

    def test_keys_open_and_close_each_menu_and_the_icon_opens_the_last_one(self):
        # Keys 1 to 8 open the menus, in their order, or close the one that
        # shows, except in a field or with Ctrl, Alt or Meta. Escape, a click
        # outside the menu and its close button close it, and nothing behind
        # it takes that click. About shows the version that runs.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                menu, title, icon = page.locator("#menu"), page.locator("#menu-title"), page.locator(MENU_BUTTON)
                for selector in REMOVED_CONTROLS:
                    expect(page.locator(selector)).to_have_count(0)
                expect(title).to_have_text("Receiver")      # which interfaces() opened
                expect(icon).to_have_attribute("aria-expanded", "true")
                for key, name in enumerate(MENUS, start=1):
                    page.keyboard.press(str(key))
                    expect(title).to_have_text(MENUS[name])
                    expect(page.locator("#menu section:not([hidden])")).to_have_attribute("data-menu", name)
                    expect(page.locator('#menu nav button[aria-current="true"]')).to_have_attribute("data-menu", name)
                expect(page.locator("#ab-version")).to_have_text(f"Radiolack {__version__}")
                page.keyboard.press("7")
                expect(page.locator('#menu section[data-menu="settings"]')).to_contain_text("1 to 8")
                page.keyboard.press("7")        # the menu that shows: closes it
                expect(menu).to_be_hidden()
                expect(icon).to_have_attribute("aria-expanded", "false")
                for key in ("9", "Control+2", "Alt+2", "Meta+2"):
                    page.keyboard.press(key)
                    expect(menu).to_be_hidden()
                page.keyboard.press("2")
                expect(title).to_have_text("Bookmarks")
                page.locator("#bm-filter").click()
                page.keyboard.type("3")         # into the field
                expect(page.locator("#bm-filter")).to_have_value("3")
                expect(title).to_have_text("Bookmarks")
                page.keyboard.press("Escape")   # from the keyboard: the focus goes back to the icon
                expect(menu).to_be_hidden()
                expect(icon).to_be_focused()
                icon.click()                    # the last menu shown
                expect(title).to_have_text("Bookmarks")
                page.get_by_role("button", name="Close the menu", exact=True).click()
                expect(menu).to_be_hidden()
                expect(icon).not_to_be_focused()
                page.keyboard.press("3")
                expect(title).to_have_text("Scan")
                vfo = page.locator("#freq").get_attribute("aria-valuenow")
                box = page.locator("#specwrap").bounding_box()
                page.mouse.click(box["x"] + box["width"] * 0.9, box["y"] + box["height"] / 2)   # beside the menu
                expect(menu).to_be_hidden()
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", vfo)     # nothing tuned there

    def test_in_one_column_the_menu_shows_its_list_then_the_menu_chosen(self):
        # In a narrow window the icon opens the list of menus, a menu takes
        # the whole card, and its arrow goes back to the list.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1, **NARROW) as [page]:
                nav, title = page.locator("#menu nav"), page.locator("#menu-title")
                back = page.get_by_role("button", name="Back to the menus", exact=True)
                expect(nav).to_be_hidden()      # the Receiver menu, alone
                back.click()
                expect(nav).to_be_visible()
                expect(title).to_be_hidden()
                expect(page.locator('#menu nav button[data-menu="receiver"]')).to_have_attribute("aria-current", "true")
                page.locator('#menu nav button[data-menu="scan"]').click()
                expect(nav).to_be_hidden()
                expect(title).to_have_text("Scan")
                page.keyboard.press("Escape")
                page.locator(MENU_BUTTON).click()   # the list again
                expect(nav).to_be_visible()
                page.keyboard.press("4")            # a key opens its menu
                expect(nav).to_be_hidden()
                expect(title).to_have_text("Record")
                back.click()
                page.set_viewport_size({"width": 1280, "height": 800})     # two columns: the list beside the last menu
                expect(nav).to_be_visible()
                expect(title).to_be_visible()
                expect(title).to_have_text("Record")
                expect(back).to_be_hidden()

    def test_preferences_are_kept_for_the_next_interfaces(self):
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            url = self.serve(store=Store(directory))    # no preference yet
            with self.subTest(browser=browser):
                with self.interfaces(browser, url, 1) as [page]:
                    self.open_menu(page, "settings")
                    expect(page.locator("#st-storage")).to_have_text(
                        f"Settings, bookmarks and history are saved in {Store(directory).location}.")
                    self.open_menu(page, "display")
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
                    self.open_menu(page, "scan")     # on its Signals tab
                    expect(page.locator('#scan-tabs button[data-tab="signals"]')).to_have_attribute(
                        "aria-pressed", "true")
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

    def test_a_signal_pressed_while_a_new_list_comes_takes_the_click(self):
        # Each list rebuilds the Signals table: on the simulated band, about
        # every second, as the beacons' margins move. A list that comes
        # between the press and the release of a click waits until the click,
        # which reaches the button pressed.
        for browser in BROWSERS:
            url = self.serve()      # nothing ignored yet
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "scan")     # on its Signals tab
                ignore = page.locator("#pk-body button[data-ignore]").first
                expect(ignore).to_be_visible()
                point = self.press(page, ignore)
                # The signal of the button pressed, whichever list showed it.
                frequency = page.evaluate("([x, y]) => document.elementFromPoint(x, y).dataset.ignore", point)
                self.assertIsNotNone(frequency)
                self.wait_for_signals(page, len(self.signals[page]) + 2)    # one at least after the press
                page.mouse.up()
                expect(page.locator("#pk-excluded")).to_have_text("1 ignored frequency.")
                expect(page.locator(f'#pk-body button[data-tune="{frequency}"]')).to_have_count(0)

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
                    self.open_menu(page, "bookmarks")      # the scan watches the ticked ones
                    expect(page.locator("#bm-list li")).to_have_text(
                        [re.compile(r"^Beacon"), re.compile(r"^Far away.*outside the span")])
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                    expect(page.locator(MENU_BUTTON)).not_to_have_class(SCANNING)
                first.locator("#mem-start").click()
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text(re.compile(r"^(Listening|Waiting): Beacon$"))
                    expect(page.locator("#mem-start")).to_have_text("Stop the scan")
                    expect(page.locator(MENU_BUTTON)).to_have_class(SCANNING)     # the icon's dot, green
                    expect(page.locator("#m-mode")).to_have_text("CW")
                    expect(page.locator("#status")).to_contain_text(f"{DOT} memory scan")
                second.keyboard.press("ArrowRight")     # one step up: tuning by hand stops the scan
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text("Stopped: the receiver was tuned by hand.")
                    expect(page.locator("#mem-start")).to_have_text("Scan the ticked bookmarks")
                    expect(page.locator(MENU_BUTTON)).not_to_have_class(SCANNING)
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
                    self.open_menu(page, "bookmarks")
                    expect(page.locator("#bm-list li")).to_have_text(
                        [re.compile(r"^Low.*out of reach"), re.compile(r"^Near[^·]*$"), re.compile(r"^Far[^·]*$")])
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                first.locator("#mem-start").click()
                for page in (first, second):    # Near and Far take a window each
                    expect(page.locator("#mem-state")).to_have_text("Watching 2 bookmarks, window 2 of 2")
                    expect(page.locator("#mem-start")).to_have_text("Stop the scan")
                    expect(page.locator("#status")).to_contain_text(f"{DOT} memory scan")
                second.locator("#mem-start").click()
                for page in (first, second):
                    expect(page.locator("#mem-state")).to_have_text("Stopped")
                    expect(page.locator("#mem-start")).to_have_text("Scan the ticked bookmarks")
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
                    self.open_menu(page, "scan")
                    page.locator('#scan-tabs button[data-tab="sweep"]').click()
                    expect(page.locator("#sw-to")).to_have_value("440.000")
                    expect(page.locator("#sw-state")).to_have_text("Stopped")
                field = first.locator("#sw-from")
                field.fill("1240")      # above the range's end: the whole range moves
                field.press("Enter")
                for page in (first, second):
                    expect(page.locator("#sw-from")).to_have_value("1240.000")
                    expect(page.locator("#sw-to")).to_have_value("1250.000")
                first.locator("#sw-window").press("ArrowRight")
                for page in (first, second):
                    expect(page.locator("#sw-window-v")).to_have_text("0.75 s")
                first.locator("#sw-start").click()
                for page in (first, second):     # 17 windows of 2.4 MHz, a quarter of the rate apart at most
                    expect(page.locator("#sw-state")).to_have_text(re.compile(r"^Pass 1, window ([2-9]|1\d) of 17$"))
                    expect(page.locator("#sw-start")).to_have_text("Stop")
                    expect(page.locator(MENU_BUTTON)).to_have_class(SCANNING)
                    expect(page.locator("#status")).to_contain_text(f"{DOT} sweep")
                second.locator("#sw-start").click()
                for page in (first, second):
                    expect(page.locator("#sw-state")).to_have_text("Stopped")
                    expect(page.locator("#sw-start")).to_have_text("Start")
                    expect(page.locator(MENU_BUTTON)).not_to_have_class(SCANNING)
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
                    self.open_menu(page, "scan")
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
                expect(first.locator("#hist-note")).to_have_text(f"145.5000 MHz added to the bookmarks as {V40_NAME}.")
                expect(first.locator("#marks .mark")).to_have_text([V40_NAME])
                clear = first.locator('#hist-body button[data-clear="433500000"]')
                clear.click()       # a first press asks for a second one
                expect(clear).to_have_text("Confirm")
                clear.click()
                expect(first.locator("#hist-body button[data-tune]")).to_have_text(["145.5000"])
                expect(first.locator("#hist-note")).to_be_hidden()
                # The other interface sees the change once it asks again, as when its tab shows.
                second.locator('#scan-tabs button[data-tab="signals"]').click()
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

    def test_a_history_button_pressed_while_the_table_changes_takes_the_click(self):
        # A first press of Clear that no second one follows turns back into
        # Clear, which rebuilds the table: a button pressed meanwhile stays
        # until the click, which reaches it.
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            heard = time.time()
            Store(directory).save(OBSERVATIONS_FILE, {"channels": [
                kept_channel(433_500_000, heard - 7200), kept_channel(145_500_000, heard - 600)]})
            url = self.serve(store=Store(directory))
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "scan")
                page.locator('#scan-tabs button[data-tab="history"]').click()
                clear = page.locator('#hist-body button[data-clear="433500000"]')
                clear.click()
                expect(clear).to_have_text("Confirm")
                self.press(page, page.locator('#hist-body button[data-mark="145500000"]'))
                page.wait_for_timeout(HISTORY_CONFIRM_MS + 500)     # the Clear turns back meanwhile
                page.mouse.up()
                expect(page.locator("#hist-note")).to_have_text(f"145.5000 MHz added to the bookmarks as {V40_NAME}.")
                expect(clear).to_have_text("Clear")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_every_interface_records_the_audio_and_lists_the_recordings(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP), recordings=folder)    # the VFO on 145.6 MHz
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                for page in (first, second):
                    self.open_menu(page, "record")
                    expect(page.locator("#rec-list .empty")).to_have_text("No recording yet")
                    expect(page.locator("#rec-summary")).to_contain_text(f"In {shown_path(folder)}, ")
                first.locator("#rec-audio").click()
                for page in (first, second):    # once the first audio made the file
                    expect(page.locator("#rec-state")).to_have_text(re.compile(
                        rf"^145\.600 MHz NFM {DOT} \d+:\d\d {DOT} [\d.]+ (B|KiB|MiB)$"))
                    expect(page.locator("#rec-audio")).to_have_text("Stop")
                    expect(page.locator(MENU_BUTTON)).to_have_class(RECORDING)
                    expect(page.locator("#status")).to_contain_text(f"{DOT} recording audio")
                    expect(page.locator("#rec-list li.rec .num")).to_have_text("Recording")
                second.locator("#rec-audio").click()
                for page in (first, second):
                    expect(page.locator("#rec-audio")).to_have_text("Record audio")
                    expect(page.locator(MENU_BUTTON)).not_to_have_class(RECORDING)
                    expect(page.locator("#status")).not_to_contain_text("recording")
                    expect(page.locator("#rec-list li.rec .name")).to_contain_text(["145.600 MHz NFM"])
                    expect(page.locator("#rec-list a.link")).to_have_text(["Download"])
                [recorded] = folder.iterdir()
                with first.expect_download() as downloading:
                    first.locator("#rec-list a.link").click()
                self.assertEqual(downloading.value.suggested_filename, recorded.name)
                downloaded = Path(downloading.value.path()).read_bytes()
                self.assertEqual((downloaded[:4], downloaded[8:12]), (b"RIFF", b"WAVE"))
                delete = first.locator("#rec-list button[data-del]")
                delete.click()      # a first press asks for a second one
                expect(delete).to_have_text("Confirm")
                delete.click()
                expect(first.locator("#rec-list .empty")).to_have_text("No recording yet")
                self.assertEqual(list(folder.iterdir()), [])
                # This computer's interfaces choose the folder, which must exist, in the Record menu.
                field, default = first.locator("#rec-folder"), first.locator("#rec-default")
                expect(field).to_have_value(shown_path(folder))
                expect(default).to_be_disabled()
                other, missing = folder.parent / "other", folder.parent / "missing"
                other.mkdir()
                field.fill(str(other))
                field.press("Enter")
                expect(field).to_have_value(shown_path(other))
                expect(default).to_be_enabled()
                field.fill(str(missing))
                field.press("Enter")
                expect(first.locator("#rec-folder-msg")).to_have_text(f"Refused: {shown_path(missing)} does not exist.")
                expect(field).to_have_value(shown_path(other))      # the folder in use
                default.click()
                expect(field).to_have_value(shown_path(folder))
                expect(first.locator("#rec-folder-msg")).to_be_empty()

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_every_interface_records_the_raw_iq_with_the_audio_and_downloads_its_two_files(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP), recordings=folder)    # on 145 MHz
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                for page in (first, second):
                    self.open_menu(page, "record")
                    expect(page.locator("#rec-list .empty")).to_have_text("No recording yet")
                    expect(page.locator("#rec-iq-state")).to_be_empty()
                first.locator("#rec-iq").click()
                for page in (first, second):    # once the first samples made the files
                    expect(page.locator("#rec-iq-state")).to_have_text(re.compile(
                        rf"^145\.000 MHz {DOT} 2\.400 MS/s {DOT} \d+:\d\d {DOT} [\d.]+ (B|KiB|MiB|GiB)$"))
                    expect(page.locator("#rec-iq")).to_have_text("Stop")
                    expect(page.locator("#rec-audio")).to_have_text("Record audio")
                    expect(page.locator("#rec-state")).to_be_empty()
                    expect(page.locator(MENU_BUTTON)).to_have_class(RECORDING)
                    expect(page.locator("#status")).to_contain_text(f"{DOT} recording I/Q")
                    expect(page.locator("#rec-list li.rec .num")).to_have_text("Recording")
                second.locator("#rec-audio").click()    # both at once
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(f"{DOT} recording audio and I/Q")
                    expect(page.locator("#rec-list li.rec .num")).to_have_text(["Recording", "Recording"])
                first.locator("#rec-audio").click()     # each stops on its own
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(f"{DOT} recording I/Q")
                    expect(page.locator("#rec-iq")).to_have_text("Stop")
                second.locator("#rec-iq").click()
                iq_row = first.locator("#rec-list li.rec", has_text="I/Q")
                for page in (first, second):
                    expect(page.locator("#rec-iq")).to_have_text("Record I/Q")
                    expect(page.locator("#rec-iq-state")).to_be_empty()
                    expect(page.locator(MENU_BUTTON)).not_to_have_class(RECORDING)
                    expect(page.locator("#status")).not_to_contain_text("recording")
                    expect(page.locator("#rec-list li.rec", has_text="I/Q").locator(".name")).to_contain_text(
                        re.compile(rf"^145\.000 MHz I/Q.* {DOT} 2\.400 MS/s {DOT} "))
                    expect(page.locator("#rec-list li.rec", has_text="I/Q").locator("a.link")).to_have_text(
                        ["Data", "Metadata"])
                [data] = folder.glob("*.sigmf-data")
                with first.expect_download() as downloading:
                    iq_row.get_by_role("link", name="Data", exact=True).click()
                self.assertEqual(downloading.value.suggested_filename, data.name)
                downloaded = Path(downloading.value.path()).read_bytes()
                self.assertTrue(downloaded and len(downloaded) % 2 == 0)        # whole I/Q pairs
                self.assertEqual(downloaded, data.read_bytes()[:len(downloaded)])   # its thread may be closing it
                with first.expect_download() as downloading:
                    iq_row.get_by_role("link", name="Metadata", exact=True).click()
                self.assertEqual(downloading.value.suggested_filename, data.name.replace(".sigmf-data", ".sigmf-meta"))
                metadata = json.loads(Path(downloading.value.path()).read_text(encoding="ascii"))
                self.assertEqual((metadata["global"]["core:datatype"], metadata["global"]["core:sample_rate"]),
                                 ("cu8", 2_400_000))
                self.assertEqual(metadata["captures"][0]["core:frequency"], 145_000_000)
                delete = iq_row.locator("button[data-del]")
                delete.click()      # a first press asks for a second one
                expect(delete).to_have_text("Confirm")
                delete.click()
                expect(first.locator("#rec-list li.rec")).to_have_count(1)
                expect(first.locator("#rec-list li.rec .name")).to_contain_text("145.600 MHz NFM")    # the audio's
                self.assertEqual([path.suffix for path in folder.iterdir()], [".wav"])

    def test_the_simulated_band_is_not_recorded(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
        url = self.serve(recordings=folder)
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "record")
                expect(page.locator("#rec-state")).to_have_text("The simulated band is not recorded.")
                expect(page.locator("#rec-audio")).to_be_disabled()
                expect(page.locator("#rec-iq")).to_be_disabled()
                expect(page.locator("#rec-iq-state")).to_be_empty()
                expect(page.locator("#rec-list .empty")).to_have_text("No recording yet")

    def test_a_recording_button_pressed_while_the_list_changes_takes_the_click(self):
        # A first press of Delete that no second one follows turns back into
        # Delete, which rebuilds the list: a button pressed meanwhile stays
        # until the click, which reaches it.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "recordings"
        folder.mkdir()
        started = time.time()
        older = recording_name(started - 7200, 433_500_000.0, "AM")
        newer = recording_name(started - 600, 145_500_000.0, "NFM")
        for name in (older, newer):     # recorded before, without audio: the list needs no receiver
            (folder / name).write_bytes(wav_header(12_000, 0))
        url = self.serve(recordings=folder)
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "record")
                armed = page.locator(f'#rec-list button[data-del="{older}"]')
                pressed = page.locator(f'#rec-list button[data-del="{newer}"]')
                armed.click()
                expect(armed).to_have_text("Confirm")
                self.press(page, pressed)
                page.wait_for_timeout(RECORDINGS_CONFIRM_MS + 500)  # the Delete turns back meanwhile
                page.mouse.up()
                expect(pressed).to_have_text("Confirm")
                expect(armed).to_have_text("Delete")

    def test_remote_access_says_why_it_cannot_turn_on(self):
        url = self.serve()      # which finds no address on a local network
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                self.open_menu(page, "settings")
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
                self.open_menu(page, "settings")
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
                self.open_menu(computer, "settings")
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
                    self.logo_box(device, 20)
                    device.locator("#pair-code").fill("ABCD-EFGH")
                    device.locator("#pair-submit").click()
                    expect(device.locator("#pair-msg")).to_have_text("Wrong code.")
                    device.locator("#pair-code").fill(code.lower())
                    device.locator("#pair-submit").click()
                    expect(device.locator("#status")).to_contain_text("MS/s")      # the interface, with frames
                    expect(device.locator("#status")).to_contain_text(REMOTE_FRAME_RATE)     # half the frames
                    self.open_menu(device, "settings")
                    expect(device.locator("#st-remote-client")).to_be_visible()
                    for section in ("#st-storage-section", "#st-remote-section"):
                        expect(device.locator(section)).to_be_hidden()
                    self.open_menu(device, "about")       # the version, without the updates or a report
                    expect(device.locator("#ab-remote-client")).to_be_visible()
                    expect(device.locator("#ab-version")).to_have_text(f"Radiolack {__version__}")
                    for section in ("#ab-update-section", "#ab-report-section"):
                        expect(device.locator(section)).to_be_hidden()
                    expect(device.locator("#ab-legal")).to_be_visible()
                    expect(device.locator("#status")).not_to_contain_text("remote access")
                    self.open_menu(device, "record")      # the recordings, not their folder
                    expect(device.locator("#rec-folder-section")).to_be_hidden()
                    # The device keeps its own settings when its page reloads, as Safari's often do.
                    self.open_menu(device, "display")
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

    def test_a_device_button_pressed_while_remote_access_changes_takes_the_click(self):
        # Every state of remote access rebuilds the list of paired devices:
        # one that comes between the press and the release of Forget, such
        # as another interface's new port, waits until the click, which
        # forgets the device pressed.
        for browser in BROWSERS:
            directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "radiolack"
            access = RemoteAccess()
            access.pair("Phone")    # before this start: remote access stays off, listening nowhere
            Store(directory).save(REMOTE_FILE, access.stored())
            url = self.serve(store=Store(directory))
            port = free_port()
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                for page in (first, second):
                    self.open_menu(page, "settings")
                self.press(first, first.get_by_role("button", name="Forget Phone", exact=True))
                second.locator("#st-remote-port").fill(str(port))
                second.locator("#st-remote-port").blur()
                expect(first.locator("#st-remote-port")).to_have_value(str(port))  # the state came during the press
                first.mouse.up()
                expect(first.locator("#st-remote-devices")).to_have_text("No paired device")

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
    def test_the_frequency_correction_takes_hundredths_and_is_shared(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                expect(first.locator("#src-address")).to_be_hidden()   # the source picks its own address
                field = first.locator("#src-ppm")
                for typed, shown in (("-2.6", "-2.60"), ("1.234", "1.23"), ("150", "100.00"), ("0", "0.00")):
                    field.fill(typed)
                    field.blur()        # the change applies when the field loses the focus
                    for page in (first, second):
                        expect(page.locator("#src-ppm")).to_have_value(shown)

    def test_calibrating_needs_a_receiver_with_a_frequency_correction(self):
        url = self.serve()      # the simulated band
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                expect(page.locator("#src-calibrate")).to_be_disabled()
                expect(page.locator("#src-calibrate-note")).to_have_text(
                    "The simulated band has no frequency correction to calibrate.")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_calibration_brings_a_carrier_to_its_true_frequency(self):
        # The fake's carrier, at 145.010 MHz on the air, shows 465.5 Hz low
        # through its crystal, 3.21 ppm fast.
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource("carrier", program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                page.keyboard.press("Escape")       # the Receiver menu lies over the readout
                page.locator("#freq").dblclick()
                page.locator("#freq input").fill("145.0095")
                page.locator("#freq input").press("Enter")
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145009500")
                self.open_menu(page, "receiver")
                page.locator("#src-calibrate").click()
                dialog, true = page.locator("#calibrate-dialog"), page.locator("#calibrate-true")
                expect(dialog).to_be_visible()
                expect(true).to_have_value("145.010")       # the VFO, rounded to the kilohertz
                expect(page.locator("#calibrate-in-use")).to_have_text("0.00 ppm")
                expect(page.locator("#calibrate-measured")).to_contain_text(
                    re.compile(rf"^145\.00953\d\d MHz {DOT} \d+ dB above the noise, "
                               r"46[45]\.\d Hz below its true frequency$"))
                expect(page.locator("#calibrate-correction")).to_have_text("3.21 ppm")
                apply = page.locator("#calibrate-apply")
                true.fill("146")                  # a typo, far beyond what a crystal is off
                expect(page.locator("#calibrate-note")).to_contain_text("beyond the -100 to 100 ppm")
                expect(apply).to_be_disabled()
                true.fill("145.012")              # 17 ppm: possible, but more than a drift
                expect(page.locator("#calibrate-note")).to_contain_text("more than a crystal drifts")
                expect(apply).to_be_enabled()
                true.fill("145.01")
                expect(page.locator("#calibrate-note")).to_have_text("")
                apply.click()
                expect(page.locator("#src-ppm")).to_have_value("3.21")
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145010000")
                expect(page.locator("#calibrate-in-use")).to_have_text("3.21 ppm")
                expect(page.locator("#calibrate-cancel")).to_have_text("Close")
                expect(page.locator("#calibrate-measured")).to_contain_text(
                    re.compile(r", 0\.\d Hz (above|below) its true frequency$"))
                page.locator("#calibrate-cancel").click()
                expect(dialog).to_be_hidden()

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_antenna_power_turns_on_with_a_second_press_and_off_with_one(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                power = first.locator("#src-power")
                expect(first.locator("#src-power-v")).to_have_text("off")
                expect(power).to_have_text("Turn on")
                power.click()
                expect(power).to_have_text("Confirm: 4.5 V on the antenna socket")
                expect(second.locator("#src-power-v")).to_have_text("off")     # nothing sent yet
                power.click()
                for page in (first, second):
                    expect(page.locator("#src-power-v")).to_have_text("on")
                    expect(page.locator("#src-power")).to_have_text("Turn off")
                second.locator("#src-power").click()                          # off at once
                for page in (first, second):
                    expect(page.locator("#src-power-v")).to_have_text("off")
                    expect(page.locator("#src-power")).to_have_text("Turn on")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_wfm_says_it_needs_a_higher_sample_rate_than_the_lowest_ones(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        note = "WFM needs a sample rate of 1.024 MS/s or more"
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                first.locator("#src-rate").select_option("240000")
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 0.240 MS/s")
                    expect(page.locator("#status")).not_to_contain_text(note)
                self.open_menu(second, "radio")
                second.locator('#mode-group button[data-mode="WFM"]').click()
                for page in (first, second):
                    expect(page.locator("#m-mode")).to_have_text("WFM")     # the mode stays
                    expect(page.locator("#status")).to_contain_text(f"{note} {DOT} RTL-SDR {DOT} 0.240 MS/s")
                expect(second.locator("#dm-audio-note")).to_have_text(f"{note}.")
                first.locator("#src-rate").select_option("1024000")
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 1.024 MS/s")
                    expect(page.locator("#status")).not_to_contain_text(note)
                expect(second.locator("#dm-audio-note")).to_be_hidden()
                second.locator('#mode-group button[data-mode="NFM"]').click()
                first.locator("#src-rate").select_option("2400000")
                expect(second.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_shortwave_samples_directly_below_the_tuner_without_gain_control(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                hf_input = first.locator("#src-hf")
                expect(hf_input).to_have_value("none")
                expect(first.locator("#src-hf-note")).to_contain_text("The tuner alone")
                expect(first.locator("#src-rtl-agc")).to_be_disabled()
                hf_input.select_option("q")
                for page in (first, second):
                    expect(page.locator("#src-hf")).to_have_value("q")
                    expect(page.locator("#src-hf-note")).to_contain_text("an RTL-SDR Blog V3")
                    expect(page.locator("#src-rtl-agc")).to_be_enabled()
                    expect(page.locator("#src-gain-note")).to_be_hidden()
                centre = first.locator("#src-center")
                centre.fill("7.1")
                centre.press("Enter")
                for page in (first, second):
                    expect(page.locator("#src-center")).to_have_value("7.100")
                    expect(page.locator("#src-gain-note")).to_have_text("Direct sampling: no gain control")
                    expect(page.locator("#src-gain")).to_be_disabled()
                    expect(page.locator("#src-agc")).to_be_disabled()
                    expect(page.locator("#src-hf-note")).to_contain_text(
                        "signals above and below 14.4 MHz mirror each other")
                first.locator("#src-rtl-agc").check()
                expect(second.locator("#src-rtl-agc")).to_be_checked()
                second.locator("#src-rtl-agc").uncheck()
                expect(first.locator("#src-rtl-agc")).not_to_be_checked()
                second.locator("#src-hf").select_option("none")     # the centre comes back within the tuner
                for page in (first, second):
                    expect(page.locator("#src-center")).to_have_value("24.000")
                    expect(page.locator("#src-gain-note")).to_be_hidden()
                    expect(page.locator("#src-agc")).to_be_enabled()
                    expect(page.locator("#src-rtl-agc")).to_be_disabled()

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_the_driver_s_computation_is_chosen_for_every_interface_and_labels_the_samples(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # on 145 MHz, placed 2.5 kHz below
        truncated = round(r82xx.tuned_hz("truncating", 144_997_500, 2_400_000))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                driver = first.locator("#src-driver")
                expect(driver).to_have_value("unknown")
                expect(driver).to_be_enabled()
                expect(first.locator("#src-driver-note")).to_contain_text("Calibrate will identify the driver")
                expect(first.locator("#src-center")).to_have_value("145.000")       # the centre asked
                expect(first.locator("#src-center-note")).to_have_text("In use: 144.9975 MHz, where the tuner sits.")
                driver.select_option("truncating")
                for page in (first, second):
                    expect(page.locator("#src-driver")).to_have_value("truncating")
                    expect(page.locator("#src-driver-note")).to_contain_text("the RTL-SDR Blog driver")
                    expect(page.locator("#src-center-note")).to_have_text(
                        f"In use: {mhz(truncated)} MHz, where the tuner sits.")
                self.assertIn({"type": "source", "driver_pll": "truncating"}, map(json.loads, self.sent[first]))
                second.locator("#src-driver").select_option("rounding")
                for page in (first, second):
                    expect(page.locator("#src-driver")).to_have_value("rounding")
                    expect(page.locator("#src-driver-note")).to_contain_text("rtl-sdr 2.0.2 and later")
                    expect(page.locator("#src-center-note")).to_have_text("In use: 144.9975 MHz, where the tuner sits.")
                second.locator("#src-driver").select_option("unknown")       # as each browser found it
                expect(first.locator("#src-driver")).to_have_value("unknown")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_converter_moves_the_frequencies_shown_and_the_receiver_stays_where_it_is_tuned(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                converter, oscillator = first.locator("#src-converter"), first.locator("#src-converter-lo")
                expect(converter).to_have_value("none")
                expect(oscillator).to_have_value("125.000")
                expect(first.locator("#src-converter-note")).to_have_text("Nothing between the antenna and the receiver.")
                expect(first.locator("#src-converter-lo-note")).to_contain_text("such as 125 MHz or 9750 MHz")
                converter.select_option("up")
                for page in (first, second):
                    expect(page.locator("#src-converter")).to_have_value("up")
                    expect(page.locator("#src-center")).to_have_value("20.000")
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "20600000")    # the same signal
                    expect(page.locator("#src-converter-note")).to_contain_text("an upconverter")
                # Behind an upconverter of 9750 MHz, all the tuner reaches is below 0 Hz: refused, as it was.
                oscillator.fill("9750 MHz")
                oscillator.press("Enter")
                expect(first.locator("#src-note")).to_contain_text(
                    "Change refused: through this converter the receiver reaches nothing on the air from 1.2 MHz")
                expect(oscillator).to_have_value("125.000")
                converter.select_option("down")
                oscillator.fill("9750")
                oscillator.press("Enter")
                for page in (first, second):
                    expect(page.locator("#src-converter-lo")).to_have_value("9750.000")
                    expect(page.locator("#src-center")).to_have_value("9895.000")
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "9895600000")
                    expect(page.locator("#src-converter-note")).to_contain_text("13 to 18 V")
                second.locator("#src-converter").select_option("none")
                for page in (first, second):
                    expect(page.locator("#src-center")).to_have_value("145.000")
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145600000")

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
                expect(first.locator("#src-note")).to_contain_text("Choose a receiver in the list above.")
                expect(first.locator("#src-hf-field")).to_be_hidden()     # the simulated band has no shortwave
                expect(first.locator("#src-power-row")).to_be_hidden()    # nor antenna power
                expect(first.locator("#src-driver-field")).to_be_hidden()     # nor a tuner on a grid
                expect(first.locator("#src-center-note")).to_be_hidden()
                receiver.select_option("rtl_sdr")
                for page in (first, second):
                    expect(page.locator("#src-device")).to_have_value("rtl_sdr")
                    expect(page.locator("#src-note")).to_contain_text("Rafael Micro R820T on this computer")
                    expect(page.locator("#src-hf-field")).to_be_visible()
                    expect(page.locator("#src-driver-field")).to_be_visible()
                receiver.select_option("rtl_tcp")
                for page in (first, second):
                    expect(page.locator("#status")).to_contain_text(
                        f"Waiting for rtl_tcp at {address}: connection refused\N{HORIZONTAL ELLIPSIS}")
                    expect(page.locator("#src-address")).to_have_value(address)
                second.locator("#src-device").select_option("synthetic")
                for page in (first, second):
                    expect(page.locator("#src-device")).to_have_value("synthetic")
                    expect(page.locator("#src-address")).to_be_hidden()

    def test_the_rtl_tcp_server_address_changes_in_the_receiver_menu(self):
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
                page.keyboard.press("Escape")       # the Receiver menu lies over the spectrum
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
                self.assertAlmostEqual(high - low, 2.4 / 1.15 * 1.5 ** 9, delta=0.2)      # past ten spans
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
                expect(page.locator("#src-center")).to_have_value("145.000")     # the view alone moved
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145600000")

    @unittest.skipUnless(os.name == "posix", "the fake rtl_tcp program stops on SIGTERM")
    def test_a_click_outside_the_span_moves_it_for_every_interface(self):
        FakeRtlTcpRuns(self)    # where the fake rtl_tcp program imports from
        for browser in BROWSERS:
            url = self.serve(RtlSdrSource(program=FAKE_RTL_TCP))     # each browser starts on 145 MHz
            with self.subTest(browser=browser), self.interfaces(browser, url, 2) as [first, second]:
                expect(first.locator("#status")).to_contain_text(f"RTL-SDR {DOT} 2.400 MS/s")
                for page in (first, second):
                    page.keyboard.press("Escape")   # the Receiver menu lies over the spectrum
                for _ in range(7):
                    first.keyboard.press("-")       # about 35.6 MHz wide, from 118.9 MHz
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
                centre = in_use(vfo - 75_000) / 1e6     # asked a 32nd of the span below the VFO, on the grid
                for page in (first, second):
                    expect(page.locator("#freq")).to_have_attribute("aria-valuenow", str(vfo))
                    expect(page.locator("#src-center")).to_have_value(mhz(vfo - 75_000))
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
                self.open_menu(page, "radio")
                page.locator("#dm-step").select_option("25000")
                page.keyboard.press("Escape")       # closes the menu, which lies over the spectrum
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                while " ago" not in self.waterfall_readout_at(page, 145.5, age):
                    self.assertLess(time.monotonic(), deadline, f"the waterfall has no line {age} lines down")
                    page.wait_for_timeout(100)
                freq = page.locator("#freq")
                freq.focus()
                for _ in range(4):      # steps of 250 kHz: the second goes past 145.96 MHz, into the span's upper tenth
                    freq.press("Shift+ArrowUp")
                deadline = time.monotonic() + TIMEOUT_MS / 1000
                # The view frames the new span, asked at 146.025 MHz, on the grid 15 kHz below.
                self.assertEqual(in_use(146_025_000), 146_010_000)
                while abs(self.view_edges(page)[0] - 144.81) > 0.005:
                    self.assertLess(time.monotonic(), deadline, "the span did not move")
                    page.wait_for_timeout(50)
                # The waterfall's older lines keep their frequencies, within the new span only.
                self.assertNotIn(" dB", self.waterfall_readout_at(page, 146.7, age))
                self.assertRegex(self.waterfall_readout_at(page, 145.5, age), rf"^145\.\d+ MHz {DOT} -?\d+ dB {DOT} \d+ s ago")
                # One move, 75 kHz below the step that went past: the steps made meanwhile add up.
                expect(page.locator("#src-center")).to_have_value("146.025")
                expect(freq).to_have_attribute("aria-valuenow", "146600000")
                centres = [message["centre_hz"] for message in map(json.loads, self.sent[page]) if "centre_hz" in message]
                self.assertEqual(centres, [146_025_000])
                low, high = self.view_edges(page)
                self.assertAlmostEqual(high, 147.21, delta=0.005)

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
                self.open_menu(page, "radio")
                page.locator("#dm-step").select_option("25000")
                page.keyboard.press("Escape")       # closes the menu, which lies over the spectrum
                span = self.view_edges(page)
                freq = page.locator("#freq")
                freq.focus()
                for _ in range(8):      # steps of 250 kHz, from 145.6 MHz: a source that does not tune keeps its span
                    freq.press("Shift+ArrowUp")
                expect(freq).to_have_attribute("aria-valuenow", "146200000")
                self.assertEqual(self.view_edges(page), span)
                self.assertEqual([message for message in self.sent[page] if "centre_hz" in message], [])

    def test_the_view_leaves_the_simulated_span_and_comes_back_to_the_vfo_or_the_span(self):
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the spectrum
                fit = page.get_by_role("button", name="Show the whole span")
                expect(fit).to_be_hidden()          # the view frames the span
                span = self.view_edges(page)
                for _ in range(3):
                    page.keyboard.press("-")        # past the span, as with a receiver that tunes
                low, high = self.view_edges(page)
                self.assertAlmostEqual(high - low, 2.4 / 1.15 * 1.5 ** 3, delta=0.02)
                expect(fit).to_be_visible()
                page.keyboard.press("0")
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
                expect(back).to_have_text(f"{mhz(vfo)} MHz \N{RIGHTWARDS ARROW}")
                expect(back).to_have_attribute("aria-label", f"Show the VFO, at {mhz(vfo)} MHz")
                back.click()
                expect(back).to_be_hidden()
                low, high = self.view_edges(page)   # around the VFO, at the same zoom
                self.assertAlmostEqual((low + high) / 2, vfo / 1e6, delta=0.001)
                self.assertAlmostEqual(high - low, (span[1] - span[0]) / 1.5 ** 3, delta=0.01)
                fit.click()
                expect(fit).to_be_hidden()
                self.assertEqual(self.view_edges(page), span)

    def test_the_view_visits_the_whole_plan_and_tunes_nothing_beyond_reach(self):
        # The simulated band, whose reach is its span: zooming out holds at
        # the span's width, then goes on to the whole plan, from 0 Hz. Beyond
        # the span nothing tunes: a mouse sees why before the click, and the
        # status line says it after; the context menu there disables Tune and
        # Ignore and keeps the rest.
        url = self.serve()      # the simulated band, from 143.8 to 146.2 MHz, the VFO on 145.6 MHz
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                expect(page.locator("#status")).to_contain_text(FRAME_RATE)
                page.keyboard.press("Escape")       # the Receiver menu lies over the spectrum
                span = self.view_edges(page)
                page.keyboard.press("-")            # one step out passes the detent at the span's width
                low, high = self.view_edges(page)
                self.assertAlmostEqual(high - low, 2.4 * 1.5 / 1.15, delta=0.01)
                for _ in range(25):
                    page.keyboard.press("-")        # out to the whole plan, and no further
                width = page.locator("#specwrap").bounding_box()["width"]
                low, high = self.view_edges(page)   # the spectrum's second pixel and its last but one
                self.assertAlmostEqual(low, PLAN_END_MHZ / width, delta=0.05)
                self.assertAlmostEqual(high, PLAN_END_MHZ * (width - 1) / width, delta=0.05)
                _, text = self.readout_at(page, 0.5)
                self.assertIn(f" MHz {DOT} out of reach", text)
                expect(page.locator("#tune-tip")).to_have_text("Beyond the receiver's range: a click tunes nothing")
                expect(page.locator("#span-preview")).to_be_hidden()
                demods = len([m for m in map(json.loads, self.sent[page]) if m.get("type") == "demod"])
                page.mouse.down()
                page.mouse.up()
                expect(page.locator("#status")).to_have_text(re.compile(
                    rf"^\d+\.\d+ MHz is beyond this receiver's range \(143\.800 to 146\.200 MHz\) {DOT} Synthetic source"))
                expect(page.locator("#freq")).to_have_attribute("aria-valuenow", "145600000")
                self.assertEqual(len([m for m in map(json.loads, self.sent[page]) if m.get("type") == "demod"]), demods)
                ctx = page.locator("#ctx")
                page.mouse.click(*self.spectrum_point(page, 0.5), button="right")
                expect(ctx).to_be_visible()
                for name in ("Tune here", "Ignore in the scanner"):
                    expect(ctx.get_by_role("menuitem", name=name)).to_be_disabled()
                for name in ("Add to bookmarks", "Centre the view here", "Zoom to the segment"):
                    expect(ctx.get_by_role("menuitem", name=name)).to_be_enabled()
                expect(ctx.get_by_role("menuitem", name="Add to bookmarks")).to_be_focused()
                page.keyboard.press("Escape")
                expect(ctx).to_be_hidden()
                page.keyboard.press("0")            # frames the span: there, Tune is back
                self.assertEqual(self.view_edges(page), span)
                page.mouse.click(*self.spectrum_point(page, 0.3), button="right")
                expect(ctx.get_by_role("menuitem", name="Tune here")).to_be_enabled()
                expect(ctx.get_by_role("menuitem", name="Ignore in the scanner")).to_be_enabled()
                page.keyboard.press("Escape")

    @staticmethod
    def spectrum_point(page: Page, fraction: float) -> tuple[float, float]:
        """The point of the spectrum ``fraction`` of the way from its first
        pixel to its last, where readout_at puts the pointer."""
        box = page.locator("#specwrap").bounding_box()
        return box["x"] + 1 + (box["width"] - 2) * fraction, box["y"] + box["height"] * 0.3

    def test_a_span_too_narrow_to_see_shows_as_a_marker(self):
        # Seen from far, the span shows as a marker 2 px wide in the spectrum
        # and the waterfall, which holds no measurement.
        url = self.serve()
        for browser in BROWSERS:
            with self.subTest(browser=browser), self.interfaces(browser, url, 1) as [page]:
                found = page.evaluate(SPAN_MARKER_PROBE)
                self.assertEqual(found["marker"], [3, 5])
                self.assertIsNone(found["wide"])                # 3 px wide: the span itself
                self.assertEqual(found["doubled"], [1.5, 5.5])  # 2 CSS px at twice the scale
                plain, marked = found["spectrum"]
                for x in (3, 4):
                    self.assertGreater(marked[x], plain[x] + 50)
                for x in (0, 1, 2, 5, 6, 7):
                    self.assertEqual(marked[x], plain[x])
                self.assertEqual([found["waterfall"][x] > 90 for x in range(8)], [False] * 3 + [True] * 2 + [False] * 3)
                self.assertEqual([found["waterfall"][x] for x in (0, 1, 2, 5, 6, 7)], [0] * 6)
                self.assertEqual(found["extents"], [[-0.7e6, 6.425e9], [0, 146.2e6], [0, 6.425e9]])
                self.assertEqual(found["fitted"], [[0, 4], [0, 100], [98, 100]])
                self.assertEqual(found["inView"], [[0, 0], [0, 2], [2, 2], [1, 3], [3, 3], [0, 3]])
                self.assertEqual(found["decimals"], [0, 0, 0, 1, 2, 3, 6, 6])
                self.assertEqual(found["note"], "5760.000 MHz is beyond this receiver's range (0.000 to 1767.200 MHz)")


if __name__ == "__main__":
    unittest.main()
