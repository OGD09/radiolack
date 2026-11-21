# Radiolack agent guide

Receive-only SDR receiver for every frequency its receivers cover, with
ham-focused band plans (Python 3.11+, aiohttp; web interface shown in a
native window), rewritten from scratch: v0 replaced the airband explorer on
`main` on 2026-11-10, and work continues there. User documentation is in
README.md. Design decisions, milestones and open questions are in
docs/design/scoping.md: read it before substantive work. The milestones
after v3 are designed in docs/design/coverage-and-precision.md (shortwave,
tuning to the hertz, the band plan from 0 Hz to 10.5 GHz: each pull
request's rules and limits) and docs/design/later-milestones.md (SoapySDR
and those that follow, up to v4); docs/design/range-scan.md designs the
range scan that replaces the sweep and the signals scan, and
docs/design/bookmark-categories.md the bookmarks' categories.
docs/design/ui-mockup.html is the visual reference, and docs/design/lessons.md
keeps what the rewrite reuses from the airband explorer, whose code stays in
the history under the tag `v0.1.0`.

## Layout

- `src/radiolack/`: `cli` (entry point: `--ui`, `--port`, `--remote`,
  `--scan`, `--bias-tee`, `--rtl-sdr`, `--rtl-tcp`, `--soapy`), `server` (aiohttp on 127.0.0.1,
  own event loop in a background thread; interface files, read once at
  start, the `/stream` WebSocket and the recordings' downloads
  (`/recordings/<file>`); starts the scan given at start;
  remote access's HTTPS listener, a second application with a runner of
  its own, in the same loop, which follows the computer's addresses while
  on), `remote` (remote access: the
  local certificate authority, with `cryptography`, the access code, paired
  devices with their sessions and interface preferences, the private
  addresses to listen on), `engine`
  (reads a source, removes the DC offset, shares spectrum frames, the source
  state, the demodulation settings, the bookmarks, the scanner state, the
  signals found, the recording state, the SoapySDR devices found and the
  update check's findings with
  clients, sends audio to those that ask, and the fine spectrum of the
  VFO's channel, computed once while at least one asks, remote clients'
  with fewer bins, passes their changes on,
  switches between kinds of
  sources, restores and saves the settings and the source in use, a
  receiver's own once its source learns which it reads; searches for
  SoapySDR devices when this computer's interface asks, never at start
  nor for a remote client, never the driver of the device in use, and
  opens one chosen among them, by its driver and serial number, or by the
  device arguments that this computer's interfaces alone give, never
  while a search runs; sends a
  problem report to this computer's interface that asks; measures the
  carrier at the VFO for the interfaces that ask, gathering the VFO's
  channel, unblanked, only while one waits, and sends each the result
  alone; checks
  the rtl-sdr driver's computation on that carrier for the interfaces that
  ask, moving an R820T's or R828D's centre off its grid for a few points,
  then back whatever ends the check, and saves what it found; runs the
  automatic calibration for any client, on demand only, tuning the
  receiver and the VFO to each standard-frequency station within reach
  and back, whatever ends it, and sends every client its state, the
  latest forgotten with another receiver; keeps the
  interfaces' preferences, this computer's
  and each paired device's; runs the memory scan, which tunes the VFO for
  every client and, with a receiver that tunes, visits the ticked
  bookmarks outside the span window after window, and the range scan
  (the protocol's sweep), which steps such a receiver across a range, or
  searches the current window alone without tuning it, for a number of
  passes or without end, and may tune the VFO alone to each window's
  active signals in turn, in the band plan's mode ("signals" starts it
  over the current window, listening), one scan at a time, saving the
  receiver's settings of its start and bringing the receiver back once
  stopped, unless on a signal or a bookmark; sends each signal found and each observed channel
  with the frequency it shows on, the band plan's channel or the 100 Hz;
  keeps the observations, the simulated band's excepted, and
  sends them to the client that asks; keeps each range scan, the
  simulated band's excepted, under the name given at its start, sends
  the kept scans to every client and a kept scan's channels to the
  client that asks, and renames and deletes them for any client; records the VFO's audio, and an
  RTL-SDR's raw I/Q, when a client asks, never on the simulated band,
  until a client stops it or a new audio rate (the audio), a new sample
  rate or another receiver (the I/Q), the disk or the WAV limit does,
  lists the recordings folder and deletes from it for the client that
  asks; idle without
  clients unless a scan or a recording runs; remote clients get neither
  the storage, update nor remote access states, nor the recordings
  folder, which they may not change, nor the SoapySDR device arguments,
  which they may not give, may not search for SoapySDR devices, and get
  lighter spectrum frames),
  `recordings` (the audio's WAV files and the raw I/Q's SigMF files, data
  and metadata, the I/Q in the receiver's format, `cu8`, `ci8`,
  `ci16_le` or `cf32_le`, with `radiolack:full_scale`, a declared optional
  extension, where an integer format's full scale is not its type's;
  each recording written by a thread of its own, in batches of 4 MiB at
  most, that checks the free space before each write, and the WAV limit
  for the audio, the I/Q waiting for the disk bounded by its rate and
  format; named after their start, frequency and mode, or sample rate for
  the I/Q; the recordings folder, `$XDG_DATA_HOME/radiolack/recordings`
  unless the user chose another, its checks and its list; opening a
  recording's file to download it, and deleting a recording, with an I/Q
  recording's metadata, only for the files named as recordings; an I/Q
  recording's length from the format its metadata names),
  `report` (problem reports: the latest warnings and errors of the
  `radiolack` logger, kept in memory from `cli`'s start, which still writes
  them on the error output; the report's facts, without the receiver's
  address or frequency, the home folder written `~`; GitHub's page for a
  new issue with it, the oldest messages left out to keep its address
  short; Radiolack sends nothing itself), `updates` (update check: apt run
  as the user, with Radiolack's APT source alone and none of the system's
  apt configuration, in a temporary folder; only for the Debian package's
  Radiolack), `store` (settings, bookmarks, observations, kept scans and
  remote access files in
  `$XDG_CONFIG_HOME/radiolack`: whole-file writes, readable by the user
  only, unreadable files set aside), `bookmarks` (bookmarks, their categories, one level deep, and their checks; the file keeps the bookmarks as an older Radiolack reads them and the categories beside them),
  `scanners` (detection on the engine's FFTs, averaged over a third of a
  second, against the local noise; the list of the signals found and the
  ignored frequencies; the memory scan's watch of the ticked bookmarks'
  channels in the same FFTs, and its states; the windows that the ticked
  bookmarks take and the memory scan's visits; the range scan's windows
  across a range, the one window of a range within the span, its passes
  and progress, and its turns among a window's active signals, the one
  that waited longest first, and their states),
  `bandplan` (the band plan as the engine reads it,
  `web/bandplan-europe.json`, the file the interface loads: the mode the
  History's Bookmark button gives a frequency, for the range scan, and
  the channel a signal shows on, for the lists, and the standard-frequency
  stations, for the automatic calibration; an unreadable plan gives
  no mode, which the output says; `tests/test_bandplan.py` checks in Node
  that its modes agree with the interface's), `observations` (the channels
  where signals were found, for the History: how signals join a channel,
  its counters, the limits and the checks of the saved file), `keptscans`
  (the range scans kept under their names, range-scan.md, pull request 4:
  each scan's channels within its range, on the lists' frequency, its
  passes and ending, the squelch's open time while it listened, the
  limits, names and checks of the saved file, a scan that ran read back as
  interrupted),
  `preferences` (the interfaces' preferences and their checks), `spectrum`
  (display FFT with NumPy, noise estimate around the VFO, or beyond a WFM
  channel), `dsp` (FIR design and overlap-save FFT filters that shift and
  resample, a few samples among zeros filtered alone, the Hann window),
  `fine` (the fine spectrum of the VFO's channel for the deep zoom: FFTs
  of 8192 bins of the front filter's output, about ten frames a second,
  started again at each change of setting, and the binary fine frame),
  `demod` (one VFO: channel level, squelch,
  audio at 12 kHz, or 32 kHz in WFM; SAM, synchronous AM: the carrier's
  search and lock, sought with or without audio, and the detector that
  follows it, AM's envelope without a lock, the switch faded; the
  automatic notch in AM, SAM, USB and LSB: the lines found in the
  detector's output, before the gate, and the audio filter's taps without
  them, computed again in the block's worker thread only when a notch
  comes, goes or moves, the audio faded from the old taps to the new; the
  noise blanker in every mode: the impulses of the receiver's samples
  zeroed before the front filter, a signal longer than an impulse and a
  block where it would zero too much left whole, the impulses of the
  latest second counted, and the samples as received for the taps, the
  measure's channel and the fine spectrum's front filter output, while it
  blanks: the blanked front's output plus that of the samples zeroed,
  filtered alone), `rtltcp` (rtl_tcp client: `RtlTcpSource` for an
  rtl_tcp the user runs, whose address the interface can change,
  `RtlSdrSource` runs rtl_tcp itself while it connects; a lost server is a
  waiting state; direct sampling below the tuner's range for shortwave;
  the antenna power, off at each connection unless turned on, never saved,
  turned off before the source closes; the converter in front of the
  receiver, converting only the frequency it sends and swapping I and Q
  behind an inverted one; the correction's whole ppm by command 0x05 and
  the rest by the frequency it asks for; an R820T or R828D on its tuner
  placed on its grid, none of the correction by 0x05, its samples labelled
  with the centre in use, where the driver's computation that the
  receiver's `driver_pll` names puts the tuner, and the VFO's shift scaled
  by the correction (coverage-and-precision.md, pull request 14a); the
  request in use and the centre that reaches a request off the grid, for
  the driver's check (14b); hands the raw samples to an I/Q recording's
  tap),
  `r82xx` (rtl-sdr's two computations of an R820T's or R828D's PLL, ported
  from the driver, GPL-2.0-or-later, whose docstring names the versions
  and commits and keeps the notice that `packaging/copyright` repeats; the
  tuner's IF for the sample rate, and the grid where the error is the same
  at every centre frequency),
  `calibration` (the automatic calibration, later-milestones.md, section
  3: the band plan's standard-frequency stations within reach, twelve at
  most, lowest first, where the receiver tunes for each, the window where
  the carrier is sought, the correction a carrier implies and the
  proposal, apart from the engine),
  `correction` (the frequency correction in hundredths of ppm: its checks,
  the whole part a receiver takes itself and the frequency to ask for the
  rest), `carrier` (the measure of a carrier in the VFO's channel, before
  the demodulator: Hann window, FFT of one second, the strongest line
  within the filter placed by a parabola, its margin over the median, two
  seconds that must agree within 1 Hz; apart from the engine and the
  interface, for later runs across several stations), `pllcheck` (the
  check of the driver's computation: the centres off the grid where the
  two computations differ, and the computation that the carrier's moves
  between them follow, whatever the crystal and the label; apart from the
  engine), `converter` (a converter in front of a receiver: its kinds and
  oscillator, the frequency the receiver tunes for one on the air and the
  other way round, the reach on the air),
  `soapy` (receivers through SoapySDR, later-milestones.md, section 1,
  pull request 2: the `SoapySDR` module imported only when such a source
  opens its device or `--soapy` is given, a waiting state naming
  `python3-soapysdr` without it; device arguments that must name a
  driver; the device's range, rates up to 8 MS/s, analogue bandwidth, one
  gain stage, correction (the whole ppm to a device that takes one, the
  rest by the label alone, the frequency asked staying the one tuned) and
  native format with the drivers' full scales;
  pull request 4: several gain stages, the overall gain through the
  module's spread, or the source's own around a held stage (the HackRF's
  AMP, at its lowest at each opening without saved gains, never moved by
  the overall gain), each read back; the antenna sockets; the device's
  settings as it describes them, boxes, choices and numbers, bounded, free
  texts and transmit-named ones left out, written between reads and read
  back, a refusal shown; antenna power by key or name, `bias_tee`, off as
  soon as the device opens and before it is released, on only when asked,
  never saved;
  a reading thread per session that alone calls the device, into a
  `NativeBuffer` that drops the oldest, and closes the stream and the
  device whatever ends it; settings under driver and serial number, the
  engine restoring them once the device is found; overflows, drops and
  late blocks logged at most once a minute; pull request 5: the samples
  kept, in the device's format, handed to an I/Q recording's tap through
  the event loop, a second at most waiting for it, those beyond left out
  and counted, I and Q swapped behind an inverted converter, and
  `raw_format`, SigMF's datatype and the full scale where it is not the
  format's; receive stream only), `soapysearch` (the search for SoapySDR devices,
  pull request 3: the listed local USB drivers, `LISTED_DRIVERS`, `sdrplay`
  to join them, one at a time with `driver=NAME` alone, never without a
  driver nor `rtlsdr`, in a thread of its own, one search at a time; each
  device's driver, serial number and label alone, in memory only; the
  search's state, a failed driver's and the missing module's),
  `sources` (opens each kind of source from its saved
  record, for the Receiver list and at start), `synthetic` (simulated 2 m
  band), `window` (GTK 3 and WebKitGTK through PyGObject; the main thread
  runs GTK; an ephemeral WebKit context writes nothing to disk; downloads
  go to the user's Downloads folder without asking, under a name not taken
  there, and a `radiolack-download` event tells the page where; when
  WebKit's web process ends, after a crash in WebKitGTK for instance, the
  page loads again, unless it ended less than a minute before: a message
  with a Reload button then takes its place; WebKit paints on the CPU,
  `WEBKIT_SKIA_ENABLE_CPU_RENDERING=1` unless the user set it, as Skia's
  GPU painting crashed WebKitGTK 2.50's web process; Radiolack's pages on
  GitHub, under `PROJECT_URL` (`__init__`), alone open in the user's web
  browser).
- `src/radiolack/web/`: interface files, served as they are: ES modules, no
  build step. `stream.js` (stream client, which decodes the fine frames it
  asks for), `audio.js` and
  `audio-worklet.js` (audio output, AudioWorklet playback), `gl.js`,
  `spectrum-gl.js` and `waterfall-gl.js` (WebGL2 views; the waterfall
  holds the wide spectrum's history and the fine spectrum's, each line
  drawn within a window of its bins), `history.js` (a spectrum's frames as
  the interface keeps them: trace, peak hold and waterfall lines with
  their time and centre), `deepzoom.js` (the deep zoom, later-milestones.md,
  section 2: when to ask for the fine frames, which spectrum shows, the
  cover, the narrowest view and where a frequency lies among a frame's
  bins), `bandplan.js`
  (band plan loader, and the mode the plan gives, SAM counting as AM), `antenna.js` (antenna lengths for the VFO's
  frequency), `frequency.js` (the one formatter and the one reader of
  exact frequencies), `shortwave.js` (the Receiver menu's shortwave
  input), `power.js` (the antenna power's second press), `converter.js`
  (the Receiver menu's converter, and the view that follows its change), `tunerpll.js` (the
  Receiver menu's rtl-sdr driver, the centre in use, and the grid's point
  a retune takes to keep the VFO's channel clear of the centre), `tuning.js` (the
  Automatic step), `categories.js` (the Bookmarks menu's categories: the
  list's sections, a category's box, the questions that moving bookmarks
  and removing a category ask, and a category's name's check),
  `channels.js` (the band plan's channels and known
  frequencies: what the VFO is on, the Automatic step along a set's
  channels, what the band strip draws of them and what a press there lands
  on), `planlist.js` (the Bookmarks menu's band plan list: the view, or the
  filter across the plan, at most 200 rows), `rates.js` (what a mode needs of the sample rate),
  `memorytour.js` (what the Bookmarks menu says of the memory scan's tour
  across several windows),
  `view.js` (the view's limits up to the band plan's end, what lies beyond
  the receiver's reach, and what the view draws as it widens) and
  `correction.js` (the frequency correction's field), `calibration.js`
  (the Calibrate dialog: the calibration on the stations, its stations'
  table, its proposal and why none; the correction from a carrier
  measured and its true frequency, with its derivation, the driver's
  check and what the dialog shows), `devices.js` (the Receiver menu's SoapySDR devices:
  the list, Search again and the arguments field, this computer's alone
  for the last two, and when the first search is asked) and `advanced.js`
  (a SoapySDR device's antenna sockets and Advanced section: a slider per
  gain stage and its settings, the device's texts set as text) carry JSDoc
  types; `main.js`
  still holds the mockup's code under `// @ts-nocheck` and gets types as it
  is rewritten. `tests/test_deepzoom.py` runs `deepzoom.js` and
  `history.js` in Node, `tests/test_devices.py` `devices.js` and the
  devices state's decoding, `tests/test_categories.py` `categories.js`
  and the bookmarks message's decoding, `tests/test_memorytour.py` `memorytour.js`,
  `tests/test_calibration_dialog.py` `calibration.js`'s calibration on
  the stations and the calibration state's decoding, and `tests/test_advanced.py` `advanced.js`,
  the antenna power's warning and the device's fields' decoding, skipped
  where Node is missing. `bandplan-europe.json`
  is the band plan data ([docs/design/band-plan.md](docs/design/band-plan.md)).
  `pair.html`, `pair.css` and `pair.js` (typed) are the pairing page that
  remote access serves to a device that is not paired, with `style.css`
  and the icons. `favicon.svg` and `apple-touch-icon.png` (the iPhone's
  Home Screen) are the icons, and `pair.html` inlines the logo in the
  text's colour: `packaging/identity.py` draws them. The interface names
  Radiolack in its title only, and keeps its settings in one menu
  (scoping.md, 2026-11-15), whose last entry, About, shows the version
  that runs, the updates, the links to GitHub and the legal notice:
  README.md repeats the notice word for word, which
  `tests/test_legal_notice.py` checks (scoping.md, 2026-11-16).
- `packaging/`: `build_deb.py` (Debian package, built with dpkg-deb from the
  Python package as it is; it recommends rtl-sdr alone and suggests
  SoapySDR's Python module and the drivers' modules under the names the
  four supported systems all give them, `SUGGESTS`, which the tests pin:
  read the four systems' indexes again before changing them, and never
  recommend them, as libsoapysdr recommends every SoapySDR module
  (scoping.md, 2026-11-18); reproducible: every file carries the last
  commit's date, or SOURCE_DATE_EPOCH, and dpkg-deb's own settings in the
  environment change nothing), `radiolack.1` (manual page, whose
  version and date the build fills in; the tests check that it documents
  every option of `cli.build_parser()` and no other), `smoke_test.py`
  (checks a Radiolack from the outside: listening on 127.0.0.1 only, Host
  and Origin refusals, interface files and Content-Security-Policy, stream,
  update check, remote access off at start, stop on SIGTERM; `--window`
  checks that the window's toolkit loads, `--check-updates` that the update
  check reaches the APT repository, `--remote` that remote access turns on
  on the computer's private addresses, refuses plain HTTP, other hosts and
  unpaired devices, pairs one and turns off, then that Radiolack started
  with `--remote` prints addresses, a fingerprint and a code that work;
  run it only where its network is the check's own),
  `release.py` (tags a release on `main` and starts the next development
  version), `apt_repository.py` (builds and signs the APT repository: it
  checks the repository's signature with gpgv, then signs only what the key
  signed before and the packages it is given), `publish_apt.py` (publishes a
  release in the APT repository on the `gh-pages` branch, which GitHub Pages
  serves: it rebuilds the release from its tag and compares it with
  GitHub's, signs through `apt_repository.py`, checks the signatures with
  the package's key and pushes once the owner agrees), `radiolack.sources`
  and `radiolack-archive-keyring.gpg` (the APT source and the owner's public
  key, which the package installs as conffiles in /etc/apt; README.md gives
  the key's fingerprint, which the tests check), `radiolack.service` (the
  systemd service, under a dynamic user in systemd's sandbox, which the
  package installs in /usr/lib/systemd/system and never enables or starts;
  `build_deb.py` writes the maintainer scripts that debhelper would, and
  the tests pin the unit's sandbox), `radiolack.desktop` (menu
  entry), `radiolack.svg` (icon), `identity.py` (draws the logo and the
  icons, run by hand: the name's outlines come from the font Saira
  Italic, kept out of the repository; no font ships or loads) and
  `copyright` (the package's licence notice). The window's program name,
  `window.PROGRAM`, matches the menu entry's and the icon's names: change
  them together.
- `docs/identity/`: README.md's logo, for GitHub's light and dark themes,
  and the social preview, which the owner uploads in GitHub's settings.
- `docs/design/protocol.md`: the stream between engine and interface. Change
  it with the engine, the server and `stream.js`, and raise the protocol
  version for any incompatible change.
- `tests/`: unittest tests without hardware; pytest runs them too.
  `test_browser.py` drives the interface in headless browsers, on request.
  `tests/__init__.py` points `XDG_CONFIG_HOME` and `XDG_DATA_HOME` (the
  recordings) at a temporary folder for the whole run, and `GNUPGHOME` at
  an empty GnuPG home in it, which the
  programs the tests start inherit; those that import the package too keep
  it rather than make their own, and the run removes it at its end. It
  also lets `import SoapySDR` find the stand-in alone
  (`tests.fakes.stand_in_soapysdr`), the real module being missing for
  the tests even where it is installed; `tests/test_soapy.py` tests the
  SoapySDR source against the stand-in, whose `hackrf_one`, `rspdx_r2`
  and `rtl_sdr` declare devices shaped as their modules declare them, and
  whose `events` log the order of settings written and closes.
  The fake rtl_tcp's optional tuner error takes `radiolack.r82xx`'s
  computations, as `tools/measure_pll.py` does; the fake rtl_tcp
  program's `references` device serves a carrier on every
  standard-frequency station through a crystal that is off, for the
  automatic calibration (`tests/test_calibration.py`).
- `tools/`: development tools, run from a checkout and never installed:
  the Debian package and pip take `src/radiolack` alone.
  `measure_pll.py` (coverage-and-precision.md, pull requests 13 and 14a)
  measures how well a Radiolack already running on a receiver with an
  R820T or R828D labels its samples, through its stream on 127.0.0.1,
  moving only the centre frequency and the tuner's grid (`tuner_grid`,
  never saved), which it restores, and names the driver's computation to
  choose; `tests/test_measure_pll.py` runs it against the fake rtl_tcp.
  `measure_listening.py` (spike 8, later-milestones.md, section 2) times
  the engine's block processing on synthetic samples at 2.4 MS/s without
  the four listening tools, with each, with the fine spectrum and the
  blanker together, and with the four, all of them the package's: SAM as
  the VFO's mode, the notch and the blanker as its settings and the fine
  spectrum as the engine gives it a block, needing no receiver, with the
  scene's impulses or without them (`--without-impulses`);
  `tests/test_listening.py` runs the measure briefly, never reading its
  figures, `tests/test_sam.py` checks SAM, `tests/test_notch.py` the
  notch, `tests/test_blanker.py` the blanker and `tests/test_fine.py`
  the fine spectrum. Spike 7 (later-milestones.md, section 1, Any
  receiver): `measure_rates.py` times the engine's block processing, one
  NFM channel with audio and detection, and a SoapySDR source's intake
  (CS8, CS16 or CF32 into the buffer that drops the oldest, `NativeBuffer`,
  and to complex64) at 2.4 to 10 MS/s on synthetic samples, needing no
  receiver, back to back (the work, on which its rule decides) and then
  paced, the engine's blocks one every 1/30 s (what a system monitor shows
  on a processor that slows down between blocks, and the blocks that take
  longer than their period); `check_search.py` runs `SoapySDR.Device.enumerate` for each
  driver named under `unshare -rn`, with a preloaded library, compiled at
  each run, whose seccomp filter refuses and reports every socket but
  netlink's and whose stand-ins for librtlsdr's listing find no receiver;
  it refuses `rtlsdr` and a search without a driver; `measure_reader.py`
  opens a SoapySDR receiver, reads it from a thread into a `NativeBuffer`
  while the engine's processing takes its blocks, and reports the samples
  lost, leaving out timestamps that run at a constant multiple of the
  samples read, as the rtlsdr module's do: only when the owner asks for it
  in the current conversation, on the receiver the owner names.
  `tests/test_measure_rates.py` (the paced measure on a clock of its
  own, never waiting),
  `tests/test_check_search.py` (its trap, where `unshare -rn` and a C
  compiler work, never a search) and `tests/test_measure_reader.py` test
  them, the last against `tests/soapysdr/SoapySDR.py`, the stand-in for
  SoapySDR's Python module (`tests.fakes.stand_in_soapysdr`), which fails
  a test that asks it for a transmit direction, and whose wall clock a
  test may replace.

## Commands

- Tests: `python3 -m pytest` or `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Type check of the interface: `npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json`
  (needs Node.js).
- Browser tests: `RADIOLACK_BROWSER_TESTS=1 python3 -m unittest tests.test_browser -v`,
  with the `browser` extra (Playwright, pinned in pyproject.toml) and its
  browsers installed (README.md, Tests); `RADIOLACK_BROWSERS=firefox`
  (comma-separated) keeps them to some browsers.
- Run without installing: `PYTHONPATH=src python3 -m radiolack` (window) or
  `--ui none` (address only). The server reads `web/` at start: restart it
  to see a change there.
- Debian package: `python3 packaging/build_deb.py --maintainer "Name <address>"`
  writes `dist/radiolack_VERSION_all.deb` (needs dpkg-deb);
  `tests/test_packaging.py` builds one twice in a temporary folder, compares
  them and runs `packaging/smoke_test.py` on the package's files. On an
  installed package: `python3 packaging/smoke_test.py`.
- Logo and icons: `python3 packaging/identity.py --font PATH`, with the font
  file that its docstring names, fontTools, Inkscape and ImageMagick;
  replace the inlined logo in `pair.html` with the one it prints.
  `tests/test_identity.py` checks that every copy agrees.
- PLL measure, only when the owner asks for it in the current
  conversation, against the Radiolack the owner started on the receiver:
  `python3 tools/measure_pll.py PORT [--output FILE]` (its `--help` says
  what to set first). It tunes that receiver.
- Sample rates' cost: `python3 tools/measure_rates.py [--runs N] [--seconds S]
  [--rates 2.4,4,...] [--output FILE]`, synthetic samples only; about ten
  minutes with its defaults, back to back then paced. Its paced figures
  depend on the processor's governor and the computer's other work.
- SoapySDR search: `python3 tools/check_search.py [DRIVER ...]`, the
  design's drivers by default. Run a SoapySDR search only through it,
  never without a driver nor for `rtlsdr`: a search without a driver runs
  SoapyRemote's, which asks avahi-daemon, through D-Bus, to search the
  network, even from a network namespace of its own.
- Reading thread, only when the owner asks for it in the current
  conversation, on the receiver the owner names: `python3
  tools/measure_reader.py ARGS [--rate MS/S] [--seconds S] [--output
  FILE]`. It opens and reads that receiver.
- Listening tools' cost: `python3 tools/measure_listening.py [--runs N]
  [--seconds S] [--without-impulses] [--output FILE]`, synthetic samples only; about three
  minutes with its defaults. Figures depend on the computer's other work:
  compare configurations within one measure, not across measures.
- Release, only when the owner asks: `python3 packaging/release.py X.Y.Z` on
  a `main` without changes and at the commit of `origin/main`. It commits
  the version, tags it `vX.Y.Z` and commits the next `.dev0`; it pushes
  nothing. `tests/test_release.py` runs it in temporary repositories.
  Pushing a `vX.Y.Z` tag publishes a release on GitHub
  (`.github/workflows/release.yml`): never push one unless the owner asks
  for that push.
- APT repository: `python3 packaging/apt_repository.py --key FINGERPRINT
  REPOSITORY [PACKAGE.deb ...]` signs with the owner's key, so only the
  owner runs it on the real repository. `tests/test_apt_repository.py`
  signs with throwaway keys in a GnuPG home of its own, through a stand-in
  pinentry, and reads the repositories with apt-get run as the current user
  in a folder of its own.
- APT publication, only when the owner asks, and only the owner runs it:
  `python3 packaging/publish_apt.py X.Y.Z` signs with the owner's key and
  pushes `gh-pages`. `tests/test_publish_apt.py` runs it in temporary
  repositories, with throwaway keys and a stand-in for GitHub's downloads,
  and fails a year before the real key's signing subkey expires.
- CI (GitHub Actions, `.github/workflows/`): `tests.yml` runs the unittest
  command against the package installed with pip on Python 3.11 and 3.15;
  `interface.yml` runs the type check and the browser tests, one job per
  browser, when the package, the tests or pyproject.toml change; `package.yml`
  builds the Debian package twice and compares the builds, puts it in an APT
  repository signed with a key made for the run, then installs it from
  there, checks that apt reads its APT source, runs it (`smoke_test.py
  --window --check-updates --remote` under Xvfb: its update check reads the
  published APT repository, and remote access listens on the container's
  network), checks that it installs its service without enabling it,
  removes it (its source and key, and the service's enabling and state,
  stay) and purges it
  on Debian 12 and 13
  and Ubuntu 24.04 and 26.04, with lintian on Debian 13, when the package or
  packaging change; `workflows.yml` runs actionlint
  (`.github/actionlint.yaml`) and zizmor when `.github/` changes;
  `release.yml`, on a `vX.Y.Z` tag, checks the tag, calls tests.yml,
  interface.yml and package.yml (`workflow_call`) and publishes the package,
  its SHA256SUMS and, for a public repository, an attestation, from the
  `release` environment. Dependabot (`.github/dependabot.yml`) updates the
  actions and Playwright weekly, after a seven-day cooldown. Keep: every
  action pinned to a full commit with its version in a comment, and this
  repository's workflows called through `$/`, not `./`; checkouts
  with `persist-credentials: false`; read-only permissions except the
  publish job's; `${{ }}` values passed to scripts through `env`, never
  inside `run`; job and step timeouts; concurrency cancellation, with
  literal group prefixes (in a called workflow, `github.workflow` names the
  caller); the path filters; `cache-mode: none` in release.yml; and the
  cache of the browsers' system packages (the runners' Ubuntu mirror is at
  times too slow for them). The TypeScript version appears in
  interface.yml, README.md and here.

## Safety rules (non-negotiable)

- Everything is receive-only: never add transmit features. A SoapySDR
  source names the receive direction alone, reads and writes only the
  device's own settings and its receive channel's, never a transmit
  channel's, and neither shows nor writes a setting named for
  transmission; a HackRF's `bias_tx` powers its socket in reception too,
  and is antenna power, under the rule below, nothing else.
- Antenna power, an rtl_tcp source's or a SoapySDR device's setting that
  powers its socket (`soapy.is_antenna_power`), is off at each opening
  whatever the device or a saved file says, turned on only by the user's
  second press or `--bias-tee` at start, never saved, and turned off
  before the receiver is released: never power an antenna socket unasked,
  and never turn it on in a check on real hardware.
- Never open the SDR receiver, start `rtl_tcp` or tune real hardware unless
  the user asks for it in the current conversation. Running the interface
  without a receiver is fine. `radiolack --rtl-sdr` starts rtl_tcp, which
  opens the receiver, as soon as an interface opens (the default window
  included), or at start with `--scan`, and `radiolack --rtl-tcp` tunes
  the receiver behind a running rtl_tcp, as `tools/measure_pll.py` tunes
  the receiver behind the Radiolack it drives: tests and checks use the
  fake rtl_tcp server of `tests/fakes.py` or the fake rtl_tcp program
  `tests/fake_rtl_tcp.py` instead. `radiolack --soapy ARGS` searches for
  and opens a SoapySDR device, an RTL-SDR included with `driver=rtlsdr`,
  just as `--rtl-sdr` opens its receiver, and so do choosing a device in
  the Receiver menu's SoapySDR list and its "SoapySDR device arguments"
  field; the menu's search, at its first showing and on "Search again",
  opens no stream but has each listed driver's module talk to the devices
  plugged in, some opening them to read their serial number: never run
  them, nor call `SoapySDR.Device`, `SoapySDR.Device.enumerate` or
  `SoapySDRUtil`, outside the stand-in module of `tests/soapysdr/` unless
  the owner asks for it in the current conversation, on the receiver the
  owner names. To look at the interface, run Radiolack with temporary
  `XDG_CONFIG_HOME` and `XDG_DATA_HOME` through a launcher that puts
  `tests/soapysdr` first on the import path and declares stand-in
  devices, never the plain command, which would find the real module.
  `radiolack` without any of these options
  reopens the source used last, which may be a receiver, a SoapySDR device
  included, read at start
  with `--scan`, and choosing a receiver in the Receiver menu's list opens
  it: a check started with a temporary `XDG_CONFIG_HOME` begins on the
  simulated band.
- Never install, upgrade or remove the Debian package, or any system
  package: that needs sudo, which the owner runs.
- Never enable, start, restart or edit `radiolack.service`, or any other
  system service: that needs sudo, which the owner runs, and the service
  opens the receiver and turns remote access on, on the owner's network.
  The unit is checked with `systemd-analyze` and the tests, which run the
  maintainer scripts on a copy of the package's files, with stand-ins for
  the commands that act on this computer.
- Never use, export or change the owner's GnuPG keys (`~/.gnupg`), never
  sign anything with them and never handle a passphrase: the owner signs
  the APT repository. Tests and checks sign with throwaway keys in a
  temporary `GNUPGHOME`. Never push to the `gh-pages` branch: the owner
  publishes the APT repository there. Replace
  `packaging/radiolack-archive-keyring.gpg` only with a public key that the
  owner exported.
- Never connect to Gqrx (it may be running: connecting to its remote control
  starts its DSP) and never write its configuration (`~/.config/gqrx/`).
- Tests use ephemeral ports, never fixed ones (in particular not 1234, 1235,
  7356 or 8443), never open a window and never reach the network: the
  update check's tests use stand-ins for apt, or `file:` repositories signed
  with throwaway keys. Only `tests/test_browser.py`
  launches browsers: headless, and only when `RADIOLACK_BROWSER_TESTS=1` is
  set, as in CI.
- The server listens on 127.0.0.1 only. Only remote access listens on other
  interfaces: off at each start, HTTPS and access code, and turned on only by
  this computer: its interface, in Settings, or the `--remote` option given
  at start. Never turn it on implicitly (no other option, setting or saved
  state turns it on), and never on the owner's network for a check,
  `radiolack --remote` included: the tests make it listen on 127.0.0.1,
  or move to 127.0.0.2, loopback too on Linux (`remote_addresses`), and
  `smoke_test.py --remote` runs in CI's containers or in a network
  namespace of its own (`unshare -rn` with a dummy interface).
- `data/` holds the airband explorer's personal data: apart from its README,
  never write, replace, regenerate or delete anything there, above all
  `data/airband-peaks.json`. `profiles/` holds personal files: leave them
  untouched and untracked.
- `~/.config/radiolack` (or `$XDG_CONFIG_HOME/radiolack`) holds the owner's
  settings and bookmarks, and `~/.local/share/radiolack` (or
  `$XDG_DATA_HOME/radiolack`) the owner's recordings: never read, write or
  delete them. A check that runs Radiolack sets `XDG_CONFIG_HOME` and
  `XDG_DATA_HOME` to temporary folders.
- Never modify or delete anything in `~/code/pc_repair`.
- Recording (scoping.md, 2026-11-14, for v3): the audio and the raw I/Q
  only when a user starts it, never at start and never on the simulated
  band; never transcriptions. Tests and checks record only synthetic
  signals, the fake rtl_tcp or the stand-in SoapySDR module's devices,
  into temporary folders, never a real receiver.
- Only stop processes that Radiolack started itself.
- Versioned files stay generic: no personal frequencies, places, call signs
  or data.

## Conventions

- English only, everywhere in the repository: code, identifiers, comments,
  documentation, tests and the interface (labels, messages, tooltips,
  accessibility text). No other language and no translation layer (decided
  2026-11-09). Chat with the owner may be in French; nothing in French lands
  in the repository.
- No emoji in the interface; extend the mockup's visual style.
- The interface's Content-Security-Policy forbids inline scripts and inline
  style attributes: style through classes or `element.style`.
- Code that runs every animation frame writes to the DOM only when a value
  changes: in WebKitGTK, rewriting an unchanged text still repaints.
- Disk errors stay distinct from receiver errors; recoverable disconnections
  are waiting states, not errors.
- Keep the server compatible with aiohttp 3.8 (Debian 12) and the latest
  release (CI).
- The band plan is a detailed guide, not a legal reference: keep
  `bandplan-europe.json` generic (no operators, broadcasters, call signs,
  places or local data), sourced from the primary documents that
  docs/design/band-plan.md names, every edge and name from a document read,
  names written for Radiolack and nothing copied from another program's
  band plan, one entry per line and in English (ASCII);
  `tests/test_bandplan.py` checks its structure.
- Update README.md, and docs/design/ when relevant, with every user-facing
  change.
- Radiolack is GPL-3.0-or-later (`LICENSE`): a new dependency, or code or
  data taken from elsewhere, needs a licence compatible with it.
  `pyproject.toml` leaves the licence out while a supported system's
  setuptools cannot read its SPDX form (setuptools 77; Debian 12 has 66).
- Versions have three numbers, X.Y.Z (Y for new features, Z for fixes). A
  release is an annotated tag `vX.Y.Z` on `main` that matches `__version__`;
  between releases `main` holds the next `.dev0`. A published tag never
  moves.
- Check for yourself, every time, whatever a command can tell you: CI
  results, a release's run, a publication, a merge state, an installed
  package. Never ask the owner whether checks have passed: wait for running
  ones with one bounded watch (`gh pr checks N --watch`, `gh run watch ID
  --exit-status`), then read their outcome. Ask the owner only for what needs
  them: a decision, their approval of the `release` environment, sudo, their
  key (decided 2026-11-15).
