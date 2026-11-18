# Milestones after Coverage and precision

Designed with the owner on 2026-11-16, in the order they come, before v4,
the public release. [`scoping.md`](scoping.md) holds their summaries and
[`coverage-and-precision.md`](coverage-and-precision.md) the milestone
before them.

The owner asked for every one of them to be designed now. Where a choice
needs a measure that nobody has made yet, the milestone starts with a
spike, and this file fixes beforehand what the spike measures and what each
result decides: nothing waits for a new question.

Rules they share:

- Receive-only. Radiolack never transmits, whatever the receiver can do
  (decision of 2026-11-08).
- Tests need no receiver and reach no network; the owner checks on
  hardware.
- Protocol version 6 stays wherever a new field or message does: an older
  page ignores what it does not know, and the hello lists what the engine
  can do.
- Each pull request brings its documentation and tests.

## 1. Any receiver

Goal: the owner's SDRplay RSPdx-R2, 1 kHz to 2 GHz, and HackRF One, 1 MHz
to 6 GHz, and other receivers, through SoapySDR. An RTL-SDR keeps rtl_tcp.
The RSPdx-R2 joined on 2026-11-18, the day before the owner received it:
it is the first device checked after the V3, the HackRF One following
when it is back.

### Decisions

- **Gains**: one overall slider, and an Advanced section with one slider
  per stage of the receiver (a HackRF has three: AMP, LNA, VGA). The
  HackRF's input amplifier starts off: strong signals can damage it.
- **Devices listed**: those of the local USB drivers Radiolack knows
  (`hackrf`, `airspy`, `airspyhf`, `lime`, `bladerf`, `sdrplay`), searched
  one driver at a time, so that no search reaches the network: Radiolack
  talks only to its receiver, its clients and its APT repository. Any other
  device opens through a field, "SoapySDR device arguments", on this
  computer's interfaces only, as the rtl_tcp server's address.
- An RTL-SDR is not listed there: it has its own entries. The arguments
  field takes `driver=rtlsdr`, which is how the milestone is first checked,
  on the owner's V3, before the RSPdx-R2's driver is installed.
- **What a device declares beyond gains, antennas and antenna power** (an
  RSPdx-R2 has notch filters for the broadcast bands and a high dynamic
  range mode below 2 MHz): shown in the Advanced section as the device
  describes each, a box or a choice under the device's own name for it,
  saved with the device, at the device's default until changed. A setting
  that puts power on an antenna socket, whatever the device calls it,
  follows the antenna power's rules instead.

### Design

- **Source.** `soapy.py`, a source like the others: its range, its sample
  rates, its gains and what is settable come from the device, and the
  interface shows what the state describes, as since Coverage and
  precision. The `SoapySDR` Python module is imported when a SoapySDR
  source is asked for; without it, the Receiver menu says which package
  brings it.
- **Samples**: the device's native format among CS8, CS16 and CF32, read
  by a thread into the same buffer rules as the rtl_tcp client's: a reader
  that falls behind loses the oldest, and an overflow that the device
  reports is logged at most once a minute. A device unplugged is a waiting
  state, tried again, not an error.
- **Receive only**: the source opens a receive stream and has no code that
  names a transmit direction; a test fails if the stand-in module sees one.
- **Sample rates**: those of the device that the first spike allows.
- **Analogue bandwidth**, where the device has one: the nearest at or below
  0.8 times the sample rate.
- **Correction**: in software, by the scaling of Coverage and precision,
  with the whole ppm to the device only where it takes one.
- **Settings read from the device**: a direct-sampling setting becomes the
  shortwave input, an antenna-power setting becomes `bias_tee`, with the
  same rules (off at each start unless `--bias-tee`, never saved, a second
  press), several antenna sockets become a choice.
- **Each device keeps its settings**, under its driver and serial number.
- **`--soapy ARGS`** opens a device at start, as `--rtl-sdr` does; the
  service's administrator puts it in the unit with `systemctl edit`.
- **I/Q recordings** take the device's format: SigMF's `ci8`, `ci16_le` or
  `cf32_le`.
- **Package**: `Recommends: python3-soapysdr`; the drivers' modules are
  suggested, under the names that the four supported systems give them,
  which `package.yml` checks.

### Spikes, first pull request

| Question | Measure | Decides |
| --- | --- | --- |
| Which sample rates can the engine hold? | The engine on synthetic samples at 4, 5, 8 and 10 MS/s on the owner's laptop, with one NFM channel and detection. | A rate is offered when it stays under 50% of one core; above 3.2 MS/s otherwise none is. |
| Does a search by driver stay off the network? | The search run in a network namespace of its own (`unshare -rn`), with the remote module installed, and the module's code. | If a driver's search sends anything, that driver leaves the list and goes through the arguments field. |
| Does a Python thread read 2.4 MS/s without losing samples? | With the owner's V3 through `driver=rtlsdr`, at the owner's request. | If not, the reader moves to a process of its own before anything else is built. |

Measured on 2026-11-18 ([`scoping.md`](scoping.md), spike 7, with the
tables): the first two questions on the owner's laptop, with
`tools/measure_rates.py` and `tools/check_search.py`; the third the same
day, on the owner's word, with the owner's V3 on that laptop and
`tools/measure_reader.py`: no sample lost over 60 s, so the reader stays a
thread, and pull request 2 builds the source that way.

As built, where the spikes settle or put in question a point of this
section; pull requests 2 to 6 rely on these:

- **Sample rates.** The engine's processing of one receiver, one NFM
  channel with audio and detection, and the intake together took 33 to
  38% of one core at 8 MS/s and 48 to 54% at 10 MS/s, by the format and
  the measure: a device's rates up to 8 MS/s are offered, those above are
  not, 10 MS/s staying under 50% in neither every measure nor every run,
  and a rate between two measured ones costing no more than the higher. The RSPdx-R2
  gets its rates up to 8 MS/s, the HackRF One 2 to 8 MS/s. Nothing else
  changes with the rate: the block stays the rate over 30, the display's
  FFTs 8192 samples, whose bins widen to 977 Hz at 8 MS/s, and the deep
  zoom's 48 wide bins, 47 kHz there, exceed the fine spectrum's cover, so
  that the cover alone decides, as on a paired device. Most of the cost at
  the higher rates is the demodulator's front filter, whose transition
  stays 8 kHz; a first stage of decimation would lower it, should 10 MS/s
  be wanted later. Those figures are back to back, the rule's measure
  (below, Back to back and paced); 8 MS/s is to confirm on a receiver at
  that rate, the RSPdx-R2.
- **The intake.** `NativeBuffer` (tools/measure_rates.py) is the buffer
  that pull request 2 starts from: the samples kept as bytes in the
  device's format, half a second at most, the oldest dropped and counted,
  converted to complex64 when the engine takes a block, as the rtl_tcp
  client converts its bytes; reads of the stream's MTU into one array.
- **The native format and its full scale.** SoapySDR 0.8's Python binding
  cannot call `getNativeStreamFormat`, which takes the full scale as a C++
  reference. The source takes the first format the device lists among
  CS8, CS16 and CF32, as the modules of the RTL-SDR (CS8), the HackRF
  (CS8) and SDRplay (CS16) list their native one first. The full scale,
  which sets the levels in dBFS, comes from a table of the listed drivers'
  native formats, read in their modules' code: 128 for the RTL-SDR's and
  the HackRF's CS8, 32767 for SDRplay's and the Airspy's CS16, and 2048
  for bladeRF's CS16, 12 bits, whose levels a full scale of 32768 would put
  24 dB low. A driver outside the table reads CF32, which its module scales
  itself, at the intake's highest cost, still within the rule.
- **The search.** `Device.enumerate` with a `driver` runs that driver's
  search alone, in the calling thread, after loading every installed
  module at the first call; without one it runs every module's,
  SoapyRemote's included, which asks avahi-daemon over D-Bus to search the
  network, a way out that a network namespace does not close. Pull request
  3 searches the listed drivers one by one, in a worker thread, never
  without a driver; its tests check, through the stand-in's `searches`,
  that every search names one of them.
