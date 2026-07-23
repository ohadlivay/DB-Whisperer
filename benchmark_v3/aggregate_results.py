"""Aggregate exactly five compatible Evaluation V3 campaign repetitions."""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import random
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

from benchmark_v3.run_evaluation import ARMS
from benchmark_v3.scoring import summarize_arm
from benchmark_v3.validate_results import validate_aggregate, validate_reports

BOOTSTRAP_SEED = 20260723
AMBIGUITY_FUNNEL = {
    "recall": "detection",
    "mechanism_accuracy": "mechanism_correct",
    "option_match": "option_match",
    "resolution": "resolution",
    "compliance": "compliance",
    "final_alignment": "final_alignment",
}
COMPONENTS = (
    "ambiguity", "correctness", "efficiency", "safety", "grounding", "etl",
)


def bootstrap_ci(values: Sequence[float], *, samples: int = 2000) -> tuple[float, float]:
    rng = random.Random(BOOTSTRAP_SEED)
    estimates = sorted(mean(rng.choices(tuple(values), k=len(values))) for _ in range(samples))
    return round(estimates[int(samples * 0.025)], 4), round(estimates[min(samples - 1, int(samples * 0.975))], 4)


def distribution(
    values: Sequence[float], *, bootstrap_units: Sequence[float] | None = None,
) -> dict[str, Any]:
    if not values:
        return {"mean": None, "stddev": None, "min": None, "max": None, "confidence_interval_95": None}
    return {
        "mean": round(mean(values), 4),
        "stddev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4), "max": round(max(values), 4),
        "confidence_interval_95": list(bootstrap_ci(bootstrap_units or values)),
    }


def _numeric(score: Mapping[str, Any], name: str) -> float | None:
    value = score.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _per_run_arm(
    records: Sequence[Mapping[str, Any]], arm: str, etl_score: float,
) -> dict[str, float | None]:
    query = [record for record in records if record["arm"] == arm]
    summary = summarize_arm(query, etl_score)
    output: dict[str, float | None] = {
        "pass_rate": 100 * summary["passed_cases"] / summary["case_count"],
        "latency_seconds": mean(float(record["duration_seconds"]) for record in query),
        "composite": float(summary["composite"]),
    }
    for component in COMPONENTS:
        output[component] = 100 * float(summary["components"][component])
    for label, value in summary["ambiguity_metrics"].items():
        output[f"ambiguity.{label}"] = 100 * float(value)
    return output


