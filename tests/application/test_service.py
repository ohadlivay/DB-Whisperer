"""Tests for application-owned ambiguity orchestration."""

from __future__ import annotations

from pathlib import Path
import sys
from threading import Lock
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.application import ApplicationService
from db_whisperer.contracts import (
    AmbiguityDecision,
    ComponentState,
    QueryCandidate,
    QueryResult,
    Relationship,
    SchemaMetadata,
)


class QuerySpy:
    def __init__(self) -> None:
        self.generated_requests = []
        self.executed_candidates = []

    def generate_candidate(self, request):
        self.generated_requests.append(request)
        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=f"SELECT {request.attempt_number} AS value",
            message="SQL generated.",
        )

    def execute_candidate(self, candidate, database_path):
        self.executed_candidates.append((candidate, database_path))
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql=candidate.sql,
            columns=("value",),
            rows=((candidate.attempt_number,),),
        )


class AmbiguitySpy:
    def __init__(self, *decisions: AmbiguityDecision) -> None:
        self.decisions = list(decisions)
        self.requests = []

    def evaluate(self, request):
        self.requests.append(request)
        return self.decisions.pop(0)


class RaisingAmbiguitySpy:
    def evaluate(self, request):
        raise RuntimeError("judge unavailable")


class InvalidAmbiguitySpy:
    def evaluate(self, request):
        return None


class RecordingEventLogger:
    def __init__(self) -> None:
        self.events = []

    def log_event(
        self,
        event,
        component,
        details,
        request_id=None,
        model=None,
    ) -> None:
        self.events.append((event, component, details, model))


class MixedFailureQuerySpy:
    def generate_candidate(self, request):
        if request.attempt_number == 1:
            return QueryCandidate(
                attempt_number=request.attempt_number,
                state=ComponentState.FAILED,
                message=(
                    "OpenRouter response did not contain JSON "
                    "with an SQL field."
                ),
            )
        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=f"SELECT missing_{request.attempt_number} FROM data",
            message="SQL generated.",
        )

    def execute_candidate(self, candidate, database_path):
        if candidate.attempt_number == 2:
            return QueryResult(
                state=ComponentState.FAILED,
                message="Query execution failed: column not found",
                sql=candidate.sql,
            )
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql=candidate.sql,
            columns=("value",),
            rows=((1,),),
        )


class ConcurrentQuerySpy:
    def __init__(self) -> None:
        self.lock = Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def generate_candidate(self, request):
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(
                self.max_active_calls,
                self.active_calls,
            )
        time.sleep(0.1)
        with self.lock:
            self.active_calls -= 1
        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=f"SELECT {request.attempt_number} AS value",
            message="SQL generated.",
        )

    def execute_candidate(self, candidate, database_path):
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql=candidate.sql,
            columns=("value",),
            rows=((candidate.attempt_number,),),
        )


class JoinPathSpy:
    def __init__(self, decision: AmbiguityDecision) -> None:
        self.decision = decision
        self.requests = []

    def detect(self, request):
        self.requests.append(request)
        return self.decision


class RaisingJoinPathSpy:
    def __init__(self) -> None:
        self.requests = []

    def detect(self, request):
        self.requests.append(request)
        raise RuntimeError("join-path detector unavailable")


RELATIONAL_SCHEMA = SchemaMetadata(
    database_path="database.duckdb",
    table_names=("patients", "labevents"),
    relationships=(
        Relationship(
            child_table="labevents",
            child_column="subject_id",
            parent_table="patients",
            parent_column="subject_id",
        ),
    ),
)


class JoinPathGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event_logger = RecordingEventLogger()

    def _clarify_decision(self) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question="Labs for a specific visit, or all visits?",
            options=("A specific visit", "All visits"),
            reason="Two join paths.",
            mechanism="join-path",
        )

    def test_join_path_clarification_short_circuits_generation(self) -> None:
        querier = QuerySpy()
        ambiguity = AmbiguitySpy()
        join_path = JoinPathSpy(self._clarify_decision())
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.PENDING, result.state)
        self.assertFalse(result.complete)
        self.assertEqual("join-path", result.ambiguity.mechanism)
        self.assertEqual(
            ("A specific visit", "All visits"),
            result.ambiguity.options,
        )
        self.assertIsNone(result.query_result)
        self.assertEqual((), result.candidates)
        # No SQL was generated and the candidate judge never ran.
        self.assertEqual([], querier.generated_requests)
        self.assertEqual([], ambiguity.requests)
        self.assertEqual(1, len(join_path.requests))
        self.assertIn(
            "join_path_detection",
            [event[0] for event in self.event_logger.events],
        )

    def test_join_path_pass_continues_to_candidate_flow(self) -> None:
        querier = QuerySpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Candidates agree.",
            )
        )
        join_path = JoinPathSpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Single path.",
                mechanism="join-path",
            )
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual("SELECT 3 AS value", result.query_result.sql)
        self.assertEqual(1, len(join_path.requests))
        self.assertEqual(3, len(querier.generated_requests))
        self.assertEqual(1, len(ambiguity.requests))

    def test_join_path_gate_runs_on_nonterminal_clarification_round(self) -> None:
        # A second entity pair can still be ambiguous after the first is
        # answered, so the gate must run on non-terminal clarification rounds;
        # it delegates settled-pair exclusion to the detector.
        join_path = JoinPathSpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                mechanism="join-path",
            )
        )
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
        )
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=ambiguity,
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            max_iterations=3,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
            clarifications=("Question: scope?\nSelected answer: all",),
            iteration=2,
        )

        self.assertTrue(result.complete)
        self.assertEqual(1, len(join_path.requests))
        # The clarification passed through to the detector for settled-pair
        # exclusion.
        self.assertEqual(
            ("Question: scope?\nSelected answer: all",),
            join_path.requests[0].clarifications,
        )

    def test_join_path_gate_skipped_without_relationships(self) -> None:
        join_path = JoinPathSpy(self._clarify_decision())
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
            ),
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="show the data",
            schema=SchemaMetadata(database_path="database.duckdb"),
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual([], join_path.requests)

    def test_join_path_gate_can_be_disabled(self) -> None:
        join_path = JoinPathSpy(self._clarify_decision())
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
            ),
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            enable_join_path_detection=False,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual([], join_path.requests)

    def test_join_path_failure_degrades_to_candidate_flow(self) -> None:
        join_path = RaisingJoinPathSpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
        )
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=ambiguity,
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual(1, len(join_path.requests))
        self.assertEqual(1, len(ambiguity.requests))

    def test_join_path_gate_skipped_for_single_table_with_self_fk(self) -> None:
        # A single table carrying a self-referential relationship must isolate
        # the table-count guard: CLAUDE.md calls single-table behavior the most
        # dangerous wrong assumption.
        join_path = JoinPathSpy(self._clarify_decision())
        single_table = SchemaMetadata(
            database_path="database.duckdb",
            table_names=("events",),
            relationships=(
                Relationship(
                    child_table="events",
                    child_column="parent_event_id",
                    parent_table="events",
                    parent_column="event_id",
                ),
            ),
        )
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
            ),
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="show the events",
            schema=single_table,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual([], join_path.requests)

    def test_join_path_gate_skipped_on_terminal_iteration(self) -> None:
        # With max_iterations=1, round 1 is terminal; the gate must not fire and
        # leave a clarification the user could never answer.
        join_path = JoinPathSpy(self._clarify_decision())
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(),
            join_path=join_path,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            max_iterations=1,
        )

        result = application.submit_query(
            prompt="labs for patient 123",
            schema=RELATIONAL_SCHEMA,
            api_key="key",
            model="provider/model",
            iteration=1,
        )

        self.assertTrue(result.complete)
        self.assertEqual([], join_path.requests)
        self.assertEqual("SELECT 2 AS value", result.query_result.sql)


class ApplicationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = SchemaMetadata(database_path="database.duckdb")
        self.event_logger = RecordingEventLogger()

    def test_preview_table_executes_read_only_limit_query(self) -> None:
        querier = QuerySpy()
        application = ApplicationService(
            querier=querier,
            event_logger=self.event_logger,
        )
        schema = SchemaMetadata(
            database_path="database.duckdb",
            table_names=('order"items',),
        )

        result = application.preview_table('order"items', schema)

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        candidate, database_path = querier.executed_candidates[0]
        self.assertEqual(
            'SELECT * FROM "order""items" LIMIT 10;',
            candidate.sql,
        )
        self.assertEqual("database.duckdb", database_path)

    def test_preview_table_rejects_unknown_table(self) -> None:
        querier = QuerySpy()
        application = ApplicationService(
            querier=querier,
            event_logger=self.event_logger,
        )

        result = application.preview_table("unknown", self.schema)

        self.assertEqual(ComponentState.FAILED, result.state)
        self.assertEqual([], querier.executed_candidates)

    def test_pass_returns_last_query_from_current_iteration(self) -> None:
        querier = QuerySpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Candidates agree.",
            )
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            event_logger=self.event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual(1, result.iteration)
        self.assertEqual("SELECT 3 AS value", result.query_result.sql)
        self.assertEqual(((3,),), result.query_result.rows)
        self.assertEqual(3, len(querier.generated_requests))
        self.assertEqual(3, len(ambiguity.requests[0].pairs))

    def test_failed_ambiguity_returns_last_successful_query(self) -> None:
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(
                    state=ComponentState.FAILED,
                    reason=(
                        "Clarification judgment requires exactly two options."
                    ),
                )
            ),
            event_logger=self.event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual("SELECT 3 AS value", result.query_result.sql)
        self.assertEqual(ComponentState.FAILED, result.ambiguity.state)

    def test_malformed_clarification_returns_last_successful_query(
        self,
    ) -> None:
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(
                    state=ComponentState.ACCEPTED,
                    passed=False,
                    question="Which scope?",
                    options=("All records",),
                )
            ),
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual("SELECT 2 AS value", result.query_result.sql)

    def test_ambiguity_exception_returns_last_successful_query(self) -> None:
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=RaisingAmbiguitySpy(),
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual("SELECT 2 AS value", result.query_result.sql)
        self.assertIn("judge unavailable", result.ambiguity.reason)

    def test_invalid_ambiguity_result_returns_last_successful_query(
        self,
    ) -> None:
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=InvalidAmbiguitySpy(),
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual("SELECT 2 AS value", result.query_result.sql)
        self.assertIn("invalid decision", result.ambiguity.reason)

    def test_submit_query_accepts_user_selected_candidate_count(self) -> None:
        querier = QuerySpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Candidates agree.",
            )
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            event_logger=self.event_logger,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            candidate_count=5,
        )

        self.assertTrue(result.complete)
        self.assertEqual(5, len(querier.generated_requests))
        self.assertEqual("SELECT 5 AS value", result.query_result.sql)

    def test_clarification_is_added_to_every_candidate_next_round(self) -> None:
        querier = QuerySpy()
        question = "Should null values be ignored?"
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question=question,
                options=("Ignore null values", "Include null values"),
                reason="Null handling differs.",
            ),
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Candidates agree.",
            ),
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        first = application.submit_query(
            prompt="Summarize the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )
        clarification = (
            f"Question: {question}\n"
            "Selected answer: Ignore null values"
        )
        second = application.submit_query(
            prompt="Summarize the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=(clarification,),
            iteration=2,
        )

        self.assertEqual(ComponentState.PENDING, first.state)
        self.assertFalse(first.complete)
        self.assertEqual(question, first.ambiguity.question)
        self.assertTrue(second.complete)
        self.assertEqual(
            [(clarification,), (clarification,)],
            [
                request.clarifications
                for request in sorted(
                    querier.generated_requests[2:],
                    key=lambda request: request.attempt_number,
                )
            ],
        )
        self.assertEqual([3, 4], [
            request.attempt_number
            for request in sorted(
                querier.generated_requests[2:],
                key=lambda request: request.attempt_number,
            )
        ])
        self.assertEqual((), ambiguity.requests[0].clarifications)
        self.assertEqual(
            (clarification,),
            ambiguity.requests[1].clarifications,
        )

    def test_third_iteration_returns_last_query_without_another_judgment(
        self,
    ) -> None:
        querier = QuerySpy()
        ambiguity = AmbiguitySpy()
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            max_iterations=3,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            iteration=3,
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertTrue(result.complete)
        self.assertEqual("SELECT 6 AS value", result.query_result.sql)
        self.assertEqual([], ambiguity.requests)

    def test_insufficient_candidates_reports_and_logs_failure_causes(
        self,
    ) -> None:
        event_logger = RecordingEventLogger()
        application = ApplicationService(
            querier=MixedFailureQuerySpy(),
            ambiguity=AmbiguitySpy(),
            event_logger=event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="Show the data",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.FAILED, result.state)
        self.assertIn(
            "Only 1 of 3 candidate queries executed successfully",
            result.message,
        )
        self.assertIn("1 generation failure", result.message)
        self.assertIn("1 execution failure", result.message)
        self.assertIn("did not contain JSON", result.message)
        self.assertIn("column not found", result.message)
        self.assertIn("logs/prompts.jsonl", result.message)
        self.assertEqual(
            [
                "candidate_generation_failed",
                "candidate_generated",
                "candidate_execution_failed",
                "candidate_generated",
                "candidate_executed",
            ],
            [event[0] for event in event_logger.events],
        )
        failed_execution = event_logger.events[2][2]
        self.assertEqual(
            "SELECT missing_2 FROM data",
            failed_execution["sql"],
        )

    def test_candidate_generation_runs_in_parallel_and_keeps_order(
        self,
    ) -> None:
        querier = ConcurrentQuerySpy()
        application = ApplicationService(
            querier=querier,
            ambiguity=AmbiguitySpy(
                AmbiguityDecision(
                    state=ComponentState.ACCEPTED,
                    passed=True,
                )
            ),
            event_logger=self.event_logger,
            candidates_per_iteration=4,
            max_parallel_candidates=4,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
        )

        self.assertGreaterEqual(querier.max_active_calls, 2)
        self.assertEqual(
            [1, 2, 3, 4],
            [
                candidate.attempt_number
                for candidate in result.candidates
            ],
        )
        self.assertEqual("SELECT 4 AS value", result.query_result.sql)


if __name__ == "__main__":
    unittest.main()
