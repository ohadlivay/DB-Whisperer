"""Non-blocking terminal rendering for Evaluation V3 campaigns."""

from __future__ import annotations

from threading import Event, Lock, Thread
from typing import Any, Mapping, TextIO

from benchmark_v3.observability import CampaignObserver


def format_duration(value: Any) -> str:
    """Format seconds as a stable HH:MM:SS value for human monitoring."""

    if value is None:
        return "--:--:--"
    seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class TerminalProgress:
    """Render independent live snapshots without delaying campaign workers."""

    def __init__(
        self,
        observer: CampaignObserver,
        *,
        stream: TextIO,
        interval: float = 1.0,
        interactive: bool | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("progress interval must be positive")
        self.observer = observer
        self.stream = stream
        self.interval = float(interval)
        self._interactive_override = interactive
        self._last_width = 0
        self._stop = Event()
        self._thread = Thread(target=self._render_loop, daemon=True)
        self._lifecycle_lock = Lock()
        self._started = False
        self._stopped = False

    @staticmethod
    def snapshot(status: Mapping[str, Any]) -> str:
        complete = int(status.get("completed_units", 0))
        total = int(status.get("total_units", 0))
        percent = 100.0 * complete / total if total else 0.0
        return (
            f"Overall evaluation {percent:5.1f}% | "
            f"{complete}/{total} tests complete | "
            f"elapsed {format_duration(status.get('elapsed_seconds'))} | "
            f"ETA {format_duration(status.get('eta_seconds'))} | "
            f"passed {status.get('passed', 0)} failed {status.get('failed', 0)} | "
            f"calls {status.get('model_calls', 0)} retries {status.get('retries', 0)} | "
            f"${float(status.get('cost_usd', 0)):.4f}/"
            f"${float(status.get('budget_usd', 0)):.2f}"
        )

    def _interactive(self) -> bool:
        if self._interactive_override is not None:
            return self._interactive_override
        try:
            return bool(self.stream.isatty())
        except (AttributeError, OSError):
            return False

    def render_once(self) -> None:
        self._render(final=False)

    def _render(self, *, final: bool) -> None:
        rendered = self.snapshot(self.observer.snapshot())
        try:
            if self._interactive():
                width = max(self._last_width, len(rendered))
                self._last_width = width
                self.stream.write(
                    "\r" + rendered.ljust(width) + ("\n" if final else "")
                )
            else:
                self.stream.write(rendered + "\n")
            self.stream.flush()
        except OSError:
            # A detached terminal should not interrupt the campaign workers.
            return

    def _render_loop(self) -> None:
        while True:
            self.render_once()
            if self._stop.wait(self.interval):
                break
        self._render(final=True)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError(
                    "TerminalProgress cannot be restarted after it has stopped."
                )
            if self._started:
                return
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            started = self._started
            self._stop.set()
        if started and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        elif not started:
            finalizer = Thread(
                target=self._render,
                kwargs={"final": True},
                daemon=True,
            )
            finalizer.start()
            finalizer.join(timeout=1.0)
