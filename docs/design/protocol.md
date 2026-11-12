# Interface protocol

How the engine and the interface talk. Protocol version 6, 2026-11-11.

The interface opens one WebSocket to `/stream` on the server that served it.
The server accepts the connection only when its `Host` header names the
server (`127.0.0.1:<port>` or `localhost:<port>`) and its `Origin` header is
the interface's own origin (`http://127.0.0.1:<port>` or
`http://localhost:<port>`). Browsers always send `Origin` with a WebSocket
handshake, so a page from another site cannot read the stream.

A device that remote access serves, a remote client, opens its stream on
remote access's HTTPS server instead, with its session cookie
([Remote access](#remote-access)). It receives the same messages, apart
from this computer's own states, and may not change this computer's own
settings.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 6, "remote": false}
   ```

   `remote` is true for a remote client.

2. Then the source state, a JSON text message, sent again whenever it
   changes, whichever interface changed it. It also carries `dc_removal`, a
   setting of the engine's own that applies to any source:

   ```json
   {"type": "source", "kind": "rtl_tcp", "status": "streaming", "detail": "",
    "address": "127.0.0.1:1234", "tuner": "Rafael Micro R820T",
    "centre_hz": 145000000.0, "sample_rate_hz": 2400000.0, "gain_db": null, "ppm": 0,
    "gains_db": [0.0, 0.9, 1.4, 2.7, 49.6], "range_hz": [24000000.0, 1766000000.0],
    "dc_removal": true,
    "settable": ["address", "centre_hz", "sample_rate_hz", "gain_db", "ppm", "dc_removal"],
    "kinds": ["synthetic", "rtl_sdr", "rtl_tcp"]}
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
   | `settable` | Settings an interface may change; only `dc_removal` for the synthetic source. Never `address` for a remote client. |
   | `kinds` | Kinds of source the engine may switch to, `kind` among them; empty when it reads one source only. |

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

6. Then, to this computer's interfaces only, the storage state, a JSON text
   message, sent again whenever it changes:

   ```json
   {"type": "storage", "directory": "~/.config/radiolack", "saving": true, "problem": ""}
   ```

   | Field | Meaning |
   | --- | --- |
   | `directory` | The settings folder, `~` standing for the home folder; null when the engine saves nothing (a server started without a settings folder, as in tests). |
   | `saving` | False while some changes do not reach the disk: a file could not be written, and the engine tries again at the next change, or a file is left as it is until the next start, as it cannot be read or comes from a newer Radiolack. |
   | `problem` | What went wrong with the files, in sentences for the user, or empty. A file that could not be read and was set aside shows here while `saving` stays true. |

7. Then, to this computer's interfaces only, the update state, a JSON text
   message, sent again whenever it changes:

   ```json
   {"type": "update", "status": "available", "version": "0.3.0", "latest": "0.4.0", "detail": "", "check_at_start": true}
   ```

   | Field | Meaning |
   | --- | --- |
   | `status` | What the latest check for a newer Radiolack found: `idle` (no check yet), `checking`, `current` (the latest version is installed, and runs), `available` (a newer version is in the APT repository), `installed` (a newer version was installed since Radiolack started, and runs once it restarts), `failed`, or `unavailable` (no check can run, such as for a Radiolack that does not run from its Debian package). |
   | `version` | The version of the Radiolack that runs. |
   | `latest` | The newer version with `available` and `installed`; otherwise null. |
   | `detail` | Why, with `failed` and `unavailable`, in a sentence for the user; otherwise empty. |
   | `check_at_start` | Whether the engine checks when it starts. |

8. Then, to this computer's interfaces only, remote access's state, a JSON
   text message, sent again whenever it changes:

   ```json
   {"type": "remote", "on": true, "port": 8443,
    "urls": ["https://192.168.1.20:8443/", "https://studio.local:8443/"],
    "code": "K7PM-3XQH", "blocked": false,
    "devices": [{"id": "5f0c2a91", "label": "iPhone · Safari", "paired": 1794000000,
                 "seen": 1794003600, "connected": true}],
    "fingerprint": "3A 4B … 9F", "problem": ""}
   ```

   (`fingerprint` shortened here.)

   | Field | Meaning |
   | --- | --- |
   | `on` | Whether remote access listens. False at each start. |
   | `port` | The port it listens on, or will. |
   | `urls` | What a device opens: by each address it listens on, then by this computer's mDNS name, when its host name is a valid one. The name also leads to the computer's IPv6 addresses, if any, where remote access does not listen. Empty while off. |
   | `code` | The access code, in two groups of four characters, while on; null while off. |
   | `blocked` | Too many wrong codes were entered (10): the code no longer works until a new one is made. |
   | `devices` | The paired devices, in the order they were paired: `id`, which a `remote` message names; `label`, such as `iPhone · Safari`, made of known words only; `paired` and `seen`, when it was paired and last used, in Unix time, the latter to the hour; `connected`, whether it has a stream open. |
   | `fingerprint` | The SHA-256 fingerprint of the local authority's root certificate, which devices install, as upper-case hex bytes separated by spaces; null before remote access first turns on. |
   | `problem` | Why remote access could not turn on, in sentences for the user, or empty. |

9. Binary spectrum frames, about 30 per second while the source streams.
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

10. Binary audio frames, about 30 per second, only to an interface that asked
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

11. An error, to one interface only, when the engine refuses a message from
    it. `command` is the message's type, or null when it is not a JSON
    object of a known type:

    ```json
    {"type": "error", "command": "source", "message": "the receiver tunes from 24 to 1766 MHz"}
    ```

What is waiting for an interface goes out in this order: the source state,
the demodulation settings, the bookmarks, the storage state, the update
state and remote access's state, each when it changed (and the preferences
at the start), then errors, then audio frames, then spectrum frames. A client that reads too
slowly loses its oldest frames: the engine keeps the latest four spectrum
frames and fifteen audio frames (half a second) for it and never slows down
for one client; it keeps its latest states, and up to eight errors. When
Radiolack stops, the server closes the stream with code 1001 (going away).