- **The drivers listed**: `hackrf`, `airspy`, `lime` and `bladerf`, whose
  searches made no socket; `airspyhf`, by its code (libusb's listing);
  `sdrplay` once `tools/check_search.py sdrplay` has run on the owner's
  laptop with SDRplay's API and module installed, before pull request 3
  lists it: SDRplay's closed API speaks to SDRplay's service on the same
  computer, which the rule, about the network, does not exclude.
- **The stand-in module**, `tests/soapysdr/SoapySDR.py`, which
  `tests.fakes.stand_in_soapysdr` puts first on the import path whether
  the real module is installed or not: SoapySDR 0.8's names and values for
  what Radiolack uses, devices that a test declares (driver, serial,
  formats, rates, frequency range, MTU, timestamps, which may advance at a
  multiple of the samples as the rtlsdr module's do), found by
  `enumerate`, which keeps every search's arguments, made by
  `Device(args)`, giving a tone in their native format at their rate,
  paced by the wall clock, which a test may replace, with the overflows
  (and the samples they lose, which the timestamps skip), silent losses,
  time-outs and stream errors that a test scripts by read; a device can
  fail to open. Every call that takes a direction refuses any but
  `SOAPY_SDR_RX`, and `writeStream` refuses all, recording the request, so
  that the test fails on the way out even if the code caught the refusal.
  Pull request 2 adds what the source needs: gains, antennas, the settings
  a device declares.
- **The reading thread.** The binding releases Python's lock in
  `readStream` (SWIG's `-threads`): the engine's processing runs while the
  reader waits. The tool reads as the source will, and reports the device's
  overflows, the gaps in its timestamps and the samples dropped for an
  engine behind, three different losses. On the owner's V3 through the
  rtlsdr module, at 2.4 MS/s for 60 s with the engine's processing beside
  it: 144179200 samples in 1100 reads of the MTU, 131072, as many as the
  wall clock expects within a millisecond, no overflow, no read that timed
  out or failed, nothing dropped; the reading thread took 1.2% of one
  core. The reader stays a thread in the engine's process.
- **Timestamps.** The rtlsdr module 0.3.3's timestamps advance by twice the
  samples read, at every read: they count the I and Q bytes. Read as
  samples, they made the tool's first verdict wrong. A device's timestamps
  are not always a count of samples: should the source use them, it checks first
  how they advance, as the tool now does; otherwise it relies on the
  device's overflows and its own buffer's count.
- **Back to back and paced.** The rule's figures are back to back, the
  blocks processed one after the other: the work to do. Processed one
  every 33 ms, as a receiver delivers them, on the owner's laptop, whose
  processor slows down between blocks, the engine took about 40% of one
  core at every rate from 2.4 to 10 MS/s, against 9 to 32% back to back,
  and what grew with the rate was the time a block takes: at 8 and
  10 MS/s, 3 blocks of 69 took longer than their 33 ms, 37 ms at most;
  `tools/measure_rates.py`, which now measures both, found 7 and 10 of
  750 there, 54 ms at most, and a few from 3.2 MS/s up, which the
  half-second buffer absorbs while the mean stays under half the period
  (scoping.md, spike 7, for the tables). The
  decision stands, rates up to 8 MS/s offered and 10 MS/s not; a share of
  a core read on a system monitor while Radiolack runs is the paced one.
  Pull request 2's source therefore reports, as it runs, the blocks that
  took longer than their period and the samples its buffer dropped, an
  engine or a reader behind, beside the device's overflows (logged at
  most once a minute, with their count), so that a rate that a computer
  does not hold shows on that computer. 8 MS/s remains to be confirmed
  with a receiver at that rate, the RSPdx-R2, once pull request 2's
  source runs it.

### Pull requests

1. The spikes and their results.
2. The source, a stand-in `SoapySDR` module for the tests, and `--soapy`.
3. The Receiver menu: the devices found, Search again, the arguments field.
4. Gains (overall and per stage), antenna choice, antenna power.
5. I/Q recordings in the device's format.
6. Package, service notes, README.md, CI.

As built, pull request 2 (`soapy.py`, `--soapy`, the stand-in's
additions); pull requests 3 to 6 rely on these:

- **The module.** `SoapySDR` is imported when a SoapySDR source first
  opens its device, in its reading thread, and when `--soapy` is given.
  Missing, `--soapy` exits with status 1, saying that `python3-soapysdr`
  brings it, as `--rtl-sdr` does without rtl_tcp; a SoapySDR source
  reopened at start waits, with that text as its detail, which the
  Receiver menu and the status line show, and tries again, so that
  installing the package is enough. Radiolack starts, runs and passes its
  tests without it: within the tests, `import SoapySDR` finds the stand-in
  or nothing (`tests/__init__.py`), never the real module.
- **Arguments and identity.** `--soapy ARGS` takes SoapySDR device
  arguments that must name a driver (`soapy.parse_args`), so that no search
  runs every module's. The source searches with them and opens the first
  device found; from then on it searches by that device's serial number,
  and its settings key becomes `soapy DRIVER SERIAL`, its `reopen`
  `{"kind": "soapy", "args": "driver=DRIVER,serial=SERIAL"}`, an `index`
  given dropped. Since the key is known only once the device opens, the
  engine, told by the source's change, then restores the settings saved
  for that device, as at a switch, drops what it kept under the former key,
  and starts again a scan started meanwhile, such as `--scan`'s, from those
  settings (`Engine._identified`). The detail of a device not found names
  its driver alone, not its serial number nor the other arguments: it goes
  into problem reports.
- **Reading.** One thread per session, `radiolack-soapy`, alone calls the
  device: it opens it, reads what it offers, applies the settings between
  reads of the stream's MTU (100 ms time-out each) and keeps the samples in
  a `NativeBuffer`, which moved from `tools/measure_rates.py` to `soapy.py`
  and gained `take_now`, for the engine's read, which waits in the event
  loop for the thread's signal. After a change of rate, frequency or
  correction it drops 0.4 s, as the rtl_tcp client. The device not found,
  failing to open, without a usable format or rate, a stream that cannot be
  set up, ten stream errors in a row (an unplugged device) or 5 s without
  samples (a silent one) end the session: the source waits with the reason
  as its detail and tries again after 0.5 s, then twice as long each time
  up to 5 s. Nobody reading for 2 s ends it too, releasing the device. Its
  `finally`, whatever ends it, deactivates and closes the stream and
  releases the device; a close repeated, or coming during that stop, only
  waits for it (`wait_closed`, up to 5 s, then a warning).
- **Receive only.** The source names `SOAPY_SDR_RX` alone; the stand-in
  refuses any other direction and `writeStream`, and records the request,
  which fails the test even when the code caught the refusal; a test reads
  `soapy.py` for any transmit name.
- **Formats.** As the spikes found, with one addition: a driver outside the
  table whose device gives no CF32 reads its native CS8 or CS16 at the
  nominal full scale, which a warning says; a device with none of the
  three waits, saying so. The table goes by the driver that the device
  arguments name, not by the key that the device gives for itself, which
  modules write otherwise: the rtlsdr module's is "RTLSDR", and the first
  check on a receiver found the V3 read in CF32 for it, fixed since.
- **Sample rates**: those the device lists up to 8 MS/s, or, for a device
  that gives ranges only, the common ones within them (0.25, 1.024, 2.048,
  2.4, 3.2, 4, 5, 6 and 8 MS/s) for the menu, any rate within a range being
  accepted; at its first opening, the rate asked becomes the nearest the
  device offers. The source state gains `sample_rates_hz`, which the menu
  lists, and `driver`; the rtl_tcp sources send their usual seven rates and
  null, the simulated band an empty list and null.
- **Analogue bandwidth**: the nearest at or below 0.8 times the rate among
  the listed ones and the ranges' steps, the narrowest if all are wider,
  set with each new rate.
- **Correction.** A device declares one of its own by
  `hasFrequencyCorrection`, or by a `CORR` frequency component where the
  binding lacks that call. It takes the whole ppm by
  `setFrequencyCorrection` (or `setFrequency(..., "CORR", ppm)`), and each
  overall `setFrequency` then passes `{"CORR": ppm}` as its arguments:
  SoapySDR's default spreads the overall frequency across the components
  and would otherwise set the correction to the remainder. The label takes
  the device's correction to correct its sampling clock too, as librtlsdr's
  does; a device without one takes none, and Radiolack the whole ppm, by
  the frequency asked, the label and `rate_scale`. `grid_hz` is 0, and
  `driver_pll` and `tuner_grid` are null. Whether the rtlsdr module, the
  first checked, behaves so is for the owner's check on the V3.
- **Gain.** One gain stage: `gain_db` is that stage's, by name, with
  automatic gain where the device has a gain mode (its default then), or
  else the device's own gain at its opening. Several stages, as a HackRF's
  or an SDRplay's: the device's own gains, `gain_db` not settable and
  null, `gains_db` empty, until pull request 4: one overall value spread
  across stages would be a choice that pull request makes (made since:
  pull request 4, below).
- **`--bias-tee` with `--soapy`** is refused at parse time (status 2):
  the source has no antenna power yet, and a service started so would run
  without the power it asks for. A SoapySDR source reopened from the saved
  record with `--bias-tee` alone is ignored, saying so, as the simulated
  band is. Pull request 4 accepts it (below).
- **Reporting.** Device overflows, samples the buffer dropped and blocks
  that took the engine longer than their period (the time between one read
  that gave samples and the next, under 2 s, a longer one being a pause)
  are logged at most once a minute, with their counts since the previous
  line, and once more when the source stops. The device's timestamps are
  not used (module docstring).
- **I/Q recordings** are refused by the source's `tap`, which the engine
  calls before any file is written: the interface shows the refusal, and
  no file says the wrong format. Pull request 5 brings them (below).
- **The Receiver menu** before pull request 3: the entry "SoapySDR device"
  in the list, selected while a SoapySDR source is in use; its note, such
  as "R820T through SoapySDR, driver rtlsdr.", with "Waiting: ..." and the
  reason while it waits; the sample rates the device offers; the centre
  frequency, the correction, Calibrate, the converter and its oscillator
  as for any receiver; the gain slider for a device with one gain stage
  only. Choosing the entry from another source works once `--soapy` has
  named a device; before that the engine refuses, saying to start with
  `--soapy` (pull request 3 adds the list and the field). An interface older than this pull request shows no entry
  selected, an empty note and its own rate list, and otherwise works
  (protocol.md, Versions).
- **Not checked on a receiver**: everything above runs against the
  stand-in only. The owner's V3 through `driver=rtlsdr` checks the rest:
  the rtlsdr module's correction and its CORR component, its gain stage
  (`TUNER`) and gain mode, and 2.4 MS/s held without loss.

As built, pull request 3 (`soapysearch.py`, the devices state and the
`devices` message, `devices.js`); pull requests 4 to 6 rely on these:

- **The drivers searched**: `hackrf`, `airspy`, `airspyhf`, `lime` and
  `bladerf` (`soapysearch.LISTED_DRIVERS`), in that order, each alone with
  `driver=NAME`. `sdrplay` is not listed yet: it joins as the tuple's last
  entry, a one-line change with its test's expectations, once
  `tools/check_search.py sdrplay` has run on the owner's laptop; until then
  an SDRplay opens through the arguments field, `driver=sdrplay`. A search
  that names no driver or `rtlsdr` raises before reaching the module.
- **When a search runs.** Only when one of this computer's interfaces
  asks: the first time when the Receiver menu shows the SoapySDR part
  while no search has run since the engine started, then on "Search
  again". Never at start, nor at a connection, nor for a remote client:
  the searches talk to the devices plugged in (some modules open a device
  to read its serial number), so they run when someone looks at the list,
  not on their own. One search at a time, in a daemon thread
  (`radiolack-soapy-search`), its result handed to the event loop; a
  request while one runs is ignored, and the interface disables Search
  again meanwhile. A search waits for a source being released to finish
  closing; the engine's close stops waiting for it, up to 5 s, then warns.
- **While a SoapySDR device streams**, its driver is not searched again,
  and that driver's devices stay as the previous search found them: a
  module whose search opens its devices could disturb the one in use. The
  other drivers are searched. Opening a device, by the list, the arguments
  or the `soapy` entry, is refused while a search runs.
- **What is kept**: a device's driver, serial number and label, the label
  on one line and cut at 100 characters, a serial number of 64 characters
  at most and without `,` or `=`, or none; at most 64 devices, duplicates
  merged; nothing else of a module's listing, such as a bus position. The
  list lives in memory only. A driver's search that raises leaves the
  others' findings, and the state says `failed` with the driver and its
  module's message; without the module nothing is searched, `unavailable`
  with the text that names `python3-soapysdr`.
- **The device in use** heads the list, `in_use` true, whether a search
  found it or not (an RTL-SDR through `driver=rtlsdr`, for instance), with
  the label its listing gave when it opened, or its hardware key.
- **Choosing.** A device found opens by `driver=DRIVER,serial=SERIAL`,
  which `soapy.parse_args` checks and the engine saves as the `soapy`
  record, as `--soapy` would; the device then keeps its settings under
  `soapy DRIVER SERIAL`. The arguments field takes what `--soapy` takes,
  with its refusal texts, shown under the Receiver list as the source's
  refusals are. The same arguments, or the device in use, change nothing.
- **The Receiver menu.** A second list, "SoapySDR device", under the
  Receiver list, then "Search again" and the "SoapySDR device arguments"
  field, shown while a SoapySDR device is in use, or once "SoapySDR device"
  is picked in the Receiver list while none was ever chosen: the part then
  shows without switching anything until a device is chosen. Once one was
  chosen (the devices state's `chosen`), the entry opens it again at once,
  as before. The list, Search again and the field are disabled while a
  search runs.
- **Remote clients** receive the devices state, without the arguments in
  use, and may choose among the devices found, as they may switch the
  kind of source; neither search nor arguments: the server refuses
  `search` and `args` from them (`engine.LOCAL_DEVICE_FIELDS`), and their
  interface shows neither Search again nor the field.
- **Protocol**: version 6 stays (protocol.md, Versions). The devices state
  goes only to engines whose sources include `soapy`, after the recording
  state.
- **Not checked on a receiver**: the stand-in only. The owner's V3 through
  the arguments field's `driver=rtlsdr` checks that the field opens it and
  that the list shows it in use; the listed drivers' searches with real
  modules await their receivers.

As built, pull request 4 (gains, sockets, settings and antenna power in
`soapy.py`, the stand-in's devices, `advanced.js`); pull requests 5 and 6
rely on these:

- **Gains.** One stage: as pull request 2 built it. Several: `gain_db`
  becomes the overall gain, through SoapySDR's `setGain(RX, 0, value)`,
  which the device's module spreads across its stages, and
  `stage_gains_db` each stage's, by the device's names; after either, the
  reading thread reads back every stage and the overall gain, which the
  state then shows. A message that changes stages moves them, and the
  overall gain follows; the overall gain moves when no stage changed. At
  an opening, only the stages that differ from what the device holds are
  written. The overall range is the module's `getGainRange(RX, 0)`, or
  the stages' sum where the binding lacks it, offered in 1 dB steps unless
  it declares others. Automatic gain where the device has a gain mode, as
  for one stage. The gains are saved with the device; a saved stage that
  the module no longer declares is ignored.
- **The HackRF's amplifier**, the decision left to this pull request: a
  stage named in `soapy.HELD_STAGES`, `AMP` alone, starts at its lowest at
  every opening without saved gains, whatever the device's default, and
  only its own slider moves it. SoapyHackRF's overall gain turns AMP on
  above 51 dB, as its code was remembered, so on a device with a held
  stage the overall gain never goes through the module's spread: the
  source spreads the value less the held stages' gain over the other
  stages itself, each in the order the device lists them taking what the
  next ones leave, as SoapySDR's default spread does (`_overall`). This
  departs from "no distribution of Radiolack's own" for that case only: a
  first version that let the module spread the gain and then put AMP back
  wrote AMP on for a moment, which the stand-in's log of writes showed.
  The overall gains offered then run from the held stages' gain to the
  other stages' highest plus it: 0 to 102 dB with AMP off, 14 to 116 dB
  with it on. Whether SoapyHackRF spreads as remembered, and lists its
  stages as LNA, AMP and VGA with those ranges, is for the owner's check.
- **SDRplay's stages**, `IFGR` and `RFGR` in SoapySDRPlay3 as remembered,
  are gain reductions: the Advanced section shows them under those names,
  where more is less gain. What the module makes of an overall gain, which
  it may take as a reduction or spread otherwise, is to read on the
  RSPdx-R2, as are its stages' ranges and its settings' keys and types.
- **Antenna sockets**: `listAntennas` with more than one name becomes
  `antenna` and `antennas` (16 at most), the device's own choice until
  changed, then saved; one socket or none shows nothing.
- **Settings.** `getSettingInfo()`, the device's, then
  `getSettingInfo(RX, 0)`, the receive channel's, become
  `device_setting_info` and `device_settings`, 32 at most: a boolean is a
  box; a setting with options, a choice, kept only when every option is a
  printable name of 64 characters at most, named by `optionNames` or by
  its value; an integer or a float with a range, a number on its steps. A
  free text without options is left out, the decision left to this pull
  request: the module parses it as it likes, so no check of Radiolack's
  can make it harmless, and none of the three modules checked declares
  one that matters; a number without a range is left out too. A setting
  named for transmission (a word of its key or name starting with `tx` or
  `transmit`) is never shown nor written, nor anything of a transmit
  channel; antenna power never shows there. Names and descriptions are
  cleaned of control characters and cut at 64 and 300 characters, and
  the interface sets them as text. Each starts at the device's value,
  read at its opening; the state, and so the saved record, holds every
  value the device shows, and a later opening writes those that differ.
  The reading thread writes a change between two reads, as text
  (`true`/`false`, the option, the number), reads it back and keeps what
  the device gives; a refusal keeps the value the device holds and shows
  in `device_refusal`, which the Receiver menu's note shows as the other
  refusals, until the next change. A key or a stage that the device does
  not declare is ignored once the device is known, so that a saved record
  outlives a module's update; before, anything of a JSON type passes,
  which the opening fits.
- **Antenna power.** A setting whose key or name, lower case with its
  letters and digits alone, holds `bias`, `antennapower` or `antpower`
  (`soapy.is_antenna_power`; wide on purpose, erring towards power) is
  antenna power: `biastee` (RTL-SDR), `bias_tx` (HackRF, which powers the
  socket in reception too) and `biasT_ctrl` (SDRplay). Off is a boolean's
  `false`, a choice's option that says off (`off`, `0`, `false`, `no`,
  `disabled`), or 0 within a number's range, on likewise; one whose off
  value cannot be told is left as it is, with a warning, and never
  written. Every such setting is written off as soon as the capabilities
  are read, before the stream is set up, and in the session's `finally`
  before the stream and the device are closed: at a close, when nobody
  reads, after an error, an unplugged device included. `bias_tee` is on
  only when asked: the interface's second press, or `--bias-tee`, now
  accepted with `--soapy`, which the engine gives the source before the
  device opens and the source applies once it does, or ignores with a
  warning for a device without antenna power. Asked since the start, it
  comes back on at the next opening, after the off. Never saved: the
  engine's `UNSAVED_SETTINGS`, and a `biastee` in a saved record's
  `device_settings` is dropped as an unknown key. Several such settings,
  the other decision left here: all are off at the opening and at the
  release, and `bias_tee` drives one, the receive channel's before the
  device's, one not named for transmission before one that is, then the
  first declared; the log names the others.
- **Shortwave input**, the decision left here: an RTL-SDR's
  `direct_samp` stays a choice in the Advanced section (Off, I-ADC,
  Q-ADC) rather than becoming `hf_input`. The shortwave input's rules (the
  centre brought within the new reach, the RTL2832's AGC, the tuner's gain
  greyed, the range below the tuner's) rest on what librtlsdr does behind
  rtl_tcp; through the module they would depend on how SoapyRTLSDR
  declares its range and treats `setFrequency` in direct sampling, which
  nothing here can read: out of proportion for a receiver that has the
  rtl_tcp entries with the whole behaviour. The source reads the range
  once, at opening: whether the module's range lets a centre below
  24 MHz be asked while `direct_samp` is on is for the owner's check, and
  until then shortwave on an RTL-SDR goes through those entries.
- **Protocol**: version 6 stays (protocol.md, Versions): the new fields
  are optional, an older interface leaves out the settings it does not
  know and ignores the fields, and its gain slider and antenna power
  button keep working, the second press naming an RTL-SDR's 4.5 V.
- **Remote clients** receive the same state and may change the gains, the
  socket, the settings and the antenna power, as they may an RTL-SDR's:
  `engine.LOCAL_SETTINGS` stays `address` alone.
- **The Receiver menu**: "Antenna socket" under the converter for a
  device with several; the Advanced section at the menu's bottom, one
  slider per stage then the settings, each with its description as a
  note; "Tuner gain" for the overall gain; the antenna power's second
  press says "Confirm: power on the antenna socket", without a voltage.
  `advanced.js` (typed) holds the view and builds the controls, made anew
  only when the device's controls change, their values written only where
  they differ; `main.js` wires them.
- **The stand-in** declares stages with ranges, steps and gains at
  opening, an overall spread (SoapySDR's default or a module's), a gain
  mode, sockets, settings of each type, the device's and the channel's,
  refusals, and the order of writes and closes (`events`). Its three
  shapes: `hackrf_one`, `rspdx_r2` and `rtl_sdr`. The RTL-SDR's settings
  are those SoapyRTLSDR 0.3.3 declared on the owner's V3 (keys, names,
  types, values and options; one antenna, `RX`; driver key `RTLSDR`),
  their descriptions and `direct_samp`'s option names as the module's code
  was remembered; the HackRF's and the RSPdx-R2's are remembered, to be
  read on the devices.
- **Unchanged**: the order of the first settings at an opening, sample
  rate, correction, then frequency, for which librtlsdr prints "[R82XX]
  PLL not locked!" twice, harmlessly, as the tuner is set before its
  first frequency.
- **Not checked on a receiver**: the stand-in only. The owner's V3
  through `driver=rtlsdr` checks the TUNER slider and automatic gain, the
  settings that show (direct sampling, offset tuning, I/Q swap, digital
  AGC) and that `biastee` is the antenna power, off at opening, on at the
  second press, off at release; the HackRF and the RSPdx-R2 the rest when
  they come.
- **Left for later**: I/Q recordings in the device's format (pull request
  5); the package, the service notes and CI (pull request 6); `sdrplay`
  among the searched drivers. The "Tuner AGC" box is enabled for a
  SoapySDR device without a gain mode, whose engine then refuses
  automatic gain, saying so, as since pull request 2: the state does not
  say whether a device has one.

As built, pull request 5 (I/Q recordings in the device's format:
`soapy.py`'s tap, `recordings.py`'s formats, the engine's `raw_format`);
pull request 6 relies on these:

- **The tap.** The reading thread hands the tap the samples it keeps, as
  the device gives them, in the format the source reads it in, before the
  DC removal and the conversion to complex64: at the same moments as
  rtl_tcp's tap, nothing while the device settles after a change (0.4 s),
  then the new label's centre in use, on the air, and sample rate. It
  copies each kept read into a queue and posts one drain at a time to the
  event loop, which hands the samples to the engine: the engine's
  recording code runs in the event loop, as for rtl_tcp, and its contract
  (`data, centre_hz, rate_hz, joined`) is unchanged. The thread never
  waits for the disk nor for the event loop: once more than a second of
  samples (`soapy.TAP_S`) waits for the event loop, a read's samples are
  not queued, counted in the losses' line ("samples not recorded, the
  event loop behind"), and the next start a new capture. A gap, and so a
  new capture, also follows a new session (an unplugged device back), a
  failed read or an overflow, a new label (retune, correction) and a new
  tap. A tap that raises is left, logged, and the source goes on.
- **Disk falling behind.** The recording's thread writes as soon as
  4 MiB wait (`WRITE_BATCH_BYTES`), so that at 32 or 64 MB/s it does not
  wait for its 1 s tick; the engine stops the recording, `disk`, "the
  disk did not keep up", once more than 2 s of samples at the recording's
  rate and format (`IQ_PENDING_S`), and at least 64 MiB, wait: 128 MiB at
  64 MB/s. Memory stays bounded by that, and the reading thread is never
  slowed: the device's samples, the spectrum and the audio go on.
- **Free space** is checked before each write, against the batch about to
  be written: a write that would leave less than 2 GiB free is not made,
  and the recording stops, `space`. The former check, every second
  whatever was written, could overshoot by a second of samples, 64 MB at
  the highest rate, and more if the thread fell behind; it stays for a
  recording that writes nothing meanwhile.
- **The format** is SigMF's: `ci8` for CS8, `ci16_le` for CS16, `cf32_le`
  for CF32 (`_be` on a big-endian computer, as the samples are the
  machine's), set by the first samples handed (`raw_format`, which the
  source sets before each call) and fixed for the recording: samples in
  another format end it, `receiver`, as another receiver does. rtl_tcp's
  stays `cu8`, its tests as they were.
- **The full scale**, the decision left here: SigMF's core has no field
  for it, and a description in free text is read by no program. A device
  whose integers stop short of their type's full scale (`FULL_SCALES`:
  SDRplay and Airspy at 32767, bladeRF at 2048) gets
  `radiolack:full_scale` in the global object, declared in
  `core:extensions` as `{"name": "radiolack", "version": "1.0.0",
  "optional": true}`, as SigMF asks of any namespace other than `core`:
  readers that do not know it may ignore it, and the samples themselves
  are never rescaled, which would lose the device's bits. A format at its
  type's full scale (CS8 at 128, CF32 at 1.0) writes neither field.
- **Behind an inverted converter** the tap swaps I and Q, as rtl_tcp's
  does, so that the data holds the band as it is on the air, as the
  captures' frequencies are; a swap loses nothing in any format.
- **Names**: unchanged, start, centre frequency and sample rate. The
  format stays out of the name: the metadata gives it, and a name that
  changed with the format would break the list's and the downloads'
  rules for no reader that needs it. The list reads `core:datatype` from
  the metadata's first 64 KiB to compute an I/Q recording's length; a
  recording without readable metadata, or whose datatype the engine does
  not know, lists its size without a length (formerly the length took
  `cu8`).
- **`core:hw`** is the source's `hardware`: "SoapySDR DRIVER", the
  device's hardware key, and the converter, never a serial number nor an
  address. The captures' `core:datetime` is taken when the event loop
  hands the samples, within milliseconds of their arrival.
- **Refusals**: the source's `tap` no longer refuses. The simulated band,
  and a recording at start, stay refused as before. A recording started
  before the device opens takes the sample rate asked, which the opening
  may fit to another: its first samples then end it, `sample_rate`.
- **Protocol**: version 6 stays. No message changes: an I/Q recording's
  format is in its metadata, and the list's `seconds` comes from it.
- **Tests**: `tests/sigmf_check.py` checks every recording the tests
  write against SigMF 1.0.0's rules for the fields Radiolack writes,
  without a dependency; the soapy tests compare each format's data, bit
  for bit, with the samples the stand-in gave.
- **Not checked on a receiver**: the stand-in only. The owner's V3
  through `driver=rtlsdr` checks a `ci8` recording at 2.4 MS/s, the
  RSPdx-R2 a `ci16_le` one with its full scale, the HackRF its `ci8` at
  8 MS/s and what the disk does at 16 MB/s.

### Done when

On the owner's laptop, the V3 opened through SoapySDR tunes, plays and
scans as it does through rtl_tcp. With the RSPdx-R2: Radiolack lists it,
tunes from shortwave to 2 GHz, chooses among its antenna sockets, plays a
shortwave broadcast station on the owner's loop with less hiss than the
V3's direct sampling gives, shows its other settings in the Advanced
section, and never powers an antenna socket unasked. With the HackRF One:
Radiolack lists it,
tunes from 1 MHz to 6 GHz, plays a broadcast station, shows a signal above
2 GHz, sweeps a band, records I/Q that opens in another program at the
right frequency, runs as the service with `--soapy`, and never transmits.

### Limits

- Only the RSPdx-R2, the HackRF One and the V3 are checked on hardware;
  other devices work as far as SoapySDR and their modules do.
- SDRplay's module, SoapySDRPlay3 (MIT), needs SDRplay's own closed
  library, its API, version 3.15 or later for an RSPdx-R2 (read on the
  module's page on 2026-11-18), which Radiolack neither ships nor installs:
  the user installs SDRplay's API from SDRplay and builds the module.
  SDRplay tests its Linux installer on Ubuntu's long-term releases only.
- The HackRF's crystal is ordinary: the correction and its calibration
  matter more than with a V3.
- Network devices (SoapyRemote, USRP, PlutoSDR over IP) open through the
  arguments field only.

## 2. Listening tools

Goal: what shortwave listening lacks after Coverage and precision: a zoom
that goes on to a few hertz per bin, synchronous AM, a notch and a noise
blanker. The notch and the blanker come back to the Radio menu, which
dropped them as dead controls on 2026-11-15.

### Decisions

- **The zoom goes on**: below the widest spectrum's limit, the spectrum
  and the waterfall pass to a fine spectrum of the VFO's channel, without a
  panel of its own, as the owner chose over an inset.
- The notch is automatic, one box. The blanker is one box.

### Design

- **Fine spectrum.** The engine computes an FFT of 8192 bins of the VFO's
  channel, before the demodulator: 5.9 Hz per bin at the channel's 48 kHz
  (39 Hz in WFM), about ten frames a second, each the mean of those since
  the last. It sends them, as a binary frame of a new type with the
  spectrum frames' header, to the interfaces that asked
  (`{"type": "fine", "on": true}`), and computes nothing when none did.
  Paired devices get 2048 bins, each the highest of those it groups.
- **Deep zoom.** Once the view is narrower than 48 bins of the wide
  spectrum and lies within the fine spectrum's cover, 0.4 times the
  channel's rate on each side of the VFO (19.2 kHz), the interface asks for
  the fine frames and draws them in the spectrum and the waterfall. The
  finest view becomes 16 fine bins, 94 Hz. Panning past the cover, or
  zooming out, returns to the wide frames. The waterfall keeps a history
  for each, at its own resolution; the fine one starts empty and realigns
  when the VFO moves, as the wide one does when the span moves. The cover's
  edges take the lit window's decor, and Display's resolution line says
  which spectrum shows.
- **SAM**, a mode beside AM with AM's filter widths: a band-pass 80 Hz
  wide follows the strongest line within 1 kHz of the VFO, the channel is
  turned by that carrier's phase, and its real part is the audio. Under
  10 dB of carrier over the noise it detects the envelope, as AM, and the
  state says that it has no lock. It is computed block by block, without a
  loop per sample. The band plan still names AM: SAM counts as AM for the
  mode that the plan chooses, so tuning within AM ranges keeps SAM.
  Bookmarks and recordings take it.
- **Notch**, in AM, SAM, USB and LSB: in the audio, lines at least 15 dB
  over their surroundings, at most 30 Hz wide and steady for half a
  second, between 100 Hz and 5 kHz, are removed, four at most, through the
  audio filter's response, and released a second after they stop.
- **Noise blanker**, on the samples that go to the VFO's channel, never on
  the display, which shows what is received: samples over six times the
  block's median magnitude are zeroed, with two samples on each side.
- The demodulation state gains `notch`, `blanker` and SAM's lock; they are
  saved with the receiver's settings, and shared by every interface, as the
  mode is.

### Spike, first pull request

Each tool on the owner's laptop at 2.4 MS/s. Together they may add 6% of
one core, 1.5% each, as the planning session set the shares. A tool over
its share is reworked once; still over, it is kept and its measured cost
documented, as the owner decided on 2026-11-16 ([`scoping.md`](scoping.md),
Decisions).

Measured on 2026-11-17 ([`scoping.md`](scoping.md), spike 8, with the
tables), with `tools/measure_listening.py` and the prototypes of
`tools/listening.py`, which pull requests 2 to 5 start from: on the
owner's laptop (Intel Core i7-7500U, Python 3.11, NumPy 1.24), the engine's
processing of one receiver at 2.4 MS/s, the VFO in AM, takes 12.5 to 13.9%
of one core. Added to it, the median of seven runs:

| Tool | First version | After its rework | Rule |
| --- | --- | --- | --- |
| Synchronous AM | 2.14% (1.48 to 3.45) | 1.00% and 1.01% | reworked once, within its share; 1.58% in a third, busier measure, kept with that cost |
| Automatic notch | 0.46% | 0.43% and 0.37% | within its share |
| Noise blanker | 4.60% (3.50 to 5.95) | 1.40% and 1.54% | reworked once, at its share: kept, with this cost |
| Fine spectrum | 0.52% | 0.36% and 0.42% | within its share |
| The four together | 7.90% | 3.16% and 3.27% | within 6% |

The first version's figures are those of a measure made under the same
load as the reworked version's two (load average about 2 on four logical
processors); a measure made earlier under heavier load (about 7) gave
2.93% and 5.83% for the two tools reworked, against a baseline of 18.9%.

As built, where the prototypes left a choice open or showed a point of
this section wrong; pull requests 2 to 5 rely on these:

- **SAM holds its carrier.** "The strongest line within 1 kHz" finds the
  carrier, but cannot follow it: in a selective fade, where the carrier
  falls below its sidebands, a sideband's steady line outgrows it, and the
  prototype jumped to it. Once locked, the search keeps to the band-pass,
  40 Hz on each side of the carrier followed; it searches the whole 1 kHz
  again only without a lock.
- **SAM's lock.** Every 0.1 s, an FFT of 4096 samples of the channel
  (11.7 Hz per bin at 48 kHz, Hann): the carrier's power is that of the
  five bins of its main lobe, less the noise in them; the noise is the
  median of the bins within 1 kHz over ln 2, scaled to 80 Hz. Under 10 dB,
  no lock, and the envelope as AM, the switch made at a block's start; a
  short crossfade there is for pull request 2.
- **SAM's band-pass is three moving averages in cascade**, of 439 samples
  each at 48 kHz, by cumulative sums: half gain at 40 Hz, at least 39 dB
  down from 100 Hz, where AM's audio starts, a lag of 657 samples. A Kaiser
  filter of 1441 taps through `dsp.FftFilter`, the first version's, took
  more than half of SAM's time and put it over its share. Within AM's audio path
  delay of 720 samples (10 ms of squelch and a 5 ms fade), the rest is a
  plain delay: SAM keeps AM's delay, and the squelch's gate stays in step.
- **SAM's audio**: the real part of the channel turned by the carrier's
  phase, less the carrier's amplitude, over a level that follows the
  carrier's mean amplitude at once when it rises and falls at the SSB
  AGC's 10 dB a second. Unfaded, it plays as loud as AM; a carrier that
  fades alone leaves the sidebands' audio as it is, where the envelope
  folds.
- **A notch through the audio filter's response is wider than the line.**
  The audio filter's transition is 60 Hz: a zero 30 Hz wide in its
  response would take a few decibels off the line. Each notch zeroes the
  line's 30 Hz and half the transition on each side, 90 Hz in all, which
  removed a whistle by more than 50 dB and a voice-like sound around it by
  less than 0.1 dB on test signals (the tests ask for 20 dB and 1 dB). The filter keeps its length, so a notch
  costs no filtering; computing the taps takes about 2 ms, when a notch
  comes, goes or follows its line by 5 Hz or more. `demod.audio_taps`
  gains the notches.
- **The notch reads the detector's output**, at 48 kHz before the gate and
  the audio filter, so that a notched line is still seen while it lasts:
  every 0.1 s, an FFT of 8192 samples (5.9 Hz per bin, Hann). "At most
  30 Hz wide": the line's level falls by 15 dB within 17.6 Hz on both
  sides; "its surroundings": the median of the bins within 300 Hz beyond
  that; "steady": found within 9 Hz at every analysis for half a second.
  The release comes 1.0 to 1.2 s after the line stops, as the analysis's
  0.17 s still holds it for a while. On AM's envelope a heterodyne gives a
  second, weaker line at twice its pitch, which is notched too.
- **The blanker's median is taken from every 16th sample**, 5000 of a
  block at 2.4 MS/s, which estimates the block's median within about 1% for
  noise; the impulses are sought in the samples' real and imaginary parts,
  and only those over the limit over the square root of 2 have their
  magnitude computed. A whole block's median took most of the first
  version's time. The figure is the worst case, impulses in every
  block (100 a second): a block without them is given back as it is,
  without a copy. An impulse at a block's start does not reach back into
  the previous block, two samples (0.8 µs) at most. It acts on the samples
  that the demodulator receives, after the display's FFT has read them, so
  that Calibrate's carrier measure, which taps the channel, reads the
  blanked samples too.
- **The fine spectrum reads the front filter's output**, the 48 kHz before
  the channel filter: the channel that Calibrate's tap gives is the mode's
  passband only. The demodulator gains a second tap. The cover's edges,
  19.2 kHz from the VFO, lie in the front filter's transition: flat to
  16 kHz, 0.8 dB down at 18 kHz and 3.1 dB at 19.2 kHz. Kept as designed:
  the lit window's decor marks the edge, and narrowing the cover to 16 kHz
  would shrink the deep zoom for a level that only the last 3 kHz lose.
  In CW the channel's 0 Hz is the VFO less the CW pitch, which the frames'
  labels take into account.
- **The fine spectrum's FFTs**: Hann, not the wide spectrum's
  Blackman-Harris, whose main lobe, 4 bins (23 Hz) on each side, merges
  two carriers 20 Hz apart, which Hann shows apart. One FFT every 4800
  samples at 48 kHz, overlapping, each a frame; at WFM's 320 kHz one every
  8192 samples, a frame the mean of about four. At 320 kHz the prototype
  alone took 0.7% of one core, against 0.2% at 48 kHz; the engine's
  processing was measured in AM only.

As built in pull request 2, SAM, where it departs from the prototype or
settles what it left open:

- **SAM lives in `demod.py`**: `CarrierSearch` finds the carrier and holds
  the lock, `SyncDetector` follows it, `MovingAverages` is the band-pass;
  `tools/listening.py` keeps no copy, and `tools/measure_listening.py`
  measures SAM as the VFO's mode, which puts it where the prototype was,
  in place of AM's detector, with the search on the channel.
- **The search belongs to the demodulator**, not to the detector, and runs
  in SAM whether or not an interface asked for audio, so that every
  interface learns the lock: an FFT of 4096 samples every 0.1 s.
- **A lock gained waits for the band-pass.** Before a lock, the turn runs
  at the latest carrier found, 0 Hz at first, and the band-pass holds what
  that leaves of the carrier; the carrier found takes the band-pass's span,
  1315 samples (27 ms), to fill it. The prototype switched at once, and on
  a test station its audio leapt to 27 times full scale, before the audio
  filter, in the block after its first lock. SAM now gives the envelope
  until the span has passed, a block at 2.4 MS/s.
- **The level after a lock gained** starts from the larger of the
  carrier's mean amplitude and the channel's, which is AM's level, then
  follows the carrier as designed. The prototype's level rose across the
  block from what the band-pass held before the lock, too loud for that
  block; and a lock gained during a fade of the carrier alone would start
  from the faded carrier, far louder than AM.
- **The switch between the detectors** fades linearly over 5 ms, the
  squelch's fade, from one output to the other: both have AM's delay, so
  they are in step, and the audio moves no more from one sample to the
  next than either output does, plus their difference spread over 240
  samples. A test switches by hand in a selective fade, where the two
  differ by more than 0.2 of full scale, and checks that bound and that
  the step stays under a third of their difference.
- **The lock goes by half a second, and holds a little lower.** One
  search's margin scatters by 1 to 2 dB around the carrier's: by the
  design's 10 dB alone, search by search, a carrier at 10 dB gained and
  lost the lock five times a second, and one at 12 dB once a second. The
  lock goes by the mean of the latest five searches' margins, gained from
  10 dB and lost under 7 dB, which leaves under one change a minute from
  8.5 to 12 dB. A lock is then gained 0.2 s after a carrier comes. The
  demodulation state follows, and goes to the interfaces only when it
  changes.
- **A lock holds through a fade** (after 0.15.0, on real stations). On
  shortwave broadcast stations, on 2026-11-18, a V3 in direct sampling on
  a small loop: locked without a change on strong stations, but on one
  whose carrier fades around the lock's level the mean alone gained and
  lost the lock 52 times in 90 s. A lock is now lost only once that mean
  stayed under 7 dB for 15 searches in a row, 1.5 s, about 2 s after a
  carrier goes, and through a weak search the carrier stays where it was
  found. In alternate runs of 25 s on four stations, the changes fell from
  55 to 14, and to none on the station that had 10 and 6. A synthetic
  carrier fading across the level every other second reproduces it in the
  tests.
- **The plan's AM.** The interface's `planMode` (bandplan.js) gives SAM
  where the plan names AM and the VFO is in SAM: a range tuned into, a
  known frequency or a channel pressed, and the mode of a bookmark added
  from the plan, the History or the spectrum's menu.
- **Its cost** is the prototype's. The measure's tool, run before and
  after the move on the owner's laptop under a far heavier load than
  spike 8's, its baseline about twice as long, gave SAM 2.35% of one core
  over a baseline of 27.17% before (load average 4.2, 9.0 at the end) and
  2.94% over 25.76% after (8.5, then 7.3): a twelfth, then a ninth. The
  other tools, unchanged, rose as well (the four together from 7.63% to
  8.52%), and the detector with its search, timed in turn with the
  prototype's on the same channel, takes the same time within 1%: the
  difference is the load's.

As built in pull request 3, the notch, where it departs from the prototype
or settles what it left open:

- **The notch lives in `demod.py`**: `AutoNotch` is the prototype's,
  which now says whether its notches changed, and `audio_taps` takes the
  notches; the audio path holds the notch, reading its detector's output
  before the squelch's gate. `tools/listening.py` keeps no copy, and
  `tools/measure_listening.py` measures it as the VFO's setting.
- **Where the taps are computed**: in the audio path, in the worker thread
  that processes the engine's block, never in the event loop. A change
  takes 3 to 5 ms there with the fade below, measured on the owner's
  laptop, against a block of 33 ms of signal at 2.4 MS/s and the 150 ms
  that each interface keeps buffered: it never stalls the audio.
- **No click at a change.** The filter keeps the samples it remembers, so
  new taps would take over between two samples and the audio would step by
  what they remove, up to the line's amplitude. A copy of the filter keeps
  the old taps for 5 ms, the squelch's fade, and the audio fades linearly
  from its output to the new one, in step: from one sample to the next it
  moves no more than either does, plus their difference, the removed line,
  spread over 60 samples. A test checks that bound at a change and at the
  notch turned off, against the same taps switched at once.
- **The setting and the state.** `notch`, off by default, is a
  demodulation setting, saved with the receiver's and kept across modes,
  acting in AM, SAM, USB and LSB only; a bookmark carries only its mode, as
  before. The state's `notch_lines` is the number of lines removed now
  while the notch acts, null otherwise: enough to tell the user that the
  notch works on what is heard, and it changes at most when a notch comes
  or goes, while the frequencies would change each time one moves. It
  counts only while audio is made, for an interface or a recording,
  since the notch reads the audio path.
- **"The strongest" is the highest over its surroundings**, the margin the
  analysis measures, not the line's level: a weaker whistle in a quiet part
  of the audio counts before a stronger one inside the voice. A notched line
  keeps its notch while it lasts, whatever comes after.
- **Steady means found at every analysis.** A single miss starts the half
  second again, which keeps keyed tones (Morse from 12 to 30 words a
  minute), tones held a quarter of a second, a vibrato and beeps of 0.4 s
  out of the notch, as the tests check. The cost: a voice's harmonic that
  crosses a whistle during its first half second makes it wait a little
  longer, 1.1 s from its start in one test against 0.6 to 0.7 s otherwise.
- **Calm at the threshold.** A line found by about a quarter, half or three
  quarters of the analyses changed the taps once a minute at most, in nine
  minutes of noise: gained only after half a second found every time,
  held while it is found once a second, and read before the filter, the
  notch neither comes and goes nor moves.
- **Its cost.** The measure, run before and after the move on the owner's
  laptop under a light load (load average 0.7 to 1.4), gave the notch
  0.32% of one core over a baseline of 10.64% before, and 0.48% over
  11.13% and 0.50% over 10.89% after: a thirtieth, then a twenty-second of
  the baseline, against spike 8's 2.9 to 4.1%. Timed in turn on the same
  blocks, the prototype added 0.42% and the package's notch 0.47%, over
  10.41%: the fade, a copy of the filter and one block filtered twice at
  each of the scene's four changes in ten seconds, adds about 0.05%, and
  the rest of the difference between the measures is theirs.

As built in pull request 4, the noise blanker, where it departs from the
prototype or settles what it left open:

- **The blanker lives in `demod.py`**: `NoiseBlanker` is the prototype's,
  with the two bounds below and a count, and the demodulator runs it on
  the samples it receives, after the display's FFT has read them, where
  the prototype was measured, while the demodulation settings' `blanker`
  is on. `tools/listening.py` keeps no copy, and
  `tools/measure_listening.py` measures it as the VFO's setting, and
  without the scene's impulses with `--without-impulses`.
- **Every mode, WFM included.** Nothing argued against WFM: broadcast FM's
  envelope is steady and never crosses six times the median, and a
  station's neighbours in the span cross it no more. On impulses over a
  weak signal the tests take the audio's error down by more than 15 dB in
  each of the seven modes (21 to 60 dB on the test signals), while a
  signal without impulses comes out the same sample for sample.
- **A strong signal is not an impulse.** The rule alone blanks a strong
  signal that fills less than half a block, whose median is then the
  noise's: a carrier keyed on during a block lost every sample it had
  there, up to 16 ms. Two bounds, which the tests check on a carrier keyed
  abruptly 7 ms in 23 at 34 dB over the noise, an AM station at full
  modulation, with a tone and with a voice-like sound, and a broadcast FM
  station with three neighbours, none of which loses a sample: samples
  over the limit whose zeroed spans touch make one impulse, and one that
  spans more than 16 samples is a signal, left as it is; and a block where
  the impulses would zero more than 2% of its samples, or more than one
  impulse's reach in a block of fewer than 1000, is left whole. The
  fraction blanked is then at most 2% of a block, plus the two samples
  that an impulse at the end of the block before reaches; a strong signal
  that starts within the last 16 samples of a block, or ends within the
  first 16 of one, loses those and two more, 7.5 µs at 2.4 MS/s.
- **Across blocks**, as the prototype: an impulse at a block's end zeroes
  the next block's first two samples; one at a block's start does not
  reach back into the block before, which the channel's filters took
  already. Reaching back would delay every block by two samples, and copy
  every one, where a block without an impulse is not copied.
- **Calibrate's measure and the driver's check take the channel
  unblanked.** Where the prototype sat, they read the blanked samples. A
  measure must not change with a listening setting: blanking leaves a
  carrier where it is, but impulses raise the noise within the filter, and
  blanking them raised a measured margin by about 9 dB in the tests,
  enough for the same carrier to pass the measure's 20 dB with the blanker
  and fail it without. While the blanker is on and a measure or a check
  runs, the demodulator keeps copies of its front and channel filters,
  made at the first block blanked since the measure began, which filter
  the samples as received for the measure: a second pair of filters for
  the measure's few seconds, or the check's ten. Until a block is blanked
  the measure takes the channel itself, which is then the same. The tests
  check the measure's channel bit for bit against the blanker off, from
  the block after the copies' first (that block's filters remember the
  blanked block before the measure, a few hundred microseconds), and
  through the engine a measure's margin and every point of a check
  within 1 dB and 1.5 dB of the measure without the blanker, while the
  blanked channel's would be 8.7 and 9 dB higher. (Since pull request 5
  the front filter's copy is a filter of the samples zeroed alone, and the
  measure's channel the same within rounding: below.)
