"""Validation for generated DuckDB SQL."""

from __future__ import annotations

import re

import duckdb


class SQLValidationError(ValueError):
    """Raised when generated SQL is not safe to execute."""


FORBIDDEN_SQL = re.compile(
    r"\b("
    r"ATTACH|COPY|CREATE|DELETE|DROP|EXPORT|IMPORT|INSERT|INSTALL|"
    r"LOAD|PRAGMA|REPLACE|TRUNCATE|UPDATE|"
    r"READ_CSV|READ_CSV_AUTO|READ_JSON|READ_JSON_AUTO|READ_PARQUET|"
    r"PARQUET_SCAN|CSV_SCAN|SQLITE_SCAN|POSTGRES_SCAN|HTTPFS"
    r")\b",
    flags=re.IGNORECASE,
)


def validate_read_only_sql(sql: str) -> str:
    """Require one SELECT statement and reject external-access functions."""
    normalized = sql.strip()
    if not normalized:
        raise SQLValidationError("Generated SQL is empty.")
    if FORBIDDEN_SQL.search(normalized):
        raise SQLValidationError("Generated SQL contains a forbidden operation.")

    try:
        statements = duckdb.extract_statements(normalized)
    except duckdb.Error as error:
        raise SQLValidationError(f"Generated SQL is invalid: {error}") from error

    if len(statements) != 1:
        raise SQLValidationError("Generated SQL must contain one statement.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SQLValidationError("Generated SQL must be a SELECT statement.")
    return normalized
