# Architecture

RadioLack is a small, single-process command line package. Responsibilities
are split by module; there is no plugin framework, web interface or service.

## Data flow

```
RTL-SDR --USB--> rtl_tcp (child or external, 127.0.0.1:1234)
                    |
                    v
            RadioLack bridge (127.0.0.1:1235) ----> Gqrx: audio, panorama, S-meter
                    |  (same bytes)                   ^
                    v                                 | remote control (127.0.0.1:7356)
            Capture -> FFT detection -> Monitor ------+
                                          |
                                          v
                               observation store (JSON)
```

Gqrx 2.15.9 does not export its spectrum through its remote protocol, so
detection runs on the shared I/Q stream while Gqrx demodulates the selected
carrier. RadioLack controls Gqrx's VFO, mode, filter and squelch, and reads
its S-meter.

## Modules

| Module | Responsibility |
| --- | --- |
| `receiver` | `rtl_tcp` child process, I/Q bridge thread, frame capture, endpoints and port probes. |
| `gqrx` | Remote-control client (tune, squelch with read-back, meter, gains, restore) and separate profile creation. |
| `ownership` | Processes behind local TCP sockets, read from Linux `/proc`: the remote control is driven only when it belongs to the Gqrx reading the bridge. |
| `detection` | Averaged FFT spectrum, local-noise peak extraction, carrier grid. |
| `scanning` | Overlapping window plan, coverage check, full-band explorer state. |
| `listening` | Ten-second listen validation and per-listen telemetry. |
| `squelch` | Per-frequency squelch learning from settled meter readings. |
| `monitor` | Main loop: connections, exploration, candidate selection, listens, squelch updates. |
| `storage` | Observation store (atomic JSON, lock), squelch memories, legacy import. |
| `stats` | Measurement summaries, per-frequency statistics, global summary. |
| `profiles` | TOML band profiles with strict validation. |
| `cli` | Commands, path and endpoint resolution, exit codes. |
| `errors` | Error families: storage (3), receiver (4), Gqrx (5). |

## Processes and threads

- The main thread runs the monitor loop (one tick every 100 ms).
- One daemon thread runs the bridge; it serves one Gqrx client at a time
  and accepts a new one when Gqrx reconnects.
- `scan` and `watch` start at most one child, `rtl_tcp`, unless
  `--external-source` is given. Only that child is ever stopped.
- Gqrx is never started or stopped by RadioLack.

## I/Q bridge invariants

- The `rtl_tcp` header (`RTL0`, tuner type, gain count) and every sample
  byte are relayed unchanged.
- Buffers are bounded (1 MiB towards Gqrx, 64 KiB towards `rtl_tcp`): when a
  buffer is full, the bridge stops reading that side and TCP backpressure
  slows the sender. A paused or starting Gqrx never causes dropped bytes or
  unbounded memory.
- Reads may be fragmented: Gqrx commands are forwarded and interpreted only
  as complete 5-byte commands, and partial I/Q pairs keep their alignment
  when samples are discarded.
- Writes may be partial: unsent bytes stay queued.
- Gqrx may open its source before starting its DSP; the bridge tolerates
  that delay.
- Commands are observed: frequency (1), sample rate (2) and the other
  controls (for example 3 gain mode, 4 gain, 8 AGC, 9 direct sampling,
  10 offset tuning, 14 bias tee) are tracked. Any changed value increments
  the capture epoch, drops the current frame and discards samples for 0.4 s.
  The frame metadata therefore always gives the actual hardware center.
- The bridge and a managed `rtl_tcp` listen on loopback addresses only.

## Detection

Each observation averages eight Hann-windowed 8192-point FFTs of one frame
(about 27 ms of samples at 2.4 MS/s). Bins are kept when they are within
+/-32 % of the sample rate from the center (the outer roll-off is ignored),
more than 3 kHz from DC, and inside the band. A local maximum is a peak when
it stands at least the margin (15 dB) above the median of usable bins within
+/-50 kHz (and by the same margin above the median of the whole usable
window). Peaks are snapped to the profile's carrier grid (3 kHz tolerance)
and kept once per channel, sorted by decreasing margin. A candidate needs
two consecutive observations above the margin and no active cooldown.

Levels are FFT-bin powers relative to full scale: they are not calibrated
dBm, not an antenna-input SNR and not comparable to Gqrx's S-meter.

## Full-band exploration

- Plan: first focus `low + 0.1 * rate`, last focus `high - 0.1 * rate`, step
  `0.25 * rate` (32 windows for 118-137 MHz at 2.4 MS/s). `radiolack plan`
  verifies that every grid channel is usable in some window for hardware
  center offsets of -15 %, 0 and +15 % of the rate, which covers the band
  edges and each window's DC gap.
- Move: mute (`L SQL 0`), hop to `focus + 2 * rate`, then to `focus`. Gqrx
  keeps small VFO changes inside its current window; the hop forces a real
  hardware retune.
- Confirmation: a frame from a new epoch, received at least 0.4 s after the
  request, whose center is within 18 % of the rate from the focus. Without
  confirmation within 6 s the monitor stops with a Gqrx error.
- A quiet window is observed for 1 s. After a listen, the explorer always
  moves to the next window, even if the carrier persists, and wraps to the
  first window at the end of the band. The cooldown prevents an immediate
  repeat of the same frequency from an overlapping window.

## Listen sequence

1. Mute, set mode and filter, tune the carrier, then apply the squelch last
   and read it back (memorized value, or a provisional value 6 dB below the
   lowest of three settled meter readings).
2. During 10 s, every fresh frame is an observation of the carrier (present
   or not), and Gqrx's meter and squelch are read every 200 ms.
3. At the deadline the VFO is checked (a manual change cancels logging) and
   audio is muted.
4. Validation: full duration, at least 1 s of confirmed presence (credit is
   only given between consecutive observations where the carrier was
   present, at most 0.25 s per gap) and at least 5 s of fresh observations.
5. The observation and the station's squelch memory are written in one
   atomic update. A disk error stops the monitor with a storage error.

Epoch changes, a stale stream (no fresh frame for 0.5 s), a lost Gqrx input
or remote control cancel the listen without writing anything.

## Error model

Storage problems (`StorageError`, exit 3) never look like radio problems
(`ReceiverError`, exit 4; `GqrxError`, exit 5). Recoverable situations
(Gqrx closed, remote control disabled, paused stream) are waiting states with
a single message, not errors. Usage errors exit with 2 before any file,
socket or process is touched.
