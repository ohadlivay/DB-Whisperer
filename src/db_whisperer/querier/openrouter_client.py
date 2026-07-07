"""OpenRouter chat-completion client."""

from __future__ import annotations

import json
from typing import Any

import requests

from db_whisperer.prompt_logging import PromptLogger, PromptLogSink


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter cannot return a usable SQL response."""


class OpenRouterClient:
    """Generate SQL through OpenRouter's chat-completions API."""

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: float = 60,
        prompt_logger: PromptLogSink | None = None,
    ) -> None:
        self.session = session
        self.timeout_seconds = timeout_seconds
        self.prompt_logger = prompt_logger or PromptLogger()

    def generate_sql(
        self,
        prompt: str,
        api_key: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Send the complete prompt and extract SQL from the JSON response."""
        if not api_key.strip():
            raise OpenRouterError("OpenRouter API key is required.")
        if not model.strip():
            raise OpenRouterError("OpenRouter model is required.")

        request_id = self.prompt_logger.log_prompt(
            component="querier",
            model=model,
            prompt=prompt,
            metadata=metadata,
        )
        try:
            http_client = self.session or requests
            response = http_client.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                    "X-OpenRouter-Title": "DB Whisperer",
                },
                json={
                    "model": model.strip(),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 1.3,
                    "max_tokens": 10000,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            content = payload["choices"][0]["message"]["content"]
            self.prompt_logger.log_response(
                request_id=request_id,
                component="querier",
                model=model,
                response=content,
            )
        except (requests.RequestException, KeyError, IndexError, ValueError) as error:
            self.prompt_logger.log_event(
                event="request_failed",
                component="querier",
                request_id=request_id,
                model=model,
                details={"error": str(error)},
            )
            raise OpenRouterError(f"OpenRouter request failed: {error}") from error

        if not isinstance(content, str):
            self.prompt_logger.log_event(
                event="response_validation_failed",
                component="querier",
                request_id=request_id,
                model=model,
                details={
                    "error": "OpenRouter returned non-text content.",
                    "response_type": type(content).__name__,
                },
            )
            raise OpenRouterError("OpenRouter returned non-text content.")

        try:
            parsed = json.loads(content)
            sql = parsed["sql"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            self.prompt_logger.log_event(
                event="response_validation_failed",
                component="querier",
                request_id=request_id,
                model=model,
                details={
                    "error": str(error),
                    "expected": 'JSON object with a non-empty "sql" field',
                },
            )
            raise OpenRouterError(
                "OpenRouter response did not contain JSON with an SQL field."
            ) from error

        if not isinstance(sql, str) or not sql.strip():
            self.prompt_logger.log_event(
                event="response_validation_failed",
                component="querier",
                request_id=request_id,
                model=model,
                details={"error": "OpenRouter returned empty SQL."},
            )
            raise OpenRouterError("OpenRouter returned empty SQL.")
        return sql.strip()
