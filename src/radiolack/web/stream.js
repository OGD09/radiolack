/*
 * Engine stream: the WebSocket that brings spectrum frames, audio, the fine
 * spectrum's frames when this interface asks, the source state, the
 * demodulation settings, the bookmarks, the scanner state, the
 * signals the engine found, the saved preferences, the recording state, the
 * storage state, the update state, remote access's state and the SoapySDR
 * devices from the engine, and the observations, the recordings, a problem report, the
 * measure of a carrier and the check of the rtl-sdr driver when this
 * interface asks, and takes this interface's changes, its preferences, its
 * request for audio, its recordings, its update requests, its remote access
 * settings, its report requests, its carrier measure and driver check
 * requests, its request for the fine spectrum and its SoapySDR device
 * requests to it
 * (docs/design/protocol.md). It ignores binary frames of a kind it does not
 * know.
 * It reconnects by itself after a disconnection. On a device that remote
 * access serves, the engine sends neither the storage, update nor remote
 * access state, nor the recordings folder, nor a problem report, and takes
 * none of their changes; it keeps that device's preferences apart from this
 * computer's.
 */

export const PROTOCOL_VERSION = 6;
const FRAME_KIND_SPECTRUM = 1;
const FRAME_VERSION = 2;
const HEADER_BYTES = 40;
const FLAG_SQUELCH_OPEN = 0x1;
const FRAME_KIND_AUDIO = 2;
const AUDIO_VERSION = 1;
const AUDIO_HEADER_BYTES = 16;
const FRAME_KIND_FINE = 3;
const FINE_VERSION = 1;
/** The fine spectrum's cover, on each side of a fine frame's centre, in frame widths. */
export const FINE_COVER = 0.4;
const RETRY_FIRST_MS = 250;
const RETRY_MAX_MS = 4000;
const CLOSE_NOT_PAIRED = 4001;  // a remote device that is not paired, or no longer
const CLOSE_REMOTE_OFF = 4002;  // the computer turned remote access off
const SOURCE_STATUSES = ['connecting', 'streaming', 'waiting', 'idle'];
const SETTINGS = ['address', 'hf_input', 'converter_lo_hz', 'converter', 'centre_hz', 'sample_rate_hz', 'gain_db',
  'stage_gains_db', 'antenna', 'ppm', 'device_settings', 'driver_pll', 'rtl_agc', 'bias_tee', 'dc_removal'];
const SETTING_TYPES = ['bool', 'choice', 'number'];
const HF_INPUTS = ['none', 'q', 'i', 'tuner'];
const DRIVER_PLLS = ['unknown', 'truncating', 'rounding'];
const CONVERTERS = ['none', 'up', 'down', 'down_inverted'];
const COMMANDS = ['source', 'demod', 'audio', 'fine', 'bookmark', 'scanner', 'observations', 'preferences',
  'recording', 'recordings', 'update', 'remote', 'report', 'carrier', 'devices'];
const DEVICE_SEARCHES = ['never', 'searching', 'done', 'failed', 'unavailable'];
const UPDATE_STATUSES = ['idle', 'checking', 'current', 'available', 'installed', 'failed', 'unavailable'];

/**
 * @typedef {object} SpectrumFrame
 * @property {number} sequence          wraps at 2^32
 * @property {number} centreHz
 * @property {number} rateHz            sample rate: the width of the frame
 * @property {number} dbMin             level of code 0, dBFS
 * @property {number} dbStep            level step per code, dB
 * @property {number | null} levelDb    power in the VFO's channel, dBFS; null: nothing received there
 * @property {number | null} squelchDb  squelch threshold in use, dBFS; null: not known yet
 * @property {boolean} squelchOpen
 * @property {Uint8Array} codes         one code per bin, lowest frequency first
 */

/**
 * The fine spectrum of the VFO's channel (docs/design/protocol.md): bin `i`
 * of `n` is centred on `centreHz - widthHz / 2 + i * widthHz / n`, on the
 * air. Its cover, where the engine's front filter passes the signal, is
 * `FINE_COVER * widthHz` on each side of `centreHz`, its last 3 kHz falling
 * by up to 3.1 dB at 48 kHz; the bins beyond it show the filter's stopband,
 * not the air. `centreHz` is the VFO, or in CW the VFO less the CW pitch,
 * 700 Hz.
 * @typedef {object} FineFrame
 * @property {number} sequence          wraps at 2^32, shared by every interface's fine frames
 * @property {number} centreHz          the frequency on the air of bin `n / 2`
 * @property {number} widthHz           the width of the frame on the air: the channel's rate
 * @property {number} dbMin             level of code 0, dBFS
 * @property {number} dbStep            level step per code, dB
 * @property {Uint8Array} codes         one code per bin, lowest frequency first
 */

/**
 * Audio that follows the previous frame unless `position` says otherwise.
 * @typedef {object} AudioFrame
 * @property {number} position  of the first sample, counted at `rateHz`; wraps at 2^32
 * @property {number} rateHz
 * @property {ArrayBuffer} pcm  16-bit signed little-endian mono samples
 */

/**
 * The demodulation settings, shared by every interface.
 * @typedef {object} DemodState
 * @property {number} frequencyHz   the VFO
 * @property {string} mode          one of `modes`
 * @property {number} bandwidthHz   the channel filter's width
 * @property {number} squelchDb     the threshold set by hand, dBFS
 * @property {boolean} squelchAuto  the threshold follows the noise instead
 * @property {string[]} modes       modes the engine takes
 * @property {string[]} audioModes  modes it demodulates to audio; the others are silent
 * @property {boolean | null} samLocked  in SAM, whether it holds its carrier; null in the other
 *   modes, and from an engine older than SAM
 * @property {boolean | null} notch  whether the automatic notch is on, which acts in AM, SAM, USB
 *   and LSB only; null from an engine older than the notch
 * @property {number | null} notchLines  while the notch acts, the lines it removes now, 0 to 4;
 *   null otherwise
 * @property {boolean | null} blanker  whether the noise blanker is on, which acts in every mode;
 *   null from an engine older than the blanker
 * @property {number | null} blankerImpulses  while the blanker is on, the impulses it zeroed in the
 *   latest second; null otherwise
 */

/**
 * Changes to ask of the engine; the others stay as they are.
 * @typedef {object} DemodChanges
 * @property {number} [frequencyHz]
 * @property {string} [mode]
 * @property {number} [bandwidthHz]
 * @property {number} [squelchDb]
 * @property {boolean} [squelchAuto]
 * @property {boolean} [notch]
 * @property {boolean} [blanker]
 */

/**
 * A named frequency with a mode, shared by every interface. Its id lasts as
 * long as the engine runs.
 * @typedef {object} Bookmark
 * @property {number} id
 * @property {number} frequencyHz
 * @property {string} name
 * @property {string} mode
 * @property {boolean} scan     the memory scan stops here
 */

/**
 * Fields of a bookmark to set; the others stay as they are.
 * @typedef {object} BookmarkFields
 * @property {number} [frequencyHz]
 * @property {string} [name]
 * @property {string} [mode]
 * @property {boolean} [scan]
 */

/**
 * The engine's scanners, shared by every interface: signal detection, the
 * memory scan, the sweep and the signals scan.
 * @typedef {object} ScannerState
 * @property {boolean} detection    the engine looks for signals in its spectra
 * @property {number} thresholdDb   how far above the local noise a signal rises
 * @property {number[]} ignoredHz   frequencies whose signals are not listed, lowest first
 * @property {number} resumeS
 *   how long the memory scan and the signals scan wait, once the squelch closed, before they watch again
 * @property {number | null} listenS
 *   how long the signals scan listens to a signal, at most, while another waits; null with an older engine
 * @property {string | null} scan   the scan that runs: "memory", "sweep", "signals", or null
 * @property {string | null} stopped
 *   why the latest scan stopped: "tuned", "source", "bookmarks", "range", "detection" or "start"; null when an
 *   interface stopped it, or none ran
 * @property {string | null} stoppedScan  the scan that `stopped` is about: "memory", "sweep" or "signals", or null
 * @property {MemoryScanState} memory
 * @property {SweepState | null} sweep  null with an engine older than the sweep
 * @property {SignalScanState | null} signals  null with an engine older than the signals scan
 */

/**
 * The memory scan's state.
 * @typedef {object} MemoryScanState
 * @property {number[]} watched     the ticked bookmarks it watches, those within the receiver's reach, by id
 * @property {number | null} windows  how many windows of the receiver they take; null with an older engine
 * @property {number | null} window   the window the receiver is asked for, from 0; null while it does not run
 * @property {string | null} state  "watching", "checking", "listening" or "waiting"; null while it does not run
 * @property {number | null} bookmarkId  the bookmark tuned to, null while watching
 */

/**
 * The sweep's settings and progress.
 * @typedef {object} SweepState
 * @property {number} lowHz        the range it sweeps, from this frequency
 * @property {number} highHz       to this one
 * @property {number} windowS      the time per window
 * @property {number | null} windows  how many windows the range takes; null while it does not run
 * @property {number | null} window   the window the receiver is asked for, from 0
 * @property {number | null} passes   the passes done
 */

/**
 * The signals scan's progress.
 * @typedef {object} SignalScanState
 * @property {string | null} state  "watching", "checking", "listening" or "waiting"; null while it does not run
 * @property {number | null} frequencyHz  the channel tuned to, as the signals show on it; null while watching
 */

/**
 * Scanner settings to change; the others stay as they are.
 * @typedef {object} ScannerChanges
 * @property {boolean} [detection]
 * @property {number} [thresholdDb]
 * @property {number} [resumeS]
 * @property {number} [listenS]
 * @property {number} [sweepLowHz]
 * @property {number} [sweepHighHz]
 * @property {number} [sweepWindowS]
 */

/**
 * A signal the engine found while it runs.
 * @typedef {object} Peak
 * @property {number} frequencyHz   its centre of power
 * @property {number} channelHz
 *   the frequency it shows on: its band plan channel, or its frequency to the 100 Hz; with an older engine, its frequency
 * @property {number} widthHz
 * @property {number} marginDb      its highest bin above the local noise, when last found
 * @property {boolean} active       found within the last 1.5 s
 * @property {number} ageS          seconds since it was last found, when the engine sent the list
 */