def _family_units(
    records: Sequence[Mapping[str, Any]], arm: str, etl_score: float,
) -> dict[str, list[float]]:
    """Return bootstrap units: per-question-family frozen summaries."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record["arm"] == arm:
            grouped[str(record["family_id"])].append(record)
    units: dict[str, list[float]] = defaultdict(list)
    for rows in grouped.values():
        summary = summarize_arm(list(rows), etl_score)
        units["composite"].append(float(summary["composite"]))
        for component in COMPONENTS:
            units[component].append(100 * float(summary["components"][component]))
        for label, value in summary["ambiguity_metrics"].items():
            units[f"ambiguity.{label}"].append(100 * float(value))
        units["pass_rate"].append(
            100 * float(summary["passed_cases"]) / float(summary["case_count"])
        )
        units["latency_seconds"].append(
            mean(float(record["duration_seconds"]) for record in rows)
        )
    return units


def _usage(report: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = report.get("usage", {})
    return usage if isinstance(usage, Mapping) else {}


def aggregate_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign_path = campaign_dir / "campaign.json"
    if not campaign_path.exists():
        raise ValueError("campaign metadata is missing")
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(campaign_dir.glob("run-*.json"))]
    validate_reports(reports, campaign)
    status_path = campaign_dir / "status.json"
    if not status_path.exists():
        raise ValueError("authoritative campaign status is missing")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    required_usage = (
        "model_calls", "retries", "prompt_tokens", "completion_tokens",
        "cost_usd", "elapsed_seconds",
    )
    if not isinstance(status, Mapping) or any(key not in status for key in required_usage):
        raise ValueError("authoritative campaign status usage is missing")
    if any(
        isinstance(status[key], bool)
        or not isinstance(status[key], (int, float))
        or not math.isfinite(float(status[key]))
        or float(status[key]) < 0
        for key in required_usage
    ):
        raise ValueError("authoritative campaign status usage must be finite")
    per_arm: dict[str, dict[str, list[float]]] = {arm: defaultdict(list) for arm in ARMS}
    family_units: dict[str, dict[str, list[float]]] = {arm: defaultdict(list) for arm in ARMS}
    etl_scores: list[float] = []
    failures: list[dict[str, Any]] = []
    oracle_reviews: list[dict[str, Any]] = []
    for report in reports:
        records = report["records"]
        etl_score = mean(
            float(record["score"].get("score", 0.0))
            for record in records if record["arm"] == "etl"
        )
        etl_scores.append(etl_score * 100)
        for arm in ARMS:
            for name, value in _per_run_arm(records, arm, etl_score).items():
                if value is not None:
                    per_arm[arm][name].append(value)
            for name, values in _family_units(records, arm, etl_score).items():
                family_units[arm][name].extend(values)
        for record in records:
            if record["score"]["passed"] is False:
                failures.append(dict(record))
            if record["score"].get("oracle_review") is True:
                oracle_reviews.append(dict(record))
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        metrics = per_arm[arm]
        arms[arm] = {
            "pass_rate": distribution(metrics["pass_rate"], bootstrap_units=family_units[arm]["pass_rate"]),
            "composite": distribution(metrics["composite"], bootstrap_units=family_units[arm]["composite"]),
            "components": {name: distribution(metrics[name], bootstrap_units=family_units[arm][name]) for name in COMPONENTS},
            "ambiguity_metrics": {name.replace("ambiguity.", ""): distribution(metrics[name], bootstrap_units=family_units[arm][name]) for name in metrics if name.startswith("ambiguity.")},
            "latency_seconds": distribution(metrics["latency_seconds"], bootstrap_units=family_units[arm]["latency_seconds"]),
        }
    baseline = per_arm["baseline"]
    arm_deltas = {
        arm: {
            name: distribution(
                [value - base for value, base in zip(per_arm[arm][name], baseline[name])],
                bootstrap_units=[
                    value - base for value, base in zip(
                        family_units[arm][name], family_units["baseline"][name],
                    )
                ],
            )
            for name in ("composite", *COMPONENTS)
        }
        for arm in ARMS if arm != "baseline"
    }
    first = reports[0]
    warnings = sorted({
        str(warning)
        for source in (*reports, campaign)
        for warning in source.get("relationship_warnings", ())
    })
    aggregate = {
        "report_type": "dbwhisperer_v3_aggregate", "complete": True,
        "suite_version": first["suite_version"], "suite_hash": first["suite_hash"],
        "model": first["model"], "fingerprint": first["fingerprint"],
        "arms": arms, "arm_deltas": arm_deltas,
        "shared_etl": distribution(etl_scores),
        "usage": {"scope": "campaign_global", **{key: status[key] for key in required_usage}},
        "operational": {
            "scope": "campaign_global", "per_repetition": None,
            "metrics": {key: status[key] for key in required_usage},
        },
        "failures": failures, "oracle_reviews": oracle_reviews,
        "relationship_warnings": warnings,
        "records": [record for report in reports for record in report["records"]],
        "run_reports": reports, "campaign": campaign,
    }
    validate_aggregate(aggregate)
    return aggregate


def aggregate(paths: list[Path]) -> dict[str, Any]:
    """Compatibility wrapper for callers that provide five sibling reports."""
    if not paths:
        raise ValueError("At least one V3 report is required.")
    return aggregate_campaign(paths[0].parent)


__all__ = ["aggregate", "aggregate_campaign", "bootstrap_ci", "distribution", "validate_aggregate"]
