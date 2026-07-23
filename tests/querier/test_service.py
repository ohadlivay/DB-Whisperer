"""Tests for SQL generation, validation, and execution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import (
    ComponentState,
    CsvUpload,
    QueryRequest,
    Relationship,
)
from db_whisperer.etler import ETLService
from db_whisperer.querier import QueryService
from db_whisperer.querier.schema_linker import SchemaLinker
from db_whisperer.querier.sql_validator import (
    SQLValidationError,
    validate_read_only_sql,
)


class FakeOpenRouterClient:
    def __init__(self, sql: str | list[str]) -> None:
        self.sql = sql
        self.prompts: list[str] = []
        self.metadata: list[dict | None] = []

    def generate_sql(
        self,
        prompt: str,
        api_key: str,
        model: str,
        metadata=None,
    ) -> str:
        self.prompts.append(prompt)
        self.metadata.append(metadata)
        if isinstance(self.sql, list):
            return self.sql.pop(0)
        return self.sql


class QueryServiceTest(unittest.TestCase):
    def test_query_sends_complete_prompt_and_executes_sql(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest(
                [
                    CsvUpload(
                        "data.csv",
                        b"category,amount\nA,10\nA,15\nB,7\n",
                    )
                ]
            )
            client = FakeOpenRouterClient(
                """
                SELECT category, SUM(amount) AS total
                FROM data
                GROUP BY category
                ORDER BY category
                """
            )
            service = QueryService(client=client)
            request = QueryRequest(
                prompt="Total amount by category",
                schema=ingestion.schema,
                api_key="test-key",
                model="test/model",
                clarifications=("Exclude null amounts.",),
            )

            result = service.query(request)

            self.assertEqual(ComponentState.ACCEPTED, result.state)
            self.assertEqual(("category", "total"), result.columns)
            self.assertEqual((("A", 25), ("B", 7)), result.rows)
            self.assertEqual(1, len(client.prompts))
            sent_prompt = client.prompts[0]
            self.assertIn("DATABASE SCHEMA", sent_prompt)
            self.assertIn("TOP 5 ROWS", sent_prompt)
            self.assertIn("SHAPE", sent_prompt)
            self.assertIn("COLUMN STATISTICS", sent_prompt)
            self.assertIn("USER REQUEST", sent_prompt)
            self.assertIn(
            "CLARIFICATIONS (BINDING; OVERRIDE CONFLICTING OR AMBIGUOUS "
            "WORDING IN THE ORIGINAL REQUEST)\n- Exclude null amounts.",
                sent_prompt,
            )

    def test_unsafe_generated_sql_is_not_executed(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest([CsvUpload("data.csv", b"id\n1\n")])
            service = QueryService(
                client=FakeOpenRouterClient("DROP TABLE data")
            )

            result = service.query(
                QueryRequest(
                    prompt="Remove the data",
                    schema=ingestion.schema,
                    api_key="test-key",
                    model="test/model",
                )
            )

            self.assertEqual(ComponentState.FAILED, result.state)
            self.assertIsNone(result.sql)
            self.assertEqual("validator", result.failure_kind)
            self.assertEqual(1, len(service.client.prompts))

    def test_missing_column_is_recorded_as_schema_resolution(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest([CsvUpload("data.csv", b"id\n1\n")])
            service = QueryService(
                client=FakeOpenRouterClient('SELECT "missing_column" FROM "data";')
            )

            result = service.query(
                QueryRequest(
                    prompt="Show the unavailable value",
                    schema=ingestion.schema,
                    api_key="test-key",
                    model="test/model",
                )
            )

            self.assertEqual(ComponentState.FAILED, result.state)
            self.assertEqual("schema_resolution", result.failure_kind)
            self.assertIn("missing_column", result.sql)

    def test_invalid_sql_is_preserved_on_failed_candidate(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest([CsvUpload("data.csv", b"id\n1\n")])
            service = QueryService(
                client=FakeOpenRouterClient("SELEC broken")
            )

            candidate = service.generate_candidate(
                QueryRequest(
                    prompt="Show the data",
                    schema=ingestion.schema,
                    api_key="test-key",
                    model="test/model",
                )
            )

            self.assertEqual(ComponentState.FAILED, candidate.state)
            self.assertEqual("SELEC broken", candidate.sql)
            self.assertIn("Generated SQL", candidate.message)

    def test_invalid_sql_is_retried_with_validation_feedback(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest(
                [CsvUpload("data.csv", b"field name\nA\nB\n")]
            )
            client = FakeOpenRouterClient(
                [
                    (
                        'SELECT "field name", COUNT(*) FROM "data" '
                        'GROUP BY "field name'
                    ),
                    (
                        'SELECT "field name", COUNT(*) FROM "data" '
                        'GROUP BY "field name";'
                    ),
                ]
            )
            service = QueryService(client=client)

            candidate = service.generate_candidate(
                QueryRequest(
                    prompt="Count rows by field",
                    schema=ingestion.schema,
                    api_key="test-key",
                    model="test/model",
                    attempt_number=4,
                )
            )

            self.assertEqual(ComponentState.ACCEPTED, candidate.state)
            self.assertEqual(2, len(client.prompts))
            self.assertIn("VALIDATION RETRY", client.prompts[1])
            self.assertIn("unterminated quoted identifier", client.prompts[1])
            self.assertEqual(
                [
                    {"attempt_number": 4, "validation_retry": 0},
                    {"attempt_number": 4, "validation_retry": 1},
                ],
                client.metadata,
            )

    def test_validator_rejects_multiple_statements_and_external_reads(self) -> None:
        with self.assertRaises(SQLValidationError):
            validate_read_only_sql("SELECT 1; SELECT 2")
        with self.assertRaises(SQLValidationError):
            validate_read_only_sql("SELECT * FROM read_csv_auto('secret.csv')")

    def test_query_service_prunes_prompt_via_rag(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "test.duckdb"
            ).ingest(
                [
                    CsvUpload("customers.csv", b"customer_id,name\n1,Ada\n2,Bo\n3,Cy\n"),
                    CsvUpload("orders.csv", b"order_id,customer_id\n10,1\n11,2\n12,1\n13,3\n"),
                ]
            )

            # Use a mock linker that returns only customers
            class StubSchemaLinker:
                def link_schema(
                    self,
                    user_prompt,
                    schema,
                    api_key,
                    model,
                    required_tables=None,
                ):
                    return {"customers"}

            client = FakeOpenRouterClient("SELECT 1")
            service = QueryService(
                client=client,
                rag_threshold=1,
                schema_linker=StubSchemaLinker(),
            )

            prompt = service.build_prompt(
                QueryRequest(
                    prompt="Show customers",
                    schema=ingestion.schema,
                    api_key="test-key",
                    model="test/model",
                )
            )

            # customers should be profiled
            self.assertIn('CREATE TABLE "customers" (', prompt)
            
            # orders should be pruned
            self.assertNotIn('CREATE TABLE "orders" (', prompt)

    def test_clarification_pins_admissions_without_biasing_initial_prompt(
        self,
    ) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            ingestion = ETLService(
                Path(directory) / "clinical.duckdb"
            ).ingest(
                [
                    CsvUpload(
                        "patients.csv",
                        b"subject_id,dob\n1,2000-01-01\n2,2001-01-01\n",
                    ),
                    CsvUpload(
                        "admissions.csv",
                        (
                            b"hadm_id,subject_id,admittime\n"
                            b"10,1,2024-02-01\n11,2,2023-03-01\n"
                        ),
                    ),
                ]
            )
            schema = replace(
                ingestion.schema,
                relationships=(Relationship(
                    child_table="admissions",
                    child_column="subject_id",
                    parent_table="patients",
                    parent_column="subject_id",
                ),),
            )
            service = QueryService(
                client=FakeOpenRouterClient("SELECT 1"),
                rag_threshold=1,
                schema_linker=SchemaLinker(),
            )

            initial = service.build_prompt(QueryRequest(
                prompt="Show me all the patients from 2024",
                schema=schema,
                api_key="key",
                model="model",
            ))
            clarified = service.build_prompt(QueryRequest(
                prompt="Show me all the patients from 2024",
                schema=schema,
                api_key="key",
                model="model",
                clarifications=(
                    "Question: Born or admitted in 2024? "
                    '(clarifying which column: "patients.dob" or '
                    '"admissions.admittime")\n'
                    "Selected answer: Admitted in 2024",
                ),
            ))

            self.assertIn('CREATE TABLE "patients" (', initial)
            self.assertNotIn('CREATE TABLE "admissions" (', initial)
            self.assertIn('CREATE TABLE "patients" (', clarified)
            self.assertIn('CREATE TABLE "admissions" (', clarified)
            self.assertIn(
                '"admissions"."subject_id" -> '
                '"patients"."subject_id"',
                clarified,
            )



if __name__ == "__main__":
    unittest.main()
