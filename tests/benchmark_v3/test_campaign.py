from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Lock
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb

from benchmark_v3.contracts import EvaluationCase, EvaluationSuite, ReferenceContract, load_suite
from benchmark_v3.run_evaluation import (
    build_etl_schedule,
    build_schedule,
    CampaignConfig,
    CampaignDataset,
    CampaignFingerprint,
    CampaignResult,
    WorkItem,
    _checkpoint_payload,
    _run_etl_cell,
    _cached_dataset_is_valid,
    _hash_paths,
    _fingerprint,
    _fingerprint_payload,
    _database_hash,
    _prepare_dataset,
    _reference_artifact_path,
    _serialize_schema,
    _load_matching_checkpoints,
    run_campaign,
    publish_campaign,
    main,
    _replace_staged,
    _campaign_directory,
)
from benchmark_v3.run_evaluation import BENCHMARK_DIR, DEFAULT_OUTPUT, DEFAULT_SUITE, PROJECT_ROOT, SRC
from db_whisperer.contracts import ComponentState, QueryResult, SchemaMetadata
from benchmark_v3.observability import BudgetStop, InfrastructureStop
from benchmark_v3.rescore_campaign import rescore_campaign
from tests.benchmark_v3.test_aggregation import write_campaign
from benchmark_v3.aggregate_results import aggregate_campaign
from benchmark_v3.review_package import write_review_package
from benchmark_v3.publication import (
    approve_campaign,
    publish_approved_campaign,
    sha256_file,
)
from benchmark_v3.preflight import run_preflight


SUITE_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark_v3"
    / "cases"
    / "evaluation_cases.json"
)


