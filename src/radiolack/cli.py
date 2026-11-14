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
from .remote import DEFAULT_PORT, MIN_PORT, private_addresses
from .rtltcp import DEFAULT_ADDRESS, RTL_TCP_PROGRAM, RtlSdrSource, RtlTcpSource, parse_address
from .server import LOOPBACK_HOST, InterfaceUnreadable, LocalServer
from .sources import Sources
from .store import Store, settings_dir
from .updates import UpdateCheck


def _port(text: str, lowest: int = 0) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not lowest <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range {lowest}-65535: {value}")
    return value


def _remote_port(text: str) -> int:
    """A port that remote access accepts: lower ones need administrator rights."""
    return _port(text, MIN_PORT)


def _rtl_tcp_address(text: str) -> tuple[str, int]:
    """HOST, PORT or HOST:PORT, as rtltcp.parse_address reads them."""
    try:
        return parse_address(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _device(text: str) -> str:
    """A receiver's index or serial number, as rtl_tcp's -d option takes it."""
    if not text.strip():
        raise argparse.ArgumentTypeError("no receiver given")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiolack",
        description="Receive-only SDR receiver. Without --rtl-sdr or --rtl-tcp, it opens the "
                    "receiver used last, the simulated 2 m band the first time; the Source panel "
                    "switches between them. Settings, bookmarks and history are saved in "
                    "$XDG_CONFIG_HOME/radiolack (by default ~/.config/radiolack). Remote access, "
                    "for phones and tablets on the local network, is off at each start: Settings "
                    "or --remote turns it on.",
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
    # Without PORT, 0: remote access listens on the port it used last.
    parser.add_argument(
        "--remote", nargs="?", const=0, type=_remote_port, metavar="PORT",
        help="turn remote access on at start, for devices on this computer's local networks, on PORT "
             f"(default: the port used last, {DEFAULT_PORT} the first time), and print the addresses "
             "they open, the certificate's fingerprint and the access code; exit with status 1 when "
             "it cannot turn on",
    )
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument(
        "--rtl-sdr", nargs="?", const="0", type=_device, metavar="DEVICE",
        help="read the RTL-SDR receiver plugged into this computer, through an rtl_tcp that "
             "Radiolack starts while an interface is open: DEVICE is its index or serial "
             "number (default: 0, the first one)",
    )
    # Without ADDRESS, an empty address: the source takes the saved one.
    sources.add_argument(
        "--rtl-tcp", nargs="?", const=(), type=_rtl_tcp_address, metavar="ADDRESS",
        help="read an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS: HOST, "
             "PORT or HOST:PORT (default: the address used last, the first time "
             f"{DEFAULT_ADDRESS[0]}:{DEFAULT_ADDRESS[1]}); the Source panel can change it",
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
        server = LocalServer(args.port, source=source, sources=Sources(), store=Store(settings_dir()),
                             updates=UpdateCheck(), remote_addresses=private_addresses)
    except InterfaceUnreadable as error:
        print(f"radiolack: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"radiolack: cannot listen on {LOOPBACK_HOST}:{args.port}: "
              f"{error.strerror or error}", file=sys.stderr)
        return 1
    with server:
        if args.remote is not None and not _turn_on_remote(server, args.remote or None):
            return 1
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


def _turn_on_remote(server: LocalServer, port: int | None) -> bool:
    """Turn remote access on for --remote, on ``port`` or the port used last,
    and print what a device needs to reach it; False, once the error output
    says why, when it cannot."""
    try:
        state = server.turn_on_remote(port)
    except ValueError as error:     # a port that remote access refuses
        print(f"radiolack: remote access cannot turn on: {error}", file=sys.stderr)
        return False
    except TimeoutError:
        print("radiolack: remote access did not turn on in time", file=sys.stderr)
        return False
    if not state["on"]:
        print(f"radiolack: remote access cannot turn on: {state['problem']}", file=sys.stderr)
        return False
    print("Remote access is on: open one of these addresses on a device of this computer's local networks.")
    for url in state["urls"]:
        print(f"  {url}")
    print(f"Certificate fingerprint (SHA-256): {state['fingerprint']}")
    if state["blocked"]:
        print(f"Access code: {state['code']}, which no longer works after too many wrong codes: "
              "Settings > Remote access on this computer makes a new one.", flush=True)
    else:
        print(f"Access code: {state['code']}", flush=True)
    return True


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
