# Radiolack

Receive-only SDR receiver. Radiolack reads I/Q samples from an SDR receiver,
shows a full-screen spectrum and waterfall, demodulates the signal and scans
the bands. It covers every frequency its receiver tunes, and a colour strip
under the spectrum names what each range is used for, with detailed plans
inside the amateur bands. It is a desktop application whose interface is a
web page, so that a phone, a tablet or another computer will be able to
control it and listen to it over the local network.

**Status: early rewrite.** Radiolack reads an RTL-SDR receiver through
rtl_tcp, which it runs itself for a receiver plugged into this computer, or
simulates a 2 m band (synthetic I/Q computed in memory). The
engine computes the spectrum and streams it to the interface, which draws the
spectrum and the waterfall with WebGL2. The Source panel switches between the
simulated band and the receivers, sets the receiver's
centre frequency, sample rate, gain and frequency correction, changes the
address of an rtl_tcp server, and turns the removal of the DC spike on or
off; antenna power comes next. The engine
demodulates NFM, AM, USB, LSB and CW, and the interface plays the audio; the
Demod panel's CTCSS and processing settings do nothing yet. Levels are in
dBFS, so the S-meter is not calibrated. Settings and
bookmarks are kept from one start to the next, and Radiolack installs as a
package on Debian and Ubuntu. The remote access switch is
only the mockup's: Radiolack keeps listening on 127.0.0.1 only. The plan, the
milestones and the decisions are in
[docs/design/scoping.md](docs/design/scoping.md).

**Receive-only.** Radiolack never transmits. It records no audio, no
transcription and no raw I/Q.

**English only.** The interface, the code and the documentation are in
English, with no translations.

## Requirements

- Python 3.11 or later, aiohttp 3.8 or later and NumPy 1.24 or later.
- For the desktop window on Linux: PyGObject with GTK 3 and WebKitGTK 2.30 or
  later. Debian and Ubuntu: `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1`.
  Without them, Radiolack shows its interface in a web browser instead.
- The interface draws with WebGL2. Without it, its status line says so and
  the spectrum and the waterfall stay empty.
- For an RTL-SDR receiver: rtl_tcp, which comes with librtlsdr. Debian and
  Ubuntu: `sudo apt install rtl-sdr`.
- Windows and macOS, each with its own installer, are planned for the public
  release.

## Installation

### Debian and Ubuntu package

On Debian 12 or later and Ubuntu 24.04 or later, Radiolack installs as a
Debian package for the system's Python, with the `radiolack` command, a menu
entry and an icon. Build the package from the repository, with your name and
address as its maintainer; building needs only Python and dpkg-deb:

```bash
python3 packaging/build_deb.py --maintainer "Your Name <you@example.org>"
```

It prints the package's path, `dist/radiolack_VERSION_all.deb`. Install
that file with apt, which also installs what Radiolack needs (aiohttp,
NumPy, PyGObject, GTK 3 and WebKitGTK) and the rtl-sdr package it
recommends:

```bash
sudo apt install ./dist/radiolack_VERSION_all.deb
```

Radiolack then shows in the applications menu. A package built from a later
commit installs over an earlier one, and `sudo apt remove radiolack` removes
it, leaving your settings and bookmarks. DEBFULLNAME and DEBEMAIL, as
Debian's tools read them, can give the maintainer instead of
`--maintainer`. Radiolack does not update itself yet.

### From the repository

For development, install Radiolack from the repository, in a virtual
environment that can see the system's PyGObject:

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

