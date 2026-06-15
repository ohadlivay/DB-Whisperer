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
        self.assertIn(
            "=== USER REQUEST ===\nCompare the values\n"
            "=== END USER REQUEST ===",
            prompt,
        )
        self.assertIn(
            "=== PREVIOUS CLARIFICATIONS ===\nNONE\n"
            "=== END PREVIOUS CLARIFICATIONS ===",
            prompt,
        )
        self.assertIn("UNIQUE ALTERNATIVE COUNT: 2", prompt)
        self.assertIn("--- ALTERNATIVE 1 OF 2 ---", prompt)
        self.assertIn("--- ALTERNATIVE 2 OF 2 ---", prompt)
        self.assertIn("candidate_1", prompt)
        self.assertIn("candidate_2", prompt)
        self.assertIn("SQL BEGIN\nSELECT value FROM data\nSQL END", prompt)
        self.assertIn("SELECT value FROM data", prompt)
        self.assertIn("WHERE value IS NOT NULL", prompt)
        self.assertIn('"rows": 3', prompt)
        self.assertIn('"null_count": 1', prompt)
        self.assertIn('"distinct_count": 1', prompt)
        self.assertIn("TABLE SUMMARY BEGIN\n{", prompt)
        self.assertIn("TABLE SUMMARY END", prompt)

    def test_table_summary_uses_indented_json(self) -> None:
        pair = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql="SELECT value FROM data",
            columns=("value",),
            rows=((1,),),
        )
        request = AmbiguityRequest(
            user_query="Show values",
            pairs=(pair, pair),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertIn(
            'TABLE SUMMARY BEGIN\n{\n  "column_statistics":',
            prompt,
        )

    def test_instructions_make_question_discriminate_alternatives(self) -> None:
        instructions = " ".join(STATIC_INSTRUCTIONS.split())

        self.assertIn(
            "Use differences in their SQL and returned tables",
            instructions,
        )
        self.assertIn(
            "specific missing information needed to choose between",
            instructions,
        )
        self.assertIn(
            "Each option must correspond to an interpretation present",
            instructions,
        )
        self.assertIn(
            "single most important two-way distinction",
            instructions,
        )
        self.assertIn(
            "Do not ask the same question again",
            instructions,
        )
        self.assertIn(
            "Treat those answers as part of the user's intent",
            instructions,
        )

    def test_prompt_includes_previous_clarifications(self) -> None:
        request = AmbiguityRequest(
            user_query="Summarize values",
            pairs=(
                ExecutedQueryPair(
                    candidate_id="candidate_1",
                    sql="SELECT AVG(value) FROM data",
                    columns=("avg",),
                    rows=((1.0,),),
                ),
                ExecutedQueryPair(
                    candidate_id="candidate_2",
                    sql=(
                        "SELECT AVG(value) FROM data "
                        "WHERE value IS NOT NULL"
                    ),
                    columns=("avg",),
                    rows=((1.0,),),
                ),
            ),
            api_key="key",
            model="provider/model",
            clarifications=(
                "Question: Should null values be ignored?\n"
                "Selected answer: Ignore null values",
            ),
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertIn("--- CLARIFICATION 1 ---", prompt)
        self.assertIn("Question: Should null values be ignored?", prompt)
        self.assertIn("Selected answer: Ignore null values", prompt)

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

    def test_default_prompt_samples_only_five_rows(self) -> None:
        pair = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql="SELECT value FROM data",
            columns=("value",),
            rows=tuple((value,) for value in range(6)),
        )
        request = AmbiguityRequest(
            user_query="Show values",
            pairs=(pair, pair),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertIn('"rows": 6', prompt)
        self.assertIn('"omitted_rows": 1', prompt)
        self.assertNotIn('"value": 5', prompt)

    def test_exact_duplicate_alternatives_are_shown_once(self) -> None:
        first = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql="SELECT value FROM data",
            columns=("value",),
            rows=((1,), (2,)),
        )
        duplicate = ExecutedQueryPair(
            candidate_id="candidate_2",
            sql=first.sql,
            columns=first.columns,
            rows=first.rows,
        )
        request = AmbiguityRequest(
            user_query="Show values",
            pairs=(first, duplicate),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertIn("UNIQUE ALTERNATIVE COUNT: 1", prompt)
        self.assertIn("CANDIDATE ID: candidate_1", prompt)
        self.assertNotIn("CANDIDATE ID: candidate_2", prompt)

    def test_differences_outside_sample_remain_distinct(self) -> None:
        first = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql="SELECT value FROM data",
            columns=("value",),
            rows=((1,), (2,), (3,)),
        )
        second = ExecutedQueryPair(
            candidate_id="candidate_2",
            sql=first.sql,
            columns=first.columns,
            rows=((1,), (2,), (4,)),
        )
        request = AmbiguityRequest(
            user_query="Show values",
            pairs=(first, second),
            api_key="key",
            model="provider/model",
        )

        prompt = AmbiguityPromptBuilder(
            max_rows_per_table=2
        ).build(request)

        self.assertIn("UNIQUE ALTERNATIVE COUNT: 2", prompt)
        self.assertIn("--- ALTERNATIVE 1 OF 2 ---", prompt)
        self.assertIn("--- ALTERNATIVE 2 OF 2 ---", prompt)


if __name__ == "__main__":
    unittest.main()