## From the interface

A `source` text message changes the settings it names, among those listed in
`settable`, or switches to another kind of source; the others stay as they
are:

```json
{"type": "source", "centre_hz": 433500000}
```

| Field | Value |
| --- | --- |
| `kind` | Another kind of source, among `kinds`, alone in its message. The source in use closes, keeping its settings for when it comes back, and the engine opens the receiver used last of that kind (the first RTL-SDR plugged in, the first time) with the settings saved for it, the demodulation settings included. Frames resume once the previous receiver is released. |
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

An `update` message asks the engine to check for a newer Radiolack now, or
turns its check at start on or off, for the next starts:

```json
{"type": "update", "check": true}
{"type": "update", "check_at_start": false}
```

The engine starts a check unless one is running, saves the check at start
with its settings, and this computer's interfaces receive the update state
as it changes. It refuses other fields, and values other than true or false.

A `remote` message changes remote access ([Remote access](#remote-access)),
one setting at a time:

```json
{"type": "remote", "on": true}
{"type": "remote", "port": 8444}
{"type": "remote", "new_code": true}
{"type": "remote", "forget": "5f0c2a91"}
```

| Field | Value |
| --- | --- |
| `on` | `true` or `false`. Turning on gives remote access a new server certificate, for this computer's current addresses, and listens; turning off closes the remote streams, then releases the port. Both go on in the background: the remote access state tells how it went, with a `problem` when remote access could not turn on. |
| `port` | From 1024 to 65535, for the next turn-on: only while remote access is off. |
| `new_code` | `true`: a new access code, which no wrong code has been tried against. Every device is forgotten, and its streams close. |
| `forget` | A paired device's `id`: the device is forgotten, and its streams close. |

The engine refuses a message without a setting or with more than one, an
unknown or invalid setting, or a device it does not know, and answers the
sender with an error. This computer's interfaces receive remote access's
state as it changes.

A remote client may send `source`, `demod`, `bookmark` and `audio`
messages, but neither `preferences`, `update` nor `remote` messages, nor a
`source` message that names `address`: the engine refuses them with the
error "only Radiolack's interface on the computer may change this". A remote
interface starts from the preferences the engine sends, and keeps its
changes to itself.

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

## Remote access

While on, remote access serves the interface and the stream over HTTPS, with
a server certificate from Radiolack's local authority, on the port and the
addresses its state gives. It answers only peers whose own address is
private (the ranges README.md lists, under Remote access), and only requests
whose `Host` header names it as one of its `urls` does; while off or turning
off, it answers every request with 503. Its answers carry
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`.

| Request | Answer |
| --- | --- |
| `GET /` | The interface, `index.html`, to a paired device; the pairing page, `pair.html`, to any other. |
| `GET /certificate` | The local authority's root certificate, in PEM, as an attachment of type `application/x-x509-ca-cert`, which a device installs. |
| `GET /<file>` | An interface file, as on the loopback server. Before pairing, only `pair.js`, `pair.css` and `style.css`: 403 for the others. |
| `POST /pair` | Checks an access code, below. |
| `GET /stream` | The stream, to a paired device. |

A device is paired while it sends the `__Host-radiolack` cookie that the
right code gave it. The engine keeps only the cookie's SHA-256, forgets a
device unused for 90 days and pairs up to 20 devices.

`/pair` takes a JSON body of up to 4 KiB, such as `{"code": "k7pm 3xqh"}`,
from a page of the same origin: its `Origin` header must be `https://`
followed by its `Host` header. The code ignores case, spaces and dashes.
Codes are checked one at a time, and the answer is JSON, with a sentence for
the user in `error`:

| Status | Body | When |
| --- | --- | --- |
| 200 | `{"paired": true}` | The right code, with the session cookie (`Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, 400 days), or a device paired already. |
| 400 | `{"error": "Send the access code."}` | No `code` text in the body. |
| 403 | `{"error": "Wrong code."}` | A wrong code, answered after a second. |
| 409 | `{"error": …}` | 20 devices are paired already. |
| 429 | `{"error": …}` | 10 wrong codes were entered: the code no longer works until a new one is made. |

Other refusals are plain text: 403 for another origin, 413 for a larger
body, 415 for a body that is not `application/json`, and 503 while off.

The stream needs the same origin too. Its handshake succeeds whatever the
cookie, so that the page learns why the stream closes: with code 4001 at
once for a device that is not paired, and later when the device is
forgotten or a new code is made; with code 4002 when remote access turns
off. The server pings a remote client every 20 s, and drops one that stops
answering.

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
  number. It ignores a source state, demodulation settings, bookmarks, a
  storage state, an update state or a remote access state with a missing or
  malformed field, and leaves out source settings it does not know and
  preferences that are not numbers, true or false, texts or null.
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
- A remote client whose stream closes with code 4001 stops. If its stream
  had opened, it reloads its page, which is then the pairing page;
  otherwise, as reloading would bring the same page back, it asks for a
  reload. One whose stream closes with code 4002 reconnects in the same way
  as after a drop, saying that remote access is off on the computer, until
  a hello comes.
- While its page is hidden, the interface closes the stream, unless it plays
  audio; it opens it again when the page shows.

## Versions

- 6 (2026-11-11): remote access: the hello's `remote`, remote access's
  state, the `remote` message, the HTTPS server, remote clients and their
  close codes 4001 and 4002. The storage and update states go to this
  computer's interfaces only.
- 5 (2026-11-10): the update state and the `update` message.
- 4 (2026-11-09): the bookmarks, the preferences and the storage state, and
  the `bookmark` and `preferences` messages. The same day, an `rtl_tcp`
  source's `address` became settable, without a new version: an interface
  leaves out the settings it does not know, and changes only those listed
  in `settable`. On 2026-11-10, the source state's `kinds` and a `source`
  message's `kind`, also without a new version: an interface that does not
  know them reads the state as before and never switches.
- 3 (2026-11-09): the demodulation settings, the `demod` and `audio`
  messages, audio frames, the channel level and squelch in spectrum frames
  (frame format 2), and the command's type in errors.
- 2 (2026-11-09): the source state, the `source` message from the interface,
  and errors. The hello no longer names the source: the state does.
- 1 (2026-11-09): hello and spectrum frames.
