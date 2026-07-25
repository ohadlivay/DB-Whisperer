"""Immutable counterfactual rescoring for completed Evaluation V3 evidence."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmark_v3.contracts import EvaluationCase, load_suite
from benchmark_v3.observability import atomic_json
from benchmark_v3.run_evaluation import (
    ARMS,
    DEFAULT_SUITE,
    _deserialize_schema,
)
from benchmark_v3.scoring import score_query_case, summarize_arm
from db_whisperer.contracts import ComponentState, QueryResult


OUTPUT_NAME = "counterfactual-rescore.json"


def _source_hash(directory: Path) -> str:
    digest = sha256()
    for path in sorted(
        item
        for item in directory.rglob("*")
        if item.is_file() and item.name != OUTPUT_NAME
    ):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _scorer_version() -> str:
    return sha256(
        Path(__file__).with_name("scoring.py").read_bytes()
    ).hexdigest()


def _query_result(payload: Mapping[str, Any]) -> QueryResult:
    raw_state = str(payload.get("state", "failed")).casefold()
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


def _reference_evidence(
    directory: Path,
    suite_hash: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    for path in sorted(directory.glob("references-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("suite_hash") == suite_hash
            and isinstance(payload.get("schema"), Mapping)
            and isinstance(payload.get("references"), Mapping)
        ):
            return payload["schema"], payload["references"]
    raise ValueError(
        "counterfactual rescore requires the campaign's frozen reference artifact"
    )


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
        _query_result(reference_payload)
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
    rescored["score"] = score_query_case(
        case,
        actual,
        expected,
        schema,
        turns,
    )
    return rescored


def rescore_campaign(campaign_dir: str | Path) -> Path:
    """Write a new score artifact without modifying source campaign files."""

    directory = Path(campaign_dir).resolve()
    suite = load_suite(DEFAULT_SUITE)
    source_campaign_hash = _source_hash(directory)
    schema_payload, references = _reference_evidence(
        directory,
        suite.sha256,
    )
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
