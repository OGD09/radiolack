# RadioLack agent guide

Receive-only radio exploration package (Python 3.11+, NumPy). User-facing
documentation is in README.md; design notes are in docs/.

## Layout

- `src/radiolack/`: `receiver` (rtl_tcp child, I/Q bridge, capture),
  `gqrx` (remote control, separate profile), `detection` (FFT peaks, channel
  grid), `scanning` (window plan, explorer), `listening` (listen validation,
  telemetry), `squelch` (learning), `monitor` (main loop), `storage` (store,
  legacy import), `stats`, `profiles`, `cli`, `errors`.
- `src/radiolack/builtin_profiles/`: versioned band profiles (TOML).
- `tests/`: hardware-free unit tests; `tests/integration/`: simulated
  rtl_tcp and Gqrx over local sockets.

## Commands

- Tests: `python3 -m pytest` or `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
- Run without installing: `PYTHONPATH=src python3 -m radiolack --help`

## Safety rules (non-negotiable)

- Everything is receive-only: never add transmit features.
- Never start a real listening session (`scan`, `watch`) and never run
  `check` against the real receiver unless the user asks for it in the
  current conversation. Gqrx may be running: connecting to its remote control
  starts its DSP (`U DSP 1`). Tests must use ephemeral ports, never the
  defaults (1234, 1235, 7356).
- `data/airband-peaks.json` is the migrated history: never write, replace,
  regenerate or delete it. New observations go to a separate store.
- Never modify or delete anything in `~/code/pc_repair`.
- Never write Gqrx's usual profile (`~/.config/gqrx/default.conf`); only
  stop child processes started by RadioLack itself.
- Do not record audio, transcriptions or raw I/Q.
- `data/` and personal profiles stay out of version control; versioned
  examples must be generic and free of personal data.

## Conventions

- Code, comments and documentation in English.
- Disk errors (`StorageError`) must stay distinct from radio errors
  (`ReceiverError`, `GqrxError`); recoverable disconnections are waiting
  states, not errors.
- Legacy rows are never rewritten or given current parameters; unknown
  measurements stay `null` with supporting counts.
- Squelch values come from Gqrx's meter, never from FFT bins; see
  docs/squelch.md before changing the learner.
- Update README.md (and docs/ when relevant) with every user-facing change.