- **What takes the blanked channel**: the channel level, the S-meter and
  the squelch, which crackle no longer opens; SAM's search, the notch's
  analysis, the audio and its recordings. The memory scan holds on a
  channel while the VFO's squelch is open, which crackle then no longer
  holds; its search of the channels, as detection and the display, reads
  the spectrum of the samples as received, as do the I/Q recordings.
- **The state's `blanker_impulses`**, null while the blanker is off, is
  the impulses zeroed in the latest second of samples, the whole blocks
  that make it up scaled to a second: a count rather than a flag, which
  tells the user how thick the crackle is and that the blanker catches
  it, and a second rather than a block, so that the state changes at most
  once a second. It is found with or without audio, as the blanker acts
  on the channel itself. Over 60 s of samples at 2.4 MS/s through the
  demodulator, impulses every 10 ms, the measure's, changed it once, from
  0 to 100, and impulses every 7.3 ms, out of step with the seconds,
  once too, from 0 to 137: the count scaled to a second rounds away the
  impulse more or less that a second's blocks catch. Random crackle can
  change it every second, never more often.
- **Its cost** is spike 8's. The measure, run on the owner's laptop
  before the move under a light load (load average 0.5 at the start, 1.3
  at the end), gave the prototype 1.24% of one core over a baseline of
  11.28%, an eleventh; after it (0.4, then 1.5), the package's blanker
  1.38% over 11.02%, an eighth, against spike 8's 1.40% over 12.54% and
  1.54% over 12.71%. The impulses' grouping and the count add little to
  the prototype's work, within what the runs spread. Without the scene's
  impulses, the usual case, the blanker added 0.90% over 10.96% (load
  average 1.5 to 1.6), a twelfth: the median and the search for impulses
  remain, the copy of the block goes. The tables are in the pull
  request.

