# Squelch learning

Gqrx's squelch gates the audio. RadioLack keeps one squelch memory per
frequency so that a revisited station opens at once while noise stays muted.
Detection never depends on the squelch: it runs on the FFT of the shared I/Q
stream. Squelch values always come from Gqrx's S-meter (dBFS of the
demodulated channel), never from FFT-bin levels.

## Diagnosis of the prototype memories

The migrated history (227 listens with telemetry) shows that the prototype
often learned the squelch from carrier levels:

- in 49 listens the meter never rose above the squelch
  (`meter_above_squelch_fraction` 0): the squelch stayed closed for the whole
  listen; all 49 used a squelch learned as "noise";
- in 123 listens the learned noise floor was within 3 dB of, or above, the
  median meter level measured during the listen;
- at 130.475 MHz the meter minimum stayed near -54 dBFS (the real noise),
  but the learned "noise" values were -44.5, -48.0 and -45.3 dBFS. The squelch
  went to -46.8, -45.0, then -43.0 dBFS, and 13 of the 32 listens of that
  frequency had the meter never above the squelch.

Cause: the FFT tap sees samples as soon as they reach the bridge, while
Gqrx demodulates them later and its meter averages. When a carrier stops, the
FFT is quiet but the meter still shows the carrier for a while. The prototype
tested "quiet" at the instant of each meter reading, so carrier tails were
learned as noise and the squelch climbed above the carrier.

25 of the 29 legacy memories are noise-learned. They are imported
unchanged; the carrier guard below corrects them during the next listen in
which the carrier is measured.

## Rules (learning version 2)

1. **Classification.** During a listen, and afterwards while Gqrx stays on a
   validated station, the meter is read every 200 ms. Each reading is
   classified with the FFT activity near the station (a relaxed 6 dB margin,
   within one channel spacing): `active_settled` or `quiet_settled` once the
   FFT state has been stable for the guard (1.5 s, `--meter-guard-seconds`),
   otherwise `transition` or `unknown`, which are ignored.
2. **Carrier level.** The median of at least three active-settled readings
   of the current visit, else the carrier level stored in the memory.
3. **Noise.** At least four quiet-settled readings spanning 0.5 s within a
   3 dB range (sliding window of six). Their median is rejected, and counted,
   when it is less than 6 dB below the carrier level.
4. **Target.** Noise + 3 dB, never above carrier - 3 dB, clamped to
   -149..-1 dBFS.
5. **Changes.** Differences under 1.5 dB are ignored. Raising is limited to
   2 dB per update with at least 2 s between updates; lowering (the safe
   direction for reception) is immediate.
6. **Carrier guard.** Once the carrier level is known, the memorized squelch
   is lowered at once (`carrier_guard`) when it is 0.5 dB or more above its
   safe target: the stored noise + 3 dB when that noise is at least 6 dB below
   the carrier (the same separation as rule 3), else carrier - 3 dB. Busy
   channels rarely stay quiet for the guard plus 0.5 s, so rule 3 may never
   run there; a target at carrier - 3 dB alone would mute weaker stations on
   the same channel. Prototype noise estimates learned from carrier tails sit
   close to or above the carrier and are ignored by the separation test.
7. **Application.** During a listen every change is applied, read back and
   recorded in `squelch_changes`. Outside a listen audio stays muted; changes
   are saved for validated stations and applied on the next visit.
8. **New frequency.** Without a matching memory, the provisional squelch is
   6 dB below the lowest of three settled meter readings (`signal_fallback`):
   it keeps the detected carrier audible without claiming a noise measurement.
   A rejected first listen creates no memory.

On every visit RadioLack checks the receiver context, sets mode, filter and
frequency, applies the memorized squelch last and reads it back; a mismatch
stops the monitor with a Gqrx error. A gain, sample-rate, filter or mode
change makes the memory inapplicable.

## Distinct values

| Value | Field |
| --- | --- |
| Estimated noise | `noise_floor_dbfs`, `noise_measured_at_utc` |
| Provisional setting | `provisional_squelch_dbfs`, `provisional_measured_at_utc` |
| Measured carrier | `carrier_meter_dbfs`, `carrier_measured_at_utc` |
| Requested squelch | `squelch_dbfs`, `source`, `updated_at_utc` |
| Read-back squelch | `last_verified_squelch_dbfs`, `last_verified_at_utc` |
| Context | `receiver_context` |

## Checking it on hardware

Each new observation records `meter_dbfs_fft_quiet_settled`,
`meter_dbfs_fft_active_settled`, `meter_samples_unsettled` and
`meter_above_squelch_fraction_fft_active_settled`:

- quiet-settled readings should sit at the noise level, clearly below the
  active-settled ones; if they still show carrier levels, the meter lags more
  than the guard: raise `--meter-guard-seconds` (for example to 2.5);
- the active-settled fraction above the squelch should stay close to 1 for
  real carriers;
- a large `meter_samples_unsettled` with no learning means stations are
  rarely quiet long enough; the guard can be lowered, but not below the lag.

First hardware session (20 minutes, 58 saved listens on 22 frequencies):
quiet-settled medians stayed between -60 and -54 dBFS, a median of 11.5 dB
below the active-settled ones, and the active-settled fraction above the
squelch had a median of 1.0 (never below 0.5). On a busy channel with a
strong station (120.775 MHz: carrier -28.9 dBFS, stored noise -57.2 dBFS)
quiet gaps never lasted long enough to measure noise in four listens, and the
first guard left the squelch at carrier - 3 dB; rule 6 now uses the credible
stored noise instead (-54.2 dBFS there).

## Residual risk

If the meter lags more than the guard before any carrier level is known for
that frequency, a long carrier tail can still be taken for noise. The 3 dB
range check rejects short tails, raising is limited to 2 dB per update, and
the next settled carrier reading lowers the squelch below the carrier.
