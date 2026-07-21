"""Tests for pre-SQL semantic-column analysis."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.semantic_column_service import (
    SemanticColumnAmbiguityService,
    semantic_bucket,
)
from db_whisperer.contracts import (
    ColumnMetadata,
    ComponentState,
    ExecutedQueryPair,
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
    SemanticColumnRequest,
)


class FakeClient:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.prompts = []

    def evaluate(self, prompt, api_key, model):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.response


def schema() -> SchemaMetadata:
    return SchemaMetadata(
        table_names=("orders", "customers"),
        columns=(
            ColumnMetadata("order_date", "DATE", "orders"),
            ColumnMetadata("required_date", "DATE", "orders"),
            ColumnMetadata("name", "VARCHAR", "customers"),
        ),
    )


def request(clarifications=()) -> SemanticColumnRequest:
    return SemanticColumnRequest(
        user_query="show the important dates",
        schema=schema(),
        api_key="key",
        model="provider/model",
        clarifications=clarifications,
    )


class SemanticColumnAnalysisTest(unittest.TestCase):
    def test_bucket_mapping(self) -> None:
        self.assertEqual("temporal", semantic_bucket("TIMESTAMP WITH TIME ZONE"))
        self.assertEqual("numeric", semantic_bucket("DECIMAL(10,2)"))
        self.assertEqual("boolean", semantic_bucket("BOOLEAN"))
        self.assertEqual("textual", semantic_bucket("VARCHAR"))

    def test_retains_valid_same_type_findings_and_drops_unknowns(self) -> None:
        client = FakeClient({
            "terms": [{
                "term": "dates",
                "columns": [
                    {"table": "orders", "column": "required_date"},
                    {"table": "missing", "column": "date"},
                    {"table": "orders", "column": "order_date"},
                    {"table": "customers", "column": "name"},
                ],
            }]
        })

        analysis = SemanticColumnAmbiguityService(client=client).analyze(request())

        self.assertTrue(analysis.ambiguous)
        self.assertEqual("dates", analysis.terms[0].term)
        self.assertEqual("temporal", analysis.terms[0].bucket)
        self.assertEqual(
            ("orders.order_date", "orders.required_date"),
            tuple(column.qualified_name for column in analysis.terms[0].columns),
        )
        self.assertIn("missing.date", analysis.reason)

    def test_excludes_term_settled_by_qualified_column_bookkeeping(self) -> None:
        client = FakeClient({
            "terms": [{
                "term": "dates",
                "columns": [
                    {"table": "orders", "column": "order_date"},
                    {"table": "orders", "column": "required_date"},
                ],
            }]
        })
        clarification = (
            'Question: Which date? (clarifying which column: '
            '"orders.order_date" or "orders.required_date")\n'
            "Selected answer: order date"
        )

        analysis = SemanticColumnAmbiguityService(client=client).analyze(
            request((clarification,))
        )

        self.assertFalse(analysis.ambiguous)
        self.assertIn("already been clarified", analysis.reason)

    def test_malformed_response_is_failure(self) -> None:
        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"wrong": []})
        ).analyze(request())
        self.assertEqual(ComponentState.FAILED, analysis.state)

    def test_deterministic_fallback_uses_strongest_term(self) -> None:
        client = FakeClient({
            "terms": [{
                "term": "dates",
                "columns": [
                    {"table": "orders", "column": "order_date"},
                    {"table": "orders", "column": "required_date"},
                ],
            }]
        })
        service = SemanticColumnAmbiguityService(client=client)
        decision = service.fallback_decision(service.analyze(request()))
        self.assertEqual("semantic-column", decision.mechanism)
        self.assertEqual(2, len(decision.options))
        self.assertIn("orders.order_date", decision.question)

    def test_fallback_anchors_to_column_used_by_sql_candidates(self) -> None:
        candidates = tuple(
            SemanticColumnCandidate(table, column, "TIMESTAMP", "temporal")
            for table, column in (
                ("admissions", "admittime"),
                ("admissions", "dischtime"),
                ("patients", "dob"),
            )
        )
        analysis = SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            terms=(
                SemanticAmbiguityTerm(
                    term="from 2024",
                    bucket="temporal",
                    columns=candidates,
                ),
            ),
        )
        pairs = (
            ExecutedQueryPair(
                candidate_id="candidate_1",
                sql='SELECT * FROM "patients" WHERE YEAR("dob") = 2024',
                columns=("dob",),
                rows=(),
            ),
            ExecutedQueryPair(
                candidate_id="candidate_2",
                sql='SELECT "dob" FROM "patients"',
                columns=("dob",),
                rows=(),
            ),
        )

        decision = SemanticColumnAmbiguityService.fallback_decision(
            analysis,
            pairs=pairs,
        )

        self.assertEqual(
            ("patients.dob", "admissions.admittime"),
            decision.evidence_columns,
        )


if __name__ == "__main__":
    unittest.main()
