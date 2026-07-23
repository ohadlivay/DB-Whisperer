from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest

from benchmark_v3.observability import CampaignObserver
from benchmark_v3.progress import TerminalProgress


class ProgressTest(unittest.TestCase):
    def test_snapshot_contains_required_progress_fields(self) -> None:
        rendered = TerminalProgress.snapshot({
            "completed_units": 5, "total_units": 20,
            "elapsed_seconds": 125, "eta_seconds": 375,
            "passed": 4, "failed": 1, "model_calls": 18,
            "retries": 2, "cost_usd": 0.42, "budget_usd": 3.75,
            "active": [{
                "run": 1, "case": "stay_icu", "arm": "full",
                "phase": "judging",
            }],
            "eta_by_arm_category": {"full/ambiguity": 375},
            "latest_error": "timeout",
        })
        self.assertIn("25.0%", rendered)
        self.assertIn("elapsed 00:02:05", rendered)
        self.assertIn("ETA 00:06:15", rendered)
        self.assertIn("$0.4200/$3.75", rendered)
        self.assertIn("r1:stay_icu/full [judging]", rendered)
        self.assertIn("full/ambiguity 00:06:15", rendered)
        self.assertIn("error timeout", rendered)

    def test_eta_uses_arm_and_category_rolling_durations(self) -> None:
        work_items = (
            type("Item", (), {
                "key": "one", "repetition": 1, "case_id": "a",
                "arm": "baseline", "category": "control",
            })(),
            type("Item", (), {
                "key": "two", "repetition": 1, "case_id": "b",
                "arm": "full", "category": "ambiguity",
            })(),
            type("Item", (), {
                "key": "three", "repetition": 2, "case_id": "c",
                "arm": "full", "category": "ambiguity",
            })(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), work_items, 3.75)
            observer.complete_cell(
                duration=10,
                arm="baseline",
                category="control",
                passed=True,
            )
            observer.complete_cell(
                duration=40,
                arm="full",
                category="ambiguity",
                passed=True,
            )
            self.assertEqual(40, observer.status["eta_by_arm_category"]["full/ambiguity"])
            self.assertGreater(observer.status["eta_seconds"], 0)

    def test_redirected_stream_uses_newline_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            stream = StringIO()
            progress = TerminalProgress(observer, stream=stream, interval=0.01)
            progress.render_once()
            self.assertTrue(stream.getvalue().endswith("\n"))
            self.assertNotIn("\r", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
