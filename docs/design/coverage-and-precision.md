# Coverage and precision

Design of the milestone that follows v3, decided with the owner on
2026-11-16. Two requests started it:

1. Cover the whole spectrum, from 0 Hz to 10 GHz, with a precise band plan
   over that range, and explore the shortwave bands, which Radiolack could
   not reach: it stopped near 23 MHz.
2. Tune very precisely: a bookmark set at a precise frequency came out
   rounded.

[`scoping.md`](scoping.md) holds the decisions and the milestone's summary.
This file holds what an implementation needs: the limits found in the code,
what the receivers allow, each pull request's rules, and every limit that
remains. The milestones that follow are in
[`later-milestones.md`](later-milestones.md).

Line numbers refer to commit `a4a7bf9` (0.13.1.dev0).

## Where the limits were

### Lowest and highest frequency, layer by layer

| Layer | Limit | Where |
| --- | --- | --- |
| rtl_tcp client | R820T and R828D tuners: centre from 24 to 1766 MHz, so the span reaches 22.8 MHz at 2.4 MS/s. E4000 52 to 2200 MHz, FC0012 22 to 948.6 MHz, FC0013 22 to 1100 MHz, FC2580 146 to 924 MHz. | `rtltcp.py:109` to `:119` |
| rtl_tcp client | A command carries the frequency in 32 bits: 4,294,967,295 Hz at most, the only limit for an unknown tuner. | `rtltcp.py:120`, `:121` |
| rtl_tcp client | Only commands 0x01 to 0x05 are sent: no direct sampling (0x09), no RTL2832 AGC (0x08), no bias tee (0x0e). | `rtltcp.py:80` to `:84` |
| rtl_tcp client | Sample rates from 900,001 Hz to 3.2 MS/s; librtlsdr's lower range, 225,001 to 300,000 Hz, is left out. | `rtltcp.py:56` to `:58` |
| Engine | The VFO, the bookmarks, the sweep's range, the ignored frequencies and the observations take 1 Hz to 100 GHz. | `demod.py:103`, `bookmarks.py:24`, `scanners.py:80`, `observations.py:185` |
| Engine | The simulated band does not tune: its reach is its span. | `synthetic.py:21`, `engine.py:241` |
| Protocol | Frequencies are JSON numbers and a float64 in the frames: no limit of its own below 100 GHz. | `protocol.md:110`, `:397`, `:587` |
| Recordings | File names hold up to 99,999.999999 MHz. | `recordings.py:86` |
| Interface | The view reaches ten spans, its centre within the receiver's reach; with the simulated band it stays within the span. | `main.js:40`, `:1199`, `:1200` |
| Band plan | 198 bands and 81 segments from 9 kHz to 6.425 GHz, one use per range. | `bandplan-europe.json` |

So the bottom near 23 MHz was the tuner's range, written in one table, and
nothing else: the engine, the protocol and the saved files already took
every frequency up to 100 GHz.

### Rounding and steps

