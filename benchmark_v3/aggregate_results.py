"""Aggregate one or more compatible Evaluation V3 reports."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from benchmark_v3.run_evaluation import ARMS


def aggregate(paths: list[Path]) -> dict[str, Any]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not reports:
        raise ValueError("At least one V3 report is required.")
    fingerprint = {(item["suite_version"], item["suite_hash"], item["model"]) for item in reports}
    if len(fingerprint) != 1:
        raise ValueError("V3 reports have incompatible suite/model fingerprints.")
    rows = [row for report in reports for row in report["records"]]
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        passed = sum(bool(row["score"]["passed"]) for row in selected)
        by_arm[arm] = {
            "passed": passed,
            "total": len(selected),
            "rate": round(passed / len(selected), 4) if selected else None,
        }
    return {
        "report_type": "dbwhisperer_v3_aggregate",
        "suite_version": reports[0]["suite_version"],
        "suite_hash": reports[0]["suite_hash"],
        "model": reports[0]["model"],
        "arms": by_arm,
        "records": rows,
    }
