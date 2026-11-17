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

Goal: the owner's HackRF One, 1 MHz to 6 GHz, and other receivers,
through SoapySDR. An RTL-SDR keeps rtl_tcp.

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
  on the owner's V3, before the HackRF is back.

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

### Pull requests

1. The spikes and their results.
2. The source, a stand-in `SoapySDR` module for the tests, and `--soapy`.
3. The Receiver menu: the devices found, Search again, the arguments field.
4. Gains (overall and per stage), antenna choice, antenna power.
5. I/Q recordings in the device's format.
6. Package, service notes, README.md, CI.

### Done when

On the owner's laptop, the V3 opened through SoapySDR tunes, plays and
scans as it does through rtl_tcp. With the HackRF One: Radiolack lists it,
tunes from 1 MHz to 6 GHz, plays a broadcast station, shows a signal above
2 GHz, sweeps a band, records I/Q that opens in another program at the
right frequency, runs as the service with `--soapy`, and never transmits.

### Limits

- Only the HackRF One and the V3 are checked on hardware; other devices
  work as far as SoapySDR and their modules do.
- SDRplay's module needs SDRplay's own closed library, which Radiolack
  neither ships nor installs.
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
- The blanker's threshold is fixed.

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
