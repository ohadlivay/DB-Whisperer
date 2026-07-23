"""Parsed SQL evidence used by Evaluation V3 scoring."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp


@dataclass(frozen=True)
class SQLAnalysis:
    """Deterministic logical features of one DuckDB query."""

    tables: tuple[str, ...]
    columns: tuple[str, ...]
    aliases: tuple[str, ...]
    join_count: int
    has_order: bool
    limit: int | None


def _first_seen(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))  # type: ignore[arg-type]


def _outer_limit(tree: exp.Expression) -> int | None:
    limit = tree.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    expression = limit.expression
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return None
    try:
        return int(expression.this)
    except (TypeError, ValueError):
        return None


def _has_cte_ancestor(expression: exp.Expression) -> bool:
    parent = expression.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return True
        parent = parent.parent
    return False


def analyze_sql(sql: str) -> SQLAnalysis:
    """Parse SQL once and return logical joins plus grounding identifiers.

    CTE names are derived relations, so they are excluded from physical table
    evidence. Joins in every nested scope count, while ordering and limiting
    describe only the outer result returned to the evaluator.
    """

    tree = sqlglot.parse_one(sql, read="duckdb")
    ctes = tuple(tree.find_all(exp.CTE))
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in ctes
        if cte.alias_or_name
    }
    table_nodes = (
        *(table for cte in ctes for table in cte.this.find_all(exp.Table)),
        *(
            table
            for table in tree.find_all(exp.Table)
            if not _has_cte_ancestor(table)
        ),
    )
    tables = _first_seen(
        table.name.casefold()
        for table in table_nodes
        if table.name and table.name.casefold() not in cte_names
    )
    aliases = _first_seen(
        alias.alias.casefold()
        for alias in tree.find_all(exp.Alias)
        if alias.alias
    )
    columns = _first_seen(
        column.name.casefold()
        for column in tree.find_all(exp.Column)
        if column.name and column.name.casefold() not in aliases
    )
    return SQLAnalysis(
        tables=tables,
        columns=columns,
        aliases=aliases,
        join_count=sum(1 for _ in tree.find_all(exp.Join)),
        has_order=tree.args.get("order") is not None,
        limit=_outer_limit(tree),
    )
