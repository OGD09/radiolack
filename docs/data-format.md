# Data format

## Files in the data directory

| File | Content | Written by RadioLack |
| --- | --- | --- |
| `airband-peaks.json` | History migrated from the prototype (legacy journal). | Never: import source only. |
| `observations.json` | Working store: observations, squelch memories, imports. | Yes, atomically. |
| `observations.json.lock` | Lock file holding the PID of the writing process. | Yes. |
| `rtl_tcp.log` | Output of the `rtl_tcp` child. | Yes (appended). |
| `gqrx-radiolack.conf` | Separate Gqrx profile. | Once, never overwritten. |

None of these files is versioned.

## Store document

```json
{
  "format": "radiolack.observations",
  "format_version": 1,
  "created_at_utc": "2026-11-07T12:00:00+00:00",
  "observations": [],
  "stations": {},
  "imports": []
}
```

Every update writes a temporary file in the same directory, fsyncs it and
renames it over the store, so readers (`stats`, `check`) always see a
complete document and a failed write leaves the previous one intact. The
store is created with mode 0600. One process at a time may write it (lock
file); reading needs no lock. Non-finite numbers (`NaN`, `Infinity`) are
rejected on read and write.

## Observations

`observations` is an append-only ledger of validated listens. Each row keeps
the fields of the version that recorded it, plus:

- `id`: `obs-` and 20 hexadecimal digits, derived from the row's identity
  (see "Import identity" below);
- `origin`: `{"kind": "legacy-import", "import_id", "source_index"}` for
  imported rows, `{"kind": "radiolack", "version"}` for new ones.

Times are UTC ISO 8601 strings. Unknown values are `null` or absent; they are
never filled with zeros or with current parameters.

### Generations

| Generation | Rows in the migrated history | Fields |
| --- | --- | --- |
| Minimal (prototype) | 19 | Core fields below. |
| Squelch (prototype) | 58 | Core + `squelch_dbfs`, `squelch_source`, `gqrx_noise_floor_dbfs`. |
| Telemetry v1 (prototype) | 227 | Squelch + telemetry v1. |
| Telemetry v2 (RadioLack) | new rows | Telemetry v1 + v2 additions. |

Six legacy rows were validated with an 8 dB margin and 298 with 15 dB; each
row keeps its own `required_margin_db`.

Core fields: `frequency_hz`, `frequency_mhz`, `mode`, `filter_hz`,
`listening_started_at_utc`, `confirmed_at_utc`, `listening_seconds`,
`active_seconds`, `observed_seconds`, `median_margin_db`, `peak_margin_db`,
`required_margin_db`, `minimum_active_seconds`, `receiver_center_hz`,
`sample_rate_hz`.

Telemetry v1 fields:

- `telemetry_version`, `actual_elapsed_seconds`, `coverage_fraction`
  (observed / listen duration), `active_fraction_of_observed`,
  `fft_observations`, `signal_peak_observations`, `observed_signal_bursts`,
  `longest_active_run_seconds`, `max_observation_gap_seconds`;
- `fft_measurements`: summaries of `signal_bin_dbfs`, `local_noise_bin_dbfs`,
  `margin_db`, `peak_bin_offset_from_channel_hz`, plus `fft_size`,
  `averages`, `window`, `resolution_hz`. FFT-bin values, not dBm;
- `gqrx_measurements`: summaries of `meter_dbfs` (Gqrx S-meter) and
  `readback_squelch_dbfs`, `meter_above_squelch_fraction`, `gain_stages_db`;
- `receiver_context` (see stations), `scan` (`mode`, `cycle`,
  `window_number`, `window_count`, `window_observation_seconds`) and
  `quality_flags` (`limited_fft_coverage`: coverage below 80 %;
  `provisional_squelch`: squelch not learned from noise). Legacy rows use
  `scan.mode` `full_airband`; new rows use `full_band` or `fixed_window`.

Telemetry v2 additions:

- `minimum_observed_seconds`: the fresh-observation criterion in force;
- `squelch_changes`: `offset_seconds` from the listen start,
  `requested_dbfs`, `readback_dbfs`, `reason` (`noise` or `carrier_guard`);
