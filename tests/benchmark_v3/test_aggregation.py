from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark_v3.aggregate_results as aggregation
from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import ARMS, DEFAULT_SUITE
from benchmark_v3.scoring import summarize_arm
from benchmark_v3.report_model import build_report_model
from benchmark_v3.aggregate_results import bootstrap_ci


def _score(*, passed: bool, recall: bool, oracle: bool = False) -> dict[str, object]:
    return {
        "passed": passed,
        "correctness": 1.0 if passed else 0.0,
        "efficiency": 1.0 if passed else 0.0,
        "safety": None,
        "grounding": 1.0 if passed else 0.0,
        "oracle_review": oracle,
        "ambiguity": {
            "applicable": True,
            "detection": recall,
            "mechanism_correct": recall,
            "plausibility": recall,
            "target_coverage": recall,
            "option_match": recall,
            "resolution": recall,
            "compliance": recall,
            "final_alignment": recall,
        },
        "reason": "ok" if passed else "retained failure",
    }


def _report(repetition: int, *, macro_fixture: bool = False) -> dict[str, object]:
    suite = load_suite(DEFAULT_SUITE)
    fingerprint = {
        "suite_hash": suite.sha256, "dataset_hash": "dataset", "model": suite.model,
        "prompt_hash": "prompt", "scorer_version": "scorer", "candidate_count": 3,
        "arms": list(ARMS), "runtime_hash": "runtime",
    }
    records: list[dict[str, object]] = []
    for case in suite.query_cases:
        for arm in ARMS:
            applicable = case.family_id in {"from_2024", "stay"}
            recall = case.family_id == "from_2024"
            if macro_fixture and case.family_id == "stay" and case.id != "stay_hospital":
                applicable = False
            score = _score(
                passed=not (case.id == "count_admissions" and arm == "full"),
                recall=recall,
                oracle=(case.id == "count_admissions" and arm == "full"),
            )
            score["ambiguity"]["applicable"] = applicable  # type: ignore[index]
            score["ambiguity"]["expected"] = case.category == "ambiguity"  # type: ignore[index]
            records.append({
                "run": repetition, "case_id": case.id, "family_id": case.family_id,
                "category": case.category, "arm": arm, "clarifications": [],
                "terminal": {
                    "category": "accepted",
                    "generated_candidates": 1 if arm == "baseline" else 3,
                    "executed_candidates": 1 if arm == "baseline" else 3,
                    "successful_candidates": 1 if arm == "baseline" else 3,
                    "messages": [],
                },
                "best_preclarification_result": {
                    "state": "accepted",
                    "message": "ok",
                    "sql": "SELECT 1",
                    "columns": [],
                    "rows": [],
                    "truncated": False,
                },
                "result": {"state": "accepted", "sql": "SELECT 1", "columns": [], "rows": []},
                "score": score, "duration_seconds": 0.5,
                "observation": {
                    "valid": True,
                    "source": "system",
                    "outcome": (
                        "success" if score["passed"] else "system_failure"
                    ),
                },
            })
    for case in suite.etl_cases:
        records.append({
            "run": repetition, "case_id": case.id, "family_id": case.family_id,
            "category": case.category, "arm": "etl", "clarifications": [],
            "result": {"state": "accepted", "sql": None, "columns": [], "rows": []},
            "score": {"passed": True, "score": 1.0}, "duration_seconds": 0.25,
            "observation": {
                "valid": True,
                "source": "system",
                "outcome": "success",
            },
        })
    return {
        "report_type": "dbwhisperer_v3_run", "suite_version": suite.version,
        "suite_hash": suite.sha256, "model": suite.model, "arms": list(ARMS),
        "fingerprint": fingerprint, "repetition": repetition, "records": records,
        "usage": {"model_calls": 4, "retries": 1, "cost_usd": 0.02},
    }