As built in pull request 5, the fine spectrum in the engine and the
protocol, where it departs from the prototype or settles what it left
open, and what pull request 6 needs to draw it:

- **The fine spectrum lives in `fine.py`**: `FineSpectrum` is the
  prototype's, which starts again at another key, and `encode_fine`
  makes the frame. `tools/listening.py`, which held no other prototype
  any more, is gone (the history keeps it), and
  `tools/measure_listening.py` measures the package's fine spectrum as
  the engine gives it a block, coded and encoded as the frames are.
- **Computed only while asked**: the engine makes the spectrum when the
  first interface asks (`{"type": "fine", "on": true}`), drops it with its
  partial mean when none does, and the demodulator's `front_tap` exists
  only then; without it a block's processing is unchanged, filter for
  filter, as a test counts. One spectrum serves every interface, with one
  sequence; a remote client's frames take 2048 bins, each the highest of
  the four it groups, and it gets every frame, about 0.17 Mbit/s. A slow
  client keeps the latest two.
- **A frame never mixes two settings.** Each block gives the spectrum a
  key: the count of the source's changes (a switch of source included),
  the VFO's frequency and mode, DC removal, the samples' centre in use
  and rate, and `rate_scale`. Another key drops the samples and the
  partial mean and starts again; and as the block is processed in a
  worker thread, its frames are dropped if the engine's setting changed
  meanwhile, or the spectrum was dropped and made again. The filter width
  is not in the key: the front filter does not depend on it.
