from __future__ import annotations

import json
from pathlib import Path
import tempfile
from threading import Lock
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_v3.contracts import EvaluationCase, EvaluationSuite, load_suite
from benchmark_v3.run_evaluation import (
    build_etl_schedule,
    build_schedule,
    CampaignConfig,
    CampaignDataset,
    CampaignFingerprint,
    WorkItem,
    _checkpoint_payload,
    _fingerprint,
    _fingerprint_payload,
    _prepare_dataset,
    _reference_artifact_path,
    _serialize_schema,
    run_campaign,
)
from db_whisperer.contracts import ComponentState, QueryResult, SchemaMetadata
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
            fingerprint = _fingerprint(suite, "dataset")
            (directory / "checkpoints" / f"{skipped.key}.json").write_text(
                json.dumps(_checkpoint_payload(skipped, fingerprint, self._record(skipped))),
                encoding="utf-8",
            )
            (directory / "campaign.json").write_text(
                json.dumps({"fingerprint": _fingerprint_payload(fingerprint)}), encoding="utf-8"
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

    def test_fingerprint_changes_for_prompt_and_runtime_inputs(self) -> None:
        suite = self._suite()
        with patch("benchmark_v3.run_evaluation._source_hash", side_effect=(
            "prompt-a", "runtime-a", "scorer-a",
        )):
            first = _fingerprint(suite, "dataset")
        with patch("benchmark_v3.run_evaluation._source_hash", side_effect=(
            "prompt-b", "runtime-a", "scorer-a",
        )):
            prompt_changed = _fingerprint(suite, "dataset")
        with patch("benchmark_v3.run_evaluation._source_hash", side_effect=(
            "prompt-a", "runtime-b", "scorer-a",
        )):
            runtime_changed = _fingerprint(suite, "dataset")
        self.assertNotEqual(first, prompt_changed)
        self.assertNotEqual(first, runtime_changed)

    def test_campaign_metadata_exists_before_the_first_cell_runs_and_warnings_propagate(self) -> None:
        suite = self._suite()
        observed: list[dict[str, object]] = []
        class Progress:
            def __init__(self) -> None:
                self.started = False; self.stopped = False
            def start(self) -> None: self.started = True
            def stop(self) -> None: self.stopped = True
        progress = Progress()
        dataset = CampaignDataset(
            SchemaMetadata(discovery_notes=("relationship sampled",)), "dataset", {}, {},
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def runner(item: WorkItem, *args: object) -> dict[str, object]:
                observed.append(json.loads((directory / "campaign.json").read_text()))
                return self._record(item)
            run_campaign(CampaignConfig(
                suite, directory, "offline", 1, dataset, cell_runner=runner,
                progress_factory=lambda observer: progress,
            ))
            final = json.loads((directory / "campaign.json").read_text())
        self.assertTrue(observed[0]["fingerprint"])
        self.assertEqual(["relationship sampled"], final["relationship_warnings"])
        self.assertTrue(progress.started)
        self.assertTrue(progress.stopped)

    def test_worker_service_session_is_closed_after_campaign(self) -> None:
        suite = self._suite()
        class Session:
            closed = False
            def close(self) -> None: self.closed = True
        session = Session()
        query = SimpleNamespace(
            client=SimpleNamespace(session=session),
            generate_candidate=lambda request: SimpleNamespace(
                state=ComponentState.FAILED,
                message="Generated SQL contains a forbidden operation.",
            ),
            execute_candidate=lambda candidate, database_path: QueryResult(
                state=ComponentState.FAILED,
                message="Generated SQL contains a forbidden operation.",
            ),
        )
        failed = SimpleNamespace(
            submit_query=lambda **kwargs: SimpleNamespace(
                state=ComponentState.FAILED, complete=True, query_result=None,
                ambiguity=None, candidates=(),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            run_campaign(CampaignConfig(
                suite, Path(temporary), "offline", 1,
                CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
                service_factory=lambda observer: (query, {
                    "candidate_only": failed, "semantic_only": failed, "full": failed,
                }),
            ))
        self.assertTrue(session.closed)

    def test_reference_artifact_reuses_schema_references_and_warnings(self) -> None:
        suite = self._suite()
        schema = SchemaMetadata(discovery_notes=("cached relationship warning",))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = _reference_artifact_path(directory, "dataset", suite.sha256)
            artifact.write_text(json.dumps({
                "dataset_hash": "dataset", "suite_hash": suite.sha256,
                "schema": _serialize_schema(schema), "references": {},
            }), encoding="utf-8")
            with patch("benchmark_v3.run_evaluation.ingest_dataset", side_effect=AssertionError("cache miss")):
                dataset = _prepare_dataset(suite, directory, "dataset")
        self.assertEqual(("cached relationship warning",), dataset.schema.discovery_notes)

    def test_official_campaign_executes_ten_shared_etl_observations(self) -> None:
        full_suite = load_suite(SUITE_PATH)
        suite = EvaluationSuite(
            name="etl-fixtures", version=full_suite.version, path=full_suite.path,
            dataset_path=full_suite.dataset_path, model=full_suite.model,
            repetitions=5, candidate_count=3, budget_usd=3.75,
            cases=full_suite.etl_cases, sha256="etl-fixtures",
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_campaign(CampaignConfig(
                suite, Path(temporary), "offline", 2,
                CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
                progress_factory=lambda observer: type("Progress", (), {
                    "start": lambda self: None, "stop": lambda self: None,
                })(),
            ))
        etl = [record for record in result.records if record["arm"] == "etl"]
        self.assertEqual(10, len(etl))
        self.assertTrue(all(record["score"]["passed"] for record in etl))

    def test_official_schedule_has_440_query_cells_and_ten_shared_etl_cells(self) -> None:
        suite = load_suite(SUITE_PATH)
        query_schedule = build_schedule(suite)
        etl_schedule = build_etl_schedule(suite)

        self.assertEqual(440, len(query_schedule))
        self.assertEqual(10, len(etl_schedule))
        self.assertEqual({"etl"}, {item.arm for item in etl_schedule})

    def test_checkpoint_without_matching_fingerprint_is_rejected_before_resume(self) -> None:
        suite = self._suite()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stale = WorkItem(1, "case", "family", "correctness", "baseline")
            (directory / "checkpoints").mkdir()
            (directory / "checkpoints" / f"{stale.key}.json").write_text(
                json.dumps(self._record(stale)), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                run_campaign(CampaignConfig(
                    suite, directory, "offline", 1, self._dataset(),
                    cell_runner=lambda item, *args: self._record(item),
                ))

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
