from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from benchmark_v2.run_evaluation import load_env_file


class RunnerConfigurationTest(unittest.TestCase):
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