/**
 * A channel where the engine found signals, which it keeps across restarts.
 * Times are seconds since 1970, UTC.
 * @typedef {object} Observation
 * @property {number} frequencyHz
 * @property {number} channelHz      the frequency it shows on, as a signal's (Peak)
 * @property {number} firstS          when first found
 * @property {number} lastS           when last found
 * @property {number} transmissions
 * @property {number} activeS         how long it was active, its transmissions together
 * @property {number} marginDb        its highest margin above the local noise
 * @property {number[]} hours
 *   how many of its transmissions began in each hour of the day, from 0 h, on the engine's computer
 */

/**
 * An interface's own settings, by name, which the engine keeps for the next
 * interfaces: numbers, true or false, short texts or null.
 * @typedef {Record<string, number | boolean | string | null>} Preferences
 */

/**
 * An audio recording that runs.
 * @typedef {object} RunningRecording
 * @property {string | null} file       its file, once made: the first audio makes it
 * @property {number} startedS          when it started, in Unix time
 * @property {number} frequencyHz       the VFO's frequency when it started
 * @property {string} mode              the mode when it started
 * @property {number | null} rateHz     the audio rate, once the first audio came
 * @property {number} seconds           its length so far, to the tenth
 * @property {number} bytes             its file's size so far
 */

/**
 * An I/Q recording that runs.
 * @typedef {object} RunningIqRecording
 * @property {string | null} file       its data file, once made: the first samples make it
 * @property {number} startedS          when it started, in Unix time
 * @property {number} frequencyHz       the receiver's centre frequency when it started
 * @property {number} rateHz            the receiver's sample rate, which stays the same
 * @property {number} seconds           its length so far, to the tenth
 * @property {number} bytes             its data's size so far
 */

/**
 * Why the latest recording stopped by itself.
 * @typedef {object} RecordingStop
 * @property {string} kind      "audio" or "iq"
 * @property {string} reason    "rate", "source", "sample_rate", "receiver", "converter", "space", "full" or "disk"
 * @property {string} detail    what went wrong with the disk, or ""
 */

/**
 * The engine's recordings, shared by every interface.
 * @typedef {object} RecordingState
 * @property {RunningRecording | null} audio  the audio recording that runs, if one does
 * @property {RunningIqRecording | null} iq   the I/Q recording that runs, if one does
 * @property {RecordingStop | null} stopped
 *   why the latest recording stopped by itself, until one of its kind starts; null when an interface stopped
 *   it, or none ran
 * @property {string | null} folder
 *   where the recordings go, "~" standing for the home folder; null on a device that remote access serves,
 *   or when the engine does not record
 * @property {string | null} defaultFolder  where they go unless another folder was chosen; null as `folder`
 */

/**
 * A recording in the recordings folder.
 * @typedef {object} RecordingFile
 * @property {string} name            its file, which Download and Delete take: an I/Q recording's data file
 * @property {string} kind            "audio" or "iq"
 * @property {number} startedS        when it started, in Unix time, as its name tells
 * @property {number} frequencyHz     the VFO's frequency when it started, or the receiver's centre for I/Q
 * @property {string} mode            the mode when it started, or "I/Q"
 * @property {number | null} rateHz   an I/Q recording's sample rate; null for audio
 * @property {string | null} meta     an I/Q recording's metadata file; null for audio, or when it is missing
 * @property {number | null} bytes    its size, with an I/Q recording's metadata; null when it cannot be read
 * @property {number | null} seconds  its length, to the tenth; null when it cannot be read
 * @property {boolean} running        a recording that runs, which is neither downloaded nor deleted
 */

/**
 * The recordings folder's recordings, which the engine sends to the
 * interface that asks.
 * @typedef {object} RecordingList
 * @property {RecordingFile[]} files    the latest, the newest first
 * @property {number} more              how many older ones the folder holds besides
 * @property {number | null} freeBytes  the space free on the folder's disk; null when unknown
 * @property {string} problem           why the folder could not be read, or ""
 */

/**
 * Where the engine saves the settings and bookmarks.
 * @typedef {object} StorageState
 * @property {string | null} directory  the settings folder; null: nothing is saved
 * @property {boolean} saving           changes reach the disk
 * @property {string} problem           what went wrong with the files, or ""
 */

/**
 * What the engine's update check found.
 * @typedef {'idle' | 'checking' | 'current' | 'available' | 'installed' | 'failed' | 'unavailable'} UpdateStatus
 */

/**
 * @typedef {object} UpdateState
 * @property {UpdateStatus} status
 * @property {string} version         the Radiolack that runs
 * @property {string | null} latest   the newer version: in the repository (available) or installed (installed)
 * @property {string} detail          why the check failed or is unavailable, or ""
 * @property {boolean} checkAtStart   the engine checks when it starts
 */

/**
 * A problem report, which the engine sends to the interface that asks, for
 * its user to share or not.
 * @typedef {object} ProblemReport
 * @property {string} text      the report, in plain text
 * @property {string} issueUrl  GitHub's page for a new issue, with the report as its description
 */

/**
 * The measure of the carrier at the VFO, which the engine sends to the
 * interface that asks: its frequency on the air, to the tenth of a hertz,
 * or why there is none.
 * @typedef {object} CarrierMeasure
 * @property {number | null} id           the request's id, which this interface gave
 * @property {number | null} frequencyHz  null: no frequency, see `reason`
 * @property {number | null} marginDb     how far the line stands above the median of the filter's bins;
 *                                        null when nothing was measured, or no line stands out
 * @property {string | null} reason       null with a frequency; "silent", "no_signal", "several",
 *                                        "unstable", or one this interface does not know
 */

/**
 * What a check of the rtl-sdr driver found, which the engine sends to the
 * interface that asks: which of the driver's computations an R820T's or
 * R828D's PLL follows, or why it names none.
 * @typedef {object} DriverCheck
 * @property {number | null} id         the request's id, which this interface gave
 * @property {string | null} driverPll  "truncating" or "rounding", now set; null: none found, see `reason`
 * @property {string | null} previous   the computation set when the check began; null for a source without one
 * @property {string | null} reason     null with a computation; why none otherwise, such as "lost" or "scan",
 *                                      or one this interface does not know
 */

/**
 * A device paired with remote access.
 * @typedef {object} RemoteDevice
 * @property {string} id            names it in forget requests
 * @property {string} label         such as "iPhone · Safari"
 * @property {number} pairedS       when it was paired, in Unix time
 * @property {number} seenS         when it was last used, in Unix time, to the hour
 * @property {boolean} connected    it has a stream open
 */

/**
 * Remote access: other devices on the local network reach Radiolack over
 * HTTPS with an access code. This computer's interfaces only.
 * @typedef {object} RemoteState
 * @property {boolean} on
 * @property {number} port              where it listens, or will
 * @property {string[]} urls            what a device opens, by name then by address; empty while off
 * @property {string | null} code       the access code, while on
 * @property {boolean} blocked          too many wrong codes: the code works again once renewed
 * @property {RemoteDevice[]} devices   paired, in the order they were
 * @property {string | null} fingerprint  SHA-256 of the certificate devices install; null before the first turn-on
 * @property {string} problem           why it could not turn on, or ""
 */

/**
 * A SoapySDR device that the engine lists: one its search found, or the
 * device in use.
 * @typedef {object} SoapyDevice
 * @property {string} driver    such as "hackrf"
 * @property {string} serial    its serial number; "" when its module gives none
 * @property {string} label     as its module names it
 * @property {boolean} inUse    the source in use reads it
 */

/**
 * The engine's search for SoapySDR devices: "never" (none yet), "searching",
 * "done", "failed" (a driver's search failed: see `detail`) or
 * "unavailable" (the SoapySDR module is missing: see `detail`).
 * @typedef {'never' | 'searching' | 'done' | 'failed' | 'unavailable'} DeviceSearchStatus
 */

/**
 * The SoapySDR devices: what the engine's search found, and the device in
 * use, first.
 * @typedef {object} DevicesState
 * @property {DeviceSearchStatus} search
 * @property {string} detail            why a search failed or cannot run, or ""
 * @property {string[]} drivers         the drivers the engine searches, one at a time
 * @property {SoapyDevice[]} devices
 * @property {boolean} chosen           a device was chosen before: the Receiver list's "soapy" opens it again
 * @property {string | null} args       the device arguments in use; null without a SoapySDR device in use,
 *                                      and on a device that remote access serves
 */

/**
 * @typedef {'source' | 'demod' | 'audio' | 'fine' | 'bookmark' | 'scanner' | 'observations' | 'preferences'
 *   | 'recording' | 'recordings' | 'update' | 'remote' | 'report' | 'carrier' | 'devices'} Command
 */

/**
 * "streaming" while samples arrive; "connecting", "waiting" (to retry, see
 * `detail`) and "idle" (no interface listened) while they do not.
 * @typedef {'connecting' | 'streaming' | 'waiting' | 'idle'} SourceStatus
 */

/**
 * @typedef {'address' | 'hf_input' | 'converter_lo_hz' | 'converter' | 'centre_hz' | 'sample_rate_hz' | 'gain_db'
 *   | 'stage_gains_db' | 'antenna' | 'ppm' | 'device_settings' | 'driver_pll' | 'rtl_agc' | 'bias_tee'
 *   | 'dc_removal'} Setting
 */

/**
 * One of a SoapySDR device's several gain stages, under the device's name.
 * @typedef {object} GainStage
 * @property {string} name
 * @property {number[]} gainsDb   the gains it offers, lowest first
 */

/**
 * A SoapySDR device's value of one of its settings.
 * @typedef {boolean | number | string} DeviceValue
 */

/**
 * One of a SoapySDR device's settings, as the device describes it: a box,
 * a choice among options, or a number within a range. Its texts are the
 * device's, shown as text.
 * @typedef {object} DeviceSetting
 * @property {string} key
 * @property {string} name
 * @property {string} description
 * @property {'bool' | 'choice' | 'number'} type
 * @property {{ value: string, name: string }[]} options    a choice's
 * @property {[number, number, number] | null} range         a number's lowest, highest and step (0: any)
 * @property {boolean} integer                               a number in whole units
 */

