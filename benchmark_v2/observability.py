"""Live campaign status, event logging, checkpoints, and usage tracking."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock, local
from time import perf_counter, sleep
from typing import Any

import requests

from db_whisperer.prompt_logging import PromptLogger


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(payload, indent=2, default=str) + "\n"
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, path)
            return
        except PermissionError as error:
            last_error = error
            sleep(0.025 * (attempt + 1))
    # OneDrive and antivirus scanners can transiently hold the destination on
    # Windows. A direct replacement is less atomic but keeps the campaign alive;
    # readers already tolerate a partial/invalid snapshot and retry in 2 seconds.
    try:
        path.write_text(serialized, encoding="utf-8")
        temporary.unlink(missing_ok=True)
    except OSError:
        if last_error is not None:
            raise last_error
        raise


class CampaignObserver:
    def __init__(self, campaign_dir: Path, total_units: int, budget_usd: float) -> None:
        self.campaign_dir = campaign_dir
        self.status_path = campaign_dir / "status.json"
        self.events_path = campaign_dir / "events.jsonl"
        self.console_path = campaign_dir / "console.log"
        self.prompt_path = campaign_dir / "prompts.jsonl"
        self.checkpoint_dir = campaign_dir / "checkpoints"
        self._lock = Lock()
        self._started = perf_counter()
        initial: dict[str, Any] = {
            "campaign_id": campaign_dir.name,
            "state": "initializing",
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "total_units": total_units,
            "completed_units": 0,
            "passed": 0,
            "failed": 0,
            "pending": 0,
            "skipped": 0,
            "model_calls": 0,
            "retries": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "budget_usd": budget_usd,
            "current": {},
            "latest_error": "",
        }
        if self.status_path.exists():
            try:
                existing = json.loads(self.status_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    initial.update(existing)
                    initial["state"] = "resuming"
                    initial["total_units"] = total_units
                    initial["budget_usd"] = budget_usd
            except (OSError, json.JSONDecodeError):
                pass
        checkpoints = list(self.checkpoint_dir.glob("*.json"))
        if checkpoints:
            passed = 0
            for checkpoint in checkpoints:
                try:
                    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                    passed += int(bool(payload.get("score", {}).get("passed")))
                except (OSError, json.JSONDecodeError):
                    continue
            initial["completed_units"] = len(checkpoints)
            initial["passed"] = passed
            initial["failed"] = len(checkpoints) - passed
        self.status = initial
        self.prompt_logger = PromptLogger(self.prompt_path)
        self.publish()

    def publish(self, **changes: Any) -> None:
        with self._lock:
            self.status.update(changes)
            self.status["updated_at"] = utc_now()
            self.status["elapsed_seconds"] = round(perf_counter() - self._started, 3)
            complete = int(self.status.get("completed_units", 0))
            total = int(self.status.get("total_units", 0))
            elapsed = float(self.status["elapsed_seconds"])
            self.status["progress"] = round(complete / total, 6) if total else 0.0
            self.status["eta_seconds"] = (
                round((elapsed / complete) * (total - complete), 1) if complete else None
            )
            atomic_json(self.status_path, self.status)

    def event(self, event: str, *, severity: str = "info", **details: Any) -> None:
        record = {"timestamp": utc_now(), "event": event, "severity": severity, **details}
        serialized = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        if severity == "error":
            self.publish(latest_error=str(details.get("message", event)))

    def console(self, message: str) -> None:
        safe = message.replace("\r", " ")
        # A campaign can outlive the shell or tool session that launched it.
        # On Windows, writing to that closed stdout handle raises OSError 22.
        # Terminal mirroring is best-effort; the durable console log must keep
        # the evaluation alive and remains the authoritative copy.
        try:
            print(safe, flush=True)
        except OSError:
            pass
        with self._lock:
            with self.console_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} {safe}\n")

    def checkpoint(self, key: str, payload: dict[str, Any]) -> Path:
        path = self.checkpoint_dir / f"{key}.json"
        atomic_json(path, payload)
        self.event("checkpoint_written", checkpoint=str(path), key=key)
        return path

    def completed(self, *, passed: bool) -> None:
        changes = {
            "completed_units": int(self.status["completed_units"]) + 1,
            "passed": int(self.status["passed"]) + int(passed),
            "failed": int(self.status["failed"]) + int(not passed),
        }
        self.publish(**changes)

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        """Atomically add usage from parallel candidate requests."""
        with self._lock:
            self.status["model_calls"] = int(self.status.get("model_calls", 0)) + 1
            self.status["prompt_tokens"] = int(self.status.get("prompt_tokens", 0)) + prompt_tokens
            self.status["completion_tokens"] = int(self.status.get("completion_tokens", 0)) + completion_tokens
            self.status["cost_usd"] = round(float(self.status.get("cost_usd", 0.0)) + cost_usd, 8)
            self.status["updated_at"] = utc_now()
            self.status["elapsed_seconds"] = round(perf_counter() - self._started, 3)
            atomic_json(self.status_path, self.status)


class InstrumentedSession(requests.Session):
    """Requests session that captures OpenRouter usage without logging secrets."""

    def __init__(self, observer: CampaignObserver) -> None:
        super().__init__()
        self.observer = observer
        # ApplicationService generates candidates in parallel. requests.Session
        # is mutable and must not be shared by those worker threads; a stalled
        # pool acquisition is not covered by Requests' connect/read timeout.
        self._thread_local = local()

    def _transport(self) -> requests.Session:
        transport = getattr(self._thread_local, "transport", None)
        if transport is None:
            transport = requests.Session()
            self._thread_local.transport = transport
        return transport

    def post(self, url: str, *args: Any, **kwargs: Any):  # type: ignore[override]
        if float(self.observer.status.get("cost_usd", 0.0)) >= float(self.observer.status["budget_usd"]):
            raise requests.RequestException("Campaign budget ceiling reached before request.")
        started = perf_counter()
        self.observer.event("model_call_started")
        response = self._transport().post(url, *args, **kwargs)
        duration = perf_counter() - started
        usage: dict[str, Any] = {}
        response_model = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                response_model = str(payload.get("model", ""))
        except ValueError:
            pass
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost = float(usage.get("cost") or 0.0)
        self.observer.record_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
        self.observer.event(
            "model_call_completed",
            response_model=response_model,
            duration_seconds=round(duration, 4),
            status_code=response.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
        return response