- in `gqrx_measurements`: `fft_state_guard_seconds`,
  `meter_dbfs_fft_active_settled`, `meter_dbfs_fft_quiet_settled`,
  `meter_samples_unsettled`, `meter_above_squelch_fraction_fft_active_settled`,
  `invalid_meter_readings`;
- `squelch_carrier_meter_dbfs`, `squelch_learning_version`, `profile`,
  `confirmation_observations`, `cooldown_seconds`, `recorded_by`.

A summary object is `{"samples", "min", "p10", "median", "mean", "p90",
"max", "stddev"}` rounded to 0.001, or `null` when nothing was measured.

## Stations (squelch memories)

`stations` maps a frequency in Hz (string) to the squelch memory applied on
the next visit:

| Field | Meaning |
| --- | --- |
| `squelch_dbfs` | Requested squelch for the next visit (dBFS, -150..0). |
| `source` | `noise` (learned from quiet readings), `signal_fallback` (provisional, from carrier readings) or `carrier_guard` (lowered below a measured carrier). |
| `noise_floor_dbfs` | Estimated noise from settled quiet meter readings, or `null`. |
| `noise_measured_at_utc` | When that estimate was made. |
| `provisional_squelch_dbfs`, `provisional_measured_at_utc` | First provisional setting of a new memory. |
| `carrier_meter_dbfs`, `carrier_measured_at_utc` | Settled carrier level on the meter, used as a ceiling. |
| `last_verified_squelch_dbfs`, `last_verified_at_utc` | Last value read back from Gqrx. |
| `receiver_context` | `sample_rate_hz`, `filter_hz`, `mode`, `receiver_controls` (rtl_tcp command code and value pairs, such as `[4, 296]` for 29.6 dB gain). |
| `updated_at_utc`, `learning_version` | Last change; 2 for memories written by RadioLack (absent in legacy memories). |

A memory is used only when the current receiver context is identical: any
gain, sample-rate, filter or mode change invalidates it.

## Imports

`imports` lists each import that changed the store: `id`, `kind`,
`imported_at_utc`, `imported_by`, `source_path`, `source_sha256`,
`source_bytes`, `source_schema_version`, `source_detector`, and the counts of
source rows, added rows, rows already present, conflicts, stations added,
unchanged or kept, and legacy counter mismatches.

## Import identity

- A row with both `listening_started_at_utc` and `confirmed_at_utc` is
  identified by `listen|frequency|start|confirmation`; other rows by their
  full canonical JSON content. A counter separates identical rows within one
  source (`#0`, `#1`, ...).
- A row whose identity is already stored with identical content is counted
  as already present; with different content it is a conflict and the stored
  row is kept.
- A legacy squelch memory is added only when the store has none for that
  frequency; an existing memory (possibly updated since) is never replaced.
- If nothing is new, the store file is not rewritten.
- The legacy `frequency_stats` counters are compared with counts recomputed
  from the rows; differences are reported, and the rows win.
- Only journals with `schema_version` 1 and `detector` `fft-peaks` are
  accepted. The old sequential scanner's format is rejected.

## Statistics

`radiolack stats --json` computes, per frequency: `validated_detection_count`,
`first_confirmed_at_utc`, `last_confirmed_at_utc` (by parsed time),
`confirmations_by_utc_hour`, `total_listening_seconds`,
`total_observed_seconds`, `total_active_seconds` (each with a
`*_supporting_detections` count), `active_fraction_of_observed` (from rows
that have both values), `mean_detection_median_margin_db`,
`highest_peak_margin_db`, `margin_supporting_detections`,
`last_validated_squelch_dbfs`, `telemetry_detections`,
`validations_by_required_margin_db`, sample-weighted `mean_signal_bin_dbfs`
and `mean_gqrx_meter_dbfs` with their maxima and supporting counts, and
`unparsed_timestamps` when some times could not be read. The first fields
reproduce the prototype's `frequency_stats` exactly for the migrated history.

The summary gives `validated_listens`, `distinct_frequencies`,
`stations_with_squelch_memory`, the first and last confirmation, and counts by
origin, telemetry version and required margin.
