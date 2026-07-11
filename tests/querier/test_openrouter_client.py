"""Tests for the OpenRouter request and response contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.querier.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"sql": "SELECT 1"})
                    }
                }
            ]
        }


class FakeSession:
    def __init__(self) -> None:
        self.request: dict | None = None

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.request = {"url": url, **kwargs}
        return FakeResponse()


class InvalidJsonResponse(FakeResponse):
    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": "SELECT * FROM data"
                    }
                }
            ]
        }


class InvalidJsonSession(FakeSession):
    def post(self, url: str, **kwargs: object) -> InvalidJsonResponse:
        self.request = {"url": url, **kwargs}
        return InvalidJsonResponse()


class OversizedNumberResponse(FakeResponse):
    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"sql": ' + ("9" * 5000) + "}"
                    }
                }
            ]
        }


class OversizedNumberSession(FakeSession):
    def post(self, url: str, **kwargs: object) -> OversizedNumberResponse:
        self.request = {"url": url, **kwargs}
        return OversizedNumberResponse()


class RecordingPromptLogger:
    def __init__(self) -> None:
        self.records = []

    def log_prompt(
        self,
        component,
        model,
        prompt,
        metadata=None,
    ) -> str:
        self.records.append(
            (
                "prompt",
                "request-1",
                component,
                model,
                prompt,
                metadata,
            )
        )
        return "request-1"

    def log_response(
        self,
        request_id,
        component,
        model,
        response,
    ) -> None:
        self.records.append(
            ("response", request_id, component, model, response)
        )

    def log_event(
        self,
        event,
        component,
        details,
        request_id=None,
        model=None,
    ) -> None:
        self.records.append(
            ("event", request_id, component, model, event, details)
        )


class OpenRouterClientTest(unittest.TestCase):
    def test_sends_complete_prompt_as_chat_message(self) -> None:
        session = FakeSession()
        prompt_logger = RecordingPromptLogger()
        client = OpenRouterClient(
            session=session,
            prompt_logger=prompt_logger,
        )

        sql = client.generate_sql(
            prompt="complete database prompt",
            api_key="secret",
            model="provider/model",
        )

        self.assertEqual("SELECT 1", sql)
        self.assertIsNotNone(session.request)
        payload = session.request["json"]
        self.assertEqual("provider/model", payload["model"])
        self.assertEqual(
            [{"role": "user", "content": "complete database prompt"}],
            payload["messages"],
        )
        self.assertEqual(
            {"type": "json_object"},
            payload["response_format"],
        )
        self.assertEqual(
            [
                (
                    "prompt",
                    "request-1",
                    "querier",
                    "provider/model",
                    "complete database prompt",
                    None,
                ),
                (
                    "response",
                    "request-1",
                    "querier",
                    "provider/model",
                    '{"sql": "SELECT 1"}',
                ),
            ],
            prompt_logger.records,
        )

    def test_logs_raw_answer_and_error_when_json_is_invalid(self) -> None:
        prompt_logger = RecordingPromptLogger()
        client = OpenRouterClient(
            session=InvalidJsonSession(),
            prompt_logger=prompt_logger,
        )

        with self.assertRaisesRegex(
            OpenRouterError,
            "did not contain JSON",
        ):
            client.generate_sql(
                prompt="database prompt",
                api_key="secret",
                model="provider/model",
                metadata={"attempt_number": 2},
            )

        self.assertEqual(
            "SELECT * FROM data",
            prompt_logger.records[1][4],
        )
        self.assertEqual(
            {"attempt_number": 2},
            prompt_logger.records[0][5],
        )
        error_event = prompt_logger.records[2]
        self.assertEqual("response_validation_failed", error_event[4])
        self.assertEqual("request-1", error_event[1])

    def test_treats_unparseable_json_values_as_validation_failure(self) -> None:
        prompt_logger = RecordingPromptLogger()
        client = OpenRouterClient(
            session=OversizedNumberSession(),
            prompt_logger=prompt_logger,
        )

        with self.assertRaisesRegex(
            OpenRouterError,
            "did not contain JSON",
        ):
            client.generate_sql(
                prompt="database prompt",
                api_key="secret",
                model="provider/model",
            )

        error_event = prompt_logger.records[2]
        self.assertEqual("response_validation_failed", error_event[4])
        self.assertIn("expected", error_event[5])

    def test_generate_json_success(self) -> None:
        session = FakeSession()
        prompt_logger = RecordingPromptLogger()
        client = OpenRouterClient(
            session=session,
            prompt_logger=prompt_logger,
        )

        result = client.generate_json(
            prompt="JSON query prompt",
            api_key="secret",
            model="provider/model",
        )

        self.assertEqual({"sql": "SELECT 1"}, result)
        self.assertIsNotNone(session.request)
        payload = session.request["json"]
        self.assertEqual("provider/model", payload["model"])
        self.assertEqual(
            [{"role": "user", "content": "JSON query prompt"}],
            payload["messages"],
        )
        self.assertEqual(
            {"type": "json_object"},
            payload["response_format"],
        )

    def test_generate_json_invalid_json(self) -> None:
        prompt_logger = RecordingPromptLogger()
        client = OpenRouterClient(
            session=InvalidJsonSession(),
            prompt_logger=prompt_logger,
        )

        with self.assertRaisesRegex(
            OpenRouterError,
            "response did not contain valid JSON",
        ):
            client.generate_json(
                prompt="JSON query prompt",
                api_key="secret",
                model="provider/model",
            )


if __name__ == "__main__":
    unittest.main()
