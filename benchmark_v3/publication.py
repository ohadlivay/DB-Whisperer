"""Hash-bound approval and atomic HTML publication for Evaluation V3."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any

from benchmark_v3.contracts import load_suite
from benchmark_v3.observability import _safe_value, atomic_json
from benchmark_v3.report_model import build_report_model
from benchmark_v3.report_contract import validate_report_model
from benchmark_v3.run_evaluation import (
    DEFAULT_SUITE,
    PROJECT_ROOT,
    _promote_publication,
)
from benchmark_v3.validate_results import validate_aggregate


def sha256_file(path: str | Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def _require_official_suite(
    directory: Path,
    aggregate: dict[str, Any] | None = None,
) -> None:
    expected = load_suite(DEFAULT_SUITE).sha256
    campaign_path = directory / "campaign.json"
    if campaign_path.is_file():
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        if campaign.get("suite_hash") != expected:
            raise ValueError(
                "official publication requires the frozen default suite hash"
            )
    if aggregate is not None and aggregate.get("suite_hash") != expected:
        raise ValueError(
            "official publication requires the frozen default suite hash"
        )


def approve_campaign(
    campaign_dir: str | Path,
    *,
    approved_by: str,
) -> Path:
    """Bind explicit approval to the current aggregate bytes and campaign ID."""

    directory = Path(campaign_dir).resolve()
    aggregate_path = directory / "aggregate.json"
    review_json = directory / "review-package.json"
    review_markdown = directory / "review-package.md"
    if not aggregate_path.is_file():
        raise ValueError("validated aggregate is missing")
    if not review_json.is_file() or not review_markdown.is_file():
        raise ValueError("review package must exist before approval")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    _require_official_suite(directory, aggregate)
    validate_aggregate(aggregate)
    model = build_report_model(aggregate)
    validate_report_model(model)
    reviewed_model = json.loads(review_json.read_text(encoding="utf-8"))
    normalized_model = json.loads(
        json.dumps(_safe_value(model), default=str, allow_nan=False)
    )
    if reviewed_model != normalized_model:
        raise ValueError("review package JSON does not match aggregate")
    from benchmark_v3.review_package import _markdown

    if review_markdown.read_text(encoding="utf-8") != _markdown(model):
        raise ValueError("review package Markdown does not match aggregate")
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    output = directory / "report-approval.json"
    atomic_json(output, {
        "report_type": "dbwhisperer_v3_report_approval",
        "campaign_id": directory.name,
        "aggregate_sha256": sha256_file(aggregate_path),
        "review_json_sha256": sha256_file(review_json),
        "review_markdown_sha256": sha256_file(review_markdown),
        "approved_by": approved_by.strip(),
        "approved_at": datetime.now(timezone.utc).isoformat(),
    })
    return output


def publish_approved_campaign(
    campaign_dir: str | Path,
    *,
    one_page_path: Path | None = None,
    full_report_path: Path | None = None,
) -> tuple[Path, Path]:
    """Render and atomically promote exactly two approved HTML reports."""

    directory = Path(campaign_dir).resolve()
    _require_official_suite(directory)
    aggregate_path = directory / "aggregate.json"
    approval_path = directory / "report-approval.json"
    if not aggregate_path.is_file() or not approval_path.is_file():
        raise ValueError("aggregate and report approval are required")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    if approval.get("campaign_id") != directory.name:
        raise ValueError("approval campaign identity does not match")
    if approval.get("aggregate_sha256") != sha256_file(aggregate_path):
        raise ValueError("approval hash does not match aggregate")
    _require_official_suite(directory, aggregate)
    review_json = directory / "review-package.json"
    review_markdown = directory / "review-package.md"
    if (
        not review_json.is_file()
        or approval.get("review_json_sha256") != sha256_file(review_json)
        or not review_markdown.is_file()
        or approval.get("review_markdown_sha256")
        != sha256_file(review_markdown)
    ):
        raise ValueError("approval hash does not match reviewed package")
    validate_aggregate(aggregate)
    validate_report_model(build_report_model(aggregate))

    one_page = one_page_path or (
        PROJECT_ROOT / "docs" / "evaluation_method_one_page.html"
    )
    full_report = full_report_path or (
        PROJECT_ROOT / "docs" / "evaluation_report.html"
    )
    stage = directory / (
        f".report-publication-{os.getpid()}-"
        f"{datetime.now(timezone.utc).strftime('%f')}"
    )
    try:
        from benchmark_v3.render_report import write_reports

        stage.mkdir(parents=True, exist_ok=False)
        staged_one = stage / one_page.name
        staged_full = stage / full_report.name
        outputs = write_reports(
            aggregate_path,
            staged_one,
            staged_full,
        )
        if (
            len(outputs) != 2
            or {path.suffix for path in outputs} != {".html"}
            or any(not path.is_file() or path.stat().st_size == 0 for path in outputs)
        ):
            raise ValueError("renderer did not produce exactly two HTML reports")
        one_page.parent.mkdir(parents=True, exist_ok=True)
        full_report.parent.mkdir(parents=True, exist_ok=True)
        _promote_publication(
            (staged_one, staged_full),  # type: ignore[arg-type]
            (one_page, full_report),  # type: ignore[arg-type]
            stage,
        )
        campaign_path = directory / "campaign.json"
        if campaign_path.is_file():
            campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
            campaign["published"] = True
            campaign["published_aggregate_sha256"] = sha256_file(aggregate_path)
            atomic_json(campaign_path, campaign)
        return one_page, full_report
    finally:
        shutil.rmtree(stage, ignore_errors=True)
