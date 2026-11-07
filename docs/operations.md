# Operations

Commands for real reception sessions. Nothing here is run by the tests.

## Before a session

- Release the dongle: close any Gqrx instance using it directly over USB
  and stop the prototype scripts (`airband-scan.py`, `airband-peaks.py`) and
  any other `rtl_tcp`. Only one program can open the RTL-SDR.
- Run the read-only readiness report (tools, USB device, ports, Gqrx
  reachability and the process holding its remote control, store, lock):

  ```bash
  radiolack check
  ```

  A `WARN` on the Gqrx remote control naming another program (for example a
  Gqrx started with an older profile) means that program holds port 7356:
  close it through its window. RadioLack never drives a Gqrx that does not
  read its bridge, and the RadioLack Gqrx could not open its remote control.

- Make sure the working store exists (`radiolack import data/airband-peaks.json`),
  or add `--create-store` to start an empty one.
- Create the separate Gqrx profile once with `radiolack gqrx-profile`. With
  custom ports, pass the same `--bridge` and `--gqrx` values to
  `gqrx-profile` and to `scan`/`watch`.

## Full-band session

Terminal 1:

```bash
radiolack scan
```

Terminal 2 (use the absolute path printed by `gqrx-profile`):

```bash
gqrx -c "$PWD/data/gqrx-radiolack.conf"
```

Sequence: RadioLack starts `rtl_tcp` on 127.0.0.1:1234 (log in
`data/rtl_tcp.log`) and its bridge on 127.0.0.1:1235, then waits. Gqrx opens
the bridge as its `rtl_tcp` input. RadioLack connects to Gqrx's remote
control on 127.0.0.1:7356, saves Gqrx's settings, sets LNB offset 0, the
profile's mode and filter, mutes, and starts Gqrx's DSP. Exploration begins.

Console lines (UTC times):

| Line | Meaning |
| --- | --- |
| `WINDOW n/32 \| ... MHz \| FFT center ...` | A window move was confirmed by the I/Q stream. |
| `LISTEN f MHz \| margin ... \| squelch ... verified \| memory new/reapplied` | Ten-second listen started. |
| `SQL GUARD f MHz \| carrier ... \| stored noise ... \| squelch ...` | Memorized squelch lowered at once: to the stored noise + 3 dB, or to carrier - 3 dB when no stored noise is credible ("no usable stored noise"). |
| `SQL LEARNED f MHz \| noise ... \| squelch ...` | Squelch set from a noise level measured during this visit. |
| `Waiting: the Gqrx remote-control port ... belongs to PID ...` | Another program holds the remote control; RadioLack waits and never drives it. |
| `SAVED f MHz \| active ... \| validated listens n` | Observation written. |
| `REJECTED f MHz \| active ...` | Listen completed but not conclusive; nothing written. |
| `Listen cancelled: ...; not logged.` | Interruption; nothing written. |

Do not tune manually in full-band mode: a manual VFO change cancels the
listen, and Gqrx not following the window plan for 6 s stops the monitor.
Gqrx's zoom does not matter: detection always inspects the whole received
window.

## Fixed-window session

```bash
radiolack watch
```

RadioLack watches the current Gqrx window. Moving the panorama manually is
allowed; a hardware retune during a listen cancels it.

## External rtl_tcp

```bash
rtl_tcp -a 127.0.0.1 -p 1234 -s 2400000 -f 136500000 -g 29.6
```

```bash
radiolack scan --external-source
```

RadioLack never stops an external `rtl_tcp`.

## Stopping

Ctrl+C in the monitor's terminal, or from any terminal:

```bash
radiolack stop
```

`stop` signals only the process that holds the store's lock and whose
command line is RadioLack's. Gqrx's saved settings are restored and the
current listen, if any, is not logged. Then close Gqrx through its window:
killing it leaves `crashed=true` in its profile and shows a "Crash Detected"
dialog at the next launch (answering No loads the profile normally). To go
back to direct USB reception, close both and open your usual Gqrx profile,
which RadioLack never modifies.

## After a session

```bash
radiolack stats
```

```bash
radiolack stats --frequency 121.5 --json
```

The store can be copied at any time for a backup: writes are atomic.

## Hardware trial checklist

These manual checks complement the simulated tests, which cannot prove RF
behaviour, real Gqrx timing or audio intelligibility:

1. `radiolack check` reports the tools, the dongle and free ports.
2. `radiolack scan` starts; `data/rtl_tcp.log` shows no device error.
3. Gqrx connects; `WINDOW` lines advance through all windows and wrap.
4. An active frequency produces `LISTEN` then `SAVED`; audio is audible
   during the listen and muted between listens.
5. `radiolack stop` ends the session; Gqrx returns to its previous settings.
6. In `radiolack stats --json` and the new observations, quiet-settled meter
   readings sit below active-settled ones (see docs/squelch.md); adjust
   `--meter-guard-seconds` if needed.
