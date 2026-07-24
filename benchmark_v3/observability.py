"""Durable, secret-safe observation for live Evaluation V3 campaigns."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import re
from tempfile import NamedTemporaryFile
from threading import Lock, RLock, local
from time import perf_counter, sleep
from typing import Any, Callable, Protocol, TypeVar
from uuid import uuid4

import requests


TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_ATOMIC_WRITE_LOCK = Lock()
_SENSITIVE_FIELD_NAMES = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "password",
    "secret",
}
_SECRET_TEXT = re.compile(
    r"(?i)(?:authorization|api[_ -]?key|access[_ -]?token)\s*[:=]\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]+")
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+")
ResponseT = TypeVar("ResponseT")


class WorkItemLike(Protocol):
    """The small runner-facing work-item contract used by observation only."""

    key: str
    repetition: int
    case_id: str
    arm: str
    category: str


class BudgetStop(RuntimeError):
    """Raised before a request when the recorded campaign cost is exhausted."""


class InfrastructureStop(RuntimeError):
    """Raised when evaluation infrastructure cannot produce a valid observation."""


class UsageValidationError(ValueError):
    """Raised when provider usage cannot safely affect campaign budget."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_value(value: Any) -> Any:
    """Recursively remove credentials from durable event and prompt records."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if not _is_sensitive_field(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        text = _SECRET_TEXT.sub("[REDACTED]", value)
        text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
        return _OPENAI_STYLE_KEY.sub("[REDACTED]", text)
    return value


def _is_sensitive_field(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return bool(
        normalized in _SENSITIVE_FIELD_NAMES
        or "authorization" in normalized
        or normalized.endswith("api_key")
        or normalized.endswith("access_token")
    )


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a JSON file, tolerating short Windows file locks."""

    serialized = json.dumps(
        _safe_value(payload),
        indent=2,
        default=str,
        allow_nan=False,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    with _ATOMIC_WRITE_LOCK:
        for attempt in range(8):
            temporary_path: Path | None = None
            try:
                with NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary.write(serialized)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, path)
                return
            except PermissionError as error:
                last_error = error
                sleep(0.025 * (attempt + 1))
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not atomically write {path}")


def initial_status(
    campaign_dir: Path,
    work_items: tuple[WorkItemLike, ...],
    budget_usd: float,
) -> dict[str, Any]:
    """Load a resumable status snapshot or create the V3 status shape."""

    status_path = campaign_dir / "status.json"
    status: dict[str, Any] = {
        "campaign_id": campaign_dir.name,
        "state": "initializing",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "total_units": len(work_items),
        "completed_units": 0,
        "passed": 0,
        "failed": 0,
        "model_calls": 0,
        "retries": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "budget_usd": budget_usd,
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
        "eta_by_arm_category": {},
        "active_by_key": {},
        "active": [],
        "latest_error": "",
        "infrastructure_failure": None,
    }
    if status_path.exists():
        try:
            existing = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                status.update(existing)
                status["state"] = "resuming"
        except (OSError, json.JSONDecodeError):
            pass
    status["total_units"] = len(work_items)
    status["budget_usd"] = float(budget_usd)
    checkpoint_results = _load_checkpoint_results(campaign_dir, work_items)
    checkpoint_keys = set(checkpoint_results)
    checkpoint_passes = sum(checkpoint_results.values())
    completed = len(checkpoint_keys)
    status["completed_units"] = completed
    status["passed"] = checkpoint_passes
    status["failed"] = completed - checkpoint_passes
    # Work from a previous process is no longer active after initialization.
    status["active_by_key"] = {}
    status["active"] = []
    # A new process may resume after credentials or provider availability were
    # repaired. Historical failures remain durable in events.jsonl.
    status["infrastructure_failure"] = None
    return status


def _load_checkpoint_results(
    campaign_dir: Path,
    work_items: tuple[WorkItemLike, ...],
) -> dict[str, bool]:
    """Return pass/fail results only for checkpoints in the current work graph."""

    expected_keys = {item.key for item in work_items}
    results: dict[str, bool] = {}
    for checkpoint in (campaign_dir / "checkpoints").glob("*.json"):
        if checkpoint.stem not in expected_keys:
            continue
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            score = payload.get("score", {})
            passed = (
                score.get("passed", payload.get("passed", False))
                if isinstance(score, dict)
                else payload.get("passed", False)
            )
            results[checkpoint.stem] = bool(passed)
        except (OSError, json.JSONDecodeError):
            continue
    return results


