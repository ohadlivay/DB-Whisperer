"""Logical SQL analysis for grounding and join-efficiency scoring."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp


@dataclass(frozen=True)
class SQLAnalysis:
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    aliases: tuple[str, ...]
    join_count: int


def analyze_sql(sql: str) -> SQLAnalysis:
    tree = sqlglot.parse_one(sql, read="duckdb")
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = tuple(
        dict.fromkeys(
            table.name.lower()
            for table in tree.find_all(exp.Table)
            if table.name and table.name.lower() not in cte_names
        )
    )
    columns = tuple(dict.fromkeys(column.name.lower() for column in tree.find_all(exp.Column) if column.name))
    aliases = tuple(dict.fromkeys(alias.alias.lower() for alias in tree.find_all(exp.Alias) if alias.alias))
    joins = sum(1 for _ in tree.find_all(exp.Join))
    return SQLAnalysis(tables=tables, columns=columns, aliases=aliases, join_count=joins)

