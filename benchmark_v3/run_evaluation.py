"""Run the four-arm Evaluation V3 hybrid-ambiguity campaign."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from benchmark_v3.contracts import EvaluationCase, load_suite
from benchmark_v3.scoring import score_case
from db_whisperer.ambiguity import (
    AmbiguityPromptBuilder,
    AmbiguityService,
    SemanticColumnAmbiguityService,
)
from db_whisperer.application import ApplicationService
from db_whisperer.contracts import (
    ComponentState,
    CsvUpload,
    QueryCandidate,
    QueryRequest,
    QueryResult,
)
from db_whisperer.etler import ETLService
from db_whisperer.querier import QueryService


BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE = BENCHMARK_DIR / "cases" / "evaluation_cases.json"
DEFAULT_OUTPUT = BENCHMARK_DIR / "results" / "evaluation_v3.json"
ARMS = ("baseline", "candidate_only", "semantic_only", "full")


def build_services(candidate_count: int) -> tuple[QueryService, dict[str, ApplicationService]]:
    query = QueryService(max_result_rows=1000)

    def application(
        *,
        semantic: bool,
        schema: bool,
        relationships: bool,
        candidates: bool,
    ) -> ApplicationService:
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
        "candidate_only": application(
            semantic=False, schema=False, relationships=False, candidates=True
        ),
        "semantic_only": application(
            semantic=True, schema=True, relationships=False, candidates=False
        ),
        "full": application(
            semantic=True, schema=True, relationships=True, candidates=True
        ),
    }


def choose_option(case: EvaluationCase, options: tuple[str, ...]) -> tuple[str, bool]:
    for option in options:
        normalized = option.casefold()
        if case.option_tokens and all(token in normalized for token in case.option_tokens):
            return option, True
    return options[0], False


def run_application(
    case: EvaluationCase,
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
                history[-1]["compliance_passed"] = bool(
                    decision is not None
                    and decision.compliance_passed is True
                )
                history[-1]["compliant_alternatives"] = (
                    list(decision.compliant_alternatives)
                    if decision is not None
                    else []
                )
            return workflow.query_result, history
        decision = workflow.ambiguity
        if decision is None or len(decision.options) != 2:
            return workflow.query_result, history
        chosen, matched = choose_option(case, decision.options)
        history.append({
            "question": decision.question,
            "options": list(decision.options),
            "chosen": chosen,
            "matched_intent": matched,
            "mechanism": decision.mechanism,
        })
        clarifications = (*clarifications, f"Question: {decision.question}\nSelected answer: {chosen}")
    return None, history


def ingest_dataset(dataset_path: Path) -> Any:
    files = tuple(
        CsvUpload(path.name, path.read_bytes())
        for path in sorted(dataset_path.rglob("*.csv"))
    )
    result = ETLService().ingest(files)
    if result.state != ComponentState.ACCEPTED:
        raise RuntimeError(result.message)
    return result.schema


def expected_result(case: EvaluationCase, query: QueryService, database_path: str) -> QueryResult | None:
    if case.expected_sql is None:
        return None
    return query.execute_candidate(
        QueryCandidate(0, ComponentState.ACCEPTED, sql=case.expected_sql),
        database_path,
    )


def run(suite_path: Path, output: Path, api_key: str) -> dict[str, Any]:
    suite = load_suite(suite_path)
    schema = ingest_dataset(suite.dataset_path)
    query, applications = build_services(suite.candidate_count)
    records: list[dict[str, Any]] = []
    for repetition in range(1, suite.repetitions + 1):
        for case in suite.query_cases:
            reference = expected_result(case, query, schema.database_path or "")
            for arm in ARMS:
                if arm == "baseline":
                    actual = query.query(QueryRequest(case.question, schema, api_key, suite.model))
                    clarifications: list[dict[str, Any]] = []
                else:
                    actual, clarifications = run_application(
                        case, schema, applications[arm], api_key, suite.model
                    )
                records.append({
                    "run": repetition,
                    "case_id": case.id,
                    "family_id": case.family_id,
                    "category": case.category,
                    "arm": arm,
                    "clarifications": clarifications,
                    "result": {
                        "state": actual.state if actual else "missing",
                        "sql": actual.sql if actual else None,
                        "columns": list(actual.columns) if actual else [],
                        "rows": [list(row) for row in actual.rows] if actual else [],
                    },
                    "score": score_case(case, actual, reference, clarifications),
                })
    report = {
        "report_type": "dbwhisperer_v3_run",
        "suite_version": suite.version,
        "suite_hash": suite.sha256,
        "model": suite.model,
        "arms": list(ARMS),
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required for a live V3 campaign.")
    run(args.suite, args.output, api_key)


if __name__ == "__main__":
    main()
