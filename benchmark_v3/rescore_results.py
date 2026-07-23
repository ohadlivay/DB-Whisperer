"""Retrospectively rescore a completed V3 run without model calls."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from typing import Any

from benchmark_v3.contracts import ARMS, EvaluationCase, EvaluationSuite, current_id, load_suite
from benchmark_v3.run_evaluation import DEFAULT_SUITE, build_services, ingest_dataset
from benchmark_v3.scoring import SCORING_VERSION, classify_intent, score_arm_case
from db_whisperer.contracts import ComponentState, QueryCandidate, QueryResult


LEGACY_SUITE_HASH = "8317dda116d13b95fef7d85f192d721aeb89d02b28c0af4827e577a7de4a67af"


def _query_result(payload: dict[str, Any]) -> QueryResult | None:
    if payload.get("state") not in {state.value for state in ComponentState}:
        return None
    return QueryResult(
        state=ComponentState(payload["state"]),
        message=str(payload.get("message") or ""),
        sql=payload.get("sql"),
        columns=tuple(payload.get("columns", [])),
        rows=tuple(tuple(row) for row in payload.get("rows", [])),
    )


def _intent_history(
    case: EvaluationCase,
    suite: EvaluationSuite,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    competitors = [
        member for member in suite.query_cases
        if member.ambiguous and member.family_id == case.family_id and member.id != case.id
    ]
    revised = []
    for item in history:
        status = classify_intent(case, tuple(competitors), str(item.get("chosen", "")))
        revised.append({**item, "matched_intent": status == "matched", "intent_status": status})
    return revised


def _validate(raw: dict[str, Any], suite: EvaluationSuite) -> None:
    if raw.get("report_type") != "dbwhisperer_v3_run":
        raise ValueError("Expected a raw Evaluation V3 run.")
    if tuple(raw.get("arms", [])) != ARMS:
        raise ValueError("Raw report arms do not match Evaluation V3.")
    if raw.get("suite_hash") not in {suite.sha256, LEGACY_SUITE_HASH}:
        raise ValueError("Raw report suite hash is not supported for retrospective rescoring.")
    expected = {case.id for case in suite.query_cases}
    found = {current_id(row["case_id"]) for row in raw.get("records", [])}
    if found != expected:
        raise ValueError("Raw report cases do not match the current normalized suite.")


def rescore(input_path: Path, output_path: Path, suite_path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    suite = load_suite(suite_path)
    _validate(raw, suite)
    cases = {case.id: case for case in suite.query_cases}
    with tempfile.TemporaryDirectory(prefix="dbw-v3-rescore-") as temporary:
        schema = ingest_dataset(suite.dataset_path, Path(temporary) / "dataset.duckdb")
        query, _ = build_services(suite.candidate_count)
        references = {
            case.id: (
                query.execute_candidate(
                    QueryCandidate(0, ComponentState.ACCEPTED, sql=case.expected_sql),
                    schema.database_path or "",
                )
                if case.expected_sql else None
            )
            for case in suite.query_cases
        }
        records = []
        for row in raw["records"]:
            case_id = current_id(row["case_id"])
            case = cases[case_id]
            history = _intent_history(case, suite, row.get("clarifications", []))
            corrected = score_arm_case(
                case,
                row["arm"],
                _query_result(row.get("result", {})),
                references[case_id],
                history,
                schema,
            )
            records.append({
                **row,
                "case_id": case_id,
                "family_id": current_id(row["family_id"]),
                "clarifications": history,
                "original_score": row["score"],
                "score": corrected,
            })
    report = {
        **{key: raw[key] for key in ("suite_version", "suite_hash", "model", "arms", "etl")},
        "report_type": "dbwhisperer_v3_rescored",
        "scoring_version": f"{SCORING_VERSION}-retrospective",
        "retrospective": True,
        "source_report": str(input_path.resolve()),
        "case_contracts": [asdict(case) for case in suite.query_cases],
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.report.with_name(f"{args.report.stem}_rescored.json")
    rescore(args.report.resolve(), output.resolve(), args.suite.resolve())
    print(f"Rescored report: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
