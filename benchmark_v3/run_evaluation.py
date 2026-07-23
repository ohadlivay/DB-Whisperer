"""Run the final, progress-visible Evaluation V3.1 campaign."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Lock, local
import time
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_v3.contracts import ARMS, EvaluationCase, EvaluationSuite, load_suite
from benchmark_v3.scoring import (
    SCORING_VERSION,
    choose_intended_option,
    score_arm_case,
    score_etl_manifest,
)
from db_whisperer.ambiguity import AmbiguityPromptBuilder, AmbiguityService, SemanticColumnAmbiguityService
from db_whisperer.application import ApplicationService
from db_whisperer.contracts import ComponentState, CsvUpload, QueryCandidate, QueryRequest, QueryResult
from db_whisperer.etler import ETLService
from db_whisperer.prompt_logging import PromptLogger
from db_whisperer.querier import QueryService


BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = BENCHMARK_DIR / "cases" / "evaluation_cases.json"
DEFAULT_OUTPUT = BENCHMARK_DIR / "results" / "evaluation_v3_1.json"
DEFAULT_WORKERS = 2
MAX_WORKERS = 8


@dataclass(frozen=True)
class EvaluationJob:
    repetition: int
    case_index: int
    case: EvaluationCase
    arm_index: int
    arm: str

    @property
    def order(self) -> tuple[int, int, int]:
        return self.repetition, self.case_index, self.arm_index


def _duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def _safe_error(error: BaseException | str | None) -> str:
    if error is None:
        return ""
    if isinstance(error, BaseException):
        return type(error).__name__
    text = str(error).replace("\r", " ").replace("\n", " ")
    return text[:300]


class ProgressReporter:
    """Thread-safe console and sanitized JSONL campaign progress."""

    def __init__(self, path: Path, total: int) -> None:
        self.path = path
        self.total = total
        self.completed = 0
        self.started = time.monotonic()
        self.lock = Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def stage(self, name: str) -> None:
        print(f"[stage] {name}", flush=True)
        self._write({"event": "stage", "timestamp": _timestamp(), "stage": name})

    def record(self, job: EvaluationJob, row: dict[str, Any], seconds: float, error: str = "") -> None:
        with self.lock:
            self.completed += 1
            elapsed = time.monotonic() - self.started
            eta = (
                _duration((elapsed / self.completed) * (self.total - self.completed))
                if self.completed >= 4 else "calculating"
            )
            state = row["result"]["state"]
            passed = bool(row["score"]["passed"])
            percent = 100 * self.completed / self.total
            print(
                f"[{self.completed:03d}/{self.total} {percent:4.1f}%] "
                f"rep {job.repetition}/{row['repetitions']} | {job.case.id} | {job.arm} | "
                f"{state} | {'pass' if passed else 'fail'} | elapsed {_duration(elapsed)} | ETA {eta}",
                flush=True,
            )
            self._write({
                "event": "evaluation_completed",
                "timestamp": _timestamp(),
                "completed": self.completed,
                "total": self.total,
                "percent": round(percent, 1),
                "repetition": job.repetition,
                "case_id": job.case.id,
                "arm": job.arm,
                "duration_seconds": round(seconds, 3),
                "state": state,
                "passed": passed,
                "error": _safe_error(error),
            })

    def _write(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_services(candidate_count: int) -> tuple[QueryService, dict[str, ApplicationService]]:
    query = QueryService(max_result_rows=1000)

    def application(*, semantic: bool, schema: bool, relationships: bool, candidates: bool) -> ApplicationService:
        ambiguity = AmbiguityService(prompt_builder=AmbiguityPromptBuilder(
            include_semantic_findings=semantic,
            include_schema_context=schema,
            include_relationships=relationships,
            include_candidate_evidence=candidates,
        ))
        return ApplicationService(
            querier=query,
            ambiguity=ambiguity,
            semantic_column=SemanticColumnAmbiguityService(),
            candidates_per_iteration=candidate_count,
            max_parallel_candidates=candidate_count,
            enable_semantic_column_detection=semantic,
        )

    return query, {
        "candidate_only": application(semantic=False, schema=False, relationships=False, candidates=True),
        "semantic_only": application(semantic=True, schema=True, relationships=False, candidates=False),
        "full": application(semantic=True, schema=True, relationships=True, candidates=True),
    }


def competing_intents(case: EvaluationCase, suite: EvaluationSuite) -> tuple[EvaluationCase, ...]:
    return tuple(
        item for item in suite.query_cases
        if item.ambiguous and item.family_id == case.family_id and item.id != case.id
    )


def choose_option(
    case: EvaluationCase,
    options: tuple[str, ...],
    competitors: tuple[EvaluationCase, ...] = (),
) -> tuple[str, str]:
    return choose_intended_option(case, competitors, options)


def run_application(
    case: EvaluationCase,
    competitors: tuple[EvaluationCase, ...],
    schema: Any,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> tuple[QueryResult | None, list[dict[str, Any]]]:
    clarifications: tuple[str, ...] = ()
    history: list[dict[str, Any]] = []
    for iteration in range(1, application.max_iterations + 1):
        workflow = application.submit_query(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
        )
        if workflow.complete or workflow.state == ComponentState.FAILED:
            if history:
                decision = workflow.ambiguity
                history[-1]["compliance_passed"] = bool(decision and decision.compliance_passed is True)
                history[-1]["compliant_alternatives"] = list(decision.compliant_alternatives) if decision else []
            return workflow.query_result, history
        decision = workflow.ambiguity
        if decision is None or len(decision.options) != 2:
            return workflow.query_result, history
        chosen, status = choose_option(case, decision.options, competitors)
        history.append({
            "question": decision.question,
            "options": list(decision.options),
            "chosen": chosen,
            "matched_intent": status == "matched",
            "intent_status": status,
            "mechanism": decision.mechanism,
        })
        clarifications = (*clarifications, f"Question: {decision.question}\nSelected answer: {chosen}")
    return None, history


def ingest_dataset(dataset_path: Path, database_path: Path) -> Any:
    files = tuple(
        CsvUpload(path.name, path.read_bytes())
        for path in sorted(
            (path for path in dataset_path.rglob("*") if path.suffix.casefold() == ".csv"),
            key=lambda path: str(path).casefold(),
        )
    )
    if not files:
        raise RuntimeError(f"No CSV files found in {dataset_path}.")
    result = ETLService(database_path=database_path).ingest(files)
    if result.state != ComponentState.ACCEPTED:
        raise RuntimeError(result.message)
    return result.schema


def run_etl_cases(suite: EvaluationSuite, workdir: Path) -> list[dict[str, Any]]:
    records = []
    for case in suite.etl_cases:
        result = ETLService(database_path=workdir / f"{case.id}.duckdb").ingest(
            tuple(CsvUpload(path.name, path.read_bytes()) for path in case.fixture_files)
        )
        score = score_etl_manifest(result.schema, case.manifest or {}) if result.state == ComponentState.ACCEPTED else {"passed": False, "checks": []}
        records.append({"case_id": case.id, "state": result.state, "message": result.message, "score": score})
    return records


def expected_result(case: EvaluationCase, query: QueryService, database_path: str) -> QueryResult | None:
    return (
        query.execute_candidate(QueryCandidate(0, ComponentState.ACCEPTED, sql=case.expected_sql), database_path)
        if case.expected_sql else None
    )


def prepare_references(
    suite: EvaluationSuite,
    query: QueryService,
    database_path: str,
) -> dict[str, QueryResult | None]:
    """Execute each frozen gold query exactly once per campaign."""
    return {
        case.id: expected_result(case, query, database_path)
        for case in suite.query_cases
    }


def thread_local_provider(factory: Callable[[], Any]) -> Callable[[], Any]:
    """Return one lazily-created service bundle per executor thread."""
    storage = local()

    def provide() -> Any:
        if not hasattr(storage, "value"):
            storage.value = factory()
        return storage.value

    return provide


def _result_payload(result: QueryResult | None) -> dict[str, Any]:
    return {
        "state": result.state if result else "missing",
        "message": result.message if result else "",
        "sql": result.sql if result else None,
        "columns": list(result.columns) if result else [],
        "rows": [list(row) for row in result.rows] if result else [],
    }


def _failed_result(error: BaseException) -> QueryResult:
    return QueryResult(ComponentState.FAILED, _safe_error(error))


def execute_jobs(
    jobs: list[EvaluationJob],
    workers: int,
    evaluate: Callable[[EvaluationJob], dict[str, Any]],
    on_error: Callable[[EvaluationJob, Exception], dict[str, Any]],
    progress: ProgressReporter,
) -> list[dict[str, Any]]:
    completed: list[tuple[tuple[int, int, int], dict[str, Any]]] = []

    def timed(job: EvaluationJob) -> tuple[EvaluationJob, dict[str, Any], float, str]:
        started = time.monotonic()
        try:
            return job, evaluate(job), time.monotonic() - started, ""
        except Exception as error:
            return job, on_error(job, error), time.monotonic() - started, _safe_error(error)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-eval")
    try:
        futures = [executor.submit(timed, job) for job in jobs]
        for future in as_completed(futures):
            job, row, seconds, error = future.result()
            progress.record(job, row, seconds, error)
            completed.append((job.order, row))
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()
    return [row for _, row in sorted(completed, key=lambda item: item[0])]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def run(
    suite_path: Path,
    output: Path,
    api_key: str,
    workers: int = DEFAULT_WORKERS,
    progress_path: Path | None = None,
    render_html: bool = True,
) -> dict[str, Any]:
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
    suite = load_suite(suite_path)
    jobs = [
        EvaluationJob(repetition, case_index, case, arm_index, arm)
        for repetition in range(1, suite.repetitions + 1)
        for case_index, case in enumerate(suite.query_cases)
        for arm_index, arm in enumerate(ARMS)
    ]
    progress = ProgressReporter(
        progress_path or output.with_name(f"{output.stem}.progress.jsonl"),
        len(jobs),
    )
    prompt_log = PromptLogger().path
    print(f"Detailed prompt log: {prompt_log}", flush=True)
    print("Warning: the detailed prompt log can contain sensitive dataset samples.", flush=True)
    started_at = _timestamp()
    with tempfile.TemporaryDirectory(prefix="dbw-v3-1-") as temporary:
        workdir = Path(temporary)
        progress.stage("Dataset ingestion")
        schema = ingest_dataset(suite.dataset_path, workdir / "dataset.duckdb")
        progress.stage("ETL fixtures")
        etl_records = run_etl_cases(suite, workdir)
        progress.stage("Reference-query preparation")
        reference_query, _ = build_services(suite.candidate_count)
        references = prepare_references(suite, reference_query, schema.database_path or "")
        services = thread_local_provider(lambda: build_services(suite.candidate_count))

        def row_for(job: EvaluationJob, actual: QueryResult | None, history: list[dict[str, Any]]) -> dict[str, Any]:
            score = score_arm_case(job.case, job.arm, actual, references[job.case.id], history, schema)
            return {
                "run": job.repetition,
                "repetitions": suite.repetitions,
                "case_id": job.case.id,
                "family_id": job.case.family_id,
                "category": job.case.category,
                "arm": job.arm,
                "clarifications": history,
                "result": _result_payload(actual),
                "score": score,
            }

        def evaluate(job: EvaluationJob) -> dict[str, Any]:
            query, applications = services()
            if job.arm == "baseline":
                actual = query.query(QueryRequest(job.case.question, schema, api_key, suite.model))
                history: list[dict[str, Any]] = []
            else:
                actual, history = run_application(
                    job.case,
                    competing_intents(job.case, suite),
                    schema,
                    applications[job.arm],
                    api_key,
                    suite.model,
                )
            return row_for(job, actual, history)

        def on_error(job: EvaluationJob, error: Exception) -> dict[str, Any]:
            return row_for(job, _failed_result(error), [])

        progress.stage(f"Live campaign ({workers} workers)")
        records = execute_jobs(jobs, workers, evaluate, on_error, progress)
    report = {
        "report_type": "dbwhisperer_v3_evaluation",
        "scoring_version": SCORING_VERSION,
        "retrospective": False,
        "started_at": started_at,
        "completed_at": _timestamp(),
        "suite_version": suite.version,
        "suite_hash": suite.sha256,
        "model": suite.model,
        "workers": workers,
        "arms": list(ARMS),
        "case_contracts": [asdict(case) for case in suite.query_cases],
        "etl": etl_records,
        "records": records,
    }
    progress.stage("Final aggregation and report generation")
    _atomic_json(output, report)
    if render_html:
        from benchmark_v3.render_report import write_report
        write_report(output, output.with_suffix(".html"))
    return report


def worker_count(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("workers must be an integer") from error
    if not 1 <= workers <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"workers must be between 1 and {MAX_WORKERS}")
    return workers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=worker_count, default=DEFAULT_WORKERS)
    parser.add_argument("--progress-log", type=Path)
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required for a live V3.1 campaign.")
    run(args.suite.resolve(), args.output.resolve(), api_key, args.workers, args.progress_log)


if __name__ == "__main__":
    main()
