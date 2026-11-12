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
from this computer's own states, with lighter spectrum frames, and may not
change this computer's own settings: the engine keeps its device's
preferences apart from this computer's.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 6, "remote": false, "scanners": ["peaks", "memory", "history", "sweep"]}
   ```

   `remote` is true for a remote client. `scanners` lists the scanners that
   the engine runs: `peaks`, which finds the signals in the span (the
   scanner state and the signals found, below), `memory`, the memory scan
   (the scanner state's `scan` and `memory`, below), `history`, which
   keeps the channels where signals were found (the observations, below),
   and `sweep`, the sweep (the scanner state's `sweep_low_hz`,
   `sweep_high_hz`, `sweep_window_s`, `stopped_scan` and `sweep`, below).
   An engine older than the scanners sends no `scanners`: it runs none.

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
   | `status` | `streaming` while samples arrive. Otherwise `connecting`, `waiting` (to try again; see `detail`) or `idle` (no interface listened and no scan ran, so the source hung up). |
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

5. Then the scanner state, a JSON text message, sent again whenever it
   changes, whichever interface or scan changed it. The engine saves its
   settings with the other settings, but never whether a scan runs:

   ```json
   {"type": "scanner", "detection": true, "threshold_db": 12.0, "ignored_hz": [144800000.0],
    "resume_s": 2.0, "sweep_low_hz": 430000000.0, "sweep_high_hz": 440000000.0, "sweep_window_s": 0.5,
    "scan": "memory", "stopped": null, "stopped_scan": null,
    "memory": {"watched": [3, 7, 12], "windows": 1, "window": 0, "state": "listening", "bookmark_id": 7},
    "sweep": {"windows": null, "window": null, "passes": null}}
   ```

   | Field | Meaning |
   | --- | --- |
   | `detection` | Whether the engine looks for signals in its spectra. |
   | `threshold_db` | How far a signal's highest bin must rise above the local noise, from 6 to 30 dB. |
   | `ignored_hz` | Frequencies whose signals are not listed, lowest first, each a whole number of Hz: at most 200. A signal is left out when its centre lies within 1.5 kHz of one, or within a quarter of its width. |
   | `resume_s` | How long the memory scan waits, once the squelch closed, before it watches every bookmark again, from 0 to 8 s. |
   | `sweep_low_hz`, `sweep_high_hz` | The range the sweep steps the receiver across, the lowest below the highest, each a whole number of Hz from 1 Hz to 100 GHz: 430 to 440 MHz until an interface changes it. |
   | `sweep_window_s` | How long the sweep reads each window, once the receiver's samples come from there, from 0.25 to 2 s. |
   | `scan` | The scan that runs: `memory`, `sweep`, or null. |
   | `stopped` | Why the latest scan stopped by itself: `tuned` (an interface tuned the VFO elsewhere, or changed the centre frequency or the sample rate), `source` (an interface chose another kind of source), `bookmarks` (no ticked bookmark lies within the receiver's reach any more) or `range` (the receiver refused a window of the memory scan or of the sweep's range); null while a scan runs, when an interface stopped it and before any. |
   | `stopped_scan` | The scan that `stopped` is about: `memory` or `sweep`; null when `stopped` is. |
   | `memory.watched` | The ids of the ticked bookmarks that the memory scan watches, whether it runs or not, in frequency order: those in the part of the span that detection searches, or, with a receiver that tunes when others lie within its reach, those it can bring into that part of its span. While a scan runs, the span is the one it started from. |
   | `memory.windows` | How many windows of the receiver they take: 1 when they all lie in the span, 0 when there is none. |
   | `memory.window` | The window the receiver is asked for, from 0, in frequency order; null while the memory scan does not run. |
   | `memory.state` | The memory scan's state: `watching` the bookmarks, `checking` that the squelch opens on the one just tuned to, `listening` while it stays open, `waiting` for `resume_s` since it closed; null while the memory scan does not run. |
   | `memory.bookmark_id` | The bookmark tuned to; null while watching, and while the memory scan does not run. |
   | `sweep.windows` | How many windows the sweep's range takes at the sample rate; null while the sweep does not run, as are the next two. |
   | `sweep.window` | The window the receiver is asked for, from 0. |
   | `sweep.passes` | The passes done since the sweep started, or since its range last changed. |

   The memory scan watches every ticked bookmark of the span at once, in
   every block of samples: the power in the passband of its mode's filter,
   once tuned (NFM 10 kHz, AM 8 kHz, USB and LSB 2.7 kHz, CW 500 Hz; USB
   above the frequency, LSB below it, the others around it), against the
   local noise there, as detection finds it, which the engine knows 10
   blocks after the scan starts or the span changes. A bookmark is active
   when its power would open the
   squelch, with its settings: 8 dB above that noise with the automatic
   squelch, `squelch_db` otherwise. While watching, the engine tunes the
   VFO to the active bookmark furthest above its noise, with that
   bookmark's mode and the mode's filter width, for every interface. It
   then checks that the squelch opens there within 0.25 s, or it watches
   again without that bookmark for 1 s; it listens while the squelch stays
   open, waits `resume_s` once it closes, listens again if it reopens
   meanwhile, and then watches again.

   With a receiver that tunes, one whose source state lists `centre_hz` in
   `settable`, the memory scan also visits the ticked bookmarks outside the
   span, when some lie within the receiver's reach (its `range_hz`). They
   take as few windows as the engine finds, each bookmark at most 0.4 times
   the sample rate from a window's centre frequency, and more than 3 kHz
   from it, as detection searches; the bookmarks of the span take windows
   like the others. The scan visits the windows in turn, in frequency
   order, the lowest after the highest, from the one nearest the receiver's
   centre frequency at its start: the engine asks the receiver for a
   window's centre frequency, which every interface then receives in the
   source state, and once the samples come from there and the noise is
   known, it watches that window's bookmarks for 1 s, and again for 1 s
   after each one it stopped on, before it asks for the next window. It
   also moves on when, 2 s after asking for a window, the samples still
   come from another centre frequency, and it watches only while they come
   from the window asked for. A scan with one window, as when every ticked
   bookmark within reach lies in the span, never tunes the receiver. The
   VFO stays inside each window, as it does whenever the span changes. The
   windows change with the ticked bookmarks and the receiver's range: a
   running scan then visits the new ones from the one nearest the receiver.

   The sweep steps a receiver that tunes, one whose source state lists
   `centre_hz` in `settable`, across its range, window after window: their
   centres lie a quarter of the sample rate apart at most, the first and
   the last a tenth of the rate inside the range, rounded to the Hz; a
   range narrower than that gets two windows, an eighth of the rate on each
   side of its middle. The engine asks the receiver for a window's centre
   frequency, which every interface then receives in the source state, and
   once the samples come from there, reads `sweep_window_s` of them as two
   searches of detection in a row, before it asks for the next window; after
   the last one, a new pass begins with the first. It also moves on when,
   2 s after asking for a window, the samples still come from another
   centre frequency. The VFO stays inside each window, as it does whenever
   the span changes.

   One scan runs at a time: starting one stops the other. A scan runs on
   without any interface, until an interface stops it, or it stops by
   itself (`stopped`). An interface's change of the centre frequency or
   the sample rate stops it at once, before the samples show the change.
   When an interface stops a scan, or it stops with `bookmarks` or
   `range`, the engine asks the receiver for its centre frequency at the
   scan's start again and puts the VFO back: after a sweep, and after a
   memory scan that asked the receiver for another centre frequency and
   stops while watching; on a bookmark, they stay. Tuning by hand
   (`tuned`) or another kind of source (`source`) leaves them as they are.
   While a scan runs, the receiver's settings that the engine saves are
   its centre frequency and the VFO at the scan's start.

6. Then the signals found, a JSON text message, sent again when the list
   changes, at most once a second, and at once when a `scanner` message or
   a switch to another kind of source changed it:

   ```json
   {"type": "peaks", "peaks": [
     {"frequency_hz": 145637800, "width_hz": 5273, "margin_db": 30.1, "active": true, "age_s": 0.0},
     {"frequency_hz": 144800012, "width_hz": 7617, "margin_db": 20.6, "active": false, "age_s": 12.4}]}
   ```

   | Field | Meaning |
   | --- | --- |
   | `frequency_hz` | The signal's centre of power, a whole number of Hz. |
   | `width_hz` | The width of the bins it covered, in Hz, when last found. |
   | `margin_db` | How far its highest bin rose above the local noise when last found, in dB. |
   | `active` | Whether it was found within the last 1.5 s. |
   | `age_s` | Seconds since it was last found, when the message was sent. |

   While detection is on, the engine looks for signals in the bin powers
   it computes for the frames, at their full resolution whatever the
   client. It averages those of 10 blocks of one capture, a third of a
   second, or while the sweep runs those of half `sweep_window_s` (at
   least one block), then keeps each run of bins whose highest rises at least
   `threshold_db` above the local noise, the median of the bins within
   50 kHz, with the bins around it that stay within 4 dB of the threshold.
   It leaves out the outer tenth of the span on each side and 3 kHz on each
   side of the centre frequency, and levels below -150 dBFS count as
   -150 dBFS. A signal is listed once found in two searches in a row;
   a later detection within 1.5 kHz of it, or within a quarter of its
   width, updates it, its frequency moving 30% of the way to the
   detection's. The engine keeps the 200 signals found last, in the spans
   the receiver left too, and sends 50: the active ones first, strongest
   first, then the others, latest found first. The list changes when a
   signal comes or goes, becomes active or inactive, or moves by 100 Hz or
   1 dB: the time since a signal was found does not change it, so an
   interface counts that time on from `age_s`. The list empties when
   detection turns off and when the engine switches to another kind of
   source.

7. Then the preferences, a JSON text message sent once: the latest that an
   interface of this computer gave the engine (`preferences` message), or
   to a remote client, the latest that an interface of its device gave,
   and this computer's while its device has given none; they are kept for
   the next start. They are the interface's own settings, which the engine
   stores without knowing their meaning: up to 100 values, named by 1 to 40
   characters, each a number, true or false, a text of at most 100
   characters or null.

   ```json
   {"type": "preferences", "values": {"palette": "gray", "volume": 70, "showPlan": true}}
   ```

8. Then, to this computer's interfaces only, the storage state, a JSON text
   message, sent again whenever it changes:

   ```json
   {"type": "storage", "directory": "~/.config/radiolack", "saving": true, "problem": ""}
   ```

   | Field | Meaning |
   | --- | --- |
   | `directory` | The settings folder, `~` standing for the home folder; null when the engine saves nothing (a server started without a settings folder, as in tests). |
   | `saving` | False while some changes do not reach the disk: a file could not be written, and the engine tries again at the next change, or a file is left as it is until the next start, as it cannot be read or comes from a newer Radiolack. |
   | `problem` | What went wrong with the files, in sentences for the user, or empty. A file that could not be read and was set aside shows here while `saving` stays true. |

9. Then, to this computer's interfaces only, the update state, a JSON text
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

10. Then, to this computer's interfaces only, remote access's state, a JSON
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

11. Binary spectrum frames, about 30 per second while the source streams.
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

    A remote client, whose link may be slower, gets about 15 frames per
    second, with at most 2048 bins: each of its frames gives the mean power
    of two of the frames above, each bin the highest of the bins it groups,
    so that narrow signals stay visible, with the channel level and squelch
    of the latter. Its frames have a sequence of their own.

    The channel level is the mean power, over the same samples, in the VFO's
    channel: a full-scale carrier on the VFO reads 0 dBFS. It is NaN when
    the VFO lies outside the span. The squelch opens once the power averaged
    over 10 ms reaches the threshold and closes 3 dB below it; the flag gives
    its state at the end of the samples. The threshold is `squelch_db`, or
    the automatic one, which is NaN while the engine has no noise estimate
    (the VFO outside the span); the squelch then stays closed.

12. Binary audio frames, about 30 per second, only to an interface that asked
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

13. The observations, a JSON text message, to one interface only, when it
    asks for them (`observations` message): the channels where the engine
    found signals, the latest found first.

    ```json
    {"type": "observations", "channels": [
      {"frequency_hz": 145500000, "first_s": 1794000000, "last_s": 1794350000, "transmissions": 12,
       "active_s": 95.0, "margin_db": 31.0,
       "hours": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 10, 0, 0, 0, 0]}]}
    ```

    | Field | Meaning |
    | --- | --- |
    | `frequency_hz` | The channel's frequency, a whole number of Hz: it moves 30% of the way to each signal found there. |
    | `first_s`, `last_s` | When a signal was first and last found there, in Unix time, to the second. |
    | `transmissions` | How many transmissions: a signal found there more than 1.5 s after the previous one begins a new one. |
    | `active_s` | How long signals were found there, in seconds, to the tenth: the time between detections at most 1.5 s apart. |
    | `margin_db` | How far the strongest rose above the local noise, in dB, to the tenth. |
    | `hours` | 24 counts: how many transmissions began in each hour of the day, from 0:00, in the local time of the engine's computer. |

    The engine records the signals listed in each search (the signals
    found, above), with every kind of source but the simulated band: the
    ignored frequencies, which are not listed, never are. A signal within
    1.5 kHz of a channel, or within a quarter of its own width, counts for
    the nearest; any other begins a channel. The engine keeps at most 2000
    channels, the least recently found leaving first, and forgets a channel
    not found for 90 days. It keeps them in `observations.json`, written at
    most every 10 minutes, a second after a client clears some, and when it
    stops.

14. An error, to one interface only, when the engine refuses a message from
    it. `command` is the message's type, or null when it is not a JSON
    object of a known type:

    ```json
    {"type": "error", "command": "source", "message": "the receiver tunes from 24 to 1766 MHz"}
    ```

What is waiting for an interface goes out in this order: the source state,
the demodulation settings, the bookmarks, the scanner state, the signals
found, the storage state, the update state and remote access's state, each
when it changed (and the preferences at the start), then the observations
and errors, in the order of the messages they answer, then audio frames,
then spectrum frames. A client that reads too
slowly loses its oldest frames: the engine keeps the latest four spectrum
frames and fifteen audio frames (half a second) for it and never slows down
for one client; it keeps its latest states, and up to eight observations
and errors together. When
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

A `scanner` message changes the signal detection, the memory scan or the
sweep, the same for every interface:

```json
{"type": "scanner", "detection": false}
{"type": "scanner", "threshold_db": 18}
{"type": "scanner", "resume_s": 4}
{"type": "scanner", "sweep_low_hz": 1240000000, "sweep_high_hz": 1300000000}
{"type": "scanner", "sweep_window_s": 1}
{"type": "scanner", "ignore": 144800000}
{"type": "scanner", "restore": true}
{"type": "scanner", "scan": "memory"}
{"type": "scanner", "scan": "sweep"}
{"type": "scanner", "scan": null}
```

| Field | Value |
| --- | --- |
| `detection` | `true` or `false`. Turning detection off empties the list of the signals found. |
| `threshold_db` | A number from 6 to 30. |
| `resume_s` | A number from 0 to 8. |
| `sweep_low_hz`, `sweep_high_hz` | Frequencies from 1 Hz to 100 GHz, rounded to the Hz, the lowest below the highest once the message applies: one may come without the other. A new range starts a running sweep again from its first window. |
| `sweep_window_s` | A number from 0.25 to 2. A running sweep takes it at once. |
| `ignore` | A frequency from 1 Hz to 100 GHz, rounded to the Hz: the signals within 1.5 kHz of it, or within a quarter of their width, leave the list and are not listed again. A frequency within 1.5 kHz of one already ignored adds none. |
| `restore` | `true`: no frequency is ignored any more. The signals left out come back once found again. |
| `scan` | `memory` starts the memory scan, `sweep` the sweep, unless it runs, stopping the other scan; null stops the scan that runs, if any. |

`detection`, `threshold_db`, `resume_s`, `sweep_low_hz`, `sweep_high_hz`
and `sweep_window_s` may come together; `ignore`, `restore` and `scan`
come alone in their message. The engine refuses the whole message,
changing nothing, when it has no setting, an unknown or invalid one, a new
frequency to ignore while 200 are ignored already, the memory scan to start
while it would watch no bookmark (`memory.watched` is empty), the
sweep to start with a source that does not tune, or the sweep to start or
a running sweep's new range when the receiver cannot tune to every window
of the range (its `range_hz`). Otherwise every interface receives the
scanner state, and the signals found at once if they changed.

An `observations` message asks for the observations, or forgets some of
them for every interface:

```json
{"type": "observations"}
{"type": "observations", "clear": 145500000}
{"type": "observations", "clear_all": true}
```

| Field | Value |
| --- | --- |
| `clear` | A frequency from 1 Hz to 100 GHz: the channel nearest to it, within 1.5 kHz, is forgotten; none is when no channel lies that close. |
| `clear_all` | `true`: every channel is forgotten. |

`clear` and `clear_all` come alone in their message. The engine refuses
the whole message, changing nothing, when it has an unknown or invalid
field. Otherwise it answers the sender alone with the observations, after
the change if any: the other interfaces see it when they next ask.

A `preferences` message gives the engine this interface's preferences,
which replace the ones it kept for this computer's interfaces, or for the
remote client's device:

```json
{"type": "preferences", "values": {"palette": "gray", "volume": 70, "showPlan": true}}
```

The engine saves them and sends them to the interfaces of the same
computer or device that connect later; open interfaces receive nothing, so
each keeps its own. It refuses values that do not fit the preferences
message, and those of a device forgotten meanwhile.

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

A remote client may send `source`, `demod`, `bookmark`, `scanner`,
`observations`, `preferences` and `audio` messages, but neither `update` nor `remote` messages, nor a
`source` message that names `address`: the engine refuses them with the
error "only Radiolack's interface on the computer may change this". Its
`preferences` are its device's own: they change neither this computer's
nor another device's.

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
device unused for 90 days and pairs up to 20 devices; a forgotten device's
preferences go with it. A device that opens remote access by two of its
`urls` gets a cookie for each, and is paired, with preferences of its own,
by each.

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

The stream accepts permessage-deflate (RFC 7692), which browsers offer and
which halves the spectrum frames; a message from the device must stay
within 64 KiB once inflated. The loopback server's stream does not compress.
On a link slower than the stream, the server keeps at most about 16 KiB
unsent in the socket (`TCP_NOTSENT_LOWAT`, where the system has it) and in
its transport: the engine then drops the device's oldest frames, as for any
client that reads too slowly, instead of the device hearing the radio later
and later.

## Connection life

- The engine computes frames only while at least one client listens or a
  scan runs. The `rtl_sdr` and `rtl_tcp` sources connect at the first read
  and hang up 2 s after the last one; the `rtl_sdr` source starts rtl_tcp
  before connecting and stops it on hanging up.
- A source that waits sends no frames, but the stream stays open: the source
  state says why (`status` and `detail`). This differs from the stream itself
  dropping, which the interface shows as waiting for the engine.
- The interface ignores binary frames until the hello, and any frame of
  another kind or format version, without bins, or whose centre, rate, level
  or step is not a finite number; the rate and the step must also be
  positive. It ignores audio frames while it has not asked for audio, and
  those with an odd number of sample bytes or a rate that is not a positive
  number. It ignores a source state, demodulation settings, bookmarks, a
  scanner state, a list of signals found, observations, a storage state, an
  update state or a remote access state with a missing or malformed field,
  and leaves
  out source settings it does not know and preferences that are not
  numbers, true or false, texts or null.
- An interface whose hello does not list `peaks` among its `scanners`, as
  an older engine's, shows that signal detection needs Radiolack to
  restart, and offers none of its settings; one whose hello does not list
  `memory` does the same for the memory scan, one whose hello does not
  list `history` for the History, asking for no observations, and one whose
  hello does not list `sweep` for the sweep. An interface reads a scanner
  state without the sweep's fields, as an older engine's, as one without a
  sweep.
- The interface asks for the observations when its History shows, then
  every 30 s while it shows and its page is visible.
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
  computer's interfaces only. On 2026-11-12, a remote client's
  `preferences` message, which the engine keeps for its device, and the
  preferences that it sends a remote client, its device's, without a new
  version: the messages are the same, an interface that does not send its
  preferences as a remote client keeps its changes to itself as before,
  and one that sends them to an engine that refuses them receives an
  error, the rest working. A new version would stop the interface that a
  running Radiolack serves, which it reads from the disk at each request,
  from the installation of a newer package until Radiolack restarts. On
  2026-11-12 also, for the same reason and without a new version: the
  hello's `scanners`, the scanner state, the signals found and the
  `scanner` message, as detection moved from the interface to the engine.
  An older interface ignores messages of unknown types and finds signals
  itself, as before; a newer one learns from the hello whether the engine
  finds them. Later that day, as the memory scan moved to the engine too:
  the hello's `memory` scanner, the scanner state's `resume_s`, `scan`,
  `stopped` and `memory`, and the `scanner` message's `resume_s` and
  `scan`. An older interface ignores the fields it does not know and scans
  the bookmarks itself, as before. Later that day again, the hello's
  `history` scanner, the observations and the `observations` message: an
  older interface never asks for them. Later still, the hello's `sweep`
  scanner, the scanner state's `sweep_low_hz`, `sweep_high_hz`,
  `sweep_window_s`, `stopped_scan` and `sweep`, its `scan` value `sweep`
  and its `stopped` value `range`, and the `scanner` message's
  `sweep_low_hz`, `sweep_high_hz`, `sweep_window_s` and `scan` value
  `sweep`: an older interface ignores the fields it does not know, shows no
  sweep that runs, and may show why a sweep stopped as if the memory scan
  had. Later again, as the memory scan visits the ticked bookmarks outside
  the span with a receiver that tunes, `memory.watched` lists them, with
  the scanner state's `memory.windows` and `memory.window`, and `stopped`
  may be `range` for the memory scan: an older interface ignores the
  fields it does not know, shows no window, and marks the ticked bookmarks
  beyond the receiver's reach as outside the span.
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
