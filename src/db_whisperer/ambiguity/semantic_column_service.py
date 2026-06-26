"""Semantic-type column ambiguity detection (Component B, secondary mechanism).

This is the PDF's Mechanism 2, a fallback after the join-path mechanism finds no
multiplicity. The flow mirrors the join-path detector:

1. The LLM finds natural-language terms whose meaning maps to more than one
   column (for example "dates" -> admission date vs discharge date vs date of
   birth) and lists the columns each term could mean.
2. A deterministic guard keeps only columns that exist in the schema and groups
   a term's columns by semantic type (temporal, numeric, boolean, textual). A
   term is ambiguous only when two or more of its columns share one type, so the
   model cannot pair a date with a name.
3. When an ambiguous term remains, the LLM (with a deterministic fallback) writes
   one two-option clarification choosing between the two most likely columns.

When nothing is ambiguous -- no term, a single matching column, or columns of
different types -- the detector passes so the application proceeds to SQL
generation. Any failure is surfaced as a failed decision rather than a silent
guess.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    SchemaMetadata,
    SemanticColumnRequest,
)


MECHANISM = "semantic-column"
DEFAULT_MAX_TERMS = 6

# Coarse semantic buckets derived from the DuckDB data type. Two columns are
# "the same kind" only when they fall in the same bucket; this is the
# deterministic guard against the model pairing, say, a date with a name.
TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")
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

# A (table, column) pair.
ColumnRef = tuple[str, str]


def semantic_bucket(data_type: str) -> str:
    """Map a DuckDB data type to a coarse semantic bucket."""
    upper = data_type.upper()
    if upper.startswith(TEMPORAL_TYPES):
        return "temporal"
    if upper.startswith("BOOL"):
        return "boolean"
    if upper.startswith(NUMERIC_TYPES):
        return "numeric"
    return "textual"


@dataclass(frozen=True)
class _AmbiguousTerm:
    """One query term that maps to several same-bucket columns."""

    term: str
    bucket: str
    columns: tuple[ColumnRef, ...]


class SemanticColumnAmbiguityService:
    """Detect terms that map to multiple columns of the same semantic type."""

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

    def detect(self, request: SemanticColumnRequest) -> AmbiguityDecision:
        """Return a semantic-column clarification, a pass, or a failure."""
        validation_error = self._validate_request(request)
        if validation_error:
            return self._failure(validation_error)

        column_buckets = self._column_buckets(request.schema)
        if not column_buckets:
            return self._pass("Schema exposes no columns to compare.")

        # Cheap deterministic pre-check: if no two columns anywhere share a
        # semantic bucket, no term can be ambiguous between same-type columns,
        # so skip the extraction LLM call entirely.
        if not self._has_same_bucket_pair(column_buckets):
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

        terms, dropped, capped = self._ambiguous_terms(judgment, column_buckets)
        context_note = self._context_note(dropped, capped)
        if terms is None:
            return self._failure(
                "Term extraction returned no usable terms list."
            )
        if not terms:
            return self._pass(
                self._join_text(
                    "No term maps to more than one same-type column.",
                    context_note,
                )
            )

        # Exclude terms already resolved by a previous clarification so a
        # multi-term question keeps clarifying remaining terms across rounds.
        unsettled = self._unsettled_terms(terms, request.clarifications)
        if not unsettled:
            return self._pass(
                self._join_text(
                    "All ambiguous terms have already been clarified.",
                    context_note,
                )
            )

        chosen = self._choose_term(unsettled)
        interpretations, extra_columns = self._select_two_columns(
            chosen.columns
        )
        question, options, used_llm = self._clarification(
            request, chosen, interpretations
        )
        question = self._ensure_columns_named(question, interpretations)
        reason = self._reason(
            chosen,
            extra_columns,
            len(unsettled) - 1,
            context_note,
            used_llm,
        )
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=question,
            options=options,
            reason=reason,
            mechanism=MECHANISM,
        )

    # -- Validation ------------------------------------------------------

    @staticmethod
    def _validate_request(request: SemanticColumnRequest) -> str | None:
        if not request.user_query.strip():
            return "User query is required."
        if not request.api_key.strip():
            return "OpenRouter API key is required."
        if not request.model.strip():
            return "OpenRouter model is required."
        if request.schema is None:
            return "Schema metadata is required."
        return None

    # -- Schema columns --------------------------------------------------

    @staticmethod
    def _column_buckets(schema: SchemaMetadata) -> dict[ColumnRef, str]:
        """Map every known ``(table, column)`` to its semantic bucket."""
        buckets: dict[ColumnRef, str] = {}
        if schema.tables:
            for table in schema.tables:
                for column in table.columns:
                    buckets[(table.table_name, column.name)] = semantic_bucket(
                        column.data_type
                    )
            return buckets
        for column in schema.columns:
            buckets[(column.table_name, column.name)] = semantic_bucket(
                column.data_type
            )
        return buckets

    @staticmethod
    def _has_same_bucket_pair(column_buckets: dict[ColumnRef, str]) -> bool:
        """True when at least two columns share one semantic bucket."""
        seen: set[str] = set()
        for bucket in column_buckets.values():
            if bucket in seen:
                return True
            seen.add(bucket)
        return False

    # -- Term mapping ----------------------------------------------------

    def _ambiguous_terms(
        self,
        judgment: dict[str, object],
        column_buckets: dict[ColumnRef, str],
    ) -> tuple[tuple[_AmbiguousTerm, ...] | None, tuple[str, ...], bool]:
        """Extract terms that map to two or more same-bucket known columns.

        Returns ``(terms, dropped, capped)``. ``terms`` is ``None`` only when
        the response is structurally invalid. ``dropped`` lists column
        references the model returned that are not in the schema (reported, not
        guessed). ``capped`` is ``True`` when more ambiguous terms were found
        than the ``max_terms`` analysis cap.
        """
        raw_terms = judgment.get("terms")
        if not isinstance(raw_terms, list):
            return None, (), False

        ambiguous: list[_AmbiguousTerm] = []
        dropped: list[str] = []
        for raw in raw_terms:
            if not isinstance(raw, dict):
                continue
            term = raw.get("term")
            columns = raw.get("columns")
            if not isinstance(term, str) or not term.strip():
                continue
            if not isinstance(columns, list):
                continue

            known: list[ColumnRef] = []
            for entry in columns:
                if not isinstance(entry, dict):
                    continue
                table = entry.get("table")
                column = entry.get("column")
                if not isinstance(table, str) or not isinstance(column, str):
                    continue
                ref = (table.strip(), column.strip())
                if ref in column_buckets:
                    if ref not in known:
                        known.append(ref)
                else:
                    label = f"{ref[0]}.{ref[1]}"
                    if label not in dropped:
                        dropped.append(label)

            bucket, members = self._largest_same_bucket(known, column_buckets)
            if len(members) >= 2:
                ambiguous.append(
                    _AmbiguousTerm(
                        term=term.strip(),
                        bucket=bucket,
                        columns=tuple(members),
                    )
                )

        capped = len(ambiguous) > self.max_terms
        return tuple(ambiguous[: self.max_terms]), tuple(dropped), capped

    @staticmethod
    def _largest_same_bucket(
        columns: list[ColumnRef],
        column_buckets: dict[ColumnRef, str],
    ) -> tuple[str, list[ColumnRef]]:
        """Return the bucket with the most of ``columns``, and those columns."""
        grouped: dict[str, list[ColumnRef]] = {}
        for ref in columns:
            grouped.setdefault(column_buckets[ref], []).append(ref)
        if not grouped:
            return "", []
        # Most-populated bucket first, then bucket name for determinism.
        bucket = min(grouped, key=lambda name: (-len(grouped[name]), name))
        members = sorted(grouped[bucket])
        return bucket, members

    def _context_note(self, dropped: tuple[str, ...], capped: bool) -> str:
        parts: list[str] = []
        if dropped:
            parts.append(
                f"Ignored {len(dropped)} column reference(s) the model "
                f"returned that are not in the schema: {', '.join(dropped)}."
            )
        if capped:
            parts.append(
                f"More than {self.max_terms} ambiguous terms were found; only "
                f"the first {self.max_terms} were analyzed, so some "
                "column ambiguity may be unreported."
            )
        return " ".join(parts)

    @staticmethod
    def _join_text(*parts: str) -> str:
        return " ".join(part for part in parts if part)

    # -- Term selection --------------------------------------------------

    @staticmethod
    def _choose_term(terms: list[_AmbiguousTerm]) -> _AmbiguousTerm:
        """Pick the most ambiguous term (most columns), then by term text."""
        return min(
            terms,
            key=lambda term: (-len(term.columns), term.term.casefold()),
        )

    @classmethod
    def _unsettled_terms(
        cls,
        terms: tuple[_AmbiguousTerm, ...],
        clarifications: tuple[str, ...],
    ) -> list[_AmbiguousTerm]:
        """Drop terms whose columns a prior answer already chose between."""
        return [
            term
            for term in terms
            if not cls._term_settled(term, clarifications)
        ]

    @classmethod
    def _term_settled(
        cls,
        term: _AmbiguousTerm,
        clarifications: tuple[str, ...],
    ) -> bool:
        """True if one clarification already named two of the term's columns.

        Columns are matched as fully-qualified ``table.column`` references, so
        two same-named columns in different tables (``customers.name`` vs
        ``stores.name``) cannot settle from a single bare-name token. The asked
        question always names both presented columns this way, so a settled
        term's two columns both appear in the accumulated answer.
        """
        for clarification in clarifications:
            named = sum(
                1
                for table, column in term.columns
                if cls._names_qualified_ref(clarification, table, column)
            )
            if named >= 2:
                return True
        return False

    @staticmethod
    def _names_qualified_ref(text: str, table: str, column: str) -> bool:
        """Match ``table.column`` as a whole identifier token."""
        pattern = (
            rf"(?<![A-Za-z0-9_]){re.escape(table)}\.{re.escape(column)}"
            r"(?![A-Za-z0-9_])"
        )
        return re.search(pattern, text) is not None

    @classmethod
    def _ensure_columns_named(
        cls,
        question: str,
        interpretations: tuple[ColumnRef, ColumnRef],
    ) -> str:
        """Append both qualified column refs unless the question already has them.

        The appended ``"table.column"`` references are what ``_term_settled``
        looks for next round, so they must always reach the recorded
        clarification -- an LLM-written question rarely contains the literal
        qualified form, so this normally appends.
        """
        first, second = interpretations
        if cls._names_qualified_ref(
            question, *first
        ) and cls._names_qualified_ref(question, *second):
            return question
        return (
            f'{question} (clarifying which column: '
            f'"{first[0]}.{first[1]}" or "{second[0]}.{second[1]}")'
        )

    @staticmethod
    def _select_two_columns(
        columns: tuple[ColumnRef, ...],
    ) -> tuple[tuple[ColumnRef, ColumnRef], int]:
        """Return the two columns to present (sorted) and the extra count."""
        # ``columns`` is already sorted by ``_largest_same_bucket``.
        first = columns[0]
        second = columns[1]
        extra = max(0, len(columns) - 2)
        return (first, second), extra

    # -- Clarification ---------------------------------------------------

    def _clarification(
        self,
        request: SemanticColumnRequest,
        term: _AmbiguousTerm,
        interpretations: tuple[ColumnRef, ColumnRef],
    ) -> tuple[str, tuple[str, str], bool]:
        """Generate the question and two options, LLM-first with a fallback."""
        try:
            judgment = self.client.evaluate(
                prompt=self.prompt_builder.build_clarification_prompt(
                    request,
                    term.term,
                    interpretations,
                ),
                api_key=request.api_key,
                model=request.model,
            )
        except (AmbiguityJudgeError, ValueError):
            judgment = None

        if judgment is not None:
            parsed = self._parse_clarification(judgment)
            if parsed is not None:
                question, options = parsed
                return question, options, True

        question, options = self._fallback_clarification(term, interpretations)
        return question, options, False

    @staticmethod
    def _parse_clarification(
        judgment: dict[str, object],
    ) -> tuple[str, tuple[str, str]] | None:
        question = judgment.get("question")
        options = judgment.get("options")
        if not isinstance(question, str) or not question.strip():
            return None
        if not isinstance(options, list) or len(options) != 2:
            return None
        if not all(
            isinstance(option, str) and option.strip() for option in options
        ):
            return None
        first, second = (option.strip() for option in options)
        if first.casefold() == second.casefold():
            return None
        return question.strip(), (first, second)

    @classmethod
    def _fallback_clarification(
        cls,
        term: _AmbiguousTerm,
        interpretations: tuple[ColumnRef, ColumnRef],
    ) -> tuple[str, tuple[str, str]]:
        """Build a transparent question directly from the two columns."""
        labels = [cls._column_label(ref) for ref in interpretations]
        if labels[0].casefold() == labels[1].casefold():
            labels = [
                f"{interpretations[index][0]}.{interpretations[index][1]}"
                for index in range(2)
            ]
        question = (
            f'The term "{term.term}" could mean more than one column. '
            "Which one do you mean?"
        )
        return question, (labels[0], labels[1])

    @staticmethod
    def _column_label(ref: ColumnRef) -> str:
        table, column = ref
        return f'"{column}" (from {table})'

    # -- Reason / outcomes ----------------------------------------------

    @staticmethod
    def _reason(
        term: _AmbiguousTerm,
        extra_columns: int,
        other_terms: int,
        context_note: str,
        used_llm: bool,
    ) -> str:
        parts = [
            f'The term "{term.term}" maps to {len(term.columns)} '
            f"{term.bucket} columns."
        ]
        if extra_columns:
            parts.append(
                f"Presented the two most likely of {len(term.columns)} columns."
            )
        if other_terms:
            parts.append(
                f"{other_terms} other ambiguous term(s) will be clarified in "
                "following rounds."
            )
        if not used_llm:
            parts.append(
                "Used a deterministic clarification because the model did not "
                "return a usable question."
            )
        if context_note:
            parts.append(context_note)
        return " ".join(parts)

    @staticmethod
    def _pass(reason: str) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=True,
            reason=reason,
            mechanism=MECHANISM,
        )

    @staticmethod
    def _failure(message: str) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.FAILED,
            reason=message,
            mechanism=MECHANISM,
        )
