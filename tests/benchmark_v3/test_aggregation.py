from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import benchmark_v3.aggregate_results as aggregation
from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import ARMS, DEFAULT_SUITE


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
            records.append({
                "run": repetition, "case_id": case.id, "family_id": case.family_id,
                "category": case.category, "arm": arm, "clarifications": [],
                "result": {"state": "accepted", "sql": "SELECT 1", "columns": [], "rows": []},
                "score": score, "duration_seconds": 0.5,
            })
    for case in suite.etl_cases:
        records.append({
            "run": repetition, "case_id": case.id, "family_id": case.family_id,
            "category": case.category, "arm": "etl", "clarifications": [],
            "result": {"state": "accepted", "sql": None, "columns": [], "rows": []},
            "score": {"passed": True, "score": 1.0}, "duration_seconds": 0.25,
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
        self.assertIn("latency_seconds", aggregate["arms"]["full"])
        self.assertEqual(0.02, aggregate["operational"]["cost_usd"]["mean"])
        self.assertEqual(1.0, aggregate["operational"]["retries"]["mean"])
