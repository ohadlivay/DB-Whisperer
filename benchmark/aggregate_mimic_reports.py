"""Aggregate multiple DBWhisperer MIMIC A/B evaluation reports.

This script does not run the benchmark, call OpenRouter, or read DuckDB files.
It combines saved ``mimic_ab_*.json`` reports into one aggregate artifact that
can be rendered later as the final 10-run evaluation report.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import pstdev
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = BENCHMARK_DIR / "results" / "mimic_ab_aggregate.json"
COMPARISON_KEYS = ("full_better", "tie", "baseline_better", "unscored")


def load_report(path: Path) -> dict[str, Any]:
    """Load one single-run MIMIC report."""
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Report must contain one JSON object: {path}")
    if payload.get("report_type") == "mimic_ab_aggregate":
        raise ValueError(f"Aggregate reports cannot be re-aggregated: {path}")
    if not isinstance(payload.get("cases"), list) or not payload["cases"]:
        raise ValueError(f"Report has no case results: {path}")
    return payload


def load_reports(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    """Load and validate all supplied report paths."""
    if not paths:
        raise ValueError("At least one report path is required.")
    reports = [(path.expanduser().resolve(), load_report(path)) for path in paths]
    _validate_compatible_reports(reports)
    return reports


def _validate_compatible_reports(
    reports: list[tuple[Path, dict[str, Any]]],
) -> None:
    """Ensure reports describe the same benchmark suite and case set."""
    first_path, first = reports[0]
    suite = first.get("suite")
    case_ids = [case.get("id") for case in first.get("cases", [])]
    if not suite or any(not isinstance(case_id, str) for case_id in case_ids):
        raise ValueError(f"Invalid suite or case IDs in {first_path}")

    for path, report in reports[1:]:
        if report.get("suite") != suite:
            raise ValueError(
                f"Report suite mismatch: {path} has {report.get('suite')}, "
                f"expected {suite}"
            )
        current_ids = [case.get("id") for case in report.get("cases", [])]
        if current_ids != case_ids:
            raise ValueError(f"Report case order or IDs do not match: {path}")


def aggregate_reports(
    reports: list[tuple[Path, dict[str, Any]]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Combine single-run reports into one aggregate JSON payload."""
    if not reports:
        raise ValueError("At least one report is required.")
    generated = generated_at or datetime.now(timezone.utc)
    first_report = reports[0][1]
    all_cases = [
        case
        for _, report in reports
        for case in report.get("cases", [])
    ]
    case_ids = [case["id"] for case in first_report["cases"]]

    return {
        "report_type": "mimic_ab_aggregate",
        "generated_at": generated.isoformat(),
        "suite": first_report.get("suite"),
        "dataset": first_report.get("dataset"),
        "tested_model": first_report.get("tested_model"),
        "judge": aggregate_judge_metadata([report for _, report in reports]),
        "run_count": len(reports),
        "source_reports": [
            {
                "path": str(path),
                "run_id": report.get("run_id"),
                "started_at": report.get("started_at"),
                "completed_at": report.get("completed_at"),
                "summary": report.get("summary", {}),
            }
            for path, report in reports
        ],
        "schema": aggregate_schema([report for _, report in reports]),
        "summary": summarize_cases(all_cases),
        "cases": [
            aggregate_case(case_id, [report for _, report in reports])
            for case_id in case_ids
        ],
    }