/**
 * The engine's source: what it is, whether samples arrive, and the settings
 * asked of the receiver, with the engine's own DC removal. Frames carry the
 * setting they were taken with.
 * @typedef {object} SourceState
 * @property {string} kind              such as "synthetic", "rtl_sdr", "rtl_tcp" or "soapy"
 * @property {SourceStatus} status
 * @property {string} detail            why the source waits, or ""
 * @property {string | null} address    of a network source, as host:port
 * @property {string | null} tuner      name of the receiver's tuner, or a SoapySDR device's hardware, once known
 * @property {string | null} driver     a SoapySDR device's driver, such as "sdrplay"; null: another source
 * @property {number} centreHz         the centre frequency asked
 * @property {number} centreInUseHz    where the receiver sits on the air for it, which labels the frames: an
 *                                      R820T's or R828D's tuner is placed on its grid, up to 56.25 kHz away
 * @property {number} rateHz
 * @property {number[]} sampleRatesHz   the sample rates the Receiver menu offers, lowest first; empty: its own
 * @property {number | null} gainDb     null: automatic gain, or no gain setting
 * @property {boolean} automaticGain    the receiver has automatic gain: false for a SoapySDR device without
 *                                      it, true otherwise, and for an engine that does not tell
 * @property {number | null} ppm        frequency correction, parts per million to the hundredth; null: none
 * @property {boolean} dcRemoval        the engine takes a constant offset off the samples
 * @property {number[]} gainsDb         manual gains the tuner offers, lowest first; a SoapySDR device's overall gains
 * @property {GainStage[]} gainStages   a SoapySDR device's several gain stages; empty: one or none
 * @property {Record<string, number>} stageGainsDb  each stage's gain, by name
 * @property {string | null} antenna    a SoapySDR device's antenna socket in use
 * @property {string[]} antennas        its sockets, when it has several; empty otherwise
 * @property {Record<string, DeviceValue>} deviceSettings  a SoapySDR device's settings' values, by key
 * @property {DeviceSetting[]} deviceSettingInfo           those settings, as the device describes them
 * @property {string | null} deviceRefusal what the device refused last, until the next change
 * @property {[number, number]} rangeHz centre frequencies the source takes
 * @property {string | null} hfInput    what an RTL-SDR uses for shortwave: "none", "q", "i" or "tuner";
 *                                      null: a source without it
 * @property {string[]} hfInputs        the values of hfInput the receiver offers
 * @property {boolean} direct           the receiver samples directly, without its tuner
 * @property {boolean | null} rtlAgc    the RTL2832's AGC while sampling directly; null: a source without it
 * @property {boolean | null} biasTee   antenna power, 4.5 V on the antenna socket; null: a source without it
 * @property {string | null} converter  in front of the receiver: "none", "up", "down" or "down_inverted";
 *                                      null: a source without it, or one this interface does not know
 * @property {number | null} converterLoHz the converter's oscillator, whole Hz; null: a source without it
 * @property {string | null} driverPll  how the receiver's driver computes an R820T's or R828D's PLL:
 *                                      "unknown", "truncating" or "rounding"; null: a source without it
 * @property {Setting[]} settable       settings an interface may change
 * @property {string[]} kinds           kinds of source the engine may switch to; empty: it reads one only
 */

/**
 * Changes to ask of the engine; the others stay as they are.
 * @typedef {object} SourceChanges
 * @property {string} [kind]            another kind of source, among SourceState.kinds, asked alone
 * @property {string} [address]         of an rtl_tcp server: HOST, PORT or HOST:PORT
 * @property {number} [centreHz]
 * @property {number} [rateHz]
 * @property {number | null} [gainDb]   null: automatic gain
 * @property {Record<string, number>} [stageGainsDb]   some stages' gains, by name
 * @property {string} [antenna]         one of SourceState.antennas
 * @property {Record<string, DeviceValue>} [deviceSettings]  some of the device's settings, by key
 * @property {number} [ppm]             from -100 to 100, to the hundredth
 * @property {string} [hfInput]         one of SourceState.hfInputs
 * @property {boolean} [rtlAgc]
 * @property {boolean} [biasTee]
 * @property {string} [converter]       one of "none", "up", "down" and "down_inverted"
 * @property {number} [converterLoHz]   whole Hz, above 0 and at most 100 GHz
 * @property {string} [driverPll]       one of "unknown", "truncating" and "rounding"
 * @property {boolean} [dcRemoval]
 */

/**
 * "connecting" before the first answer, "open" once the engine greeted the
 * interface, "waiting" while it retries after a disconnection,
 * "remote-off" while it retries after the computer turned remote access
 * off, "unpaired" once the computer no longer knows this device, which must
 * pair again (the stream then stops), and "incompatible" when the engine
 * speaks another protocol version.
 * @typedef {'connecting' | 'open' | 'waiting' | 'remote-off' | 'unpaired' | 'incompatible'} StreamState
 */

/**
 * @typedef {object} StreamHandlers
 * @property {(frame: SpectrumFrame) => void} onFrame
 * @property {(frame: AudioFrame) => void} onAudio     while this interface asks for audio
 * @property {(frame: FineFrame) => void} [onFine]
 *   about ten a second while this interface asks for the fine spectrum
 * @property {(source: SourceState) => void} onSource  sent first, then at every change
 * @property {(demod: DemodState) => void} onDemod     sent next, then at every change this interface has not overtaken
 * @property {(bookmarks: Bookmark[]) => void} onBookmarks  sent next, then at every change, in frequency order
 * @property {(scanner: ScannerState) => void} onScanner    sent next, then at every change
 * @property {(peaks: Peak[]) => void} onPeaks
 *   sent next, then when the list changes, at most about once a second: active signals first, strongest first
 * @property {(observations: Observation[]) => void} onObservations
 *   sent when this interface asks for them, or cleared some: the latest found first
 * @property {(values: Preferences) => void} onPreferences
 *   sent next, once per connection: the latest saved for this computer's interfaces, or for this device
 * @property {(recording: RecordingState) => void} onRecording
 *   sent next, then at every change, and every second while a recording runs
 * @property {(recordings: RecordingList) => void} onRecordings
 *   sent when this interface asks for them, or deleted one
 * @property {(storage: StorageState) => void} onStorage    sent next, then at every change
 * @property {(update: UpdateState) => void} onUpdate       sent next, then at every change
 * @property {(remote: RemoteState) => void} onRemote       sent next, then at every change
 * @property {(devices: DevicesState) => void} [onDevices]
 *   with sources that open SoapySDR devices: sent after the recording state, then at every change
 * @property {(report: ProblemReport) => void} onReport     sent when this interface asks for one
 * @property {(measure: CarrierMeasure) => void} onCarrier  sent when this interface asks for one
 * @property {(check: DriverCheck) => void} onDriverCheck   sent when this interface asks for one
 * @property {(message: string, command: Command | null) => void} onError
 *   why the engine refused a command from this interface, and which kind of command
 * @property {(state: StreamState) => void} onState
 */

/** @param {number} value */
const dbOrNull = (value) => (Number.isFinite(value) ? value : null);

/**
 * Decode a binary message. Returns null for anything but a well-formed
 * spectrum frame of a version this interface reads.
 * @param {ArrayBuffer} buffer
 * @returns {SpectrumFrame | null}
 */
export function decodeFrame(buffer) {
  if (buffer.byteLength <= HEADER_BYTES) return null;
  const header = new DataView(buffer, 0, HEADER_BYTES);
  if (header.getUint8(0) !== FRAME_KIND_SPECTRUM || header.getUint8(1) !== FRAME_VERSION) return null;
  const frame = {
    sequence: header.getUint32(4, true),
    centreHz: header.getFloat64(8, true),
    rateHz: header.getFloat64(16, true),
    dbMin: header.getFloat32(24, true),
    dbStep: header.getFloat32(28, true),
    levelDb: dbOrNull(header.getFloat32(32, true)),
    squelchDb: dbOrNull(header.getFloat32(36, true)),
    squelchOpen: (header.getUint16(2, true) & FLAG_SQUELCH_OPEN) !== 0,
    codes: new Uint8Array(buffer, HEADER_BYTES),
  };
  const valid = Number.isFinite(frame.centreHz) && Number.isFinite(frame.rateHz) && frame.rateHz > 0
    && Number.isFinite(frame.dbMin) && Number.isFinite(frame.dbStep) && frame.dbStep > 0;
  return valid ? frame : null;
}

/**
 * Decode a binary message. Returns null for anything but a well-formed fine
 * frame of a version this interface reads.
 * @param {ArrayBuffer} buffer
 * @returns {FineFrame | null}
 */
export function decodeFine(buffer) {
  if (buffer.byteLength <= HEADER_BYTES) return null;
  const header = new DataView(buffer, 0, HEADER_BYTES);
  if (header.getUint8(0) !== FRAME_KIND_FINE || header.getUint8(1) !== FINE_VERSION) return null;
  const frame = {
    sequence: header.getUint32(4, true),
    centreHz: header.getFloat64(8, true),
    widthHz: header.getFloat64(16, true),
    dbMin: header.getFloat32(24, true),
    dbStep: header.getFloat32(28, true),
    codes: new Uint8Array(buffer, HEADER_BYTES),
  };
  const valid = Number.isFinite(frame.centreHz) && Number.isFinite(frame.widthHz) && frame.widthHz > 0
    && Number.isFinite(frame.dbMin) && Number.isFinite(frame.dbStep) && frame.dbStep > 0;
  return valid ? frame : null;
}

/**
 * Decode a binary message. Returns null for anything but a well-formed
 * audio frame of a version this interface reads.
 * @param {ArrayBuffer} buffer
 * @returns {AudioFrame | null}
 */
