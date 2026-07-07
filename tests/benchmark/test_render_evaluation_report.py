"""Tests for the static evaluation HTML report renderer."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmark"))

import render_evaluation_report as renderer  # noqa: E402


def fixture_report() -> dict:
    return {
        "run_id": "run-1",
        "suite": "mimic_iii_clinical_ambiguity",
        "dataset": "data/mimic",
        "tested_model": "google/gemma-4-31b-it",
        "judge": {
            "enabled": True,
            "model": "google/gemma-4-31b-it",
            "self_judged": True,
        },
        "schema": {
            "table_count": 26,
            "relationship_count": 42,
            "discovery_complete": True,
        },
        "summary": {
            "total_cases": 2,
            "baseline": {
                "normalized_percentage": 50.0,
            },
            "full": {
                "normalized_percentage": 100.0,
            },
            "overall_comparison": {
                "full_better": 1,
                "tie": 1,
                "baseline_better": 0,
                "unscored": 0,
            },
            "ambiguous": {
                "expected_clarification_rate": 1.0,
            },
            "control": {
                "spurious_clarification_rate": 0.0,
            },
            "unreliable_cases": [],
        },
        "cases": [
            {
                "id": "tc_04_patient_labs_subject_history",
                "question": "Show me lab results for patient 10006.",
                "ambiguity_type": "join-path",
                "comparison": "full_better",
                "baseline": {
                    "deterministic_score": {"score": 0},
                    "result": {"sql": "SELECT baseline;"},
                },
                "full": {
                    "deterministic_score": {"score": 4},
                    "workflow": {
                        "query_result": {"sql": "SELECT full;"}
                    },
                    "clarifications": [
                        {
                            "mechanism": "join-path",
                            "chosen": "direct path",
                        }
                    ],
                },
                "qualitative_judgment": {
                    "status": "accepted",
                    "clarification_quality": "pass",
                    "trust_note": "The clarification made the intent explicit.",
                    "reason": "The full pipeline selected the intended path.",
                },
            },
            {
                "id": "tc_01_count_admissions",
                "question": "How many hospital admissions are in the database?",
                "ambiguity_type": "none",
                "comparison": "tie",
                "baseline": {
                    "deterministic_score": {"score": 4},
                    "result": {"sql": "SELECT COUNT(*) FROM ADMISSIONS;"},
                },
                "full": {
                    "deterministic_score": {"score": 4},
                    "workflow": {
                        "query_result": {
                            "sql": "SELECT COUNT(*) FROM ADMISSIONS;"
                        }
                    },
                    "clarifications": [],
                },
            },
        ],
    }


class RenderReportTest(unittest.TestCase):
    def test_render_contains_main_sections_and_metrics(self) -> None:
        html = renderer.render_report(fixture_report())

        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("How Well DB Whisperer Handles", html)
        self.assertIn("Evaluation Framework", html)
        self.assertIn("Results Overview", html)
        self.assertIn("What Was Evaluated", html)
        self.assertIn("Detailed Case Results", html)
        self.assertIn("Discussion and Conclusions", html)
        self.assertIn("Baseline Correctness", html)
        self.assertIn("DB Whisperer Correctness", html)
        self.assertIn("100.0%", html)
        self.assertIn("View Detailed Case Results", html)
        self.assertNotIn("tc_04_patient_labs_subject_history", html)

    def test_render_escapes_untrusted_values(self) -> None:
        report = fixture_report()
        report["cases"][0]["question"] = "<script>alert(1)</script>"

        html = renderer.render_case_details_report(report)

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_write_report_creates_output_file(self) -> None:
        with TemporaryDirectory() as name:
            tmp = Path(name)
            report_path = tmp / "report.json"
            output_path = tmp / "evaluation_report.html"
            report_path.write_text(
                __import__("json").dumps(fixture_report()),
                encoding="utf-8",
            )

            result = renderer.write_report(report_path, output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertTrue((tmp / "evaluation_report_cases.html").exists())
            self.assertIn(
                "Evaluation Framework",
                output_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "tc_04_patient_labs_subject_history",
                (tmp / "evaluation_report_cases.html").read_text(encoding="utf-8"),
            )

    def test_helpers_format_missing_values(self) -> None:
        self.assertEqual(renderer.score_text(None), "unscored")
        self.assertEqual(renderer.percent(None), "n/a")
        self.assertEqual(renderer.safe_percentage(150), 100.0)


if __name__ == "__main__":
    unittest.main()
