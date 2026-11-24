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
rtl_tcp, which it runs itself for a receiver plugged into this computer,
other receivers through SoapySDR ([SoapySDR receivers](#soapysdr-receivers)),
or simulates a 2 m band (synthetic I/Q computed in memory). The
engine computes the spectrum and streams it to the interface, which draws the
spectrum and the waterfall with WebGL2 and keeps every setting in one menu
([The menu](#the-menu)). The Receivers menu switches between the
simulated band and the receivers, sets the receiver's
centre frequency, sample rate, gain and frequency correction, changes the
address of an rtl_tcp server, and turns the removal of the DC spike and
the antenna power on or off. The engine
demodulates NFM, AM, synchronous AM (SAM), USB, LSB, CW and WFM,
broadcast FM in mono, with an automatic notch for whistles in AM, SAM and
SSB and a noise blanker for crackle in every mode, and the interface plays
the audio. The engine also
finds the signals in the span, which every interface lists, watches the
ticked bookmarks, outside the span too with a receiver that tunes, and
scans a range, stopping to listen to each signal it finds if asked, across
windows with such a receiver ([Scanners](#scanners)), from an interface
or, with `--scan`, at each start. Any interface records the
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
- For a receiver through SoapySDR (`--soapy`), optional: SoapySDR's Python
  module and the driver's module. Debian and Ubuntu: `python3-soapysdr` and,
  for instance, `soapysdr-module-hackrf`, which the Debian package suggests
  and never installs ([SoapySDR, for other
  receivers](#soapysdr-for-other-receivers)).
- Windows and macOS, each with its own installer, are planned for the public
  release.

## Installation

### Debian and Ubuntu package

On Debian 12 or later and Ubuntu 24.04 or later, Radiolack installs as a
Debian package for the system's Python, with the `radiolack` command, its
manual page (`man radiolack`), a menu entry and an icon. apt also installs
what Radiolack needs (aiohttp, cryptography, NumPy, PyGObject, GTK 3,
WebKitGTK, and the certificates that apt needs to read its APT repository
over HTTPS) and the rtl-sdr package it recommends. SoapySDR's packages, for
other receivers, are only suggested: apt installs none of them unless asked
([SoapySDR, for other receivers](#soapysdr-for-other-receivers)).

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

### SoapySDR, for other receivers

Radiolack reads receivers other than RTL-SDRs through SoapySDR
([SoapySDR receivers](#soapysdr-receivers)), which is optional: the
Debian package suggests its packages and installs none of them. Install
SoapySDR's Python module and the module of the receiver's driver, for a
HackRF for instance:

```bash
sudo apt install --no-install-recommends python3-soapysdr soapysdr-module-hackrf
```

`--no-install-recommends` keeps apt to the packages named and what they
need. Without it, apt also installs what SoapySDR's library recommends,
every SoapySDR module: about 33 packages and 50 MB on Debian 12 for
`python3-soapysdr` alone, among them the remote module and UHD's.
Radiolack needs none of them beyond the receiver's, and never
lets SoapySDR search without a driver, so that the remote module's search
does not run.

The Debian package suggests the drivers' modules that Debian 12 and 13 and
Ubuntu 24.04 and 26.04 all have under the same name, as read in their
package indexes on 2026-11-18:

| Receiver | Module's package |
|---|---|
| HackRF | `soapysdr-module-hackrf` |
| Airspy R2, Airspy Mini | `soapysdr-module-airspy` |
| LimeSDR | `soapysdr-module-lms7` |
| bladeRF | `soapysdr-module-bladerf` |
| RTL-SDR, through SoapySDR rather than rtl_tcp | `soapysdr-module-rtlsdr` |

`soapysdr-module-airspyhf`, for an Airspy HF+, exists on Ubuntu 26.04
only, so the package does not suggest it; install it there by name. On
Debian 12, each module's package brings its driver's library, and with it
the device rules that let users open the receiver, except a LimeSDR's,
which are in `limesuite-udev`, a package that the library only
recommends: name it too with `--no-install-recommends`. Unplug the
receiver and plug it in again once the rules are installed. Not every
driver's rules let the service's user in: see [As a
service](#as-a-service).

An SDRplay needs SDRplay's own API, a closed library with a service of its
own, from SDRplay, and the SoapySDRPlay3 module, built by the user from its
source: Radiolack ships and installs neither, and an SDRplay opens through its
device arguments, `driver=sdrplay`, rather than from the list of devices
found ([SoapySDR receivers](#soapysdr-receivers)).

## Usage

```bash
radiolack
```

opens the interface in a desktop window, on the source used last: the
simulated 2 m band the first time. In the Receivers menu, "Change
device…" switches to a receiver ([RTL-SDR receivers](#rtl-sdr-receivers)). Closing the window
stops Radiolack.

| Option | Effect |
| --- | --- |
| `--ui window` | Desktop window (default). When it cannot open (no display, no GTK or WebKitGTK), Radiolack says why, keeps running and prints the interface's address. |
| `--ui browser` | Opens the interface in the default web browser. |
| `--ui none` | Only prints the interface's address. |
| `--port PORT` | Listens on this port instead of any free one. |
| `--remote [PORT]` | Turns remote access on at start, on PORT (default: the port used last, 8443 the first time), and prints the addresses that devices open, the certificate's fingerprint and the access code: see [Without a window](#without-a-window). |
| `--scan memory`, `--scan sweep`, `--scan signals` | Starts a scan at start without waiting for an interface: the memory scan, the range scan with its saved settings (`sweep`), or the range scan over the current window, stopping to listen (`signals`): see [Without a window](#without-a-window). When it cannot start, Radiolack says why and runs on without it. |
| `--bias-tee` | Turns the antenna power on at start (4.5 V on the antenna socket of an RTL-SDR Blog V3 or V4, or a SoapySDR device's own, once it opens) for the first receiver given or reopened: see [Antenna power](#antenna-power). Off without it; never saved. The simulated band and a SoapySDR device without antenna power ignore it, which Radiolack says. |
| `--rtl-sdr [DEVICE]` | Reads the RTL-SDR receiver plugged into this computer instead of the sources used last, through an rtl_tcp that Radiolack runs. DEVICE is the receiver's index or serial number (default 0, the first one): see [RTL-SDR receivers](#rtl-sdr-receivers). |
| `--rtl-tcp [ADDRESS]` | Reads an RTL-SDR receiver through an rtl_tcp server you run, at ADDRESS (default: the address used last, 127.0.0.1:1234 the first time), instead of the sources used last. The Receivers menu can change the address. One rtl_tcp server at a time. |
| `--soapy ARGS` | Reads a receiver through SoapySDR instead of the sources used last, by its device arguments, which must name a driver, such as `driver=sdrplay`: see [SoapySDR receivers](#soapysdr-receivers). |
| `--version` | Prints the version. |

`--rtl-sdr`, `--rtl-tcp` and `--soapy` may come together, and `--rtl-sdr`
and `--soapy` more than once, up to four receivers: each receiver given
is read on its own, in the order given, with its own spectrum, VFO,
scans and recordings, and `--scan` and `--bias-tee` apply to the first.
Without them, Radiolack opens the receivers used last. The Receivers menu
adds and removes receivers and chooses the one an interface shows
([Several receivers](#several-receivers)); the bookmarks and the History
are common to them. Each receiver after the first runs in a process of its own, so that
two receivers at 2.4 MS/s fit on a laptop's processor; the first
stays as long as Radiolack runs. Should a receiver's process stop, the
receiver goes away and Radiolack says so.

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
the port cannot be used, when `--rtl-sdr` finds no rtl_tcp installed, when
`--soapy` finds no SoapySDR Python module or when `--remote` cannot turn
remote access on, 2 for invalid options.

### The menu

The spectrum, the waterfall and the readout, the frequency with its mode
and its S-meter, take the whole window. A click or a tap on the mode or the
filter width under the frequency opens the Mode and filter panel
([Demodulation and audio](#demodulation-and-audio)). Everything else is in one menu,
behind an icon: a small square at the top left with a mouse, a round button
between the step buttons at the bottom on a touch screen. The menu lists
eight menus beside the one it shows:

- Radio: the mode, the band plan's mode and step, the squelch, the volume,
  the automatic notch, the noise blanker, the filter width and the tuning
  step.
- Bookmarks: the bookmarks, the band plan's ranges and known frequencies
  in view and the memory scan ([Settings and bookmarks](#settings-and-bookmarks)).
- Scan: the signals found, the range scan and the History, in three tabs
  ([Scanners](#scanners)).
- Record: the recordings and, on this computer, their folder
  ([Recording](#recording)).
- Receivers: one card per receiver ([Several receivers](#several-receivers)),
  then the settings of the one this window shows, under Device (its
  device, its name, "Change device…" and Remove), Tuning, Gain, Input and Advanced
  ([RTL-SDR receivers](#rtl-sdr-receivers)): the SoapySDR devices found,
  with a device's antenna sockets and, under Advanced, its gain stages
  and settings ([SoapySDR receivers](#soapysdr-receivers)).
- Display: the spectrum's height, the spectrum, the waterfall, and the band
  plan, the bookmarks and the channels and known frequencies on the
  spectrum.
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
red, or that the memory scan or the range scan runs, in green, on any
receiver.

## Several receivers

Radiolack reads up to four receivers at once, each with its own
spectrum, VFO, scans and recordings; the bookmarks and the History are
common to them. An interface shows one receiver at a time: two windows,
or a window and a phone, show two receivers side by side. The Receivers
menu opens on one card per receiver, numbered from 1:

- Each card names the device and its serial number, or an rtl_tcp
  server's address, and says whether it is receiving, with its own
  centre frequency, waits for its device or is idle,
  read by no interface and running nothing. A tag tells a scan or a
  recording that runs on it, such as "Memory scan" or "Recording audio".
- The card of the receiver this window shows is framed in blue and says
  "Shown here"; the others have "Show here", which shows that receiver in
  this window alone; the others go on with their scans and recordings.
- "+ Add a receiver", under the cards, says how many more devices are
  plugged in, up to four receivers, and lists the devices that no
  receiver reads: the simulated band, each RTL-SDR receiver plugged in,
  an rtl_tcp server and the SoapySDR devices found. Choosing one opens it
  in a new receiver, which this window then shows, with the settings kept
  for that device.

The settings below, headed "Receiver N" with the device's name, apply to
the receiver shown. Under Device, Name gives the device a name of your
own, such as "V3 ADS-B" or "Nooelec 2 m", 40 characters at most: the
cards, the chip beside the frequency and the device lists show it in
place of the device's own name, on every interface, a phone included,
and it stays with the device, by its serial number (a SoapySDR device's
driver and serial number, an rtl_tcp server's address), whichever
receiver reads it later. Emptied, the field gives the device its own
name back. "Change device…" shows the list of
devices: one that another receiver reads shows "read by receiver N" and
cannot be chosen. Remove stops the receiver's scan and its recordings,
releases its device and keeps its settings; the interfaces that showed it
show receiver 1, which stays as long as Radiolack runs and cannot be
removed. While a scan or a recording runs on it, a second press
confirms, saying what stops.

With several receivers, a chip beside the frequency, such as "2
RTL2838UHIDIR", names the receiver this window shows; a press on it opens
the Receivers menu. A device is named by the name you gave it, or else as
it names itself: an RTL-SDR's product, such as RTL2838UHIDIR for the chip
it carries, or a SoapySDR device's label, once the Receivers menu has
listed it; its kind before.

Each device remembers the receiver it chose or added last, and shows it
again at its next connection while Radiolack still runs it; this
computer's windows share that choice, the last one made. Radiolack
reopens the receivers at its next start, unless a receiver option is
given. The protocol calls them radios ([docs/design/protocol.md](docs/design/protocol.md)).

## RTL-SDR receivers

Radiolack reads RTL-SDR receivers through rtl_tcp. The Receivers menu's
device list, which "Change device…" shows, switches between the simulated band ("Synthetic band (2 m)"),
each receiver plugged into this computer, such as "RTL-SDR · NESDR SMArt
v5 · 59196661" (its name and serial number), a receiver behind an rtl_tcp
server ("RTL-SDR · rtl_tcp"), for every open interface, and a receiver
read through SoapySDR ("SoapySDR device",
[SoapySDR receivers](#soapysdr-receivers)). Each keeps its own
settings, demodulation included, and Radiolack opens the one used last at
the next start. "RTL-SDR · rtl_tcp" connects to the address used last. The
options below choose one on the command line.

The receivers plugged in:

- Radiolack lists them each time the Receivers menu shows, and again every
  2 seconds while it shows, so that a receiver plugged in meanwhile
  appears; never otherwise. It reads what Linux tells of the USB devices
  (`/sys/bus/usb/devices`) and opens none of them, so the list never
  disturbs a receiver that another program uses. It knows the receivers
  that librtlsdr opens, by their USB IDs.
- Choosing one opens it by its serial number, and its settings are kept
  under it: the next start without an option opens that receiver again,
  wherever it is plugged in. The first time you choose the only receiver
  plugged in, it starts from the settings kept for the first receiver,
  `--rtl-sdr` without a device, which were its own.
- A device that another of Radiolack's receivers reads shows "read by
  receiver N" ([Several receivers](#several-receivers)). A receiver that
  another program holds stays listed, with "in use by
  another program" (Gqrx or an rtl_tcp, for instance), or "held by the DVB
  driver" when Linux took it as a TV tuner (the rtl-sdr package's
  blacklist file prevents that). Choosing it makes Radiolack wait until
  the receiver is free. The receiver in use stays listed even unplugged,
  with "not found".
- rtl_tcp finds a receiver by its serial number only when no other has the
  same, and when the number cannot be read as an index: with two
  receivers plugged in, rtl_tcp reads `00000001` as the second receiver.
  Such receivers stay listed but cannot be chosen, and the menu says why:
  give each its own serial number with `rtl_eeprom -s`, from the rtl-sdr
  package, with one receiver plugged in at a time (it writes the
  receiver's EEPROM; unplug and plug it in again afterwards). Radiolack
  never guesses which index is which: librtlsdr's order need not follow
  the list's.
- A device that remote access serves sees the same list and may choose
  among the receivers, as it may switch between receivers.
- When none can be chosen (none plugged in, receivers that rtl_tcp cannot
  tell apart, or a system other than Linux), "RTL-SDR · this computer"
  opens the receiver used last, the first one plugged in the first time.

For a receiver plugged into this computer, Radiolack runs rtl_tcp itself:

```bash
radiolack --rtl-sdr
```

- Radiolack starts rtl_tcp, on 127.0.0.1 and a free port, when an interface
  opens or a scan starts, `--scan` at start included, and stops it once no
  interface is open and no scan runs, or when Radiolack stops:
  the receiver then stays free for other programs, such as Gqrx. Once it
  has been read, rtl_tcp 0.6.0 heeds the request to stop only about 5 s
  later: Radiolack ends it after a second instead. On Linux,
  should Radiolack end without stopping rtl_tcp, killed for instance, the
  system stops it (through setpriv, from util-linux).
- `--rtl-sdr 1` reads the second receiver, and `--rtl-sdr SERIAL` the one
  with that serial number, as rtl_tcp's `-d` option takes them. The
  Receivers menu lists the receivers plugged in with their serial
  numbers.
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
Receivers menu, and rtl_tcp's default address, 127.0.0.1:1234, the first time.
rtl_tcp has no access control: anyone who reaches it can use the receiver,
so keep it on 127.0.0.1, or on a network you trust when the receiver is on
another computer.

The Receivers menu's "Server address" field changes the address while
Radiolack runs, for instance when the receiver's computer answers at another
address on another network: type HOST, PORT or HOST:PORT and press Enter.
Radiolack hangs up and connects there with the same settings, and every open
interface follows; the field shows the full address, HOST:1234 for a host
alone. An address Radiolack cannot read is refused, with the reason in the
menu, and the field shows the address in use again. When nobody answers
there, the status line says why and Radiolack tries again by itself until
you give another address.

rtl_tcp sends every sample, two bytes each: 4.8 MB/s at 2.4 MS/s, about
17 GB an hour, 2 MB/s at 1.024 MS/s and 0.48 MB/s at 0.240 MS/s. Over a
slow or metered link, such as a phone's connection, lower the sample rate.

The two lowest sample rates, 0.240 and 0.300 MS/s, come from librtlsdr's
lower range of rates, 225,001 to 300,000 samples per second: a span as wide
as a shortwave band, 29 and 37 Hz per bin of the spectrum, and less work
for the computer. WFM's channel is 320 kHz wide: at these rates WFM plays
nothing, the mode stays, and the status line and Radio's audio note say
"WFM needs a sample rate of 1.024 MS/s or more". The finest view stays
4 kHz wide whatever the rate.

With either option:

- At each connection, Radiolack sends its own settings, since rtl_tcp keeps
  those of its previous client: 145 MHz, 2.4 MS/s, automatic gain and no
  frequency correction at first, then the latest ones.
- The Receivers menu names the tuner and sets the centre frequency, the sample
  rate (0.240 and 0.300 MS/s, or 1.024 to 2.88 MS/s), the gain and the
  frequency correction. "Tuner
  AGC" gives automatic gain; otherwise the gain is one of those the tuner
  offers. The frequency correction, in ppm from -100.00 to +100.00 to the
  hundredth, makes up for a receiver whose clock is off, which shifts every
  signal by as many millionths of its frequency (10 ppm: 1.45 kHz at
  145 MHz): set it so that a known signal sits on its frequency
  ([Frequency correction](#frequency-correction)). A change shows in every
  open interface. Once you press Enter or leave a field, it shows the
  setting in use: as the receiver took it, a correction rounded to the
  hundredth for instance, or the previous one when the change is refused, such as a frequency the tuner cannot reach,
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
  once frames come from the new span. An R820T or R828D puts the span's
  centre on its grid, up to 56.25 kHz away ([The tuner's
  grid](#the-tuners-grid)); when that would bring the channel within 5 kHz
  of the centre, the span goes to the grid's point below. Beyond the
  receiver's reach nothing tunes, and the status line says why.
- Stepping (the wheel, the arrow keys, the − and + buttons) moves the span
  by the same rule when a step would enter the outer tenth of the span on
  either side, which the receiver's filter dims: about every megahertz at
  2.4 MS/s. One move at a time: steps made while the receiver settles add
  up, and the VFO goes there with the new span. Dragging the filter stays
  within the span, and so does stepping with a source that does not tune,
  such as the simulated band.
- After a change of frequency, sample rate or frequency correction, the
  display waits 0.4 s for the receiver to settle.
- Radiolack keeps half a second of samples for the computer to catch up.
  When it falls further behind, the oldest samples go, and the audio skips:
  their count, in samples and in seconds of signal, goes to the error
  output at most once a minute, among the warnings of a [problem
  report](#problem-reports).
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
  Fitipower FC0012 and FC0013, FCI FC2580. Shortwave comes through direct
  sampling or a tuner that covers it (below), and antenna power through the
  bias tee (below). A converter in front of the receiver, such as an
  upconverter or a satellite LNB, moves its range (below).

### Antenna power

An RTL-SDR Blog V3 or V4 can power an active antenna or a preamplifier
through its antenna socket: 4.5 V, at most 180 mA, with its bias tee. The
Receivers menu's "Antenna power (bias tee)" shows whether it is on, on every
interface. Turning it on takes two presses: the first one makes the button
say "Confirm: 4.5 V on the antenna socket" for 5 s, and the second one turns
the power on; turning it off takes one.

A SoapySDR receiver whose module declares a setting that powers its antenna
socket, such as a HackRF's, an SDRplay's or an RTL-SDR's, has the same
control, under the same rules, whose first press says "Confirm: power on
the antenna socket": the module does not say the voltage. See [SoapySDR
receivers](#soapysdr-receivers).

The antenna power is off at each start, unless Radiolack was started with
`--bias-tee`, and it is never saved. Radiolack sends its state at each
connection to rtl_tcp, since rtl_tcp keeps that of its previous client, and
turns it off before it closes the receiver: when it stops, when another
receiver of the list takes its place, and before it leaves an rtl_tcp server
for another address.

Limits:

- Power into an antenna that is a short circuit for direct current, such as
  a loop or a folded dipole, or into a receiver's input that is not made
  for it, may damage the dongle or what is connected to it.
- An rtl_tcp older than 0.5.4 ignores the command, and Radiolack cannot read
  the state back from the receiver.
- A crash, a power cut or a killed Radiolack leaves the power as it was.
- An rtl_tcp server that Radiolack did not start may set the power by
  itself, for instance with its `-T` option.
- It is not enough for a satellite LNB, which needs 13 to 18 V.

### Shortwave

The Receivers menu's "Shortwave" list says what a receiver uses below its
tuner's range, and is kept with that receiver's settings; the line under
it says what the choice needs:

- "None": the tuner alone, as before.
- "Direct sampling, Q input (RTL-SDR Blog V3)": below the tuner's range,
  the RTL2832 samples its Q input, which the V3 wires to its antenna
  socket, at 28.8 MS/s without the tuner; above it, the tuner takes over.
  The centre frequency goes down to half the sample rate, so that the span
  starts at 0 Hz: 1.2 MHz at 2.4 MS/s.
- "Direct sampling, I input": the same on the I input, for a dongle
  modified for it.
- "Tuner (RTL-SDR Blog V4)": a tuner that covers shortwave by itself, as
  the V4's does; the centre frequency goes down to half the sample rate.
  Offered with an R820T or R828D tuner only.

Radiolack switches by itself: a centre frequency below the tuner's range
samples directly, another uses the tuner. Another choice in the list, or
another sample rate, brings the centre frequency within the new reach. While
the receiver samples directly, the menu says so under the list: the gain
controls are disabled ("Direct sampling: no gain control"), the gain set
before comes back with the tuner, and "RTL2832 AGC while sampling directly",
off by default, turns on the RTL2832's own AGC, to try when shortwave is too
quiet or too loud.

Limits:

- Direct sampling receives nothing below about 500 kHz on a V3, and covers
  up to 28.8 MHz at most: signals above and below 14.4 MHz mirror each
  other, a signal at f also showing at 28.8 MHz − f. It has no gain
  control.
- A dongle other than a V3 needs a hardware modification for direct
  sampling, and Radiolack cannot tell whether it has one.
- With an E4000 or FC2580 tuner, whose range starts above what direct
  sampling reaches, the centre frequencies in between are refused, with the
  two ranges in the menu.
- The V4 setting has not been checked on a V4. With an rtl-sdr that does
  not know the V4 (0.6.0, Debian 12's), a V4 tunes wrong on every band, and
  Radiolack cannot see it: rtl_tcp announces a V4 as any R828D.
- A tuner asked for a frequency it cannot reach says nothing: with the
  wrong setting, the band is silent, not refused.

### Converter

A converter in front of the receiver moves a range of frequencies into the
tuner's: an upconverter brings shortwave above 24 MHz, and a satellite LNB
brings the 3 cm band down to a few hundred megahertz. The Receivers menu's
"Converter" list says which one is there, and "Converter oscillator" its
oscillator's frequency, typed as any frequency (125 for 125 MHz, 9750,
9.75 GHz); both are kept with the receiver's settings:

- "None": nothing between the antenna and the receiver, as before.
- "Upconverter": the receiver tunes the frequency plus the oscillator's,
  such as 125 MHz: 7.1 MHz on the air is 132.1 MHz for the receiver.
- "Downconverter (LNB)": the receiver tunes the frequency minus the
  oscillator's, such as 9750 MHz: 10,489.5 MHz on the air is 739.5 MHz for
  the receiver.
- "Downconverter, inverted spectrum": the receiver tunes the oscillator's
  frequency minus the frequency, for an oscillator above the band, which
  mirrors the spectrum. Radiolack swaps I and Q as the samples arrive, so
  that the spectrum, the audio and an I/Q recording show the band as it is
  on the air.

Every frequency Radiolack shows and keeps is the one on the air: the
spectrum, the VFO, the bookmarks, the scans, the History and the
recordings' names. Only the frequency asked of the receiver is converted.
The converter applies on top of the tuner and of direct sampling alike. The
centre frequency reaches what the receiver tunes, converted, and stays at or
above half the sample rate, so that the span never goes below 0 Hz.

Changing the converter or its oscillator keeps the receiver where it is
tuned: the frequencies shown change, and the signals stay where they are on
the screen. The VFO stays on the signal it was on, at its new frequency,
the signals found are found again at theirs, and the bookmarks keep their
frequencies on the air. A change that would put the receiver's centre
frequency out of reach on the air, below half the sample rate for instance,
is refused, with the reason in the menu: tune elsewhere first, or type the
oscillator before choosing the converter. As with a change of centre
frequency, a scan stops; an I/Q recording stops too, as its metadata
describes one converter, while an audio recording goes on.

Limits:

- The frequency correction in ppm is the receiver's crystal's, not the
  converter's: type the oscillator's true frequency in its field, measured
  if you can.
- An LNB's oscillator moves with temperature, by far more than the
  receiver's crystal: a few hundred kilohertz is common for a cheap one.
- An LNB needs 13 to 18 V on its cable, which no dongle's antenna power
  gives: power it through a bias tee of its own, and keep the dongle's
  antenna power off.
- No converter has been checked on hardware.

### Frequency correction

The receiver's crystal sets every frequency it tunes and its sample rate:
a crystal 10 ppm fast shows a signal 1.45 kHz too low at 145 MHz, and
71 Hz too low at 7.1 MHz. The Receivers menu's "Frequency correction (ppm)"
takes it to the hundredth of a ppm, from -100.00 to +100.00: a hundredth
is worth 0.07 Hz at 7 MHz and 1.45 Hz at 145 MHz, where a whole ppm was
145 Hz. A correction saved by an earlier Radiolack, a whole number, reads
as it is.

librtlsdr takes whole ppm only: the receiver gets the nearest whole number,
-3 for -2.6, as before, and Radiolack corrects the rest itself by asking
the receiver for a slightly different frequency, so that it truly tunes the
one shown. A change of the hundredths alone retunes the receiver like a
change of centre frequency. This applies on the tuner, in direct sampling
and through a converter alike, to the frequency the receiver itself tunes:
the converter's oscillator is taken as typed.

An R820T or R828D, the tuner of most dongles, gets no correction at all,
since a corrected crystal would move its grid ([The tuner's
grid](#the-tuners-grid)): Radiolack takes the whole of it, by the frequency
it asks for, by where it says the samples were taken, and by the VFO's
distance from the centre frequency, scaled as the receiver's sample rate is.

Limits:

- The sample rate keeps the rest, at most half a ppm: a signal at the edge
  of a 2.4 MS/s span shows up to 0.6 Hz off. With an R820T or R828D it
  keeps the whole correction, which Radiolack makes up for at the VFO
  only: elsewhere in the span, a signal shows up to the correction times
  its distance from the centre off, 72 Hz at the edge of a 2.4 MS/s span
  for a crystal 60 ppm off.
- In direct sampling, the RTL2832's mixer tunes in steps of 6.87 Hz,
  rounded down: up to 6.9 Hz below the frequency asked.
- Within half a ppm of the tuner's range's edges, the frequency asked
  stays within the range, so that the receiver does not switch to direct
  sampling by itself or lose its lock: a centre frequency there may be up
  to 12 Hz off at 24 MHz and 883 Hz off at 1766 MHz.
- A crystal drifts with temperature, by up to 1 ppm (145 Hz at 145 MHz)
  as the receiver warms up: hundredths hold only after a calibration on a
  known signal ([Calibration](#calibration)), once the receiver is warm,
  and the next calibration may differ.
- The tuner's own PLL may add an error of its own, fixed for each centre
  frequency, which the correction does not include: an R820T's or R828D's
  is the same at every centre frequency of a range on its grid, and gone
  once its driver is set ([The tuner's grid](#the-tuners-grid)).

### The tuner's grid

The PLL of an R820T or R828D cannot set every frequency exactly, and how
far it misses depends on the driver behind rtl_tcp: rtl-sdr up to 2.0.1
(Debian 12's 0.6.0) and the RTL-SDR Blog driver err by another amount at
each centre frequency, from -172 to +78 Hz at 2 m and from -1376 to
+621 Hz at 23 cm, so that a signal moved each time the span did. Where the
tuner's oscillator, the frequency asked plus 1.815 MHz at 2.4 MS/s, is a
multiple of 112.5 kHz, both drivers come out the same at every centre
frequency: rtl-sdr 2.0.2 and later exactly, the older ones one constant low,
110 Hz from 111 to 221 MHz, 220 Hz up to 442 MHz and 879 Hz above 885 MHz.

So Radiolack asks such a tuner for the nearest centre frequency on that
grid, at most 56.25 kHz from the one you set, and shows it under the
Receivers menu's centre frequency ("In use: 144.9975 MHz, where the tuner
sits"). The spectrum, the VFO, the scans and the recordings follow it: you
tune as before. A new centre frequency set there moves the VFO by the
difference between the centres you set, so that it comes back where it was
with the centre.

The Receivers menu's "rtl-sdr driver" tells Radiolack which driver runs,
and Radiolack saves it with the receiver:

- "Unknown", the default: Radiolack takes the tuner as exact on its grid.
  With an older driver, every signal then shows the same amount high at
  every centre frequency of a range, 110 Hz at 2 m, until Calibrate
  identifies the driver.
- "rtl-sdr up to 2.0.1, RTL-SDR Blog": Radiolack takes that constant off
  everywhere.
- "rtl-sdr 2.0.2 and later": the tuner sits where Radiolack places it.

Calibrate identifies the driver on a steady carrier and saves it with the
receiver, and checks it again at every calibration ([Calibration](#calibration)).
You may also choose it here: the rtl-sdr package that provides rtl_tcp
gives the version, and the RTL-SDR Blog driver computes as 2.0.1 does. A
choice changes only where Radiolack says the samples were taken: nothing
is asked of the receiver.

A converter changes nothing: it is the frequency the receiver itself tunes
that goes on the grid.

Limits:

- Not on the grid, and taken as exact: direct sampling, other tuners, any
  R828D below 28.8 MHz, where an RTL-SDR Blog V4's driver adds 28.8 MHz,
  and an oscillator below 27.66 MHz, a centre frequency below about
  25.8 MHz at 2.4 MS/s.
- At the very edges of the tuner's range, the centre frequency may move by
  up to 112.5 kHz, 102.5 kHz at 1766 MHz.
- At 0.240 MS/s, the part of a span that the scans search, 96 kHz on
  either side of its centre, falls short of the grid's next point: the
  sweep and the memory scan cannot search 3 kHz on either side of each
  point of the grid, every 112.5 kHz. From 0.300 MS/s they search everything.
- The constant comes from the driver's code, read for rtl-sdr 2.0.1 and
  2.0.2 and the RTL-SDR Blog fork, and was measured on one RTL-SDR Blog V3
  with rtl-sdr 0.6.0 near 111 MHz, at the tuner's divider 16, which covers
  111 to 221 MHz: there, with the driver identified, a carrier stayed within
  0.2 Hz across 53 centre frequencies, where it moved by 203 Hz before. A
  driver that computes otherwise keeps the error it has, and Calibrate then
  names neither computation; a driver changed without changing the setting
  may put signals up to twice the constant off until the next calibration.

For developers, `tools/measure_pll.py`, run from a checkout, measures how
well a Radiolack already running on such a receiver labels its samples: it
moves the centre frequency for a few minutes, off the grid and on it,
measures one steady carrier at each, and says whether the driver set holds
or which to choose. Its `--help` says what it needs; the Debian package does
not install it.

Also for developers, `tools/measure_listening.py` measures the processor
time that synchronous AM, Radiolack's SAM, the automatic notch, the noise
blanker and the fine spectrum ([Spectrum view](#spectrum-view)) each add to
the engine at 2.4 MS/s, the fine spectrum with the blanker too, on
synthetic samples, without a receiver; it takes about three minutes,
`--output FILE` keeps its table and `--without-impulses` leaves the
scene's impulses out, the usual case for the blanker.

`tools/measure_radios.py` measures several radios in one Radiolack (the
Several receivers at once milestone,
[docs/design/later-milestones.md](docs/design/later-milestones.md),
section 5): each reads synthetic samples at 2.4 MS/s with a listener
playing its audio, for ten minutes, and the tool reports Radiolack's
processor time and whether the audio ran dry; `--radios` sets how many,
one to four. Linux only.

Three more developer tools prepare SoapySDR receivers (the Any receiver
milestone, [docs/design/later-milestones.md](docs/design/later-milestones.md),
section 1). `tools/measure_rates.py` measures the processor time of the
engine and of bringing a device's samples in at sample rates from 2.4 to
10 MS/s, on synthetic samples, in about ten minutes: back to back, the
work to do, then paced as a receiver delivers its blocks, what a system
monitor shows while Radiolack runs and whether each block fits its 1/30 s.
`tools/check_search.py`
runs SoapySDR's search for each driver named in a network namespace of its
own, every socket refused and reported, and says which searches stay off
the network; it needs `unshare`, a C compiler and the SoapySDR Python
module, never searches for `rtlsdr` and opens no device.
`tools/measure_reader.py` reads a SoapySDR receiver from a Python thread
for a minute and reports the samples lost, taking the device's timestamps
into account only where they count samples (some modules' do not): it
opens and reads the receiver named, so run it only on a receiver that
nothing else uses. Their `--help`
says more; the Debian package installs none of them.

### Calibration

The Receivers menu's Calibrate finds the frequency correction on the
standard-frequency stations, by itself, or on a carrier whose frequency you
know. Either way Radiolack measures the strongest line near the frequency
to a tenth of a hertz, over two seconds, which the display, at 293 Hz per
bin at 2.4 MS/s, could never show.

On the stations, once the receiver has warmed up for a quarter of an
hour, "Calibrate on the stations" tunes the receiver to each
standard-frequency and time-signal station of the band plan within its
reach, twelve at most, lowest first, the VFO on the station in AM, and
measures its carrier within 200 ppm of the station, 2 kHz at most; then the
receiver and the VFO come back where they were. A run takes a few seconds
per station, for every interface, and goes on when the dialog closes; the
Receivers menu says while it runs, and Stop ends it. The table shows, for
each station, how far its carrier lies from the station's frequency, how
far it stands above the noise, and the correction it implies, or why it
gave none. "Proposed correction" is the median of the corrections that
agree within 0.3 ppm, from two stations at least, or from one alone, the
only one received, standing 30 dB or more above the noise, which the
dialog flags; stations outside that group show greyed. "Apply the
proposal" sends it as a change in the Receivers menu would. Otherwise it
says why it proposes nothing: no station received, one too weak, stations
that disagree, or a correction beyond -100 to 100 ppm. A run stopped with
Stop proposes from the stations it measured; one ended by a change of the
receiver's settings, other than its gains, antenna and DC spike, or of the
VFO, by a scan or a recording, proposes nothing; choosing another receiver
forgets the latest proposal. A change that ends a run applies once the
receiver and the VFO are back: a press on the spectrum while it shows a
station tunes the VFO there, outside the span the receiver comes back to,
where it reads nothing until the receiver is tuned there too.

The stations lie on shortwave and below: set the shortwave input first
([Shortwave](#shortwave)). In direct sampling at 2.4 MS/s the receiver
reaches the stations from 2.5 MHz up; a lower sample rate reaches lower,
though a V3 in direct sampling does not reach the long-wave stations,
below 500 kHz. Which stations come in depends on
the hour and the season; some days none does: try again at another hour,
or check the antenna. The run does not start while a recording runs, nor
behind a converter, whose oscillator would pass for the crystal's error,
nor on a receiver that does not tune; it stops a scan, as tuning by hand
does. While it runs, the measure on a known carrier waits for its end.

On a known carrier:

1. Let the receiver warm up for a quarter of an hour.
2. Tune the VFO onto a steady carrier whose frequency you know, in AM or
   CW, and keep it off the centre frequency, where removing the DC spike
   takes a carrier out.
3. Open Receiver > Calibrate. Every two seconds it shows the carrier
   measured, how far it stands above the noise, and how far it lies from
   "True frequency of this carrier", which starts from the VFO's frequency
   rounded to the kilohertz: type the true frequency if it differs. On an
   R820T or R828D, it first identifies the rtl-sdr driver (below).
4. Apply sends "Correction to apply", the correction that brings the
   carrier to its true frequency, as a change in the Receivers menu would,
   and tunes the VFO there, where the carrier then lies. The next measures
   show it there, within about a hertz.

Good references are carriers whose frequency is kept exact on purpose: the
standard-frequency and time-signal stations of the shortwave bands, on
round frequencies such as 5, 10 or 15 MHz, and the carriers of AM
broadcasters, most of which hold their frequency closely; check a station's
frequency in a current source before you rely on it. A signal whose carrier
is not its strongest line gives no reference: NFM and SSB have none to
measure, and a CW signal that keys on and off moves between measures.

Calibrate in direct sampling ([Shortwave](#shortwave)) to measure the
crystal alone: on the tuner, the result also takes in the tuner's own error
at that centre frequency, which another centre frequency does not share.
An R820T's or R828D's own error is the same at every centre frequency of a
range on its grid, and none is left once its driver is set ([The tuner's
grid](#the-tuners-grid)).

On an RTL-SDR whose tuner is an R820T or R828D, the first steady carrier
that Calibrate measures also identifies the rtl-sdr driver: the receiver's
centre frequency moves for about ten seconds, for every interface, by up to
2 kHz off the tuner's grid at three points, and comes back. How far the
carrier moves from one point to the next tells the driver's two
computations apart, whatever the crystal's error, the correction, the
driver set and the true frequency. "rtl-sdr driver" then says what it
found, which is saved with the receiver; at the next calibration it says
"confirmed", or that it changed, as when the driver was upgraded. The
correction comes only from the measures taken once the driver is known.

The check is not made, and the calibration goes on with the driver set,
while a scan runs or an I/Q recording is made, in direct sampling, on
other tuners, where Radiolack does not place the tuner on its grid, or
with the VFO within 3 kHz of the centre frequency or near the span's edge:
the dialog says why. A measure spoilt at a point, by a burst of voice on
an AM carrier or samples lost for a computer behind, is made again, for up
to 6 s a point. It names no driver when the carrier fades or moves during
the check, or follows neither computation within 5 Hz; the driver set
stays. An audio recording goes on through it, as through any change of
the centre frequency, and while the tuner is off its grid what it hears
may sit off by the tuner's own error there, under 200 Hz at 2 m.

The dialog says why it shows no frequency: nothing received at the VFO, no
line standing 20 dB above the noise within the filter, two lines of nearly
equal strength, or a line that moved by more than 1 Hz between two
measures. It refuses a correction beyond -100 to 100 ppm, which a typo
gives, and warns before a change of more than 10 ppm, more than a crystal
drifts: check the true frequency and that the VFO is on that carrier. The
simulated band has no correction to calibrate.

Limits:

- The result is as true as the frequency you type.
- On shortwave, the ionosphere moves a carrier by up to about 1 Hz, and
  more when the path changes: measure a few times and at a quiet hour.
- Behind a converter, the converter's oscillator is taken as exact, and
  the correction is the receiver's own.
- The receiver corrects its sample rate by whole ppm only: a carrier far
  from the centre frequency measures up to half a ppm of that distance off,
  0.6 Hz at the edge of a 2.4 MS/s span. An R820T or R828D corrects none of
  it, but Radiolack scales the VFO's distance from the centre: the carrier
  measured at the VFO keeps no more.

## SoapySDR receivers

Radiolack reads other receivers through SoapySDR, such as an SDRplay, a
HackRF or an Airspy, and an RTL-SDR too, with SoapySDR's Python module
(`python3-soapysdr`) and the driver's own module installed ([SoapySDR, for
other receivers](#soapysdr-for-other-receivers)). Name the receiver by its
SoapySDR device arguments, which must name a driver:

```bash
radiolack --soapy driver=sdrplay
```

- `driver=rtlsdr,serial=00000001` picks one receiver among several of a
  driver. A driver is required so that no search reaches the network:
  without one, SoapySDR asks every module to search, its remote module
  included.
- Or choose the receiver in the Receivers menu: "SoapySDR device" in the
  device list, under "Change device…", shows the SoapySDR devices found, a "Search again" button
  and the "SoapySDR device arguments" field. The first time the menu shows
  them, Radiolack searches for HackRF, Airspy, Airspy HF+, LimeSDR and
  bladeRF devices, one driver at a time, never without a driver and never
  for RTL-SDR receivers, which have the list's entries of their own;
  "Search again" searches once more, after plugging a receiver in for
  instance. Choosing a device found opens it, by its driver and serial
  number; any other device, an SDRplay or an RTL-SDR for instance, opens
  by its arguments, typed in the field as `--soapy` takes them, such as
  `driver=rtlsdr`, which Radiolack checks as `--soapy` does, saying why it
  refuses them. The list shows the device in use first, whether a search
  found it or not; a search leaves the drivers of the devices in use alone,
  so as not to disturb it, and no receiver opens while a search runs.
  Radiolack never searches at start, nor while no interface of this
  computer asks; a search opens no receiver's stream, but each driver's
  search talks to the devices plugged in. Without the Python module, the
  list says which package brings it, and nothing is searched. Once a device
  was chosen, "SoapySDR device" in the device list opens it again at
  once.
- A device that remote access serves sees the list and may choose among
  the devices found, but neither searches nor sees or gives arguments:
  this computer's interface does.
- Radiolack opens the receiver when an interface opens or a scan starts,
  and releases it once none is open and no scan runs, as for an RTL-SDR
  on this computer. Once found, the receiver is known by its driver and
  serial number: its settings are saved under them, and the next start
  without `--soapy` opens that receiver again.
- The Receivers menu shows "SoapySDR device", with the receiver's name and
  driver once it opens. Its sample rates are those the receiver offers, up
  to 8 MS/s, which the engine holds; where the receiver has an analogue
  filter, Radiolack sets it to the nearest bandwidth at or below 0.8 times
  the sample rate. The frequency correction: a receiver that corrects
  itself takes the nearest whole ppm, as an RTL-SDR does, and Radiolack the
  rest by where it says the samples were taken, never by asking for a
  slightly different frequency as it does through rtl_tcp: a tuner moves in
  steps of its own, an R820T's about 110 Hz near 111 MHz, which a frequency
  asked a few hertz off would not follow. A correction measured on a
  carrier through SoapySDR also takes in the tuner's step at that
  frequency: elsewhere an R820T may sit up to one step off, which rtl_tcp
  avoids by placing it on its grid ([The tuner's grid](#the-tuners-grid)).
- Gains: "Tuner gain" moves the receiver's one gain stage, or with several
  its overall gain, which the receiver's SoapySDR module spreads across
  them; "Tuner AGC" turns automatic gain on where the receiver has it,
  and is greyed where it has none.
  With several stages, the Receivers menu's Advanced section, at its
  bottom, has one slider per stage, under the receiver's names for them,
  such as a HackRF's LNA, AMP and VGA, or an SDRplay's IFGR and RFGR,
  which are gain reductions: more is less gain. A stage's change shows in
  the overall gain, and the other way round, as the receiver holds them.
  A HackRF's input amplifier, AMP, is off at each first opening, whatever
  the receiver's default, and only its own slider turns it on: the overall
  gain moves LNA and VGA alone, around the amplifier as it is. The gains
  are saved with the receiver.
- Antenna sockets: a receiver with several, such as an SDRplay RSPdx's,
  shows "Antenna socket" with the receiver's names for them; the
  receiver's own choice until changed, then saved with it.
- The receiver's other settings, as its module describes them, show under
  Advanced, each with the module's description: a box, a list or a number
  within its range, such as an SDRplay's notch filters or an RTL-SDR's
  direct sampling, offset tuning and digital AGC. Each starts at the
  receiver's own value, is saved with the receiver once changed, and is
  written between two reads of the receiver, then read back: one that the
  receiver refuses keeps its value, and the note under Device
  says why. Radiolack leaves out the settings that are free text, which
  the module would read as it likes, those named for transmission, and
  more than 32. For shortwave on an RTL-SDR, the device list's RTL-SDR
  entries' Shortwave list does more than the
  module's direct sampling ([Shortwave](#shortwave)).
- Antenna power: a setting whose key or name speaks of a bias tee or
  antenna power, such as `biastee`, `bias_tx` or `biasT_ctrl`, is the
  Receivers menu's "Antenna power (bias tee)", under the rules of an
  RTL-SDR's ([Antenna power](#antenna-power)): off at each opening of the
  receiver, whatever the receiver or a saved file says, unless Radiolack
  was started with `--bias-tee` or an interface turned it on since; never
  saved; off before Radiolack releases the receiver, however it stops
  reading it. Never a plain box under Advanced. A receiver with several
  such settings has them all off, and the control drives one: its receive
  channel's before the receiver's own, then the first declared.
- Without the Python module, `--soapy` says which package to install and
  exits; a SoapySDR receiver reopened at start waits, saying the same, and
  tries again by itself once it is installed. An unplugged or silent
  receiver is a wait too, with its reason in the Receivers menu and the
  status line, tried again by itself.
- Radiolack opens the receiver's receive stream and nothing else. Its
  overflows, the samples dropped when the computer falls behind (in
  seconds of signal too), the blocks that took longer than their period
  and the samples that an I/Q recording did not get go to the error output
  at most once a minute.
- Record I/Q keeps the receiver's samples in the format Radiolack reads
  them in, 8 or 16-bit integers or 32-bit floats, as the receiver gives
  them ([Recording](#recording)).
- The service runs a SoapySDR receiver too, with `--soapy` in its command
  ([As a service](#as-a-service)).
- Checked on a receiver so far: an RTL-SDR through `driver=rtlsdr`, which
  opens, takes its gain and sample rates, plays WFM, is released when no
  interface is open, and stops. Its frequency correction, the Advanced
  section's stages and settings, its antenna power and an I/Q recording
  are not yet checked on a receiver, nor is any other driver: an SDRplay
  and a HackRF come next. Other receivers work as far as SoapySDR and
  their modules do. The tests run against a stand-in for SoapySDR's
  Python module ([Tests](#tests)).

## Demodulation and audio

The engine demodulates one VFO, which every interface shows: a click on the
spectrum, the wheel, the keyboard or a bookmark tunes it, and a change made
in one interface shows in the others, the last change winning. The Radio
menu sets the mode, the filter width and the squelch, and every mode
plays.

The mode and the filter width under the frequency open the Mode and filter
panel, under them, or rising from the bottom edge on a phone: the modes by
family (FM: NFM and WFM; AM: AM and SAM; Sideband: USB and LSB; Morse: CW),
the band plan's mode at the VFO marked "plan", then the filter width, with
Narrow, Normal and Wide (0.6 times, once and 1.5 times the width the mode
opens with, within its limits), and the band plan's box, the Radio menu's
own. A mode chosen there keeps the panel open, for its width; Escape or a
press outside it closes it, without tuning. The M key still steps to the
next mode. NFM audio is de-emphasized, its CTCSS tone is removed and its band
stops at 3.5 kHz. AM audio follows the carrier's level: its loudness depends
on the modulation, not on the signal's strength. USB and LSB keep their own
sideband, and CW sounds a carrier on the VFO at 700 Hz, through a filter
centred on the VFO, 500 Hz wide by default. A slow AGC brings SSB and CW
signals to the same level: it takes the gain down as soon as a signal
arrives and lets it come back at 10 dB per second. AM, SAM, SSB and CW
audio runs from 100 Hz to 5 kHz.

SAM, synchronous AM, is for AM stations that fade, as on shortwave. It
follows the station's carrier and detects the audio along it, so that
selective fading, which takes the carrier down below its sidebands and
makes AM's envelope distort, leaves the audio clear; it plays as loud as
AM. SAM takes AM's filter widths and comes after AM in the Radio menu and
with the M key. It looks for the strongest line within 1 kHz of the VFO,
so it needs no exact tuning, and once it holds it, follows it within 40 Hz
on each side, so that a sideband growing over a fading carrier does not
take its place. The line under the frequency says "locked" while it holds
the carrier, the same on every interface, and "no lock" otherwise: it
takes a carrier from 10 dB over the noise (within 80 Hz around it, over
half a second) and keeps it until it has stayed under 7 dB for a second
and a half, so that a carrier that fades for a moment does not lose it;
without one it plays the envelope, as AM does, and takes the carrier again
once it comes back, the switch fading over 5 ms. Its limits: one carrier, the strongest within
1 kHz, so a stronger station or heterodyne nearer the VFO takes the lock;
both sidebands, never one alone; and no lock on a weak carrier, where it
sounds as AM. SAM adds about a twelfth to the engine's processing of a
2.4 MS/s receiver.

The automatic notch, Radio > "Automatic notch", takes whistles out of the
audio, such as the heterodyne of a carrier beside an AM station or a tuning
tone on an SSB voice. It acts in AM, SAM, USB and LSB; not in CW, whose
signal is itself a steady tone, nor in NFM and WFM, where the box is greyed
with a note. It is off at first, shared by every interface as the mode is,
saved with the receiver's settings and kept when the mode changes; a
bookmark does not carry it. It looks ten times a second at the audio
before the squelch and the audio filter, and removes the lines between
100 Hz and 5 kHz that stand 15 dB or more over the audio around them, are
at most 30 Hz wide and have stayed for half a second: 45 Hz on each side of
each, through the audio filter, which the audio fades into over 5 ms
without a click. A whistle goes within about a second of its start, and
comes back a second after it stops; one that drifts slowly is followed.
The note under the box says how many lines it removes now, the same on
every interface, while an interface listens or a recording runs. Its
limits: it is automatic only, with no manual notch; it removes four lines
at most, those that stand highest over their surroundings first, and keeps
those it took while they last, even when stronger ones come; a whistle
crossed by a voice's harmonic during its first half second may take a
little longer; and a note held longer than half a second, in music or a
long vowel, is taken for a whistle while it lasts and for a second after.
The recordings take the notched audio. It
adds about 4% to the engine's processing of a 2.4 MS/s receiver.

The noise blanker, Radio > "Noise blanker", thins crackle: the short
impulses of ignition, electric fences, switching supplies and distant
lightning. It acts in every mode, on the receiver's samples before the
VFO's channel is made of them, where an impulse is still a few samples
long rather than spread over the channel's filter. It is off at first,
shared by every interface as the mode is, saved with the receiver's
settings and kept when the mode changes; a bookmark does not carry it. In
every block of samples, a thirtieth of a second, it zeroes the samples
whose magnitude is more than six times the block's median, with two
samples on each side. What is longer than 16 samples over that limit is a
signal, such as a strong carrier keyed on, and is left as it is, and so is
a block where it would zero more than 2% of the samples; a strong AM
station at full modulation, a broadcast FM station or a carrier that
fills the block never cross the limit. The note under the box says how
many impulses it zeroed in the latest second, the same on every interface,
at most once a second. The audio, its recordings, the S-meter and the
squelch take the blanked channel, so crackle no longer opens the squelch;
the spectrum, the waterfall, the signals found, the scans' search, the I/Q
recordings and Calibrate, its measure and its check of the driver, take
what the receiver sent, so that they show and measure the same with the
blanker on or off. Its limits: the
threshold is fixed, with no strength to set; it removes only impulses
shorter than a few samples at the receiver's rate, 16 at most, 7 µs at
2.4 MS/s, and those that stand six times over the median of everything the
receiver takes in, which a strong station elsewhere in the span raises; it
does nothing against steady noise, hiss or hum, nor against impulses
longer than that, such as a lightning crash; and a strong signal that
starts or stops within the last or first 16 samples of a block loses
those, with two more. It adds about an eighth to the engine's processing
of a 2.4 MS/s receiver while impulses come, less without them.

Frequencies are exact to the hertz. They show in MHz with as many decimals
as they have, from 3 to 6 (145.500, 145.6125, 7.074001); measurements keep
their rounding: the signals found and the History's channels to 100 Hz, the
readout under the pointer to 10 Hz. A double-click on the frequency, or a
tap on a touch screen, opens it whole for typing: Enter or leaving the
field tunes there, unless it is unchanged, and Escape cancels. Every
frequency field (the VFO's, a bookmark's, the range scan's range, the centre
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
20 kHz, and 1 Hz in a narrower view. Beside a set of numbered channels
([Band plan](#band-plan)), Automatic goes from channel to channel, wherever
they lie: a click lands on the nearest channel, and a step past the set's
last channel, or into a gap of more than two channels, moves by the spacing.
The line under the frequency names the known frequency or the channel the
VFO is on, else its range, and shows the step in use: "PMR446 · ch 3 ·
step 12.5 kHz". A
click, dragging the filter, the wheel, the arrow keys and the − and +
buttons land on the step's grid, and Shift with an arrow key steps ten
times as far; a typed frequency, a bookmark and a digit do not.

WFM plays broadcast FM in mono, through a filter 180 kHz wide by default,
from 50 to 250 kHz. Its audio has the de-emphasis of the band plan in use,
50 µs in Europe and 75 µs in the Americas ([Band plan](#band-plan)), and runs
from 40 Hz to 15 kHz, without the stereo pilot; it comes at 32 kHz, while
the other modes' audio comes at 12 kHz. The band plan gives FM broadcast
WFM, so tuning into 87.5 to 108 MHz (88 to 108 MHz in the Americas, 87 to 108 MHz in Asia-Pacific) selects it, with Radio > "Mode and step
from the band plan". Where the plan gives AM, SAM counts as AM: tuning
there in SAM keeps SAM.

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
the page loads; the Radio menu then says so. A speaker at the top right,
mirroring the menu's icon (under it in a window 420 pixels wide or less,
with a mouse), shows whether the page plays sound: crossed out
while the browser holds the audio or the volume is 0, with waves while it
plays. A click or a tap on it starts held audio, mutes playing audio by
setting the volume to 0, or brings the volume back. It sets the volume at
hand too, with a gauge under it while the volume changes: a press dragged up
or down follows the gauge, the whole range in 160 pixels, and a notch of the
wheel over it, or its Up and Down arrow keys, move the volume 5%. Brought
down to 0 that way, a click or a tap brings back the volume the gesture began
from. The Radio menu's Volume slider stays. On an iPhone, Safari keeps playing the audio
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

The view also goes past the span, whatever the receiver, the simulated band
included: zooming out holds at the span's width, then goes on out to the
whole band plan, from 0 Hz to its end, or to the end of the receiver's
reach where that is higher. Nothing is measured outside the span, and the
view shows no level there: the span stands out like a lit window whose
light fades away from it, the grid goes on, blurred, and the band plan's
colours tint the background. Beyond the receiver's reach the background is
hatched, under a band strip that stays readable. The readout says "outside
the span" or "out of reach" there. Seen from far, where the span would be
narrower than 3 pixels, a marker 2 pixels wide shows its place in the
spectrum and the waterfall. The frequency scale drops its decimals as the
view widens, down to whole megahertz.

Clicking outside the span, within the receiver's reach, tunes there
([RTL-SDR receivers](#rtl-sdr-receivers)); the simulated band, which does
not tune, reaches its span alone. Beyond the reach nothing tunes: a click,
a tap, a typed frequency, a bookmark, a signal found or a channel of the
History there leaves the VFO where it is, and the status line says why,
with the receiver's range. With a mouse, a tip says so before the click.
Right-clicking there, or a long press, disables Tune here and Ignore in the
scanner, and keeps Add to bookmarks, for another receiver, Centre the view
here and Zoom to the segment.

Within reach, with a mouse, an outline first shows where the span would go,
and a tip says what the click does and whether it stops a scan. Until frames come from
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

The waterfall draws a line per pixel row, newest at the top, and covers
the time its height shows. A handle on its right edge sets that time
without the menu: drag it down to show more time, or up for less, or
turn the wheel over it, 15% a notch; its Up and Down arrow keys do the
same, and a double click brings back the default, 25 lines a second.
Display's Time shown sets it too, from 10 seconds to 20 minutes. While
the time changes, the waterfall stretches the lines it holds; once it
stops, it draws them again at the new speed, each new line taking the
highest level of the lines it replaces, so that brief signals stay, and
a shorter time after a longer one leaves the past as coarse as it was. A
line that spans four spectrum frames or more keeps the highest level of
each bin among them, so that a slow waterfall keeps brief signals too; a
faster one keeps their mean, as before. The timestamps come every 10
seconds, or at a longer step, up to 10 minutes, without the seconds from
a minute up, when the time shown would crowd them. The speed is kept
with each interface's preferences: a taller window shows more time. On
a touch screen, the handle stops above the step button.

Around the VFO, the zoom goes on into a fine spectrum of the VFO's
channel, a few hertz per bin. The spectrum shows 8192 bins over the
receiver's span, 293 Hz each at an RTL-SDR's 2.4 MS/s, and its narrowest
view is 16 of them, 4.7 kHz (and never under 4 kHz). The engine also
computes, before the demodulator, 8192 bins over the channel's 48 kHz,
5.9 Hz each (320 kHz and 39 Hz in WFM), about ten times a second, the
samples as received whatever the noise blanker; 19.2 kHz on each side of
the channel's centre show the air (128 kHz in WFM), the rest the channel
filter's edge. The channel's centre is the VFO, or 700 Hz below it in CW.
Once the view is narrower than 48 bins of the spectrum, 14 kHz at 2.4 MS/s,
and lies within those 19.2 kHz, the interface asks for the fine spectrum,
and the spectrum and the waterfall show it in the same place, down to 16
of its bins: 94 Hz (625 Hz in WFM). The view goes back to the spectrum
once it is 72 bins of the spectrum wide, 21 kHz, or the fine spectrum
shows less than half of it, and the interface stops asking. Zooming in
elsewhere stops at 4.7 kHz, and a narrower view dragged towards the edge
of the fine spectrum stops there. Display gives the spectrum shown and its
hertz per bin.

The fine spectrum's edges look like the span's, a lit window, and nothing
is drawn beyond them: the readout says "beyond the fine spectrum" there,
and gives the frequency to the hertz once a pixel spans less than 10 Hz.
Until its first frame comes, about a fifth of a second after the zoom, the
spectrum stays on show. Its waterfall is its own, apart from the
spectrum's, which goes on: it starts empty at each zoom in, follows the
VFO's small moves, starts again after a longer one, and pauses rather
than stretch while no frame comes, as while the VFO moves by drag or
wheel. The fine spectrum needs a receiver as fast as the channel (320 kHz
in WFM), and an engine as recent as the interface; an older one keeps the
zoom at 4.7 kHz. A hidden page asks for none. A device paired through
remote access gets 2048 bins, 23 Hz each, each the strongest of the four
it groups, about 0.17 Mbit/s before compression, and zooms in down to
375 Hz ([Remote access](#remote-access)).

## Scanners

The engine looks for signals in the spectrum it computes, whatever each
interface shows, and every interface lists the same ones: the Scan menu's
Signals tab gives each signal's frequency, how far it rises above the
noise around it and when it was last found. A signal is found when it rises
at least the threshold above that noise (12 dB by default, from 6 to 50 dB)
over a third of a second, twice in a row, away from the outer tenth of the
span on each side and from the 3 kHz on each side of the centre, where a
receiver's DC spike shows. The list runs by frequency, the lowest first,
so that a signal keeps its place as others come and go: those found within
the last 1.5 s show as active. It shows 50 signals at most: the active
ones, the strongest of them if more than 50 are active, then the latest
found. The list keeps the signals of the spans the receiver left, and
empties when detection is turned off or another receiver is chosen. In a
channelized range of the band plan, a signal shows on its channel, and the
signals found on one channel share its row, as a broadcast station's
narrow and wide signals do: the row shows the strongest active one, or
else the one found latest, and its Ignore leaves them all out. In the FM
broadcast band, every signal shows on its nearest channel, however narrow:
a station whose sound goes quiet narrows as it does. A station there keeps
one row: the parts of its spectrum that show apart, up to 90 kHz from its
strongest, half the WFM filter's width, count for it, so that the range
scan listens to it once and the History keeps one channel for it.

A click on a frequency tunes to it. Ignore leaves its frequency out of the
list, for every interface, until Restore brings the ignored frequencies
back, with the signals found later on its channel, a station's narrow
ones included; in the FM broadcast band, it leaves out the whole station,
90 kHz on either side of it, half the WFM filter's width, as a station's
signals may split across it. The spectrum's context menu ignores a
frequency too. Whether detection
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
and 6 s with four. Under the scan's button, the menu says so whenever the ticked
bookmarks take several windows: how many, how long a tour takes, the
shortest transmission it cannot miss, and the width that bookmarks must
fit within to take one window, which the scan watches without a break. While every ticked bookmark within the receiver's reach
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

The Range tab's range scan runs in the engine too, the same for every
interface. It searches a range, 430 to 440 MHz by default, pass after
pass, with detection's threshold, which the Range tab shows too: the
signals found there join the Signals list and the History. "Current
window" sets the range to the part of the receiver's span that detection
searches, its centre frequency plus or minus 0.4 times the sample rate,
within it to the kHz. A range within that part of the span, when the scan
starts, takes that one window and never tunes the receiver: it runs with
any source, the simulated band included. A wider range needs a receiver
that tunes, such as an RTL-SDR: the scan steps it across the range, window
after window.
The windows lie a quarter of the sample rate apart at most, the first and
the last a tenth of the rate inside the range, so that every frequency of
the range lies in the part that detection searches of a window, even one at
the centre of another window, where the DC spike shows. Once the
receiver's samples come from a window, the scan stays there the time per
window (0.5 s by default, from 0.25 to 2 s), long enough for detection to
find a signal there twice in a row, then asks for the next one; it also
moves on when, 2 s after asking for a window, the samples still come from
elsewhere. At 2.4 MS/s, a pass of the 17 windows of 430 to 440 MHz takes
about 16 s, the receiver taking 0.4 s to settle at each. The spectrum and
the waterfall show each window, and the VFO stays inside it; the waterfall
keeps the lines of the windows before it at their frequencies, where they
overlap it ([Spectrum view](#spectrum-view)). Passes are endless by
default, until the scan is stopped; with Endless unticked, the scan stops
by itself after the number of passes set, from 1 to 99, and brings the
receiver and the VFO back where they were when it started.

With "Stop to listen", once a window's searches end the scan takes the
signals active there within its range as the window's queue (a window
reaches a little past the range's ends: the signals there are left out)
and listens to each in turn, the strong and the weak alike, with the VFO
alone, then goes on to the next window. It tunes the VFO to the signal
that waited longest, one it
never tuned to first, the lowest of them, then the one it tuned to least
recently, so that each takes its turn; the VFO lands on the frequency the
list shows, in the mode that the band plan gives there, as the History's
Bookmark button does (NFM on a 2 m repeater, USB in the SSB part, AM in
the airband, WFM on broadcast FM), taking that mode's usual filter width
when the mode changes, and keeps its mode where the plan gives none. It
stays while the squelch opens within a quarter of a second and stays open,
then waits "Resume after silence" once it closes, as the memory scan does.
After "Listen at most" (10 s by default, from 2 to 60 s), it leaves the
signal for another one of the queue, even while the squelch is open, so
that a signal on the air all the time does not hold the scan; once each
signal of the queue was tuned to, it goes on. A signal heard in one
window is not queued again in the windows that overlap it during the same
pass, and a signal found after a window's searches waits for the next
pass. The Range tab says the pass, the window and the signal it is on,
Signals' list marks that signal, and the VFO's marker moves on the
spectrum of every interface. One VFO hears one signal at a time, and while
the scan listens, the other windows are not searched: a short transmission
may end before its turn comes, but it still joins the list and the
History. Ignoring the signal it listens to moves it on at once. Listening
needs detection: the scan does not start while detection is off, and
stops when it is turned off.

Any interface starts and stops the range scan, a paired device's
included, and every interface shows where it stands; their status lines
add "range scan" while it runs. One scan runs at a time: starting the
memory scan or the range scan stops the one that runs. A range wider than
the current window starts only when the receiver tunes to every window of
the range. The scan goes on with no interface open until an interface
stops it, which brings the receiver back to its centre frequency and the
VFO where they were when it started, unless it is on a signal it listens
to, where they stay; a receiver that refuses a window and the passes set
bring them back too. Tuning by hand from any interface (the VFO, the
centre frequency or the sample rate) or choosing another receiver stops
it, leaving the receiver where it is. While it runs, the receiver's
settings saved are those it comes back to, and the memory scan's windows
are those it would take from that span, where a memory scan started then
begins. A new range starts a running scan again from its first window.
Whether it runs is never saved: none runs at start, unless `--scan sweep`
starts it with its settings, or `--scan signals` over the current window,
stopping to listen, without end, its settings left as they are ([Without a
window](#without-a-window)). Its range, time per window, passes,
listening, longest listening time and resume time are the same for every
interface and kept for the next start.

Each range scan is kept, under the name typed in the Range tab's Name
field when it starts (60 characters at most, on one line), or else under
its range and start, as "430.000-440.000 MHz, 2026-11-20 14:05". A kept
scan holds its start and end, its range, whether it stopped to listen, the
passes set and done, and why it ended; and each channel it found within
its range, on the frequency the Signals list shows it on: when it was
first and last found, how many searches found it, its highest level above
the noise and, when the scan listened, how long the squelch stayed open
there. A signal found within 1.5 kHz of a channel counts for it. Nothing
else is kept: no audio and no samples. The engine keeps 100 scans at most,
the oldest leaving first, and 1000 channels in each, the least recently
found leaving first; a scan on the simulated band is not kept.

The Range tab's "Past scans" lists them, the latest first, the running
one included, with their start, range and how many channels each found,
for every interface, a paired device's included; while a scan runs, the
list follows it at most every 10 s. A click on a scan opens its channels,
by frequency: a click on a frequency tunes to it, its tooltip giving when
it was first and last found, and Bookmark adds a bookmark there with the
band plan's mode, as in the History. Rename gives the scan another name,
and Delete forgets it, once it no longer runs; "Clear all", under the
list, forgets every scan but the running one. Delete and Clear all
forget for every interface, and each waits for a second press within 5 s.
A scan that ran when Radiolack stopped ends there; one that ran when the
computer lost power comes back as interrupted, at its latest channel
found.

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

An I/Q recording keeps the receiver's samples as the receiver gives them,
before Radiolack removes their DC offset or converts them, in
[SigMF](https://sigmf.org) files: Inspectrum and SDRangel open them, and
GNU Radio's File Source reads their data. The `.sigmf-data` file holds
the I/Q pairs, I first, in the receiver's format, which the metadata's
`core:datatype` names:

| Receiver | Format | 2.4 MS/s | 8 MS/s |
|---|---|---|---|
| RTL-SDR through rtl_tcp | `cu8`, 8-bit unsigned | 4.8 MB/s, 17 GB an hour | |
| SoapySDR: RTL-SDR, HackRF | `ci8`, 8-bit signed | 4.8 MB/s, 17 GB an hour | 16 MB/s, 58 GB an hour |
| SoapySDR: SDRplay, Airspy, bladeRF | `ci16_le`, 16-bit signed | 9.6 MB/s, 35 GB an hour | 32 MB/s, 115 GB an hour |
| SoapySDR: any other | `cf32_le`, 32-bit floats | 19.2 MB/s, 69 GB an hour | 64 MB/s, 230 GB an hour |

(`_be` instead of `_le` on a big-endian computer.) A receiver whose
integers stop short of their type's full scale, such as an SDRplay's or
an Airspy's at 32767 or a bladeRF's 12 bits at 2048, has that full scale
in the metadata's `radiolack:full_scale` field, which the metadata declares
as an optional extension: divide the samples by it for levels in full-scale
units. The samples themselves are never scaled. The `.sigmf-meta` file
beside the data gives the sample rate, the receiver and the captures: where
each starts in the data, its centre frequency and its start in UTC. Behind
a [converter](#converter), the receiver's description names it, with its
oscillator's frequency, and the centre frequencies are those on the air;
behind an inverted one, I and Q are swapped, so that the data too holds
the band as it is on the air. A retune or a new frequency correction
starts a new capture, as does a lost rtl_tcp or an unplugged SoapySDR
receiver that comes back, or samples that the receiver or the computer
lost; the samples that come while the receiver settles, for 0.4 s after a
change, are left out, as from the spectrum. A new sample rate, another
receiver, another rtl_tcp address or another converter ends the
recording. Its files are named after its start in
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

A recording stops by itself when its next write would leave less than
2 GiB free on its disk, which it checks before each write, an audio
recording when its file reaches the WAV format's limit of 4 GiB (about 49
hours, or 18 in WFM), and either on a disk error, such as a disk too slow
for it: an I/Q recording writes from 4.8 MB/s (8 bits at 2.4 MS/s) to
64 MB/s (32-bit floats at 8 MS/s), in writes of 4 MiB, and stops once
more than 2 s of samples, and at least 64 MiB, wait for the disk. The menu
and the status line then say why, as does Radiolack's output.

**Law.** Local law may forbid recording what some radio services carry,
such as air traffic, emergency services or private networks, or disclosing
it: check the rules where you listen before you record.

## Settings and bookmarks

Radiolack keeps its settings and bookmarks in `~/.config/radiolack`
(`$XDG_CONFIG_HOME/radiolack` when that variable is set):

- `settings.json` holds each receiver's settings: the Receivers menu's and the
  demodulation settings (VFO, mode, filter width, squelch, automatic notch,
  noise blanker). The simulated
  band and each receiver plugged into this computer, by index or serial
  number, have their own; rtl_tcp has one set whatever its address, which is
  saved with them. It also records the source in use, for the next start,
  and the scanners' settings: whether detection is on, its threshold, the
  ignored frequencies, the memory scan's and the range scan's resume
  time, and the range scan's longest listening time, range, time per
  window, passes and listening ([Scanners](#scanners)), and the band plan
  in use ([Band plan](#band-plan)), and the names given to the devices
  ([Several receivers](#several-receivers)). It also holds the own
  settings of this computer's interfaces: the display, waterfall and
  overlay settings, the volume, the tuning step, the band plan's choice of
  mode and whether the spectrum marks the signals found. Last, it records whether Radiolack checks for updates at start
  ([Updates](#updates)) and the recordings folder chosen in Settings
  ([Recording](#recording)).
- `bookmarks.json` holds the bookmarks and, once one is made, their
  categories, listed beside the bookmarks: an older Radiolack reads the
  same bookmarks, and loses only the categories when it saves them.
- `observations.json` holds the channels of the History
  ([Scanners](#scanners)). It reaches the disk at most every 10 minutes, a
  few seconds after a Clear, and when Radiolack stops.
- `scans.json` holds the kept range scans and their channels
  ([Scanners](#scanners)). It reaches the disk when a scan starts or ends,
  is renamed or deleted, every 10 s while a running scan finds channels or
  listens, and when Radiolack stops.
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

Bookmarks sort into categories, one level deep. "New category" opens a
field for its name, up to 40 characters and unlike any other category's,
whatever the case: Enter keeps it and Escape leaves it. Once a category
exists, the list shows the uncategorized bookmarks first, under
Uncategorized, then each category by name, its bookmarks indented under
a heading that shows how many it holds and hides or shows them on a click;
what each interface hides is its own, and not saved. The heading's box
ticks all its bookmarks for the memory scan, or unticks them all when all
are ticked, and shows a dash when some are. Rename opens the name as
New category does, and Remove asks to confirm; its bookmarks then become
uncategorized. A bookmark moves in three ways: dragged with the mouse onto
a category's heading, or Uncategorized's, which asks "Move … to …?" with
Confirm and Cancel; by the Category choice that Edit opens beside its name
and frequency, which moves it at once; or in the Select mode, where each
row's box selects a bookmark, and the bar under the toolbar moves the
selected ones to the category chosen when Move is pressed. Done ends the
Select mode. The filter keeps the headings where a bookmark matches, and
shows all the bookmarks of a category whose name matches.

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
Radiolack on the Home Screen. An iPhone with iOS 26.6.1 shows it there as
the letter R rather than Radiolack's icon: the part of iOS that makes the
Home Screen icon refuses Radiolack's certificate, which Safari trusts, so
it never gets the icon. Opened from the Home Screen as a web app,
Radiolack runs apart from Safari: it asks for the code once more, then
stays paired too.

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
coarser: 18.75 kHz wide at 2.4 MS/s, instead of 4.7 kHz, and 375 Hz
instead of 94 Hz around the VFO, where the fine spectrum shows
([Spectrum view](#spectrum-view)). On a slower link,
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

`--scan memory` or `--scan sweep` starts the memory scan or the range scan
at each start, with its saved settings, as an interface starts it, and
`--scan signals` the range scan over the current window, stopping to
listen to each signal, without end, so that after a power
cut or an upgrade the computer goes on scanning, and the History fills,
without any interface:

```bash
radiolack --ui none --rtl-sdr --scan memory
```

The receiver then opens at start, not once an interface opens. When the
scan cannot start, as the memory scan without a ticked bookmark within the
receiver's reach, a range wider than the current window with a source that
does not tune, or listening with detection turned off, Radiolack
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

The service's command leaves out `--bias-tee`: for an active antenna that
needs the receiver's power, add it there.

With several receivers ([Several receivers](#several-receivers)), give
each in the service's command, an RTL-SDR by its serial number, which the
Receivers menu's device list shows: the receivers given at start replace
the receivers used last, so a receiver added from an interface lasts until
the service restarts. `--scan` and `--bias-tee` apply to the first receiver
given:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/radiolack --ui none --rtl-sdr 00000001 --rtl-sdr 00000002 --remote --port 8080 --scan memory
```

Each radio after the first runs in a process of its own, under the
service's user and within its sandbox; stopping the service stops them
all, each releasing its receiver first. Two receivers at 2.4 MS/s, each
with a listener, took 56% of one core of a Core i7-7500U in all (spike 9
in [docs/design/scoping.md](docs/design/scoping.md)), and each radio added
takes 56 MB of memory before it reads a sample. Several receivers are not
yet checked under the service.

For a receiver read through SoapySDR, install SoapySDR's packages first
([SoapySDR, for other receivers](#soapysdr-for-other-receivers)), then put
`--soapy ARGS` in place of `--rtl-sdr`, with the receiver's device
arguments, its serial number too when several receivers of its driver are
plugged in, and restart the service:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/radiolack --ui none --soapy driver=hackrf --remote --port 8080
```

Without `python3-soapysdr`, `--soapy` exits, saying which package to
install, and systemd tries again every 10 s. The service with a SoapySDR
receiver is not yet checked on a receiver.

- The service's user opens the receiver as it opens an RTL-SDR: through
  the plugdev group, when the driver's device rules give the receiver to
  plugdev. In Debian 12's packages, an RTL-SDR's and a HackRF's rules do,
  and a LimeSDR's let every user in; an Airspy's, an Airspy HF+'s and a
  bladeRF's give the receiver only to the user sitting at the computer
  (`uaccess`), which the service's user never is. For those, add a rule of
  your own that gives the receiver to plugdev, with the vendor and product
  of the driver's rule in `/usr/lib/udev/rules.d` (or `/lib/udev/rules.d`),
  for an Airspy in `/etc/udev/rules.d/70-radiolack-airspy.rules`:

  ```text
  SUBSYSTEM=="usb", ATTR{idVendor}=="1d50", ATTR{idProduct}=="60a1", MODE="0660", GROUP="plugdev"
  ```

  then `sudo udevadm control --reload` and plug the receiver in again.
- The sandbox lets the service reach USB devices (`DevicePolicy=closed`
  with `DeviceAllow=char-usb_device rw`), which a driver that opens its
  receiver itself through libusb, as the modules above do, needs.
- An SDRplay is not checked as the service, and may not work there: its
  driver talks to SDRplay's own service, which opens the receiver, by
  means that Radiolack has not read, as SDRplay's API is closed. An
  administrator who tries it would look first at the settings that may
  stand in the way: `DynamicUser` (a user made at each start, and the
  `RemoveIPC` it implies), `PrivateTmp`, `RestrictAddressFamilies`,
  `SystemCallFilter`, `MemoryDenyWriteExecute` and `DevicePolicy`, and
  would start the service after SDRplay's own (`After=`), loosening only
  what the journal shows failing, with `systemctl edit`.

It runs as a user of its own, which systemd makes at each start
(`DynamicUser`), in the plugdev group that an RTL-SDR's device rules let
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
views' WebGL2, the window's size and the audio it lost since it loaded
(the times playback ran dry, its frames late or paused, as a retune pauses
them, the seconds that the engine dropped for it, the computer or the
network behind, and the seconds it skipped, the device behind); the
receiver's kind and state, with its
tuner, its shortwave input and sample rate but neither its address nor its
frequency; how long
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

The strip under the spectrum shows what each range is used for, from 0 Hz to
10.5 GHz: broadcasting, aviation, shipping, mobile networks, satellites,
short-range devices, radar, amateur bands and more, with nothing allocated
below 8.3 kHz. Inside the amateur bands, from 2200 m to 3 cm, it follows
the IARU band plans of its region, a few segments per band. Hovering the strip names a
range and its edges, and clicking it zooms to the range. Zoomed out, where a
band's ranges would be too narrow to read, the band shows as one block of
its colour, named in full, by a short name or not at all as room allows,
and clicking it zooms to the band. With Radio > "Mode
and step from the band plan", tuning into a range selects its usual mode,
SAM staying where the plan gives AM.
The Automatic tuning step takes the channel spacing of the ranges that have
channels ([Demodulation and audio](#demodulation-and-audio)).

The plan also numbers channels and names known frequencies. Europe's
channel sets are those that a regulation or the IARU Region 1 plans number
across Europe: CB, PMR446, maritime VHF, the amateur FM and repeater channels of
10 m, 6 m, 2 m, 70 cm and 23 cm, the DAB blocks of Band III, the UHF
television channels and DECT. The Americas' are CB, FM broadcast, maritime
VHF, and FRS and GMRS; Asia-Pacific's is maritime VHF. The known frequencies are the distress and
safety frequencies, the time signals and standard frequencies, the radio
astronomy lines, Galileo's carriers and the amateur centres of activity and
calling frequencies, and in the Americas MURS and the weather radio
frequencies. No broadcaster, operator or local repeater is named. Zoomed in, the strip draws a set's channels as ticks along
its lower edge once they lie 6 px apart, with their numbers once they lie
28 px apart, and each known frequency as a small mark along its upper edge,
with its name where it fits; the ranges' names come first, then the known
frequencies', then the numbers. Hovering a tick or a mark names it, and
clicking or tapping it tunes there, in the known frequency's or the set's
mode, within the receiver's reach: the marks answer in the strip's upper
half, the ticks in its lower half, and a name or a number anywhere it is
drawn; elsewhere the click zooms to the range. Display > "Channels and
known frequencies" hides the ticks, the marks and their names; the line
under the frequency still names them. A bookmark added on a channel or a
known frequency takes its name.

Bookmarks > Plan names the band plan in use and chooses another, for every
interface at once, as the bookmarks are; Radiolack remembers it. The plan
gives the strip, the modes and steps, the channels that the signals found
show on, the stations of the automatic calibration and WFM's de-emphasis.
Four plans so far: Europe (ITU Region 1), used unless another is chosen;
the Americas (ITU Region 2), whose FM broadcast has 75 µs of de-emphasis,
with the IARU Region 2 band plan in the amateur bands, 1.25 m and 33 cm
among them, and medium wave by 10 kHz; Asia-Pacific (ITU Region 3), with
the IARU Region 3 band plan in the amateur bands, 80 m up to 3900 kHz, 40 m
up to 7200 kHz and 2 m up to 148 MHz, FM broadcast from 87 MHz and maritime
VHF as its one channel set; and France (metropolitan), which is
Europe's changed where France's
national table of frequency allocations differs: no 4 m or 9 cm amateur
band, 160 m up to 1850 kHz, Band III up to 225 MHz, and a few utility
ranges. Below 30 MHz, where signals cross borders, the parts of a band
that France does not allocate but its neighbours use stay, as segments
named "outside France".

The Bookmarks menu lists the ranges in view, each with the known
frequencies inside it; a click on a range frames it, and a click on a known
frequency tunes there in its mode. Its filter also searches the whole
plan's ranges, channel sets and known frequencies, and a click on a set
frames it. The list holds at most 200 rows: zoom in or refine the filter
for the others. Display > "Band plan" hides the strip.

A thin bar above the strip, the overview bar, shows the whole plan at
once, from 1 kHz to its end on a logarithmic scale, seven decades, in the
bands' colours, with a tick at each decade: 10 kHz, 100 kHz, 1 MHz and on.
The bands within the receiver's reach are brighter; a light box marks the
span, a bracket the view and a blue line the VFO. Hovering the bar names
the band under the pointer and its edges; clicking or tapping it frames
that band, whatever the view, and dragging along it frames each band it
passes, the last one staying framed. Narrow bands stay narrow there: on a
bar 1280 px wide, 40 m takes about 2 px, which the tooltip, or a drag,
finds. The bar is 10 px high with a mouse and 14 px on a touch screen.
Display > "Overview bar" hides it, and it hides with the strip. Nothing
below 1 kHz shows on it, though the view still starts at 0 Hz; the
spectrum and the waterfall stay linear.

The plan is a detailed guide, not a legal reference: Europe's is written by
hand from the ITU Radio Regulations, the CEPT's European table and the IARU
Region 1 band plans, France's from Europe's and the ANFR's national table
(TNRBF), the Americas' from the Radio Regulations' Region 2 column and the
IARU Region 2 band plan, with its channel sets and some steps from the
United States' rules, which are national, Asia-Pacific's from the Radio
Regulations' Region 3 column and the IARU Region 3 band plan; each is read
a second time
against its sources. National tables
differ and are the reference, and local law may
forbid listening to some services or disclosing what they carry. Its format,
its sources with their editions, and what could not be checked are in
[docs/design/band-plan.md](docs/design/band-plan.md).

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
never yours; they record only the fake rtl_tcp and synthetic signals.
SoapySDR receivers are tested against a stand-in for SoapySDR's Python
module (`tests/soapysdr/SoapySDR.py`): within the tests, `import SoapySDR`
finds the stand-in or nothing, never the real module, and a test fails if
anything asks the stand-in for a transmit direction. Where
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
its keys and its layout from a phone to a computer, the Receivers menu and
its list, a change made in one interface showing in another, the bookmarks,
their names and a click that tunes, the S-meter's squelch, the signals
found, the memory scan, in the span and
across windows, the range scan, over the current window and across
windows, listening and its passes, the named scans kept, the History, the recordings, their downloads and
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
  engine.py     holds the radios and what they share with the
                interfaces: the bookmarks, the band plan, the
                observations, the kept scans, the preferences, the update
                check's findings and remote access's state; saves the
                settings and the observations
  radio.py      one receiver's radio: reads its source, removes its DC
                offset, shares spectrum frames, the demodulation
                settings, the signals found and the recording state,
                sends audio and the fine spectrum to those that ask, and
                records the audio and the receiver's raw I/Q
  recordings.py the audio's WAV files and the raw I/Q's SigMF files, each
                recording written by a thread of its own, their folder and
                its list
  scanners.py   detection: the signals in the receiver's span, their list
                and the ignored frequencies; the memory scan, its windows
                and its visits; the range scan's windows, passes and
                listening, and its turns among a window's signals
  bandplan.py   the band plan's modes and channels, as the engine reads
                them for the range scan and the lists of signals
  observations.py
                the channels where signals were found, for the History
  keptscans.py  the range scans kept under their names, with the channels
                each found
  remote.py     remote access: access code, paired devices and their
                interface settings, local authority and certificates,
                addresses to listen on
  store.py      settings, bookmarks, observations, kept scans and remote
                access files
  bookmarks.py  bookmarks, their categories and their checks
  preferences.py
                the interfaces' own settings and their checks
  demod.py      one VFO: channel level, squelch and the audio of each mode
  dsp.py        FIR filter design and FFT filters that shift and resample
  fine.py       the fine spectrum of the VFO's channel, for the deep zoom
  rtldevices.py the RTL-SDR receivers plugged in, read from sysfs without
                opening them, with librtlsdr's table of their USB IDs
                (GPL-2.0-or-later)
  rtltcp.py     RTL-SDR receivers through rtl_tcp, run by Radiolack or by you,
                an R820T's or R828D's grid, and their raw samples for an I/Q
                recording
  soapy.py      receivers through SoapySDR: device arguments, what the
                device offers, its gain stages, sockets, settings and
                antenna power, the reading thread and its buffer, and the
                raw samples for an I/Q recording
  soapysearch.py
                the search for SoapySDR devices, one listed driver at a
                time, for the Receivers menu
  r82xx.py      rtl-sdr's two computations of an R820T's PLL, ported from
                rtl-sdr (GPL-2.0-or-later), and the tuner's grid
  converter.py  a converter in front of a receiver: frequencies on the air
                and for the receiver
  correction.py the frequency correction in hundredths of ppm, split between
                the receiver and Radiolack
  carrier.py    the measure of a carrier, to a tenth of a hertz, for the
                calibration
  sources.py    opens each kind of source, for the device list and at start
  spectrum.py   display FFT, and the noise level around the VFO
  synthetic.py  simulated 2 m band
  updates.py    update check: asks the APT repository for a newer version
  report.py     problem reports: the latest warnings and errors, kept in
                memory, and GitHub's page for a new issue
  window.py     desktop window (GTK 3 and WebKitGTK), which saves downloads
                and opens GitHub's page for a new issue in the web browser
  web/          interface: ES modules served as they are, no build step
tests/          tests without hardware
tools/          measure_pll.py: measures how well Radiolack labels an
                R820T's samples; measure_listening.py: the cost of each
                listening tool; measure_rates.py: the sample rates the
                engine holds; check_search.py: whether a SoapySDR search
                stays off the network; measure_reader.py: whether a
                Python thread reads a SoapySDR receiver without losing
                samples; measure_radios.py: several radios in one
                Radiolack; run from a checkout
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
you are: the band plan follows a simplified regional table. They are not
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
