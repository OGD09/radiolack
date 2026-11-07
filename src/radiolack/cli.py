"""Command line interface.

Commands: profiles, plan, gqrx-profile, import, scan, watch, stats, check, stop.
Everything is receive-only. Monitoring commands launch at most one child
process (rtl_tcp) and only ever stop that child.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time

from . import __version__
from .errors import GqrxError, RadioLackError, ReceiverError, StopRequested, StorageError
from .gqrx import prepare_gqrx_profile
from .monitor import Monitor, MonitorSettings, console
from .ownership import command_line, describe, listening_pids
from .profiles import DEFAULT_PROFILE, ProfileError, builtin_profile_names, load_profile
from .receiver import (SUPPORTED_SAMPLE_RATES, Bridge, Capture, Endpoint, RtlTcpProcess, parse_endpoint,
                       port_in_use)
from .scanning import uncovered_channels, usable_span, window_centers
from .squelch import SquelchSettings
from .stats import frequency_statistics, parse_utc, store_summary
from .storage import ObservationStore, import_legacy, load_store, lock_path_for

DEFAULT_RTL_TCP = "127.0.0.1:1234"
DEFAULT_BRIDGE = "127.0.0.1:1235"
DEFAULT_GQRX = "127.0.0.1:7356"
DEFAULT_RATE = 2400000
DEFAULT_GAIN_DB = 29.6
STORE_NAME = "observations.json"
GQRX_PROFILE_NAME = "gqrx-radiolack.conf"
RTL_TCP_LOG_NAME = "rtl_tcp.log"
# Common RTL2832U USB ids (vendor:product); other RTL-SDR ids are not checked.
RTL_SDR_USB_IDS = {("0bda", "2838"), ("0bda", "2832")}
EXIT_INTERRUPTED = 130
EXIT_TERMINATED = 143


class UsageError(Exception):
    """Invalid option combination; reported like an argparse error (exit 2)."""


# -- paths -------------------------------------------------------------------

def data_directory(args) -> Path:
    value = args.data_dir or os.environ.get("RADIOLACK_DATA_DIR") or "data"
    return Path(value).expanduser().absolute()


def store_location(args) -> Path:
    value = args.store or os.environ.get("RADIOLACK_STORE")
    return Path(value).expanduser().absolute() if value else data_directory(args) / STORE_NAME


# -- validation ----------------------------------------------------------------

def _endpoint(text):
    try:
        return parse_endpoint(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _same_listener(first: Endpoint, second: Endpoint) -> bool:
    if first.is_loopback and second.is_loopback:
        return first.port == second.port
    return (first.host, first.port) == (second.host, second.port)


def validate_endpoints(args, *, managed_rtl_tcp: bool):
    if not args.bridge.is_loopback:
        raise UsageError(f"--bridge must be a loopback address (got {args.bridge})")
    if not args.gqrx.is_loopback:
        raise UsageError(f"--gqrx must be a loopback address: Gqrx has to run on this machine "
                         f"to read the local bridge (got {args.gqrx})")
    if managed_rtl_tcp and not args.rtl_tcp.is_loopback:
        raise UsageError("A managed rtl_tcp listens on loopback only; use --external-source for "
                         f"a remote rtl_tcp (got {args.rtl_tcp})")
    pairs = ((args.bridge, args.rtl_tcp), (args.bridge, args.gqrx), (args.rtl_tcp, args.gqrx))
    if any(_same_listener(first, second) for first, second in pairs):
        raise UsageError("--rtl-tcp, --bridge and --gqrx must use three different local ports")


def validate_tuning(args, profile):
    if args.rate not in SUPPORTED_SAMPLE_RATES:
        raise UsageError(f"--rate must be one of {', '.join(map(str, SUPPORTED_SAMPLE_RATES))}")
    center = profile.initial_center_hz if args.center is None else args.center
    if not profile.grid.contains(center):
        raise UsageError(f"--center must lie inside the profile band "
                         f"{profile.grid.low_hz}-{profile.grid.high_hz} Hz")
    return center


def profile_with_overrides(args):
    profile = load_profile(args.profile)
    overrides = {"margin_db": args.margin_db, "min_active_seconds": args.min_active_seconds,
                 "cooldown_seconds": args.cooldown_seconds,
                 "window_seconds": getattr(args, "window_seconds", None)}
    overrides = {key: value for key, value in overrides.items() if value is not None}
    return replace(profile, **overrides) if overrides else profile


# -- commands --------------------------------------------------------------------

def command_profiles(args):
    for name in builtin_profile_names():
        profile = load_profile(name)
        print(f"{profile.name}\n  {profile.summary}\n  {profile.description}")
    print("\nAny TOML file with the same structure can be passed to --profile.")
    return 0


def command_plan(args):
    profile = load_profile(args.profile)
    if args.rate not in SUPPORTED_SAMPLE_RATES:
        raise UsageError(f"--rate must be one of {', '.join(map(str, SUPPORTED_SAMPLE_RATES))}")
    centers = window_centers(args.rate, profile.grid.low_hz, profile.grid.high_hz)
    missing = uncovered_channels(profile.grid, args.rate, centers)
    print(f"Profile {profile.name}: {profile.summary}")
    print(f"{len(centers)} overlapping windows at {args.rate / 1e6:g} MS/s; about "
          f"{len(centers) * profile.window_seconds:g} s per quiet cycle, plus "
          f"{profile.listen_seconds:g} s per listen.")
    for index, center in enumerate(centers, start=1):
        low, high = usable_span(center, args.rate, profile.grid)
        print(f"  {index:3d}  focus {center / 1e6:11.6f} MHz  usable {low / 1e6:.3f}-{high / 1e6:.3f} MHz")
    channels = len(profile.grid.channels())
    if missing:
        print(f"WARNING: {len(missing)} of {channels} grid channels are not covered for some receiver "
              f"offsets, e.g. {', '.join(f'{f / 1e6:.6f}' for f in missing[:5])} MHz.")
        return 1
    print(f"Coverage: all {channels} grid channels, band edges and DC gaps included, for hardware "
          "center offsets up to 15 % of the sample rate.")
    return 0


def command_gqrx_profile(args):
    profile = load_profile(args.profile)
    validate_endpoints(args, managed_rtl_tcp=False)
    center = validate_tuning(args, profile)
    output = args.output if args.output is not None else data_directory(args) / GQRX_PROFILE_NAME
    try:
        path = prepare_gqrx_profile(output, bridge=args.bridge, remote=args.gqrx, sample_rate=args.rate,
                                    center_hz=center, mode=profile.mode, filter_hz=profile.filter_hz)
    except FileExistsError:
        print(f"radiolack: {Path(output).absolute()} already exists; it was not overwritten. "
              "Use it as is or choose another --output.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"radiolack: cannot create the Gqrx profile: {error}", file=sys.stderr)
        return 1
    print(f"Created Gqrx profile: {path}\n"
          f"Input: rtl_tcp={args.bridge} | remote control: {args.gqrx} | "
          f"{profile.mode} {profile.filter_hz / 1000:g} kHz\n"
          f"Open it with: gqrx -c {path}")
    return 0


def command_import(args):
    report = import_legacy(args.source, store_location(args), dry_run=args.dry_run)
    print(f"Source: {report.source_path}\n  SHA-256: {report.source_sha256}\n"
          f"  Legacy journal: {report.source_observations} validated listens, "
          f"{report.source_frequencies} frequencies, {report.source_stations} squelch memories, "
          f"{report.source_enriched} with telemetry")
    if not report.legacy_counters_checked:
        print("  Legacy counters: absent from the source (recomputed from the ledger)")
    elif report.legacy_counter_mismatches:
        print(f"  WARNING: legacy counters differ from the ledger for "
              f"{len(report.legacy_counter_mismatches)} frequencies: "
              + ", ".join(f"{key} stored {stored} / ledger {ledger}"
                          for key, (stored, ledger) in report.legacy_counter_mismatches.items()))
    else:
        print("  Legacy counters: identical to the counts recomputed from the ledger")
    print(f"Store: {report.store_path}{' (new)' if report.store_created else ''}\n"
          f"  Observations: {report.added} added, {report.already_present} already present, "
          f"{len(report.conflicts)} conflicts\n"
          f"  Squelch memories: {report.stations_added} added, {report.stations_unchanged} unchanged, "
          f"{len(report.stations_kept)} kept as in the store")
    if report.conflicts:
        print(f"  WARNING: {len(report.conflicts)} legacy rows differ from stored rows with the same "
              "identity; the stored rows were kept.")
    if args.dry_run:
        print("Dry run: nothing was written.")
    elif report.written:
        print(f"Imported ({report.import_id}). The source file was only read.")
    else:
        print("Nothing new: the store was not modified.")
    return 0


def command_monitor(args):
    profile = profile_with_overrides(args)
    validate_endpoints(args, managed_rtl_tcp=not args.external_source)
    center = validate_tuning(args, profile)
    if not math.isfinite(args.gain) or not 0 <= args.gain <= 50 or args.device < 0:
        raise UsageError("--gain must be between 0 and 50 dB and --device must not be negative")
    guard = args.meter_guard_seconds
    if not math.isfinite(guard) or not 0.5 <= guard <= 10:
        raise UsageError("--meter-guard-seconds must be between 0.5 and 10")
    settings = MonitorSettings(profile=profile, full_band=args.full_band, gqrx=args.gqrx,
                               squelch=SquelchSettings(fft_guard_s=guard))
    store = ObservationStore(store_location(args), create=args.create_store)
    bridge = backend = None
    try:
        capture = Capture(center, args.rate)
        bridge = Bridge(capture, args.bridge, args.rtl_tcp)
        if not args.external_source:
            log_path = args.rtl_tcp_log or data_directory(args) / RTL_TCP_LOG_NAME
            backend = RtlTcpProcess(args.rtl_tcp, center_hz=center, sample_rate=args.rate,
                                    gain_db=args.gain, device_index=args.device, log_path=log_path)
            backend.start()
        bridge.start()
        source = f"external rtl_tcp {args.rtl_tcp}" if args.external_source else \
            f"rtl_tcp {args.rtl_tcp} started by RadioLack (log: {backend.log_path})"
        console(f"I/Q source: {source}")
        console(f"Gqrx input: rtl_tcp={args.bridge} | Gqrx remote control: {args.gqrx}")
        console(f"Profile {profile.name}: {profile.summary}")
        console(f"Detection: margin >= {profile.margin_db:g} dB in {profile.confirm_observations} observations"
                f" | listen {profile.listen_seconds:g} s | active >= {profile.min_active_seconds:g} s | "
                f"observed >= {profile.min_observed_seconds:g} s | cooldown {profile.cooldown_seconds:g} s")
        console(f"Squelch: learned from Gqrx meter readings taken after {guard:g} s of stable FFT state")
        console(f"Store: {store.path} ({len(store.observations)} validated listens)")
        if args.full_band:
            count = len(window_centers(args.rate, profile.grid.low_hz, profile.grid.high_hz))
            console(f"Exploring the full band in {count} overlapping windows, "
                    f"{profile.window_seconds:g} s each when quiet.")
        else:
            console("Watching the current Gqrx window only.")
        console("Stop with Ctrl+C or `radiolack stop`: Gqrx settings are restored and "
                "incomplete listens are not logged.")
        Monitor(settings, capture, bridge, store, backend=backend).run()
    finally:
        if bridge is not None:
            bridge.close()
        if backend is not None:
            backend.stop()
        store.close()
    return 0


def _cell(value, template="{:.1f}"):
    return "-" if value is None else template.format(value)


def _short_time(text):
    parsed = parse_utc(text)
    return "-" if parsed is None else parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def command_stats(args):
    path = store_location(args)
    data = load_store(path)
    summary = store_summary(data["observations"], data["stations"])
    per_frequency = frequency_statistics(data["observations"])
    if args.frequency is not None:
        wanted = str(round(args.frequency * 1e6))
        per_frequency = {key: value for key, value in per_frequency.items() if key == wanted}
    rows = list(per_frequency.values())
    if args.sort == "count":
        rows.sort(key=lambda item: (-item["validated_detection_count"], item["frequency_hz"]))
    if args.limit is not None:
        rows = rows[:max(0, args.limit)]
    if args.json:
        print(json.dumps({"store": str(path), "summary": summary,
                          "frequencies": {str(item["frequency_hz"]): item for item in rows}}, indent=2))
        return 0
    print(f"Store: {path}")
    print(f"Validated listens: {summary['validated_listens']} | frequencies: "
          f"{summary['distinct_frequencies']} | squelch memories: {summary['stations_with_squelch_memory']}")
    print(f"Period (UTC): {_short_time(summary['first_confirmed_at_utc'])} to "
          f"{_short_time(summary['last_confirmed_at_utc'])}")
    print("Origins: " + (", ".join(f"{key} {value}" for key, value in summary["by_origin"].items()) or "-"))
    print("Required margin used (dB): "
          + (", ".join(f"{key}: {value}" for key, value in summary["by_required_margin_db"].items()) or "-"))
    print("Telemetry versions: "
          + (", ".join(f"{key}: {value}" for key, value in summary["by_telemetry_version"].items()) or "-"))
    print()
    print(f"{'MHz':>11} {'Listens':>7}  {'First (UTC)':16}  {'Last (UTC)':16} {'Active':>6} "
          f"{'Margin':>11} {'FFT bin':>13} {'Meter':>6}  Busiest UTC hours")
    print(f"{'':>11} {'':>7}  {'':16}  {'':16} {'frac':>6} {'mean/max dB':>11} {'mean/max dBFS':>13} "
          f"{'mean':>6}")
    for item in rows:
        hours = sorted(item["confirmations_by_utc_hour"].items(), key=lambda pair: (-pair[1], pair[0]))[:3]
        print(f"{item['frequency_mhz']:11.6f} {item['validated_detection_count']:7d}  "
              f"{_short_time(item['first_confirmed_at_utc']):16}  {_short_time(item['last_confirmed_at_utc']):16} "
              f"{_cell(item['active_fraction_of_observed'], '{:.2f}'):>6} "
              f"{_cell(item['mean_detection_median_margin_db']) + '/' + _cell(item['highest_peak_margin_db']):>11} "
              f"{_cell(item['mean_signal_bin_dbfs']) + '/' + _cell(item['max_signal_bin_dbfs']):>13} "
              f"{_cell(item['mean_gqrx_meter_dbfs']):>6}  "
              + " ".join(f"{hour}h:{count}" for hour, count in hours))
    print("\nCounts are validated listens, not conversations, transmissions or users. FFT-bin levels "
          "are relative dBFS (not dBm); meter values are Gqrx dBFS. '-' means unknown. "
          "Use --json for every field.")
    return 0


def _usb_rtl_sdr_present():
    found = []
    for device in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vendor = (device / "idVendor").read_text().strip()
            product = (device / "idProduct").read_text().strip()
        except OSError:
            continue
        if (vendor, product) in RTL_SDR_USB_IDS:
            found.append(f"{vendor}:{product}")
    return found


def command_check(args):
    """Read-only readiness report: opens no I/Q stream and sends no Gqrx command."""
    failures = 0

    def line(status, label, detail):
        nonlocal failures
        failures += status == "FAIL"
        print(f"{status:4}  {label:22} {detail}")

    executable = shutil.which("rtl_tcp")
    line("OK" if executable else "FAIL", "rtl_tcp", executable or "not installed (Debian: sudo apt install rtl-sdr)")
    executable = shutil.which("gqrx")
    line("OK" if executable else "FAIL", "gqrx", executable or "not installed (Debian: sudo apt install gqrx-sdr)")
    devices = _usb_rtl_sdr_present()
    line("OK" if devices else "WARN", "RTL-SDR USB device",
         f"present ({', '.join(devices)})" if devices else
         "no 0bda:2838/0bda:2832 device found (unplugged, or another id)")
    if args.rtl_tcp.is_loopback:
        busy = port_in_use(args.rtl_tcp)
        line("WARN" if busy else "OK", f"rtl_tcp port {args.rtl_tcp.port}",
             "in use: an rtl_tcp may already run (reuse it with --external-source)" if busy else "free")
    else:
        line("OK", "rtl_tcp source", f"external {args.rtl_tcp} (not probed)")
    if args.bridge.is_loopback:
        busy = port_in_use(args.bridge)
        line("FAIL" if busy else "OK", f"bridge port {args.bridge.port}",
             "in use by another program or RadioLack monitor" if busy else "free")
    else:
        line("FAIL", "bridge", f"{args.bridge} is not a loopback address")
    try:
        with socket.create_connection((args.gqrx.host, args.gqrx.port), timeout=1):
            pass
    except OSError:
        line("WARN", "Gqrx remote control", f"not reachable on {args.gqrx}: open Gqrx with the RadioLack "
             "profile and enable Tools > Remote control")
    else:
        holders = sorted(listening_pids(args.gqrx.port)) if args.gqrx.is_loopback else []
        owners = ", ".join(describe(pid) for pid in holders)
        if not holders:
            line("OK", "Gqrx remote control", f"reachable on {args.gqrx}")
        elif all(GQRX_PROFILE_NAME in command_line(pid) for pid in holders):
            line("OK", "Gqrx remote control", f"reachable on {args.gqrx}: {owners}")
        else:
            line("WARN", "Gqrx remote control", f"reachable on {args.gqrx} but held by {owners}, not by a "
                 f"Gqrx started with {GQRX_PROFILE_NAME}: RadioLack only drives the Gqrx reading its bridge")
    path = store_location(args)
    try:
        data = load_store(path)
        line("OK", "observation store", f"{path} ({len(data['observations'])} validated listens)")
    except StorageError as error:
        missing = not path.exists()
        line("WARN" if missing else "FAIL", "observation store",
             f"{path} missing: run `radiolack import ...` or use --create-store" if missing else str(error))
    directory = path.parent if path.parent.exists() else data_directory(args)
    writable = directory.exists() and os.access(directory, os.W_OK)
    line("OK" if writable else "WARN", "data directory",
         f"{directory} {'writable' if writable else 'missing or not writable'}")
    lock = lock_path_for(path)
    if lock.exists():
        with lock.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                line("WARN", "store lock", f"in use by RadioLack process {handle.read().strip() or '?'}")
    return 1 if failures else 0


def _lock_owner(lock: Path):
    """PID holding the store lock, or None when no process holds it."""
    with lock.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = handle.read().strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None


def command_stop(args):
    """Send SIGTERM to the RadioLack process holding this store's lock, and wait."""
    path = store_location(args)
    lock = lock_path_for(path)
    owner = _lock_owner(lock) if lock.exists() else None
    if owner is None:
        print(f"No RadioLack process is using {path}.")
        return 0
    if owner <= 0:
        raise StorageError(f"{lock} is locked but holds no process id; stop the process from its terminal.")
    try:
        command_line = Path(f"/proc/{owner}/cmdline").read_bytes()
    except OSError:
        command_line = b""
    if b"radiolack" not in command_line:
        raise RadioLackError(f"Process {owner} holds the store lock but does not look like RadioLack; "
                             "it was not signalled.")
    os.kill(owner, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if _lock_owner(lock) is None:
            print(f"Stopped RadioLack process {owner}.")
            return 0
        time.sleep(0.2)
    print(f"Process {owner} did not stop within {args.timeout:g} s; check its terminal.", file=sys.stderr)
    return 1


# -- parser ------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="radiolack", description=(
        "Receive-only radio exploration: shared rtl_tcp I/Q, FFT peak detection, Gqrx control "
        "and observation statistics."))
    parser.add_argument("--version", action="version", version=f"radiolack {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    paths = argparse.ArgumentParser(add_help=False)
    paths.add_argument("--data-dir", help="directory for the store, rtl_tcp log and Gqrx profile "
                                          "(default: $RADIOLACK_DATA_DIR or ./data)")
    paths.add_argument("--store", help="observation store (default: $RADIOLACK_STORE or "
                                       "DATA_DIR/observations.json)")
    band = argparse.ArgumentParser(add_help=False)
    band.add_argument("--profile", default=DEFAULT_PROFILE,
                      help="built-in profile name or TOML file (default: %(default)s)")
    ports = argparse.ArgumentParser(add_help=False)
    ports.add_argument("--rtl-tcp", type=_endpoint, default=DEFAULT_RTL_TCP,
                       help="rtl_tcp I/Q source (default: %(default)s)")
    ports.add_argument("--bridge", type=_endpoint, default=DEFAULT_BRIDGE,
                       help="local bridge that Gqrx reads (default: %(default)s)")
    ports.add_argument("--gqrx", type=_endpoint, default=DEFAULT_GQRX,
                       help="Gqrx remote control (default: %(default)s)")
    tuning = argparse.ArgumentParser(add_help=False)
    tuning.add_argument("--rate", type=int, default=DEFAULT_RATE,
                        help="sample rate in S/s (default: %(default)s)")
    tuning.add_argument("--center", type=int, help="initial hardware center in Hz (default: from profile)")

    commands.add_parser("profiles", help="list built-in band profiles")
    plan = commands.add_parser("plan", parents=[band], help="show the full-band window plan and its coverage")
    plan.add_argument("--rate", type=int, default=DEFAULT_RATE, help="sample rate in S/s (default: %(default)s)")
    gqrx = commands.add_parser("gqrx-profile", parents=[paths, band, ports, tuning],
                               help="create a separate Gqrx profile reading the bridge (never overwrites)")
    gqrx.add_argument("--output", type=Path, help="profile file (default: DATA_DIR/gqrx-radiolack.conf)")
    imported = commands.add_parser("import", parents=[paths],
                                   help="import a legacy pc_repair journal into the store (read-only source)")
    imported.add_argument("source", type=Path, help="legacy airband-peaks.json")
    imported.add_argument("--dry-run", action="store_true", help="report without writing")
    for name, full_band, text in (("scan", True, "explore the whole band in overlapping windows"),
                                  ("watch", False, "observe the current Gqrx window only")):
        monitor = commands.add_parser(name, parents=[paths, band, ports, tuning], help=text)
        monitor.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB,
                             help="rtl_tcp tuner gain in dB (default: %(default)s)")
        monitor.add_argument("--device", type=int, default=0, help="RTL-SDR device index (default: 0)")
        monitor.add_argument("--margin-db", type=float, help="detection margin (default: profile)")
        monitor.add_argument("--min-active-seconds", type=float, help="required RF presence (default: profile)")
        monitor.add_argument("--cooldown-seconds", type=float, help="delay before reselecting a frequency")
        monitor.add_argument("--meter-guard-seconds", type=float, default=SquelchSettings.fft_guard_s,
                             help="stable FFT state required before a Gqrx meter reading counts as "
                                  "carrier or noise for squelch learning (default: %(default)s)")
        if full_band:
            monitor.add_argument("--window-seconds", type=float, help="observation per quiet window")
        monitor.add_argument("--external-source", action="store_true",
                             help="use an already running rtl_tcp instead of starting one")
        monitor.add_argument("--rtl-tcp-log", type=Path, help="rtl_tcp log (default: DATA_DIR/rtl_tcp.log)")
        monitor.add_argument("--create-store", action="store_true",
                             help="create an empty store if none exists")
        monitor.set_defaults(full_band=full_band)
    stats = commands.add_parser("stats", parents=[paths], help="show statistics per frequency")
    stats.add_argument("--json", action="store_true", help="print every field as JSON")
    stats.add_argument("--sort", choices=("count", "frequency"), default="count")
    stats.add_argument("--limit", type=int, help="show only the first N frequencies")
    stats.add_argument("--frequency", type=float, metavar="MHZ", help="only this frequency, in MHz")
    commands.add_parser("check", parents=[paths, ports], help="read-only readiness report (no reception)")
    stop = commands.add_parser("stop", parents=[paths], help="stop the RadioLack process using the store")
    stop.add_argument("--timeout", type=float, default=15.0, help="seconds to wait (default: %(default)s)")
    return parser


COMMANDS = {"profiles": command_profiles, "plan": command_plan, "gqrx-profile": command_gqrx_profile,
            "import": command_import, "scan": command_monitor, "watch": command_monitor,
            "stats": command_stats, "check": command_check, "stop": command_stop}

_CATEGORIES = ((StorageError, "storage error"), (ReceiverError, "receiver error"),
               (GqrxError, "Gqrx error"), (RadioLackError, "error"))


def _terminate(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise StopRequested()


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    monitoring = args.command in ("scan", "watch")
    previous = signal.signal(signal.SIGTERM, _terminate) if monitoring else None
    try:
        return COMMANDS[args.command](args)
    except (ProfileError, UsageError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("Stopped. Incomplete listens were not logged." if monitoring else "Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except StopRequested:
        print("Stopped. Incomplete listens were not logged.", file=sys.stderr)
        return EXIT_TERMINATED
    except RadioLackError as error:
        label = next(text for kind, text in _CATEGORIES if isinstance(error, kind))
        print(f"radiolack: {label}: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"radiolack: system error: {error}", file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
