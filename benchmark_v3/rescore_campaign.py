"""Immutable counterfactual rescoring for completed Evaluation V3 evidence."""

from __future__ import annotations

import argparse
from copy import deepcopy
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark_v3.aggregate_results import aggregate_reports
from benchmark_v3.contracts import (
    EvaluationCase,
    load_suite,
    validate_reference_suite,
)
from benchmark_v3.observability import atomic_json
from benchmark_v3.run_evaluation import (
    ARMS,
    DEFAULT_SUITE,
    _deserialize_schema,
    _serialize_schema,
    build_services,
    ingest_dataset,
)
from benchmark_v3.scoring import score_query_case, summarize_arm
from db_whisperer.contracts import ComponentState, QueryResult


OUTPUT_NAME = "counterfactual-rescore.json"
CORRECTED_AGGREGATE_NAME = "corrected-aggregate.json"
LEDGER_NAME = "rescore-change-ledger.json"
REPORT_MANIFEST_NAME = "corrected-report-publication.json"
REPORTING_EXCLUSIONS = frozenset({"lab_frequency_with_labels"})
LAB_EXCLUSION_REASON = (
    "The saved question leaves frequency grain unresolved but the case was "
    "classified as non-ambiguous and has no simulated clarification answer."
)
DERIVED_NAMES = frozenset({
    OUTPUT_NAME,
    CORRECTED_AGGREGATE_NAME,
    LEDGER_NAME,
    REPORT_MANIFEST_NAME,
})
DERIVED_DIR_NAMES = frozenset({"corrected-review"})
REFERENCE_ONLY_ORDER_CASES = frozenset({
    "admission_duration_null_safe",
    "stay_hospital",
    "stay_icu",
    "ctl_stay_hospital",
    "ctl_stay_icu",
})


def _source_hash(directory: Path) -> str:
    digest = sha256()
    for path in sorted(
        item
        for item in directory.rglob("*")
        if (
            item.is_file()
            and item.name not in DERIVED_NAMES
            and not set(
                item.relative_to(directory).parts
            ).intersection(DERIVED_DIR_NAMES)
        )
    ):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _scorer_version() -> str:
    digest = sha256()
    for name in ("scoring.py", "sql_analysis.py", "rescore_campaign.py"):
        digest.update(name.encode("utf-8"))
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def _query_result(
    payload: Mapping[str, Any],
    *,
    assume_accepted: bool = False,
) -> QueryResult:
    raw_state = str(payload.get(
        "state",
        "accepted" if assume_accepted else "failed",
    )).casefold()
    try:
        state = ComponentState(raw_state)
    except ValueError:
        state = ComponentState.FAILED
    return QueryResult(
        state=state,
        message=str(payload.get("message", "historical saved result")),
        sql=(
            str(payload["sql"])
            if isinstance(payload.get("sql"), str)
            else None
        ),
        columns=tuple(str(value) for value in payload.get("columns", ())),
        rows=tuple(
            tuple(row)
            for row in payload.get("rows", ())
            if isinstance(row, list)
        ),
        truncated=bool(payload.get("truncated", False)),
    )


def _counterfactual_case(case: EvaluationCase) -> EvaluationCase:
    """Apply approved scoring-only intent corrections to a frozen V3 case."""

    if case.id not in REFERENCE_ONLY_ORDER_CASES or case.reference is None:
        return case
    return replace(
        case,
        reference=replace(
            case.reference,
            ordered=False,
            order_semantics="none",
        ),
    )


