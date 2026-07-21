"""Neutral relationship connectivity used for schema-linker bridge tables."""

from __future__ import annotations

from collections import deque

from db_whisperer.contracts import SchemaMetadata


def shortest_table_connection(
    schema: SchemaMetadata,
    source: str,
    target: str,
) -> tuple[str, ...]:
    """Return one deterministic shortest table chain, or an empty tuple.

    This utility intentionally finds one connection only. It does not enumerate,
    count, or interpret alternative routes and is not an ambiguity mechanism.
    """
    if source == target:
        return (source,) if source in schema.table_names else ()
    known = set(schema.table_names)
    if source not in known or target not in known:
        return ()
    adjacency: dict[str, set[str]] = {table: set() for table in known}
    for relationship in schema.relationships:
        child, parent = relationship.child_table, relationship.parent_table
        if child in known and parent in known:
            adjacency[child].add(parent)
            adjacency[parent].add(child)

    queue = deque([(source, (source,))])
    visited = {source}
    while queue:
        current, chain = queue.popleft()
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor in visited:
                continue
            next_chain = (*chain, neighbor)
            if neighbor == target:
                return next_chain
            visited.add(neighbor)
            queue.append((neighbor, next_chain))
    return ()
