"""Application-layer orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from db_whisperer.contracts import (
    AmbiguityDecision,
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
    SemanticColumnAnalysis,
    SemanticColumnRequest,
)
from db_whisperer.ambiguity import (
    AmbiguityService,
    SemanticColumnAmbiguityService,
)
from db_whisperer.ambiguity.candidate_alternatives import (
    cluster_executed_pairs,
)
from db_whisperer.etler import ETLService
from db_whisperer.prompt_logging import PromptLogger, PromptLogSink
from db_whisperer.querier import QueryService


@dataclass(frozen=True)
class _CandidateBatch:
    candidates: tuple[QueryCandidate, ...]
    results: tuple[tuple[str, QueryResult], ...]
    pairs: tuple[ExecutedQueryPair, ...]
    generation_failures: tuple[tuple[int, str], ...]
    execution_failures: tuple[tuple[int, str], ...]


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
        semantic_column: SemanticColumnAmbiguityService | None = None,
        event_logger: PromptLogSink | None = None,
        candidates_per_iteration: int = DEFAULT_CANDIDATES_PER_ITERATION,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_parallel_candidates: int = DEFAULT_MAX_PARALLEL_CANDIDATES,
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
        self.semantic_column = (
            semantic_column or SemanticColumnAmbiguityService()
        )
        self.event_logger = event_logger or PromptLogger()
        self.candidates_per_iteration = candidates_per_iteration
        self.max_iterations = max_iterations
        self.max_parallel_candidates = max_parallel_candidates
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

        # Analyze semantic-column ambiguity before SQL, but defer question
        # selection until executed alternatives are available. This prevents a
        # schema-only finding from pre-empting a more important candidate
        # difference while keeping semantic ambiguity independently actionable.
        semantic_analysis = self._analyze_semantic_columns(
            prompt=prompt,
            schema=schema,
            api_key=api_key,
            model=model,
            iteration=iteration,
            clarifications=clarifications,
        )

        batch = self._run_candidate_batch(
            prompt=prompt,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
            candidates_per_iteration=candidates_per_iteration,
            compliance_retry=False,
        )
        all_candidates = list(batch.candidates)
        if not batch.results:
            return self._failure(
                self._candidate_failure_message(
                    candidates_per_iteration,
                    0,
                    batch.generation_failures,
                    batch.execution_failures,
                ),
                iteration,
                tuple(all_candidates),
            )

        last_result = batch.results[-1][1]
        if iteration == self.max_iterations and not clarifications:
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(all_candidates),
            )

        if len(batch.pairs) < 2 and not clarifications:
            return self._failure(
                self._candidate_failure_message(
                    candidates_per_iteration,
                    len(batch.pairs),
                    batch.generation_failures,
                    batch.execution_failures,
                ),
                iteration,
                tuple(all_candidates),
            )

        ambiguity = self._evaluate_ambiguity(
            prompt=prompt,
            schema=schema,
            api_key=api_key,
            model=model,
            clarifications=clarifications,
            iteration=iteration,
            semantic_analysis=semantic_analysis,
            pairs=batch.pairs,
            compliance_retry=False,
        )

        if clarifications and ambiguity.compliance_passed is False:
            self.event_logger.log_event(
                event="clarification_compliance_retry_started",
                component="application",
                model=model,
                details={"iteration": iteration, "reason": ambiguity.reason},
            )
            retry_batch = self._run_candidate_batch(
                prompt=prompt,
                schema=schema,
                api_key=api_key,
                model=model,
                clarifications=clarifications,
                iteration=iteration,
                candidates_per_iteration=candidates_per_iteration,
                compliance_retry=True,
            )
            all_candidates.extend(retry_batch.candidates)
            if not retry_batch.results:
                return self._compliance_failure(
                    iteration,
                    tuple(all_candidates),
                    ambiguity,
                    self._candidate_failure_message(
                        candidates_per_iteration,
                        0,
                        retry_batch.generation_failures,
                        retry_batch.execution_failures,
                    ),
                    model,
                )
            batch = retry_batch
            ambiguity = self._evaluate_ambiguity(
                prompt=prompt,
                schema=schema,
                api_key=api_key,
                model=model,
                clarifications=clarifications,
                iteration=iteration,
                semantic_analysis=semantic_analysis,
                pairs=batch.pairs,
                compliance_retry=True,
            )

        if clarifications:
            if (
                ambiguity.state != ComponentState.ACCEPTED
                or ambiguity.compliance_passed is not True
            ):
                return self._compliance_failure(
                    iteration,
                    tuple(all_candidates),
                    ambiguity,
                    ambiguity.reason,
                    model,
                )
            selected = self._select_compliant_result(batch, ambiguity)
            if selected is None:
                return self._compliance_failure(
                    iteration,
                    tuple(all_candidates),
                    ambiguity,
                    "No validated compliant alternative could be selected.",
                    model,
                )
            selected_alternative, last_result = selected
            self.event_logger.log_event(
                event="clarification_compliant_result_selected",
                component="application",
                model=model,
                details={
                    "iteration": iteration,
                    "alternative_id": selected_alternative,
                    "support": dict(ambiguity.candidate_support).get(
                        selected_alternative, 0
                    ),
                    "sql": last_result.sql,
                },
            )

        valid_clarification = (
            ambiguity.state == ComponentState.ACCEPTED
            and ambiguity.passed is False
            and bool(ambiguity.question)
            and len(ambiguity.options) == 2
        )
        if clarifications and iteration == self.max_iterations:
            if valid_clarification:
                self.event_logger.log_event(
                    event="clarification_suppressed_at_iteration_limit",
                    component="application",
                    model=model,
                    details={
                        "iteration": iteration,
                        "question": ambiguity.question,
                    },
                )
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(all_candidates),
                ambiguity=ambiguity,
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
                candidates=tuple(all_candidates),
                ambiguity=ambiguity,
            )
        if ambiguity.passed:
            return QueryWorkflowResult(
                state=ComponentState.ACCEPTED,
                message=last_result.message,
                iteration=iteration,
                complete=True,
                query_result=last_result,
                candidates=tuple(all_candidates),
                ambiguity=ambiguity,
            )
        return QueryWorkflowResult(
            state=ComponentState.PENDING,
            message=ambiguity.question or "Please clarify your request.",
            iteration=iteration,
            query_result=last_result,
            candidates=tuple(all_candidates),
            ambiguity=ambiguity,
        )

    def _run_candidate_batch(
        self,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        clarifications: tuple[str, ...],
        iteration: int,
        candidates_per_iteration: int,
        compliance_retry: bool,
    ) -> _CandidateBatch:
        candidates: list[QueryCandidate] = []
        results: list[tuple[str, QueryResult]] = []
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
                        compliance_retry=compliance_retry,
                    ),
                    range(1, candidates_per_iteration + 1),
                )
            )

        batch_label = "compliance-retry-" if compliance_retry else ""
        for candidate_index, candidate, query_result in outcomes:
            candidates.append(candidate)
            event_details = {"compliance_retry": compliance_retry}
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
                    extra_details=event_details,
                )
                continue
            self._log_candidate_event(
                event="candidate_generated",
                model=model,
                iteration=iteration,
                candidate_index=candidate_index,
                candidate=candidate,
                extra_details=event_details,
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
                    extra_details=event_details,
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
                extra_details=event_details,
            )
            candidate_id = (
                f"iteration-{iteration}-{batch_label}candidate-"
                f"{candidate_index}"
            )
            results.append((candidate_id, query_result))
            pairs.append(
                ExecutedQueryPair.from_query_result(
                    candidate_id=candidate_id,
                    result=query_result,
                )
            )
        return _CandidateBatch(
            candidates=tuple(candidates),
            results=tuple(results),
            pairs=tuple(pairs),
            generation_failures=tuple(generation_failures),
            execution_failures=tuple(execution_failures),
        )

    def _evaluate_ambiguity(
        self,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        clarifications: tuple[str, ...],
        iteration: int,
        semantic_analysis: SemanticColumnAnalysis | None,
        pairs: tuple[ExecutedQueryPair, ...],
        compliance_retry: bool,
    ) -> AmbiguityDecision:
        try:
            ambiguity = self.ambiguity.evaluate(
                AmbiguityRequest(
                    user_query=prompt.strip(),
                    pairs=pairs,
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                    schema=schema,
                    semantic_analysis=semantic_analysis,
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

        fallback_used = False
        fallback_trigger_reason = ""
        if (
            not clarifications
            and ambiguity.state != ComponentState.ACCEPTED
            and semantic_analysis is not None
            and semantic_analysis.ambiguous
        ):
            fallback_trigger_reason = ambiguity.reason
            ambiguity = self.semantic_column.fallback_decision(
                semantic_analysis,
                pairs=pairs,
            )
            fallback_used = True
        if not ambiguity.candidate_support:
            ambiguity = replace(
                ambiguity,
                candidate_support=tuple(
                    (cluster.alternative_id, cluster.support_count)
                    for cluster in cluster_executed_pairs(pairs)
                ),
            )
        self.event_logger.log_event(
            event="ambiguity_decision",
            component="application",
            model=model,
            details={
                "iteration": iteration,
                "compliance_retry": compliance_retry,
                "state": ambiguity.state,
                "passed": ambiguity.passed,
                "mechanism": ambiguity.mechanism,
                "question": ambiguity.question,
                "options": list(ambiguity.options),
                "evidence_columns": list(ambiguity.evidence_columns),
                "evidence_alternatives": list(
                    ambiguity.evidence_alternatives
                ),
                "candidate_support": [
                    {"alternative_id": alternative_id, "support": support}
                    for alternative_id, support in ambiguity.candidate_support
                ],
                "candidate_rejection_reason": (
                    ambiguity.candidate_rejection_reason
                ),
                "compliance_passed": ambiguity.compliance_passed,
                "compliant_alternatives": list(
                    ambiguity.compliant_alternatives
                ),
                "rejected_alternatives": [
                    {"alternative_id": alternative_id, "reason": reason}
                    for alternative_id, reason
                    in ambiguity.rejected_alternatives
                ],
                "reason": ambiguity.reason,
                "fallback_used": fallback_used,
                "fallback_trigger_reason": fallback_trigger_reason,
            },
        )
        return ambiguity

    @staticmethod
    def _select_compliant_result(
        batch: _CandidateBatch,
        ambiguity: AmbiguityDecision,
    ) -> tuple[str, QueryResult] | None:
        compliant = set(ambiguity.compliant_alternatives)
        eligible = [
            cluster
            for cluster in cluster_executed_pairs(batch.pairs)
            if cluster.alternative_id in compliant
        ]
        if not eligible:
            return None
        selected = max(eligible, key=lambda cluster: cluster.support_count)
        results_by_id = dict(batch.results)
        result = results_by_id.get(selected.representative.candidate_id)
        if result is None:
            return None
        return selected.alternative_id, result

    def _compliance_failure(
        self,
        iteration: int,
        candidates: tuple[QueryCandidate, ...],
        ambiguity: AmbiguityDecision,
        reason: str,
        model: str,
    ) -> QueryWorkflowResult:
        message = (
            "No SQL result was returned because DB Whisperer could not "
            "verify that the selected clarification was applied. "
            + (reason.strip() or "Clarification compliance was unavailable.")
        )
        self.event_logger.log_event(
            event="clarification_compliance_failed",
            component="application",
            model=model,
            details={"iteration": iteration, "reason": reason},
        )
        return QueryWorkflowResult(
            state=ComponentState.FAILED,
            message=message,
            iteration=iteration,
            complete=True,
            query_result=None,
            candidates=candidates,
            ambiguity=ambiguity,
        )

    def _analyze_semantic_columns(
        self,
        prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        iteration: int,
        clarifications: tuple[str, ...],
    ) -> SemanticColumnAnalysis | None:
        """Collect pre-SQL semantic evidence without interrupting execution."""
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
            analysis = self.semantic_column.analyze(
                SemanticColumnRequest(
                    user_query=prompt.strip(),
                    schema=schema,
                    api_key=api_key,
                    model=model,
                    clarifications=clarifications,
                )
            )
            if not isinstance(analysis, SemanticColumnAnalysis):
                raise TypeError(
                    "Semantic-column analyzer returned an invalid result."
                )
        except Exception as error:  # noqa: BLE001 - degrade gracefully.
            analysis = SemanticColumnAnalysis(
                state=ComponentState.FAILED,
                reason=f"Semantic-column detection failed: {error}",
            )

        self.event_logger.log_event(
            event="semantic_column_detection",
            component="application",
            model=model,
            details={
                "iteration": iteration,
                "state": analysis.state,
                "term_count": len(analysis.terms),
                "reason": analysis.reason,
            },
        )
        return analysis

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
        compliance_retry: bool,
    ) -> tuple[int, QueryCandidate, QueryResult | None]:
        """Generate and execute one candidate inside a worker thread."""
        attempt_number = (
            (iteration - 1) * candidates_per_iteration
            + candidate_index
        )
        if compliance_retry:
            attempt_number = (
                (self.max_iterations + iteration) * candidates_per_iteration
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
                    compliance_retry=compliance_retry,
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
        extra_details: dict[str, object] | None = None,
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
        if extra_details:
            details.update(extra_details)
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
        generation_failures: Sequence[tuple[int, str]],
        execution_failures: Sequence[tuple[int, str]],
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
