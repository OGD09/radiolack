#!/usr/bin/env python3
"""Check that a SoapySDR search filtered by driver stays off the network:
later-milestones.md, section 1, the first pull request's second spike, and
spike 7 of scoping.md.

For each driver named, it runs the search as Radiolack will,
``SoapySDR.Device.enumerate({"driver": DRIVER})``, in a Python process of
its own, and reports every socket that the process or any of its threads
tried to make, and every call into librtlsdr. Three barriers keep the
search off the network and away from an RTL-SDR, whatever the modules do:

- the process runs in a network namespace of its own (``unshare -rn``),
  which has no interface up and no route;
- a seccomp filter, installed by a small library preloaded into the
  process before it runs any code, traps every ``socket()`` system call
  but those of the kernel's netlink family (which libusb uses to learn of
  devices plugged in), whichever library makes it and in whichever thread:
  the call fails with EACCES, and the trap writes its family and type on
  the error output. Every way to the network, or to a daemon that would
  reach it for the process (avahi-daemon, which SoapyRemote's search asks
  through D-Bus, a socket on the file system that a network namespace does
  not hide), starts with a socket, so a search that makes none can have
  sent nothing;
- the same library replaces librtlsdr's functions that list, name and open
  receivers: they report the call and find no receiver, so that no search
  opens an RTL-SDR, even one whose module the filter would not exclude.

Before any search, a check of its own makes sure the trap works: a socket
of the internet family and one of the Unix family must be refused and
reported. The library is compiled with the C compiler (``cc``) into a
temporary folder, removed at the end.

It refuses the ``rtlsdr`` driver and a search without a driver, which
reaches every module, SoapyRemote's included. ``remote``, the search that
does reach the network, is accepted as the control that shows the trap
catches it. It needs Linux on x86-64, ``unshare`` allowed to make a user
and network namespace, a C compiler, and the SoapySDR Python module; it
never opens a device. It runs from a checkout of the repository; the
Debian package does not install it.
"""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

# The local USB drivers that the design lists (later-milestones.md, section 1).
DRIVERS = ("hackrf", "airspy", "airspyhf", "lime", "bladerf", "sdrplay")
REFUSED = ("rtlsdr",)       # an RTL-SDR may be plugged in and in use: never searched
CONTROL = "remote"          # the search that reaches the network
TIMEOUT_S = 60.0            # for one search
EXIT_CLEAN = 0              # every search stayed off the network
EXIT_SENT = 1               # a search tried to make a socket or called librtlsdr
EXIT_UNABLE = 2             # the check could not run
TRAP = "radiolack-trap:"    # the library's lines on the error output

LIBRARY = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>

static void say(const char *text) {
    ssize_t written = write(2, text, strlen(text));
    (void)written;
}

static void trapped(int signal, siginfo_t *info, void *context) {
    ucontext_t *state = context;
    char line[128];
    int length = snprintf(line, sizeof line, "%s socket family=%lld type=%lld\n", TRAP,
                          (long long)state->uc_mcontext.gregs[REG_RDI],
                          (long long)(state->uc_mcontext.gregs[REG_RSI] & 0xf));
    if (length > 0) say(line);
    state->uc_mcontext.gregs[REG_RAX] = -EACCES;     /* the call fails */
    (void)signal;
    (void)info;
}

__attribute__((constructor)) static void install(void) {
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_sigaction = trapped;
    action.sa_flags = SA_SIGINFO;
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1),     /* x32 calls */
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_socket, 0, 2),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_NETLINK, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
    };
    struct sock_fprog program = {sizeof filter / sizeof filter[0], filter};
    if (sigaction(SIGSYS, &action, NULL) != 0 || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
            || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) != 0) {
        say(TRAP " not installed\n");
        _exit(3);
    }
    say(TRAP " installed\n");
}