| Place | What it does | Where |
| --- | --- | --- |
| Engine | The VFO, a bookmark, a sweep edge and an ignored frequency are rounded to the hertz. | `demod.py:103`, `bookmarks.py:127`, `scanners.py:672` |
| DSP | The shift to the VFO is exact: whole FFT bins, then the rest after the inverse FFT. | `dsp.py:134`, `:135` |
| Scanners | A signal found is its centre of power, to the hertz; signals within 1.5 kHz, or a quarter of their width, are one. | `scanners.py:74`, `:263` |
| Observations | A channel moves 30% of the way to each signal found there, to the hertz. | `observations.py:56` |
| Interface | Frequencies show in MHz with 4 decimals, 100 Hz: the bookmarks, their tooltips, the History, the sweep's range, the centre frequency. | `main.js:18` |
| Interface | The typed entry opens with those 4 decimals, and leaving it applies it: opening it and clicking elsewhere rounds the VFO to 100 Hz. | `main.js:3052`, `:3077` |
| Interface | A click, a drag, the wheel, the arrow keys and the step buttons land on the step's grid. | `main.js:142`, `:1314`, `:2969`, `:2980` |
| Interface | Steps: Automatic (the band plan's, else 100 Hz), 10 Hz, 100 Hz, 1 kHz, 6.25, 12.5 and 25 kHz. | `main.js:66`, `index.html:94` |
| Interface | The wheel over a digit of the readout changes that digit, down to 1 Hz, but stays within the span, and a touch screen has no wheel. | `main.js:3155` |
| Interface | A bookmark's frequency cannot be edited, only its name. | `main.js:2143` |
| Interface | Signals found show on their channel or to 100 Hz: they are measurements, and the bins are 293 Hz wide at 2.4 MS/s. | `main.js:1366` |
| Receiver | The frequency correction is a whole number of ppm. | `rtltcp.py:59` |
| Display | The FFT has 8192 bins: 293 Hz per bin at 2.4 MS/s, and the finest view is 16 bins or 4 kHz. | `spectrum.py:20`, `main.js:1198` |

So the engine kept the hertz, and the rounding came from the interface: its
4 decimals, its grid, and a bookmark that could not be retyped.

### What the band plan file holds

`bandplan-europe.json`, format 1: a name, 18 colour types, and contiguous
bands from 9 kHz to 6.425 GHz, each with a name, a type, and optionally a
short name, a mode, a tuning step and segments. `bandplan.js` checks it and
makes one sorted list of ranges; `rangeAt` finds a frequency's range by
bisection. The strip draws a band's segments while they average 12 px, else
the band as one block (`main.js:866`). Its `step` mixes channel spacings
(9 kHz, 12.5 kHz, 25 kHz / 3) and plain defaults (1 kHz in the utility
ranges, 10 Hz in the amateur CW bands).

## What the receivers allow

Checked on 2026-11-16 in the source of osmocom's rtl-sdr (`master`, 2.0.3)
and of the RTL-SDR Blog fork, read on their GitHub mirrors, and on
rtl-sdr.com. Figures marked *derived* come from reading that code and were
never measured.

### RTL-SDR

- **Tuner.** R820T, R820T2 and R828D: 24 to 1766 MHz. librtlsdr checks no
  range: outside it the tuner's PLL does not lock, the driver says so on its
  own error output, and rtl_tcp, which checks no command's result, tells its
  client nothing.
- **Direct sampling.** rtl_tcp's command 0x09 (1: the I input, 2: the Q
  input, 0: off) makes the RTL2832 sample its input at 28.8 MS/s without the
  tuner. An RTL-SDR Blog V3 has the Q input wired to its antenna socket; any
  other dongle needs a hardware modification. Present in librtlsdr 0.6.0
  (Debian 12) and later.
  - It covers 0 to 28.8 MHz, and the V3's input passes from about 500 kHz.
  - The two halves mirror each other: a signal at f also shows at
    28.8 MHz − f.
  - The tuner is asleep: no gain setting applies.
  - The frequency is set by the RTL2832's own mixer, in steps of 6.87 Hz,
    rounded down.
  - In the Blog fork, 0x09 = 0 means automatic: a frequency below 24 MHz
    switches to the Q input by itself.
- **RTL-SDR Blog V4.** Its R828D tuner sits behind a 28.8 MHz upconverter:
  below 28.8 MHz the driver tunes it by itself, from about 500 kHz, on a
  plain frequency command, with the tuner's gains. It needs osmocom's
  rtl-sdr 2.0.0 or later, or the Blog fork: Debian 13, Ubuntu 24.04 and
  26.04 have it, Debian 12 (0.6.0) does not and mistunes a V4. rtl_tcp's
  header announces a V4 as any R828D: a client cannot tell.
- **Bias tee.** Command 0x0e, in rtl_tcp since 0.5.4: 4.5 V on the antenna
  socket of a V3 or V4, at most 180 mA. In the Blog fork, command 0x0a
  switches it too on these tuners: Radiolack never sends 0x0a.
- **Frequency correction.** Whole ppm only (command 0x05), in every fork.
  It corrects the tuner's reference and the sample rate.
- **Crystal.** A V3 or V4: within 2 ppm when new, drifting 0.5 to 1 ppm
  with temperature. Other dongles: tens of ppm.
- **Tuner PLL, *derived*.** The driver programs the tuner's oscillator in
  steps, and does not make up the rest in the RTL2832's mixer. Up to
  rtl-sdr 2.0.1 and in the Blog fork, the real frequency may lie from about
  2.8 kHz / m below to 1.3 kHz / m above the one asked, m being the tuner's
  divider: 16 near 145 MHz (−175 to +80 Hz), 8 near 435 MHz (−350 to
  +160 Hz), 2 near 1296 MHz (−1.4 to +0.65 kHz). From 2.0.2 it stays
  within 440 Hz / m. The error is fixed for a given centre frequency, and
  rtl_tcp does not say which driver runs. The owner's computers run 0.6.0
  and 2.0.1.

