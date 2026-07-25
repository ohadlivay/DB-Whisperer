"""Strict structural validation for frozen Evaluation V3 campaign output."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import ARMS, DEFAULT_SUITE
from benchmark_v3.scoring import required_filter_present

COMPATIBILITY_FIELDS = (
    "suite_version", "suite_hash", "dataset_hash", "model", "prompt_hash",
    "scorer_version", "candidate_count", "arms", "runtime_hash",
)
UNRESOLVED_STATES = {"", "missing", "pending", "running", "resuming"}
_MISSING_FILTER_PREFIX = "required filter is missing: "
_DISTRIBUTION_FIELDS = {
    "mean", "stddev", "min", "max", "confidence_interval_95",
}
TERMINAL_CATEGORIES = {
    "accepted",
    "safety_rejection",
    "unresolved_clarification",
    "unnecessary_clarification",
    "target_option_missing",
    "initial_generation_format_failure",
    "candidate_quorum_failure",
    "sql_validation_failure",
    "sql_execution_failure",
    "post_clarification_generation_failure",
    "clarification_compliance_failure",
    "no_final_result",
}
_SERIALIZED_RESULT_FIELDS = {
    "state",
    "message",
    "sql",
    "columns",
    "rows",
    "truncated",
}


def _metadata(report: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = report.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        raise ValueError("campaign report fingerprint is missing")
    return {
        "suite_version": report.get("suite_version"),
        "suite_hash": report.get("suite_hash"),
        "dataset_hash": fingerprint.get("dataset_hash"),
        "model": report.get("model"),
        "prompt_hash": fingerprint.get("prompt_hash"),
        "scorer_version": fingerprint.get("scorer_version"),
        "candidate_count": fingerprint.get("candidate_count"),
        "arms": report.get("arms"),
        "runtime_hash": fingerprint.get("runtime_hash"),
    }


def _finite(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"metric must be finite: {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _finite(item, f"{path}.{key}")
    elif isinstance(value, Sequence):
        for index, item in enumerate(value):
            _finite(item, f"{path}[{index}]")


def _published_distributions(value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        return
    if "mean" in value or "confidence_interval_95" in value:
        if not _DISTRIBUTION_FIELDS <= set(value):
            raise ValueError(f"published distribution is missing fields: {path}")
        interval = value["confidence_interval_95"]
        if not isinstance(interval, list) or len(interval) != 2:
            raise ValueError(f"published distribution interval is invalid: {path}")
        numeric_fields = ("mean", "stddev", "min", "max")
        if any(
            isinstance(value[field], bool)
            or not isinstance(value[field], (int, float))
            or not math.isfinite(float(value[field]))
            for field in numeric_fields
        ) or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in interval
        ):
            raise ValueError(f"published distribution must be finite: {path}")
    for key, item in value.items():
        _published_distributions(item, f"{path}.{key}")


def _expected_graph() -> dict[tuple[str, str], tuple[str, str]]:
    suite = load_suite(DEFAULT_SUITE)
    return {
        **{
            (case.id, arm): (case.family_id, case.category)
            for case in suite.query_cases for arm in ARMS
        },
        **{
            (case.id, "etl"): (case.family_id, case.category)
            for case in suite.etl_cases
        },
    }


def validate_reports(
    reports: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any] | None = None,
) -> None:
    """Reject incomplete, incompatible, malformed, or non-finite run reports."""
    if len(reports) != 5:
        raise ValueError("five complete compatible repetitions are required")
    if campaign is not None and campaign.get("complete") is not True:
        raise ValueError("campaign has unresolved incomplete state")
    expected = _expected_graph()
    baseline: dict[str, Any] | None = None
    repetitions: set[int] = set()
    for report in reports:
        if report.get("report_type") != "dbwhisperer_v3_run":
            raise ValueError("invalid V3 run report")
        repetition = report.get("repetition")
        if not isinstance(repetition, int):
            raise ValueError("run report repetition is missing")
        repetitions.add(repetition)
        metadata = _metadata(report)
        if metadata["arms"] != list(ARMS):
            raise ValueError("run report arms must exactly match V3 arms")
        if baseline is None:
            baseline = metadata
        elif any(metadata[field] != baseline[field] for field in COMPATIBILITY_FIELDS):
            raise ValueError("V3 reports have incompatible fingerprints")
        if campaign is not None and campaign.get("fingerprint") != report.get("fingerprint"):
            raise ValueError("campaign fingerprint is incompatible with run report")
        fingerprint = report["fingerprint"]
        for field in (
            "suite_hash", "dataset_hash", "model", "prompt_hash",
            "scorer_version", "candidate_count", "arms", "runtime_hash",
        ):
            if field in report and report[field] != fingerprint.get(field):
                raise ValueError("run report top-level fingerprint is incompatible")
        records = report.get("records")
        if not isinstance(records, list):
            raise ValueError("run report records are missing")
        seen: set[tuple[str, str]] = set()
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("run record is invalid")
            key = (record.get("case_id"), record.get("arm"))
            if key not in expected or key in seen:
                raise ValueError("run report expected work graph is missing or duplicated")
            seen.add(key)
            if (
                record.get("family_id"), record.get("category")
            ) != expected[key]:
                raise ValueError("run report expected work graph is incompatible")
            if record.get("run") != repetition:
                raise ValueError("run record identity is incompatible")
            result = record.get("result")
            score = record.get("score")
            observation = record.get("observation")
            if not isinstance(result, Mapping) or not isinstance(score, Mapping):
                raise ValueError("run record result or score is missing")
            if record.get("arm") in ARMS:
                terminal = record.get("terminal")
                if (
                    not isinstance(terminal, Mapping)
                    or terminal.get("category") not in TERMINAL_CATEGORIES
                ):
                    raise ValueError(
                        "run record terminal category is missing or invalid"
                    )
                for count_field in (
                    "generated_candidates",
                    "executed_candidates",
                    "successful_candidates",
                ):
                    count = terminal.get(count_field)
                    if (
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count < 0
                    ):
                        raise ValueError(
                            "run record terminal counts are invalid"
                        )
                best = record.get("best_preclarification_result")
                if best is not None:
                    if (
                        not isinstance(best, Mapping)
                        or not _SERIALIZED_RESULT_FIELDS <= set(best)
                        or best.get("state") != "accepted"
                        or not isinstance(best.get("columns"), list)
                        or not isinstance(best.get("rows"), list)
                        or not isinstance(best.get("truncated"), bool)
                    ):
                        raise ValueError(
                            "best preclarification result is incomplete"
                        )
            if (
                not isinstance(observation, Mapping)
                or observation.get("valid") is not True
                or observation.get("source") != "system"
                or observation.get("outcome")
                not in {"success", "system_failure"}
            ):
                raise ValueError(
                    "run report requires a valid system observation for every cell"
                )
            if str(result.get("state", "")).casefold() in UNRESOLVED_STATES:
                raise ValueError("run report contains unresolved incomplete state")
            if not isinstance(score.get("passed"), bool):
                raise ValueError("run record passed score is missing")
            clarifications = record.get("clarifications", [])
            if not isinstance(clarifications, list):
                raise ValueError("run record clarifications are invalid")
            if (
                str(result.get("state", "")).casefold() == "accepted"
                and clarifications
                and all(
                    isinstance(turn, Mapping)
                    and turn.get("matched_intent") is True
                    for turn in clarifications
                )
                and not isinstance(
                    clarifications[-1].get("compliance_passed"),
                    bool,
                )
            ):
                raise ValueError(
                    "matched clarification is missing final compliance evidence"
                )
            reason = str(score.get("reason", ""))
            sql = result.get("sql")
            if (
                reason.startswith(_MISSING_FILTER_PREFIX)
                and isinstance(sql, str)
            ):
                required = reason.removeprefix(_MISSING_FILTER_PREFIX)
                try:
                    contradicted = required_filter_present(sql, required)
                except Exception:
                    contradicted = False
                if contradicted:
                    raise ValueError(
                        "stored score reason contradicts parsed SQL filter"
                    )
            _finite(score, "score")
            _finite(record.get("duration_seconds"), "duration_seconds")
        if seen != set(expected):
            raise ValueError("run report expected work graph is missing")
        _finite(report.get("usage", {}), "usage")
    if repetitions != {1, 2, 3, 4, 5}:
        raise ValueError("five complete compatible repetitions are required")


def validate_aggregate(payload: Mapping[str, Any]) -> None:
    """Validate a produced aggregate before public report rendering."""
    if payload.get("report_type") != "dbwhisperer_v3_aggregate":
        raise ValueError("invalid V3 aggregate")
    if payload.get("complete") is not True:
        raise ValueError("aggregate has unresolved incomplete state")
    reports = payload.get("run_reports")
    if not isinstance(reports, list):
        raise ValueError("aggregate raw run reports are missing")
    campaign = payload.get("campaign")
    if not isinstance(campaign, Mapping):
        raise ValueError("aggregate campaign evidence is missing")
    if campaign.get("publishable") is not True:
        raise ValueError("aggregate campaign is not publishable")
    validate_reports(reports, campaign)
    _finite(payload.get("arms"), "arms")
    _finite(payload.get("arm_deltas"), "arm_deltas")
    _finite(payload.get("shared_etl"), "shared_etl")
    _finite(payload.get("usage"), "usage")
    _finite(payload.get("operational"), "operational")
    if "shared_etl" not in payload or "arm_deltas" not in payload:
        raise ValueError("published distribution is missing")
    _published_distributions(payload["shared_etl"], "shared_etl")
    _published_distributions(payload["arms"], "arms")
    _published_distributions(payload["arm_deltas"], "arm_deltas")
