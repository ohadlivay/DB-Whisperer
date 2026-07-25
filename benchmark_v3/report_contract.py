"""Reader-facing data contract for Evaluation V3 reports."""

from __future__ import annotations

import math
from typing import Any, Mapping


ARMS = ("baseline", "candidate_only", "semantic_only", "full")
REQUIRED_KEYS = (
    "research_question",
    "experimental_design",
    "methodology",
    "provenance",
    "headline_metrics",
    "arm_deltas",
    "ambiguity_funnel",
    "correctness_diagnostics",
    "projection_diagnostics",
    "terminal_outcomes",
    "case_findings",
    "findings",
    "interpretations",
    "recommendations",
    "limitations",
    "report_readiness",
)
AMBIGUITY_METRICS = (
    "recall",
    "specificity",
    "plausibility",
    "target_coverage",
    "resolution",
    "compliance",
    "final_alignment",
)


def _require(condition: bool, path: str) -> None:
    if not condition:
        raise ValueError(f"report model is missing or invalid: {path}")


def _finite_tree(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        _require(math.isfinite(float(value)), path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _finite_tree(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _finite_tree(item, f"{path}[{index}]")


def validate_report_model(model: Mapping[str, Any]) -> None:
    """Reject a presentation model that cannot populate both reports."""

    for key in REQUIRED_KEYS:
        _require(key in model, key)
    _require(bool(str(model["research_question"]).strip()), "research_question")
    for key in ("experimental_design", "methodology", "provenance"):
        _require(isinstance(model[key], Mapping) and bool(model[key]), key)
    provenance = model["provenance"]
    _require(
        bool(str(provenance.get("result_provenance", "")).strip()),
        "provenance.result_provenance",
    )
    for key in ("headline_metrics", "ambiguity_funnel"):
        value = model[key]
        _require(isinstance(value, Mapping), key)
        _require(tuple(value) == ARMS, key + ".arms")
    for arm in ARMS:
        metrics = model["ambiguity_funnel"][arm]
        _require(isinstance(metrics, Mapping), f"ambiguity_funnel.{arm}")
        for metric in AMBIGUITY_METRICS:
            _require(metric in metrics, f"ambiguity_funnel.{arm}.{metric}")
    _require(
        isinstance(model["terminal_outcomes"], Mapping)
        and bool(model["terminal_outcomes"]),
        "terminal_outcomes",
    )
    case_findings = model["case_findings"]
    _require(isinstance(case_findings, Mapping), "case_findings")
    _require(
        isinstance(case_findings.get("successes"), list),
        "case_findings.successes",
    )
    _require(
        isinstance(case_findings.get("failures"), list),
        "case_findings.failures",
    )
    for key in ("findings", "limitations"):
        _require(isinstance(model[key], list) and bool(model[key]), key)
    for key in ("interpretations", "recommendations"):
        _require(isinstance(model[key], list), key)
    readiness = model["report_readiness"]
    _require(isinstance(readiness, Mapping) and bool(readiness), "report_readiness")
    for key, value in readiness.items():
        _require(value is True, f"report_readiness.{key}")
    operations = model.get("operations")
    _require(isinstance(operations, Mapping), "operations")
    _require(
        isinstance(operations.get("metrics"), Mapping),
        "operations.metrics",
    )
    for key in ("cost_usd", "elapsed_seconds"):
        _require(key in operations["metrics"], f"operations.metrics.{key}")
    _finite_tree(model, "model")
