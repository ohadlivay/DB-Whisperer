"""Unified LLM-based ambiguity evaluation."""

from __future__ import annotations

import re

from db_whisperer.ambiguity.candidate_alternatives import (
    CandidateAlternative,
    cluster_executed_pairs,
)
from db_whisperer.ambiguity.openrouter_client import (
    AmbiguityJudgeError,
    AmbiguityOpenRouterClient,
)
from db_whisperer.ambiguity.prompt_builder import AmbiguityPromptBuilder
from db_whisperer.contracts import (
    AmbiguityDecision,
    AmbiguityRequest,
    ComponentState,
    SemanticAmbiguityTerm,
    SemanticInterpretation,
)


class AmbiguityService:
    """Make one decision from primary candidate and supporting schema evidence."""

    def __init__(
        self,
        client: AmbiguityOpenRouterClient | None = None,
        prompt_builder: AmbiguityPromptBuilder | None = None,
    ) -> None:
        self.client = client or AmbiguityOpenRouterClient()
        self.prompt_builder = prompt_builder or AmbiguityPromptBuilder()

    def evaluate(self, request: AmbiguityRequest) -> AmbiguityDecision:
        validation_error = self._validate_request(request)
        if validation_error:
            return self._failure(validation_error)

        clusters = cluster_executed_pairs(request.pairs)
        semantic_available = bool(
            self.prompt_builder.include_semantic_findings
            and request.semantic_analysis is not None
            and request.semantic_analysis.ambiguous
        )
        candidate_diversity = bool(
            self.prompt_builder.include_candidate_evidence
            and len(clusters) >= 2
        )
        candidate_support = self._candidate_support(clusters)
        if (
            not candidate_diversity
            and not semantic_available
            and not request.clarifications
        ):
            return AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason=(
                    "Ambiguity check skipped because fewer than two unique "
                    "alternatives remained and no semantic finding exists."
                ),
                candidate_support=candidate_support,
            )

        try:
            judgment = self.client.evaluate(
                prompt=self.prompt_builder.build(request),
                api_key=request.api_key,
                model=request.model,
            )
            return self._parse_judgment(
                judgment,
                request=request,
                has_candidate_diversity=candidate_diversity,
                semantic_available=semantic_available,
                clusters=clusters,
            )
        except (AmbiguityJudgeError, ValueError) as error:
            return self._failure(
                str(error),
                candidate_support=candidate_support,
            )

    @staticmethod
    def _validate_request(request: AmbiguityRequest) -> str | None:
        if not request.user_query.strip():
            return "User query is required."
        if not request.pairs:
            return "At least one executed SQL/table pair is required."
        if len(request.pairs) < 2 and not request.clarifications:
            return (
                "At least two executed SQL/table pairs are required before "
                "a clarification has been selected."
            )
        if not request.api_key.strip():
            return "OpenRouter API key is required."
        if not request.model.strip():
            return "OpenRouter model is required."
        candidate_ids = [pair.candidate_id for pair in request.pairs]
        if len(set(candidate_ids)) != len(candidate_ids):
            return "Candidate IDs must be unique."
        for pair in request.pairs:
            if not pair.candidate_id.strip():
                return "Every pair requires a candidate ID."
            if not pair.sql.strip():
                return "Every pair requires SQL."
            if any(len(row) != len(pair.columns) for row in pair.rows):
                return f"Pair {pair.candidate_id} has malformed table rows."
        return None

    @classmethod
    def _parse_judgment(
        cls,
        judgment: dict[str, object],
        request: AmbiguityRequest,
        has_candidate_diversity: bool,
        semantic_available: bool,
        clusters: tuple[CandidateAlternative, ...],
    ) -> AmbiguityDecision:
        candidate_support = cls._candidate_support(clusters)
        status = judgment.get("status")
        reason = judgment.get("reason", "")
        if not isinstance(reason, str):
            return cls._failure("Ambiguity reason must be text.")
        compliance = cls._parse_compliance(
            judgment,
            clusters,
            required=bool(request.clarifications),
        )
        if isinstance(compliance, str):
            return cls._failure(
                compliance,
                candidate_support=candidate_support,
            )
        compliance_passed, compliant_ids, rejected = compliance
        if status == "noncompliant":
            if not request.clarifications:
                return cls._failure(
                    "Noncompliant status requires previous clarifications.",
                    candidate_support=candidate_support,
                )
            if compliance_passed:
                return cls._failure(
                    "Noncompliant status cannot include a compliant alternative.",
                    candidate_support=candidate_support,
                )
            return AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=None,
                reason=reason.strip(),
                candidate_support=candidate_support,
                compliance_passed=False,
                rejected_alternatives=rejected,
            )
        if request.clarifications and not compliance_passed:
            return cls._failure(
                "A judgment with no compliant alternatives must use status "
                "noncompliant.",
                candidate_support=candidate_support,
            )
        if status == "pass":
            return AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason=reason.strip(),
                candidate_support=candidate_support,
                compliance_passed=compliance_passed,
                compliant_alternatives=compliant_ids,
                rejected_alternatives=rejected,
            )
        if status != "clarify":
            return cls._failure("Ambiguity judgment status must be pass or clarify.")

        source = judgment.get("source")
        if source not in {"candidate-comparison", "semantic-column"}:
            return cls._failure(
                "Clarification source must be candidate-comparison or "
                "semantic-column."
            )
        if source == "candidate-comparison" and not has_candidate_diversity:
            return cls._failure(
                "Candidate clarification requires two unique alternatives."
            )
        if source == "semantic-column" and not semantic_available:
            return cls._failure(
                "Semantic clarification requires a validated semantic finding."
            )

        question = judgment.get("question")
        options = judgment.get("options")
        if not isinstance(question, str) or not question.strip():
            return cls._failure("Clarification judgment requires one question.")
        if not isinstance(options, list) or len(options) != 2:
            return cls._failure(
                "Clarification judgment requires exactly two options."
            )
        if not all(isinstance(option, str) and option.strip() for option in options):
            return cls._failure("Clarification options must be non-empty text.")
        normalized_options = tuple(str(option).strip() for option in options)
        if normalized_options[0].casefold() == normalized_options[1].casefold():
            return cls._failure("Clarification options must be distinct.")

        normalized_question = question.strip()
        evidence_columns: tuple[str, ...] = ()
        evidence_interpretations: tuple[str, ...] = ()
        evidence_dimension = ""
        evidence_alternatives: tuple[str, ...] = ()
        candidate_rejection_reason = ""
        if source == "candidate-comparison":
            selected_alternatives = cls._candidate_alternatives(
                clusters,
                judgment.get("alternative_ids"),
            )
            if selected_alternatives is None:
                return cls._failure(
                    "Candidate clarification must select exactly two distinct "
                    "alternative IDs from the executed evidence.",
                    candidate_support=candidate_support,
                )
            if compliant_ids and any(
                cluster.alternative_id not in compliant_ids
                for cluster in selected_alternatives
            ):
                return cls._failure(
                    "Candidate clarification may reference only alternatives "
                    "that apply every previous clarification.",
                    candidate_support=candidate_support,
                )
            evidence_alternatives = tuple(
                cluster.alternative_id for cluster in selected_alternatives
            )
        if source == "semantic-column":
            if has_candidate_diversity:
                raw_rejection = judgment.get("candidate_rejection_reason")
                if not isinstance(raw_rejection, str) or not raw_rejection.strip():
                    return cls._failure(
                        "Semantic clarification must explain why candidate "
                        "differences failed the plausibility gate.",
                        candidate_support=candidate_support,
                    )
                candidate_rejection_reason = raw_rejection.strip()
            term = cls._semantic_finding(
                request,
                judgment.get("semantic_finding_id"),
            )
            if term is None:
                return cls._failure(
                    "Semantic clarification must name an exact finding ID."
                )
            selected = cls._semantic_interpretations(
                term,
                judgment.get("interpretation_ids"),
            )
            if selected is None:
                return cls._failure(
                    "Semantic clarification must select exactly two distinct "
                    "interpretation IDs from the named finding."
                )
            first, second = selected
            evidence_interpretations = (
                first.interpretation_id,
                second.interpretation_id,
            )
            evidence_dimension = term.dimension
            evidence_columns = tuple(dict.fromkeys((
                *first.grounding.columns,
                *second.grounding.columns,
            )))
            normalized_question += (
                " [grounding: "
                + ", ".join(f'"{column}"' for column in evidence_columns)
                + "]"
            )

        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=normalized_question,
            options=normalized_options,
            reason=reason.strip(),
            mechanism=str(source),
            evidence_columns=evidence_columns,
            evidence_interpretations=evidence_interpretations,
            evidence_dimension=evidence_dimension,
            evidence_alternatives=evidence_alternatives,
            candidate_support=candidate_support,
            candidate_rejection_reason=candidate_rejection_reason,
            compliance_passed=compliance_passed,
            compliant_alternatives=compliant_ids,
            rejected_alternatives=rejected,
        )

    @staticmethod
    def _parse_compliance(
        judgment: dict[str, object],
        clusters: tuple[CandidateAlternative, ...],
        required: bool,
    ) -> tuple[
        bool | None,
        tuple[str, ...],
        tuple[tuple[str, str], ...],
    ] | str:
        raw = judgment.get("compliance")
        if not required:
            if raw is not None:
                return (
                    "Compliance classifications are allowed only when "
                    "previous clarifications exist."
                )
            return None, (), ()
        if not isinstance(raw, list) or len(raw) != len(clusters):
            return (
                "Clarified judgments require exactly one compliance item "
                "for every executed alternative."
            )

        known_ids = {cluster.alternative_id for cluster in clusters}
        seen: set[str] = set()
        compliant: list[str] = []
        rejected: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                return "Every compliance item must be an object."
            alternative_id = item.get("alternative_id")
            applies_all = item.get("applies_all")
            item_reason = item.get("reason")
            if (
                not isinstance(alternative_id, str)
                or alternative_id not in known_ids
            ):
                return (
                    "Every compliance item must use an exact executed "
                    "alternative ID."
                )
            if alternative_id in seen:
                return "Compliance alternative IDs must be unique."
            if not isinstance(applies_all, bool):
                return "Compliance applies_all values must be boolean."
            if not isinstance(item_reason, str) or not item_reason.strip():
                return "Every compliance item requires a grounded reason."
            seen.add(alternative_id)
            if applies_all:
                compliant.append(alternative_id)
            else:
                rejected.append((alternative_id, item_reason.strip()))
        if seen != known_ids:
            return "Compliance must classify every executed alternative."
        return bool(compliant), tuple(compliant), tuple(rejected)

    @staticmethod
    def _candidate_alternatives(
        clusters: tuple[CandidateAlternative, ...],
        raw_ids: object,
    ) -> tuple[CandidateAlternative, CandidateAlternative] | None:
        if not isinstance(raw_ids, list) or len(raw_ids) != 2:
            return None
        if not all(isinstance(value, str) and value.strip() for value in raw_ids):
            return None
        normalized = tuple(str(value).strip() for value in raw_ids)
        if normalized[0] == normalized[1]:
            return None
        known = {cluster.alternative_id: cluster for cluster in clusters}
        if any(value not in known for value in normalized):
            return None
        return known[normalized[0]], known[normalized[1]]

    @staticmethod
    def _candidate_support(
        clusters: tuple[CandidateAlternative, ...],
    ) -> tuple[tuple[str, int], ...]:
        return tuple(
            (cluster.alternative_id, cluster.support_count)
            for cluster in clusters
        )

    @staticmethod
    def _semantic_finding(
        request: AmbiguityRequest,
        raw_id: object,
    ) -> SemanticAmbiguityTerm | None:
        if not isinstance(raw_id, str) or request.semantic_analysis is None:
            return None
        match = re.fullmatch(r"semantic_([1-9][0-9]*)", raw_id.strip())
        if match is None:
            return None
        index = int(match.group(1)) - 1
        if index >= len(request.semantic_analysis.terms):
            return None
        return request.semantic_analysis.terms[index]

    @staticmethod
    def _semantic_interpretations(
        term: SemanticAmbiguityTerm,
        raw_ids: object,
    ) -> tuple[SemanticInterpretation, SemanticInterpretation] | None:
        if not isinstance(raw_ids, list) or len(raw_ids) != 2:
            return None
        if not all(
            isinstance(value, str) and value.strip()
            for value in raw_ids
        ):
            return None
        normalized = tuple(str(value).strip() for value in raw_ids)
        if normalized[0] == normalized[1]:
            return None
        known = {
            interpretation.interpretation_id: interpretation
            for interpretation in term.interpretations
        }
        if any(value not in known for value in normalized):
            return None
        return known[normalized[0]], known[normalized[1]]

    @staticmethod
    def _failure(
        message: str,
        candidate_support: tuple[tuple[str, int], ...] = (),
    ) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.FAILED,
            reason=message,
            candidate_support=candidate_support,
        )