### Beyond the tuner

- **A converter** in front of the receiver moves a range into the tuner's:
  an upconverter adds its oscillator's frequency (125 or 120 MHz for the
  common ones), a satellite LNB subtracts it (9750 or 10,600 MHz), which
  brings 10,489.5 MHz to 739.5 MHz. The software only has to add or
  subtract that frequency. An LNB needs 13 to 18 V on its cable, which no
  dongle's bias tee gives.
- **Other receivers**, such as the owner's HackRF One (1 MHz to 6 GHz),
  come through SoapySDR, in the next milestone. None of the common ones
  reaches 10 GHz without a converter.

### What follows for the owner's receivers

| Range | With the RTL-SDR Blog V3 | With the HackRF One (next milestone) |
| --- | --- | --- |
| 0 to 500 kHz | Not received: below the V3's input. | Not received: below 1 MHz. |
| 0.5 to 14.4 MHz | Direct sampling. | From 1 MHz. |
| 14.4 to 24 MHz | Direct sampling, with the mirror of 4.8 to 14.4 MHz over it. | Yes. |
| 24 to 1766 MHz | The tuner. | Yes. |
| 1766 MHz to 6 GHz | Not received. | Yes. |
| 6 to 10.5 GHz | Only through a converter. | Only through a converter. |

## Decisions

All of 2026-11-16, each also in [`scoping.md`](scoping.md).

- **Three things stay apart**: what the software shows and stores (0 Hz to
  10.5 GHz, to the hertz), what the receiver in use reaches (its range, as
  its source describes it), and how true a frequency is (the crystal, the
  tuner, the converter).
- **Shortwave with an RTL-SDR** comes three ways, chosen per receiver:
  direct sampling, switched by itself below the tuner's range; a tuner that
  covers it (V4), which cannot be checked without one; a converter.
- **The view visits the whole plan** whatever the receiver, the simulated
  band included; nothing is tuned beyond the receiver's reach.
- **1 Hz everywhere**: the interface stops rounding. Finer was set aside
  for good: no receiver in scope holds a tenth of a hertz.
- **The Automatic step follows the zoom**, and a channel spacing where the
  band plan gives one. No new modifier key.
- **The correction takes hundredths of ppm**, with a tool that measures a
  known carrier.
- **The tuner's PLL error is measured before any code corrects it.**
- **The band plan** runs from 0 Hz to 10.5 GHz for Europe, with named
  channels and known frequencies: a detailed guide, still not a legal
  reference.
- **Antenna power** (bias tee) is off at each start unless `--bias-tee` is
  given, and never saved.
- **Neither the saved files' format version nor the protocol's changes**:
  every new field is one that an older Radiolack or page ignores.

## Pull requests

In this order, each with its documentation (README.md, the manual page
where an option changes, protocol.md, this file's limits) and its tests,
which need no receiver: the fake rtl_tcp server and program of `tests/`
gain what each needs. The owner checks on the receiver.

Common rules:

- The interface's new code goes into typed modules (JSDoc, checked by
  TypeScript), as `antenna.js` did: `main.js` only calls them.
- A setting that a source does not have is null in its state and absent
  from `settable`: the interface hides or disables its control. The
  SoapySDR sources of the next milestone describe themselves the same way.
- Protocol version 6 stays: new state fields, new command fields, and a
  new capability in the hello where an older engine must grey a control.

### 1. This design

`scoping.md`, this file and `later-milestones.md`.

### 2. Exact frequencies in the interface

Interface only.

- **One formatter**, `formatFrequency(hz)`: MHz with 3 to 6 decimals, the
  zeros at the end dropped (145.500, 145.6125, 145.61255). It replaces the
  4 decimals wherever a frequency is exact: the bookmarks' list, tooltips
  and labels, the VFO's edge label, the antenna dialog, the context menu's
  title, the sweep's range, the centre frequency. Measurements keep their
  rounding: the signals found, the History's channels, the pointer's
  readout.
- **One reader**, `parseFrequency(text)`: a number with a point or a comma,
  then an optional unit, in any case and after optional spaces: `Hz`, `k`
  or `kHz`, `M` or `MHz`, `G` or `GHz`. Without a unit, a number below
  100,000 is in MHz and another in Hz, as before. The result is rounded to
  the hertz and must lie from 1 Hz to 100 GHz. Every frequency field uses
  it: the VFO's entry, a bookmark's frequency, the sweep's range, the
  centre frequency.
