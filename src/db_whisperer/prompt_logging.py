"""Persistent logging for language-model prompts and responses."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4


DEFAULT_LOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "logs"
    / "prompts.jsonl"
)
_WRITE_LOCK = Lock()


class PromptLogSink(Protocol):
    """Boundary used by LLM clients to record complete interactions."""

    def log_prompt(
        self,
        component: str,
        model: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record one prompt and return its request ID."""

    def log_response(
        self,
        request_id: str,
        component: str,
        model: str,
        response: Any,
    ) -> None:
        """Record one raw model response for a logged prompt."""

    def log_event(
        self,
        event: str,
        component: str,
        details: dict[str, Any],
        request_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record a structured diagnostic event."""


class PromptLogger:
    """Append complete LLM interactions to a JSON Lines file."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or os.getenv("DB_WHISPERER_PROMPT_LOG")
        self.path = (
            Path(configured_path).expanduser()
            if configured_path
            else DEFAULT_LOG_PATH
        )

    def log_prompt(
        self,
        component: str,
        model: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write one timestamped prompt record without credentials."""
        request_id = str(uuid4())
        record = {
            "id": str(uuid4()),
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "prompt",
            "component": component,
            "model": model.strip(),
            "prompt": prompt,
        }
        if metadata:
            record["metadata"] = metadata
        self._write(record)
        return request_id

    def log_response(
        self,
        request_id: str,
        component: str,
        model: str,
        response: Any,
    ) -> None:
        """Write one raw LLM response correlated with its prompt."""
        record = {
            "id": str(uuid4()),
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "response",
            "component": component,
            "model": model.strip(),
            "response": response,
        }
        self._write(record)

    def log_event(
        self,
        event: str,
        component: str,
        details: dict[str, Any],
        request_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Write a structured diagnostic event."""
        record = {
            "id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "component": component,
            "details": details,
        }
        if request_id:
            record["request_id"] = request_id
        if model:
            record["model"] = model.strip()
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        """Serialize and append one log record."""
        serialized = json.dumps(
            record,
            ensure_ascii=False,
            default=str,
        )

        with _WRITE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as log_file:
                log_file.write(serialized + "\n")
