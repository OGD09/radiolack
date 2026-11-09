# Radiolack agent guide

Receive-only SDR receiver for every frequency its receivers cover, with
ham-focused band plans, being rewritten on the `rewrite` branch (Python 3.11+,
aiohttp; web interface shown in a native window). User documentation is in README.md. Design decisions, milestones and
open questions are in docs/design/scoping.md: read it before substantive work.
docs/design/ui-mockup.html is the visual reference, and docs/design/lessons.md
keeps what the rewrite reuses from the airband explorer, which stays on `main`.

## Layout

- `src/radiolack/`: `cli` (entry point: `--ui`, `--port`, `--rtl-sdr`,
  `--rtl-tcp`), `server` (aiohttp on 127.0.0.1, own event loop in a
  background thread; interface files and the `/stream` WebSocket), `engine`
  (reads a source, removes the DC offset, shares spectrum frames and the
  source state with clients, passes their changes to the source; idle
  without clients), `spectrum`
  (display FFT with NumPy), `rtltcp` (rtl_tcp client: `RtlTcpSource` for an
  rtl_tcp the user runs, `RtlSdrSource` runs rtl_tcp itself while it
  connects; a lost server is a waiting state), `synthetic` (simulated 2 m
  band), `window` (GTK 3 and WebKitGTK through PyGObject; the main thread
  runs GTK).
- `src/radiolack/web/`: interface files, served as they are: ES modules, no
  build step. `stream.js` (stream client), `gl.js`, `spectrum-gl.js` and
  `waterfall-gl.js` (WebGL2 views) and `bandplan.js` (band plan loader)
  carry JSDoc types; `main.js` still holds the mockup's code under
  `// @ts-nocheck` and gets types as it is rewritten. `bandplan-europe.json`
  is the band plan data ([docs/design/band-plan.md](docs/design/band-plan.md)).
- `docs/design/protocol.md`: the stream between engine and interface. Change
  it with the engine, the server and `stream.js`, and raise the protocol
  version for any incompatible change.
- `tests/`: unittest tests without hardware; pytest runs them too.
  `test_browser.py` drives the interface in headless browsers, on request.

## Commands

- Tests: `python3 -m pytest` or `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Type check of the interface: `npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json`
  (needs Node.js).
- Browser tests: `RADIOLACK_BROWSER_TESTS=1 python3 -m unittest tests.test_browser -v`,
  with the `browser` extra (Playwright, pinned in pyproject.toml) and its
  browsers installed (README.md, Tests).
- Run without installing: `PYTHONPATH=src python3 -m radiolack` (window) or
  `--ui none` (address only).
- CI (GitHub Actions): `.github/workflows/tests.yml` runs the unittest
  command against the installed package on Python 3.11 and 3.15;
  `.github/workflows/interface.yml` runs the type check and the browser tests
  when the package, the tests or pyproject.toml change. Keep their job and
  step timeouts, their concurrency cancellation and the path filters. The
  TypeScript version appears in interface.yml, README.md and here.

## Safety rules (non-negotiable)

- Everything is receive-only: never add transmit features.
- Never open the SDR receiver, start `rtl_tcp` or tune real hardware unless
  the user asks for it in the current conversation. Running the interface
  without a receiver is fine. `radiolack --rtl-sdr` starts rtl_tcp, which
  opens the receiver, as soon as an interface opens (the default window
  included), and `radiolack --rtl-tcp` tunes the receiver behind a running
  rtl_tcp: tests and checks use the fake rtl_tcp server of `tests/fakes.py`
  or the fake rtl_tcp program `tests/fake_rtl_tcp.py` instead.
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
