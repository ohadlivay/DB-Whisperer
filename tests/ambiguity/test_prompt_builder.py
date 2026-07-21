"""Tests for ambiguity prompt construction."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.prompt_builder import (
    AmbiguityPromptBuilder,
    SEMANTIC_ONLY_INSTRUCTIONS,
    STATIC_INSTRUCTIONS,
)
from db_whisperer.contracts import (
    AmbiguityRequest,
    ComponentState,
    ExecutedQueryPair,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
)


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
            '"source": "candidate-comparison"',
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
        self.assertIn("ALTERNATIVE 1 OF 2: alternative_1", prompt)
        self.assertIn("ALTERNATIVE 2 OF 2: alternative_2", prompt)
        self.assertIn("SUPPORT: 1 OF 2 CANDIDATES", prompt)
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

    def test_semantic_only_ablation_hides_candidate_evidence(self) -> None:
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

        prompt = AmbiguityPromptBuilder(
            include_candidate_evidence=False
        ).build(request)

        self.assertTrue(prompt.startswith(SEMANTIC_ONLY_INSTRUCTIONS))
        self.assertNotIn("EXECUTED ALTERNATIVES", prompt)
        self.assertNotIn("SELECT value FROM data", prompt)

    def test_semantic_findings_use_stable_ids_and_separate_fields(self) -> None:
        pair = ExecutedQueryPair(
            candidate_id="candidate_1",
            sql='SELECT "dob" FROM "patients"',
            columns=("dob",),
            rows=(),
        )
        request = AmbiguityRequest(
            user_query="Show patients from 2024",
            pairs=(pair, pair),
            api_key="key",
            model="provider/model",
            semantic_analysis=SemanticColumnAnalysis(
                state=ComponentState.ACCEPTED,
                terms=(
                    SemanticAmbiguityTerm(
                        term="from 2024",
                        bucket="temporal",
                        columns=(
                            SemanticColumnCandidate(
                                "patients", "dob", "TIMESTAMP", "temporal"
                            ),
                            SemanticColumnCandidate(
                                "admissions",
                                "admittime",
                                "TIMESTAMP",
                                "temporal",
                            ),
                        ),
                    ),
                ),
            ),
        )

        prompt = AmbiguityPromptBuilder().build(request)

        self.assertIn("SEMANTIC FINDING semantic_1", prompt)
        self.assertIn("TERM: from 2024", prompt)
        self.assertIn("BUCKET: temporal", prompt)
        self.assertNotIn("from 2024 (temporal)", prompt)
        self.assertIn('"semantic_finding_id"', prompt)

    def test_instructions_make_question_discriminate_alternatives(self) -> None:
        instructions = " ".join(STATIC_INSTRUCTIONS.split())

        self.assertIn(
            "Compare only compliant SQL and results",
            instructions,
        )
        self.assertIn(
            "Eligible candidate-derived distinctions take priority",
            instructions,
        )
        self.assertIn(
            "singleton interpretation is eligible only when",
            instructions,
        )
        self.assertIn(
            '"A" versus "A or B"',
            instructions,
        )
        self.assertIn(
            "single most important unresolved two-way distinction",
            instructions,
        )
        self.assertIn(
            "Do not repeat them",
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
        self.assertIn("Clarifications are binding", prompt)
        self.assertIn('"applies_all"', prompt)

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
        self.assertIn("SUPPORT: 2 OF 2 CANDIDATES", prompt)
        self.assertIn("CANDIDATE IDS: candidate_1, candidate_2", prompt)

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
        self.assertIn("ALTERNATIVE 1 OF 2: alternative_1", prompt)
        self.assertIn("ALTERNATIVE 2 OF 2: alternative_2", prompt)


if __name__ == "__main__":
    unittest.main()
