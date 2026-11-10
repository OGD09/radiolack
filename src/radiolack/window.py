"""Native desktop window that shows the Radiolack interface.

On Linux the window is GTK 3 with the system's WebKitGTK, through PyGObject
(docs/design/scoping.md, spike 1). It loads the interface from the local
server, stays on that server, writes no browser data to disk, and returns
when it is closed or when the process receives SIGINT or SIGTERM.
"""

from __future__ import annotations

import signal
from urllib.parse import urlsplit

TITLE = "Radiolack"
# The program's name for the desktop: the window's class and application id,
# which match its menu entry (radiolack.desktop) and name its icon.
PROGRAM = "radiolack"
DEFAULT_SIZE = (1280, 800)
# Shown until the page paints; matches --bg in the interface.
BACKGROUND = "#0a0c0f"
_WEBKIT_API_VERSIONS = ("4.1", "4.0")


class WindowUnavailable(Exception):
    """The native window cannot open here; the message says why."""


def unavailable_reason() -> str | None:
    """Return why the native window cannot open, or None if it can."""
    try:
        _load_toolkit()
    except WindowUnavailable as reason:
        return str(reason)
    return None


def show(url: str) -> None:
    """Show the interface at ``url`` until the window is closed.

    Raises WindowUnavailable when the window cannot open.
    """
    Gdk, GLib, Gtk, WebKit2 = _load_toolkit()

    # A desktop program plays sound without a click first. WebKitGTK allows
    # that only through a policy given when the view is created. The context
    # is ephemeral: WebKit keeps its cache and storage in memory instead of
    # folders named after the program, as the engine saves what the
    # interface needs.
    view = WebKit2.WebView(
        web_context=WebKit2.WebContext.new_ephemeral(),
        website_policies=WebKit2.WebsitePolicies(autoplay=WebKit2.AutoplayPolicy.ALLOW))
    background = Gdk.RGBA()
    background.parse(BACKGROUND)
    view.set_background_color(background)
    view.connect("decide-policy", _stay_on_origin, _origin(url), WebKit2)

    Gtk.Window.set_default_icon_name(PROGRAM)   # once installed with its icon; otherwise none
    window = Gtk.Window(title=TITLE)
    window.set_default_size(*DEFAULT_SIZE)
    window.add(view)
    window.connect("destroy", Gtk.main_quit)

    def request_close(_signum, _frame):
        GLib.idle_add(window.close)

    stop_signals = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.signal(signum, request_close) for signum in stop_signals}
    try:
        view.load_uri(url)
        window.show_all()
        Gtk.main()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _load_toolkit():
    """Import GTK 3 and WebKitGTK and check for a display."""
    try:
        import gi
    except ImportError:
        raise WindowUnavailable("PyGObject is not installed") from None
    try:
        # Gdk too: unversioned, it would load GDK 4, which GTK 3 cannot use.
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
    except ValueError:
        raise WindowUnavailable("GTK 3 is not installed") from None
    for version in _WEBKIT_API_VERSIONS:
        try:
            gi.require_version("WebKit2", version)
            break
        except ValueError:
            continue
    else:
        raise WindowUnavailable("WebKitGTK is not installed")
    try:
        from gi.repository import GLib
        GLib.set_prgname(PROGRAM)   # before GTK starts, which otherwise names it after the script
        from gi.repository import Gdk, Gtk, WebKit2
    except ImportError as error:
        raise WindowUnavailable(f"GTK or WebKitGTK failed to load: {error}") from None
    if not hasattr(WebKit2, "WebsitePolicies"):
        raise WindowUnavailable("WebKitGTK 2.30 or later is required")
    if Gdk.Display.get_default() is None:
        raise WindowUnavailable("no graphical display")
    return Gdk, GLib, Gtk, WebKit2


def _origin(uri: str) -> tuple[str, str, int | None]:
    """Return the scheme, host and port that identify the origin of ``uri``."""
    parts = urlsplit(uri)
    try:
        port = parts.port
    except ValueError:  # Malformed port: matches no origin.
        port = -1
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def _stay_on_origin(_view, decision, decision_type, origin, WebKit2) -> bool:
    """Refuse new windows and navigation away from the interface.

    Without an address bar there is no way back, for example after a file is
    dropped on the window.
    """
    if decision_type == WebKit2.PolicyDecisionType.RESPONSE:
        return False
    uri = decision.get_navigation_action().get_request().get_uri()
    if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION and _origin(uri) == origin:
        return False
    decision.ignore()
    return True
