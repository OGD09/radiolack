# Radiolack rewrite: scoping

Status: v0 merged into `main` on 2026-11-10 (started on the `rewrite` branch
on 2026-11-08).
Visual reference: [`ui-mockup.html`](ui-mockup.html), validated on 2026-11-08
(simulated data, no receiver, no audio).

This document fixes what the rewritten Radiolack is, what it is not, how it is
built and in which order. The first milestone replaced the Gqrx-driven airband
tool on `main`; that tool stays in the history at commit `f0e1cf6`.

## Purpose

Radiolack becomes a standalone, receive-only SDR program for every frequency
its receiver covers, with a ham-focused interface: a band plan strip names
what each range is used for, with detailed plans inside the amateur bands. It
reads I/Q samples from an SDR receiver, shows a full-screen spectrum and
waterfall, demodulates and plays the signal, and scans the bands for
activity. It runs as a desktop application and can let a phone, a tablet or
another computer control it and listen to it over the local network. It
depends on no other radio program, only on receiver drivers.

## Decisions

| Date | Decision |
| --- | --- |
| 2026-11-08 | Standalone program for the amateur bands, inspired by SDR++, SDR#, SDRangel and OpenWebRX+. No dependency on Gqrx or any other radio program; receiver drivers (rtl_tcp, librtlsdr, SoapySDR) are fine. |
| 2026-11-08 | Receive-only. Transmit stays out of scope until the owner holds an amateur licence, and would then need a decision of its own. |
| 2026-11-08 | Same repository, rewritten from scratch on the `rewrite` branch. The name "Radiolack" shows discreetly in the top-left corner of the UI. |
| 2026-11-08 | Spectrum and waterfall fill the screen; discreet buttons open panels for fine-tuning; many display options; peak scanner and sweep scanner. |
| 2026-11-08 | Desktop application. A Settings option turns on a built-in web service so a phone, a tablet or another computer can control it and receive its audio. |
| 2026-11-08 | Phones are first-class remote clients (touch, portrait and landscape). No receiver is ever plugged into a phone. |
| 2026-11-08 | Maximum compatibility: the owner uses an iPhone, but every mainstream browser and the three desktop systems are targets. |
| 2026-11-08 | Making the program available to the public is the long-term goal: distributing it so that anyone can install it at home. Opening a receiver to anyone on the internet (a public WebSDR) is not a goal. |
| 2026-11-08 | The UI mockup is the visual reference. It can still change deeply. |
| 2026-11-08 | Desktop shell for v0: the system's WebKitGTK through PyGObject, validated by spike 1. Windows and macOS shells are chosen before v4. |
| 2026-11-09 | UI code: plain JavaScript ES modules served as they are, with no build step. Types are written in JSDoc comments and checked by TypeScript; only that check needs Node. Revisit if the UI needs a framework or npm libraries. |
| 2026-11-09 | English only: code, identifiers, comments, documentation and the interface. No other language and no translation layer; this replaces the English and French interface planned on 2026-11-08. |
| 2026-11-09 | All frequencies: Radiolack tunes and labels the whole range of its receivers, not only the amateur bands. The band plan covers 9 kHz to 6.425 GHz with a simplified European table (ITU Region 1 allocations named by their main use) and simplified IARU Region 1 plans inside the amateur bands ([`band-plan.md`](band-plan.md)). This replaces "for the amateur bands" (2026-11-08) and answers the open question on tuning. |
| 2026-11-09 | Browser tests (Playwright: Chromium, Firefox and WebKit) run headless and only on request (`RADIOLACK_BROWSER_TESTS=1`), as CI does; the usual test commands launch no browser. |
| 2026-11-09 | Persistence: the engine saves everything, in `$XDG_CONFIG_HOME/radiolack` (`settings.json` and `bookmarks.json`, each with a format version), including the interface's own settings: the page's origin, and with it the browser's storage, changes with the port at each start. Receiver settings are saved per receiver (the simulated band, each `--rtl-sdr` device, each `--rtl-tcp` address). Until remote access, one set of interface settings is kept, that of the interface that changed one last, which the next interface opens with; open interfaces keep their own. v1 keeps them per device. Remote access is never saved. A file Radiolack cannot read is set aside, never overwritten. |
| 2026-11-09 | rtl_tcp address: the address is only the way to the receiver, and changes with the networks the computers are on. The user gives the address that works where they are, on the command line or in the Source panel, and Radiolack says when it does not answer; it does not try several addresses by itself. One set of rtl_tcp settings is kept whatever the address, with the address used last, which `--rtl-tcp` without an address reopens. This replaces "each `--rtl-tcp` address" in the persistence decision above. |
| 2026-11-10 | Source choice: the Source panel's Receiver list switches between the simulated band, a receiver plugged into this computer and an rtl_tcp server while Radiolack runs; each keeps its own settings. Without `--rtl-sdr` or `--rtl-tcp`, Radiolack opens the source used last, the simulated band the first time. |
| 2026-11-10 | Installation: each system gets its usual installer. The Debian package comes first, for Debian 12 or later and Ubuntu 24.04 or later, on the system's Python and libraries, with a menu entry and an icon; the owner's v0 check uses it. The macOS (.dmg) and Windows (.msi or .exe) installers come with their window shells, before v4. |
| 2026-11-10 | Updates: at start, Radiolack says when a newer version is out, with a button that installs it and restarts Radiolack; Settings checks on demand and can turn the check at start off. On Linux the update comes from a signed APT repository and upgrades Radiolack only, once the system has asked for the administrator's password. The button shows in the local window only, never through remote access. The check is the only connection Radiolack makes besides the receiver and its clients, and carries no identifier. It needs published packages: see the publication decision below. |
| 2026-11-10 | Licence: GPL-3.0-or-later, copyright OGD09. It is the licence of most SDR software (Gqrx, SDR++, SDRangel, GNU Radio), whose code Radiolack can then reuse; anyone who distributes a modified Radiolack must publish its source under the same licence. Every dependency fits it (aiohttp: Apache-2.0; NumPy: BSD; PyGObject, GTK and WebKitGTK: LGPL), and so do GPL drivers that installers may carry, such as librtlsdr. Set aside: the AGPL, OpenWebRX's choice, whose duties for versions used over a network Radiolack's private remote access does not call for, and permissive licences, which allow closed versions. |
| 2026-11-10 | Publication: the repository becomes public. Updates need packages that Radiolack can fetch without the owner's credentials, and the packages hold the Python code anyway. The history was audited first (2026-11-10) and stays as it is: rewriting it would not remove the commits GitHub keeps for pull requests. The owner switches the repository's visibility. |

