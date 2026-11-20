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
change this computer's own settings nor ask for a problem report: the engine keeps its device's
preferences apart from this computer's.

## From the engine

1. First, one JSON text message:

   ```json
   {"type": "hello", "protocol": 6, "version": "0.13.0", "remote": false,
    "scanners": ["peaks", "memory", "history", "sweep", "signals"], "recording": ["audio", "iq"],
    "measures": ["carrier", "driver_pll"], "spectra": ["fine"]}
   ```

   `version` is the version of the Radiolack that runs, which About shows on
   every interface, remote clients included; an engine older than About
   sends none.

   `remote` is true for a remote client. `scanners` lists the scanners that
   the engine runs: `peaks`, which finds the signals in the span (the
   scanner state and the signals found, below), `memory`, the memory scan
   (the scanner state's `scan` and `memory`, below), `history`, which
   keeps the channels where signals were found (the observations, below),
   `sweep`, the sweep (the scanner state's `sweep_low_hz`,
   `sweep_high_hz`, `sweep_window_s`, `stopped_scan` and `sweep`, below),
   and `signals`, the signals scan (the scanner state's `listen_s` and
   `signals`, below).
   An engine older than the scanners sends no `scanners`: it runs none.
   `recording` lists what the engine records: `audio`, the audio of the
   VFO, and `iq`, the receiver's raw I/Q (the recording state and the
   recordings, below). An engine older than the recordings sends no
   `recording`: it records nothing; one older than the I/Q recordings lists
   `audio` alone. `measures` lists what the engine measures when an
   interface asks: `carrier`, the carrier at the VFO (the carrier's
   measure, below), and `driver_pll`, the check of the rtl-sdr driver's
   computation on that carrier (the driver's check, below). An engine
   older than the calibration sends no `measures`: it measures nothing;
   one older than the driver's check lists `carrier` alone. `spectra` lists
   the spectra that the engine sends, besides the spectrum frames, to an
   interface that asks: `fine`, the fine spectrum of the VFO's channel (the
   fine frames, below). An engine older than the fine spectrum sends no
   `spectra`: it sends none.

2. Then the source state, a JSON text message, sent again whenever it
   changes, whichever interface changed it. It also carries `dc_removal`, a
   setting of the engine's own that applies to any source:

   ```json
   {"type": "source", "kind": "rtl_tcp", "status": "streaming", "detail": "",
    "address": "127.0.0.1:1234", "tuner": "Rafael Micro R820T", "driver": null,
    "centre_hz": 145000000.0, "centre_in_use_hz": 144997500.0, "sample_rate_hz": 2400000.0,
    "sample_rates_hz": [240000.0, 300000.0, 1024000.0, 1536000.0, 2048000.0, 2400000.0, 2880000.0],
    "gain_db": null, "ppm": 0,
    "gains_db": [0.0, 0.9, 1.4, 2.7, 49.6], "range_hz": [24000000.0, 1766000000.0],
    "hf_input": "none", "hf_inputs": ["none", "q", "i", "tuner"], "direct": false,
    "rtl_agc": false, "bias_tee": false, "converter": "none", "converter_lo_hz": 125000000.0,
    "driver_pll": "unknown", "tuner_grid": true, "dc_removal": true,
    "settable": ["address", "hf_input", "converter_lo_hz", "converter", "sample_rate_hz", "centre_hz",
                 "gain_db", "ppm", "driver_pll", "tuner_grid", "rtl_agc", "bias_tee", "dc_removal"],
    "kinds": ["synthetic", "rtl_sdr", "rtl_tcp", "soapy"]}
   ```

   (`gains_db` shortened here.)

   | Field | Meaning |
   | --- | --- |
   | `kind` | The source: `synthetic` for the simulated 2 m band, `rtl_sdr` for an RTL-SDR receiver on the engine's computer, through an rtl_tcp that the engine runs, `rtl_tcp` for an RTL-SDR receiver behind an rtl_tcp server that the user runs, `soapy` for a receiver that SoapySDR reads on the engine's computer, such as an SDRplay, a HackRF or an RTL-SDR. The engine switches to `soapy` only once a device was chosen, in the Receiver menu (the `devices` message) or by `--soapy`; before that it refuses the switch, saying so. |
   | `status` | `streaming` while samples arrive. Otherwise `connecting`, `waiting` (to try again; see `detail`) or `idle` (no interface listened and no scan ran, so the source hung up). |
   | `detail` | Why the source waits, such as `connection refused`, `connection lost`, `no samples`, `not an rtl_tcp server`, `no receiver found` or `the receiver is in use by another program`, and for a `soapy` source the SoapySDR module or device's reason, such as `the SoapySDR Python module is not installed (on Debian and Ubuntu: python3-soapysdr)`, `no sdrplay device found with the arguments given` or `no samples: the device stopped answering`; empty otherwise. |
   | `address` | `host:port` of the rtl_tcp server of an `rtl_tcp` source (an IPv6 host in brackets), which an interface may change; null for the other sources. |
   | `tuner` | Name of the receiver's tuner once known, or null; for a `soapy` source, the device's hardware key, such as `RSPdx-R2`. |
   | `driver` | The SoapySDR driver of a `soapy` source, such as `sdrplay`, known from the start; null for the other sources. |
   | `centre_hz`, `sample_rate_hz` | The settings asked of the receiver, the centre frequency on the air, through the converter if there is one. Each frame carries the sample rate it was taken with and the centre in use (`centre_in_use_hz`): after a change, frames follow a moment later. |
   | `centre_in_use_hz` | Where the receiver sits on the air for `centre_hz`, which labels the samples: the frames' centre, the carrier's measure, the I/Q recordings' captures. An RTL-SDR whose tuner is an R820T or R828D, on that tuner, is placed on its grid: the frequency asked of it moves, by 56.25 kHz at most (up to 112.5 kHz at the very edges of its range), to one whose oscillator, the frequency plus the IF of the sample rate, is a multiple of 112.5 kHz, where the driver's error is the same at every one. It is then labelled where `driver_pll` puts the tuner, and the correction moves it too. Within half a hertz of `centre_hz` for every other source and setting: direct sampling, another tuner, an RTL-SDR Blog V4 below 28.8 MHz, an oscillator below 27.66 MHz. A float, as the frames' centre: the two are equal. |
   | `sample_rates_hz` | The sample rates the Receiver menu offers, lowest first, `sample_rate_hz` among them for a `soapy` source: an RTL-SDR's usual ones, or those a SoapySDR device lists, or the common ones within the ranges it gives, up to 8 MS/s, which the engine holds; empty for a source whose rate does not change. An RTL-SDR source takes other rates within librtlsdr's ranges; a `soapy` source only those its device offers. |
   | `gain_db` | Manual tuner gain in dB, or null for automatic gain (and for a source without a gain setting). A `soapy` source has one for a device with a gain stage or more, and lists `gain_db` among `settable` then: the one stage's gain, or with several the overall gain, which SoapySDR's module spreads across the stages, read back from the device after each change, its stages' too. On a device with a held stage (`gain_stages`), the overall gain never moves it: the engine spreads what is left over the other stages itself. |
   | `automatic_gain` | A `soapy` source alone: whether the device has automatic gain, which a null `gain_db` asks for; null until the device first opens. Other sources leave it out, an interface reading its absence as automatic gain offered. The Receiver menu greys its Tuner AGC box for a device without it, unless automatic gain is on. |
   | `ppm` | Frequency correction of the receiver's clock, in parts per million to the hundredth, such as 37.42; null for a source without one. An RTL-SDR takes its nearest whole number itself, and the engine the rest, by scaling the frequency it asks for: `centre_hz` stays the frequency asked. An R820T or R828D takes none, since a corrected crystal would move its grid: the engine takes the whole of it, by the frequency it asks for, the samples' label and the VFO's distance from the centre, scaled by as much, as the receiver samples that much faster. A `soapy` source corrects the same way: a device that declares a correction of its own takes the nearest whole number, any other none, and the engine the rest. An interface older than the hundredths takes whole numbers only, and drops a source state that holds another until it is reloaded. |
   | `gains_db` | Manual gains the tuner offers, lowest first; empty when it offers automatic gain only. For a `soapy` device with several stages, the overall gains it offers, in steps of 1 dB unless it declares others, with each held stage at its gain: they move when a held stage does. |
   | `range_hz` | Lowest and highest centre frequency the source takes, with its shortwave input, converter and sample rate. With direct sampling and a tuner whose range starts above 28.8 MHz (E4000, FC2580), the centre frequencies between the two are refused. Through a converter, it is the receiver's range converted to frequencies on the air, from half the sample rate at the lowest, so that the span never goes below 0 Hz, to 100 GHz less half the sample rate at the highest. |
   | `hf_input` | What an RTL-SDR uses for shortwave: `none`; `q` or `i`, direct sampling on that input of the RTL2832, by itself below the tuner's range; `tuner`, a tuner that covers shortwave, as an RTL-SDR Blog V4's does. Null for a source without it. |
   | `hf_inputs` | The values of `hf_input` the receiver offers: all four for an R820T or R828D tuner and while the tuner is unknown, the first three for another; empty for a source without them. |
   | `direct` | Whether the receiver samples directly, without its tuner: its gain does not apply meanwhile, and signals mirror each other about 14.4 MHz. |
   | `rtl_agc` | Whether the RTL2832's own AGC runs while the receiver samples directly; null for a source without it. |
   | `bias_tee` | Whether the antenna power is on: 4.5 V on the antenna socket of an RTL-SDR Blog V3 or V4, or for a `soapy` device the setting that powers its antenna socket, found by its key or name (`bias`, `antenna power` or `ant power` in it, whatever the case and punctuation, such as `biastee`, `bias_tx` or `biasT_ctrl`), which the device's settings never show. Off at each start, and for a `soapy` device at each opening, whatever the device or a saved file says, unless Radiolack was started with `--bias-tee` or an interface turned it on since; never saved, and turned off before the engine releases the receiver, however its session ends. A device with several such settings has them all off, and `bias_tee` drives the first: its receive channel's before the device's own, one not named for transmission before one that is, then in the order declared. Null for a source without it, and for a `soapy` device until it opens. |