- **The VFO's entry** opens with the whole frequency, and applies only a
  value that differs from the VFO's: leaving it unchanged moves nothing. A
  frequency beyond the receiver's reach tunes nothing and says so in the
  status line.
- **A bookmark's frequency** is edited beside its name; an invalid one
  keeps the row open with a note.
- **Dragging a digit** of the readout up or down changes it, by one for
  each 16 px, with any pointer; the drag starts after 8 px, so that a tap
  still opens the keypad on a touch screen. The wheel over a digit stays.
  Both now move the span when the frequency leaves it, as a click outside
  the span does.
- **Steps**: Automatic, 1, 10, 50, 100 and 500 Hz, 1, 5, 6.25, 8.33, 9,
  10, 12.5, 25, 50 and 100 kHz. The 8.33 kHz step is 25 kHz / 3, as the
  band plan's airband step.
- A click, a drag, the wheel, the arrow keys and the step buttons still
  land on the step's grid; a typed frequency, a bookmark and a digit never
  do. Shift still multiplies the step by ten.

### 3. The Automatic step follows the zoom

Interface, and the band plan's `step`.

- Where the VFO's range has a channel spacing and the view shows at least
  two channels (its width is at least twice the spacing), the step is that
  spacing.
- Elsewhere it follows the width of the view:

  | View at least | Step |
  | --- | --- |
  | 200 MHz | 100 kHz |
  | 20 MHz | 10 kHz |
  | 2 MHz | 1 kHz |
  | 200 kHz | 100 Hz |
  | 20 kHz | 10 Hz |
  | narrower | 1 Hz |

- The line under the frequency shows the step, as before, and changes with
  the zoom; it is written only when its text changes.
- In the band plan, `step` becomes the channel spacing alone: the ranges
  without channels lose theirs (the utility ranges' 1 kHz, the amateur CW
  bands' 10 Hz, the beacons' 500 Hz), and the 100 Hz default goes.
  Broadcast keeps 9 kHz and 5 kHz, CB 5 kHz, the airband 25 kHz / 3, FM
  broadcast 50 kHz, and the channelized ranges their 6.25, 12.5 or 25 kHz.
- A fixed step chosen in Radio applies whatever the zoom.

Limit: a paired device's finest view is 16 of its 2048 bins, 18.75 kHz at
2.4 MS/s, so it reaches the 1 Hz step there but not at 2.88 MS/s; dragging a
digit covers that.

### 4. Shortwave with an RTL-SDR

`rtltcp.py`, the engine's state, the protocol, the Receiver menu.

- **A setting per receiver, `hf_input`**: `none` (as before), `q` or `i`
  (direct sampling on that input), `tuner` (an RTL-SDR Blog V4, whose tuner
  covers shortwave). The Receiver menu names them "None", "Direct sampling,
  Q input (RTL-SDR Blog V3)", "Direct sampling, I input" and "Tuner
  (RTL-SDR Blog V4)", each with a line that says what it needs. It is saved
  with the receiver's settings.
- **Reach.** With `q` or `i`, the centre frequency goes down to half the
  sample rate, so that the span starts at 0 Hz; with `tuner` too. The VFO's
  lowest frequency stays 1 Hz.
- **Switching.** A centre frequency below the tuner's range samples
  directly, another uses the tuner. Entering: 0x09 with 2 or 1, then the
  frequency. Leaving: 0x09 with 0, then the gain mode and the gain, which
  the tuner forgot, then the frequency. At each connection: the correction,
  the sample rate, 0x09, the frequency, the gain. The samples of the
  settling time are dropped, as after any retune. 0x08 follows 0x09 at each
  connection too, since rtl_tcp keeps it from its previous client.
- **Direct sampling's span** stays within 0 to 28.8 MHz. With a tuner whose
  range starts above what direct sampling reaches (E4000, FC2580), the
  centre frequencies in between are refused, with the message that names
  the two ranges.
- **While sampling directly**, the state says so (`direct`), the gain
  controls are disabled with the note "Direct sampling: no gain control",
  the saved gain comes back with the tuner, and the Receiver menu says that
  signals above and below 14.4 MHz mirror each other.
- **`rtl_agc`**, the RTL2832's own AGC (0x08): a box in the Receiver menu,
  enabled with `q` or `i`, sent as 1 on entering direct sampling and 0 on
  leaving. Off by default; to try when shortwave is too quiet or too loud.
- **State and commands**: the source's state gains `hf_input`, `hf_inputs`
  (those offered: all four for an R820T or R828D tuner, the first three for
  another, none for a source without them), `direct` and `rtl_agc`;
  `settable` and the source command gain `hf_input` and `rtl_agc`.
