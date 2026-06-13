"""Tests for schema-aware prompt construction."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import CsvUpload
from db_whisperer.etler import ETLService
from db_whisperer.querier.prompt_builder import (
    PromptBuilder,
    STATIC_INSTRUCTIONS,
)


class PromptBuilderTest(unittest.TestCase):
    def test_prompt_contains_all_required_database_context(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest(
                [
                    CsvUpload(
                        "data.csv",
                        (
                            b"item name,total amount,active\n"
                            b"row_1,10,true\n"
                            b"row_2,20,false\n"
                            b"row_3,30,true\n"
                            b"row_4,40,false\n"
                            b"row_5,50,true\n"
                            b"row_6,60,false\n"
                        ),
                    )
                ]
            )

            prompt = PromptBuilder().build(
                "What is the total amount?",
                ingestion.schema,
            )

            self.assertTrue(prompt.startswith(STATIC_INSTRUCTIONS))
            self.assertIn("DATABASE SCHEMA", prompt)
            self.assertIn('CREATE TABLE "data" (', prompt)
            self.assertIn('    "item name" VARCHAR,', prompt)
            self.assertIn('    "total amount" BIGINT,', prompt)
            self.assertIn("TOP 5 ROWS", prompt)
            self.assertIn("row_1", prompt)
            self.assertIn("row_5", prompt)
            self.assertNotIn("row_6", prompt)
            self.assertIn("SHAPE", prompt)
            self.assertIn("6 rows x 3 columns", prompt)
            self.assertIn("COLUMN STATISTICS", prompt)
            self.assertIn('"null_count": 0', prompt)
            self.assertIn('"distinct_count": 6', prompt)
            self.assertIn('"mean": 35.0', prompt)
            self.assertIn(
                'VALID IDENTIFIERS\n'
                'Table: "data"\n'
                '- "item name"\n'
                '- "total amount"\n'
                '- "active"',
                prompt,
            )
            self.assertIn("USER REQUEST\nWhat is the total amount?", prompt)
            section_positions = [
                prompt.index(STATIC_INSTRUCTIONS),
                prompt.index("DATABASE SCHEMA"),
                prompt.index("TOP 5 ROWS"),
                prompt.index("SHAPE"),
                prompt.index("COLUMN STATISTICS"),
                prompt.index("\n\nVALID IDENTIFIERS\n"),
                prompt.index("USER REQUEST"),
            ]
            self.assertEqual(sorted(section_positions), section_positions)

    def test_identifier_rules_are_generic_and_schema_derived(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest(
                [
                    CsvUpload(
                        "generic records.csv",
                        b"Field With Spaces,field-with-symbol\nA,1\n",
                    )
                ]
            )

            prompt = PromptBuilder().build(
                "Return the records",
                ingestion.schema,
            )

            self.assertIn(
                'CREATE TABLE "generic_records" (',
                prompt,
            )
            self.assertIn('"Field With Spaces" VARCHAR', prompt)
            self.assertIn('"field-with-symbol" BIGINT', prompt)
            self.assertIn(
                "Never replace spaces or punctuation with underscores",
                prompt,
            )
            self.assertIn(
                "Terminate the SQL statement with a semicolon",
                prompt,
            )
            self.assertIn(
                "verify that every SQL identifier quote",
                prompt,
            )
            self.assertNotIn("vehicle make", prompt)
            self.assertNotIn("crash severity", prompt)

    def test_clarifications_are_appended_only_when_present(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest([CsvUpload("data.csv", b"value\n1\n2\n")])
            builder = PromptBuilder()

            without_clarifications = builder.build(
                "Show the values",
                ingestion.schema,
            )
            with_clarifications = builder.build(
                "Show the values",
                ingestion.schema,
                (
                    "Ignore null values.",
                    "Return one row per category.",
                    "   ",
                ),
            )

            self.assertNotIn("CLARIFICATIONS", without_clarifications)
            self.assertIn(
                "CLARIFICATIONS\n"
                "- Ignore null values.\n"
                "- Return one row per category.",
                with_clarifications,
            )
            self.assertGreater(
                with_clarifications.index("CLARIFICATIONS"),
                with_clarifications.index("USER REQUEST"),
            )


if __name__ == "__main__":
    unittest.main()
