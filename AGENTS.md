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

- `src/radiolack/`: `cli` (entry point: `--ui`, `--port`, `--rtl-sdr`,
  `--rtl-tcp`), `server` (aiohttp on 127.0.0.1, own event loop in a
  background thread; interface files and the `/stream` WebSocket), `engine`
  (reads a source, removes the DC offset, shares spectrum frames, the source
  state, the demodulation settings and the bookmarks with clients, sends
  audio to those that ask, passes their changes on, switches between kinds
  of sources, restores and saves the settings and the source in use; idle
  without clients), `store` (settings and bookmarks files in
  `$XDG_CONFIG_HOME/radiolack`: whole-file writes, unreadable files set
  aside), `bookmarks` (bookmarks and their checks), `spectrum`
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
- `packaging/`: `build_deb.py` (Debian package, built with dpkg-deb from the
  Python package as it is; reproducible: every file carries the last
  commit's date, or SOURCE_DATE_EPOCH), `radiolack.1` (manual page, whose
  version and date the build fills in; the tests check that it documents
  every option of `cli.build_parser()` and no other), `smoke_test.py`
  (checks a Radiolack from the outside: listening on 127.0.0.1 only, Host
  and Origin refusals, interface files and Content-Security-Policy, stream,
  stop on SIGTERM; `--window` checks that the window's toolkit loads),
  `release.py` (tags a release on `main` and starts the next development
  version), `radiolack.desktop` (menu entry), `radiolack.svg` (icon) and
  `copyright` (the package's licence notice). The window's program name,
  `window.PROGRAM`, matches the menu entry's and the icon's names: change
  them together.
- `docs/design/protocol.md`: the stream between engine and interface. Change
  it with the engine, the server and `stream.js`, and raise the protocol
  version for any incompatible change.
- `tests/`: unittest tests without hardware; pytest runs them too.
  `test_browser.py` drives the interface in headless browsers, on request.
  `tests/__init__.py` points `XDG_CONFIG_HOME` at a temporary folder for the
  whole run, which the programs the tests start inherit; those that import
  the package too keep it rather than make their own, and the run removes it
  at its end.

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
- CI (GitHub Actions, `.github/workflows/`): `tests.yml` runs the unittest
  command against the package installed with pip on Python 3.11 and 3.15;
  `interface.yml` runs the type check and the browser tests when the
  package, the tests or pyproject.toml change; `package.yml` builds the
  Debian package twice and compares the builds, then installs, runs
  (`smoke_test.py --window` under Xvfb) and removes it on Debian 12 and 13
  and Ubuntu 24.04 and 26.04, with lintian on Debian 13, when the package or
  packaging change; `workflows.yml` runs actionlint
  (`.github/actionlint.yaml`) and zizmor when `.github/` changes;
  `release.yml`, on a `vX.Y.Z` tag, checks the tag, calls tests.yml,
  interface.yml and package.yml (`workflow_call`) and publishes the package,
  its SHA256SUMS and, for a public repository, an attestation, from the
  `release` environment. Dependabot (`.github/dependabot.yml`) updates the
  actions and Playwright weekly, after a seven-day cooldown. Keep: every
  action pinned to a full commit with its version in a comment; checkouts
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
- Never connect to Gqrx (it may be running: connecting to its remote control
  starts its DSP) and never write its configuration (`~/.config/gqrx/`).
- Tests use ephemeral ports, never fixed ones (in particular not 1234, 1235,
  7356 or 8443), and never open a window. Only `tests/test_browser.py`
  launches browsers: headless, and only when `RADIOLACK_BROWSER_TESTS=1` is
  set, as in CI.
- The server listens on 127.0.0.1 only. Listening on other interfaces belongs
  to remote access (v1): off by default, HTTPS and access code, never turned
  on implicitly.
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
