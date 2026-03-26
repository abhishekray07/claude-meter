#!/usr/bin/env python3
"""Analyze normalized Claude sniffer logs for limit-estimation spikes."""

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path


MODEL_PRICE_UNITS_5M = {
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
}


def load_records(log_path):
    with Path(log_path).open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def usage_value(record, meter="effective_tokens_raw", cache_read_weight=1.0):
    model = _coalesce_model(record)
    usage = record.get("usage", {}) or {}
    input_tokens = _numeric_usage_value(usage.get("input_tokens"))
    cache_creation_input_tokens = _numeric_usage_value(
        usage.get("cache_creation_input_tokens")
    )
    cache_read_input_tokens = _numeric_usage_value(usage.get("cache_read_input_tokens"))
    output_tokens = _numeric_usage_value(usage.get("output_tokens"))

    if meter == "effective_tokens_raw":
        return (
            input_tokens
            + cache_creation_input_tokens
            + cache_read_input_tokens
            + output_tokens
        )
    if meter == "effective_tokens_no_cache_read":
        return input_tokens + cache_creation_input_tokens + output_tokens
    if meter == "effective_tokens_io_only":
        return input_tokens + output_tokens
    if meter == "effective_tokens_weighted":
        return (
            input_tokens
            + cache_creation_input_tokens
            + (cache_read_input_tokens * cache_read_weight)
            + output_tokens
        )
    if meter == "price_equivalent_5m":
        price_units = _model_price_units_5m(model)
        return (
            input_tokens * price_units["input"]
            + cache_creation_input_tokens * price_units["cache_write"]
            + cache_read_input_tokens * price_units["cache_read"]
            + output_tokens * price_units["output"]
        )

    raise ValueError(f"unknown usage meter: {meter}")


def _effective_tokens(record):
    return usage_value(record, meter="effective_tokens_raw")


def _numeric_usage_value(value):
    if isinstance(value, (int, float)):
        return value
    return 0


def _coalesce_model(record):
    return record.get("response_model") or record.get("request_model") or ""


def _model_price_units_5m(model):
    for prefix, price_units in MODEL_PRICE_UNITS_5M.items():
        if model.startswith(prefix):
            return price_units
    return MODEL_PRICE_UNITS_5M["claude-opus-4-6"]


def _record_sort_timestamp(record):
    return record.get("response_timestamp") or record.get("request_timestamp") or ""


def _parse_iso_timestamp(value):
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _account_fingerprint(record):
    return record.get("account_fingerprint") or "unknown"


