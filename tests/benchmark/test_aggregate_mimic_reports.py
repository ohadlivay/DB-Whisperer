"""Tests for aggregating repeated MIMIC evaluation reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmark"))

import aggregate_mimic_reports as aggregate  # noqa: E402


def case_result(
    case_id: str,
    *,
    ambiguous: bool,
    should_clarify: bool,
    comparison: str,
    baseline_score: int,
    full_score: int,
    asked: bool,
    unreliable: bool = False,
    tests: list[str] | None = None,
) -> dict:
    return {
        "id": case_id,
        "question": f"Question {case_id}?",
        "category": "fixture",
        "ambiguous": ambiguous,
        "ambiguity_type": "join-path" if ambiguous else "none",
        "should_clarify": should_clarify,
        "tests": tests or ["fixture"],
        "comparison": comparison,
        "score_delta": full_score - baseline_score,
        "baseline": {
            "deterministic_score": {"score": baseline_score},
        },
        "full": {
            "deterministic_score": {"score": full_score},
            "clarifications": [
                {
                    "declared": True,
                    "matched_simulated_answer": True,
                }
            ] if asked else [],
            "unreliable": unreliable,
        },
    }


def report(run_id: str, cases: list[dict]) -> dict:
    return {
        "run_id": run_id,
        "suite": "mimic_iii_clinical_ambiguity",
        "dataset": "data/mimic",
        "tested_model": "google/gemma-4-31b-it",
        "judge": {
            "enabled": False,
            "model": None,
            "self_judged": False,
        },
        "started_at": "2026-07-07T00:00:00+00:00",
        "completed_at": "2026-07-07T00:10:00+00:00",
        "schema": {
            "table_count": 26,
            "relationship_count": 60,
            "discovery_complete": False,
            "discovery_notes": ["Skipped malformed rows."],
        },
        "summary": {"total_cases": len(cases)},
        "cases": cases,
    }


class AggregateMimicReportsTest(unittest.TestCase):
    def test_aggregates_scores_comparisons_and_reliability(self) -> None:
        reports = [
            (
                Path("run1.json"),
                report(
                    "run1",
                    [
                        case_result(
                            "amb",
                            ambiguous=True,
                            should_clarify=True,
                            comparison="full_better",
                            baseline_score=0,
                            full_score=4,
                            asked=True,
                        ),
                        case_result(
                            "ctl",
                            ambiguous=False,
                            should_clarify=False,
                            comparison="tie",
                            baseline_score=4,
                            full_score=4,
                            asked=False,
                        ),
                    ],
                ),
            ),
            (
                Path("run2.json"),
                report(
                    "run2",
                    [
                        case_result(
                            "amb",
                            ambiguous=True,
                            should_clarify=True,
                            comparison="tie",
                            baseline_score=0,
                            full_score=0,
                            asked=True,
                            unreliable=True,
                        ),
                        case_result(
                            "ctl",
                            ambiguous=False,
                            should_clarify=False,
                            comparison="baseline_better",
                            baseline_score=4,
                            full_score=0,
                            asked=True,
                            unreliable=True,
                        ),
                    ],
                ),
            ),
        ]

        payload = aggregate.aggregate_reports(
            reports,
            generated_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["report_type"], "mimic_ab_aggregate")
        self.assertEqual(payload["run_count"], 2)
        self.assertEqual(payload["summary"]["total_cases"], 4)
        self.assertEqual(payload["summary"]["baseline"]["mean"], 2.0)
        self.assertEqual(payload["summary"]["full"]["mean"], 2.0)
        self.assertEqual(
            payload["summary"]["overall_comparison"],
            {
                "full_better": 1,
                "tie": 2,
                "baseline_better": 1,
                "unscored": 0,
            },
        )
        self.assertEqual(
            payload["summary"]["unreliable_cases"],
            ["amb", "ctl"],
        )
        self.assertEqual(
            payload["summary"]["ambiguous"]["expected_clarification_rate"],
            1.0,
        )
        self.assertEqual(
            payload["summary"]["control"]["spurious_clarification_rate"],
            0.5,
        )
        factors = payload["summary"]["factor_scores"]
        self.assertEqual(
            factors["correctness"]["baseline"]["normalized_percentage"],
            50.0,
        )
        self.assertEqual(
            factors["correctness"]["full"]["normalized_percentage"],
            50.0,
        )
        self.assertEqual(
            factors["ambiguity_detection"]["full"]["percentage"],
            100.0,
        )
        self.assertEqual(
            factors["clarification_quality"]["full"]["percentage"],
            50.0,
        )
        self.assertEqual(
            factors["unnecessary_interruptions"]["full"]["percentage"],
            50.0,
        )
        reliable = payload["summary"]["reliable_only"]
        self.assertEqual(reliable["total_cases"], 2)
        self.assertEqual(reliable["excluded_case_results"], 2)
        self.assertEqual(reliable["baseline"]["mean"], 2.0)
        self.assertEqual(reliable["full"]["mean"], 4.0)
        self.assertEqual(
            reliable["overall_comparison"],
            {
                "full_better": 1,
                "tie": 1,
                "baseline_better": 0,
                "unscored": 0,
            },
        )

        amb = payload["cases"][0]
        self.assertEqual(amb["id"], "amb")
        self.assertEqual(amb["baseline"]["mean"], 0.0)
        self.assertEqual(amb["full"]["mean"], 2.0)
        self.assertEqual(amb["unreliable_count"], 1)
        self.assertEqual(amb["clarification_rate"], 1.0)
        self.assertEqual(len(amb["runs"]), 2)
        self.assertEqual(amb["reliable_only"]["run_count"], 1)
        self.assertEqual(amb["reliable_only"]["full"]["mean"], 4.0)

    def test_rejects_mismatched_case_ids(self) -> None:
        reports = [
            (
                Path("run1.json"),
                report(
                    "run1",
                    [
                        case_result(
                            "a",
                            ambiguous=False,
                            should_clarify=False,
                            comparison="tie",
                            baseline_score=4,
                            full_score=4,
                            asked=False,
                        )
                    ],
                ),
            ),
            (
                Path("run2.json"),
                report(
                    "run2",
                    [
                        case_result(
                            "b",
                            ambiguous=False,
                            should_clarify=False,
                            comparison="tie",
                            baseline_score=4,
                            full_score=4,
                            asked=False,
                        )
                    ],
                ),
            ),
        ]

        with self.assertRaisesRegex(ValueError, "case order or IDs"):
            aggregate._validate_compatible_reports(reports)

    def test_write_aggregate_creates_file(self) -> None:
        with TemporaryDirectory() as name:
            tmp = Path(name)
            first = tmp / "run1.json"
            second = tmp / "run2.json"
            first.write_text(
                json.dumps(
                    report(
                        "run1",
                        [
                            case_result(
                                "case",
                                ambiguous=False,
                                should_clarify=False,
                                comparison="tie",
                                baseline_score=4,
                                full_score=4,
                                asked=False,
                            )
                        ],
                    )
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    report(
                        "run2",
                        [
                            case_result(
                                "case",
                                ambiguous=False,
                                should_clarify=False,
                                comparison="tie",
                                baseline_score=0,
                                full_score=4,
                                asked=False,
                            )
                        ],
                    )
                ),
                encoding="utf-8",
            )

            output = aggregate.write_aggregate(
                [first, second],
                tmp / "aggregate.json",
            )

            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_count"], 2)
            self.assertEqual(payload["cases"][0]["full"]["mean"], 4.0)

    def test_scores_safety_on_explicit_safety_cases(self) -> None:
        safe_failure = case_result(
            "safe",
            ambiguous=False,
            should_clarify=False,
            comparison="tie",
            baseline_score=4,
            full_score=4,
            asked=False,
            tests=["sql_safety", "no_destructive_execution"],
        )
        safe_failure["baseline"]["result"] = {
            "state": "failed",
            "sql": None,
        }
        safe_failure["full"]["workflow"] = {
            "query_result": {
                "state": "failed",
                "sql": None,
            }
        }
        unsafe_accept = case_result(
            "unsafe",
            ambiguous=False,
            should_clarify=False,
            comparison="tie",
            baseline_score=0,
            full_score=0,
            asked=False,
            tests=["sql_safety"],
        )
        unsafe_accept["baseline"]["result"] = {
            "state": "accepted",
            "sql": "DELETE FROM x;",
        }
        unsafe_accept["full"]["workflow"] = {
            "query_result": {
                "state": "accepted",
                "sql": "DELETE FROM x;",
            }
        }

        payload = aggregate.aggregate_reports(
            [(Path("run.json"), report("run", [safe_failure, unsafe_accept]))]
        )

        safety = payload["summary"]["factor_scores"]["safety"]
        self.assertEqual(safety["baseline"]["passed"], 1)
        self.assertEqual(safety["baseline"]["total"], 2)
        self.assertEqual(safety["full"]["percentage"], 50.0)


if __name__ == "__main__":
    unittest.main()
