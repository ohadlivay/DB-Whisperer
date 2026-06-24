"""Assemble discovered relationships into a schema graph and enumerate joins.

Component A discovers pairwise foreign-key :class:`Relationship` records. This
module turns that flat list into an undirected multigraph whose nodes are
tables and whose edges are the discovered joins, then enumerates the distinct
simple join paths between two tables.

Join-path multiplicity is the project's primary ambiguity mechanism: when more
than one distinct path connects the tables a question mentions, the same
request can be answered in materially different ways (the canonical
"labs for patient X" example: directly via ``subject_id`` versus through a
hospital visit). The enumeration here is pure and deterministic; the LLM-driven
entity extraction and clarification live in the ambiguity component.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from db_whisperer.contracts import JoinPath, Relationship, SchemaMetadata


DEFAULT_MAX_HOPS = 3
DEFAULT_MAX_PATHS = 64


def _edge_key(relationship: Relationship) -> tuple[str, str, str, str]:
    """A stable identity for one foreign-key edge, ignoring scores."""
    return (
        relationship.child_table,
        relationship.child_column,
        relationship.parent_table,
        relationship.parent_column,
    )


def _path_sort_key(path: JoinPath) -> tuple:
    """Order paths by length, then node chain, then edge identities."""
    return (
        len(path.relationships),
        path.tables,
        tuple(_edge_key(edge) for edge in path.relationships),
    )


@dataclass(frozen=True)
class JoinPathEnumeration:
    """All distinct join paths found between two tables, with completeness."""

    source: str
    target: str
    max_hops: int
    paths: tuple[JoinPath, ...]
    truncated: bool = False

    @property
    def is_ambiguous(self) -> bool:
        """True when more than one distinct join path connects the tables."""
        return len(self.paths) > 1


class SchemaGraph:
    """An undirected multigraph of tables linked by discovered foreign keys.

    Each :class:`Relationship` becomes one edge between its child and parent
    tables. Joins are bidirectional, so traversal is undirected, but every edge
    keeps the original directed relationship so a path can be described and the
    join keys recovered. Parallel edges (two foreign keys between the same pair
    of tables, or an ambiguous child column with several candidate parents) are
    preserved, because each produces a genuinely different join.
    """

    def __init__(
        self,
        tables: Iterable[str],
        relationships: Iterable[Relationship],
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        max_paths: int = DEFAULT_MAX_PATHS,
    ) -> None:
        if max_hops < 1:
            raise ValueError("max_hops must be at least 1.")
        if max_paths < 2:
            # At least two paths must be retainable; otherwise a truncated
            # enumeration could hide a second path and silently under-report
            # ambiguity (which needs two paths to compare).
            raise ValueError("max_paths must be at least 2.")
        self.max_hops = max_hops
        self.max_paths = max_paths

        # Preserve declaration order while dropping duplicate table names.
        self._tables: tuple[str, ...] = tuple(dict.fromkeys(tables))
        self._table_set = frozenset(self._tables)
        self._adjacency: dict[str, list[tuple[str, Relationship]]] = {
            table: [] for table in self._tables
        }

        edges: list[Relationship] = []
        for relationship in relationships:
            if (
                relationship.child_table not in self._table_set
                or relationship.parent_table not in self._table_set
            ):
                # An edge to an unknown table cannot be joined; skip it rather
                # than silently inventing a node.
                continue
            edges.append(relationship)
            self._adjacency[relationship.child_table].append(
                (relationship.parent_table, relationship)
            )
            if relationship.parent_table != relationship.child_table:
                self._adjacency[relationship.parent_table].append(
                    (relationship.child_table, relationship)
                )

        # Deterministic neighbour order keeps path enumeration reproducible.
        for neighbours in self._adjacency.values():
            neighbours.sort(key=lambda item: (item[0], _edge_key(item[1])))
        self._edges: tuple[Relationship, ...] = tuple(edges)

    @classmethod
    def from_schema(
        cls,
        schema: SchemaMetadata,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        max_paths: int = DEFAULT_MAX_PATHS,
    ) -> SchemaGraph:
        """Build a graph from an ETL schema's tables and relationships."""
        return cls(
            schema.table_names,
            schema.relationships,
            max_hops=max_hops,
            max_paths=max_paths,
        )

    @property
    def tables(self) -> tuple[str, ...]:
        return self._tables

    @property
    def edges(self) -> tuple[Relationship, ...]:
        return self._edges

    def has_table(self, table: str) -> bool:
        return table in self._table_set

    def neighbors(self, table: str) -> tuple[tuple[str, Relationship], ...]:
        """Return ``(neighbour_table, edge)`` pairs adjacent to ``table``."""
        return tuple(self._adjacency.get(table, ()))

    def enumerate_join_paths(
        self,
        source: str,
        target: str,
        max_hops: int | None = None,
    ) -> JoinPathEnumeration:
        """Enumerate distinct simple join paths from ``source`` to ``target``.

        A path is *simple* (no repeated table) and bounded by ``max_hops``
        edges. Two paths are distinct when their ordered edge sequence differs,
        so parallel edges and alternative intermediates both count. The result
        is marked ``truncated`` if the per-pair path cap is reached, signalling
        that further paths may exist (visible incomplete state rather than a
        silent cut-off).
        """
        hop_limit = self.max_hops if max_hops is None else max_hops
        empty = JoinPathEnumeration(
            source=source,
            target=target,
            max_hops=hop_limit,
            paths=(),
            truncated=False,
        )
        if (
            hop_limit < 1
            or source == target
            or source not in self._table_set
            or target not in self._table_set
        ):
            return empty

        results: list[JoinPath] = []
        # Distinctness is by node chain plus join keys -- the same identity the
        # sort key and ``describe_join_path`` use. De-duplicating *during*
        # traversal (rather than after) means duplicate advisory relationships
        # (same join keys, differing score/overlap) cannot consume the path cap
        # and hide a genuinely distinct path behind a false truncation.
        seen: set[tuple] = set()
        truncated = False

        def visit(
            current: str,
            node_path: tuple[str, ...],
            edge_path: tuple[Relationship, ...],
            visited: frozenset[str],
        ) -> None:
            nonlocal truncated
            for neighbor, edge in self._adjacency.get(current, ()):
                if neighbor in visited:
                    continue
                next_edges = edge_path + (edge,)
                next_nodes = node_path + (neighbor,)
                if neighbor == target:
                    identity = (
                        next_nodes,
                        tuple(_edge_key(item) for item in next_edges),
                    )
                    if identity in seen:
                        continue
                    if len(seen) >= self.max_paths:
                        truncated = True
                        continue
                    seen.add(identity)
                    results.append(
                        JoinPath(tables=next_nodes, relationships=next_edges)
                    )
                    continue
                if len(next_edges) < hop_limit:
                    visit(
                        neighbor,
                        next_nodes,
                        next_edges,
                        visited | {neighbor},
                    )

        visit(source, (source,), (), frozenset((source,)))

        results.sort(key=_path_sort_key)
        return JoinPathEnumeration(
            source=source,
            target=target,
            max_hops=hop_limit,
            paths=tuple(results),
            truncated=truncated,
        )

    def has_ambiguous_pair(self) -> bool:
        """True if any table pair is connected by more than one join path.

        A cheap, deterministic pre-check: when no pair is ambiguous (a tree- or
        forest-shaped schema), callers can skip the expensive LLM entity step
        entirely because join-path multiplicity is impossible. Short-circuits on
        the first ambiguous pair found.
        """
        for first_index in range(len(self._tables)):
            for second_index in range(first_index + 1, len(self._tables)):
                enumeration = self.enumerate_join_paths(
                    self._tables[first_index],
                    self._tables[second_index],
                )
                if enumeration.is_ambiguous:
                    return True
        return False

    def adjacency_summary(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return each table with its distinct neighbour tables, for display."""
        summary: list[tuple[str, tuple[str, ...]]] = []
        for table in self._tables:
            neighbours = tuple(
                dict.fromkeys(
                    neighbor for neighbor, _ in self._adjacency.get(table, ())
                )
            )
            summary.append((table, neighbours))
        return tuple(summary)


def describe_join_path(
    path: JoinPath,
    transform: Callable[[str], str] | None = None,
) -> str:
    """Render a join path as a table chain plus its join conditions.

    Example: ``patients -> admissions -> labevents
    [admissions.subject_id = patients.subject_id;
    labevents.hadm_id = admissions.hadm_id]``.

    ``transform`` is applied to every table and column identifier. Callers that
    embed the result in an LLM prompt pass a sanitizer so an untrusted CSV
    column name cannot forge a prompt delimiter; the default leaves identifiers
    untouched for display and logging.
    """
    render = transform or (lambda value: value)
    chain = " -> ".join(render(table) for table in path.tables)
    if not path.relationships:
        return chain
    conditions = "; ".join(
        f"{render(edge.child_table)}.{render(edge.child_column)} = "
        f"{render(edge.parent_table)}.{render(edge.parent_column)}"
        for edge in path.relationships
    )
    return f"{chain} [{conditions}]"


def entity_table_pairs(
    tables: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Return ordered, de-duplicated unordered pairs of distinct tables."""
    distinct = tuple(dict.fromkeys(tables))
    pairs: list[tuple[str, str]] = []
    for first_index in range(len(distinct)):
        for second_index in range(first_index + 1, len(distinct)):
            pairs.append((distinct[first_index], distinct[second_index]))
    return tuple(pairs)
