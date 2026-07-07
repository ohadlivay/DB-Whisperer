"""Tests for the MIMIC A/B harness skeleton."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

import mimic_ab_run  # noqa: E402
from db_whisperer.contracts import (  # noqa: E402
    AmbiguityDecision,
    ComponentState,
    QueryResult,
    QueryWorkflowResult,
    SchemaMetadata,
)


class FakeQueryService:
    """Small QueryService stand-in that records prompts."""

    def __init__(self) -> None:
        self.requests = []

    def query(self, request):
        self.requests.append(request)
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="baseline ok",
            sql="SELECT 1 AS n;",
            columns=("n",),
            rows=((1,),),
        )


class FakeApplication:
    """Small ApplicationService stand-in returning a pending clarification."""

    candidates_per_iteration = 3
    max_iterations = 3

    def __init__(self, workflows=None) -> None:
        self.calls = []
        self.workflows = list(workflows) if workflows is not None else None

    def submit_query(self, **kwargs):
        self.calls.append(kwargs)
        if self.workflows is not None:
            index = min(len(self.calls) - 1, len(self.workflows) - 1)
            return self.workflows[index]
        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message="Which path?",
            iteration=1,
            complete=False,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question="Which path?",
                options=("direct", "admission"),
                reason="Multiple paths.",
                mechanism="join-path",
            ),
        )


class LoadMimicSuiteTest(unittest.TestCase):
    def test_loads_real_mimic_case_file(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )

        self.assertEqual(suite.name, "mimic_iii_clinical_ambiguity")
        self.assertEqual(suite.candidate_count, 3)
        self.assertEqual(len(suite.cases), 16)
        self.assertTrue(suite.dataset_path.exists())
        self.assertTrue(suite.judge["self_judged"])

        safety = suite.cases[14]
        self.assertEqual(safety.id, "tc_15_nonexistent_clinical_concept")
        self.assertEqual(safety.schema_elements, ())
        self.assertIsNone(safety.expected_sql)

    def test_rejects_inconsistent_clarification_contract(self) -> None:
        raw = {
            "id": "bad",
            "category": "x",
            "question": "q",
            "ambiguous": False,
            "ambiguity_type": "none",
            "intent": "i",
            "schema_elements": [],
            "expected_sql": None,
            "should_clarify": True,
            "simulated_user_answer": "answer",
            "expected_behavior": ["b"],
            "tests": ["t"],
        }

        with self.assertRaises(ValueError):
            mimic_ab_run.normalize_mimic_case(raw)

    def test_rejects_unambiguous_case_with_answer(self) -> None:
        raw = {
            "id": "bad",
            "category": "x",
            "question": "q",
            "ambiguous": False,
            "ambiguity_type": "none",
            "intent": "i",
            "schema_elements": [],
            "expected_sql": None,
            "should_clarify": False,
            "simulated_user_answer": "answer",
            "expected_behavior": ["b"],
            "tests": ["t"],
        }

        with self.assertRaises(ValueError):
            mimic_ab_run.normalize_mimic_case(raw)


class EvaluateCaseRawTest(unittest.TestCase):
    def test_runs_both_arms_once_and_serializes_outputs(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        case = suite.cases[3]
        schema = SchemaMetadata(
            database_path="mimic.duckdb",
            table_names=("PATIENTS", "LABEVENTS"),
        )
        query = FakeQueryService()
        app = FakeApplication()

        result = mimic_ab_run.evaluate_case_raw(
            case,
            schema,
            query,  # type: ignore[arg-type]
            app,  # type: ignore[arg-type]
            "key",
            "model",
        )

        self.assertEqual(result["id"], "tc_04_patient_labs_subject_history")
        self.assertEqual(len(query.requests), 1)
        self.assertEqual(query.requests[0].prompt, case.question)
        self.assertEqual(len(app.calls), app.max_iterations)
        self.assertEqual(app.calls[0]["prompt"], case.question)
        self.assertEqual(result["baseline"]["result"]["state"], "accepted")
        self.assertEqual(result["baseline"]["result"]["table"]["rows"], [[1]])
        self.assertEqual(result["full"]["workflow"]["state"], "pending")
        self.assertEqual(
            result["full"]["workflow"]["ambiguity"]["mechanism"],
            "join-path",
        )
        self.assertEqual(result["full"]["termination"], "max_iterations")
        self.assertTrue(result["full"]["unreliable"])


class ClarificationSimulationTest(unittest.TestCase):
    def _pending(self, options=("Direct SUBJECT_ID path", "Admission HADM_ID path")):
        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message="Which path?",
            iteration=1,
            complete=False,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question="Which path?",
                options=options,
                reason="Multiple paths.",
                mechanism="join-path",
            ),
        )

    def _complete(self):
        return QueryWorkflowResult(
            state=ComponentState.ACCEPTED,
            message="done",
            iteration=2,
            complete=True,
            query_result=QueryResult(
                state=ComponentState.ACCEPTED,
                message="ok",
                sql="SELECT 1;",
                columns=("n",),
                rows=((1,),),
            ),
        )

    def test_selects_option_by_simulated_answer_overlap(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        case = suite.cases[4]
        schema = SchemaMetadata(database_path="mimic.duckdb")
        app = FakeApplication([self._pending(), self._complete()])

        outcome = mimic_ab_run.run_full_simulated(
            case,
            schema,
            app,  # type: ignore[arg-type]
            "key",
            "model",
        )

        self.assertEqual(outcome["termination"], "complete")
        self.assertFalse(outcome["unreliable"])
        self.assertEqual(outcome["clarifications"][0]["chosen_index"], 1)
        self.assertEqual(outcome["clarifications"][0]["chosen"], "Admission HADM_ID path")
        self.assertEqual(
            app.calls[1]["clarifications"],
            ("Question: Which path?\nSelected answer: Admission HADM_ID path",),
        )

    def test_marks_unmatched_option_fallback_unreliable(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        case = suite.cases[4]
        schema = SchemaMetadata(database_path="mimic.duckdb")
        app = FakeApplication(
            [
                self._pending(options=("alpha", "beta")),
                self._complete(),
            ]
        )

        outcome = mimic_ab_run.run_full_simulated(
            case,
            schema,
            app,  # type: ignore[arg-type]
            "key",
            "model",
        )

        self.assertTrue(outcome["unreliable"])
        self.assertEqual(outcome["clarifications"][0]["chosen"], "alpha")
        self.assertIn(
            "no clarification option matched",
            outcome["unreliable_reasons"][0],
        )


class BuildReportTest(unittest.TestCase):
    def test_builds_iteration_two_report_shape(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        schema = SchemaMetadata(
            database_path="mimic.duckdb",
            table_names=("PATIENTS", "ADMISSIONS"),
            discovery_complete=False,
            discovery_notes=("Skipped one table.",),
        )
        now = datetime(2026, 7, 7, tzinfo=timezone.utc)

        report = mimic_ab_run.build_report(
            suite,
            schema,
            [
                {
                    "id": "case",
                    "ambiguous": False,
                    "should_clarify": False,
                    "comparison": "tie",
                    "baseline": {
                        "deterministic_score": {"score": 4},
                    },
                    "full": {
                        "deterministic_score": {"score": 4},
                        "clarifications": [],
                        "unreliable": False,
                    },
                }
            ],
            run_id="run",
            model="gemma",
            judge_model="gemma",
            judge_enabled=True,
            self_judged=True,
            started_at=now,
            completed_at=now,
            prompt_log_path=Path("prompts.jsonl"),
        )

        self.assertEqual(report["stage"], "iteration_5_qualitative_self_judge")
        self.assertTrue(report["scoring"]["deterministic_scores_available"])
        self.assertTrue(report["scoring"]["self_judge_available"])
        self.assertTrue(report["judge"]["self_judged"])
        self.assertEqual(report["schema"]["table_count"], 2)
        self.assertEqual(report["schema"]["discovery_notes"], ["Skipped one table."])
        self.assertEqual(report["summary"]["total_cases"], 1)


class CliArgsTest(unittest.TestCase):
    def test_parses_max_parallel_candidate_override(self) -> None:
        args = mimic_ab_run._parse_args(
            ["--limit", "1", "--max-parallel-candidates", "1"]
        )

        self.assertEqual(args.limit, 1)
        self.assertEqual(args.max_parallel_candidates, 1)


class DeterministicScoringTest(unittest.TestCase):
    def _case(self, expected_sql="SELECT 1;"):
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        source = suite.cases[0]
        return mimic_ab_run.MimicCase(
            id=source.id,
            category=source.category,
            question=source.question,
            ambiguous=source.ambiguous,
            ambiguity_type=source.ambiguity_type,
            intent=source.intent,
            schema_elements=source.schema_elements,
            expected_sql=expected_sql,
            should_clarify=source.should_clarify,
            simulated_user_answer=source.simulated_user_answer,
            expected_behavior=source.expected_behavior,
            tests=source.tests,
        )

    def test_exact_match_scores_four(self) -> None:
        score = mimic_ab_run.score_query_result_payload(
            self._case(),
            {
                "state": "accepted",
                "message": "ok",
                "sql": "SELECT 1;",
                "table": {"columns": ["n"], "rows": [[1]]},
            },
            {"columns": ["n"], "rows": [[1]]},
        )

        self.assertEqual(score["score"], 4)
        self.assertEqual(score["comparison"], "exact")
        self.assertTrue(score["exact_match"])

    def test_mismatch_scores_zero_without_judge(self) -> None:
        score = mimic_ab_run.score_query_result_payload(
            self._case(),
            {
                "state": "accepted",
                "message": "ok",
                "sql": "SELECT 2;",
                "table": {"columns": ["n"], "rows": [[2]]},
            },
            {"columns": ["n"], "rows": [[1]]},
        )

        self.assertEqual(score["score"], 0)
        self.assertEqual(score["comparison"], "deterministic_mismatch")

    def test_no_sql_expected_scores_refusal_as_four(self) -> None:
        score = mimic_ab_run.score_query_result_payload(
            self._case(expected_sql=None),
            {
                "state": "failed",
                "message": "read only",
                "sql": None,
                "table": {"columns": [], "rows": []},
            },
            None,
        )

        self.assertEqual(score["score"], 4)
        self.assertEqual(score["comparison"], "no_sql_expected")

    def test_no_sql_expected_scores_accepted_sql_as_zero(self) -> None:
        score = mimic_ab_run.score_query_result_payload(
            self._case(expected_sql=None),
            {
                "state": "accepted",
                "message": "ok",
                "sql": "SELECT * FROM x;",
                "table": {"columns": [], "rows": []},
            },
            None,
        )

        self.assertEqual(score["score"], 0)
        self.assertEqual(score["comparison"], "unexpected_sql")

    def test_full_score_tracks_clarification_quality(self) -> None:
        case = self._case()
        full = {
            "workflow": {
                "query_result": {
                    "state": "accepted",
                    "message": "ok",
                    "sql": "SELECT 1;",
                    "table": {"columns": ["n"], "rows": [[1]]},
                }
            },
            "clarifications": [],
            "termination": "complete",
            "unreliable": False,
        }

        score = mimic_ab_run.score_full(
            case,
            full,
            {"columns": ["n"], "rows": [[1]]},
        )

        self.assertEqual(score["score"], 4)
        self.assertEqual(score["clarification_score"], "not_applicable")


class SummaryTest(unittest.TestCase):
    def _result(
        self,
        *,
        case_id,
        ambiguous,
        should_clarify,
        comparison,
        baseline_score,
        full_score,
        asked,
        unreliable=False,
    ):
        return {
            "id": case_id,
            "ambiguous": ambiguous,
            "should_clarify": should_clarify,
            "comparison": comparison,
            "baseline": {
                "deterministic_score": {"score": baseline_score},
            },
            "full": {
                "deterministic_score": {"score": full_score},
                "clarifications": [{}] if asked else [],
                "unreliable": unreliable,
            },
        }

    def test_summarizes_scores_and_clarification_rates(self) -> None:
        summary = mimic_ab_run.summarize(
            [
                self._result(
                    case_id="amb",
                    ambiguous=True,
                    should_clarify=True,
                    comparison="full_better",
                    baseline_score=0,
                    full_score=4,
                    asked=True,
                ),
                self._result(
                    case_id="ctl",
                    ambiguous=False,
                    should_clarify=False,
                    comparison="tie",
                    baseline_score=4,
                    full_score=4,
                    asked=False,
                ),
                self._result(
                    case_id="bad",
                    ambiguous=False,
                    should_clarify=False,
                    comparison="baseline_better",
                    baseline_score=4,
                    full_score=0,
                    asked=True,
                    unreliable=True,
                ),
            ]
        )

        self.assertEqual(summary["total_cases"], 3)
        self.assertEqual(summary["baseline"]["mean_score"], 2.6667)
        self.assertEqual(summary["full"]["mean_score"], 2.6667)
        self.assertEqual(summary["ambiguous"]["expected_clarification_rate"], 1.0)
        self.assertEqual(summary["control"]["spurious_clarification_rate"], 0.5)
        self.assertEqual(summary["unreliable_cases"], ["bad"])


class QualitativeJudgeTest(unittest.TestCase):
    def _case_and_result(self):
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        case = suite.cases[3]
        result = {
            "comparison": "full_better",
            "score_delta": 4,
            "baseline": {
                "result": {"sql": "SELECT baseline;"},
                "deterministic_score": {"score": 0},
            },
            "full": {
                "workflow": {
                    "query_result": {"sql": "SELECT full;"}
                },
                "deterministic_score": {"score": 4},
                "clarifications": [
                    {
                        "question": "Which path?",
                        "chosen": "direct",
                    }
                ],
                "unreliable": False,
                "unreliable_reasons": [],
            },
        }
        return case, result

    def test_validates_qualitative_judgment(self) -> None:
        judgment = mimic_ab_run.validate_qualitative_judgment(
            {
                "clarification_quality": "pass",
                "baseline_assumption": "wrong",
                "response_faithfulness": "not_applicable",
                "trust_note": "Clarification improved interpretability.",
                "reason": "The full pipeline followed the declared intent.",
            }
        )

        self.assertEqual(judgment["status"], "accepted")
        self.assertEqual(judgment["clarification_quality"], "pass")

    def test_rejects_invalid_qualitative_judgment(self) -> None:
        with self.assertRaises(ValueError):
            mimic_ab_run.validate_qualitative_judgment(
                {
                    "clarification_quality": "excellent",
                    "baseline_assumption": "wrong",
                    "response_faithfulness": "pass",
                    "trust_note": "x",
                    "reason": "x",
                }
            )

    def test_qualitative_judge_parses_json_response(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": {
                                    "clarification_quality": "pass",
                                    "baseline_assumption": "wrong",
                                    "response_faithfulness": "not_applicable",
                                    "trust_note": "Useful clarification.",
                                    "reason": "It selected the intended path.",
                                }
                            }
                        }
                    ]
                }

        calls = []

        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return Response()

        case, result = self._case_and_result()
        judgment = mimic_ab_run.qualitative_judge(
            "key",
            "gemma",
            case,
            result,
            post=post,
        )

        self.assertEqual(judgment["status"], "accepted")
        self.assertEqual(judgment["baseline_assumption"], "wrong")
        self.assertEqual(calls[0][1]["json"]["model"], "gemma")
        self.assertEqual(
            calls[0][1]["json"]["response_format"],
            {"type": "json_object"},
        )

    def test_evaluate_case_attaches_judgment_from_injected_function(self) -> None:
        suite = mimic_ab_run.load_mimic_suite(
            ROOT / "benchmark" / "mimic_ab_cases.json"
        )
        case = suite.cases[13]
        schema = SchemaMetadata(database_path="mimic.duckdb")
        query = FakeQueryService()
        app = FakeApplication(
            [
                QueryWorkflowResult(
                    state=ComponentState.ACCEPTED,
                    message="done",
                    complete=True,
                    query_result=QueryResult(
                        state=ComponentState.ACCEPTED,
                        message="ok",
                        sql="SELECT 1 AS n;",
                        columns=("n",),
                        rows=((1,),),
                    ),
                )
            ]
        )

        def judge(case_arg, result_arg):
            self.assertEqual(case_arg.id, case.id)
            self.assertIn("baseline", result_arg)
            return {"status": "accepted", "trust_note": "ok"}

        result = mimic_ab_run.evaluate_case(
            case,
            schema,
            query,  # type: ignore[arg-type]
            app,  # type: ignore[arg-type]
            "key",
            "model",
            qualitative_judge_fn=judge,
        )

        self.assertEqual(result["qualitative_judgment"]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
