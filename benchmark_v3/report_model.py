"""Presentation-only model shared by the two Evaluation V3 HTML renderers."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import DEFAULT_SUITE
from benchmark_v3.validate_results import validate_aggregate


def build_report_model(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    """Expose already-scored values for renderers without deriving scores."""
    validate_aggregate(aggregate)
    arms = aggregate["arms"]
    suite = load_suite(DEFAULT_SUITE)
    frozen_cases = {case.id: case for case in (*suite.query_cases, *suite.etl_cases)}
    records = []
    for record in aggregate["records"]:
        case = frozen_cases.get(record["case_id"])
        evidence = dict(record)
        if case is not None:
            evidence.update({
                "question": case.question,
                "expected_sql": case.expected_sql,
                "comparison": asdict(case.reference) if case.reference else {"comparison_mode": case.comparison_mode},
                "required_tables": list(case.required_tables),
                "forbidden_tables": list(case.forbidden_tables),
            })
        records.append(evidence)
    return {
        "title": "DB Whisperer Evaluation V3",
        "methodology": {
            "design": "five complete compatible repetitions",
            "ambiguity": "family-macro recall/specificity with frozen scoring weights",
            "bootstrap": "question-family resampling, seed 20260723",
        },
        "provenance": {
            "suite_version": aggregate["suite_version"],
            "suite_hash": aggregate["suite_hash"],
            "model": aggregate["model"],
            "fingerprint": aggregate["fingerprint"],
        },
        "headline_metrics": {arm: arms[arm]["composite"] for arm in arms},
        "arm_cards": arms, "arms": arms, "arm_deltas": aggregate["arm_deltas"],
        "charts": {"composite": {arm: arms[arm]["composite"] for arm in arms}},
        "tables": {"arms": arms, "deltas": aggregate["arm_deltas"]},
        "ambiguity_funnel": {
            arm: arms[arm]["ambiguity_metrics"] for arm in arms
        },
        "shared_etl": aggregate["shared_etl"], "usage": aggregate["usage"],
        "operational": aggregate["operational"],
        "operations": aggregate["operational"],
        "failures": aggregate["failures"], "oracle_reviews": aggregate["oracle_reviews"],
        "cases": records, "evidence": {
            "failures": aggregate["failures"], "oracle_reviews": aggregate["oracle_reviews"],
        },
        "findings": [], "limitations": [
            "Operational usage is campaign-global when per-repetition attribution is unavailable."
        ], "warnings": aggregate.get("relationship_warnings", []),
        "records": records,
    }