export function decodeAudio(buffer) {
  if (buffer.byteLength < AUDIO_HEADER_BYTES || (buffer.byteLength - AUDIO_HEADER_BYTES) % 2) return null;
  const header = new DataView(buffer, 0, AUDIO_HEADER_BYTES);
  if (header.getUint8(0) !== FRAME_KIND_AUDIO || header.getUint8(1) !== AUDIO_VERSION) return null;
  const rateHz = header.getFloat64(8, true);
  if (!(Number.isFinite(rateHz) && rateHz > 0)) return null;
  return { position: header.getUint32(4, true), rateHz, pcm: buffer.slice(AUDIO_HEADER_BYTES) };
}

/**
 * Decode a demodulation state message. Returns null when a field is missing
 * or malformed.
 * @param {any} message  a parsed JSON message of type "demod"
 * @returns {(DemodState & { acknowledged: number }) | null}
 */
export function decodeDemod(message) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const names = (value) => Array.isArray(value) && value.every((name) => typeof name === 'string');
  const m = message;
  const valid = finite(m.frequency_hz) && names(m.modes) && m.modes.includes(m.mode)
    && finite(m.bandwidth_hz) && m.bandwidth_hz > 0 && finite(m.squelch_db) && typeof m.squelch_auto === 'boolean'
    && names(m.audio_modes) && Number.isSafeInteger(m.acknowledged) && m.acknowledged >= 0;
  if (!valid) return null;
  return {
    frequencyHz: m.frequency_hz,
    mode: m.mode,
    bandwidthHz: m.bandwidth_hz,
    squelchDb: m.squelch_db,
    squelchAuto: m.squelch_auto,
    modes: m.modes.slice(),
    audioModes: m.audio_modes.slice(),
    samLocked: typeof m.sam_locked === 'boolean' ? m.sam_locked : null,
    notch: typeof m.notch === 'boolean' ? m.notch : null,
    notchLines: Number.isSafeInteger(m.notch_lines) && m.notch_lines >= 0 ? m.notch_lines : null,
    blanker: typeof m.blanker === 'boolean' ? m.blanker : null,
    blankerImpulses: Number.isSafeInteger(m.blanker_impulses) && m.blanker_impulses >= 0 ? m.blanker_impulses : null,
    acknowledged: m.acknowledged,
  };
}

/**
 * A SoapySDR device's gain stages, antenna sockets and settings from a
 * source state: what is malformed, or missing in an older engine's state,
 * reads as nothing, the rest of the state unaffected.
 * @param {any} m  a parsed JSON message of type "source"
 */
export function decodeDeviceParts(m) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value @returns {value is string} */
  const text = (value) => typeof value === 'string';
  /** @param {unknown} value */
  const object = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);
  /** @type {GainStage[]} */
  const gainStages = Array.isArray(m.gain_stages) ? m.gain_stages
    .filter((/** @type {any} */ s) => object(s) && text(s.name) && Array.isArray(s.gains_db) && s.gains_db.length > 0
      && s.gains_db.every(finite))
    .map((/** @type {any} */ s) => ({ name: s.name, gainsDb: s.gains_db.slice() })) : [];
  /** @type {Record<string, number>} */
  const stageGainsDb = {};
  if (object(m.stage_gains_db)) {
    for (const [name, gain] of Object.entries(m.stage_gains_db)) if (finite(gain)) stageGainsDb[name] = /** @type {number} */ (gain);
  }
  const antennas = Array.isArray(m.antennas) && m.antennas.every(text) ? m.antennas.slice() : [];
  /** @type {DeviceSetting[]} */
  const deviceSettingInfo = Array.isArray(m.device_setting_info) ? m.device_setting_info
    .filter((/** @type {any} */ s) => object(s) && text(s.key) && text(s.name) && text(s.description)
      && SETTING_TYPES.includes(s.type) && Array.isArray(s.options)
      && s.options.every((/** @type {any} */ o) => object(o) && text(o.value) && text(o.name))
      && (s.range === null || (Array.isArray(s.range) && s.range.length === 3 && s.range.every(finite)))
      && (s.type !== 'choice' || s.options.length > 0) && (s.type !== 'number' || s.range !== null))
    .map((/** @type {any} */ s) => ({
      key: s.key, name: s.name, description: s.description, type: s.type,
      options: s.options.map((/** @type {any} */ o) => ({ value: o.value, name: o.name })),
      range: s.range === null ? null : [s.range[0], s.range[1], s.range[2]], integer: s.integer === true,
    })) : [];
  /** @type {Record<string, DeviceValue>} */
  const deviceSettings = {};
  if (object(m.device_settings)) {
    for (const [key, value] of Object.entries(m.device_settings)) {
      if (typeof value === 'boolean' || finite(value) || text(value)) deviceSettings[key] = /** @type {DeviceValue} */ (value);
    }
  }
  return {
    gainStages,
    stageGainsDb,
    antenna: text(m.antenna) ? m.antenna : null,
    antennas,
    deviceSettings,
    deviceSettingInfo,
    deviceRefusal: text(m.device_refusal) && m.device_refusal ? m.device_refusal : null,
  };
}

/**
 * Decode a source state message. Returns null when a field is missing or
 * malformed; settings this interface does not know are left out. The
 * shortwave, antenna power, converter, driver and sample rates fields,
 * which an engine older than them does not send, read as a source without
 * them; so does a converter that this interface does not know. Automatic
 * gain, unknown (null) or not sent, reads as offered. A SoapySDR
 * device's stages, sockets and settings read as `decodeDeviceParts` reads
 * them.
 * @param {any} message  a parsed JSON message of type "source"
 * @returns {SourceState | null}
 */
export function decodeSource(message) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const textOrNull = (value) => value === null || typeof value === 'string';
  const m = message;
  const valid = typeof m.kind === 'string' && SOURCE_STATUSES.includes(m.status) && typeof m.detail === 'string'
    && textOrNull(m.address) && textOrNull(m.tuner)
    && finite(m.centre_hz) && finite(m.sample_rate_hz) && m.sample_rate_hz > 0
    && (m.gain_db === null || finite(m.gain_db))
    && (m.automatic_gain === undefined || m.automatic_gain === null || typeof m.automatic_gain === 'boolean')
    && (m.ppm === null || finite(m.ppm)) && typeof m.dc_removal === 'boolean'
    && Array.isArray(m.gains_db) && m.gains_db.every(finite)
    && Array.isArray(m.range_hz) && m.range_hz.length === 2 && m.range_hz.every(finite)
    && m.range_hz[0] <= m.range_hz[1]
    && Array.isArray(m.settable) && m.settable.every((/** @type {unknown} */ name) => typeof name === 'string')
    && (m.hf_input === undefined || m.hf_input === null || HF_INPUTS.includes(m.hf_input))
    && (m.hf_inputs === undefined || (Array.isArray(m.hf_inputs)
      && m.hf_inputs.every((/** @type {unknown} */ input) => typeof input === 'string')))
    && (m.direct === undefined || typeof m.direct === 'boolean')
    && (m.rtl_agc === undefined || m.rtl_agc === null || typeof m.rtl_agc === 'boolean')
    && (m.bias_tee === undefined || m.bias_tee === null || typeof m.bias_tee === 'boolean')
    && (m.converter === undefined || m.converter === null || typeof m.converter === 'string')
    && (m.converter_lo_hz === undefined || m.converter_lo_hz === null || (finite(m.converter_lo_hz) && m.converter_lo_hz > 0))
    && (m.centre_in_use_hz === undefined || finite(m.centre_in_use_hz))
    && (m.driver_pll === undefined || m.driver_pll === null || typeof m.driver_pll === 'string')
    && (m.driver === undefined || textOrNull(m.driver))
    && (m.sample_rates_hz === undefined || (Array.isArray(m.sample_rates_hz)
      && m.sample_rates_hz.every((/** @type {unknown} */ rate) => finite(rate) && /** @type {number} */ (rate) > 0)));
  if (!valid) return null;
  return {
    kind: m.kind,
    status: m.status,
    detail: m.detail,
    address: m.address,
    tuner: m.tuner,
    driver: m.driver ?? null,
    centreHz: m.centre_hz,
    centreInUseHz: m.centre_in_use_hz ?? m.centre_hz,
    rateHz: m.sample_rate_hz,
    sampleRatesHz: Array.isArray(m.sample_rates_hz) ? m.sample_rates_hz.slice() : [],
    gainDb: m.gain_db,
    automaticGain: m.automatic_gain !== false,
    ppm: m.ppm,
    dcRemoval: m.dc_removal,
    gainsDb: m.gains_db.slice(),
    ...decodeDeviceParts(m),
    rangeHz: [m.range_hz[0], m.range_hz[1]],
    hfInput: m.hf_input ?? null,
    hfInputs: Array.isArray(m.hf_inputs) ? m.hf_inputs.filter((/** @type {string} */ input) => HF_INPUTS.includes(input)) : [],
    direct: m.direct === true,
    rtlAgc: m.rtl_agc ?? null,
    biasTee: m.bias_tee ?? null,
    converter: CONVERTERS.includes(m.converter) ? m.converter : null,
    converterLoHz: m.converter_lo_hz ?? null,
    driverPll: DRIVER_PLLS.includes(m.driver_pll) ? m.driver_pll : null,
    settable: m.settable.filter((/** @type {string} */ name) => SETTINGS.includes(name)),
    kinds: Array.isArray(m.kinds) ? m.kinds.filter((/** @type {unknown} */ kind) => typeof kind === 'string') : [],
  };
}

/**
 * Decode a bookmarks message. Returns null when an entry is missing a field
 * or holds a malformed one.
 * @param {any} message  a parsed JSON message of type "bookmarks"
 * @returns {Bookmark[] | null}
 */
export function decodeBookmarks(message) {
  if (!Array.isArray(message.bookmarks)) return null;
  /** @type {Bookmark[]} */
  const bookmarks = [];
  for (const b of message.bookmarks) {
    const valid = b !== null && typeof b === 'object' && Number.isSafeInteger(b.id) && b.id > 0
      && typeof b.frequency_hz === 'number' && Number.isFinite(b.frequency_hz)
      && typeof b.name === 'string' && typeof b.mode === 'string' && typeof b.scan === 'boolean';
    if (!valid) return null;
    bookmarks.push({ id: b.id, frequencyHz: b.frequency_hz, name: b.name, mode: b.mode, scan: b.scan });
  }
  return bookmarks;
}