## Scope

In scope, across the milestones below:

- Receivers: RTL-SDR through rtl_tcp first, then SoapySDR devices.
- Frequencies: the whole range of the receiver; the band plan covers 9 kHz to
  6.425 GHz.
- Display: spectrum and waterfall with zoom and pan, palettes, averaging, peak
  hold, a band plan strip and bookmarks on the spectrum.
- Reception: one VFO (NFM, AM, USB, LSB, CW) with filter width, squelch and
  AGC. More VFOs only if a later need shows up.
- Scanners: peak detection, sweep across several receiver windows, memory scan
  over bookmarks.
- Band plans: Europe first (ITU Region 1, with the IARU Region 1 amateur
  plans), then the other regions.
- Remote access: control and audio over the local network.
- Language: English only.

Out of scope:

- Transmitting, in any form.
- Recording audio, transcriptions or raw I/Q (current repository rule; see the
  open questions).
- Receivers attached to phones, and native mobile apps.
- Opening a receiver to anyone on the internet (a public WebSDR).
- Driving other radio programs.

Dropped from the current code: the VHF airband explorer and its Gqrx bridge.
Their migrated history in `data/` stays untouched; it is never written,
replaced or deleted. What the new engine reuses from that code is summarised
in [`lessons.md`](lessons.md).

## Compatibility targets

