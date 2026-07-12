"""Aggregate five compatible Evaluation V2 run reports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
from statistics import mean, pstdev
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark_v2.observability import atomic_json
from benchmark_v2.run_evaluation import ARMS, BENCHMARK_DIR


def load_reports(campaign_dir: Path) -> list[dict[str, Any]]:
    paths = sorted((campaign_dir / "run-results").glob("run-*/report.json"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len(reports) != 5:
        raise ValueError(f"Expected five completed reports, found {len(reports)}")
    first = reports[0]
    keys = ("suite", "suite_version", "suite_hash", "model", "candidate_count", "scoring_mode")
    for report in reports[1:]:
        for key in keys:
            if report.get(key) != first.get(key):
                raise ValueError(f"Incompatible report field: {key}")
        if tuple(report.get("arms", {})) != tuple(first.get("arms", {})):
            raise ValueError("Incompatible arm set")
    return reports


def bootstrap_ci(values: list[float], samples: int = 2000) -> list[float]:
    if not values:
        return [0.0, 0.0]
    rng = random.Random(20260712)
    estimates = sorted(mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return [round(estimates[int(samples * 0.025)], 3), round(estimates[int(samples * 0.975)], 3)]


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "mean": round(mean(values), 4) if values else None,
        "stddev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4) if values else None,
        "max": round(max(values), 4) if values else None,
        "confidence_interval_95": bootstrap_ci(values),
    }


def aggregate(campaign_dir: Path) -> dict[str, Any]:
    reports = load_reports(campaign_dir)
    arms: dict[str, Any] = {}
    for arm in ARMS:
        summaries = [report["arms"][arm]["summary"] for report in reports]
        component_names = tuple(summaries[0]["components"])
        arms[arm] = {
            "composite": distribution([float(value["composite"]) for value in summaries]),
            "components": {
                component: distribution([100 * float(value["components"][component]) for value in summaries])
                for component in component_names
            },
            "ambiguity_metrics": {
                metric: distribution([100 * float(value["ambiguity_metrics"][metric]) for value in summaries])
                for metric in summaries[0]["ambiguity_metrics"]
            },
            "runs": summaries,
        }
    total_cost = max(float(report.get("usage", {}).get("cost_usd") or 0) for report in reports)
    first_cases = reports[0]["arms"][ARMS[0]]["cases"]
    cases: list[dict[str, Any]] = []
    for first_case in first_cases:
        case_id = first_case["case_id"]
        arm_values: dict[str, Any] = {}
        for arm in ARMS:
            rows = [
                next(row for row in report["arms"][arm]["cases"] if row["case_id"] == case_id)
                for report in reports
            ]
            arm_values[arm] = {
                "pass_rate": round(100 * sum(bool(row["score"]["passed"]) for row in rows) / len(rows), 2),
                "correctness": distribution([100 * float(row["score"]["correctness"]) for row in rows if row["score"].get("correctness") is not None]),
                "efficiency": distribution([100 * float(row["score"]["efficiency"]) for row in rows if row["score"].get("efficiency") is not None]),
                "grounding": distribution([100 * float(row["score"]["grounding"]) for row in rows if row["score"].get("grounding") is not None]),
                "safety": distribution([100 * float(row["score"]["safety"]) for row in rows if row["score"].get("safety") is not None]),
                "runs": rows,
            }
        cases.append({
            "case_id": case_id,
            "family_id": first_case["family_id"],
            "category": first_case["category"],
            "arms": arm_values,
        })
    return {
        "report_type": "dbwhisperer_v2_aggregate",
        "scoring_mode": "deterministic_scoring_only",
        "campaign_id": campaign_dir.name,
        "suite": reports[0]["suite"],
        "suite_version": reports[0].get("suite_version", "unknown"),
        "suite_hash": reports[0]["suite_hash"],
        "model": reports[0]["model"],
        "candidate_count": reports[0]["candidate_count"],
        "run_count": len(reports),
        "arms": arms,
        "cases": cases,
        "schema": reports[0]["schema"],
        "usage": {"campaign_cost_usd": total_cost},
        "source_reports": [f"run-results/run-{index:02d}/report.json" for index in range(1, 6)],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--output", type=Path, default=BENCHMARK_DIR / "results" / "aggregate" / "evaluation_v2_aggregate.json")
    args = parser.parse_args()
    payload = aggregate(args.campaign_dir.resolve())
    atomic_json(args.output.resolve(), payload)
    print(f"Aggregate report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
