"""Pre-SQL analysis of vague terms that may denote same-kind columns."""

from __future__ import annotations

import re

from db_whisperer.ambiguity.openrouter_client import (
    AmbiguityJudgeError,
    AmbiguityOpenRouterClient,
)
from db_whisperer.ambiguity.semantic_column_prompt_builder import (
    SemanticColumnPromptBuilder,
)
from db_whisperer.contracts import (
    AmbiguityDecision,
    ComponentState,
    ExecutedQueryPair,
    SEMANTIC_DIMENSIONS,
    SEMANTIC_OPERATIONS,
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
    SemanticGrounding,
    SemanticInterpretation,
    SemanticColumnRequest,
)


MECHANISM = "semantic-column"
DEFAULT_MAX_TERMS = 6
DIMENSION_PRIORITY = {
    "measure_definition": 0,
    "aggregation_grain": 1,
    "temporal_role": 2,
    "entity_scope": 3,
    "episode_scope": 4,
    "filter_scope": 5,
    "column_meaning": 6,
}
ColumnRef = tuple[str, str]

TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "REAL", "DECIMAL",
)


def semantic_bucket(data_type: str) -> str:
    """Map a DuckDB type to a conservative coarse semantic bucket."""
    upper = data_type.upper()
    if upper.startswith(TEMPORAL_TYPES):
        return "temporal"
    if upper.startswith("BOOL"):
        return "boolean"
    if upper.startswith(NUMERIC_TYPES):
        return "numeric"
    return "textual"


