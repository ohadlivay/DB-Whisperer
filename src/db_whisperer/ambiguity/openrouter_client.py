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
                    "max_tokens": 10000,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            self.prompt_logger.log_response(
                request_id=request_id,
                component="ambiguity",
                model=model,
                response={
                    "response_id": payload.get("id"),
                    "response_model": payload.get("model"),
                    "choice": choice,
                    "usage": payload.get("usage"),
                },
            )
        except (
            requests.RequestException,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            self.prompt_logger.log_event(
                event="request_failed",
                component="ambiguity",
                request_id=request_id,
                model=model,
                details={"error": str(error)},
            )
            raise AmbiguityJudgeError(
                f"Ambiguity judge request failed: {error}"
            ) from error

        if content is None:
            details = self._response_details(payload, choice, message)
            self.prompt_logger.log_event(
                event="response_validation_failed",
                component="ambiguity",
                request_id=request_id,
                model=model,
                details=details,
            )
            raise AmbiguityJudgeError(
                self._missing_content_message(details)
            )

        if isinstance(content, dict):
            judgment = content
        elif isinstance(content, str):
            try:
                judgment = json.loads(content)
            except json.JSONDecodeError as error:
                details = self._response_details(payload, choice, message)
                details.update(
                    error=str(error),
                    content_preview=content[:500],
                )
                self.prompt_logger.log_event(
                    event="response_validation_failed",
                    component="ambiguity",
                    request_id=request_id,
                    model=model,
                    details=details,
                )
                raise AmbiguityJudgeError(
                    "Ambiguity judge returned text, but it was not valid JSON "
                    f"(finish_reason={details['finish_reason']})."
                ) from error
        else:
            details = self._response_details(payload, choice, message)
            details.update(
                error="Unsupported response content type.",
                response_type=type(content).__name__,
            )
            self.prompt_logger.log_event(
                event="response_validation_failed",
                component="ambiguity",
                request_id=request_id,
                model=model,
                details=details,
            )
            raise AmbiguityJudgeError(
                "Ambiguity judge returned an unsupported content type: "
                f"{type(content).__name__}."
            )

        if not isinstance(judgment, dict):
            raise AmbiguityJudgeError(
                "Ambiguity judge returned a non-object response."
            )
        return judgment

    @staticmethod
    def _response_details(
        payload: dict[str, Any],
        choice: dict[str, Any],
        message: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "finish_reason": choice.get("finish_reason"),
            "native_finish_reason": choice.get("native_finish_reason"),
            "choice_error": choice.get("error"),
            "tool_calls": message.get("tool_calls"),
            "refusal": message.get("refusal"),
            "reasoning": message.get("reasoning"),
            "usage": payload.get("usage"),
            "response_id": payload.get("id"),
            "response_model": payload.get("model"),
        }

    @staticmethod
    def _missing_content_message(details: dict[str, Any]) -> str:
        error = details.get("choice_error")
        if isinstance(error, dict) and error.get("message"):
            return f"OpenRouter provider error: {error['message']}"
        if details.get("tool_calls"):
            return "OpenRouter returned tool calls instead of text content."
        if details.get("refusal"):
            return "The model refused the ambiguity request."
        if details.get("reasoning"):
            return (
                "The model returned reasoning but no final text response."
            )

        finish_reason = details.get("finish_reason")
        if finish_reason == "length":
            return "The ambiguity response reached its token limit."
        if finish_reason == "content_filter":
            return "The ambiguity response was blocked by a content filter."
        if finish_reason == "error":
            return "OpenRouter reported a provider generation error."
        return (
            "OpenRouter returned no text content "
            f"(finish_reason={finish_reason or 'unknown'})."
        )
