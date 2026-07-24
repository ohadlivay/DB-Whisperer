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
    baseline = arms["baseline"]
    full = arms["full"]
    full_delta = aggregate["arm_deltas"]["full"]["composite"]
    delta_lower, delta_upper = full_delta["confidence_interval_95"]
    findings = [
        (
            f"Full System composite was {full['composite']['mean']:.2f}/100 "
            f"versus Baseline {baseline['composite']['mean']:.2f}/100 "
            f"(paired difference {full_delta['mean']:+.2f}; 95% CI "
            f"{delta_lower:+.2f} to {delta_upper:+.2f})."
        ),
        (
            "Full System ambiguity recall was "
            f"{full['ambiguity_metrics']['recall']['mean']:.2f}% with "
            f"{full['ambiguity_metrics']['specificity']['mean']:.2f}% "
            "specificity, but final alignment was "
            f"{full['ambiguity_metrics']['final_alignment']['mean']:.2f}%."
        ),
        (
            "Strict query pass rates remained low: Full System "
            f"{full['pass_rate']['mean']:.2f}% and Baseline "
            f"{baseline['pass_rate']['mean']:.2f}%; correctness was "
            f"{full['components']['correctness']['mean']:.2f}% and "
            f"{baseline['components']['correctness']['mean']:.2f}%, "
            "respectively."
        ),
    ]
    return {
        "title": "DB Whisperer Evaluation V3",
        "methodology": {
            "design": "five complete compatible repetitions",
            "ambiguity": "family-macro recall/specificity with frozen scoring weights",
            "bootstrap": (
                "arm metrics/deltas: 2,000-replicate paired, stratified "
                "percentile resampling of repetitions and question families, "
                "seed 20260723"
            ),
            "shared_etl_uncertainty": (
                "2,000-replicate repetition-only percentile bootstrap"
            ),
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
        "findings": findings, "limitations": [
            "Operational usage is campaign-global when per-repetition attribution is unavailable."
        ], "warnings": aggregate.get("relationship_warnings", []),
        "records": records,
    }