| `gain_stages` | A `soapy` device's gain stages, when it has several, each as `{"name": "LNA", "gains_db": [0.0, 8.0, ...]}`, under the device's names, with the gains each offers, lowest first; empty otherwise. A held stage, the HackRF's `AMP`, starts at its lowest at each opening without saved gains, whatever the device's default, and only its own change moves it. SDRplay's `IFGR` and `RFGR` are gain reductions, shown under those names: more is less gain. |
| `stage_gains_db` | Each stage's gain as the device holds it, by name: `{"LNA": 16.0, "AMP": 0.0, "VGA": 16.0}`; empty without several stages. Saved with the device. |
| `antenna` | The antenna socket in use, by the device's name for it, for a `soapy` device that has several; the device's own until changed, then saved with the device. Null otherwise. |
| `antennas` | The sockets, as the device names them, when it has several (16 at most); empty otherwise. |
| `device_settings` | The values the device holds of its own settings (`device_setting_info`), by key: true or false for a box, the option's value for a choice, a number. Saved with the device; the device's own until changed. Empty for a source without them. |
| `device_setting_info` | A `soapy` device's settings as it describes them, its own and its receive channel's, 32 at most, each as `{"key": "rfnotch_ctrl", "name": "RF Notch Filter", "description": "...", "type": "bool", "options": [], "range": null, "integer": false}`: `type` `bool`, a box; `choice`, one of `options`, each `{"value": "1", "name": "I-ADC"}`; `number`, within `range`, `[lowest, highest, step]`, a whole number when `integer`. Names and descriptions are the device's texts, printable, cut at 64 and 300 characters, which an interface shows as text, never as markup. Left out: a free text, which the device's module would parse; a number without a range; a choice whose options are not short printable names; a setting named for transmission (a word starting with `tx` or `transmit`); antenna power (`bias_tee`). An RTL-SDR's direct sampling, `direct_samp`, is one of them, a choice: for shortwave with every behaviour, its `rtl_tcp` and `rtl_sdr` entries' shortwave input. |
| `device_refusal` | What the device refused at the last change of its gains, socket, settings or antenna power, such as `the device refused HDR Mode: ...`; the state then holds what the device kept. Null otherwise, and again at the next change. |
   | `converter` | What stands in front of the receiver: `none`; `up`, an upconverter, the receiver tuning the frequency plus the oscillator's; `down`, a downconverter such as a satellite LNB, the frequency minus the oscillator's; `down_inverted`, a downconverter whose oscillator lies above the band, the oscillator's frequency minus the frequency, which mirrors the spectrum: the engine swaps I and Q as the samples arrive, before the spectrum, the audio and an I/Q recording. Every frequency the engine gives and takes is the one on the air. Null for a source without it. |
   | `converter_lo_hz` | The converter's oscillator, in whole Hz, kept without a converter too; null for a source without it. |
   | `driver_pll` | How the driver behind rtl_tcp computes an R820T's or R828D's PLL, which the user chooses for each receiver and the engine saves with it: `unknown`, the default, labels the samples as if the tuner were exact; `truncating`, rtl-sdr up to 2.0.1 and the RTL-SDR Blog fork, which sit one constant low per divider of the tuner on its grid and otherwise err by another amount at each centre frequency; `rounding`, rtl-sdr 2.0.2 and later, exact on the grid. Known, the label is where the ported computation puts the tuner. It changes the label alone, never what is asked of the receiver. Settable on every RTL-SDR source, used for an R820T or R828D only; null for a source without it. |
   | `tuner_grid` | Whether an R820T or R828D is placed on its grid: true at each start, never saved, and changed only by tools that measure the tuner (`tools/measure_pll.py`), which need centres off it; the interface neither shows nor changes it. Off, the frequency asked is the centre asked, and a known `driver_pll` labels it with that centre's own error. Null for a source without it. |
   | `dc_removal` | Whether the engine takes each block's mean off the samples before the FFT, so that a constant offset, such as an RTL-SDR's, does not show as a spike at the centre frequency; a signal exactly on the centre frequency goes with it. True when Radiolack starts. |
   | `settable` | Settings an interface may change; only `dc_removal` for the synthetic source. Never `address` for a remote client. |
   | `kinds` | Kinds of source the engine may switch to, `kind` among them; empty when it reads one source only. |

