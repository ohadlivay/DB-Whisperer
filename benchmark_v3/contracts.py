"""Independent suite contracts for Evaluation V3."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any

from sqlglot import exp, parse_one

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from db_whisperer.contracts import (
    ComponentState,
    QueryCandidate,
    SchemaMetadata,
)
from db_whisperer.querier import QueryService
from db_whisperer.querier.sql_validator import (
    SQLValidationError,
    validate_read_only_sql,
)


QUERY_CATEGORIES = {"ambiguity", "control", "correctness", "safety"}
ALLOWED_MECHANISMS = {"none", "candidate-comparison", "semantic-column"}
ALLOWED_COMPARISON_MODES = {
    "scalar",
    "multiset",
    "ordered",
    "top_n",
    "compatible_subset",
}
REQUIRED_CAPABILITIES = {
    "scalar",
    "grouping",
    "ordering",
    "dictionary_join",
    "multi_table_filter",
    "date_arithmetic",
    "null_handling",
    "distinct",
    "having",
    "ranking",
    "top_n",
    "write_safety",
    "multi_statement_safety",
    "external_scan_safety",
    "missing_schema",
}
EXPECTED_CASE_IDS = {
    "from_2024_birth",
    "from_2024_admission",
    "ctl_from_2024_birth",
    "ctl_from_2024_admission",
    "stay_hospital",
    "stay_icu",
    "ctl_stay_hospital",
    "ctl_stay_icu",
    "diagnoses_occurrences",
    "diagnoses_distinct_patients",
    "ctl_diagnoses_occurrences",
    "ctl_diagnoses_distinct_patients",
    "count_admissions",
    "admissions_by_type",
    "lab_frequency_with_labels",
    "icu_mortality_by_first_careunit",
    "admission_duration_null_safe",
    "patients_with_multiple_admissions_ranked",
    "safe_delete",
    "safe_multi_statement_ddl",
    "safe_external_scan",
    "missing_clinical_concept",
    "etl_single",
    "etl_relational",
}


@dataclass(frozen=True)
class ReferenceContract:
    comparison_mode: str
    required_filters: tuple[str, ...] = ()
    required_grouping: tuple[str, ...] = ()
    ordered: bool = False
    limit: int | None = None


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    family_id: str
    kind: str
    category: str
    capabilities: tuple[str, ...] = ()
    question: str = ""
    ambiguous: bool = False
    should_clarify: bool = False
    expected_mechanism: str = "none"
    intent_id: str = ""
    option_token_groups: tuple[tuple[str, ...], ...] = ()
    required_tables: tuple[str, ...] = ()
    forbidden_tables: tuple[str, ...] = ()
    required_column_groups: tuple[tuple[str, ...], ...] = ()
    expected_sql: str | None = None
    reference: ReferenceContract | None = None
    fixture_files: tuple[Path, ...] = ()
    manifest: dict[str, Any] | None = None

    @property
    def comparison_mode(self) -> str:
        """Expose the result comparison contract directly to later scorers."""
        return self.reference.comparison_mode if self.reference else "none"

    @property
    def option_tokens(self) -> tuple[str, ...]:
        """Retain the V3 runner's original flattened signature interface."""
        return tuple(
            token
            for group in self.option_token_groups
            for token in group
        )


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


def _string_tuple(values: Any) -> tuple[str, ...]:
    return tuple(str(value).casefold() for value in values or ())


def _string_groups(values: Any) -> tuple[tuple[str, ...], ...]:
    return tuple(_string_tuple(group) for group in values or ())


def _load_reference(payload: Any) -> ReferenceContract | None:
    if payload is None:
        return None
    return ReferenceContract(
        comparison_mode=str(payload.get("comparison_mode", "")).strip(),
        required_filters=tuple(
            str(value).strip()
            for value in payload.get("required_filters", ())
        ),
        required_grouping=tuple(
            str(value).strip()
            for value in payload.get("required_grouping", ())
        ),
        ordered=bool(payload.get("ordered", False)),
        limit=(
            int(payload["limit"])
            if payload.get("limit") is not None
            else None
        ),
    )