- The problem report says which input is in use, never a frequency.

Limits, all in README.md:

- Direct sampling: nothing below about 500 kHz on a V3; mirrors above
  14.4 MHz; no gain control; a dongle other than a V3 needs a hardware
  modification, and Radiolack cannot tell whether it has one.
- The V4 setting is not checked on a V4. With a driver that does not know
  the V4 (librtlsdr 0.6.0, Debian 12), a V4 tunes wrong on every band, and
  Radiolack cannot see it.
- A tuner asked for a frequency it cannot reach says nothing: with the
  wrong setting, the band is silent, not refused.

### 5. Low sample rates

- An RTL-SDR also takes 225,001 to 300,000 samples per second; the
  Receiver menu adds 0.240 and 0.300 MS/s: 29 and 37 Hz per bin, a span as
  wide as a shortwave band, and less CPU.
- WFM needs a channel of 320 kHz: at these rates it plays nothing, the
  mode stays, and the status line says "WFM needs a sample rate of
  1.024 MS/s or more".
- The tests run detection, the memory scan and the sweep at 240 kS/s.

Limit: the finest view stays 4 kHz wide until the deep zoom of the
Listening tools milestone.

### 6. Antenna power

- **`bias_tee`** on an RTL-SDR source: command 0x0e. Never saved. Off, and
  sent as off, at each connection, unless Radiolack was started with
  `--bias-tee`, which turns it on for the receiver given or reopened at
  start; a source without it ignores the option with a warning. An
  interface changes it afterwards, and a reconnection applies the state in
  use.
- Radiolack turns it off before it closes a source on which it was on:
  when it stops, and when another source takes its place; also before an
  rtl_tcp source leaves its server for another address, so that the
  receiver left behind is not left powered.
- **Receiver menu**: "Antenna power (bias tee)", on every interface, as the
  other receiver settings; turning it on waits for a second press, which
  says "4.5 V on the antenna socket"; turning it off is immediate.
- The manual page documents `--bias-tee`; the service's unit does not use
  it: `systemctl edit radiolack` adds it.

Limits: an rtl_tcp older than 0.5.4 ignores the command, and Radiolack
cannot read the state back; a crash leaves the power as it was; an rtl_tcp
server that Radiolack did not start may set it by itself; it is not enough
for a satellite LNB; power into an antenna that is a short circuit for
direct current may damage the receiver, which README.md says.

### 7. The view visits the whole plan

Interface only. This replaces, in the decision of 2026-11-13, "out to ten
spans wide with its centre within the receiver's reach", "the simulated
band keeps the view within its span" and "beyond the receiver's reach to
the nearest frequency".

- **The view** goes from 0 Hz to the end of the band plan, or of the
  receiver's reach when that is higher, with every source. Zooming out
  still holds at the span's width before going past it, and goes on to the
  whole extent.
- **Decor.** Outside the span and within reach: the lit window, as before.
  Beyond reach: the hatching, under a band strip that stays readable.
  Nothing there looks like a measurement.
- **A span narrower than 3 px** shows as a marker 2 px wide in the
  spectrum and the waterfall, so that the receiver's place stays visible.
- **Beyond reach, nothing tunes**: a click, a tap, a typed frequency, a
  bookmark, a signal or a channel of the History leave the VFO where it is.
  A mouse sees a tip before the click, and the status line then says, for
  instance, "5760.000 MHz is beyond this receiver's range (0.5 to
  1766 MHz)". The context menu there disables Tune and Ignore, and keeps
  Bookmark, Center view and Zoom to range: a bookmark may be prepared for
  another receiver. As built, the range in the note is the receiver's
  reach, the one that decides, rather than its centre range: half the
  sample rate wider on each side, given from 0 Hz at the lowest (0.000 to
  1767.200 MHz for a V3 at 2.4 MS/s, its span alone for the simulated
  band), so that no frequency the note calls beyond the range tunes, nor
  the other way round.
- **The frequency axis** drops its decimals as the view widens (0 to 6).
- The strip and its glow draw only the bands in view, found by bisection:
  the plan grows.

### 8. A converter in front of the receiver

