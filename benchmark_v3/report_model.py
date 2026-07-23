"""Presentation-only model shared by the two Evaluation V3 HTML renderers."""
from __future__ import annotations

from typing import Any, Mapping

from benchmark_v3.validate_results import validate_aggregate


def build_report_model(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    """Expose already-scored values, labels, provenance, tables, and findings."""
    validate_aggregate(aggregate)
    return {
        "title": "DB Whisperer Evaluation V3",
        "provenance": {
            "suite_version": aggregate["suite_version"],
            "suite_hash": aggregate["suite_hash"],
            "model": aggregate["model"],
            "fingerprint": aggregate["fingerprint"],
        },
        "arms": aggregate["arms"], "arm_deltas": aggregate["arm_deltas"],
        "shared_etl": aggregate["shared_etl"], "usage": aggregate["usage"],
        "operational": aggregate["operational"],
        "failures": aggregate["failures"], "oracle_reviews": aggregate["oracle_reviews"],
        "records": aggregate["records"],
    }