| Area | Targets |
| --- | --- |
| Desktop app | Linux x86_64 first. Windows 10/11 and macOS (Apple silicon and Intel) for the public release. Proposed later: a headless mode on Linux arm64 (Raspberry Pi next to the antenna), used only through the web UI. |
| Remote clients | Safari on iOS and iPadOS, Chrome, Edge and Firefox on desktop and Android, Safari on macOS: current and previous major versions. |
| Receivers | RTL-SDR (R820T and R828D tuners, RTL-SDR Blog V3 and V4) first, then SoapySDR devices (Airspy, HackRF, SDRplay, LimeSDR, PlutoSDR). |
| Language | English only, whatever the system language. |

## Architecture

```mermaid
flowchart LR
  subgraph app ["Radiolack desktop app"]
    src["Source: rtl_tcp, SoapySDR"] --> dsp["DSP: FFT, channel, demodulators"]
    dsp --> scan["Scanners"]
    dsp --> srv["Web server: HTTP and WebSocket"]
    scan --> srv
    srv --- store[("Settings, bookmarks, observations")]
    srv --- win["Native window: web UI"]
  end
  rx[("SDR receiver")] --> src
  srv -.-|"LAN, HTTPS, access code, only when enabled"| remote["Phone, tablet or other computer: same web UI"]
```

### Components

- **Engine** (Python 3.11+, asyncio, NumPy). It owns the receiver and all
  state.
  - Sources: an rtl_tcp client, with an rtl_tcp child process that Radiolack
    starts on a free local port; SoapySDR later; synthetic I/Q generated in
    memory for tests and demos (never recorded). The Source panel switches
    between them while Radiolack runs.
  - DSP: display FFT (window, averaging, peak hold), channel extraction (mix
    and decimate), NFM, AM, USB, LSB and CW demodulators, squelch, AGC, audio
    resampling.
  - Scanners: peaks, sweep, memory.
  - Storage: settings, bookmarks, band plans and observations in the
    platform's directories (XDG on Linux, AppData on Windows, Application
    Support on macOS).
  - Server: aiohttp, for HTTP and WebSocket in one dependency. It serves the
    UI files and, per client, spectrum rows (uint8 dB values), audio, state
    updates (JSON) and commands.
- **UI** (JavaScript ES modules, served as they are: no build step). Types
  live in JSDoc comments and TypeScript checks them without producing files;
  nothing else needs Node. No UI framework at first, like the mockup; revisit
  if the panels grow complex. WebGL2 spectrum and waterfall, AudioWorklet
  playback behind a jitter buffer, mouse, keyboard and touch input. The
  files ship inside the Python package as they are.
- **Desktop shell**: a native window that shows the UI served by the local
  engine on the loopback interface. On Linux it is the system's WebKitGTK
  through PyGObject (spike 1). Windows and macOS shells are chosen before v4,
  with pywebview (WebView2, WKWebView) as the candidate. Where no native
  window is available, `radiolack --ui browser` opens the UI in the default
  browser.
- **Packaging**: on Linux, a Debian package (`packaging/build_deb.py`) that
  installs the Python package for the system's Python 3, with the
  `radiolack` command, a menu entry, an icon and its licence notice, and
  depends on the system's aiohttp, NumPy, PyGObject, GTK 3 and WebKitGTK.
  The macOS and Windows installers come with their shells (spike 5).

### Behaviour rules

- One receiver state is shared by every client: frequency, mode, filter,
  squelch, gain and scanners. A change from any client reaches the others at
  once, and the last change wins.
- View settings stay on each device: zoom, palette, panel layout, volume and
  mute. The engine keeps the latest for the next interface to open (one set
  in v0, one per device from v1).
- The engine sends audio only to clients that turned it on, and encodes each
  format once whatever the number of clients.
- Settings live in the engine. Security settings (remote access, access code)
  can only be changed from the local window.

### Remote access

- Off by default. When turned on, the engine also listens on the local
  network, over HTTPS only.
- The app generates an access code (no password to invent). A device enters
  it once and keeps a session; "New code" signs every device out. Failed
  attempts are rate-limited.
- Settings list the connected devices, which can be disconnected.
- From outside the home: through a VPN (Tailscale, WireGuard). Radiolack never
  asks the user to forward a port on their router.