- **Two settings per receiver**: `converter`, one of `none`, `up`
  (upconverter: the receiver tunes the frequency plus the oscillator's),
  `down` (downconverter or LNB: the frequency minus the oscillator's) and
  `down_inverted` (the oscillator's minus the frequency, which mirrors the
  spectrum), and `converter_lo_hz`, its oscillator's frequency to the
  hertz.
- Every frequency that the engine, the interface, the bookmarks, the scans
  and the recordings see is the one on the air; only the source converts,
  for the command it sends. Its range is the receiver's, converted, and
  kept at or above half the sample rate.
- `down_inverted` swaps I and Q as the samples arrive, before the DSP and
  the I/Q recording, which then holds the band as it is on the air.
- **Receiver menu**: "Converter" (None, Upconverter, Downconverter (LNB),
  Downconverter, inverted spectrum) and "Converter oscillator", read by
  `parseFrequency`, with 125 MHz and 9750 MHz as examples. Changing either
  keeps the receiver where it is tuned: the frequencies shown change, the
  signals stay.
- An I/Q recording's metadata gives the frequency on the air and describes
  the converter.

As built, where this section left a choice open:

- The reach also stops at 100 GHz less half the sample rate, Radiolack's
  highest frequency, which an LNB's oscillator near 100 GHz could pass. A
  change that leaves no reach at all, such as a wider sample rate behind an
  inverted converter whose oscillator lies just above the tuner's lowest
  frequency, is refused like any other conversion out of reach.
- The oscillator is kept without a converter too, 125 MHz until typed, so
  that it can be typed before the converter is chosen.
- Another converter or oscillator does what a new centre frequency does
  today, and more, since every frequency moves: a scan stops (`tuned`); an
  I/Q recording stops (reason `converter`), as its metadata describes one
  converter; the signals found are forgotten, and found again at their new
  frequencies; the VFO moves with the conversion, so as to stay on its
  signal; an audio recording goes on, as through a retune. The bookmarks
  keep their frequencies on the air. The interface's view moves with the
  signals it shows, and its buffers start again.
- The saved settings of a receiver are applied all at once, then one at a
  time only if the receiver refuses them together: one at a time, a saved
  converter would meet the default centre frequency first and could be
  refused. A converter through which a tuner, once known, reaches nothing
  is left (`none`), with a warning, as an input the tuner does not offer is.
- The problem report names the converter, never its oscillator's frequency.

Limits: the correction in ppm is the receiver's crystal's, not the
converter's: the oscillator's true frequency goes in its field. An LNB's
oscillator moves with temperature, by far more than the receiver's crystal.
No converter is checked on hardware.

### 9. The correction in hundredths of ppm

- `ppm` becomes a number with two decimals, from −100.00 to 100.00. The
  receiver takes its nearest whole number w, by command 0x05, as before,
  only when w changes. The rest is Radiolack's: for a frequency F, it asks
  the receiver for F × (1 + w / 10⁶) / (1 + ppm / 10⁶), rounded to the
  hertz, since librtlsdr takes a correction to mean that the crystal runs
  at its nominal frequency times (1 + correction / 10⁶). The samples keep
  the frequency asked as their label, within 0.5 Hz.
- It applies in direct sampling and through a converter alike, to the
  frequency that the receiver itself tunes.
- A whole number saved before reads as it is. A source without a hardware
  correction takes w = 0 and corrects the whole of it this way.
- The fake rtl_tcp gains a crystal error, in parts per billion: with the
  correction set to it, a carrier shows at its frequency within 1 Hz.

Limits: the sample rate keeps the rest, at most 0.5 ppm: 0.6 Hz at the edge
of a 2.4 MS/s span. The RTL2832's mixer rounds direct sampling's frequency
down by up to 6.9 Hz.

### 10. Calibration on a known signal

- **The engine measures a carrier** when an interface asks
  (`{"type": "carrier", "action": "measure"}`): one second of the VFO's
  channel, before the demodulator, through a Hann window and an FFT; the
  strongest line within the filter, placed between bins by a parabola; its
  margin over the median of the filter's bins. Two measures in a row must
  agree within 1 Hz. It answers that interface alone, with the frequency on
  the air to the tenth of a hertz and the margin, or with why not:
  `no_signal` (a margin under 20 dB), `unstable`, `silent` (the VFO outside
  the span). The hello lists `carrier`, so that a page facing an older
  engine greys the tool. The tests check the frequency in every mode.
- **Receiver menu > Calibrate**, a modal dialog on every interface: the
  carrier measured, again every two seconds; "True frequency of this
  carrier", read by `parseFrequency`; the correction that would bring it
  there, beside the one in use; Apply, which sends it. After Apply, the
  carrier measures at its true frequency within 1 Hz: the tests check it on
  the fake rtl_tcp's crystal error.
- It needs a source with a correction: not the simulated band.

Limits: the result is as true as the frequency typed; on shortwave the
ionosphere moves a carrier by up to about 1 Hz; with a converter it takes
the converter's oscillator as exact; on the tuner it also takes in the
tuner's own error at that centre frequency (pull request 13), so the
crystal alone is best measured in direct sampling.

### 11. The band plan from 0 Hz to 10.5 GHz

- **Scope**: 0 Hz to 10.5 GHz without gaps, "Not allocated" below
  8.3 kHz. 10.5 GHz, not 10, keeps the 3 cm amateur band whole, with the
  narrowband segment that amateur satellites use.
- **Sources**, as before: the ITU Radio Regulations' Article 5 for
  Region 1, the CEPT's European Common Allocation table for the main use,
  the IARU Region 1 band plans inside the amateur bands, each in the
  edition current when the pull request is written, named with its date in
  [`band-plan.md`](band-plan.md). The 198 bands of today are checked again
  against them, the amateur bands against what IARU Region 1 published
  after its 2026 conference.
- **Licence**: only frequencies and facts are taken, from the primary
  documents, and every name is written for Radiolack. Nothing is copied
  from another program's band plan, whatever its licence, nor extracted in
  bulk from a database.
- **Format 2** (below), with empty `channels` and `spots`, so that pull
  request 12 adds data and display only.
- **A second reading**: once the file is written, a reviewer who has not
  seen the author's notes checks every edge and every name against the
  sources; `band-plan.md` records the date, what was checked, what was
  corrected and what could not be checked.
- AGENTS.md's rule becomes "a detailed guide, not a legal reference", and
  README.md's Band plan section follows.

### 12. Named channels and known frequencies

Data:

- **Channel sets**: the channels that a regulation or a long-standing
  international convention numbers across Europe, such as CB, PMR446,
  short-range devices at 433 MHz, the maritime VHF channels, the amateur
  repeater and simplex channels of the IARU Region 1 plans, the DAB blocks,
  the UHF television channels, the Wi-Fi channels.
- **Spots**: single frequencies that are public and stable, such as the
  distress and calling frequencies, the standard-frequency and time
  signals, the amateur conventions (digital modes, beacons, calling
  frequencies, space stations), navigation and identification systems, the
  hydrogen line. A standard-frequency station whose carrier is the standard
  carries `reference`, for the calibration milestone.
- **Not included**: broadcasters, operators, local repeaters, national
  channel plans, anything that changes with a satellite's or a network's
  life, anything personal.
- About 25 sets and 150 to 250 spots, at most 60 and 400. Same sources
  rule, same second reading.

Display, with one strip, as the owner chose:

- **The line under the frequency** names the channel the VFO is on, or the
  spot within 50 Hz: "PMR446 · ch 3 · step 12.5 kHz".
- **On the strip**, channels show as ticks along its lower edge once they
  are 6 px apart, with their numbers once 28 px apart; spots as small marks
  along its upper edge, with their names where they fit, left to right, as
  the bookmarks' marks do. A tooltip names each; a click or a tap tunes
  there, with the spot's mode, within the receiver's reach.
- **Display > On the spectrum > "Channels and known frequencies"**, a
  preference of each interface, on by default, hides the ticks, the marks
  and their names; the line under the frequency keeps naming them.
- **The Bookmarks menu's plan list** follows the view rather than the
  span, at most 200 rows, lists the spots under their range, and its filter
  searches the sets and the spots. A click frames a range or tunes to a
  spot.
- **A new bookmark** made on a channel or a spot takes its name.
- **The Automatic step** takes a set's spacing as the range's channel
  spacing.

### 13. The tuner's PLL, measured

On the owner's receiver, and only when the owner asks for it in that
session.

- `tools/measure_pll.py` drives a Radiolack that the owner started on the
  receiver, through its stream on 127.0.0.1: with the VFO on one steady
  carrier above 24 MHz, it moves the centre frequency through 40 values
  1.234 kHz apart, then through values that put the tuner's oscillator
  (the centre plus 3.57 MHz) on multiples of 112.5 kHz, and measures the
  carrier at each. No reference is needed: an error of the PLL shows as a
  carrier that moves with the centre frequency.
- `tests/r82xx_pll.py` holds both of the driver's computations, ported
  from rtl-sdr (GPL-2.0-or-later), and predicts each value.
- **Rule, fixed beforehand.** A spread of 30 Hz or more near 145 MHz that
  follows one of the two predictions: pull request 14 is built. Under
  10 Hz: the error is recorded as not observed, and nothing is built. In
  between: the owner sees the table.
- The result goes into `scoping.md`'s spikes, with its table.

### 14. PLL-exact centre frequencies, only if 13 says so

- For an R820T or R828D on a 28.8 MHz crystal, the source moves each
  centre frequency it is asked for, by 56 kHz at most, to one whose
  oscillator frequency is a multiple of 112.5 kHz, where both computations
  of the driver come out exact, which the tests prove with the two ported
  computations across the range. The source reports the centre in use.
- The driver's own correction then stays at 0, since a corrected reference
  breaks the exactness: Radiolack corrects the whole of `ppm` itself, labels
  the samples with the frequency they are truly centred on, constant
  offset of the RTL2832's mixer included, and scales the shift to the VFO
  by the same factor.
- The sweep, the memory scan's windows and the interface's pending retune
  compare with the centre that the source reports, not with the one they
  asked (`scanners.py:549`, `:566`, `:650`).

Limits: it rests on the driver's internals, so a fork that computes
otherwise falls back to the error it has today, silently; the sample rate's
error is no longer corrected by the receiver, at most 72 Hz at the edge of
a span for a crystal 60 ppm off.

## Band plan file, format 2

Format 1's fields stay, with `step` as the channel spacing alone. Added:

```json
{
  "format": 2,
  "channels": [
    {"name": "PMR446", "from": 446000000, "to": 446200000, "label": "ch {n}",
     "spacing": 12500, "first": 446006250, "count": 16, "number": 1},
    {"name": "Maritime VHF", "from": 156000000, "to": 162050000, "label": "ch {n}",
     "spacing": 25000, "list": [{"n": "16", "f": 156800000, "note": "Distress, safety and calling"}]}
  ],
  "spots": [
    {"f": 10000000, "name": "Standard frequency (10 MHz)", "type": "science", "mode": "AM", "reference": true}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `channels[].name` | The set's name: English, ASCII, at most 40 characters. |
| `channels[].from`, `to` | Where the set applies, in whole Hz, inside one band; sets do not overlap. |
| `channels[].label` | A channel's label, `{n}` being its number. |
| `channels[].spacing` | Whole Hz between channels: the VFO is on a channel within half of it, and the Automatic step takes it. |
| `channels[].first`, `count`, `number` | A regular set: its first channel's centre, how many, and the first one's number. |
| `channels[].list` | Instead, each channel: `n`, its number or name, `f`, its centre, and an optional `note`. |
| `channels[].mode` | Optional, as a band's. |
| `spots[].f`, `name`, `type` | A known frequency, in whole Hz, its name, at most 40 characters, and one of `types`. |
| `spots[].mode` | Optional: the demodulator a click on the spot selects. |
| `spots[].reference` | Optional, true: a standard-frequency carrier. |

The numbers above show the format; the data comes from the sources.
`tests/test_bandplan.py` checks the bands from 0 to 10.5 GHz without gaps,
each set inside one band, each spot inside the plan, the names and the
file's size, at most 150 kB.

## Limits that remain after this milestone

- Reception stops at the receiver: nothing below about 500 kHz or above
  1766 MHz with the V3, whatever the view shows.
- Direct sampling mirrors, has no gain control, and is the only shortwave
  path checked on hardware.
- A frequency is exact to the hertz as a number. How true it is depends on
  the crystal, calibrated to about a tenth of a ppm and drifting by up to
  1 ppm with temperature, on the tuner's PLL until pull request 13 settles
  it, and on a converter's oscillator.
- The display shows 293 Hz per bin at 2.4 MS/s and 29 Hz at 240 kS/s:
  tuning finer than that is by ear, by the step, or by the carrier's
  measure.
- The band plan is written by hand from public documents and read twice:
  it is a guide, and national tables are the reference.
- The view's axis is linear: from 0 Hz to 10.5 GHz, shortwave is narrower
  than a pixel. The plan list frames any band, and the overview bar of a
  later milestone shows them all.

## Set aside

- Tuning below one hertz, for good.
- Transmitting, by the rule of 2026-11-08.
- Modifier keys for finer steps (Shift for 10 Hz, Ctrl and Shift for
  1 Hz): the zoom gives the step, and the digits the exact value.
- A band strip of two rows, and a table of every allocation by service.
- Choosing a digit with the arrow keys.
- A two-range reach in the protocol, for tuners with a gap.
