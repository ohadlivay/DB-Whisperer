"""Pure session logic for the human-in-the-loop study GUI.

Kept free of Streamlit so the counterbalancing, scoring, and record building can
be unit tested without a browser. The GUI (``study_app.py``) owns rendering and
session state; everything here is deterministic given a participant id.

A participant sees each task exactly once. Half the tasks are shown by the
*asking* version (full pipeline, may ask one clarifying question) and half by the
*direct* version (single-pass baseline, never asks); the split and the task order
are balanced and seeded by the participant id, so a run is reproducible and the
version comparison is counterbalanced across tasks. For an ambiguous task the
participant is given one of the two sibling goals (also seeded), so the goal text
never reveals which interpretation is "expected".
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from random import Random
from typing import Any


VERSION_ASKING = "asking"
VERSION_DIRECT = "direct"


@dataclass(frozen=True)
class TaskInstance:
    """One task as a specific participant will see it."""

    task_id: str
    dataset: str
    version: str
    ambiguous: bool
    question: str
    clarification_question: str
    goal_id: str
    goal_text: str
    correct_key: str
    baseline_pick: str
    interpretations: tuple[dict[str, Any], ...]

    @property
    def asks_question(self) -> bool:
        """True only when this instance will show a clarifying question."""
        return self.version == VERSION_ASKING and self.ambiguous

    def option_keys(self) -> tuple[str, ...]:
        return tuple(item["key"] for item in self.interpretations)

    def option_label(self, key: str) -> str:
        return self._interpretation(key).get("option_label", "")

    def answer_for(self, key: str) -> dict[str, Any]:
        return self._interpretation(key)["answer"]

    def displayed_key(self, chosen_key: str | None) -> str:
        """The interpretation actually shown to the participant.

        The asking version uses the participant's clicked option; the direct
        version uses the baseline's assumed single guess. A control task has one
        interpretation, so both versions show it.
        """
        if self.asks_question:
            if chosen_key is None:
                raise ValueError("An asking task needs a chosen option.")
            return chosen_key
        return self.baseline_pick

    def is_correct(self, chosen_key: str | None) -> bool:
        """Did the answer the participant ended up with match their goal?"""
        return self.displayed_key(chosen_key) == self.correct_key

    def _interpretation(self, key: str) -> dict[str, Any]:
        for item in self.interpretations:
            if item["key"] == key:
                return item
        raise KeyError(f"Unknown interpretation key: {key!r}")


def load_scenarios(path: Path) -> tuple[dict[str, Any], ...]:
    """Load and lightly validate the generated scenario file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("scenarios.json must contain a non-empty 'scenarios' list.")
    return tuple(scenarios)


def filter_scenarios_by_dataset(
    scenarios: tuple[dict[str, Any], ...],
    allowed: list[str] | None,
) -> tuple[dict[str, Any], ...]:
    """Keep only scenarios whose dataset is in ``allowed``; ``None`` keeps all.

    A public deployment sets this (e.g. to ``BikeStores`` only) so an open,
    unscreened link never serves the clinical MIMIC tasks, which need a
    clinically-literate rater to judge.
    """
    if not allowed:
        return tuple(scenarios)
    keep = {name for name in allowed if name}
    return tuple(s for s in scenarios if s.get("dataset") in keep)


def _seed(participant_id: str) -> int:
    digest = hashlib.sha256(participant_id.strip().encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def assign_versions(count: int, rng: Random) -> list[str]:
    """Balanced asking/direct labels for ``count`` tasks, then shuffled.

    With an odd count the extra task is assigned at random, so no version is
    systematically favoured across participants.
    """
    half = count // 2
    labels = [VERSION_ASKING] * half + [VERSION_DIRECT] * half
    if len(labels) < count:
        labels.append(rng.choice((VERSION_ASKING, VERSION_DIRECT)))
    rng.shuffle(labels)
    return labels


def build_plan(
    scenarios: tuple[dict[str, Any], ...],
    participant_id: str,
) -> tuple[TaskInstance, ...]:
    """Deterministically lay out one participant's session.

    Versions are balanced *within* each (dataset, ambiguity) stratum, not just
    overall, so a participant always contributes both an asking and a direct
    trial for ambiguous tasks and for control tasks. A single overall balance
    could otherwise hand one participant every ambiguous task in the same
    version, contributing no within-cell comparison.
    """
    rng = Random(_seed(participant_id))

    strata: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for scenario in scenarios:
        strata.setdefault(
            (scenario["dataset"], scenario["ambiguous"]), []
        ).append(scenario)
    version_by_id: dict[str, str] = {}
    for key in sorted(strata):
        group = strata[key]
        for scenario, version in zip(
            group, assign_versions(len(group), rng), strict=True
        ):
            version_by_id[scenario["id"]] = version

    order = list(scenarios)
    rng.shuffle(order)

    plan: list[TaskInstance] = []
    for scenario in order:
        goals = scenario["goals"]
        goal = goals[rng.randrange(len(goals))] if len(goals) > 1 else goals[0]
        plan.append(
            TaskInstance(
                task_id=scenario["id"],
                dataset=scenario["dataset"],
                version=version_by_id[scenario["id"]],
                ambiguous=scenario["ambiguous"],
                question=scenario["question"],
                clarification_question=scenario.get("clarification_question", ""),
                goal_id=goal["goal_id"],
                goal_text=goal["text"],
                correct_key=goal["correct_key"],
                baseline_pick=scenario["baseline_pick"],
                interpretations=tuple(scenario["interpretations"]),
            )
        )
    return tuple(plan)


def make_task_record(
    participant_id: str,
    instance: TaskInstance,
    position: int,
    chosen_key: str | None,
    trust: int,
    clarity: int | None,
    naturalness: int | None,
    elapsed_seconds: float | None,
    timestamp: str,
) -> dict[str, Any]:
    """Build one JSON-serialisable result row for a completed task.

    ``correct`` and ``comprehension`` are recorded objectively so analysis never
    has to re-derive them: ``comprehension`` is whether the clicked option
    matched the goal (asking + ambiguous only); ``correct`` is whether the final
    answer matched the goal in either version.
    """
    comprehension: bool | None = None
    if instance.asks_question:
        comprehension = chosen_key == instance.correct_key
    return {
        "participant_id": participant_id,
        "position": position,
        "task_id": instance.task_id,
        "dataset": instance.dataset,
        "version": instance.version,
        "ambiguous": instance.ambiguous,
        "goal_id": instance.goal_id,
        "correct_key": instance.correct_key,
        "asked": instance.asks_question,
        "chosen_key": chosen_key,
        "displayed_key": instance.displayed_key(chosen_key),
        "correct": instance.is_correct(chosen_key),
        "comprehension": comprehension,
        "trust": trust,
        "clarity": clarity,
        "naturalness": naturalness,
        "elapsed_seconds": elapsed_seconds,
        "timestamp": timestamp,
    }