/**
 * Decode a scanner state message. Returns null when a field is missing or
 * malformed, apart from the sweep's, which an engine older than the sweep
 * does not send: without them, or with one malformed, the sweep is null;
 * the memory scan's windows, which an engine older than them does not
 * send: missing or malformed, they are null; and the signals scan's, which
 * an engine older than it does not send: the longest listening time and
 * the signals scan's progress are null, missing or malformed.
 * @param {any} message  a parsed JSON message of type "scanner"
 * @returns {ScannerState | null}
 */
export function decodeScanner(message) {
  const m = message;
  const memory = m.memory;
  const valid = typeof m.detection === 'boolean' && typeof m.threshold_db === 'number'
    && Number.isFinite(m.threshold_db) && Array.isArray(m.ignored_hz)
    && m.ignored_hz.every((/** @type {unknown} */ f) => typeof f === 'number' && Number.isFinite(f))
    && typeof m.resume_s === 'number' && Number.isFinite(m.resume_s)
    && (m.scan === null || typeof m.scan === 'string') && (m.stopped === null || typeof m.stopped === 'string')
    && memory !== null && typeof memory === 'object' && Array.isArray(memory.watched)
    && memory.watched.every((/** @type {unknown} */ id) => Number.isSafeInteger(id))
    && (memory.state === null || typeof memory.state === 'string')
    && (memory.bookmark_id === null || Number.isSafeInteger(memory.bookmark_id));
  if (!valid) return null;
  return {
    detection: m.detection,
    thresholdDb: m.threshold_db,
    ignoredHz: m.ignored_hz.slice(),
    resumeS: m.resume_s,
    listenS: typeof m.listen_s === 'number' && Number.isFinite(m.listen_s) && m.listen_s > 0 ? m.listen_s : null,
    scan: m.scan,
    stopped: m.stopped,
    stoppedScan: typeof m.stopped_scan === 'string' ? m.stopped_scan : null,
    memory: {
      watched: memory.watched.slice(),
      windows: countOrNull(memory.windows),
      window: countOrNull(memory.window),
      state: memory.state,
      bookmarkId: memory.bookmark_id,
    },
    sweep: decodeSweep(m),
    signals: decodeSignalScan(m.signals),
  };
}

/**
 * The signals scan's progress in a scanner state message, or null when it
 * is missing or malformed.
 * @param {any} progress  the message's `signals`
 * @returns {SignalScanState | null}
 */
function decodeSignalScan(progress) {
  const valid = progress !== null && typeof progress === 'object'
    && (progress.state === null || typeof progress.state === 'string')
    && (progress.frequency_hz === null || (typeof progress.frequency_hz === 'number' && Number.isFinite(progress.frequency_hz)));
  return valid ? { state: progress.state, frequencyHz: progress.frequency_hz } : null;
}

/**
 * A count in a message: a whole number from 0, or null when it is missing or
 * malformed.
 * @param {unknown} n
 * @returns {number | null}
 */
function countOrNull(n) {
  return typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 ? n : null;
}

/**
 * The sweep's settings and progress in a scanner state message, or null when
 * one is missing or malformed.
 * @param {any} m  a parsed JSON message of type "scanner"
 * @returns {SweepState | null}
 */
function decodeSweep(m) {
  const progress = m.sweep;
  if (progress === null || typeof progress !== 'object') return null;
  const counts = [progress.windows, progress.window, progress.passes];
  const valid = [m.sweep_low_hz, m.sweep_high_hz, m.sweep_window_s].every(
    (/** @type {unknown} */ v) => typeof v === 'number' && Number.isFinite(v) && v > 0)
    && (counts.every((n) => n === null) || counts.every((n) => Number.isSafeInteger(n) && n >= 0));
  if (!valid) return null;
  return {
    lowHz: m.sweep_low_hz,
    highHz: m.sweep_high_hz,
    windowS: m.sweep_window_s,
    windows: progress.windows,
    window: progress.window,
    passes: progress.passes,
  };
}

/**
 * Decode a peaks message. Returns null when an entry is missing a field or
 * holds a malformed one.
 * @param {any} message  a parsed JSON message of type "peaks"
 * @returns {Peak[] | null}
 */
export function decodePeaks(message) {
  if (!Array.isArray(message.peaks)) return null;
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @type {Peak[]} */
  const peaks = [];
  for (const p of message.peaks) {
    const valid = p !== null && typeof p === 'object' && finite(p.frequency_hz) && finite(p.width_hz)
      && finite(p.margin_db) && typeof p.active === 'boolean' && finite(p.age_s);
    if (!valid) return null;
    peaks.push({ frequencyHz: p.frequency_hz, channelHz: finite(p.channel_hz) ? p.channel_hz : p.frequency_hz,
      widthHz: p.width_hz, marginDb: p.margin_db, active: p.active, ageS: p.age_s });
  }
  return peaks;
}

/**
 * Decode an observations message. Returns null when it holds no list, or
 * holds a malformed channel.
 * @param {any} message  a parsed JSON message of type "observations"
 * @returns {Observation[] | null}
 */
export function decodeObservations(message) {
  if (!Array.isArray(message.channels)) return null;
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @type {Observation[]} */
  const channels = [];
  for (const c of message.channels) {
    const valid = c !== null && typeof c === 'object' && finite(c.frequency_hz) && finite(c.first_s)
      && finite(c.last_s) && Number.isSafeInteger(c.transmissions) && finite(c.active_s) && finite(c.margin_db)
      && Array.isArray(c.hours) && c.hours.length === 24
      && c.hours.every((/** @type {unknown} */ n) => Number.isSafeInteger(n));
    if (!valid) return null;
    channels.push({ frequencyHz: c.frequency_hz, channelHz: finite(c.channel_hz) ? c.channel_hz : c.frequency_hz,
      firstS: c.first_s, lastS: c.last_s, transmissions: c.transmissions, activeS: c.active_s, marginDb: c.margin_db,
      hours: c.hours.slice() });
  }
  return channels;
}

/**
 * Decode a preferences message. Returns null unless its values are a JSON
 * object; values that are not numbers, true or false, texts or null are left
 * out.
 * @param {any} message  a parsed JSON message of type "preferences"
 * @returns {Preferences | null}
 */
export function decodePreferences(message) {
  const values = message.values;
  if (values === null || typeof values !== 'object' || Array.isArray(values)) return null;
  // fromEntries defines own properties: a "__proto__" entry cannot change the prototype.
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value === null
    || typeof value === 'boolean' || typeof value === 'string'
    || (typeof value === 'number' && Number.isFinite(value))));
}

/**
 * Decode a recording state message. Returns null when a field is missing or
 * malformed, apart from the folders, which the engine sends this computer's
 * interfaces only: missing or malformed, they are null; and `iq`, which an
 * engine that records no I/Q leaves out: missing, it is null.
 * @param {any} message  a parsed JSON message of type "recording"
 * @returns {RecordingState | null}
 */
export function decodeRecording(message) {
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const textOrNull = (value) => (typeof value === 'string' ? value : null);
  const { audio, stopped } = message;
  const iq = message.iq === undefined ? null : message.iq;
  const validAudio = audio === null || (typeof audio === 'object' && (audio.file === null || typeof audio.file === 'string')
    && finite(audio.started) && finite(audio.frequency_hz) && typeof audio.mode === 'string'
    && (audio.rate_hz === null || finite(audio.rate_hz)) && finite(audio.seconds) && finite(audio.bytes));
  const validIq = iq === null || (typeof iq === 'object' && (iq.file === null || typeof iq.file === 'string')
    && finite(iq.started) && finite(iq.frequency_hz) && finite(iq.rate_hz) && iq.rate_hz > 0
    && finite(iq.seconds) && finite(iq.bytes));
  const validStop = stopped === null || (typeof stopped === 'object' && typeof stopped.kind === 'string'
    && typeof stopped.reason === 'string' && typeof stopped.detail === 'string');
  if (!validAudio || !validIq || !validStop) return null;
  return {
    audio: audio && {
      file: audio.file,
      startedS: audio.started,
      frequencyHz: audio.frequency_hz,
      mode: audio.mode,
      rateHz: audio.rate_hz,
      seconds: audio.seconds,
      bytes: audio.bytes,
    },
    iq: iq && {
      file: iq.file,
      startedS: iq.started,
      frequencyHz: iq.frequency_hz,
      rateHz: iq.rate_hz,
      seconds: iq.seconds,
      bytes: iq.bytes,
    },
    stopped: stopped && { kind: stopped.kind, reason: stopped.reason, detail: stopped.detail },
    folder: textOrNull(message.folder),
    defaultFolder: textOrNull(message.default_folder),
  };
}

/**
 * Decode a recordings message. Returns null when it holds no list, or holds
 * a malformed recording: an I/Q recording also needs its sample rate and its
 * metadata file, or null.
 * @param {any} message  a parsed JSON message of type "recordings"
 * @returns {RecordingList | null}
 */
export function decodeRecordings(message) {
  if (!Array.isArray(message.files)) return null;
  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  /** @param {unknown} value */
  const sizeOrNull = (value) => value === null || (finite(value) && /** @type {number} */ (value) >= 0);
  /** @type {RecordingFile[]} */
  const files = [];
  for (const f of message.files) {
    const valid = f !== null && typeof f === 'object' && typeof f.name === 'string' && typeof f.kind === 'string'
      && finite(f.started) && finite(f.frequency_hz) && typeof f.mode === 'string'
      && sizeOrNull(f.bytes) && sizeOrNull(f.seconds) && typeof f.running === 'boolean'
      && (f.kind !== 'iq' || (finite(f.rate_hz) && f.rate_hz > 0 && (f.meta === null || typeof f.meta === 'string')));
    if (!valid) return null;
    const iq = f.kind === 'iq';
    files.push({ name: f.name, kind: f.kind, startedS: f.started, frequencyHz: f.frequency_hz, mode: f.mode,
      rateHz: iq ? f.rate_hz : null, meta: iq ? f.meta : null, bytes: f.bytes, seconds: f.seconds,
      running: f.running });
  }
  const valid = Number.isSafeInteger(message.more) && message.more >= 0 && sizeOrNull(message.free_bytes)
    && typeof message.problem === 'string';
  return valid ? { files, more: message.more, freeBytes: message.free_bytes, problem: message.problem } : null;
}

