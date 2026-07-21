"""Independent suite contracts for Evaluation V3."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


QUERY_CATEGORIES = {
    "relationship_scope", "semantic_column", "control", "correctness", "safety"
}


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
    suite_path = Path(path).resolve()
    raw = suite_path.read_bytes()
    payload = json.loads(raw)
    base = suite_path.parent
    cases = tuple(
        EvaluationCase(
            id=str(item.get("id", "")).strip(),
            family_id=str(item.get("family_id", "")).strip(),
            kind=str(item.get("kind", "")).strip(),
            category=str(item.get("category", "")).strip(),
            question=str(item.get("question", "")).strip(),
            ambiguous=bool(item.get("ambiguous", False)),
            should_clarify=bool(item.get("should_clarify", False)),
            expected_mechanism=str(item.get("expected_mechanism", "none")),
            intent_id=str(item.get("intent_id", "")),
            option_tokens=tuple(str(value).casefold() for value in item.get("option_tokens", [])),
            expected_sql=item.get("expected_sql"),
            fixture_files=tuple((base / value).resolve() for value in item.get("fixture_files", [])),
            manifest=item.get("manifest"),
        )
        for item in payload.get("cases", [])
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
        cases=cases,
        sha256=sha256(raw).hexdigest(),
    )
    validate_suite_shape(suite)
    return suite


def validate_suite_shape(suite: EvaluationSuite) -> None:
    errors: list[str] = []
    expected_counts = {
        "relationship_scope": 4,
        "semantic_column": 4,
        "control": 4,
        "correctness": 2,
        "safety": 2,
        "etl": 2,
    }
    counts = {
        category: sum(case.category == category for case in suite.cases)
        for category in expected_counts
    }
    if counts != expected_counts:
        errors.append(f"category counts are {counts}, expected {expected_counts}")
    if suite.version.split(".")[0] != "3":
        errors.append("suite version must be V3")
    if suite.repetitions < 1 or suite.candidate_count < 2:
        errors.append("repetitions must be positive and candidate_count at least two")
    ids = [case.id for case in suite.cases]
    if not all(ids) or len(ids) != len(set(ids)):
        errors.append("case IDs must be unique and non-empty")
    for case in suite.query_cases:
        if case.category not in QUERY_CATEGORIES or not case.question:
            errors.append(f"{case.id}: invalid query case")
        if case.category != "safety" and not case.expected_sql:
            errors.append(f"{case.id}: expected SQL is required")
        if case.expected_mechanism == "join-path":
            errors.append(f"{case.id}: removed join-path mechanism is forbidden")
    missing = [str(path) for case in suite.etl_cases for path in case.fixture_files if not path.is_file()]
    if missing:
        errors.append("missing fixtures: " + ", ".join(missing))
    if errors:
        raise ValueError("Invalid Evaluation V3 suite: " + "; ".join(errors))
