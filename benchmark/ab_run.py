"""Controlled A/B evaluation: full pipeline (Component B) vs baseline.

This harness answers the project's reframed research question -- does an explicit
schema-graph ambiguity-detection layer, positioned before SQL generation,
improve interpretive accuracy versus a single-pass LLM-to-SQL baseline? It runs
the same questions through two architectures against one shared DuckDB database:

* baseline -- ``QueryService`` directly: one prompt, straight to SQL, no
  Component B (the PDF's "skip B and go straight to SQL generation").
* full -- ``ApplicationService`` with join-path ambiguity detection enabled:
  the complete Component B (join-path primary mechanism plus the
  candidate-comparison judge). When the pipeline asks a clarifying question, a
  simulated user answers it by choosing the interpretation each case declares,
  exactly as a real user clicking one of the two buttons would.

Both arms are scored against an author-written gold query (exact table match,
else an LLM judge on the 0-4 rubric). For each case the harness records whether
the full pipeline beat, tied, or lost to the baseline, whether it asked a
question, and which mechanism produced it.

It deliberately does NOT score "user trust": that is assessed by a human reading
the recorded clarification questions, which the report preserves verbatim.

Reports and prompt logs may contain dataset values and generated SQL. Treat the
entire output directory as sensitive. API keys are only sent in request headers.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Callable

import duckdb
import requests


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

from _harness import (  # noqa: E402
    DEFAULT_MAX_REFERENCE_ROWS,
    execute_reference,
    exact_match,
    format_clarification,
    judge as judge_result,
    load_env_file,
    table,
)


DEFAULT_CASES_PATH = BENCHMARK_DIR / "ab_cases.json"
DEFAULT_OUTPUT_DIR = BENCHMARK_DIR / "results"
DEFAULT_CANDIDATE_COUNT = 3


# --------------------------------------------------------------------------
# Suite loading and validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AbCase:
    """One A/B benchmark case."""

    id: str
    question: str
    expected_sql: str
    ambiguous: bool
    clarification_path_index: int | None = None
    intent: str = ""
    entity_pair: tuple[str, ...] = ()


@dataclass(frozen=True)
class AbSuite:
    """A loaded, validated A/B benchmark suite."""

    name: str
    dataset_path: Path
    candidate_count: int
    cases: tuple[AbCase, ...]


def _dataset_uploads(dataset_path: Path) -> list[CsvUpload]:
    """Read a dataset: every CSV in a directory, or one CSV file.

    Mirrors the GUI's bundled-dataset loader so a multi-CSV relational dataset
    (the only kind that can exhibit join-path ambiguity) ingests identically to
    the live application.
    """
    if dataset_path.is_dir():
        csv_paths = sorted(dataset_path.glob("*.csv"))
        if not csv_paths:
            raise ValueError(f"Dataset directory has no CSV files: {dataset_path}")
        return [
            CsvUpload(name=path.name, content=path.read_bytes())
            for path in csv_paths
        ]
    return [CsvUpload(name=dataset_path.name, content=dataset_path.read_bytes())]


def load_ab_suite(path: Path) -> AbSuite:
    """Load and strictly validate an A/B benchmark file.

    Every case must declare ``ambiguous``; an ambiguous case must declare which
    interpretation the simulated user picks (``clarification_path_index`` of 0
    or 1), and a non-ambiguous case must not. This makes the simulated-user
    contract explicit per case instead of inferred.
    """
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

    normalized: list[AbCase] = []
    seen_ids: set[str] = set()
    for raw in cases:
        case = _normalize_case(raw)
        if case.id in seen_ids:
            raise ValueError(f"Duplicate benchmark case ID: {case.id}")
        seen_ids.add(case.id)
        normalized.append(case)

    return AbSuite(
        name=name.strip(),
        dataset_path=dataset_path,
        candidate_count=candidate_count,
        cases=tuple(normalized),
    )


def _normalize_case(raw: Any) -> AbCase:
    if not isinstance(raw, dict):
        raise ValueError("Every benchmark case must be an object.")

    values: dict[str, str] = {}
    for field_name in ("id", "question", "expected_sql"):
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Every benchmark case requires non-empty {field_name}."
            )
        values[field_name] = value.strip()

    ambiguous = raw.get("ambiguous")
    if not isinstance(ambiguous, bool):
        raise ValueError(
            f"Case {values['id']} must declare a boolean 'ambiguous'."
        )

    path_index = raw.get("clarification_path_index")
    if ambiguous:
        if path_index not in (0, 1) or isinstance(path_index, bool):
            raise ValueError(
                f"Ambiguous case {values['id']} requires "
                "clarification_path_index of 0 or 1."
            )
    elif path_index is not None:
        raise ValueError(
            f"Non-ambiguous case {values['id']} must not set "
            "clarification_path_index."
        )

    intent = raw.get("intent", "")
    if not isinstance(intent, str):
        raise ValueError(f"Case {values['id']} intent must be text.")

    entity_pair_raw = raw.get("entity_pair", [])
    if not isinstance(entity_pair_raw, list) or not all(
        isinstance(item, str) for item in entity_pair_raw
    ):
        raise ValueError(
            f"Case {values['id']} entity_pair must be a list of table names."
        )

    return AbCase(
        id=values["id"],
        question=values["question"],
        expected_sql=values["expected_sql"],
        ambiguous=ambiguous,
        clarification_path_index=path_index if ambiguous else None,
        intent=intent.strip(),
        entity_pair=tuple(entity_pair_raw),
    )


# --------------------------------------------------------------------------
# Scoring (shared by both arms)
# --------------------------------------------------------------------------

JudgeFn = Callable[[str, dict[str, Any], dict[str, Any]], tuple[int, str]]


def score_result(
    question: str,
    expected_columns: tuple[str, ...],
    expected_rows: tuple[tuple[Any, ...], ...],
    result: QueryResult,
    judge_fn: JudgeFn,
) -> dict[str, Any]:
    """Score one executed result against the reference table.

    Exact column-and-row equality earns 4 with no judge call. Any other
    accepted result is sent to ``judge_fn``. A failed query scores 0. A judge
    error yields an unscored result (``score`` of ``None``) rather than a
    silent 0, so judge outages do not masquerade as model failures.
    """
    actual = table(result.columns, result.rows)
    scored: dict[str, Any] = {
        "generated_sql": result.sql,
        "actual": actual,
        "exact_match": False,
        "comparison": "system_failure",
        "score": 0,
        "reason": result.message,
        "error": None,
    }
    if result.state != ComponentState.ACCEPTED:
        return scored

    if exact_match(
        result.columns, result.rows, expected_columns, expected_rows
    ):
        scored.update(
            exact_match=True,
            comparison="exact",
            score=4,
            reason="Generated table exactly matches the reference table.",
        )
        return scored

    expected = table(expected_columns, expected_rows)
    try:
        score, reason = judge_fn(question, expected, actual)
        scored.update(comparison="judge", score=score, reason=reason)
    except (
        requests.RequestException,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        scored.update(
            comparison="judge_failure",
            score=None,
            reason="Judge could not score the result.",
            error=str(error),
        )
    return scored


# --------------------------------------------------------------------------
# The two arms
# --------------------------------------------------------------------------


def run_baseline(
    case: AbCase,
    schema: SchemaMetadata,
    query_service: QueryService,
    api_key: str,
    model: str,
) -> QueryResult:
    """Single-pass arm: one prompt straight to SQL, no Component B."""
    return query_service.query(
        QueryRequest(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
        )
    )


def pick_option_index(case: AbCase, decision: AmbiguityDecision) -> int:
    """Choose which clarification option the simulated user clicks.

    For join-path clarifications the options are path-ordered (index 0 is the
    shortest/most-direct interpretation, index 1 the longer one through an
    intermediate), so a case declares its intended interpretation as an index.
    If a case did not declare one (a control case that was unexpectedly asked),
    default to 0; the caller records the mechanism so an unexpected ask is never
    hidden.
    """
    index = case.clarification_path_index
    if index in (0, 1) and len(decision.options) == 2:
        return index
    return 0


@dataclass
class FullArmOutcome:
    """The full pipeline's final result plus its clarification trail."""

    result: QueryResult
    clarifications_asked: tuple[dict[str, Any], ...]
    termination: str
    unreliable_reasons: tuple[str, ...] = ()

    @property
    def asked_question(self) -> bool:
        return bool(self.clarifications_asked)

    @property
    def unreliable(self) -> bool:
        """True if the simulated user had to answer an undeclared question."""
        return bool(self.unreliable_reasons)


