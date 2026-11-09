# Radiolack

Receive-only SDR receiver for the amateur radio bands. Radiolack reads I/Q
samples from an SDR receiver, shows a full-screen spectrum and waterfall,
demodulates the signal and scans the bands. It is a desktop application whose
interface is a web page, so that a phone, a tablet or another computer will be
able to control it and listen to it over the local network.

**Status: early rewrite.** No receiver is supported yet: the engine simulates
a 2 m band (synthetic I/Q computed in memory), computes its spectrum and
streams it to the interface, which draws the spectrum and the waterfall with
WebGL2. Nothing is demodulated yet, so there is no audio. The receiver
settings are shown but cannot be changed, tuning stays within the simulated
band, and levels are in dBFS, so the S-meter is not calibrated. Bookmarks and
settings are not saved yet, and the remote access switch is only the
mockup's: Radiolack keeps listening on 127.0.0.1 only. The plan, the
milestones and the decisions are in
[docs/design/scoping.md](docs/design/scoping.md).

**Receive-only.** Radiolack never transmits. It records no audio, no
transcription and no raw I/Q.

## Requirements

- Python 3.11 or later, aiohttp 3.8 or later and NumPy 1.24 or later.
- For the desktop window on Linux: PyGObject with GTK 3 and WebKitGTK 2.30 or
  later. Debian and Ubuntu: `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1`.
  Without them, Radiolack shows its interface in a web browser instead.
- The interface draws with WebGL2. Without it, its status line says so and
  the spectrum and the waterfall stay empty.
- Windows and macOS are planned for the public release.

## Installation

From the repository, in a virtual environment that can see the system's
PyGObject:

```bash
python3 -m venv --system-site-packages .venv
```

```bash
.venv/bin/pip install -e .
```

The `radiolack` command is then `.venv/bin/radiolack`. On Debian and Ubuntu,
`sudo apt install python3-aiohttp python3-numpy` provides aiohttp and NumPy
from the system, and `.venv/bin/pip install --no-build-isolation -e .` then
installs without downloading anything. To run without installing, use
`PYTHONPATH=src python3 -m radiolack` from the repository.

## Usage

```bash
radiolack
```

opens the interface in a desktop window. Closing the window stops Radiolack.

| Option | Effect |
| --- | --- |
| `--ui window` | Desktop window (default). When it cannot open (no display, no GTK or WebKitGTK), Radiolack says why, keeps running and prints the interface's address. |
| `--ui browser` | Opens the interface in the default web browser. |
| `--ui none` | Only prints the interface's address. |
| `--port PORT` | Listens on this port instead of any free one. |
| `--version` | Prints the version. |

Without a desktop window, stop Radiolack with Ctrl+C.

Radiolack listens on 127.0.0.1 only, so other devices cannot reach it yet. It
answers only requests addressed to 127.0.0.1 or localhost, which keeps web
pages from reaching it through DNS rebinding. The desktop window stays on the
interface: it opens no other page and no new window.

The interface receives the spectrum from the engine over a WebSocket on the
same address ([docs/design/protocol.md](docs/design/protocol.md)), which the
server accepts only from the interface's own origin. The engine computes
nothing while no interface is connected, and the interface closes its stream
while its page is hidden, in a background browser tab for instance.

Exit codes: 0 after a normal stop (window closed, Ctrl+C or SIGTERM), 1 when
the port cannot be used, 2 for invalid options.

## Tests

```bash
python3 -m pytest
```

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The tests need no receiver and open no window or browser; their servers
listen on ephemeral ports. GitHub Actions runs them against the installed
package on Python 3.11 and 3.15.

The interface's JavaScript carries JSDoc types, which TypeScript checks
without producing any file. The check needs Node.js, downloads TypeScript on
its first run and does not run in CI yet:

```bash
npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json
```

## Documentation

- [docs/design/scoping.md](docs/design/scoping.md): purpose, decisions,
  architecture, milestones, spikes and open questions.
- [docs/design/protocol.md](docs/design/protocol.md): the stream between the
  engine and the interface.
- [docs/design/ui-mockup.html](docs/design/ui-mockup.html): the validated
  visual reference, with simulated data; it opens directly in a browser.
- [docs/design/lessons.md](docs/design/lessons.md): what the rewrite reuses
  from the airband explorer.

## Previous version

Until the first milestone replaces it, the `main` branch keeps the previous
Radiolack, a Gqrx-driven explorer of the VHF airband. Its personal data in
`data/` stays untouched (see [data/README.md](data/README.md)).

## Project layout

```text
src/radiolack/
  cli.py        command line
  server.py     local web server (aiohttp): interface files and the stream
  engine.py     reads a source and shares spectrum frames with the interfaces
  spectrum.py   display FFT
  synthetic.py  simulated 2 m band
  window.py     desktop window (GTK 3 and WebKitGTK)
  web/          interface: ES modules served as they are, no build step
tests/          tests without hardware
docs/design/    design documents
jsconfig.json   settings of the interface's type check
```
