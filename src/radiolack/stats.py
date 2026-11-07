"""Telemetry summaries and per-frequency statistics computed from observations.

Counts are validated listens, not conversations, transmissions or users: a
long transmission can be validated again and short ones can be missed.
Missing measurements stay unknown (``None``) and every aggregate reports how
many observations contributed to it. Historical rows are never given the
parameters or measurements of later versions.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math

import numpy as np


def measurement_summary(values):
    """Compact statistics; absent measurements stay unknown, never zero-filled."""
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    if not np.isfinite(array).all():
        raise ValueError("Non-finite telemetry measurement")
    return {"samples": len(values), "min": round(float(array.min()), 3),
            "p10": round(float(np.percentile(array, 10)), 3),
            "median": round(float(np.median(array)), 3),
            "mean": round(float(array.mean()), 3),
            "p90": round(float(np.percentile(array, 90)), 3),
            "max": round(float(array.max()), 3), "stddev": round(float(array.std()), 3)}


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_utc(timestamp) -> datetime | None:
    """Timezone-aware ISO 8601 timestamp converted to UTC, else None."""
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def event_timestamp(event):
    return event.get("confirmed_at_utc") or event.get("listening_started_at_utc")


def _validate(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid observation: not an object")
    frequency = event.get("frequency_hz")
    if type(frequency) is not int or frequency <= 0:
        raise ValueError("Invalid observation frequency")
    timestamp = event_timestamp(event)
    if timestamp is not None and not isinstance(timestamp, str):
        raise ValueError("Invalid observation timestamp")
    return frequency, timestamp


def _summary_value(event, group, name, field):
    summary = (event.get(group) or {}).get(name)
    if isinstance(summary, dict) and is_number(summary.get(field)) and is_number(summary.get("samples")):
        return summary[field], summary["samples"]
    return None


def frequency_statistics(events) -> dict[str, dict]:
    """Per-frequency aggregates keyed by frequency in Hz (as a string)."""
    stats = {}
    for event in events:
        frequency, timestamp = _validate(event)
        item = stats.setdefault(str(frequency), {
            "frequency_hz": frequency, "frequency_mhz": round(frequency / 1000000, 6),
            "validated_detection_count": 0, "first_confirmed_at_utc": None,
            "last_confirmed_at_utc": None, "confirmations_by_utc_hour": {},
            "_first": None, "_last": None, "_unparsed": 0,
        })
        item["validated_detection_count"] += 1
        parsed = parse_utc(timestamp)
        if parsed is not None:
            if item["_first"] is None or parsed < item["_first"]:
                item["_first"], item["first_confirmed_at_utc"] = parsed, timestamp
            if item["_last"] is None or parsed >= item["_last"]:
                item["_last"], item["last_confirmed_at_utc"] = parsed, timestamp
            histogram = item["confirmations_by_utc_hour"]
            hour = f"{parsed.hour:02d}"
            histogram[hour] = histogram.get(hour, 0) + 1
        elif timestamp is not None:
            item["_unparsed"] += 1
        # Accumulate only measurements actually present in each historical row.
        for key in ("listening_seconds", "observed_seconds", "active_seconds",
                    "median_margin_db", "peak_margin_db"):
            if is_number(event.get(key)):
                item.setdefault("_" + key, []).append(event[key])
        if is_number(event.get("squelch_dbfs")):
            item["last_validated_squelch_dbfs"] = event["squelch_dbfs"]
        if is_number(event.get("observed_seconds")) and is_number(event.get("active_seconds")):
            item["_paired_observed"] = item.get("_paired_observed", 0) + event["observed_seconds"]
            item["_paired_active"] = item.get("_paired_active", 0) + event["active_seconds"]
        if event.get("telemetry_version") is not None:
            item["telemetry_detections"] = item.get("telemetry_detections", 0) + 1
        margin = event.get("required_margin_db")
        if is_number(margin):
            thresholds = item.setdefault("validations_by_required_margin_db", {})
            thresholds[f"{margin:g}"] = thresholds.get(f"{margin:g}", 0) + 1
        for prefix, group, name in (("signal_bin", "fft_measurements", "signal_bin_dbfs"),
                                    ("gqrx_meter", "gqrx_measurements", "meter_dbfs")):
            mean = _summary_value(event, group, name, "mean")
            maximum = _summary_value(event, group, name, "max")
            if mean is not None and maximum is not None and mean[1] > 0:
                item.setdefault(f"_{prefix}", []).append((mean[0], mean[1], maximum[0]))
    for item in stats.values():
        del item["_first"], item["_last"]
        unparsed = item.pop("_unparsed")
        if unparsed:
            item["unparsed_timestamps"] = unparsed
        item["confirmations_by_utc_hour"] = dict(sorted(item["confirmations_by_utc_hour"].items()))
        for key in ("listening_seconds", "observed_seconds", "active_seconds"):
            values = item.pop("_" + key, [])
            item["total_" + key] = round(sum(values), 3) if values else None
            item[key + "_supporting_detections"] = len(values)
        # Use matched rows for this ratio, never unrelated partial-history sums.
        observed = item.pop("_paired_observed", 0)
        active = item.pop("_paired_active", 0)
        item["active_fraction_of_observed"] = round(active / observed, 4) if observed > 0 else None
        medians = item.pop("_median_margin_db", [])
        peaks = item.pop("_peak_margin_db", [])
        item["mean_detection_median_margin_db"] = round(sum(medians) / len(medians), 3) if medians else None
        item["highest_peak_margin_db"] = round(max(peaks), 3) if peaks else None
        item["margin_supporting_detections"] = len(medians)
        item.setdefault("last_validated_squelch_dbfs", None)
        item.setdefault("telemetry_detections", 0)
        item.setdefault("validations_by_required_margin_db", {})
        for prefix in ("signal_bin", "gqrx_meter"):
            rows = item.pop(f"_{prefix}", [])
            weight = sum(samples for _, samples, _ in rows)
            item[f"mean_{prefix}_dbfs"] = (round(sum(mean * samples for mean, samples, _ in rows) / weight, 3)
                                          if weight else None)
            item[f"max_{prefix}_dbfs"] = round(max(maximum for _, _, maximum in rows), 3) if rows else None
            item[f"{prefix}_supporting_detections"] = len(rows)
    return dict(sorted(stats.items(), key=lambda pair: int(pair[0])))


def store_summary(observations, stations) -> dict:
    """Global counts across the whole observation history."""
    first = last = None
    first_text = last_text = None
    origins, versions, margins = Counter(), Counter(), Counter()
    for event in observations:
        _validate(event)
        parsed = parse_utc(event_timestamp(event))
        if parsed is not None:
            if first is None or parsed < first:
                first, first_text = parsed, event_timestamp(event)
            if last is None or parsed >= last:
                last, last_text = parsed, event_timestamp(event)
        origins[(event.get("origin") or {}).get("kind", "unknown")] += 1
        versions[str(event.get("telemetry_version", "none"))] += 1
        if is_number(event.get("required_margin_db")):
            margins[f"{event['required_margin_db']:g}"] += 1
    return {
        "validated_listens": len(observations),
        "distinct_frequencies": len({event["frequency_hz"] for event in observations}),
        "stations_with_squelch_memory": len(stations),
        "first_confirmed_at_utc": first_text,
        "last_confirmed_at_utc": last_text,
        "by_origin": dict(sorted(origins.items())),
        "by_telemetry_version": dict(sorted(versions.items())),
        "by_required_margin_db": dict(sorted(margins.items(), key=lambda pair: float(pair[0]))),
    }
