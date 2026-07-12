"""Versioned data contracts for Evaluation V2."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


QUERY_CATEGORIES = {"join_path", "semantic_column", "control", "correctness", "safety"}


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    family_id: str
    kind: str
    category: str
    question: str = ""
    ambiguous: bool = False
    should_clarify: bool = False
    expected_mechanism: str = "none"
    intent_id: str = ""
    option_tokens: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    forbidden_tables: tuple[str, ...] = ()
    required_column_groups: tuple[tuple[str, ...], ...] = ()
    minimum_joins: int = 0
    expected_sql: str | None = None
    fixture_files: tuple[Path, ...] = ()
    manifest: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvaluationSuite:
    name: str
    version: str
    path: Path
    dataset_path: Path
    model: str
    repetitions: int
    candidate_count: int
    budget_usd: float
    cases: tuple[EvaluationCase, ...]
    sha256: str

    @property
    def query_cases(self) -> tuple[EvaluationCase, ...]:
        return tuple(case for case in self.cases if case.kind == "query")

    @property
    def etl_cases(self) -> tuple[EvaluationCase, ...]:
        return tuple(case for case in self.cases if case.kind == "etl")


def load_suite(path: str | Path) -> EvaluationSuite:
    suite_path = Path(path).expanduser().resolve()
    raw_bytes = suite_path.read_bytes()
    payload = json.loads(raw_bytes)
    base = suite_path.parent
    cases: list[EvaluationCase] = []
    for item in payload.get("cases", []):
        kind = str(item.get("kind", ""))
        cases.append(
            EvaluationCase(
                id=str(item.get("id", "")).strip(),
                family_id=str(item.get("family_id", "")).strip(),
                kind=kind,
                category=str(item.get("category", "")).strip(),
                question=str(item.get("question", "")).strip(),
                ambiguous=bool(item.get("ambiguous", False)),
                should_clarify=bool(item.get("should_clarify", False)),
                expected_mechanism=str(item.get("expected_mechanism", "none")),
                intent_id=str(item.get("intent_id", "")),
                option_tokens=tuple(str(v).lower() for v in item.get("option_tokens", [])),
                required_tables=tuple(str(v).lower() for v in item.get("required_tables", [])),
                forbidden_tables=tuple(str(v).lower() for v in item.get("forbidden_tables", [])),
                required_column_groups=tuple(
                    tuple(str(v).lower() for v in group)
                    for group in item.get("required_column_groups", [])
                ),
                minimum_joins=int(item.get("minimum_joins", 0)),
                expected_sql=item.get("expected_sql"),
                fixture_files=tuple((base / value).resolve() for value in item.get("fixture_files", [])),
                manifest=item.get("manifest"),
            )
        )
    suite = EvaluationSuite(
        name=str(payload.get("name", "")).strip(),
        version=str(payload.get("version", "")).strip(),
        path=suite_path,
        dataset_path=(base / str(payload.get("dataset", ""))).resolve(),
        model=str(payload.get("model", "")).strip(),
        repetitions=int(payload.get("repetitions", 0)),
        candidate_count=int(payload.get("candidate_count", 0)),
        budget_usd=float(payload.get("budget_usd", 0)),
        cases=tuple(cases),
        sha256=sha256(raw_bytes).hexdigest(),
    )
    validate_suite_shape(suite)
    return suite


def validate_suite_shape(suite: EvaluationSuite) -> None:
    errors: list[str] = []
    if len(suite.cases) != 18:
        errors.append(f"expected 18 cases, found {len(suite.cases)}")
    if suite.repetitions != 5:
        errors.append("repetitions must be 5")
    if suite.candidate_count != 2:
        errors.append("candidate_count must be 2")
    ids = [case.id for case in suite.cases]
    if not all(ids) or len(ids) != len(set(ids)):
        errors.append("case IDs must be non-empty and unique")
    expected_counts = {
        "join_path": 4,
        "semantic_column": 4,
        "control": 4,
        "correctness": 2,
        "safety": 2,
        "etl": 2,
    }
    actual = {key: sum(case.category == key for case in suite.cases) for key in expected_counts}
    if actual != expected_counts:
        errors.append(f"category counts are {actual}, expected {expected_counts}")
    for case in suite.cases:
        if case.kind == "query":
            if case.category not in QUERY_CATEGORIES or not case.question:
                errors.append(f"{case.id}: invalid query case")
            if case.category == "safety" and case.expected_sql is not None:
                errors.append(f"{case.id}: safety case must not have expected SQL")
            if case.category != "safety" and not case.expected_sql:
                errors.append(f"{case.id}: query case requires expected SQL")
        elif case.kind == "etl":
            if not case.fixture_files or not case.manifest:
                errors.append(f"{case.id}: ETL case requires fixtures and manifest")
        else:
            errors.append(f"{case.id}: unknown kind {case.kind!r}")
        if case.minimum_joins < 0:
            errors.append(f"{case.id}: minimum_joins cannot be negative")
    ambiguous_families: dict[str, list[EvaluationCase]] = {}
    for case in suite.cases:
        if case.ambiguous:
            ambiguous_families.setdefault(case.family_id, []).append(case)
    if len(ambiguous_families) != 4:
        errors.append("suite must contain four ambiguous families")
    for family, members in ambiguous_families.items():
        if len(members) != 2 or len({case.question for case in members}) != 1:
            errors.append(f"{family}: must contain two identical-wording variants")
        if len({case.intent_id for case in members}) != 2:
            errors.append(f"{family}: interpretations must be distinct")
    for family in ambiguous_families:
        controls = [case for case in suite.cases if case.category == "control" and case.family_id == family]
        if len(controls) != 1:
            errors.append(f"{family}: requires exactly one matched control")
    missing = [str(path) for case in suite.etl_cases for path in case.fixture_files if not path.is_file()]
    if missing:
        errors.append("missing fixture files: " + ", ".join(missing))
    if errors:
        raise ValueError("Invalid V2 suite: " + "; ".join(errors))

