"""Schema graph assembly and join-path enumeration.

Builds an undirected multigraph from the foreign-key relationships discovered
by Component A and enumerates the distinct join paths between tables. This is
the deterministic foundation of the join-path ambiguity mechanism.
"""

from db_whisperer.schema_graph.graph import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_PATHS,
    JoinPathEnumeration,
    SchemaGraph,
    describe_join_path,
    entity_table_pairs,
)

__all__ = [
    "DEFAULT_MAX_HOPS",
    "DEFAULT_MAX_PATHS",
    "JoinPathEnumeration",
    "SchemaGraph",
    "describe_join_path",
    "entity_table_pairs",
]
