"""Natural-language to DuckDB SQL generation and execution."""

from __future__ import annotations

import duckdb

from db_whisperer.contracts import (
    ComponentState,
    QueryCandidate,
    QueryRequest,
    QueryResult,
)
from db_whisperer.querier.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
)
from db_whisperer.querier.prompt_builder import PromptBuilder
from db_whisperer.querier.sql_validator import (
    SQLValidationError,
    validate_read_only_sql,
)


class QueryService:
    """Build prompts, generate SQL, validate it, and query DuckDB."""

    def __init__(
        self,
        client: OpenRouterClient | None = None,
        prompt_builder: PromptBuilder | None = None,
        max_result_rows: int = 1000,
    ) -> None:
        self.client = client or OpenRouterClient()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_result_rows = max_result_rows

    def build_prompt(self, request: QueryRequest) -> str:
        """Expose prompt construction for evaluation and testing."""
        return self.prompt_builder.build(
            request.prompt,
            request.schema,
            request.clarifications,
        )

    def generate_candidate(self, request: QueryRequest) -> QueryCandidate:
        """Generate and validate one SQL candidate."""
        try:
            prompt = self.build_prompt(request)
            sql = self.client.generate_sql(
                prompt=prompt,
                api_key=request.api_key,
                model=request.model,
                metadata={"attempt_number": request.attempt_number},
            )
        except (
            duckdb.Error,
            OSError,
            ValueError,
            OpenRouterError,
        ) as error:
            return QueryCandidate(
                attempt_number=request.attempt_number,
                state=ComponentState.FAILED,
                message=str(error),
            )

        try:
            validated_sql = validate_read_only_sql(sql)
        except SQLValidationError as error:
            return QueryCandidate(
                attempt_number=request.attempt_number,
                state=ComponentState.FAILED,
                sql=sql,
                message=str(error),
            )

        return QueryCandidate(
            attempt_number=request.attempt_number,
            state=ComponentState.ACCEPTED,
            sql=validated_sql,
            message="SQL generated.",
        )

    def execute_candidate(
        self,
        candidate: QueryCandidate,
        database_path: str | None,
    ) -> QueryResult:
        """Execute an accepted candidate against DuckDB in read-only mode."""
        if candidate.state != ComponentState.ACCEPTED or not candidate.sql:
            return QueryResult(
                state=ComponentState.FAILED,
                message=candidate.message or "No valid SQL was generated.",
            )
        if not database_path:
            return QueryResult(
                state=ComponentState.FAILED,
                message="Upload a CSV file before querying.",
            )

        try:
            sql = validate_read_only_sql(candidate.sql)
            connection = duckdb.connect(database_path, read_only=True)
            try:
                cursor = connection.execute(sql)
                columns = tuple(item[0] for item in cursor.description)
                fetched_rows = cursor.fetchmany(self.max_result_rows + 1)
            finally:
                connection.close()
        except (duckdb.Error, SQLValidationError, OSError) as error:
            return QueryResult(
                state=ComponentState.FAILED,
                message=f"Query execution failed: {error}",
                sql=candidate.sql,
            )

        truncated = len(fetched_rows) > self.max_result_rows
        rows = tuple(
            tuple(row)
            for row in fetched_rows[: self.max_result_rows]
        )
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message=f"Returned {len(rows)} row(s).",
            sql=sql,
            columns=columns,
            rows=rows,
            truncated=truncated,
        )

    def query(self, request: QueryRequest) -> QueryResult:
        """Generate one SQL statement and execute it directly."""
        candidate = self.generate_candidate(request)
        return self.execute_candidate(
            candidate,
            request.schema.database_path,
        )
