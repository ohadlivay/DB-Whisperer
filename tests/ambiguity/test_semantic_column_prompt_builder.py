"""Tests for the semantic-column prompt builder."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity import SemanticColumnPromptBuilder
from db_whisperer.contracts import (
    ColumnMetadata,
    SchemaMetadata,
    SemanticColumnRequest,
    TableSchema,
)


def _schema() -> SchemaMetadata:
    columns = (
        ColumnMetadata("order_date", "DATE", "orders"),
        ColumnMetadata("shipped_date", "DATE", "orders"),
        ColumnMetadata("order_status", "VARCHAR", "orders"),
    )
    return SchemaMetadata(
        database_path="orders.duckdb",
        table_names=("orders",),
        columns=columns,
        tables=(TableSchema("orders", columns, row_count=5),),
    )


def _request(clarifications: tuple[str, ...] = ()) -> SemanticColumnRequest:
    return SemanticColumnRequest(
        user_query="show me the dates for order 5",
        schema=_schema(),
        api_key="key",
        model="provider/model",
        clarifications=clarifications,
    )


class SemanticColumnPromptBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = SemanticColumnPromptBuilder()

    def test_term_prompt_lists_columns_with_data_types(self) -> None:
        prompt = self.builder.build_term_prompt(_request())

        self.assertIn("=== TABLES ===", prompt)
        self.assertIn("order_date (DATE)", prompt)
        self.assertIn("order_status (VARCHAR)", prompt)
        self.assertIn("=== USER QUESTION ===", prompt)
        self.assertIn("show me the dates for order 5", prompt)

    def test_term_prompt_includes_previous_clarifications(self) -> None:
        prompt = self.builder.build_term_prompt(
            _request(clarifications=("Question: which date?\nSelected: order_date",))
        )
        self.assertIn("=== PREVIOUS CLARIFICATIONS ===", prompt)
        self.assertIn("order_date", prompt)

    def test_term_prompt_sanitizes_control_characters(self) -> None:
        columns = (
            ColumnMetadata("good", "DATE", "t"),
            ColumnMetadata("bad\nname", "DATE", "t"),
        )
        schema = SchemaMetadata(
            database_path="t.duckdb",
            table_names=("t",),
            columns=columns,
            tables=(TableSchema("t", columns, row_count=1),),
        )
        request = SemanticColumnRequest(
            user_query="dates",
            schema=schema,
            api_key="key",
            model="provider/model",
        )

        prompt = self.builder.build_term_prompt(request)

        # A newline in a column name must not break the section structure.
        self.assertNotIn("bad\nname", prompt)
        self.assertIn("bad name", prompt)

    def test_clarification_prompt_names_term_and_two_columns(self) -> None:
        prompt = self.builder.build_clarification_prompt(
            _request(),
            "dates",
            (("orders", "order_date"), ("orders", "shipped_date")),
        )

        self.assertIn("=== AMBIGUOUS TERM ===", prompt)
        self.assertIn('"dates"', prompt)
        self.assertIn("INTERPRETATION 1", prompt)
        self.assertIn("orders.order_date", prompt)
        self.assertIn("INTERPRETATION 2", prompt)
        self.assertIn("orders.shipped_date", prompt)


if __name__ == "__main__":
    unittest.main()
