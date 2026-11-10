# Interface protocol

How the engine and the interface talk. Protocol version 4, 2026-11-09.

The interface opens one WebSocket to `/stream` on the server that served it.
The server accepts the connection only when its `Host` header names the
server (`127.0.0.1:<port>` or `localhost:<port>`) and its `Origin` header is
the interface's own origin (`http://127.0.0.1:<port>` or
`http://localhost:<port>`). Browsers always send `Origin` with a WebSocket
handshake, so a page from another site cannot read the stream.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 4}
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
    "settable": ["address", "centre_hz", "sample_rate_hz", "gain_db", "ppm", "dc_removal"]}
   ```

   (`gains_db` shortened here.)

   | Field | Meaning |
   | --- | --- |
   | `kind` | The source: `synthetic` for the simulated 2 m band, `rtl_sdr` for an RTL-SDR receiver on the engine's computer, through an rtl_tcp that the engine runs, `rtl_tcp` for an RTL-SDR receiver behind an rtl_tcp server that the user runs. |
   | `status` | `streaming` while samples arrive. Otherwise `connecting`, `waiting` (to try again; see `detail`) or `idle` (no interface listened, so the source hung up). |
   | `detail` | Why the source waits, such as `connection refused`, `connection lost`, `no samples`, `not an rtl_tcp server`, `no receiver found` or `the receiver is in use by another program`; empty otherwise. |
   | `address` | `host:port` of the rtl_tcp server of an `rtl_tcp` source (an IPv6 host in brackets), which an interface may change; null for the other sources. |
   | `tuner` | Name of the receiver's tuner once known, or null. |
   | `centre_hz`, `sample_rate_hz` | The settings asked of the receiver. Each frame carries those it was taken with: after a change, frames follow a moment later. |
   | `gain_db` | Manual tuner gain in dB, or null for automatic gain (and for a source without a gain setting). |
   | `ppm` | Frequency correction of the receiver's clock, in whole parts per million; null for a source without one. |
   | `gains_db` | Manual gains the tuner offers, lowest first; empty when it offers automatic gain only. |
   | `range_hz` | Lowest and highest centre frequency the source takes. |
   | `dc_removal` | Whether the engine takes each block's mean off the samples before the FFT, so that a constant offset, such as an RTL-SDR's, does not show as a spike at the centre frequency; a signal exactly on the centre frequency goes with it. True when Radiolack starts. |
   | `settable` | Settings an interface may change; only `dc_removal` for the synthetic source. |

3. Then the demodulation settings, a JSON text message, sent again whenever
   they change, whichever interface changed them. They are the same for
   every interface, apart from `acknowledged`:

   ```json
   {"type": "demod", "frequency_hz": 145600000.0, "mode": "NFM", "bandwidth_hz": 10000.0,
    "squelch_db": -80.0, "squelch_auto": true, "modes": ["NFM", "AM", "USB", "LSB", "CW"],
    "audio_modes": ["NFM", "AM", "USB", "LSB", "CW"], "acknowledged": 0}
   ```

   | Field | Meaning |
   | --- | --- |
   | `frequency_hz` | The VFO, a whole number of Hz. It starts a quarter of the span above the centre frequency. When the span changes, a VFO left outside it moves to its nearest edge; a VFO set outside the span reads nothing and stays silent. |
   | `mode` | One of `modes`. |
   | `bandwidth_hz` | Width of the channel filter: centred on the VFO for NFM, AM and CW, above it for USB, below it for LSB. |
   | `squelch_db` | The squelch threshold set by hand, in dBFS of channel power. |
   | `squelch_auto` | Whether the threshold follows the noise instead: 8 dB above the noise power in the channel, which the engine estimates from the median of the spectrum's bins within 100 kHz of the VFO. |
   | `modes` | The modes the engine takes. |
   | `audio_modes` | The modes it demodulates to audio; the others are measured, level and squelch, but silent. All five for now. |
   | `acknowledged` | The `id` of this interface's last `demod` message that the engine handled, 0 before any (see "From the interface"). |

4. Then the bookmarks, a JSON text message, sent again whenever they
   change, whichever interface changed them:

   ```json
   {"type": "bookmarks", "bookmarks": [
     {"id": 2, "frequency_hz": 145500000.0, "name": "FM calling", "mode": "NFM", "scan": true}]}
   ```

   | Field | Meaning |
   | --- | --- |
   | `id` | Names the bookmark in `bookmark` messages while the engine runs; the engine numbers the bookmarks again at each start. |
   | `frequency_hz` | A whole number of Hz, from 1 Hz to 100 GHz. |
   | `name` | 1 to 60 characters on one line. |
   | `mode` | One of the demodulation settings' `modes`. |
   | `scan` | Whether the memory scan stops on it. |

   They come in frequency order, then by name, ignoring case: at most 2000.

5. Then the preferences, a JSON text message sent once: the latest that an
   interface gave the engine (`preferences` message), kept for the next
   start. They are the interface's own settings, which the engine stores
   without knowing their meaning: up to 100 values, named by 1 to 40
   characters, each a number, true or false, a text of at most 100
   characters or null.

   ```json
   {"type": "preferences", "values": {"palette": "gray", "volume": 70, "showPlan": true}}
   ```

6. Then the storage state, a JSON text message, sent again whenever it
   changes:

   ```json
   {"type": "storage", "directory": "~/.config/radiolack", "saving": true, "problem": ""}
   ```

   | Field | Meaning |
   | --- | --- |
   | `directory` | The settings folder, `~` standing for the home folder; null when the engine saves nothing (a server started without a settings folder, as in tests). |
   | `saving` | False while some changes do not reach the disk: a file could not be written, and the engine tries again at the next change, or a file is left as it is until the next start, as it cannot be read or comes from a newer Radiolack. |
   | `problem` | What went wrong with the files, in sentences for the user, or empty. A file that could not be read and was set aside shows here while `saving` stays true. |

7. Binary spectrum frames, about 30 per second while the source streams.
   Each frame is a 40-byte little-endian header followed by one byte per FFT
   bin:

   | Offset | Type | Field |
   | --- | --- | --- |
   | 0 | uint8 | kind: 1, a spectrum frame |
   | 1 | uint8 | frame format version: 2 |
   | 2 | uint16 | flags: bit 0 set while the squelch is open; the other bits are 0 |
   | 4 | uint32 | sequence number, wrapping at 2^32 |
   | 8 | float64 | centre frequency, Hz |
   | 16 | float64 | sample rate, Hz: the width of the frame |
   | 24 | float32 | level of code 0, dBFS |
   | 28 | float32 | level step per code, dB |
   | 32 | float32 | channel level, dBFS: NaN when nothing is received there |
   | 36 | float32 | squelch threshold in use, dBFS: NaN while unknown |
   | 40 | uint8[] | one code per bin, lowest frequency first |

   With `n` bins, bin `i` is centred on `centre - rate / 2 + i * rate / n`,
   and code `c` stands for `level0 + c * step` dBFS. Code 0 also stands for
   every lower level. A full-scale tone centred on a bin reads 0 dBFS; levels
   are not calibrated in dBm.

   A frame averages a few FFTs of the latest samples, after DC removal when
   it is on. The interface smooths successive frames for its spectrum trace
   and its waterfall.

   The channel level is the mean power, over the same samples, in the VFO's
   channel: a full-scale carrier on the VFO reads 0 dBFS. It is NaN when
   the VFO lies outside the span. The squelch opens once the power averaged
   over 10 ms reaches the threshold and closes 3 dB below it; the flag gives
   its state at the end of the samples. The threshold is `squelch_db`, or
   the automatic one, which is NaN while the engine has no noise estimate
   (the VFO outside the span); the squelch then stays closed.

8. Binary audio frames, about 30 per second, only to an interface that asked
   for audio (`audio` message). Each frame is a 16-byte little-endian header
   followed by 16-bit samples:

   | Offset | Type | Field |
   | --- | --- | --- |
   | 0 | uint8 | kind: 2, an audio frame |
   | 1 | uint8 | audio format version: 1 |
   | 2 | uint16 | reserved, 0 |
   | 4 | uint32 | position of the first sample, counted at the audio rate, wrapping at 2^32 |
   | 8 | float64 | audio rate, Hz |
   | 16 | int16[] | mono samples, full scale 32767 |

   The audio rate is 12 kHz for the usual receiver rates; at an unusual
   sample rate it comes within 0.5% of it, such as 12026.2 Hz at
   2345678 Hz. A frame follows the previous one when its position is the
   previous position plus the previous number of samples; otherwise samples
   are missing, such as frames the engine dropped for a slow client.

   In NFM the audio is the FM discriminator's output, full scale at 5 kHz of
   deviation, with a 750 µs de-emphasis (unit gain at 1 kHz), a high-pass
   filter at 280 Hz that removes CTCSS tones and a low-pass filter at
   3.5 kHz. In AM it is the envelope over the carrier's level, less one:
   the modulation, 0.8 of full scale at 100%, whatever the signal's
   strength. In USB and LSB it is the channel's real part, and in CW too,
   with the VFO at 700 Hz in the channel, so that a carrier on the VFO
   sounds at 700 Hz; an AGC brings a steady signal to 0.4 of full scale and
   lets the gain come back at 10 dB per second. AM, USB, LSB and CW audio
   runs from 100 Hz to 5 kHz. The audio is silent while the squelch is
   closed, with 5 ms fades, and in the modes not in `audio_modes`. It lags
   its samples by the filters' delay and the squelch's look-ahead: about
   40 ms in NFM and AM, 45 ms in USB and LSB, 60 ms in CW.

9. An error, to one interface only, when the engine refuses a message from
   it. `command` is the message's type, or null when it is not a JSON
   object of a known type:

   ```json
   {"type": "error", "command": "source", "message": "the receiver tunes from 24 to 1766 MHz"}
   ```

What is waiting for an interface goes out in this order: the source state,
the demodulation settings, the bookmarks and the storage state, each when it
changed (and the preferences at the start), then errors, then audio frames,
then spectrum frames. A client that reads too
slowly loses its oldest frames: the engine keeps the latest four spectrum
frames and fifteen audio frames (half a second) for it and never slows down
for one client; it keeps its latest states, and up to eight errors. When
Radiolack stops, the server closes the stream with code 1001 (going away).

## From the interface

A `source` text message changes the settings it names, among those listed in
`settable`; the others stay as they are:

```json
{"type": "source", "centre_hz": 433500000}
```

| Field | Value |
| --- | --- |
| `address` | For an `rtl_tcp` source: the rtl_tcp server to read, as `HOST`, `PORT` or `HOST:PORT` (an IPv6 host in brackets), the missing part being `127.0.0.1` or `1234`. Another address hangs up and connects there with the same settings; the tuner is unknown until the server answers. |
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

A `demod` message changes the demodulation settings it names, the same way:

```json
{"type": "demod", "id": 12, "frequency_hz": 145500000, "mode": "NFM"}
```

| Field | Value |
| --- | --- |
| `frequency_hz` | The VFO in Hz, from 1 Hz to 100 GHz, rounded to the Hz. Outside the span it reads nothing. |
| `mode` | One of `modes`. |
| `bandwidth_hz` | From 100 to 25000 Hz. |
| `squelch_db` | From -160 to 0 dBFS. |
| `squelch_auto` | `true` or `false`. |
| `id` | Optional: a whole number from 1 to 2^53 - 1, which the engine returns in `acknowledged`. |

The engine handles an `id` even when it refuses the changes: the sender then
receives the settings, unchanged, before the error. An interface that sends
changes faster than the settings come back, while the VFO is dragged for
instance, gives each message a higher `id` and ignores settings whose
`acknowledged` is lower than its last one: they would undo its latest change
on screen. Settings that carry its last `id` include every change before it,
from any interface. A refused `id`, which is not such a number, refuses the
whole message.

A `bookmark` message adds, changes or removes one bookmark:

```json
{"type": "bookmark", "action": "add", "frequency_hz": 145500000, "name": "FM calling", "mode": "NFM", "scan": true}
{"type": "bookmark", "action": "change", "id": 2, "name": "Calling"}
{"type": "bookmark", "action": "remove", "id": 2}
```

`add` needs `frequency_hz`, `name` and `mode`; `scan` is true unless given.
`change` names a bookmark by its `id` and sets the fields it gives, and
`remove` takes only the `id`. The fields take the values listed under the
bookmarks message: the frequency is rounded to the Hz, and a name loses its
spaces at both ends and keeps single spaces between words. The engine
refuses the whole message, changing nothing, when a field is missing,
unknown or invalid, when the `id` is unknown (another interface may have
removed the bookmark) or when there are already 2000 bookmarks. Otherwise
every interface receives the bookmarks.

A `preferences` message gives the engine this interface's preferences,
which replace the ones it kept:

```json
{"type": "preferences", "values": {"palette": "gray", "volume": 70, "showPlan": true}}
```

The engine saves them and sends them to the interfaces that connect later;
open interfaces receive nothing, so each keeps its own. It refuses values
that do not fit the preferences message.

An `audio` message starts or stops the audio frames for this interface; they
are off when the stream opens:

```json
{"type": "audio", "on": true}
```

The engine demodulates audio only while at least one interface asked for it,
but always measures the channel for the spectrum frames.

While an `rtl_sdr` or `rtl_tcp` source waits, changes are kept and sent when
it connects. Until its first connection, and after a change of address, the
tuner is unknown: `range_hz` spans every frequency an rtl_tcp command can
carry, any gain passes, and the source brings both within what the tuner can
do once it connects.

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
  positive. It ignores audio frames while it has not asked for audio, and
  those with an odd number of sample bytes or a rate that is not a positive
  number. It ignores a source state, demodulation settings, bookmarks or a
  storage state with a missing or malformed field, and leaves out source
  settings it does not know and preferences that are not numbers, true or
  false, texts or null.
- The interface applies the preferences of its page's first connection
  only: after a reconnection, its own stand. It gives the engine its
  preferences within a second of a change, and when its page hides.
- The interface asks for audio while it can play it, with its volume above
  0. It keeps about 150 ms buffered, follows the engine's clock within 0.5%,
  fills a gap of up to 0.5 s with silence, and starts again, buffering
  first, after a longer gap, a change of audio rate or running dry.
- A hello with another protocol version stops the interface, which asks for a
  reload: the page and the engine come from different versions.
- When the stream drops, the interface shows a waiting state and reconnects
  after 250 ms, then after twice as long each time, up to 4 s.
- While its page is hidden, the interface closes the stream, unless it plays
  audio; it opens it again when the page shows.

## Versions

- 4 (2026-11-09): the bookmarks, the preferences and the storage state, and
  the `bookmark` and `preferences` messages. The same day, an `rtl_tcp`
  source's `address` became settable, without a new version: an interface
  leaves out the settings it does not know, and changes only those listed
  in `settable`.
- 3 (2026-11-09): the demodulation settings, the `demod` and `audio`
  messages, audio frames, the channel level and squelch in spectrum frames
  (frame format 2), and the command's type in errors.
- 2 (2026-11-09): the source state, the `source` message from the interface,
  and errors. The hello no longer names the source: the state does.
- 1 (2026-11-09): hello and spectrum frames.
