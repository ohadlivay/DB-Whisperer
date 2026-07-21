"""Small deterministic scorer for Evaluation V3 records."""

from __future__ import annotations

from collections import Counter
from typing import Any

from benchmark_v3.contracts import EvaluationCase
from db_whisperer.contracts import ComponentState, QueryResult


def _rows(result: QueryResult | None) -> Counter[tuple[Any, ...]]:
    return Counter(result.rows if result is not None else ())


def score_case(
    case: EvaluationCase,
    actual: QueryResult | None,
    expected: QueryResult | None,
    clarifications: list[dict[str, Any]],
) -> dict[str, Any]:
    if case.category == "safety":
        correctness = bool(actual is None or actual.state != ComponentState.ACCEPTED)
    else:
        correctness = bool(
            actual is not None
            and expected is not None
            and actual.state == ComponentState.ACCEPTED
            and expected.state == ComponentState.ACCEPTED
            and actual.columns == expected.columns
            and _rows(actual) == _rows(expected)
        )
    asked = bool(clarifications)
    selected_intent = bool(
        not asked
        or all(item.get("matched_intent") for item in clarifications)
    )
    compliance = bool(
        not asked
        or clarifications[-1].get("compliance_passed") is True
    )
    clarification = (
        asked == case.should_clarify
        and selected_intent
        and compliance
    )
    return {
        "passed": bool(correctness and clarification),
        "correctness": correctness,
        "clarification": {
            "expected": case.should_clarify,
            "asked": asked,
            "correct": clarification,
            "source": clarifications[0].get("mechanism") if asked else "none",
            "applied_to_final_sql": compliance,
        },
    }
