from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmark_v3.render_report import (
    REPORT_TABS, render_full_report, render_one_page, write_reports,
)


def fixture_model() -> dict:
    distribution = {"mean": 75.0, "stddev": 1.0, "min": 74.0, "max": 76.0, "confidence_interval_95": [74.0, 76.0]}
    arms = {
        arm: {"composite": distribution, "pass_rate": distribution,
              "components": {"ambiguity": distribution, "correctness": distribution, "efficiency": distribution, "safety": distribution, "grounding": distribution, "etl": distribution},
              "ambiguity_metrics": {"recall": distribution, "specificity": distribution, "false_positive_rate": distribution, "false_negative_rate": distribution},
              "latency_seconds": distribution}
        for arm in ("baseline", "candidate_only", "semantic_only", "full")
    }
    case = {"case_id": "unsafe<script>alert(1)</script>", "family_id": "safe", "category": "safety", "arm": "full", "run": 1,
            "result": {"sql": "SELECT 1", "columns": ["answer"], "rows": [[1]]},
            "score": {"passed": False, "reason": "failure", "ambiguity": {"candidate_support": [["a", 2]], "compliance": False}},
            "clarifications": [{"question": "Which?", "candidate_support": [["a", 2]], "compliance_passed": False}]}
    return {"title": "DB Whisperer Evaluation V3", "provenance": {"suite_version": "3.0", "suite_hash": "hash", "model": "model"},
            "methodology": {"design": "five complete compatible repetitions"}, "arms": arms, "arm_cards": arms,
            "headline_metrics": {arm: distribution for arm in arms}, "arm_deltas": {"full": {"composite": distribution}},
            "ambiguity_funnel": {arm: arms[arm]["ambiguity_metrics"] for arm in arms},
            "shared_etl": distribution, "operations": {"scope": "campaign_global", "metrics": {"cost_usd": 0.1, "retries": 2}},
            "usage": {"scope": "campaign_global", "cost_usd": 0.1}, "cases": [case], "evidence": {"failures": [case]},
            "failures": [case], "oracle_reviews": [], "findings": ["<b>finding</b>"], "limitations": ["No live campaign claim."], "warnings": ["relationship warning"],
            "charts": {}, "tables": {}}


class ReportingTest(unittest.TestCase):
    def test_writes_exactly_two_populated_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); aggregate = directory / "aggregate.json"
            aggregate.write_text(json.dumps({"model": fixture_model()}))
            outputs = write_reports(aggregate, directory / "evaluation_method_one_page.html", directory / "evaluation_report.html")
            self.assertEqual(2, len(outputs))
            self.assertEqual(("evaluation_method_one_page.html", "evaluation_report.html"), tuple(path.name for path in outputs))
            one_page, full = (path.read_text(encoding="utf-8") for path in outputs)
        self.assertIn("Candidate Only", one_page)
        self.assertIn("Semantic Only", one_page)
        self.assertIn("Full System", one_page)
        self.assertIn("Ambiguity funnel", full)
        self.assertIn("Clarification compliance", full)
        self.assertEqual(8, len(REPORT_TABS))

    def test_reports_have_all_tabs_and_no_v2_language(self) -> None:
        combined = render_one_page(fixture_model()) + render_full_report(fixture_model())
        for tab_id, label in REPORT_TABS:
            self.assertIn(f'id="{tab_id}"', combined)
            self.assertIn(label, combined)
        self.assertIn("&lt;script&gt;", combined)
        self.assertNotIn("Join Only", combined)
        self.assertNotIn("five configurations", combined)
        self.assertNotIn("join-path ambiguity", combined.casefold())
        self.assertIn("K=3", combined)
        self.assertIn("five repetitions", combined)

    def test_model_evidence_is_escaped_and_rendering_does_not_mutate_it(self) -> None:
        model = fixture_model(); case = model["cases"][0]
        case.update({"question": "<script>alert(1)</script>", "expected_sql": "SELECT '<b>expected</b>'"})
        case["result"]["rows"] = [["<img src=x onerror=alert(1)>"]]  # type: ignore[index]
        case["clarifications"][0]["question"] = "<span>transcript</span>"  # type: ignore[index]
        model["warnings"] = ["<svg/onload=alert(1)>"]
        before = json.dumps(model, sort_keys=True)
        rendered = render_one_page(model) + render_full_report(model)
        self.assertEqual(before, json.dumps(model, sort_keys=True))
        for unsafe in ("<script>alert(1)</script>", "<b>expected</b>", "<img src=x onerror=alert(1)>", "<span>transcript</span>", "<svg/onload=alert(1)>"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("Expected SQL", rendered)
        self.assertIn("Generated SQL", rendered)
        self.assertIn("Clarifications", rendered)
        self.assertIn("Candidate support", rendered)

    def test_method_changes_document_required_decisions(self) -> None:
        document = (Path(__file__).parents[2] / "docs" / "EVALUATION_V3_METHOD_CHANGES.md").read_text(encoding="utf-8")
        for heading in (
            "Why V2 no longer matches DB Whisperer", "Experimental arms", "Test-suite redesign",
            "Ambiguity-funnel scoring", "Correctness and least-sufficient joins",
            "K=3, five repetitions, and budget control", "Faster campaign execution",
            "Progress, checkpoints, and resume", "Aggregation and publication", "Interpretation limits",
        ):
            self.assertIn(heading, document)
        for decision in ("$3.75", "two workers", "K=3", "five", "birth date", "admission date"):
            self.assertIn(decision, document)
        self.assertNotIn("join-path ambiguity", document.casefold())
