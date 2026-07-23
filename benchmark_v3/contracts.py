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
ARMS = ("baseline", "candidate_only", "semantic_only", "full")


def current_id(value: str) -> str:
    return f"rs_{value[3:]}" if value.startswith("jp_") else value


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
    required_predicates: tuple[dict[str, Any], ...] = ()
    required_aggregates: tuple[str, ...] = ()
    result_policy: str = "relational"
    result_key_groups: tuple[tuple[str, ...], ...] = ()
    ordered_result: bool = False
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
            required_tables=tuple(str(value).casefold() for value in item.get("required_tables", [])),
            forbidden_tables=tuple(str(value).casefold() for value in item.get("forbidden_tables", [])),
            required_column_groups=tuple(
                tuple(str(value).casefold() for value in group)
                for group in item.get("required_column_groups", [])
            ),
            minimum_joins=int(item.get("minimum_joins", 0)),
            required_predicates=tuple(item.get("required_predicates", [])),
            required_aggregates=tuple(
                str(value).casefold() for value in item.get("required_aggregates", [])
            ),
            result_policy=str(item.get("result_policy", "relational")),
            result_key_groups=tuple(
                tuple(str(value).casefold() for value in group)
                for group in item.get("result_key_groups", [])
            ),
            ordered_result=bool(item.get("ordered_result", False)),
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
    if len(suite.cases) != sum(expected_counts.values()):
        errors.append(f"expected 18 cases, found {len(suite.cases)}")
    if suite.version.split(".")[0] != "3":
        errors.append("suite version must be V3")
    if suite.repetitions != 5 or suite.candidate_count != 2:
        errors.append("the frozen V3 suite requires five repetitions and two candidates")
    if not suite.dataset_path.is_dir():
        errors.append(f"dataset directory does not exist: {suite.dataset_path}")
    ids = [case.id for case in suite.cases]
    if not all(ids) or len(ids) != len(set(ids)):
        errors.append("case IDs must be unique and non-empty")
    for case in suite.query_cases:
        if case.category not in QUERY_CATEGORIES or not case.question:
            errors.append(f"{case.id}: invalid query case")
        if case.category == "safety" and case.expected_sql is not None:
            errors.append(f"{case.id}: safety cases cannot define expected SQL")
        if case.category != "safety" and not case.expected_sql:
            errors.append(f"{case.id}: expected SQL is required")
        if case.expected_mechanism == "join-path":
            errors.append(f"{case.id}: removed join-path mechanism is forbidden")
        if case.should_clarify and (
            not case.option_tokens
            or case.expected_mechanism not in {"candidate-comparison", "semantic-column"}
        ):
            errors.append(f"{case.id}: invalid clarification expectation")
        if case.minimum_joins < 0:
            errors.append(f"{case.id}: minimum_joins cannot be negative")
        if case.result_policy not in {"relational", "scalar", "keyed_rows", "safety"}:
            errors.append(f"{case.id}: invalid result_policy")
        if case.result_policy == "keyed_rows" and not case.result_key_groups:
            errors.append(f"{case.id}: keyed_rows requires result_key_groups")
        for predicate in case.required_predicates:
            if (
                predicate.get("operator") not in {"eq", "is_not_null"}
                or not str(predicate.get("column", "")).strip()
                or (predicate.get("operator") == "eq" and "value" not in predicate)
            ):
                errors.append(f"{case.id}: invalid required predicate")
    for case in suite.etl_cases:
        if not case.fixture_files or not case.manifest:
            errors.append(f"{case.id}: ETL fixtures and manifest are required")
    if len(suite.query_cases) + len(suite.etl_cases) != len(suite.cases):
        errors.append("every case kind must be query or etl")
    ambiguous_families = {
        case.family_id for case in suite.query_cases if case.ambiguous
    }
    for family in ambiguous_families:
        members = [case for case in suite.query_cases if case.ambiguous and case.family_id == family]
        controls = [case for case in suite.query_cases if case.category == "control" and case.family_id == family]
        if (
            len(members) != 2
            or len({case.question for case in members}) != 1
            or len({case.intent_id for case in members}) != 2
        ):
            errors.append(f"{family}: requires two distinct intents with identical wording")
        if len(controls) != 1:
            errors.append(f"{family}: requires exactly one matched control")
    missing = [str(path) for case in suite.etl_cases for path in case.fixture_files if not path.is_file()]
    if missing:
        errors.append("missing fixtures: " + ", ".join(missing))
    if errors:
        raise ValueError("Invalid Evaluation V3 suite: " + "; ".join(errors))
