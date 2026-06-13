"""Tests for the ambiguity OpenRouter boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.openrouter_client import (
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


class RecordingPromptLogger:
    def __init__(self) -> None:
        self.records = []

    def log_prompt(self, component, model, prompt) -> str:
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
            prompt_logger.records[1][4],
        )


if __name__ == "__main__":
    unittest.main()
