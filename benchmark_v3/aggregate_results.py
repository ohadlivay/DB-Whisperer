"""Aggregate exactly five compatible Evaluation V3 campaign repetitions."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import random
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

from benchmark_v3.run_evaluation import ARMS
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
COMPONENTS = ("correctness", "efficiency", "safety", "grounding")


def bootstrap_ci(values: Sequence[float], *, samples: int = 2000) -> tuple[float, float]:
    rng = random.Random(BOOTSTRAP_SEED)
    estimates = sorted(mean(rng.choices(tuple(values), k=len(values))) for _ in range(samples))
    return round(estimates[int(samples * 0.025)], 4), round(estimates[min(samples - 1, int(samples * 0.975))], 4)


def distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "stddev": None, "min": None, "max": None, "confidence_interval_95": None}
    return {
        "mean": round(mean(values), 4),
        "stddev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4), "max": round(max(values), 4),
        "confidence_interval_95": list(bootstrap_ci(values)),
    }


def _numeric(score: Mapping[str, Any], name: str) -> float | None:
    value = score.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _per_run_arm(records: Sequence[Mapping[str, Any]], arm: str) -> dict[str, float | None]:
    query = [record for record in records if record["arm"] == arm]
    output: dict[str, float | None] = {
        "pass_rate": 100 * mean(float(record["score"]["passed"]) for record in query),
        "latency_seconds": mean(float(record["duration_seconds"]) for record in query),
    }
    for component in COMPONENTS:
        values = [value for record in query if (value := _numeric(record["score"], component)) is not None]
        if component == "efficiency":
            values = [
                value for record in query
                if _numeric(record["score"], "correctness") == 1.0
                and (value := _numeric(record["score"], component)) is not None
            ]
        output[component] = 100 * mean(values) if values else None
    family_metrics: dict[str, list[float]] = defaultdict(list)
    for record in query:
        ambiguity = record["score"].get("ambiguity", {})
        if ambiguity.get("applicable") is True:
            family_metrics[str(record["family_id"])].append(float(ambiguity.get("detection") is True))
    output["ambiguity_score"] = 100 * mean(mean(values) for values in family_metrics.values()) if family_metrics else None
    components = [output[name] for name in ("ambiguity_score", *COMPONENTS) if output[name] is not None]
    output["composite"] = mean(components) if components else None
    for label, metric in AMBIGUITY_FUNNEL.items():
        by_family: dict[str, list[float]] = defaultdict(list)
        for record in query:
            ambiguity = record["score"].get("ambiguity", {})
            if ambiguity.get("applicable") is True:
                by_family[str(record["family_id"])].append(float(ambiguity.get(metric) is True))
        output[f"ambiguity.{label}"] = 100 * mean(mean(values) for values in by_family.values()) if by_family else None
    return output


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
    per_arm: dict[str, dict[str, list[float]]] = {arm: defaultdict(list) for arm in ARMS}
    etl_scores: list[float] = []
    failures: list[dict[str, Any]] = []
    oracle_reviews: list[dict[str, Any]] = []
    usage = defaultdict(float)
    operational_values: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        for key, value in _usage(report).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] += float(value)
                operational_values[key].append(float(value))
        records = report["records"]
        for arm in ARMS:
            for name, value in _per_run_arm(records, arm).items():
                if value is not None:
                    per_arm[arm][name].append(value)
        for record in records:
            if record["arm"] == "etl":
                etl_scores.append(float(record["score"].get("score", 0.0)) * 100)
            if record["score"]["passed"] is False:
                failures.append(dict(record))
            if record["score"].get("oracle_review") is True:
                oracle_reviews.append(dict(record))
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        metrics = per_arm[arm]
        arms[arm] = {
            "pass_rate": distribution(metrics["pass_rate"]),
            "composite": distribution(metrics["composite"]),
            "components": {name: distribution(metrics[name]) for name in COMPONENTS},
            "ambiguity_metrics": {name.replace("ambiguity.", ""): distribution(metrics[name]) for name in metrics if name.startswith("ambiguity.")},
            "latency_seconds": distribution(metrics["latency_seconds"]),
        }
    baseline = per_arm["baseline"]
    arm_deltas = {
        arm: {name: distribution([value - base for value, base in zip(per_arm[arm][name], baseline[name])]) for name in ("composite", *COMPONENTS)}
        for arm in ARMS if arm != "baseline"
    }
    first = reports[0]
    aggregate = {
        "report_type": "dbwhisperer_v3_aggregate", "complete": True,
        "suite_version": first["suite_version"], "suite_hash": first["suite_hash"],
        "model": first["model"], "fingerprint": first["fingerprint"],
        "arms": arms, "arm_deltas": arm_deltas,
        "shared_etl": distribution(etl_scores), "usage": dict(usage),
        "operational": {
            key: distribution(values) for key, values in operational_values.items()
        },
        "failures": failures, "oracle_reviews": oracle_reviews,
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
