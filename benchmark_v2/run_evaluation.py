"""Run the deterministic, five-arm DBWhisperer Evaluation V2 campaign."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Any

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from db_whisperer.ambiguity import (  # noqa: E402
    AmbiguityService,
    JoinPathAmbiguityService,
    SemanticColumnAmbiguityService,
)
from db_whisperer.ambiguity.openrouter_client import AmbiguityOpenRouterClient  # noqa: E402
from db_whisperer.application import ApplicationService  # noqa: E402
from db_whisperer.contracts import (  # noqa: E402
    ComponentState,
    CsvUpload,
    QueryCandidate,
    QueryRequest,
    QueryResult,
)
from db_whisperer.etler import ETLService  # noqa: E402
from db_whisperer.querier import QueryService  # noqa: E402
from db_whisperer.querier.openrouter_client import OpenRouterClient  # noqa: E402

from benchmark_v2.contracts import EvaluationCase, EvaluationSuite, load_suite  # noqa: E402
from benchmark_v2.observability import (  # noqa: E402
    CampaignObserver,
    InstrumentedSession,
    atomic_json,
    utc_now,
)
from benchmark_v2.scoring import (  # noqa: E402
    option_index,
    score_etl_manifest,
    score_query_case,
    serialize_result,
    summarize_arm,
)


BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = BENCHMARK_DIR / "cases" / "evaluation_cases.json"
ARMS = (
    "baseline",
    "candidate_only",
    "join_only",
    "semantic_only",
    "full",
)


def load_env_file(path: Path) -> None:
    """Load simple local settings without overriding process variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def csv_upload(path: Path) -> CsvUpload:
    return CsvUpload(name=path.name, content=path.read_bytes())


def dataset_uploads(directory: Path) -> tuple[CsvUpload, ...]:
    paths = sorted(directory.glob("*.csv"), key=lambda value: value.name.lower())
    if not paths:
        raise ValueError(f"No CSV files found in dataset: {directory}")
    return tuple(csv_upload(path) for path in paths)


def snapshot_database(path: str) -> dict[str, int]:
    connection = duckdb.connect(path, read_only=True)
    try:
        tables = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
        return {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
            for name in tables
        }
    finally:
        connection.close()


def expected_result(case: EvaluationCase, query: QueryService, database_path: str) -> QueryResult | None:
    if case.expected_sql is None:
        return None
    return query.execute_candidate(
        QueryCandidate(attempt_number=0, state=ComponentState.ACCEPTED, sql=case.expected_sql),
        database_path,
    )


def build_services(observer: CampaignObserver, candidate_count: int) -> tuple[QueryService, dict[str, ApplicationService]]:
    session = InstrumentedSession(observer)
    prompt_logger = observer.prompt_logger
    query_client = OpenRouterClient(session=session, prompt_logger=prompt_logger)
    query = QueryService(client=query_client, max_result_rows=1000)
    ambiguity_client = AmbiguityOpenRouterClient(session=session, prompt_logger=prompt_logger)

    def application(join: bool, semantic: bool) -> ApplicationService:
        return ApplicationService(
            querier=query,
            ambiguity=AmbiguityService(client=ambiguity_client),
            join_path=JoinPathAmbiguityService(client=ambiguity_client),
            semantic_column=SemanticColumnAmbiguityService(client=ambiguity_client),
            event_logger=prompt_logger,
            candidates_per_iteration=candidate_count,
            max_parallel_candidates=candidate_count,
            enable_join_path_detection=join,
            enable_semantic_column_detection=semantic,
        )

    return query, {
        "candidate_only": application(False, False),
        "join_only": application(True, False),
        "semantic_only": application(False, True),
        "full": application(True, True),
    }


def run_baseline(case: EvaluationCase, schema: Any, query: QueryService, api_key: str, model: str) -> QueryResult:
    candidate = query.generate_candidate(
        QueryRequest(prompt=case.question, schema=schema, api_key=api_key, model=model, attempt_number=1)
    )
    return query.execute_candidate(candidate, schema.database_path)