class CampaignTest(unittest.TestCase):
    def test_preflight_is_offline_and_checks_report_readiness(self) -> None:
        historical = (
            PROJECT_ROOT
            / "benchmark_v3"
            / "results"
            / "runs"
            / "v3-official-evidence-final-20260724"
        )
        with patch(
            "requests.Session.post",
            side_effect=AssertionError("preflight must not call the network"),
        ):
            result = run_preflight(DEFAULT_SUITE, historical_campaign=historical)

        self.assertTrue(result.passed, result.errors)
        for name in (
            "suite",
            "references",
            "scorer",
            "report_contract",
            "renderer",
            "fingerprint",
            "historical_rescore",
            "public_html_unchanged",
        ):
            self.assertTrue(result.checks[name], name)

    def test_approval_binds_campaign_and_aggregate_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "campaign-id"
            directory.mkdir()
            write_campaign(directory)
            aggregate = directory / "aggregate.json"
            aggregate.write_text(
                json.dumps(aggregate_campaign(directory)),
                encoding="utf-8",
            )
            write_review_package(aggregate, directory)

            approval = approve_campaign(directory, approved_by="user")

            payload = json.loads(approval.read_text(encoding="utf-8"))
            self.assertEqual(directory.name, payload["campaign_id"])
            self.assertEqual(
                sha256_file(aggregate),
                payload["aggregate_sha256"],
            )
            self.assertEqual("user", payload["approved_by"])

    def test_publish_rejects_changed_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "campaign-id"
            directory.mkdir()
            write_campaign(directory)
            aggregate = directory / "aggregate.json"
            aggregate.write_text(
                json.dumps(aggregate_campaign(directory)),
                encoding="utf-8",
            )
            write_review_package(aggregate, directory)
            approve_campaign(directory, approved_by="user")
            aggregate.write_text('{"changed":true}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "approval hash|aggregate"):
                publish_approved_campaign(directory)

    def test_rescore_writes_new_artifact_without_mutating_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            suite = load_suite(DEFAULT_SUITE)
            references = {
                case.id: {
                    "sql": case.expected_sql,
                    "columns": [],
                    "rows": [],
                    "truncated": False,
                    "join_count": 0,
                    "comparison_mode": case.comparison_mode,
                }
                for case in suite.query_cases
                if case.expected_sql
            }
            (directory / "references-fixture.json").write_text(
                json.dumps({
                    "suite_hash": suite.sha256,
                    "schema": {
                        "database_path": None,
                        "source_names": [],
                        "table_names": [],
                        "columns": [],
                        "row_count": None,
                        "tables": [],
                        "relationships": [],
                        "discovery_complete": True,
                        "discovery_notes": [],
                    },
                    "references": references,
                }),
                encoding="utf-8",
            )
            before = {
                path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*")
                if path.is_file()
            }

            output = rescore_campaign(directory)

            after = {
                path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*")
                if path.is_file() and path != output
            }
            self.assertEqual(before, after)
            self.assertEqual("counterfactual-rescore.json", output.name)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                "dbwhisperer_v3_counterfactual_rescore",
                payload["report_type"],
            )
            self.assertIn("source_campaign_hash", payload)
            self.assertIn("scorer_version", payload)

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
            "observation": {
                "valid": True,
                "source": "system",
                "outcome": "success" if passed else "system_failure",
            },
        }

    def test_compatible_checkpoint_skips_exactly_that_cell(self) -> None:
        suite = self._suite()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            skipped = WorkItem(1, "case", "family", "correctness", "baseline")
            (directory / "checkpoints").mkdir()
            fingerprint = _fingerprint(suite, "dataset", workers=1)
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

    def test_publication_requires_complete_official_campaign_and_keeps_public_reports_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); public = directory / "public"; public.mkdir()
            one_page, full = public / "evaluation_method_one_page.html", public / "evaluation_report.html"
            one_page.write_text("old one"); full.write_text("old full")
            (directory / "campaign.json").write_text(json.dumps({"complete": False, "repetitions": 5, "records": [{}] * 450}))
            self.assertFalse(publish_campaign(directory, one_page_path=one_page, full_report_path=full))
            self.assertEqual("old one", one_page.read_text()); self.assertEqual("old full", full.read_text())

    def test_custom_suite_hash_cannot_publish_or_touch_public_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); public = directory / "public"; public.mkdir()
            one_page, full = public / "evaluation_method_one_page.html", public / "evaluation_report.html"
            one_page.write_text("old one"); full.write_text("old full")
            (directory / "campaign.json").write_text(json.dumps({"complete": True, "suite_hash": "changed-question-or-reference", "repetitions": 5, "records": [{}] * 450}))
            with patch("benchmark_v3.aggregate_results.aggregate_campaign", side_effect=AssertionError("aggregate")), patch("benchmark_v3.render_report.write_reports", side_effect=AssertionError("render")):
                self.assertFalse(publish_campaign(directory, one_page_path=one_page, full_report_path=full))
            campaign = json.loads((directory / "campaign.json").read_text())
            self.assertTrue(campaign["complete"]); self.assertIn("report approval", campaign["latest_error"])
            self.assertEqual("old one", one_page.read_text()); self.assertEqual("old full", full.read_text())

    def test_approved_publication_writes_exactly_two_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); public = directory / "public"; public.mkdir()
            write_campaign(directory)
            aggregate_path = directory / "aggregate.json"
            aggregate_path.write_text(json.dumps(aggregate_campaign(directory)), encoding="utf-8")
            write_review_package(aggregate_path, directory)
            approve_campaign(directory, approved_by="reviewer")
            def render_staged(_: Path, staged_one: Path, staged_full: Path) -> tuple[Path, Path]:
                staged_one.write_text("new one"); staged_full.write_text("new full")
                return staged_one, staged_full
            with patch("benchmark_v3.render_report.write_reports", side_effect=render_staged) as write:
                self.assertTrue(publish_campaign(directory, one_page_path=public / "evaluation_method_one_page.html", full_report_path=public / "evaluation_report.html"))
            write.assert_called_once()
            self.assertEqual("new one", (public / "evaluation_method_one_page.html").read_text())
            self.assertEqual("new full", (public / "evaluation_report.html").read_text())

    def test_publication_rolls_back_both_reports_when_promotion_fails(self) -> None:
        for failed_promotion in (1, 2):
            with self.subTest(failed_promotion=failed_promotion), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary); public = directory / "public"; public.mkdir()
                one_page = public / "evaluation_method_one_page.html"; full = public / "evaluation_report.html"
                one_page.write_bytes(b"old one"); full.write_bytes(b"old full")
                before = tuple(path.read_bytes() for path in (one_page, full))
                write_campaign(directory)
                aggregate_path = directory / "aggregate.json"
                aggregate_path.write_text(json.dumps(aggregate_campaign(directory)), encoding="utf-8")
                write_review_package(aggregate_path, directory)
                approve_campaign(directory, approved_by="reviewer")
                calls = 0
                def render_staged(_: Path, staged_one: Path, staged_full: Path) -> tuple[Path, Path]:
                    staged_one.write_text("new one"); staged_full.write_text("new full")
                    return staged_one, staged_full
                def replace(source: Path, target: Path) -> Path:
                    nonlocal calls
                    calls += 1
                    if calls == failed_promotion: raise OSError("injected promotion failure")
                    return source.replace(target)
                with patch("benchmark_v3.render_report.write_reports", side_effect=render_staged), patch("benchmark_v3.run_evaluation._replace_staged", side_effect=replace):
                    self.assertFalse(publish_campaign(directory, one_page_path=one_page, full_report_path=full))
                self.assertEqual(before, tuple(path.read_bytes() for path in (one_page, full)))

    def test_publication_error_preserves_public_reports_and_marks_campaign_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); public = directory / "public"; public.mkdir()
            one_page, full = public / "evaluation_method_one_page.html", public / "evaluation_report.html"
            one_page.write_text("old one"); full.write_text("old full")
            (directory / "campaign.json").write_text(json.dumps({"complete": True, "suite_hash": load_suite(DEFAULT_SUITE).sha256, "repetitions": 5, "records": [{}] * 450}))
            with patch("benchmark_v3.aggregate_results.aggregate_campaign", side_effect=RuntimeError("broken aggregate")):
                self.assertFalse(publish_campaign(directory, one_page_path=one_page, full_report_path=full))
            campaign = json.loads((directory / "campaign.json").read_text())
            self.assertTrue(campaign["complete"]); self.assertIn("publication failed", campaign["latest_error"])
            self.assertEqual("old one", one_page.read_text()); self.assertEqual("old full", full.read_text())

    def test_checkpoint_rejects_non_system_observation_even_when_marked_valid(self) -> None:
        item = WorkItem(1, "case", "family", "correctness", "baseline")
        record = self._record(item)
        record["observation"] = {
            "valid": True,
            "source": "provider",
            "outcome": "infrastructure_failure",
        }

        with self.assertRaisesRegex(ValueError, "valid system observation"):
            _checkpoint_payload(item, _fingerprint(self._suite(), "dataset"), record)

    def test_failed_etl_observation_receives_zero_credit(self) -> None:
        item = WorkItem(1, "etl_case", "etl_family", "etl", "etl")
        case = EvaluationCase(
            id="etl_case",
            family_id="etl_family",
            kind="etl",
            category="etl",
            question="",
            fixture_files=(),
            manifest={},
        )
        failed_ingestion = SimpleNamespace(
            state=ComponentState.FAILED,
            schema=SchemaMetadata(),
        )
        service = SimpleNamespace(ingest=lambda uploads: failed_ingestion)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("benchmark_v3.run_evaluation.ETLService", return_value=service),
            patch(
                "benchmark_v3.run_evaluation.score_etl_manifest",
                return_value={"score": 0.75},
            ),
        ):
            record = _run_etl_cell(item, case, Path(temporary))

        self.assertFalse(record["score"]["passed"])
        self.assertEqual(0.0, record["score"]["score"])
        self.assertEqual("system_failure", record["observation"]["outcome"])

    def test_dataset_preparation_failure_is_resumable_infrastructure_stop(self) -> None:
        suite = self._suite()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch(
                "benchmark_v3.run_evaluation._prepare_dataset",
                side_effect=RuntimeError("dataset preparation broke"),
            ):
                result = run_campaign(
                    CampaignConfig(
                        suite,
                        directory,
                        "offline",
                        1,
                        dataset=None,
                    )
                )
            status = json.loads((directory / "status.json").read_text())
            campaign = json.loads((directory / "campaign.json").read_text())

        self.assertTrue(result.stopped_for_infrastructure)
        self.assertIn("dataset preparation broke", result.stop_reason)
        self.assertEqual("harness", status["infrastructure_failure"]["source"])
        self.assertFalse(campaign["complete"])
        self.assertIn("dataset preparation broke", campaign["latest_error"])

    def test_campaign_directory_uses_safe_unique_slug_shape(self) -> None:
        self.assertEqual(DEFAULT_OUTPUT / "official-20260723", _campaign_directory("official-20260723"))
        with self.assertRaises(ValueError):
            _campaign_directory("../unsafe")

    def test_main_exits_nonzero_when_campaign_is_not_published(self) -> None:
        result = CampaignResult(
            CampaignFingerprint(
                "suite", "data", "model", "prompt", "scorer", 3, (), "runtime"
            ),
            frozenset(),
            (),
            published=False,
            stop_reason="provider authorization failed",
        )
        with patch("benchmark_v3.run_evaluation.run_campaign", return_value=result), patch("benchmark_v3.run_evaluation.os.getenv", return_value="key"), patch("sys.argv", ["run_evaluation", "--campaign-id", "smoke"]):
            with self.assertRaisesRegex(SystemExit, "provider authorization failed"):
                main()

    def test_main_distinguishes_complete_processing_from_publication_failure(self) -> None:
        result = CampaignResult(
            CampaignFingerprint(
                "suite", "data", "model", "prompt", "scorer", 3, (), "runtime"
            ),
            frozenset(f"cell-{index}" for index in range(450)),
            (),
            published=False,
            stop_reason="publication failed: renderer broke",
            publication_failed=True,
        )
        with (
            patch("benchmark_v3.run_evaluation.run_campaign", return_value=result),
            patch("benchmark_v3.run_evaluation.os.getenv", return_value="key"),
            patch(
                "sys.argv",
                ["run_evaluation", "--campaign-id", "official"],
            ),
        ):
            with self.assertRaisesRegex(
                SystemExit,
                "Processing completed.*publication failed.*No evaluation cells need rerun",
            ):
                main()

    def test_main_forces_single_line_progress_when_requested(self) -> None:
        captured: list[CampaignConfig] = []
        result = CampaignResult(
            CampaignFingerprint(
                "suite", "data", "model", "prompt", "scorer", 3, (), "runtime"
            ),
            frozenset(),
            (),
            review_ready=True,
        )

        def run(config: CampaignConfig) -> CampaignResult:
            captured.append(config)
            return result

        with (
            patch("benchmark_v3.run_evaluation.run_campaign", side_effect=run),
            patch("benchmark_v3.run_evaluation.os.getenv", return_value="key"),
            patch(
                "sys.argv",
                [
                    "run_evaluation",
                    "--campaign-id",
                    "official",
                    "--interactive-progress",
                ],
            ),
        ):
            main()

        self.assertIsNotNone(captured[0].progress_factory)

    def test_external_launcher_uses_only_project_virtualenv_python(self) -> None:
        launcher = (BENCHMARK_DIR / "run_official_evaluation.cmd").read_text(encoding="utf-8")
        self.assertIn(".venv\\Scripts\\python.exe", launcher)
        self.assertNotIn("\n  python -m", launcher)
        self.assertIn("No project virtualenv Python", launcher)
        self.assertIn("Read-Host 'OpenRouter API key' -AsSecureString", launcher)
        self.assertIn("ZeroFreeBSTR", launcher)
        self.assertNotIn("set /p OPENROUTER_API_KEY", launcher)
        self.assertIn("--interactive-progress", launcher)
        self.assertIn('if "%~2"=="1" set "REPETITIONS=1"', launcher)
        self.assertIn("--repetitions %REPETITIONS%", launcher)

    def test_live_runbook_orders_targeted_before_official(self) -> None:
        runbook = (
            BENCHMARK_DIR / "LIVE_VALIDATION_RUNBOOK.md"
        ).read_text(encoding="utf-8")
        self.assertLess(
            runbook.index("Targeted one-repetition"),
            runbook.index("Official five-repetition"),
        )
        self.assertIn("semantic_only", runbook)
        self.assertIn("full", runbook)
        self.assertIn("review-package.md", runbook)
        self.assertIn("Do not publish HTML", runbook)

    def test_targeted_launcher_is_masked_and_nonpublishing(self) -> None:
        launcher = (
            BENCHMARK_DIR / "run_targeted_evaluation.cmd"
        ).read_text(encoding="utf-8")
        self.assertIn("Read-Host 'OpenRouter API key' -AsSecureString", launcher)
        self.assertIn("ZeroFreeBSTR", launcher)
        self.assertIn("-m benchmark_v3.run_targeted_evaluation", launcher)
        self.assertNotIn("-m benchmark_v3.publish", launcher)
        self.assertIn("--arm semantic_only --arm full", launcher)

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

    def test_harness_failure_is_not_checkpointed_or_scored(self) -> None:
        suite = self._suite()
        calls: list[str] = []
        def runner(item: WorkItem, *args: object) -> dict[str, object]:
            calls.append(item.key)
            if len(calls) == 1:
                raise RuntimeError("ordinary failure")
            return self._record(item)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = run_campaign(CampaignConfig(suite, directory, "offline", 1, self._dataset(), cell_runner=runner))
            failed_key = calls[0]
            status = json.loads((directory / "status.json").read_text())
            checkpoint_exists = (
                directory / "checkpoints" / f"{failed_key}.json"
            ).exists()
        self.assertEqual(1, len(calls))
        self.assertNotIn(failed_key, result.completed_keys)
        self.assertFalse(checkpoint_exists)
        self.assertTrue(result.stopped_for_infrastructure)
        self.assertEqual("harness", status["infrastructure_failure"]["source"])

    def test_provider_stop_leaves_cell_uncheckpointed_for_resume(self) -> None:
        suite = self._suite()
        calls: list[str] = []

        def runner(item: WorkItem, *args: object) -> dict[str, object]:
            calls.append(item.key)
            raise InfrastructureStop("provider authorization failed")

        with tempfile.TemporaryDirectory() as temporary:
            result = run_campaign(
                CampaignConfig(
                    suite,
                    Path(temporary),
                    "offline",
                    1,
                    self._dataset(),
                    cell_runner=runner,
                )
            )

        self.assertEqual(1, len(calls))
        self.assertEqual(frozenset(), result.completed_keys)
        self.assertTrue(result.stopped_for_infrastructure)
        self.assertEqual(0, len(result.records))

    def test_fingerprint_changes_when_runtime_configuration_changes(self) -> None:
        suite = self._suite()
        base = _fingerprint(suite, "dataset", workers=1)
        changed = _fingerprint(suite, "dataset", workers=2)
        self.assertNotEqual(base, changed)
        self.assertNotEqual(base.runtime_hash, changed.runtime_hash)

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

    def test_fingerprint_hashes_all_campaign_behavior_sources_deterministically(self) -> None:
        suite = self._suite()
        source_paths = tuple(sorted(
            (
                *SRC.joinpath("db_whisperer").rglob("*.py"),
                *BENCHMARK_DIR.glob("*.py"),
            ),
            key=lambda path: path.as_posix(),
        ))
        fingerprint = _fingerprint(suite, "dataset")
        self.assertEqual(_hash_paths(source_paths), fingerprint.prompt_hash)
        relative = {path.relative_to(PROJECT_ROOT).as_posix() for path in source_paths}
        self.assertIn("src/db_whisperer/ambiguity/semantic_column_prompt_builder.py", relative)
        self.assertIn("src/db_whisperer/querier/relationship_connectivity.py", relative)
        self.assertIn("benchmark_v3/sql_analysis.py", relative)

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
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "datasets" / "dataset.duckdb"
            database.parent.mkdir()
            connection = duckdb.connect(str(database))
            connection.execute("CREATE TABLE cached_marker (id INTEGER)")
            connection.close()
            schema = SchemaMetadata(
                database_path=str(database.resolve()),
                discovery_notes=("cached relationship warning",),
            )
            artifact = _reference_artifact_path(directory, "dataset", suite.sha256)
            artifact.write_text(json.dumps({
                "dataset_hash": "dataset", "suite_hash": suite.sha256,
                "schema": _serialize_schema(schema), "references": {},
                "database_hash": _database_hash(database),
            }), encoding="utf-8")
            with patch("benchmark_v3.run_evaluation.ingest_dataset", side_effect=AssertionError("cache miss")):
                dataset = _prepare_dataset(suite, directory, "dataset")
        self.assertEqual(("cached relationship warning",), dataset.schema.discovery_notes)

    def test_canary_preparation_validates_frozen_official_suite_shape(self) -> None:
        suite = replace(load_suite(DEFAULT_SUITE), repetitions=1)
        received: list[int] = []

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def ingest(dataset_path: Path, database: Path) -> SchemaMetadata:
                return SchemaMetadata(database_path=str(database.resolve()))

            def validate(reference_suite, schema, query):
                received.append(reference_suite.repetitions)
                return {}

            query = SimpleNamespace(client=SimpleNamespace(session=None))
            with (
                patch(
                    "benchmark_v3.run_evaluation.ingest_dataset",
                    side_effect=ingest,
                ),
                patch(
                    "benchmark_v3.run_evaluation.build_services",
                    return_value=(query, {}),
                ),
                patch(
                    "benchmark_v3.run_evaluation.validate_reference_suite",
                    side_effect=validate,
                ),
                patch(
                    "benchmark_v3.run_evaluation._database_hash",
                    return_value="database-hash",
                ),
            ):
                _prepare_dataset(suite, directory, "dataset")

        self.assertEqual([5], received)

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

    def test_checkpoint_record_must_repeat_the_exact_work_identity_and_shape(self) -> None:
        suite = self._suite()
        item = WorkItem(1, "case", "family", "correctness", "baseline")
        fingerprint = _fingerprint(suite, "dataset")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "checkpoints").mkdir()
            payload = _checkpoint_payload(item, fingerprint, self._record(item))
            payload["record"]["arm"] = "full"
            (directory / "checkpoints" / f"{item.key}.json").write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "record"):
                _load_matching_checkpoints(directory, (item,), fingerprint)

    def test_cache_with_missing_campaign_database_rebuilds_before_cells(self) -> None:
        suite = self._suite()
        schema = SchemaMetadata(database_path="missing.duckdb")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = _reference_artifact_path(directory, "dataset", suite.sha256)
            artifact.write_text(json.dumps({
                "dataset_hash": "dataset", "suite_hash": suite.sha256,
                "schema": _serialize_schema(schema), "references": {},
            }))
            with patch("benchmark_v3.run_evaluation.ingest_dataset", side_effect=RuntimeError("rebuild attempted")):
                with self.assertRaisesRegex(RuntimeError, "rebuild"):
                    _prepare_dataset(suite, directory, "dataset")

    def test_cache_with_corrupt_campaign_database_rebuilds_before_cells(self) -> None:
        suite = self._suite()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "datasets" / "dataset.duckdb"
            database.parent.mkdir()
            database.write_bytes(b"not a duckdb database")
            artifact = _reference_artifact_path(directory, "dataset", suite.sha256)
            artifact.write_text(json.dumps({
                "dataset_hash": "dataset", "suite_hash": suite.sha256,
                "schema": _serialize_schema(SchemaMetadata(database_path=str(database.resolve()))),
                "references": {}, "database_hash": _database_hash(database),
            }))
            with patch("benchmark_v3.run_evaluation.ingest_dataset", side_effect=RuntimeError("rebuild attempted")):
                with self.assertRaisesRegex(RuntimeError, "rebuild"):
                    _prepare_dataset(suite, directory, "dataset")

    def test_cache_rejects_incomplete_reference_contracts(self) -> None:
        case = EvaluationCase(
            id="needs_reference", family_id="needs_reference", kind="query",
            category="correctness", question="count rows", expected_sql="SELECT 1",
            reference=ReferenceContract("scalar"),
        )
        suite = EvaluationSuite(
            name="offline", version="v3", path=SUITE_PATH,
            dataset_path=SUITE_PATH.parent, model="model", repetitions=1,
            candidate_count=3, budget_usd=3.75, cases=(case,), sha256="suite",
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "dataset.duckdb"
            connection = duckdb.connect(str(database)); connection.execute("SELECT 1"); connection.close()
            cached = {
                "dataset_hash": "dataset", "suite_hash": suite.sha256,
                "database_hash": _database_hash(database),
                "schema": _serialize_schema(SchemaMetadata(database_path=str(database.resolve()))),
                "references": {},
            }
            self.assertFalse(_cached_dataset_is_valid(cached, suite, "dataset", database))

    def test_progress_starts_before_slow_dataset_preparation(self) -> None:
        suite = self._suite()
        lifecycle: list[str] = []
        class Progress:
            def start(self) -> None: lifecycle.append("start")
            def stop(self) -> None: lifecycle.append("stop")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("benchmark_v3.run_evaluation._prepare_dataset", side_effect=lambda *args: lifecycle.append("prepare") or self._dataset()):
                run_campaign(CampaignConfig(
                    suite, Path(temporary), "offline", 1, dataset=None,
                    cell_runner=lambda item, *args: self._record(item),
                    progress_factory=lambda observer: Progress(),
                ))
        self.assertEqual("start", lifecycle[0])
        self.assertLess(lifecycle.index("start"), lifecycle.index("prepare"))
        self.assertEqual("stop", lifecycle[-1])

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
