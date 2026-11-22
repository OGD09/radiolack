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

from . import __version__, report, window
from .recordings import default_folder
from .remote import DEFAULT_PORT, MIN_PORT, private_addresses
from .rtltcp import DEFAULT_ADDRESS, RTL_TCP_PROGRAM, RtlSdrSource, RtlTcpSource, parse_address
from .scanners import SCAN_NAMES, SCANS
from .engine import MAX_RADIOS, Source
from .server import LOOPBACK_HOST, InterfaceUnreadable, LocalServer
from .soapy import SoapySource, module_problem, parse_args
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


def _soapy_args(text: str) -> str:
    """SoapySDR device arguments that name a driver (soapy.parse_args)."""
    try:
        parse_args(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return text.strip()


class _AddRadio(argparse.Action):
    """--rtl-sdr, --rtl-tcp and --soapy: each adds a radio reading its
    receiver to ``radios``, in the order given, as (kind, value)."""

    def __call__(self, parser, namespace, values, option_string=None):
        kind = option_string.lstrip("-").replace("-", "_")
        radios = list(getattr(namespace, self.dest) or [])
        radios.append((kind, values))
        setattr(namespace, self.dest, radios)


def _checked_radios(parser: argparse.ArgumentParser, radios: list[tuple[str, object]]) -> None:
    """Exit through ``parser`` for more than MAX_RADIOS receivers, two rtl_tcp
    servers, or a receiver given twice."""
    if len(radios) > MAX_RADIOS:
        parser.error(f"{MAX_RADIOS} receivers at most")
    if sum(kind == "rtl_tcp" for kind, _ in radios) > 1:
        parser.error("one rtl_tcp server at most: the Receiver menu keeps one address")
    if len(set(radios)) < len(radios):
        parser.error("a receiver is given twice")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiolack",
        description="Receive-only SDR receiver. --rtl-sdr, --rtl-tcp and --soapy may come together and "
                    f"more than once, up to {MAX_RADIOS} receivers, each read by a radio of its own, in the "
                    "order given; without them, it opens the receivers used last, the simulated 2 m band "
                    "the first time; the Receiver menu switches between them and adds radios. Settings, bookmarks and history are saved in "
                    "$XDG_CONFIG_HOME/radiolack (by default ~/.config/radiolack), and recordings, "
                    "made only from the interface, in $XDG_DATA_HOME/radiolack/recordings (by default "
                    "~/.local/share/radiolack/recordings) unless Settings chooses another folder. "
                    "Remote access, for phones and tablets on the local network, is off at each "
                    "start: Settings or --remote turns it on.",
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
    parser.add_argument(
        "--scan", choices=SCANS,
        help="start a scan at start, on the first radio, which goes on without any interface: memory, the "
             "memory scan, or sweep, "
             "the range scan, with their saved settings, or signals, the range scan over the current window, "
             "stopping to listen; when it cannot start, say why and run on without it",
    )
    parser.add_argument(
        "--bias-tee", action="store_true",
        help="turn the antenna power (bias tee: 4.5 V on the antenna socket of an RTL-SDR Blog V3 or V4, "
             "or a SoapySDR device's own) on at start, for the first receiver given or reopened; off otherwise, "
             "and never saved",
    )
    parser.add_argument(
        "--rtl-sdr", nargs="?", const="0", type=_device, metavar="DEVICE", action=_AddRadio, dest="radios",
        help="read the RTL-SDR receiver plugged into this computer, through an rtl_tcp that "
             "Radiolack starts while an interface is open or a scan runs: DEVICE is its index "
             "or serial number (default: 0, the first one)",
    )
    # Without ADDRESS, an empty address: the source takes the saved one.
    parser.add_argument(
        "--rtl-tcp", nargs="?", const=(), type=_rtl_tcp_address, metavar="ADDRESS", action=_AddRadio,
        dest="radios",
        help="read an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS: HOST, "
             "PORT or HOST:PORT (default: the address used last, the first time "
             f"{DEFAULT_ADDRESS[0]}:{DEFAULT_ADDRESS[1]}); the Receiver menu can change it",
    )
    parser.add_argument(
        "--soapy", type=_soapy_args, metavar="ARGS", action=_AddRadio, dest="radios",
        help="read a receiver through SoapySDR (python3-soapysdr and the driver's module), such as an "
             "SDRplay, a HackRF or an RTL-SDR, while an interface is open or a scan runs: ARGS are its "
             "device arguments, KEY=VALUE separated by commas, which must name a driver, such as "
             "driver=sdrplay or driver=rtlsdr,serial=00000001",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    radios = args.radios or []
    _checked_radios(parser, radios)
    report.keep_recent_messages()   # for the reports that the interface offers to share
    kinds = {kind for kind, _ in radios}
    if "rtl_sdr" in kinds and shutil.which(RTL_TCP_PROGRAM) is None:
        print(f"radiolack: {RTL_TCP_PROGRAM} is not installed (on Debian or Ubuntu: "
              "sudo apt install rtl-sdr)", file=sys.stderr)
        return 1
    if "soapy" in kinds and (problem := module_problem()) is not None:
        print(f"radiolack: {problem}", file=sys.stderr)
        return 1
    given = [_source(kind, value) for kind, value in radios]
    try:
        server = LocalServer(args.port, source=given[0] if given else None, more_sources=given[1:],
                             sources=Sources(), store=Store(settings_dir()),
                             updates=UpdateCheck(), remote_addresses=private_addresses,
                             recordings=default_folder())
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
        if args.bias_tee:
            _power_antenna(server)
        if args.scan is not None:
            _start_scan(server, args.scan)
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


def _source(kind: str, value: object) -> Source:
    """The source of a receiver given at start."""
    if kind == "rtl_sdr":
        return RtlSdrSource(str(value))
    if kind == "rtl_tcp":
        return RtlTcpSource(*value)     # an address, or none: the saved one
    return SoapySource(str(value))


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


def _power_antenna(server: LocalServer) -> None:
    """Turn the antenna power on for --bias-tee; a source without it, such
    as the simulated band, ignores the option, which the error output says."""
    try:
        server.power_antenna()
    except ValueError as error:
        print(f"radiolack: --bias-tee is ignored: {error}", file=sys.stderr)
    except TimeoutError:
        print("radiolack: the antenna power did not turn on in time", file=sys.stderr)


def _start_scan(server: LocalServer, scan: str) -> None:
    """Start the scan given to --scan, before any interface opens; when it
    cannot start, the error output says why, and Radiolack runs on without
    it."""
    name = SCAN_NAMES[scan]
    try:
        server.start_scan(scan)
    except ValueError as error:
        print(f"radiolack: the {name} cannot start: {error}", file=sys.stderr)
    except TimeoutError:
        print(f"radiolack: the {name} did not start in time", file=sys.stderr)


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
