"""Tests for the deployment pieces: webhook sink, importer, dataset filter."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
STUDY_DIR = ROOT / "benchmark" / "study"
sys.path.insert(0, str(STUDY_DIR))

import import_webhook  # noqa: E402
import sink  # noqa: E402
from study_logic import filter_scenarios_by_dataset  # noqa: E402


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class PostSessionTest(unittest.TestCase):
    def test_no_url_is_reported_not_raised(self) -> None:
        ok, message = sink.post_session(None, {"a": 1})
        self.assertFalse(ok)
        self.assertIn("no webhook", message)

    def test_success_sends_json_payload(self) -> None:
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _Response(200)

        ok, message = sink.post_session("https://hook", {"x": 1}, post=fake_post)
        self.assertTrue(ok)
        self.assertEqual(captured["url"], "https://hook")
        self.assertEqual(captured["json"], {"x": 1})
        self.assertEqual(captured["headers"]["Accept"], "application/json")

    def test_non_2xx_is_a_failure(self) -> None:
        ok, message = sink.post_session("https://hook", {}, post=lambda *a, **k: _Response(500))
        self.assertFalse(ok)
        self.assertIn("500", message)

    def test_network_error_never_raises(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("dns exploded")

        ok, message = sink.post_session("https://hook", {}, post=boom)
        self.assertFalse(ok)
        self.assertIn("dns exploded", message)

    def test_build_payload_shape(self) -> None:
        payload = sink.build_session_payload([{"type": "task"}], "p1")
        self.assertEqual(payload["participant_id"], "p1")
        self.assertEqual(payload["n_records"], 1)
        self.assertEqual(payload["records"], [{"type": "task"}])


class FilterScenariosTest(unittest.TestCase):
    SCENARIOS = (
        {"id": "b1", "dataset": "BikeStores"},
        {"id": "m1", "dataset": "MIMIC"},
    )

    def test_none_keeps_all(self) -> None:
        self.assertEqual(filter_scenarios_by_dataset(self.SCENARIOS, None), self.SCENARIOS)

    def test_empty_list_keeps_all(self) -> None:
        self.assertEqual(filter_scenarios_by_dataset(self.SCENARIOS, []), self.SCENARIOS)

    def test_restricts_to_named_dataset(self) -> None:
        kept = filter_scenarios_by_dataset(self.SCENARIOS, ["BikeStores"])
        self.assertEqual([s["id"] for s in kept], ["b1"])

    def test_unknown_dataset_yields_empty(self) -> None:
        self.assertEqual(filter_scenarios_by_dataset(self.SCENARIOS, ["Nope"]), ())


class ImportWebhookTest(unittest.TestCase):
    def test_iter_submissions_shapes(self) -> None:
        self.assertEqual(len(import_webhook.iter_submissions([{"a": 1}, {"b": 2}])), 2)
        self.assertEqual(len(import_webhook.iter_submissions({"submissions": [{"a": 1}]})), 1)
        self.assertEqual(len(import_webhook.iter_submissions({"a": 1})), 1)  # single dict
        self.assertEqual(import_webhook.iter_submissions("junk"), [])

    def test_records_from_submission_handles_stringified_json(self) -> None:
        recs = [{"type": "task", "participant_id": "p1"}]
        self.assertEqual(import_webhook.records_from_submission({"records": recs}), recs)
        # Some providers stringify nested JSON:
        self.assertEqual(
            import_webhook.records_from_submission({"records": json.dumps(recs)}), recs
        )
        self.assertEqual(import_webhook.records_from_submission({"records": None}), [])

    def test_write_results_reconstructs_jsonl(self) -> None:
        export = [
            {"participant_id": "alice", "records": [
                {"type": "session_start", "participant_id": "alice"},
                {"type": "task", "participant_id": "alice", "trust": 4},
            ]},
            {"participant_id": "bob", "records": [{"type": "task", "participant_id": "bob"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            participants, records = import_webhook.write_results(export, out)
            self.assertEqual((participants, records), (2, 3))
            alice = (out / "alice.jsonl").read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(alice), 2)
            self.assertEqual(json.loads(alice[1])["trust"], 4)

    def test_empty_export_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(import_webhook.write_results([], Path(tmp)), (0, 0))


if __name__ == "__main__":
    unittest.main()
