"""Bounded, reproducible four-arm Evaluation V3 campaign runner."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from importlib import metadata as importlib_metadata
import os
from pathlib import Path
import random
import re
import shutil
import stat
import sys
from threading import Lock, local
from time import perf_counter
from typing import Any, Callable, Mapping

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_v3.contracts import EvaluationCase, EvaluationSuite, load_suite, validate_reference_suite
from benchmark_v3.observability import BudgetStop, CampaignObserver, InstrumentedSession, atomic_json
from benchmark_v3.progress import TerminalProgress
from benchmark_v3.scoring import SafetyEvidence, score_etl_manifest, score_query_case
from db_whisperer.ambiguity import AmbiguityPromptBuilder, AmbiguityService, SemanticColumnAmbiguityService
from db_whisperer.ambiguity.openrouter_client import AmbiguityOpenRouterClient
from db_whisperer.application import ApplicationService
from db_whisperer.contracts import ComponentState, CsvUpload, QueryCandidate, QueryRequest, QueryResult, SchemaMetadata
from db_whisperer.etler import ETLService
from db_whisperer.querier import QueryService
from db_whisperer.querier.openrouter_client import OpenRouterClient
from db_whisperer.querier.sql_validator import FORBIDDEN_SQL

BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = BENCHMARK_DIR / "cases" / "evaluation_cases.json"
DEFAULT_OUTPUT = BENCHMARK_DIR / "results" / "runs"
ARMS = ("baseline", "candidate_only", "semantic_only", "full")
_FORBIDDEN_OPERATION = "Generated SQL contains a forbidden operation."
OFFICIAL_REPETITIONS = 5
OFFICIAL_RECORD_COUNT = 450
_CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class WorkItem:
    repetition: int
    case_id: str
    family_id: str
    category: str
    arm: str

    @property
    def key(self) -> str:
        return f"run-{self.repetition:02d}-{self.case_id}-{self.arm}"


@dataclass(frozen=True)
class ClarificationTurn:
    iteration: int
    mechanism: str
    question: str | None
    options: tuple[str, str]
    chosen_index: int | None
    chosen: str | None
    matched_intent: bool
    candidate_support: tuple[tuple[str, int], ...] = ()
    candidate_rejection_reason: str = ""
    fallback_used: bool | None = None
    compliance_retry_used: bool | None = None
    compliance_passed: bool | None = None
    compliant_alternatives: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "mechanism": self.mechanism,
            "question": self.question,
            "options": list(self.options),
            "chosen_index": self.chosen_index,
            "chosen": self.chosen,
            "matched_intent": self.matched_intent,
            "candidate_support": [list(pair) for pair in self.candidate_support],
            "candidate_rejection_reason": self.candidate_rejection_reason,
            "fallback_used": self.fallback_used,
            "compliance_retry_used": self.compliance_retry_used,
            "compliance_passed": self.compliance_passed,
            "compliant_alternatives": list(self.compliant_alternatives),
        }


@dataclass(frozen=True)
class CampaignFingerprint:
    suite_hash: str
    dataset_hash: str
    model: str
    prompt_hash: str
    scorer_version: str
    candidate_count: int
    arms: tuple[str, ...]
    runtime_hash: str


@dataclass(frozen=True)
class CampaignDataset:
    schema: SchemaMetadata
    dataset_hash: str
    references: Mapping[str, QueryResult]
    reference_joins: Mapping[str, int]


@dataclass(frozen=True)
class CampaignConfig:
    suite: EvaluationSuite
    campaign_dir: Path
    api_key: str
    workers: int = 2
    dataset: CampaignDataset | None = None
    service_factory: Callable[[CampaignObserver], tuple[QueryService, dict[str, ApplicationService]]] | None = None
    cell_runner: Callable[..., dict[str, Any]] | None = None
    progress_factory: Callable[[CampaignObserver], TerminalProgress] | None = None


@dataclass(frozen=True)
class CampaignResult:
    fingerprint: CampaignFingerprint
    completed_keys: frozenset[str]
    records: tuple[dict[str, Any], ...]
    stopped_for_budget: bool = False
    published: bool = False


def _hash_paths(paths: tuple[Path, ...]) -> str:
    digest = sha256()
    for path in paths:
        digest.update(str(path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _hash_files(path: Path) -> str:
    files = tuple(sorted(path.rglob("*.csv"))) if path.is_dir() else (path,)
    return _hash_paths(files)


def _source_hash(*relative_paths: str) -> str:
    return _hash_paths(tuple(PROJECT_ROOT / path for path in relative_paths))


def _behavior_source_paths() -> tuple[Path, ...]:
    """Return deterministic, non-test Python sources that can change a run."""
    sources = (
        *SRC.joinpath("db_whisperer").rglob("*.py"),
        *BENCHMARK_DIR.glob("*.py"),
    )
    return tuple(sorted(
        (path for path in sources if "tests" not in path.parts),
        key=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
    ))


def _fingerprint(
    suite: EvaluationSuite,
    dataset_hash: str,
    *,
    workers: int = 2,
) -> CampaignFingerprint:
    prompt_hash = _hash_paths(_behavior_source_paths())
    runtime_source_hash = _source_hash(
        "benchmark_v3/run_evaluation.py",
        "benchmark_v3/observability.py",
        "requirements.txt",
    )
    scorer_hash = _source_hash("benchmark_v3/scoring.py")
    runtime_inputs = {
        "python": sys.version,
        "dependencies": {
            package: importlib_metadata.version(package)
            for package in ("duckdb", "requests", "streamlit", "sqlglot")
        },
        "workers": workers,
        "repetitions": suite.repetitions,
        "candidate_count": suite.candidate_count,
        "arms": ARMS,
        "runtime_source_hash": runtime_source_hash,
    }
    runtime_hash = sha256(
        json.dumps(runtime_inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return CampaignFingerprint(
        suite_hash=suite.sha256,
        dataset_hash=dataset_hash,
        model=suite.model,
        prompt_hash=prompt_hash,
        scorer_version=scorer_hash,
        candidate_count=suite.candidate_count,
        arms=ARMS,
        runtime_hash=runtime_hash,
    )


def _fingerprint_payload(fingerprint: CampaignFingerprint) -> dict[str, Any]:
    """Return the JSON-normalized fingerprint used for all durable checks."""
    payload = asdict(fingerprint)
    payload["arms"] = list(fingerprint.arms)
    return payload


def build_schedule(
    suite: EvaluationSuite,
    repetitions: int | None = None,
) -> tuple[WorkItem, ...]:
    """Fixed-seed query schedule; ETL fixtures are deliberately separate."""
    count = repetitions if repetitions is not None else suite.repetitions
    items: list[WorkItem] = []
    for repetition in range(1, count + 1):
        cases = list(suite.query_cases)
        random.Random(2112 + repetition).shuffle(cases)
        arms = ARMS[repetition % len(ARMS):] + ARMS[:repetition % len(ARMS)]
        for case in cases:
            items.extend(
                WorkItem(repetition, case.id, case.family_id, case.category, arm)
                for arm in arms
            )
    return tuple(items)


def build_etl_schedule(
    suite: EvaluationSuite,
    repetitions: int | None = None,
) -> tuple[WorkItem, ...]:
    count = repetitions if repetitions is not None else suite.repetitions
    return tuple(
        WorkItem(repetition, case.id, case.family_id, case.category, "etl")
        for repetition in range(1, count + 1)
        for case in suite.etl_cases
    )


def build_services(
    observer: CampaignObserver | int,
    candidate_count: int | None = None,
) -> tuple[QueryService, dict[str, ApplicationService]]:
    """Build one isolated transport and service graph per outer worker."""
    if isinstance(observer, int):
        candidate_count, observer = observer, None
    if candidate_count is None:
        raise ValueError("candidate_count is required")
    session = InstrumentedSession(observer) if observer is not None else None
    logger = observer.prompt_logger if observer is not None else None
    query = QueryService(
        client=OpenRouterClient(session=session, prompt_logger=logger),
        max_result_rows=1000,
    )

    def application(
        semantic: bool,
        schema: bool,
        relationships: bool,
        candidates: bool,
    ) -> ApplicationService:
        ambiguity = AmbiguityService(
            client=AmbiguityOpenRouterClient(session=session, prompt_logger=logger),
            prompt_builder=AmbiguityPromptBuilder(
                include_semantic_findings=semantic,
                include_schema_context=schema,
                include_relationships=relationships,
                include_candidate_evidence=candidates,
            ),
        )
        return ApplicationService(
            querier=query,
            ambiguity=ambiguity,
            semantic_column=SemanticColumnAmbiguityService(),
            event_logger=logger,
            candidates_per_iteration=candidate_count,
            max_parallel_candidates=candidate_count,
            enable_semantic_column_detection=semantic,
        )

    return query, {
        "candidate_only": application(False, False, False, True),
        "semantic_only": application(True, True, False, False),
        "full": application(True, True, True, True),
    }


def _uploads(paths: tuple[Path, ...]) -> tuple[CsvUpload, ...]:
    return tuple(CsvUpload(path.name, path.read_bytes()) for path in paths)


def ingest_dataset(
    dataset_path: Path,
    database_path: Path | None = None,
) -> SchemaMetadata:
    files = tuple(sorted(dataset_path.rglob("*.csv")))
    result = ETLService(database_path=database_path).ingest(_uploads(files))
    if result.state != ComponentState.ACCEPTED:
        raise RuntimeError(result.message)
    return result.schema


def _reference_artifact_path(
    campaign_dir: Path,
    dataset_hash: str,
    suite_hash: str,
) -> Path:
    return campaign_dir / f"references-{dataset_hash}-{suite_hash}.json"


def _database_hash(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cached_dataset_is_valid(
    cached: Mapping[str, Any],
    suite: EvaluationSuite,
    dataset_hash: str,
    database: Path,
) -> bool:
    if cached.get("dataset_hash") != dataset_hash or cached.get("suite_hash") != suite.sha256:
        return False
    if not database.is_file() or cached.get("database_hash") != _database_hash(database):
        return False
    try:
        connection = duckdb.connect(str(database), read_only=True)
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()
    except duckdb.Error:
        return False
    schema = cached.get("schema")
    references = cached.get("references")
    if not isinstance(schema, Mapping) or not isinstance(references, Mapping):
        return False
    try:
        cached_schema = _deserialize_schema(schema)
    except (KeyError, TypeError, ValueError):
        return False
    if cached_schema.database_path != str(database.resolve()):
        return False
    expected = {case.id: case for case in suite.query_cases if case.expected_sql}
    if set(references) != set(expected):
        return False
    for case_id, case in expected.items():
        value = references.get(case_id)
        if not isinstance(value, Mapping):
            return False
        if (
            value.get("sql") != case.expected_sql
            or not isinstance(value.get("join_count"), int)
            or value.get("comparison_mode") != case.comparison_mode
            or not isinstance(value.get("truncated"), bool)
        ):
            return False
        if value["join_count"] != _expected_join_count(case.expected_sql):
            return False
        if (
            not isinstance(value.get("columns"), list)
            or not all(isinstance(column, str) for column in value["columns"])
            or not isinstance(value.get("rows"), list)
            or not all(isinstance(row, list) for row in value["rows"])
        ):
            return False
    return True


def _expected_join_count(sql: str) -> int:
    """Derive the frozen reference join contract from its validated SQL."""
    from benchmark_v3.sql_analysis import analyze_sql
    from db_whisperer.querier.sql_validator import validate_read_only_sql

    return analyze_sql(validate_read_only_sql(sql)).join_count


def _serialize_schema(schema: SchemaMetadata) -> dict[str, Any]:
    return asdict(schema)


def _deserialize_schema(value: Mapping[str, Any]) -> SchemaMetadata:
    from db_whisperer.contracts import ColumnMetadata, Relationship, TableSchema
    tables = tuple(
        TableSchema(
            table_name=table["table_name"],
            columns=tuple(ColumnMetadata(**column) for column in table["columns"]),
            row_count=table["row_count"],
            key_columns=tuple(table.get("key_columns", ())),
            id_key_columns=tuple(table.get("id_key_columns", ())),
            primary_key=tuple(table.get("primary_key", ())),
        )
        for table in value.get("tables", ())
    )
    return SchemaMetadata(
        database_path=value.get("database_path"),
        source_names=tuple(value.get("source_names", ())),
        table_names=tuple(value.get("table_names", ())),
        columns=tuple(ColumnMetadata(**column) for column in value.get("columns", ())),
        row_count=value.get("row_count"),
        tables=tables,
        relationships=tuple(Relationship(**item) for item in value.get("relationships", ())),
        discovery_complete=bool(value.get("discovery_complete", True)),
        discovery_notes=tuple(value.get("discovery_notes", ())),
    )


def _prepare_dataset(
    suite: EvaluationSuite,
    campaign_dir: Path,
    dataset_hash: str,
) -> CampaignDataset:
    artifact = _reference_artifact_path(campaign_dir, dataset_hash, suite.sha256)
    database = campaign_dir / "datasets" / f"{dataset_hash}.duckdb"
    if artifact.exists():
        cached = json.loads(artifact.read_text(encoding="utf-8"))
        if _cached_dataset_is_valid(cached, suite, dataset_hash, database):
            refs = {
                key: QueryResult(
                    state=ComponentState.ACCEPTED,
                    message="cached reference",
                    sql=value["sql"],
                    columns=tuple(value["columns"]),
                    rows=tuple(tuple(row) for row in value["rows"]),
                    truncated=bool(value["truncated"]),
                )
                for key, value in cached["references"].items()
            }
            return CampaignDataset(
                _deserialize_schema(cached["schema"]), dataset_hash, refs,
                {key: int(value["join_count"]) for key, value in cached["references"].items()},
            )
        artifact.unlink(missing_ok=True)
    try:
        if database.exists():
            database.chmod(database.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass
    schema = ingest_dataset(suite.dataset_path, database)
    query, _ = build_services(suite.candidate_count)
    try:
        evidence = validate_reference_suite(suite, schema, query)
    finally:
        _close_services((query, {}))
    refs = {
        key: QueryResult(
            state=ComponentState.ACCEPTED,
            message="reference",
            sql=value["sql"],
            columns=tuple(value["columns"]),
            rows=tuple(tuple(row) for row in value["rows"]),
            truncated=bool(value["truncated"]),
        )
        for key, value in evidence.items()
    }
    atomic_json(artifact, {
        "dataset_hash": dataset_hash,
        "suite_hash": suite.sha256,
        "schema": _serialize_schema(schema),
        "references": evidence,
        "discovery_warnings": list(schema.discovery_notes),
        "database_hash": _database_hash(database),
    })
    try:
        database.chmod(database.stat().st_mode & ~stat.S_IWRITE)
    except OSError:
        pass
    return CampaignDataset(schema, dataset_hash, refs, {key: int(value["join_count"]) for key, value in evidence.items()})


def _choose(
    case: EvaluationCase,
    options: tuple[str, str],
) -> tuple[int | None, str | None, bool]:
    for index, option in enumerate(options):
        normalized = option.casefold()
        if any(
            group and all(token.casefold() in normalized for token in group)
            for group in case.option_token_groups
        ):
            return index, option, True
    return None, None, False


def choose_option(case: EvaluationCase, options: tuple[str, ...]) -> tuple[str, bool]:
    if len(options) != 2:
        return "", False
    _, selected, matched = _choose(case, (options[0], options[1]))
    return selected or "", matched


def _database_snapshot(schema: SchemaMetadata) -> str | None:
    if not schema.database_path:
        return None
    try:
        return sha256(Path(schema.database_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _safety_evidence(
    case: EvaluationCase,
    candidates: tuple[QueryCandidate, ...],
    candidate_results: tuple[QueryResult, ...],
    before: str | None,
    after: str | None,
) -> SafetyEvidence:
    unchanged = before is not None and before == after
    attempted_sql = any(
        candidate.state == ComponentState.ACCEPTED
        and bool(getattr(candidate, "sql", ""))
        for candidate in candidates
    )
    for result in candidate_results:
        if result.failure_kind == "schema_resolution":
            return SafetyEvidence(
                "schema", unchanged, "schema_resolution", case_id=case.id,
                attempted_sql=attempted_sql,
            )
    for candidate in candidates:
        if candidate.message != _FORBIDDEN_OPERATION:
            continue
        match = FORBIDDEN_SQL.search(getattr(candidate, "sql", "") or "")
        operation = match.group(1).upper() if match else ""
        return SafetyEvidence(
            "validator", unchanged, "forbidden_operation", operation,
            case_id=case.id, attempted_sql=attempted_sql,
        )
    return SafetyEvidence("unknown", unchanged, case_id=case.id, attempted_sql=attempted_sql)


def _baseline_result(
    query: QueryService,
    request: QueryRequest,
) -> tuple[QueryResult, QueryCandidate]:
    candidate = query.generate_candidate(request)
    return query.execute_candidate(candidate, request.schema.database_path), candidate


def run_cell(
    item: WorkItem,
    case: EvaluationCase,
    dataset: CampaignDataset,
    query: QueryService,
    apps: Mapping[str, ApplicationService],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    started = perf_counter()
    turns: list[ClarificationTurn] = []
    before = _database_snapshot(dataset.schema) if case.category == "safety" else None
    trace_candidates: list[QueryCandidate] = []
    trace_results: list[QueryResult] = []
    if item.arm == "baseline":
        request = QueryRequest(case.question, dataset.schema, api_key, model)
        result, candidate = _baseline_result(query, request)
        trace_candidates.append(candidate)
        trace_results.append(result)
    else:
        result: QueryResult | None = None
        answers: tuple[str, ...] = ()
        workflow = None
        for iteration in range(1, 4):
            workflow = apps[item.arm].submit_query(
                prompt=case.question, schema=dataset.schema, api_key=api_key,
                model=model, clarifications=answers, iteration=iteration,
            )
            result = workflow.query_result
            trace_candidates.extend(getattr(workflow, "candidates", ()))
            trace_results.extend(getattr(workflow, "candidate_results", ()))
            if result is not None:
                trace_results.append(result)
            if workflow.complete or workflow.state == ComponentState.FAILED:
                break
            decision = workflow.ambiguity
            if decision is None or len(decision.options) != 2 or len(turns) == 2:
                result = None
                break
            index, selected, matched = _choose(case, (decision.options[0], decision.options[1]))
            turn = ClarificationTurn(
                iteration, decision.mechanism, decision.question,
                (decision.options[0], decision.options[1]), index, selected,
                matched, decision.candidate_support, decision.candidate_rejection_reason,
                fallback_used=getattr(workflow, "semantic_fallback_used", None),
                compliance_retry_used=getattr(workflow, "compliance_retry_used", None),
                compliance_passed=decision.compliance_passed,
                compliant_alternatives=decision.compliant_alternatives,
            )
            turns.append(turn)
            if not matched or selected is None:
                result = None
                break
            answers += (f"Question: {decision.question}\nSelected answer: {selected}",)
    clarifications = [turn.to_record() for turn in turns]
    safety = (
        _safety_evidence(
            case, tuple(trace_candidates), tuple(trace_results), before,
            _database_snapshot(dataset.schema),
        )
        if case.category == "safety" else None
    )
    return {
        "run": item.repetition,
        "case_id": case.id,
        "family_id": case.family_id,
        "category": case.category,
        "arm": item.arm,
        "clarifications": clarifications,
        "result": {
            "state": result.state if result else "missing",
            "sql": result.sql if result else None,
            "columns": list(result.columns) if result else [],
            "rows": [list(row) for row in result.rows] if result else [],
        },
        "score": score_query_case(
            case, result, dataset.references.get(case.id), dataset.schema,
            clarifications, safety_evidence=safety,
        ),
        "relationship_warnings": list(dataset.schema.discovery_notes),
        "duration_seconds": round(perf_counter() - started, 3),
    }


def _run_etl_cell(item: WorkItem, case: EvaluationCase, campaign_dir: Path) -> dict[str, Any]:
    started = perf_counter()
    fixture_db = campaign_dir / "etl" / f"{item.key}.duckdb"
    result = ETLService(database_path=fixture_db).ingest(_uploads(case.fixture_files))
    score = score_etl_manifest(result.schema, case.manifest or {})
    score["passed"] = bool(result.state == ComponentState.ACCEPTED and score["score"] == 1.0)
    return {
        "run": item.repetition,
        "case_id": case.id,
        "family_id": case.family_id,
        "category": case.category,
        "arm": "etl",
        "clarifications": [],
        "result": {"state": result.state, "sql": None, "columns": [], "rows": []},
        "score": score,
        "relationship_warnings": list(result.schema.discovery_notes),
        "duration_seconds": round(perf_counter() - started, 3),
    }


def _checkpoint_payload(
    item: WorkItem,
    fingerprint: CampaignFingerprint,
    record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "work_item": asdict(item),
        "fingerprint": _fingerprint_payload(fingerprint),
        "record": record,
        # Observability's resume counter consumes this top-level summary.
        "score": record["score"],
    }


def _load_matching_checkpoints(
    directory: Path,
    schedule: tuple[WorkItem, ...],
    fingerprint: CampaignFingerprint,
) -> dict[str, dict[str, Any]]:
    expected = {item.key: asdict(item) for item in schedule}
    checkpoint_dir = directory / "checkpoints"
    records: dict[str, dict[str, Any]] = {}
    if not checkpoint_dir.exists():
        return records
    for path in checkpoint_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != _fingerprint_payload(fingerprint):
            raise ValueError("checkpoint fingerprint is incompatible")
        if path.stem not in expected or payload.get("work_item") != expected[path.stem]:
            raise ValueError("checkpoint work identity is incompatible")
        record = payload.get("record")
        expected_record = expected[path.stem]
        if not isinstance(record, dict):
            raise ValueError("checkpoint record is invalid")
        if any(
            record.get(field) != expected_record[source]
            for field, source in (
                ("run", "repetition"), ("case_id", "case_id"),
                ("family_id", "family_id"), ("category", "category"),
                ("arm", "arm"),
            )
        ):
            raise ValueError("checkpoint record work identity is incompatible")
        result = record.get("result")
        score = record.get("score")
        if (
            not isinstance(result, Mapping)
            or not {"state", "sql", "columns", "rows"} <= set(result)
            or not isinstance(score, Mapping)
            or not isinstance(score.get("passed"), bool)
        ):
            raise ValueError("checkpoint record shape is invalid")
        records[path.stem] = record
    return records


def _close_services(services: tuple[QueryService, Mapping[str, ApplicationService]]) -> None:
    query, _ = services
    session = getattr(getattr(query, "client", None), "session", None)
    if session is not None and hasattr(session, "close"):
        session.close()


def _failure_record(item: WorkItem, error: Exception, duration: float) -> dict[str, Any]:
    return {
        "run": item.repetition, "case_id": item.case_id,
        "family_id": item.family_id, "category": item.category, "arm": item.arm,
        "clarifications": [],
        "result": {"state": "failed", "sql": None, "columns": [], "rows": []},
        "score": {"passed": False, "reason": str(error)},
        "duration_seconds": round(duration, 3),
    }


def run_campaign(config: CampaignConfig) -> CampaignResult:
    if config.workers not in {1, 2}:
        raise ValueError("workers must be one or two")
    dataset_hash = config.dataset.dataset_hash if config.dataset else _hash_files(config.suite.dataset_path)
    fingerprint = _fingerprint(config.suite, dataset_hash, workers=config.workers)
    query_schedule = build_schedule(config.suite)
    schedule = query_schedule + build_etl_schedule(config.suite)
    campaign_path = config.campaign_dir / "campaign.json"
    existing = json.loads(campaign_path.read_text(encoding="utf-8")) if campaign_path.exists() else {}
    if existing.get("fingerprint") not in (None, _fingerprint_payload(fingerprint)):
        raise ValueError("campaign fingerprint is incompatible")
    checkpoint_records = _load_matching_checkpoints(config.campaign_dir, schedule, fingerprint)
    metadata = {
        "report_type": "dbwhisperer_v3_run", "suite_version": config.suite.version,
        "suite_hash": config.suite.sha256, "model": config.suite.model,
        "arms": list(ARMS), "fingerprint": _fingerprint_payload(fingerprint),
    }
    # Make compatibility durable before preparing data or admitting a cell.
    atomic_json(campaign_path, {**metadata, "complete": False, "records": list(checkpoint_records.values())})
    observer = CampaignObserver(config.campaign_dir, schedule, config.suite.budget_usd)
    progress = (config.progress_factory or (lambda value: TerminalProgress(value, stream=sys.stderr)))(observer)
    progress.start()
    try:
        dataset = config.dataset or _prepare_dataset(
            config.suite, config.campaign_dir, dataset_hash,
        )
    except Exception:
        progress.stop()
        raise
    query_cases = {case.id: case for case in config.suite.query_cases}
    etl_cases = {case.id: case for case in config.suite.etl_cases}
    services_by_thread = local()
    service_lock = Lock()
    created_services: list[tuple[QueryService, Mapping[str, ApplicationService]]] = []
    records = list(checkpoint_records.values())
    completed = set(checkpoint_records)
    stopped = False

    def execute(item: WorkItem) -> tuple[WorkItem, dict[str, Any]]:
        observer.activate(item, "running")
        if item.arm == "etl":
            return item, _run_etl_cell(item, etl_cases[item.case_id], config.campaign_dir)
        if config.cell_runner is not None:
            return item, config.cell_runner(item, query_cases[item.case_id], dataset)
        services = getattr(services_by_thread, "services", None)
        if services is None:
            services = (config.service_factory or (lambda value: build_services(value, config.suite.candidate_count)))(observer)
            services_by_thread.services = services
            with service_lock:
                created_services.append(services)
        query, apps = services
        return item, run_cell(item, query_cases[item.case_id], dataset, query, apps, config.api_key, config.suite.model)

    pending = [item for item in schedule if item.key not in completed]
    try:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            iterator = iter(pending)
            futures: dict[Any, tuple[WorkItem, float]] = {}

            def submit_next() -> bool:
                try:
                    item = next(iterator)
                except StopIteration:
                    return False
                futures[pool.submit(execute, item)] = (item, perf_counter())
                return True

            for _ in range(config.workers):
                if not submit_next():
                    break
            while futures:
                future = next(as_completed(futures))
                item, started = futures.pop(future)
                try:
                    _, record = future.result()
                except BudgetStop:
                    stopped = True
                    observer.deactivate(item)
                    continue
                except Exception as error:
                    observer.event("cell_failed", severity="error", error=str(error), key=item.key)
                    record = _failure_record(item, error, perf_counter() - started)
                records.append(record)
                observer.checkpoint(item.key, _checkpoint_payload(item, fingerprint, record))
                observer.complete_cell(
                    duration=float(record.get("duration_seconds", 0)), arm=item.arm,
                    category=item.category, passed=bool(record["score"].get("passed")),
                )
                observer.deactivate(item)
                completed.add(item.key)
                if not stopped:
                    submit_next()
    finally:
        for services in created_services:
            _close_services(services)
        progress.stop()

    warnings = list(dataset.schema.discovery_notes)
    payload = {
        **metadata,
        "complete": not stopped and len(completed) == len(schedule),
        "repetitions": config.suite.repetitions,
        "records": records,
        "relationship_warnings": warnings,
        "query_cell_count": len(query_schedule),
        "etl_observation_count": len(build_etl_schedule(config.suite)),
    }
    for repetition in range(1, config.suite.repetitions + 1):
        atomic_json(config.campaign_dir / f"run-{repetition:02d}.json", {
            **metadata, "repetition": repetition,
            "records": [record for record in records if record["run"] == repetition],
            "relationship_warnings": warnings,
        })
    atomic_json(campaign_path, payload)
    published = False
    if payload["complete"] and config.suite.repetitions == OFFICIAL_REPETITIONS and len(records) == OFFICIAL_RECORD_COUNT:
        published = publish_campaign(config.campaign_dir)
        if not published:
            stopped = True
    return CampaignResult(fingerprint, frozenset(completed), tuple(records), stopped, published)


def _replace_staged(source: Path, target: Path) -> Path:
    """Promotion seam kept separate from rollback for injected-failure tests."""
    return source.replace(target)


def _promote_publication(staged: tuple[Path, Path, Path], targets: tuple[Path, Path, Path], stage_dir: Path) -> None:
    backups = stage_dir / "backups"
    backups.mkdir()
    backup_paths: dict[Path, Path | None] = {}
    for index, target in enumerate(targets):
        if target.exists():
            backup = backups / f"{index}-{target.name}"
            shutil.copy2(target, backup)
            backup_paths[target] = backup
        else:
            backup_paths[target] = None
    try:
        for source, target in zip(staged, targets, strict=True):
            _replace_staged(source, target)
    except Exception:
        for target in targets:
            backup = backup_paths[target]
            if backup is not None and backup.exists():
                os.replace(backup, target)
            elif target.exists():
                target.unlink()
        raise


def publish_campaign(
    campaign_dir: Path,
    *,
    one_page_path: Path | None = None,
    full_report_path: Path | None = None,
) -> bool:
    """Publish only a complete official campaign; raw evidence is never removed."""
    campaign_path = campaign_dir / "campaign.json"
    if not campaign_path.exists():
        return False
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    if (
        campaign.get("complete") is not True
        or campaign.get("repetitions") != OFFICIAL_REPETITIONS
        or not isinstance(campaign.get("records"), list)
        or len(campaign["records"]) != OFFICIAL_RECORD_COUNT
    ):
        return False
    if campaign.get("suite_hash") != load_suite(DEFAULT_SUITE).sha256:
        campaign["complete"] = False
        campaign["latest_error"] = "official publication requires the frozen default suite hash"
        atomic_json(campaign_path, campaign)
        return False
    one_page_path = one_page_path or PROJECT_ROOT / "docs" / "evaluation_method_one_page.html"
    full_report_path = full_report_path or PROJECT_ROOT / "docs" / "evaluation_report.html"
    stage_dir = campaign_dir / f".publication-{os.getpid()}-{datetime.now(timezone.utc).strftime('%f')}"
    try:
        from benchmark_v3.aggregate_results import aggregate_campaign, validate_aggregate
        from benchmark_v3.render_report import write_reports

        aggregate = aggregate_campaign(campaign_dir)
        validate_aggregate(aggregate)
        aggregate_path = campaign_dir / "aggregate.json"
        stage_dir.mkdir(parents=True, exist_ok=False)
        staged_aggregate = stage_dir / "aggregate.json"
        staged_one_page = stage_dir / one_page_path.name
        staged_full_report = stage_dir / full_report_path.name
        atomic_json(staged_aggregate, aggregate)
        write_reports(staged_aggregate, staged_one_page, staged_full_report)
        _promote_publication(
            (staged_aggregate, staged_one_page, staged_full_report),
            (aggregate_path, one_page_path, full_report_path),
            stage_dir,
        )
        campaign["published"] = True
        campaign.pop("latest_error", None)
        atomic_json(campaign_path, campaign)
        return True
    except Exception as error:
        campaign["complete"] = False
        campaign["latest_error"] = f"publication failed: {error}"
        atomic_json(campaign_path, campaign)
        return False
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _campaign_directory(campaign_id: str | None) -> Path:
    identifier = campaign_id or datetime.now(timezone.utc).strftime("campaign-%Y%m%d-%H%M%S-%f")
    if not _CAMPAIGN_ID.fullmatch(identifier):
        raise ValueError("campaign id must be a safe lowercase slug")
    return DEFAULT_OUTPUT / identifier


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=OFFICIAL_REPETITIONS)
    parser.add_argument("--campaign-id")
    parser.add_argument(
        "--interactive-progress",
        action="store_true",
        help="Keep one campaign-wide progress line updated in place.",
    )
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required for a live V3 campaign.")
    if args.workers not in {1, 2} or not 1 <= args.repetitions <= OFFICIAL_REPETITIONS:
        raise SystemExit("workers must be 1 or 2; repetitions must be between 1 and 5.")
    try:
        campaign_dir = _campaign_directory(args.campaign_id)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    suite = replace(load_suite(args.suite), repetitions=args.repetitions)
    progress_factory = None
    if args.interactive_progress:
        progress_factory = lambda observer: TerminalProgress(
            observer,
            stream=sys.stderr,
            interactive=True,
        )
    result = run_campaign(
        CampaignConfig(
            suite,
            campaign_dir,
            api_key,
            args.workers,
            progress_factory=progress_factory,
        )
    )
    if args.repetitions != OFFICIAL_REPETITIONS or not result.published or result.stopped_for_budget:
        raise SystemExit("Campaign did not complete and publish the official five-repetition result.")


if __name__ == "__main__":
    main()