- **The samples as received, cheaply.** With the blanker on, the
  prototype read the blanked front. A copy of the front filter fed the
  samples as received, as pull request 4 did for the measure, took 5.2%
  of one core over the blanker alone, as much as the front filter itself,
  which is half the engine's processing: over the fine spectrum's 1.5%.
  Reworked once: the filters are linear, so the samples as received
  filter to the blanked samples' output plus that of the samples zeroed
  alone, a few in a block. `FftFilter.process_sparse` filters those: only
  the segments that read one, and of each only the bins that the output
  takes, by a direct transform while the samples are few (an FFT beyond
  that), the same within rounding as `process` on the samples with their
  zeros, and the blanker gives the indices it zeroed. Calibrate's measure
  and the driver's check take it too, for their channel: the same within
  rounding as the blanker off, no longer bit for bit, which the tests now
  check to a millionth of the largest sample and a carrier to a
  microhertz; a fine frame's codes may differ by one in a few bins in ten
  thousand, a level on a code's edge.
- **Its cost.** Measured on the owner's laptop, seven runs each, under a
  light load (load average 0.6 to 1.1); spike 8's figures above stay as
  they were. Before the move, the prototype added 0.29% of one core over
  a baseline of 10.46%, reading the blanked front, and the four tools
  2.79%. After it, with the copy of the front filter, the fine spectrum
  added 0.40% over 10.45%, and 5.20% over the blanker alone with the
  blanker on, the four 7.70%; after the rework, 0.52% over 10.30%, a
  twentieth, 1.19% over the blanker alone with the blanker on, and the
  four 3.76%, about a third, with the scene's impulses in every block,
  the worst case. Without them, the usual case, 0.42% over 11.35%, 0.47%
  over the blanker, and the four 2.52%, a fifth. The fine spectrum alone
  costs more than the prototype as the measure now codes and encodes each
  frame, as the engine does; with the blanker it adds what filtering the
  samples zeroed costs, about 0.2 ms a block, most of it NumPy's calls
  rather than arithmetic. The tables are in the pull request.