def _validated_nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageValidationError(f"invalid provider usage: {field}")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise UsageValidationError(
            f"invalid provider usage: {field}"
        ) from error
    if not math.isfinite(numeric) or numeric < 0:
        raise UsageValidationError(f"invalid provider usage: {field}")
    return numeric


def _nonnegative_token_count(value: Any, field: str) -> int:
    numeric = _validated_nonnegative_number(value, field)
    if not numeric.is_integer():
        raise UsageValidationError(f"invalid provider usage: {field}")
    return int(numeric)


def _nonnegative_cost(value: Any) -> float:
    return _validated_nonnegative_number(value, "cost_usd")


def _provider_usage(response: requests.Response) -> tuple[int, int, float, str]:
    """Read finite, nonnegative provider usage without retaining response body."""

    try:
        payload = response.json()
    except ValueError:
        return 0, 0, 0.0, ""
    if not isinstance(payload, Mapping):
        raise UsageValidationError("invalid provider usage: response payload")
    raw_usage = payload.get("usage", {})
    if not isinstance(raw_usage, Mapping):
        raise UsageValidationError("invalid provider usage: usage payload")
    raw_cost = (
        raw_usage["cost"]
        if "cost" in raw_usage
        else raw_usage.get("total_cost", 0.0)
    )
    return (
        _nonnegative_token_count(raw_usage.get("prompt_tokens", 0), "prompt_tokens"),
        _nonnegative_token_count(
            raw_usage.get("completion_tokens", 0),
            "completion_tokens",
        ),
        _nonnegative_cost(raw_cost),
        str(payload.get("model", "")),
    )