- Remote clients need HTTPS anyway: browsers allow AudioWorklet, the wake lock
  and app installation only on secure pages. Spike 3 picks the certificate
  setup.

### Audio path

- v0: the engine sends 16-bit PCM over WebSocket to the local window, which
  plays it with an AudioWorklet. The local window uses the same path as remote
  clients, so v1 only adds compression and the network hop.
- v1: PCM on the local network (192 kbit/s for 12 kHz mono), Opus at about
  32 kbit/s for slower links, decoded by WebCodecs or by a WebAssembly decoder
  where WebCodecs lacks Opus.
- iPhone with the screen locked: Safari interrupts Web Audio when the screen
  locks or the page goes to the background. Spike 2 compares Safari's audio
  session API (`navigator.audioSession`) with a plain audio stream played by an
  `<audio>` element, which iOS keeps playing at the cost of a few seconds of
  delay.

### Privacy

- No telemetry and no account. Radiolack talks only to the receiver, to the
  clients the user lets in and, once updates exist, to its update server, a
  check that Settings can turn off.
- No browser data on disk: the window's WebKit context is ephemeral, and the
  engine saves what the interface needs with the settings.
- Nothing personal in the repository: no owner-specific frequencies, places or
  call signs in code, examples or tests.

## Milestones

### v0: local desktop app (Linux)

Goal: replace Gqrx for the owner's daily listening on 2 m and 70 cm.

- Engine and native window; rtl_tcp source; sample rate, centre frequency,
  gain, PPM correction and DC removal.
- Spectrum and waterfall with the mockup's interactions (mouse, keyboard and
  touch).
- One VFO: NFM, AM, USB, LSB and CW, filter width, squelch, audio on the
  computer.
- Bookmarks and the band plan: all frequencies, with the IARU Region 1 plans
  inside the amateur bands.
- Settings panel with persistence; English interface.
- Debian package with a menu entry and an icon.
- Tests without hardware: DSP on synthetic I/Q; engine and server over local
  sockets on ephemeral ports; UI tests in Chromium, Firefox and WebKit
  (Playwright); CI updated with job timeouts and cancellation.

Done when the owner confirms on their computer that Radiolack, installed from
its Debian package, starts from its menu entry, shows the live band, tunes a
local repeater with clean audio and remembers its settings, and when every
test passes in CI.

### v1: remote access

- Settings > Remote access as in the mockup: HTTPS, access code, connected
  devices.
- Remote clients: the same UI, touch input, shared receiver, a volume per
  device; PCM audio on the local network, Opus for slower links. The engine
  keeps each device's interface settings apart (v0 keeps one set).
- iPhone: listening with the screen on; listening with the screen locked
  according to spike 2.

Done when, from the owner's iPhone on the home Wi-Fi, the owner can tune and
listen with less than half a second of delay, and no device connects without
the code.

### Later milestones

- v2, scanners: peak scanner, sweep scanner, memory scan, and an observation
  store separate from the protected airband history. v1 and v2 can swap if the
  scanners matter more than remote access.
- v3, receivers and bands: SoapySDR devices, HF (direct sampling, RTL-SDR Blog
  V4), band plans for the other regions.
- v4, public release: Windows and macOS builds and installers, code signing,
  updates on every system, user documentation, accessibility review.

## Spikes and risks

| # | Question | Why it matters | When |
| --- | --- | --- | --- |
| 1 | Does the native window (WebKitGTK) draw a full-screen WebGL2 waterfall smoothly and play AudioWorklet audio at a low CPU cost on the owner's laptop? | It chooses the desktop shell. | Done: yes, within the limits below. |
| 2 | How does an iPhone keep listening with the screen locked? | The owner's phone; Safari interrupts Web Audio in the background. | Start of v1 |
| 3 | Which HTTPS setup can a non-expert complete on an iPhone and on Android: a local certificate authority installed once per device, Tailscale certificates, or a certificate the user provides? | Remote clients need a secure page, and installing a certificate on iOS takes several steps. | Start of v1 |
| 4 | Python DSP load with scanners at 2.4 MS/s. | Measured on an i7-7500U: display FFT and one NFM channel take about 15% of one core, 23% with four channels. | v2 |
| 5 | Packaging Python, NumPy and drivers for Windows and macOS, including the WinUSB driver on Windows. | Public release. | Before v4 |

