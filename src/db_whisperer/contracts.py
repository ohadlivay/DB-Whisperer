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
class JoinPath:
    """One ordered chain of tables connected by discovered relationships.

    ``tables`` is the node sequence visited from a source table to a target
    table (length >= 2). ``relationships`` are the foreign-key edges used to
    walk it, in traversal order, so ``len(relationships) == len(tables) - 1``.
    A relationship is undirected for join purposes; the same edge can be walked
    parent-to-child or child-to-parent depending on the surrounding tables.
    """

    tables: tuple[str, ...]
    relationships: tuple[Relationship, ...]

    @property
    def hop_count(self) -> int:
        """Number of join edges in the path."""
        return len(self.relationships)

    @property
    def intermediate_tables(self) -> tuple[str, ...]:
        """Tables strictly between the source and target endpoints."""
        return self.tables[1:-1]


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
    """User request and K executed SQL/table pairs sent to Component B."""

    user_query: str
    pairs: tuple[ExecutedQueryPair, ...]
    api_key: str
    model: str
    clarifications: tuple[str, ...] = ()


@dataclass(frozen=True)
class JoinPathRequest:
    """User request plus schema graph sent to the join-path detector.

    The detector extracts the entities mentioned in ``user_query``, maps them
    to tables in ``schema``, enumerates join paths between those tables, and
    flags ambiguity when more than one distinct path connects an entity pair.
    """

    user_query: str
    schema: SchemaMetadata
    api_key: str
    model: str
    clarifications: tuple[str, ...] = ()


@dataclass(frozen=True)
class AmbiguityDecision:
    """Pass or one two-option clarification returned by Component B.

    ``mechanism`` records which ambiguity mechanism produced the decision
    (for example ``"join-path"`` for schema-graph join-path multiplicity, or
    the default empty string for the executed-candidate comparison judge), so
    the GUI and evaluation harness can distinguish them.
    """

    state: ComponentState
    passed: bool | None = None
    question: str | None = None
    options: tuple[str, ...] = ()
    reason: str = ""
    mechanism: str = ""


@dataclass(frozen=True)
class QueryWorkflowResult:
    """Application-layer response returned to the GUI."""

    state: ComponentState
    message: str
    iteration: int = 1
    complete: bool = False
    query_result: QueryResult | None = None
    candidates: tuple[QueryCandidate, ...] = field(default_factory=tuple)
    ambiguity: AmbiguityDecision | None = None