3. Then the demodulation settings, a JSON text message, sent again whenever
   they change, whichever interface changed them. They are the same for
   every interface, apart from `acknowledged`:

   ```json
   {"type": "demod", "frequency_hz": 145600000.0, "mode": "NFM", "bandwidth_hz": 10000.0,
    "squelch_db": -80.0, "squelch_auto": true, "notch": false, "blanker": false, "sam_locked": null,
    "notch_lines": null, "blanker_impulses": null, "modes": ["NFM", "AM", "SAM", "USB", "LSB", "CW", "WFM"],
    "audio_modes": ["NFM", "AM", "SAM", "USB", "LSB", "CW", "WFM"], "acknowledged": 0}
   ```

   | Field | Meaning |
   | --- | --- |
   | `frequency_hz` | The VFO, a whole number of Hz. It starts a quarter of the span above the centre frequency. When the span changes, a VFO left outside it moves to its nearest edge; a VFO set outside the span reads nothing and stays silent. |
   | `mode` | One of `modes`. |
   | `bandwidth_hz` | Width of the channel filter: centred on the VFO for NFM, AM, SAM, CW and WFM, above it for USB, below it for LSB. SAM takes AM's widths. |
   | `squelch_db` | The squelch threshold set by hand, in dBFS of channel power. |
   | `squelch_auto` | Whether the threshold follows the noise instead: 8 dB above the noise power in the channel, which the engine estimates from the median of the spectrum's bins within 100 kHz of the VFO. In WFM, whose channel fills those bins, it takes the median of the bins from the channel's edge to 100 kHz beyond it, on the quieter side, as a neighbouring station may fill the other. |
   | `notch` | Whether the automatic notch is on: off by default, saved with the receiver's settings, kept across modes, and acting in AM, SAM, USB and LSB only, not in CW, whose signal is a steady tone, nor in NFM and WFM. While it acts it removes from the audio the lines that stand 15 dB or more over their surroundings, are at most 30 Hz wide, are found at every analysis, ten times a second, for half a second, and lie between 100 Hz and 5 kHz, four at most, those that stand highest first; each one leaves a second after it was last found. A bookmark does not carry it. |
   | `blanker` | Whether the noise blanker is on: off by default, saved with the receiver's settings, kept across modes, and acting in every mode, on the samples that make the VFO's channel alone, before its filters. In each block of samples it zeroes the impulses, samples whose magnitude exceeds six times the block's median, estimated on every 16th sample, with two samples on each side, an impulse at a block's end reaching two samples into the next. Samples over that limit whose zeroed spans touch make one impulse; one that spans more than 16 samples is a signal, left as it is, and a block where impulses would zero more than 2% of its samples is left whole. The channel level, the squelch, SAM's search, the notch's analysis, the audio and its recordings take the blanked channel; the spectrum frames, the fine frames, the signals found, the scans' search, the carrier's measure, the driver's check and the I/Q recordings take the samples as received. A bookmark does not carry it. |
   | `sam_locked` | In SAM, synchronous AM, whether it holds a carrier: true while a line within 1 kHz of the VFO, once held followed within 40 Hz of it, stands 10 dB or more over the noise within 80 Hz, sought ten times a second and judged on the mean of the latest five searches, a lock held staying until that mean has been under 7 dB for 1.5 s; false otherwise, when the audio is AM's envelope. Null in the other modes. The state is sent again whenever it changes, to every interface; it is the engine's, never saved, and found whether or not an interface asked for audio. |
   | `notch_lines` | While the notch acts, the lines it removes now, 0 to 4; null when it is off, in the modes where it does not act, and from an engine older than the notch. The engine finds them in the audio it makes: 0 while no interface asks for audio and nothing is recorded. The state is sent again whenever it changes, to every interface; it is the engine's, never saved. |
   | `blanker_impulses` | While the blanker is on, the impulses it zeroed in the latest second of samples, counted over the whole blocks that make it up and scaled to a second, 0 before the first second; null when it is off, and from an engine older than the blanker. It changes at most once a second, whether or not an interface asked for audio, and the state is sent again whenever it changes, to every interface; it is the engine's, never saved. |
   | `modes` | The modes the engine takes. |
   | `audio_modes` | The modes it demodulates to audio; the others are measured, level and squelch, but silent. All seven for now. |
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
    "resume_s": 2.0, "listen_s": 10.0, "sweep_low_hz": 430000000.0, "sweep_high_hz": 440000000.0,
    "sweep_window_s": 0.5, "sweep_listen": false, "sweep_passes": null, "scan": "memory", "stopped": null,
    "stopped_scan": null,
    "memory": {"watched": [3, 7, 12], "windows": 1, "window": 0, "state": "listening", "bookmark_id": 7},
    "sweep": {"windows": null, "window": null, "passes": null, "passes_set": null},
    "signals": {"state": null, "frequency_hz": null}}
   ```

   | Field | Meaning |
   | --- | --- |
   | `detection` | Whether the engine looks for signals in its spectra. |
   | `threshold_db` | How far a signal's highest bin must rise above the local noise, from 6 to 50 dB. |
   | `ignored_hz` | Frequencies whose signals are not listed, lowest first, each a whole number of Hz: at most 200. A signal is left out when its centre lies within 1.5 kHz of one, or within a quarter of its width. |
   | `resume_s` | How long the memory scan and the signals scan wait, once the squelch closed, before they watch again, from 0 to 8 s. |
   | `listen_s` | How long the signals scan stays on a signal at most while another one waits, and the listening sweep on a signal of a window, from 2 to 60 s. |
   | `sweep_low_hz`, `sweep_high_hz` | The range the sweep steps the receiver across, the lowest below the highest, each a whole number of Hz from 1 Hz to 100 GHz: 430 to 440 MHz until an interface changes it. |
   | `sweep_window_s` | How long the sweep reads each window, once the receiver's samples come from there, from 0.25 to 2 s. |
   | `sweep_listen` | Whether the sweep listens to each window's active signals once it read the window: false until an interface changes it. |
   | `sweep_passes` | How many passes the sweep makes before it stops by itself, from 1 to 99, or null for endless: null until an interface changes it. |
   | `scan` | The scan that runs: `memory`, `sweep`, `signals`, or null. |
   | `stopped` | Why the latest scan stopped by itself: `tuned` (an interface tuned the VFO elsewhere, or changed the centre frequency or the sample rate), `source` (an interface chose another kind of source), `bookmarks` (no ticked bookmark lies within the receiver's reach any more), `range` (the receiver refused a window of the memory scan or of the sweep's range), `detection` (an interface turned detection off while the signals scan or a listening sweep ran), `done` (the sweep made its `sweep_passes` passes) or `start` (the scan given at start, by `--scan`, could not start: Radiolack's output says why); null while a scan runs, when an interface stopped it and before any. An older interface shows a reason it does not know as a plain stop. |
   | `stopped_scan` | The scan that `stopped` is about: `memory`, `sweep` or `signals`; null when `stopped` is. |
   | `memory.watched` | The ids of the ticked bookmarks that the memory scan watches, whether it runs or not, in frequency order: those in the part of the span that detection searches, or, with a receiver that tunes when others lie within its reach, those it can bring into that part of its span. While a scan runs, the span is the one it started from. |
   | `memory.windows` | How many windows of the receiver they take: 1 when they all lie in the span, 0 when there is none. |
   | `memory.window` | The window the receiver is asked for, from 0, in frequency order; null while the memory scan does not run. |
   | `memory.state` | The memory scan's state: `watching` the bookmarks, `checking` that the squelch opens on the one just tuned to, `listening` while it stays open, `waiting` for `resume_s` since it closed; null while the memory scan does not run. |
   | `memory.bookmark_id` | The bookmark tuned to; null while watching, and while the memory scan does not run. |
   | `sweep.windows` | How many windows the sweep's range takes at the sample rate; null while the sweep does not run, as are the next two. |
   | `sweep.window` | The window the receiver is asked for, from 0. |
   | `sweep.passes` | The passes done since the sweep started, or since its range last changed. |
   | `sweep.passes_set` | The passes it makes before it stops, as `sweep_passes` stood when it started or last changed; null while it is endless. |
   | `signals.state` | The signals scan's state, or the listening sweep's while it listens in a window: `watching` the active signals, `checking` that the squelch opens on the one just tuned to, `listening` while it stays open, `waiting` for `resume_s` since it closed; null while the signals scan does not run. |
   | `signals.frequency_hz` | The signal tuned to, at the frequency it shows on among the signals found (their `channel_hz`), a whole number of Hz; null while watching, and while neither the signals scan nor a listening sweep listens. |

   The memory scan watches every ticked bookmark of the span at once, in
   every block of samples: the power in the passband of its mode's filter,
   once tuned (NFM 10 kHz, AM 8 kHz, USB and LSB 2.7 kHz, CW 500 Hz, WFM
   180 kHz; USB above the frequency, LSB below it, the others around it),
   against the local noise there, as detection finds it, or for WFM the
   local noise beyond the passband's edges, on the quieter side, as the
   automatic squelch reads it; the engine knows that noise 10 blocks after
   the scan starts or the span changes. A bookmark is active
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
   the span changes. A range that lies within the part of the receiver's
   span that detection searches, its centre frequency plus or minus 0.4
   times the sample rate, when the sweep starts takes that one window, the
   span itself: the sweep then never tunes the receiver, and runs with any
   source. After `sweep_passes` passes, the sweep stops by itself (`done`).

   With `sweep_listen`, once the engine read a window, it takes the
   window's active signals of that moment as the window's queue, leaving
   out those it tuned to earlier in the pass, which another window that
   overlaps this one held, and listens to each in turn as the signals scan
   does (below), the one that waited longest first across windows and
   passes; once `listen_s` has passed on a signal of the queue, it leaves
   it, whether another waits or not. Once every signal of the queue was
   tuned to, or is no longer active, and the scan left the last, it asks
   for the next window. A signal found after the window was read waits for
   the next pass. While it listens, `signals` gives its state and the
   signal tuned to. A listening sweep needs detection, and stops when
   detection turns off (`detection`).

   The signals scan listens to the active signals of the span in turn, with
   the VFO alone: those found within the last 1.5 s (the signals found,
   below) in the part of the span that detection searches, each at the
   frequency it shows on (`channel_hz`), those within 1.5 kHz of each other
   counting as one; the ignored frequencies are never listed, so never
   tuned to. While watching, the engine tunes the VFO, for every
   interface, to the active signal that waited longest: one it never tuned
   to first, the lowest of them, then the one it tuned to least recently,
   remembering the latest 200 it tuned to. The VFO takes the mode that the
   band plan gives that frequency, as the History's Bookmark button does (a
   known frequency's, else a channel set's, else its range's; SAM stays
   where it gives AM), and that mode's filter width when the mode changes;
   where the plan gives none, the mode stays. The engine then checks that
   the squelch opens there within 0.25 s, or it watches again without that
   signal for 1 s; it listens while the squelch stays open, waits
   `resume_s` once it closes, listens again if it reopens meanwhile, and
   then watches again. Once `listen_s` has passed since it tuned to a
   signal, it leaves it, listening or waiting, as soon as another active
   signal waits, so that a signal on the air all the time does not keep
   the others from their turn. Ignoring a frequency moves the scan on at
   once when it is on a signal there. The signals scan never tunes the
   receiver, needs detection, and stops when detection turns off
   (`detection`).

   One scan runs at a time: starting one stops the other. A scan runs on
   without any interface, until an interface stops it, or it stops by
   itself (`stopped`). An interface's change of the centre frequency or
   the sample rate stops it at once, before the samples show the change.
   When an interface stops a scan, or it stops with `bookmarks` or
   `range`, the engine asks the receiver for its centre frequency at the
   scan's start again and puts the VFO back: after a sweep, and after a
   memory scan that asked the receiver for another centre frequency and
   stops while watching; on a bookmark, they stay. A sweep that stops by
   itself after its passes (`done`) comes back too, and a listening sweep
   on a signal (checking, listening or waiting) stays there. The signals
   scan leaves the VFO where it is. Tuning by hand (`tuned`) or another kind of source
   (`source`) leaves them as they are. While a scan runs, the receiver's
   settings that the engine saves are its centre frequency, and the VFO's
   frequency, mode and filter width, at the scan's start.

6. Then the signals found, a JSON text message, sent again when the list
   changes, at most once a second, and at once when a `scanner` message or
   a switch to another kind of source changed it:

   ```json
   {"type": "peaks", "peaks": [
     {"frequency_hz": 145637800, "channel_hz": 145637500, "width_hz": 5273, "margin_db": 30.1, "active": true,
      "age_s": 0.0},
     {"frequency_hz": 144300120, "channel_hz": 144300100, "width_hz": 2344, "margin_db": 20.6, "active": false,
      "age_s": 12.4}]}
   ```

   | Field | Meaning |
   | --- | --- |
   | `frequency_hz` | The signal's centre of power, a whole number of Hz. |
   | `channel_hz` | The frequency it shows on, a whole number of Hz: where the band plan's step is 12.5 kHz or more, the nearest channel of that step, for a signal 4 kHz wide or more, or within 1.5 kHz of it; elsewhere its frequency to the 100 Hz. |
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

8. Then the recording state, a JSON text message, sent again whenever it
   changes, and every second while a recording runs:

   ```json
   {"type": "recording",
    "audio": {"file": "2026-11-14_15-30-45Z_145.500000MHz_NFM.wav", "started": 1794670245,
              "frequency_hz": 145500000, "mode": "NFM", "rate_hz": 12000, "seconds": 61.4, "bytes": 2947244},
    "iq": {"file": "2026-11-14_15-31-02Z_145.000000MHz_2.400000MSps.sigmf-data", "started": 1794670262,
           "frequency_hz": 145000000, "rate_hz": 2400000, "seconds": 44.2, "bytes": 212160000},
    "stopped": null,
    "folder": "~/.local/share/radiolack/recordings", "default_folder": "~/.local/share/radiolack/recordings"}
   ```

   | Field | Meaning |
   | --- | --- |
   | `audio` | The audio recording that runs, or null. `file` is its file's name, null until the first audio makes the file; `started` its start, in Unix time, to the second; `frequency_hz` and `mode` the VFO's when it started, which name the file; `rate_hz` the audio rate, null until the first audio; `seconds` its length, to the tenth; `bytes` its file's size, 0 until the file is made. |
   | `iq` | The I/Q recording that runs, or null. `file` is its data file's name, null until the first samples make the files; `started` its start, in Unix time, to the second; `frequency_hz` and `rate_hz` the receiver's centre frequency and sample rate when it started, which name the files; `seconds` its length, to the tenth; `bytes` its data's size, 0 until the files are made. |
   | `stopped` | Why the latest recording stopped by itself, until a recording of its kind starts: `kind` (`audio` or `iq`), `reason` and `detail`, a sentence for the user with `disk`, otherwise empty. Null when an interface stopped it, or none ran since the engine started. When both recordings stop at once, as when the disk fills, it tells of the latter. |
   | `folder` | To this computer's interfaces only: the folder the recordings go to, `~` standing for the home folder, or null when the engine does not record (a server started without a recordings folder, as in tests). |
   | `default_folder` | To this computer's interfaces only: the folder they go to unless another was chosen, `$XDG_DATA_HOME/radiolack/recordings`, or null as `folder`. |

   A recording stops by itself for one of these `reason`s:

   | Reason | When |
   | --- | --- |
   | `rate` | Audio: the audio rate changed, into or out of WFM. |
   | `source` | Audio: the simulated band was chosen; another receiver goes on recording. |
   | `sample_rate` | I/Q: the receiver's sample rate changed. |
   | `receiver` | I/Q: another kind of source, or another rtl_tcp address, was chosen. |
   | `converter` | I/Q: another converter, or another oscillator behind one, was chosen. |
   | `space` | Less than 2 GiB is free on its disk, at its start or later. |
   | `full` | Audio: its file reached the WAV format's limit of 4 GiB. |
   | `disk` | A disk error, such as a folder that went, or a disk too slow for the recording: `detail` says which. |

   A recording writes 16-bit mono WAV at the audio rate, from the block
   after it started, squelched silences included: the file is made with
   the first audio, readable by its owner only, and named after the start
   in UTC, the frequency and the mode, such as
   `2026-11-14_15-30-45Z_145.500000MHz_NFM.wav`, with `-2` to `-999`
   before `.wav` when the name is taken. Its header follows each write, so
   that a crash leaves a file that plays. A recording stopped before any
   audio leaves no file. It goes on without any client, like a scan, and
   never starts at start: whether one runs is not saved.

   An I/Q recording keeps the receiver's samples as the receiver gives
   them, before the DC removal and any conversion, from the samples that
   arrive after it started, whether the engine reads them or not, in a
   SigMF recording: a `.sigmf-data` file of I/Q pairs, I first, in the
   receiver's format, and a `.sigmf-meta` file, both readable by their owner only and named after the
   start in UTC, the receiver's centre frequency and its sample rate, such
   as `2026-11-14_15-31-02Z_145.000000MHz_2.400000MSps.sigmf-data`, with
   `-2` to `-999` before the extension when either name is taken. Its
   metadata, SigMF 1.0.0, gives `core:datatype`, the format: `cu8` for
   rtl_tcp's unsigned 8-bit pairs, and for a SoapySDR receiver the format
   the engine reads it in, `ci8`, `ci16_le` or `cf32_le` (`_be` on a
   big-endian computer); `core:sample_rate`; `core:hw` (the receiver, such
   as `RTL-SDR, Rafael Micro R820T` or `SoapySDR sdrplay, RSPdx-R2`, and
   the converter in front of it, such as `RTL-SDR, Rafael Micro R820T,
   behind a downconverter with its oscillator at 9750 MHz`);
   `core:recorder` (`Radiolack` and its version); and, for a receiver
   whose integers stop short of their type's full scale, such as an
   SDRplay's at 32767 or a bladeRF's at 2048, `radiolack:full_scale`, that
   full scale, with `core:extensions` declaring the optional extension
   `radiolack` 1.0.0: the samples are never scaled. Then a
   capture for each stretch of samples: `core:sample_start`,
   `core:frequency`, the receiver's centre frequency on the air, and `core:datetime`,
   the time of its first sample, in UTC to the millisecond; no annotation.
   A retune, by an interface or a scan, starts a capture, as do a change of
   frequency correction and the samples that come after a gap, such as
   when rtl_tcp's connection drops and comes back, a SoapySDR receiver is
   unplugged and back or reports an overflow, or the engine's event loop
   falls more than a second behind a SoapySDR receiver's reading thread. The engine writes the
   metadata whole, renaming a new file over the old one, with the first
   samples, then at most every 5 s while the captures change, and at the
   end: a crash leaves a recording that reads. It takes about 17 GB an hour
   at 2.4 MS/s in 8 bits, twice that in 16 bits and four times in 32-bit
   floats: up to 230 GB an hour at 8 MS/s. The engine writes in batches of
   4 MiB at most, checking before each write that it leaves 2 GiB free,
   and stops the recording (`disk`) once more than 2 s of samples, and at
   least 64 MiB, wait for the disk. Like the audio's, it goes on without any client, never
   starts at start and never runs on the simulated band; both may run at
   once.

9. Then, when the engine opens SoapySDR devices (`soapy` among the source
   state's `kinds`), the SoapySDR devices, a JSON text message, sent again
   whenever it changes:

   ```json
   {"type": "devices", "search": "done", "detail": "",
    "drivers": ["hackrf", "airspy", "airspyhf", "lime", "bladerf"],
    "devices": [{"driver": "hackrf", "serial": "0001", "label": "HackRF One #0 0001", "in_use": true},
                {"driver": "airspy", "serial": "0002", "label": "airspy 0002", "in_use": false}],
    "chosen": true, "args": "driver=hackrf,serial=0001"}
   ```

   | Field | Meaning |
   | --- | --- |
   | `search` | The search's state: `never` (none since the engine started), `searching`, `done`, `failed` (a driver's search failed; the others' findings are listed) or `unavailable` (the SoapySDR Python module is missing, and nothing was searched). |
   | `detail` | Why, with `failed` and `unavailable`, a sentence for the user, such as `the SoapySDR Python module is not installed (on Debian and Ubuntu: python3-soapysdr)` or `the airspy search failed: …`; otherwise empty. |
   | `drivers` | The drivers that a search asks, in its order, each alone: never `rtlsdr`, whose receivers have the Receiver list's entries of their own. |
   | `devices` | The SoapySDR device in use first, with `in_use` true, whether a search found it or not (an RTL-SDR opened by `driver=rtlsdr`, for instance), then those that the latest search found, at most 64. `driver` and `serial` name a device; `label` is its module's label, or its driver and serial. Nothing else of a module's listing is kept, such as a position on the bus. |
   | `chosen` | Whether a SoapySDR device was ever chosen, so that the source message's `kind` `soapy` opens it again rather than being refused. |
   | `args` | To this computer's interfaces only: the device arguments of the SoapySDR device in use, or null while another source is in use; always null for a remote client. |

   The engine searches only when one of this computer's interfaces asks
   (the `devices` message, below): never at start, never for a remote
   client, never without a driver, one driver at a time and one search at a
   time, in a thread of its own. The driver of the SoapySDR device in use is
   not searched again, so that no search disturbs the device that streams:
   that driver's devices stay as the previous search found them. A search
   opens no stream, but each module's search talks to the devices plugged
   in. The list lives in memory only.

10. Then, to this computer's interfaces only, the storage state, a JSON text
    message, sent again whenever it changes:

    ```json
    {"type": "storage", "directory": "~/.config/radiolack", "saving": true, "problem": ""}
    ```

    | Field | Meaning |
    | --- | --- |
    | `directory` | The settings folder, `~` standing for the home folder; null when the engine saves nothing (a server started without a settings folder, as in tests). |
    | `saving` | False while some changes do not reach the disk: a file could not be written, and the engine tries again at the next change, or a file is left as it is until the next start, as it cannot be read or comes from a newer Radiolack. |
    | `problem` | What went wrong with the files, in sentences for the user, or empty. A file that could not be read and was set aside shows here while `saving` stays true. |

11. Then, to this computer's interfaces only, the update state, a JSON text
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

12. Then, to this computer's interfaces only, remote access's state, a JSON
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
    | `on` | Whether remote access is on: it then listens on the addresses that `urls` gives, unless it has none. False at each start. |
    | `port` | The port it listens on, or will. |
    | `urls` | What a device opens: by each address it listens on, then by this computer's mDNS name, when its host name is a valid one. The name also leads to the computer's IPv6 addresses, if any, where remote access does not listen. Empty while off, and while on without an address to listen on, as `problem` then says. |
    | `code` | The access code, in two groups of four characters, while on; null while off. |
    | `blocked` | Too many wrong codes were entered (10): the code no longer works until a new one is made. |
    | `devices` | The paired devices, in the order they were paired: `id`, which a `remote` message names; `label`, such as `iPhone · Safari`, made of known words only; `paired` and `seen`, when it was paired and last used, in Unix time, the latter to the hour; `connected`, whether it has a stream open. |
    | `fingerprint` | The SHA-256 fingerprint of the local authority's root certificate, which devices install, as upper-case hex bytes separated by spaces; null before remote access first turns on. |
    | `problem` | Why remote access could not turn on, or, while on, why it listens nowhere, in sentences for the user; or empty. |

13. Binary spectrum frames, about 30 per second while the source streams.
    Each frame is a 40-byte little-endian header followed by one byte per FFT
    bin:

    | Offset | Type | Field |
    | --- | --- | --- |
    | 0 | uint8 | kind: 1, a spectrum frame |
    | 1 | uint8 | frame format version: 2 |
    | 2 | uint16 | flags: bit 0 set while the squelch is open; the other bits are 0 |
    | 4 | uint32 | sequence number, wrapping at 2^32 |
    | 8 | float64 | centre frequency, Hz: the source state's `centre_in_use_hz` when the samples were taken |
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
    the VFO lies outside the span. With the noise blanker (`blanker`) it is
    that of the blanked channel, as the squelch's, while the frame's bins
    show the samples as received. The squelch opens once the power averaged
    over 10 ms reaches the threshold and closes 3 dB below it; the flag gives
    its state at the end of the samples. The threshold is `squelch_db`, or
    the automatic one, which is NaN while the engine has no noise estimate
    (the VFO outside the span); the squelch then stays closed.

14. Binary audio frames, about 30 per second, only to an interface that asked
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

    The audio rate is 12 kHz, or 32 kHz in WFM, for the usual receiver
    rates; at an unusual sample rate it comes within 0.5% of it, such as
    12026.2 Hz at 2345678 Hz. It changes as the mode goes into or out of
    WFM. A frame follows the previous one when its position is the previous
    position plus the previous number of samples; otherwise samples are
    missing, such as frames the engine dropped for a slow client.

    In NFM the audio is the FM discriminator's output, full scale at 5 kHz of
    deviation, with a 750 µs de-emphasis (unit gain at 1 kHz), a high-pass
    filter at 280 Hz that removes CTCSS tones and a low-pass filter at
    3.5 kHz. In AM it is the envelope over the carrier's level, less one:
    the modulation, 0.8 of full scale at 100%, whatever the signal's
    strength. In USB and LSB it is the channel's real part, and in CW too,
    with the VFO at 700 Hz in the channel, so that a carrier on the VFO
    sounds at 700 Hz; an AGC brings a steady signal to 0.4 of full scale and
    lets the gain come back at 10 dB per second. In SAM it is the channel
    turned by the phase of its carrier, which a band-pass 80 Hz wide
    follows, along the carrier, less the carrier, over a level that follows
    the carrier's at once and falls at 10 dB per second: unfaded, as loud as
    AM, and the sidebands' audio stays as it is while the carrier fades
    alone. Without a lock (`sam_locked`) it is AM's, the switch fading over
    5 ms. With the noise blanker (`blanker`), in every mode, each of these
    takes the channel of the samples with their impulses zeroed. AM, SAM,
    USB, LSB and CW audio runs from 100 Hz to 5 kHz; with
    the notch (`notch`), in AM, SAM, USB and LSB, less 45 Hz on each side of
    each line it removes, the audio fading over 5 ms from the filter before
    a change to the filter after it. In WFM the audio is the FM discriminator's
    output too, full scale at 75 kHz of deviation, broadcast's peak, with a
    50 µs de-emphasis (unit gain at the lowest frequencies) and a band from
    40 Hz to 15 kHz: mono, without the stereo pilot at 19 kHz or what lies
    above it. The audio is silent while the squelch is closed, with 5 ms
    fades, and in the modes not in `audio_modes`. It lags its samples by the
    filters' delay and the squelch's look-ahead: about 40 ms in NFM, AM, SAM
    and WFM, 45 ms in USB and LSB, 60 ms in CW.

15. Binary fine frames, about 10 per second, only to an interface that asked
    for the fine spectrum (`fine` message): the spectrum of the VFO's
    channel before the demodulator, at a few hertz per bin. Each frame has
    the spectrum frames' 40-byte little-endian header, of another kind,
    followed by one byte per bin:

    | Offset | Type | Field |
    | --- | --- | --- |
    | 0 | uint8 | kind: 3, a fine frame |
    | 1 | uint8 | fine format version: 1 |
    | 2 | uint16 | flags: 0 |
    | 4 | uint32 | sequence number, wrapping at 2^32 |
    | 8 | float64 | centre frequency, Hz: the frequency on the air of the channel's 0 Hz, bin `n / 2` |
    | 16 | float64 | width, Hz: the channel's rate on the air |
    | 24 | float32 | level of code 0, dBFS |
    | 28 | float32 | level step per code, dB |
    | 32 | float32 | NaN: a fine frame carries no channel level |
    | 36 | float32 | NaN: a fine frame carries no squelch threshold |
    | 40 | uint8[] | one code per bin, lowest frequency first: 8192 |

    As in a spectrum frame, with `n` bins, bin `i` is centred on
    `centre - width / 2 + i * width / n`, and code `c` stands for
    `level0 + c * step` dBFS, code 0 every lower level too; the levels are
    the spectrum frames' (a full-scale tone centred on a bin reads 0 dBFS),
    and so are `level0` and `step`.

    The engine takes the channel from the demodulator's front filter, which
    brings the VFO's neighbourhood to the channel's rate, before the mode's
    filter: 48 kHz in every mode but WFM, 320 kHz in WFM, or within 0.5% of
    it at an unusual sample rate, as the width tells. A bin is then 5.9 Hz
    wide, or 39 Hz in WFM. The centre is the VFO's frequency, and in CW the
    VFO's less the CW pitch, 700 Hz, where the channel's 0 Hz lies. Both
    the centre's distance from the VFO and the width are scaled, as every
    frequency on the air, when the receiver's samples are labelled with a
    rate that differs from the rate asked (an R820T or R828D whose tuner is
    placed on its grid, with a correction). The cover, where the front
    filter passes the signal, is 0.4 times the width on each side of the
    centre (19.2 kHz at 48 kHz, 128 kHz in WFM): flat to 16 kHz, 0.8 dB
    down at 18 kHz and 3.1 dB at the edge at 48 kHz, within 0.1 dB in WFM;
    the bins beyond it show the filter's stopband, not the air.

    Each FFT is Hann-windowed, over 8192 samples of the channel (0.17 s at
    48 kHz, 0.026 s in WFM), and a frame is the mean power of the FFTs that
    ended since the previous frame: at 48 kHz one FFT every 4800 samples,
    which overlap, a frame each; in WFM one every 8192 samples, three or
    four to a frame. The first frame comes once 8192 samples came, 0.17 s
    at 48 kHz, 0.1 s in WFM, then one every 0.1 s. A frame never mixes two
    settings: a change of the VFO's frequency or mode, of the source's
    settings, of DC removal (`dc_removal`) or of the source starts the FFTs
    again, dropping the samples gathered, and the frames of a block
    processed while such a change came are dropped. While the VFO moves,
    as it does block after block during a drag, no frame comes; nor while
    the VFO's channel receives nothing (the VFO outside the span, WFM below
    320 kHz, or a sample rate below the channel's). With the noise blanker
    (`blanker`), a fine frame shows the samples as received, as the
    spectrum frames do.

    The engine computes the fine spectrum only while at least one interface
    asked for it, once for all of them, and every interface's frames take
    the same sequence: a gap in it is frames dropped for a slow interface,
    or sent while it had not asked. A remote client gets every frame, with
    2048 bins, each the highest of the four it groups, so that narrow lines
    stay visible.

16. The observations, a JSON text message, to one interface only, when it
    asks for them (`observations` message): the channels where the engine
    found signals, the latest found first.

    ```json
    {"type": "observations", "channels": [
      {"frequency_hz": 145500000, "channel_hz": 145500000, "first_s": 1794000000, "last_s": 1794350000,
       "transmissions": 12,
       "active_s": 95.0, "margin_db": 31.0,
       "hours": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 10, 0, 0, 0, 0]}]}
    ```

    | Field | Meaning |
    | --- | --- |
    | `frequency_hz` | The channel's frequency, a whole number of Hz: it moves 30% of the way to each signal found there. |
    | `channel_hz` | The frequency it shows on, as a signal found narrower than 4 kHz would (`channel_hz` there); `observations.json` keeps none. |
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

17. The recordings, a JSON text message, to one interface only, when it
    asks for them or deleted one (`recordings` message): the latest 500
    recordings of the recordings folder, the newest first.

    ```json
    {"type": "recordings", "files": [
      {"name": "2026-11-14_15-31-02Z_145.000000MHz_2.400000MSps.sigmf-data", "kind": "iq",
       "started": 1794670262, "frequency_hz": 145000000, "mode": "I/Q", "bytes": 212161024, "seconds": 44.2,
       "running": false, "rate_hz": 2400000, "meta": "2026-11-14_15-31-02Z_145.000000MHz_2.400000MSps.sigmf-meta"},
      {"name": "2026-11-14_15-30-45Z_145.500000MHz_NFM.wav", "kind": "audio", "started": 1794670245,
       "frequency_hz": 145500000, "mode": "NFM", "bytes": 2947244, "seconds": 61.4, "running": false}],
     "more": 0, "free_bytes": 52613349376, "problem": ""}
    ```

    | Field | Meaning |
    | --- | --- |
    | `files` | The regular files of the folder named as the engine names its recordings, links excluded: WAV files, and I/Q recordings' data files. Each gives its `name`, its `kind` (`audio` or `iq`), its start (`started`, in Unix time), `frequency_hz` and `mode` as its name tells them (`I/Q` for an I/Q recording), its size in `bytes` and its length in `seconds`, to the tenth, from its WAV header, or its data's size and the format that its metadata's `core:datatype` names, within the metadata's first 64 KiB; both are null when the file cannot be read, and `seconds` for a file that is not Radiolack's WAV and for an I/Q recording whose metadata is missing or names no format that the engine knows. `running` is true for a recording that runs. An I/Q recording also gives `rate_hz`, its sample rate as its name tells it, and `meta`, its metadata file, which `bytes` includes, or null when it is missing. |
    | `more` | How many older recordings the folder holds besides. |
    | `free_bytes` | The space free on the folder's disk, or on its nearest folder that exists; null when it cannot be known. |
    | `problem` | Why the folder could not be read, in a sentence for the user, or empty. A folder not made yet holds no recording. |

18. A problem report, a JSON text message, to one interface of this
    computer only, when it asks for one (`report` message): what its user
    may share, or not, when Radiolack misbehaves (README.md, Problem
    reports).

    ```json
    {"type": "report", "text": "Radiolack 0.12.0 (Debian package)\nPython 3.11.2, aiohttp 3.8.4, NumPy 1.24.2\n...",
     "issue_url": "https://github.com/OGD09/radiolack/issues/new?body=..."}
    ```

    | Field | Meaning |
    | --- | --- |
    | `text` | The report, in plain text: Radiolack's version, Python's, aiohttp's and NumPy's, the system, the desktop window's toolkit when the window runs, what the interface told of itself, the receiver's kind, state, tuner and sample rate, how long Radiolack has run, then its latest 50 warnings and errors, in UTC, oldest first; the home folder is written `~`. |
    | `issue_url` | GitHub's page for a new issue of Radiolack, with the report as its description, under a heading for the user's own: the oldest warnings and errors left out, as many as needed to keep the address under 7000 characters. |

19. A carrier's measure, a JSON text message, to one interface only, when
    it asks for one (`carrier` message), about two seconds later: the
    strongest line within the VFO's filter, as the receiver gives the
    channel, before the demodulator, and unblanked whether the noise
    blanker is on or not: the measure does not change with it.

    ```json
    {"type": "carrier", "id": 12, "frequency_hz": 9999987.3, "margin_db": 47.2, "reason": null}
    {"type": "carrier", "id": 13, "frequency_hz": null, "margin_db": 12.4, "reason": "no_signal"}
    ```

    | Field | Meaning |
    | --- | --- |
    | `id` | The `id` of the interface's latest `carrier` message, or null when it gave none. |
    | `frequency_hz` | The line's frequency on the air, to the tenth of a hertz, in every mode, through direct sampling and a converter alike; null when there is none, and `reason` says why. |
    | `margin_db` | How far the line stands above the median of the filter's bins, in dB, to the tenth; null when nothing was measured, or no line stands out within the filter. |
    | `reason` | Null with a frequency. Otherwise `silent`: the VFO's channel receives nothing, outside the span, in WFM below a sample rate of 320 kHz, or no samples came for 6 s; `no_signal`: no line stands 20 dB above the median within the filter, or the strongest bin there is no peak, its line lying beyond the filter's edge; `several`: another line, more than 5 Hz from the strongest, comes within 6 dB of it; `unstable`: two seconds in a row disagree by more than 1 Hz, or the measure, which starts again whenever the receiver's settings, the VFO's frequency, mode or filter, or the span change, could not finish within 6 s. An interface shows a reason it does not know as a failed measure. |

    The engine measures one second of the channel, at the channel rate, 48
    or 320 kHz (bins of 1 Hz), through a Hann window and an FFT, places the
    strongest bin within the filter between its neighbours with a parabola
    through their levels in dB, then measures the next second: the result
    is their mean. A second without a line, or whose line stands too low or
    has a rival, ends the measure at once. In CW the channel's 0 Hz lies
    the CW pitch below the VFO; the frequency given is the one on the air
    either way. The engine gathers the channel only while an interface
    waits for a measure: the interfaces that ask while one runs share it,
    and each receives it alone, with its own latest `id`; one that goes
    away stops waiting, and once none waits nothing more is measured.

20. A driver's check, a JSON text message, to the interfaces that asked
    for it (`carrier` message with the action `identify`) only: which of
    the rtl-sdr driver's two computations an R820T's or R828D's PLL
    follows, or why none was named.

    ```json
    {"type": "driver_pll", "id": 3, "driver_pll": "truncating", "previous": "unknown", "reason": null}
    {"type": "driver_pll", "id": 4, "driver_pll": null, "previous": "rounding", "reason": "scan"}
    ```

    | Field | Meaning |
    | --- | --- |
    | `id` | The `id` of the interface's `identify` message, or null when it gave none. |
    | `driver_pll` | `truncating` or `rounding`: the computation the carrier followed, now the source's `driver_pll`, saved with the receiver; null when none was named, and `reason` says why. |
    | `previous` | The source's `driver_pll` when the check began: `unknown`, `truncating` or `rounding`; null for a source without it. An answer equal to it confirms it; another replaced it. |
    | `reason` | Null with a computation. Otherwise, before anything moved: `receiver`, the source is not an RTL-SDR whose tuner is an R820T or R828D; `direct`, it samples directly; `range`, the tuner is not placed on its grid there (`centre_in_use_hz`); `scan`, a memory scan or a sweep runs; `iq`, an I/Q recording runs; `position`, the VFO lies within 3 kHz of the centre in use, or nearer the span's edge than the filter's width and 2 kHz, where a move of the centre would bring the DC residue onto the carrier or take it out of the span; `indistinct`, no centre within 2 kHz of the centre in use lets the two computations differ by 40 Hz or more. `lost`, before anything moved, the VFO's channel receives nothing at this sample rate (WFM below 320 kHz), or, after the check moved the receiver, a point had no steady carrier within 6 s (the carrier's measure's `silent`, `no_signal`, `several` or `unstable`, or no samples). Once every point is measured: `neither`, the carrier's moves followed neither computation within 5 Hz at every point; `both`, they followed both. And `stopped`: the check stopped before its end: an interface changed the source's settings or the VFO's frequency, mode or filter, a scan or an I/Q recording started, every interface that asked went away, or Radiolack stopped. A failed check leaves `driver_pll` as it was. An interface shows a reason it does not know as a failed check. |

    The engine measures the carrier, as for a carrier's measure, on the
    channel unblanked whether the noise blanker is on or not, at the
    centre in use, then at three centres off the tuner's grid
    (`tuner_grid` false), within 2 kHz of it, chosen where the two
    computations put the carrier 40 Hz apart at least, and compares how
    far the carrier moved from one centre to the next with what each
    computation predicts: what the crystal's error, the correction, the
    `driver_pll` set and the true frequency add is the same at every point,
    and cancels out. Whatever ends the check, the receiver goes back to its
    centre and its grid, with the computation found in the same change,
    and the source state goes out to every interface before the answer.
    The check takes about ten seconds, and every interface sees the
    centre move. The interfaces that ask while it runs wait for the same
    one; each receives the answer with its own latest `id`. While it runs,
    a carrier's measure waits for its end, and is then taken from the
    receiver as it is back. An interface that goes away stops waiting;
    once none waits, the check stops.

21. An error, to one interface only, when the engine refuses a message from
    it. `command` is the message's type, or null when it is not a JSON
    object of a known type:

    ```json
    {"type": "error", "command": "source", "message": "the receiver tunes from 24 to 1766 MHz"}
    ```

What is waiting for an interface goes out in this order: the source state,
the demodulation settings, the bookmarks, the scanner state, the signals
found, the recording state, the storage state, the update state and remote
access's state, each when it changed (and the preferences at the start),
then the observations, the recordings, the reports, the carrier's
measures, the driver's checks and errors, in the
order of the messages they answer, then audio frames, then fine frames,
then spectrum frames. A client that reads too slowly loses its oldest
frames: the engine keeps the latest four spectrum frames, fifteen audio
frames (half a second) and two fine frames (a fifth of a second) for it and
never slows down for one client; it keeps its latest states, and up to
eight observations, recordings, reports, carrier's measures, driver's
checks and errors together. When
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
| `kind` | Another kind of source, among `kinds`, alone in its message. The source in use closes, keeping its settings for when it comes back, and the engine opens the receiver used last of that kind (the first RTL-SDR plugged in, the first time) with the settings saved for it, the demodulation settings included. Frames resume once the previous receiver is released. `soapy` is refused, with an error that says so, until a SoapySDR device was chosen once, by the `devices` message or `--soapy` (the devices message's `chosen`), and while a search for SoapySDR devices runs. |
| `address` | For an `rtl_tcp` source: the rtl_tcp server to read, as `HOST`, `PORT` or `HOST:PORT` (an IPv6 host in brackets), the missing part being `127.0.0.1` or `1234`. Another address hangs up and connects there with the same settings; the tuner is unknown until the server answers. |
| `centre_hz` | Centre frequency in Hz, within `range_hz`, rounded to the Hz. |
| `sample_rate_hz` | Sample rate in Hz. rtl_tcp takes librtlsdr's two ranges: 225001 to 300000 Hz and 900001 to 3200000 Hz; a `soapy` source the rates its device offers up to 8 MS/s, and before the device is first found any rate up to 8 MS/s, which the device then brings to the nearest it offers. Below 320000 Hz, WFM's channel rate, WFM gives no channel level and no audio. |
| `gain_db` | Tuner gain in dB, or null for automatic gain. A gain becomes the nearest in `gains_db`. |
| `ppm` | Frequency correction in parts per million, from -100 to 100, rounded to the hundredth; a whole number, as an older interface sends, is taken as it is. The receiver retunes: frames follow a moment later, as after a change of `centre_hz`; an R820T or R828D whose request stays on the same point of its grid does not, and only the label changes. |
| `hf_input` | One of `hf_inputs`. Without a `centre_hz` in the same message, the centre frequency moves to the nearest the new input reaches, as it does after a new `sample_rate_hz`. Entering or leaving direct sampling retunes: frames follow a moment later. |
| `rtl_agc` | `true` or `false`; applies while the receiver samples directly. |
| `bias_tee` | `true` or `false`. The interface asks for a second press before it sends `true`. A `soapy` device not yet opened takes it, and turns its antenna power on once it opens, or ignores it, saying so in the engine's log, if it has none. |
| `stage_gains_db` | Some of a `soapy` device's stages, by name, each to a gain in dB, which becomes the nearest the stage offers: `{"LNA": 24}`. A stage the device does not have is ignored. The overall gain is then read back. Before the device first opens, any stage, which its opening fits. |
| `antenna` | One of `antennas`; before the device first opens, any name, which its opening fits. |
| `device_settings` | Some of the device's settings, by key, each to a value its `device_setting_info` takes: `{"rfnotch_ctrl": true, "agc_setpoint": -40}`. A number becomes the nearest on its steps. A key the device does not declare is ignored. The engine writes them between two reads of the device and reads each back: a setting that the device refuses keeps its value, and `device_refusal` says why. |
| `converter` | `none`, `up`, `down` or `down_inverted`. Without a `centre_hz` in the same message, the receiver stays where it is tuned: the centre frequency on the air moves, and the VFO moves with it, so as to stay on the same signal. The change is refused when that centre frequency would be out of the new `range_hz`, or when the receiver would reach nothing on the air. Another converter, or another oscillator behind one, stops a scan (`tuned`) and an I/Q recording (`converter`), and empties the signals found, which detection finds again at their new frequencies; frames follow a moment later. |
| `converter_lo_hz` | The oscillator's frequency, from 1 Hz to 100 GHz, rounded to the Hz; as `converter` for the rest. |
| `driver_pll` | `unknown`, `truncating` or `rounding`. The label of the samples changes, and frames carry the new `centre_in_use_hz`; nothing is asked of the receiver. |
| `tuner_grid` | `true` or `false`. Off, the next centre is asked as it is, which retunes when it moves the frequency asked. |
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
| `bandwidth_hz` | From 100 to 25000 Hz, or in WFM from 50000 to 250000 Hz: the range of the mode that the message leaves. A new mode that the width in use does not suit, into or out of WFM, takes its width once tuned to a bookmark (NFM 10 kHz, AM 8 kHz, USB and LSB 2.7 kHz, CW 500 Hz, WFM 180 kHz), unless the message gives one. |
| `squelch_db` | From -160 to 0 dBFS. |
| `squelch_auto` | `true` or `false`. |
| `notch` | `true` or `false`. |
| `blanker` | `true` or `false`. |
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

A `scanner` message changes the signal detection, the memory scan, the
sweep or the signals scan, the same for every interface:

```json
{"type": "scanner", "detection": false}
{"type": "scanner", "threshold_db": 18}
{"type": "scanner", "resume_s": 4}
{"type": "scanner", "listen_s": 20}
{"type": "scanner", "sweep_low_hz": 1240000000, "sweep_high_hz": 1300000000}
{"type": "scanner", "sweep_window_s": 1}
{"type": "scanner", "sweep_listen": true, "sweep_passes": 3}
{"type": "scanner", "ignore": 144800000}
{"type": "scanner", "restore": true}
{"type": "scanner", "scan": "memory"}
{"type": "scanner", "scan": "sweep"}
{"type": "scanner", "scan": "signals"}
{"type": "scanner", "scan": null}
```

| Field | Value |
| --- | --- |
| `detection` | `true` or `false`. Turning detection off empties the list of the signals found and stops the signals scan and a listening sweep. |
| `threshold_db` | A number from 6 to 50. |
| `resume_s` | A number from 0 to 8. |
| `listen_s` | A number from 2 to 60. |
| `sweep_low_hz`, `sweep_high_hz` | Frequencies from 1 Hz to 100 GHz, rounded to the Hz, the lowest below the highest once the message applies: one may come without the other. A new range starts a running sweep again from its first window. |
| `sweep_window_s` | A number from 0.25 to 2. A running sweep takes it at once. |
| `sweep_listen` | `true` or `false`. A running sweep takes it at once: turned off while it listens, it goes on to the next window. |
| `sweep_passes` | A whole number from 1 to 99, or null for endless. A running sweep takes it at once, the passes done counting: it stops at the end of a pass once they reach it. |
| `ignore` | A frequency from 1 Hz to 100 GHz, rounded to the Hz: the signals within 1.5 kHz of it, or within a quarter of their width, leave the list and are not listed again. A frequency within 1.5 kHz of one already ignored adds none. |
| `restore` | `true`: no frequency is ignored any more. The signals left out come back once found again. |
| `scan` | `memory` starts the memory scan, `sweep` the sweep, `signals` the signals scan, unless it runs, stopping the scan that runs; null stops the scan that runs, if any. |

`detection`, `threshold_db`, `resume_s`, `listen_s`, `sweep_low_hz`,
`sweep_high_hz`, `sweep_window_s`, `sweep_listen` and `sweep_passes` may come together; `ignore`, `restore` and `scan`
come alone in their message. The engine refuses the whole message,
changing nothing, when it has no setting, an unknown or invalid one, a new
frequency to ignore while 200 are ignored already, the memory scan to start
while it would watch no bookmark (`memory.watched` is empty), the
signals scan to start while detection is off, the sweep to start with
`sweep_listen` while detection is off, `sweep_listen` turned on while a
sweep runs and detection is off, the sweep to start with a source that does
not tune and a range wider than its span, or the sweep to start or
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

A `recording` message starts or stops a recording, the same for every
interface, or chooses the recordings folder, one setting at a time:

```json
{"type": "recording", "audio": true}
{"type": "recording", "audio": false}
{"type": "recording", "iq": true}
{"type": "recording", "folder": "~/Music/Radiolack"}
{"type": "recording", "folder": null}
```

| Field | Value |
| --- | --- |
| `audio` | `true` starts recording the audio, unless an audio recording runs; `false` stops the audio recording that runs, if any: its file is complete a moment later. |
| `iq` | `true` starts recording the receiver's raw I/Q, unless an I/Q recording runs; `false` stops the I/Q recording that runs, if any: its files are complete a moment later. |
| `folder` | The folder that the recordings go to from now on, a full path, `~` standing for the home folder, up to 4096 characters; null or an empty text for the default folder. Only while nothing records. The engine checks, in the background, that the folder exists and that it may write into it, then saves it with its settings: this computer's interfaces receive the recording state, or the sender an error. Recordings made before stay where they are. |

The engine refuses a message without a setting or with more than one, an
unknown or invalid setting, a recording on the simulated band ("the
simulated band is not recorded"), an I/Q recording from a source without
raw samples, a recording or a folder when it has no recordings folder
("this Radiolack does not record"), and a folder while a recording runs;
it answers the sender with an error. A recording that
cannot go on, such as with less than 2 GiB free, starts and stops at once:
the recording state's `stopped` says why.

A `recordings` message asks for the recordings, or deletes one:

```json
{"type": "recordings"}
{"type": "recordings", "delete": "2026-11-14_15-30-45Z_145.500000MHz_NFM.wav"}
```

`delete` takes a recording as the list names it, named as the engine names
them, but not a recording that runs: a WAV file, or an I/Q recording's data
file, whose metadata goes first. The engine refuses the whole message,
changing nothing, when it has an unknown or invalid field. Otherwise it
deletes the file in the background, when it is a regular file, and answers
the sender alone with the recordings, after the deletion: an error comes
first when the file could not be deleted, and a file already gone is no
error. The other interfaces see the change when they next ask.

Recordings are downloaded by HTTP, on the loopback server and on remote
access's: `GET /recordings/<file>` answers a recording of the list, or an
I/Q recording's metadata, which the engine names so, a regular file that is
not one of a recording that runs, as an attachment with its length, of
type `audio/wav` for a WAV file and `application/octet-stream` for SigMF's
files, which a browser then saves under their own name, and
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`. It answers 404 for any other name, a link
included, 403 for a recording that cannot be read, and 405 for `HEAD`.
The loopback server answers requests addressed to it alone (`Host`), as for
the interface's files; remote access, paired devices alone: 403 for the
others.

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

