# Radiolack agent guide

Receive-only SDR receiver for the amateur radio bands, being rewritten on the
`rewrite` branch (Python 3.11+, aiohttp; web interface shown in a native
window). User documentation is in README.md. Design decisions, milestones and
open questions are in docs/design/scoping.md: read it before substantive work.
docs/design/ui-mockup.html is the visual reference, and docs/design/lessons.md
keeps what the rewrite reuses from the airband explorer, which stays on `main`.

## Layout

- `src/radiolack/`: `cli` (entry point: `--ui`, `--port`), `server` (aiohttp
  on 127.0.0.1, own event loop in a background thread; interface files and
  the `/stream` WebSocket), `engine` (reads a source and shares spectrum
  frames with clients; idle without clients), `spectrum` (display FFT with
  NumPy), `synthetic` (simulated 2 m band), `window` (GTK 3 and WebKitGTK
  through PyGObject; the main thread runs GTK).
- `src/radiolack/web/`: interface files, served as they are: ES modules, no
  build step. `stream.js` (stream client), `gl.js`, `spectrum-gl.js` and
  `waterfall-gl.js` (WebGL2 views) carry JSDoc types; `main.js` still holds
  the mockup's code under `// @ts-nocheck` and gets types as it is rewritten.
- `docs/design/protocol.md`: the stream between engine and interface. Change
  it with the engine, the server and `stream.js`, and raise the protocol
  version for any incompatible change.
- `tests/`: unittest tests without hardware; pytest runs them too.

## Commands

- Tests: `python3 -m pytest` or `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Type check of the interface: `npx --yes --package=typescript@7.0.2 -- tsc -p jsconfig.json`
  (needs Node.js; not run by CI yet).
- Run without installing: `PYTHONPATH=src python3 -m radiolack` (window) or
  `--ui none` (address only).
- CI: `.github/workflows/tests.yml` (GitHub Actions) runs the unittest
  command against the installed package on Python 3.11 and 3.15; keep its job
  and step timeouts and its concurrency cancellation.

## Safety rules (non-negotiable)

- Everything is receive-only: never add transmit features.
- Never open the SDR receiver, start `rtl_tcp` or tune real hardware unless
  the user asks for it in the current conversation. Running the interface
  without a receiver is fine.
- Never connect to Gqrx (it may be running: connecting to its remote control
  starts its DSP) and never write its configuration (`~/.config/gqrx/`).
- Tests use ephemeral ports, never fixed ones (in particular not 1234, 1235,
  7356 or 8443), and never open a window or a browser.
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
- Update README.md, and docs/design/ when relevant, with every user-facing
  change.
