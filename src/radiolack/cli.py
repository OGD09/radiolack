"""Command-line entry point: start the local server and show the interface."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import signal
import sys
import threading
import webbrowser
from collections.abc import Iterator

from . import __version__, window
from .rtltcp import RTL_TCP_PROGRAM, RtlSdrSource, RtlTcpSource
from .server import LOOPBACK_HOST, LocalServer
from .store import Store, settings_dir

RTL_TCP_ADDRESS = (LOOPBACK_HOST, 1234)     # where rtl_tcp listens by default


def _port(text: str, lowest: int = 0) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not lowest <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range {lowest}-65535: {value}")
    return value


def _rtl_tcp_address(text: str) -> tuple[str, int]:
    """HOST, PORT or HOST:PORT, an IPv6 address in brackets; rtl_tcp's
    default address gives the missing part."""
    host, port = RTL_TCP_ADDRESS
    if text.isdigit():
        return host, _port(text, 1)
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1:]
        if end < 0 or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(f"not an address: {text!r}")
        host = text[1:end]
        if rest:
            port = _port(rest[1:], 1)
    elif text.count(":") > 1:
        raise argparse.ArgumentTypeError(f"an IPv6 address goes in brackets, as in [::1]:1234: {text!r}")
    else:
        host, colon, port_text = text.partition(":")
        if colon:
            port = _port(port_text, 1)
    if not host:
        raise argparse.ArgumentTypeError(f"no host in {text!r}")
    return host, port


def _device(text: str) -> str:
    """A receiver's index or serial number, as rtl_tcp's -d option takes it."""
    if not text.strip():
        raise argparse.ArgumentTypeError("no receiver given")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiolack",
        description="Receive-only SDR receiver. Without --rtl-sdr or --rtl-tcp, it shows a "
                    "simulated 2 m band. Settings and bookmarks are saved in "
                    "$XDG_CONFIG_HOME/radiolack (by default ~/.config/radiolack).",
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
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument(
        "--rtl-sdr", nargs="?", const="0", type=_device, metavar="DEVICE",
        help="read the RTL-SDR receiver plugged into this computer, through an rtl_tcp that "
             "Radiolack starts while an interface is open: DEVICE is its index or serial "
             "number (default: 0, the first one)",
    )
    sources.add_argument(
        "--rtl-tcp", nargs="?", const=RTL_TCP_ADDRESS, type=_rtl_tcp_address, metavar="ADDRESS",
        help="read an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS: HOST, "
             f"PORT or HOST:PORT (default: {RTL_TCP_ADDRESS[0]}:{RTL_TCP_ADDRESS[1]})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source: RtlTcpSource | None = None
    if args.rtl_sdr is not None:
        if shutil.which(RTL_TCP_PROGRAM) is None:
            print(f"radiolack: {RTL_TCP_PROGRAM} is not installed (on Debian or Ubuntu: "
                  "sudo apt install rtl-sdr)", file=sys.stderr)
            return 1
        source = RtlSdrSource(args.rtl_sdr)
    elif args.rtl_tcp is not None:
        source = RtlTcpSource(*args.rtl_tcp)
    try:
        server = LocalServer(args.port, source=source, store=Store(settings_dir()))
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