A `report` message asks the engine for a problem report, which comes to the
sender alone, with what the interface tells of itself, if it does:

```json
{"type": "report", "page": "Mozilla/5.0 (X11; Linux x86_64) ...; WebGL2 views; 1280x800 at 1x"}
```

`page`, a line of text of 300 characters at most, goes into the report as
it is. The engine refuses other fields and another `page`, and the message
of a remote client.

A `carrier` message asks the engine to measure the carrier at the VFO,
which comes to the sender alone (the carrier's measure, above):

```json
{"type": "carrier", "action": "measure", "id": 12}
```

```json
{"type": "carrier", "action": "identify", "id": 3}
```

`action` is `measure`, or `identify`, which asks for the driver's check
on the carrier at the VFO (the driver's check, above) instead; `id`,
optional, a whole number from 1 to 2⁵³ − 1, which the measure or the check
repeats. An interface that asks again before its measure comes waits for
the same one, which carries its new id; likewise for a check. The engine
refuses other fields and values.

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
| `on` | `true` or `false`. Turning on gives remote access a new server certificate, for this computer's current addresses, and listens; turning off closes the remote streams, then releases the port. Both go on in the background: the remote access state tells how it went, with a `problem` when remote access could not turn on. While on, remote access looks at this computer's addresses every 10 s: once they change, it closes the remote streams with code 1012, then listens on the new addresses with a new certificate. It stays on, listening nowhere, while there are none or while it cannot listen on them, and tries again at each check where it could not listen; `problem` says why. |
| `port` | From 1024 to 65535, for the next turn-on: only while remote access is off. |
| `new_code` | `true`: a new access code, which no wrong code has been tried against. Every device is forgotten, and its streams close. |
| `forget` | A paired device's `id`: the device is forgotten, and its streams close. |

The engine refuses a message without a setting or with more than one, an
unknown or invalid setting, or a device it does not know, and answers the
sender with an error. This computer's interfaces receive remote access's
state as it changes.

A `devices` message, to an engine that sends the SoapySDR devices, searches
for them or opens one, one field at a time:

```json
{"type": "devices", "search": true}
{"type": "devices", "choose": {"driver": "hackrf", "serial": "0001"}}
{"type": "devices", "args": "driver=rtlsdr"}
```

| Field | Value |
| --- | --- |
| `search` | `true`: a search, unless one runs, which it leaves to end. The devices state says `searching`, then what it found. |
| `choose` | A device that the latest search found, by its `driver` and `serial`, both text: the engine opens it by those two, as `driver=hackrf,serial=0001`, and saves that as the SoapySDR device to open again. The device in use changes nothing. |
| `args` | SoapySDR device arguments, `KEY=VALUE` separated by commas, naming a driver, as `--soapy` takes them, `driver=rtlsdr` included: the engine opens the device they name and saves them. The arguments in use change nothing. |

Opening a device switches to it as the source message's `kind` does,
the SoapySDR device in use closing first; its settings are those saved
for its driver and serial number. The engine refuses a message without a
field or with more than one, an unknown or invalid field, a device the
latest search did not find (search again), arguments that `--soapy` would
refuse, which it says why, and opening a device while a search runs.

A remote client may send `source`, `demod`, `bookmark`, `scanner`,
`observations`, `preferences`, `recording`, `recordings`, `carrier`,
`audio`, `fine` and `devices` messages, but neither `update` nor `remote` messages,
nor a `source` message that names `address`, nor a `recording` message that names
`folder`, nor a `devices` message that names `search` or `args`: the
engine refuses them with the error "only Radiolack's
interface on the computer may change this". It may choose among the
SoapySDR devices found, as it may switch to another kind of source. Its `preferences` are its
device's own: they change neither this computer's nor another device's.
Its recording state comes without `folder` and `default_folder`, and its
devices state with `args` null.

An `audio` message starts or stops the audio frames for this interface; they
are off when the stream opens:

```json
{"type": "audio", "on": true}
```

The engine demodulates audio only while at least one interface asked for it,
but always measures the channel for the spectrum frames.

A `fine` message starts or stops the fine frames for this interface; they
are off when the stream opens:

```json
{"type": "fine", "on": true}
```

The engine computes the fine spectrum only while at least one interface
asked for it, and forgets what it gathered once none does. Asking twice
changes nothing, nor does stopping without having asked. A message
without `on`, or whose `on` is not `true` or `false`, is refused with the
error "the fine spectrum must be turned on or off: true or false", and one
with another field with "unknown fine setting: …". An engine older than
the fine spectrum refuses the message as one of an unknown type: an
interface sends it only when the hello lists `fine` among its `spectra`.

While an `rtl_sdr` or `rtl_tcp` source waits, changes are kept and sent when
it connects. Until its first connection, and after a change of address, the
tuner is unknown: `range_hz` spans every frequency an rtl_tcp command can
carry, any gain and any shortwave input pass, and the source brings them
within what the tuner can do once it connects, `none` replacing an input it
does not offer.

## Remote access

While on, remote access serves the interface and the stream over HTTPS, with
a server certificate from Radiolack's local authority, on the port and the
addresses its state gives. It answers only peers whose own address is
private (the ranges README.md lists, under Remote access), and only requests
whose `Host` header names it as one of its `urls` does; while off, turning
off or moving to new addresses, it answers every request with 503. Its
answers carry `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`
and `Referrer-Policy: no-referrer`.

| Request | Answer |
| --- | --- |
| `GET /` | The interface, `index.html`, to a paired device; the pairing page, `pair.html`, to any other. |
| `GET /certificate` | The local authority's root certificate, in PEM, as an attachment of type `application/x-x509-ca-cert`, which a device installs. |
| `GET /<file>` | An interface file, as on the loopback server. Before pairing, only `pair.js`, `pair.css`, `style.css`, `favicon.svg` and `apple-touch-icon.png`: 403 for the others. |
| `POST /pair` | Checks an access code, below. |
| `GET /stream` | The stream, to a paired device. |
| `GET /recordings/<file>` | A recording, or an I/Q recording's metadata, to a paired device, as on the loopback server ([From the interface](#from-the-interface)); 403 to any other. |

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
| 200 | `{"paired": true}` | The right code, with the session cookie (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, 400 days), or a device paired already, whatever the code, with its cookie set again. Not `Strict`: Safari leaves such cookies out of the pages it restores, its WebSocket included, and a Home Screen web app reopened would be asked for the code again. |
| 400 | `{"error": "Send the access code."}` | No `code` text in the body. |
| 403 | `{"error": "Wrong code."}` | A wrong code, answered after a second. |
| 409 | `{"error": …}` | 20 devices are paired already. |
| 429 | `{"error": …}` | 10 wrong codes were entered: the code no longer works until a new one is made. |

Other refusals are plain text: 403 for another origin, 413 for a larger
body, 415 for a body that is not `application/json`, and 503 while off or
moving.

The stream needs the same origin too. Its handshake succeeds whatever the
cookie, so that the page learns why the stream closes: with code 4001 at
once for a device that is not paired, and later when the device is
forgotten or a new code is made; with code 4002 when remote access turns
off; with code 1012 (Service Restart) when it moves to this computer's new
addresses. The server pings a remote client every 20 s, and drops one that
stops answering.

The stream accepts permessage-deflate (RFC 7692), which browsers offer and
which halves the spectrum frames; a message from the device must stay
within 64 KiB once inflated. The loopback server's stream does not compress.
On a link slower than the stream, the server keeps at most about 16 KiB
unsent in the socket (`TCP_NOTSENT_LOWAT`, where the system has it) and in
its transport: the engine then drops the device's oldest frames, as for any
client that reads too slowly, instead of the device hearing the radio later
and later.

## Connection life

- The engine computes frames only while at least one client listens, a
  scan runs or a recording does. The `rtl_sdr` and `rtl_tcp` sources connect at the first read
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
  number; fine frames while it has not asked for them, and those whose
  centre, width, level or step is not a finite number, or whose width or
  step is not positive. An interface older than the fine spectrum never
  asks for fine frames, and would ignore them as frames of another kind.
  It ignores a source state, demodulation settings, bookmarks, a
  scanner state, a list of signals found, observations, a recording state,
  recordings, a storage state, an update state or a remote access state
  with a missing or malformed field, apart from the recording state's
  folders, which it reads as null, and leaves
  out source settings it does not know and preferences that are not
  numbers, true or false, texts or null.
- An interface whose hello does not list `peaks` among its `scanners`, as
  an older engine's, shows that signal detection needs Radiolack to
  restart, and offers none of its settings; one whose hello does not list
  `memory` does the same for the memory scan, one whose hello does not
  list `history` for the History, asking for no observations, and one whose
  hello does not list `sweep` for the sweep, and one whose hello does not
  list `signals` for the signals scan. An interface reads a scanner state
  without the sweep's fields, as an older engine's, as one without a
  sweep, and one without `listen_s` or `signals` as one without a signals
  scan; it shows signals found and observations without `channel_hz` on
  their `frequency_hz`.
- An interface whose hello does not list `carrier` among its `measures`,
  as an older engine's, disables Calibrate and says that it needs
  Radiolack to restart. While its Calibrate dialog is open, the interface
  asks for a carrier's measure, with a new `id` each time, as soon as the
  previous one came and two seconds after it went at the earliest, and
  again after 10 s without an answer; it asks again at once, and ignores
  the measures of older ids, after a change of the source's settings or
  of the VFO's frequency, mode or filter, so that no measure taken before
  the change computes a correction. Closing the dialog asks for no more.
  When the hello lists `driver_pll` and the receiver has an R820T or R828D
  on its tuner, the dialog asks for the driver's check once per opening,
  at the first carrier measured, and offers no correction until the check
  answers: then it measures again, so that the correction comes from a
  measure labelled with the computation found. A check answered `stopped`
  is asked again at the next carrier measured; any other failure lets the
  calibration go on with the computation set. Closing the dialog during a
  check ignores its answer, but the check runs to its end and saves what
  it found.
- The interface asks for the observations when its History shows, then
  every 30 s while it shows and its page is visible.
- An interface whose hello does not list `audio` in its `recording`, as an
  older engine's, shows that recording needs Radiolack to restart, and
  asks for no recordings; one whose hello does not list `iq` offers no I/Q
  recording, and reads a recording state without `iq` as one without an
  I/Q recording. The interface asks for the recordings when its Record
  menu shows, when a recording that runs makes its file or stops, when
  the folder changes, and every 30 s while the menu shows and its page is
  visible; a recording that runs takes its length and size from the
  recording state.
- An interface asks for the fine frames only when the hello lists `fine`
  among its `spectra`, and asks again after a reconnection, as the engine
  forgets what a stream asked when it closes.
- The interface applies the preferences of its page's first connection
  only: after a reconnection, its own stand. It gives the engine its
  preferences within a second of a change, and when its page hides.
- The interface asks for audio while it can play it, with its volume above
  0. It keeps about 150 ms buffered, follows the engine's clock within 0.5%,
  fills a gap of up to 0.5 s with silence, and starts again, buffering
  first, after a longer gap, a change of audio rate or running dry.
- A hello with another protocol version stops the interface, which asks for a
  reload: the page and the engine come from different versions. As a running
  Radiolack serves the interface it read at start, this happens only to a
  page loaded before Radiolack restarted, which the reload replaces.
- When the stream drops, the interface shows a waiting state and reconnects
  after 250 ms, then after twice as long each time, up to 4 s.
- A remote client whose stream closes with code 4001 stops. If its stream
  had opened, it reloads its page, which is then the pairing page;
  otherwise, as reloading would bring the same page back, it asks for a
  reload. One whose stream closes with code 4002 reconnects in the same way
  as after a drop, saying that remote access is off on the computer, until
  a hello comes. One whose stream closes with code 1012 reconnects as after
  a drop: on an address that remains, it finds remote access again.
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
  running Radiolack serves, which it then read from the disk at each
  request, from the installation of a newer package until Radiolack
  restarts (since 2026-11-14, it serves the interface it read at start). On
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
  beyond the receiver's reach as outside the span. On 2026-11-14, the WFM
  mode, without a new version either: `modes` and `audio_modes` gain WFM,
  with its filter widths and its 32 kHz audio. An older interface ignores
  the settings of a mode it does not know, keeping those it shows, and
  plays the audio at the rate its frames give, as before. The same day, the
  scanner state's `stopped` value `start`, for a scan given at start that
  could not start, which an older interface shows without a reason; and
  remote access that follows the computer's addresses: close code 1012,
  which an older interface takes for a drop, and a remote access state
  that is on with empty `urls` and a `problem`, which an older interface
  shows as on, with an empty address. Later that day, the recordings: the
  hello's `recording`, the recording state, the recordings, the
  `recording` and `recordings` messages and the downloads, which an older
  interface ignores, as it does any message of an unknown type. Later
  still, the I/Q recordings: the hello's `recording` value `iq`, the
  recording state's `iq`, its `stopped` kind `iq` and reasons
  `sample_rate` and `receiver`, the `recording` message's `iq`, the
  recordings' I/Q entries, with `rate_hz` and `meta`, and the downloads of
  SigMF files. An interface that records audio only ignores `iq`, shows an
  I/Q recording's stop as a plain stop of its recording and lists an I/Q
  recording as one of mode `I/Q`, which it downloads as its data file. On
  2026-11-15, the problem report: the `report` messages, which an older
  interface never sends, and so never receives. On 2026-11-16, the hello's
  `version`, which an older interface ignores; the same day, the source
  state's `hf_input`, `hf_inputs`, `direct` and `rtl_agc`, settable in a
  `source` message, which an older interface leaves out: it neither shows
  nor changes them; and `bias_tee`, likewise; and `converter` and
  `converter_lo_hz`, likewise, with the recording state's `stopped` reason
  `converter`, which an older interface shows as a plain stop. An older
  interface behind a converter shows and takes the frequencies on the air,
  as every interface does. Later that day, the calibration: the hello's
  `measures`, which an older interface ignores, and the `carrier`
  messages, which an older interface never sends, and so never receives.
  Later, the driver's check (coverage-and-precision.md, pull request 14b):
  `driver_pll` in the hello's `measures`, the `carrier` message's action
  `identify` and the `driver_pll` answer, which an older interface never
  asks for, and so never receives. It sees another interface's check as
  the centre in use moving for about ten seconds, then `driver_pll`
  changing in the source state.
  Later, the tuner's grid (coverage-and-precision.md, pull request 14a):
  the source state's `centre_in_use_hz`, `driver_pll` and `tuner_grid`,
  the last two settable in a `source` message, which an older interface
  leaves out: it neither shows nor changes them. The frames of an R820T or
  R828D then carry the centre in use, up to 56.25 kHz from the centre
  asked: an older interface shows the span where it is, and a retune it
  asks for waits for frames from the centre asked, which never come, so
  that the VFO stays where it was instead of going to the frequency
  clicked, until the next retune; a reload brings a newer interface.
  On 2026-11-17, the SAM mode (later-milestones.md, section 2, pull
  request 2), without a new version: `modes` and `audio_modes` gain SAM,
  and the demodulation state gains `sam_locked`, which an older interface
  ignores. An older interface lists SAM in no menu and drops every
  demodulation state whose mode it does not know: while the engine is in
  SAM, it keeps showing the VFO, mode, filter width and squelch it showed
  last, though the VFO moves for another interface, and catches up once
  the mode is one it knows. Its own changes, made from what it shows,
  still reach the engine: a tune moves the VFO in SAM, a mode other than
  the one it shows replaces SAM, while the one it shows sends nothing.
  Its audio, its frames and its S-meter are SAM's, as for any interface.
  A bookmark or a recording in SAM shows with its mode's name; tuning to
  such a bookmark there moves the VFO and leaves the engine's mode as it
  is. A reload brings a newer interface.
  Later that day, the automatic notch (later-milestones.md, section 2,
  pull request 3), without a new version: the demodulation state gains
  `notch` and `notch_lines`, and the `demod` message `notch`. An older
  interface ignores both fields and never sends `notch`: it shows no box,
  its changes leave the notch as another interface set it, and it plays
  the notched audio, as every interface does. A reload brings a newer
  interface. A newer interface reads a demodulation state without
  `notch`, an older engine's, as one without the notch, and greys its box.
  Later that day, the noise blanker (later-milestones.md, section 2, pull
  request 4), without a new version: the demodulation state gains
  `blanker` and `blanker_impulses`, and the `demod` message `blanker`. An
  older interface ignores both fields and never sends `blanker`: it shows
  no box, its changes leave the blanker as another interface set it, and
  its audio, S-meter and squelch are those of the blanked channel, as
  every interface's. A reload brings a newer interface. A newer interface
  reads a demodulation state without `blanker`, an older engine's, as one
  without the blanker, and greys its box with a note.
  Later that day, the fine spectrum (later-milestones.md, section 2, pull
  request 5), without a new version: the hello's `spectra`, the `fine`
  message and the fine frames, of kind 3. An older interface ignores
  `spectra` and never sends `fine`, and so never receives a fine frame;
  it would ignore one anyway, as it reads a binary frame by its kind and
  drops the kinds it does not know. A newer interface asks only an engine
  whose hello lists `fine`, so an older engine never receives `fine`.
  Later that day, SoapySDR sources (later-milestones.md, section 1, pull
  request 2), without a new version: the source state's kind `soapy`, in
  `kind` and `kinds`, and its fields `driver` and `sample_rates_hz`, which
  every source sends. An older interface ignores both fields; for a
  `soapy` source its Receiver list, which has no such entry, shows none
  selected and its note stays empty, while the spectrum, the audio, the
  centre frequency, the correction and the other settings that `settable`
  lists work as for any source; its sample rates are its own list's, to
  which it adds the rate in use, and the engine refuses one the device
  does not offer, saying which it offers. A newer interface reads a state
  without either field, an older engine's, as one without a driver whose
  rates are the interface's own.
  Later that day, the Receiver menu's SoapySDR devices (later-milestones.md,
  section 1, pull request 3), without a new version: the devices state and
  the `devices` message. An older interface ignores the devices state, as
  it ignores every message of a type it does not know, and never sends
  `devices`: its Receiver list's entries work as before, and choosing
  `soapy` there opens the device chosen last, or is refused, as before,
  until one was chosen by a newer interface or `--soapy`. A reload brings
  a newer interface. A newer interface shows no devices part for an older
  engine, which sends no devices state, and never sends it `devices`: its
  `soapy` entry then sends `kind` at once, which such an engine opens or
  refuses as before.
  On 2026-11-18, a SoapySDR device's gains, sockets, settings and antenna
  power (later-milestones.md, section 1, pull request 4), without a new
  version: the source state's `gain_stages`, `stage_gains_db`, `antenna`,
  `antennas`, `device_settings`, `device_setting_info` and
  `device_refusal`, with `stage_gains_db`, `antenna` and
  `device_settings` settable in a `source` message; and a `soapy` source's
  `gain_db` for several stages and its `bias_tee`, which were not
  settable before. An older interface
  leaves out the settings it does not know and ignores the fields: its
  Tuner gain slider moves the overall gain of a device with several
  stages, as it does an RTL-SDR's, and its antenna power button works,
  though its second press names an RTL-SDR's 4.5 V; it shows neither the
  stages, the sockets nor the device's settings, which stay as another
  interface or the saved file set them. A reload brings a newer interface.
  A newer interface reads a state without these fields, an older
  engine's, as a device without them, and shows no Advanced section.
  The same day, a `soapy` source's `automatic_gain` (later-milestones.md,
  section 1, after pull request 4), without a new version: an older
  interface ignores it and leaves its Tuner AGC box enabled, the engine
  refusing automatic gain for a device without it, as before; a newer
  interface reads its absence, an older engine's or another source's, as
  automatic gain offered.
  On 2026-11-19, the signals scan, without a new version: the hello's
  `signals` scanner, the scanner state's `listen_s` and `signals`, its
  `scan` and `stopped_scan` value `signals` and its `stopped` value
  `detection`, the `scanner` message's `listen_s` and `scan` value
  `signals`, and the `channel_hz` of the signals found and of the
  observations. An older interface ignores the fields it does not know:
  it shows a signals scan that runs as no scan, though its VFO follows
  it, and starting its memory scan or sweep stops it; it shows the
  signals found and the observations where it did, finding the same
  channels itself. A newer interface offers no signals scan to an older
  engine.
  On 2026-11-20, the detection threshold's range widened from 6-30 dB to
  6-50 dB, without a new version: an older interface's slider stops at
  30 dB and shows a threshold above it at its end, and an older engine
  refuses a threshold above 30 dB with an error, keeping its own.
  The same day, the range scan's engine (range-scan.md, pull request 2),
  without a new version: the scanner state's `sweep_listen`,
  `sweep_passes` and `sweep.passes_set`, the `signals` object while the
  sweep listens, `stopped`'s value `done`, the `scanner` message's
  `sweep_listen` and `sweep_passes`, and the one window of a range within
  the span. An older interface ignores the fields it does not know, starts
  the sweep with the saved settings, shows `done` as a plain stop and a
  listening sweep as a sweep, its VFO following it.
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