- **What pull request 6 needs.** The frame: the spectrum frames' header,
  kind 3, version 1, flags 0, no level and no squelch (NaN), the levels
  coded as the spectrum frames' (the same `level0` and step), 8192 bins,
  or 2048 for a remote client; the hello's `spectra` lists `fine`, and
  `EngineStream.setFine(true)` asks for the frames, again after a
  reconnection, `onFine` receives them decoded (`decodeFine`).
  The centre is the frequency on the air of the channel's 0 Hz, bin
  `n / 2`: the VFO, and in CW the VFO less the CW pitch, 700 Hz, so the
  VFO lies 700 Hz above the frame's centre; both that distance and the
  width are scaled by the receiver's `rate_scale`, and the width is the
  channel's rate on the air, 48 kHz or WFM's 320 kHz, within 0.5% at an
  unusual sample rate. The cover is `FINE_COVER`, 0.4 of the width on
  each side of the centre, not of the VFO: in CW it reaches 700 Hz
  further below the VFO than above. Within it the front filter is flat to
  16 kHz, 0.8 dB down at 18 kHz and 3.1 dB at the edge, 19.2 kHz; in WFM
  within 0.1 dB to 128 kHz; beyond it the bins show the filter's
  stopband, which the interface must not draw as the air. About ten
  frames a second, one every tenth of a second of the channel's samples
  (one FFT of 8192 each at 48 kHz, overlapping; three or four averaged in
  WFM). After any retune,
  a step, a mode or a change of the receiver's settings, the first frame
  comes 0.17 s later at 48 kHz (0.1 s in WFM), and while the VFO moves
  block after block, as in a drag, none comes: the waterfall's fine
  history should wait rather than stretch. Nothing comes either while
  the VFO's channel receives nothing (outside the span, WFM below
  320 kHz, a receiver slower than the channel). The level of a carrier
  on a bin reads within 1 dB of the wide spectrum's in the tests; two
  carriers 20 Hz apart show apart in every mode.

