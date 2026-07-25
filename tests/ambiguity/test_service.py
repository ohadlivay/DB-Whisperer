"""Tests for ambiguity judgment."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity import AmbiguityService
from db_whisperer.contracts import (
    AmbiguityRequest,
    ColumnMetadata,
    ComponentState,
    ExecutedQueryPair,
    QueryResult,
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticGrounding,
    SemanticInterpretation,
)


class FakeJudgeClient:
    def __init__(self, judgment: dict[str, object]) -> None:
        self.judgment = judgment
        self.prompts: list[str] = []

    def evaluate(
        self,
        prompt: str,
        api_key: str,
        model: str,
    ) -> dict[str, object]:
        self.prompts.append(prompt)
        return self.judgment


def request() -> AmbiguityRequest:
    return AmbiguityRequest(
        user_query="Summarize the values",
        pairs=(
            ExecutedQueryPair(
                candidate_id="candidate_1",
                sql="SELECT AVG(value) FROM data",
                columns=("avg(value)",),
                rows=((10.0,),),
            ),
            ExecutedQueryPair(
                candidate_id="candidate_2",
                sql="SELECT AVG(value) FROM data WHERE value IS NOT NULL",
                columns=("avg(value)",),
                rows=((10.0,),),
            ),
        ),
        api_key="key",
        model="provider/model",
    )


def semantic_request() -> AmbiguityRequest:
    base = request()
    interpretations = (
        SemanticInterpretation(
            interpretation_id="interpretation_1",
            label="Birth year",
            meaning="Filter by patient birth year.",
            relevance=1,
            grounding=SemanticGrounding(
                tables=("patients",),
                columns=("patients.dob",),
                operations=("filter",),
                temporal_role="birth",
            ),
        ),
        SemanticInterpretation(
            interpretation_id="interpretation_2",
            label="Admission year",
            meaning="Filter by hospital admission year.",
            relevance=2,
            grounding=SemanticGrounding(
                tables=("admissions",),
                columns=("admissions.admittime",),
                operations=("filter",),
                temporal_role="admission",
            ),
        ),
    )
    return AmbiguityRequest(
        user_query="Show patients from 2024",
        pairs=base.pairs,
        api_key=base.api_key,
        model=base.model,
        schema=SchemaMetadata(
            columns=(
                ColumnMetadata("admittime", "TIMESTAMP", "admissions"),
                ColumnMetadata("dob", "TIMESTAMP", "patients"),
            ),
        ),
        semantic_analysis=SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            terms=(
                SemanticAmbiguityTerm(
                    term="from 2024",
                    dimension="temporal_role",
                    interpretations=interpretations,
                ),
            ),
        ),
    )


def clarified_request() -> AmbiguityRequest:
    base = request()
    return AmbiguityRequest(
        user_query=base.user_query,
        pairs=base.pairs,
        api_key=base.api_key,
        model=base.model,
        clarifications=(
            "Question: Should null values be ignored?\n"
            "Selected answer: Ignore null values",
        ),
    )


class AmbiguityServiceTest(unittest.TestCase):
    def test_clarified_pass_classifies_every_alternative(self) -> None:
        decision = AmbiguityService(client=FakeJudgeClient({
            "status": "pass",
            "reason": "One compliant interpretation remains.",
            "compliance": [
                {
                    "alternative_id": "alternative_1",
                    "applies_all": False,
                    "reason": "It does not exclude null values.",
                },
                {
                    "alternative_id": "alternative_2",
                    "applies_all": True,
                    "reason": "It explicitly excludes null values.",
                },
            ],
        })).evaluate(clarified_request())

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertTrue(decision.compliance_passed)
        self.assertEqual(("alternative_2",), decision.compliant_alternatives)
        self.assertEqual(
            (("alternative_1", "It does not exclude null values."),),
            decision.rejected_alternatives,
        )

    def test_noncompliant_status_requires_all_alternatives_rejected(self) -> None:
        decision = AmbiguityService(client=FakeJudgeClient({
            "status": "noncompliant",
            "reason": "Neither query applies the answer.",
            "compliance": [
                {
                    "alternative_id": "alternative_1",
                    "applies_all": False,
                    "reason": "Null handling is unchanged.",
                },
                {
                    "alternative_id": "alternative_2",
                    "applies_all": False,
                    "reason": "The filter does not implement the answer.",
                },
            ],
        })).evaluate(clarified_request())

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.compliance_passed)
        self.assertIsNone(decision.passed)

    def test_clarified_judgment_rejects_missing_compliance_item(self) -> None:
        decision = AmbiguityService(client=FakeJudgeClient({
            "status": "pass",
            "reason": "Looks correct.",
            "compliance": [{
                "alternative_id": "alternative_1",
                "applies_all": True,
                "reason": "It applies the answer.",
            }],
        })).evaluate(clarified_request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("every executed alternative", decision.reason)

    def test_clarified_single_pair_is_evaluated_for_compliance(self) -> None:
        base = clarified_request()
        single = AmbiguityRequest(
            user_query=base.user_query,
            pairs=base.pairs[:1],
            api_key=base.api_key,
            model=base.model,
            clarifications=base.clarifications,
        )
        client = FakeJudgeClient({
            "status": "pass",
            "reason": "The only alternative applies the answer.",
            "compliance": [{
                "alternative_id": "alternative_1",
                "applies_all": True,
                "reason": "The requested filter is present.",
            }],
        })

        decision = AmbiguityService(client=client).evaluate(single)

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertTrue(decision.compliance_passed)
        self.assertEqual(1, len(client.prompts))

    def test_returns_pass(self) -> None:
        client = FakeJudgeClient(
            {"status": "pass", "reason": "Same interpretation."}
        )

        decision = AmbiguityService(client=client).evaluate(request())

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertTrue(decision.passed)
        self.assertIsNone(decision.question)
        self.assertEqual(1, len(client.prompts))

    def test_returns_one_clarification_question(self) -> None:
        client = FakeJudgeClient(
            {
                "status": "clarify",
                "source": "candidate-comparison",
                "alternative_ids": ["alternative_1", "alternative_2"],
                "question": "Should null values be ignored?",
                "options": ["Ignore null values", "Include null values"],
                "reason": "The SQL alternatives handle nulls differently.",
            }
        )

        decision = AmbiguityService(client=client).evaluate(request())

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.passed)
        self.assertEqual(
            "Should null values be ignored?",
            decision.question,
        )
        self.assertEqual(
            ("Ignore null values", "Include null values"),
            decision.options,
        )
        self.assertEqual(
            ("alternative_1", "alternative_2"),
            decision.evidence_alternatives,
        )
        self.assertEqual(
            (("alternative_1", 1), ("alternative_2", 1)),
            decision.candidate_support,
        )

    def test_rejects_missing_question(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({"status": "clarify"})
        ).evaluate(request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIsNone(decision.passed)

    def test_rejects_clarification_without_exactly_two_options(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient(
                {
                    "status": "clarify",
                    "source": "candidate-comparison",
                    "question": "Which scope?",
                    "options": ["All records"],
                }
            )
        ).evaluate(request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("exactly two options", decision.reason)

    def test_rejects_duplicate_clarification_options(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient(
                {
                    "status": "clarify",
                    "source": "candidate-comparison",
                    "question": "Which scope?",
                    "options": ["All records", "all records"],
                }
            )
        ).evaluate(request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("distinct", decision.reason)

    def test_semantic_clarification_uses_stable_id_and_validated_columns(
        self,
    ) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({
                "status": "clarify",
                "source": "semantic-column",
                "semantic_finding_id": "semantic_1",
                "interpretation_ids": [
                    "interpretation_1",
                    "interpretation_2",
                ],
                "candidate_rejection_reason": (
                    "The candidate difference is not a natural reading."
                ),
                "question": "Born in 2024 or admitted in 2024?",
                "options": ["Born in 2024", "Admitted in 2024"],
                "reason": "Candidates use DOB; schema supports admission.",
            })
        ).evaluate(semantic_request())

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.passed)
        self.assertEqual(
            ("interpretation_1", "interpretation_2"),
            decision.evidence_interpretations,
        )
        self.assertEqual(
            ("patients.dob", "admissions.admittime"),
            decision.evidence_columns,
        )
        self.assertEqual("temporal_role", decision.evidence_dimension)
        self.assertIn("patients.dob", decision.question)
        self.assertIn(
            "not a natural reading",
            decision.candidate_rejection_reason,
        )

    def test_semantic_clarification_rejects_display_label_as_id(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({
                "status": "clarify",
                "source": "semantic-column",
                "semantic_finding_id": "semantic_1 (temporal)",
                "interpretation_ids": [
                    "interpretation_1",
                    "interpretation_2",
                ],
                "candidate_rejection_reason": "Candidate distinction is weak.",
                "question": "Which date?",
                "options": ["Birth", "Admission"],
            })
        ).evaluate(semantic_request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("exact finding ID", decision.reason)

    def test_semantic_clarification_rejects_unknown_interpretation(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({
                "status": "clarify",
                "source": "semantic-column",
                "semantic_finding_id": "semantic_1",
                "interpretation_ids": [
                    "interpretation_1",
                    "interpretation_99",
                ],
                "candidate_rejection_reason": "Candidate distinction is weak.",
                "question": "Which date?",
                "options": ["Birth", "Death"],
            })
        ).evaluate(semantic_request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("interpretation IDs", decision.reason)

    def test_candidate_clarification_rejects_unknown_alternative_id(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({
                "status": "clarify",
                "source": "candidate-comparison",
                "alternative_ids": ["alternative_1", "alternative_99"],
                "question": "Which interpretation?",
                "options": ["First", "Second"],
            })
        ).evaluate(request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("alternative IDs", decision.reason)

    def test_semantic_choice_requires_candidate_rejection_rationale(self) -> None:
        decision = AmbiguityService(
            client=FakeJudgeClient({
                "status": "clarify",
                "source": "semantic-column",
                "semantic_finding_id": "semantic_1",
                "interpretation_ids": [
                    "interpretation_1",
                    "interpretation_2",
                ],
                "question": "Born or admitted?",
                "options": ["Born", "Admitted"],
            })
        ).evaluate(semantic_request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("plausibility gate", decision.reason)

    def test_outlier_candidate_can_yield_to_grounded_semantic_finding(
        self,
    ) -> None:
        base = semantic_request()
        candidates = (
            ExecutedQueryPair(
                candidate_id="candidate_1",
                sql=(
                    'SELECT * FROM "patients" WHERE YEAR("dob") = 2024 '
                    'OR YEAR("dod") = 2024'
                ),
                columns=("dob", "dod"),
                rows=(),
            ),
            ExecutedQueryPair(
                candidate_id="candidate_2",
                sql='SELECT * FROM "patients" WHERE YEAR("dob") = 2024',
                columns=("dob", "dod"),
                rows=(),
            ),
            ExecutedQueryPair(
                candidate_id="candidate_3",
                sql='SELECT * FROM "patients" WHERE YEAR("dob") = 2024',
                columns=("dob", "dod"),
                rows=(),
            ),
        )
        request_with_outlier = AmbiguityRequest(
            user_query=base.user_query,
            pairs=candidates,
            api_key=base.api_key,
            model=base.model,
            schema=base.schema,
            semantic_analysis=base.semantic_analysis,
        )
        client = FakeJudgeClient({
            "status": "clarify",
            "source": "semantic-column",
            "semantic_finding_id": "semantic_1",
            "interpretation_ids": [
                "interpretation_1",
                "interpretation_2",
            ],
            "candidate_rejection_reason": (
                "Born-or-deceased is a singleton arbitrary union not "
                "supported by the wording."
            ),
            "question": "Do you mean born or admitted in 2024?",
            "options": ["Born in 2024", "Admitted in 2024"],
        })

        decision = AmbiguityService(client=client).evaluate(
            request_with_outlier
        )

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertEqual("semantic-column", decision.mechanism)
        self.assertEqual(
            (("alternative_1", 1), ("alternative_2", 2)),
            decision.candidate_support,
        )
        self.assertEqual(
            ("patients.dob", "admissions.admittime"),
            decision.evidence_columns,
        )
        self.assertIn("SUPPORT: 1 OF 3 CANDIDATES", client.prompts[0])
        self.assertIn("SUPPORT: 2 OF 3 CANDIDATES", client.prompts[0])

    def test_requires_at_least_two_pairs(self) -> None:
        base = request()
        one_pair = AmbiguityRequest(
            user_query=base.user_query,
            pairs=base.pairs[:1],
            api_key=base.api_key,
            model=base.model,
        )
        client = FakeJudgeClient({"status": "pass"})

        decision = AmbiguityService(client=client).evaluate(one_pair)

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertEqual([], client.prompts)

    def test_skips_llm_when_deduplication_leaves_one_alternative(
        self,
    ) -> None:
        base = request()
        duplicate_result = AmbiguityRequest(
            user_query=base.user_query,
            pairs=(
                base.pairs[0],
                ExecutedQueryPair(
                    candidate_id="candidate_2",
                    sql=base.pairs[0].sql,
                    columns=base.pairs[0].columns,
                    rows=base.pairs[0].rows,
                    truncated=base.pairs[0].truncated,
                ),
            ),
            api_key=base.api_key,
            model=base.model,
        )
        client = FakeJudgeClient(
            {
                "status": "clarify",
                "question": "This should not be called.",
                "options": ["A", "B"],
            }
        )

        decision = AmbiguityService(client=client).evaluate(duplicate_result)

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertTrue(decision.passed)
        self.assertIn("skipped", decision.reason)
        self.assertEqual([], client.prompts)

    def test_rejects_duplicate_candidate_ids(self) -> None:
        base = request()
        duplicate = AmbiguityRequest(
            user_query=base.user_query,
            pairs=(
                base.pairs[0],
                ExecutedQueryPair(
                    candidate_id=base.pairs[0].candidate_id,
                    sql=base.pairs[1].sql,
                    columns=base.pairs[1].columns,
                    rows=base.pairs[1].rows,
                ),
            ),
            api_key=base.api_key,
            model=base.model,
        )
        client = FakeJudgeClient({"status": "pass"})

        decision = AmbiguityService(client=client).evaluate(duplicate)

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertEqual([], client.prompts)

    def test_pair_can_be_created_from_successful_query_result(self) -> None:
        pair = ExecutedQueryPair.from_query_result(
            "candidate_1",
            QueryResult(
                state=ComponentState.ACCEPTED,
                message="ok",
                sql="SELECT 1 AS value",
                columns=("value",),
                rows=((1,),),
            ),
        )

        self.assertEqual("candidate_1", pair.candidate_id)
        self.assertEqual(((1,),), pair.rows)


if __name__ == "__main__":
    unittest.main()
