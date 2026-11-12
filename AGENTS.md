# Radiolack agent guide

Receive-only SDR receiver for every frequency its receivers cover, with
ham-focused band plans (Python 3.11+, aiohttp; web interface shown in a
native window), rewritten from scratch: v0 replaced the airband explorer on
`main` on 2026-11-10, and work continues there. User documentation is in
README.md. Design decisions, milestones and open questions are in
docs/design/scoping.md: read it before substantive work.
docs/design/ui-mockup.html is the visual reference, and docs/design/lessons.md
keeps what the rewrite reuses from the airband explorer, whose code stays in
the history under the tag `v0.1.0`.

## Layout

- `src/radiolack/`: `cli` (entry point: `--ui`, `--port`, `--remote`,
  `--rtl-sdr`, `--rtl-tcp`), `server` (aiohttp on 127.0.0.1, own event loop
  in a background thread; interface files and the `/stream` WebSocket; remote
  access's HTTPS listener, a second application with a runner of its own,
  in the same loop), `remote` (remote access: the local certificate
  authority, with `cryptography`, the access code, paired devices with
  their sessions and interface preferences, the private addresses to listen
  on), `engine`
  (reads a source, removes the DC offset, shares spectrum frames, the source
  state, the demodulation settings, the bookmarks, the scanner state, the
  signals found and the update check's findings with clients, sends audio
  to those that ask, passes their changes on, switches between kinds of
  sources, restores and saves the settings and the source in use; keeps the
  interfaces' preferences, this computer's
  and each paired device's; runs the memory scan, which tunes the VFO for
  every client and, with a receiver that tunes, visits the ticked
  bookmarks outside the span window after window, and the sweep, which
  steps such a receiver across a range, one scan at a time, saving the
  receiver's settings of its start and bringing the receiver back once
  stopped;
  keeps the observations, the simulated band's excepted, and
  sends them to the client that asks; idle without clients unless a scan
  runs; remote clients get neither the storage, update nor remote access
  states, and lighter spectrum frames),
  `updates` (update check: apt run
  as the user, with Radiolack's APT source alone and none of the system's
  apt configuration, in a temporary folder; only for the Debian package's
  Radiolack), `store` (settings, bookmarks, observations and remote access
  files in
  `$XDG_CONFIG_HOME/radiolack`: whole-file writes, readable by the user
  only, unreadable files set aside), `bookmarks` (bookmarks and their checks),
  `scanners` (detection on the engine's FFTs, averaged over a third of a
  second, against the local noise; the list of the signals found and the
  ignored frequencies; the memory scan's watch of the ticked bookmarks'
  channels in the same FFTs, and its states; the windows that the ticked
  bookmarks take and the memory scan's visits; the sweep's windows across a
  range and its progress), `observations` (the channels
  where signals were found, for the History: how signals join a channel,
  its counters, the limits and the checks of the saved file),
  `preferences` (the interfaces' preferences and their checks), `spectrum`
  (display FFT with NumPy, noise estimate around the VFO), `dsp` (FIR design
  and overlap-save FFT filters that shift and resample), `demod` (one VFO:
  channel level, squelch, audio at 12 kHz in every mode), `rtltcp` (rtl_tcp client: `RtlTcpSource` for an
  rtl_tcp the user runs, whose address the interface can change,
  `RtlSdrSource` runs rtl_tcp itself while it connects; a lost server is a
  waiting state), `sources` (opens each kind of source from its saved
  record, for the Receiver list and at start), `synthetic` (simulated 2 m
  band), `window` (GTK 3 and WebKitGTK through PyGObject; the main thread
  runs GTK; an ephemeral WebKit context writes nothing to disk).
- `src/radiolack/web/`: interface files, served as they are: ES modules, no
  build step. `stream.js` (stream client), `audio.js` and
  `audio-worklet.js` (audio output, AudioWorklet playback), `gl.js`,
  `spectrum-gl.js` and `waterfall-gl.js` (WebGL2 views) and `bandplan.js`
  (band plan loader) carry JSDoc types; `main.js` still holds the mockup's code under
  `// @ts-nocheck` and gets types as it is rewritten. `bandplan-europe.json`
  is the band plan data ([docs/design/band-plan.md](docs/design/band-plan.md)).
  `pair.html`, `pair.css` and `pair.js` (typed) are the pairing page that
  remote access serves to a device that is not paired.
- `packaging/`: `build_deb.py` (Debian package, built with dpkg-deb from the
  Python package as it is; reproducible: every file carries the last
  commit's date, or SOURCE_DATE_EPOCH), `radiolack.1` (manual page, whose
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
  the key's fingerprint, which the tests check), `radiolack.desktop` (menu
  entry), `radiolack.svg` (icon) and
  `copyright` (the package's licence notice). The window's program name,
  `window.PROGRAM`, matches the menu entry's and the icon's names: change
  them together.
- `docs/design/protocol.md`: the stream between engine and interface. Change
  it with the engine, the server and `stream.js`, and raise the protocol
  version for any incompatible change.
- `tests/`: unittest tests without hardware; pytest runs them too.
  `test_browser.py` drives the interface in headless browsers, on request.
  `tests/__init__.py` points `XDG_CONFIG_HOME` at a temporary folder for the
  whole run, and `GNUPGHOME` at an empty GnuPG home in it, which the
  programs the tests start inherit; those that import the package too keep
  it rather than make their own, and the run removes it at its end.

## Commands

- Tests: `python3 -m pytest` or `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Type check of the interface: `npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json`
  (needs Node.js).
- Browser tests: `RADIOLACK_BROWSER_TESTS=1 python3 -m unittest tests.test_browser -v`,
  with the `browser` extra (Playwright, pinned in pyproject.toml) and its
  browsers installed (README.md, Tests).
- Run without installing: `PYTHONPATH=src python3 -m radiolack` (window) or
  `--ui none` (address only).
- Debian package: `python3 packaging/build_deb.py --maintainer "Name <address>"`
  writes `dist/radiolack_VERSION_all.deb` (needs dpkg-deb);
  `tests/test_packaging.py` builds one twice in a temporary folder, compares
  them and runs `packaging/smoke_test.py` on the package's files. On an
  installed package: `python3 packaging/smoke_test.py`.
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
  `interface.yml` runs the type check and the browser tests when the
  package, the tests or pyproject.toml change; `package.yml` builds the
  Debian package twice and compares the builds, puts it in an APT
  repository signed with a key made for the run, then installs it from
  there, checks that apt reads its APT source, runs it (`smoke_test.py
  --window --check-updates --remote` under Xvfb: its update check reads the
  published APT repository, and remote access listens on the container's
  network), removes it (its source and key stay) and purges it
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

- Everything is receive-only: never add transmit features.
- Never open the SDR receiver, start `rtl_tcp` or tune real hardware unless
  the user asks for it in the current conversation. Running the interface
  without a receiver is fine. `radiolack --rtl-sdr` starts rtl_tcp, which
  opens the receiver, as soon as an interface opens (the default window
  included), and `radiolack --rtl-tcp` tunes the receiver behind a running
  rtl_tcp: tests and checks use the fake rtl_tcp server of `tests/fakes.py`
  or the fake rtl_tcp program `tests/fake_rtl_tcp.py` instead. `radiolack`
  without either option reopens the source used last, which may be a
  receiver, and choosing a receiver in the Source panel's list opens it: a
  check started with a temporary `XDG_CONFIG_HOME` begins on the simulated
  band.
- Never install, upgrade or remove the Debian package, or any system
  package: that needs sudo, which the owner runs.
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
  `radiolack --remote` included: the tests make it listen on 127.0.0.1
  (`remote_addresses`), and `smoke_test.py --remote` runs in CI's
  containers or in a network namespace of its own (`unshare -rn` with a
  dummy interface).
- `data/` holds the airband explorer's personal data: apart from its README,
  never write, replace, regenerate or delete anything there, above all
  `data/airband-peaks.json`. `profiles/` holds personal files: leave them
  untouched and untracked.
- `~/.config/radiolack` (or `$XDG_CONFIG_HOME/radiolack`) holds the owner's
  settings and bookmarks: never read, write or delete it. A check that runs
  Radiolack sets `XDG_CONFIG_HOME` to a temporary folder.
- Never modify or delete anything in `~/code/pc_repair`.
- Do not record audio, transcriptions or raw I/Q (open question in
  scoping.md; the rule holds until the user decides otherwise).
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
- The band plan is a simplified guide, not a legal reference: keep
  `bandplan-europe.json` generic, sourced (docs/design/band-plan.md), one
  entry per line and in English (ASCII); `tests/test_bandplan.py` checks its
  structure.
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
