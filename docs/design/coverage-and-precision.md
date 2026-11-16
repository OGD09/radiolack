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
  frequency. Leaving: 0x09 with 0, then the frequency, the sample rate and
  the gain, which the tuner forgot. At each connection: the correction,
  0x09, the frequency, the sample rate, the gain. The samples of the
  settling time are dropped, as after any retune. 0x08 follows 0x09 at each
  connection too, since rtl_tcp keeps it from its previous client. As
  first built, the sample rate came before 0x09 at each connection and was
  not sent on leaving, which left the tuner's filter at its widest and its
  IF at 3.57 MHz whatever the sample rate: pull request 13's measure found
  it, and the order above, with the reasons, is section 13's "As built".
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

As built, where this section left a choice open or a rule had to bend:

- The split lives in `correction.py`, with the source's
  `hardware_correction` saying whether it takes a whole part itself; the
  value is kept as hundredths, and w is rounded half away from zero (0.5
  gives 1, −0.5 gives −1, 99.5 gives 100), within the range whatever the
  value.
- On the tuner, the frequency asked stays within the tuner's range and
  32 bits, an exception to "within 0.5 Hz": a scaled request just below
  24 MHz would make the RTL-SDR Blog driver, which takes 0x09 = 0 as
  automatic, switch to direct sampling by itself, and one just above the
  tuner's top may not lock. Within half a ppm of an edge, the samples'
  label may then be off by as much: 12 Hz at 24 MHz, 883 Hz at 1766 MHz.
  Direct sampling, chosen by 0x09 beforehand, and a tuner that covers
  shortwave keep the scaled request. Whether the receiver samples directly
  is decided on the frequency it truly tunes, as the reach is.
- An interface older than this pull request checks that `ppm` is whole,
  and drops a source state that holds hundredths until it is reloaded:
  a page left open across an upgrade. Protocol version 6 stays, as the
  section asks; raising it would have refused those pages outright.

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

As built, where this section left a choice open or a rule had to bend:

- The measure lives in `carrier.py`, which knows nothing of the engine or
  the dialog: `strongest_line` measures a stretch of channel, and
  `CarrierMeasure` gathers it as the demodulator makes it, through a tap
  after the channel filter. The hello lists `carrier` under `measures`.
- A fourth reason, `several`: another line, more than 5 Hz from the
  strongest, within 6 dB of it. Without it, two steady carriers of nearly
  equal strength would give a stable measure of either one, and a
  correction computed for the other; an FM signal whose carrier vanishes
  shows the same way. `silent` also covers WFM below its channel rate and
  a receiver that sends nothing for 6 s.
- The measure starts again whenever the receiver's settings, the VFO's
  frequency, mode or filter, or the span change, so that its two seconds
  come from one setting; one that cannot finish within 6 s, as while a
  memory scan moves the VFO, answers `unstable`. Measuring never stops a
  scan.
- The request and the answer carry an optional `id`: the dialog ignores
  the answer to an older request, which may hold a measure taken before
  the correction or the converter changed, delivered after the new source
  state.
- The correction is computed in `calibration.js`, where the derivation is
  written: p' = (1 + p) × T_s / T_m − 1, on the frequencies the receiver
  tunes, T_s the true one and T_m the one measured, so that behind a
  converter the oscillator is taken as exact and the crystal scales the
  receiver's own frequencies only.
- A true frequency typed far from the carrier: both corrections lie within
  −100 to 100 ppm, so no change legitimately exceeds 200 ppm, and the range
  check refuses any typo that far, with a message that asks to check the
  frequency and the VFO. A change of more than 10 ppm, more than a crystal
  drifts but possible for a cheap dongle never calibrated, is warned of,
  and Apply stays available.
- Apply also tunes the VFO to the true frequency, keeping its mode and
  filter: the carrier moves by the change, 5.4 kHz at 145 MHz for 37 ppm,
  which would leave it outside the filter, and the next measure would find
  nothing. The dialog stays open on the next measures, and Cancel then
  reads Close.
- The dialog notes that a converter's oscillator is taken as exact when
  one is set, and that the tuner's own error is included when the
  receiver could sample directly but does not.

