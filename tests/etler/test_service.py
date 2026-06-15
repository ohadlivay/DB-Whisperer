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
            self.assertTrue(
                all(
                    column.table_name == "sales_data"
                    for column in result.schema.columns
                )
            )
            self.assertEqual(1, len(result.schema.tables))
            self.assertEqual("sales_data", result.schema.tables[0].table_name)
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

    def test_ingest_loads_multiple_files(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            result = service.ingest(
                [
                    CsvUpload("customers.csv", b"id,name\n1,Ada\n2,Bo\n"),
                    CsvUpload("orders.csv", b"order_id,id\n10,1\n11,2\n12,1\n"),
                ]
            )

            self.assertEqual(ComponentState.ACCEPTED, result.state)
            self.assertEqual(
                {"customers", "orders"},
                set(result.schema.table_names),
            )
            self.assertEqual(2, len(result.schema.tables))
            self.assertIsNone(result.schema.row_count)

            connection = duckdb.connect(
                result.schema.database_path,
                read_only=True,
            )
            try:
                customer_rows = connection.execute(
                    "SELECT COUNT(*) FROM customers"
                ).fetchone()[0]
                order_rows = connection.execute(
                    "SELECT COUNT(*) FROM orders"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(2, customer_rows)
            self.assertEqual(3, order_rows)

    def test_heterogeneous_column_falls_back_to_varchar(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            result = service.ingest(
                [CsvUpload("readings.csv", b"val\n1\nhello\n2\n")]
            )

            self.assertEqual(ComponentState.ACCEPTED, result.state)
            self.assertEqual(3, result.schema.row_count)
            value_column = next(
                column
                for column in result.schema.columns
                if column.name == "val"
            )
            self.assertEqual("VARCHAR", value_column.data_type)

    def test_ingest_rejects_empty_file(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "test.duckdb")

            result = service.ingest([CsvUpload("empty.csv", b"")])

            self.assertEqual(ComponentState.FAILED, result.state)
            self.assertIsNone(result.schema.database_path)


class RelationshipDiscoveryTest(unittest.TestCase):
    def _ingest(self, files: dict[str, bytes]):
        directory = TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        service = ETLService(Path(directory.name) / "test.duckdb")
        uploads = [
            CsvUpload(name=name, content=content)
            for name, content in files.items()
        ]
        result = service.ingest(uploads)
        self.assertEqual(ComponentState.ACCEPTED, result.state, result.message)
        return result.schema

    @staticmethod
    def _fks(schema) -> set[tuple[str, str, str, str]]:
        return {
            (r.child_table, r.child_column, r.parent_table, r.parent_column)
            for r in schema.relationships
        }

    @staticmethod
    def _rows(header: str, *rows: str) -> bytes:
        return ("\n".join((header, *rows)) + "\n").encode("utf-8")

    def test_discovers_foreign_key_from_value_overlap(self) -> None:
        schema = self._ingest(
            {
                "customers.csv": self._rows(
                    "customer_id,name", "1,Ada", "2,Bo", "3,Cy"
                ),
                "orders.csv": self._rows(
                    "order_id,customer_id", "100,1", "101,2", "102,1", "103,3"
                ),
                "products.csv": self._rows("product_id,sku", "7,AAA", "8,BBB"),
            }
        )
        fks = self._fks(schema)
        self.assertIn(("orders", "customer_id", "customers", "customer_id"), fks)
        self.assertFalse(
            any("products" in (fk[0], fk[2]) for fk in fks),
            f"unrelated products table should not appear: {fks}",
        )

    def test_unique_child_fk_column_is_discovered(self) -> None:
        # A foreign key that happens to be unique on the child side (a
        # one-to-one link, or a small sample with one row per parent) must
        # still be discovered, not skipped as if it were a primary key.
        schema = self._ingest(
            {
                "customers.csv": self._rows(
                    "customer_id,name", "1,Ada", "2,Bo", "3,Cy"
                ),
                "orders.csv": self._rows(
                    "order_id,customer_id", "100,1", "101,2", "102,3"
                ),
            }
        )
        self.assertIn(
            ("orders", "customer_id", "customers", "customer_id"),
            self._fks(schema),
        )

    def test_self_referential_fk_detected(self) -> None:
        schema = self._ingest(
            {
                "staff.csv": self._rows(
                    "staff_id,manager_id", "1,", "2,1", "3,1", "4,2"
                ),
            }
        )
        relationship = next(
            r
            for r in schema.relationships
            if r.child_column == "manager_id"
        )
        self.assertEqual("staff", relationship.parent_table)
        self.assertEqual("staff_id", relationship.parent_column)
        self.assertEqual("self-reference", relationship.cardinality)

    def test_row_id_columns_create_no_relationships(self) -> None:
        schema = self._ingest(
            {
                "a.csv": self._rows("row_id,name", "1,x", "2,y", "3,z"),
                "b.csv": self._rows("row_id,label", "1,p", "2,q", "3,r"),
            }
        )
        self.assertEqual((), schema.relationships)

    def test_low_distinct_named_dimension_is_accepted(self) -> None:
        schema = self._ingest(
            {
                "stores.csv": self._rows("store_id,city", "1,A", "2,B", "3,C"),
                "sales.csv": self._rows(
                    "sale_id,store_id", "1,1", "2,2", "3,3", "4,1"
                ),
            }
        )
        self.assertIn(("sales", "store_id", "stores", "store_id"), self._fks(schema))

    def test_coincidental_low_distinct_overlap_is_rejected(self) -> None:
        schema = self._ingest(
            {
                "widgets.csv": self._rows("widget_id,name", "1,a", "2,b", "3,c"),
                "events.csv": self._rows(
                    "event_id,flag_id", "10,1", "11,2", "12,3", "13,1"
                ),
            }
        )
        self.assertFalse(
            any(r.child_column == "flag_id" for r in schema.relationships),
            "low-distinct flag_id without a strong signal must not match",
        )

    def test_ambiguous_parents_emitted_together(self) -> None:
        schema = self._ingest(
            {
                "items_a.csv": self._rows("item_id,a", "1,x", "2,y", "3,z"),
                "items_b.csv": self._rows("item_id,b", "1,x", "2,y", "3,z"),
                "events.csv": self._rows(
                    "event_id,item_id", "10,1", "11,2", "12,3", "13,1"
                ),
            }
        )
        matches = [
            r for r in schema.relationships if r.child_column == "item_id"
        ]
        self.assertEqual(2, len(matches))
        self.assertTrue(all(r.ambiguous for r in matches))
        self.assertEqual(
            {"items_a", "items_b"},
            {r.parent_table for r in matches},
        )

    def test_disjoint_range_keeps_fk_with_orphan_above_parent_max(self) -> None:
        parent = self._rows("pid", *(str(i) for i in range(1, 21)))
        # cid is non-unique (1 repeats) so it is an FK candidate, with one
        # orphan (999) above the parent's max — ~95% overlap must still pass.
        child = self._rows(
            "cid", *(str(i) for i in range(1, 21)), "1", "999"
        )
        schema = self._ingest({"parent.csv": parent, "child.csv": child})
        self.assertIn(("child", "cid", "parent", "pid"), self._fks(schema))

    def test_id_key_preferred_over_non_id_unique_column(self) -> None:
        schema = self._ingest(
            {
                "cat.csv": self._rows("widget_id,amount", "5,5", "6,6", "7,7"),
                "item.csv": self._rows(
                    "item_id,widget_id", "1,5", "2,6", "3,7", "4,5"
                ),
            }
        )
        matches = [
            r for r in schema.relationships if r.child_column == "widget_id"
        ]
        self.assertTrue(matches)
        self.assertTrue(all(r.parent_column == "widget_id" for r in matches))
        self.assertFalse(any(r.parent_column == "amount" for r in matches))

    def test_actual_cased_identifiers_and_no_duplicates(self) -> None:
        schema = self._ingest(
            {
                "Customers.csv": self._rows("Customer_Id,Name", "1,Ada", "2,Bo"),
                "Orders.csv": self._rows(
                    "Order_Id,Customer_Id", "9,1", "8,2", "7,1"
                ),
            }
        )
        self.assertIn(
            ("orders", "Customer_Id", "customers", "Customer_Id"),
            self._fks(schema),
        )
        self.assertEqual(
            len(schema.relationships),
            len(set(schema.relationships)),
        )

    def test_overlap_is_cast_safe_across_inferred_types(self) -> None:
        directory = TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        database_path = Path(directory.name) / "cast.duckdb"
        connection = duckdb.connect(str(database_path))
        try:
            connection.execute("CREATE TABLE parent (dim_id VARCHAR)")
            connection.execute("INSERT INTO parent VALUES ('1'), ('2'), ('3')")
            connection.execute("CREATE TABLE child (dim_id BIGINT)")
            connection.execute("INSERT INTO child VALUES (1), (2), (3)")

            service = ETLService(database_path)
            forward = service._compute_overlap(
                connection, "child", "dim_id", "parent", "dim_id"
            )
            backward = service._compute_overlap(
                connection, "parent", "dim_id", "child", "dim_id"
            )
        finally:
            connection.close()

        self.assertEqual(1.0, forward)
        self.assertEqual(1.0, backward)

    def test_sampling_marks_partial_discovery(self) -> None:
        directory = TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        service = ETLService(Path(directory.name) / "sample.duckdb")
        service._MAX_OVERLAP_SAMPLE = 2  # force the sampling path

        result = service.ingest(
            [
                CsvUpload(
                    "customers.csv",
                    self._rows("customer_id,name", "1,a", "2,b", "3,c"),
                ),
                CsvUpload(
                    "orders.csv",
                    self._rows(
                        "order_id,customer_id", "10,1", "11,2", "12,3", "13,1"
                    ),
                ),
            ]
        )
        schema = result.schema
        self.assertFalse(schema.discovery_complete)
        self.assertTrue(schema.discovery_notes)
        relationship = next(
            r for r in schema.relationships if r.child_column == "customer_id"
        )
        self.assertTrue(relationship.sampled)


if __name__ == "__main__":
    unittest.main()
