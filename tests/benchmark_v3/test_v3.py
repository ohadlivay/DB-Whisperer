from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Barrier, Lock, get_ident
import time
import unittest
from unittest.mock import patch

from benchmark_v3.aggregate_results import aggregate
from benchmark_v3.contracts import ARMS, EvaluationCase, load_suite
from benchmark_v3.run_evaluation import (
    DEFAULT_OUTPUT,
    DEFAULT_SUITE,
    DEFAULT_WORKERS,
    MAX_WORKERS,
    EvaluationJob,
    ProgressReporter,
    build_services,
    choose_option,
    competing_intents,
    execute_jobs,
    prepare_references,
    run,
    run_etl_cases,
    thread_local_provider,
    worker_count,
)
from benchmark_v3.render_report import write_report
from benchmark_v3.scoring import score_case
from db_whisperer.contracts import ComponentState, QueryResult, SchemaMetadata


class EvaluationV3Test(unittest.TestCase):
    def test_suite_has_four_active_arms_and_no_join_path_mechanism(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        self.assertEqual(
            ("baseline", "candidate_only", "semantic_only", "full"),
            ARMS,
        )
        self.assertEqual("3.1.0", suite.version)
        self.assertEqual("evaluation_v3_1.json", DEFAULT_OUTPUT.name)
        self.assertNotIn("join_path", {case.category for case in suite.cases})
        self.assertNotIn("join-path", {case.expected_mechanism for case in suite.cases})
        self.assertFalse(any(case.id.startswith("jp_") for case in suite.cases))
        self.assertFalse(any(case.family_id.startswith("jp_") for case in suite.cases))

    def test_arm_configuration_is_independent_and_meaningful(self) -> None:
        _, applications = build_services(2)
        candidate = applications["candidate_only"]
        semantic = applications["semantic_only"]
        full = applications["full"]
        self.assertFalse(candidate.enable_semantic_column_detection)
        self.assertFalse(candidate.ambiguity.prompt_builder.include_schema_context)
        self.assertTrue(semantic.enable_semantic_column_detection)
        self.assertFalse(semantic.ambiguity.prompt_builder.include_relationships)
        self.assertFalse(semantic.ambiguity.prompt_builder.include_candidate_evidence)
        self.assertTrue(full.ambiguity.prompt_builder.include_relationships)
        self.assertTrue(full.ambiguity.prompt_builder.include_candidate_evidence)

    def test_option_selection_is_deterministic(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        case = suite.query_cases[0]
        option, status = choose_option(
            case,
            ("Use hospital admissions", "Use the patient directly"),
            competing_intents(case, suite),
        )
        self.assertEqual("matched", status)
        self.assertEqual("Use the patient directly", option)

    def test_option_selection_rejects_ties(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        option, status = choose_option(case, ("patient", "patient"))
        self.assertEqual("indeterminate", status)
        self.assertEqual("patient", option)

    def test_family_relative_option_matching_covers_competing_and_unrelated_text(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        case = suite.query_cases[0]
        competitors = competing_intents(case, suite)
        _, competing_status = choose_option(
            case,
            ("Use hospital admissions", "unrelated wording"),
            competitors,
        )
        _, unrelated_status = choose_option(
            case,
            ("unrelated wording", "also unrelated"),
            competitors,
        )
        self.assertEqual("mismatched", competing_status)
        self.assertEqual("indeterminate", unrelated_status)

    def test_worker_cli_range_and_default(self) -> None:
        self.assertEqual(2, DEFAULT_WORKERS)
        self.assertEqual(1, worker_count("1"))
        self.assertEqual(MAX_WORKERS, worker_count(str(MAX_WORKERS)))
        with self.assertRaises(Exception):
            worker_count("0")
        with self.assertRaises(Exception):
            worker_count(str(MAX_WORKERS + 1))

    def test_parallel_jobs_are_bounded_deterministic_and_progress_is_sanitized(self) -> None:
        case = EvaluationCase(id="case", family_id="family", kind="query", category="control")
        jobs = [EvaluationJob(1, index, replace(case, id=f"case_{index}"), 0, "baseline") for index in range(6)]
        lock = Lock()
        active = 0
        maximum = 0

        def evaluate(job: EvaluationJob) -> dict:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02 if job.case_index % 2 else 0.04)
            with lock:
                active -= 1
            return {
                "repetitions": 1,
                "result": {"state": "accepted", "sql": "sensitive"},
                "score": {"passed": True},
                "case_id": job.case.id,
            }

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()) as output:
            progress_path = Path(temporary) / "progress.jsonl"
            rows = execute_jobs(
                jobs,
                2,
                evaluate,
                lambda job, error: {},
                ProgressReporter(progress_path, len(jobs)),
            )
            progress_log = progress_path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in progress_log.splitlines()]
        self.assertEqual(2, maximum)
        self.assertEqual([f"case_{index}" for index in range(6)], [row["case_id"] for row in rows])
        progress_text = output.getvalue()
        for expected in ("6/6", "100.0%", "rep 1/1", "case_", "baseline", "accepted", "elapsed", "ETA"):
            self.assertIn(expected, progress_text)
        approved = {
            "event", "timestamp", "completed", "total", "percent", "repetition",
            "case_id", "arm", "duration_seconds", "state", "passed", "error",
        }
        self.assertTrue(all(set(event) <= approved for event in events))
        self.assertNotIn("sensitive", progress_log)

    def test_failed_job_is_recorded_without_stopping_campaign(self) -> None:
        case = EvaluationCase(id="case", family_id="family", kind="query", category="control")
        jobs = [EvaluationJob(1, index, replace(case, id=f"case_{index}"), 0, "baseline") for index in range(3)]

        def evaluate(job: EvaluationJob) -> dict:
            if job.case_index == 1:
                raise RuntimeError("private\nvalue")
            return {"repetitions": 1, "result": {"state": "accepted"}, "score": {"passed": True}}

        def failed(job: EvaluationJob, error: Exception) -> dict:
            return {"repetitions": 1, "result": {"state": "failed"}, "score": {"passed": False}}

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            progress_path = Path(temporary) / "progress.jsonl"
            rows = execute_jobs(jobs, 2, evaluate, failed, ProgressReporter(progress_path, len(jobs)))
            events = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(3, len(rows))
        self.assertEqual("failed", rows[1]["result"]["state"])
        failure_event = next(event for event in events if event["case_id"] == "case_1")
        self.assertEqual("RuntimeError", failure_event["error"])

    def test_thread_local_services_are_isolated(self) -> None:
        created = []
        lock = Lock()
        barrier = Barrier(2)

        def factory() -> object:
            value = object()
            with lock:
                created.append((get_ident(), value))
            return value

        provider = thread_local_provider(factory)

        def use_provider() -> tuple[object, object]:
            first = provider()
            barrier.wait()
            return first, provider()

        with ThreadPoolExecutor(max_workers=2) as executor:
            pairs = list(executor.map(lambda _: use_provider(), range(2)))
        self.assertEqual(2, len(created))
        self.assertIs(pairs[0][0], pairs[0][1])
        self.assertIs(pairs[1][0], pairs[1][1])
        self.assertIsNot(pairs[0][0], pairs[1][0])

    def test_reference_queries_execute_once_per_case(self) -> None:
        suite = load_suite(DEFAULT_SUITE)

        class Query:
            def __init__(self) -> None:
                self.calls = 0

            def execute_candidate(self, candidate, database_path):
                self.calls += 1
                return QueryResult(ComponentState.ACCEPTED, "ok", candidate.sql, (), ())

        query = Query()
        references = prepare_references(suite, query, "database.duckdb")
        self.assertEqual(sum(case.expected_sql is not None for case in suite.query_cases), query.calls)
        self.assertEqual({case.id for case in suite.query_cases}, set(references))

    def test_scoring_requires_expected_clarification_behavior(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        result = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            columns=("x",),
            rows=((1,),),
        )
        score = score_case(case, result, result, [])
        self.assertFalse(score["passed"])

    def test_scoring_requires_clarification_compliance(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        result = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            columns=("x",),
            rows=((1,),),
        )
        score = score_case(case, result, result, [{
            "matched_intent": True,
            "mechanism": case.expected_mechanism,
            "compliance_passed": False,
        }])

        self.assertFalse(score["passed"])
        self.assertFalse(
            score["clarification"]["applied_to_final_sql"]
        )

    def test_scoring_does_not_gate_on_evidence_mechanism(self) -> None:
        case = EvaluationCase(
            id="arbitrary_case", family_id="family", kind="query", category="semantic_column",
            should_clarify=True, expected_mechanism="candidate-comparison",
            required_tables=("records",),
        )
        result = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            sql="SELECT value FROM records",
            columns=("value",),
            rows=((1,),),
        )
        score = score_case(case, result, result, [{
            "matched_intent": True,
            "mechanism": "semantic-column",
            "compliance_passed": True,
        }])
        self.assertTrue(score["passed"])

    def test_generic_predicate_scoring_is_case_id_and_limit_independent(self) -> None:
        case = EvaluationCase(
            id="unrelated_alpha", family_id="f", kind="query", category="control",
            required_tables=("records",), required_column_groups=(("value",),),
            required_predicates=({"column": "record_id", "operator": "eq", "value": 42},),
        )
        actual = QueryResult(
            ComponentState.ACCEPTED, "ok",
            sql='SELECT value, extra FROM records WHERE record_id = 42 LIMIT 999',
            columns=("value", "extra"), rows=(("x", 1),),
        )
        self.assertTrue(score_case(case, actual, None, [])["passed"])
        self.assertEqual(
            score_case(case, actual, None, [])["correctness"],
            score_case(replace(case, id="totally_different"), actual, None, [])["correctness"],
        )
        wrong = replace(actual, sql='SELECT value FROM records WHERE record_id = 7')
        self.assertFalse(score_case(case, wrong, None, [])["correctness"])

    def test_scalar_scoring_ignores_alias_but_not_value(self) -> None:
        case = EvaluationCase(
            id="scalar", family_id="scalar", kind="query", category="correctness",
            required_tables=("events",), required_aggregates=("count",), result_policy="scalar",
        )
        expected = QueryResult(ComponentState.ACCEPTED, "ok", sql="SELECT count(*) FROM events", columns=("count",), rows=((9,),))
        actual = QueryResult(ComponentState.ACCEPTED, "ok", sql="SELECT count(*) AS total_events FROM events", columns=("total_events",), rows=((9,),))
        self.assertTrue(score_case(case, actual, expected, [])["passed"])
        self.assertFalse(score_case(case, replace(actual, rows=((8,),)), expected, [])["correctness"])

    def test_safety_separates_containment_from_refusal(self) -> None:
        case = EvaluationCase(
            id="unsafe", family_id="unsafe", kind="query", category="safety", result_policy="safety",
        )
        safe_fallback = QueryResult(ComponentState.ACCEPTED, "ok", sql="SELECT 1", columns=("1",), rows=((1,),))
        score = score_case(case, safe_fallback, None, [])
        self.assertTrue(score["safety"]["containment"])
        self.assertFalse(score["safety"]["refusal_fidelity"])

    def test_scoring_accepts_a_grounded_required_column_subset(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[8]
        expected = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            sql="SELECT hadm_id, charttime, value FROM labevents WHERE subject_id=10006 AND hadm_id IS NOT NULL",
            columns=("hadm_id", "charttime", "value"),
            rows=((1, "2026-01-01", "x"),),
        )
        actual = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            sql="SELECT hadm_id, charttime FROM labevents WHERE subject_id=10006 AND hadm_id IS NOT NULL",
            columns=("hadm_id", "charttime"),
            rows=((1, "2026-01-01"),),
        )
        score = score_case(case, actual, expected, [], SchemaMetadata())
        self.assertTrue(score["passed"])

    def test_etl_cases_are_executed_and_match_their_manifests(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        with tempfile.TemporaryDirectory() as temporary:
            records = run_etl_cases(suite, Path(temporary))
        self.assertEqual(2, len(records))
        self.assertTrue(all(record["score"]["passed"] for record in records))

    def test_aggregate_includes_etl_results(self) -> None:
        report = {
            "report_type": "dbwhisperer_v3_rescored",
            "retrospective": True,
            "scoring_version": "test",
            "suite_version": "3.0.0",
            "suite_hash": "hash",
            "model": "model",
            "records": [
                {
                    "run": 1,
                    "case_id": "case_1",
                    "family_id": "family_1",
                    "category": "control",
                    "arm": arm,
                    "clarifications": [],
                    "score": {
                        "passed": True,
                        "correctness": True,
                        "sql_contract": {"checks": []},
                        "result_contract": {"checks": []},
                        "safety": None,
                        "clarification": {
                            "expected": False,
                            "asked": False,
                            "intent_matched": False,
                            "source": "none",
                            "applied_to_final_sql": False,
                        },
                    },
                    "original_score": {"passed": True},
                }
                for arm in ARMS
            ],
            "etl": [{"score": {"passed": True}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            result = aggregate([path])
        self.assertEqual({"passed": 1, "total": 1}, {
            key: result["etl"][key] for key in ("passed", "total")
        })

    def test_html_report_accepts_a_raw_v3_run(self) -> None:
        report = {
            "report_type": "dbwhisperer_v3_rescored",
            "retrospective": True,
            "scoring_version": "test",
            "suite_version": "3.0.0",
            "suite_hash": "hash",
            "model": "model",
            "records": [{
                "run": 1,
                "case_id": "case_1",
                "family_id": "family_1",
                "category": "control",
                "arm": arm,
                "result": {"sql": "SELECT 1", "columns": ["1"]},
                "clarifications": [],
                "score": {
                    "passed": True,
                    "correctness": True,
                    "sql_contract": {"checks": []},
                    "result_contract": {"checks": []},
                    "safety": None,
                    "clarification": {
                        "expected": False,
                        "asked": False,
                        "intent_matched": False,
                        "source": "none",
                        "applied_to_final_sql": False,
                    },
                },
                "original_score": {"passed": True},
            } for arm in ARMS],
            "etl": [],
            "case_contracts": [{"id": "case_1", "question": "Synthetic question", "intent_id": "one"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "run.json"
            output = Path(temporary) / "report.html"
            source.write_text(json.dumps(report), encoding="utf-8")
            summary, details = write_report(source, output)
            self.assertIn("Retrospective analysis", summary.read_text(encoding="utf-8"))
            self.assertIn("1 unique prompts", summary.read_text(encoding="utf-8"))
            self.assertIn("case_1", details.read_text(encoding="utf-8"))

    def test_live_v3_1_report_has_no_retrospective_language(self) -> None:
        report = {
            "report_type": "dbwhisperer_v3_evaluation",
            "retrospective": False,
            "scoring_version": "3.1",
            "suite_version": "3.1.0",
            "suite_hash": "hash",
            "model": "model",
            "records": [{
                "run": 1,
                "case_id": "case_1",
                "family_id": "family_1",
                "category": "control",
                "arm": arm,
                "result": {"state": "accepted", "sql": "SELECT 1", "columns": ["1"]},
                "clarifications": [],
                "score": {
                    "passed": True,
                    "correctness": True,
                    "sql_contract": {"checks": []},
                    "result_contract": {"checks": []},
                    "safety": None,
                    "clarification": {
                        "expected": False, "asked": False, "intent_matched": False,
                        "source": "none", "applied_to_final_sql": False,
                    },
                },
            } for arm in ARMS],
            "etl": [],
            "case_contracts": [{"id": "case_1", "question": "Synthetic question", "intent_id": "one"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "evaluation_v3_1.json"
            output = Path(temporary) / "evaluation_v3_1.html"
            source.write_text(json.dumps(report), encoding="utf-8")
            summary, details = write_report(source, output)
            combined = summary.read_text(encoding="utf-8") + details.read_text(encoding="utf-8")
        self.assertIn("Final V3.1 evaluation", combined)
        self.assertNotIn("Retrospective analysis", combined)
        self.assertNotIn("original_score", combined)
        self.assertNotIn("original:", combined)

    def test_fully_mocked_campaign_writes_json_and_both_html_reports(self) -> None:
        source_suite = load_suite(DEFAULT_SUITE)
        suite = replace(source_suite, repetitions=1, cases=(source_suite.query_cases[0],))
        result = QueryResult(ComponentState.ACCEPTED, "ok", "SELECT 1", ("1",), ((1,),))

        class Query:
            def query(self, request):
                return result

            def execute_candidate(self, candidate, database_path):
                return result

        def fake_services(candidate_count):
            return Query(), {arm: object() for arm in ARMS if arm != "baseline"}

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            output = Path(temporary) / "evaluation_v3_1.json"
            with (
                patch("benchmark_v3.run_evaluation.load_suite", return_value=suite),
                patch("benchmark_v3.run_evaluation.ingest_dataset", return_value=SchemaMetadata(database_path="database.duckdb")),
                patch("benchmark_v3.run_evaluation.run_etl_cases", return_value=[]),
                patch("benchmark_v3.run_evaluation.build_services", side_effect=fake_services),
                patch("benchmark_v3.run_evaluation.run_application", return_value=(result, [])),
            ):
                report = run(DEFAULT_SUITE, output, "fake-key", workers=2)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("3.1", payload["scoring_version"])
            self.assertEqual(4, len(payload["records"]))
            self.assertEqual(report["records"], payload["records"])
            self.assertTrue(output.with_suffix(".html").exists())
            self.assertTrue(output.with_name("evaluation_v3_1_cases.html").exists())


if __name__ == "__main__":
    unittest.main()
