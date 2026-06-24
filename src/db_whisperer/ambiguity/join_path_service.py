"""Schema-graph join-path ambiguity detection (Component B, primary mechanism).

The flow follows the project's primary ambiguity mechanism:

1. The LLM extracts the entities a question mentions and maps them to tables.
2. The deterministic schema graph enumerates the distinct join paths between
   each pair of mentioned tables.
3. When an entity pair has more than one distinct path, the request is
   ambiguous: the same wording maps to different joins with different results.
   The LLM (with a deterministic fallback) writes one two-option clarification.

When nothing is ambiguous -- a single path, fewer than two entity tables, or no
graph at all -- the detector returns a pass so the application proceeds to
normal SQL generation. Any failure is surfaced as a failed decision rather than
a silent guess; the application then falls back to its candidate-comparison
judge.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from db_whisperer.ambiguity.join_path_prompt_builder import (
    JoinPathPromptBuilder,
)
from db_whisperer.ambiguity.openrouter_client import (
    AmbiguityJudgeError,
    AmbiguityOpenRouterClient,
)
from db_whisperer.contracts import (
    AmbiguityDecision,
    ComponentState,
    JoinPath,
    JoinPathRequest,
)
from db_whisperer.schema_graph import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_PATHS,
    JoinPathEnumeration,
    SchemaGraph,
    entity_table_pairs,
)


MECHANISM = "join-path"
DEFAULT_MAX_ENTITY_TABLES = 6


@dataclass(frozen=True)
class _AmbiguousPair:
    """One entity-table pair connected by more than one distinct join path."""

    source: str
    target: str
    enumeration: JoinPathEnumeration


class JoinPathAmbiguityService:
    """Detect join-path multiplicity between the entities a question mentions."""

    def __init__(
        self,
        client: AmbiguityOpenRouterClient | None = None,
        prompt_builder: JoinPathPromptBuilder | None = None,
        max_hops: int = DEFAULT_MAX_HOPS,
        max_paths: int = DEFAULT_MAX_PATHS,
        max_entity_tables: int = DEFAULT_MAX_ENTITY_TABLES,
    ) -> None:
        if max_entity_tables < 2:
            raise ValueError("max_entity_tables must be at least 2.")
        self.client = client or AmbiguityOpenRouterClient()
        self.prompt_builder = prompt_builder or JoinPathPromptBuilder()
        self.max_hops = max_hops
        self.max_paths = max_paths
        self.max_entity_tables = max_entity_tables

    def detect(self, request: JoinPathRequest) -> AmbiguityDecision:
        """Return a join-path clarification, a pass, or a failure."""
        validation_error = self._validate_request(request)
        if validation_error:
            return self._failure(validation_error)

        graph = SchemaGraph.from_schema(
            request.schema,
            max_hops=self.max_hops,
            max_paths=self.max_paths,
        )
        if len(graph.tables) < 2 or not graph.edges:
            return self._pass(
                "Schema graph has no joinable tables; join-path detection "
                "skipped."
            )

        # Cheap deterministic pre-check: if no table pair anywhere is connected
        # by more than one path, join-path ambiguity is impossible, so skip the
        # entity-extraction LLM call entirely (common for tree-shaped schemas).
        if not graph.has_ambiguous_pair():
            return self._pass(
                "No table pair in the schema graph has more than one join "
                "path; join-path ambiguity is impossible."
            )

        try:
            entity_judgment = self.client.evaluate(
                prompt=self.prompt_builder.build_entity_prompt(request),
                api_key=request.api_key,
                model=request.model,
            )
        except (AmbiguityJudgeError, ValueError) as error:
            return self._failure(f"Entity extraction failed: {error}")

        entity_tables, dropped, capped = self._entity_tables(
            entity_judgment, graph
        )
        context_note = self._context_note(dropped, capped)
        if entity_tables is None:
            return self._failure(
                "Entity extraction returned no usable entities list."
            )
        if len(entity_tables) < 2:
            return self._pass(
                self._join_text(
                    "Fewer than two distinct entity tables were referenced; "
                    "no join-path ambiguity is possible.",
                    context_note,
                )
            )

        ambiguous_pairs, any_truncated = self._ambiguous_pairs(
            graph, entity_tables
        )
        if not ambiguous_pairs:
            return self._pass(
                self._join_text(
                    "At most one join path connects the mentioned entities.",
                    context_note,
                )
            )

        # Exclude pairs already resolved by a previous clarification so a
        # multi-entity question keeps clarifying remaining pairs across rounds
        # instead of proceeding to SQL with known join-path ambiguity left over.
        unsettled = self._unsettled_pairs(ambiguous_pairs, request.clarifications)
        if not unsettled:
            return self._pass(
                self._join_text(
                    "All join-path ambiguities between the mentioned entities "
                    "have already been clarified.",
                    context_note,
                )
            )

        chosen = self._choose_pair(unsettled)
        interpretations, extra_paths = self._select_two_paths(
            chosen.enumeration.paths
        )
        question, options, used_llm = self._clarification(
            request, chosen, interpretations
        )
        # Name both tables in the question so the next round can recognise this
        # pair as settled from the accumulated clarifications.
        question = self._ensure_pair_named(
            question, chosen.source, chosen.target
        )
        reason = self._reason(
            chosen,
            extra_paths,
            len(unsettled) - 1,
            context_note,
            used_llm,
            any_truncated or chosen.enumeration.truncated,
            not request.schema.discovery_complete,
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
    def _validate_request(request: JoinPathRequest) -> str | None:
        if not request.user_query.strip():
            return "User query is required."
        if not request.api_key.strip():
            return "OpenRouter API key is required."
        if not request.model.strip():
            return "OpenRouter model is required."
        if request.schema is None:
            return "Schema metadata is required."
        return None

    # -- Entity mapping --------------------------------------------------

    def _entity_tables(
        self,
        judgment: dict[str, object],
        graph: SchemaGraph,
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...], bool]:
        """Extract the distinct, known tables the entities mapped to.

        Returns ``(tables, dropped, capped)``. ``tables`` is ``None`` only when
        the response is structurally invalid. ``dropped`` lists table names the
        model returned that are not in the graph (reported, never guessed).
        ``capped`` is ``True`` when more known tables were referenced than the
        ``max_entity_tables`` analysis cap, so the cut is never silent.
        """
        entities = judgment.get("entities")
        if not isinstance(entities, list):
            return None, (), False

        ordered: list[str] = []
        dropped: list[str] = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            table = entity.get("table")
            if not isinstance(table, str) or not table.strip():
                continue
            name = table.strip()
            if not graph.has_table(name):
                if name not in dropped:
                    dropped.append(name)
                continue
            if name not in ordered:
                ordered.append(name)
        capped = len(ordered) > self.max_entity_tables
        return tuple(ordered[: self.max_entity_tables]), tuple(dropped), capped

    def _context_note(self, dropped: tuple[str, ...], capped: bool) -> str:
        """Render visible notes for dropped (hallucinated) and capped tables."""
        parts: list[str] = []
        if dropped:
            parts.append(
                f"Ignored {len(dropped)} table name(s) the model referenced "
                f"that are not in the schema: {', '.join(dropped)}."
            )
        if capped:
            parts.append(
                f"More than {self.max_entity_tables} entity tables were "
                f"referenced; only the first {self.max_entity_tables} were "
                "analyzed, so some join-path ambiguity may be unreported."
            )
        return " ".join(parts)

    @staticmethod
    def _join_text(*parts: str) -> str:
        """Join non-empty text fragments with single spaces."""
        return " ".join(part for part in parts if part)

    # -- Path analysis ---------------------------------------------------

    def _ambiguous_pairs(
        self,
        graph: SchemaGraph,
        entity_tables: tuple[str, ...],
    ) -> tuple[list[_AmbiguousPair], bool]:
        ambiguous: list[_AmbiguousPair] = []
        any_truncated = False
        for source, target in entity_table_pairs(entity_tables):
            enumeration = graph.enumerate_join_paths(source, target)
            any_truncated = any_truncated or enumeration.truncated
            if enumeration.is_ambiguous:
                ambiguous.append(
                    _AmbiguousPair(
                        source=source,
                        target=target,
                        enumeration=enumeration,
                    )
                )
        return ambiguous, any_truncated

    @staticmethod
    def _choose_pair(pairs: list[_AmbiguousPair]) -> _AmbiguousPair:
        """Pick the most ambiguous pair (most paths), then by name order."""
        return min(
            pairs,
            key=lambda pair: (
                -len(pair.enumeration.paths),
                pair.source,
                pair.target,
            ),
        )

    @classmethod
    def _unsettled_pairs(
        cls,
        pairs: list[_AmbiguousPair],
        clarifications: tuple[str, ...],
    ) -> list[_AmbiguousPair]:
        """Drop pairs both of whose tables were named in one prior answer."""
        return [
            pair
            for pair in pairs
            if not cls._pair_settled(pair.source, pair.target, clarifications)
        ]

    @classmethod
    def _pair_settled(
        cls,
        source: str,
        target: str,
        clarifications: tuple[str, ...],
    ) -> bool:
        """True if a single clarification already named both pair tables."""
        return any(
            cls._names_token(clarification, source)
            and cls._names_token(clarification, target)
            for clarification in clarifications
        )

    @staticmethod
    def _names_token(text: str, name: str) -> bool:
        """Match ``name`` as a whole identifier token (so 'order' != 'order_id')."""
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
        return re.search(pattern, text) is not None

    @classmethod
    def _ensure_pair_named(cls, question: str, source: str, target: str) -> str:
        """Append a context clause unless the question already names both tables."""
        if cls._names_token(question, source) and cls._names_token(
            question, target
        ):
            return question
        return (
            f'{question} (clarifying how "{source}" and "{target}" connect)'
        )

    @staticmethod
    def _select_two_paths(
        paths: tuple[JoinPath, ...],
    ) -> tuple[tuple[JoinPath, JoinPath], int]:
        """Return the two most distinct paths (shortest and longest)."""
        # ``paths`` is already sorted shortest-first by the enumerator.
        first = paths[0]
        second = paths[-1]
        extra = max(0, len(paths) - 2)
        return (first, second), extra

    # -- Clarification ---------------------------------------------------

    def _clarification(
        self,
        request: JoinPathRequest,
        pair: _AmbiguousPair,
        interpretations: tuple[JoinPath, JoinPath],
    ) -> tuple[str, tuple[str, str], bool]:
        """Generate the question and two options, LLM-first with a fallback."""
        try:
            judgment = self.client.evaluate(
                prompt=self.prompt_builder.build_clarification_prompt(
                    request,
                    pair.source,
                    pair.target,
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

        question, options = self._fallback_clarification(
            pair.source, pair.target, interpretations
        )
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
        source: str,
        target: str,
        interpretations: tuple[JoinPath, JoinPath],
    ) -> tuple[str, tuple[str, str]]:
        """Build a transparent question directly from the two real paths."""
        labels = [
            cls._path_label(path) for path in interpretations
        ]
        if labels[0].casefold() == labels[1].casefold():
            labels = [
                f"{label} (option {index})"
                for index, label in enumerate(labels, start=1)
            ]
        question = (
            f'There is more than one way to connect "{source}" and '
            f'"{target}". Which connection do you mean?'
        )
        return question, (labels[0], labels[1])

    @staticmethod
    def _path_label(path: JoinPath) -> str:
        intermediates = path.intermediate_tables
        if intermediates:
            return "Linked through " + ", ".join(intermediates)
        if path.relationships:
            # A direct (single-edge) path: name the join key so two parallel
            # direct edges (e.g. sender_id vs recipient_id) stay distinct and
            # meaningful instead of collapsing to "Linked directly".
            edge = path.relationships[0]
            return f"Linked directly on {edge.child_table}.{edge.child_column}"
        return "Linked directly"

    # -- Reason / outcomes ----------------------------------------------

    @staticmethod
    def _reason(
        pair: _AmbiguousPair,
        extra_paths: int,
        other_pairs: int,
        context_note: str,
        used_llm: bool,
        truncated: bool,
        discovery_incomplete: bool,
    ) -> str:
        parts = [
            f"Found {len(pair.enumeration.paths)} distinct join paths between "
            f"'{pair.source}' and '{pair.target}'."
        ]
        if extra_paths:
            parts.append(
                f"Presented the two most distinct of "
                f"{len(pair.enumeration.paths)} paths."
            )
        if other_pairs:
            parts.append(
                f"{other_pairs} other entity pair(s) are also ambiguous and "
                "will be clarified in following rounds."
            )
        if not used_llm:
            parts.append(
                "Used a deterministic clarification because the model did not "
                "return a usable question."
            )
        if truncated:
            parts.append(
                "Path enumeration hit its limit; more paths may exist."
            )
        if discovery_incomplete:
            parts.append(
                "Relationship discovery was incomplete, so some paths may be "
                "missing."
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
