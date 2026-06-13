"""Tests for ambiguity prompt construction."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.prompt_builder import (
    AmbiguityPromptBuilder,
    STATIC_INSTRUCTIONS,
)
from db_whisperer.contracts import AmbiguityRequest, ExecutedQueryPair


class AmbiguityPromptBuilderTest(unittest.TestCase):
    def test_prompt_contains_user_query_and_all_executed_pairs(self) -> None:
        request = AmbiguityRequest(
            user_query="Compare the values",
            pairs=(
                ExecutedQueryPair(
                    candidate_id="candidate_1",
                    sql="SELECT value FROM data",
                    columns=("value",),
                    rows=((1,), (None,), (1,)),
                ),
                ExecutedQueryPair(
                    candidate_id="candidate_2",
                    sql="SELECT value FROM data WHERE value IS NOT NULL",
                    columns=("value",),
                    rows=((1,), (1,)),
                ),
            ),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertTrue(prompt.startswith(STATIC_INSTRUCTIONS))
        self.assertIn(
            '"options": ["<first choice>", "<second choice>"]',
            prompt,
        )
        self.assertIn("USER REQUEST\nCompare the values", prompt)
        self.assertIn("candidate_1", prompt)
        self.assertIn("candidate_2", prompt)
        self.assertIn("SELECT value FROM data", prompt)
        self.assertIn("WHERE value IS NOT NULL", prompt)
        self.assertIn('"rows": 3', prompt)
        self.assertIn('"null_count": 1', prompt)
        self.assertIn('"distinct_count": 1', prompt)

    def test_prompt_bounds_rows_but_preserves_shape(self) -> None:
        pair = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql="SELECT value FROM data",
            columns=("value",),
            rows=tuple((value,) for value in range(5)),
        )
        request = AmbiguityRequest(
            user_query="Show values",
            pairs=(pair, pair),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder(max_rows_per_table=2).build(request)

        self.assertIn('"rows": 5', prompt)
        self.assertIn('"omitted_rows": 3', prompt)


if __name__ == "__main__":
    unittest.main()
