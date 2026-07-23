"""Aggregate corrected Evaluation V3 reports into transparent arm summaries."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

from benchmark_v3.contracts import ARMS, current_id


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _rate(rows: list[dict[str, Any]], predicate: Any) -> float:
    return _mean([float(predicate(row)) for row in rows])


def _failure_reasons(row: dict[str, Any]) -> list[str]:
    score = row["score"]
    reasons = [
        check["name"]
        for section in ("sql_contract", "result_contract")
        for check in score.get(section, {}).get("checks", [])
        if not check.get("passed")
    ]
    clarification = score.get("clarification", {})
    if clarification.get("expected") and clarification.get("applicable", True):
        if not clarification.get("asked"):
            reasons.append("clarification:not_asked")
        elif not clarification.get("intent_matched"):
            reasons.append("clarification:intent_not_matched")
        elif not clarification.get("applied_to_final_sql"):
            reasons.append("clarification:not_applied")
    elif not clarification.get("expected") and clarification.get("asked"):
        reasons.append("clarification:unnecessary")
    safety = score.get("safety")
    if safety:
        if not safety["containment"]:
            reasons.append("safety:containment")
        if not safety["refusal_fidelity"]:
            reasons.append("safety:refusal_fidelity")
    return reasons


def _arm_summary(arm: str, rows: list[dict[str, Any]], retrospective: bool) -> dict[str, Any]:
    ambiguous = [row for row in rows if row["score"]["clarification"]["expected"]]
    controls = [row for row in rows if row["category"] == "control"]
    query_rows = [row for row in rows if row["category"] != "safety"]
    safety_rows = [row for row in rows if row["category"] == "safety"]
    detection = _rate(ambiguous, lambda row: row["score"]["clarification"]["asked"])
    intent = _rate(ambiguous, lambda row: row["score"]["clarification"].get("intent_matched", False))
    resolved = _rate(
        ambiguous,
        lambda row: row["score"]["clarification"].get("intent_matched", False)
        and row["score"]["clarification"].get("applied_to_final_sql", False),
    )
    ambiguity = 0.0 if arm == "baseline" else _mean([detection, intent, resolved])
    correctness = _rate(query_rows, lambda row: row["score"]["correctness"])
    specificity = _rate(controls, lambda row: not row["score"]["clarification"]["asked"])
    safety = _mean([
        float(row["score"].get("safety", {}).get("behavior", 0.0))
        for row in safety_rows
    ])
    components = {
        "answer_correctness": round(100 * correctness, 2),
        "ambiguity_resolution": round(100 * ambiguity, 2),
        "control_specificity": round(100 * specificity, 2),
        "safety_behavior": round(100 * safety, 2),
    }
    component_counts = {
        "answer_correctness": f"{sum(bool(row['score']['correctness']) for row in query_rows)}/{len(query_rows)}",
        "ambiguity_resolution": (
            "N/A" if arm == "baseline" else
            f"detected {sum(bool(row['score']['clarification']['asked']) for row in ambiguous)}/{len(ambiguous)}; "
            f"intent {sum(bool(row['score']['clarification'].get('intent_matched')) for row in ambiguous)}/{len(ambiguous)}; "
            f"resolved {sum(bool(row['score']['clarification'].get('intent_matched') and row['score']['clarification'].get('applied_to_final_sql')) for row in ambiguous)}/{len(ambiguous)}"
        ),
        "control_specificity": f"{sum(not row['score']['clarification']['asked'] for row in controls)}/{len(controls)}",
        "safety_behavior": (
            f"containment {sum(bool(row['score']['safety']['containment']) for row in safety_rows)}/{len(safety_rows)}; "
            f"refusal {sum(bool(row['score']['safety']['refusal_fidelity']) for row in safety_rows)}/{len(safety_rows)}"
        ),
    }
    passed = sum(bool(row["score"]["passed"]) for row in rows)
    original_passed = (
        sum(bool(row.get("original_score", {}).get("passed")) for row in rows)
        if retrospective else None
    )
    mechanisms = Counter(
        row["score"]["clarification"].get("source", "none")
        for row in ambiguous if row["score"]["clarification"].get("asked")
    )
    return {
        "passed": passed,
        "total": len(rows),
        "rate": round(passed / len(rows), 4) if rows else None,
        "original_passed": original_passed,
        "composite": round(_mean(list(components.values())), 2),
        "components": components,
        "component_counts": component_counts,
        "ambiguity_metrics": {
            "detection_recall": round(100 * detection, 2),
            "intended_option_match": round(100 * intent, 2),
            "compliant_resolution": round(100 * resolved, 2),
        },
        "ambiguity_counts": {
            "detection_recall": f"{sum(bool(row['score']['clarification']['asked']) for row in ambiguous)}/{len(ambiguous)}",
            "intended_option_match": f"{sum(bool(row['score']['clarification'].get('intent_matched')) for row in ambiguous)}/{len(ambiguous)}",
            "compliant_resolution": f"{sum(bool(row['score']['clarification'].get('intent_matched') and row['score']['clarification'].get('applied_to_final_sql')) for row in ambiguous)}/{len(ambiguous)}",
        },
        "safety_metrics": {
            "containment": round(100 * _rate(safety_rows, lambda row: row["score"]["safety"]["containment"]), 2),
            "refusal_fidelity": round(100 * _rate(safety_rows, lambda row: row["score"]["safety"]["refusal_fidelity"]), 2),
        },
        "mechanisms": dict(mechanisms),
        "failure_reasons": dict(Counter(reason for row in rows for reason in _failure_reasons(row))),
    }


def aggregate(paths: list[Path]) -> dict[str, Any]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not reports:
        raise ValueError("At least one V3 report is required.")
    report_types = {"dbwhisperer_v3_evaluation", "dbwhisperer_v3_rescored"}
    if any(report.get("report_type") not in report_types for report in reports):
        raise ValueError("Expected final or retrospectively rescored V3 reports.")
    retrospective = any(bool(report.get("retrospective")) for report in reports)
    if retrospective != all(bool(report.get("retrospective")) for report in reports):
        raise ValueError("Final and retrospective reports cannot be aggregated together.")
    fingerprint = {(item["suite_version"], item["suite_hash"], item["model"], item["scoring_version"]) for item in reports}
    if len(fingerprint) != 1:
        raise ValueError("V3 reports have incompatible suite/model/scoring fingerprints.")
    rows = [row for report in reports for row in report["records"]]
    etl_rows = [row for report in reports for row in report.get("etl", [])]
    arms = {
        arm: _arm_summary(arm, [row for row in rows if row["arm"] == arm], retrospective)
        for arm in ARMS
    }
    case_ids = tuple(dict.fromkeys(current_id(row["case_id"]) for row in rows))
    contracts = {
        current_id(item["id"]): item
        for item in reports[0].get("case_contracts", [])
    }
    cases = []
    for case_id in case_ids:
        case_rows = [row for row in rows if current_id(row["case_id"]) == case_id]
        cases.append({
            "case_id": case_id,
            "family_id": current_id(case_rows[0]["family_id"]),
            "category": case_rows[0]["category"],
            "contract": contracts.get(case_id, {}),
            "arms": {
                arm: {
                    "passed": sum(bool(row["score"]["passed"]) for row in selected),
                    "total": len(selected),
                    "correctness": round(100 * _rate(selected, lambda row: row["score"]["correctness"]), 2),
                    "clarification_rate": round(100 * _rate(selected, lambda row: row["score"]["clarification"]["asked"]), 2),
                    "resolution": round(100 * _rate(selected, lambda row: row["score"]["clarification"].get("intent_matched", False) and row["score"]["clarification"].get("applied_to_final_sql", False)), 2),
                    "failure_reasons": dict(Counter(reason for row in selected for reason in _failure_reasons(row))),
                    "runs": selected,
                }
                for arm in ARMS
                if (selected := [row for row in case_rows if row["arm"] == arm])
            },
        })
    return {
        "report_type": "dbwhisperer_v3_aggregate",
        "retrospective": retrospective,
        "scoring_version": reports[0]["scoring_version"],
        "suite_version": reports[0]["suite_version"],
        "suite_hash": reports[0]["suite_hash"],
        "model": reports[0]["model"],
        "run_count": len({row.get("run") for row in rows}),
        "case_count": len(case_ids),
        "unique_prompt_count": len({item.get("question") for item in contracts.values()}),
        "evaluation_count": len(rows),
        "arms": arms,
        "cases": cases,
        "etl": {
            "passed": sum(bool(row["score"]["passed"]) for row in etl_rows),
            "total": len(etl_rows),
            "records": etl_rows,
        },
        "records": rows,
        "source_reports": [str(path) for path in paths],
    }
