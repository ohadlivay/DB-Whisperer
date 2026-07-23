"""Bounded, resumable four-arm Evaluation V3 campaign runner."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import sys
from time import perf_counter
from threading import local
from typing import Any, Callable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_v3.contracts import EvaluationCase, EvaluationSuite, load_suite, validate_reference_suite
from benchmark_v3.observability import BudgetStop, CampaignObserver, InstrumentedSession, atomic_json
from benchmark_v3.scoring import SafetyEvidence, score_case
from db_whisperer.ambiguity import AmbiguityPromptBuilder, AmbiguityService, SemanticColumnAmbiguityService
from db_whisperer.ambiguity.openrouter_client import AmbiguityOpenRouterClient
from db_whisperer.application import ApplicationService
from db_whisperer.contracts import ComponentState, CsvUpload, QueryCandidate, QueryRequest, QueryResult, SchemaMetadata
from db_whisperer.etler import ETLService
from db_whisperer.querier import QueryService
from db_whisperer.querier.openrouter_client import OpenRouterClient

BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = BENCHMARK_DIR / "cases" / "evaluation_cases.json"
DEFAULT_OUTPUT = BENCHMARK_DIR / "results" / "campaign"
ARMS = ("baseline", "candidate_only", "semantic_only", "full")


@dataclass(frozen=True)
class WorkItem:
    repetition: int; case_id: str; family_id: str; category: str; arm: str
    @property
    def key(self) -> str: return f"run-{self.repetition:02d}-{self.case_id}-{self.arm}"

@dataclass(frozen=True)
class ClarificationTurn:
    """One deterministic clarification action recorded by a campaign cell."""

    iteration: int
    mechanism: str
    question: str | None
    options: tuple[str, ...]
    chosen: str
    matched_intent: bool
    candidate_support: tuple[tuple[str, int], ...] = ()
    candidate_rejection_reason: str = ""
    fallback_used: bool = False
    compliance_passed: bool | None = None
    compliant_alternatives: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        """Return the stable JSON shape consumed by deterministic scoring."""

        return {
            "iteration": self.iteration,
            "mechanism": self.mechanism,
            "question": self.question,
            "options": list(self.options),
            "chosen": self.chosen,
            "matched_intent": self.matched_intent,
            "candidate_support": [list(pair) for pair in self.candidate_support],
            "candidate_rejection_reason": self.candidate_rejection_reason,
            "fallback_used": self.fallback_used,
            "compliance_passed": self.compliance_passed,
            "compliant_alternatives": list(self.compliant_alternatives),
        }

@dataclass(frozen=True)
class CampaignFingerprint:
    suite_hash: str; dataset_hash: str; model: str; prompt_hash: str; scorer_version: str; candidate_count: int; arms: tuple[str, ...]; runtime_hash: str

@dataclass(frozen=True)
class CampaignDataset:
    schema: SchemaMetadata; dataset_hash: str; references: Mapping[str, QueryResult]; reference_joins: Mapping[str, int]

@dataclass(frozen=True)
class CampaignConfig:
    suite: EvaluationSuite; campaign_dir: Path; api_key: str; workers: int = 2; dataset: CampaignDataset | None = None; service_factory: Callable[[CampaignObserver], tuple[QueryService, dict[str, ApplicationService]]] | None = None; cell_runner: Callable[..., dict[str, Any]] | None = None

@dataclass(frozen=True)
class CampaignResult:
    fingerprint: CampaignFingerprint; completed_keys: frozenset[str]; records: tuple[dict[str, Any], ...]; stopped_for_budget: bool = False

def _hash_files(path: Path) -> str:
    digest = sha256()
    for file in sorted(path.rglob("*.csv")) if path.is_dir() else (path,):
        digest.update(file.name.encode()); digest.update(file.read_bytes())
    return digest.hexdigest()

def build_schedule(suite: EvaluationSuite, repetitions: int | None = None) -> tuple[WorkItem, ...]:
    count = repetitions if repetitions is not None else suite.repetitions
    items: list[WorkItem] = []
    for repetition in range(1, count + 1):
        cases = list(suite.query_cases); random.Random(2112 + repetition).shuffle(cases)
        arms = ARMS[repetition % len(ARMS):] + ARMS[:repetition % len(ARMS)]
        for case in cases:
            items.extend(WorkItem(repetition, case.id, case.family_id, case.category, arm) for arm in arms)
    return tuple(items)

def build_services(observer: CampaignObserver | int, candidate_count: int | None = None) -> tuple[QueryService, dict[str, ApplicationService]]:
    """Create worker-local clients; int form preserves offline validation use."""
    if isinstance(observer, int):
        candidate_count, observer = observer, None
    assert candidate_count is not None
    session = InstrumentedSession(observer) if observer is not None else None
    logger = observer.prompt_logger if observer is not None else None
    query = QueryService(client=OpenRouterClient(session=session, prompt_logger=logger), max_result_rows=1000)
    def app(semantic: bool, schema: bool, relationships: bool, candidates: bool) -> ApplicationService:
        ambiguity = AmbiguityService(client=AmbiguityOpenRouterClient(session=session, prompt_logger=logger), prompt_builder=AmbiguityPromptBuilder(include_semantic_findings=semantic, include_schema_context=schema, include_relationships=relationships, include_candidate_evidence=candidates))
        return ApplicationService(querier=query, ambiguity=ambiguity, semantic_column=SemanticColumnAmbiguityService(), event_logger=logger, candidates_per_iteration=candidate_count, max_parallel_candidates=candidate_count, enable_semantic_column_detection=semantic)
    return query, {"candidate_only": app(False, False, False, True), "semantic_only": app(True, True, False, False), "full": app(True, True, True, True)}

def ingest_dataset(dataset_path: Path) -> SchemaMetadata:
    files = tuple(CsvUpload(path.name, path.read_bytes()) for path in sorted(dataset_path.rglob("*.csv")))
    result = ETLService().ingest(files)
    if result.state != ComponentState.ACCEPTED: raise RuntimeError(result.message)
    return result.schema

def _dataset(suite: EvaluationSuite) -> CampaignDataset:
    schema = ingest_dataset(suite.dataset_path); query, _ = build_services(suite.candidate_count)
    refs = validate_reference_suite(suite, schema, query)
    results = {
        key: QueryResult(
            state=ComponentState.ACCEPTED,
            message="reference",
            sql=value["sql"],
            columns=tuple(value["columns"]),
            rows=tuple(tuple(row) for row in value["rows"]),
            truncated=bool(value["truncated"]),
        )
        for key, value in refs.items()
    }
    return CampaignDataset(schema, _hash_files(suite.dataset_path), results, {key: int(value["join_count"]) for key, value in refs.items()})

def _fingerprint(suite: EvaluationSuite, dataset: CampaignDataset) -> CampaignFingerprint:
    return CampaignFingerprint(suite.sha256, dataset.dataset_hash, suite.model, sha256(b"v3-prompts").hexdigest(), "v3", suite.candidate_count, ARMS, sha256(b"runner-v3").hexdigest())

def _choose(case: EvaluationCase, options: tuple[str, ...]) -> tuple[str, bool]:
    for option in options:
        normalized = option.casefold()
        if any(
            group and all(token.casefold() in normalized for token in group)
            for group in case.option_token_groups
        ):
            return option, True
    return options[0], False


def choose_option(
    case: EvaluationCase,
    options: tuple[str, ...],
) -> tuple[str, bool]:
    """Compatibility entry point for deterministic option selection."""

    return _choose(case, options)


def _database_snapshot(schema: SchemaMetadata) -> str | None:
    if not schema.database_path:
        return None
    path = Path(schema.database_path)
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None

def _safety_evidence(result: QueryResult | None, before: str | None, after: str | None) -> SafetyEvidence:
    message = (result.message if result is not None else "").casefold()
    if "validator" in message or "unsafe" in message:
        source = "validator"
    elif "policy" in message or "refus" in message:
        source = "policy"
    else:
        source = "transport"
    return SafetyEvidence(source, before is not None and before == after)

def run_cell(item: WorkItem, case: EvaluationCase, dataset: CampaignDataset, query: QueryService, apps: Mapping[str, ApplicationService], api_key: str, model: str) -> dict[str, Any]:
    started = perf_counter(); turns: list[ClarificationTurn] = []
    before = _database_snapshot(dataset.schema) if case.category == "safety" else None
    if item.arm == "baseline": result = query.query(QueryRequest(case.question, dataset.schema, api_key, model))
    else:
        result: QueryResult | None = None; answers: tuple[str, ...] = ()
        for iteration in range(1, 4):
            workflow = apps[item.arm].submit_query(prompt=case.question, schema=dataset.schema, api_key=api_key, model=model, clarifications=answers, iteration=iteration)
            result = workflow.query_result
            if workflow.complete or workflow.state == ComponentState.FAILED: break
            decision = workflow.ambiguity
            if decision is None or len(decision.options) != 2 or len(turns) == 2:
                result = None; break
            chosen, matched = _choose(case, decision.options)
            turns.append(ClarificationTurn(iteration, decision.mechanism, decision.question, decision.options, chosen, matched, decision.candidate_support, decision.candidate_rejection_reason))
            answers += (f"Question: {decision.question}\nSelected answer: {chosen}",)
        if turns:
            decision = workflow.ambiguity
            last = turns[-1]
            turns[-1] = ClarificationTurn(last.iteration, last.mechanism, last.question, last.options, last.chosen, last.matched_intent, last.candidate_support, last.candidate_rejection_reason, last.fallback_used, bool(decision and decision.compliance_passed), decision.compliant_alternatives if decision else ())
    safety = _safety_evidence(result, before, _database_snapshot(dataset.schema)) if case.category == "safety" else None
    clarifications = [turn.to_record() for turn in turns]
    return {"run": item.repetition, "case_id": case.id, "family_id": case.family_id, "category": case.category, "arm": item.arm, "clarifications": clarifications, "result": {"state": result.state if result else "missing", "sql": result.sql if result else None, "columns": list(result.columns) if result else [], "rows": [list(row) for row in result.rows] if result else []}, "score": score_case(case, result, dataset.references.get(case.id), clarifications, safety_evidence=safety), "duration_seconds": round(perf_counter()-started, 3)}

def run_campaign(config: CampaignConfig) -> CampaignResult:
    if config.workers not in {1, 2}: raise ValueError("workers must be one or two")
    dataset = config.dataset or _dataset(config.suite); schedule = build_schedule(config.suite)
    observer = CampaignObserver(config.campaign_dir, schedule, config.suite.budget_usd); fingerprint = _fingerprint(config.suite, dataset)
    atomic_json(config.campaign_dir / f"references-{dataset.dataset_hash}.json", {"dataset_hash": dataset.dataset_hash, "references": {key: {"sql": value.sql, "columns": list(value.columns), "rows": [list(row) for row in value.rows], "truncated": value.truncated, "join_count": dataset.reference_joins.get(key, 0)} for key, value in dataset.references.items()}, "discovery_warnings": list(dataset.schema.discovery_notes)})
    campaign_path = config.campaign_dir / "campaign.json"; existing = json.loads(campaign_path.read_text()) if campaign_path.exists() else {}
    if existing.get("fingerprint") not in (None, asdict(fingerprint)): raise ValueError("campaign fingerprint is incompatible")
    cases = {case.id: case for case in config.suite.query_cases}
    valid_keys = {item.key for item in schedule}
    checkpoint_paths = tuple(
        path
        for path in observer.checkpoint_dir.glob("*.json")
        if path.stem in valid_keys
    )
    completed = {path.stem for path in checkpoint_paths}
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in checkpoint_paths
    ]
    worker_services = local()
    def execute(item: WorkItem) -> tuple[WorkItem, dict[str, Any]]:
        observer.activate(item, "running")
        if config.cell_runner is not None:
            return item, config.cell_runner(item, cases[item.case_id], dataset)
        services = getattr(worker_services, "services", None)
        if services is None:
            services = (config.service_factory or (lambda o: build_services(o, config.suite.candidate_count)))(observer)
            worker_services.services = services
        query, apps = services
        return item, run_cell(item, cases[item.case_id], dataset, query, apps, config.api_key, config.suite.model)
    pending = [item for item in schedule if item.key not in completed]; stopped = False
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        iterator = iter(pending)
        futures: dict[Any, WorkItem] = {}
        for _ in range(config.workers):
            try:
                item = next(iterator)
            except StopIteration:
                break
            futures[pool.submit(execute, item)] = item
        while futures:
            future = next(as_completed(futures))
            item = futures.pop(future)
            try: _, record = future.result()
            except BudgetStop:
                stopped = True
                observer.deactivate(item)
                continue
            except Exception as error: record = {"run": item.repetition, "case_id": item.case_id, "family_id": item.family_id, "category": item.category, "arm": item.arm, "clarifications": [], "result": {"state": "failed", "sql": None, "columns": [], "rows": []}, "score": {"passed": False, "reason": str(error)}}
            records.append(record); observer.checkpoint(item.key, record); observer.complete_cell(duration=float(record.get("duration_seconds", 0)), arm=item.arm, category=item.category, passed=bool(record["score"].get("passed"))); observer.deactivate(item); completed.add(item.key)
            if not stopped:
                try:
                    next_item = next(iterator)
                except StopIteration:
                    continue
                futures[pool.submit(execute, next_item)] = next_item
    metadata = {
        "report_type": "dbwhisperer_v3_run",
        "suite_version": config.suite.version,
        "suite_hash": config.suite.sha256,
        "model": config.suite.model,
        "arms": list(ARMS),
        "fingerprint": asdict(fingerprint),
    }
    payload = {
        **metadata,
        "complete": not stopped and len(completed) == len(schedule),
        "records": records,
    }
    config.campaign_dir.mkdir(parents=True, exist_ok=True)
    for repetition in range(1, config.suite.repetitions + 1):
        atomic_json(config.campaign_dir / f"run-{repetition:02d}.json", {
            **metadata,
            "repetition": repetition,
            "records": [record for record in records if record["run"] == repetition],
        })
    atomic_json(campaign_path, payload)
    return CampaignResult(fingerprint, frozenset(completed), tuple(records), stopped)

def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE); parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--workers", type=int, default=2); args = parser.parse_args(); key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key: raise SystemExit("OPENROUTER_API_KEY is required for a live V3 campaign.")
    run_campaign(CampaignConfig(load_suite(args.suite), args.output, key, args.workers))

if __name__ == "__main__": main()