/**
 * Decode a storage message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "storage"
 * @returns {StorageState | null}
 */
export function decodeStorage(message) {
  const m = message;
  const valid = (m.directory === null || typeof m.directory === 'string') && typeof m.saving === 'boolean'
    && typeof m.problem === 'string';
  return valid ? { directory: m.directory, saving: m.saving, problem: m.problem } : null;
}

/**
 * Decode an update message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "update"
 * @returns {UpdateState | null}
 */
export function decodeUpdate(message) {
  const m = message;
  const valid = UPDATE_STATUSES.includes(m.status) && typeof m.version === 'string'
    && (m.latest === null || typeof m.latest === 'string') && typeof m.detail === 'string'
    && typeof m.check_at_start === 'boolean';
  return valid
    ? { status: m.status, version: m.version, latest: m.latest, detail: m.detail, checkAtStart: m.check_at_start }
    : null;
}

/**
 * Decode a problem report message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "report"
 * @returns {ProblemReport | null}
 */
export function decodeReport(message) {
  const valid = typeof message.text === 'string' && typeof message.issue_url === 'string';
  return valid ? { text: message.text, issueUrl: message.issue_url } : null;
}

/**
 * Decode a carrier measure message. Returns null when a field is missing or
 * malformed: a frequency comes without a reason, and a reason without a
 * frequency.
 * @param {any} message  a parsed JSON message of type "carrier"
 * @returns {CarrierMeasure | null}
 */
export function decodeCarrier(message) {
  const m = message;
  /** @param {unknown} value */
  const numberOrNull = (value) => value === null || (typeof value === 'number' && Number.isFinite(value));
  const valid = (m.id === null || Number.isSafeInteger(m.id)) && numberOrNull(m.frequency_hz)
    && numberOrNull(m.margin_db) && (m.reason === null || typeof m.reason === 'string')
    && (m.frequency_hz === null) === (m.reason !== null);
  return valid ? { id: m.id, frequencyHz: m.frequency_hz, marginDb: m.margin_db, reason: m.reason } : null;
}

/**
 * Decode a driver check message. Returns null when a field is missing or
 * malformed: a computation comes without a reason, and a reason without a
 * computation.
 * @param {any} message  a parsed JSON message of type "driver_pll"
 * @returns {DriverCheck | null}
 */
export function decodeDriverCheck(message) {
  const m = message;
  const found = m.driver_pll === null || (DRIVER_PLLS.includes(m.driver_pll) && m.driver_pll !== 'unknown');
  const valid = (m.id === null || Number.isSafeInteger(m.id)) && found
    && (m.previous === null || DRIVER_PLLS.includes(m.previous))
    && (m.reason === null || typeof m.reason === 'string') && (m.driver_pll === null) === (m.reason !== null);
  return valid ? { id: m.id, driverPll: m.driver_pll, previous: m.previous, reason: m.reason } : null;
}

/**
 * Decode a remote access message. Returns null when a field is missing or
 * malformed.
 * @param {any} message  a parsed JSON message of type "remote"
 * @returns {RemoteState | null}
 */
export function decodeRemote(message) {
  /** @param {unknown} value */
  const textOrNull = (value) => value === null || typeof value === 'string';
  /** @param {any} d */
  const device = (d) => d !== null && typeof d === 'object' && typeof d.id === 'string'
    && typeof d.label === 'string' && Number.isSafeInteger(d.paired) && Number.isSafeInteger(d.seen)
    && typeof d.connected === 'boolean';
  const m = message;
  const valid = typeof m.on === 'boolean' && Number.isSafeInteger(m.port) && Array.isArray(m.urls)
    && m.urls.every((/** @type {unknown} */ url) => typeof url === 'string') && textOrNull(m.code)
    && typeof m.blocked === 'boolean' && Array.isArray(m.devices) && m.devices.every(device)
    && textOrNull(m.fingerprint) && typeof m.problem === 'string';
  if (!valid) return null;
  return {
    on: m.on,
    port: m.port,
    urls: m.urls.slice(),
    code: m.code,
    blocked: m.blocked,
    devices: m.devices.map((/** @type {any} */ d) => (
      { id: d.id, label: d.label, pairedS: d.paired, seenS: d.seen, connected: d.connected })),
    fingerprint: m.fingerprint,
    problem: m.problem,
  };
}

/**
 * Decode a SoapySDR devices message. Returns null when a field is missing
 * or malformed.
 * @param {any} message  a parsed JSON message of type "devices"
 * @returns {DevicesState | null}
 */
export function decodeDevices(message) {
  /** @param {unknown} value */
  const text = (value) => typeof value === 'string';
  /** @param {any} d */
  const device = (d) => d !== null && typeof d === 'object' && text(d.driver) && d.driver !== '' && text(d.serial)
    && text(d.label) && typeof d.in_use === 'boolean';
  const m = message;
  const valid = DEVICE_SEARCHES.includes(m.search) && text(m.detail) && Array.isArray(m.drivers)
    && m.drivers.every(text) && Array.isArray(m.devices) && m.devices.every(device)
    && typeof m.chosen === 'boolean' && (m.args === null || text(m.args));
  if (!valid) return null;
  return {
    search: m.search,
    detail: m.detail,
    drivers: m.drivers.slice(),
    devices: m.devices.map((/** @type {any} */ d) => ({ driver: d.driver, serial: d.serial, label: d.label, inUse: d.in_use })),
    chosen: m.chosen,
    args: m.args,
  };
}

/**
 * The fields of a bookmark command, named as the engine names them.
 * @param {BookmarkFields} fields
 */
function bookmarkFields(fields) {
  /** @type {Record<string, string | number | boolean>} */
  const message = {};
  if (fields.frequencyHz !== undefined) message.frequency_hz = fields.frequencyHz;
  if (fields.name !== undefined) message.name = fields.name;
  if (fields.mode !== undefined) message.mode = fields.mode;
  if (fields.scan !== undefined) message.scan = fields.scan;
  return message;
}

