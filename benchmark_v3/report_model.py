"""Presentation-only model shared by the two Evaluation V3 HTML renderers."""
from __future__ import annotations

from dataclasses import asdict
from collections import Counter
from typing import Any, Mapping

from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import DEFAULT_SUITE
from benchmark_v3.report_contract import validate_report_model
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
        {
            "finding_id": "full_vs_baseline_composite",
            "kind": "comparative",
            "claim": (
                f"Full System composite was {full['composite']['mean']:.2f}/100 "
                f"versus Baseline {baseline['composite']['mean']:.2f}/100."
            ),
            "evidence": {
                "baseline": baseline["composite"],
                "full": full["composite"],
                "delta": full_delta,
            },
            "caveat": (
                f"The paired 95% interval is {delta_lower:+.2f} to "
                f"{delta_upper:+.2f}; interpret the point difference with it."
            ),
        },
        {
            "finding_id": "full_ambiguity_funnel",
            "kind": "diagnostic",
            "claim": (
                "Full System ambiguity recall was "
                f"{full['ambiguity_metrics']['recall']['mean']:.2f}% and "
                "final alignment was "
                f"{full['ambiguity_metrics']['final_alignment']['mean']:.2f}%."
            ),
            "evidence": full["ambiguity_metrics"],
            "caveat": "Detection alone does not prove resolution or final alignment.",
        },
        {
            "finding_id": "semantic_correctness",
            "kind": "outcome",
            "claim": (
                "Semantic correctness was "
                f"{full['components']['correctness']['mean']:.2f}% for Full "
                f"System and {baseline['components']['correctness']['mean']:.2f}% "
                "for Baseline."
            ),
            "evidence": {
                "baseline": baseline["components"]["correctness"],
                "full": full["components"]["correctness"],
            },
            "caveat": "Join efficiency and projection precision are reported separately.",
        },
    ]
    query_records = [
        record for record in records if record.get("arm") in arms
    ]
    terminal_counts = Counter(
        str(record.get("terminal", {}).get("category", "not_recorded"))
        for record in query_records
    )
    denominator = len(query_records)
    terminal_outcomes = {
        category: {"count": count, "denominator": denominator}
        for category, count in sorted(terminal_counts.items())
    }
    correctness_diagnostics = {}
    projection_diagnostics = {}
    for arm in arms:
        arm_records = [
            record for record in query_records if record.get("arm") == arm
        ]
        comparisons = [
            record.get("score", {}).get("comparison", {})
            for record in arm_records
        ]
        correctness_diagnostics[arm] = {
            "semantic_compatible": sum(
                comparison.get("semantic_compatible") is True
                for comparison in comparisons
            ),
            "incompatible": sum(
                comparison.get("semantic_compatible") is not True
                for comparison in comparisons
            ),
            "denominator": len(arm_records),
        }
        projection_diagnostics[arm] = {
            "records_with_extra_columns": sum(
                bool(comparison.get("extra_columns"))
                for comparison in comparisons
            ),
            "alias_mappings": sum(
                len(comparison.get("aliases_used", ()))
                for comparison in comparisons
            ),
            "duration_representations": dict(Counter(
                str(comparison["duration_representation"])
                for comparison in comparisons
                if comparison.get("duration_representation")
            )),
            "denominator": len(arm_records),
        }
    successes = [
        record for record in query_records
        if record.get("score", {}).get("passed") is True
    ]
    retained_failures = [
        record for record in query_records
        if record.get("score", {}).get("passed") is not True
    ]
    model = {
        "title": "DB Whisperer Evaluation V3",
        "research_question": (
            "How accurately can DB Whisperer translate natural-language "
            "requests into executable schema-aware SQL while using targeted "
            "clarification to reduce unresolved interpretation errors?"
        ),
        "experimental_design": {
            "arms": ["baseline", "candidate_only", "semantic_only", "full"],
            "candidate_count": aggregate["fingerprint"]["candidate_count"],
            "repetitions": 5,
            "query_cases": 22,
            "etl_cases": 2,
            "budget_usd": 3.75,
        },
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
            "result_provenance": "new live-campaign aggregate evidence",
        },
        "headline_metrics": {arm: arms[arm]["composite"] for arm in arms},
        "arm_cards": arms, "arms": arms, "arm_deltas": aggregate["arm_deltas"],
        "charts": {"composite": {arm: arms[arm]["composite"] for arm in arms}},
        "tables": {"arms": arms, "deltas": aggregate["arm_deltas"]},
        "ambiguity_funnel": {
            arm: arms[arm]["ambiguity_metrics"] for arm in arms
        },
        "correctness_diagnostics": correctness_diagnostics,
        "projection_diagnostics": projection_diagnostics,
        "terminal_outcomes": terminal_outcomes,
        "case_findings": {
            "successes": successes[:10],
            "failures": retained_failures[:10],
        },
        "shared_etl": aggregate["shared_etl"], "usage": aggregate["usage"],
        "operational": aggregate["operational"],
        "operations": aggregate["operational"],
        "failures": aggregate["failures"], "oracle_reviews": aggregate["oracle_reviews"],
        "cases": records, "evidence": {
            "failures": aggregate["failures"], "oracle_reviews": aggregate["oracle_reviews"],
        },
        "findings": findings,
        "interpretations": [
            "Composite differences must be interpreted with paired confidence intervals.",
            "Clarification plausibility, target coverage, compliance, and final alignment are distinct stages.",
        ],
        "recommendations": [
            "Review terminal-outcome and case evidence before approving HTML publication.",
            "Use projection precision as a diagnostic rather than a semantic correctness gate.",
        ],
        "limitations": [
            "Operational usage is campaign-global when per-repetition attribution is unavailable."
        ],
        "report_readiness": {
            "validated_aggregate": True,
            "four_arm_metrics": True,
            "ambiguity_funnel": True,
            "correctness_projection_diagnostics": True,
            "terminal_outcomes": True,
            "representative_cases": bool(query_records),
            "provenance_and_operations": True,
            "findings_and_limitations": True,
        },
        "warnings": aggregate.get("relationship_warnings", []),
        "records": records,
    }
    validate_report_model(model)
    return model