def _has_usable_usage(record):
    usage = record.get("usage", {}) or {}
    return any(
        isinstance(usage.get(field), (int, float))
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def build_utilization_intervals(records, meter="effective_tokens_raw", cache_read_weight=1.0):
    eligible = []
    for record in records:
        if not (200 <= (record.get("status") or 0) < 300):
            continue
        if not record.get("request_timestamp"):
            continue
        windows = ((record.get("ratelimit") or {}).get("windows") or {})
        for window_name, window_data in windows.items():
            utilization = window_data.get("utilization")
            if not isinstance(utilization, (int, float)):
                continue
            eligible.append(
                {
                    "record": record,
                    "window": window_name,
                    "utilization": utilization,
                    "account_fingerprint": _account_fingerprint(record),
                    "declared_plan_tier": record.get("declared_plan_tier") or "unknown",
                    "sort_timestamp": _record_sort_timestamp(record),
                }
            )

    eligible.sort(
        key=lambda record: (
            record["account_fingerprint"],
            record["declared_plan_tier"],
            record["window"],
            record["sort_timestamp"],
            record["record"].get("id") or 0,
        )
    )

    intervals = []
    previous_by_group_window = {}

    for item in eligible:
        group = item["declared_plan_tier"]
        window_name = item["window"]
        record = item["record"]
        utilization = item["utilization"]
        account_fingerprint = item["account_fingerprint"]

        key = (account_fingerprint, group, window_name)
        state = previous_by_group_window.get(key)
        if state is None:
            previous_by_group_window[key] = {
                "anchor_record": record,
                "anchor_utilization": utilization,
                "pending_records": [],
            }
            continue

        anchor_utilization = state["anchor_utilization"]
        if utilization > anchor_utilization:
            interval_records = list(state["pending_records"])
            interval_records.append(record)
            complete_usage = all(_has_usable_usage(interval_record) for interval_record in interval_records)
            if complete_usage:
                usage_total = sum(
                    usage_value(interval_record, meter=meter, cache_read_weight=cache_read_weight)
                    for interval_record in interval_records
                )
                implied_cap = usage_total / round(utilization - anchor_utilization, 10)
            else:
                usage_total = None
                implied_cap = None
            delta_utilization = round(utilization - anchor_utilization, 10)
            intervals.append(
                {
                    "account_fingerprint": account_fingerprint,
                    "declared_plan_tier": group,
                    "window": window_name,
                    "start_id": interval_records[0].get("id"),
                    "end_id": record.get("id"),
                    "start_timestamp": interval_records[0].get("request_timestamp"),
                    "end_timestamp": record.get("request_timestamp"),
                    "utilization_before": anchor_utilization,
                    "utilization_after": utilization,
                    "delta_utilization": delta_utilization,
                    "record_count": len(interval_records),
                    "meter": meter,
                    "complete_usage": complete_usage,
                    "usage_total": usage_total,
                    "implied_cap": implied_cap,
                    "models": sorted(
                        {
                            model
                            for interval_record in interval_records
                            for model in (_coalesce_model(interval_record),)
                            if model
                        }
                    ),
                }
            )
            previous_by_group_window[key] = {
                "anchor_record": record,
                "anchor_utilization": utilization,
                "pending_records": [],
            }
        elif utilization == anchor_utilization:
            state["pending_records"].append(record)
        else:
            previous_by_group_window[key] = {
                "anchor_record": record,
                "anchor_utilization": utilization,
                "pending_records": [],
            }

    return intervals


def summarize_windows(records):
    summary = {}
    for record in records:
        windows = ((record.get("ratelimit") or {}).get("windows") or {})
        model = record.get("response_model")
        for window_name, window_data in windows.items():
            item = summary.setdefault(
                window_name,
                {
                    "count": 0,
                    "statuses": {},
                    "min_utilization": None,
                    "max_utilization": None,
                    "models": set(),
                },
            )
            item["count"] += 1

            status = window_data.get("status")
            if status:
                item["statuses"][status] = item["statuses"].get(status, 0) + 1

            utilization = window_data.get("utilization")
            if isinstance(utilization, (int, float)):
                if item["min_utilization"] is None or utilization < item["min_utilization"]:
                    item["min_utilization"] = utilization
                if item["max_utilization"] is None or utilization > item["max_utilization"]:
                    item["max_utilization"] = utilization

            if model:
                item["models"].add(model)

    rendered = {}
    for window_name, item in summary.items():
        rendered[window_name] = {
            "count": item["count"],
            "statuses": item["statuses"],
            "min_utilization": item["min_utilization"],
            "max_utilization": item["max_utilization"],
            "models": sorted(item["models"]),
        }
    return rendered


def build_adjacent_deltas(records, meter="effective_tokens_raw", cache_read_weight=1.0):
    eligible = []
    for record in records:
        if not (200 <= (record.get("status") or 0) < 300):
            continue
        if not record.get("session_id"):
            continue
        if not record.get("request_timestamp"):
            continue
        eligible.append(record)

    eligible.sort(key=lambda record: (record.get("session_id"), record.get("request_timestamp")))

    deltas = []
    previous_by_session_window = {}
    for record in eligible:
        windows = ((record.get("ratelimit") or {}).get("windows") or {})
        for window_name, window_data in windows.items():
            utilization = window_data.get("utilization")
            if not isinstance(utilization, (int, float)):
                continue

            key = (record["session_id"], window_name)
            previous = previous_by_session_window.get(key)
            if previous is not None:
                prev_utilization = previous["window_data"].get("utilization")
                if isinstance(prev_utilization, (int, float)):
                    delta_utilization = round(utilization - prev_utilization, 10)
                    if delta_utilization > 0:
                        effective_tokens = usage_value(
                            record,
                            meter=meter,
                            cache_read_weight=cache_read_weight,
                        )
                        implied_cap = (
                            effective_tokens / delta_utilization
                            if effective_tokens > 0
                            else None
                        )
                        deltas.append(
                            {
                                "session_id": record["session_id"],
                                "window": window_name,
                                "previous_id": previous["record"].get("id"),
                                "current_id": record.get("id"),
                                "previous_timestamp": previous["record"].get("request_timestamp"),
                                "current_timestamp": record.get("request_timestamp"),
                                "response_model": record.get("response_model"),
                                "utilization_before": prev_utilization,
                                "utilization_after": utilization,
                                "delta_utilization": delta_utilization,
                                "effective_tokens": effective_tokens,
                                "implied_cap_tokens": implied_cap,
                            }
                        )

            previous_by_session_window[key] = {
                "record": record,
                "window_data": window_data,
            }

    return deltas


def build_meter_comparison(records):
    comparison = {}
    for meter in ("effective_tokens_raw", "price_equivalent_5m"):
        deltas = [
            delta
            for delta in build_adjacent_deltas(records, meter=meter)
            if delta["window"] == "5h" and delta.get("implied_cap_tokens") is not None
        ]
        caps = [delta["implied_cap_tokens"] for delta in deltas]
        if not caps:
            continue
        comparison.setdefault("5h", {})[meter] = {
            "count": len(caps),
            "min": min(caps),
            "median": statistics.median(caps),
            "max": max(caps),
        }
    return comparison


def _quantile(sorted_values, percentile):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * percentile
    lower = int(idx)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    fraction = idx - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def filter_estimate_band_intervals(
    intervals,
    window="5h",
    meter="price_equivalent_5m",
    max_record_count=10,
    max_duration_minutes=20,
):
    eligible = [
        interval
        for interval in intervals
        if interval.get("window") == window
        and interval.get("meter") == meter
        and interval.get("complete_usage") is True
        and isinstance(interval.get("implied_cap"), (int, float))
    ]

    eligible.sort(
        key=lambda interval: (
            interval.get("account_fingerprint") or "unknown",
            interval.get("declared_plan_tier") or "unknown",
            interval.get("window") or "",
            interval.get("meter") or "",
            interval.get("start_timestamp") or "",
            interval.get("end_timestamp") or "",
            interval.get("end_id") or 0,
        )
    )

    filtered = []
    seen_first_by_cohort = set()

    for interval in eligible:
        cohort = (
            interval.get("account_fingerprint") or "unknown",
            interval.get("declared_plan_tier") or "unknown",
            interval.get("window") or "",
            interval.get("meter") or "",
        )
        if cohort not in seen_first_by_cohort:
            seen_first_by_cohort.add(cohort)
            continue

        if len(interval.get("models") or []) != 1:
            continue
        if (interval.get("record_count") or 0) > max_record_count:
            continue

        start_at = _parse_iso_timestamp(interval.get("start_timestamp") or "")
        end_at = _parse_iso_timestamp(interval.get("end_timestamp") or "")
        if start_at is None or end_at is None:
            continue
        duration_minutes = (end_at - start_at).total_seconds() / 60
        if duration_minutes < 0 or duration_minutes > max_duration_minutes:
            continue

        filtered.append(interval)

    return filtered


def summarize_estimate_band(intervals):
    caps = sorted(interval["implied_cap"] for interval in intervals if isinstance(interval.get("implied_cap"), (int, float)))
    if not caps:
        return {"count": 0}
    return {
        "count": len(caps),
        "min": min(caps),
        "p25": _quantile(caps, 0.25),
        "median": statistics.median(caps),
        "p75": _quantile(caps, 0.75),
        "max": max(caps),
    }


def build_estimate_band(records, window="5h", meter="price_equivalent_5m"):
    intervals = build_utilization_intervals(records, meter=meter)
    filtered = filter_estimate_band_intervals(intervals, window=window, meter=meter)
    if not filtered:
        return {}
    return {
        window: {
            meter: summarize_estimate_band(filtered),
        }
    }


def build_utilization_time_series(records, window="5h"):
    pairs = []
    for record in records:
        windows = ((record.get("ratelimit") or {}).get("windows") or {})
        if window not in windows:
            continue
        utilization = windows[window].get("utilization")
        if not isinstance(utilization, (int, float)):
            continue
        ts = record.get("response_timestamp") or ""
        if not ts:
            continue
        pairs.append({"timestamp": ts, "utilization": utilization})
    pairs.sort(key=lambda p: p["timestamp"])
    return pairs


def detect_resets(time_series, threshold=0.10):
    resets = []
    for i in range(1, len(time_series)):
        prev = time_series[i - 1]
        curr = time_series[i]
        drop = prev["utilization"] - curr["utilization"]
        if drop >= threshold:
            prev_dt = _parse_iso_timestamp(prev["timestamp"])
            curr_dt = _parse_iso_timestamp(curr["timestamp"])
            elapsed = None
            if prev_dt is not None and curr_dt is not None:
                elapsed = (curr_dt - prev_dt).total_seconds()
            resets.append({
                "timestamp": curr["timestamp"],
                "pre_utilization": prev["utilization"],
                "post_utilization": curr["utilization"],
                "elapsed_seconds_since_prior": elapsed,
            })
    return resets


def build_raw_vs_weighted_ratios(records, window="5h"):
    eligible = []
    for record in records:
        windows = ((record.get("ratelimit") or {}).get("windows") or {})
        if window not in windows:
            continue
        utilization = windows[window].get("utilization")
        if not isinstance(utilization, (int, float)):
            continue
        ts = record.get("response_timestamp") or ""
        if not ts:
            continue
        eligible.append((ts, utilization, record))
    eligible.sort(key=lambda t: t[0])

    ratios = []
    for i in range(1, len(eligible)):
        prev_ts, prev_util, prev_rec = eligible[i - 1]
        curr_ts, curr_util, curr_rec = eligible[i]
        if curr_util <= prev_util:
            continue
        raw = usage_value(curr_rec, meter="effective_tokens_raw")
        weighted = usage_value(curr_rec, meter="price_equivalent_5m")
        ratio = raw / weighted if weighted != 0 else None
        ratios.append({
            "timestamp": curr_ts,
            "raw_tokens": raw,
            "weighted_tokens": weighted,
            "ratio": ratio,
        })
    return ratios


def build_per_model_caps(records, window="5h", meter="price_equivalent_5m"):
    intervals = build_utilization_intervals(records, meter=meter)
    model_caps = {}
    for interval in intervals:
        if interval.get("window") != window:
            continue
        models = interval.get("models") or []
        if len(models) != 1:
            continue
        cap = interval.get("implied_cap")
        if not isinstance(cap, (int, float)):
            continue
        model_name = models[0]
        model_caps.setdefault(model_name, []).append(cap)
    result = {}
    for model_name, caps in model_caps.items():
        result[model_name] = statistics.median(caps)
    return result


def render_analysis(log_path):
    records = list(load_records(log_path))
    time_series = build_utilization_time_series(records, window="5h")
    summary = {
        "record_count": len(records),
        "window_summary": summarize_windows(records),
        "adjacent_deltas": build_adjacent_deltas(records),
        "interval_estimates": build_utilization_intervals(records),
        "meter_comparison": build_meter_comparison(records),
        "estimate_band": build_estimate_band(records),
        "utilization_time_series": time_series,
        "resets": detect_resets(time_series),
        "raw_vs_weighted_ratios": build_raw_vs_weighted_ratios(records),
        "per_model_caps": build_per_model_caps(records),
    }
    return json.dumps(summary, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze normalized Claude sniffer logs."
    )
    parser.add_argument("log_path", help="Path to normalized JSONL output")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON summary",
    )
    args = parser.parse_args()

    rendered = render_analysis(args.log_path)
    if args.pretty:
        print(json.dumps(json.loads(rendered), indent=2, sort_keys=True))
    else:
        print(rendered)


if __name__ == "__main__":
    main()
