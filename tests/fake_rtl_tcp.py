"""Stand-in for the rtl_tcp program, run by tests in place of the real one.

It takes rtl_tcp's -a, -p, -n and -d options and serves FakeRtlTcp on the
port given. The device decides what it does:

- "none", "busy", "denied" and "crash": writes on stderr what rtl_tcp and
  librtlsdr write for a missing, busy or forbidden receiver, or something
  unknown, and exits.
- "mute": never listens, until stopped.
- "stubborn": serves, but ignores SIGTERM, logging "<pid> ignoring" each time.
- "carrier": serves CARRIER_HZ through a crystal CARRIER_CRYSTAL_PPB off,
  its R820T's PLL computed as rtl-sdr up to 2.0.1 does, until SIGTERM.
- "crackle": serves noise with an impulse of CRACKLE_SAMPLES samples every
  CRACKLE_PERIOD, which a noise blanker removes, until SIGTERM.
- any other value: serves until SIGTERM.

Like rtl_tcp, it opens the receiver for a moment before it listens. Each run
appends "<pid> started <arguments>" to the file that the FAKE_RTL_TCP_LOG
environment variable names, "<pid> command <command> <value>" for each
command it receives, then "<pid> stopping" on SIGTERM.
"""

import argparse
import asyncio
import functools
import os
import signal
import sys
import time

from radiolack import r82xx
from tests.fakes import FakeRtlTcp

OPEN_S = 0.2    # rtl_tcp opens the receiver before it listens
CARRIER_HZ = 145_010_000            # the "carrier" device's carrier, on the air
CARRIER_CRYSTAL_PPB = 3_210         # and its crystal's error
CRACKLE_PERIOD = 2_000              # the "crackle" device's impulses: one every this many samples,
CRACKLE_SAMPLES = 2                 # each this long
FOUND = ("Found 1 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 00000001\n\n"
         "Using device 0: Generic RTL2832U OEM\n")
FAILURES = {
    "none": ("No supported devices found.\n", 1),
    "busy": (FOUND + "usb_claim_interface error -6\nFailed to open rtlsdr device #0.\n", 1),
    "denied": (FOUND + "usb_open error -3\nPlease fix the device permissions, e.g. by "
               "installing the udev rules file rtl-sdr.rules\nFailed to open rtlsdr device #0.\n", 1),
    "crash": ("Something unexpected happened\n", 3),
}


def log(event: str) -> None:
    path = os.environ.get("FAKE_RTL_TCP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(f"{os.getpid()} {event}\n")


def stop(_signum, _frame) -> None:
    log("stopping")
    os._exit(0)


def ignore(_signum, _frame) -> None:
    log("ignoring")


async def serve(port: int, device: str) -> None:
    scene = {}
    if device == "carrier":
        scene = {"carriers": {CARRIER_HZ: 0.3}, "crystal_ppb": CARRIER_CRYSTAL_PPB,
                 "tuner_pll": functools.partial(r82xx.tuned_hz, "truncating")}
    elif device == "crackle":
        scene = {"carriers": {}, "impulses": (CRACKLE_PERIOD, CRACKLE_SAMPLES)}
    async with FakeRtlTcp(port=port, chunk_bytes=8000, **scene) as fake:
        fake.on_command = lambda command, value: log(f"command {command} {value}")
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", required=True)
    parser.add_argument("-p", type=int, required=True)
    parser.add_argument("-n", type=int)
    parser.add_argument("-d", default="0")
    args = parser.parse_args()
    log("started " + " ".join(sys.argv[1:]))
    if args.d in FAILURES:
        text, code = FAILURES[args.d]
        sys.stderr.write(text)
        sys.exit(code)
    signal.signal(signal.SIGTERM, ignore if args.d == "stubborn" else stop)
    sys.stderr.write(FOUND + "Found Rafael Micro R820T tuner\nTuned to 100000000 Hz.\n")
    sys.stderr.flush()
    time.sleep(OPEN_S)
    if args.d == "mute":
        while True:
            time.sleep(60)
    asyncio.run(serve(args.p, args.d))


if __name__ == "__main__":
    main()
