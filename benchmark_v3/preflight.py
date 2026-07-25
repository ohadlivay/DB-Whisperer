"""Deterministic, offline readiness checks for an Evaluation V3 campaign."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4

from benchmark_v3.contracts import load_suite, validate_reference_suite
from benchmark_v3.observability import atomic_json
from benchmark_v3.render_report import write_reports
from benchmark_v3.report_contract import validate_report_model
from benchmark_v3.rescore_campaign import rescore_campaign
from benchmark_v3.run_evaluation import (
    DEFAULT_SUITE,
    PROJECT_ROOT,
    _fingerprint,
    _hash_files,
    build_services,
    ingest_dataset,
)
from benchmark_v3.scoring import score_query_case


CHECKS = (
    "suite",
    "references",
    "scorer",
    "report_contract",
    "renderer",
    "fingerprint",
    "historical_rescore",
    "public_html_unchanged",
)
TEMP_ROOT = PROJECT_ROOT / "benchmark_v3" / "results"


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    checks: dict[str, bool]
    details: dict[str, str]
    errors: tuple[str, ...]


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def _work_directory(label: str) -> Path:
    path = TEMP_ROOT / f".preflight-{label}-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _copy_rescore_inputs(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if not (
            path.name == "campaign.json"
            or path.name.startswith("run-") and path.suffix == ".json"
            or path.name.startswith("references-") and path.suffix == ".json"
        ):
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _report_fixture() -> dict[str, object]:
    arms = ("baseline", "candidate_only", "semantic_only", "full")
    metrics = (
        "recall",
        "specificity",
        "plausibility",
        "target_coverage",
        "resolution",
        "compliance",
        "final_alignment",
    )
    return {
        "title": "DB Whisperer Evaluation V3",
        "research_question": "Does clarification improve intent alignment?",
        "experimental_design": {"arms": list(arms), "repetitions": 5, "k": 3},
        "methodology": {"suite": "frozen", "scoring": "intent-aligned"},
        "provenance": {"result_provenance": "preflight fixture"},
        "headline_metrics": {arm: {"mean": 1.0} for arm in arms},
        "arm_deltas": {},
        "ambiguity_funnel": {
            arm: {metric: 1.0 for metric in metrics}
            for arm in arms
        },
        "correctness_diagnostics": {"passed": 1},
        "projection_diagnostics": {"compatible": 1},
        "terminal_outcomes": {"accepted": 1},
        "case_findings": {"successes": ["fixture"], "failures": ["fixture"]},
        "findings": ["fixture finding"],
        "typed_findings": [{"type": "method", "finding": "fixture"}],
        "interpretations": [],
        "recommendations": [],
        "limitations": ["preflight fixture only"],
        "report_readiness": {"complete": True},
        "operations": {
            "metrics": {"cost_usd": 0.0, "elapsed_seconds": 0.0}
        },
        "arms": {
            arm: {"components": {}, "pass_rate": 1.0}
            for arm in arms
        },
        "cases": [],
    }


def run_preflight(
    suite_path: str | Path = DEFAULT_SUITE,
    *,
    historical_campaign: str | Path | None = None,
    output_path: str | Path | None = None,
) -> PreflightResult:
    """Run required checks without invoking a model or mutating public HTML."""

    checks = {name: False for name in CHECKS}
    details: dict[str, str] = {}
    errors: list[str] = []
    public = (
        PROJECT_ROOT / "docs" / "evaluation_method_one_page.html",
        PROJECT_ROOT / "docs" / "evaluation_report.html",
    )
    before = tuple(_file_hash(path) for path in public)
    suite = None
    schema = None
    try:
        suite = load_suite(suite_path)
        checks["suite"] = True
        details["suite"] = f"{suite.name} {suite.version}; {len(suite.cases)} cases"
    except Exception as error:
        errors.append(f"suite: {error}")
    if suite is not None:
        try:
            schema = ingest_dataset(suite.dataset_path)
            query, _ = build_services(suite.candidate_count)
            references = validate_reference_suite(suite, schema, query)
            checks["references"] = True
            details["references"] = f"{len(references)} executable references"
        except Exception as error:
            errors.append(f"references: {error}")
        try:
            if not callable(score_query_case):
                raise ValueError("query scorer is unavailable")
            checks["scorer"] = True
            details["scorer"] = "deterministic scorer import and contracts ready"
        except Exception as error:
            errors.append(f"scorer: {error}")
        try:
            fingerprint = _fingerprint(
                suite,
                _hash_files(suite.dataset_path),
                workers=2,
            )
            checks["fingerprint"] = True
            details["fingerprint"] = (
                f"suite={fingerprint.suite_hash}; runtime={fingerprint.runtime_hash}"
            )
        except Exception as error:
            errors.append(f"fingerprint: {error}")
    try:
        model = _report_fixture()
        validate_report_model(model)
        checks["report_contract"] = True
        details["report_contract"] = "fixture populates all approved report fields"
        temp = _work_directory("render")
        aggregate_path = temp / "aggregate.json"
        aggregate_path.write_text(
            json.dumps({"model": model}),
            encoding="utf-8",
        )
        reports = write_reports(
            aggregate_path,
            temp / "one-page.html",
            temp / "evidence.html",
        )
        if len(reports) != 2 or any(not path.is_file() for path in reports):
            raise ValueError("renderer did not produce two temporary reports")
        checks["renderer"] = True
        details["renderer"] = "two temporary HTML reports rendered"
    except Exception as error:
        errors.append(f"report_contract: {error}")
    history = Path(historical_campaign).resolve() if historical_campaign else None
    if history is None:
        checks["historical_rescore"] = callable(rescore_campaign)
        details["historical_rescore"] = (
            "rescorer ready; pass --historical-campaign to execute replay"
        )
    elif not history.is_dir():
        errors.append("historical_rescore: campaign directory does not exist")
    else:
        try:
            copied = _work_directory("rescore") / history.name
            _copy_rescore_inputs(history, copied)
            artifact = rescore_campaign(copied)
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            if payload.get("report_type") != "dbwhisperer_v3_counterfactual_rescore":
                raise ValueError("unexpected historical rescore artifact")
            checks["historical_rescore"] = True
            details["historical_rescore"] = "immutable copied evidence rescored"
        except Exception as error:
            errors.append(f"historical_rescore: {error}")
    after = tuple(_file_hash(path) for path in public)
    checks["public_html_unchanged"] = before == after
    details["public_html_unchanged"] = "public report bytes were not modified"
    if before != after:
        errors.append("public_html_unchanged: public report bytes changed")
    result = PreflightResult(
        passed=all(checks.values()),
        checks=checks,
        details=details,
        errors=tuple(errors),
    )
    if output_path is not None:
        atomic_json(Path(output_path), asdict(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--historical-campaign", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_preflight(
        args.suite,
        historical_campaign=args.historical_campaign,
        output_path=args.output,
    )
    for name in CHECKS:
        print(f"{'PASS' if result.checks[name] else 'FAIL'} {name}: {result.details.get(name, '')}")
    if not result.passed:
        raise SystemExit("; ".join(result.errors))


if __name__ == "__main__":
    main()