### Spike 1 result (2026-11-08)

A test page drew a WebGL2 waterfall (2048-bin rows, one per frame) under a
spectrum trace and ran a silent AudioWorklet, measured for 10 s after a 2.5 s
warm-up on the owner's laptop: i7-7500U, Intel HD 620 with Mesa 22.3.6,
Wayland, 59 Hz screen at scale 2. WebKitGTK 2.50.6 ran through PyGObject;
Chromium ran in app mode. CPU covers the whole process tree.

| Shell | Drawing size (device px) | Frames/s | 95th percentile frame | CPU (one core) | Audio |
| --- | --- | --- | --- | --- | --- |
| WebKitGTK, default policy | 3200 × 1662 | 52.2 | 26 ms | 57% | suspended |
| WebKitGTK, autoplay allowed | 3200 × 1662 | 50.6 | 28 ms | 82% | 48 kHz |
| Chromium | 2760 × 1628 | 57.2 | 19 ms | 84% | 48 kHz |
| WebKitGTK, autoplay allowed | 2400 × 1400 | 58.9 | 21 ms | 52% | 48 kHz |
| Chromium | 2360 × 1308 | 58.9 | 16.8 ms | 36% | 48 kHz |

- Both shells draw on the GPU. Up to about 3.4 million device pixels both
  keep up with the screen; at 5.3 million WebKitGTK drops to about 51
  frames/s while Chromium holds 57.
- At the same size Chromium uses less CPU (36% against 52% of one core).
- WebKitGTK keeps an AudioContext suspended until a user gesture, unless the
  web view is created with a policy that allows autoplay (WebKitGTK website
  policies, which can only be set when the view is created).

The mockup itself, served by the v0 skeleton in that window (1280 × 800 CSS
px, scale 2), runs at about 20 frames/s: its frame callback takes 40 ms, of
which 30 ms go to 2D-canvas `stroke()` calls for the spectrum traces and 7 ms
to `fill()`. In Chromium the same callback takes 2.5 ms.

Consequences:

- v0 uses WebKitGTK through PyGObject directly: system packages on Linux, no
  extra Python dependency, and control over options that must be set when the
  view is created, such as the autoplay policy.
- The UI draws the spectrum and the waterfall with WebGL2, not with 2D-canvas
  paths.
- The UI keeps its drawing buffers at about 3.4 million device pixels or less
  on large HiDPI windows, and starts audio on the first user gesture wherever
  autoplay is blocked, as remote browsers require anyway.

## Preparing a public release

Public distribution multiplies the work. These choices are cheap now and
expensive later, so they apply from v0:

- English only in code and interface: no translations to maintain.
- Band plans by region, with no owner-specific data.
- Cross-platform code paths only.
- Secure defaults for remote access.
- Dependencies whose licences fit Radiolack's, GPL-3.0-or-later.

These costs wait for v4:

- Code signing: the Apple Developer Program (99 USD a year) with
  notarisation, and a Windows signing certificate or signing service to avoid
  SmartScreen warnings.
- Installers and updates for three systems, built on each system (GitHub
  Actions minutes are free for public repositories).
- Support and documentation for other people's receivers and drivers.
- A check that the name "Radiolack" is free to use.

## Open questions

1. Recording: the repository forbids recording audio and raw I/Q today, while
   most SDR programs offer it. Keep the rule, or allow recording on demand?
2. Wide FM: the band plan shows FM broadcast (87.5 to 108 MHz), which needs a
   WFM demodulator, but the scope lists NFM, AM, USB, LSB and CW only. Add WFM
   to v0, or later?

## Repository plan

- `rewrite` branch created from `main` on 2026-11-08.
- v0 skeleton, 2026-11-08: the airband explorer's package, tests and docs are
  removed from the branch, after [`lessons.md`](lessons.md) kept what the new
  design needs. `radiolack` serves `src/radiolack/web/` (for now a copy of the
  mockup) on 127.0.0.1 and shows it in the native window, or in a browser.
  New `AGENTS.md` and `README.md` keep the safety rules; CI runs the new tests.
