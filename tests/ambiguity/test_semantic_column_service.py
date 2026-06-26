"""Tests for semantic-type column ambiguity detection (Mechanism 2)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity import SemanticColumnAmbiguityService
from db_whisperer.ambiguity.openrouter_client import AmbiguityJudgeError
from db_whisperer.ambiguity.semantic_column_service import semantic_bucket
from db_whisperer.contracts import (
    ColumnMetadata,
    ComponentState,
    SchemaMetadata,
    SemanticColumnRequest,
    TableSchema,
)


def _column(name: str, data_type: str, table: str) -> ColumnMetadata:
    return ColumnMetadata(name=name, data_type=data_type, table_name=table)


def _schema(table: str, columns: list[tuple[str, str]]) -> SchemaMetadata:
    metadata = tuple(_column(name, dtype, table) for name, dtype in columns)
    return SchemaMetadata(
        database_path=f"{table}.duckdb",
        table_names=(table,),
        columns=metadata,
        tables=(
            TableSchema(
                table_name=table,
                columns=metadata,
                row_count=10,
            ),
        ),
    )


# An orders table with three interchangeable DATE columns -- the canonical
# "which date do you mean?" ambiguity, plus columns of other types.
ORDERS_SCHEMA = _schema(
    "orders",
    [
        ("order_id", "INTEGER"),
        ("order_date", "DATE"),
        ("required_date", "DATE"),
        ("shipped_date", "DATE"),
        ("order_status", "VARCHAR"),
        ("customer_id", "INTEGER"),
    ],
)

# Every column is a different semantic bucket -> no same-type pair exists.
ALL_DISTINCT_SCHEMA = _schema(
    "mixed",
    [
        ("created", "DATE"),
        ("amount", "INTEGER"),
        ("label", "VARCHAR"),
        ("active", "BOOLEAN"),
    ],
)


class FakeColumnClient:
    """Return queued responses (dicts) or raise queued exceptions."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def evaluate(self, prompt: str, api_key: str, model: str) -> dict:
        self.prompts.append(prompt)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def _request(
    schema: SchemaMetadata,
    query: str = "show me the dates for order 5",
    clarifications: tuple[str, ...] = (),
) -> SemanticColumnRequest:
    return SemanticColumnRequest(
        user_query=query,
        schema=schema,
        api_key="key",
        model="provider/model",
        clarifications=clarifications,
    )


def _dates_term(*columns: str) -> dict:
    return {
        "terms": [
            {
                "term": "dates",
                "columns": [
                    {"table": "orders", "column": column}
                    for column in columns
                ],
            }
        ]
    }


class SemanticBucketTest(unittest.TestCase):
    def test_buckets(self) -> None:
        self.assertEqual(semantic_bucket("DATE"), "temporal")
        self.assertEqual(semantic_bucket("TIMESTAMP WITH TIME ZONE"), "temporal")
        self.assertEqual(semantic_bucket("BIGINT"), "numeric")
        self.assertEqual(semantic_bucket("DECIMAL(18,2)"), "numeric")
        self.assertEqual(semantic_bucket("BOOLEAN"), "boolean")
        self.assertEqual(semantic_bucket("VARCHAR"), "textual")