As built in pull request 6, the deep zoom in the interface, where it
settles what the design left open:

- **The decisions in typed modules.** `deepzoom.js` decides when the
  interface asks, which spectrum shows, the cover, the narrowest view and
  where a frequency lies among a frame's bins; `history.js` keeps a
  spectrum's frames (trace, peak hold, waterfall lines with their time and
  centre), one for the wide spectrum and one for the fine; `waterfall-gl.js`
  holds a history for each, and draws a line within a window of its bins,
  so that the fine lines' stopband never shows. `tests/test_deepzoom.py`
  runs the first two in Node, skipped where Node is missing; main.js wires
  them. The protocol stays at version 6.
- **The switch.** The interface predicts the cover from the VFO and the
  mode, before any frame: the frame's centre, the VFO or 700 Hz below it
  in CW, and its width, 48 or 320 kHz. It asks once the view is narrower
  than 48 wide bins and lies wholly within the cover, and stops once the
  view is 72 wide bins wide, one key's zoom step past 48, or the cover
  takes less than half of it, so that the view never flips at one width or
  one edge. On a paired device, 48 wide bins (56 kHz) are wider than the
  cover (38.4 kHz): the cover alone decides there. A hidden page never
  asks; the wide frames go on feeding the wide history, as before.
- **What shows.** The fine spectrum shows once a frame came since the
  asking began and that frame's cover takes at least half the view;
  otherwise the wide spectrum shows, at its own bins: before the first
  frame, about 0.17 s, and after a long move of the VFO until the next
  frame. Each is drawn at its own frames' centre, never at the VFO's. The
  automatic scale and the noise floor follow the spectrum shown, and snap
  to it at the switch rather than glide.
- **The narrowest view** is 16 bins of the spectrum it may show: 94 Hz,
  625 Hz in WFM, 375 Hz on a paired device. A view narrower than the wide
  spectrum's narrowest, 4.7 kHz at 2.4 MS/s, must lie within the cover:
  zooming in from a view elsewhere stops at 4.7 kHz, a narrow view dragged
  past the cover stops at its edge, and the view never jumps to the cover.
  A narrower view widens by itself only when it must: to 625 Hz on a change
  to WFM, to 4.7 kHz when the fine frames can no longer come (an engine
  without them after a restart, the VFO outside the span, a receiver
  slower than the channel).
- **The fine waterfall** starts empty at each asking, rather than show
  lines from an earlier zoom; slides when the VFO moves by less than its
  width and starts again otherwise, on a lost stream and on another
  source. Its lines repeat the latest frame for at most 150 ms, against
  250 ms for the wide one, as its frames come every 0.1 s, then wait.
- **The wide spectrum moved by half a bin**: it drew bin i from its low
  edge, 146 Hz high at 2.4 MS/s, whereas the protocol centres it there;
  the deep zoom would show the shift. Both spectra and both waterfalls now
  draw bins at their centres.
- **The readout** gives the hertz once a pixel spans less than 10 Hz, the
  fine spectrum's level under the pointer, and "beyond the fine spectrum"
  within the span but outside the cover.
- **A 1 Hz step**, at 94 Hz across, moves the VFO's line about 13 pixels
  on a window 1280 pixels wide, and the fine bins with it, as the frame's
  centre follows the VFO; a carrier stays at its frequency on the air. The
  browser tests check that a bin's edge moves 1 Hz while two carriers
  20 Hz apart stay apart at their frequencies.

### Pull requests

1. The spike and its figures.
2. SAM.
3. The notch.
4. The noise blanker.
5. The fine spectrum in the engine and the protocol.
6. The deep zoom in the interface.

### Done when

On shortwave, on the owner's receiver: SAM holds a fading broadcast
station without the distortion AM gives; the notch removes a whistle on a
voice; the blanker thins a crackle; and the zoom goes on to less than
100 Hz across, where a 1 Hz step moves a carrier visibly.

### Limits

- The fine spectrum covers 19 kHz on each side of the VFO, no more.
- The notch does not act in CW, where the signal is a steady tone, nor in
  NFM and WFM.
- The blanker's threshold is fixed, against the median of the whole span
  that the receiver takes in; it removes impulses of 16 samples at most,
  in blocks where they take 2% of the samples at most, and nothing of a
  steady noise.

Set aside: a manual notch, a blanker strength, SAM on one sideband, noise
reduction.

## 3. Automatic calibration

Goal: the correction found by Radiolack, on the standard-frequency
stations that the band plan marks `reference`.

### Decisions

- **On demand only**: a button in the Calibrate dialog; the owner applies
  the result. No option at start.
