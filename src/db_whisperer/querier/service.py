"""Natural-language to DuckDB SQL generation and execution."""

from __future__ import annotations

import json

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
from db_whisperer.querier.schema_linker import (
    SchemaLinker,
    clarification_required_tables,
)
from db_whisperer.querier.sql_validator import (
    SQLValidationError,
    validate_read_only_sql,
)


class QueryService:
    """Build prompts, generate SQL, validate it, and query DuckDB."""

    MAX_VALIDATION_RETRIES = 1

    def __init__(
        self,
        client: OpenRouterClient | None = None,
        prompt_builder: PromptBuilder | None = None,
        max_result_rows: int = 1000,
        rag_threshold: int = 5,
        schema_linker: SchemaLinker | None = None,
    ) -> None:
        self.client = client or OpenRouterClient()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_result_rows = max_result_rows
        self.rag_threshold = rag_threshold
        self.schema_linker = schema_linker or SchemaLinker(client=self.client)

    def build_prompt(self, request: QueryRequest) -> str:
        """Expose prompt construction for evaluation and testing."""
        allowed_tables = None
        if len(request.schema.table_names) > self.rag_threshold:
            required_tables = clarification_required_tables(
                request.clarifications,
                request.schema,
            )
            allowed_tables = self.schema_linker.link_schema(
                user_prompt=request.prompt,
                schema=request.schema,
                api_key=request.api_key,
                model=request.model,
                required_tables=required_tables,
            )
        return self.prompt_builder.build(
            request.prompt,
            request.schema,
            request.clarifications,
            allowed_tables=allowed_tables,
            compliance_retry=request.compliance_retry,
        )

    def generate_candidate(self, request: QueryRequest) -> QueryCandidate:
        """Generate and validate one SQL candidate."""
        try:
            prompt = self.build_prompt(request)
        except (
            duckdb.Error,
            OSError,
            ValueError,
        ) as error:
            return QueryCandidate(
                attempt_number=request.attempt_number,
                state=ComponentState.FAILED,
                message=str(error),
            )

        generation_prompt = prompt
        sql: str | None = None
        for validation_retry in range(self.MAX_VALIDATION_RETRIES + 1):
            try:
                sql = self.client.generate_sql(
                    prompt=generation_prompt,
                    api_key=request.api_key,
                    model=request.model,
                    metadata={
                        "attempt_number": request.attempt_number,
                        "validation_retry": validation_retry,
                    },
                )
            except OpenRouterError as error:
                return QueryCandidate(
                    attempt_number=request.attempt_number,
                    state=ComponentState.FAILED,
                    sql=sql,
                    message=str(error),
                )

            try:
                validated_sql = validate_read_only_sql(sql)
            except SQLValidationError as error:
                can_retry = str(error).startswith(
                    "Generated SQL is invalid:"
                )
                if (
                    not can_retry
                    or validation_retry == self.MAX_VALIDATION_RETRIES
                ):
                    return QueryCandidate(
                        attempt_number=request.attempt_number,
                        state=ComponentState.FAILED,
                        sql=sql,
                        message=str(error),
                    )
                generation_prompt = self._validation_repair_prompt(
                    prompt,
                    sql,
                    error,
                )
                continue

            return QueryCandidate(
                attempt_number=request.attempt_number,
                state=ComponentState.ACCEPTED,
                sql=validated_sql,
                message="SQL generated.",
            )

        raise AssertionError("SQL validation retry loop exited unexpectedly.")

    @staticmethod
    def _validation_repair_prompt(
        original_prompt: str,
        invalid_sql: str,
        error: SQLValidationError,
    ) -> str:
        """Ask the model to repair one rejected SQL response."""
        feedback = (
            "VALIDATION RETRY\n"
            "The previous SQL response was rejected by DuckDB. Correct only "
            "the SQL syntax or identifiers needed to resolve the error, while "
            "preserving the user's requested meaning.\n"
            f"Previous SQL: {json.dumps(invalid_sql, ensure_ascii=True)}\n"
            f"DuckDB error: {error}\n"
            "Return exactly one JSON object with a complete SQL statement. "
            "Close every quoted identifier and string literal, and terminate "
            "the SQL statement with a semicolon."
        )
        return f"{original_prompt}\n\n{feedback}"

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
                failure_kind=(
                    "validator"
                    if candidate.message == "Generated SQL contains a forbidden operation."
                    else "generation"
                ),
            )
        if not database_path:
            return QueryResult(
                state=ComponentState.FAILED,
                message="Upload a CSV file before querying.",
                failure_kind="missing_schema",
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
                failure_kind=(
                    "schema_resolution"
                    if isinstance(error, (duckdb.BinderException, duckdb.CatalogException))
                    else "execution"
                ),
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
