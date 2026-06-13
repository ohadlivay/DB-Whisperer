"""OpenRouter client for ambiguity evaluation."""

from __future__ import annotations

import json
from typing import Any

import requests

from db_whisperer.prompt_logging import PromptLogger, PromptLogSink


class AmbiguityJudgeError(RuntimeError):
    """Raised when the ambiguity judge cannot return usable JSON."""


class AmbiguityOpenRouterClient:
    """Call OpenRouter and return the structured ambiguity judgment."""

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: float = 60,
        prompt_logger: PromptLogSink | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.prompt_logger = prompt_logger or PromptLogger()

    def evaluate(
        self,
        prompt: str,
        api_key: str,
        model: str,
    ) -> dict[str, Any]:
        """Send the ambiguity prompt and parse its JSON object."""
        if not api_key.strip():
            raise AmbiguityJudgeError("OpenRouter API key is required.")
        if not model.strip():
            raise AmbiguityJudgeError("OpenRouter model is required.")

        request_id = self.prompt_logger.log_prompt(
            component="ambiguity",
            model=model,
            prompt=prompt,
        )
        try:
            response = self.session.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                    "X-OpenRouter-Title": "DB Whisperer",
                },
                json={
                    "model": model.strip(),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 2500,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            self.prompt_logger.log_response(
                request_id=request_id,
                component="ambiguity",
                model=model,
                response=content,
            )
            if isinstance(content, str):
                judgment = json.loads(content)
            elif isinstance(content, dict):
                judgment = content
            else:
                raise TypeError("Response content is not JSON text.")
        except (
            requests.RequestException,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            raise AmbiguityJudgeError(
                f"Ambiguity judge request failed: {error}"
            ) from error

        if not isinstance(judgment, dict):
            raise AmbiguityJudgeError(
                "Ambiguity judge returned a non-object response."
            )
        return judgment