class SemanticColumnAmbiguityService:
    """Analyze semantic-column ambiguity without deciding when to interrupt."""

    def __init__(
        self,
        client: AmbiguityOpenRouterClient | None = None,
        prompt_builder: SemanticColumnPromptBuilder | None = None,
        max_terms: int = DEFAULT_MAX_TERMS,
    ) -> None:
        if max_terms < 1:
            raise ValueError("max_terms must be at least 1.")
        self.client = client or AmbiguityOpenRouterClient()
        self.prompt_builder = prompt_builder or SemanticColumnPromptBuilder()
        self.max_terms = max_terms

    def analyze(self, request: SemanticColumnRequest) -> SemanticColumnAnalysis:
        """Return all validated, unresolved vague-term findings."""
        validation_error = self._validate_request(request)
        if validation_error:
            return self._failure(validation_error)

        columns = self._columns(request.schema)
        if not columns:
            return self._pass("Schema exposes no columns to compare.")

        try:
            judgment = self.client.evaluate(
                prompt=self.prompt_builder.build_term_prompt(request),
                api_key=request.api_key,
                model=request.model,
            )
        except (AmbiguityJudgeError, ValueError) as error:
            return self._failure(f"Term extraction failed: {error}")

        terms, dropped, capped = self._parse_findings(
            judgment,
            request.schema,
            columns,
            request.user_query,
        )
        if terms is None:
            return self._failure(
                "Term extraction returned no usable findings list."
            )

        unresolved = tuple(
            term for term in terms
            if not self._term_settled(term, request.clarifications)
        )
        notes: list[str] = []
        if dropped:
            notes.append(
                f"Ignored {len(dropped)} unknown column reference(s): "
                + ", ".join(dropped)
                + "."
            )
        if capped:
            notes.append(
                f"Only the first {self.max_terms} ambiguous terms were retained."
            )
        if not unresolved:
            base = (
                "All semantic-column ambiguities have already been clarified."
                if terms
                else "No unresolved semantic-intent finding was validated."
            )
            return self._pass(" ".join((base, *notes)).strip())

        return SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            terms=unresolved,
            reason=" ".join(
                (
                    f"Found {len(unresolved)} unresolved semantic-column "
                    "ambiguity term(s).",
                    *notes,
                )
            ).strip(),
        )

    @staticmethod
    def _validate_request(request: SemanticColumnRequest) -> str | None:
        if not request.user_query.strip():
            return "User query is required."
        if not request.api_key.strip():
            return "OpenRouter API key is required."
        if not request.model.strip():
            return "OpenRouter model is required."
        return None

    @staticmethod
    def _columns(
        schema: SchemaMetadata,
    ) -> dict[ColumnRef, SemanticColumnCandidate]:
        result: dict[ColumnRef, SemanticColumnCandidate] = {}
        source = (
            (
                (table.table_name, column.name, column.data_type)
                for table in schema.tables
                for column in table.columns
            )
            if schema.tables
            else (
                (column.table_name, column.name, column.data_type)
                for column in schema.columns
            )
        )
        for table, column, data_type in source:
            result[(table, column)] = SemanticColumnCandidate(
                table=table,
                column=column,
                data_type=data_type,
                bucket=semantic_bucket(data_type),
            )
        return result

    @staticmethod
    def _has_same_bucket_pair(
        columns: dict[ColumnRef, SemanticColumnCandidate],
    ) -> bool:
        seen: set[str] = set()
        for candidate in columns.values():
            if candidate.bucket in seen:
                return True
            seen.add(candidate.bucket)
        return False

    def _parse_findings(
        self,
        judgment: dict[str, object],
        schema: SchemaMetadata,
        columns: dict[ColumnRef, SemanticColumnCandidate],
        user_query: str,
    ) -> tuple[tuple[SemanticAmbiguityTerm, ...] | None, tuple[str, ...], bool]:
        if not isinstance(judgment, dict):
            return None, (), False
        raw_terms = judgment.get("findings")
        if not isinstance(raw_terms, list):
            return None, (), False

        known_tables = set(schema.table_names)
        known_tables.update(column.table_name for column in schema.columns)
        known_tables.update(table.table_name for table in schema.tables)
        known_columns = {
            candidate.qualified_name: candidate for candidate in columns.values()
        }

        findings: list[SemanticAmbiguityTerm] = []
        dropped: list[str] = []
        for raw in raw_terms:
            if not isinstance(raw, dict):
                continue
            if raw.get("resolved_by_context") is True:
                continue
            term = raw.get("term")
            dimension = raw.get("dimension")
            raw_interpretations = raw.get("interpretations")
            if not isinstance(term, str) or not term.strip():
                continue
            normalized_term = " ".join(
                "".join(
                    character if character.isalnum() else " "
                    for character in term.casefold()
                ).split()
            )
            normalized_query = " ".join(
                "".join(
                    character if character.isalnum() else " "
                    for character in user_query.casefold()
                ).split()
            )
            if not normalized_term or normalized_term not in normalized_query:
                continue
            if dimension not in SEMANTIC_DIMENSIONS:
                continue
            if not isinstance(raw_interpretations, list):
                continue

            parsed: list[tuple[int, str, str, SemanticGrounding]] = []
            relevance_values: list[int] = []
            for entry in raw_interpretations:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                meaning = entry.get("meaning")
                relevance = entry.get("relevance")
                raw_tables = entry.get("tables")
                raw_columns = entry.get("columns")
                raw_operations = entry.get("operations")
                grain = entry.get("grain", "")
                temporal_role = entry.get("temporal_role", "")
                if (
                    not isinstance(label, str)
                    or not label.strip()
                    or not isinstance(meaning, str)
                    or not meaning.strip()
                    or isinstance(relevance, bool)
                    or not isinstance(relevance, int)
                    or relevance < 1
                    or not isinstance(raw_tables, list)
                    or not raw_tables
                    or not isinstance(raw_columns, list)
                    or not raw_columns
                    or not isinstance(raw_operations, list)
                    or not raw_operations
                    or not isinstance(grain, str)
                    or not isinstance(temporal_role, str)
                ):
                    continue

                tables = tuple(
                    value.strip() for value in raw_tables
                    if isinstance(value, str) and value.strip()
                )
                column_names = tuple(
                    value.strip() for value in raw_columns
                    if isinstance(value, str) and value.strip()
                )
                operations = tuple(
                    value.strip() for value in raw_operations
                    if isinstance(value, str) and value.strip()
                )
                unknown_tables = [
                    value for value in tables if value not in known_tables
                ]
                unknown_columns = [
                    value for value in column_names
                    if value not in known_columns
                ]
                unknown_operations = [
                    value for value in operations
                    if value not in SEMANTIC_OPERATIONS
                ]
                mismatched_columns = [
                    value for value in column_names
                    if value.split(".", 1)[0] not in tables
                ]
                invalid_values = (
                    len(tables) != len(raw_tables)
                    or len(column_names) != len(raw_columns)
                    or len(operations) != len(raw_operations)
                    or unknown_tables
                    or unknown_columns
                    or unknown_operations
                    or mismatched_columns
                )
                if invalid_values:
                    for value in (
                        *unknown_tables,
                        *unknown_columns,
                        *unknown_operations,
                        *mismatched_columns,
                    ):
                        if value not in dropped:
                            dropped.append(value)
                    continue

                relevance_values.append(relevance)
                parsed.append((
                    relevance,
                    label.strip(),
                    meaning.strip(),
                    SemanticGrounding(
                        tables=tables,
                        columns=column_names,
                        operations=operations,
                        grain=grain.strip(),
                        temporal_role=temporal_role.strip(),
                    ),
                ))

            if len(relevance_values) != len(set(relevance_values)):
                continue

            unique: list[tuple[int, str, str, SemanticGrounding]] = []
            seen_grounding: set[SemanticGrounding] = set()
            for item in sorted(parsed, key=lambda value: value[0]):
                if item[3] in seen_grounding:
                    continue
                seen_grounding.add(item[3])
                unique.append(item)
            if len(unique) < 2:
                continue

            interpretations = tuple(
                SemanticInterpretation(
                    interpretation_id=f"interpretation_{index}",
                    label=label,
                    meaning=meaning,
                    relevance=index,
                    grounding=grounding,
                )
                for index, (_, label, meaning, grounding) in enumerate(
                    unique,
                    start=1,
                )
            )
            findings.append(
                SemanticAmbiguityTerm(
                    term=term.strip(),
                    dimension=dimension,
                    interpretations=interpretations,
                )
            )

        capped = len(findings) > self.max_terms
        return tuple(findings[: self.max_terms]), tuple(dropped), capped

    @classmethod
    def _term_settled(
        cls,
        term: SemanticAmbiguityTerm,
        clarifications: tuple[str, ...],
    ) -> bool:
        return any(
            sum(
                interpretation.label.casefold() in text.casefold()
                for interpretation in term.interpretations
            ) >= 2
            for text in clarifications
        )

    @staticmethod
    def _names_qualified_ref(text: str, qualified_name: str) -> bool:
        pattern = (
            rf"(?<![A-Za-z0-9_]){re.escape(qualified_name)}"
            r"(?![A-Za-z0-9_])"
        )
        return re.search(pattern, text) is not None

    @classmethod
    def fallback_decision(
        cls,
        analysis: SemanticColumnAnalysis,
        pairs: tuple[ExecutedQueryPair, ...] = (),
    ) -> AmbiguityDecision:
        """Build a deterministic two-option question from the strongest term."""
        if not analysis.ambiguous:
            return AmbiguityDecision(
                state=ComponentState.FAILED,
                reason="No semantic finding is available for fallback.",
                mechanism=MECHANISM,
            )
        term = min(
            analysis.terms,
            key=lambda item: (
                DIMENSION_PRIORITY.get(item.dimension, 99),
                min(
                    interpretation.relevance
                    for interpretation in item.interpretations
                ),
                item.term.casefold(),
            ),
        )
        first, second = cls._fallback_interpretations(term, pairs)
        first_columns = first.grounding.columns
        second_columns = second.grounding.columns
        evidence_columns = tuple(dict.fromkeys(
            (*first_columns, *second_columns)
        ))
        grounding_note = "[grounding: " + ", ".join(
            f'"{column}"' for column in evidence_columns
        ) + "]"
        question = (
            f'The phrase "{term.term}" has two plausible meanings. '
            f"Which one do you mean? {grounding_note}"
        )
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=question,
            options=(
                first.label,
                second.label,
            ),
            reason=(
                "Used a deterministic semantic-column clarification because "
                "the unified ambiguity judge failed."
            ),
            mechanism=MECHANISM,
            evidence_columns=evidence_columns,
            evidence_interpretations=(
                first.interpretation_id,
                second.interpretation_id,
            ),
            evidence_dimension=term.dimension,
        )

    @classmethod
    def _fallback_interpretations(
        cls,
        term: SemanticAmbiguityTerm,
        pairs: tuple[ExecutedQueryPair, ...],
    ) -> tuple[SemanticInterpretation, SemanticInterpretation]:
        """Prefer the interpretation grounded in candidate SQL."""
        scores = {
            interpretation: sum(
                any(
                    cls._sql_names_column_name(pair.sql, column)
                    for column in interpretation.grounding.columns
                )
                for pair in pairs
            )
            for interpretation in term.interpretations
        }
        highest = max(scores.values(), default=0)
        if highest:
            first = next(
                interpretation
                for interpretation in term.interpretations
                if scores[interpretation] == highest
            )
            second = next(
                interpretation
                for interpretation in term.interpretations
                if interpretation != first
            )
            return first, second
        return term.interpretations[0], term.interpretations[1]

    @staticmethod
    def _sql_names_column_name(
        sql: str,
        qualified_name: str,
    ) -> bool:
        column_name = qualified_name.rsplit(".", 1)[-1]
        escaped = re.escape(column_name)
        quoted = rf'(?i)(?<![A-Za-z0-9_])"{escaped}"(?![A-Za-z0-9_])'
        bare = rf"(?i)(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
        return (
            re.search(quoted, sql) is not None
            or re.search(bare, sql) is not None
        )

    @staticmethod
    def _pass(reason: str) -> SemanticColumnAnalysis:
        return SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            reason=reason,
        )

    @staticmethod
    def _failure(reason: str) -> SemanticColumnAnalysis:
        return SemanticColumnAnalysis(
            state=ComponentState.FAILED,
            reason=reason,
        )
