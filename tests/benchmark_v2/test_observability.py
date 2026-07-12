from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark_v2.observability import CampaignObserver, InstrumentedSession


class ObservabilityTest(unittest.TestCase):
    def test_status_and_events_are_immediately_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), total_units=2, budget_usd=3.75)
            observer.publish(current={"case": "c1", "arm": "full"})
            observer.event("case_arm_started", case="c1", arm="full")
            observer.completed(passed=True)
            status = json.loads((Path(temporary) / "status.json").read_text(encoding="utf-8"))
            event = json.loads((Path(temporary) / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(1, status["completed_units"])
            self.assertEqual("case_arm_started", event["event"])

    def test_prompt_logger_does_not_add_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), 1, 3.75)
            observer.prompt_logger.log_prompt("querier", "model", "safe prompt")
            content = observer.prompt_path.read_text(encoding="utf-8")
            self.assertNotIn("Authorization", content)
            self.assertNotIn("api_key", content)

    def test_streamlit_monitor_renders_and_hides_raw_logs(self) -> None:
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            observer = CampaignObserver(path, 10, 3.75)
            observer.publish(state="running", completed_units=2, current={"case": "c1", "arm": "full"})
            observer.prompt_logger.log_prompt("querier", "model", "SENSITIVE_SAMPLE_MARKER")
            previous = os.environ.get("DBW_V2_CAMPAIGN_DIR")
            os.environ["DBW_V2_CAMPAIGN_DIR"] = str(path)
            try:
                app = AppTest.from_file(Path(__file__).resolve().parents[2] / "benchmark_v2" / "monitor.py")
                app.run(timeout=10)
                self.assertFalse(app.exception)
                rendered = " ".join(item.value for item in app.markdown)
                self.assertNotIn("SENSITIVE_SAMPLE_MARKER", rendered)
                self.assertGreaterEqual(len(app.metric), 6)
            finally:
                if previous is None:
                    os.environ.pop("DBW_V2_CAMPAIGN_DIR", None)
                else:
                    os.environ["DBW_V2_CAMPAIGN_DIR"] = previous

    def test_atomic_status_retries_transient_windows_lock(self) -> None:
        import benchmark_v2.observability as module

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "status.json"
            real_replace = module.os.replace
            attempts = 0

            def flaky_replace(source, target):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("locked")
                return real_replace(source, target)

            with patch.object(module.os, "replace", side_effect=flaky_replace):
                module.atomic_json(destination, {"state": "running"})
            self.assertEqual("running", json.loads(destination.read_text())["state"])
            self.assertEqual(3, attempts)

    def test_resume_reconciles_progress_from_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            first = CampaignObserver(path, 2, 3.75)
            first.checkpoint("one", {"score": {"passed": True}})
            first.checkpoint("two", {"score": {"passed": False}})
            first.publish(completed_units=1, passed=1, failed=0)
            resumed = CampaignObserver(path, 2, 3.75)
            self.assertEqual(2, resumed.status["completed_units"])
            self.assertEqual(1, resumed.status["passed"])
            self.assertEqual(1, resumed.status["failed"])

    def test_closed_stdout_does_not_stop_durable_console_logging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), 1, 3.75)
            with patch("builtins.print", side_effect=OSError(22, "Invalid argument")):
                observer.console("campaign continues")
            content = observer.console_path.read_text(encoding="utf-8")
            self.assertIn("campaign continues", content)

    def test_instrumented_session_uses_thread_local_transports(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        with tempfile.TemporaryDirectory() as temporary:
            observer = CampaignObserver(Path(temporary), 1, 3.75)
            session = InstrumentedSession(observer)
            barrier = Barrier(2)

            def transport_id() -> int:
                transport = session._transport()
                barrier.wait(timeout=2)
                return id(transport)

            with ThreadPoolExecutor(max_workers=2) as executor:
                identifiers = tuple(executor.map(lambda _: transport_id(), range(2)))
            self.assertNotEqual(identifiers[0], identifiers[1])
