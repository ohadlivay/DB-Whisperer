"""Application-layer orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from db_whisperer.contracts import (
    AmbiguityDecision,
    AmbiguityRequest,
    ComponentState,
    CsvUpload,
    ExecutedQueryPair,
    IngestionResult,
    JoinPathRequest,
    QueryCandidate,
    QueryRequest,
    QueryResult,
    QueryWorkflowResult,
    SchemaMetadata,
    SemanticColumnRequest,
)
from db_whisperer.ambiguity import (
    AmbiguityService,
    JoinPathAmbiguityService,
    SemanticColumnAmbiguityService,
)
from db_whisperer.etler import ETLService
from db_whisperer.prompt_logging import PromptLogger, PromptLogSink
from db_whisperer.querier import QueryService


class ApplicationService:
    """Coordinate GUI requests across the project components."""

    DEFAULT_CANDIDATES_PER_ITERATION = 5
    DEFAULT_MAX_ITERATIONS = 3
    DEFAULT_MAX_PARALLEL_CANDIDATES = 5

    def __init__(
        self,
        etler: ETLService | None = None,
        querier: QueryService | None = None,
        ambiguity: AmbiguityService | None = None,
        join_path: JoinPathAmbiguityService | None = None,
        semantic_column: SemanticColumnAmbiguityService | None = None,
        event_logger: PromptLogSink | None = None,
        candidates_per_iteration: int = DEFAULT_CANDIDATES_PER_ITERATION,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_parallel_candidates: int = DEFAULT_MAX_PARALLEL_CANDIDATES,
        enable_join_path_detection: bool = True,
        enable_semantic_column_detection: bool = True,
    ) -> None:
        if candidates_per_iteration < 2:
            raise ValueError("At least two candidates are required.")
        if max_iterations < 1:
            raise ValueError("At least one iteration is required.")
        if max_parallel_candidates < 1:
            raise ValueError("At least one parallel candidate is required.")

        self.etler = etler or ETLService()
        self.querier = querier or QueryService()
        self.ambiguity = ambiguity or AmbiguityService()
        self.join_path = join_path or JoinPathAmbiguityService()
        self.semantic_column = (
            semantic_column or SemanticColumnAmbiguityService()
        )
        self.event_logger = event_logger or PromptLogger()
        self.candidates_per_iteration = candidates_per_iteration
        self.max_iterations = max_iterations
        self.max_parallel_candidates = max_parallel_candidates
        self.enable_join_path_detection = enable_join_path_detection
        self.enable_semantic_column_detection = (
            enable_semantic_column_detection
        )

    def ingest_csvs(self, files: Sequence[CsvUpload]) -> IngestionResult:
        """Route an uploaded CSV file to Component A."""
        return self.etler.ingest(files)

    def preview_table(
        self,
        table_name: str,
        schema: SchemaMetadata,
        limit: int = 10,
    ) -> QueryResult:
        """Return a small read-only preview of one known table."""
        if table_name not in schema.table_names:
            return QueryResult(
                state=ComponentState.FAILED,
                message="The preview table is not part of the loaded schema.",
            )
        if limit < 1:
            return QueryResult(
                state=ComponentState.FAILED,
                message="The preview row limit must be positive.",
            )

        escaped_table = table_name.replace('"', '""')
        quoted_table = f'"{escaped_table}"'
        candidate = QueryCandidate(
            attempt_number=0,
            state=ComponentState.ACCEPTED,
            sql=f"SELECT * FROM {quoted_table} LIMIT {limit};",
            message="Preview query ready.",
        )
        return self.querier.execute_candidate(
            candidate,
            schema.database_path,
        )

    def submit_query(
        self,
        prompt: str,
        schema: SchemaMetadata | None,
        api_key: str,
        model: str,
        clarifications: tuple[str, ...] = (),
        iteration: int = 1,
        candidate_count: int | None = None,
    ) -> QueryWorkflowResult:
        """Run one candidate-generation and ambiguity-check iteration."""
        candidates_per_iteration = (
            self.candidates_per_iteration
            if candidate_count is None
            else candidate_count
        )
        if not prompt.strip():
            return self._failure("Enter a question.", iteration)
        if schema is None or not schema.database_path:
            return self._failure(
                "Upload a CSV file before querying.",
                iteration,
            )
        if not api_key.strip():
            return self._failure(
                "Enter an OpenRouter API key.",
                iteration,
            )
        if not model.strip():
            return self._failure(
                "Enter an OpenRouter model ID.",
                iteration,
            )
        if iteration < 1 or iteration > self.max_iterations:
            return self._failure(
                f"Iteration must be between 1 and {self.max_iterations}.",
                iteration,
            )
        if candidates_per_iteration < 2:
            return self._failure(
                "At least two SQL candidates are required.",
                iteration,
            )

        # Primary ambiguity mechanism: before generating any SQL, ask the
        # schema graph whether the mentioned entities are connected by more
        # than one join path. This runs on every non-terminal round of a
        # multi-table question (the detector skips pairs already settled by a
        # prior answer) and only when there is a graph to traverse, so
        # single-table datasets keep their previous behaviour.
        join_path_clarification = self._detect_join_path_ambiguity(
            prompt=prompt,
            schema=schema,
            api_key=api_key,
            model=model,
            iteration=iteration,
            clarifications=clarifications,
        )
        if join_path_clarification is not None:
            return join_path_clarification

        # Secondary ambiguity mechanism: after join-path multiplicity is ruled
        # out, ask whether a vague term in the question maps to more than one
        # column of the same semantic type (the PDF's "dates" -> admission vs
        # discharge vs date of birth). This runs for single-table datasets too,
        # which have no join graph to traverse.
        semantic_column_clarification = (
            self._detect_semantic_column_ambiguity(
                prompt=prompt,
                schema=schema,
                api_key=api_key,
                model=model,
                iteration=iteration,
                clarifications=clarifications,
            )
        )
        if semantic_column_clarification is not None:
            return semantic_column_clarification

        candidates: list[QueryCandidate] = []
        successful_results: list[QueryResult] = []
        pairs: list[ExecutedQueryPair] = []
        generation_failures: list[tuple[int, str]] = []
        execution_failures: list[tuple[int, str]] = []

        worker_count = min(
            candidates_per_iteration,
            self.max_parallel_candidates,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = tuple(
                executor.map(
                    lambda candidate_index: self._process_candidate(
                        candidate_index=candidate_index,
                        candidates_per_iteration=candidates_per_iteration,
                        iteration=iteration,
                        prompt=prompt,
                        schema=schema,
                        api_key=api_key,
                        model=model,
                        clarifications=clarifications,
                    ),
                    range(1, candidates_per_iteration + 1),
                )
            )

        for candidate_index, candidate, query_result in outcomes:
            candidates.append(candidate)
            if candidate.state != ComponentState.ACCEPTED:
                generation_failures.append(
                    (candidate_index, candidate.message)
                )
                self._log_candidate_event(
                    event="candidate_generation_failed",
                    model=model,
                    iteration=iteration,
                    candidate_index=candidate_index,
                    candidate=candidate,
                    error=candidate.message,
                )
                continue
            self._log_candidate_event(
                event="candidate_generated",
                model=model,
                iteration=iteration,
                candidate_index=candidate_index,
                candidate=candidate,
            )

            if (
                query_result is None
                or query_result.state != ComponentState.ACCEPTED
            ):
                execution_message = (
                    query_result.message
                    if query_result is not None
                    else "Candidate execution returned no result."
                )
                execution_failures.append(
                    (candidate_index, execution_message)
                )
                self._log_candidate_event(
                    event="candidate_execution_failed",
                    model=model,
                    iteration=iteration,
                    candidate_index=candidate_index,
                    candidate=candidate,
                    error=execution_message,
                )
                continue
            self._log_candidate_event(
                event="candidate_executed",
                model=model,
                iteration=iteration,
                candidate_index=candidate_index,
                candidate=candidate,
                row_count=len(query_result.rows),
                truncated=query_result.truncated,
            )

            successful_results.append(query_result)
            pairs.append(
                ExecutedQueryPair.from_query_result(
                    candidate_id=(
                        f"iteration-{iteration}-candidate-{candidate_index}"
                    ),
                    result=query_result,
                )
            )

        if not successful_results:
            return self._failure(
                self._candidate_failure_message(
                    candidates_per_iteration,
                    0,
                    generation_failures,
                    execution_failures,
                ),
                iteration,
                tuple(candidates),
            )

        last_result = successful_results[-1]
        if iteration == self.max_iterations:
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(candidates),
            )

        if len(pairs) < 2:
            return self._failure(
                self._candidate_failure_message(
                    candidates_per_iteration,
                    len(pairs),
                    generation_failures,
                    execution_failures,
                ),
                iteration,
                tuple(candidates),
            )

        try:
            ambiguity = self.ambiguity.evaluate(
                AmbiguityRequest(
                    user_query=prompt.strip(),
                    pairs=tuple(pairs),
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                )
            )
            if not isinstance(ambiguity, AmbiguityDecision):
                raise TypeError(
                    "Ambiguity judge returned an invalid decision."
                )
        except Exception as error:
            ambiguity = AmbiguityDecision(
                state=ComponentState.FAILED,
                reason=f"Ambiguity judgment failed: {error}",
            )

        valid_clarification = (
            ambiguity.state == ComponentState.ACCEPTED
            and ambiguity.passed is False
            and bool(ambiguity.question)
            and len(ambiguity.options) == 2
        )
        if (
            ambiguity.state != ComponentState.ACCEPTED
            or (
                ambiguity.passed is not True
                and not valid_clarification
            )
        ):
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(candidates),
                ambiguity=ambiguity,
            )
        if ambiguity.passed:
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(candidates),
                ambiguity=ambiguity,
            )
        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message=ambiguity.question or "Please clarify your request.",
            iteration=iteration,
            query_result=last_result,
            candidates=tuple(candidates),
            ambiguity=ambiguity,
        )

    def _detect_join_path_ambiguity(
        self,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        iteration: int,
        clarifications: tuple[str, ...],
    ) -> QueryWorkflowResult | None:
        """Run the schema-graph join-path gate, returning a pending result.

        Returns a ``PENDING`` workflow result when a join-path clarification is
        needed, or ``None`` when the gate is not applicable, passes, or fails
        (in which case the caller continues to normal candidate generation).
        """
        if (
            not self.enable_join_path_detection
            # A clarification consumes an iteration, so never gate on the final
            # allowed round -- it must return a result the user can act on. The
            # gate may still run on earlier clarification rounds so that a
            # multi-entity question can resolve each ambiguous join pair in
            # turn; the detector excludes pairs already settled by an answer.
            or iteration >= self.max_iterations
            or len(schema.table_names) < 2
            or not schema.relationships
        ):
            return None

        try:
            decision = self.join_path.detect(
                JoinPathRequest(
                    user_query=prompt.strip(),
                    schema=schema,
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                )
            )
            if not isinstance(decision, AmbiguityDecision):
                raise TypeError(
                    "Join-path detector returned an invalid decision."
                )
        except Exception as error:  # noqa: BLE001 - degrade gracefully.
            decision = AmbiguityDecision(
                state=ComponentState.FAILED,
                reason=f"Join-path detection failed: {error}",
                mechanism="join-path",
            )

        self.event_logger.log_event(
            event="join_path_detection",
            component="application",
            model=model,
            details={
                "iteration": iteration,
                "state": decision.state,
                "passed": decision.passed,
                "reason": decision.reason,
            },
        )

        is_clarification = (
            decision.state == ComponentState.ACCEPTED
            and decision.passed is False
            and bool(decision.question)
            and len(decision.options) == 2
        )
        if not is_clarification:
            return None

        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message=decision.question or "Please clarify your request.",
            iteration=iteration,
            complete=False,
            query_result=None,
            candidates=(),
            ambiguity=decision,
        )

    def _detect_semantic_column_ambiguity(
        self,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        iteration: int,
        clarifications: tuple[str, ...],
    ) -> QueryWorkflowResult | None:
        """Run the semantic-column gate, returning a pending result.

        Returns a ``PENDING`` workflow result when a column clarification is
        needed, or ``None`` when the gate is not applicable, passes, or fails
        (in which case the caller continues to normal candidate generation).
        """
        # The detector reads columns from schema.tables when the flat column
        # list is empty, so the guard must count them the same way or it would
        # skip detection on a tables-only schema the detector could analyze.
        total_columns = len(schema.columns) or sum(
            len(table.columns) for table in schema.tables
        )
        if (
            not self.enable_semantic_column_detection
            # A clarification consumes an iteration, so never gate on the final
            # allowed round -- it must return a result the user can act on.
            or iteration >= self.max_iterations
            # Need at least two columns for any same-type pair to exist.
            or total_columns < 2
        ):
            return None

        try:
            decision = self.semantic_column.detect(
                SemanticColumnRequest(
                    user_query=prompt.strip(),
                    schema=schema,
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                )
            )
            if not isinstance(decision, AmbiguityDecision):
                raise TypeError(
                    "Semantic-column detector returned an invalid decision."
                )
        except Exception as error:  # noqa: BLE001 - degrade gracefully.
            decision = AmbiguityDecision(
                state=ComponentState.FAILED,
                reason=f"Semantic-column detection failed: {error}",
                mechanism="semantic-column",
            )

        self.event_logger.log_event(
            event="semantic_column_detection",
            component="application",
            model=model,
            details={
                "iteration": iteration,
                "state": decision.state,
                "passed": decision.passed,
                "reason": decision.reason,
            },
        )

        is_clarification = (
            decision.state == ComponentState.ACCEPTED
            and decision.passed is False
            and bool(decision.question)
            and len(decision.options) == 2
        )
        if not is_clarification:
            return None

        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message=decision.question or "Please clarify your request.",
            iteration=iteration,
            complete=False,
            query_result=None,
            candidates=(),
            ambiguity=decision,
        )

    def _process_candidate(
        self,
        candidate_index: int,
        candidates_per_iteration: int,
        iteration: int,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        clarifications: tuple[str, ...],
    ) -> tuple[int, QueryCandidate, QueryResult | None]:
        """Generate and execute one candidate inside a worker thread."""
        attempt_number = (
            (iteration - 1) * candidates_per_iteration
            + candidate_index
        )
        try:
            candidate = self.querier.generate_candidate(
                QueryRequest(
                    prompt=prompt.strip(),
                    schema=schema,
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                    attempt_number=attempt_number,
                )
            )
            if candidate.state != ComponentState.ACCEPTED:
                return candidate_index, candidate, None

            result = self.querier.execute_candidate(
                candidate,
                schema.database_path,
            )
            return candidate_index, candidate, result
        except Exception as error:
            candidate = QueryCandidate(
                attempt_number=attempt_number,
                state=ComponentState.FAILED,
                message=f"Candidate processing failed: {error}",
            )
            return candidate_index, candidate, None

    def _log_candidate_event(
        self,
        event: str,
        model: str,
        iteration: int,
        candidate_index: int,
        candidate: QueryCandidate,
        error: str | None = None,
        row_count: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        details = {
            "iteration": iteration,
            "candidate_index": candidate_index,
            "attempt_number": candidate.attempt_number,
            "sql": candidate.sql,
        }
        if error:
            details["error"] = error
        if row_count is not None:
            details["row_count"] = row_count
        if truncated is not None:
            details["truncated"] = truncated
        self.event_logger.log_event(
            event=event,
            component="application",
            model=model,
            details=details,
        )

    @staticmethod
    def _candidate_failure_message(
        requested: int,
        successful: int,
        generation_failures: list[tuple[int, str]],
        execution_failures: list[tuple[int, str]],
    ) -> str:
        summary = (
            f"Only {successful} of {requested} candidate queries executed "
            f"successfully ("
            f"{ApplicationService._failure_count_text(
                len(generation_failures), 'generation'
            )}, "
            f"{ApplicationService._failure_count_text(
                len(execution_failures), 'execution'
            )})."
        )
        causes: list[str] = []
        if generation_failures:
            index, error = generation_failures[0]
            causes.append(f"Candidate {index} generation: {error}")
        if execution_failures:
            index, error = execution_failures[0]
            causes.append(f"Candidate {index} execution: {error}")
        if causes:
            summary += " " + " ".join(causes)
        return summary + " See logs/prompts.jsonl for full details."

    @staticmethod
    def _failure_count_text(count: int, stage: str) -> str:
        suffix = "failure" if count == 1 else "failures"
        return f"{count} {stage} {suffix}"

    @staticmethod
    def _failure(
        message: str,
        iteration: int,
        candidates: tuple[QueryCandidate, ...] = (),
    ) -> QueryWorkflowResult:
        result = QueryResult(
            state=ComponentState.FAILED,
            message=message,
        )
        return QueryWorkflowResult(
            state=ComponentState.FAILED,
            message=message,
            iteration=iteration,
            query_result=result,
            candidates=candidates,
        )
