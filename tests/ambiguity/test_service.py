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
    ComponentState,
    ExecutedQueryPair,
    QueryResult,
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


class AmbiguityServiceTest(unittest.TestCase):
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
                    "question": "Which scope?",
                    "options": ["All records", "all records"],
                }
            )
        ).evaluate(request())

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("distinct", decision.reason)

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
