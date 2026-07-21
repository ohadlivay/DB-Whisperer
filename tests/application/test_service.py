"""Tests for application-owned ambiguity orchestration."""

from __future__ import annotations

from pathlib import Path
import sys
from threading import Lock
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity import SemanticColumnAmbiguityService
from db_whisperer.application import ApplicationService
from db_whisperer.contracts import (
    AmbiguityDecision,
    ColumnMetadata,
    ComponentState,
    QueryCandidate,
    QueryResult,
    Relationship,
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
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


class ComplianceRetryQuerySpy(QuerySpy):
    def generate_candidate(self, request):
        self.generated_requests.append(request)
        column = "admittime" if request.compliance_retry else "dob"
        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=f'SELECT "{column}" FROM "patients"',
            message="SQL generated.",
        )

    def execute_candidate(self, candidate, database_path):
        self.executed_candidates.append((candidate, database_path))
        column = "admittime" if "admittime" in candidate.sql else "dob"
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql=candidate.sql,
            columns=(column,),
            rows=(("2024-01-01",),),
        )


class SupportedComplianceQuerySpy(QuerySpy):
    def generate_candidate(self, request):
        self.generated_requests.append(request)
        column = "admittime" if request.attempt_number % 3 != 0 else "dob"
        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=f'SELECT "{column}" FROM "patients"',
            message="SQL generated.",
        )

    def execute_candidate(self, candidate, database_path):
        column = "admittime" if "admittime" in candidate.sql else "dob"
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql=candidate.sql,
            columns=(column,),
            rows=(("2024-01-01",),),
        )


class SemanticAnalysisSpy:
    def __init__(self, analysis) -> None:
        self.analysis = analysis
        self.requests = []

    def analyze(self, request):
        self.requests.append(request)
        return self.analysis

    @staticmethod
    def fallback_decision(analysis, pairs=()):
        term = analysis.terms[0]
        first, second = term.columns[:2]
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=(
                f'Which {term.term} do you mean? '
                f'(clarifying which column: "{first.qualified_name}" or '
                f'"{second.qualified_name}")'
            ),
            options=(first.qualified_name, second.qualified_name),
            reason="Deterministic semantic fallback.",
            mechanism="semantic-column",
            evidence_columns=(first.qualified_name, second.qualified_name),
        )


class RaisingSemanticAnalysisSpy(SemanticAnalysisSpy):
    def __init__(self) -> None:
        super().__init__(None)

    def analyze(self, request):
        self.requests.append(request)
        raise RuntimeError("semantic analysis unavailable")


def _semantic_analysis() -> SemanticColumnAnalysis:
    return SemanticColumnAnalysis(
        state=ComponentState.ACCEPTED,
        terms=(
            SemanticAmbiguityTerm(
                term="date",
                bucket="temporal",
                columns=(
                    SemanticColumnCandidate(
                        table="orders",
                        column="order_date",
                        data_type="DATE",
                        bucket="temporal",
                    ),
                    SemanticColumnCandidate(
                        table="orders",
                        column="required_date",
                        data_type="DATE",
                        bucket="temporal",
                    ),
                ),
            ),
        ),
        reason="One vague date term.",
    )


COLUMN_SCHEMA = SchemaMetadata(
    database_path="database.duckdb",
    table_names=("orders",),
    columns=(
        ColumnMetadata("order_date", "DATE", "orders"),
        ColumnMetadata("required_date", "DATE", "orders"),
    ),
)


class HybridAmbiguityFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.event_logger = RecordingEventLogger()

    def test_semantic_analysis_runs_before_candidates_and_is_deferred(self) -> None:
        querier = QuerySpy()
        semantic = SemanticAnalysisSpy(_semantic_analysis())
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question="Which candidate interpretation?",
                options=("Candidate A", "Candidate B"),
                reason="Candidate difference is primary.",
                mechanism="candidate-comparison",
            )
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            semantic_column=semantic,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="show the date",
            schema=COLUMN_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.PENDING, result.state)
        self.assertEqual("candidate-comparison", result.ambiguity.mechanism)
        self.assertEqual(2, len(querier.generated_requests))
        self.assertEqual(1, len(semantic.requests))
        self.assertIs(ambiguity.requests[0].semantic_analysis, semantic.analysis)
        self.assertIs(ambiguity.requests[0].schema, COLUMN_SCHEMA)
        decision_event = next(
            event for event in self.event_logger.events
            if event[0] == "ambiguity_decision"
        )
        self.assertEqual(
            [
                {"alternative_id": "alternative_1", "support": 1},
                {"alternative_id": "alternative_2", "support": 1},
            ],
            decision_event[2]["candidate_support"],
        )

    def test_failed_unified_judge_uses_semantic_fallback(self) -> None:
        semantic = SemanticAnalysisSpy(_semantic_analysis())
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=RaisingAmbiguitySpy(),
            semantic_column=semantic,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="show the date",
            schema=COLUMN_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertEqual(ComponentState.PENDING, result.state)
        self.assertEqual("semantic-column", result.ambiguity.mechanism)
        self.assertIn("orders.order_date", result.ambiguity.question)
        decision_events = [
            event for event in self.event_logger.events
            if event[0] == "ambiguity_decision"
        ]
        self.assertEqual(1, len(decision_events))
        self.assertTrue(decision_events[0][2]["fallback_used"])
        self.assertEqual(
            ["orders.order_date", "orders.required_date"],
            decision_events[0][2]["evidence_columns"],
        )

    def test_analysis_failure_degrades_to_candidate_judge(self) -> None:
        semantic = RaisingSemanticAnalysisSpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
        )
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=ambiguity,
            semantic_column=semantic,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="show the date",
            schema=COLUMN_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual(ComponentState.FAILED, ambiguity.requests[0].semantic_analysis.state)

    def test_analysis_can_be_disabled_for_evaluation(self) -> None:
        semantic = SemanticAnalysisSpy(_semantic_analysis())
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(state=ComponentState.ACCEPTED, passed=True)
        )
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=ambiguity,
            semantic_column=semantic,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            enable_semantic_column_detection=False,
        )

        result = application.submit_query(
            prompt="show the date",
            schema=COLUMN_SCHEMA,
            api_key="key",
            model="provider/model",
        )

        self.assertTrue(result.complete)
        self.assertEqual([], semantic.requests)
        self.assertIsNone(ambiguity.requests[0].semantic_analysis)

    def test_terminal_iteration_skips_analysis_and_judging(self) -> None:
        semantic = SemanticAnalysisSpy(_semantic_analysis())
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=AmbiguitySpy(),
            semantic_column=semantic,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            max_iterations=1,
        )

        result = application.submit_query(
            prompt="show the date",
            schema=COLUMN_SCHEMA,
            api_key="key",
            model="provider/model",
            iteration=1,
        )

        self.assertTrue(result.complete)
        self.assertEqual([], semantic.requests)


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
                compliance_passed=True,
                compliant_alternatives=(
                    "alternative_1",
                    "alternative_2",
                ),
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

    def test_clarified_result_uses_highest_support_compliant_alternative(
        self,
    ) -> None:
        application = ApplicationService(
            querier=SupportedComplianceQuerySpy(),
            ambiguity=AmbiguitySpy(AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Both alternatives apply the clarification.",
                compliance_passed=True,
                compliant_alternatives=("alternative_1", "alternative_2"),
            )),
            event_logger=self.event_logger,
            candidates_per_iteration=3,
        )

        result = application.submit_query(
            prompt="Show patients from 2024",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=(
                "Question: Born or admitted?\nSelected answer: Admitted",
            ),
            iteration=2,
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertIn("admittime", result.query_result.sql)
        selected = next(
            event for event in self.event_logger.events
            if event[0] == "clarification_compliant_result_selected"
        )
        self.assertEqual(2, selected[2]["support"])

    def test_noncompliant_batch_retries_once_and_returns_verified_sql(
        self,
    ) -> None:
        querier = ComplianceRetryQuerySpy()
        ambiguity = AmbiguitySpy(
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                reason="Candidates still use date of birth.",
                compliance_passed=False,
                rejected_alternatives=(("alternative_1", "Uses dob."),),
            ),
            AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason="Retry applies admission time.",
                compliance_passed=True,
                compliant_alternatives=("alternative_1",),
            ),
        )
        application = ApplicationService(
            querier=querier,
            ambiguity=ambiguity,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Show patients from 2024",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=(
                "Question: Born or admitted?\nSelected answer: Admitted",
            ),
            iteration=2,
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertIn("admittime", result.query_result.sql)
        self.assertEqual(4, len(result.candidates))
        self.assertEqual(
            [False, False, True, True],
            [request.compliance_retry for request in querier.generated_requests],
        )
        self.assertEqual(2, len(ambiguity.requests))

    def test_second_noncompliant_batch_fails_without_result(self) -> None:
        noncompliant = AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            reason="No candidate applies admission time.",
            compliance_passed=False,
            rejected_alternatives=(("alternative_1", "Uses dob."),),
        )
        application = ApplicationService(
            querier=ComplianceRetryQuerySpy(),
            ambiguity=AmbiguitySpy(noncompliant, noncompliant),
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Show patients from 2024",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=(
                "Question: Born or admitted?\nSelected answer: Admitted",
            ),
            iteration=2,
        )

        self.assertEqual(ComponentState.FAILED, result.state)
        self.assertIsNone(result.query_result)
        self.assertIn("could not verify", result.message)

    def test_clarified_judge_failure_fails_closed_without_retry(self) -> None:
        querier = QuerySpy()
        application = ApplicationService(
            querier=querier,
            ambiguity=RaisingAmbiguitySpy(),
            event_logger=self.event_logger,
            candidates_per_iteration=2,
        )

        result = application.submit_query(
            prompt="Return the values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=("Question: Which?\nSelected answer: First",),
            iteration=2,
        )

        self.assertEqual(ComponentState.FAILED, result.state)
        self.assertIsNone(result.query_result)
        self.assertEqual(2, len(querier.generated_requests))

    def test_final_iteration_still_checks_clarification_compliance(self) -> None:
        ambiguity = AmbiguitySpy(AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=True,
            reason="The settled answer is applied.",
            compliance_passed=True,
            compliant_alternatives=("alternative_1", "alternative_2"),
        ))
        application = ApplicationService(
            querier=QuerySpy(),
            ambiguity=ambiguity,
            event_logger=self.event_logger,
            candidates_per_iteration=2,
            max_iterations=3,
        )

        result = application.submit_query(
            prompt="Return values",
            schema=self.schema,
            api_key="key",
            model="provider/model",
            clarifications=("Question: Which?\nSelected answer: First",),
            iteration=3,
        )

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertEqual(1, len(ambiguity.requests))

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