/* librtlsdr's listing and opening: no receiver, and the call reported. */
uint32_t rtlsdr_get_device_count(void) { say(TRAP " rtlsdr_get_device_count\n"); return 0; }
const char *rtlsdr_get_device_name(uint32_t index) { (void)index; say(TRAP " rtlsdr_get_device_name\n"); return ""; }
int rtlsdr_get_device_usb_strings(uint32_t index, char *manufacturer, char *product, char *serial) {
    (void)index; (void)manufacturer; (void)product; (void)serial;
    say(TRAP " rtlsdr_get_device_usb_strings\n");
    return -1;
}
int rtlsdr_get_index_by_serial(const char *serial) { (void)serial; say(TRAP " rtlsdr_get_index_by_serial\n"); return -1; }
int rtlsdr_open(void **device, uint32_t index) { (void)device; (void)index; say(TRAP " rtlsdr_open\n"); return -1; }
"""

# The child processes' code, run with ``python3 -I``.
SELF_CHECK = """
import ctypes
import socket
for family in (socket.AF_INET, socket.AF_UNIX):
    try:
        socket.socket(family, socket.SOCK_DGRAM).close()
    except PermissionError:
        print("refused", family)
print("receivers", ctypes.CDLL(None).rtlsdr_get_device_count())
"""
SEARCH = """
import sys
import SoapySDR
found = SoapySDR.Device.enumerate({"driver": sys.argv[1]})
print("found", len(found))
"""

FAMILIES = {1: "unix", 2: "inet", 10: "inet6", 17: "packet"}
TYPES = {1: "stream", 2: "datagram", 3: "raw", 5: "seqpacket"}


@dataclass
class Search:
    """What one search did."""

    driver: str
    registered: bool | None = None                  # whether a module registers it, None if unknown
    found: int | None = None                        # devices listed, None if the search failed
    sockets: list[str] = field(default_factory=list)    # the sockets it tried to make
    rtlsdr: list[str] = field(default_factory=list)     # its calls into librtlsdr
    problem: str = ""                               # why the search could not run

    @property
    def clean(self) -> bool:
        return not self.sockets and not self.rtlsdr


def driver_name(text: str) -> str:
    """A driver's name, as SoapySDR registers it, for the command line."""
    name = text.strip()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
        raise argparse.ArgumentTypeError(f"not a driver's name: {text!r}")
    if name in REFUSED:
        raise argparse.ArgumentTypeError(f"{name} is never searched: an RTL-SDR may be plugged in and in use")
    return name


def describe_socket(line: str) -> str:
    match = re.search(r"family=(-?\d+) type=(-?\d+)", line)
    if match is None:
        return line
    family, kind = int(match[1]), int(match[2])
    return f"{FAMILIES.get(family, f'family {family}')} {TYPES.get(kind, f'type {kind}')}"


def trapped(stderr: str) -> tuple[list[str], list[str], bool]:
    """The sockets refused, the librtlsdr calls, and whether the trap was
    installed, from a child's error output."""
    sockets, rtlsdr, installed = [], [], False
    for line in stderr.splitlines():
        if not line.startswith(TRAP):
            continue
        what = line[len(TRAP):].strip()
        if what == "installed":
            installed = True
        elif what.startswith("socket "):
            sockets.append(describe_socket(what))
        elif what.startswith("rtlsdr_"):
            rtlsdr.append(what)
    return sockets, rtlsdr, installed


def command(library: Path, *program: str) -> list[str]:
    """A child process in a network namespace of its own, the library
    preloaded, without the session bus's address."""
    return ["unshare", "-rn", "env", "-u", "DBUS_SESSION_BUS_ADDRESS", "-u", "DBUS_SYSTEM_BUS_ADDRESS",
            f"LD_PRELOAD={library}", *program]


def python(code: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", code, *arguments)


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)


def build(folder: Path, compiler: str) -> Path:
    """The trap's library, compiled in ``folder``."""
    source = folder / "trap.c"
    library = folder / "trap.so"
    source.write_text(f'#define TRAP "{TRAP}"\n{LIBRARY}', encoding="utf-8")
    subprocess.run([compiler, "-shared", "-fPIC", "-O1", "-Wall", "-o", str(library), str(source)],
                   capture_output=True, text=True, timeout=TIMEOUT_S, check=True)
    return library


def self_check(library: Path) -> str:
    """Empty when the trap refuses and reports a socket of each family;
    otherwise what went wrong."""
    result = run(command(library, *python(SELF_CHECK)))
    sockets, rtlsdr, installed = trapped(result.stderr)
    if not installed:
        return f"the trap did not install: {result.stderr.strip()[-300:]}"
    if result.returncode != 0:
        return f"the check failed: {result.stderr.strip()[-300:]}"
    refused = [line for line in result.stdout.splitlines() if line.startswith("refused")]
    if len(refused) != 2 or sorted(sockets) != ["inet datagram", "unix datagram"]:
        return f"the trap let a socket through: refused {refused}, reported {sockets}"
    if "receivers 0" not in result.stdout.splitlines() or rtlsdr != ["rtlsdr_get_device_count"]:
        return f"librtlsdr's listing is not replaced: {result.stdout.strip()!r}, reported {rtlsdr}"
    return ""