def write_campaign(directory: Path, *, reports: int = 5, macro_fixture: bool = False) -> None:
    payloads = [_report(index, macro_fixture=macro_fixture) for index in range(1, reports + 1)]
    for payload in payloads:
        (directory / f"run-{payload['repetition']:02d}.json").write_text(json.dumps(payload))
    (directory / "campaign.json").write_text(json.dumps({
        "complete": reports == 5,
        "fingerprint": payloads[0]["fingerprint"],
        "records": [record for payload in payloads for record in payload["records"]],
    }))
    (directory / "status.json").write_text(json.dumps({
        "model_calls": 20, "retries": 5, "prompt_tokens": 100,
        "completion_tokens": 40, "cost_usd": 0.1, "elapsed_seconds": 12.0,
    }))


class AggregationTest(unittest.TestCase):
    def _aggregate(self, directory: Path) -> dict[str, object]:
        self.assertTrue(hasattr(aggregation, "aggregate_campaign"))
        return aggregation.aggregate_campaign(directory)

    def test_requires_five_complete_compatible_repetitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory, reports=4)
            with self.assertRaisesRegex(ValueError, "five complete"):
                self._aggregate(directory)

    def test_macro_averages_ambiguity_by_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory, macro_fixture=True)
            aggregate = self._aggregate(directory)
        self.assertEqual(
            50.0,
            aggregate["arms"]["full"]["ambiguity_metrics"]["recall"]["mean"],
        )

    def test_includes_failures_oracle_flags_and_operational_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            aggregate = self._aggregate(directory)
        self.assertTrue(aggregate["failures"])
        self.assertTrue(aggregate["oracle_reviews"])
        self.assertEqual(20, aggregate["usage"]["model_calls"])
        self.assertEqual("campaign_global", aggregate["usage"]["scope"])
        self.assertIn("latency_seconds", aggregate["arms"]["full"])
        self.assertEqual(0.1, aggregate["operational"]["metrics"]["cost_usd"])
        self.assertEqual(5, aggregate["operational"]["metrics"]["retries"])

    def test_matches_frozen_summarize_arm_semantics_and_report_model_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            aggregate = self._aggregate(directory)
        report = aggregate["run_reports"][0]
        rows = [row for row in report["records"] if row["arm"] == "full"]
        etl = [row["score"]["score"] for row in report["records"] if row["arm"] == "etl"]
        summary = summarize_arm(rows, sum(etl) / len(etl))
        self.assertEqual(
            summary["composite"],
            aggregate["arms"]["full"]["composite"]["mean"],
        )
        self.assertEqual(
            summary["ambiguity_metrics"]["false_positive_rate"] * 100,
            aggregate["arms"]["full"]["ambiguity_metrics"]["false_positive_rate"]["mean"],
        )
        model = build_report_model(aggregate)
        self.assertTrue({"methodology", "headline_metrics", "arm_cards", "charts", "tables", "findings", "limitations", "cases", "evidence", "ambiguity_funnel", "operations", "warnings"} <= set(model))
        self.assertIn("paired, stratified", model["methodology"]["bootstrap"])
        self.assertIn("2,000-replicate", model["methodology"]["bootstrap"])
        self.assertIn("repetitions", model["methodology"]["bootstrap"])
        self.assertIn("question families", model["methodology"]["bootstrap"])
        self.assertIn(
            "repetition-only",
            model["methodology"]["shared_etl_uncertainty"],
        )
        self.assertEqual(3, len(model["findings"]))
        self.assertTrue(
            any(
                item["finding_id"] == "full_vs_baseline_composite"
                and "delta" in item["evidence"]
                for item in model["findings"]
            )
        )
        self.assertTrue(
            any(
                "final alignment" in item["claim"]
                for item in model["findings"]
            )
        )
        query_case = next(row for row in model["cases"] if row["arm"] == "full")
        self.assertNotEqual("Not recorded", query_case.get("question"))
        self.assertTrue(query_case.get("expected_sql"))
        self.assertIn("comparison", query_case)

    def test_bootstrap_interval_uses_question_family_units_not_run_means(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            aggregate = self._aggregate(directory)
        run_values = [
            summarize_arm(
                [row for row in report["records"] if row["arm"] == "full"],
                1.0,
            )["composite"]
            for report in aggregate["run_reports"]
        ]
        self.assertNotEqual(
            list(bootstrap_ci(run_values)),
            aggregate["arms"]["full"]["composite"]["confidence_interval_95"],
        )

    def test_family_bootstrap_recomputes_the_campaign_statistic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            aggregate = self._aggregate(directory)

        full = aggregate["arms"]["full"]
        distributions = [
            full["pass_rate"],
            full["composite"],
            *full["components"].values(),
            *full["ambiguity_metrics"].values(),
        ]
        for metric in distributions:
            with self.subTest(metric=metric):
                lower, upper = metric["confidence_interval_95"]
                self.assertLessEqual(lower, metric["mean"])
                self.assertGreaterEqual(upper, metric["mean"])

    def test_family_bootstrap_is_paired_and_deterministic(self) -> None:
        reports = [_report(index) for index in range(1, 6)]
        for report in reports:
            baseline_scores = {
                row["case_id"]: row["score"]
                for row in report["records"]
                if row["arm"] == "baseline"
            }
            for row in report["records"]:
                if row["arm"] in ARMS:
                    row["score"] = deepcopy(
                        baseline_scores[row["case_id"]]
                    )

        first = aggregation._bootstrap_campaign_estimates(
            reports,
            [100.0] * 5,
            samples=25,
        )
        second = aggregation._bootstrap_campaign_estimates(
            reports,
            [100.0] * 5,
            samples=25,
        )

        self.assertEqual(first, second)
        _, deltas = first
        for arm in deltas.values():
            for estimates in arm.values():
                self.assertEqual({0.0}, set(estimates))

    def test_family_bootstrap_preserves_strata_and_cluster_occurrences(
        self,
    ) -> None:
        reports = [_report(index) for index in range(1, 6)]
        calls: list[tuple[int, Counter[str], int]] = []
        real_summarize = summarize_arm

        def recording_summary(
            rows: list[dict[str, object]],
            etl_score: float,
        ) -> dict[str, object]:
            calls.append((
                len(rows),
                Counter(str(row["category"]) for row in rows),
                len({str(row["family_id"]) for row in rows}),
            ))
            return real_summarize(rows, etl_score)

        with patch(
            "benchmark_v3.aggregate_results.summarize_arm",
            side_effect=recording_summary,
        ):
            aggregation._bootstrap_campaign_estimates(
                reports,
                [100.0] * 5,
                samples=3,
            )

        self.assertEqual(12, len(calls))
        for row_count, categories, family_count in calls:
            self.assertEqual(110, row_count)
            self.assertEqual(
                Counter({
                    "ambiguity": 30,
                    "control": 30,
                    "correctness": 30,
                    "safety": 20,
                }),
                categories,
            )
            self.assertEqual(13, family_count)

    def test_includes_frozen_ambiguity_etl_components_and_shared_etl_per_repetition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            for path in directory.glob("run-*.json"):
                report = json.loads(path.read_text())
                etl = [row for row in report["records"] if row["arm"] == "etl"]
                etl[0]["score"]["score"] = 0.0
                etl[1]["score"]["score"] = 1.0
                report["relationship_warnings"] = ["sampled relation"]
                path.write_text(json.dumps(report))
            campaign = json.loads((directory / "campaign.json").read_text())
            campaign["relationship_warnings"] = ["campaign warning", "sampled relation"]
            (directory / "campaign.json").write_text(json.dumps(campaign))
            aggregate = self._aggregate(directory)
        shared = aggregate["shared_etl"]
        self.assertEqual(50.0, shared["mean"])
        self.assertEqual(0.0, shared["stddev"])
        self.assertEqual(50.0, shared["min"])
        self.assertEqual(50.0, shared["max"])
        self.assertIn("ambiguity", aggregate["arms"]["full"]["components"])
        self.assertIn("etl", aggregate["arms"]["full"]["components"])
        self.assertEqual(["campaign warning", "sampled relation"], build_report_model(aggregate)["warnings"])
