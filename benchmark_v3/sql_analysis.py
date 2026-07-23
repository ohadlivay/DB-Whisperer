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
    order_by: tuple[tuple[str, str], ...] = ()
    offset: int | None = None


def _first_seen(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))  # type: ignore[arg-type]


def _outer_integer(
    tree: exp.Expression,
    name: str,
    expected_type: type[exp.Expression],
) -> int | None:
    clause = tree.args.get(name)
    if not isinstance(clause, expected_type):
        return None
    expression = clause.args.get("expression")
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return None
    try:
        return int(expression.this)
    except (TypeError, ValueError):
        return None


def _normalized_expression(expression: exp.Expression) -> str:
    copied = expression.copy()
    for column in copied.find_all(exp.Column):
        column.set("catalog", None)
        column.set("db", None)
        column.set("table", None)
    return copied.sql(dialect="duckdb", normalize=True).casefold()


def _outer_order_by(tree: exp.Expression) -> tuple[tuple[str, str], ...]:
    order = tree.args.get("order")
    if not isinstance(order, exp.Order):
        return ()
    aliases = {
        expression.alias.casefold(): expression.this
        for expression in tree.expressions
        if isinstance(expression, exp.Alias) and expression.alias
    }
    normalized: list[tuple[str, str]] = []
    for ordered in order.expressions:
        expression = ordered.this
        if isinstance(expression, exp.Column) and not expression.table:
            expression = aliases.get(expression.name.casefold(), expression)
        normalized.append(
            (
                _normalized_expression(expression),
                "desc" if ordered.args.get("desc") else "asc",
            )
        )
    return tuple(normalized)


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
        limit=_outer_integer(tree, "limit", exp.Limit),
        order_by=_outer_order_by(tree),
        offset=_outer_integer(tree, "offset", exp.Offset),
    )
