"""Tests for the ambiguity OpenRouter boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.openrouter_client import (
    AmbiguityJudgeError,
    AmbiguityOpenRouterClient,
)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "clarify",
                                "question": "Did you mean X or Y?",
                                "options": ["X", "Y"],
                                "reason": "Two interpretations were found.",
                            }
                        )
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


class NullContentResponse(FakeResponse):
    def json(self) -> dict:
        return {
            "id": "generation-1",
            "model": "provider/model",
            "choices": [
                {
                    "finish_reason": "length",
                    "native_finish_reason": "max_tokens",
                    "message": {
                        "content": None,
                        "role": "assistant",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 2500,
                "total_tokens": 2600,
            },
        }


class NullContentSession(FakeSession):
    def post(self, url: str, **kwargs: object) -> NullContentResponse:
        self.request = {"url": url, **kwargs}
        return NullContentResponse()


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
            ("prompt", "request-1", component, model, prompt)
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


class AmbiguityOpenRouterClientTest(unittest.TestCase):
    def test_sends_prompt_and_returns_json_object(self) -> None:
        session = FakeSession()
        prompt_logger = RecordingPromptLogger()
        client = AmbiguityOpenRouterClient(
            session=session,
            prompt_logger=prompt_logger,
        )

        judgment = client.evaluate(
            prompt="ambiguity prompt",
            api_key="secret",
            model="provider/model",
        )

        self.assertEqual("clarify", judgment["status"])
        self.assertEqual("Did you mean X or Y?", judgment["question"])
        self.assertEqual(["X", "Y"], judgment["options"])
        payload = session.request["json"]
        self.assertEqual("provider/model", payload["model"])
        self.assertEqual(
            [{"role": "user", "content": "ambiguity prompt"}],
            payload["messages"],
        )
        self.assertEqual(
            {"type": "json_object"},
            payload["response_format"],
        )
        self.assertEqual(
            ("prompt", "request-1", "ambiguity"),
            prompt_logger.records[0][:3],
        )
        self.assertEqual(
            ("response", "request-1", "ambiguity"),
            prompt_logger.records[1][:3],
        )
        self.assertIn(
            '"options": ["X", "Y"]',
            prompt_logger.records[1][4]["choice"]["message"]["content"],
        )

    def test_reports_token_limit_when_content_is_null(self) -> None:
        prompt_logger = RecordingPromptLogger()
        client = AmbiguityOpenRouterClient(
            session=NullContentSession(),
            prompt_logger=prompt_logger,
        )

        with self.assertRaisesRegex(
            AmbiguityJudgeError,
            "reached its token limit",
        ):
            client.evaluate(
                prompt="ambiguity prompt",
                api_key="secret",
                model="provider/model",
            )

        raw_response = prompt_logger.records[1][4]
        self.assertEqual(
            "length",
            raw_response["choice"]["finish_reason"],
        )
        validation_event = prompt_logger.records[2]
        self.assertEqual(
            "response_validation_failed",
            validation_event[4],
        )
        self.assertEqual(
            "max_tokens",
            validation_event[5]["native_finish_reason"],
        )


if __name__ == "__main__":
    unittest.main()
