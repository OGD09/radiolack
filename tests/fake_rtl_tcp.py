"""Stand-in for the rtl_tcp program, run by tests in place of the real one.

It takes rtl_tcp's -a, -p, -n and -d options and serves FakeRtlTcp on the
port given. The device decides what it does:

- "none", "busy", "denied" and "crash": writes on stderr what rtl_tcp and
  librtlsdr write for a missing, busy or forbidden receiver, or something
  unknown, and exits.
- "mute": never listens, until stopped.
- "stubborn": serves, but ignores SIGTERM.
- any other value: serves until SIGTERM.

Like rtl_tcp, it opens the receiver for a moment before it listens. Each run
appends "<pid> started <arguments>" to the file that the FAKE_RTL_TCP_LOG
environment variable names, then "<pid> stopping" on SIGTERM.
"""

import argparse
import asyncio
import os
import signal
import sys
import time

from tests.fakes import FakeRtlTcp

OPEN_S = 0.2    # rtl_tcp opens the receiver before it listens
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


async def serve(port: int) -> None:
    async with FakeRtlTcp(port=port, chunk_bytes=8000):
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
    signal.signal(signal.SIGTERM, signal.SIG_IGN if args.d == "stubborn" else stop)
    sys.stderr.write(FOUND + "Found Rafael Micro R820T tuner\nTuned to 100000000 Hz.\n")
    sys.stderr.flush()
    time.sleep(OPEN_S)
    if args.d == "mute":
        while True:
            time.sleep(60)
    asyncio.run(serve(args.p))


if __name__ == "__main__":
    main()