- Live display, 2026-11-09: the engine computes the display FFT (NumPy, 8192
  bins, about 30 frames/s) of a simulated 2 m band and streams it over a
  WebSocket ([`protocol.md`](protocol.md)) to the interface, which draws the
  spectrum and the waterfall with WebGL2 and keeps the mockup's panels and
  interactions. The interface is plain ES modules, type-checked from their
  JSDoc. Measured in the native window on the owner's laptop (1280 × 800 CSS
  px at scale 2; other programs were busy, so CPU figures vary by about
  ±10 points between runs): 56 to 60 frames/s against 20 for the mockup, a
  frame callback of about 1 ms, and 1.05 to 1.2 cores for the whole process
  tree while streaming: about 0.5 in the WebKit web process, 0.3 on the GTK
  main thread and 0.25 for the synthetic source and the FFT. With the stream
  open and no frames, the tree takes 0.08 to 0.13 cores; a static page takes
  0.01.
- Band plan, 2026-11-09: the strip, the readouts and the Bands panel read a
  band plan file served with the interface ([`band-plan.md`](band-plan.md)):
  198 bands from 9 kHz to 6.425 GHz, 81 segments inside them, checked by the
  tests and against the sources it names. The engine still captures the
  simulated 2 m band only.
- rtl_tcp source, 2026-11-09: `radiolack --rtl-tcp [ADDRESS]` reads an
  RTL-SDR receiver through an rtl_tcp server that the user runs. The source
  state (connection, tuner, settings) reaches every interface, which can set
  the centre frequency, the sample rate and the gain (protocol version 2); a
  typed frequency or a bookmark outside the span moves the span. A refused,
  silent or lost connection is a waiting state, retried by itself, and the
  source hangs up while no interface listens. Tested against a fake rtl_tcp
  server; not yet with a receiver.
- Managed rtl_tcp, 2026-11-09: `radiolack --rtl-sdr [DEVICE]` runs rtl_tcp
  itself, on 127.0.0.1 and a free port, for each connection, and stops it
  when the source hangs up or Radiolack stops, so that the receiver stays
  free for other programs while no interface listens; on Linux, setpriv has
  the kernel stop it should Radiolack die first. A receiver that rtl_tcp
  cannot open (none found, in use, no permission) is a waiting state with
  that reason. Tested with a fake rtl_tcp program; not yet with a receiver.
- Frequency correction and DC removal, 2026-11-09: the Source panel sets the
  receiver's correction in whole ppm, -100 to +100 (rtl_tcp command 5, sent
  at each connection like the other settings, with 0.4 s of settling after a
  change). DC removal is the engine's own setting, on by default and shared
  by every interface: it takes each block's mean off the samples before the
  FFT, which also removes a signal exactly on the centre frequency, and will
  apply to demodulation too. Tested with the fakes; not yet with a receiver.
- Type check and browser tests in CI, 2026-11-09: a second workflow, which
  runs only when the package, the tests or `pyproject.toml` change, checks
  the interface's types and runs Playwright tests in headless Chromium,
  Firefox and WebKit against the simulated band and the fake rtl_tcp
  program: the stream, the Source panel, and a change made in one interface
  showing in another. WebKit 27.2 refuses its own stylesheet for each
  `<select>` under the interface's policy, without visible effect
  (WebKitGTK 2.50 does not): the tests ignore that one message in WebKit.
  On GitHub's runner (Ubuntu 26.04, no GPU), Firefox first refused
  WebGL2: without a GPU it refuses Mesa's software renderer and needs a
  display, so the tests set its `webgl.force-enabled` preference and the
  workflow runs them under Xvfb, as checked locally with the GPU hidden.