def _reference_evidence(
    directory: Path,
    suite: Any,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    for path in sorted(directory.glob("references-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("suite_hash") == suite.sha256
            and isinstance(payload.get("schema"), Mapping)
            and isinstance(payload.get("references"), Mapping)
        ):
            return payload["schema"], payload["references"]
    schema = ingest_dataset(suite.dataset_path)
    query, _ = build_services(suite.candidate_count)
    executed = validate_reference_suite(suite, schema, query)
    references = {
        case_id: {
            **result,
            "state": "accepted",
            "message": "regenerated current-suite reference",
        }
        for case_id, result in executed.items()
    }
    return _serialize_schema(schema), references


def _rescore_record(
    record: Mapping[str, Any],
    case: EvaluationCase,
    schema: Any,
    references: Mapping[str, Any],
) -> dict[str, Any]:
    rescored = deepcopy(dict(record))
    if case.kind != "query" or case.category == "safety":
        return rescored
    result_payload = record.get("result")
    reference_payload = references.get(case.id)
    if not isinstance(result_payload, Mapping):
        result_payload = {}
    actual = _query_result(result_payload)
    expected = (
        _query_result(reference_payload, assume_accepted=True)
        if isinstance(reference_payload, Mapping)
        else None
    )
    clarifications = record.get("clarifications")
    turns = (
        [dict(value) for value in clarifications if isinstance(value, Mapping)]
        if isinstance(clarifications, list)
        else []
    )
    rescored["original_score"] = deepcopy(record.get("score"))
    corrected_case = _counterfactual_case(case)
    rescored["score"] = score_query_case(
        corrected_case,
        actual,
        expected,
        schema,
        turns,
        arm=str(record.get("arm", "")) or None,
    )
    return rescored


def rescore_campaign(campaign_dir: str | Path) -> Path:
    """Write a new score artifact without modifying source campaign files."""

    directory = Path(campaign_dir).resolve()
    suite = load_suite(DEFAULT_SUITE)
    source_campaign_hash = _source_hash(directory)
    campaign = json.loads(
        (directory / "campaign.json").read_text(encoding="utf-8")
    )
    status = json.loads(
        (directory / "status.json").read_text(encoding="utf-8")
    )
    source_aggregate_path = directory / "aggregate.json"
    source_aggregate = (
        json.loads(source_aggregate_path.read_text(encoding="utf-8"))
        if source_aggregate_path.is_file()
        else None
    )
    schema_payload, references = _reference_evidence(directory, suite)
    schema = _deserialize_schema(schema_payload)
    cases = {case.id: case for case in suite.cases}
    reports: list[dict[str, Any]] = []
    for path in sorted(directory.glob("run-[0-9][0-9].json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        records = report.get("records")
        if not isinstance(records, list):
            raise ValueError(f"{path.name}: records are missing")
        copied = deepcopy(report)
        copied["records"] = [
            _rescore_record(record, cases[str(record["case_id"])], schema, references)
            for record in records
            if isinstance(record, Mapping)
            and str(record.get("case_id")) in cases
        ]
        for record in copied["records"]:
            if str(record.get("case_id")) in REPORTING_EXCLUSIONS:
                record["reporting_excluded"] = True
                record["reporting_exclusion_reason"] = LAB_EXCLUSION_REASON
        reports.append(copied)
    if not reports:
        raise ValueError("counterfactual rescore requires saved run reports")

    etl_values = [
        float(record["score"]["score"])
        for report in reports
        for record in report["records"]
        if record.get("arm") == "etl"
        and isinstance(record.get("score"), Mapping)
        and record["score"].get("score") is not None
    ]
    etl_score = (
        sum(etl_values) / len(etl_values) if etl_values else 0.0
    )
    summaries = {
        arm: summarize_arm(
            [
                record
                for report in reports
                for record in report["records"]
                if record.get("arm") == arm
            ],
            etl_score,
        )
        for arm in ARMS
    }
    sensitivity_aggregate = aggregate_reports(
        reports,
        campaign,
        status,
    )
    corrected_aggregate = aggregate_reports(
        reports,
        campaign,
        status,
        excluded_case_ids=REPORTING_EXCLUSIONS,
    )
    source_aggregate_hash = (
        sha256(source_aggregate_path.read_bytes()).hexdigest()
        if source_aggregate_path.is_file()
        else None
    )
    score_changes: list[dict[str, Any]] = []
    for report in reports:
        for record in report["records"]:
            original = record.get("original_score")
            corrected = record.get("score")
            if not isinstance(original, Mapping) or original == corrected:
                continue
            case_id = str(record.get("case_id"))
            if case_id in {
                "admission_duration_null_safe",
                "stay_hospital",
                "stay_icu",
            }:
                rule = "required_concepts_and_duration_representation"
            elif case_id == "patients_with_multiple_admissions_ranked":
                rule = "tie_aware_intent_ranking"
            else:
                rule = "intent_aligned_required_concepts"
            score_changes.append({
                "run": record.get("run"),
                "arm": record.get("arm"),
                "case_id": case_id,
                "correction_rule": rule,
                "original_passed": original.get("passed"),
                "corrected_passed": corrected.get("passed"),
                "original_correctness": original.get("correctness"),
                "corrected_correctness": corrected.get("correctness"),
                "original_reason": original.get("reason"),
                "corrected_reason": corrected.get("reason"),
            })
    denominator_adjustments = [{
        "case_id": "lab_frequency_with_labels",
        "action": "excluded_from_corrected_headline_metrics",
        "reason": LAB_EXCLUSION_REASON,
        "affected_cells": sum(
            str(record.get("case_id")) == "lab_frequency_with_labels"
            for report in reports
            for record in report["records"]
        ),
    }]
    change_ledger_summary = {
        "score_changes": len(score_changes),
        "pass_status_flips": sum(
            change["original_passed"] != change["corrected_passed"]
            for change in score_changes
        ),
        "correction_rules": dict(sorted(Counter(
            str(change["correction_rule"]) for change in score_changes
        ).items())),
    }
    ledger_path = directory / LEDGER_NAME
    atomic_json(ledger_path, {
        "report_type": "dbwhisperer_v3_rescore_change_ledger",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_campaign_hash": source_campaign_hash,
        "source_aggregate_sha256": source_aggregate_hash,
        "scorer_version": _scorer_version(),
        "score_changes": score_changes,
        "denominator_adjustments": denominator_adjustments,
    })
    corrected_aggregate.update({
        "derived_report_type": "dbwhisperer_v3_corrected_aggregate",
        "counterfactual": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_campaign_hash": source_campaign_hash,
        "source_aggregate_sha256": source_aggregate_hash,
        "corrected_scorer_version": _scorer_version(),
        "result_provenance": (
            "deterministic offline rescore of saved live-campaign SQL, "
            "results, and clarification evidence; no model calls"
        ),
        "reporting_adjustments": {
            "excluded_case_ids": sorted(REPORTING_EXCLUSIONS),
            "exclusion_reason": LAB_EXCLUSION_REASON,
            "score_change_count": len(score_changes),
            "ledger": LEDGER_NAME,
        },
        "change_ledger_summary": change_ledger_summary,
        "original_reported": (
            {
                "arms": source_aggregate.get("arms"),
                "arm_deltas": source_aggregate.get("arm_deltas"),
                "shared_etl": source_aggregate.get("shared_etl"),
            }
            if isinstance(source_aggregate, Mapping)
            else None
        ),
        "sensitivity": {
            "all_cases": {
                "arms": sensitivity_aggregate["arms"],
                "arm_deltas": sensitivity_aggregate["arm_deltas"],
                "shared_etl": sensitivity_aggregate["shared_etl"],
                "included_case_ids": "all frozen suite cases",
            },
        },
    })
    corrected_path = directory / CORRECTED_AGGREGATE_NAME
    atomic_json(corrected_path, corrected_aggregate)
    output = directory / OUTPUT_NAME
    atomic_json(output, {
        "report_type": "dbwhisperer_v3_counterfactual_rescore",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_campaign_hash": source_campaign_hash,
        "suite_hash": suite.sha256,
        "scorer_version": _scorer_version(),
        "counterfactual": True,
        "limitation": (
            "This reuses saved SQL, results, and clarifications. It does not "
            "measure the changed semantic detector or generate new model output."
        ),
        "arm_summaries": summaries,
        "run_reports": reports,
        "corrected_aggregate": CORRECTED_AGGREGATE_NAME,
        "change_ledger": LEDGER_NAME,
        "score_change_count": len(score_changes),
        "reporting_exclusions": denominator_adjustments,
    })
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Counterfactually rescore immutable V3 campaign evidence."
    )
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args(argv)
    output = rescore_campaign(args.campaign_dir)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
