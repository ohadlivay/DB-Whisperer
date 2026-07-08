"""Tests for the SchemaLinker service."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import (
    ColumnMetadata,
    Relationship,
    SchemaMetadata,
    TableSchema,
)
from db_whisperer.querier.schema_linker import SchemaLinker


class MockOpenRouterClient:
    def __init__(self, response_dict: dict) -> None:
        self.response_dict = response_dict
        self.last_prompt = None

    def generate_json(
        self,
        prompt: str,
        api_key: str,
        model: str,
        metadata: dict | None = None,
    ) -> dict:
        self.last_prompt = prompt
        return self.response_dict


class FailingMockOpenRouterClient:
    def generate_json(
        self,
        prompt: str,
        api_key: str,
        model: str,
        metadata: dict | None = None,
    ) -> dict:
        raise RuntimeError("OpenRouter API is offline")


class SchemaLinkerTest(unittest.TestCase):
    def setUp(self) -> None:
        # Create a mock schema metadata where patients and labevents are only
        # connected via admissions.
        self.schema = SchemaMetadata(
            table_names=("patients", "admissions", "labevents"),
            tables=(
                TableSchema(
                    table_name="patients",
                    columns=(
                        ColumnMetadata(name="subject_id", data_type="INTEGER"),
                        ColumnMetadata(name="gender", data_type="VARCHAR"),
                    ),
                    row_count=100,
                ),
                TableSchema(
                    table_name="admissions",
                    columns=(
                        ColumnMetadata(name="hadm_id", data_type="INTEGER"),
                        ColumnMetadata(name="subject_id", data_type="INTEGER"),
                        ColumnMetadata(name="admission_type", data_type="VARCHAR"),
                    ),
                    row_count=200,
                ),
                TableSchema(
                    table_name="labevents",
                    columns=(
                        ColumnMetadata(name="itemid", data_type="INTEGER"),
                        ColumnMetadata(name="hadm_id", data_type="INTEGER"),
                        ColumnMetadata(name="value", data_type="VARCHAR"),  # generic
                    ),
                    row_count=500,
                ),
            ),
            relationships=(
                Relationship(
                    child_table="admissions",
                    child_column="subject_id",
                    parent_table="patients",
                    parent_column="subject_id",
                ),
                Relationship(
                    child_table="labevents",
                    child_column="hadm_id",
                    parent_table="admissions",
                    parent_column="hadm_id",
                ),
            ),
        )
        self.linker = SchemaLinker()

    def test_match_tokens_exact_table_name(self) -> None:
        matched = self.linker.match_tokens(
            "Show all information about patients", self.schema
        )
        self.assertEqual({"patients"}, matched)

    def test_match_tokens_non_generic_column_name(self) -> None:
        # 'gender' is unique/non-generic, maps to 'patients'
        matched = self.linker.match_tokens(
            "What genders do we have?", self.schema
        )
        self.assertEqual({"patients"}, matched)

    def test_match_tokens_generic_column_ignored(self) -> None:
        # 'value' is a generic column name, should be ignored
        matched_only_value = self.linker.match_tokens(
            "Show the value column", self.schema
        )
        self.assertEqual(set(), matched_only_value)

    def test_match_tokens_multiple_tables(self) -> None:
        matched = self.linker.match_tokens(
            "What were the admission_type of patients?", self.schema
        )
        self.assertEqual({"patients", "admissions"}, matched)

    def test_link_schema_token_only_completes_graph_path(self) -> None:
        # Asking for gender (patients) and itemid (labevents)
        # These are disconnected directly, so schema graph must add admissions (bridge table)
        allowed = self.linker.link_schema(
            user_prompt="genders and itemid info",
            schema=self.schema,
            api_key="",
            model="",
        )
        # Should include patients, labevents, and the intermediate admissions
        self.assertEqual({"patients", "admissions", "labevents"}, allowed)

    def test_link_schema_llm_success_and_graph_completes(self) -> None:
        # Mock client returning patients and labevents
        client = MockOpenRouterClient({"tables": ["patients", "labevents"]})
        linker = SchemaLinker(client=client)

        allowed = linker.link_schema(
            user_prompt="some clinical query",
            schema=self.schema,
            api_key="secret-key",
            model="some-model",
        )

        self.assertEqual({"patients", "admissions", "labevents"}, allowed)
        self.assertIn("=== USER QUESTION ===", client.last_prompt)

    def test_link_schema_llm_failure_falls_back_to_tokens(self) -> None:
        # Failing client should trigger fallback to match_tokens
        client = FailingMockOpenRouterClient()
        linker = SchemaLinker(client=client)

        # Prompt matches 'gender' (patients)
        allowed = linker.link_schema(
            user_prompt="patients genders list",
            schema=self.schema,
            api_key="secret-key",
            model="some-model",
        )

        self.assertEqual({"patients"}, allowed)

    def test_link_schema_empty_failsafe_returns_all(self) -> None:
        # When neither matches anything, it should return all tables
        allowed = self.linker.link_schema(
            user_prompt="completely unrelated text query",
            schema=self.schema,
            api_key="",
            model="",
        )
        self.assertEqual({"patients", "admissions", "labevents"}, set(allowed))


if __name__ == "__main__":
    unittest.main()
