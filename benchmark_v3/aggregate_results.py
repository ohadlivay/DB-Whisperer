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
    estimates = [
        mean(rng.choices(tuple(values), k=len(values)))
        for _ in range(samples)
    ]
    return _percentile_interval(estimates)


def _percentile_interval(
    estimates: Sequence[float],
) -> tuple[float, float]:
    estimates = sorted(estimates)
    samples = len(estimates)
    return round(estimates[int(samples * 0.025)], 4), round(estimates[min(samples - 1, int(samples * 0.975))], 4)


def distribution(
    values: Sequence[float],
    *,
    bootstrap_estimates: Sequence[float] | None = None,
) -> dict[str, Any]:
    if not values:
        return {"mean": None, "stddev": None, "min": None, "max": None, "confidence_interval_95": None}
    return {
        "mean": round(mean(values), 4),
        "stddev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4), "max": round(max(values), 4),
        "confidence_interval_95": list(
            _percentile_interval(bootstrap_estimates)
            if bootstrap_estimates is not None
            else bootstrap_ci(values)
        ),
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


def _bootstrap_campaign_estimates(
    reports: Sequence[Mapping[str, Any]],
    etl_scores: Sequence[float],
    *,
    samples: int = 2000,
) -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, dict[str, list[float]]],
]:
    """Recompute paired statistics after stratified run/family resampling."""

    rows: dict[int, dict[str, dict[str, list[Mapping[str, Any]]]]] = {
        index: {
            arm: defaultdict(list)
            for arm in ARMS
        }
        for index in range(len(reports))
    }
    family_categories: dict[str, set[str]] = defaultdict(set)
    for index, report in enumerate(reports):
        for record in report["records"]:
            arm = str(record["arm"])
            if arm not in ARMS:
                continue
            family = str(record["family_id"])
            rows[index][arm][family].append(record)
            family_categories[family].add(str(record["category"]))

    strata: dict[str, list[str]] = defaultdict(list)
    for family, categories in family_categories.items():
        if categories <= {"ambiguity", "control"}:
            stratum = "ambiguity"
        elif len(categories) == 1:
            stratum = next(iter(categories))
        else:
            raise ValueError(
                f"question family crosses incompatible strata: {family}"
            )
        strata[stratum].append(family)
    for families in strata.values():
        families.sort()

    rng = random.Random(BOOTSTRAP_SEED)
    arm_estimates: dict[str, dict[str, list[float]]] = {
        arm: defaultdict(list) for arm in ARMS
    }
    delta_estimates: dict[str, dict[str, list[float]]] = {
        arm: defaultdict(list) for arm in ARMS if arm != "baseline"
    }
    for _ in range(samples):
        sampled_runs = rng.choices(
            range(len(reports)),
            k=len(reports),
        )
        sampled_families: list[tuple[str, str, int]] = []
        for stratum in sorted(strata):
            families = strata[stratum]
            for occurrence, family in enumerate(
                rng.choices(families, k=len(families))
            ):
                sampled_families.append((stratum, family, occurrence))

        sampled_metrics: dict[str, dict[str, float | None]] = {}
        sampled_etl = mean(
            etl_scores[index] / 100.0
            for index in sampled_runs
        )
        for arm in ARMS:
            sampled_rows: list[Mapping[str, Any]] = []
            for stratum, family, occurrence in sampled_families:
                synthetic_family = (
                    f"{family}#bootstrap-{stratum}-{occurrence}"
                )
                for index in sampled_runs:
                    sampled_rows.extend(
                        {
                            **record,
                            "family_id": synthetic_family,
                        }
                        for record in rows[index][arm][family]
                    )
            metrics = _per_run_arm(sampled_rows, arm, sampled_etl)
            sampled_metrics[arm] = metrics
            for name, value in metrics.items():
                if value is not None:
                    arm_estimates[arm][name].append(value)

        baseline = sampled_metrics["baseline"]
        for arm in ARMS:
            if arm == "baseline":
                continue
            for name in ("composite", *COMPONENTS):
                value = sampled_metrics[arm][name]
                base = baseline[name]
                if value is not None and base is not None:
                    delta_estimates[arm][name].append(value - base)
    return arm_estimates, delta_estimates


def _usage(report: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = report.get("usage", {})
    return usage if isinstance(usage, Mapping) else {}


def aggregate_reports(
    reports: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    excluded_case_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Aggregate validated reports, optionally excluding declared case IDs."""

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
    etl_scores: list[float] = []
    failures: list[dict[str, Any]] = []
    oracle_reviews: list[dict[str, Any]] = []
    for report in reports:
        records = [
            record for record in report["records"]
            if str(record.get("case_id")) not in excluded_case_ids
        ]
        etl_score = mean(
            float(record["score"].get("score", 0.0))
            for record in records if record["arm"] == "etl"
        )
        etl_scores.append(etl_score * 100)
        for arm in ARMS:
            for name, value in _per_run_arm(records, arm, etl_score).items():
                if value is not None:
                    per_arm[arm][name].append(value)
        for record in records:
            if record["score"]["passed"] is False:
                failures.append(dict(record))
            if record["score"].get("oracle_review") is True:
                oracle_reviews.append(dict(record))
    metric_reports = [
        {
            **dict(report),
            "records": [
                record for record in report["records"]
                if str(record.get("case_id")) not in excluded_case_ids
            ],
        }
        for report in reports
    ]
    bootstrap_estimates, bootstrap_deltas = (
        _bootstrap_campaign_estimates(metric_reports, etl_scores)
    )
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        metrics = per_arm[arm]
        arms[arm] = {
            "pass_rate": distribution(metrics["pass_rate"], bootstrap_estimates=bootstrap_estimates[arm]["pass_rate"]),
            "composite": distribution(metrics["composite"], bootstrap_estimates=bootstrap_estimates[arm]["composite"]),
            "components": {name: distribution(metrics[name], bootstrap_estimates=bootstrap_estimates[arm][name]) for name in COMPONENTS},
            "ambiguity_metrics": {name.replace("ambiguity.", ""): distribution(metrics[name], bootstrap_estimates=bootstrap_estimates[arm][name]) for name in metrics if name.startswith("ambiguity.")},
            "latency_seconds": distribution(metrics["latency_seconds"], bootstrap_estimates=bootstrap_estimates[arm]["latency_seconds"]),
        }
    baseline = per_arm["baseline"]
    arm_deltas = {
        arm: {
            name: distribution(
                [value - base for value, base in zip(per_arm[arm][name], baseline[name])],
                bootstrap_estimates=bootstrap_deltas[arm][name],
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
        "run_reports": list(reports), "campaign": dict(campaign),
    }
    validate_aggregate(aggregate)
    return aggregate


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
    return aggregate_reports(reports, campaign, status)


def aggregate(paths: list[Path]) -> dict[str, Any]:
    """Compatibility wrapper for callers that provide five sibling reports."""
    if not paths:
        raise ValueError("At least one V3 report is required.")
    return aggregate_campaign(paths[0].parent)


__all__ = [
    "aggregate",
    "aggregate_campaign",
    "aggregate_reports",
    "bootstrap_ci",
    "distribution",
    "validate_aggregate",
]
