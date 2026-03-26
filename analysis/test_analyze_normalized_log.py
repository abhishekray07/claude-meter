import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_normalized_log as analyzer


class TestAnalyzeNormalizedLog(unittest.TestCase):
    def _write_jsonl(self, records):
        tmp = tempfile.NamedTemporaryFile("w", delete=False)
        with tmp:
            for record in records:
                tmp.write(json.dumps(record) + "\n")
        return Path(tmp.name)

    def test_summarizes_observed_windows(self):
        records = [
            {
                "id": 1,
                "status": 200,
                "response_model": "claude-sonnet-4-6",
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.10},
                        "7d": {"status": "allowed", "utilization": 0.20},
                    }
                },
            },
            {
                "id": 2,
                "status": 429,
                "response_model": "claude-sonnet-4-6",
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "rejected", "utilization": 1.0},
                    }
                },
            },
        ]

        summary = analyzer.summarize_windows(records)

        self.assertEqual(
            summary,
            {
                "5h": {
                    "count": 2,
                    "statuses": {"allowed": 1, "rejected": 1},
                    "min_utilization": 0.1,
                    "max_utilization": 1.0,
                    "models": ["claude-sonnet-4-6"],
                },
                "7d": {
                    "count": 1,
                    "statuses": {"allowed": 1},
                    "min_utilization": 0.2,
                    "max_utilization": 0.2,
                    "models": ["claude-sonnet-4-6"],
                },
            },
        )

    def test_builds_adjacent_window_deltas_for_successful_requests(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T20:00:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "response_model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 40,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T20:05:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "response_model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 15,
                    "output_tokens": 20,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.15},
                    }
                },
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T20:06:00.000+00:00",
                "session_id": "session-2",
                "status": 200,
                "response_model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 7,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.17},
                    }
                },
            },
        ]

        deltas = analyzer.build_adjacent_deltas(records)

        self.assertEqual(
            deltas,
            [
                {
                    "session_id": "session-1",
                    "window": "5h",
                    "previous_id": 1,
                    "current_id": 2,
                    "previous_timestamp": "2026-03-25T20:00:00.000+00:00",
                    "current_timestamp": "2026-03-25T20:05:00.000+00:00",
                    "response_model": "claude-sonnet-4-6",
                    "utilization_before": 0.10,
                    "utilization_after": 0.15,
                    "delta_utilization": 0.05,
                    "effective_tokens": 50,
                    "implied_cap_tokens": 1000.0,
                }
            ],
        )

    def test_usage_value_supports_candidate_meters(self):
        record = {
            "response_model": "claude-opus-4-6",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 100,
                "output_tokens": 5,
            }
        }

        self.assertEqual(analyzer.usage_value(record, meter="effective_tokens_raw"), 135)
        self.assertEqual(
            analyzer.usage_value(record, meter="effective_tokens_no_cache_read"),
            35,
        )
        self.assertEqual(analyzer.usage_value(record, meter="effective_tokens_io_only"), 15)
        self.assertEqual(
            analyzer.usage_value(record, meter="effective_tokens_weighted", cache_read_weight=0.25),
            60,
        )
        self.assertEqual(
            analyzer.usage_value(record, meter="price_equivalent_5m"),
            350.0,
        )

    def test_build_utilization_intervals_sorts_by_response_timestamp_and_window(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "response_timestamp": "2026-03-25T22:30:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "7d": {"status": "allowed", "utilization": 0.10},
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T22:05:00.000+00:00",
                "response_timestamp": "2026-03-25T22:06:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 200, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "7d": {"status": "allowed", "utilization": 0.10},
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T22:10:00.000+00:00",
                "response_timestamp": "2026-03-25T22:40:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 300, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "7d": {"status": "allowed", "utilization": 0.11},
                        "5h": {"status": "allowed", "utilization": 0.11},
                    }
                },
            },
        ]

        intervals = analyzer.build_utilization_intervals(records)

        self.assertEqual(
            [
                (interval["window"], interval["start_id"], interval["end_id"])
                for interval in intervals
            ],
            [("5h", 1, 3), ("7d", 1, 3)],
        )

    def test_build_utilization_intervals_accumulates_flat_spans(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 50,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T22:01:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 200,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 50,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T22:02:00.000+00:00",
                "session_id": "session-2",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 300,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 50,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.11},
                    }
                },
            },
        ]

        intervals = analyzer.build_utilization_intervals(records)

        self.assertEqual(len(intervals), 1)
        self.assertEqual(
            intervals[0],
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "start_id": 2,
                "end_id": 3,
                "start_timestamp": "2026-03-25T22:01:00.000+00:00",
                "end_timestamp": "2026-03-25T22:02:00.000+00:00",
                "utilization_before": 0.10,
                "utilization_after": 0.11,
                "delta_utilization": 0.01,
                "record_count": 2,
                "meter": "effective_tokens_raw",
                "complete_usage": True,
                "usage_total": 600,
                "implied_cap": 60000.0,
                "models": ["claude-opus-4-6"],
            },
        )

    def test_build_utilization_intervals_ignores_session_boundaries_for_both_windows(self):
        records = [
            {
                "id": 10,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "session_id": "session-a",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "5h": {"utilization": 0.10},
                        "7d": {"utilization": 0.20},
                    }
                },
            },
            {
                "id": 11,
                "request_timestamp": "2026-03-25T22:01:00.000+00:00",
                "session_id": "session-b",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 20, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "5h": {"utilization": 0.10},
                        "7d": {"utilization": 0.20},
                    }
                },
            },
            {
                "id": 12,
                "request_timestamp": "2026-03-25T22:02:00.000+00:00",
                "session_id": "session-a",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 30, "output_tokens": 0},
                "ratelimit": {
                    "windows": {
                        "5h": {"utilization": 0.11},
                        "7d": {"utilization": 0.21},
                    }
                },
            },
        ]

        intervals = analyzer.build_utilization_intervals(records)

        self.assertEqual(
            [(interval["window"], interval["start_id"], interval["end_id"]) for interval in intervals],
            [("5h", 11, 12), ("7d", 11, 12)],
        )

    def test_build_utilization_intervals_marks_incomplete_usage(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-123",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T22:01:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-123",
                "response_model": "claude-opus-4-6",
                "usage": {},
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T22:02:00.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-123",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 30, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.11}}},
            },
        ]

        intervals = analyzer.build_utilization_intervals(records)

        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["account_fingerprint"], "acct-123")
        self.assertFalse(intervals[0]["complete_usage"])
        self.assertIsNone(intervals[0]["usage_total"])
        self.assertIsNone(intervals[0]["implied_cap"])

    def test_build_utilization_intervals_groups_by_account_fingerprint(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-a",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T22:01:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-b",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 20, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T22:02:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-a",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 30, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.11}}},
            },
            {
                "id": 4,
                "request_timestamp": "2026-03-25T22:03:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "account_fingerprint": "acct-b",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 40, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.11}}},
            },
        ]

        intervals = analyzer.build_utilization_intervals(records)

        self.assertEqual(
            [(interval["account_fingerprint"], interval["start_id"], interval["end_id"]) for interval in intervals],
            [("acct-a", 3, 3), ("acct-b", 4, 4)],
        )

    def test_cli_outputs_summary_json(self):
        log_path = self._write_jsonl(
            [
                {
                    "id": 1,
                    "request_timestamp": "2026-03-25T20:00:00.000+00:00",
                    "session_id": "session-1",
                    "status": 200,
                    "response_model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                    "ratelimit": {
                        "windows": {
                            "5h": {"status": "allowed", "utilization": 0.10},
                        }
                    },
                }
            ]
        )

        output = analyzer.render_analysis(log_path)
        parsed = json.loads(output)

        self.assertEqual(parsed["record_count"], 1)
        self.assertIn("5h", parsed["window_summary"])
        self.assertIn("interval_estimates", parsed)
        self.assertIn("adjacent_deltas", parsed)
        self.assertIn("meter_comparison", parsed)
        self.assertIn("estimate_band", parsed)

    def test_builds_5h_meter_comparison_for_raw_and_price_equivalent(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T20:00:00.000+00:00",
                "response_timestamp": "2026-03-25T20:00:01.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.10},
                    }
                },
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T20:05:00.000+00:00",
                "response_timestamp": "2026-03-25T20:05:01.000+00:00",
                "session_id": "session-1",
                "status": 200,
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 4,
                    "cache_creation_input_tokens": 8,
                    "cache_read_input_tokens": 10,
                    "output_tokens": 2,
                },
                "ratelimit": {
                    "windows": {
                        "5h": {"status": "allowed", "utilization": 0.12},
                    }
                },
            },
        ]

        comparison = analyzer.build_meter_comparison(records)

        self.assertEqual(
            comparison,
            {
                "5h": {
                    "effective_tokens_raw": {
                        "count": 1,
                        "min": 1200.0,
                        "median": 1200.0,
                        "max": 1200.0,
                    },
                    "price_equivalent_5m": {
                        "count": 1,
                        "min": 6250.0,
                        "median": 6250.0,
                        "max": 6250.0,
                    },
                }
            },
        )

    def test_filters_clean_5h_intervals_for_estimate_band(self):
        intervals = [
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:00:00Z",
                "end_timestamp": "2026-03-25T20:02:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 5_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:03:00Z",
                "end_timestamp": "2026-03-25T20:05:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 7_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:06:00Z",
                "end_timestamp": "2026-03-25T20:08:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 9_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:09:00Z",
                "end_timestamp": "2026-03-25T20:45:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 11_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:46:00Z",
                "end_timestamp": "2026-03-25T20:48:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 13_000_000.0,
                "models": ["claude-opus-4-6", "claude-sonnet-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:49:00Z",
                "end_timestamp": "2026-03-25T20:51:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 15_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "7d",
                "meter": "price_equivalent_5m",
                "start_timestamp": "2026-03-25T20:52:00Z",
                "end_timestamp": "2026-03-25T20:54:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 17_000_000.0,
                "models": ["claude-opus-4-6"],
            },
            {
                "account_fingerprint": "unknown",
                "declared_plan_tier": "max_20x",
                "window": "5h",
                "meter": "effective_tokens_raw",
                "start_timestamp": "2026-03-25T20:55:00Z",
                "end_timestamp": "2026-03-25T20:57:00Z",
                "record_count": 2,
                "complete_usage": True,
                "implied_cap": 19_000_000.0,
                "models": ["claude-opus-4-6"],
            },
        ]

        filtered = analyzer.filter_estimate_band_intervals(intervals)

        self.assertEqual(
            [interval["implied_cap"] for interval in filtered],
            [7_000_000.0, 9_000_000.0, 15_000_000.0],
        )

    def test_summarizes_filtered_estimate_band(self):
        intervals = [
            {"implied_cap": 7_000_000.0},
            {"implied_cap": 9_000_000.0},
            {"implied_cap": 15_000_000.0},
        ]

        summary = analyzer.summarize_estimate_band(intervals)

        self.assertEqual(
            summary,
            {
                "count": 3,
                "min": 7_000_000.0,
                "p25": 8_000_000.0,
                "median": 9_000_000.0,
                "p75": 12_000_000.0,
                "max": 15_000_000.0,
            },
        )


    def test_build_utilization_time_series_extracts_ordered_pairs(self):
        records = [
            {
                "id": 1,
                "status": 200,
                "response_timestamp": "2026-03-25T22:05:00.000+00:00",
                "ratelimit": {"windows": {"5h": {"utilization": 0.30}}},
            },
            {
                "id": 2,
                "status": 200,
                "response_timestamp": "2026-03-25T22:00:00.000+00:00",
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 3,
                "status": 200,
                "response_timestamp": "2026-03-25T22:02:00.000+00:00",
                "ratelimit": {"windows": {"5h": {"utilization": 0.20}}},
            },
            {
                "id": 4,
                "status": 200,
                "response_timestamp": "2026-03-25T22:06:00.000+00:00",
                "ratelimit": {"windows": {"7d": {"utilization": 0.50}}},
            },
        ]

        ts = analyzer.build_utilization_time_series(records, window="5h")

        self.assertEqual(len(ts), 3)
        self.assertEqual(
            [entry["utilization"] for entry in ts],
            [0.10, 0.20, 0.30],
        )
        self.assertEqual(ts[0]["timestamp"], "2026-03-25T22:00:00.000+00:00")
        self.assertEqual(ts[1]["timestamp"], "2026-03-25T22:02:00.000+00:00")
        self.assertEqual(ts[2]["timestamp"], "2026-03-25T22:05:00.000+00:00")

    def test_detect_resets_finds_large_drops(self):
        time_series = [
            {"timestamp": "2026-03-25T22:00:00.000+00:00", "utilization": 0.50},
            {"timestamp": "2026-03-25T22:05:00.000+00:00", "utilization": 0.60},
            {"timestamp": "2026-03-25T22:10:00.000+00:00", "utilization": 0.10},
            {"timestamp": "2026-03-25T22:15:00.000+00:00", "utilization": 0.20},
        ]

        resets = analyzer.detect_resets(time_series, threshold=0.10)

        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["timestamp"], "2026-03-25T22:10:00.000+00:00")
        self.assertAlmostEqual(resets[0]["pre_utilization"], 0.60)
        self.assertAlmostEqual(resets[0]["post_utilization"], 0.10)
        self.assertEqual(resets[0]["elapsed_seconds_since_prior"], 300.0)

    def test_detect_resets_ignores_small_drops(self):
        time_series = [
            {"timestamp": "2026-03-25T22:00:00.000+00:00", "utilization": 0.50},
            {"timestamp": "2026-03-25T22:05:00.000+00:00", "utilization": 0.45},
            {"timestamp": "2026-03-25T22:10:00.000+00:00", "utilization": 0.55},
        ]

        resets = analyzer.detect_resets(time_series, threshold=0.10)

        self.assertEqual(resets, [])

    def test_build_raw_vs_weighted_ratios(self):
        records = [
            {
                "id": 1,
                "status": 200,
                "response_timestamp": "2026-03-25T22:00:00.000+00:00",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 2,
                "status": 200,
                "response_timestamp": "2026-03-25T22:05:00.000+00:00",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 10,
                },
                "ratelimit": {"windows": {"5h": {"utilization": 0.20}}},
            },
            {
                "id": 3,
                "status": 200,
                "response_timestamp": "2026-03-25T22:10:00.000+00:00",
                "response_model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                },
                "ratelimit": {"windows": {"5h": {"utilization": 0.15}}},
            },
        ]

        ratios = analyzer.build_raw_vs_weighted_ratios(records, window="5h")

        self.assertEqual(len(ratios), 1)
        self.assertEqual(ratios[0]["timestamp"], "2026-03-25T22:05:00.000+00:00")
        self.assertEqual(ratios[0]["raw_tokens"], 30)
        self.assertEqual(ratios[0]["weighted_tokens"], 350.0)
        self.assertAlmostEqual(ratios[0]["ratio"], 30 / 350.0)

    def test_build_per_model_caps_groups_by_model(self):
        records = [
            {
                "id": 1,
                "request_timestamp": "2026-03-25T22:00:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.10}}},
            },
            {
                "id": 2,
                "request_timestamp": "2026-03-25T22:01:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-opus-4-6",
                "usage": {"input_tokens": 200, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.11}}},
            },
            {
                "id": 3,
                "request_timestamp": "2026-03-25T22:02:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 50, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.11}}},
            },
            {
                "id": 4,
                "request_timestamp": "2026-03-25T22:03:00.000+00:00",
                "status": 200,
                "declared_plan_tier": "max_20x",
                "response_model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 0},
                "ratelimit": {"windows": {"5h": {"utilization": 0.12}}},
            },
        ]

        caps = analyzer.build_per_model_caps(records, window="5h", meter="price_equivalent_5m")

        self.assertIn("claude-opus-4-6", caps)
        self.assertIn("claude-sonnet-4-6", caps)
        self.assertEqual(len(caps), 2)
        self.assertIsInstance(caps["claude-opus-4-6"], float)
        self.assertIsInstance(caps["claude-sonnet-4-6"], float)


if __name__ == "__main__":
    unittest.main()
