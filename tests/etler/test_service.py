"""Tests for the single-CSV ETL implementation."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import ComponentState, CsvUpload
from db_whisperer.etler import ETLService


class ETLServiceTest(unittest.TestCase):
    def test_ingest_creates_queryable_persistent_database(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            database_path = Path(directory) / "test.duckdb"
            service = ETLService(database_path)

            result = service.ingest(
                [
                    CsvUpload(
                        name="Sales Data.csv",
                        content=(
                            b"item,quantity,price\n"
                            b"A,2,4.5\n"
                            b"B,3,7.0\n"
                        ),
                    )
                ]
            )

            self.assertEqual(ComponentState.ACCEPTED, result.state)
            self.assertEqual(("sales_data",), result.schema.table_names)
            self.assertEqual(2, result.schema.row_count)
            self.assertEqual(
                ["item", "quantity", "price"],
                [column.name for column in result.schema.columns],
            )
            self.assertEqual(
                str(database_path.resolve()),
                result.schema.database_path,
            )

            connection = duckdb.connect(
                result.schema.database_path,
                read_only=True,
            )
            try:
                rows = connection.execute(
                    """
                    SELECT item, quantity, price
                    FROM sales_data
                    ORDER BY item
                    """
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(
                [("A", 2, 4.5), ("B", 3, 7.0)],
                rows,
            )

    def test_ingest_replaces_previous_database(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            service.ingest([CsvUpload("first.csv", b"id\n1\n")])
            result = service.ingest([CsvUpload("second.csv", b"value\nnew\n")])

            connection = duckdb.connect(
                result.schema.database_path,
                read_only=True,
            )
            try:
                tables = connection.execute("SHOW TABLES").fetchall()
            finally:
                connection.close()

            self.assertEqual([("second",)], tables)

    def test_ingest_rejects_multiple_files(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            result = service.ingest(
                [
                    CsvUpload("first.csv", b"id\n1\n"),
                    CsvUpload("second.csv", b"id\n2\n"),
                ]
            )

            self.assertEqual(ComponentState.FAILED, result.state)
            self.assertIsNone(result.schema.database_path)

    def test_ingest_rejects_empty_file(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            result = service.ingest([CsvUpload("empty.csv", b"")])

            self.assertEqual(ComponentState.FAILED, result.state)
            self.assertIsNone(result.schema.database_path)


if __name__ == "__main__":
    unittest.main()
