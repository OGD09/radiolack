# Interface protocol

How the engine and the interface talk. Protocol version 1, 2026-11-09.

The interface opens one WebSocket to `/stream` on the server that served it.
The server accepts the connection only when its `Host` header names the
server (`127.0.0.1:<port>` or `localhost:<port>`) and its `Origin` header is
the interface's own origin (`http://127.0.0.1:<port>` or
`http://localhost:<port>`). Browsers always send `Origin` with a WebSocket
handshake, so a page from another site cannot read the stream.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 1, "source": "synthetic"}
   ```

   `source` names the kind of source the engine reads: `synthetic` for the
   simulated 2 m band.

2. Then binary spectrum frames, about 30 per second. Each frame is a 32-byte
   little-endian header followed by one byte per FFT bin:

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

   A frame averages a few FFTs of the latest samples. The interface smooths
   successive frames for its spectrum trace and its waterfall.

A client that reads too slowly loses its oldest frames: the engine keeps the
latest four for it and never slows down for one client. When Radiolack
stops, the server closes the stream with code 1001 (going away).

## From the interface

Nothing yet: the server reads and ignores text messages of up to 64 KiB.
Commands (tuning, settings) come with the next steps of v0.

## Connection life

- The engine computes frames only while at least one client listens.
- The interface ignores binary frames until the hello, and any frame of
  another kind or format version, without bins, or whose centre, rate, level
  or step is not a finite number; the rate and the step must also be
  positive.
- A hello with another protocol version stops the interface, which asks for a
  reload: the page and the engine come from different versions.
- When the stream drops, the interface shows a waiting state and reconnects
  after 250 ms, then after twice as long each time, up to 4 s.
- While its page is hidden, the interface closes the stream; it opens it
  again when the page shows.
