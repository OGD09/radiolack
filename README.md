# RadioLack

Personal toolkit for radio reception, frequency exploration and observation
analysis. The first module explores the VHF airband: it shares one RTL-SDR
I/Q stream between Gqrx and an FFT peak detector, tunes Gqrx to detected
carriers, validates ten-second listens and keeps per-frequency statistics
and squelch memories.

It was migrated from the `airband-peaks.py` prototype of `~/code/pc_repair`
(which stays untouched) and imports its history without loss.

**Receive-only.** RadioLack never transmits. It records no audio, no
transcription and no raw I/Q: only compact measurements of validated
listens. All its network endpoints are local by default.

## Requirements

- Linux, Python 3.11 or later, NumPy.
- For reception only: an RTL-SDR dongle, `rtl_tcp` (Debian: `sudo apt install rtl-sdr`)
  and Gqrx 2.15 (Debian: `sudo apt install gqrx-sdr`; developed against 2.15.9).

## Installation

```bash
python3 -m venv --system-site-packages .venv
```

```bash
.venv/bin/pip install --no-build-isolation -e .
```

`--system-site-packages` reuses the system NumPy (Debian `python3-numpy`);
`--no-build-isolation` installs without downloading build tools. With
network access, `.venv/bin/pip install -e ".[test]"` also installs pytest.
The `radiolack` command is then `.venv/bin/radiolack`. Without installing,
run `PYTHONPATH=src python3 -m radiolack` from the repository.

## Quick start

1. Import the existing history into a working store (the source is only read):

   ```bash
   radiolack import data/airband-peaks.json
   ```

   This creates `data/observations.json`. Running it again adds nothing.
2. Check readiness without receiving anything:

   ```bash
   radiolack check
   ```

3. Create the separate Gqrx profile once (never overwrites a file, never
   touches your usual `~/.config/gqrx/default.conf`):

   ```bash
   radiolack gqrx-profile
   ```

4. Start exploring the band (see [docs/operations.md](docs/operations.md)
   for the full procedure). Close any Gqrx instance that uses the dongle
   directly first, then:

   ```bash
   radiolack scan
   ```

   and, in another terminal, open Gqrx with the profile path printed in
   step 3 (`gqrx -c /absolute/path/data/gqrx-radiolack.conf`).
5. Stop with Ctrl+C, or from another terminal:

   ```bash
   radiolack stop
   ```

6. Read the statistics:

   ```bash
   radiolack stats
   ```

## Commands

| Command | Purpose |
| --- | --- |
| `profiles` | List built-in band profiles. |
| `plan` | Show the full-band window plan and verify grid coverage (edges and DC gaps). |
| `gqrx-profile` | Create a separate Gqrx profile reading the bridge, with local remote control. |
| `import SOURCE` | Import a legacy `pc_repair` FFT journal into the store; `--dry-run` reports only. |
| `scan` | Explore the whole band in overlapping windows (full-band mode). |
| `watch` | Observe the current Gqrx window only (fixed-window mode; manual tuning allowed). |
| `stats` | Per-frequency statistics; `--json`, `--sort count\|frequency`, `--limit N`, `--frequency MHZ`. |
| `check` | Read-only readiness report: tools, USB device, ports, Gqrx reachability and the process holding its remote control, store. |
| `stop` | Send SIGTERM to the RadioLack process holding the store lock and wait (`--timeout`, 15 s). |

Main `scan`/`watch` options (defaults in parentheses):

| Option | Meaning |
| --- | --- |
| `--profile` | Built-in profile name or TOML file (`airband-8k33`). |
| `--rtl-tcp`, `--bridge`, `--gqrx` | Endpoints (`127.0.0.1:1234`, `127.0.0.1:1235`, `127.0.0.1:7356`); three distinct ports, bridge and Gqrx on loopback. |
| `--rate`, `--center` | Sample rate (2400000; also 1024000, 1800000, 2048000) and initial center (from the profile). |
| `--gain`, `--device` | `rtl_tcp` tuner gain (29.6 dB) and device index (0). |
| `--external-source` | Use an already running `rtl_tcp` instead of starting one. |
| `--margin-db`, `--min-active-seconds`, `--cooldown-seconds`, `--window-seconds` | Override the profile's detection margin, required RF presence, cooldown and quiet-window time (`--window-seconds` for `scan` only). |
| `--meter-guard-seconds` | Stable FFT state required before a Gqrx meter reading counts as carrier or noise for squelch learning (1.5). |
| `--create-store` | Create an empty store if none exists (otherwise the command asks for an import first). |
| `--rtl-tcp-log` | `rtl_tcp` log file (`DATA_DIR/rtl_tcp.log`). |

## Configuration

- **Data directory**: `--data-dir`, else `$RADIOLACK_DATA_DIR`, else `./data`.
  It holds the store, the `rtl_tcp` log and the generated Gqrx profile.
