from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmark_v2.aggregate_results import aggregate
from benchmark_v2.render_report import write_report
from benchmark_v2.run_evaluation import ARMS


def fake_report(run: int) -> dict:
    summary = {
        "composite": 75 + run,
        "components": {name: 0.75 for name in ("ambiguity", "correctness", "efficiency", "etl", "safety", "grounding")},
        "ambiguity_metrics": {name: 0.75 for name in ("recall", "specificity", "mechanism_accuracy", "option_match", "resolution", "final_sql_alignment")},
        "passed_cases": 10,
        "case_count": 16,
    }
    return {
        "report_type": "dbwhisperer_v2_run", "scoring_mode": "deterministic_scoring_only", "run": run,
        "suite": "suite", "suite_version": "2.1.0", "suite_hash": "abc", "model": "model", "candidate_count": 2,
        "schema": {"table_count": 2}, "usage": {"cost_usd": run / 10},
        "arms": {arm: {"summary": summary, "cases": []} for arm in ARMS},
    }


class ReportingTest(unittest.TestCase):
    def test_five_runs_aggregate_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary) / "campaign"
            for run in range(1, 6):
                path = campaign / "run-results" / f"run-{run:02d}"
                path.mkdir(parents=True)
                (path / "report.json").write_text(json.dumps(fake_report(run)), encoding="utf-8")
            payload = aggregate(campaign)
            aggregate_path = Path(temporary) / "aggregate.json"
            aggregate_path.write_text(json.dumps(payload), encoding="utf-8")
            summary, details = write_report(aggregate_path, Path(temporary) / "report.html")
            self.assertTrue(summary.is_file())
            self.assertTrue(details.is_file())
            rendered = summary.read_text(encoding="utf-8")
            self.assertIn("Deterministic benchmark", rendered)
            self.assertIn("Experimental arms", rendered)
            self.assertIn("Candidate-count ablation", rendered)
            self.assertIn("How the evaluation works", rendered)
            self.assertIn("What is an arm?", rendered)
            self.assertIn("What is an ablation?", rendered)
            self.assertIn("No human evaluator or LLM judge was used", rendered)
            self.assertIn("400 evaluations", rendered)
            self.assertIn("Suite 2.1.0", rendered)
            self.assertNotIn("Publication candidate", rendered)
            self.assertNotIn("95% CI", rendered)
            self.assertNotIn("confidence interval", rendered.lower())
            self.assertLess(summary.stat().st_size, 100_000)

    def test_pre_revision_aggregate_has_no_publication_status_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary) / "campaign"
            for run in range(1, 6):
                report = fake_report(run)
                report.pop("suite_version")
                path = campaign / "run-results" / f"run-{run:02d}"
                path.mkdir(parents=True)
                (path / "report.json").write_text(json.dumps(report), encoding="utf-8")
            # Reproduce a legacy aggregate, whose version metadata was absent.
            payload = aggregate(campaign)
            aggregate_path = Path(temporary) / "aggregate.json"
            aggregate_path.write_text(json.dumps(payload), encoding="utf-8")
            summary, _ = write_report(aggregate_path, Path(temporary) / "report.html")
            rendered = summary.read_text(encoding="utf-8")
            self.assertNotIn("Publication candidate", rendered)
            self.assertNotIn("Pilot results", rendered)