class SemanticColumnServiceTest(unittest.TestCase):
    def test_flags_term_with_two_options(self) -> None:
        client = FakeColumnClient(
            _dates_term("order_date", "required_date", "shipped_date"),
            {
                "question": "Which kind of date do you mean?",
                "options": ["When it was placed", "When it must ship"],
                "reason": "Several dates.",
            },
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.passed)
        self.assertEqual("semantic-column", decision.mechanism)
        self.assertEqual(
            ("When it was placed", "When it must ship"), decision.options
        )
        # The two presented columns (sorted top two) are named so a later round
        # can recognise the term as settled.
        self.assertIn("order_date", decision.question)
        self.assertIn("required_date", decision.question)
        self.assertEqual(2, len(client.prompts))

    def test_precheck_skips_llm_when_no_same_bucket(self) -> None:
        client = FakeColumnClient()  # would raise if called

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ALL_DISTINCT_SCHEMA, "show me the created amount label")
        )

        self.assertTrue(decision.passed)
        self.assertEqual([], client.prompts)
        self.assertIn("No two columns share a semantic type", decision.reason)

    def test_passes_when_term_maps_to_one_column(self) -> None:
        client = FakeColumnClient(_dates_term("order_date"))

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertTrue(decision.passed)
        self.assertEqual(1, len(client.prompts))
        self.assertIn("more than one same-type column", decision.reason)

    def test_columns_of_different_types_are_not_flagged(self) -> None:
        # A term mapping to a DATE and an INTEGER must not be flagged: the
        # deterministic bucket guard requires two columns of the same kind.
        client = FakeColumnClient(
            {
                "terms": [
                    {
                        "term": "value",
                        "columns": [
                            {"table": "orders", "column": "order_date"},
                            {"table": "orders", "column": "customer_id"},
                        ],
                    }
                ]
            }
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA, "show me the value")
        )

        self.assertTrue(decision.passed)
        self.assertEqual(1, len(client.prompts))

    def test_drops_unknown_columns_and_reports_them(self) -> None:
        client = FakeColumnClient(
            {
                "terms": [
                    {
                        "term": "dates",
                        "columns": [
                            {"table": "orders", "column": "order_date"},
                            {"table": "orders", "column": "required_date"},
                            {"table": "orders", "column": "ghost_date"},
                        ],
                    }
                ]
            },
            {
                "question": "Which date?",
                "options": ["Placed", "Required"],
                "reason": "Two dates.",
            },
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        # Still ambiguous on the two known dates; the hallucinated column is
        # reported rather than silently used.
        self.assertFalse(decision.passed)
        self.assertIn("orders.ghost_date", decision.reason)

    def test_deterministic_fallback_when_clarify_fails(self) -> None:
        client = FakeColumnClient(
            _dates_term("order_date", "required_date", "shipped_date"),
            AmbiguityJudgeError("clarify model down"),
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertFalse(decision.passed)
        # Sorted top two columns become the deterministic options.
        self.assertEqual(
            ('"order_date" (from orders)', '"required_date" (from orders)'),
            decision.options,
        )
        self.assertIn("deterministic", decision.reason)
        self.assertIn("Presented the two most likely of 3 columns", decision.reason)

    def test_chooses_most_ambiguous_term(self) -> None:
        client = FakeColumnClient(
            {
                "terms": [
                    {
                        "term": "id",
                        "columns": [
                            {"table": "orders", "column": "order_id"},
                            {"table": "orders", "column": "customer_id"},
                        ],
                    },
                    {
                        "term": "dates",
                        "columns": [
                            {"table": "orders", "column": "order_date"},
                            {"table": "orders", "column": "required_date"},
                            {"table": "orders", "column": "shipped_date"},
                        ],
                    },
                ]
            },
            AmbiguityJudgeError("force fallback"),
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA, "show me the id and dates")
        )

        self.assertFalse(decision.passed)
        # The 3-column "dates" term outranks the 2-column "id" term.
        self.assertIn('"dates" maps to 3 temporal columns', decision.reason)
        self.assertIn("1 other ambiguous term(s)", decision.reason)

    def test_excludes_settled_term_on_later_round(self) -> None:
        # The recorded clarification carries the qualified refs the question
        # appends, which is what settles the term on the next round.
        answered = (
            "Question: Which date? (clarifying which column: "
            '"orders.order_date" or "orders.required_date")\n'
            "Selected answer: orders.order_date"
        )
        client = FakeColumnClient(
            _dates_term("order_date", "required_date", "shipped_date")
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA, clarifications=(answered,))
        )

        self.assertTrue(decision.passed)
        self.assertIn("already been clarified", decision.reason)
        # Only the extraction call ran; nothing left to clarify.
        self.assertEqual(1, len(client.prompts))

    def test_same_named_columns_across_tables_do_not_false_settle(self) -> None:
        # customers.name and stores.name share a bare column name; an unrelated
        # prior clarification that merely mentions "name" once must NOT settle
        # the term. (Matching bare names would falsely count it twice.)
        schema = SchemaMetadata(
            database_path="shop.duckdb",
            table_names=("customers", "stores"),
            columns=(
                _column("name", "VARCHAR", "customers"),
                _column("name", "VARCHAR", "stores"),
                _column("customer_id", "INTEGER", "customers"),
            ),
        )
        prior = "Question: Whose name?\nSelected answer: the customer name"
        client = FakeColumnClient(
            {
                "terms": [
                    {
                        "term": "name",
                        "columns": [
                            {"table": "customers", "column": "name"},
                            {"table": "stores", "column": "name"},
                        ],
                    }
                ]
            },
            {
                "question": "Which name?",
                "options": ["Customer name", "Store name"],
                "reason": "Two name columns.",
            },
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            SemanticColumnRequest(
                user_query="show me the name",
                schema=schema,
                api_key="key",
                model="provider/model",
                clarifications=(prior,),
            )
        )

        self.assertFalse(decision.passed)
        self.assertEqual("semantic-column", decision.mechanism)
        # The asked question carries qualified refs so the NEXT round can settle.
        self.assertIn("customers.name", decision.question)
        self.assertIn("stores.name", decision.question)

    def test_falls_back_when_clarify_returns_one_option(self) -> None:
        client = FakeColumnClient(
            _dates_term("order_date", "required_date"),
            {"question": "Which?", "options": ["only one"]},
        )

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertFalse(decision.passed)
        self.assertEqual(2, len(decision.options))
        self.assertNotEqual(decision.options[0], decision.options[1])

    def test_term_extraction_failure_is_reported(self) -> None:
        client = FakeColumnClient(AmbiguityJudgeError("boom"))

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("Term extraction failed", decision.reason)

    def test_malformed_terms_response_is_reported(self) -> None:
        client = FakeColumnClient({"not_terms": []})

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA)
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("no usable terms", decision.reason)

    def test_validation_rejects_empty_query_without_llm(self) -> None:
        client = FakeColumnClient()

        decision = SemanticColumnAmbiguityService(client=client).detect(
            _request(ORDERS_SCHEMA, "   ")
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("User query is required", decision.reason)
        self.assertEqual([], client.prompts)


if __name__ == "__main__":
    unittest.main()
