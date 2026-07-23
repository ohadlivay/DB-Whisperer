"""Shared data contracts for communication between project components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ComponentState(StrEnum):
    """Current state of a component response."""

    ACCEPTED = "accepted"
    FAILED = "failed"
    PENDING = "pending"


@dataclass(frozen=True)
class CsvUpload:
    """CSV data received from an interface."""

    name: str
    content: bytes
    content_type: str = "text/csv"


@dataclass(frozen=True)
class ColumnMetadata:
    """One column discovered in an imported CSV."""

    name: str
    data_type: str
    table_name: str = ""


@dataclass(frozen=True)
class TableSchema:
    """Schema for one table loaded from a single CSV file."""

    table_name: str
    columns: tuple[ColumnMetadata, ...]
    row_count: int
    key_columns: tuple[str, ...] = ()
    id_key_columns: tuple[str, ...] = ()
    primary_key: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relationship:
    """A discovered foreign-key relationship between two loaded tables.

    Relationships are advisory metadata derived from naming and data overlap;
    they are never enforced as DuckDB constraints.
    """

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    overlap: float = 1.0
    score: float = 1.0
    cardinality: str = "many-to-one"
    ambiguous: bool = False
    sampled: bool = False


@dataclass(frozen=True)
class SchemaMetadata:
    """Minimal schema information exposed by the ETL component."""

    database_path: str | None = None
    source_names: tuple[str, ...] = ()
    table_names: tuple[str, ...] = ()
    columns: tuple[ColumnMetadata, ...] = ()
    row_count: int | None = None
    tables: tuple[TableSchema, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    discovery_complete: bool = True
    discovery_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngestionResult:
    """Result returned after CSV files reach the ETL boundary."""

    state: ComponentState
    message: str
    schema: SchemaMetadata


@dataclass(frozen=True)
class QueryRequest:
    """Input for natural-language query generation."""

    prompt: str
    schema: SchemaMetadata
    api_key: str
    model: str
    clarifications: tuple[str, ...] = ()
    attempt_number: int = 1
    compliance_retry: bool = False


@dataclass(frozen=True)
class QueryCandidate:
    """One candidate returned by the Querier."""

    attempt_number: int
    state: ComponentState
    sql: str | None = None
    message: str = ""


@dataclass(frozen=True)
class QueryResult:
    """Validated SQL and rows returned by the Querier."""

    state: ComponentState
    message: str
    sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False
    # A stable failure category is intentionally separate from human-facing text.
    failure_kind: str = ""


@dataclass(frozen=True)
class ExecutedQueryPair:
    """Generated SQL paired with the table produced by its execution."""

    candidate_id: str
    sql: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool = False

    @classmethod
    def from_query_result(
        cls,
        candidate_id: str,
        result: QueryResult,
    ) -> ExecutedQueryPair:
        """Create a pair from one successfully executed query result."""
        if result.state != ComponentState.ACCEPTED or not result.sql:
            raise ValueError(
                "Only successful query results can become ambiguity pairs."
            )
        return cls(
            candidate_id=candidate_id,
            sql=result.sql,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
        )


@dataclass(frozen=True)
class AmbiguityRequest:
    """Executed alternatives plus supporting schema evidence for Component B."""

    user_query: str
    pairs: tuple[ExecutedQueryPair, ...]
    api_key: str
    model: str
    clarifications: tuple[str, ...] = ()
    schema: SchemaMetadata = field(default_factory=SchemaMetadata)
    semantic_analysis: SemanticColumnAnalysis | None = None


@dataclass(frozen=True)
class SemanticColumnRequest:
    """User request plus schema sent to pre-SQL semantic analysis."""

    user_query: str
    schema: SchemaMetadata
    api_key: str
    model: str
    clarifications: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticColumnCandidate:
    """One exact schema column associated with a vague user term."""

    table: str
    column: str
    data_type: str
    bucket: str

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class SemanticAmbiguityTerm:
    """A vague user term with two or more plausible same-kind columns."""

    term: str
    bucket: str
    columns: tuple[SemanticColumnCandidate, ...]


@dataclass(frozen=True)
class SemanticColumnAnalysis:
    """Pre-SQL semantic findings retained for the unified ambiguity judge."""

    state: ComponentState
    terms: tuple[SemanticAmbiguityTerm, ...] = ()
    reason: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.state == ComponentState.ACCEPTED and bool(self.terms)


@dataclass(frozen=True)
class AmbiguityDecision:
    """Pass or one two-option clarification returned by Component B.

    ``mechanism`` records the evidence source selected by the unified judge:
    ``"candidate-comparison"`` or ``"semantic-column"``.
    """

    state: ComponentState
    passed: bool | None = None
    question: str | None = None
    options: tuple[str, ...] = ()
    reason: str = ""
    mechanism: str = ""
    evidence_columns: tuple[str, ...] = ()
    evidence_alternatives: tuple[str, ...] = ()
    candidate_support: tuple[tuple[str, int], ...] = ()
    candidate_rejection_reason: str = ""
    compliance_passed: bool | None = None
    compliant_alternatives: tuple[str, ...] = ()
    rejected_alternatives: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class QueryWorkflowResult:
    """Application-layer response returned to the GUI."""

    state: ComponentState
    message: str
    iteration: int = 1
    complete: bool = False
    query_result: QueryResult | None = None
    candidates: tuple[QueryCandidate, ...] = field(default_factory=tuple)
    candidate_results: tuple[QueryResult, ...] = field(default_factory=tuple)
    ambiguity: AmbiguityDecision | None = None
    semantic_fallback_used: bool = False
    compliance_retry_used: bool = False