Limits: the result is as true as the frequency typed; on shortwave the
ionosphere moves a carrier by up to about 1 Hz; with a converter it takes
the converter's oscillator as exact; on the tuner it also takes in the
tuner's own error at that centre frequency (pull request 13), so the
crystal alone is best measured in direct sampling. As built: the receiver
corrects its sample rate by whole ppm only, which moves a carrier up to
half a ppm of its distance from the centre frequency (0.6 Hz at the edge
of a 2.4 MS/s span), and the frequency asked of it is rounded to the hertz
(pull request 9), so a carrier measures within about a hertz of its true
frequency after Apply, not exactly on it. A carrier on the centre
frequency goes with the DC removal, and the dialog says so.

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

As built, where this section left a choice open or a rule had to bend
([`band-plan.md`](band-plan.md) has the sources and the second reading):

- 252 bands and 130 segments, from the ECA of 9 October 2026, the Radio
  Regulations of 2024 and the IARU Region 1 plans. The ECA's edges are
  followed more closely than format 1 did: radiolocation, off-route
  aviation, the ISM bands at 6.78, 13.56 and 40.68 MHz and the ITS band
  have their own ranges.
- IARU Region 1 had published no new band plan edition after its Vienna
  conference of September 2026 when this was written: the plan keeps the
  HF plan of 2020 and the VHF Handbook 10.03, and the conference's final
  plenary minutes were read instead. None of its adopted changes moves an
  edge used here; its supplementary 23 cm plan applies only where ECC
  Decision (25)01 is implemented, a national matter, and is left out.
- The amateur bands are shown over the uses they share their range with,
  as the 3 cm band is: 13 cm up to 2450 MHz, 9 cm (3400 to 3410 MHz) and
  6 cm (5650 to 5850 MHz) are new, with Wi-Fi, mobile networks and
  short-range devices named around them.
- Two ranges of format 1 had edges that no source gives and are gone: the
  77 to 78 kHz time signal band, whose frequency the ECA names and pull
  request 12 adds as a known frequency, and the 1030 MHz interrogation
  segment, as ICAO Annex 10 was not read. ADS-B keeps the 1087.7 to
  1092.3 MHz of the Radio Regulations' footnote 5.328AA.
