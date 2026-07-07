"""MIMIC-III A/B evaluation harness skeleton.

Iteration 2 intentionally focuses on plumbing, not scoring:

* load the MIMIC-specific case file;
* ingest the bundled MIMIC CSV directory through the normal ETL boundary;
* run each case once through the baseline ``QueryService`` arm;
* run each case once through the full ``ApplicationService`` arm;
* write a structured JSON report with raw outputs.

Clarification simulation, deterministic scoring, and self-judging are added in
later iterations. Keeping this skeleton small makes the next contracts explicit
without disturbing the existing BikeStores A/B harness.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from db_whisperer.application import ApplicationService  # noqa: E402
from db_whisperer.contracts import (  # noqa: E402
    AmbiguityDecision,
    ComponentState,
    CsvUpload,
    QueryRequest,
    QueryResult,
    QueryWorkflowResult,
    SchemaMetadata,
)
from db_whisperer.etler import ETLService  # noqa: E402
from db_whisperer.prompt_logging import PromptLogger  # noqa: E402
from db_whisperer.querier import QueryService  # noqa: E402

from _harness import format_clarification, load_env_file, table  # noqa: E402


DEFAULT_CASES_PATH = BENCHMARK_DIR / "mimic_ab_cases.json"
DEFAULT_OUTPUT_DIR = BENCHMARK_DIR / "results"
DEFAULT_CANDIDATE_COUNT = 3
ALLOWED_AMBIGUITY_TYPES = {
    "none",
    "join-path",
    "semantic-column",
    "underspecified",
}


@dataclass(frozen=True)
class MimicCase:
    """One MIMIC evaluation case."""

    id: str
    category: str
    question: str
    ambiguous: bool
    ambiguity_type: str
    intent: str
    schema_elements: tuple[str, ...]
    expected_sql: str | None
    should_clarify: bool
    simulated_user_answer: str | None
    expected_behavior: tuple[str, ...]
    tests: tuple[str, ...]


@dataclass(frozen=True)
class MimicSuite:
    """Loaded and validated MIMIC evaluation suite."""

    name: str
    dataset_path: Path
    candidate_count: int
    judge: dict[str, Any]
    notes: tuple[str, ...]
    cases: tuple[MimicCase, ...]


def dataset_uploads(dataset_path: Path) -> list[CsvUpload]:
    """Read every CSV in a dataset directory, or one CSV file."""
    if dataset_path.is_dir():
        csv_paths = sorted(dataset_path.glob("*.csv"))
        if not csv_paths:
            raise ValueError(f"Dataset directory has no CSV files: {dataset_path}")
        return [
            CsvUpload(name=path.name, content=path.read_bytes())
            for path in csv_paths
        ]
    return [CsvUpload(name=dataset_path.name, content=dataset_path.read_bytes())]


def load_mimic_suite(path: Path) -> MimicSuite:
    """Load and strictly validate the MIMIC benchmark case file."""
    resolved_path = path.expanduser().resolve()
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Benchmark file must contain one JSON object.")

    name = _required_text(payload, "name", "suite")
    dataset_value = _required_text(payload, "dataset", "suite")
    cases_raw = payload.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError("Benchmark cases must be a non-empty list.")

    candidate_count = payload.get("candidate_count", DEFAULT_CANDIDATE_COUNT)
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < 2
    ):
        raise ValueError("candidate_count must be an integer >= 2.")

    dataset_path = (resolved_path.parent / dataset_value).resolve()
    if not dataset_path.exists():
        raise ValueError(f"Dataset does not exist: {dataset_path}")

    judge = payload.get("judge", {})
    if not isinstance(judge, dict):
        raise ValueError("judge must be an object when provided.")

    notes_raw = payload.get("notes", [])
    if not isinstance(notes_raw, list) or not all(
        isinstance(note, str) for note in notes_raw
    ):
        raise ValueError("notes must be a list of strings.")

    cases: list[MimicCase] = []
    seen_ids: set[str] = set()
    for raw in cases_raw:
        case = normalize_mimic_case(raw)
        if case.id in seen_ids:
            raise ValueError(f"Duplicate benchmark case ID: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)

    return MimicSuite(
        name=name,
        dataset_path=dataset_path,
        candidate_count=candidate_count,
        judge=judge,
        notes=tuple(note.strip() for note in notes_raw if note.strip()),
        cases=tuple(cases),
    )


def normalize_mimic_case(raw: Any) -> MimicCase:
    """Normalize one raw JSON case object."""
    if not isinstance(raw, dict):
        raise ValueError("Every benchmark case must be an object.")

    case_id = _required_text(raw, "id", "case")
    category = _required_text(raw, "category", case_id)
    question = _required_text(raw, "question", case_id)
    intent = _required_text(raw, "intent", case_id)
    ambiguity_type = _required_text(raw, "ambiguity_type", case_id)
    if ambiguity_type not in ALLOWED_AMBIGUITY_TYPES:
        raise ValueError(
            f"Case {case_id} has unsupported ambiguity_type: {ambiguity_type}"
        )

    ambiguous = raw.get("ambiguous")
    if not isinstance(ambiguous, bool):
        raise ValueError(f"Case {case_id} must declare boolean ambiguous.")
    should_clarify = raw.get("should_clarify")
    if not isinstance(should_clarify, bool):
        raise ValueError(f"Case {case_id} must declare boolean should_clarify.")
    if should_clarify and not ambiguous:
        raise ValueError(f"Case {case_id} cannot clarify unless ambiguous.")
    if not ambiguous and ambiguity_type != "none":
        raise ValueError(
            f"Case {case_id} must use ambiguity_type 'none' when unambiguous."
        )

    expected_sql = raw.get("expected_sql")
    if expected_sql is not None:
        if not isinstance(expected_sql, str) or not expected_sql.strip():
            raise ValueError(f"Case {case_id} expected_sql must be text or null.")
        expected_sql = expected_sql.strip()

    simulated_user_answer = raw.get("simulated_user_answer")
    if should_clarify:
        if (
            not isinstance(simulated_user_answer, str)
            or not simulated_user_answer.strip()
        ):
            raise ValueError(
                f"Case {case_id} requires a simulated_user_answer."
            )
        simulated_user_answer = simulated_user_answer.strip()
    elif simulated_user_answer is not None:
        raise ValueError(
            f"Case {case_id} must not set simulated_user_answer."
        )

    return MimicCase(
        id=case_id,
        category=category,
        question=question,
        ambiguous=ambiguous,
        ambiguity_type=ambiguity_type,
        intent=intent,
        schema_elements=_text_tuple(
            raw,
            "schema_elements",
            case_id,
            allow_empty=True,
        ),
        expected_sql=expected_sql,
        should_clarify=should_clarify,
        simulated_user_answer=simulated_user_answer,
        expected_behavior=_text_tuple(raw, "expected_behavior", case_id),
        tests=_text_tuple(raw, "tests", case_id),
    )


def _required_text(payload: dict[str, Any], field_name: str, owner: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} requires non-empty {field_name}.")
    return value.strip()


def _text_tuple(
    payload: dict[str, Any],
    field_name: str,
    owner: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    value = payload.get(field_name)
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        if allow_empty:
            raise ValueError(f"{owner} requires string list {field_name}.")
        raise ValueError(f"{owner} requires non-empty string list {field_name}.")
    return tuple(item.strip() for item in value)


def run_baseline_once(
    case: MimicCase,
    schema: SchemaMetadata,
    query_service: QueryService,
    api_key: str,
    model: str,
) -> QueryResult:
    """Run the baseline arm once: one prompt straight to SQL."""
    return query_service.query(
        QueryRequest(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
        )
    )


def run_full_once(
    case: MimicCase,
    schema: SchemaMetadata,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> QueryWorkflowResult:
    """Run the full arm once.

    Iteration 3 will turn a pending clarification into a simulated-user loop.
    For now, preserving the pending workflow result is the point of the
    skeleton: it shows which cases trigger ambiguity before scoring exists.
    """
    return application.submit_query(
        prompt=case.question,
        schema=schema,
        api_key=api_key,
        model=model,
        candidate_count=application.candidates_per_iteration,
    )


def choose_clarification_option(
    case: MimicCase,
    decision: AmbiguityDecision,
) -> tuple[int, str, bool, str | None]:
    """Pick the live option that best matches the case's simulated answer.

    The case declares intent as free text because the model may phrase
    clarification options differently across runs. Selection is deterministic:
    exact/substring matches win first, then token-overlap score. If nothing
    matches, option 0 is used so the workflow can continue, but the choice is
    marked unreliable.
    """
    options = tuple(option.strip() for option in decision.options if option.strip())
    if not options:
        return 0, "", False, "clarification had no selectable options"

    target = (case.simulated_user_answer or "").strip()
    if not case.should_clarify or not target:
        return (
            0,
            options[0],
            False,
            "case did not declare a simulated clarification answer",
        )

    normalized_target = _normalize_for_match(target)
    if not normalized_target:
        return 0, options[0], False, "simulated answer had no matchable text"

    for index, option in enumerate(options):
        normalized_option = _normalize_for_match(option)
        if (
            normalized_target == normalized_option
            or normalized_target in normalized_option
            or normalized_option in normalized_target
        ):
            return index, option, True, None

    target_tokens = set(normalized_target.split())
    best_index = 0
    best_score = 0
    for index, option in enumerate(options):
        option_tokens = set(_normalize_for_match(option).split())
        score = len(target_tokens & option_tokens)
        if score > best_score:
            best_index = index
            best_score = score

    if best_score > 0:
        return best_index, options[best_index], True, None
    return (
        0,
        options[0],
        False,
        "no clarification option matched the simulated answer",
    )


def _normalize_for_match(value: str) -> str:
    """Lowercase text and keep alphanumeric token boundaries for matching."""
    chars = [
        char.lower() if char.isalnum() else " "
        for char in value
    ]
    return " ".join("".join(chars).split())


def _unexpected_clarification_reason(
    case: MimicCase,
    decision: AmbiguityDecision,
    *,
    is_first: bool,
) -> str | None:
    """Return a reliability warning when the clarification was not declared."""
    if not case.should_clarify:
        return "case should not clarify but full pipeline asked a question"
    if not is_first:
        return "additional clarification beyond the case's declared answer"
    if case.ambiguity_type == "underspecified":
        return None
    if decision.mechanism != case.ambiguity_type:
        actual = decision.mechanism or "candidate-comparison"
        return (
            f"case expects {case.ambiguity_type} clarification but got {actual}"
        )
    return None


def run_full_simulated(
    case: MimicCase,
    schema: SchemaMetadata,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Run the full arm, answering pending clarifications automatically."""
    clarifications: tuple[str, ...] = ()
    asked: list[dict[str, Any]] = []
    unreliable_reasons: list[str] = []
    last_workflow: QueryWorkflowResult | None = None

    max_iterations = application.max_iterations
    for iteration in range(1, max_iterations + 1):
        workflow = application.submit_query(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
            candidate_count=application.candidates_per_iteration,
        )
        last_workflow = workflow
        if workflow.state != ComponentState.PENDING:
            return {
                "workflow": workflow_result_payload(workflow),
                "clarifications": asked,
                "termination": "complete"
                if workflow.state == ComponentState.ACCEPTED
                else "failed",
                "unreliable": bool(unreliable_reasons),
                "unreliable_reasons": unreliable_reasons,
            }

        decision = workflow.ambiguity
        if decision is None:
            unreliable_reasons.append(
                "workflow was pending without an ambiguity decision"
            )
            return {
                "workflow": workflow_result_payload(workflow),
                "clarifications": asked,
                "termination": "pending_without_decision",
                "unreliable": True,
                "unreliable_reasons": unreliable_reasons,
            }

        index, chosen, matched, match_reason = choose_clarification_option(
            case,
            decision,
        )
        declared_reason = _unexpected_clarification_reason(
            case,
            decision,
            is_first=(len(asked) == 0),
        )
        if declared_reason is not None:
            unreliable_reasons.append(declared_reason)
        if match_reason is not None:
            unreliable_reasons.append(match_reason)

        asked.append(
            {
                "question": decision.question,
                "options": list(decision.options),
                "mechanism": decision.mechanism,
                "reason": decision.reason,
                "chosen_index": index,
                "chosen": chosen,
                "matched_simulated_answer": matched,
                "declared": declared_reason is None and matched,
                "simulated_user_answer": case.simulated_user_answer,
            }
        )
        if not chosen:
            return {
                "workflow": workflow_result_payload(workflow),
                "clarifications": asked,
                "termination": "unanswerable_clarification",
                "unreliable": True,
                "unreliable_reasons": unreliable_reasons,
            }

        clarifications = clarifications + (
            format_clarification(decision.question or "", chosen),
        )

    return {
        "workflow": workflow_result_payload(last_workflow)
        if last_workflow is not None
        else None,
        "clarifications": asked,
        "termination": "max_iterations",
        "unreliable": True,
        "unreliable_reasons": unreliable_reasons
        + ["full pipeline did not complete within max_iterations"],
    }


