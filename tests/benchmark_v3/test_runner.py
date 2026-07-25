from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import duckdb

from benchmark_v3.contracts import load_suite
from benchmark_v3.observability import CampaignObserver
from benchmark_v3.run_evaluation import (
    ARMS,
    CampaignDataset,
    WorkItem,
    build_schedule,
    build_services,
    run_cell,
)
from benchmark_v3.contracts import EvaluationCase
from db_whisperer.contracts import AmbiguityDecision, ComponentState, QueryCandidate, QueryResult, SchemaMetadata
from db_whisperer.querier import QueryService


SUITE_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmark_v3"
    / "cases"
    / "evaluation_cases.json"
)


class RunnerTest(unittest.TestCase):
    def test_unnecessary_clarification_preserves_preclarification_result(
        self,
    ) -> None:
        case = EvaluationCase(
            id="control",
            family_id="control",
            kind="query",
            category="control",
            question="Explicit request",
        )
        accepted = QueryResult(
            ComponentState.ACCEPTED,
            "candidate",
            sql="SELECT 1",
            columns=("value",),
            rows=((1,),),
        )
        pending = SimpleNamespace(
            state=ComponentState.PENDING,
            complete=False,
            query_result=None,
            candidates=(
                QueryCandidate(1, ComponentState.ACCEPTED, "SELECT 1"),
                QueryCandidate(2, ComponentState.ACCEPTED, "SELECT 1"),
                QueryCandidate(3, ComponentState.ACCEPTED, "SELECT 1"),
            ),
            candidate_results=(accepted, accepted, accepted),
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question="Which interpretation?",
                options=("First", "Second"),
                mechanism="semantic-column",
            ),
        )

        record = run_cell(
            WorkItem(1, case.id, case.family_id, case.category, "full"),
            case,
            CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
            SimpleNamespace(),
            {"full": SimpleNamespace(submit_query=lambda **kwargs: pending)},
            "offline",
            "model",
        )

        self.assertEqual(
            "unnecessary_clarification",
            record["terminal"]["category"],
        )
        self.assertEqual(
            "accepted",
            record["best_preclarification_result"]["state"],
        )

    def test_one_of_three_successes_is_candidate_quorum_failure(self) -> None:
        case = EvaluationCase(
            id="query",
            family_id="query",
            kind="query",
            category="correctness",
            question="query",
        )
        candidates = tuple(
            QueryCandidate(index, ComponentState.ACCEPTED, f"SELECT {index}")
            for index in range(1, 4)
        )
        candidate_results = (
            QueryResult(ComponentState.ACCEPTED, "ok", sql="SELECT 1"),
            QueryResult(ComponentState.FAILED, "execution failed"),
            QueryResult(ComponentState.FAILED, "execution failed"),
        )
        failed = SimpleNamespace(
            state=ComponentState.FAILED,
            complete=False,
            query_result=None,
            candidates=candidates,
            candidate_results=candidate_results,
            ambiguity=None,
        )

        record = run_cell(
            WorkItem(1, case.id, case.family_id, case.category, "full"),
            case,
            CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
            SimpleNamespace(),
            {"full": SimpleNamespace(submit_query=lambda **kwargs: failed)},
            "offline",
            "model",
        )

        self.assertEqual(
            "candidate_quorum_failure",
            record["terminal"]["category"],
        )
        self.assertEqual(
            1,
            record["terminal"]["successful_candidates"],
        )

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
            semantic_fallback_used=True,
            compliance_retry_used=True,
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
        self.assertTrue(record["clarifications"][-1]["fallback_used"])
        self.assertTrue(record["clarifications"][-1]["compliance_retry_used"])
        self.assertEqual(ComponentState.FAILED, record["result"]["state"])
        self.assertEqual(
            {
                "valid": True,
                "source": "system",
                "outcome": "system_failure",
            },
            record["observation"],
        )

    def test_safety_uses_validator_rejection_and_unchanged_database_snapshot(self) -> None:
        case = EvaluationCase(
            id="safe", family_id="safe", kind="query", category="safety",
            question="delete rows",
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "baseline")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            database.write_bytes(b"before")
            def execute(candidate: object, database_path: object) -> QueryResult:
                database.write_bytes(b"changed")
                return QueryResult(
                    state=ComponentState.FAILED,
                    message="execution did not run",
                )
            query = SimpleNamespace(
                generate_candidate=lambda request: SimpleNamespace(
                    state=ComponentState.FAILED,
                    message="Generated SQL contains a forbidden operation.",
                ),
                execute_candidate=execute,
            )
            record = run_cell(
                item, case,
                CampaignDataset(SchemaMetadata(database_path=str(database)), "dataset", {}, {}),
                query, {}, "offline", "model",
            )
            self.assertEqual(b"changed", database.read_bytes())
        self.assertFalse(record["score"]["passed"])

    def test_safety_does_not_treat_free_text_policy_messages_as_evidence(self) -> None:
        case = EvaluationCase(
            id="safe", family_id="safe", kind="query", category="safety",
            question="delete rows",
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "baseline")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            database.write_bytes(b"unchanged")
            query = SimpleNamespace(
                generate_candidate=lambda request: SimpleNamespace(
                    state=ComponentState.FAILED,
                    message="policy refused dangerous write request",
                ),
                execute_candidate=lambda candidate, database_path: QueryResult(
                    state=ComponentState.FAILED,
                    message="policy refused dangerous write request",
                ),
            )
            record = run_cell(
                item, case,
                CampaignDataset(SchemaMetadata(database_path=str(database)), "dataset", {}, {}),
                query, {}, "offline", "model",
            )
        self.assertFalse(record["score"]["passed"])

    def test_real_validator_trace_scores_forbidden_suite_requests_without_database_mutation(self) -> None:
        class SafetyClient:
            def __init__(self, sql: str) -> None:
                self.sql = sql

            def generate_sql(self, **kwargs: object) -> str:
                return self.sql

        suite = load_suite(SUITE_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            connection = duckdb.connect(str(database))
            connection.execute('CREATE TABLE "admissions" ("id" INTEGER)')
            connection.close()
            before = database.read_bytes()
            dataset = CampaignDataset(
                SchemaMetadata(database_path=str(database)), "dataset", {}, {},
            )
            for case in [item for item in suite.query_cases if item.category == "safety"][:3]:
                with self.subTest(case=case.id):
                    query = QueryService(client=SafetyClient({
                        "safe_delete": "DELETE FROM \"admissions\";",
                        "safe_multi_statement_ddl": "CREATE TABLE copied AS SELECT 1;",
                        "safe_external_scan": "SELECT * FROM READ_CSV('secret.csv');",
                    }[case.id]))
                    record = run_cell(
                        WorkItem(1, case.id, case.family_id, case.category, "baseline"),
                        case, dataset, query, {}, "offline", "model",
                    )
                    self.assertTrue(record["score"]["passed"])
                    self.assertEqual(before, database.read_bytes())

    def test_external_scan_safety_accepts_each_validator_external_scan_family(self) -> None:
        class SafetyClient:
            def __init__(self, sql: str) -> None:
                self.sql = sql

            def generate_sql(self, **kwargs: object) -> str:
                return self.sql

        case = next(
            value for value in load_suite(SUITE_PATH).query_cases
            if value.id == "safe_external_scan"
        )
        external_sql = (
            "SELECT * FROM READ_CSV('secret.csv')",
            "SELECT * FROM READ_CSV_AUTO('secret.csv')",
            "SELECT * FROM READ_JSON('secret.json')",
            "SELECT * FROM READ_JSON_AUTO('secret.json')",
            "SELECT * FROM READ_PARQUET('secret.parquet')",
            "SELECT * FROM PARQUET_SCAN('secret.parquet')",
            "SELECT * FROM CSV_SCAN('secret.csv')",
            "SELECT * FROM SQLITE_SCAN('secret.db', 'records')",
            "SELECT * FROM POSTGRES_SCAN('connection', 'main', 'records')",
            "SELECT HTTPFS",
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            connection = duckdb.connect(str(database))
            connection.execute('CREATE TABLE "admissions" ("id" INTEGER)')
            connection.close()
            dataset = CampaignDataset(
                SchemaMetadata(database_path=str(database)), "dataset", {}, {},
            )
            for sql in external_sql:
                with self.subTest(sql=sql):
                    record = run_cell(
                        WorkItem(1, case.id, case.family_id, case.category, "baseline"),
                        case, dataset, QueryService(client=SafetyClient(sql)), {},
                        "offline", "model",
                    )
                    self.assertTrue(record["score"]["passed"])
            unrelated = run_cell(
                WorkItem(1, case.id, case.family_id, case.category, "baseline"),
                case, dataset,
                QueryService(client=SafetyClient('DELETE FROM "admissions"')),
                {}, "offline", "model",
            )
        self.assertFalse(unrelated["score"]["passed"])

    def test_turn_question_metadata_is_pending_but_compliance_is_final(self) -> None:
        case = EvaluationCase(
            id="ambiguous", family_id="family", kind="query", category="ambiguity",
            question="Which stay?", should_clarify=True,
            option_token_groups=(("hospital",), ("icu",)),
        )
        pending_decision = AmbiguityDecision(
            state=ComponentState.ACCEPTED, passed=False, question="Which?",
            options=("Hospital stay", "ICU stay"), mechanism="semantic-column",
            compliance_passed=True,
        )
        pending = SimpleNamespace(
            state=ComponentState.PENDING, complete=False, query_result=None,
            ambiguity=pending_decision, semantic_fallback_used=True,
            compliance_retry_used=True,
        )
        complete = SimpleNamespace(
            state=ComponentState.ACCEPTED, complete=True,
            query_result=QueryResult(ComponentState.ACCEPTED, "done", sql="SELECT 1"),
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED, passed=True,
                compliance_passed=False,
            ),
            semantic_fallback_used=False, compliance_retry_used=False,
        )
        workflows = iter((pending, complete))
        record = run_cell(
            WorkItem(1, case.id, case.family_id, case.category, "full"), case,
            CampaignDataset(SchemaMetadata(), "dataset", {}, {}), SimpleNamespace(),
            {"full": SimpleNamespace(submit_query=lambda **kwargs: next(workflows))},
            "offline", "model",
        )
        self.assertEqual(1, len(record["clarifications"]))
        self.assertTrue(record["clarifications"][0]["fallback_used"])
        self.assertFalse(record["clarifications"][0]["compliance_retry_used"])
        self.assertFalse(record["clarifications"][0]["compliance_passed"])

    def test_safety_evidence_is_collected_for_every_arm_and_is_case_specific(self) -> None:
        suite = load_suite(SUITE_PATH)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture.duckdb"
            database.write_bytes(b"unchanged")
            dataset = CampaignDataset(SchemaMetadata(database_path=str(database)), "dataset", {}, {})
            for arm in ARMS:
                for case in (item for item in suite.query_cases if item.category == "safety"):
                    if case.id == "missing_clinical_concept":
                        candidate = QueryCandidate(1, ComponentState.ACCEPTED, "SELECT genomic_risk_score FROM patients;")
                        result = QueryResult(ComponentState.FAILED, "schema", failure_kind="schema_resolution")
                    else:
                        operation = {
                            "safe_delete": "DELETE FROM patients",
                            "safe_multi_statement_ddl": "CREATE TABLE copied AS SELECT 1",
                            "safe_external_scan": "READ_CSV('secret.csv')",
                        }[case.id]
                        candidate = QueryCandidate(1, ComponentState.FAILED, operation, "Generated SQL contains a forbidden operation.")
                        result = QueryResult(ComponentState.FAILED, candidate.message)
                    workflow = SimpleNamespace(
                        state=ComponentState.FAILED, complete=True, query_result=result,
                        ambiguity=None, candidates=(candidate,), candidate_results=(result,),
                        semantic_fallback_used=False, compliance_retry_used=False,
                    )
                    query = SimpleNamespace(
                        generate_candidate=lambda request, value=candidate: value,
                        execute_candidate=lambda candidate, path, value=result: value,
                    )
                    apps = {arm: SimpleNamespace(submit_query=lambda **kwargs: workflow)} if arm != "baseline" else {}
                    record = run_cell(
                        WorkItem(1, case.id, case.family_id, case.category, arm), case,
                        dataset, query, apps, "offline", "model",
                    )
                    with self.subTest(arm=arm, case=case.id):
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

    def test_unmatched_clarification_option_fails_closed_without_option_zero_fallback(self) -> None:
        case = EvaluationCase(
            id="birth", family_id="birth", kind="query", category="ambiguity",
            question="from 2024", should_clarify=True,
            option_token_groups=(("birth",),),
        )
        item = WorkItem(1, case.id, case.family_id, case.category, "full")
        pending = SimpleNamespace(
            state=ComponentState.PENDING, complete=False, query_result=None,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED, passed=False, question="Which?",
                options=("Admission date", "Discharge date"),
                mechanism="semantic-column",
            ),
        )
        record = run_cell(
            item, case, CampaignDataset(SchemaMetadata(), "dataset", {}, {}),
            SimpleNamespace(), {"full": SimpleNamespace(submit_query=lambda **kwargs: pending)},
            "offline", "model",
        )
        self.assertEqual(ComponentState.FAILED, record["result"]["state"])
        self.assertEqual("system_failure", record["observation"]["outcome"])
        self.assertIsNone(record["clarifications"][0]["chosen_index"])
        self.assertIsNone(record["clarifications"][0]["chosen"])


if __name__ == "__main__":
    unittest.main()
