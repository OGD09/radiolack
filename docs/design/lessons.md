# Lessons from the airband explorer

The Gqrx-driven airband explorer is removed from the `rewrite` branch. Its
code and documentation stay on `main` (commit `f0e1cf6`: `src/radiolack/`,
`docs/architecture.md`, `docs/squelch.md`). This note keeps what the new
engine reuses; the details are in those files.

## rtl_tcp

- Stream: a 12-byte header (`RTL0`, tuner type, gain count; big-endian
  32-bit integers), then interleaved unsigned 8-bit I/Q samples.
- Commands: 5 bytes, a command byte then a big-endian 32-bit value. The old
  code tracked frequency (1), sample rate (2), gain mode (3), gain (4), AGC
  (8), direct sampling (9), offset tuning (10) and bias tee (14).
- Socket reads and writes can be partial: keep I/Q pairs aligned and keep
  unsent bytes queued.
- After a frequency or sample-rate change, samples still belong to the old
  setting for a while: the old code tagged frames with an epoch and dropped
  0.4 s of samples after each change. A retune counts as done only when a
  frame from the new epoch arrives.
- Buffers stay bounded; when one is full, stop reading and let TCP
  backpressure slow the sender. Never queue without limit. The rewrite's
  rtl_tcp source reads everything instead, keeps the latest 0.5 s of samples
  and hangs up once nothing reads: rtl_tcp queues up to 500 buffers of
  256 KiB (about 130 MB) for a client that stops reading, so backpressure
  would only move stale samples into rtl_tcp.
- Managed child: `rtl_tcp -a 127.0.0.1 -p <port> -f <Hz> -s <rate> -g <dB>
  -d <index> -n 10`, on a loopback address only, its output kept in a log.
  Known messages give the user a hint: "No supported devices found" (no
  dongle), "usb_claim_interface error" (another program holds the dongle),
  "Failed to open rtlsdr device", "Address already in use". Stop only the
  child Radiolack started: terminate, wait 3 s, then kill. The rewrite runs
  `rtl_tcp -a 127.0.0.1 -p <free port> -n 10 -d <device>` for each
  connection and sends the settings as commands, as to any rtl_tcp; it keeps
  the end of rtl_tcp's stderr in memory for those hints, and logs the
  messages it does not know.
- Sample rates used: 1.024, 1.8, 2.048 and 2.4 MS/s.

## Peak detection (for the peak scanner)

- Average several Hann-windowed FFTs: eight of 8192 points, about 27 ms of
  samples at 2.4 MS/s.
- Ignore the filter roll-off (keep bins within 32% of the sample rate from
  the centre) and 3 kHz around DC.
- A peak is a local maximum at least 15 dB above the median of the usable
  bins within 50 kHz, and above the median of the whole window. Snap it to the
  channel grid, require two consecutive observations, then apply a cooldown
  per frequency.
- FFT levels are dBFS bin powers: not calibrated dBm, and not comparable to a
  channel level meter.

## Sweep across receiver windows

- Overlapping windows: first centre at `low + 0.1 * rate`, then steps of
  `0.25 * rate`, last centre at `high - 0.1 * rate`. Check that every channel
  is usable in at least one window, including the DC gap and the edges, for
  hardware centre offsets of up to 15% of the rate.
- Confirm each move by a frame from the new epoch received at least 0.4 s
  after the request; give up after a timeout.

## Squelch

- Measure levels on the demodulated channel, never on FFT bins.
- A level meter lags: just after a carrier stops, it still reads the carrier.
  The prototype learned "noise" at those moments and set its squelch above the
  carrier; in 49 of 227 recorded listens the squelch never opened. Treat a
  reading as noise only after the channel has been quiet for a guard time
  (1.5 s worked with Gqrx's meter).
- Target noise + 3 dB, never above carrier - 3 dB. Lower at once; raise slowly
  (2 dB per step, at least 2 s apart); ignore changes under 1.5 dB.
- In the new engine the demodulator and its meter are Radiolack's own, so
  their lag is known and can be compensated directly.

## Engine behaviour

- Disk errors stay distinct from receiver errors. Recoverable disconnections
  are waiting states with a single message, not errors.
- Usage errors stop the program before any file, socket or process is
  touched.
- Load measured on an i7-7500U at 2.4 MS/s: display FFT and one NFM channel
  take about 15% of one core, 23% with four channels.
