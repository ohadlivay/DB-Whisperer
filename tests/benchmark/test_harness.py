"""Tests for the shared evaluation-harness helpers."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

import _harness  # noqa: E402
from db_whisperer.gui import app as gui_app  # noqa: E402


class ExecuteReferenceTest(unittest.TestCase):
    """The reference executor enforces read-only SQL and a row cap."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.database_path = str(Path(self._temp.name) / "ref.duckdb")
        connection = duckdb.connect(self.database_path)
        try:
            connection.execute(
                "CREATE TABLE nums AS SELECT * FROM range(1, 11) AS t(n)"
            )
        finally:
            connection.close()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_returns_columns_and_rows(self) -> None:
        columns, rows = _harness.execute_reference(
            self.database_path,
            "SELECT n FROM nums WHERE n <= 3 ORDER BY n;",
        )
        self.assertEqual(columns, ("n",))
        self.assertEqual(rows, ((1,), (2,), (3,)))

    def test_rejects_results_over_the_row_cap(self) -> None:
        with self.assertRaises(ValueError):
            _harness.execute_reference(
                self.database_path,
                "SELECT n FROM nums ORDER BY n;",
                max_rows=5,
            )

    def test_rejects_non_select_reference(self) -> None:
        with self.assertRaises(Exception):
            _harness.execute_reference(
                self.database_path,
                "DROP TABLE nums;",
            )


class ExactMatchTest(unittest.TestCase):
    """Exact match requires identical columns and rows in order."""

    def test_identical_tables_match(self) -> None:
        self.assertTrue(
            _harness.exact_match(("a",), ((1,), (2,)), ("a",), ((1,), (2,)))
        )

    def test_row_order_matters(self) -> None:
        self.assertFalse(
            _harness.exact_match(("a",), ((2,), (1,)), ("a",), ((1,), (2,)))
        )

    def test_column_names_matter(self) -> None:
        self.assertFalse(
            _harness.exact_match(("a",), ((1,),), ("b",), ((1,),))
        )


class JudgeTest(unittest.TestCase):
    """The judge call is exercised with an injected ``post``."""

    def _post(self, score_payload: object):
        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {"message": {"content": score_payload}}
                    ]
                }

        def post(*_args, **_kwargs):
            return _Response()

        return post

    def test_parses_valid_score(self) -> None:
        score, reason = _harness.judge(
            "key",
            "judge-model",
            "question",
            {"columns": [], "rows": []},
            {"columns": [], "rows": []},
            post=self._post({"score": 3, "reason": "close"}),
        )
        self.assertEqual(score, 3)
        self.assertEqual(reason, "close")

    def test_rejects_out_of_range_score(self) -> None:
        with self.assertRaises(ValueError):
            _harness.judge(
                "key",
                "judge-model",
                "question",
                {},
                {},
                post=self._post({"score": 9, "reason": "too high"}),
            )

    def test_rejects_extra_keys(self) -> None:
        with self.assertRaises(ValueError):
            _harness.judge(
                "key",
                "judge-model",
                "question",
                {},
                {},
                post=self._post({"score": 2, "reason": "ok", "extra": 1}),
            )


class FormatClarificationTest(unittest.TestCase):
    """The harness must format clarifications exactly like the live GUI."""

    def test_matches_documented_shape(self) -> None:
        self.assertEqual(
            _harness.format_clarification("  Which one? ", " All visits "),
            "Question: Which one?\nSelected answer: All visits",
        )

    def test_matches_gui_implementation(self) -> None:
        # Pin against the GUI's own formatter so the two can never drift; if the
        # GUI changes the prompt shape, this fails and forces a deliberate
        # update on both sides.
        for question, answer in (
            ("Do you mean A or B?", "A"),
            ("  spaced  ", "  answer  "),
        ):
            self.assertEqual(
                _harness.format_clarification(question, answer),
                gui_app._format_clarification(question, answer),
            )


if __name__ == "__main__":
    unittest.main()
