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
    SchemaMetadata,
)


class ETLService:
    """Accept CSV files and expose ingestion results to the application."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        configured_path = database_path or os.getenv(
            "DB_WHISPERER_DATABASE_PATH",
            "data/generated/db_whisperer.duckdb",
        )
        self.database_path = Path(configured_path)

    def ingest(self, files: Sequence[CsvUpload]) -> IngestionResult:
        """Load one uploaded CSV into a persistent DuckDB database."""
        # HALF-BAKED FEATURE: this intentionally supports exactly one CSV and
        # performs no entity resolution or relationship discovery yet.
        if len(files) != 1:
            return self._failure("Upload exactly one CSV file.")

        upload = files[0]
        if not upload.content:
            return self._failure("The uploaded CSV file is empty.")

        table_name = self._table_name(upload.name)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = self._write_staging_file(upload)

        try:
            self._reset_database()
            connection = duckdb.connect(str(self.database_path))
            try:
                quoted_table = self._quote_identifier(table_name)
                connection.execute(
                    f"""
                    CREATE TABLE {quoted_table} AS
                    SELECT *
                    FROM read_csv_auto(?, header = true)
                    """,
                    [str(staging_path)],
                )
                description = connection.execute(
                    f"DESCRIBE {quoted_table}"
                ).fetchall()
                row_count = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_table}"
                ).fetchone()[0]
            finally:
                connection.close()
        except (duckdb.Error, OSError) as error:
            self._reset_database()
            return self._failure(f"CSV ingestion failed: {error}")
        finally:
            staging_path.unlink(missing_ok=True)

        columns = tuple(
            ColumnMetadata(name=row[0], data_type=row[1])
            for row in description
        )
        resolved_path = str(self.database_path.resolve())
        return IngestionResult(
            state=ComponentState.ACCEPTED,
            message=f"Loaded {upload.name} into DuckDB.",
            schema=SchemaMetadata(
                database_path=resolved_path,
                source_names=(upload.name,),
                table_names=(table_name,),
                columns=columns,
                row_count=row_count,
            ),
        )

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
