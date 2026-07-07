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
            [{"id": "case"}],
            run_id="run",
            model="gemma",
            started_at=now,
            completed_at=now,
            prompt_log_path=Path("prompts.jsonl"),
        )

        self.assertEqual(report["stage"], "iteration_3_clarification_simulation")
        self.assertFalse(report["scoring"]["deterministic_scores_available"])
        self.assertEqual(report["schema"]["table_count"], 2)
        self.assertEqual(report["schema"]["discovery_notes"], ["Skipped one table."])
        self.assertEqual(report["cases"], [{"id": "case"}])


if __name__ == "__main__":
    unittest.main()