class _SafePromptLogger:
    """PromptLogSink-compatible JSONL logger that serializes through observer."""

    def __init__(self, observer: CampaignObserver) -> None:
        self._observer = observer

    def log_prompt(
        self,
        component: str,
        model: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        request_id = str(uuid4())
        record: dict[str, Any] = {
            "id": str(uuid4()),
            "request_id": request_id,
            "timestamp": utc_now(),
            "event": "prompt",
            "component": component,
            "model": model.strip(),
            "prompt": prompt,
        }
        if metadata:
            record["metadata"] = metadata
        self._observer._append_jsonl(self._observer.prompt_path, record)
        return request_id

    def log_response(
        self,
        request_id: str,
        component: str,
        model: str,
        response: Any,
    ) -> None:
        self._observer._append_jsonl(self._observer.prompt_path, {
            "id": str(uuid4()),
            "request_id": request_id,
            "timestamp": utc_now(),
            "event": "response",
            "component": component,
            "model": model.strip(),
            "response": response,
        })

    def log_event(
        self,
        event: str,
        component: str,
        details: dict[str, Any],
        request_id: str | None = None,
        model: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "id": str(uuid4()),
            "timestamp": utc_now(),
            "event": event,
            "component": component,
            "details": details,
        }
        if request_id:
            record["request_id"] = request_id
        if model:
            record["model"] = model.strip()
        self._observer._append_jsonl(self._observer.prompt_path, record)
        normalized_event = event.casefold()
        provider_failure_message: str | None = None
        if normalized_event == "request_failed":
            provider_failure_message = str(
                details.get(
                    "error",
                    "provider response could not produce a valid API result",
                )
            )
        elif normalized_event == "response_validation_failed":
            choice_error = details.get("choice_error")
            if isinstance(choice_error, Mapping) and choice_error:
                provider_failure_message = str(
                    choice_error.get("message") or "provider response failed"
                )
            elif choice_error:
                provider_failure_message = str(choice_error)
            elif (
                str(details.get("finish_reason", "")).casefold() == "error"
                or str(details.get("native_finish_reason", "")).casefold()
                == "error"
            ):
                provider_failure_message = (
                    "OpenRouter reported a provider generation error."
                )
        if provider_failure_message is not None:
            self._observer.record_infrastructure_failure(
                source="provider",
                kind="response",
                message=provider_failure_message,
            )
        if "fail" in event.casefold() or "error" in event.casefold():
            latest_error = details.get("error", details.get("message", event))
            self._observer.record_latest_error(latest_error)


class CampaignObserver:
    """Own V3 campaign state, durable evidence, and rolling ETA estimates."""

    def __init__(
        self,
        campaign_dir: Path,
        work_items: tuple[WorkItemLike, ...],
        budget_usd: float,
    ) -> None:
        self.campaign_dir = campaign_dir
        self.work_items = work_items
        self.budget_usd = float(budget_usd)
        self.status_path = campaign_dir / "status.json"
        self.events_path = campaign_dir / "events.jsonl"
        self.console_path = campaign_dir / "console.log"
        self.prompt_path = campaign_dir / "prompts.jsonl"
        self.checkpoint_dir = campaign_dir / "checkpoints"
        self._lock = RLock()
        self._started = perf_counter()
        self.status = initial_status(campaign_dir, work_items, budget_usd)
        self._elapsed_base = float(self.status.get("elapsed_seconds") or 0.0)
        self._durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._remaining_by_bucket = Counter(
            (item.arm, item.category) for item in work_items
        )
        checkpoint_keys = set(_load_checkpoint_results(campaign_dir, work_items))
        for work_item in work_items:
            if (
                work_item.key in checkpoint_keys
                and self._remaining_by_bucket[(work_item.arm, work_item.category)] > 0
            ):
                self._remaining_by_bucket[(work_item.arm, work_item.category)] -= 1
        self.prompt_logger = _SafePromptLogger(self)
        self.publish()

    def _elapsed_seconds(self) -> float:
        return round(self._elapsed_base + perf_counter() - self._started, 3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(
                json.dumps(self.status, default=str, allow_nan=False)
            )

    def _eta_by_arm_category_locked(self) -> dict[str, float]:
        all_durations = [
            duration for values in self._durations.values() for duration in values
        ]
        fallback = sum(all_durations) / len(all_durations) if all_durations else 0.0
        estimates: dict[str, float] = {}
        for bucket, remaining in self._remaining_by_bucket.items():
            samples = self._durations.get(bucket, ())
            average = sum(samples) / len(samples) if samples else fallback
            if samples or (remaining and average):
                estimates[f"{bucket[0]}/{bucket[1]}"] = round(
                    remaining * average,
                    3,
                )
        return estimates

    def _estimate_remaining_seconds_locked(self) -> float | None:
        estimates = self._eta_by_arm_category_locked()
        if estimates:
            return round(sum(estimates.values()), 3)
        if int(self.status.get("completed_units", 0)) >= int(
            self.status.get("total_units", 0)
        ):
            return 0.0
        return None

    def estimate_remaining_seconds(self) -> float | None:
        with self._lock:
            return self._estimate_remaining_seconds_locked()

    def _publish_locked(self, **changes: Any) -> None:
        self.status.update(changes)
        self.status["updated_at"] = utc_now()
        self.status["elapsed_seconds"] = self._elapsed_seconds()
        completed = int(self.status.get("completed_units", 0))
        total = int(self.status.get("total_units", 0))
        self.status["progress"] = round(completed / total, 6) if total else 0.0
        self.status["eta_by_arm_category"] = self._eta_by_arm_category_locked()
        self.status["eta_seconds"] = self._estimate_remaining_seconds_locked()
        atomic_json(self.status_path, self.status)

    def publish(self, **changes: Any) -> None:
        with self._lock:
            self._publish_locked(**changes)

    def activate(self, item: WorkItemLike, phase: str) -> None:
        with self._lock:
            active = dict(self.status.get("active_by_key", {}))
            active[item.key] = {
                "run": item.repetition,
                "case": item.case_id,
                "arm": item.arm,
                "phase": phase,
            }
            self._publish_locked(
                active_by_key=active,
                active=list(active.values()),
            )

    def deactivate(self, item: WorkItemLike) -> None:
        with self._lock:
            active = dict(self.status.get("active_by_key", {}))
            active.pop(item.key, None)
            self._publish_locked(
                active_by_key=active,
                active=list(active.values()),
            )

    def complete_cell(
        self,
        *,
        duration: float,
        arm: str,
        category: str,
        passed: bool,
    ) -> None:
        if duration < 0:
            raise ValueError("cell duration cannot be negative")
        bucket = (arm, category)
        with self._lock:
            self._durations[bucket].append(float(duration))
            if self._remaining_by_bucket[bucket] > 0:
                self._remaining_by_bucket[bucket] -= 1
            self._publish_locked(
                completed_units=int(self.status.get("completed_units", 0)) + 1,
                passed=int(self.status.get("passed", 0)) + int(passed),
                failed=int(self.status.get("failed", 0)) + int(not passed),
            )

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        serialized = json.dumps(
            _safe_value(record),
            ensure_ascii=False,
            default=str,
            allow_nan=False,
        )
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")

    def event(
        self,
        event: str,
        *,
        severity: str = "info",
        **details: Any,
    ) -> None:
        self._append_jsonl(self.events_path, {
            "timestamp": utc_now(),
            "event": event,
            "severity": severity,
            **details,
        })
        if severity == "error":
            self.record_latest_error(details.get("message", details.get("error", event)))

    def record_latest_error(self, error: Any) -> None:
        """Publish one sanitized operator-facing error without unsafe payloads."""

        safe_error = _safe_value(error)
        if isinstance(safe_error, (dict, list)):
            rendered = json.dumps(safe_error, ensure_ascii=False, default=str)
        else:
            rendered = str(safe_error)
        self.publish(latest_error=rendered)

    def record_infrastructure_failure(
        self,
        *,
        source: str,
        kind: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        """Block new model calls without turning an outage into a system score."""

        failure = {
            "source": source,
            "kind": kind,
            "message": str(_safe_value(message)),
            "status_code": status_code,
            "timestamp": utc_now(),
        }
        with self._lock:
            if self.status.get("infrastructure_failure") is None:
                self._publish_locked(
                    state="blocked",
                    infrastructure_failure=failure,
                    latest_error=failure["message"],
                )
        self.event(
            "infrastructure_failure",
            severity="error",
            **failure,
        )

    def current_infrastructure_failure(self) -> dict[str, Any] | None:
        with self._lock:
            failure = self.status.get("infrastructure_failure")
            return dict(failure) if isinstance(failure, Mapping) else None

    def console(self, message: str) -> None:
        safe = str(_safe_value(message)).replace("\r", " ")
        try:
            print(safe, flush=True)
        except OSError:
            pass
        with self._lock:
            self.console_path.parent.mkdir(parents=True, exist_ok=True)
            with self.console_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} {safe}\n")

    def checkpoint(self, key: str, payload: dict[str, Any]) -> Path:
        path = self.checkpoint_dir / f"{key}.json"
        atomic_json(path, payload)
        self.event("checkpoint_written", key=key, checkpoint=str(path))
        return path

    def admit_model_call(self) -> None:
        """Atomically admit and count one transport attempt.

        Provider cost is unknown until a response arrives. Calls admitted while
        recorded cost is below the ceiling may therefore remain in flight and
        report cost later; the network call itself is intentionally not
        serialized so candidate generation keeps its K-way concurrency.
        """

        with self._lock:
            failure = self.status.get("infrastructure_failure")
            if isinstance(failure, Mapping):
                raise InfrastructureStop(str(failure.get("message", "infrastructure failure")))
            if float(self.status.get("cost_usd", 0.0)) >= self.budget_usd:
                raise BudgetStop(
                    "Campaign budget ceiling reached before paid request."
                )
            self._publish_locked(
                model_calls=int(self.status.get("model_calls", 0)) + 1,
            )

    def record_retry(self) -> None:
        with self._lock:
            self._publish_locked(retries=int(self.status.get("retries", 0)) + 1)

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        validated_prompt_tokens = _nonnegative_token_count(
            prompt_tokens,
            "prompt_tokens",
        )
        validated_completion_tokens = _nonnegative_token_count(
            completion_tokens,
            "completion_tokens",
        )
        validated_cost = _nonnegative_cost(cost_usd)
        with self._lock:
            total_prompt_tokens = _nonnegative_token_count(
                int(self.status.get("prompt_tokens", 0))
                + validated_prompt_tokens,
                "prompt_tokens total",
            )
            total_completion_tokens = _nonnegative_token_count(
                int(self.status.get("completion_tokens", 0))
                + validated_completion_tokens,
                "completion_tokens total",
            )
            total_cost = _nonnegative_cost(
                float(self.status.get("cost_usd", 0.0)) + validated_cost
            )
            self._publish_locked(
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                cost_usd=round(total_cost, 8),
            )


def retry_transient(
    operation: Callable[[], ResponseT],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    random_source: random.Random,
    on_retry: Callable[[], None] | None = None,
) -> ResponseT:
    """Retry only connection failures and explicitly transient HTTP responses."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if base_delay < 0:
        raise ValueError("base_delay cannot be negative")
    for attempt in range(attempts):
        try:
            response = operation()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts - 1:
                raise
            if on_retry is not None:
                on_retry()
            delay = min(base_delay * (2 ** attempt), 30.0)
            sleep(delay + random_source.uniform(0.0, min(delay, 1.0)))
            continue
        if (
            int(getattr(response, "status_code", 0)) not in TRANSIENT_STATUS_CODES
            or attempt == attempts - 1
        ):
            return response
        if on_retry is not None:
            on_retry()
        delay = min(base_delay * (2 ** attempt), 30.0)
        sleep(delay + random_source.uniform(0.0, min(delay, 1.0)))
    raise AssertionError("transient retry loop exited unexpectedly")


class InstrumentedSession(requests.Session):
    """Thread-local concurrent transport with atomic budget admission.

    An admitted request can report provider cost after another admitted
    request has reached the recorded ceiling because cost is unavailable until
    responses arrive. Admission is serialized; network I/O is not.
    """

    def __init__(
        self,
        observer: CampaignObserver,
        *,
        attempts: int = 4,
        base_delay: float = 0.5,
        random_source: random.Random | None = None,
    ) -> None:
        super().__init__()
        self.observer = observer
        self.attempts = attempts
        self.base_delay = base_delay
        self.random_source = random_source or random.Random()
        self._thread_local = local()
        self._transports_lock = Lock()
        self._transports: list[requests.Session] = []
        self._closed = False

    def _transport(self) -> requests.Session:
        transport = getattr(self._thread_local, "transport", None)
        if transport is None:
            with self._transports_lock:
                if self._closed:
                    raise RuntimeError("InstrumentedSession is closed.")
                transport = requests.Session()
                self._transports.append(transport)
                self._thread_local.transport = transport
        return transport

    def post(self, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        started = perf_counter()

        def operation() -> requests.Response:
            self.observer.admit_model_call()
            return self._transport().post(url, *args, **kwargs)

        self.observer.event("model_call_started")
        try:
            response = retry_transient(
                operation,
                attempts=self.attempts,
                base_delay=self.base_delay,
                random_source=self.random_source,
                on_retry=self.observer.record_retry,
            )
        except requests.RequestException as error:
            self.observer.record_infrastructure_failure(
                source="provider",
                kind="transport",
                message=str(error),
            )
            self.observer.event(
                "model_call_failed",
                severity="error",
                message=str(error),
            )
            raise
        status_code = int(getattr(response, "status_code", 0))
        if status_code >= 400:
            self.observer.record_infrastructure_failure(
                source="provider",
                kind="http",
                message=f"HTTP {status_code} from model transport.",
                status_code=status_code,
            )
            self.observer.event(
                "model_call_failed",
                severity="error",
                message=f"HTTP {status_code} from model transport.",
                status_code=status_code,
            )
            return response
        try:
            prompt_tokens, completion_tokens, cost_usd, model = _provider_usage(
                response
            )
            self.observer.record_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        except UsageValidationError:
            self.observer.record_infrastructure_failure(
                source="provider",
                kind="usage",
                message="invalid provider usage",
            )
            self.observer.event(
                "model_call_failed",
                severity="error",
                message="invalid provider usage",
            )
            raise
        self.observer.event(
            "model_call_completed",
            response_model=model,
            duration_seconds=round(perf_counter() - started, 4),
            status_code=response.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
        return response

    def close(self) -> None:
        """Close every thread-local transport created by this session."""

        with self._transports_lock:
            self._closed = True
            transports = tuple(self._transports)
            self._transports.clear()
        try:
            for transport in transports:
                transport.close()
        finally:
            super().close()
