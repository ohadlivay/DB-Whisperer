from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from unittest.mock import patch

from benchmark_v2.run_evaluation import (
    V2_RETIRED_MESSAGE, build_services, load_env_file, main, run_repetition,
)


class RunnerConfigurationTest(unittest.TestCase):
    def test_build_services_reports_v2_runner_retirement(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "preserved historical experiment.*no longer runnable after join-path ambiguity removal.*benchmark_v3",
        ):
            build_services(observer=None, candidate_count=2)

    def test_load_env_supports_local_key_without_overriding_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("API_KEY='local-value'\n", encoding="utf-8")
            previous = os.environ.get("API_KEY")
            os.environ["API_KEY"] = "shell-value"
            try:
                load_env_file(path)
                self.assertEqual("shell-value", os.environ["API_KEY"])
            finally:
                if previous is None:
                    os.environ.pop("API_KEY", None)
                else:
                    os.environ["API_KEY"] = previous

    def test_main_fails_before_any_v2_side_effect(self) -> None:
        with patch("benchmark_v2.run_evaluation.parse_args", side_effect=AssertionError("args")), patch("benchmark_v2.run_evaluation.load_env_file", side_effect=AssertionError("env")), patch("benchmark_v2.run_evaluation.CampaignObserver", side_effect=AssertionError("observer")):
            with self.assertRaisesRegex(RuntimeError, "preserved historical experiment"):
                main()

    def test_repetition_fails_before_etl_or_status_side_effects(self) -> None:
        with patch("benchmark_v2.run_evaluation.ETLService", side_effect=AssertionError("etl")), patch("benchmark_v2.run_evaluation.Path.mkdir", side_effect=AssertionError("directory")):
            with self.assertRaisesRegex(RuntimeError, "preserved historical experiment"):
                run_repetition(1, None, Path("ignored"), None, "key")