- NFM and audio on the computer, 2026-11-09: the engine demodulates one VFO
  (protocol version 3). Overlap-save FFT filters shift the VFO to 0 Hz and
  bring the rate to 48 kHz, exactly for the usual receiver rates (2.4 MS/s
  by 1/50, 2.048 MS/s by 3/128), then keep the mode's channel, whose power
  each spectrum frame carries with the squelch's state. NFM goes through a
  discriminator, a 750 µs de-emphasis, a high-pass filter against CTCSS
  tones and a 3.5 kHz low-pass filter, to 16-bit PCM at 12 kHz for the
  interfaces that ask. The squelch, automatic (8 dB above the noise that the
  spectrum shows around the VFO) or set by hand, gates the discriminator's
  output with a 10 ms look-ahead and 5 ms fades: gating after the long audio
  filter let a burst of noise through at each close. The interface plays the
  audio with an AudioWorklet that keeps about 150 ms buffered, follows the
  engine's clock within 0.5% and resamples with a 16-tap windowed sinc
  (images below -89 dB). The VFO, mode, filter width and squelch are shared
  by every interface; the volume is each interface's own. In the native
  window the audio starts by itself; browsers start it on the first click or
  key press. On the owner's laptop (i7-7500U, other programs busy) the
  channel takes 1.8 ms per block of 1/30 s at 2.4 MS/s, 2.2 ms with the
  audio, about 6% of one core; the engine's process measured 38 to 44% of
  one core with the synthetic band, against 31 to 38% before. Raising
  glibc's mmap and trim thresholds made no measurable difference, so
  Radiolack leaves the allocator alone. Tested on synthetic I/Q, through the
  engine and the server, in three browsers and in the native window; not
  yet with a receiver. On GitHub's runner, which has no sound card or sound
  server, Firefox kept its audio context suspended, as it does locally with
  the sound server hidden: its audio library found no output channel, even
  with a null ALSA device. The workflow therefore starts PulseAudio with
  only a null sink.
- Settings and bookmarks, 2026-11-09: the engine restores at start, and
  saves a second after a change and when Radiolack stops, each receiver's
  source and demodulation settings, the bookmarks and the interface's own
  settings (protocol version 4: bookmarks, preferences and storage state).
  Files are written whole through a temporary file, synced, then renamed;
  writes run outside the event loop, and only for a file whose content
  changed. A file that is not valid JSON of a known version is renamed
  aside and Radiolack starts from the defaults; one that cannot be opened,
  or comes from a newer version, is left alone and not written until the
  next start; a failed write is tried again at the next change. These disk
  problems reach the interfaces apart from the receiver's state: Settings >
  Storage names them and the status line says when changes are not saved.
  A saved setting the receiver refuses keeps its default. The Bands panel
  adds, renames and removes the engine's bookmarks, which every interface
  shows. The tests run with a temporary settings folder.
- AM, SSB and CW audio, 2026-11-09: every mode plays; the protocol does not
  change, as `audio_modes` now lists all five. AM divides the envelope by
  the carrier's level, the higher of its averages over the 10 ms before and
  the 10 ms after each sample: the result is the modulation whatever the
  signal's strength, with no carrier left to thump at the squelch's gate,
  even where a carrier starts or stops at once (one average centred on the
  sample overshot by a third there). USB and LSB take the channel's real
  part, whose filter already keeps one sideband (the other comes out 88 dB
  lower); so does CW, the front filter putting the VFO at 700 Hz, so that a
  carrier on the VFO sounds at that pitch through a filter centred on the
  VFO. Their AGC follows the channel's power averaged over 10 ms, 10 ms
  ahead of the audio within the squelch's look-ahead, so that a sudden
  signal comes out without overshoot, and lets the gain come back at
  10 dB per second: a steady signal comes out at 0.4 of full scale from
  -30 to -80 dBFS. The AGC has this one speed; the Demod panel's AGC choice
  stays inactive. Through the synthetic band, the SSB contact plays in USB
  and vanishes in LSB, and the CW beacons sound at 700 Hz. Every mode costs
  about what NFM does: 3.6 to 4.0 ms per block of 1/30 s at 2.4 MS/s with
  the audio on the owner's laptop, CW the most, against 3.6 ms for NFM in
  the same run (other programs busy: the NFM entry measured 2.2 ms). Tested
  on synthetic I/Q and the synthetic band, and in three browsers; not yet
  with a receiver.