def aggregate_judge_metadata(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize whether qualitative judging was used across runs."""
    judges = [report.get("judge", {}) for report in reports]
    enabled_count = sum(1 for judge in judges if judge.get("enabled"))
    models = sorted(
        {
            str(judge.get("model"))
            for judge in judges
            if judge.get("model") is not None
        }
    )
    return {
        "enabled_run_count": enabled_count,
        "disabled_run_count": len(reports) - enabled_count,
        "models": models,
        "all_self_judged": bool(judges)
        and all(bool(judge.get("self_judged")) for judge in judges),
    }


def aggregate_schema(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate stable schema-discovery metadata."""
    schemas = [report.get("schema", {}) for report in reports]
    relationship_counts = [
        schema.get("relationship_count")
        for schema in schemas
        if isinstance(schema.get("relationship_count"), int)
    ]
    notes = sorted(
        {
            str(note)
            for schema in schemas
            for note in schema.get("discovery_notes", [])
        }
    )
    return {
        "table_count": schemas[0].get("table_count") if schemas else None,
        "relationship_count_min": min(relationship_counts)
        if relationship_counts
        else None,
        "relationship_count_max": max(relationship_counts)
        if relationship_counts
        else None,
        "discovery_complete_run_count": sum(
            1 for schema in schemas if schema.get("discovery_complete")
        ),
        "discovery_notes": notes,
    }


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scores, comparisons, and clarification rates over rows."""
    ambiguous = [case for case in cases if case.get("ambiguous")]
    control = [case for case in cases if not case.get("ambiguous")]
    return {
        "total_cases": len(cases),
        "baseline": arm_summary(cases, "baseline"),
        "full": arm_summary(cases, "full"),
        "overall_comparison": comparison_counts(cases),
        "ambiguous": group_summary(ambiguous),
        "control": group_summary(control),
        "unreliable_cases": sorted(
            {
                str(case.get("id"))
                for case in cases
                if nested(case, "full", "unreliable")
            }
        ),
    }


def aggregate_case(
    case_id: str,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate all runs for one case ID."""
    case_runs = [
        next(case for case in report["cases"] if case["id"] == case_id)
        for report in reports
    ]
    first = case_runs[0]
    baseline_scores = scores(case_runs, "baseline")
    full_scores = scores(case_runs, "full")
    return {
        "id": case_id,
        "question": first.get("question"),
        "category": first.get("category"),
        "ambiguous": first.get("ambiguous"),
        "ambiguity_type": first.get("ambiguity_type"),
        "should_clarify": first.get("should_clarify"),
        "run_count": len(case_runs),
        "baseline": score_distribution(baseline_scores),
        "full": score_distribution(full_scores),
        "comparison": comparison_counts(case_runs),
        "score_delta": distribution(
            [
                case.get("score_delta")
                for case in case_runs
                if isinstance(case.get("score_delta"), (int, float))
            ]
        ),
        "clarification_asked_count": sum(
            1 for case in case_runs if nested(case, "full", "clarifications")
        ),
        "clarification_rate": rate(
            sum(1 for case in case_runs if nested(case, "full", "clarifications")),
            len(case_runs),
        ),
        "unreliable_count": sum(
            1 for case in case_runs if nested(case, "full", "unreliable")
        ),
        "unreliable_rate": rate(
            sum(1 for case in case_runs if nested(case, "full", "unreliable")),
            len(case_runs),
        ),
        "runs": [
            {
                "run_id": report.get("run_id"),
                "comparison": case.get("comparison"),
                "baseline_score": nested(
                    case, "baseline", "deterministic_score", "score"
                ),
                "full_score": nested(
                    case, "full", "deterministic_score", "score"
                ),
                "clarification_count": len(
                    nested(case, "full", "clarifications") or []
                ),
                "unreliable": bool(nested(case, "full", "unreliable")),
            }
            for report, case in zip(reports, case_runs)
        ],
    }


def arm_summary(cases: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    """Summarize one arm across case rows."""
    return score_distribution(scores(cases, arm))


def group_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one subset of case rows."""
    should_clarify = [case for case in cases if case.get("should_clarify")]
    should_not_clarify = [case for case in cases if not case.get("should_clarify")]
    asked = [case for case in cases if nested(case, "full", "clarifications")]
    return {
        "count": len(cases),
        "baseline": arm_summary(cases, "baseline"),
        "full": arm_summary(cases, "full"),
        "comparison": comparison_counts(cases),
        "clarification_rate": rate(len(asked), len(cases)),
        "expected_clarification_rate": rate(
            len(
                [
                    case for case in should_clarify
                    if nested(case, "full", "clarifications")
                ]
            ),
            len(should_clarify),
        ),
        "spurious_clarification_rate": rate(
            len(
                [
                    case for case in should_not_clarify
                    if nested(case, "full", "clarifications")
                ]
            ),
            len(should_not_clarify),
        ),
    }


def comparison_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    """Count full-vs-baseline outcomes."""
    counts = {key: 0 for key in COMPARISON_KEYS}
    for case in cases:
        comparison = case.get("comparison", "unscored")
        if comparison not in counts:
            comparison = "unscored"
        counts[comparison] += 1
    return counts


def scores(cases: list[dict[str, Any]], arm: str) -> list[int]:
    """Extract deterministic integer scores for one arm."""
    values = [
        nested(case, arm, "deterministic_score", "score")
        for case in cases
    ]
    return [value for value in values if isinstance(value, int)]


def score_distribution(values: list[int]) -> dict[str, Any]:
    """Summarize score values on the 0-4 scale."""
    summary = distribution(values)
    mean_score = summary["mean"]
    return {
        **summary,
        "normalized_percentage": round((mean_score / 4) * 100, 2)
        if mean_score is not None
        else None,
        "exact_score_count": sum(1 for value in values if value == 4),
        "zero_score_count": sum(1 for value in values if value == 0),
    }


def distribution(values: list[int | float]) -> dict[str, Any]:
    """Return count, mean, min, max, and population standard deviation."""
    if not values:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "population_stdev": None,
        }
    mean = sum(values) / len(values)
    return {
        "count": len(values),
        "mean": round(mean, 4),
        "min": min(values),
        "max": max(values),
        "population_stdev": round(pstdev(values), 4)
        if len(values) > 1
        else 0.0,
    }


def rate(numerator: int, denominator: int) -> float | None:
    """Return a rounded rate or None when no denominator exists."""
    return round(numerator / denominator, 4) if denominator else None


def nested(payload: dict[str, Any], *keys: str) -> Any:
    """Safely read a nested dictionary path."""
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def write_aggregate(report_paths: list[Path], output_path: Path) -> Path:
    """Load reports, aggregate them, and write the aggregate JSON."""
    reports = load_reports(report_paths)
    aggregate = aggregate_reports(reports)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(aggregate, indent=2, ensure_ascii=True, default=str) + "\n",
        encoding="utf-8",
    )
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate MIMIC DBWhisperer evaluation JSON reports.",
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = write_aggregate(args.reports, args.output)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Could not aggregate reports: {error}")
        return 2
    print(f"Aggregate report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