opens the interface in a desktop window, on the source used last: the
simulated 2 m band the first time. The Source panel's Receiver list switches
to a receiver ([RTL-SDR receivers](#rtl-sdr-receivers)). Closing the window
stops Radiolack.

| Option | Effect |
| --- | --- |
| `--ui window` | Desktop window (default). When it cannot open (no display, no GTK or WebKitGTK), Radiolack says why, keeps running and prints the interface's address. |
| `--ui browser` | Opens the interface in the default web browser. |
| `--ui none` | Only prints the interface's address. |
| `--port PORT` | Listens on this port instead of any free one. |
| `--rtl-sdr [DEVICE]` | Reads the RTL-SDR receiver plugged into this computer instead of the source used last, through an rtl_tcp that Radiolack runs. DEVICE is the receiver's index or serial number (default 0, the first one): see [RTL-SDR receivers](#rtl-sdr-receivers). |
| `--rtl-tcp [ADDRESS]` | Reads an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS (default: the address used last, 127.0.0.1:1234 the first time), instead of the source used last. The Source panel can change the address. Not with `--rtl-sdr`. |
| `--version` | Prints the version. |

Without a desktop window, stop Radiolack with Ctrl+C.

Radiolack listens on 127.0.0.1 only, so other devices cannot reach it yet. It
answers only requests addressed to 127.0.0.1 or localhost, which keeps web
pages from reaching it through DNS rebinding. The desktop window stays on the
interface: it opens no other page and no new window.

The interface receives the spectrum, the audio, the receiver state, the
demodulation settings and the bookmarks from the engine, and sends its
changes, over a
WebSocket on the same address
([docs/design/protocol.md](docs/design/protocol.md)), which the server
accepts only from the interface's own origin. The engine computes nothing
while no interface is connected, and the interface closes its stream while
its page is hidden, in a background browser tab for instance, unless it
plays audio.

Exit codes: 0 after a normal stop (window closed, Ctrl+C or SIGTERM), 1 when
the port cannot be used or when `--rtl-sdr` finds no rtl_tcp installed, 2
for invalid options.

## RTL-SDR receivers

Radiolack reads RTL-SDR receivers through rtl_tcp. The Source panel's
Receiver list switches between the simulated band ("Synthetic band (2 m)"),
a receiver plugged into this computer ("RTL-SDR · this computer") and a
receiver behind an rtl_tcp server ("RTL-SDR · rtl_tcp"), for every open
interface; its other entries are not available yet. Each keeps its own
settings, demodulation included, and Radiolack opens the one used last at
the next start. "RTL-SDR · this computer" opens the receiver used last, the
first one plugged in the first time, and "RTL-SDR · rtl_tcp" connects to
the address used last. The options below choose one on the command line.

For a receiver plugged into this computer, Radiolack runs rtl_tcp itself:

```bash
radiolack --rtl-sdr
```

- Radiolack starts rtl_tcp, on 127.0.0.1 and a free port, when an interface
  opens, and stops it once no interface is open, or when Radiolack stops:
  the receiver then stays free for other programs, such as Gqrx. On Linux,
  should Radiolack end without stopping rtl_tcp, killed for instance, the
  system stops it (through setpriv, from util-linux).
- `--rtl-sdr 1` reads the second receiver, and `--rtl-sdr SERIAL` the one
  with that serial number, as rtl_tcp's `-d` option takes them.
- When rtl_tcp cannot open the receiver, the status line says why: no
  receiver found, in use by another program, or no permission (the udev
  rules of the rtl-sdr package give it). Radiolack tries again by itself,
  so plugging the receiver in or closing the other program is enough.

For a receiver behind an rtl_tcp that you run, on this computer or another
one, start rtl_tcp first, then Radiolack:

```bash
rtl_tcp -a 127.0.0.1
```

```bash
radiolack --rtl-tcp
```

`--rtl-tcp PORT`, `--rtl-tcp HOST` and `--rtl-tcp HOST:PORT` read the
rtl_tcp at that address, with an IPv6 address in brackets (`[::1]:1234`).
`--rtl-tcp` alone reads the address used last, on the command line or in the
Source panel, and rtl_tcp's default address, 127.0.0.1:1234, the first time.
rtl_tcp has no access control: anyone who reaches it can use the receiver,
so keep it on 127.0.0.1, or on a network you trust when the receiver is on
another computer.

The Source panel's "Server address" field changes the address while
Radiolack runs, for instance when the receiver's computer answers at another
address on another network: type HOST, PORT or HOST:PORT and press Enter.
Radiolack hangs up and connects there with the same settings, and every open
interface follows; the field shows the full address, HOST:1234 for a host
alone. An address Radiolack cannot read is refused, with the reason in the
panel, and the field shows the address in use again. When nobody answers
there, the status line says why and Radiolack tries again by itself until
you give another address.

rtl_tcp sends every sample, two bytes each: 4.8 MB/s at 2.4 MS/s, about
17 GB an hour, and 2 MB/s at 1.024 MS/s. Over a slow or metered link, such
as a phone's connection, lower the sample rate.

With either option:

- At each connection, Radiolack sends its own settings, since rtl_tcp keeps
  those of its previous client: 145 MHz, 2.4 MS/s, automatic gain and no
  frequency correction at first, then the latest ones.
- The Source panel names the tuner and sets the centre frequency, the sample
  rate (1.024 to 2.88 MS/s), the gain and the frequency correction. "Tuner
  AGC" gives automatic gain; otherwise the gain is one of those the tuner
  offers. The frequency correction, in whole ppm from -100 to +100, makes up
  for a receiver whose clock is off, which shifts every signal by as many
  millionths of its frequency (10 ppm: 1.45 kHz at 145 MHz): set it so that
  a known signal sits on its frequency. A change shows in every open
  interface. Once you press Enter or leave a field, it shows the setting in
  use: as the receiver took it, a whole ppm for instance, or the previous one
  when the change is refused, such as a frequency the tuner cannot reach,
  with the reason in the panel.
- "Remove the DC spike", on when Radiolack starts, takes the receiver's
  constant offset off the samples before the spectrum is computed; without
  it, the offset shows as a spike at the centre frequency. A signal exactly
  on the centre frequency goes with it. Every open interface shares this
  setting, which the engine applies to any source.
- Typing a frequency outside the received span, or choosing a bookmark there,
  moves the span so that the frequency sits a quarter of the span above its
  centre, clear of the receiver's DC spike. Other ways of tuning stay within
  the span.
- After a change of frequency, sample rate or frequency correction, the
  display waits 0.4 s for the receiver to settle.
- When rtl_tcp cannot be reached, stops sending or drops the connection, the
  status line says why and Radiolack tries again by itself, keeping its
  settings: after half a second at first, then waiting longer each time, up
  to 5 s between attempts. rtl_tcp serves one program at a time: while
  another one uses it, Radiolack waits.
- While no interface is open, Radiolack hangs up, which leaves rtl_tcp free
  for another program, and connects again when an interface opens. The
  first samples then take a moment, a little longer with `--rtl-sdr`, which
  starts rtl_tcp again.
- Tuners: Rafael Micro R820T and R828D (24 to 1766 MHz), Elonics E4000,
  Fitipower FC0012 and FC0013, FCI FC2580. HF through direct sampling or an
  upconverter and antenna power (bias-T) are not supported yet.

## Demodulation and audio

The engine demodulates one VFO, which every interface shows: a click on the
spectrum, the wheel, the keyboard or a bookmark tunes it, and a change made
in one interface shows in the others, the last change winning. The Demod
panel sets the mode, the filter width and the squelch, and every mode
plays. NFM audio is de-emphasized, its CTCSS tone is removed and its band
stops at 3.5 kHz. AM audio follows the carrier's level: its loudness depends
on the modulation, not on the signal's strength. USB and LSB keep their own
sideband, and CW sounds a carrier on the VFO at 700 Hz, through a filter
centred on the VFO, 500 Hz wide by default. A slow AGC brings SSB and CW
signals to the same level: it takes the gain down as soon as a signal
arrives and lets it come back at 10 dB per second. AM, SSB and CW audio runs
from 100 Hz to 5 kHz.

The squelch is automatic by default: it opens 8 dB above the noise around
the VFO, which the engine estimates from the spectrum, and closes 3 dB
lower. Unticking "Automatic" sets the threshold by hand, starting from where
the automatic one stood. The S-meter shows the channel's level in dBFS and
the threshold, and its bar changes colour while the squelch is open. In
SSB, whose level follows the voice, the squelch closes between words: a
threshold set by hand below the noise keeps it open.

Each interface plays the audio itself, at its own volume, about a quarter of
a second after the antenna; at volume 0 it receives none. The desktop window
plays it as soon as it opens. A web browser may hold it until the first
click or key press on the page, and the Demod panel then says so. Nothing is
recorded.

## Settings and bookmarks

Radiolack keeps its settings and bookmarks in `~/.config/radiolack`
(`$XDG_CONFIG_HOME/radiolack` when that variable is set):

- `settings.json` holds each receiver's settings: the Source panel's and the
  demodulation settings (VFO, mode, filter width, squelch). The simulated
  band and each receiver plugged into this computer, by index or serial
  number, have their own; rtl_tcp has one set whatever its address, which is
  saved with them. It also records the source in use, for the next start.
  It also holds the interface's own settings: the display, waterfall and
  overlay settings, the volume, the tuning step, the band plan's choice of
  mode and the scanners' settings. Remote access is never saved: it is off
  at each start.
- `bookmarks.json` holds the bookmarks.

The window keeps no browser data on disk: WebKit's cache and storage stay in
memory.

Every interface shows the same bookmarks and receiver settings. An
interface opens with the own settings of the interface that changed one
last; changing them in one interface leaves the other open ones as they
are. A change reaches the disk a few seconds later at most, and when
Radiolack stops.

The Bands panel adds a bookmark at the VFO, renames or removes one, and
tunes to one with its mode; the spectrum's context menu adds one anywhere.
The memory scan of the Scanners panel stops on the bookmarks ticked there.

Settings > Storage says where the files are and what went wrong with them;
the status line adds "changes not saved" while some changes do not reach
the disk. Each file is written whole, through a temporary file, so that a
crash or a power cut leaves the previous version. A file that Radiolack
cannot read as its own, damaged or edited by hand with a mistake, is renamed
`settings.json.invalid-<date>-<time>` (or `bookmarks.json…`) and Radiolack
starts without it. A file that cannot be opened, or that a newer Radiolack
wrote, is left as it is and not saved until the next start. To start again
from the defaults, delete the folder while Radiolack is stopped.

## Band plan

The strip under the spectrum shows what each range is used for, from 9 kHz to
6.425 GHz: broadcasting, aviation, shipping, mobile networks, satellites,
short-range devices, amateur bands and more. Inside the amateur bands it
follows the IARU Region 1 band plans, simplified. Hovering the strip names a
range and its edges, and clicking it zooms to the range. With Demod > "Mode
and step from the band plan", tuning into a range selects its usual mode, and
the Automatic tuning step follows the plan. The Bands panel lists the ranges
of the received span. Display > "Band plan" hides the strip.

The plan is a simplified table for Europe (ITU Region 1): a guide, not a legal
reference. National tables differ and are the reference, and local law may
forbid listening to some services or disclosing what they carry. Its format
and sources are in [docs/design/band-plan.md](docs/design/band-plan.md).

## Tests

```bash
python3 -m pytest
```

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

The tests need no receiver and open no window or browser (the browser tests
below run only on request); their servers listen on ephemeral ports, and
they use a temporary settings folder, never yours. Where dpkg-deb is
installed, they also build the Debian package in a temporary folder and run
its command. GitHub Actions runs them against Radiolack installed with pip,
on Python 3.11 and 3.15.

The interface's JavaScript carries JSDoc types, which TypeScript checks
without producing any file. The check needs Node.js and downloads TypeScript
on its first run:

```bash
npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json
```

The browser tests drive the interface in headless Chromium, Firefox and
WebKit through Playwright, against the simulated band, the fake rtl_tcp
program and rtl_tcp addresses where nothing listens: the stream, the Source
panel and its Receiver list, a change made in one interface showing in
another, the bookmarks and the saved interface settings. They
launch browsers, so they run only when
`RADIOLACK_BROWSER_TESTS=1` is set. They need the `browser` extra, which
pins Playwright's version, in the virtual environment of
[Installation](#installation), and Playwright's browsers: about 900 MB in
`~/.cache/ms-playwright`. On a system that lacks their libraries,
`.venv/bin/python -m playwright install-deps` installs them, with sudo.

```bash
.venv/bin/pip install -e ".[browser]"
```

```bash
.venv/bin/python -m playwright install --only-shell chromium firefox webkit
```

```bash
RADIOLACK_BROWSER_TESTS=1 .venv/bin/python -m unittest tests.test_browser -v
```

On a computer without a display or a GPU, such as a server, run the last
command under `xvfb-run -a` (Debian and Ubuntu: `sudo apt install xvfb`):
headless Firefox then needs a display to draw with WebGL2.

Firefox plays audio only through a sound server, PipeWire or PulseAudio:
without one, as on a server, its audio test fails. PulseAudio with only a
null sink gives it a silent output (Debian and Ubuntu:
`sudo apt install pulseaudio`), and `pulseaudio --kill` stops it:

```bash
pulseaudio --start --exit-idle-time=-1 -n --load=module-native-protocol-unix --load=module-null-sink
```

A second GitHub Actions workflow runs the type check and the browser tests,
under Xvfb and with that silent output, when the package, the tests or
`pyproject.toml` change.

## Documentation

- [docs/design/scoping.md](docs/design/scoping.md): purpose, decisions,
  architecture, milestones, spikes and open questions.
- [docs/design/protocol.md](docs/design/protocol.md): the stream between the
  engine and the interface.
- [docs/design/band-plan.md](docs/design/band-plan.md): the band plan's
  scope, sources and file format.
- [docs/design/ui-mockup.html](docs/design/ui-mockup.html): the validated
  visual reference, with simulated data; it opens directly in a browser.
- [docs/design/lessons.md](docs/design/lessons.md): what the rewrite reuses
  from the airband explorer.

## Previous version

The first milestone replaced the previous Radiolack, a Gqrx-driven explorer
of the VHF airband, on `main`. Its code stays in the history at commit
`f0e1cf6` (`git checkout f0e1cf6`), and its personal data in `data/` stays
untouched (see [data/README.md](data/README.md)).

## Project layout

```text
src/radiolack/
  cli.py        command line
  server.py     local web server (aiohttp): interface files and the stream
  engine.py     reads a source, removes its DC offset, shares spectrum
                frames, the demodulation settings and the bookmarks with
                the interfaces, sends audio to those that ask, and saves
                the settings
  store.py      settings and bookmarks files
  bookmarks.py  bookmarks and their checks
  demod.py      one VFO: channel level, squelch and the audio of each mode
  dsp.py        FIR filter design and FFT filters that shift and resample
  rtltcp.py     RTL-SDR receivers through rtl_tcp, run by Radiolack or by you
  sources.py    opens each kind of source, for the Receiver list and at start
  spectrum.py   display FFT, and the noise level around the VFO
  synthetic.py  simulated 2 m band
  window.py     desktop window (GTK 3 and WebKitGTK)
  web/          interface: ES modules served as they are, no build step
tests/          tests without hardware
docs/design/    design documents
packaging/      Debian package: build script, menu entry, icon and licence notice
jsconfig.json   settings of the interface's type check
LICENSE         GNU General Public License, version 3
```

## Licence

Copyright 2026 OGD09

Radiolack is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

Radiolack is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
A PARTICULAR PURPOSE. See the GNU General Public License, in
[LICENSE](LICENSE), for more details.
