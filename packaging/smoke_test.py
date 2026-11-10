#!/usr/bin/env python3
"""Check a Radiolack command from the outside, as its user starts it.

    python3 packaging/smoke_test.py [--window] [COMMAND ...]

starts COMMAND, by default ``radiolack`` as the Debian package installs it,
with ``--ui none`` and an empty settings folder of its own, so that it
begins on the simulated band and leaves everyone's settings alone. It then
checks that Radiolack

- prints its version, and its address on 127.0.0.1;
- listens on 127.0.0.1 only, and answers only requests addressed to it;
- serves the interface with its Content-Security-Policy, and every file the
  interface loads, each with its type;
- streams the simulated band's spectrum to the interface, and refuses the
  stream to pages from other origins;
- stops on SIGTERM with status 0, without a traceback.

With --window, it also checks that the desktop window's toolkit, GTK 3 and
WebKitGTK through PyGObject, loads for the radiolack that this Python
imports, without opening a window. This needs a graphical display, such as
xvfb-run's.

It prints each check as it passes, and exits with 0 when all pass, 1
otherwise. It needs aiohttp, as Radiolack does. The CI runs it on the
package installed on each supported release (.github/workflows/package.yml),
and tests/test_packaging.py on the package's files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from asyncio.subprocess import DEVNULL, PIPE
from urllib.parse import urlsplit

import aiohttp

START_TIMEOUT_S = 60    # a first start imports NumPy and aiohttp, slowly on a busy runner
STEP_TIMEOUT_S = 30
STOP_TIMEOUT_S = 20
ADDRESS_LINE = re.compile(r"Radiolack is running at (http://127\.0\.0\.1:\d+/) ")
# The files that an interface file names: the page's style sheet and script,
# the modules they import, the audio worklet and the band plan.
FILE_NAME = re.compile(r"""["'](?:\./)?([a-z0-9][a-z0-9_-]*\.(?:css|js|json))["']""")
FILE_TYPES = {".css": "text/css", ".js": "text/javascript", ".json": "application/json"}
OTHER_SITE = "radiolack.example"


class CheckFailed(Exception):
    """A check failed; the message says which, and why."""


def check(condition: object, failure: str) -> None:
    if not condition:
        raise CheckFailed(failure)


def passed(what: str) -> None:
    print(f"ok: {what}", flush=True)


async def within(seconds: float, awaitable, failure: str):
    """``awaitable``'s result, or CheckFailed after ``seconds``."""
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except asyncio.TimeoutError:
        raise CheckFailed(f"{failure} within {seconds:g} s") from None


async def output(arguments: list[str], label: str) -> str:
    """What a short command, called ``label`` in messages, prints;
    CheckFailed unless it succeeds in time."""
    process = await asyncio.create_subprocess_exec(*arguments, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
    try:
        out, err = await within(STEP_TIMEOUT_S, process.communicate(), f"{label} did not finish")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    check(process.returncode == 0, f"{label} exited with status {process.returncode}: "
                                   f"{err.decode('utf-8', 'replace').strip()}")
    return out.decode("utf-8", "replace").strip()


async def check_window() -> None:
    probe = ("import radiolack\n"
             "from radiolack import window\n"
             "print(radiolack.__file__)\n"
             "print(window.unavailable_reason() or '')\n")
    # Isolated: the radiolack installed for this Python, not one in the
    # current folder or on PYTHONPATH.
    lines = (await output([sys.executable, "-I", "-c", probe], "the desktop window's check")).splitlines()
    check(len(lines) == 1, f"the desktop window cannot open: {' '.join(lines[1:])}")
    passed(f"the desktop window's toolkit loads, for {lines[0]}")


async def address(process: asyncio.subprocess.Process) -> str:
    while line := await process.stdout.readline():
        match = ADDRESS_LINE.search(line.decode("utf-8", "replace"))
        if match:
            return match.group(1)
    raise CheckFailed(f"Radiolack exited with status {await process.wait()} before printing its address")


async def check_listening(url: str) -> None:
    # On Linux every address of 127.0.0.0/8 reaches the loopback interface: a
    # server that listened on every address would answer on 127.0.0.2 too.
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.2", urlsplit(url).port), 5)
    except (OSError, asyncio.TimeoutError):
        passed("listens on 127.0.0.1 only")
        return
    writer.close()
    raise CheckFailed("Radiolack answers on 127.0.0.2 too: it listens beyond 127.0.0.1")


async def check_interface(session: aiohttp.ClientSession, url: str) -> None:
    async with session.get(url) as response:
        page = await response.text()
        check(response.status == 200, f"GET / answered {response.status}")
        check(response.content_type == "text/html", f"the page is served as {response.content_type}")
        policy = response.headers.get("Content-Security-Policy", "")
    check("<title>Radiolack</title>" in page, "GET / is not Radiolack's interface")
    check("default-src 'self'" in policy and "'unsafe-" not in policy,
          f"the page's Content-Security-Policy reads {policy!r}")
    passed("serves the interface, with its Content-Security-Policy")

    names, queue = set(), FILE_NAME.findall(page)
    while queue:
        name = queue.pop()
        if name in names:
            continue
        names.add(name)
        async with session.get(url + name) as response:
            body = await response.text()
            check(response.status == 200, f"GET /{name} answered {response.status}")
            expected = FILE_TYPES[os.path.splitext(name)[1]]
            check(response.content_type == expected, f"/{name} is served as {response.content_type}, "
                                                     f"not {expected}")
        if name.endswith(".json"):
            try:
                json.loads(body)
            except ValueError as error:
                raise CheckFailed(f"/{name} is not JSON: {error}") from None
        else:
            queue.extend(FILE_NAME.findall(body))
    check({"main.js", "style.css"} <= names, f"the page loads neither main.js nor style.css: {sorted(names)}")
    passed(f"serves the {len(names)} files the interface loads")

    async with session.get(url, headers={"Host": OTHER_SITE}) as response:
        check(response.status == 403, f"a request addressed to {OTHER_SITE} answered {response.status}")
    passed("refuses requests addressed to another host")


async def check_stream(session: aiohttp.ClientSession, url: str) -> None:
    try:
        async with session.ws_connect(url + "stream", headers={"Origin": f"http://{OTHER_SITE}"}):
            pass
    except aiohttp.WSServerHandshakeError as refusal:
        check(refusal.status == 403, f"the stream answered a page from another origin with {refusal.status}")
    else:
        raise CheckFailed("the stream accepted a page from another origin")
    passed("refuses the stream to pages from other origins")

    async with session.ws_connect(url + "stream", headers={"Origin": url.rstrip("/")}) as ws:
        first = await within(STEP_TIMEOUT_S, ws.receive(), "the stream said nothing")
        hello = json.loads(first.data) if first.type is aiohttp.WSMsgType.TEXT else {}
        check(isinstance(hello, dict) and hello.get("type") == "hello" and isinstance(hello.get("protocol"), int),
              f"the stream began with {first.data!r:.200}, not its hello")
        streaming = frame = False
        async def spectrum() -> None:
            nonlocal streaming, frame
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    streaming = streaming or (isinstance(data, dict) and data.get("type") == "source"
                                              and data.get("kind") == "synthetic" and data.get("status") == "streaming")
                elif message.type is aiohttp.WSMsgType.BINARY:
                    frame = frame or message.data[:1] == b"\x01"
                if streaming and frame:
                    return
            raise CheckFailed(f"the stream closed: {ws.close_code}")
        await within(STEP_TIMEOUT_S, spectrum(), "the stream sent no spectrum of the simulated band")
    passed(f"streams the simulated band's spectrum (protocol {hello['protocol']})")


async def run(command: list[str], window: bool) -> None:
    version = await output([*command, "--version"], f"{command[-1]} --version")
    check(re.fullmatch(r"radiolack \S+", version), f"--version printed {version!r}")
    passed(f"prints its version: {version}")
    if window:
        await check_window()

    with tempfile.TemporaryDirectory(prefix="radiolack-smoke-") as settings:
        process = await asyncio.create_subprocess_exec(
            *command, "--ui", "none", stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
            env={**os.environ, "XDG_CONFIG_HOME": settings})
        errors = asyncio.ensure_future(process.stderr.read())
        outcome = "exited by itself"
        try:
            url = await within(START_TIMEOUT_S, address(process), "Radiolack printed no address")
            rest = asyncio.ensure_future(process.stdout.read())   # so that the pipe never fills
            passed(f"runs at {url}")
            await check_listening(url)
            async with aiohttp.ClientSession() as session:
                await check_interface(session, url)
                await check_stream(session, url)
        finally:
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
                    outcome = "stopped on SIGTERM"
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    outcome = f"did not stop within {STOP_TIMEOUT_S} s of SIGTERM"
            error_output = (await errors).decode("utf-8", "replace")
            if error_output.strip():
                print(f"Radiolack's error output:\n{error_output.rstrip()}", file=sys.stderr, flush=True)
        await rest
    check(outcome == "stopped on SIGTERM" and process.returncode == 0,
          f"Radiolack {outcome}, with status {process.returncode}")
    check("Traceback" not in error_output, "Radiolack printed a traceback")
    passed("stops on SIGTERM, with status 0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Radiolack command from the outside.")
    parser.add_argument("--window", action="store_true",
                        help="also check that the desktop window's toolkit loads (needs a display)")
    parser.add_argument("command", nargs="*",
                        help="the command that starts Radiolack, after -- (default: radiolack)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args.command or ["radiolack"], args.window))
    except (CheckFailed, OSError, ValueError, aiohttp.ClientError) as failure:
        print(f"smoke_test: failed: {failure}", file=sys.stderr)
        return 1
    print("smoke_test: every check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