- Names that named a station, an operator or a use no source gives were
  rewritten: the time signal's call sign, satellite operators, "Military
  aviation HF" (now "Aeronautical HF (off-route)", as the allocation says)
  and "ISS" (now "Space communication", the IARU plan's words).
- The steps outside the amateur bands are kept from format 1 without a
  source: pull request 12's channel sets bring their spacings.
- The second reading found no edge that its sources contradict and ten
  points of naming and detail; eight were corrected, and two are kept with
  their reasons in `band-plan.md`: "Wi-Fi" for the ECA's RLAN, the word a
  listener knows and the one section 12 uses, and the 1.5 GHz band named
  after its harmonised use without the T-DAB of some countries.
- "Not allocated" has a type of its own, `unallocated`.
- `bandplan.js` returns the channel sets with every channel, a regular set
  expanded, and the spots, each sorted by frequency; the format also
  requires at least one channel per set, every channel inside its set, a
  spacing above 0 and `reference` only as `true`.
- The legal notice (About and README.md) still says "the band plan follows
  a simplified European table": it is the owner's text, unchanged here.

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

As built, where this section left a choice open or a rule had to bend
([`band-plan.md`](band-plan.md) has the sources, what was left out and the
second reading):

- 16 channel sets (680 channels) and 141 known frequencies, below the 25
  sets and the 150 spots this section expected: the short-range devices at
  433 MHz and the Wi-Fi channels are left out, as no source read numbers
  them, and so are the 4 m and 10 m FM simplex channels, which the IARU
  plans space but do not number. Band III television is left out as a set
  because the DAB blocks take its range, and sets do not overlap.
- **"Inside one band" became "inside the plan".** Maritime VHF's channels
  run from the ship stations' 156.025 MHz to the coast stations' 162.025
  MHz and AIS 2, across four bands of the plan (maritime VHF, land mobile,
  the coast stations' range and AIS): one set must span them, as its
  numbers do. A set now lies inside the plan and may span several bands;
  sets still do not overlap, and a set's channels lie at least its spacing
  apart, so that a frequency is on one channel at most. The display assumes
  no set's band: it finds sets by frequency, and the Automatic step reads
  the set beside the VFO, not the range's.
- **The Automatic step on a set** applies where one of the set's channels
  lies less than one spacing from the VFO, while the view shows two
  channels or more: between maritime VHF's ship and coast channels, the
  range's own step applies. There, a click lands on the nearest channel
  rather than on a multiple of the spacing, and the step keys go from
  channel to channel, as CB's channels sit 5 kHz past the 10 kHz, PMR446's
  6.25 kHz past the 12.5 kHz and the DAB blocks 1.712 MHz apart with gaps.
  Past a gap of more than two spacings, or past the set's last channel, the
  keys move by the spacing. CB, PMR446 and maritime VHF lost the step of
  their ranges (5, 6.25 and 25 kHz, unsourced) to their sets' spacings.
- **The line under the frequency** shows the known frequency or the
  channel in place of the range's name: "PMR446 · ch 3 · step 12.5 kHz",
  "2 m FM · RV48 · step 12.5 kHz". A known frequency's name comes after its
  band's, as names repeat from band to band: "2 m amateur · SSB centre".
  The same full names go to the tooltips, the filtered plan list and new
  bookmarks; the strip writes the short name.
- **On the strip**, the ranges' names keep their places first, then the
  known frequencies' names, then the channels' numbers, each left out
  where it would touch one already placed. A number is the channel's
  designator alone ("RV48", "5A", "3"). A press lands on a known
  frequency's mark in the strip's upper half or a channel's tick in its
  lower half, within 4 px for a mouse or 10 px for a finger, a tick only
  while ticks lie at least twice that apart, so that a press never falls
  between two; a name or a number answers wherever it is drawn. Elsewhere
  the press frames the range, as before, and dragging the strip still sets
  the spectrum's height. The strip is drawn at every change of the view:
  sets and spots in view are found by bisection, and a set's channels are
  visited only once they lie 6 px apart, so at most one channel every 6 px
  of the strip and the 141 spots.
- **The plan list**, without a filter, lists the ranges in view with the
  known frequencies inside them, the bands' names over their ranges; a
  range comes with its known frequencies or not at all, so the list may
  stop a row or two short of 200. With a filter, it lists the ranges (by
  their names or their bands'), the channel sets and the known frequencies
  (by their full names) of the whole plan, in frequency order; a set's row
  says how many channels it has, and a click frames it. The filter is the
  Bookmarks menu's one field, which now filters both lists ("Filter the
  bookmarks and the band plan"). The list follows the view only while the
  menu shows, and is rebuilt only when its rows change.
- **A new bookmark**: Add current frequency, the spectrum's menu and the
  History's mark name it after the known frequency or the channel at its
  frequency, else "Bookmark n" as before; the spectrum's menu and the
  History also take the known frequency's or the set's mode.
- PMR446's channels are numbered 1 to 16 in frequency order: ECC Decision
  (15)05 and EN 303 405 give the raster, not the numbers.
- The second reading found no frequency, channel number or designator
  wrong, and fourteen points of wording, scope and detail: six names and
  notes were corrected, the others are kept with their reasons, and the
  channels and frequencies it listed as missing are left for later, all in
  `band-plan.md`.

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

As built, where this section left a choice open or a rule had to bend:

- **The oscillator is the centre plus the IF of the sample rate in use,
  not always 3.57 MHz.** librtlsdr gives the tuner a bandwidth equal to
  the sample rate it truly runs at, and the driver's IF follows it:
  3.57 MHz above 2.43 MS/s, 1.815 MHz at 2.4 MS/s, 1.7 MHz at 1.024 MS/s.
  `r82xx_pll.py` ports `r82xx_set_bandwidth` and the rate's rounding too,
  and the second sweep puts the oscillator itself on multiples of
  112.5 kHz. The table gives the oscillator of each point.
- **Only the rounding computation is exact on a multiple of 112.5 kHz.**
  The truncating one (up to rtl-sdr 2.0.1, and the RTL-SDR Blog fork)
  takes a bit of sdm only while the remainder is strictly greater than its
  weight: on an exact remainder, the last bit is left for smaller ones
  that add up to less. It is exact only when the VCO is a multiple of
  57.6 MHz (an oscillator on a multiple of 3.6 MHz near 145 MHz):
  144.1125 MHz comes out 109.9 Hz low. The second sweep therefore tells
  the two computations apart, and section 14's premise, both exact on
  those oscillators, holds for the rounding one only.
- **Drift.** The first centre is measured again after every 10 points and
  at the end. Each point's offset is taken from the reference interpolated
  in time between the measures around it, and the table gives that drift
  beside the offset. A reference that moves more than 5 Hz between two of
  its measures leaves no rule applicable: the owner sees the table, as a
  crystal still warming up cannot be told from the PLL.
- **"Follows a prediction"**: every offset within 5 Hz of it, half the
  spread under which the error is not observed. **"Near 145 MHz"**: the
  tuner's divider of 145 MHz, 16 (oscillators from 110.6 to 221.25 MHz);
  elsewhere the owner sees the table.
- **The first sweep** goes up from the centre found, or down when going up
  would bring the VFO's filter within 1 kHz of the centre frequency or
  beyond 40% of the sample rate from it. The second takes up to 8 centres,
  those whose oscillators lie nearest the first one's. Each point gets two
  measures before the carrier counts as lost; the first point one.
- **Refused at start**, with the reason: another source than an RTL-SDR,
  a tuner other than an R820T or R828D (the predictions take the 28.8 MHz
  crystal of the R820T and of the RTL-SDR Blog V4's R828D), direct
  sampling, a converter, a scan that runs, a mode whose filter is not
  centred on the VFO (AM, NFM and CW are), a filter too narrow to hold the
  carrier's predicted moves with 200 Hz to spare, a VFO below 24 MHz (or
  28.8 MHz with a V4's tuner, which upconverts below). A change of the
  receiver's or the VFO's settings by another interface, or of the centre
  frequency other than by the tool, stops it as incomplete.
- **The correction.** The predictions take the frequency that Radiolack
  asks of rtl_tcp (`correction.py`) and the crystal that librtlsdr gives
  the driver with the whole part of the correction, so they hold with any
  correction; with a whole part other than 0, the driver's crystal is no
  longer 28.8 MHz and the second sweep's oscillators are no longer exact.
- **Where it runs.** The tool reads `tests/r82xx_pll.py` by its path and
  the correction's arithmetic from `src/`: it runs from a checkout. The
  Debian package copies `src/radiolack` alone, and pip installs it alone:
  neither installs `tools/`. Exit status 0 with a verdict, 1 without one
  or when the centre frequency could not be restored, 130 after Ctrl-C.

Measured on 2026-11-16, on the owner's V3 with rtl-sdr 0.6.0
([`scoping.md`](scoping.md), spike 6, with the table):

- A spread of 203 Hz near 111 MHz, the tuner's divider of 145 MHz, that
  follows the truncating computation within 0.13 Hz at each of 53
  measures; the crystal drifted by 1.7 Hz meanwhile.
- **The oscillator was the centre plus 3.57 MHz at every sample rate**, as
  this section first had it, and not the IF of the sample rate that the
  first point above describes. That point reads the driver rightly, but
  Radiolack sends command 0x09 with 0 after the sample rate
  (`rtltcp.py`, since pull request 4), and librtlsdr's
  `rtlsdr_set_direct_sampling` then starts the tuner again: its IF goes
  back to 3.57 MHz and its filter to the widest, whatever the sample rate.
  The tool, which took the IF of the sample rate, answered that the
  carrier followed neither prediction; the figures were computed again
  from its table with the oscillator that was truly set.
- By the rule, pull request 14 is built; as the second point above says,
  its premise does not hold for this computation, so what it becomes is
  the owner's to decide.

Done after the measure, in a pull request of its own, before pull
request 14:

- **The tuner follows the sample rate again.** librtlsdr's
  `rtlsdr_set_direct_sampling` with 0 calls the tuner's init whether
  direct sampling was on or not (osmocom's rtl-sdr at 2659e2d, v2.0.1 and
  v2.0.2, and the RTL-SDR Blog fork): `r82xx_init` writes the tuner's
  registers afresh, which puts its gain back to automatic (the LNA's and
  the mixer's, and a VGA other than the one automatic gain sets), and its
  `r82xx_set_tv_standard` puts the IF back to 3.57 MHz and the filter to
  DVB-T's 6 MHz; the RTL2832 takes that IF, and the tuner retunes to the
  frequency it had. The correction stays: the init reads the crystal as
  corrected. The RTL2832's AGC and the antenna power are not the tuner's
  and stay too. `rtlsdr_set_sample_rate` sets the bandwidth, the IF and
  the RTL2832's IF again from the rate, whether the rate changed or not,
  and retunes. So `rtltcp.py` sends after 0x09 with 0, at each
  connection and on leaving direct sampling, the frequency, then the
  sample rate, then the gain: a new rate in the same change goes then,
  once. Connection: 0x05, 0x09, 0x08, 0x01, 0x02, the gain on the tuner,
  0x0e. Leaving: 0x09 with 0, 0x08 with 0 if it was on, 0x01, 0x02, the
  gain. Entering, and any change that keeps the tuner or direct sampling,
  send what they sent before.
- **Why the frequency comes before the sample rate.** In the Blog fork,
  0x09 with 0 means automatic direct sampling below 24 MHz on an R820T:
  the tuner, started again, retunes to the frequency it had, which after
  direct sampling lies below 24 MHz, so the fork goes back to direct
  sampling by itself, and the next frequency above 24 MHz leaves it,
  which starts the tuner again a second time. The sample rate after the
  frequency comes after both. With osmocom's driver, the order makes no
  difference.
- **Limit.** With the Blog fork, an R820T set to "Tuner (RTL-SDR Blog
  V4)" goes to direct sampling by itself below 24 MHz, and back to the
  tuner on a plain frequency command, which starts the tuner again: its
  filter stays at its widest until the next sample rate or connection.
  Radiolack cannot tell which driver runs, and the setting is meant for a
  V4, whose R828D the fork never switches by itself.
- **The fake rtl_tcp** starts its tuner again on 0x09 with 0: automatic
  gain, and the state `started`, whose IF `r82xx_pll.py` gives as
  3.57 MHz, until the next 0x02 (`rate`). Its optional PLL takes that
  state. The tests put a carrier where the IF of the sample rate puts it
  at connection, after a new sample rate and after direct sampling,
  within 2 Hz; with the order of before, it sits 44 Hz off at connection.
- **The tool predicts under both IFs**: each computation with the IF of
  the sample rate and with 3.57 MHz, four predictions, all in the table.
  The conclusion names those the measures follow; when that is the IF
  of a tuner started again, it says so, and what it means of the
  Radiolack measured, and gives the verdict all the same. The second
  sweep's oscillators take the IF of the sample rate, the one this
  order keeps; the tool cannot tell at start whether the Radiolack it
  measures has this order, as the calibration it needs came in between
  without a release and both say the same version. Taken the other way,
  the second sweep's oscillators miss the multiples of 112.5 kHz, which
  the conclusion says, but every point still counts for every prediction.
  The rule near 145 MHz takes the tuner's divider with the IF the
  measures follow, or the one assumed.
- **Ctrl-C.** The tool waited for the stream through `asyncio.wait_for`,
  which in Python 3.11 may swallow a cancellation that comes as its wait
  ends: about once in thirty runs of its test, Ctrl-C was lost and the
  run went to its end. It waits through `asyncio.wait` instead.
- **What a new measure should show.** At 2.4 MS/s, the oscillator at the
  centre plus 1.815 MHz, and, with rtl-sdr 0.6.0 as on the owner's V3,
  the conclusion "up to 2.0.1, IF of the rate" followed. Each offset
  changes with the oscillator, but not the spread's size: by the port's
  predictions, 170 to 220 Hz over a first sweep near 111 MHz under either
  IF, the tuner's divider being 16 under both.

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
| `channels[].from`, `to` | Where the set applies, in whole Hz, inside the plan; sets do not overlap. As built in pull request 12, a set may span several bands (maritime VHF). |
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
each set inside the plan, each spot inside the plan, the names and the
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