def evaluate_case_raw(
    case: MimicCase,
    schema: SchemaMetadata,
    query_service: QueryService,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Run both arms once and return raw, JSON-friendly outputs."""
    baseline_started = perf_counter()
    baseline_result = run_baseline_once(
        case, schema, query_service, api_key, model
    )
    baseline_duration = perf_counter() - baseline_started

    full_started = perf_counter()
    full_outcome = run_full_simulated(case, schema, application, api_key, model)
    full_duration = perf_counter() - full_started

    return {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "ambiguous": case.ambiguous,
        "ambiguity_type": case.ambiguity_type,
        "intent": case.intent,
        "schema_elements": list(case.schema_elements),
        "expected_sql": case.expected_sql,
        "should_clarify": case.should_clarify,
        "simulated_user_answer": case.simulated_user_answer,
        "tests": list(case.tests),
        "baseline": {
            "duration_seconds": round(baseline_duration, 4),
            "result": query_result_payload(baseline_result),
        },
        "full": {
            "duration_seconds": round(full_duration, 4),
            **full_outcome,
        },
    }


def query_result_payload(result: QueryResult | None) -> dict[str, Any] | None:
    """Convert a QueryResult into JSON-friendly data."""
    if result is None:
        return None
    return {
        "state": result.state.value,
        "message": result.message,
        "sql": result.sql,
        "table": table(result.columns, result.rows),
        "truncated": result.truncated,
    }


def workflow_result_payload(result: QueryWorkflowResult) -> dict[str, Any]:
    """Convert a QueryWorkflowResult into JSON-friendly data."""
    return {
        "state": result.state.value,
        "message": result.message,
        "iteration": result.iteration,
        "complete": result.complete,
        "query_result": query_result_payload(result.query_result),
        "candidates": [
            {
                "attempt_number": candidate.attempt_number,
                "state": candidate.state.value,
                "sql": candidate.sql,
                "message": candidate.message,
            }
            for candidate in result.candidates
        ],
        "ambiguity": ambiguity_payload(result.ambiguity),
    }


def ambiguity_payload(
    decision: AmbiguityDecision | None,
) -> dict[str, Any] | None:
    """Convert an AmbiguityDecision into JSON-friendly data."""
    if decision is None:
        return None
    return {
        "state": decision.state.value,
        "passed": decision.passed,
        "question": decision.question,
        "options": list(decision.options),
        "reason": decision.reason,
        "mechanism": decision.mechanism,
    }


def build_report(
    suite: MimicSuite,
    schema: SchemaMetadata,
    case_results: list[dict[str, Any]],
    *,
    run_id: str,
    model: str,
    started_at: datetime,
    completed_at: datetime,
    prompt_log_path: Path,
) -> dict[str, Any]:
    """Assemble the Iteration 2 raw-output report."""
    return {
        "run_id": run_id,
        "suite": suite.name,
        "dataset": str(suite.dataset_path),
        "tested_model": model,
        "judge": suite.judge,
        "candidate_count": suite.candidate_count,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "prompt_log": str(prompt_log_path),
        "stage": "iteration_3_clarification_simulation",
        "scoring": {
            "deterministic_scores_available": False,
            "self_judge_available": False,
            "notes": (
                "Iteration 3 records raw outputs and simulated clarification "
                "history. Scoring is added in a later iteration."
            ),
        },
        "schema": {
            "database_path": schema.database_path,
            "table_count": len(schema.table_names),
            "relationship_count": len(schema.relationships),
            "discovery_complete": schema.discovery_complete,
            "discovery_notes": list(schema.discovery_notes),
        },
        "cases": case_results,
    }


def run_suite_raw(
    suite: MimicSuite,
    *,
    api_key: str,
    model: str,
    database_path: Path,
    prompt_log_path: Path,
    limit: int | None = None,
    query_service: QueryService | None = None,
    application: ApplicationService | None = None,
    etl_service: ETLService | None = None,
) -> tuple[SchemaMetadata, list[dict[str, Any]]]:
    """Ingest the suite dataset and run raw baseline/full outputs."""
    etler = etl_service or ETLService(database_path=database_path)
    ingestion = etler.ingest(dataset_uploads(suite.dataset_path))
    if ingestion.state != ComponentState.ACCEPTED:
        raise RuntimeError(f"Ingestion failed: {ingestion.message}")

    schema = ingestion.schema
    query = query_service or QueryService()
    app = application or ApplicationService(
        etler=ETLService(database_path=database_path),
        candidates_per_iteration=suite.candidate_count,
        enable_join_path_detection=True,
        enable_semantic_column_detection=True,
        event_logger=PromptLogger(prompt_log_path),
    )

    selected_cases = suite.cases[:limit] if limit is not None else suite.cases
    results = [
        evaluate_case_raw(case, schema, query, app, api_key, model)
        for case in selected_cases
    ]
    return schema, results


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the MIMIC DBWhisperer A/B skeleton benchmark.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--model",
        default=os.getenv("OPENROUTER_MODEL") or "google/gemma-4-31b-it",
        help="OpenRouter model used by both arms.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional number of cases to run; useful for smoke tests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_env_file(BENCHMARK_DIR / ".env")
    args = _parse_args(argv)
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is required.", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("A tested model is required.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit must be positive when provided.", file=sys.stderr)
        return 2

    try:
        suite = load_mimic_suite(args.cases)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid benchmark: {error}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = BENCHMARK_DIR / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / f"mimic_ab_{run_id}.duckdb"
    prompt_log_path = output_dir / f"mimic_ab_{run_id}.prompts.jsonl"
    os.environ["DB_WHISPERER_PROMPT_LOG"] = str(prompt_log_path)

    try:
        schema, case_results = run_suite_raw(
            suite,
            api_key=api_key,
            model=args.model,
            database_path=database_path,
            prompt_log_path=prompt_log_path,
            limit=args.limit,
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    completed_at = datetime.now(timezone.utc)
    report = build_report(
        suite,
        schema,
        case_results,
        run_id=run_id,
        model=args.model,
        started_at=started_at,
        completed_at=completed_at,
        prompt_log_path=prompt_log_path,
    )
    report_path = output_dir / f"mimic_ab_{run_id}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=True, default=str) + "\n",
        encoding="utf-8",
    )

    print(f"Suite: {suite.name} ({len(case_results)} case(s) run)")
    print(f"Discovery complete: {schema.discovery_complete}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
