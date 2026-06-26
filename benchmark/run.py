"""Run the minimal DB Whisperer benchmark outside the application UI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any

import duckdb
import requests


BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from db_whisperer.contracts import (  # noqa: E402
    ComponentState,
    CsvUpload,
    QueryRequest,
    QueryResult,
    SchemaMetadata,
)
from db_whisperer.etler import ETLService  # noqa: E402
from db_whisperer.querier import QueryService  # noqa: E402

from _harness import (  # noqa: E402
    execute_reference,
    judge as judge_result,
    load_env_file,
    table,
)


DEFAULT_CASES_PATH = BENCHMARK_DIR / "cases.json"
DEFAULT_OUTPUT_DIR = BENCHMARK_DIR / "results"


class SingleAttemptQueryService(QueryService):
    """Use the production query workflow without validation retries."""

    MAX_VALIDATION_RETRIES = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the external DB Whisperer benchmark.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="Path to the benchmark JSON file.",
    )
    parser.add_argument(
        "--model",
        default=(
            os.getenv("OPENROUTER_MODEL")
            or "google/gemma-4-31b-it"
        ),
        help="OpenRouter model used to generate SQL.",
    )
    parser.add_argument(
        "--judge-model",
        default=os.getenv("BENCHMARK_JUDGE_MODEL", ""),
        help="Separate OpenRouter model used to judge non-exact results.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for reports and benchmark prompt logs.",
    )
    return parser.parse_args()


def _load_suite(path: Path) -> tuple[str, Path, list[dict[str, str]]]:
    resolved_path = path.expanduser().resolve()
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Benchmark file must contain one JSON object.")

    name = payload.get("name")
    dataset_value = payload.get("dataset")
    cases = payload.get("cases")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Benchmark name must be non-empty text.")
    if not isinstance(dataset_value, str) or not dataset_value.strip():
        raise ValueError("Benchmark dataset must be a non-empty path.")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark cases must be a non-empty list.")

    dataset_path = (resolved_path.parent / dataset_value).resolve()
    if not dataset_path.is_file():
        raise ValueError(f"Dataset does not exist: {dataset_path}")

    normalized_cases: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Every benchmark case must be an object.")
        normalized: dict[str, str] = {}
        for field in ("id", "question", "expected_sql"):
            value = case.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Every benchmark case requires non-empty {field}."
                )
            normalized[field] = value.strip()
        if normalized["id"] in seen_ids:
            raise ValueError(
                f"Duplicate benchmark case ID: {normalized['id']}"
            )
        seen_ids.add(normalized["id"])
        normalized_cases.append(normalized)

    return name.strip(), dataset_path, normalized_cases


def _evaluate_case(
    case: dict[str, str],
    schema: SchemaMetadata,
    query_service: QueryService,
    api_key: str,
    model: str,
    judge_model: str,
) -> dict[str, Any]:
    expected_columns, expected_rows = execute_reference(
        schema.database_path,
        case["expected_sql"],
    )
    expected = table(expected_columns, expected_rows)
    started = perf_counter()
    generated: QueryResult = query_service.query(
        QueryRequest(
            prompt=case["question"],
            schema=schema,
            api_key=api_key,
            model=model,
        )
    )
    duration = perf_counter() - started
    actual = table(generated.columns, generated.rows)
    result: dict[str, Any] = {
        "id": case["id"],
        "question": case["question"],
        "expected_sql": case["expected_sql"],
        "generated_sql": generated.sql,
        "expected": expected,
        "actual": actual,
        "exact_match": False,
        "comparison": "system_failure",
        "score": 0,
        "reason": generated.message,
        "duration_seconds": round(duration, 4),
        "error": None,
    }
    if generated.state != ComponentState.ACCEPTED:
        return result

    exact_match = (
        generated.columns == expected_columns
        and generated.rows == expected_rows
    )
    if exact_match:
        result.update(
            exact_match=True,
            comparison="exact",
            score=4,
            reason="Generated table exactly matches the reference table.",
        )
        return result

    try:
        score, reason = judge_result(
            api_key,
            judge_model,
            case["question"],
            expected,
            actual,
        )
        result.update(
            comparison="judge",
            score=score,
            reason=reason,
        )
    except (
        requests.RequestException,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        result.update(
            comparison="judge_failure",
            score=None,
            reason="Judge could not score the result.",
            error=str(error),
        )
    return result


def _summary(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [
        result["score"]
        for result in case_results
        if isinstance(result["score"], int)
    ]
    overall = sum(scores) / len(scores) if scores else None
    return {
        "total_cases": len(case_results),
        "scored_cases": len(scores),
        "exact_matches": sum(r["exact_match"] for r in case_results),
        "judged_cases": sum(r["comparison"] == "judge" for r in case_results),
        "system_failures": sum(
            r["comparison"] == "system_failure" for r in case_results
        ),
        "judge_failures": sum(
            r["comparison"] == "judge_failure" for r in case_results
        ),
        "score_out_of_4": round(overall, 4) if overall is not None else None,
        "normalized_percentage": (
            round(overall / 4 * 100, 2) if overall is not None else None
        ),
    }


def main() -> int:
    load_env_file(BENCHMARK_DIR / ".env")
    args = _parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is required.", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("A tested model is required.", file=sys.stderr)
        return 2
    if not args.judge_model.strip():
        print(
            "BENCHMARK_JUDGE_MODEL or --judge-model is required.",
            file=sys.stderr,
        )
        return 2

    try:
        suite_name, dataset_path, cases = _load_suite(args.cases)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid benchmark: {error}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = BENCHMARK_DIR / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / f"{run_id}.duckdb"
    prompt_log_path = output_dir / f"{run_id}.prompts.jsonl"
    os.environ["DB_WHISPERER_PROMPT_LOG"] = str(prompt_log_path)

    ingestion = ETLService(database_path=database_path).ingest(
        (
            CsvUpload(
                name=dataset_path.name,
                content=dataset_path.read_bytes(),
            ),
        )
    )
    if ingestion.state != ComponentState.ACCEPTED:
        print(f"Ingestion failed: {ingestion.message}", file=sys.stderr)
        return 1

    query_service = SingleAttemptQueryService()
    case_results: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = _evaluate_case(
                case,
                ingestion.schema,
                query_service,
                api_key,
                args.model,
                args.judge_model,
            )
        except (duckdb.Error, OSError, ValueError) as error:
            print(
                f"Invalid reference for {case['id']}: {error}",
                file=sys.stderr,
            )
            return 2
        case_results.append(result)
        score = (
            f"{result['score']}/4"
            if result["score"] is not None
            else "unscored"
        )
        print(
            f"[{result['comparison'].upper()}] "
            f"{result['id']}: {score} - {result['reason']}"
        )

    summary = _summary(case_results)
    completed_at = datetime.now(timezone.utc)
    report = {
        "run_id": run_id,
        "suite": suite_name,
        "dataset": str(dataset_path),
        "tested_model": args.model,
        "judge_model": args.judge_model,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "prompt_log": str(prompt_log_path),
        "summary": summary,
        "cases": case_results,
    }
    report_path = output_dir / f"{run_id}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    score = summary["score_out_of_4"]
    percentage = summary["normalized_percentage"]
    print(f"Overall: {score}/4 ({percentage}%)")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
