"""Tests for persistent outbound prompt logging."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.prompt_logging import PromptLogger


class PromptLoggerTest(unittest.TestCase):
    def test_writes_complete_prompt_without_api_credentials(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "prompts.jsonl"
            logger = PromptLogger(log_path)

            logger.log_prompt(
                component="querier",
                model="provider/model",
                prompt="DATABASE SCHEMA\nsecret-looking table data",
            )

            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            record = json.loads(lines[0])
            self.assertEqual("prompt", record["event"])
            self.assertEqual("querier", record["component"])
            self.assertEqual("provider/model", record["model"])
            self.assertEqual(
                "DATABASE SCHEMA\nsecret-looking table data",
                record["prompt"],
            )
            self.assertIn("timestamp", record)
            self.assertIn("id", record)
            self.assertIn("request_id", record)
            self.assertNotIn("api_key", record)
            self.assertNotIn("Authorization", record)

    def test_appends_each_prompt_as_a_separate_json_line(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "prompts.jsonl"
            logger = PromptLogger(log_path)

            logger.log_prompt("querier", "model-a", "first")
            logger.log_prompt("ambiguity", "model-b", "second")

            records = [
                json.loads(line)
                for line in log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                ["querier", "ambiguity"],
                [record["component"] for record in records],
            )
            self.assertEqual(
                ["first", "second"],
                [record["prompt"] for record in records],
            )

    def test_correlates_raw_response_with_its_prompt(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "prompts.jsonl"
            logger = PromptLogger(log_path)

            request_id = logger.log_prompt(
                "ambiguity",
                "provider/model",
                "judge this",
            )
            logger.log_response(
                request_id=request_id,
                component="ambiguity",
                model="provider/model",
                response='{"status": "pass"}',
            )

            records = [
                json.loads(line)
                for line in log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(["prompt", "response"], [
                record["event"]
                for record in records
            ])
            self.assertEqual(
                records[0]["request_id"],
                records[1]["request_id"],
            )
            self.assertEqual(
                '{"status": "pass"}',
                records[1]["response"],
            )
            self.assertNotEqual(records[0]["id"], records[1]["id"])

    def test_writes_structured_diagnostic_event(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "prompts.jsonl"
            logger = PromptLogger(log_path)

            logger.log_event(
                event="candidate_execution_failed",
                component="application",
                model="provider/model",
                details={
                    "attempt_number": 2,
                    "sql": "SELECT missing FROM data",
                    "error": "column not found",
                },
            )

            record = json.loads(
                log_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                "candidate_execution_failed",
                record["event"],
            )
            self.assertEqual(
                "SELECT missing FROM data",
                record["details"]["sql"],
            )


if __name__ == "__main__":
    unittest.main()