def run_application(
    case: EvaluationCase,
    schema: Any,
    application: ApplicationService,
    api_key: str,
    model: str,
) -> tuple[QueryResult | None, list[dict[str, Any]], str]:
    clarifications: tuple[str, ...] = ()
    asked: list[dict[str, Any]] = []
    last: QueryResult | None = None
    for iteration in range(1, application.max_iterations + 1):
        workflow = application.submit_query(
            prompt=case.question,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
            candidate_count=application.candidates_per_iteration,
        )
        if workflow.query_result is not None:
            last = workflow.query_result
        pending = (
            workflow.state == ComponentState.PENDING
            and workflow.ambiguity is not None
            and len(workflow.ambiguity.options) == 2
        )
        if not pending:
            return last, asked, "complete" if workflow.state == ComponentState.ACCEPTED else "failed"
        decision = workflow.ambiguity
        assert decision is not None
        index = option_index(decision.options, case.option_tokens)
        matched = index is not None
        chosen_index = index if index is not None else 0
        chosen = decision.options[chosen_index]
        record = {
            "iteration": iteration,
            "mechanism": decision.mechanism or "candidate-comparison",
            "question": decision.question,
            "options": list(decision.options),
            "chosen_index": chosen_index,
            "chosen": chosen,
            "matched_intent": matched,
            "reason": decision.reason,
        }
        asked.append(record)
        clarifications = (*clarifications, f"Question: {decision.question or ''}\nSelected answer: {chosen}")
    return last, asked, "max_iterations"


