"""JSON and Markdown handoff for pre-publication campaign review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from benchmark_v3.observability import atomic_json
from benchmark_v3.report_contract import validate_report_model
from benchmark_v3.report_model import build_report_model


def _markdown(model: Mapping[str, Any]) -> str:
    sections = [
        "# Campaign Review",
        "",
        "## Validity and provenance",
        "```json",
        json.dumps(model["provenance"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Executive metrics",
        "```json",
        json.dumps(model["headline_metrics"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Arm comparison",
        "```json",
        json.dumps(model["arm_deltas"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Clarification findings",
        "```json",
        json.dumps(model["ambiguity_funnel"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Correctness and projection diagnostics",
        "```json",
        json.dumps({
            "correctness": model["correctness_diagnostics"],
            "projection": model["projection_diagnostics"],
        }, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Terminal outcomes",
        "```json",
        json.dumps(model["terminal_outcomes"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Family and case evidence",
        "```json",
        json.dumps(model["case_findings"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Proposed findings",
        "```json",
        json.dumps(model["findings"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Interpretations",
        *[f"- {value}" for value in model["interpretations"]],
        "",
        "## Recommendations",
        *[f"- {value}" for value in model["recommendations"]],
        "",
        "## Limitations",
        *[f"- {value}" for value in model["limitations"]],
        "",
        "## Report-readiness checklist",
        *[
            f"- [{'x' if value else ' '}] {key}"
            for key, value in model["report_readiness"].items()
        ],
        "",
    ]
    return "\n".join(sections)


def write_review_package(
    aggregate_path: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Write review-safe evidence without invoking an HTML renderer."""

    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    embedded = aggregate.get("model") if isinstance(aggregate, Mapping) else None
    model = dict(embedded) if isinstance(embedded, Mapping) else build_report_model(
        aggregate
    )
    validate_report_model(model)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "review-package.json"
    markdown_path = directory / "review-package.md"
    atomic_json(json_path, model)
    markdown_path.write_text(_markdown(model), encoding="utf-8")
    return json_path, markdown_path
