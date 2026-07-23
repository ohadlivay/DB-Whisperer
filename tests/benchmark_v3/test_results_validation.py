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
        payload["operational"]["metrics"]["cost_usd"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"):
            aggregation.validate_aggregate(payload)

    def test_rejects_campaign_fingerprint_and_work_graph_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            campaign_path = directory / "campaign.json"
            campaign = json.loads(campaign_path.read_text())
            campaign["fingerprint"]["runtime_hash"] = "tampered"
            campaign_path.write_text(json.dumps(campaign))
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                self._aggregate(directory)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            report_path = directory / "run-01.json"
            report = json.loads(report_path.read_text())
            report["records"][0]["family_id"] = "tampered"
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "work graph"):
                self._aggregate(directory)

    def test_rejects_nonfinite_authoritative_status_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            status_path = directory / "status.json"
            status = json.loads(status_path.read_text())
            status["cost_usd"] = "not-a-number"
            status_path.write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError, "usage"):
                self._aggregate(directory)

    def test_rejects_missing_published_distribution_or_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            payload = self._aggregate(directory)
        del payload["shared_etl"]["confidence_interval_95"]
        with self.assertRaisesRegex(ValueError, "distribution"):
            aggregation.validate_aggregate(payload)

    def test_rejects_coordinated_top_level_fingerprint_tamper_and_invalid_distribution_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            report_path = directory / "run-01.json"
            report = json.loads(report_path.read_text())
            report["dataset_hash"] = "tampered"
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "top-level"):
                self._aggregate(directory)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_campaign(directory)
            payload = self._aggregate(directory)
        payload["shared_etl"]["mean"] = "50"
        with self.assertRaisesRegex(ValueError, "distribution"):
            aggregation.validate_aggregate(payload)