def run_etl_cases(suite: EvaluationSuite, run_dir: Path, observer: CampaignObserver) -> tuple[float, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for case in suite.etl_cases:
        observer.publish(current={"phase": "etl_fixture", "case": case.id})
        service = ETLService(database_path=run_dir / f"{case.id}.duckdb")
        ingestion = service.ingest(tuple(csv_upload(path) for path in case.fixture_files))
        scored = score_etl_manifest(ingestion.schema, case.manifest or {}) if ingestion.state == ComponentState.ACCEPTED else {"score": 0.0, "checks": []}
        row = {"case_id": case.id, "state": ingestion.state.value, "message": ingestion.message, "score": scored}
        rows.append(row)
        observer.event("etl_case_completed", run=run_dir.name, case=case.id, score=scored["score"])
    score = sum(row["score"]["score"] for row in rows) / len(rows)
    return score, rows


def run_repetition(
    repetition: int,
    suite: EvaluationSuite,
    campaign_dir: Path,
    observer: CampaignObserver,
    api_key: str,
) -> dict[str, Any]:
    run_id = f"run-{repetition:02d}"
    run_dir = campaign_dir / "run-results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    database_path = campaign_dir / ".work" / f"{run_id}.duckdb"
    observer.publish(state="running", current={"phase": "ingestion", "run": repetition})
    observer.console(f"[{run_id}] Loading MIMIC dataset")
    ingestion = ETLService(database_path=database_path).ingest(dataset_uploads(suite.dataset_path))
    if ingestion.state != ComponentState.ACCEPTED:
        raise RuntimeError(f"Primary ingestion failed: {ingestion.message}")
    schema = ingestion.schema
    query, applications = build_services(observer, suite.candidate_count)
    references = {
        case.id: expected_result(case, query, schema.database_path or "")
        for case in suite.query_cases
        if case.expected_sql is not None
    }
    etl_score, etl_rows = run_etl_cases(suite, run_dir, observer)
    arm_rows: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    for case in suite.query_cases:
        before = snapshot_database(schema.database_path or "") if case.category == "safety" else None
        for arm in ARMS:
            checkpoint_key = f"{run_id}-{case.id}-{arm}"
            checkpoint_path = observer.checkpoint_dir / f"{checkpoint_key}.json"
            if checkpoint_path.exists():
                row = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                arm_rows[arm].append(row)
                continue
            observer.publish(current={"phase": "case", "run": repetition, "case": case.id, "arm": arm})
            observer.console(f"[{run_id}] {case.id} / {arm}")
            observer.event("case_arm_started", run=repetition, case=case.id, arm=arm)
            started = perf_counter()
            if arm == "baseline":
                result = run_baseline(case, schema, query, api_key, suite.model)
                clarifications: list[dict[str, Any]] = []
                termination = "complete" if result.state == ComponentState.ACCEPTED else "failed"
            else:
                result, clarifications, termination = run_application(
                    case, schema, applications[arm], api_key, suite.model
                )
            after = snapshot_database(schema.database_path or "") if case.category == "safety" else None
            score = score_query_case(case, result, references.get(case.id), schema, clarifications)
            if case.category == "safety" and before != after:
                score.update(passed=False, safety=0.0, reason="database changed during safety case")
            row = {
                "case_id": case.id,
                "family_id": case.family_id,
                "category": case.category,
                "arm": arm,
                "duration_seconds": round(perf_counter() - started, 4),
                "termination": termination,
                "clarifications": clarifications,
                "result": serialize_result(result),
                "score": score,
            }
            arm_rows[arm].append(row)
            observer.checkpoint(checkpoint_key, row)
            observer.completed(passed=bool(score["passed"]))
            observer.event("case_arm_completed", run=repetition, case=case.id, arm=arm, passed=score["passed"], score=score)
            if float(observer.status["cost_usd"]) >= suite.budget_usd:
                raise RuntimeError("Campaign budget ceiling reached; resume after adding allowance.")
    summaries = {arm: summarize_arm(rows, etl_score) for arm, rows in arm_rows.items()}
    report = {
        "report_type": "dbwhisperer_v2_run",
        "scoring_mode": "deterministic_scoring_only",
        "run": repetition,
        "suite": suite.name,
        "suite_version": suite.version,
        "suite_hash": suite.sha256,
        "model": suite.model,
        "candidate_count": suite.candidate_count,
        "schema": {"table_count": len(schema.table_names), "relationship_count": len(schema.relationships), "discovery_complete": schema.discovery_complete, "notes": list(schema.discovery_notes)},
        "etl": {"score": etl_score, "cases": etl_rows},
        "arms": {arm: {"summary": summaries[arm], "cases": arm_rows[arm]} for arm in ARMS},
        "usage": {key: observer.status.get(key) for key in ("model_calls", "prompt_tokens", "completion_tokens", "cost_usd")},
        "completed_at": utc_now(),
    }
    atomic_json(run_dir / "report.json", report)
    return report


def launch_monitor(campaign_dir: Path, port: int) -> subprocess.Popen[Any]:
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(BENCHMARK_DIR / "monitor.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--",
        "--campaign-dir",
        str(campaign_dir),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, cwd=PROJECT_ROOT, creationflags=flags)  # noqa: S603


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--monitor-port", type=int, default=8502)
    parser.add_argument("--repetitions", type=int, default=None, help="Testing override; official aggregate requires five.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suite = load_suite(args.suite)
    load_env_file(BENCHMARK_DIR / ".env")
    api_key = (
        os.getenv("OPENROUTER_API_KEY", "").strip()
        or os.getenv("API_KEY", "").strip()
    )
    if not api_key:
        print("OPENROUTER_API_KEY is required.", file=sys.stderr)
        return 2
    campaign_id = args.campaign_id.strip() or datetime.now(timezone.utc).strftime("v2-%Y%m%dT%H%M%SZ")
    campaign_dir = BENCHMARK_DIR / "results" / "runs" / campaign_id
    campaign_dir.mkdir(parents=True, exist_ok=True)
    repetitions = args.repetitions or suite.repetitions
    total_units = repetitions * len(suite.query_cases) * len(ARMS)
    observer = CampaignObserver(campaign_dir, total_units, suite.budget_usd)
    observer.publish(state="running", latest_error="")
    live_pointer = BENCHMARK_DIR / "results" / "live" / "current.json"
    atomic_json(live_pointer, {"campaign_id": campaign_id, "campaign_dir": str(campaign_dir.resolve())})
    observer.event("campaign_started", suite_version=suite.version, suite_hash=suite.sha256, repetitions=repetitions, case_count=len(suite.cases), arms=list(ARMS))
    monitor_process = launch_monitor(campaign_dir, args.monitor_port) if args.monitor else None
    if monitor_process:
        observer.console(f"Live dashboard: http://127.0.0.1:{args.monitor_port}")
    observer.console(f"Campaign: {campaign_id}; logs: {campaign_dir}")
    reports: list[dict[str, Any]] = []
    try:
        for repetition in range(1, repetitions + 1):
            reports.append(run_repetition(repetition, suite, campaign_dir, observer, api_key))
    except Exception as error:  # noqa: BLE001
        observer.event("campaign_failed", severity="error", message=str(error))
        observer.publish(state="incomplete")
        observer.console(f"Campaign stopped: {error}")
        return 1
    observer.publish(
        state="complete",
        current={"phase": "complete"},
        completed_units=total_units,
    )
    manifest = {"campaign_id": campaign_id, "suite_version": suite.version, "suite_hash": suite.sha256, "reports": [f"run-results/run-{index:02d}/report.json" for index in range(1, repetitions + 1)], "completed_at": utc_now()}
    atomic_json(campaign_dir / "campaign.json", manifest)
    observer.console(f"Campaign complete: {campaign_dir / 'campaign.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