- rtl_tcp address in the Source panel, 2026-11-09: the address of an
  `rtl_tcp` source is one of its settings, which the Source panel's "Server
  address" field changes (settable in the source state; protocol version 4
  unchanged, as interfaces leave out the settings they do not know). A new
  address hangs up and connects there with the same settings, at once even
  while the source waits to try again; the tuner is unknown until the new
  server answers. The engine keeps one set of rtl_tcp settings with the
  address used last; an address given on the command line replaces the
  saved one, which is then written over at the next save or when Radiolack
  stops. Tested with fake rtl_tcp servers, and in three browsers against
  addresses where nothing listens. The panel's text fields show the engine's
  value once a change is sent, focus or not, and keep what the user types
  until then. Tried on 2026-11-09 with an R820T behind rtl_tcp on another
  computer, over a VPN with about 400 ms of round trip: the field moved the
  receiver to a port where nothing listens and back with the same settings.
  That link carried about 1.5 MB/s, short of the 2 MB/s of 1.024 MS/s: 16
  frames per second instead of 30, rtl_tcp dropping samples.
- Source switching, 2026-11-10: the Source panel's Receiver list switches
  between the simulated band, a receiver plugged into this computer and an
  rtl_tcp server, for every interface: the source state lists the `kinds`
  the engine can open, and a `source` message with `kind` alone switches
  (protocol version 4 unchanged). The engine keeps the settings of the
  source it leaves, closes it and opens the one used last of the chosen
  kind with its saved settings, demodulation included; it reads the new
  source only once the previous one has let the receiver go, as both may
  need the same device. `settings.json` records the source in use, which
  Radiolack opens at the next start unless `--rtl-sdr` or `--rtl-tcp` names
  one; a saved source it cannot open gives the simulated band. Tested with
  the fakes and in three browsers; not yet with a receiver.
- Debian package, 2026-11-10: `packaging/build_deb.py` builds an
  architecture-independent package with dpkg-deb alone, from the Python
  package as it is: the package in the system's `dist-packages`,
  byte-compiled at installation, the `radiolack` command, a menu entry and
  an icon. The window takes the program name `radiolack`, its application
  id on Wayland and its class on X11, so that the desktop matches it with
  the menu entry and the icon. A development version carries the time and
  commit of the last commit, and `.dirty` with uncommitted changes, so that
  a later build installs over an earlier one. A maintainer is required:
  dpkg warns about an installed package without one every time it reads its
  database. Tested by building, extracting and running the package; not yet
  installed.
- Licence, 2026-11-10: `LICENSE` holds the GPL's text as the Free Software
  Foundation publishes it (Debian's `/usr/share/common-licenses/GPL-3` is
  the same file), the README states it, and the Debian package carries the
  notice Debian expects in `/usr/share/doc/radiolack/copyright`. The Python
  package carries `LICENSE` too, built with Debian 12's setuptools (66) as
  with the latest (84), but `pyproject.toml` does not name the licence: its
  SPDX form needs setuptools 77, so Debian 12's would refuse the README's
  installation without build isolation, and the older table form is
  deprecated, with 18 February 2027 as setuptools' deadline.
- Leftovers, 2026-11-10: the window's WebKit context is ephemeral, so
  WebKitGTK no longer fills `~/.local/share/radiolack` and
  `~/.cache/radiolack` with a cache and storage that the interface does not
  use; checked in Xvfb with empty XDG folders, the page still loading. The
  programs the tests start that import the test package, such as the fake
  rtl_tcp, keep the run's settings folder instead of leaving one of their
  own in the temporary folder each time a signal stops them.
- v0 merged into `main`, 2026-11-10, with a merge commit that keeps the
  rewrite's commits as they are. The airband explorer stays in the history
  at commit `f0e1cf6`; work continues on `main`, and the `rewrite` branch is
  no longer used.
- Next step: the owner makes the repository public, installs the Debian
  package and checks v0 on their computer; then versioned releases, the APT
  repository and the update alert.
- `data/` stays untracked and untouched; `data/airband-peaks.json` is never
  written, replaced or deleted.
