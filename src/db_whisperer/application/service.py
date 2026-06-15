"""Application-layer orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from db_whisperer.contracts import (
    AmbiguityRequest,
    ComponentState,
    CsvUpload,
    ExecutedQueryPair,
    IngestionResult,
    QueryCandidate,
    QueryRequest,
    QueryResult,
    QueryWorkflowResult,
    SchemaMetadata,
)
from db_whisperer.ambiguity import AmbiguityService
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
        event_logger: PromptLogSink | None = None,
        candidates_per_iteration: int = DEFAULT_CANDIDATES_PER_ITERATION,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_parallel_candidates: int = DEFAULT_MAX_PARALLEL_CANDIDATES,
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
        self.event_logger = event_logger or PromptLogger()
        self.candidates_per_iteration = candidates_per_iteration
        self.max_iterations = max_iterations
        self.max_parallel_candidates = max_parallel_candidates

    def ingest_csvs(self, files: Sequence[CsvUpload]) -> IngestionResult:
        """Route an uploaded CSV file to Component A."""
        return self.etler.ingest(files)

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

        ambiguity = self.ambiguity.evaluate(
            AmbiguityRequest(
                user_query=prompt.strip(),
                pairs=tuple(pairs),
                api_key=api_key,
                model=model,
                clarifications=clarifications,
            )
        )
        if ambiguity.state == ComponentState.FAILED:
            return QueryWorkflowResult(
                state=ComponentState.FAILED,
                message=ambiguity.reason,
                iteration=iteration,
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
        if not ambiguity.question or len(ambiguity.options) != 2:
            return QueryWorkflowResult(
                state=ComponentState.FAILED,
                message=(
                    "Ambiguity response requires one question and two options."
                ),
                iteration=iteration,
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
