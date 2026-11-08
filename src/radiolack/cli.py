"""Command-line entry point: start the local server and show the interface."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
import webbrowser
from collections.abc import Iterator

from . import __version__, window
from .server import LOOPBACK_HOST, LocalServer


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 0-65535: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiolack",
        description="Receive-only SDR receiver for the amateur radio bands.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--ui", choices=("window", "browser", "none"), default="window",
        help="show the interface in a native window (default), in the default web "
             "browser, or nowhere (only print its address)",
    )
    parser.add_argument(
        "--port", type=_port, default=0,
        help=f"port to listen on, on {LOOPBACK_HOST} only (default: any free port)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = LocalServer(args.port)
    except OSError as error:
        print(f"radiolack: cannot listen on {LOOPBACK_HOST}:{args.port}: "
              f"{error.strerror or error}", file=sys.stderr)
        return 1
    with server:
        if args.ui == "window":
            reason = window.unavailable_reason()
            if reason is None:
                window.show(server.url)
                return 0
            print(f"radiolack: no native window ({reason}); "
                  "open the address below in a web browser.", file=sys.stderr)
        elif args.ui == "browser" and not webbrowser.open(server.url):
            print("radiolack: no web browser could be opened; "
                  "open the address below in one.", file=sys.stderr)
        with _stop_signals() as stop:
            print(f"Radiolack is running at {server.url} (Ctrl+C to stop).", flush=True)
            # A timeout lets signal handlers run on every platform.
            while not stop.wait(0.5):
                continue
    return 0


@contextlib.contextmanager
def _stop_signals() -> Iterator[threading.Event]:
    """Turn SIGINT and SIGTERM into an event while the block runs."""
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    stop_signals = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.signal(signum, request_stop) for signum in stop_signals}
    try:
        yield stop
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
