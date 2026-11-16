<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/identity/radiolack-dark.svg">
    <img src="docs/identity/radiolack-light.svg" alt="Radiolack">
  </picture>
</h1>

Receive-only SDR receiver. Radiolack reads I/Q samples from an SDR receiver,
shows a full-screen spectrum and waterfall, demodulates the signal and scans
the bands. It covers every frequency its receiver tunes, and a colour strip
under the spectrum names what each range is used for, with detailed plans
inside the amateur bands. It is a desktop application whose interface is a
web page, so that a phone, a tablet or another computer can also control it
and listen to it over the local network.

**Status: early rewrite.** Radiolack reads an RTL-SDR receiver through
rtl_tcp, which it runs itself for a receiver plugged into this computer, or
simulates a 2 m band (synthetic I/Q computed in memory). The
engine computes the spectrum and streams it to the interface, which draws the
spectrum and the waterfall with WebGL2 and keeps every setting in one menu
([The menu](#the-menu)). The Receiver menu switches between the
simulated band and the receivers, sets the receiver's
centre frequency, sample rate, gain and frequency correction, changes the
address of an rtl_tcp server, and turns the removal of the DC spike on or
off; antenna power comes next. The engine
demodulates NFM, AM, USB, LSB, CW and WFM, broadcast FM in mono, and the
interface plays the audio. The engine also
finds the signals in the span, which every interface lists, watches the
ticked bookmarks, outside the span too with a receiver that tunes, and
sweeps a range with such a receiver ([Scanners](#scanners)), from an
interface or, with `--scan`, at each start. Any interface records the
audio into WAV files and the receiver's raw I/Q into SigMF files, and lists
the recordings, which it downloads or deletes ([Recording](#recording)).
Levels are in
dBFS, so the S-meter is not calibrated. Settings and
bookmarks are kept from one start to the next, and Radiolack installs as a
package on Debian and Ubuntu. Remote access, off at each start unless
`--remote` turns it on, lets a phone or a tablet on the local network
control Radiolack and hear it, over HTTPS with an access code
([Remote access](#remote-access)). The plan, the
milestones and the decisions are in
[docs/design/scoping.md](docs/design/scoping.md).

**Receive-only.** Radiolack never transmits. It records the audio and the
receiver's raw I/Q only when an interface asks, never at start, and records
no transcription.

**English only.** The interface, the code and the documentation are in
English, with no translations.

## Requirements

- Python 3.11 or later, aiohttp 3.8 or later, NumPy 1.24 or later and, for
  remote access, cryptography 38 or later.
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
Debian package for the system's Python, with the `radiolack` command, its
manual page (`man radiolack`), a menu entry and an icon. apt also installs
what Radiolack needs (aiohttp, cryptography, NumPy, PyGObject, GTK 3,
WebKitGTK, and the certificates that apt needs to read its APT repository
over HTTPS) and the rtl-sdr package it recommends.

Each release on the repository's Releases page carries its package,
`radiolack_VERSION_all.deb`, and the package's checksum, `SHA256SUMS`, which
GitHub Actions built and checked
([Versions and releases](#versions-and-releases)). Download both into one
folder, check the package, then install it:

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

```bash
sudo apt install ./radiolack_VERSION_all.deb
```

Radiolack then shows in the applications menu. The package also adds
Radiolack's APT repository, so that apt installs each later release with the
system's other updates ([APT repository](#apt-repository)), and Radiolack
says when one is out ([Updates](#updates)). It also installs a service for
a computer without a display, which it never enables
([As a service](#as-a-service)). `sudo apt remove radiolack` removes
Radiolack but leaves your settings and bookmarks, and the repository's
source and key; `sudo apt purge radiolack` also removes the source and the
key, and the service's settings and History. The release notes also give
the command that checks the attestation of the package's build.

To install the latest commit instead, build the package from the
repository, with your name and address as its maintainer; building needs
only Python and dpkg-deb:

```bash
python3 packaging/build_deb.py --maintainer "Your Name <you@example.org>"
```

It prints the package's path, `dist/radiolack_VERSION_all.deb`, to install
with apt as above; a package built from a later commit installs over an
earlier one. DEBFULLNAME and DEBEMAIL, as Debian's tools read them, can give
the maintainer instead of `--maintainer`. The build is reproducible: from
the same commit, with the same maintainer and the same version of dpkg-deb,
it makes the same package, byte for byte, whatever dpkg-deb's own settings
in the environment, such as DPKG_DEB_COMPRESSOR_LEVEL, so that anyone can
rebuild a release's package and compare. Its files carry the commit's date,
or SOURCE_DATE_EPOCH when that is set: leave it unset to rebuild a release.

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
`sudo apt install python3-aiohttp python3-cryptography python3-numpy`
provides aiohttp, cryptography and NumPy from the system, and
`.venv/bin/pip install --no-build-isolation -e .` then installs without
downloading anything. To run without installing, use
`PYTHONPATH=src python3 -m radiolack` from the repository. Radiolack reads
its interface's files when it starts: restart it to see a change to
`src/radiolack/web/`.

## Usage

```bash
radiolack
```

opens the interface in a desktop window, on the source used last: the
simulated 2 m band the first time. The Receiver menu's list switches to a
receiver ([RTL-SDR receivers](#rtl-sdr-receivers)). Closing the window
stops Radiolack.

| Option | Effect |
| --- | --- |
| `--ui window` | Desktop window (default). When it cannot open (no display, no GTK or WebKitGTK), Radiolack says why, keeps running and prints the interface's address. |
| `--ui browser` | Opens the interface in the default web browser. |
| `--ui none` | Only prints the interface's address. |
| `--port PORT` | Listens on this port instead of any free one. |
| `--remote [PORT]` | Turns remote access on at start, on PORT (default: the port used last, 8443 the first time), and prints the addresses that devices open, the certificate's fingerprint and the access code: see [Without a window](#without-a-window). |
| `--scan memory`, `--scan sweep` | Starts the memory scan or the sweep at start, with its saved settings, without waiting for an interface: see [Without a window](#without-a-window). When it cannot start, Radiolack says why and runs on without it. |
| `--rtl-sdr [DEVICE]` | Reads the RTL-SDR receiver plugged into this computer instead of the source used last, through an rtl_tcp that Radiolack runs. DEVICE is the receiver's index or serial number (default 0, the first one): see [RTL-SDR receivers](#rtl-sdr-receivers). |
| `--rtl-tcp [ADDRESS]` | Reads an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS (default: the address used last, 127.0.0.1:1234 the first time), instead of the source used last. The Receiver menu can change the address. Not with `--rtl-sdr`. |
| `--version` | Prints the version. |

Without a desktop window, stop Radiolack with Ctrl+C.

Radiolack listens on 127.0.0.1 only, unless Settings or `--remote` turns
remote access on ([Remote access](#remote-access)). It answers only requests addressed to
127.0.0.1 or localhost, which keeps web pages from reaching it through DNS
rebinding. The desktop window stays on the interface: it opens no other page
and no new window. When WebKit's process that draws the page ends, after a
crash in WebKitGTK for instance, the window loads the page again, which
comes back as it was since the engine carried on, and Radiolack says so on
its error output (when the menu entry started it, the system journal with
GNOME, for instance). If it ends again within a minute, the window says
that the interface stopped, with a button that reloads it. WebKit paints
the page on the processor rather than the graphics card: its GPU painting
crashed WebKitGTK 2.50 (Debian 12), and on the processor it costs no more,
as the spectrum and the waterfall still draw on the graphics card (WebGL2).
To have WebKit paint on the graphics card, start Radiolack with
`WEBKIT_SKIA_ENABLE_CPU_RENDERING=0`.

The interface receives the spectrum, the audio, the receiver state, the
demodulation settings, the bookmarks and the signals found from the engine,
and sends its changes, over a
WebSocket on the same address
([docs/design/protocol.md](docs/design/protocol.md)), which the server
accepts only from the interface's own origin. The engine computes nothing
while no interface is connected and neither a scan nor a recording runs,
and the interface closes
its stream while
its page is hidden, in a background browser tab for instance, unless it
plays audio.

Exit codes: 0 after a normal stop (window closed, Ctrl+C or SIGTERM), 1 when
the port cannot be used, when `--rtl-sdr` finds no rtl_tcp installed or when
`--remote` cannot turn remote access on, 2 for invalid options.

### The menu

The spectrum, the waterfall and the readout, the frequency with its mode
and its S-meter, take the whole window. Everything else is in one menu,
behind an icon: a small square at the top left with a mouse, a round button
between the step buttons at the bottom on a touch screen. The menu lists
eight menus beside the one it shows:

- Radio: the mode, the band plan's mode and step, the squelch, the volume,
  the filter width and the tuning step.
- Bookmarks: the bookmarks, the band plan's ranges in the span and the
  memory scan ([Settings and bookmarks](#settings-and-bookmarks)).
- Scan: the signals found, the sweep and the History, in three tabs
  ([Scanners](#scanners)).
- Record: the recordings and, on this computer, their folder
  ([Recording](#recording)).
- Receiver: the source and its settings
  ([RTL-SDR receivers](#rtl-sdr-receivers)).
- Display: the spectrum's height, the spectrum, the waterfall, and the band
  plan and bookmarks on the spectrum.
- Settings: the interface's behaviour, the storage, remote access and the
  gestures and keys.
- About: the version that runs, on every device; on this computer, the
  updates ([Updates](#updates)) and a problem report ([Problem
  reports](#problem-reports)); Suggest an idea and Radiolack on GitHub,
  which open GitHub's pages in your web browser; and the legal notice
  ([Legal notice](#legal-notice)).

The keys 1 to 8 open these menus in this order, or close the one that
shows; in a field they type as usual, and with Ctrl, Alt or Meta they do
nothing. Escape, a click outside the menu or its close button closes it,
and the icon opens the last menu shown again. In a window 600 px wide or
less, the list of menus comes first, and a menu takes its place with an
arrow back to the list. A dot on the icon tells that a recording runs, in
red, or that the memory scan or the sweep runs, in green.

## RTL-SDR receivers

Radiolack reads RTL-SDR receivers through rtl_tcp. The Receiver menu's
list switches between the simulated band ("Synthetic band (2 m)"),
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
  opens or a scan starts, `--scan` at start included, and stops it once no
  interface is open and no scan runs, or when Radiolack stops:
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
Receiver menu, and rtl_tcp's default address, 127.0.0.1:1234, the first time.
rtl_tcp has no access control: anyone who reaches it can use the receiver,
so keep it on 127.0.0.1, or on a network you trust when the receiver is on
another computer.

The Receiver menu's "Server address" field changes the address while
Radiolack runs, for instance when the receiver's computer answers at another
address on another network: type HOST, PORT or HOST:PORT and press Enter.
Radiolack hangs up and connects there with the same settings, and every open
interface follows; the field shows the full address, HOST:1234 for a host
alone. An address Radiolack cannot read is refused, with the reason in the
menu, and the field shows the address in use again. When nobody answers
there, the status line says why and Radiolack tries again by itself until
you give another address.

rtl_tcp sends every sample, two bytes each: 4.8 MB/s at 2.4 MS/s, about
17 GB an hour, and 2 MB/s at 1.024 MS/s. Over a slow or metered link, such
as a phone's connection, lower the sample rate.

With either option:

- At each connection, Radiolack sends its own settings, since rtl_tcp keeps
  those of its previous client: 145 MHz, 2.4 MS/s, automatic gain and no
  frequency correction at first, then the latest ones.
- The Receiver menu names the tuner and sets the centre frequency, the sample
  rate (1.024 to 2.88 MS/s), the gain and the frequency correction. "Tuner
  AGC" gives automatic gain; otherwise the gain is one of those the tuner
  offers. The frequency correction, in whole ppm from -100 to +100, makes up
  for a receiver whose clock is off, which shifts every signal by as many
  millionths of its frequency (10 ppm: 1.45 kHz at 145 MHz): set it so that
  a known signal sits on its frequency. A change shows in every open
  interface. Once you press Enter or leave a field, it shows the setting in
  use: as the receiver took it, a whole ppm for instance, or the previous one
  when the change is refused, such as a frequency the tuner cannot reach,
  with the reason in the menu.
- "Remove the DC spike", on when Radiolack starts, takes the receiver's
  constant offset off the samples before the spectrum is computed; without
  it, the offset shows as a spike at the centre frequency. A signal exactly
  on the centre frequency goes with it. Every open interface shares this
  setting, which the engine applies to any source.
- Tuning outside the received span, with a click on the spectrum zoomed out
  past it ([Spectrum view](#spectrum-view)), a typed frequency, a bookmark,
  the Signals list, the History or the context menu, moves the span so that
  the frequency sits a 32nd of the span above its centre, 75 kHz at
  2.4 MS/s, with the channel at least 5 kHz from the centre: clear of the
  receiver's DC spike. The frequency shows at once, and the VFO goes there
  once frames come from the new span. A click beyond the receiver's reach
  tunes to the nearest frequency it reaches.
- Stepping (the wheel, the arrow keys, the − and + buttons) moves the span
  by the same rule when a step would enter the outer tenth of the span on
  either side, which the receiver's filter dims: about every megahertz at
  2.4 MS/s. One move at a time: steps made while the receiver settles add
  up, and the VFO goes there with the new span. Dragging the filter stays
  within the span, and so does stepping with a source that does not tune,
  such as the simulated band.
- After a change of frequency, sample rate or frequency correction, the
  display waits 0.4 s for the receiver to settle.
- When rtl_tcp cannot be reached, stops sending or drops the connection, the
  status line says why and Radiolack tries again by itself, keeping its
  settings: after half a second at first, then waiting longer each time, up
  to 5 s between attempts. rtl_tcp serves one program at a time: while
  another one uses it, Radiolack waits.
- While no interface is open and no scan runs, Radiolack hangs up, which
  leaves rtl_tcp free for another program, and connects again when an
  interface opens or a scan starts. The
  first samples then take a moment, a little longer with `--rtl-sdr`, which
  starts rtl_tcp again.
- Tuners: Rafael Micro R820T and R828D (24 to 1766 MHz), Elonics E4000,
  Fitipower FC0012 and FC0013, FCI FC2580. HF through direct sampling or an
  upconverter and antenna power (bias-T) are not supported yet.

## Demodulation and audio

The engine demodulates one VFO, which every interface shows: a click on the
spectrum, the wheel, the keyboard or a bookmark tunes it, and a change made
in one interface shows in the others, the last change winning. The Radio
menu sets the mode, the filter width and the squelch, and every mode
plays. NFM audio is de-emphasized, its CTCSS tone is removed and its band
stops at 3.5 kHz. AM audio follows the carrier's level: its loudness depends
on the modulation, not on the signal's strength. USB and LSB keep their own
sideband, and CW sounds a carrier on the VFO at 700 Hz, through a filter
centred on the VFO, 500 Hz wide by default. A slow AGC brings SSB and CW
signals to the same level: it takes the gain down as soon as a signal
arrives and lets it come back at 10 dB per second. AM, SSB and CW audio runs
from 100 Hz to 5 kHz.

Frequencies are exact to the hertz. They show in MHz with as many decimals
as they have, from 3 to 6 (145.500, 145.6125, 7.074001); measurements keep
their rounding: the signals found and the History's channels to 100 Hz, the
readout under the pointer to 10 Hz. A double-click on the frequency, or a
tap on a touch screen, opens it whole for typing: Enter or leaving the
field tunes there, unless it is unchanged, and Escape cancels. Every
frequency field (the VFO's, a bookmark's, the sweep's range, the centre
frequency) takes a point or a comma, and a unit in any case: Hz, k or kHz,
M or MHz, G or GHz; without one, a number below 100,000 is in MHz and
another in Hz, from 1 Hz to 100 GHz. A typed frequency beyond the
receiver's reach tunes nothing, and the status line says so. The wheel over
a digit of the frequency changes that digit, and so does dragging it up or
down with any pointer, by one for every 16 px; both move the span when the
frequency leaves it.

The tuning step, in the Radio menu, is Automatic or one of 1, 10, 50, 100
and 500 Hz, 1, 5, 6.25, 8.33 (25 kHz / 3, the airband's), 9, 10, 12.5, 25,
50 and 100 kHz, which applies whatever the zoom. Automatic is the channel
spacing where the band plan gives one, such as 12.5 kHz on the 2 m
repeaters or 9 kHz on medium wave, while the view shows two channels or
more; elsewhere it follows the view's width: 100 kHz from 200 MHz wide,
10 kHz from 20 MHz, 1 kHz from 2 MHz, 100 Hz from 200 kHz, 10 Hz from
20 kHz, and 1 Hz in a narrower view. The line under the frequency shows the
step in use. A
click, dragging the filter, the wheel, the arrow keys and the − and +
buttons land on the step's grid, and Shift with an arrow key steps ten
times as far; a typed frequency, a bookmark and a digit do not.

WFM plays broadcast FM in mono, through a filter 180 kHz wide by default,
from 50 to 250 kHz. Its audio has the European 50 µs de-emphasis and runs
from 40 Hz to 15 kHz, without the stereo pilot; it comes at 32 kHz, while
the other modes' audio comes at 12 kHz. The band plan gives FM broadcast
WFM, so tuning into 87.5 to 108 MHz selects it, with Radio > "Mode and step
from the band plan".

The squelch is automatic by default: it opens 8 dB above the noise around
the VFO, which the engine estimates from the spectrum, and closes 3 dB
lower. In WFM, whose station fills the spectrum around the VFO, the engine
reads the noise beyond the channel's edges, on the side that a neighbouring
station leaves quieter. Unticking "Automatic" sets the threshold by hand, starting from where
the automatic one stood. The S-meter shows the channel's level in dBFS and
the threshold, and its bar changes colour while the squelch is open. It
sets the threshold too: a notch of the wheel over it moves the threshold
1 dB, up raising it, and a click or a tap on its bar puts the threshold at
that point of its scale, from -104 dB at the left end to -10 dB at the
right, within the threshold's range of -130 to -20 dB. Either sets the
threshold by hand, the wheel starting from the automatic one in use. In
SSB, whose level follows the voice, the squelch closes between words: a
threshold set by hand below the noise keeps it open.

Each interface plays the audio itself, at its own volume, about a quarter of
a second after the antenna; at volume 0 it receives none. The desktop window
plays it as soon as it opens. A web browser may hold it until the first
click, tap or key press on the page, as Safari does on an iPhone each time
the page loads; the Radio menu then says so. On a touch screen, a speaker
at the top right shows whether the page plays sound: crossed out while the
browser holds the audio or the volume is 0, with waves while it plays. A
tap on it starts held audio, mutes playing audio by setting the volume to
0, or brings the volume back. On an iPhone, Safari keeps playing the audio
with the screen locked and with the ringer switch on silent. The Record
menu records the audio and the receiver's raw I/Q
([Recording](#recording)).

A faint λ beside the frequency opens the lengths of an antenna for it, in a
dialog that follows the VFO while it shows: the wavelength, a half-wave
dipole, end to end and for each arm, and a quarter-wave vertical, whose
radials take the same length. They are lengths to cut, 5% shorter than the
free-space wavelength gives (the classic 143/f metres for a dipole and
71.3/f for a quarter wave, f in MHz), to 3 significant digits: cut a little
longer, then trim. Listening needs no resonant antenna, though: below 30 MHz
a long wire often does as well.

## Spectrum view

The spectrum and the waterfall show the received span, or a part of it:
Ctrl + wheel, a pinch or the + and − keys zoom, and dragging the background
scrolls. While the view does not show the whole span, a small button at the
bottom right of the spectrum, two bars with an arrow between them, shows it
again, as the 0 key does. Settings lists every gesture and shortcut. The
readout at the window's bottom right gives the frequency and the level under
the pointer.

With a receiver that tunes, the view also goes past the span, out to ten
times its width; zooming out holds at the span's width before it goes on.
Nothing is measured outside the span, and the view shows no level there:
the span stands out like a lit window whose light fades away from it, the
grid goes on, blurred, and the band plan's colours tint the background.
Beyond the receiver's reach the background is hatched. The readout says
"outside the span" or "out of reach" there. The simulated band, which does
not tune, keeps the view within its span.

Clicking outside the span tunes there ([RTL-SDR receivers](#rtl-sdr-receivers)).
With a mouse, an outline first shows where the span would go, and a tip says
what the click does and whether it stops a scan. Until frames come from
there, a dashed outline marks the new span and the old span's spectrum
dims. Then a view that showed the whole span shows the new one; another view
stays where it is if it shows the VFO, and otherwise follows it at the same
zoom. When the VFO lies outside the view, a label at that edge gives its
frequency: clicking it brings the view back to the VFO, at the same zoom.

When the span moves by less than its width, as a step past its edge moves
it, the waterfall moves with it: each line stays at its own frequencies,
within the span only, so what leaves the span goes and the new part starts
empty. The peak hold moves the same way. A longer move, another sample
rate or FFT size, or another receiver starts them again.

## Scanners

The engine looks for signals in the spectrum it computes, whatever each
interface shows, and every interface lists the same ones: the Scan menu's
Signals tab gives each signal's frequency, how far it rises above the
noise around it and when it was last found. A signal is found when it rises
at least the threshold above that noise (12 dB by default, from 6 to 30 dB)
over a third of a second, twice in a row, away from the outer tenth of the
span on each side and from the 3 kHz on each side of the centre, where a
receiver's DC spike shows. The signals found within the last 1.5 s come
first, strongest first, then the others, latest first. The list keeps the
signals of the spans the receiver left, and empties when detection is
turned off or another receiver is chosen. In a channelized range of the
band plan, a signal shows on its channel.

A click on a frequency tunes to it. Ignore leaves its frequency out of the
list, for every interface, until Restore brings the ignored frequencies
back; the spectrum's context menu ignores a frequency too. Whether detection
is on, its threshold and the ignored frequencies, up to 200, are the same
for every interface, a paired device's included, and kept for the next
start. "Mark on the spectrum" is each interface's own: it marks the signals
found within the last 1.5 s on the spectrum.

The memory scan, at the foot of the Bookmarks menu, runs in the engine,
the same for every interface. It watches every ticked bookmark of the receiver's span at once,
in the part that detection searches. A bookmark is active when the power
in its mode's filter would open the squelch, with the squelch's settings:
8 dB above the noise around it with the automatic squelch, or beyond its
edges for a WFM bookmark. The scan tunes
the VFO to the active bookmark furthest above its noise, with the
bookmark's mode and that mode's usual filter width, and stays while the
squelch opens within a quarter of a second and stays open. Once the squelch
closes, it waits the resume time (2 s by default, from 0 to 8 s), then
watches every bookmark again.

With a receiver that tunes, such as an RTL-SDR, the memory scan also visits
the ticked bookmarks outside the span. They take as few windows of the
receiver as the scan finds, each bookmark in the part of a window that
detection searches, and it visits them in turn, from the window nearest
the receiver, the lowest after the highest. Once the receiver's samples
come from a window and the noise there is known, 0.33 s later, it watches
that window's bookmarks for 1 s, and for 1 s again after each one it stayed
on, then moves on; it also moves on when, 2 s after asking for a window,
the samples still come from elsewhere. At 2.4 MS/s, a window holds
1.92 MHz, nearly all of the 2 m band; the whole 70 cm band takes six. Each
visit leaves the bookmarks unwatched for about 0.75 s, the 0.4 s the
receiver takes to settle and the noise's 0.33 s, and a bookmark is watched
only while the scan is on its window: with N windows, a transmission
shorter than about 1.75 × N − 1 s may be missed, 2.5 s with two windows
and 6 s with four. While every ticked bookmark within the receiver's reach
lies in the span, the scan never tunes the receiver. The spectrum and the
waterfall show each window, and the VFO stays inside it, as with the
sweep. The list marks the ticked bookmarks beyond the receiver's reach,
which the scan leaves out; with a source that does not tune, such as the
simulated band, it watches the span's alone, and the list marks the
others as outside the span.

Any interface starts and stops the memory scan, a paired device's included,
and every interface shows where it stands, with the window it is at; their
status lines add "memory scan" while it runs. It goes on with no interface
open, keeping the receiver open, until an interface stops it. A stop brings
the receiver and the VFO back where they were when the scan started, once
it moved the receiver, unless it is on a bookmark, where they stay; so do
no ticked bookmark left within reach and a receiver that refuses a window,
which stop it too. Tuning by hand from any interface (the VFO, the centre
frequency or the sample rate) or choosing another receiver stops it,
leaving them where they are. While it runs, the receiver's settings saved
are those of its start. Whether it runs is never saved: none runs at start,
unless `--scan memory` starts it ([Without a window](#without-a-window)).
The resume time is the same for every interface and kept for the next
start.

The Sweep tab's sweep runs in the engine too, the same for every interface,
with a receiver that tunes, such as an RTL-SDR: not with the simulated band.
It steps the receiver across a range, 430 to 440 MHz by default, window
after window, pass after pass, and detection searches each window as it
does the span: the signals found there join the Signals list and the History.
The windows lie a quarter of the sample rate apart at most, the first and
the last a tenth of the rate inside the range, so that every frequency of
the range lies in the part that detection searches of a window, even one at
the centre of another window, where the DC spike shows. Once the
receiver's samples come from a window, the sweep stays there the time per
window (0.5 s by default, from 0.25 to 2 s), long enough for detection to
find a signal there twice in a row, then asks for the next one; it also
moves on when, 2 s after asking for a window, the samples still come from
elsewhere. At 2.4 MS/s, a pass of the 17 windows of 430 to 440 MHz takes
about 16 s, the receiver taking 0.4 s to settle at each. The spectrum and
the waterfall show each window, and the VFO stays inside it; the waterfall
keeps the lines of the windows before it at their frequencies, where they
overlap it ([Spectrum view](#spectrum-view)).

Any interface starts and stops the sweep, a paired device's included, and
every interface shows the pass and the window it is at; their status lines
add "sweep" while it runs. One scan runs at a time: starting the sweep
stops the memory scan, and starting the memory scan stops the sweep. The
sweep starts only when the receiver tunes to every window of the range. It
goes on with no interface open until an interface stops it, which brings
the receiver back to its centre frequency and the VFO where they were when
it started, as does a receiver that refuses a window. Tuning by hand from
any interface (the VFO, the centre frequency or the sample rate) or
choosing another receiver stops it, leaving the receiver where it is.
While it runs, the receiver's settings saved are those it comes back to,
and the memory scan's windows are those it would take from that span,
where a memory scan started then begins. A new range starts a running
sweep again from its first window. Whether it runs is never saved: none
runs at start, unless `--scan sweep` starts it. The range and the time per
window are the same for every interface and kept for the next start.

The engine keeps the channels where it found signals for 90 days: for each
one, when it was first and last heard, how many transmissions, how long it
was active, how far it rose above the noise at most and how many
transmissions began in each hour of the day, in the local time of the
computer that runs Radiolack. A signal found within 1.5 kHz of a channel,
or within a quarter of its own width, counts for that channel, and a pause
longer than 1.5 s begins a new transmission. It keeps at most 2000
channels, the least recently heard leaving first. Neither the simulated
band nor the ignored frequencies are recorded, and nothing else is kept: no
audio and no samples.

The History tab lists these channels, the latest heard first, and asks the
engine for them again every 30 s while it shows. A click on a frequency
tunes to it; its tooltip gives when it was first and last heard, its
highest level and its busiest hour. Bookmark adds a bookmark there, with
the band plan's mode. Clear forgets one channel and Clear all forgets them
all, for every interface, a paired device's included: each waits for a
second press within 5 s. The other interfaces show the change when they
next ask.

## Recording

The Record menu records the audio that the VFO plays, and the receiver's
raw I/Q: Record audio and Record I/Q each start a recording and Stop ends
it, from any interface, a paired device's included. Both may run at once,
and every interface shows the recordings that run, with a red dot on its
menu's icon and in its status line. A recording goes on without any interface,
like a scan, until an interface stops it. It never starts at start, and
whether one runs is not saved. The simulated band is not recorded: choosing
it ends an audio recording, which another receiver does not, while another
receiver, the simulated band included, ends an I/Q recording.

Each audio recording is a WAV file, 16-bit mono at the audio rate: 12 kHz, about
82 MiB per hour, or 32 kHz in WFM, about 220 MiB per hour. A new audio
rate, into or out of WFM, ends the recording; a retune, another mode at the
same rate or a squelch that closes does not, and the squelch's silences are
recorded. A file is named after its start in UTC, the VFO's frequency and
its mode, such as `2026-11-14_15-30-45Z_145.500000MHz_NFM.wav`, with `-2`,
`-3` and so on when the name is taken. Its header follows the audio, so that
a crash leaves a file that plays. A recording stopped before any audio came
leaves no file.

An I/Q recording keeps the samples of an RTL-SDR, the only receiver that
gives them, as rtl_tcp sends them, before Radiolack removes their DC offset,
in [SigMF](https://sigmf.org) files: Inspectrum and SDRangel open them, and
GNU Radio's File Source reads their data as bytes. The `.sigmf-data` file
holds the 8-bit unsigned I/Q pairs (`cu8`, I first), about 17 GB an hour at
2.4 MS/s, and the `.sigmf-meta` file beside it gives the sample rate, the
receiver and the captures: where each starts in the data, its centre
frequency and its start in UTC. A retune or a new frequency correction
starts a new capture, as does a lost rtl_tcp that comes back; the samples
that come while the receiver settles, for 0.4 s after a change, are left
out, as from the spectrum. A new sample rate, another receiver or another
rtl_tcp address ends the recording. Its files are named after its start in
UTC, the receiver's centre frequency and the sample rate, such as
`2026-11-14_15-30-45Z_145.000000MHz_2.400000MSps.sigmf-data`, with `-2`,
`-3` and so on when a name is taken. The metadata follows the captures
within 5 s, and at the end, so that a crash leaves files that read.

The recordings go to `~/.local/share/radiolack/recordings`
(`$XDG_DATA_HOME/radiolack/recordings` when that variable is set), which
Radiolack makes when needed; the folder and the files are readable by the
user only. Record > Folder, on this computer only, chooses another
folder, which must exist and which Radiolack must be able to write into;
`~` stands for the home folder. The folder changes only while nothing
records, and the recordings made before stay where they are: the list, the
downloads and the deletions follow the new folder. An empty field, or
Default folder, comes back to the default one.

The menu lists the folder's latest 500 recordings, the newest first, with
their start, length and size, an I/Q recording's sample rate too, and the
space left on its disk; it asks the engine for them again every 30 s while
it shows. Download fetches a recording from the computer that records, as
Data and Metadata fetch an I/Q recording's two files: the desktop window
saves it in the Downloads folder (`~/Downloads`, or the folder that
xdg-user-dirs names in the system's language) without asking, adding `(2)`,
`(3)` and so on to a name already there, and the menu says where it went;
a browser, a paired device's included, saves it as it saves any download.
Delete removes a recording, an I/Q recording's two files together, for every
interface, once pressed again within 5 s. Download, Data, Metadata and
Delete take only the files that the list shows, and none of a recording
that runs.

A recording stops by itself when less than 2 GiB stays free on its disk,
an audio recording when its file reaches the WAV format's limit of 4 GiB
(about 49 hours, or 18 in WFM), and either on a disk error, such as a disk
too slow for it: an I/Q recording writes about 4.8 MB/s at 2.4 MS/s. The
menu and the status line then say why, as does Radiolack's output.

**Law.** Local law may forbid recording what some radio services carry,
such as air traffic, emergency services or private networks, or disclosing
it: check the rules where you listen before you record.

## Settings and bookmarks

Radiolack keeps its settings and bookmarks in `~/.config/radiolack`
(`$XDG_CONFIG_HOME/radiolack` when that variable is set):

- `settings.json` holds each receiver's settings: the Receiver menu's and the
  demodulation settings (VFO, mode, filter width, squelch). The simulated
  band and each receiver plugged into this computer, by index or serial
  number, have their own; rtl_tcp has one set whatever its address, which is
  saved with them. It also records the source in use, for the next start,
  and the scanners' settings: whether detection is on, its threshold, the
  ignored frequencies, the memory scan's resume time, and the sweep's range
  and time per window ([Scanners](#scanners)). It also holds the own
  settings of this computer's interfaces: the display, waterfall and
  overlay settings, the volume, the tuning step, the band plan's choice of
  mode and whether the spectrum marks the signals found. Last, it records whether Radiolack checks for updates at start
  ([Updates](#updates)) and the recordings folder chosen in Settings
  ([Recording](#recording)).
- `bookmarks.json` holds the bookmarks.
- `observations.json` holds the channels of the History
  ([Scanners](#scanners)). It reaches the disk at most every 10 minutes, a
  few seconds after a Clear, and when Radiolack stops.
- `remote.json` holds what remote access keeps: its port, its access code,
  the paired devices, each with its own interface settings, and its local
  authority, but never whether it is on: it is off at each start, unless
  `--remote` turns it on ([Remote access](#remote-access)).

The window keeps no browser data on disk: WebKit's cache and storage stay in
memory.

Every interface shows the same bookmarks, receiver settings and signals
found. An
interface on this computer, the window or a browser, opens with the own
settings of this computer's interface that changed one last, and a paired
device with its own ([Remote access](#remote-access)); changing them in one
interface leaves the other open ones as they are. A change reaches the disk
a few seconds later at most, and when Radiolack stops.

The Bookmarks menu lists the bookmarks, which its filter narrows down. A
click on one tunes to it with its mode, its box ticks it for the memory
scan, which the menu starts at its foot ([Scanners](#scanners)), and Edit
opens its name and its frequency, to change, or removes it: Enter or
leaving the row keeps the changes, and Escape cancels them. A frequency
that is not one, or lies outside 1 Hz to 100 GHz, keeps the row open with a
note. "Add current frequency" adds one at the VFO, in its mode, and opens
its name selected, ready to type over: Enter or leaving the row keeps the
new name, and Escape the one it was given, Bookmark 1 or the next free
number. The spectrum's context menu adds one anywhere.

Settings > Storage says where the files are and what went wrong with them;
the status line adds "changes not saved" while some changes do not reach
the disk. Each file is written whole, through a temporary file, so that a
crash or a power cut leaves the previous version. A file that Radiolack
cannot read as its own, damaged or edited by hand with a mistake, is renamed
`settings.json.invalid-<date>-<time>` (or `bookmarks.json…`, and so on) and Radiolack
starts without it. A file that cannot be opened, or that a newer Radiolack
wrote, is left as it is and not saved until the next start. To start again
from the defaults, delete the folder while Radiolack is stopped.

## Remote access

Remote access lets a phone, a tablet or another computer on the same local
network control Radiolack and hear it in its web browser. It is off at each
start, and only this computer turns it on: its interface, in Settings >
Remote access, or the `--remote` option at start
([Without a window](#without-a-window)). Radiolack then also listens over
HTTPS, on port 8443 or the port chosen in Settings while remote access is
off or given to `--remote` (1024 to 65535), on this computer's addresses
on its local networks: the private IPv4 ranges (10.0.0.0/8, 172.16.0.0/12,
192.168.0.0/16), the range that Tailscale uses (100.64.0.0/10) and
link-local addresses. It answers only devices whose own address is in these
ranges, and only requests addressed to this computer's name or one of these
addresses, which keeps web pages from reaching it through DNS rebinding.
Never make its port reachable from the Internet. Radiolack never changes the
firewall: when this computer runs one, let the local network reach the port,
for instance `sudo ufw allow from 192.168.1.0/24 to any port 8443 proto tcp`
with ufw.

Settings shows the addresses to open: first `https://ADDRESS:8443/` with
this computer's private addresses, home networks' ranges first, which work
wherever the device reaches them; then `https://NAME.local:8443/` with its
host name, which works on networks where mDNS resolves such names (Avahi on
Linux; iOS, macOS, Android and Windows resolve them), unless this computer
also has an IPv6 address: the name then leads there too, where remote access
does not listen, and an iPhone opens it slowly or not at all.

Remote access follows this computer's addresses while it is on: within
about ten seconds of a change, such as a new address from the router or a
VPN that comes up, it listens on the new ones, and Settings shows them.
Devices on an address that remains reconnect by themselves; on a new
address, a device enters the code again. While this computer has no address
on a local network, remote access stays on and listens nowhere, as Settings
says, until it has one. It also listens on any private network that this
computer joins while it is on, such as a hotel's Wi-Fi: turn it off before
joining a network you do not trust. On each device, once:

1. Open the address. The browser warns that the connection is not private:
   Radiolack's certificate comes from Radiolack's own local authority, which
   the device does not know yet. Go on to the page.
2. Download Radiolack's certificate from that page and install it as a
   trusted authority, as the page explains for iPhone and iPad, Android and
   other devices. Before trusting it, check that its SHA-256 fingerprint,
   in the certificate's details on the device, is the one that Settings >
   Remote access shows. The browser then trusts Radiolack's pages without
   warning.
3. Enter the access code that Settings shows. The device stays paired: it
   then opens the interface at that address directly.

On an iPhone or iPad, Safari's Share > Add to Home Screen then puts
Radiolack's icon on the Home Screen.

The local authority vouches only for private addresses and for names ending
in `.local`, `.home.arpa` or `.internal`: name constraints, in the
certificate that signs Radiolack's, keep it from vouching for any other
website, and the root's key was destroyed as soon as it signed that
certificate. Radiolack gives itself a new certificate, for this computer's
current addresses, each time remote access turns on and each time they
change.

A paired device gets the interface, the spectrum and the audio, and shares
the receiver with every other interface: its changes to the VFO, the mode,
the receiver's settings, the source, the bookmarks and the scanners reach
everyone, and it may clear the History. A memory scan or a sweep started
from a phone goes on with the phone locked ([Scanners](#scanners)). It
cannot change what belongs to this computer, remote access itself, the update
check and the rtl_tcp server's address, and does not see where the settings
are saved.

A paired device keeps interface settings of its own, such as the display,
the palette and the volume: it starts from this computer's, follows them
until it changes one, then opens with its own each time, even when the
browser reloads the page, as Safari on an iPhone often does. Its changes
leave this computer's settings and the other devices' as they are. A device
that opens Radiolack by two addresses, at home and through a VPN for
instance, is paired once at each, with settings of its own at each.

A device's link may be slower than this computer's own, so a device gets a
lighter spectrum: about 15 frames a second instead of 30, in at most 2048
bins, each showing the strongest of the bins it groups, so that narrow
signals stay visible; its waterfall keeps its speed. The stream is also
compressed, as every current browser offers. With the audio, a device
receives about 0.3 Mbit/s instead of about 2.2 Mbit/s; in WFM, about
0.65 Mbit/s, as its 32 kHz audio takes 0.5 Mbit/s. Its finest zoom is
coarser: 18.75 kHz wide at 2.4 MS/s, instead of 4.7 kHz. On a slower link,
the spectrum comes less often and the sound arrives late, by about 5 s at
0.25 Mbit/s, but no later: Radiolack drops what the link cannot carry.

Settings > Remote access lists the paired devices, says which are connected
and when each was last used, and forgets one on request; a new code forgets
them all. A forgotten device must enter the code again, and so must a device
unused for 90 days; Radiolack forgets its settings with it, and it starts
again from this computer's. A wrong code is answered after a second, and
after 10 wrong codes the code no longer works until Settings makes a new
one.
Turning remote access off closes the devices' connections; their interfaces
wait for it to be on again. A change of this computer's addresses closes
them too, and the interfaces reconnect by themselves. Radiolack keeps a hash
of each device's session, never the session itself, in `remote.json`, which
only you can read.

### Without a window

On a computer without a display, such as one next to the receiver,
`--remote` turns remote access on at start, as Settings does:

```bash
radiolack --ui none --remote
```

Radiolack then prints what Settings would show: the addresses to open, the
SHA-256 fingerprint of its certificate and the access code. The code also
lands wherever the output goes, a service's journal for instance, which this
computer's administrators can read. `--remote PORT` listens on PORT, which
remote access keeps for the next starts. When remote access cannot turn on,
on a port in use or without an address on a local network yet, Radiolack
says why and exits with status 1, so that a service manager can start it
again, as systemd's `Restart=on-failure` does. Away from home, devices reach
it through a VPN that gives this computer an address in the ranges above,
such as WireGuard on a private range or Tailscale. Once on, remote access
follows this computer's addresses: within about ten seconds, it also listens
on a VPN that comes up after the start. The output then says only when
remote access listens nowhere, or cannot listen on new addresses; Settings
shows the addresses.

Settings stays on this computer's own interface, on 127.0.0.1: to turn
remote access off, forget a device or make a new code, reach it through SSH
with the same port on both ends, for instance after
`radiolack --ui none --remote --port 8080`:

```bash
ssh -L 8080:127.0.0.1:8080 COMPUTER
```

then open `http://127.0.0.1:8080/` on the computer that runs ssh.

`--scan memory` or `--scan sweep` starts that scan at each start, with its
saved settings, as an interface starts it, so that after a power
cut or an upgrade the computer goes on scanning, and the History fills,
without any interface:

```bash
radiolack --ui none --rtl-sdr --scan memory
```

The receiver then opens at start, not once an interface opens. When the
scan cannot start, as the memory scan without a ticked bookmark within the
receiver's reach or the sweep with a source that does not tune, Radiolack
says why in its output and in the menu that starts that scan, and runs on
without it: an interface can start it later. A receiver's range is known once it
answers: the output also says when the memory scan then stops because no
ticked bookmark lies within that range, and when a scan stops because the
receiver refused a window. Without `--scan`, no scan runs at start.

### As a service

On a computer without a display, next to the receiver, the Debian package's
systemd service runs Radiolack from boot, with remote access on. The package
installs it and never enables or starts it: enabling it is the
administrator's choice, as giving `--remote` at start is.

```bash
sudo systemctl enable --now radiolack
```

It runs `radiolack --ui none --rtl-sdr --remote --port 8080`: the receiver
plugged into that computer, remote access on the port it used last (8443
the first time), and that computer's own interface on 127.0.0.1:8080, which
the SSH tunnel above reaches, with Settings, the update notice and the full
spectrum. The addresses to open, the certificate's fingerprint and the
access code go to the system's journal, which root and the members of the
adm and systemd-journal groups read:

```bash
sudo journalctl -u radiolack
```

The service starts once the network is up, and again 10 s after a failure,
such as remote access without an address on a local network yet. The
package restarts it after an upgrade when it runs, and stops it before its
removal; `sudo systemctl disable --now radiolack` stops it and keeps it from
starting at boot. `sudo systemctl edit radiolack` changes its command, for
instance to start the memory scan at each start or to take another local
port, with an empty `ExecStart=` before the new one:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/radiolack --ui none --rtl-sdr --remote --port 8080 --scan memory
```

It runs as a user of its own, which systemd makes at each start
(`DynamicUser`), in the plugdev group that the receivers' device rules let
in, within systemd's sandbox: without any privilege, the users' homes
hidden, the system's files read-only, a temporary folder of its own, and
only the devices, address families and system calls that Radiolack and
rtl_tcp need, so that a flaw in remote access reaches neither a user's
account nor the system; `systemd-analyze security radiolack` details it.
Its settings, bookmarks and History live in its state directory,
`/var/lib/radiolack`, which only the service and root read, and its
recordings in `/var/lib/radiolack/recordings`; `sudo apt purge radiolack`
removes them. The sandbox lets the service write nowhere else: to record
into another folder, such as a disk mounted on `/media/radio`, which I/Q
recordings, about 17 GB an hour, may well need, make it
writable by the plugdev group, open it to the service with
`sudo systemctl edit radiolack`, restart the service, then choose the folder
in Record > Folder, through the SSH tunnel above. The files stay
readable by the service only.

```ini
[Service]
ReadWritePaths=/media/radio
```

Its interface on 127.0.0.1:8080 answers every account and program on that
computer, as Radiolack's interface does on any computer: give that
computer's accounts only to those who may use the receiver. A receiver
serves one program at a time: an rtl_tcp service or another Radiolack on
the same receiver must stay stopped, or the service's interfaces wait,
saying that another program uses the receiver.

## Updates

Installed from its Debian package, Radiolack checks at each start whether
its APT repository has a newer version, and then says so at the top of the
waterfall until you close the notice: the system's software updates, or
`sudo apt update && sudo apt upgrade`, install it, and it runs once
Radiolack restarts. Until then, Radiolack keeps serving the interface it
started with, so that a page reloaded meanwhile, in its window or on a
remote device, still matches it. About > Updates, on this computer, shows
what the latest check found, checks again on request, and turns the check
at start off, a choice saved with the settings. About shows the version
that runs on every device, remote ones included.

The check needs no password and installs nothing. It runs apt as you, with
Radiolack's APT source alone and none of the system's apt configuration, in
a temporary folder that it then removes: apt downloads the repository's
signed index from GitHub Pages, checks it against the repository's key, and
compares the latest version with the installed package's. The check is the
only connection Radiolack makes besides the receiver and its interfaces,
and it carries no identifier: GitHub Pages sees your address and apt's
version, as for any download. apt's own proxy settings do not apply to it,
but the `https_proxy` environment variable does. A check that gets no
answer within 30 seconds fails, and About > Updates says why; the notice
shows only for a newer version.

A Radiolack that does not run from the package (from the repository or
pip), or whose APT source is missing or turned off, does not check, and
About > Updates says why. On Debian 12, whose apt checks signatures
through apt-key, each check leaves an empty folder in `/run/user/UID/gnupg`,
which the end of your session removes.

## Problem reports

When Radiolack misbehaves, About > Report a problem, on this computer,
prepares a report of what it runs on: its version, and whether it came from
the Debian package; Python's, aiohttp's and NumPy's; the system and its
kernel; GTK's and WebKitGTK's in the desktop window; the web browser, the
views' WebGL2 and the window's size; the receiver's kind and state, with its
tuner and sample rate but neither its address nor its frequency; how long
Radiolack has run; and its latest 50 warnings and errors, in UTC, with your
home folder written `~`. Radiolack sends nothing by itself. Read the report
first, as its warnings may name files, folders, frequencies or addresses.
Open a GitHub issue then shows GitHub's page for a new issue in your web
browser, with the report as its description, where you say what happened
above it and submit it with your GitHub account; Copy puts it on the
clipboard instead, for an email for instance. When the warnings would make
the page's address too long for GitHub, the issue leaves out the oldest
ones; a copy keeps them all. Radiolack keeps its warnings and errors in
memory only, from its start, and still writes them on its error output.

For an idea or a question, About > Suggest an idea shows GitHub's page for
a new issue, and Radiolack on GitHub the project's page, on every device.
Both open in your web browser, where posting needs a GitHub account. The
desktop window opens Radiolack's pages on GitHub there, and no other page.

## Band plan

The strip under the spectrum shows what each range is used for, from 9 kHz to
6.425 GHz: broadcasting, aviation, shipping, mobile networks, satellites,
short-range devices, amateur bands and more. Inside the amateur bands it
follows the IARU Region 1 band plans, simplified. Hovering the strip names a
range and its edges, and clicking it zooms to the range. Zoomed out, where a
band's ranges would be too narrow to read, the band shows as one block of
its colour, named in full, by a short name or not at all as room allows,
and clicking it zooms to the band. With Radio > "Mode
and step from the band plan", tuning into a range selects its usual mode.
The Automatic tuning step takes the channel spacing of the ranges that have
channels ([Demodulation and audio](#demodulation-and-audio)). The Bookmarks menu lists the
ranges of the received span. Display > "Band plan" hides the strip.

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
they use a temporary settings folder, recordings folder and GnuPG home,
never yours; they record only the fake rtl_tcp and synthetic signals. Where
dpkg-deb is installed, they also build the Debian package twice in a
temporary folder, compare the builds and run the package's command, check
its service with systemd-analyze where it is installed, and run its
maintainer scripts on a copy of its files, with stand-ins for the commands
that would act on your system; where
git is, they make releases in temporary repositories, never in yours; where
GnuPG is too, they build APT repositories, sign them with throwaway keys and
read them with apt-get, run as you in a folder of its own, away from your
system's apt, check them for updates as Radiolack does, and publish
releases in them as the owner does, with a stand-in for GitHub's
downloads. They also check the APT repository's key
that the package carries: the one that [APT repository](#apt-repository)
gives, whose signing subkey has more than a year left, so that they fail a
year before it expires.

The interface's JavaScript carries JSDoc types, which TypeScript checks
without producing any file. The check needs Node.js and downloads TypeScript
on its first run:

```bash
npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json
```

The browser tests drive the interface in headless Chromium, Firefox and
WebKit through Playwright, against the simulated band, the fake rtl_tcp
program and rtl_tcp addresses where nothing listens: the stream, the menu,
its keys and its layout from a phone to a computer, the Receiver menu and
its list, a change made in one interface showing in another, the bookmarks,
their names and a click that tunes, the S-meter's squelch, the signals
found, the memory scan, in the span and
across windows, the sweep, the History, the recordings, their downloads and
their folder, the view past the span and tuning outside it, and the saved
interface settings. They
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

`RADIOLACK_BROWSERS` keeps them to some of the browsers, separated by
commas, such as `RADIOLACK_BROWSERS=firefox` or `chromium,webkit`: CI runs
one job per browser.

On a computer without a display or a GPU, such as a server, run the last
command under `xvfb-run -a` (Debian and Ubuntu: `sudo apt install xvfb`):
headless Firefox then needs a display to draw with WebGL2.

Chromium draws WebGL2 through the system's EGL: on the GPU where there is
one, otherwise with Mesa's software renderer, llvmpipe (Debian and Ubuntu:
`sudo apt install libegl1 libegl-mesa0 libgl1-mesa-dri`), which draws the
interface about three times as fast as Chromium's own, SwiftShader. Without
them, Chromium falls back to SwiftShader. The tests' log gives each
browser's WebGL2 renderer as a page reads it, as their durations depend on
it (in WebKit, a page always reads Apple GPU).

Firefox plays audio only through a sound server, PipeWire or PulseAudio:
without one, as on a server, its audio test fails. PulseAudio with only a
null sink gives it a silent output (Debian and Ubuntu:
`sudo apt install pulseaudio`), and `pulseaudio --kill` stops it:

```bash
pulseaudio --start --exit-idle-time=-1 -n --load=module-native-protocol-unix --load=module-null-sink
```

`packaging/smoke_test.py` checks a Radiolack from the outside: it starts it
on the simulated band, in settings and data folders of its own and on a free port,
then checks that it listens on 127.0.0.1 only, refuses requests addressed to
other hosts and streams from other origins, serves the interface with its
Content-Security-Policy and every file the interface loads, streams the
spectrum, ends its update check without failing, starts with remote access
off, and stops on SIGTERM. After installing the Debian package, it checks
the installed `radiolack`; with `--check-updates`, its update check must
also reach the APT repository, which needs the network:

```bash
python3 packaging/smoke_test.py --check-updates
```

With `--remote`, it also turns remote access on for a few seconds, on an
ephemeral port of this computer's local network addresses, and checks it as
a phone would: the certificate, the pairing page, a wrong code and the right
one, the stream, and the port released once off. It then starts Radiolack
again with `--remote`, and checks that remote access is on at the addresses
printed, with the certificate whose fingerprint is printed, and that the
code printed pairs a device.

### Continuous integration

GitHub Actions runs these workflows (`.github/workflows/`), without secrets,
services or radio hardware:

- Tests: the tests above, against Radiolack installed with pip, on Python
  3.11 and 3.15, for every push to `main` and every pull request.
- Interface: the type check and the browser tests, one job per browser,
  under Xvfb, with that silent output and Mesa's EGL, when the package, the
  tests or `pyproject.toml` change.
- Package: builds the Debian package twice and compares the builds, and
  puts it in an APT repository signed with a key made for the run; installs
  it with apt from that repository on Debian 12 and 13 and Ubuntu 24.04 and
  26.04, checks that apt reads the source that it adds, runs it there with
  `packaging/smoke_test.py`, whose update check reads the published APT
  repository with the source and key that the package adds, and which pairs
  a device with remote access on the container's network, checks that it
  installs its service without enabling it, removes it, which keeps the
  source and its key and the service's enabling and state,
  then purges it, which removes them, and checks it with lintian; when the
  package or its packaging change.
- Workflows: actionlint and zizmor check the workflows themselves when they
  change.
- Release: publishes each release (see
  [Versions and releases](#versions-and-releases)).

Each action is pinned to a commit, and Dependabot proposes updates once a
week, for versions out for a week or more (`.github/dependabot.yml`). The
workflows' token can only read the repository, except in the job that
publishes a release.

## Versions and releases

Versions have three numbers, X.Y.Z: the middle one grows with new features,
the last one with fixes. `src/radiolack/__init__.py` holds the version that
pip, `radiolack --version` and the Debian package read. Each release is an
annotated tag `vX.Y.Z` on `main`. Between two releases, `main` holds the
next version as a development version, such as 0.2.1.dev0 after 0.2.0: a
package built from it installs over the release, and the next release
installs over it.

To make a release, on a `main` without changes and at the commit of
`origin/main`:

```bash
python3 packaging/release.py 0.2.1
```

It commits the version, tags that commit `v0.2.1`, then commits the next
development version. It pushes nothing, and prints the command that
publishes the two commits and the tag together:

```bash
git push --atomic origin main v0.2.1
```

On GitHub, the tag starts the Release workflow. It checks that the tag is
annotated, is on `main` and names the version its commit holds, then runs
the Tests, Interface and Package workflows on that commit, without any
cache. Once they pass, its last job, in the repository's `release`
environment, publishes the package that the Package workflow built and
checked, with its checksum, on the tag's release page, and, while the
repository is public, an attestation of its build. It never replaces a
release that exists. The package's Maintainer field is the repository's
`DEB_MAINTAINER` variable when set, else the owner's GitHub no-reply
address.

`v0.1.0` marks the previous Radiolack, the airband explorer, and `v0.2.0`
the rewrite's first release.

### APT repository

Radiolack's APT repository, which GitHub Pages serves, carries the releases,
so that apt installs each new one with the system's other updates. The
package adds it, as two configuration files: its source,
`/etc/apt/sources.list.d/radiolack.sources`, and its key,
`/etc/apt/keyrings/radiolack-archive-keyring.gpg`, against which apt checks
every index and package. The source names the repository's only
architecture, `all`, as the package is architecture-independent: without
it, as in the source of Radiolack 0.3.0 and 0.4.0, `apt update` notes each
time that the repository has no index for the computer's architectures.
An upgrade replaces that source, unless you changed it: dpkg then asks
which one to keep. To stop using the repository, change `Enabled: yes`
to `Enabled: no` in the source: Radiolack then stops checking for updates
too. `sudo apt remove radiolack` keeps both
files, so that the source still checks out, and `sudo apt purge radiolack`
removes them. The key is the owner's:

```text
FD76 A620 CA60 3AD5 9DE3  62B1 6D45 6D30 0682 367A
```

Its primary key stays offline and only certifies; a subkey signs the
repository. apt refuses a signature by an expired key: a year before the
signing subkey expires, the tests fail and the command below warns. The
owner then extends its expiry with the primary key, puts the new public key
in `packaging/radiolack-archive-keyring.gpg`, and publishes a release with
it, which installations receive while the old expiry still holds.

The owner publishes each release in the repository once the Release
workflow has published it on GitHub, from a computer that holds the signing
subkey:

```bash
python3 packaging/publish_apt.py 0.3.0
```

The command fetches the repository from the `gh-pages` branch and checks its
signature with the package's key; downloads the release's package and
`SHA256SUMS`, and checks one against the other; builds the package again
from the release's tag and checks that both are identical, byte for byte,
so that the key signs only what the tagged source makes; adds the package
to the repository, GnuPG asking for the key's passphrase; and checks the new
signatures with the package's key. It then shows the commit it made on
`gh-pages` and pushes it once the owner agrees: nothing reaches GitHub
before. GitHub Pages publishes the branch as it is (Settings > Pages: deploy
from the `gh-pages` branch, at its root). The command needs Python, git,
dpkg-deb and GnuPG, and a dpkg-deb that builds the same package as the
Release workflow's, as Debian 12's does.

`packaging/apt_repository.py` builds the repository. It adds the packages
it is given to the repository's pool, writes the index of every package
there and signs it with the owner's GnuPG key, which asks for the key's
passphrase as usual:

```bash
python3 packaging/apt_repository.py --key FINGERPRINT REPOSITORY radiolack_0.2.1_all.deb
```

The repository has one suite, `stable`, and one component, `main`, with
architecture-independent packages only. Before it signs, the tool checks
that the repository's index carries the key's signature and lists exactly
the packages in the pool: the key signs only what it signed before, and the
packages it is given. A published version never changes. Without packages,
the command signs the repository again, as it must with a new signing
subkey before the old one expires. It needs Python, dpkg-deb and GnuPG (gpg
and gpgv).

## Logo

Radiolack's logo is the Peak, a signal's trace that rises from a flat floor
to a sharp peak, and the name in Saira Italic (Copyright 2020 The Saira
Project Authors, SIL Open Font License 1.1), drawn as outlines: no font
ships with Radiolack or loads in its interface. The icons show the Peak in
the interface's blue on a dark tile: the favicon, the Home Screen icon of
an iPhone or iPad, the menu entry's and the window's icon, and the social
preview. Beside the name, the logo takes the text's colour: on the pairing
page and at the top of this README, in GitHub's light or dark theme. The
interface names Radiolack in its window's title alone, which leaves the
frequency its room on a narrow screen.

`packaging/identity.py` draws them all from the font, which stays out of
the repository: its docstring names the file to download and the
programs it needs (fontTools, Inkscape and ImageMagick).
`tests/test_identity.py` checks that the copies agree. GitHub shows
`docs/identity/social-preview.png` where the repository's link is shared,
once the repository's owner uploads it in Settings > General > Social
preview.

## Documentation

- [docs/design/scoping.md](docs/design/scoping.md): purpose, decisions,
  architecture, milestones, spikes and open questions.
- [docs/design/coverage-and-precision.md](docs/design/coverage-and-precision.md):
  the design of the next milestone, shortwave, tuning to the hertz and the
  band plan from 0 Hz to 10.5 GHz, with its limits.
- [docs/design/later-milestones.md](docs/design/later-milestones.md): the
  milestones after it, up to the public release.
- [docs/design/protocol.md](docs/design/protocol.md): the stream between the
  engine and the interface.
- [docs/design/band-plan.md](docs/design/band-plan.md): the band plan's
  scope, sources and file format.
- [docs/design/ui-mockup.html](docs/design/ui-mockup.html): the validated
  visual reference, with simulated data; it opens directly in a browser.
  The menu has since replaced its toolbar and panels.
- [docs/design/lessons.md](docs/design/lessons.md): what the rewrite reuses
  from the airband explorer.

## Previous version

The first milestone replaced the previous Radiolack, a Gqrx-driven explorer
of the VHF airband, on `main`. Its code stays in the history under the tag
`v0.1.0` (`git checkout v0.1.0`), and its personal data in `data/` stays
untouched (see [data/README.md](data/README.md)).

## Project layout

```text
src/radiolack/
  cli.py        command line
  server.py     local web server (aiohttp): interface files, the stream and
                the recordings' downloads, and remote access's HTTPS
                listener
  engine.py     reads a source, removes its DC offset, shares spectrum
                frames, the demodulation settings, the bookmarks, the
                signals found, the recording state and the update check's
                findings with the interfaces, sends audio to those that
                ask, records it and the receiver's raw I/Q, and saves the
                settings and the observations
  recordings.py the audio's WAV files and the raw I/Q's SigMF files, each
                recording written by a thread of its own, their folder and
                its list
  scanners.py   detection: the signals in the receiver's span, their list
                and the ignored frequencies; the memory scan, its windows
                and its visits; the sweep's windows and progress
  observations.py
                the channels where signals were found, for the History
  remote.py     remote access: access code, paired devices and their
                interface settings, local authority and certificates,
                addresses to listen on
  store.py      settings, bookmarks, observations and remote access files
  bookmarks.py  bookmarks and their checks
  preferences.py
                the interfaces' own settings and their checks
  demod.py      one VFO: channel level, squelch and the audio of each mode
  dsp.py        FIR filter design and FFT filters that shift and resample
  rtltcp.py     RTL-SDR receivers through rtl_tcp, run by Radiolack or by you,
                and their raw samples for an I/Q recording
  sources.py    opens each kind of source, for the Receiver list and at start
  spectrum.py   display FFT, and the noise level around the VFO
  synthetic.py  simulated 2 m band
  updates.py    update check: asks the APT repository for a newer version
  report.py     problem reports: the latest warnings and errors, kept in
                memory, and GitHub's page for a new issue
  window.py     desktop window (GTK 3 and WebKitGTK), which saves downloads
                and opens GitHub's page for a new issue in the web browser
  web/          interface: ES modules served as they are, no build step
tests/          tests without hardware
docs/design/    design documents
docs/identity/  the logo for this README and the social preview
packaging/      Debian package (build script, manual page, menu entry,
                icon, systemd service, licence notice, APT source and
                key), its smoke test, the release script, the APT
                repository's tools and the logo's generator
.github/        GitHub Actions workflows, their settings and Dependabot's
jsconfig.json   settings of the interface's type check
LICENSE         GNU General Public License, version 3
```

## Legal notice

**Licence.** Radiolack is free software, copyright 2026 OGD09. You may run it
for any purpose, study it, share it and modify it under the terms of the GNU
General Public License, version 3 or, at your option, any later version. If
you distribute Radiolack or a modified version of it, you must do so under
the same licence and make its source code available. This notice explains
the licence and your responsibilities: it adds no condition to the licence,
which prevails over it. [Read the licence](LICENSE).

**No warranty.** Radiolack is provided "as is", without warranty of any
kind, express or implied, including the warranties of merchantability,
fitness for a particular purpose, accuracy and non-infringement. The author
does not warrant that it works without errors or interruption, or that what
it shows, plays, records or calculates is accurate or complete:
frequencies, levels, measurements, audio, detected signals, the History and
calculations may be wrong. Do not rely on it where an error could harm
people or property, such as for safety of life, distress or emergency
communications, or navigation.

**Limitation of liability.** To the fullest extent permitted by applicable
law, the author and the contributors are not liable for any damage arising
from the use of Radiolack or from the inability to use it, whether direct,
indirect, incidental, special or consequential, including loss of data or
recordings, damage to equipment or property, interference, fines,
penalties, legal costs and claims by third parties, even if advised of the
possibility of such damage.

**Your responsibility.** Radio is regulated differently from one country to
another, and sometimes within a country. You alone are responsible for
making sure that the way you install and use Radiolack, your receiver, your
antennas and any other equipment complies with every law and regulation
that applies to you, including those on:

- the use of radio frequencies and radio equipment, to receive or to
  transmit, and the licences, certificates, call signs and conditions that
  some uses require, such as the frequencies, power, modes and
  identification allowed;
- the reception of some services, which some countries restrict or forbid;
- the secrecy of communications: intercepting communications not intended
  for the general public, or recording, using, publishing or disclosing
  their content, or even their existence;
- privacy and personal data;
- harmful interference to other users of the radio spectrum.

Where the law forbids something, do not do it with Radiolack. The author
does not encourage or endorse any unlawful use.

**Guidance, not a reference.** The band plan, the modes and steps it
suggests, the antenna lengths and every other piece of guidance in
Radiolack are simplified, and may be incomplete, outdated or wrong where
you are: the band plan follows a simplified European table. They are not
legal references. The frequency allocation tables, licence conditions and
regulations of your country, and its radio authority, are.

**Equipment and safety.** Installing and using antennas and radio equipment
can be dangerous. Keep antennas and masts well away from power lines,
protect your installation against lightning, follow the instructions of
your equipment's makers and the local rules on installations and, wherever
equipment transmits, the limits on exposure to radio-frequency fields.

**Other people's communications and data.** What you receive, record or
keep, such as recordings and the History, may contain other people's
communications, voices or personal data. You are responsible for keeping,
using, sharing and deleting them lawfully.

**Remote access.** You choose the devices you let in, and you are
responsible for what is done through them. Keep the access code to
yourself, and reach Radiolack from outside your local network only through
a private network that you control, such as your own VPN.

**Privacy.** Radiolack has no telemetry and no account. It connects only to
your receiver, to the devices you let in and, for its update check, which
you can turn off, to its APT repository. Links to GitHub open in your web
browser, where GitHub's terms and privacy statement apply. A problem report
leaves only if you send it yourself. Your settings, bookmarks, History and
recordings stay on the computer that runs Radiolack, unless you download
them.

**Third parties.** Radiolack is not affiliated with, or endorsed by, the
makers of the receivers and software it works with, radio authorities,
amateur radio organisations or GitHub. Names such as RTL-SDR, Debian, Ubuntu
and GitHub belong to their owners.

**Your rights.** Some countries do not allow some of the exclusions and
limitations above, or give rights that cannot be waived, to consumers in
particular. There, they apply only as far as the law allows, and nothing in
this notice limits those rights. If a court holds part of this notice
unenforceable, the rest still applies.

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
