from __future__ import annotations

import json
from pathlib import Path
import tempfile
from threading import Lock
from time import sleep
import unittest

from benchmark_v3.contracts import EvaluationCase, EvaluationSuite, load_suite
from benchmark_v3.run_evaluation import (
    CampaignConfig,
    CampaignDataset,
    CampaignFingerprint,
    WorkItem,
    run_campaign,
)
from db_whisperer.contracts import ComponentState, SchemaMetadata
from benchmark_v3.observability import BudgetStop


SUITE_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark_v3"
    / "cases"
    / "evaluation_cases.json"
)


class CampaignTest(unittest.TestCase):
    def _suite(self, *, repetitions: int = 1) -> EvaluationSuite:
        case = EvaluationCase(
            id="case", family_id="family", kind="query", category="correctness",
            question="count rows",
        )
        return EvaluationSuite(
            name="offline", version="v3", path=SUITE_PATH,
            dataset_path=SUITE_PATH.parent, model="model", repetitions=repetitions,
            candidate_count=3, budget_usd=3.75, cases=(case,), sha256="suite",
        )

    def _dataset(self) -> CampaignDataset:
        return CampaignDataset(SchemaMetadata(), "dataset", {}, {})

    @staticmethod
    def _record(item: WorkItem, *, passed: bool = True) -> dict[str, object]:
        return {
            "run": item.repetition, "case_id": item.case_id,
            "family_id": item.family_id, "category": item.category,
            "arm": item.arm, "clarifications": [],
            "result": {"state": ComponentState.FAILED, "sql": None, "columns": [], "rows": []},
            "score": {"passed": passed}, "duration_seconds": 0.01,
        }

    def test_compatible_checkpoint_skips_exactly_that_cell(self) -> None:
        suite = self._suite()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            skipped = WorkItem(1, "case", "family", "correctness", "baseline")
            (directory / "checkpoints").mkdir()
            (directory / "checkpoints" / f"{skipped.key}.json").write_text(
                json.dumps(self._record(skipped)), encoding="utf-8"
            )
            config = CampaignConfig(
                suite=suite, campaign_dir=directory, api_key="offline", workers=1,
                dataset=self._dataset(),
                cell_runner=lambda item, *args: calls.append(item.key) or self._record(item),
            )
            result = run_campaign(config)
            report = json.loads((directory / "run-01.json").read_text())
        self.assertNotIn(skipped.key, calls)
        self.assertIn(skipped.key, result.completed_keys)
        self.assertEqual(3, len(calls))
        self.assertEqual(4, len(report["records"]))

    def test_two_workers_bound_in_flight_and_write_all_reports(self) -> None:
        suite = self._suite(repetitions=5)
        active = 0; maximum = 0; lock = Lock()
        def runner(item: WorkItem, *args: object) -> dict[str, object]:
            nonlocal active, maximum
            with lock:
                active += 1; maximum = max(maximum, active)
            sleep(0.01)
            with lock: active -= 1
            return self._record(item)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = run_campaign(CampaignConfig(suite, directory, "offline", 2, self._dataset(), cell_runner=runner))
            artifacts = [directory / f"run-{index:02d}.json" for index in range(1, 6)]
            self.assertTrue(all(path.exists() for path in artifacts))
            self.assertTrue((directory / "campaign.json").exists())
            self.assertTrue((directory / "references-dataset.json").exists())
        self.assertEqual(20, len(result.completed_keys))
        self.assertLessEqual(maximum, 2)

    def test_budget_stop_drains_admitted_cells_without_new_submissions(self) -> None:
        suite = self._suite(repetitions=2)
        calls: list[str] = []
        def runner(item: WorkItem, *args: object) -> dict[str, object]:
            calls.append(item.key)
            if len(calls) == 1: raise BudgetStop("stop")
            return self._record(item)
        with tempfile.TemporaryDirectory() as temporary:
            result = run_campaign(CampaignConfig(suite, Path(temporary), "offline", 2, self._dataset(), cell_runner=runner))
        self.assertTrue(result.stopped_for_budget)
        self.assertLessEqual(len(calls), 2)

    def test_ordinary_failure_is_checkpointed_and_campaign_continues(self) -> None:
        suite = self._suite()
        calls: list[str] = []
        def runner(item: WorkItem, *args: object) -> dict[str, object]:
            calls.append(item.key)
            if item.arm == "baseline": raise RuntimeError("ordinary failure")
            return self._record(item)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = run_campaign(CampaignConfig(suite, directory, "offline", 1, self._dataset(), cell_runner=runner))
            failed = WorkItem(1, "case", "family", "correctness", "baseline")
            checkpoint = json.loads((directory / "checkpoints" / f"{failed.key}.json").read_text())
        self.assertEqual(4, len(calls))
        self.assertIn(failed.key, result.completed_keys)
        self.assertFalse(checkpoint["score"]["passed"])

    def test_fingerprint_changes_when_runtime_configuration_changes(self) -> None:
        base = CampaignFingerprint(
            suite_hash="suite",
            dataset_hash="dataset",
            model="model",
            prompt_hash="prompt",
            scorer_version="v3",
            candidate_count=3,
            arms=("baseline", "candidate_only", "semantic_only", "full"),
            runtime_hash="runtime-a",
        )
        changed = CampaignFingerprint(
            **{**base.__dict__, "runtime_hash": "runtime-b"}
        )
        self.assertNotEqual(base, changed)

    def test_incompatible_checkpoint_is_not_resumed(self) -> None:
        suite = load_suite(SUITE_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "campaign.json").write_text(
                json.dumps({"fingerprint": {"suite_hash": "other"}}),
                encoding="utf-8",
            )
            config = CampaignConfig(
                suite=suite,
                campaign_dir=directory,
                api_key="offline",
                workers=1,
                dataset=CampaignDataset(
                    schema=SchemaMetadata(),
                    dataset_hash="dataset",
                    references={},
                    reference_joins={},
                ),
            )
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                run_campaign(config)


if __name__ == "__main__":
    unittest.main()
