from __future__ import annotations

import json
import math
from pathlib import Path
import random
import tempfile
from threading import Event, Lock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

from benchmark_v3.observability import (
    BudgetStop,
    CampaignObserver,
    InfrastructureStop,
    InstrumentedSession,
    UsageValidationError,
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

    def test_budget_reserves_concurrent_requests_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 0.5)

            observer.admit_model_call()
            observer.admit_model_call()

            with self.assertRaises(BudgetStop):
                observer.admit_model_call()
            self.assertEqual(0.5, observer.status["reserved_cost_usd"])
            observer.release_model_call()
            self.assertEqual(0.25, observer.status["reserved_cost_usd"])

    def test_budget_admission_is_atomic_with_recorded_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 1.0)
            cost_is_locked = Event()
            release_cost = Event()
            admission_started = Event()
            errors: list[BaseException] = []
            original_publish = observer._publish_locked

            def controlled_publish(**changes: object) -> None:
                if changes.get("cost_usd") == 1.0:
                    cost_is_locked.set()
                    self.assertTrue(release_cost.wait(2))
                original_publish(**changes)

            def record_cost() -> None:
                observer.record_usage(
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost_usd=1.0,
                )

            def admit() -> None:
                admission_started.set()
                try:
                    observer.admit_model_call()
                except BaseException as error:
                    errors.append(error)

            with patch.object(observer, "_publish_locked", controlled_publish):
                cost_thread = Thread(target=record_cost)
                cost_thread.start()
                self.assertTrue(cost_is_locked.wait(2))
                admission_thread = Thread(target=admit)
                admission_thread.start()
                self.assertTrue(admission_started.wait(2))
                release_cost.set()
                cost_thread.join(2)
                admission_thread.join(2)

            self.assertFalse(cost_thread.is_alive())
            self.assertFalse(admission_thread.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], BudgetStop)
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

    def test_restart_ignores_unrelated_checkpoints_and_eta_counts_only_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            work_items = (
                item("one"),
                item("two"),
                item("three"),
            )
            first = CampaignObserver(directory, work_items, 3.75)
            first.checkpoint("one", {"score": {"passed": True}})
            first.checkpoint("old-campaign-cell", {"score": {"passed": True}})

            resumed = CampaignObserver(directory, work_items, 3.75)
            resumed.complete_cell(
                duration=10,
                arm="full",
                category="ambiguity",
                passed=True,
            )

            self.assertEqual(2, resumed.status["completed_units"])
            self.assertEqual(10, resumed.status["eta_seconds"])
            self.assertEqual(
                10,
                resumed.status["eta_by_arm_category"]["full/ambiguity"],
            )

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

    def test_invalid_provider_usage_fails_closed_without_logging_response_body(self) -> None:
        invalid_usages = (
            {"prompt_tokens": 1.5, "completion_tokens": 0, "cost": 0.1},
            {"prompt_tokens": -1, "completion_tokens": 0, "cost": 0.1},
            {"prompt_tokens": 1, "completion_tokens": "2", "cost": 0.1},
            {"prompt_tokens": 1, "completion_tokens": 0, "cost": float("nan")},
            {"prompt_tokens": 1, "completion_tokens": 0, "cost": float("inf")},
            {"prompt_tokens": 1, "completion_tokens": 0, "cost": -0.01},
        )
        with tempfile.TemporaryDirectory() as temporary:
            for usage in invalid_usages:
                observer = CampaignObserver(Path(temporary), (), 3.75)
                session = InstrumentedSession(observer, attempts=1)
                response = SimpleNamespace(
                    status_code=200,
                    text="Authorization: Bearer provider-body-secret",
                    json=lambda usage=usage: {"usage": usage, "model": "model"},
                )
                transport = SimpleNamespace(post=lambda *args, **kwargs: response)
                with self.subTest(usage=usage), patch.object(
                    session,
                    "_transport",
                    return_value=transport,
                ):
                    with self.assertRaises(UsageValidationError):
                        session.post("https://example.invalid")
                    self.assertEqual(0.0, observer.status["cost_usd"])
                    self.assertNotIn(
                        "provider-body-secret",
                        observer.status["latest_error"],
                    )
                    self.assertIn("invalid provider usage", observer.status["latest_error"])

    def test_huge_provider_usage_fails_closed_with_secret_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            session = InstrumentedSession(observer, attempts=1)
            response = SimpleNamespace(
                status_code=200,
                text="Authorization: Bearer provider-body-secret",
                json=lambda: {
                    "usage": {
                        "prompt_tokens": 10 ** 10000,
                        "completion_tokens": 0,
                        "cost": 0.1,
                    },
                    "model": "model",
                },
            )
            transport = SimpleNamespace(post=lambda *args, **kwargs: response)

            with patch.object(session, "_transport", return_value=transport):
                with self.assertRaises(UsageValidationError):
                    session.post("https://example.invalid")

            self.assertEqual(0.0, observer.status["cost_usd"])
            self.assertEqual("invalid provider usage", observer.status["latest_error"])
            self.assertNotIn("provider-body-secret", observer.status["latest_error"])

    def test_usage_total_overflow_fails_without_mutating_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            observer.record_usage(
                prompt_tokens=1,
                completion_tokens=1,
                cost_usd=1e308,
            )
            before = observer.snapshot()

            with self.assertRaises(UsageValidationError):
                observer.record_usage(
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost_usd=1e308,
                )

            self.assertEqual(before, observer.status)
            self.assertTrue(math.isfinite(observer.status["cost_usd"]))

    def test_resume_clears_stale_active_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            work_item = item("stale")
            first = CampaignObserver(directory, (work_item,), 3.75)
            first.activate(work_item, "generating")

            resumed = CampaignObserver(directory, (work_item,), 3.75)

            self.assertEqual({}, resumed.status["active_by_key"])
            self.assertEqual([], resumed.status["active"])

    def test_final_http_error_blocks_further_model_calls_without_response_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            observer = CampaignObserver(directory, (), 3.75)
            session = InstrumentedSession(observer, attempts=1)
            response = requests.Response()
            response.status_code = 401
            response._content = b'{"error":"body-secret"}'
            transport = SimpleNamespace(post=lambda *args, **kwargs: response)

            with patch.object(session, "_transport", return_value=transport):
                returned = session.post("https://example.invalid")

            self.assertIs(response, returned)
            self.assertIn("HTTP 401", observer.status["latest_error"])
            self.assertEqual(
                "provider",
                observer.status["infrastructure_failure"]["source"],
            )
            self.assertEqual(
                401,
                observer.status["infrastructure_failure"]["status_code"],
            )
            with self.assertRaises(InfrastructureStop):
                observer.admit_model_call()
            durable = (
                observer.status_path.read_text(encoding="utf-8")
                + observer.events_path.read_text(encoding="utf-8")
            )
            self.assertNotIn("body-secret", durable)

    def test_exhausted_connection_failure_blocks_further_model_calls(self) -> None:
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

            failure = observer.status["infrastructure_failure"]
            self.assertEqual("provider", failure["source"])
            self.assertEqual("transport", failure["kind"])

    def test_prompt_failure_event_updates_latest_error_and_sanitizes_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            observer = CampaignObserver(directory, (), 3.75)
            headers = CaseInsensitiveDict({
                "aUtHoRiZaTiOn": "Bearer mapping-secret",
                "X-aPi-KeY": "header-secret",
                "safe": "visible",
            })

            observer.event(
                "diagnostic",
                payload={"headers": headers},
            )
            observer.prompt_logger.log_prompt(
                "querier",
                "model",
                "safe prompt",
                {"nested": [{"headers": headers}]},
            )
            observer.prompt_logger.log_event(
                event="response_validation_failed",
                component="querier",
                details={
                    "error": "Authorization: Bearer status-secret",
                    "headers": headers,
                },
            )

            durable = "".join(
                path.read_text(encoding="utf-8")
                for path in (
                    observer.events_path,
                    observer.prompt_path,
                    observer.status_path,
                )
            )
            for secret in ("mapping-secret", "header-secret", "status-secret"):
                self.assertNotIn(secret, durable)
            self.assertIn("visible", durable)
            self.assertIn("[REDACTED]", observer.status["latest_error"])

    def test_successful_http_envelope_with_provider_request_failure_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)

            observer.prompt_logger.log_event(
                event="request_failed",
                component="querier",
                details={"error": "response contained provider error envelope"},
            )

            failure = observer.current_infrastructure_failure()
            self.assertIsNotNone(failure)
            self.assertEqual("provider", failure["source"])
            self.assertEqual("response", failure["kind"])

    def test_explicit_provider_error_in_validation_event_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)

            observer.prompt_logger.log_event(
                event="response_validation_failed",
                component="ambiguity",
                details={
                    "finish_reason": "error",
                    "choice_error": {
                        "code": "provider_error",
                        "message": "upstream generation failed",
                    },
                },
            )

            failure = observer.current_infrastructure_failure()
            self.assertIsNotNone(failure)
            self.assertEqual("provider", failure["source"])
            self.assertEqual("response", failure["kind"])
            self.assertIn("upstream generation failed", failure["message"])

    def test_non_mapping_choice_error_in_validation_event_blocks_run(self) -> None:
        for choice_error in ("upstream provider failed", ["provider failed"]):
            with (
                self.subTest(choice_error=choice_error),
                tempfile.TemporaryDirectory() as temporary,
            ):
                observer = CampaignObserver(Path(temporary), (), 3.75)
                observer.prompt_logger.log_event(
                    event="response_validation_failed",
                    component="ambiguity",
                    details={
                        "finish_reason": None,
                        "choice_error": choice_error,
                    },
                )
                self.assertIsNotNone(
                    observer.current_infrastructure_failure()
                )

    def test_malformed_model_output_remains_a_system_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)

            observer.prompt_logger.log_event(
                event="response_validation_failed",
                component="querier",
                details={
                    "error": "JSON response did not contain an SQL field",
                    "expected": "SQL object",
                },
            )

            self.assertIsNone(observer.current_infrastructure_failure())

    def test_concurrent_status_event_prompt_and_checkpoint_writes_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            work_items = tuple(item(f"cell-{index}") for index in range(8))
            observer = CampaignObserver(directory, work_items, 3.75)
            start = Event()
            errors: list[BaseException] = []
            errors_lock = Lock()

            def write(index: int) -> None:
                try:
                    start.wait(2)
                    current = work_items[index]
                    observer.activate(current, "running")
                    observer.event("worker_event", worker=index)
                    observer.prompt_logger.log_prompt(
                        "querier",
                        "model",
                        f"prompt-{index}",
                    )
                    observer.checkpoint(
                        current.key,
                        {"score": {"passed": index % 2 == 0}},
                    )
                except BaseException as error:
                    with errors_lock:
                        errors.append(error)

            threads = [Thread(target=write, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(3)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            json.loads(observer.status_path.read_text(encoding="utf-8"))
            for path in (observer.events_path, observer.prompt_path):
                records = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(records)
            self.assertEqual(8, len(tuple(observer.checkpoint_dir.glob("*.json"))))

    def test_session_counts_retry_usage_and_closes_thread_transports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), (), 3.75)
            session = InstrumentedSession(
                observer,
                attempts=2,
                base_delay=0,
                random_source=random.Random(0),
            )
            created: list[object] = []
            create_lock = Lock()
            start = Event()
            all_transports_entered = Event()
            first_call_count = 0
            call_errors: list[BaseException] = []

            class Transport:
                def __init__(self) -> None:
                    self.calls = 0
                    self.closed = False

                def post(self, *args: object, **kwargs: object) -> object:
                    nonlocal first_call_count
                    self.calls += 1
                    if self.calls == 1:
                        with create_lock:
                            first_call_count += 1
                            if first_call_count == 3:
                                all_transports_entered.set()
                        if not all_transports_entered.wait(2):
                            raise AssertionError(
                                "three model transports did not overlap"
                            )
                        return SimpleNamespace(status_code=429)
                    return SimpleNamespace(
                        status_code=200,
                        json=lambda: {
                            "model": "test",
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 2,
                                "cost": 0.01,
                            },
                        },
                    )

                def close(self) -> None:
                    self.closed = True

            def make_transport() -> object:
                transport = Transport()
                with create_lock:
                    created.append(transport)
                return transport

            def call() -> None:
                try:
                    start.wait(2)
                    session.post("https://example.invalid")
                except BaseException as error:
                    with create_lock:
                        call_errors.append(error)

            with patch("benchmark_v3.observability.requests.Session", make_transport):
                threads = [Thread(target=call) for _ in range(3)]
                for thread in threads:
                    thread.start()
                start.set()
                for thread in threads:
                    thread.join(3)
                session.close()

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual([], call_errors)
            self.assertTrue(all_transports_entered.is_set())
            self.assertEqual(6, observer.status["model_calls"])
            self.assertEqual(3, observer.status["retries"])
            self.assertEqual(9, observer.status["prompt_tokens"])
            self.assertEqual(6, observer.status["completion_tokens"])
            self.assertEqual(0.03, observer.status["cost_usd"])
            self.assertEqual(3, len(created))
            self.assertTrue(all(transport.closed for transport in created))


if __name__ == "__main__":
    unittest.main()
