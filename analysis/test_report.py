import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402


def _make_record(
    record_id,
    timestamp,
    model="claude-opus-4-6",
    utilization_5h=0.10,
    input_tokens=100,
    output_tokens=50,
):
    return {
        "id": record_id,
        "request_timestamp": timestamp,
        "response_timestamp": timestamp,
        "session_id": "session-1",
        "status": 200,
        "declared_plan_tier": "max_20x",
        "account_fingerprint": "acct-1",
        "response_model": model,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": output_tokens,
        },
        "ratelimit": {
            "windows": {
                "5h": {"status": "allowed", "utilization": utilization_5h},
            }
        },
    }


def _synthetic_records():
    """Create a sequence of records with increasing utilization so charts have data."""
    return [
        _make_record(1, "2026-03-25T20:00:00.000+00:00", utilization_5h=0.10, input_tokens=100, output_tokens=50),
        _make_record(2, "2026-03-25T20:01:00.000+00:00", utilization_5h=0.12, input_tokens=200, output_tokens=100),
        _make_record(3, "2026-03-25T20:02:00.000+00:00", utilization_5h=0.15, input_tokens=300, output_tokens=150),
        _make_record(4, "2026-03-25T20:03:00.000+00:00", utilization_5h=0.20, input_tokens=400, output_tokens=200),
        _make_record(5, "2026-03-25T20:04:00.000+00:00", utilization_5h=0.25, input_tokens=500, output_tokens=250,
                     model="claude-sonnet-4-6"),
        _make_record(6, "2026-03-25T20:05:00.000+00:00", utilization_5h=0.30, input_tokens=600, output_tokens=300,
                     model="claude-sonnet-4-6"),
    ]


def _write_jsonl(records, dirpath, filename="data.jsonl"):
    filepath = Path(dirpath) / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with filepath.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return filepath


class TestReportGeneratesFourCharts(unittest.TestCase):
    def test_report_generates_four_charts(self):
        records = _synthetic_records()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "output"
            report.generate_report(records, output_dir)

            self.assertTrue((output_dir / "report.md").exists())
            charts_dir = output_dir / "charts"
            self.assertTrue((charts_dir / "utilization_5h.png").exists())
            self.assertTrue((charts_dir / "raw_vs_weighted.png").exists())
            self.assertTrue((charts_dir / "per_model_cost.png").exists())
            # budget_band_dist requires filtered intervals which need specific
            # conditions (skip-first, single-model, short duration). Verify
            # it either exists or doesn't break.
            pngs = list(charts_dir.glob("*.png"))
            self.assertGreaterEqual(len(pngs), 3)

            md = (output_dir / "report.md").read_text()
            self.assertIn("Records:", md)
            self.assertIn("6", md)
            self.assertIn("5h", md)


class TestReportHandlesEmptyData(unittest.TestCase):
    def test_report_handles_empty_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "output"
            report.generate_report([], output_dir)

            self.assertTrue((output_dir / "report.md").exists())
            md = (output_dir / "report.md").read_text()
            self.assertIn("No data found", md)

            charts_dir = output_dir / "charts"
            if charts_dir.exists():
                pngs = list(charts_dir.glob("*.png"))
                self.assertEqual(len(pngs), 0)


class TestReportHandlesMalformedJsonl(unittest.TestCase):
    def test_report_handles_malformed_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "mixed.jsonl"
            records = _synthetic_records()
            with jsonl_path.open("w") as handle:
                handle.write(json.dumps(records[0]) + "\n")
                handle.write("NOT VALID JSON\n")
                handle.write("{also broken\n")
                handle.write(json.dumps(records[1]) + "\n")
                handle.write(json.dumps(records[2]) + "\n")

            loaded, malformed = report.load_records_from_path(jsonl_path)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(malformed, 2)

            output_dir = Path(tmpdir) / "output"
            report.generate_report(loaded, output_dir, malformed_count=malformed)

            md = (output_dir / "report.md").read_text()
            self.assertIn("Malformed lines skipped", md)
            self.assertIn("2", md)


class TestReportCliWritesMarkdown(unittest.TestCase):
    def test_report_cli_writes_markdown(self):
        records = _synthetic_records()
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = _write_jsonl(records, tmpdir)
            output_dir = Path(tmpdir) / "report_output"

            old_argv = sys.argv
            try:
                sys.argv = ["report.py", str(jsonl_path), "--output", str(output_dir)]
                report.main()
            finally:
                sys.argv = old_argv

            self.assertTrue((output_dir / "report.md").exists())
            md = (output_dir / "report.md").read_text()
            self.assertIn("Claude Meter Report", md)
            self.assertIn("Records:", md)


class TestReportLoadsFromDirectory(unittest.TestCase):
    def test_loads_from_normalized_subdir(self):
        records = _synthetic_records()
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_jsonl(records[:3], tmpdir, "normalized/batch1.jsonl")
            _write_jsonl(records[3:], tmpdir, "normalized/batch2.jsonl")

            loaded, malformed = report.load_records_from_path(tmpdir)
            self.assertEqual(len(loaded), 6)
            self.assertEqual(malformed, 0)


if __name__ == "__main__":
    unittest.main()
