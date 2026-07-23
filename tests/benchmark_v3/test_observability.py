from __future__ import annotations

import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from benchmark_v3.observability import (
    BudgetStop,
    CampaignObserver,
    InstrumentedSession,
    retry_transient,
)


def item(
    key: str,
    repetition: int = 1,
    case_id: str = "stay_icu",
    arm: str = "full",
    category: str = "ambiguity",
) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        repetition=repetition,
        case_id=case_id,
        arm=arm,
        category=category,
    )


class CampaignObserverTest(unittest.TestCase):
    def test_status_events_prompt_redaction_and_checkpoint_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            observer = CampaignObserver(directory, (item("r1-stay"),), 3.75)

            observer.activate(item("r1-stay"), "generating")
            observer.event(
                "request_failed",
                message="Authorization: Bearer do-not-write-this",
                api_key="also-secret",
                severity="error",
            )
            observer.prompt_logger.log_prompt(
                "querier",
                "model",
                "safe prompt",
                {
                    "Authorization": "Bearer never-log",
                    "X-API-Key": "header-secret",
                    "attempt": 1,
                },
            )
            checkpoint = observer.checkpoint("r1-stay", {"score": {"passed": True}})
            observer.complete_cell(
                duration=12,
                arm="full",
                category="ambiguity",
                passed=True,
            )

            status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
            event_log = (directory / "events.jsonl").read_text(encoding="utf-8")
            prompt_log = (directory / "prompts.jsonl").read_text(encoding="utf-8")
            self.assertTrue(checkpoint.exists())
            self.assertEqual(1, status["completed_units"])
            self.assertEqual("generating", status["active"][0]["phase"])
            self.assertIn("full/ambiguity", status["eta_by_arm_category"])
            self.assertNotIn("do-not-write-this", event_log)
            self.assertNotIn("also-secret", event_log)
            self.assertNotIn("never-log", prompt_log)
            self.assertNotIn("header-secret", prompt_log)
            self.assertNotIn("Authorization", event_log)
            self.assertNotIn("Authorization", prompt_log)

    def test_retry_transient_retries_only_transient_failures(self) -> None:
        attempts = 0

        class Response:
            status_code = 429

        def eventually_ok() -> object:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return Response()
            return SimpleNamespace(status_code=200)

        response = retry_transient(
            eventually_ok,
            attempts=4,
            base_delay=0,
            random_source=random.Random(0),
        )

        self.assertEqual(3, attempts)
        self.assertEqual(200, response.status_code)

        calls = 0

        def permanent_failure() -> object:
            nonlocal calls
            calls += 1
            return SimpleNamespace(status_code=400)

        self.assertEqual(
            400,
            retry_transient(
                permanent_failure,
                random_source=random.Random(0),
            ).status_code,
        )
        self.assertEqual(1, calls)

    def test_instrumented_session_stops_before_a_paid_request_at_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            observer.record_usage(
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=3.75,
            )
            session = InstrumentedSession(observer)
            with self.assertRaises(BudgetStop):
                session.post("https://example.invalid")
            self.assertEqual(0, observer.status["model_calls"])

    def test_restart_reconciles_completed_cells_from_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            work_items = (item("passed"), item("failed"))
            first = CampaignObserver(directory, work_items, 3.75)
            first.checkpoint("passed", {"score": {"passed": True}})
            first.checkpoint("failed", {"score": {"passed": False}})

            resumed = CampaignObserver(directory, work_items, 3.75)

            self.assertEqual(2, resumed.status["completed_units"])
            self.assertEqual(1, resumed.status["passed"])
            self.assertEqual(1, resumed.status["failed"])

    def test_instrumented_request_failure_updates_latest_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            session = InstrumentedSession(observer, attempts=1)
            transport = SimpleNamespace(
                post=lambda *args, **kwargs: (_ for _ in ()).throw(
                    requests.ConnectionError("network down")
                )
            )
            with patch.object(session, "_transport", return_value=transport):
                with self.assertRaises(requests.ConnectionError):
                    session.post("https://example.invalid")
            self.assertIn("network down", observer.status["latest_error"])


if __name__ == "__main__":
    unittest.main()
