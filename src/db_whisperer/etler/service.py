"""ETL component boundary."""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import re
import tempfile

import duckdb

from db_whisperer.contracts import (
    ColumnMetadata,
    ComponentState,
    CsvUpload,
    IngestionResult,
    Relationship,
    SchemaMetadata,
    TableSchema,
)


NUMERIC_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
)
TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")


class ETLService:
    """Accept CSV files and load them into a single DuckDB database.

    Multiple related CSV files are loaded as multiple tables and their
    foreign-key relationships are discovered from naming and data overlap.
    """

    _MAX_KEY_SCAN_ROWS = 5_000_000
    _MAX_OVERLAP_SAMPLE = 100_000
    _OVERLAP_THRESHOLD = 0.95
    _MIN_SAFE_DISTINCT = 8
    _AMBIGUITY_EPSILON = 0.05
    _GENERIC_KEY_EXCLUSIONS = frozenset({"row_id"})

    def __init__(self, database_path: str | Path | None = None) -> None:
        configured_path = database_path or os.getenv(
            "DB_WHISPERER_DATABASE_PATH",
            "data/generated/db_whisperer.duckdb",
        )
        self.database_path = Path(configured_path)

    def ingest(self, files: Sequence[CsvUpload]) -> IngestionResult:
        """Load one or more uploaded CSV files into one DuckDB database."""
        if not files:
            return self._failure("Upload at least one CSV file.")
        if any(not upload.content for upload in files):
            return self._failure("One or more uploaded CSV files are empty.")

        table_names = self._unique_table_names(files)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        notes: list[str] = []
        load_complete = True
        try:
            self._reset_database()
            connection = duckdb.connect(str(self.database_path))
            try:
                for upload, table_name in zip(files, table_names, strict=True):
                    staging_path = self._write_staging_file(upload)
                    try:
                        load_complete &= self._load_table(
                            connection, table_name, staging_path, notes
                        )
                    finally:
                        staging_path.unlink(missing_ok=True)
                tables, key_scan_complete = self._collect_tables(
                    connection, table_names, notes
                )
                relationships, overlap_complete = self._discover_relationships(
                    connection, tables, notes
                )
            finally:
                connection.close()
        except (duckdb.Error, OSError) as error:
            self._reset_database()
            return self._failure(f"CSV ingestion failed: {error}")

        columns = tuple(
            column for table in tables for column in table.columns
        )
        row_count = tables[0].row_count if len(tables) == 1 else None
        resolved_path = str(self.database_path.resolve())
        return IngestionResult(
            state=ComponentState.ACCEPTED,
            message=self._success_message(files, tables),
            schema=SchemaMetadata(
                database_path=resolved_path,
                source_names=tuple(upload.name for upload in files),
                table_names=tuple(table.table_name for table in tables),
                columns=columns,
                row_count=row_count,
                tables=tables,
                relationships=relationships,
                discovery_complete=(
                    key_scan_complete and overlap_complete and load_complete
                ),
                discovery_notes=tuple(notes),
            ),
        )

    # -- Loading ---------------------------------------------------------

    def _load_table(
        self,
        connection: duckdb.DuckDBPyConnection,
        table_name: str,
        staging_path: Path,
        notes: list[str],
    ) -> bool:
        """Create one table from a CSV, escalating through fallbacks.

        Returns ``True`` when every row loaded cleanly, ``False`` when
        malformed rows had to be skipped (which makes discovery incomplete).

        The literal text ``NULL`` is treated as a null token so that columns
        using it (a common CSV convention, e.g. BikeStores) infer clean
        numeric types and join correctly instead of carrying a stray string.
        """
        quoted_table = self._quote_identifier(table_name)
        path = str(staging_path)
        base_options = "header = true, nullstr = ['NULL', '']"

        try:
            self._create_from_csv(connection, quoted_table, path, base_options)
            return True
        except duckdb.Error:
            pass

        # Heterogeneous columns (mixed numeric/text) break type inference.
        # Reload everything as VARCHAR so the table is still queryable;
        # downstream overlap checks cast both sides to a common form.
        try:
            self._create_from_csv(
                connection,
                quoted_table,
                path,
                f"{base_options}, all_varchar = true",
            )
            return True
        except duckdb.Error:
            pass

        # Last resort: skip structurally malformed rows so one bad line does
        # not block the whole dataset. This loses rows, so flag it.
        self._create_from_csv(
            connection,
            quoted_table,
            path,
            f"{base_options}, all_varchar = true, ignore_errors = true",
        )
        notes.append(
            f"Skipped malformed rows while loading table '{table_name}'."
        )
        return False

    @staticmethod
    def _create_from_csv(
        connection: duckdb.DuckDBPyConnection,
        quoted_table: str,
        path: str,
        options: str,
    ) -> None:
        connection.execute(f"DROP TABLE IF EXISTS {quoted_table}")
        connection.execute(
            f"""
            CREATE TABLE {quoted_table} AS
            SELECT *
            FROM read_csv_auto(?, {options})
            """,
            [path],
        )

    # -- Schema collection ----------------------------------------------

    def _collect_tables(
        self,
        connection: duckdb.DuckDBPyConnection,
        table_names: Sequence[str],
        notes: list[str],
    ) -> tuple[tuple[TableSchema, ...], bool]:
        """Describe every loaded table and detect its key columns."""
        tables: list[TableSchema] = []
        complete = True
        for table_name in table_names:
            quoted_table = self._quote_identifier(table_name)
            description = connection.execute(
                f"DESCRIBE {quoted_table}"
            ).fetchall()
            columns = tuple(
                ColumnMetadata(
                    name=row[0],
                    data_type=row[1],
                    table_name=table_name,
                )
                for row in description
            )
            row_count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_table}"
            ).fetchone()[0]

            if row_count > self._MAX_KEY_SCAN_ROWS:
                notes.append(
                    f"Skipped key detection for large table "
                    f"'{table_name}' ({row_count} rows)."
                )
                complete = False
                key_columns: tuple[str, ...] = ()
            else:
                key_columns = self._detect_key_columns(
                    connection, table_name, columns, row_count
                )

            id_key_columns = tuple(
                column.name
                for column in columns
                if column.name in key_columns
                and self._is_id_like(column.name, table_name)
            )
            primary_key = self._choose_primary_key(
                table_name, columns, key_columns
            )
            tables.append(
                TableSchema(
                    table_name=table_name,
                    columns=columns,
                    row_count=row_count,
                    key_columns=key_columns,
                    id_key_columns=id_key_columns,
                    primary_key=primary_key,
                )
            )
        return tuple(tables), complete

    def _detect_key_columns(
        self,
        connection: duckdb.DuckDBPyConnection,
        table_name: str,
        columns: tuple[ColumnMetadata, ...],
        row_count: int,
    ) -> tuple[str, ...]:
        """Return columns that are fully distinct and non-null (key-like)."""
        if row_count == 0:
            return ()
        quoted_table = self._quote_identifier(table_name)
        keys: list[str] = []
        for column in columns:
            if self._normalize(column.name) in self._GENERIC_KEY_EXCLUSIONS:
                continue
            quoted_column = self._quote_identifier(column.name)
            null_count, distinct_count = connection.execute(
                f"""
                SELECT
                    COUNT(*) - COUNT({quoted_column}),
                    COUNT(DISTINCT {quoted_column})
                FROM {quoted_table}
                """
            ).fetchone()
            if null_count == 0 and distinct_count == row_count:
                keys.append(column.name)
        return tuple(keys)

    def _choose_primary_key(
        self,
        table_name: str,
        columns: tuple[ColumnMetadata, ...],
        key_columns: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Pick the most likely single primary key from the key columns."""
        if not key_columns:
            return ()
        table_norm = self._normalize(table_name)
        singular = self._singularize(table_norm)
        preferred = {f"{table_norm}_id", f"{singular}_id"}
        for name in key_columns:
            if self._normalize(name) in preferred:
                return (name,)
        for name in key_columns:
            normalized = self._normalize(name)
            if normalized.endswith("_id") or normalized == "id":
                return (name,)
        first_column = columns[0].name
        if first_column in key_columns:
            return (first_column,)
        return (key_columns[0],)

    # -- Relationship discovery -----------------------------------------

    def _discover_relationships(
        self,
        connection: duckdb.DuckDBPyConnection,
        tables: tuple[TableSchema, ...],
        notes: list[str],
    ) -> tuple[tuple[Relationship, ...], bool]:
        """Discover foreign-key relationships from naming and value overlap.

        A child column ``<x>_id`` is matched to a parent key column when its
        non-null values are (almost) a subset of the parent key's values. The
        parent is chosen by a composite score over naming, same-name, primary
        key, self-reference, overlap, and domain-fit signals; near-ties are
        emitted as ambiguous alternatives instead of a silent guess.
        """
        profile_cache: dict[tuple[str, str], dict] = {}

        def profile(table: TableSchema, column_name: str) -> dict:
            key = (table.table_name, column_name)
            if key not in profile_cache:
                column = self._column(table, column_name)
                profile_cache[key] = self._profile_column(
                    connection, table.table_name, column
                )
            return profile_cache[key]

        # Catalog of every (parent table, key column) usable as a join target.
        parent_keys = [
            (parent, key_name)
            for parent in tables
            for key_name in parent.key_columns
        ]

        overlap_complete = True
        relationships: set[Relationship] = set()

        for child in tables:
            for column in child.columns:
                base = self._fk_base(column.name)
                if base is None or base in {"row", ""}:
                    continue
                if column.name in child.key_columns:
                    continue

                child_profile = profile(child, column.name)
                child_distinct = child_profile["distinct"]
                if child_distinct < 2:
                    continue
                sampled = child_distinct > self._MAX_OVERLAP_SAMPLE
                if sampled:
                    overlap_complete = False
                    notes.append(
                        f"Sampled overlap for large column "
                        f"'{child.table_name}.{column.name}' "
                        f"({child_distinct} distinct values)."
                    )

                survivors = self._rank_parents(
                    connection,
                    child,
                    column,
                    base,
                    child_profile,
                    parent_keys,
                    profile,
                )
                relationships.update(
                    self._emit_relationships(
                        child, column, survivors, sampled
                    )
                )

        ordered = tuple(
            sorted(
                relationships,
                key=lambda r: (
                    r.child_table,
                    r.child_column,
                    r.parent_table,
                    r.parent_column,
                ),
            )
        )
        return ordered, overlap_complete

    def _rank_parents(
        self,
        connection: duckdb.DuckDBPyConnection,
        child: TableSchema,
        column: ColumnMetadata,
        base: str,
        child_profile: dict,
        parent_keys: list[tuple[TableSchema, str]],
        profile,
    ) -> list[dict]:
        """Score every candidate parent key that passes the overlap gate."""
        child_distinct = child_profile["distinct"]
        survivors: list[dict] = []
        for parent, parent_column_name in parent_keys:
            is_self = parent.table_name == child.table_name
            if is_self and parent_column_name == column.name:
                continue

            parent_column = self._column(parent, parent_column_name)
            if not self._types_comparable(
                column.data_type, parent_column.data_type
            ):
                continue

            parent_profile = profile(parent, parent_column_name)
            if self._ranges_disjoint(child_profile, parent_profile):
                continue

            overlap = self._compute_overlap(
                connection,
                child.table_name,
                column.name,
                parent.table_name,
                parent_column_name,
            )
            if overlap < self._OVERLAP_THRESHOLD:
                continue

            name_match = self._name_match(base, parent.table_name)
            same_name_key = self._normalize(column.name) == self._normalize(
                parent_column_name
            )
            strong_signal = name_match or same_name_key or is_self
            if child_distinct < self._MIN_SAFE_DISTINCT and not strong_signal:
                continue

            parent_distinct = max(parent_profile["distinct"], 1)
            domain_fit = min(1.0, child_distinct / parent_distinct)
            parent_key_is_pk = parent_column_name in parent.primary_key
            non_id_parent = parent_column_name not in parent.id_key_columns

            score = (
                3.0 * name_match
                + 3.0 * same_name_key
                + 1.0 * parent_key_is_pk
                + 2.5 * is_self
                + 1.0 * overlap
                + 1.0 * domain_fit
                - 0.5 * (1.0 - domain_fit)
                - 2.5 * non_id_parent
            )
            survivors.append(
                {
                    "parent_table": parent.table_name,
                    "parent_column": parent_column_name,
                    "overlap": overlap,
                    "score": score,
                    "is_self": is_self,
                }
            )
        return survivors

    def _emit_relationships(
        self,
        child: TableSchema,
        column: ColumnMetadata,
        survivors: list[dict],
        sampled: bool,
    ) -> list[Relationship]:
        """Pick the best parent, or emit near-ties as ambiguous alternatives."""
        if not survivors:
            return []
        survivors.sort(key=lambda s: s["score"], reverse=True)
        best_score = survivors[0]["score"]
        chosen = [
            survivor
            for survivor in survivors
            if best_score - survivor["score"] <= self._AMBIGUITY_EPSILON
        ]
        ambiguous = len(chosen) > 1
        return [
            Relationship(
                child_table=child.table_name,
                child_column=column.name,
                parent_table=survivor["parent_table"],
                parent_column=survivor["parent_column"],
                overlap=round(survivor["overlap"], 4),
                score=round(survivor["score"], 4),
                cardinality=(
                    "self-reference" if survivor["is_self"] else "many-to-one"
                ),
                ambiguous=ambiguous,
                sampled=sampled,
            )
            for survivor in chosen
        ]

    def _profile_column(
        self,
        connection: duckdb.DuckDBPyConnection,
        table_name: str,
        column: ColumnMetadata,
    ) -> dict:
        """Collect distinct count and numeric bounds for one column."""
        quoted_table = self._quote_identifier(table_name)
        quoted_column = self._quote_identifier(column.name)
        numeric = self._is_numeric_type(column.data_type)
        if numeric:
            distinct, minimum, maximum = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT {quoted_column}),
                    MIN({quoted_column}),
                    MAX({quoted_column})
                FROM {quoted_table}
                """
            ).fetchone()
        else:
            distinct = connection.execute(
                f"SELECT COUNT(DISTINCT {quoted_column}) FROM {quoted_table}"
            ).fetchone()[0]
            minimum = maximum = None
        return {
            "distinct": distinct or 0,
            "numeric": numeric,
            "min": minimum,
            "max": maximum,
        }

    def _compute_overlap(
        self,
        connection: duckdb.DuckDBPyConnection,
        child_table: str,
        child_column: str,
        parent_table: str,
        parent_column: str,
    ) -> float:
        """Fraction of distinct non-null child values present in the parent.

        Both sides are cast to a canonical string form so a column reloaded as
        VARCHAR still compares against an inferred numeric column. A capped,
        hash-ordered sample keeps the check deterministic for huge columns.
        """
        quoted_child_table = self._quote_identifier(child_table)
        quoted_child_column = self._quote_identifier(child_column)
        quoted_parent_table = self._quote_identifier(parent_table)
        quoted_parent_column = self._quote_identifier(parent_column)
        total, matched = connection.execute(
            f"""
            WITH child_keys AS (
                SELECT DISTINCT TRIM(CAST({quoted_child_column} AS VARCHAR)) AS k
                FROM {quoted_child_table}
                WHERE {quoted_child_column} IS NOT NULL
                ORDER BY hash(TRIM(CAST({quoted_child_column} AS VARCHAR)))
                LIMIT {self._MAX_OVERLAP_SAMPLE}
            )
            SELECT
                COUNT(*),
                COUNT(*) FILTER (
                    WHERE k IN (
                        SELECT TRIM(CAST({quoted_parent_column} AS VARCHAR))
                        FROM {quoted_parent_table}
                    )
                )
            FROM child_keys
            """
        ).fetchone()
        if not total:
            return 0.0
        return matched / total

    @classmethod
    def _ranges_disjoint(cls, child_profile: dict, parent_profile: dict) -> bool:
        """True only when numeric ranges cannot possibly overlap at all."""
        if not (child_profile["numeric"] and parent_profile["numeric"]):
            return False
        if any(
            value is None
            for value in (
                child_profile["min"],
                child_profile["max"],
                parent_profile["min"],
                parent_profile["max"],
            )
        ):
            return False
        return (
            child_profile["max"] < parent_profile["min"]
            or child_profile["min"] > parent_profile["max"]
        )

    @classmethod
    def _name_match(cls, base: str, parent_table: str) -> bool:
        """True when the FK base name matches the parent table name."""
        parent_norm = cls._normalize(parent_table)
        parent_singular = cls._singularize(parent_norm)
        return base in {parent_norm, parent_singular} or parent_norm in {
            base,
            f"{base}s",
        }

    @staticmethod
    def _is_numeric_type(data_type: str) -> bool:
        return data_type.upper().startswith(NUMERIC_TYPES)

    @staticmethod
    def _types_comparable(child_type: str, parent_type: str) -> bool:
        """Allow numeric/VARCHAR mixes; reject temporal, boolean, and blob."""
        for data_type in (child_type, parent_type):
            upper = data_type.upper()
            if (
                upper.startswith(TEMPORAL_TYPES)
                or upper.startswith("BOOL")
                or upper.startswith("BLOB")
            ):
                return False
        return True

    @staticmethod
    def _column(table: TableSchema, column_name: str) -> ColumnMetadata:
        for column in table.columns:
            if column.name == column_name:
                return column
        raise KeyError(f"Column {column_name!r} not found in {table.table_name!r}.")

    @classmethod
    def _fk_base(cls, column_name: str) -> str | None:
        """Strip an id suffix to get the role base, or None if not id-like."""
        normalized = cls._normalize(column_name)
        if normalized.endswith("_id"):
            return normalized[:-3]
        if normalized.endswith("id") and len(normalized) > 2:
            return normalized[:-2]
        return None

    # -- Helpers ---------------------------------------------------------

    def _unique_table_names(self, files: Sequence[CsvUpload]) -> list[str]:
        """Derive a unique table name per file, suffixing collisions."""
        counts: dict[str, int] = {}
        names: list[str] = []
        for upload in files:
            base = self._table_name(upload.name)
            counts[base] = counts.get(base, 0) + 1
            names.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
        return names

    @staticmethod
    def _success_message(
        files: Sequence[CsvUpload],
        tables: tuple[TableSchema, ...],
    ) -> str:
        if len(tables) == 1:
            return f"Loaded {files[0].name} into DuckDB."
        return f"Loaded {len(tables)} tables into DuckDB."

    def _failure(self, message: str) -> IngestionResult:
        return IngestionResult(
            state=ComponentState.FAILED,
            message=message,
            schema=SchemaMetadata(),
        )

    def _reset_database(self) -> None:
        self.database_path.unlink(missing_ok=True)
        Path(f"{self.database_path}.wal").unlink(missing_ok=True)

    def _write_staging_file(self, upload: CsvUpload) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".csv",
            prefix="db_whisperer_",
            dir=self.database_path.parent,
            delete=False,
        ) as staging_file:
            staging_file.write(upload.content)
            return Path(staging_file.name)

    @staticmethod
    def _table_name(filename: str) -> str:
        stem = Path(filename).stem
        normalized = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").lower()
        if not normalized:
            normalized = "uploaded_data"
        if normalized[0].isdigit():
            normalized = f"table_{normalized}"
        return normalized

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return f'"{identifier.replace(chr(34), chr(34) * 2)}"'

    @staticmethod
    def _normalize(identifier: str) -> str:
        return identifier.strip().lower()

    @classmethod
    def _is_id_like(cls, column_name: str, table_name: str) -> bool:
        normalized = cls._normalize(column_name)
        if normalized == "id" or normalized.endswith("_id") or normalized.endswith("id"):
            return True
        table_norm = cls._normalize(table_name)
        return normalized in {
            f"{table_norm}_id",
            f"{cls._singularize(table_norm)}_id",
        }

    @staticmethod
    def _singularize(name: str) -> str:
        if name.endswith("ies") and len(name) > 3:
            return f"{name[:-3]}y"
        if name.endswith(("ses", "xes", "zes", "ches", "shes")):
            return name[:-2]
        if name.endswith("s") and not name.endswith("ss"):
            return name[:-1]
        return name