def factories(library: Path) -> set[str] | None:
    """The drivers whose search the installed modules register, as
    ``SoapySDRUtil --info`` lists them, which loads the modules and searches
    nothing; None when it cannot tell."""
    if shutil.which("SoapySDRUtil") is None:
        return None
    try:
        result = run(command(library, "SoapySDRUtil", "--info"))
    except subprocess.TimeoutExpired:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Available factories..."):
            return {name.strip() for name in line.split("...", 1)[1].split(",") if name.strip()}
    return None


def search(library: Path, driver: str) -> Search:
    result = Search(driver)
    try:
        child = run(command(library, *python(SEARCH, driver)))
    except subprocess.TimeoutExpired:
        result.problem = f"no answer within {TIMEOUT_S:g} s"
        return result
    result.sockets, result.rtlsdr, installed = trapped(child.stderr)
    if not installed:
        result.problem = "the trap did not install"
    elif child.returncode != 0:
        last = [line for line in child.stderr.splitlines() if line and not line.startswith(TRAP)]
        result.problem = last[-1] if last else f"exit status {child.returncode}"
    else:
        counts = [line.split()[1] for line in child.stdout.splitlines() if line.startswith("found ")]
        result.found = int(counts[0]) if counts else None
    return result


def report(searches: Sequence[Search]) -> list[str]:
    lines = ["| Driver | Devices found | Sockets tried | librtlsdr calls | Verdict |",
             "| --- | --- | --- | --- | --- |"]
    for done in searches:
        if done.sockets:
            verdict = "tried to make a socket"
        elif done.rtlsdr:
            verdict = "called librtlsdr"
        elif done.problem:
            verdict = "could not run: " + done.problem
        elif done.registered is False:
            verdict = "no module installed here registers it: nothing searched"
        else:
            verdict = "stays off the network"
        found = "" if done.found is None else str(done.found)
        lines.append(f"| {done.driver} | {found} | {', '.join(done.sockets) or 'none'} |"
                     f" {', '.join(done.rtlsdr) or 'none'} | {verdict} |")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SoapySDR's search for each driver named, as Radiolack will, in a network namespace of"
                    " its own, every socket but netlink's refused and reported, librtlsdr's listing and opening"
                    " replaced; report what each search tried. Opens no device. Refuses rtlsdr and a search"
                    " without a driver; 'remote' is the control that does reach for the network.")
    parser.add_argument("drivers", nargs="*", type=driver_name, default=list(DRIVERS),
                        help=f"drivers to search (default: {' '.join(DRIVERS)})")
    parser.add_argument("--compiler", default="cc", help="the C compiler (default cc)")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    options = build_parser().parse_args(argv)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        print("The trap needs Linux on x86-64.", file=err)
        return EXIT_UNABLE
    for program in (options.compiler, "unshare"):
        if shutil.which(program) is None:
            print(f"{program} is not installed.", file=err)
            return EXIT_UNABLE
    with tempfile.TemporaryDirectory(prefix="radiolack-search-") as folder:
        try:
            library = build(Path(folder), options.compiler)
            problem = self_check(library)
        except subprocess.CalledProcessError as error:
            print(f"The trap's library did not compile: {error.stderr.strip()[-500:]}", file=err)
            return EXIT_UNABLE
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"The trap could not run: {error}", file=err)
            return EXIT_UNABLE
        if problem:
            print(f"The trap does not work here, so no search runs: {problem}", file=err)
            return EXIT_UNABLE
        print("The trap refused and reported a socket of the internet and the Unix families, and replaced"
              " librtlsdr's listing.", file=err)
        registered = factories(library)
        searches = []
        for driver in options.drivers:
            print(f"Searching driver={driver}", file=err, flush=True)
            done = search(library, driver)
            done.registered = None if registered is None else driver in registered
            searches.append(done)
    out.write("\n".join(report(searches)) + "\n")
    return EXIT_CLEAN if all(done.clean for done in searches) else EXIT_SENT


if __name__ == "__main__":
    sys.exit(main())