- **Standard-frequency stations only**: no mobile network, no GPS.

### Design

- **A run**, by the engine, like a scan: the stations within the
  receiver's reach, twelve at most, lowest first. At each it tunes, waits
  for the settling, and measures the strongest carrier within 200 ppm of
  the station, 2 kHz at most, twice, as the calibration tool does. It then
  brings the receiver and the VFO back, as the sweep does.
- **Result**: for each station received, the carrier and the correction it
  implies. The proposal is the median of those that agree within 0.3 ppm:
  it needs two stations, or one alone at 30 dB or more, which the dialog
  flags. Otherwise it proposes nothing and says that no station was
  received, to try at another hour or to check the antenna.
- **What it stops and refuses**: it stops a scan, as tuning by hand does;
  it does not start while a recording runs, nor with a converter set, whose
  oscillator would pass for the crystal's error, nor on a source without a
  correction.
- **Protocol**: a `calibration` state for every interface (the run, its
  progress, its results, its proposal), since the receiver is shared, and
  start and stop commands. Apply is the ordinary correction.

### Pull requests

1. The run, its state and its commands, on the fake rtl_tcp, which gains
   carriers on reference frequencies and a crystal error.
2. The dialog and README.md.

### Done when

On the owner's V3 with its shortwave antenna, a run finds at least two
stations when propagation allows, proposes a correction, and a second run
after Apply proposes a change under 0.1 ppm.

### Limits

- Which stations come in depends on the hour and the season; some days
  none does.
- The ionosphere moves a carrier by up to about 1 Hz: the result is good
  to about 0.1 ppm.
- A V3 in direct sampling does not reach the long-wave stations, below
  500 kHz.
- It measures the crystal: on the tuner, a centre frequency's PLL error is
  not in it.

## 4. Band plans for other regions

Goal: the Americas, Asia-Pacific, and France, beside Europe.

### Decisions

- **The region is the engine's setting**, shared by every interface: it is
  where the receiver is. Europe unless chosen otherwise.
- The FM de-emphasis and the channel spacings follow the plan.

### Design

- **Files**, in format 2: `bandplan-americas.json` (ITU Region 2),
  `bandplan-asia-pacific.json` (Region 3), `bandplan-france.json`.
- **A plan's `wfm_deemphasis_us`**, 50 or 75: the engine reads it from the
  file of the plan in use, the one place that says it.
- **Setting**: `band_plan` in `settings.json`; the state tells every
  interface, which loads that file; the Bookmarks menu, which names the
  plan, chooses it, on every interface, as the bookmarks.
- **Sources**: Article 5 for each region and the IARU Region 2 and
  Region 3 band plans, in their current editions, by the rules of the
  European plan: names written for Radiolack, a second reading. A regional
  plan names what is common to its region; national differences are left
  out and said to be.
- **France**: the national table of frequency allocations, once its
  licence is read and allows it; otherwise that pull request stops there
  and says why.

### Pull requests

1. The choice of plan: setting, state, menu, de-emphasis from the plan,
   with Europe alone.
2. The Americas.
3. Asia-Pacific.
4. France.

### Done when

The Bookmarks menu changes the plan on every interface at once; WFM's
de-emphasis follows it; each file passes the structure tests and carries
its second reading's record; and the owner finds the French plan true to
what they know of their bands.

### Limits

- The owner cannot check the two other regions by listening: they rest on
  their sources and their second reading.
- A regional plan is coarser than a national one, and neither is a legal
  reference.

## 5. Several receivers at once

Goal: one Radiolack with more than one receiver, such as the V3 watching
the bookmarks of 2 m while the owner listens to shortwave on the HackRF.

### Decisions

- **Each interface shows one receiver at a time.** The others go on with
  their scans and recordings. Two browser windows show two receivers side
  by side; one window never shows two.
- Bookmarks and the History are common to the receivers.

### Design

- **A radio** is what the engine is today for one receiver: its source,
  its reading loop, its VFO, its scanner, its recordings. The engine
  becomes the radios and what they share: the bookmarks, the observations,
  the preferences, remote access, updates, storage.
- **Four radios at most.** Each opens its receiver only while an interface
  shows it or one of its scans or recordings runs, as today. A receiver
  belongs to one radio.
- **Protocol**: the hello lists the radios; an interface chooses one
  (`{"type": "radio", "id": ...}`) and then receives that radio's states,
  frames and audio. A page that never chooses gets the first.
- **Interface**: the Receiver menu lists the radios, switches between
  them, adds one from the sources not in use and removes one. The menu
  icon's dot counts every radio's scans and recordings, and the list says
  which radio runs what. Each device remembers the radio it showed.
- **Scans**: each radio watches the ticked bookmarks within its own reach.
- **Start**: `--rtl-sdr`, `--rtl-tcp` and `--soapy` may come together and
  more than once, a radio each, in order; `--scan` and `--bias-tee` apply
  to the first. Without them Radiolack reopens the radios used last.

### Spike, first pull request

Two radios on synthetic samples at 2.4 MS/s, each with a listener, for ten
minutes, on the owner's laptop and on the computer next to the receivers.
They pass when the audio never runs dry and Radiolack stays under 60% of
two cores. If they fail, each radio gets a process of its own, and the
spike's report says what that changes before the refactoring starts.

### Pull requests

1. The spike and its figures.
2. The radio, split from the engine, with one radio and no change in
   behaviour.
3. Several radios: engine, protocol, start options.
4. The Receiver menu's radios, the dot, each device's radio.
5. Service notes and README.md.

### Done when

On the computer next to the receivers, with the V3 and the HackRF: the
iPhone listens on the HackRF while the V3's memory scan stops on a
bookmark and the History records it; two browser windows show both; and a
restart brings both radios back.

### Limits

- Each radio costs what one engine costs: 12 to 15% of one core at
  2.4 MS/s on the owner's laptop.
- One interface plays one radio's audio.

Set aside: a split view, audio from two radios mixed, a scan option per
radio.

## 6. DRM

Goal: play the digital broadcasts of long, medium and short wave (DRM
below 30 MHz): the audio and the station's label.

### Decisions

- **A compiled component is allowed**, as the owner chose: decoding is too
  heavy for Python alone.
- To keep its cost apart, it ships as a second Debian package,
  `radiolack-drm`, for each architecture, which `radiolack` suggests. The
  main package stays independent of the architecture, built as it is and
  reproducible. Without the second package, the DRM mode is greyed and says
  which package brings it.

### Spike, first pull request

It answers three questions, and each answer is decided here.

| Question | Passes when | If it fails |
| --- | --- | --- |
| Which audio coding do the stations received in Europe use, and which libraries decode it? | AAC decodes with a library in Debian's `main`, under a licence that fits GPL-3.0-or-later. Debian 12's `libfaad2` ships a DRM build of its decoder; its licence and its use are to confirm. | The milestone closes on the report. |
| Does the same hold for xHE-AAC, which newer broadcasts use? | A library that fits the licence is packaged on the supported systems. | xHE-AAC stations show "audio coding not supported"; the rest goes on. |
| Does a real broadcast decode in real time? | An I/Q recording of each of two stations, made by the owner, decodes under 60% of one core on the owner's laptop, with the hot loops in the compiled component. | The milestone closes on the report. |

The spike also says whether the channel decoder is written for Radiolack
or taken from a program whose licence fits, and records the state of the
patents on these codings for the public release.

### Design, if the spike passes

- **A mode, DRM**: its filter takes the signal's width, its audio plays at
  32 kHz as WFM's does, and it is silent until it locks.
- **State**: lock, signal-to-noise ratio, the station's label, the audio
  coding, or why nothing plays.
- **The compiled part** holds only the loops that Python cannot run in
  time; synchronisation and equalisation stay in NumPy.
- **Tests**: broadcasts are other people's works and cannot be committed,
  so a modulator written for the tests makes the signals, with silent
  audio.
- **Packaging**: the APT repository and its tools gain architectures
  beside `all`; the release builds `radiolack-drm` reproducibly in a pinned
  container; the source's `Architectures` line, a configuration file,
  changes with a release of its own.

### Pull requests

1. The spike and its decisions.
2. The second package, with an empty component that tests itself: CI, the
   APT repository's tools, the release.
3. The channel decoder and the test modulator.
4. The audio decoding.
5. The mode, its state, the interface and README.md.

### Done when

A DRM broadcast on shortwave plays on the owner's computer with its label,
from the two packages installed from the APT repository; or the spike's
report closes the milestone and says why.

### Limits

- No DRM on VHF, no data services, no pictures.
- A second package to build and sign for each architecture.

## 7. Logarithmic overview

Goal: see the whole band plan at once. On the linear axis, from 0 Hz to
10.5 GHz, shortwave is narrower than a pixel.

### Decision

**An overview bar**, as the owner chose over a logarithmic axis for every
view: the spectrum and the waterfall stay linear, as a measuring display
is.

### Design

- **A thin bar**, 10 px with a mouse and 14 px on a touch screen, above
  the band strip: the plan from 1 kHz to its end on a logarithmic scale,
  seven decades, in the bands' colours, with a tick at each decade.
- **On it**: the receiver's reach, brighter; the span; the view, as a
  bracket; the VFO, as a line.
- **A click or a tap** frames the band under it; a drag frames each band
  it passes; a tooltip names the band and its edges.
- **Display > "Overview bar"**, each interface's preference, on by
  default; it hides with the band plan.
- A typed module, `overview.js`; the engine and the protocol do not
  change.

### Pull request

One: the bar, its tests and README.md.

### Done when

From any view, one click on the bar frames the 40 m band, and another the
3 cm band, and the bar shows where the reach, the span and the view lie.

### Limit

Nothing below 1 kHz shows on the bar; the view itself still starts at
0 Hz.