/** The address of the stream on the server that served this page. */
function streamUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/stream`;
}

/** One WebSocket to the engine at a time, opened again after each disconnection. */
export class EngineStream {
  /** @type {StreamHandlers} */
  #handlers;
  #url;
  /** @type {WebSocket | null} */
  #socket = null;
  #running = false;
  #greeted = false;
  #incompatible = false;
  #remote = false;        // the engine serves this interface through remote access
  #version = '';          // the Radiolack that runs, as its last hello said: '' from an older engine
  /** @type {string[]} */
  #scanners = [];         // the scanners the engine runs, as its last hello listed them
  /** @type {string[]} */
  #recordings = [];       // what the engine records, as its last hello listed it
  /** @type {string[]} */
  #measures = [];         // what the engine measures, as its last hello listed it
  /** @type {string[]} */
  #spectra = [];          // the spectra the engine sends when asked, as its last hello listed them
  #remoteOff = false;     // the computer turned remote access off: retrying until it is on again
  #retryMs = RETRY_FIRST_MS;
  #timer = 0;
  #audio = false;         // this interface wants audio
  #fine = false;          // this interface wants the fine spectrum
  #lastChangeId = 0;      // ids of demodulation changes, counted for the whole page
  #awaitedId = 0;         // the last one sent on this connection: older echoes are stale

  /**
   * @param {StreamHandlers} handlers
   * @param {string} [url]
   */
  constructor(handlers, url = streamUrl()) {
    this.#handlers = handlers;
    this.#url = url;
  }

  /**
   * Whether the engine serves this interface through remote access, as its
   * last hello said: it then sends neither the storage, update nor remote
   * access state, and refuses their changes.
   */
  get remote() {
    return this.#remote;
  }

  /**
   * The version of the Radiolack that runs, as its last hello said, on every
   * interface; '' before the first hello, or from an engine older than About.
   */
  get version() {
    return this.#version;
  }

  /**
   * The scanners the engine runs, as its last hello listed them, such as
   * "peaks" and "memory": an older engine runs fewer, or none.
   * @returns {string[]}
   */
  get scanners() {
    return this.#scanners.slice();
  }

  /**
   * What the engine records, as its last hello listed it, such as "audio"
   * and "iq": an older engine records less, or nothing.
   * @returns {string[]}
   */
  get recordings() {
    return this.#recordings.slice();
  }

  /**
   * What the engine measures when an interface asks, as its last hello
   * listed it, such as "carrier": an older engine measures nothing.
   * @returns {string[]}
   */
  get measures() {
    return this.#measures.slice();
  }

  /**
   * The spectra the engine sends besides the spectrum frames when an
   * interface asks, as its last hello listed them, such as "fine": an
   * older engine sends none.
   * @returns {string[]}
   */
  get spectra() {
    return this.#spectra.slice();
  }

  /** Connect, unless already connected or connecting. */
  start() {
    if (this.#running) return;
    this.#running = true;
    this.#incompatible = false;
    this.#retryMs = RETRY_FIRST_MS;
    this.#handlers.onState('connecting');
    this.#connect();
  }

  /**
   * Ask the engine to change source settings. Every interface then receives
   * the new state, or this one an error. Returns false, sending nothing,
   * when the stream is not open.
   * @param {SourceChanges} changes
   * @returns {boolean}
   */
  sendSource(changes) {
    /** @type {Record<string, string | number | boolean | null | Record<string, DeviceValue>>} */
    const message = { type: 'source' };
    if (changes.kind !== undefined) message.kind = changes.kind;
    if (changes.address !== undefined) message.address = changes.address;
    if (changes.centreHz !== undefined) message.centre_hz = changes.centreHz;
    if (changes.rateHz !== undefined) message.sample_rate_hz = changes.rateHz;
    if (changes.gainDb !== undefined) message.gain_db = changes.gainDb;
    if (changes.stageGainsDb !== undefined) message.stage_gains_db = changes.stageGainsDb;
    if (changes.antenna !== undefined) message.antenna = changes.antenna;
    if (changes.deviceSettings !== undefined) message.device_settings = changes.deviceSettings;
    if (changes.ppm !== undefined) message.ppm = changes.ppm;
    if (changes.hfInput !== undefined) message.hf_input = changes.hfInput;
    if (changes.rtlAgc !== undefined) message.rtl_agc = changes.rtlAgc;
    if (changes.biasTee !== undefined) message.bias_tee = changes.biasTee;
    if (changes.converter !== undefined) message.converter = changes.converter;
    if (changes.converterLoHz !== undefined) message.converter_lo_hz = changes.converterLoHz;
    if (changes.driverPll !== undefined) message.driver_pll = changes.driverPll;
    if (changes.dcRemoval !== undefined) message.dc_removal = changes.dcRemoval;
    return this.#send(message);
  }

  /**
   * Ask the engine to change demodulation settings. Every interface then
   * receives the new settings; until they include this change, this
   * interface ignores older settings, which would undo it on screen.
   * Returns false, sending nothing, when the stream is not open.
   * @param {DemodChanges} changes
   * @returns {boolean}
   */
  sendDemod(changes) {
    /** @type {Record<string, string | number | boolean>} */
    const message = { type: 'demod', id: this.#lastChangeId + 1 };
    if (changes.frequencyHz !== undefined) message.frequency_hz = changes.frequencyHz;
    if (changes.mode !== undefined) message.mode = changes.mode;
    if (changes.bandwidthHz !== undefined) message.bandwidth_hz = changes.bandwidthHz;
    if (changes.squelchDb !== undefined) message.squelch_db = changes.squelchDb;
    if (changes.squelchAuto !== undefined) message.squelch_auto = changes.squelchAuto;
    if (changes.notch !== undefined) message.notch = changes.notch;
    if (changes.blanker !== undefined) message.blanker = changes.blanker;
    if (!this.#send(message)) return false;
    this.#lastChangeId += 1;
    this.#awaitedId = this.#lastChangeId;
    return true;
  }

  /**
   * Ask the engine to add a bookmark. Every interface then receives the
   * bookmarks, or this one an error. Returns false, sending nothing, when
   * the stream is not open.
   * @param {{frequencyHz: number, name: string, mode: string, scan?: boolean}} bookmark
   * @returns {boolean}
   */
  addBookmark(bookmark) {
    return this.#send({ type: 'bookmark', action: 'add', ...bookmarkFields(bookmark) });
  }

  /**
   * Ask the engine to change some fields of a bookmark, as `addBookmark`.
   * @param {number} id
   * @param {BookmarkFields} changes
   * @returns {boolean}
   */
  changeBookmark(id, changes) {
    return this.#send({ type: 'bookmark', action: 'change', id, ...bookmarkFields(changes) });
  }

  /**
   * Ask the engine to remove a bookmark, as `addBookmark`.
   * @param {number} id
   * @returns {boolean}
   */
  removeBookmark(id) {
    return this.#send({ type: 'bookmark', action: 'remove', id });
  }

  /**
   * Change the engine's scanner settings: every interface then receives the
   * scanner state, or this one an error. Returns false, sending nothing,
   * when the stream is not open.
   * @param {ScannerChanges} changes
   * @returns {boolean}
   */
  setScanner(changes) {
    /** @type {Record<string, boolean | number>} */
    const message = {};
    if (changes.detection !== undefined) message.detection = changes.detection;
    if (changes.thresholdDb !== undefined) message.threshold_db = changes.thresholdDb;
    if (changes.resumeS !== undefined) message.resume_s = changes.resumeS;
    if (changes.listenS !== undefined) message.listen_s = changes.listenS;
    if (changes.sweepLowHz !== undefined) message.sweep_low_hz = changes.sweepLowHz;
    if (changes.sweepHighHz !== undefined) message.sweep_high_hz = changes.sweepHighHz;
    if (changes.sweepWindowS !== undefined) message.sweep_window_s = changes.sweepWindowS;
    return this.#send({ type: 'scanner', ...message });
  }

  /**
   * Start a scan, the memory scan ("memory"), the sweep ("sweep") or the
   * signals scan ("signals"), or stop the scan that runs (null): every
   * interface then receives the scanner state, or this one an error, such as
   * when no ticked bookmark lies in the receiver's span, when the receiver
   * does not tune, or when detection is off. Returns false, sending nothing,
   * when the stream is not open.
   * @param {'memory' | 'sweep' | 'signals' | null} scan
   * @returns {boolean}
   */
  setScan(scan) {
    return this.#send({ type: 'scanner', scan });
  }

  /**
   * Ask the engine to ignore a frequency: its signals leave the list, for
   * every interface, and stay out of it. Returns false, sending nothing,
   * when the stream is not open.
   * @param {number} frequencyHz
   * @returns {boolean}
   */
  ignoreFrequency(frequencyHz) {
    return this.#send({ type: 'scanner', ignore: frequencyHz });
  }

  /**
   * Ask the engine to ignore no frequency any more, as `ignoreFrequency`.
   * @returns {boolean}
   */
  restoreIgnored() {
    return this.#send({ type: 'scanner', restore: true });
  }

  /**
   * Ask the engine for the channels where it found signals, which come to
   * this interface only (`onObservations`). Returns false, sending nothing,
   * when the stream is not open.
   * @returns {boolean}
   */
  askObservations() {
    return this.#send({ type: 'observations' });
  }

  /**
   * Ask the engine to forget the channel within 1.5 kHz of a frequency, for
   * every interface; this one then receives the channels left. Returns
   * false, sending nothing, when the stream is not open.
   * @param {number} frequencyHz
   * @returns {boolean}
   */
  clearObservation(frequencyHz) {
    return this.#send({ type: 'observations', clear: frequencyHz });
  }

  /**
   * Ask the engine to forget every channel, as `clearObservation`.
   * @returns {boolean}
   */
  clearObservations() {
    return this.#send({ type: 'observations', clear_all: true });
  }

  /**
   * Give the engine this interface's preferences, which the next interfaces
   * of this computer, or of this device through remote access, start with;
   * open interfaces keep theirs. Returns false, sending nothing, when the
   * stream is not open.
   * @param {Preferences} values
   * @returns {boolean}
   */
  savePreferences(values) {
    return this.#send({ type: 'preferences', values });
  }

  /**
   * Start recording the audio (true), unless an audio recording runs, or
   * stop it (false): every interface then receives the recording state, or
   * this one an error, such as on the simulated band. Returns false, sending
   * nothing, when the stream is not open.
   * @param {boolean} on
   * @returns {boolean}
   */
  setRecording(on) {
    return this.#send({ type: 'recording', audio: on });
  }

  /**
   * Start recording the receiver's raw I/Q (true), unless an I/Q recording
   * runs, or stop it (false), as `setRecording` does for the audio.
   * @param {boolean} on
   * @returns {boolean}
   */
  setIqRecording(on) {
    return this.#send({ type: 'recording', iq: on });
  }

  /**
   * Choose the folder the recordings go to, a full path, "~" standing for
   * the home folder, or null for the default one: the engine checks it, then
   * this computer's interfaces receive the recording state, or this one an
   * error. Not while a recording runs, nor on a device that remote access
   * serves. Returns false, sending nothing, when the stream is not open.
   * @param {string | null} folder
   * @returns {boolean}
   */
  setRecordingFolder(folder) {
    return this.#send({ type: 'recording', folder });
  }

  /**
   * Ask the engine for the recordings of its folder, which come to this
   * interface only (`onRecordings`). Returns false, sending nothing, when the
   * stream is not open.
   * @returns {boolean}
   */
  askRecordings() {
    return this.#send({ type: 'recordings' });
  }

  /**
   * Ask the engine to delete a recording, unless it runs, with an I/Q
   * recording's metadata; this interface then receives the recordings left,
   * or an error. Returns false, sending nothing, when the stream is not open.
   * @param {string} name  the recording's file, as the list names it
   * @returns {boolean}
   */
  deleteRecording(name) {
    return this.#send({ type: 'recordings', delete: name });
  }

  /**
   * Ask the engine to check for a newer Radiolack now, unless a check is
   * running: every interface receives the update state as it changes.
   * Returns false, sending nothing, when the stream is not open.
   * @returns {boolean}
   */
  checkUpdates() {
    return this.#send({ type: 'update', check: true });
  }

  /**
   * Turn the engine's update check at start on or off, for the next starts.
   * Returns false, sending nothing, when the stream is not open.
   * @param {boolean} on
   * @returns {boolean}
   */
  setCheckAtStart(on) {
    return this.#send({ type: 'update', check_at_start: on });
  }

  /**
   * Ask the engine for a problem report, which comes to this interface only
   * (`onReport`), with what this interface tells of itself: a line of text,
   * of 300 characters at most. Returns false, sending nothing, when the
   * stream is not open.
   * @param {string} page
   * @returns {boolean}
   */
  askReport(page) {
    return this.#send({ type: 'report', page });
  }

  /**
   * Ask the engine to measure the carrier at the VFO: the result comes to
   * this interface only (`onCarrier`), about two seconds later, with `id`.
   * Asking again before it comes waits for the same measure, with the new
   * id. Returns false, sending nothing, when the stream is not open.
   * @param {number} id  a whole number from 1
   * @returns {boolean}
   */
  measureCarrier(id) {
    return this.#send({ type: 'carrier', action: 'measure', id });
  }

  /**
   * Ask the engine to check which computation the rtl-sdr driver follows,
   * on the carrier at the VFO: the receiver moves for about ten seconds, and
   * the result comes to this interface only (`onDriverCheck`), with `id`.
   * Asking while a check runs waits for that one. Returns false, sending
   * nothing, when the stream is not open.
   * @param {number} id  a whole number from 1
   * @returns {boolean}
   */
  identifyDriver(id) {
    return this.#send({ type: 'carrier', action: 'identify', id });
  }

  /**
   * Turn remote access on or off: this computer's interfaces receive its
   * state as it changes, which says why it could not turn on. Returns false,
   * sending nothing, when the stream is not open.
   * @param {boolean} on
   * @returns {boolean}
   */
  setRemoteAccess(on) {
    return this.#send({ type: 'remote', on });
  }

  /**
   * Change the port remote access listens on, while it is off. Returns
   * false, sending nothing, when the stream is not open.
   * @param {number} port
   * @returns {boolean}
   */
  setRemotePort(port) {
    return this.#send({ type: 'remote', port });
  }

  /**
   * Make a new access code: every paired device is forgotten and must enter
   * it. Returns false, sending nothing, when the stream is not open.
   * @returns {boolean}
   */
  renewRemoteCode() {
    return this.#send({ type: 'remote', new_code: true });
  }

  /**
   * Forget a paired device, which must enter the code again. Returns false,
   * sending nothing, when the stream is not open.
   * @param {string} id
   * @returns {boolean}
   */
  forgetRemoteDevice(id) {
    return this.#send({ type: 'remote', forget: id });
  }

  /**
   * Ask the engine to search for SoapySDR devices, unless a search runs:
   * every interface receives the devices state as it changes. This
   * computer's interfaces only. Returns false, sending nothing, when the
   * stream is not open.
   * @returns {boolean}
   */
  searchDevices() {
    return this.#send({ type: 'devices', search: true });
  }

  /**
   * Ask the engine to read a SoapySDR device that its search found, by its
   * driver and serial number, in place of the source in use: every
   * interface then receives the new source state, or this one an error.
   * Returns false, sending nothing, when the stream is not open.
   * @param {string} driver
   * @param {string} serial
   * @returns {boolean}
   */
  chooseDevice(driver, serial) {
    return this.#send({ type: 'devices', choose: { driver, serial } });
  }

  /**
   * Ask the engine to read the SoapySDR device that device arguments name,
   * such as "driver=rtlsdr", as `chooseDevice` does. This computer's
   * interfaces only.
   * @param {string} args
   * @returns {boolean}
   */
  openDevice(args) {
    return this.#send({ type: 'devices', args });
  }

  /**
   * Whether this interface wants audio: the engine sends audio frames only
   * then, on this connection and the next ones.
   * @param {boolean} on
   */
  setAudio(on) {
    if (on === this.#audio) return;
    this.#audio = on;
    this.#send({ type: 'audio', on });     // when not open, #hello sends it once greeted
  }

  /**
   * Whether this interface wants the fine spectrum of the VFO's channel: the
   * engine sends fine frames only then, on this connection and the next
   * ones, and only an engine whose hello lists "fine" among its `spectra` is
   * asked; an older one never is.
   * @param {boolean} on
   */
  setFine(on) {
    if (on === this.#fine) return;
    this.#fine = on;
    // When not open, #hello asks once greeted, if the engine knows the fine spectrum.
    if (this.#spectra.includes('fine')) this.#send({ type: 'fine', on });
  }

  /** Whether this interface asks the engine for the fine spectrum: it wants it, and the engine knows it. */
  get #asksFine() {
    return this.#fine && this.#spectra.includes('fine');
  }

  /** Disconnect and stop retrying; the engine idles once no client listens. */
  stop() {
    this.#running = false;
    clearTimeout(this.#timer);
    const socket = this.#socket;
    this.#socket = null;
    if (socket) socket.close(1000);
  }

  /**
   * Send a command, once the engine greeted this interface.
   * @param {Record<string, unknown>} command
   * @returns {boolean} false, sending nothing, when the stream is not open
   */
  #send(command) {
    const socket = this.#socket;
    if (!socket || !this.#greeted || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(command));
    return true;
  }

  #connect() {
    let socket;
    try {
      socket = new WebSocket(this.#url);
    } catch (error) {
      // Some browsers throw at once when a policy blocks the address: retry later like any failure.
      console.error('Radiolack: cannot open the engine stream:', error);
      this.#retry();
      return;
    }
    socket.binaryType = 'arraybuffer';
    this.#socket = socket;
    this.#greeted = false;
    this.#awaitedId = 0;
    socket.addEventListener('message', (event) => this.#receive(socket, event));
    // A failed connection fires "error" then "close": the retry is scheduled on "close" only.
    socket.addEventListener('close', (event) => this.#closed(socket, event.code));
  }

  /**
   * @param {WebSocket} socket
   * @param {MessageEvent} event
   */
  #receive(socket, event) {
    if (socket !== this.#socket) return;
    if (typeof event.data === 'string') {
      this.#text(socket, event.data);
    } else if (this.#greeted && event.data instanceof ArrayBuffer && event.data.byteLength) {
      const kind = new Uint8Array(event.data, 0, 1)[0];
      if (kind === FRAME_KIND_SPECTRUM) {
        const frame = decodeFrame(event.data);
        if (frame) this.#handlers.onFrame(frame);
      } else if (kind === FRAME_KIND_AUDIO && this.#audio) {
        const frame = decodeAudio(event.data);
        if (frame) this.#handlers.onAudio(frame);
      } else if (kind === FRAME_KIND_FINE && this.#asksFine) {
        const frame = decodeFine(event.data);
        if (frame && this.#handlers.onFine) this.#handlers.onFine(frame);
      }       // a frame of another kind, such as a newer engine's, is ignored
    }
  }

  /**
   * A JSON message: the hello first, then states and errors.
   * @param {WebSocket} socket
   * @param {string} text
   */
  #text(socket, text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      return;
    }
    if (!message || typeof message !== 'object') return;
    if (!this.#greeted) {
      if (message.type === 'hello') this.#hello(socket, message);
    } else if (message.type === 'source') {
      const source = decodeSource(message);
      if (source) this.#handlers.onSource(source);
    } else if (message.type === 'demod') {
      const demod = decodeDemod(message);
      if (demod && demod.acknowledged >= this.#awaitedId) this.#handlers.onDemod(demod);
    } else if (message.type === 'bookmarks') {
      const bookmarks = decodeBookmarks(message);
      if (bookmarks) this.#handlers.onBookmarks(bookmarks);
    } else if (message.type === 'scanner') {
      const scanner = decodeScanner(message);
      if (scanner) this.#handlers.onScanner(scanner);
    } else if (message.type === 'peaks') {
      const peaks = decodePeaks(message);
      if (peaks) this.#handlers.onPeaks(peaks);
    } else if (message.type === 'observations') {
      const observations = decodeObservations(message);
      if (observations) this.#handlers.onObservations(observations);
    } else if (message.type === 'preferences') {
      const values = decodePreferences(message);
      if (values) this.#handlers.onPreferences(values);
    } else if (message.type === 'recording') {
      const recording = decodeRecording(message);
      if (recording) this.#handlers.onRecording(recording);
    } else if (message.type === 'recordings') {
      const recordings = decodeRecordings(message);
      if (recordings) this.#handlers.onRecordings(recordings);
    } else if (message.type === 'storage') {
      const storage = decodeStorage(message);
      if (storage) this.#handlers.onStorage(storage);
    } else if (message.type === 'update') {
      const update = decodeUpdate(message);
      if (update) this.#handlers.onUpdate(update);
    } else if (message.type === 'remote') {
      const remote = decodeRemote(message);
      if (remote) this.#handlers.onRemote(remote);
    } else if (message.type === 'report') {
      const report = decodeReport(message);
      if (report) this.#handlers.onReport(report);
    } else if (message.type === 'carrier') {
      const measure = decodeCarrier(message);
      if (measure) this.#handlers.onCarrier(measure);
    } else if (message.type === 'devices') {
      const devices = decodeDevices(message);
      if (devices && this.#handlers.onDevices) this.#handlers.onDevices(devices);
    } else if (message.type === 'driver_pll') {
      const check = decodeDriverCheck(message);
      if (check) this.#handlers.onDriverCheck(check);
    } else if (message.type === 'error') {
      const command = COMMANDS.includes(message.command) ? message.command : null;
      this.#handlers.onError(String(message.message), command);
    }
  }

  /**
   * @param {WebSocket} socket
   * @param {any} message  a parsed hello
   */
  #hello(socket, message) {
    if (message.protocol !== PROTOCOL_VERSION) {
      // Reconnecting would meet the same engine: the page must be reloaded.
      this.#incompatible = true;
      socket.close(1000);
      return;
    }
    this.#greeted = true;
    this.#remote = message.remote === true;
    this.#version = typeof message.version === 'string' ? message.version : '';
    /** @param {unknown} list */
    const names = (list) => (Array.isArray(list) ? list.filter((name) => typeof name === 'string') : []);
    this.#scanners = names(message.scanners);
    this.#recordings = names(message.recording);
    this.#measures = names(message.measures);
    this.#spectra = names(message.spectra);
    this.#remoteOff = false;
    this.#retryMs = RETRY_FIRST_MS;
    if (this.#audio) socket.send(JSON.stringify({ type: 'audio', on: true }));
    if (this.#asksFine) socket.send(JSON.stringify({ type: 'fine', on: true }));
    this.#handlers.onState('open');
  }

  /**
   * @param {WebSocket} socket
   * @param {number} code  the close code
   */
  #closed(socket, code) {
    if (socket !== this.#socket) return;
    this.#socket = null;
    if (!this.#running) return;
    if (this.#incompatible) {
      this.#running = false;
      this.#handlers.onState('incompatible');
      return;
    }
    if (code === CLOSE_NOT_PAIRED) {
      // Reconnecting would meet the same refusal: the page must pair this device again.
      this.#running = false;
      this.#handlers.onState('unpaired');
      return;
    }
    if (code === CLOSE_REMOTE_OFF) this.#remoteOff = true;
    this.#retry();
  }

  #retry() {
    this.#handlers.onState(this.#remoteOff ? 'remote-off' : 'waiting');
    const delay = this.#retryMs;
    this.#retryMs = Math.min(RETRY_MAX_MS, delay * 2);
    this.#timer = window.setTimeout(() => { if (this.#running) this.#connect(); }, delay);
  }
}
