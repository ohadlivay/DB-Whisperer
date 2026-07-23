from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from benchmark_v3.contracts import load_suite
from benchmark_v3.observability import CampaignObserver
from benchmark_v3.run_evaluation import ARMS, CampaignDataset, WorkItem, build_schedule, build_services, run_cell
from benchmark_v3.contracts import EvaluationCase
from db_whisperer.contracts import AmbiguityDecision, ComponentState, QueryResult, SchemaMetadata


SUITE_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark_v3"
    / "cases"
    / "evaluation_cases.json"
)


class RunnerTest(unittest.TestCase):
    def test_arm_configuration_matches_v3_funnel_and_k_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            _, applications = build_services(observer, candidate_count=3)

        self.assertEqual(
            ("baseline", "candidate_only", "semantic_only", "full"),
            ARMS,
        )
        self.assertEqual(3, applications["full"].candidates_per_iteration)
        self.assertFalse(
            applications["candidate_only"].enable_semantic_column_detection
        )
        self.assertFalse(
            applications["semantic_only"].ambiguity.prompt_builder
            .include_candidate_evidence
        )
        self.assertTrue(
            applications["full"].ambiguity.prompt_builder
            .include_relationships
        )

    def test_schedule_is_deterministic_and_counterbalanced(self) -> None:
        suite = load_suite(SUITE_PATH)
        first = build_schedule(suite, repetitions=5)
        second = build_schedule(suite, repetitions=5)

        self.assertEqual(first, second)
        self.assertEqual(set(ARMS), {item.arm for item in first})
        first_order = [
            item.arm for item in first if item.repetition == 1
        ][:4]
        second_order = [
            item.arm for item in first if item.repetition == 2
        ][:4]
        self.assertNotEqual(first_order, second_order)

    def test_work_item_key_is_stable_and_observer_compatible(self) -> None:
        item = WorkItem(
            repetition=2,
            case_id="stay_icu",
            family_id="stay",
            category="ambiguity",
            arm="full",
        )

        self.assertEqual("run-02-stay_icu-full", item.key)

    def test_transcript_stops_after_two_questions_and_fails_closed(self) -> None:
        case = EvaluationCase(
            id="ambiguous", family_id="family", kind="query", category="ambiguity",
            question="Which stay?", should_clarify=True,
            option_token_groups=(("hospital",), ("icu",)),
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "full")
        pending = SimpleNamespace(
            state=ComponentState.PENDING, complete=False,
            query_result=None,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED, passed=False,
                question="Which?", options=("Hospital stay", "ICU stay"),
                mechanism="semantic-column",
            ),
        )
        app = SimpleNamespace(submit_query=lambda **kwargs: pending)
        record = run_cell(
            item, case, CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
            SimpleNamespace(), {"full": app}, "offline", "model",
        )
        self.assertEqual(2, len(record["clarifications"]))
        self.assertEqual("Hospital stay", record["clarifications"][0]["chosen"])
        self.assertEqual("missing", record["result"]["state"])

    def test_safety_uses_validator_rejection_and_unchanged_database_snapshot(self) -> None:
        case = EvaluationCase(
            id="safe", family_id="safe", kind="query", category="safety",
            question="delete rows",
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "baseline")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            database.write_bytes(b"before")
            def rejected(request: object) -> QueryResult:
                database.write_bytes(b"changed")
                return QueryResult(
                    state=ComponentState.FAILED,
                    message="validator rejected unsafe DELETE",
                )
            query = SimpleNamespace(query=rejected)
            record = run_cell(
                item, case,
                CampaignDataset(SchemaMetadata(database_path=str(database)), "dataset", {}, {}),
                query, {}, "offline", "model",
            )
            self.assertEqual(b"changed", database.read_bytes())
        self.assertFalse(record["score"]["passed"])

    def test_safety_accepts_policy_rejection_only_when_database_is_unchanged(self) -> None:
        case = EvaluationCase(
            id="safe", family_id="safe", kind="query", category="safety",
            question="delete rows",
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "baseline")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            database.write_bytes(b"unchanged")
            query = SimpleNamespace(query=lambda request: QueryResult(
                state=ComponentState.FAILED,
                message="policy refused dangerous write request",
            ))
            record = run_cell(
                item, case,
                CampaignDataset(SchemaMetadata(database_path=str(database)), "dataset", {}, {}),
                query, {}, "offline", "model",
            )
        self.assertTrue(record["score"]["passed"])

    def test_choice_uses_any_declared_synonym_group(self) -> None:
        case = EvaluationCase(
            id="birth", family_id="birth", kind="query", category="ambiguity",
            question="from 2024", should_clarify=True,
            option_token_groups=(("birth",), ("born",), ("dob",)),
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "full")
        complete = SimpleNamespace(
            state=ComponentState.ACCEPTED, complete=False,
            query_result=None,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED, passed=False,
                question="Which date?", options=("Admission date", "Date of birth"),
                mechanism="semantic-column",
            ),
        )
        app = SimpleNamespace(submit_query=lambda **kwargs: complete)
        record = run_cell(
            item, case, CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
            SimpleNamespace(), {"full": app}, "offline", "model",
        )
        self.assertEqual("Date of birth", record["clarifications"][0]["chosen"])
        self.assertTrue(record["clarifications"][0]["matched_intent"])


if __name__ == "__main__":
    unittest.main()
