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
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
    SemanticColumnRequest,
)


MECHANISM = "semantic-column"
DEFAULT_MAX_TERMS = 6
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
        if not self._has_same_bucket_pair(columns):
            return self._pass(
                "No two columns share a semantic type; semantic-column "
                "ambiguity is impossible."
            )

        try:
            judgment = self.client.evaluate(
                prompt=self.prompt_builder.build_term_prompt(request),
                api_key=request.api_key,
                model=request.model,
            )
        except (AmbiguityJudgeError, ValueError) as error:
            return self._failure(f"Term extraction failed: {error}")

        terms, dropped, capped = self._parse_terms(judgment, columns)
        if terms is None:
            return self._failure("Term extraction returned no usable terms list.")

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
                else "No term maps to more than one same-type column."
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

    def _parse_terms(
        self,
        judgment: dict[str, object],
        columns: dict[ColumnRef, SemanticColumnCandidate],
    ) -> tuple[tuple[SemanticAmbiguityTerm, ...] | None, tuple[str, ...], bool]:
        raw_terms = judgment.get("terms")
        if not isinstance(raw_terms, list):
            return None, (), False

        findings: list[SemanticAmbiguityTerm] = []
        dropped: list[str] = []
        for raw in raw_terms:
            if not isinstance(raw, dict):
                continue
            term = raw.get("term")
            raw_columns = raw.get("columns")
            if not isinstance(term, str) or not term.strip():
                continue
            if not isinstance(raw_columns, list):
                continue

            known: list[SemanticColumnCandidate] = []
            for entry in raw_columns:
                if not isinstance(entry, dict):
                    continue
                table, column = entry.get("table"), entry.get("column")
                if not isinstance(table, str) or not isinstance(column, str):
                    continue
                ref = (table.strip(), column.strip())
                candidate = columns.get(ref)
                if candidate is None:
                    label = f"{ref[0]}.{ref[1]}"
                    if label not in dropped:
                        dropped.append(label)
                elif candidate not in known:
                    known.append(candidate)

            grouped: dict[str, list[SemanticColumnCandidate]] = {}
            for candidate in known:
                grouped.setdefault(candidate.bucket, []).append(candidate)
            if not grouped:
                continue
            bucket = min(grouped, key=lambda key: (-len(grouped[key]), key))
            members = tuple(
                sorted(
                    grouped[bucket],
                    key=lambda item: (item.table, item.column),
                )
            )
            if len(members) >= 2:
                findings.append(
                    SemanticAmbiguityTerm(
                        term=term.strip(),
                        bucket=bucket,
                        columns=members,
                    )
                )

        findings.sort(key=lambda item: (-len(item.columns), item.term.casefold()))
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
                cls._names_qualified_ref(text, column.qualified_name)
                for column in term.columns
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
            key=lambda item: (-len(item.columns), item.term.casefold()),
        )
        first, second = cls._fallback_columns(term, pairs)
        question = (
            f'The term "{term.term}" could mean more than one column. '
            "Which one do you mean? "
            f'(clarifying which column: "{first.qualified_name}" or '
            f'"{second.qualified_name}")'
        )
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=question,
            options=(
                f'"{first.column}" (from {first.table})',
                f'"{second.column}" (from {second.table})',
            ),
            reason=(
                "Used a deterministic semantic-column clarification because "
                "the unified ambiguity judge failed."
            ),
            mechanism=MECHANISM,
            evidence_columns=(
                first.qualified_name,
                second.qualified_name,
            ),
        )

    @classmethod
    def _fallback_columns(
        cls,
        term: SemanticAmbiguityTerm,
        pairs: tuple[ExecutedQueryPair, ...],
    ) -> tuple[SemanticColumnCandidate, SemanticColumnCandidate]:
        """Prefer the column used by candidates against one alternative."""
        scores = {
            column: sum(
                cls._sql_names_column(pair.sql, column)
                for pair in pairs
            )
            for column in term.columns
        }
        highest = max(scores.values(), default=0)
        if highest:
            first = next(
                column for column in term.columns if scores[column] == highest
            )
            second = next(column for column in term.columns if column != first)
            return first, second
        return term.columns[0], term.columns[1]

    @staticmethod
    def _sql_names_column(
        sql: str,
        column: SemanticColumnCandidate,
    ) -> bool:
        escaped = re.escape(column.column)
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
