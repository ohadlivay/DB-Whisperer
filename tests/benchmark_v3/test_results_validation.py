from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import benchmark_v3.aggregate_results as aggregation
from tests.benchmark_v3.test_aggregation import write_campaign


class ResultsValidationTest(unittest.TestCase):
    def _aggregate(self, directory: Path) -> dict[str, object]:
        self.assertTrue(hasattr(aggregation, "aggregate_campaign"))
        return aggregation.aggregate_campaign(directory)

    def test_rejects_missing_cells_and_nonfinite_metrics(self) -> None:
        self.assertTrue(hasattr(aggregation, "validate_aggregate"))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            report_path = directory / "run-01.json"
            report = json.loads(report_path.read_text())
            report["records"].pop()
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "expected work"):
                self._aggregate(directory)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            report_path = directory / "run-01.json"
            report = json.loads(report_path.read_text())
            report["records"][0]["score"]["correctness"] = float("inf")
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "finite"):
                self._aggregate(directory)

    def test_accepts_complete_aggregate_without_unresolved_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            payload = self._aggregate(directory)
        aggregation.validate_aggregate(payload)

    def test_rejects_nonfinite_aggregate_metric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            payload = self._aggregate(directory)
        payload["operational"]["cost_usd"]["mean"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"):
            aggregation.validate_aggregate(payload)