def load_suite(path: str | Path) -> EvaluationSuite:
    suite_path = Path(path).resolve()
    raw = suite_path.read_bytes()
    payload = json.loads(raw)
    if _contains_retired_contract(payload.get("cases", ())):
        raise ValueError(
            "Invalid Evaluation V3 suite: retired join-path/jp_ "
            "contract values are forbidden"
        )
    base = suite_path.parent
    cases = tuple(
        EvaluationCase(
            id=str(item.get("id", "")).strip(),
            family_id=str(item.get("family_id", "")).strip(),
            kind=str(item.get("kind", "")).strip(),
            category=str(item.get("category", "")).strip(),
            capabilities=_string_tuple(item.get("capabilities", ())),
            question=str(item.get("question", "")).strip(),
            ambiguous=bool(item.get("ambiguous", False)),
            should_clarify=bool(item.get("should_clarify", False)),
            expected_mechanism=str(
                item.get("expected_mechanism", "none")
            ).strip(),
            intent_id=str(item.get("intent_id", "")).strip(),
            option_token_groups=_string_groups(
                item.get("option_token_groups", ())
            ),
            required_tables=_string_tuple(
                item.get("required_tables", ())
            ),
            forbidden_tables=_string_tuple(
                item.get("forbidden_tables", ())
            ),
            required_column_groups=_string_groups(
                item.get("required_column_groups", ())
            ),
            expected_sql=item.get("expected_sql"),
            reference=_load_reference(item.get("reference")),
            fixture_files=tuple(
                (base / str(value)).resolve()
                for value in item.get("fixture_files", ())
            ),
            manifest=item.get("manifest"),
        )
        for item in payload.get("cases", ())
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


def _contains_retired_contract(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return (
            "join_path" in normalized
            or "join-path" in normalized
            or normalized.startswith("jp_")
        )
    if isinstance(value, dict):
        return any(
            _contains_retired_contract(key)
            or _contains_retired_contract(item)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set)):
        return any(_contains_retired_contract(item) for item in value)
    if is_dataclass(value):
        return any(
            _contains_retired_contract(getattr(value, field.name))
            for field in fields(value)
        )
    return False


def _case_contract_values(case: EvaluationCase) -> tuple[Any, ...]:
    return (
        case.id,
        case.family_id,
        case.kind,
        case.category,
        case.capabilities,
        case.question,
        case.expected_mechanism,
        case.intent_id,
        case.option_token_groups,
        case.required_tables,
        case.forbidden_tables,
        case.required_column_groups,
        case.expected_sql or "",
        case.reference,
        case.manifest or {},
    )


def validate_suite_shape(suite: EvaluationSuite) -> None:
    errors: list[str] = []
    if suite.version.split(".")[0] != "3":
        errors.append("suite version must be V3")
    if len(suite.cases) != 24:
        errors.append(f"suite must contain exactly 24 cases, got {len(suite.cases)}")
    if len(suite.query_cases) != 22 or len(suite.etl_cases) != 2:
        errors.append("suite must contain exactly 22 query and 2 ETL cases")
    if (
        suite.candidate_count != 3
        or suite.repetitions != 5
        or suite.budget_usd != 3.75
    ):
        errors.append(
            "campaign settings must be K=3, repetitions=5, budget_usd=3.75"
        )

    ids = [case.id for case in suite.cases]
    if not all(ids) or len(ids) != len(set(ids)):
        errors.append("case IDs must be unique and non-empty")
    elif set(ids) != EXPECTED_CASE_IDS:
        errors.append("case IDs must match the frozen V3 coverage matrix")
    if any(
        _contains_retired_contract(_case_contract_values(case))
        for case in suite.cases
    ):
        errors.append("retired join-path/jp_ contract values are forbidden")

    capabilities = {
        capability
        for case in suite.query_cases
        for capability in case.capabilities
    }
    missing_capabilities = REQUIRED_CAPABILITIES - capabilities
    if missing_capabilities:
        errors.append(
            "missing required capability coverage: "
            + ", ".join(sorted(missing_capabilities))
        )

    for case in suite.query_cases:
        if case.category not in QUERY_CATEGORIES or not case.question:
            errors.append(f"{case.id}: invalid query case")
        if case.expected_mechanism not in ALLOWED_MECHANISMS:
            errors.append(f"{case.id}: invalid ambiguity mechanism")
        if case.category == "safety":
            if case.expected_sql is not None or case.reference is not None:
                errors.append(
                    f"{case.id}: safety cases cannot define reference SQL"
                )
        else:
            if not case.expected_sql or case.reference is None:
                errors.append(
                    f"{case.id}: expected SQL and reference are required"
                )
            else:
                try:
                    validate_read_only_sql(case.expected_sql)
                except SQLValidationError as error:
                    errors.append(
                        f"{case.id}: invalid or unsafe reference SQL: {error}"
                    )
                if not case.reference.comparison_mode:
                    errors.append(
                        f"{case.id}: reference comparison mode is required"
                    )
                elif (
                    case.reference.comparison_mode
                    not in ALLOWED_COMPARISON_MODES
                ):
                    errors.append(
                        f"{case.id}: unsupported reference comparison mode"
                    )
        if case.should_clarify != case.ambiguous:
            errors.append(
                f"{case.id}: ambiguous and should_clarify must agree"
            )
        if case.should_clarify:
            if (
                case.category != "ambiguity"
                or case.expected_mechanism == "none"
                or not case.intent_id
                or not case.option_token_groups
            ):
                errors.append(f"{case.id}: incomplete ambiguity contract")
        elif case.category == "control" and case.expected_mechanism != "none":
            errors.append(f"{case.id}: controls cannot expect clarification")

    ambiguous = [case for case in suite.query_cases if case.should_clarify]
    families = {case.family_id for case in ambiguous}
    if len(families) != 3:
        errors.append("suite must contain exactly three ambiguity families")
    for family in families:
        intentions = [
            case for case in ambiguous if case.family_id == family
        ]
        controls = [
            case
            for case in suite.query_cases
            if case.family_id == family and case.category == "control"
        ]
        if (
            len(intentions) != 2
            or len({case.question for case in intentions}) != 1
            or len({case.intent_id for case in intentions}) != 2
        ):
            errors.append(
                f"{family}: ambiguity family must pair two intentions "
                "with identical wording"
            )
        if (
            len(controls) != 2
            or {case.intent_id for case in controls}
            != {case.intent_id for case in intentions}
        ):
            errors.append(
                f"{family}: ambiguity family must have two matching controls"
            )
        ambiguous_questions = {
            case.question.casefold() for case in intentions
        }
        if any(
            case.question.casefold() in ambiguous_questions
            for case in controls
        ):
            errors.append(
                f"{family}: control wording must resolve the ambiguity"
            )

    missing_fixtures = [
        str(path)
        for case in suite.etl_cases
        for path in case.fixture_files
        if not path.is_file()
    ]
    if any(
        not case.fixture_files or case.manifest is None
        for case in suite.etl_cases
    ):
        errors.append("ETL cases require fixtures and manifests")
    if missing_fixtures:
        errors.append("missing fixtures: " + ", ".join(missing_fixtures))
    if errors:
        raise ValueError("Invalid Evaluation V3 suite: " + "; ".join(errors))


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _parsed_join_count(sql: str) -> int:
    validated = validate_read_only_sql(sql)
    tree = parse_one(validated, read="duckdb")
    return sum(1 for _ in tree.find_all(exp.Join))


def validate_reference_suite(
    suite: EvaluationSuite,
    schema: SchemaMetadata,
    query: QueryService,
) -> dict[str, dict[str, Any]]:
    """Execute reference SQL once and return serialized result/join evidence."""
    validate_suite_shape(suite)
    if not schema.database_path:
        raise ValueError("Reference validation requires a database path.")

    evidence: dict[str, dict[str, Any]] = {}
    for case in suite.query_cases:
        if case.expected_sql is None:
            continue
        result = query.execute_candidate(
            QueryCandidate(
                attempt_number=0,
                state=ComponentState.ACCEPTED,
                sql=case.expected_sql,
            ),
            schema.database_path,
        )
        if result.state != ComponentState.ACCEPTED:
            raise ValueError(
                f"{case.id}: reference SQL did not execute: {result.message}"
            )
        evidence[case.id] = {
            "sql": result.sql,
            "columns": list(result.columns),
            "rows": [
                [_serialize_value(value) for value in row]
                for row in result.rows
            ],
            "truncated": result.truncated,
            "join_count": _parsed_join_count(case.expected_sql),
            "comparison_mode": case.comparison_mode,
        }
    return evidence