- **Store**: `--store`, else `$RADIOLACK_STORE`, else `DATA_DIR/observations.json`.
- **Profiles**: a profile defines the band, mode and filter, carrier grid,
  detection margin, listen criteria and exploration timing. The built-in
  `airband-8k33` reproduces the prototype: 118 MHz (inclusive) to 137 MHz
  (exclusive), AM 8 kHz, 8.333 kHz carrier grid, 15 dB margin in two
  consecutive observations, 10 s listens with at least 1 s of RF presence and
  5 s of fresh observations, 20 s cooldown, 1 s per quiet window. Custom
  profiles are TOML files: see [profiles/example.toml](profiles/example.toml)
  and [profiles/README.md](profiles/README.md).

No Canada- or Quebec-specific band plan is shipped: such a profile must be
built from official sources (ISED Canada, NAV CANADA, RAC band plans) before
being relied upon.

## How it works

```
RTL-SDR -> rtl_tcp -> RadioLack bridge -> Gqrx (audio, panorama)
                             |
                             `-> FFT capture (same bytes, never altered)
```

- **I/Q sharing**: the bridge relays bytes unchanged to one Gqrx client with
  bounded buffers and TCP backpressure, forwards Gqrx's `rtl_tcp` commands,
  and tracks the actual hardware center, sample rate and gain controls.
  Samples are discarded for 0.4 s after any receiver change.
- **Detection**: eight Hann-windowed 8192-point FFTs are averaged per
  observation (about ten per second). A peak must stand at least 15 dB above
  its local spectral noise (median over +/-50 kHz) in two consecutive
  observations, and is snapped to the profile's carrier grid. This contrast
  is a digital FFT-bin ratio, not dBm and not an antenna SNR.
- **Exploration**: `scan` walks overlapping windows (32 at 2.4 MS/s for the
  airband) that cover the band edges and each window's DC gap. Every move is
  confirmed from the hardware center seen in the I/Q stream. After a listen
  the explorer moves to the next window even if the carrier persists, and
  wraps around at the band end. `watch` keeps the current window.
- **Listening**: the selected carrier is tuned in Gqrx (mode, filter,
  frequency, then the squelch last, read back), and listened to for 10 s while
  measuring. An observation is stored only after a complete listen with
  enough RF presence and fresh observations; any interruption stores nothing.
  A validation means "a radio signal was present", not "speech was heard".
- **Squelch**: one memory per frequency, learned from Gqrx's S-meter (never
  from FFT bins) and invalidated by any gain, rate, filter or mode change.
  Learning ignores meter readings taken while the FFT state is recent,
  because Gqrx's meter lags the FFT tap. A memory above the measured carrier
  is lowered at once, to the stored noise + 3 dB when that noise is credible:
  see [docs/squelch.md](docs/squelch.md).
- **Gqrx identity**: on Linux, before connecting to the remote control,
  RadioLack checks in `/proc` that the remote-control port belongs to the
  Gqrx reading its bridge. Another program on that port (for example a
  second Gqrx) is never driven.

Details: [docs/architecture.md](docs/architecture.md).

## Data, history and privacy

- `data/` and personal profiles are not versioned (see `.gitignore`).
- `data/airband-peaks.json` is the history migrated from the prototype. It is
  an import source: RadioLack never writes it and refuses to use it as a store.
  All new observations go to the separate working store.
- Import keeps every legacy row unchanged (only `id` and `origin` are added),
  with the thresholds and criteria of its time; old rows are never given
  current parameters or invented measurements. Rows are identified by
  frequency and listen timestamps, so a repeated import adds nothing and does
  not rewrite the store. Rows that differ from a stored row with the same
  identity are reported as conflicts and the stored row is kept; squelch
  memories already in the store are never overwritten by older ones.
- Writes are atomic (temporary file, fsync, rename): a disk error leaves the
  previous store intact and stops the monitor with a storage error, distinct
  from radio errors.
- Counts are validated listens, not conversations, transmissions or users.

Format and field reference: [docs/data-format.md](docs/data-format.md).

## Waiting states and errors

| Situation | What RadioLack does |
| --- | --- |
| Gqrx not started, or not reading the bridge | Prints "Waiting for Gqrx to connect to the I/Q bridge" once and waits. |
| Gqrx remote control disabled or unreachable | Prints "Waiting for Gqrx remote control" once and retries every 2 s. |
| Remote-control port held by another program (for example a second Gqrx) | Never connects to it: prints "Waiting: the Gqrx remote-control port ... belongs to PID ..." and retries every 2 s. Close that program, then enable Tools > Remote control in the Gqrx reading the bridge. |
| Gqrx closed or remote control lost | Cancels the current listen without logging it, then reconnects. |
| No fresh I/Q for 5 s (Gqrx paused, receiver stalled) | Cancels the listen, warns once and waits. |
| Gain, rate or frequency changed by Gqrx | Cancels the listen; samples are invalidated during settling. |
| VFO moved manually during a listen | The listen is not logged. Use `watch` for manual exploration. |
| Gqrx does not move the hardware in `scan` (6 s) | Stops with a Gqrx error (exit 5). |
| `rtl_tcp` missing, dongle absent or busy, port in use | Stops with a receiver error and the cause from the `rtl_tcp` log (exit 4). |
| Bridge port in use | Stops before starting reception (exit 4). |
| Store missing | Asks for `radiolack import ...` or `--create-store` (exit 3). |
| Invalid JSON or unknown format | Stops; the file is left untouched (exit 3). |
| Disk full or store not writable | Stops; the previous store content is intact (exit 3). |
| Store used by another RadioLack | Stops and names the process id (exit 3). |

Exit codes: 0 success; 1 other failure (including an existing Gqrx profile
file, a `check` failure or a `stop` timeout); 2 usage error; 3 storage;
4 receiver; 5 Gqrx; 130 Ctrl+C; 143 SIGTERM (`radiolack stop`).

When the monitor stops (Ctrl+C, `radiolack stop` or an error) while its
Gqrx remote-control connection is alive, the Gqrx settings saved at
connection (frequency, mode and filter, squelch, LNB offset, DSP state) are
restored. Only the `rtl_tcp` child started by this command is ever stopped.

## Tests

Hardware-free tests (unit tests plus simulated end-to-end tests):

```bash
python3 -m pytest
```

or, without pytest:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

- Unit tests use synthetic I/Q, a virtual clock and fake Gqrx/receiver
  objects: detection, coverage, listen validation, squelch learning and
  FFT/meter latency, storage, import, statistics, profiles and the CLI.
- `tests/integration/` runs `radiolack watch` as a subprocess against a
  simulated `rtl_tcp` and a simulated Gqrx on ephemeral local ports (about
  15 s): bit-exact relay, command sequence, ten-second listen before the
  write, squelch read-back, SIGINT and `radiolack stop`. They never use the
  default ports.
- `tests/test_real_history.py` imports `data/airband-peaks.json` (read-only)
  into a temporary store when the file is present, and is skipped otherwise.

GitHub Actions ([.github/workflows/tests.yml](.github/workflows/tests.yml))
runs the suite on every push to `main` and every pull request, on Python 3.11
and 3.15: it installs the package and runs the unittest command without
`PYTHONPATH`, which also checks that the built-in profiles are packaged. No
hardware, secret or personal data is involved; `data/` is not versioned, so
the real-history test is skipped there.

Hardware trials are manual and separate from these tests; see
[docs/operations.md](docs/operations.md).

## Migration notes

| Prototype (`pc_repair`) | RadioLack |
| --- | --- |
| `airband-peaks.py` | `radiolack scan` |
| `airband-peaks.py --fixed-window` | `radiolack watch` |
| `--prepare-gqrx-config FILE` | `radiolack gqrx-profile --output FILE` |
| `--output peaks.json` (read and written) | `--store observations.json`; the legacy journal is imported, never written |
| `--source-port`, `--listen-port`, `--remote-port` | `--rtl-tcp`, `--bridge`, `--gqrx` (`HOST:PORT`) |
| `frequency_stats` stored in the journal | computed on demand by `radiolack stats` |
| `scan.mode` `full_airband` | `full_band` for new rows (legacy rows keep `full_airband`) |
| `telemetry_version` 1 | 2: adds settled meter statistics, squelch changes, the minimum observed time and profile metadata |

The old sequential scanner (`airband-scan.py`) and its JSON format are not
part of RadioLack and are rejected by `import`.

## Limitations

- The store is one JSON document rewritten on each update (about 3 KB per
  validation); fine for tens of thousands of listens, a database may be
  preferable beyond that.
- The squelch guard assumes Gqrx's meter lags the FFT by less than the guard
  (1.5 s by default, `--meter-guard-seconds`). Longer lags are caught by the
  carrier-separation check once the carrier level is known; before that, a
  wrong raise is limited to 2 dB per update and undone by the carrier guard at
  the next settled carrier reading. The actual lag must be checked on hardware.
- One Gqrx client at a time on the bridge; Gqrx must run on the same machine.
- Modes are limited to Gqrx's AM and FM with symmetric passbands.

## Project layout

```
src/radiolack/   package: receiver, gqrx, ownership, detection, scanning,
                 listening, squelch, monitor, storage, stats, profiles, cli
src/radiolack/builtin_profiles/   versioned band profiles
profiles/        example profile; personal profiles (not versioned)
data/            history, store, logs, generated Gqrx profile (not versioned)
docs/            architecture, data format, squelch, operations
tests/           hardware-free tests; tests/integration: simulated pipeline
.github/workflows/   CI: hardware-free tests on Python 3.11 and 3.15
```
