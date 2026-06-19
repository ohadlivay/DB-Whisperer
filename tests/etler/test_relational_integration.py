"""End-to-end relationship discovery against the bundled datasets."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import ComponentState, CsvUpload
from db_whisperer.etler import ETLService


RELATIONAL_DIR = ROOT / "data" / "relational csv"
MIMIC_DIR = (
    ROOT
    / "data"
    / "mimic-iii-clinical-database-demo-1.4-20260615T211207Z-3-001"
    / "mimic-iii-clinical-database-demo-1.4"
)

BIKESTORES_FOREIGN_KEYS = {
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("orders", "staff_id", "staffs", "staff_id"),
    ("orders", "store_id", "stores", "store_id"),
    ("products", "brand_id", "brands", "brand_id"),
    ("products", "category_id", "categories", "category_id"),
    ("staffs", "store_id", "stores", "store_id"),
    ("staffs", "manager_id", "staffs", "staff_id"),
    ("stocks", "product_id", "products", "product_id"),
    ("stocks", "store_id", "stores", "store_id"),
}


def _load_folder(folder: Path) -> list[CsvUpload]:
    return [
        CsvUpload(name=path.name, content=path.read_bytes())
        for path in sorted(folder.glob("*.csv"))
    ]


def _fk_set(schema) -> set[tuple[str, str, str, str]]:
    return {
        (r.child_table, r.child_column, r.parent_table, r.parent_column)
        for r in schema.relationships
    }


@unittest.skipUnless(RELATIONAL_DIR.is_dir(), "BikeStores data not present.")
class BikeStoresIntegrationTest(unittest.TestCase):
    def test_discovers_the_expected_bikestores_foreign_keys(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "bikestores.duckdb")
            result = service.ingest(_load_folder(RELATIONAL_DIR))

        self.assertEqual(ComponentState.ACCEPTED, result.state, result.message)
        self.assertEqual(9, len(result.schema.tables))

        discovered = _fk_set(result.schema)
        missing = BIKESTORES_FOREIGN_KEYS - discovered
        self.assertEqual(set(), missing, f"missing FKs: {missing}")

        # item_id is part of the order_items composite key, not a foreign key.
        self.assertFalse(
            any(r.child_column == "item_id" for r in result.schema.relationships),
            "order_items.item_id must not be treated as a foreign key",
        )


@unittest.skipUnless(
    MIMIC_DIR.is_dir() and os.environ.get("DB_WHISPERER_RUN_MIMIC_TEST"),
    "Set DB_WHISPERER_RUN_MIMIC_TEST=1 to run the slow MIMIC test.",
)
class MimicIntegrationTest(unittest.TestCase):
    def test_loads_all_tables_and_finds_core_foreign_keys(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            service = ETLService(Path(directory) / "mimic.duckdb")
            result = service.ingest(_load_folder(MIMIC_DIR))

        self.assertEqual(ComponentState.ACCEPTED, result.state, result.message)
        self.assertEqual(26, len(result.schema.tables))

        discovered = _fk_set(result.schema)
        core = {
            ("admissions", "subject_id", "patients", "subject_id"),
            ("chartevents", "subject_id", "patients", "subject_id"),
            ("chartevents", "hadm_id", "admissions", "hadm_id"),
            ("chartevents", "itemid", "d_items", "itemid"),
            ("labevents", "itemid", "d_labitems", "itemid"),
        }
        missing = core - discovered
        self.assertEqual(set(), missing, f"missing core MIMIC FKs: {missing}")

        # row_id is a per-table surrogate key, never a join key.
        self.assertFalse(
            any(r.child_column == "row_id" for r in result.schema.relationships),
            "row_id must not produce relationships",
        )


if __name__ == "__main__":
    unittest.main()
