# Interface protocol

How the engine and the interface talk. Protocol version 2, 2026-11-09.

The interface opens one WebSocket to `/stream` on the server that served it.
The server accepts the connection only when its `Host` header names the
server (`127.0.0.1:<port>` or `localhost:<port>`) and its `Origin` header is
the interface's own origin (`http://127.0.0.1:<port>` or
`http://localhost:<port>`). Browsers always send `Origin` with a WebSocket
handshake, so a page from another site cannot read the stream.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 2}
   ```

2. Then the source state, a JSON text message, sent again whenever it
   changes, whichever interface changed it. It also carries `dc_removal`, a
   setting of the engine's own that applies to any source:

   ```json
   {"type": "source", "kind": "rtl_tcp", "status": "streaming", "detail": "",
    "address": "127.0.0.1:1234", "tuner": "Rafael Micro R820T",
    "centre_hz": 145000000.0, "sample_rate_hz": 2400000.0, "gain_db": null, "ppm": 0,
    "gains_db": [0.0, 0.9, 1.4, 2.7, 49.6], "range_hz": [24000000.0, 1766000000.0],
    "dc_removal": true,
    "settable": ["centre_hz", "sample_rate_hz", "gain_db", "ppm", "dc_removal"]}
   ```

   (`gains_db` shortened here.)

   | Field | Meaning |
   | --- | --- |
   | `kind` | The source: `synthetic` for the simulated 2 m band, `rtl_sdr` for an RTL-SDR receiver on the engine's computer, through an rtl_tcp that the engine runs, `rtl_tcp` for an RTL-SDR receiver behind an rtl_tcp server that the user runs. |
   | `status` | `streaming` while samples arrive. Otherwise `connecting`, `waiting` (to try again; see `detail`) or `idle` (no interface listened, so the source hung up). |
   | `detail` | Why the source waits, such as `connection refused`, `connection lost`, `no samples`, `not an rtl_tcp server`, `no receiver found` or `the receiver is in use by another program`; empty otherwise. |
   | `address` | `host:port` of the rtl_tcp server of an `rtl_tcp` source (an IPv6 host in brackets); null for the other sources. |
   | `tuner` | Name of the receiver's tuner once known, or null. |
   | `centre_hz`, `sample_rate_hz` | The settings asked of the receiver. Each frame carries those it was taken with: after a change, frames follow a moment later. |
   | `gain_db` | Manual tuner gain in dB, or null for automatic gain (and for a source without a gain setting). |
   | `ppm` | Frequency correction of the receiver's clock, in whole parts per million; null for a source without one. |
   | `gains_db` | Manual gains the tuner offers, lowest first; empty when it offers automatic gain only. |
   | `range_hz` | Lowest and highest centre frequency the source takes. |
   | `dc_removal` | Whether the engine takes each block's mean off the samples before the FFT, so that a constant offset, such as an RTL-SDR's, does not show as a spike at the centre frequency; a signal exactly on the centre frequency goes with it. True when Radiolack starts. |
   | `settable` | Settings an interface may change; only `dc_removal` for the synthetic source. |

3. Binary spectrum frames, about 30 per second while the source streams.
   Each frame is a 32-byte little-endian header followed by one byte per FFT
   bin:

   | Offset | Type | Field |
   | --- | --- | --- |
   | 0 | uint8 | kind: 1, a spectrum frame |
   | 1 | uint8 | frame format version: 1 |
   | 2 | uint16 | reserved, 0 |
   | 4 | uint32 | sequence number, wrapping at 2^32 |
   | 8 | float64 | centre frequency, Hz |
   | 16 | float64 | sample rate, Hz: the width of the frame |
   | 24 | float32 | level of code 0, dBFS |
   | 28 | float32 | level step per code, dB |
   | 32 | uint8[] | one code per bin, lowest frequency first |

   With `n` bins, bin `i` is centred on `centre - rate / 2 + i * rate / n`,
   and code `c` stands for `level0 + c * step` dBFS. Code 0 also stands for
   every lower level. A full-scale tone centred on a bin reads 0 dBFS; levels
   are not calibrated in dBm.

   A frame averages a few FFTs of the latest samples, after DC removal when
   it is on. The interface smooths successive frames for its spectrum trace
   and its waterfall.

4. An error, to one interface only, when the engine refuses a message from
   it:

   ```json
   {"type": "error", "message": "the receiver tunes from 24 to 1766 MHz"}
   ```

What is waiting for an interface goes out in this order: the source state
when it changed, then errors, then frames. A client that reads too slowly
loses its oldest frames: the engine keeps the latest four for it and never
slows down for one client; it keeps its latest state, and up to eight
errors. When Radiolack stops, the server closes the stream with code 1001
(going away).

## From the interface

A `source` text message changes the settings it names, among those listed in
`settable`; the others stay as they are:

```json
{"type": "source", "centre_hz": 433500000}
```

| Field | Value |
| --- | --- |
| `centre_hz` | Centre frequency in Hz, within `range_hz`, rounded to the Hz. |
| `sample_rate_hz` | Sample rate in Hz. rtl_tcp takes 900001 to 3200000 Hz, librtlsdr's upper range. |
| `gain_db` | Tuner gain in dB, or null for automatic gain. A gain becomes the nearest in `gains_db`. |
| `ppm` | Frequency correction in parts per million, from -100 to 100, rounded to a whole number. The receiver retunes: frames follow a moment later, as after a change of `centre_hz`. |
| `dc_removal` | `true` or `false`. |

The engine applies a valid message at once and sends the new state to every
interface: the last change wins. It refuses a whole message, changing
nothing, when one of its settings is unknown, not settable or invalid, and
answers the sender with an error. It also answers with an error any text
message that is not a JSON object of a known type. Binary messages are
ignored, and messages may be up to 64 KiB.

While an `rtl_sdr` or `rtl_tcp` source waits, changes are kept and sent when
it connects. Until its first connection the tuner is unknown: `range_hz`
spans every frequency an rtl_tcp command can carry, any gain passes, and the
source brings both within what the tuner can do once it connects.

## Connection life

- The engine computes frames only while at least one client listens. The
  `rtl_sdr` and `rtl_tcp` sources connect at the first read and hang up 2 s
  after the last one; the `rtl_sdr` source starts rtl_tcp before connecting
  and stops it on hanging up.
- A source that waits sends no frames, but the stream stays open: the source
  state says why (`status` and `detail`). This differs from the stream itself
  dropping, which the interface shows as waiting for the engine.
- The interface ignores binary frames until the hello, and any frame of
  another kind or format version, without bins, or whose centre, rate, level
  or step is not a finite number; the rate and the step must also be
  positive. It ignores a source state with a missing or malformed field, and
  leaves out settings it does not know.
- A hello with another protocol version stops the interface, which asks for a
  reload: the page and the engine come from different versions.
- When the stream drops, the interface shows a waiting state and reconnects
  after 250 ms, then after twice as long each time, up to 4 s.
- While its page is hidden, the interface closes the stream; it opens it
  again when the page shows.

## Versions

- 2 (2026-11-09): the source state, the `source` message from the interface,
  and errors. The hello no longer names the source: the state does.
- 1 (2026-11-09): hello and spectrum frames.