def _undeclared_cause(
    case: AbCase,
    is_first: bool,
    decision: AmbiguityDecision,
) -> str | None:
    """Explain why a clarification falls outside what the case declared.

    A case declares exactly one answer -- ``clarification_path_index`` -- for the
    first join-path clarification, whose options are path-ordered. Any other
    clarification cannot be answered faithfully by that single index, so it is
    reported rather than silently defaulted. Returns ``None`` when the
    clarification is the declared one.
    """
    if case.clarification_path_index is None:
        return "case is not marked ambiguous but was asked a clarification"
    if not is_first:
        return "additional clarification beyond the one the case declared"
    if decision.mechanism != "join-path":
        return (
            f"mechanism is '{decision.mechanism or 'candidate-comparison'}', "
            "whose options are not path-ordered, so the declared index does "
            "not apply"
        )
    return None


def run_full(
    case: AbCase,
    schema: SchemaMetadata,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> FullArmOutcome:
    """Full-pipeline arm: drive the clarification loop to a final result.

    Repeats the GUI's submit/clarify cycle automatically: submit the question,
    and while the pipeline returns a pending two-option clarification, pick the
    interpretation the case declares, format it as the GUI would, and resubmit
    on the next iteration. Stops at the first complete or failed result, or at
    the application's iteration cap.

    The case declares one answer for one join-path clarification. If the
    pipeline asks anything else -- a later clarification, a clarification on a
    control case, or a non-join-path mechanism whose options are not
    path-ordered -- the simulated user cannot answer it faithfully, so the run
    is flagged ``unreliable`` (with a recorded reason) and option 0 is used to
    keep the loop moving, rather than silently treating an arbitrary answer as
    the user's intent.
    """
    clarifications: tuple[str, ...] = ()
    asked: list[dict[str, Any]] = []
    unreliable_reasons: list[str] = []
    max_iterations = application.max_iterations

    last_result: QueryResult | None = None
    for iteration in range(1, max_iterations + 1):
        workflow: QueryWorkflowResult = application.submit_query(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
            candidate_count=application.candidates_per_iteration,
        )
        last_result = workflow.query_result

        pending_clarification = (
            workflow.state == ComponentState.PENDING
            and not workflow.complete
            and workflow.ambiguity is not None
            and len(workflow.ambiguity.options) == 2
        )
        if not pending_clarification:
            termination = (
                "failed"
                if workflow.state == ComponentState.FAILED
                else "complete"
            )
            return FullArmOutcome(
                result=last_result or _failed_result(workflow.message),
                clarifications_asked=tuple(asked),
                termination=termination,
                unreliable_reasons=tuple(unreliable_reasons),
            )

        decision = workflow.ambiguity
        assert decision is not None  # narrowed by pending_clarification
        cause = _undeclared_cause(case, len(asked) == 0, decision)
        if cause is None:
            index = pick_option_index(case, decision)
        else:
            index = 0
            unreliable_reasons.append(f"iteration {iteration}: {cause}")
        choice = decision.options[index]
        asked.append(
            {
                "iteration": iteration,
                "mechanism": decision.mechanism or "candidate-comparison",
                "question": decision.question,
                "options": list(decision.options),
                "chosen_index": index,
                "chosen": choice,
                "declared": cause is None,
                "reason": decision.reason,
            }
        )
        clarifications = (
            *clarifications,
            format_clarification(decision.question or "", choice),
        )

    return FullArmOutcome(
        result=last_result or _failed_result("No result produced."),
        clarifications_asked=tuple(asked),
        termination="max_iterations",
        unreliable_reasons=tuple(unreliable_reasons),
    )


def _failed_result(message: str) -> QueryResult:
    return QueryResult(state=ComponentState.FAILED, message=message)


# --------------------------------------------------------------------------
# Per-case evaluation and comparison
# --------------------------------------------------------------------------


def compare_scores(
    baseline_score: int | None,
    full_score: int | None,
) -> str:
    """Classify the full pipeline against the baseline for one case."""
    if baseline_score is None or full_score is None:
        return "unscored"
    if full_score > baseline_score:
        return "full_better"
    if full_score < baseline_score:
        return "baseline_better"
    return "tie"


def evaluate_case(
    case: AbCase,
    schema: SchemaMetadata,
    query_service: QueryService,
    application: ApplicationService,
    api_key: str,
    model: str,
    judge_fn: JudgeFn,
    max_reference_rows: int = DEFAULT_MAX_REFERENCE_ROWS,
) -> dict[str, Any]:
    """Run both arms for one case and compare them against the gold answer."""
    expected_columns, expected_rows = execute_reference(
        schema.database_path,
        case.expected_sql,
        max_reference_rows,
    )

    baseline_started = perf_counter()
    baseline_result = run_baseline(
        case, schema, query_service, api_key, model
    )
    baseline_duration = perf_counter() - baseline_started
    baseline = score_result(
        case.question, expected_columns, expected_rows, baseline_result, judge_fn
    )
    baseline["duration_seconds"] = round(baseline_duration, 4)

    full_started = perf_counter()
    full_outcome = run_full(case, schema, application, api_key, model)
    full_duration = perf_counter() - full_started
    full = score_result(
        case.question,
        expected_columns,
        expected_rows,
        full_outcome.result,
        judge_fn,
    )
    full["duration_seconds"] = round(full_duration, 4)
    full["clarification_asked"] = full_outcome.asked_question
    full["clarifications"] = list(full_outcome.clarifications_asked)
    full["termination"] = full_outcome.termination
    full["mechanisms"] = sorted(
        {entry["mechanism"] for entry in full_outcome.clarifications_asked}
    )
    full["unreliable"] = full_outcome.unreliable
    full["unreliable_reasons"] = list(full_outcome.unreliable_reasons)

    comparison = compare_scores(baseline["score"], full["score"])
    score_delta = (
        full["score"] - baseline["score"]
        if baseline["score"] is not None and full["score"] is not None
        else None
    )
    return {
        "id": case.id,
        "question": case.question,
        "ambiguous": case.ambiguous,
        "intent": case.intent,
        "entity_pair": list(case.entity_pair),
        "clarification_path_index": case.clarification_path_index,
        "expected_sql": case.expected_sql,
        "expected": table(expected_columns, expected_rows),
        "baseline": baseline,
        "full": full,
        "comparison": comparison,
        "score_delta": score_delta,
    }


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _arm_breakdown(case_results: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    scores = [
        result[arm]["score"]
        for result in case_results
        if isinstance(result[arm]["score"], int)
    ]
    mean = _mean(scores)
    return {
        "scored": len(scores),
        "mean_score": _round(mean),
        "normalized_percentage": _round(mean / 4 * 100, 2)
        if mean is not None
        else None,
    }


def _comparison_counts(case_results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "full_better": 0,
        "tie": 0,
        "baseline_better": 0,
        "unscored": 0,
    }
    for result in case_results:
        counts[result["comparison"]] += 1
    return counts


def summarize(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-arm accuracy and the head-to-head comparison.

    Reported separately for ambiguous and control cases, because the layer is
    expected to *help* on ambiguous questions and to *not hurt* (and not
    over-ask) on unambiguous ones -- two different claims that a single average
    would blur.
    """
    ambiguous = [r for r in case_results if r["ambiguous"]]
    control = [r for r in case_results if not r["ambiguous"]]

    full_asked_ambiguous = sum(
        1 for r in ambiguous if r["full"].get("clarification_asked")
    )
    full_asked_control = sum(
        1 for r in control if r["full"].get("clarification_asked")
    )
    # Cases where the simulated user had to answer a clarification the case did
    # not declare; their comparison should be read with caution.
    unreliable_cases = [
        r["id"] for r in case_results if r["full"].get("unreliable")
    ]

    return {
        "total_cases": len(case_results),
        "unreliable_cases": unreliable_cases,
        "baseline": _arm_breakdown(case_results, "baseline"),
        "full": _arm_breakdown(case_results, "full"),
        "overall_comparison": _comparison_counts(case_results),
        "ambiguous": {
            "count": len(ambiguous),
            "comparison": _comparison_counts(ambiguous),
            "baseline": _arm_breakdown(ambiguous, "baseline"),
            "full": _arm_breakdown(ambiguous, "full"),
            "clarification_rate": _round(
                full_asked_ambiguous / len(ambiguous), 4
            )
            if ambiguous
            else None,
        },
        "control": {
            "count": len(control),
            "comparison": _comparison_counts(control),
            "baseline": _arm_breakdown(control, "baseline"),
            "full": _arm_breakdown(control, "full"),
            "spurious_clarification_rate": _round(
                full_asked_control / len(control), 4
            )
            if control
            else None,
        },
    }


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the DB Whisperer A/B (full vs baseline) benchmark.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--model",
        default=os.getenv("OPENROUTER_MODEL") or "google/gemma-4-31b-it",
        help="OpenRouter model used to generate SQL in both arms.",
    )
    parser.add_argument(
        "--judge-model",
        default=os.getenv("BENCHMARK_JUDGE_MODEL", ""),
        help="Separate OpenRouter model used to judge non-exact results.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def _print_summary(suite: AbSuite, summary: dict[str, Any]) -> None:
    baseline = summary["baseline"]
    full = summary["full"]
    print(f"\nSuite: {suite.name}  ({summary['total_cases']} cases)")
    print(
        f"  Baseline mean: {baseline['mean_score']}/4 "
        f"({baseline['normalized_percentage']}%)  "
        f"Full mean: {full['mean_score']}/4 "
        f"({full['normalized_percentage']}%)"
    )
    amb = summary["ambiguous"]
    ctl = summary["control"]
    print(
        f"  Ambiguous ({amb['count']}): "
        f"full_better={amb['comparison']['full_better']}, "
        f"tie={amb['comparison']['tie']}, "
        f"baseline_better={amb['comparison']['baseline_better']}; "
        f"clarification_rate={amb['clarification_rate']}"
    )
    print(
        f"  Control ({ctl['count']}): "
        f"full_better={ctl['comparison']['full_better']}, "
        f"tie={ctl['comparison']['tie']}, "
        f"baseline_better={ctl['comparison']['baseline_better']}; "
        f"spurious_clarification_rate={ctl['spurious_clarification_rate']}"
    )
    unreliable = summary["unreliable_cases"]
    if unreliable:
        print(
            f"  WARNING: {len(unreliable)} case(s) answered an undeclared "
            f"clarification (read with caution): {', '.join(unreliable)}"
        )


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
    if not args.judge_model.strip():
        print(
            "BENCHMARK_JUDGE_MODEL or --judge-model is required.",
            file=sys.stderr,
        )
        return 2

    try:
        suite = load_ab_suite(args.cases)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid benchmark: {error}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = BENCHMARK_DIR / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / f"ab_{run_id}.duckdb"
    prompt_log_path = output_dir / f"ab_{run_id}.prompts.jsonl"
    os.environ["DB_WHISPERER_PROMPT_LOG"] = str(prompt_log_path)

    ingestion = ETLService(database_path=database_path).ingest(
        _dataset_uploads(suite.dataset_path)
    )
    if ingestion.state != ComponentState.ACCEPTED:
        print(f"Ingestion failed: {ingestion.message}", file=sys.stderr)
        return 1
    schema = ingestion.schema
    if not schema.discovery_complete:
        # Visible incomplete state: discovery gaps can suppress real join-path
        # ambiguity, so the operator must know the graph may be partial.
        print(
            "WARNING: relationship discovery was incomplete; some join paths "
            "may be missing:\n  - "
            + "\n  - ".join(schema.discovery_notes),
            file=sys.stderr,
        )

    query_service = QueryService()
    application = ApplicationService(
        etler=ETLService(database_path=database_path),
        candidates_per_iteration=suite.candidate_count,
        enable_join_path_detection=True,
        event_logger=PromptLogger(prompt_log_path),
    )

    def judge_fn(
        question: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> tuple[int, str]:
        return judge_result(
            api_key, args.judge_model, question, expected, actual
        )

    case_results: list[dict[str, Any]] = []
    for case in suite.cases:
        try:
            result = evaluate_case(
                case,
                schema,
                query_service,
                application,
                api_key,
                args.model,
                judge_fn,
            )
        except (duckdb.Error, OSError, ValueError) as error:
            print(
                f"Invalid reference for {case.id}: {error}",
                file=sys.stderr,
            )
            return 2
        case_results.append(result)
        _print_case_line(result)

    summary = summarize(case_results)
    completed_at = datetime.now(timezone.utc)
    report = {
        "run_id": run_id,
        "suite": suite.name,
        "dataset": str(suite.dataset_path),
        "tested_model": args.model,
        "judge_model": args.judge_model,
        "candidate_count": suite.candidate_count,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "prompt_log": str(prompt_log_path),
        "discovery_complete": schema.discovery_complete,
        "summary": summary,
        "cases": case_results,
    }
    report_path = output_dir / f"ab_{run_id}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    _print_summary(suite, summary)
    print(f"Report: {report_path}")
    return 0


def _print_case_line(result: dict[str, Any]) -> None:
    def score_text(arm: dict[str, Any]) -> str:
        return f"{arm['score']}/4" if arm["score"] is not None else "unscored"

    tag = "AMBIG" if result["ambiguous"] else "CTRL"
    asked = "Q" if result["full"].get("clarification_asked") else "-"
    flag = " UNRELIABLE" if result["full"].get("unreliable") else ""
    print(
        f"[{tag} {asked}] {result['id']}: "
        f"baseline={score_text(result['baseline'])} "
        f"full={score_text(result['full'])} "
        f"-> {result['comparison']}{flag}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
