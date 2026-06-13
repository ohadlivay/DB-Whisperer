"""LLM-based ambiguity evaluation."""

from __future__ import annotations

from db_whisperer.ambiguity.openrouter_client import (
    AmbiguityJudgeError,
    AmbiguityOpenRouterClient,
)
from db_whisperer.ambiguity.prompt_builder import AmbiguityPromptBuilder
from db_whisperer.contracts import (
    AmbiguityDecision,
    AmbiguityRequest,
    ComponentState,
)


class AmbiguityService:
    """Judge K executed SQL/table alternatives for material ambiguity."""

    def __init__(
        self,
        client: AmbiguityOpenRouterClient | None = None,
        prompt_builder: AmbiguityPromptBuilder | None = None,
    ) -> None:
        self.client = client or AmbiguityOpenRouterClient()
        self.prompt_builder = prompt_builder or AmbiguityPromptBuilder()

    def evaluate(self, request: AmbiguityRequest) -> AmbiguityDecision:
        """Return pass or one question with exactly two options."""
        validation_error = self._validate_request(request)
        if validation_error:
            return self._failure(validation_error)

        try:
            prompt = self.prompt_builder.build(request)
            judgment = self.client.evaluate(
                prompt=prompt,
                api_key=request.api_key,
                model=request.model,
            )
            return self._parse_judgment(judgment)
        except (AmbiguityJudgeError, ValueError) as error:
            return self._failure(str(error))

    @staticmethod
    def _validate_request(request: AmbiguityRequest) -> str | None:
        if not request.user_query.strip():
            return "User query is required."
        if len(request.pairs) < 2:
            return "At least two executed SQL/table pairs are required."
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
    ) -> AmbiguityDecision:
        status = judgment.get("status")
        reason = judgment.get("reason", "")
        if not isinstance(reason, str):
            return cls._failure("Ambiguity reason must be text.")

        if status == "pass":
            return AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=True,
                reason=reason.strip(),
            )

        if status == "clarify":
            question = judgment.get("question")
            if not isinstance(question, str) or not question.strip():
                return cls._failure(
                    "Clarification judgment requires one question."
                )
            options = judgment.get("options")
            if not isinstance(options, list) or len(options) != 2:
                return cls._failure(
                    "Clarification judgment requires exactly two options."
                )
            if not all(
                isinstance(option, str) and option.strip()
                for option in options
            ):
                return cls._failure(
                    "Clarification options must be non-empty text."
                )
            normalized_options = tuple(
                option.strip()
                for option in options
            )
            if (
                normalized_options[0].casefold()
                == normalized_options[1].casefold()
            ):
                return cls._failure(
                    "Clarification options must be distinct."
                )
            return AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question=question.strip(),
                options=normalized_options,
                reason=reason.strip(),
            )

        return cls._failure(
            "Ambiguity judgment status must be pass or clarify."
        )

    @staticmethod
    def _failure(message: str) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.FAILED,
            reason=message,
        )
