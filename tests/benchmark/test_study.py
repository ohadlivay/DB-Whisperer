"""Tests for the human-in-the-loop study logic and GUI smoke."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
STUDY_DIR = ROOT / "benchmark" / "study"
sys.path.insert(0, str(STUDY_DIR))

import study_logic  # noqa: E402
from study_logic import (  # noqa: E402
    TaskInstance,
    VERSION_ASKING,
    VERSION_DIRECT,
    build_plan,
    load_scenarios,
    make_task_record,
)


SCENARIOS_PATH = STUDY_DIR / "scenarios.json"


def _answer(value: int) -> dict:
    return {"columns": ["n"], "rows": [[value]]}


def _ambiguous(task_id: str) -> dict:
    return {
        "id": task_id,
        "dataset": "D",
        "ambiguous": True,
        "question": f"q-{task_id}",
        "clarification_question": "which?",
        "baseline_pick": "a",
        "interpretations": [
            {"key": "a", "option_label": "A", "answer": _answer(1)},
            {"key": "b", "option_label": "B", "answer": _answer(2)},
        ],
        "goals": [
            {"goal_id": f"{task_id}-a", "correct_key": "a", "text": "goal A"},
            {"goal_id": f"{task_id}-b", "correct_key": "b", "text": "goal B"},
        ],
    }


def _control(task_id: str) -> dict:
    return {
        "id": task_id,
        "dataset": "D",
        "ambiguous": False,
        "question": f"q-{task_id}",
        "clarification_question": "",
        "baseline_pick": "t",
        "interpretations": [{"key": "t", "option_label": "", "answer": _answer(9)}],
        "goals": [{"goal_id": f"{task_id}-t", "correct_key": "t", "text": "goal T"}],
    }


FIXTURE = (
    _ambiguous("amb1"),
    _ambiguous("amb2"),
    _control("ctl1"),
    _control("ctl2"),
)


class TaskInstanceTest(unittest.TestCase):
    def _instance(self, version: str, ambiguous: bool, correct_key: str) -> TaskInstance:
        scenario = _ambiguous("t") if ambiguous else _control("t")
        return TaskInstance(
            task_id="t",
            dataset="D",
            version=version,
            ambiguous=ambiguous,
            question="q",
            clarification_question="which?",
            goal_id="g",
            goal_text="goal",
            correct_key=correct_key,
            baseline_pick=scenario["baseline_pick"],
            interpretations=tuple(scenario["interpretations"]),
        )

    def test_only_asking_ambiguous_asks(self) -> None:
        self.assertTrue(self._instance(VERSION_ASKING, True, "a").asks_question)
        self.assertFalse(self._instance(VERSION_DIRECT, True, "a").asks_question)
        self.assertFalse(self._instance(VERSION_ASKING, False, "t").asks_question)

    def test_displayed_key_follows_version(self) -> None:
        asking = self._instance(VERSION_ASKING, True, "a")
        self.assertEqual(asking.displayed_key("b"), "b")  # the click wins
        direct = self._instance(VERSION_DIRECT, True, "a")
        self.assertEqual(direct.displayed_key(None), "a")  # baseline guess

    def test_asking_requires_a_choice(self) -> None:
        with self.assertRaises(ValueError):
            self._instance(VERSION_ASKING, True, "a").displayed_key(None)

    def test_correctness(self) -> None:
        asking = self._instance(VERSION_ASKING, True, "b")
        self.assertTrue(asking.is_correct("b"))
        self.assertFalse(asking.is_correct("a"))
        # The direct version is right only when its blind guess (baseline_pick
        # = "a") happens to match the goal.
        self.assertTrue(self._instance(VERSION_DIRECT, True, "a").is_correct(None))
        self.assertFalse(self._instance(VERSION_DIRECT, True, "b").is_correct(None))


class BuildPlanTest(unittest.TestCase):
    def test_covers_every_task_once(self) -> None:
        plan = build_plan(FIXTURE, "participant-1")
        self.assertEqual(len(plan), len(FIXTURE))
        self.assertEqual(
            {instance.task_id for instance in plan},
            {scenario["id"] for scenario in FIXTURE},
        )

    def test_is_deterministic_per_participant(self) -> None:
        first = build_plan(FIXTURE, "alice")
        second = build_plan(FIXTURE, "alice")
        self.assertEqual(
            [(i.task_id, i.version, i.goal_id) for i in first],
            [(i.task_id, i.version, i.goal_id) for i in second],
        )

    def test_versions_are_balanced(self) -> None:
        plan = build_plan(FIXTURE, "participant-xyz")
        counts = Counter(instance.version for instance in plan)
        self.assertEqual(counts[VERSION_ASKING], 2)
        self.assertEqual(counts[VERSION_DIRECT], 2)

    def test_goal_is_a_valid_interpretation(self) -> None:
        plan = build_plan(FIXTURE, "p")
        for instance in plan:
            self.assertIn(instance.correct_key, instance.option_keys())


class MakeRecordTest(unittest.TestCase):
    def _instance(self, version: str) -> TaskInstance:
        return TaskInstance(
            task_id="t",
            dataset="D",
            version=version,
            ambiguous=True,
            question="q",
            clarification_question="which?",
            goal_id="g",
            goal_text="goal",
            correct_key="b",
            baseline_pick="a",
            interpretations=tuple(_ambiguous("t")["interpretations"]),
        )

    def test_records_comprehension_only_when_asked(self) -> None:
        asked = make_task_record(
            "p", self._instance(VERSION_ASKING), 0, "b", 5, 4, 4, 1.0, "ts"
        )
        self.assertTrue(asked["asked"])
        self.assertTrue(asked["comprehension"])
        self.assertTrue(asked["correct"])

        wrong = make_task_record(
            "p", self._instance(VERSION_ASKING), 1, "a", 2, 3, 3, 1.0, "ts"
        )
        self.assertFalse(wrong["comprehension"])
        self.assertFalse(wrong["correct"])

        direct = make_task_record(
            "p", self._instance(VERSION_DIRECT), 2, None, 3, None, None, 1.0, "ts"
        )
        self.assertIsNone(direct["comprehension"])  # never asked
        self.assertFalse(direct["correct"])  # baseline guessed "a", goal was "b"


@unittest.skipUnless(SCENARIOS_PATH.is_file(), "scenarios.json not generated.")
class GeneratedScenariosTest(unittest.TestCase):
    def test_real_scenarios_build_a_full_plan(self) -> None:
        scenarios = load_scenarios(SCENARIOS_PATH)
        plan = build_plan(scenarios, "reviewer")
        self.assertEqual(len(plan), len(scenarios))
        # Every ambiguous scenario carries a two-option clarifying question and a
        # clarification prompt, or the asking version cannot run.
        for instance in plan:
            if instance.ambiguous:
                self.assertEqual(len(instance.interpretations), 2)
                self.assertTrue(instance.clarification_question)


class StudyAppSmokeTest(unittest.TestCase):
    APP = STUDY_DIR / "study_app.py"
    PID = "pytest_smoke"

    def tearDown(self) -> None:
        (STUDY_DIR / "results" / f"{self.PID}.jsonl").unlink(missing_ok=True)

    @staticmethod
    def _by_key(seq, key):
        return next((e for e in seq if getattr(e, "key", None) == key), None)

    @staticmethod
    def _button(app, label):
        return next((b for b in app.button if b.label == label), None)

    @unittest.skipUnless(SCENARIOS_PATH.is_file(), "scenarios.json not generated.")
    def test_welcome_screen_renders(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(self.APP)).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(app.title)
        self.assertIn("study", app.title[0].value.lower())

    @unittest.skipUnless(SCENARIOS_PATH.is_file(), "scenarios.json not generated.")
    def test_consent_advances_to_first_task_and_asks(self) -> None:
        # AppTest cannot script the whole multi-rerun wizard, but it can verify
        # the consent gate, the welcome -> task transition, and that asking the
        # assistant on the first task renders without error.
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(self.APP)).run(timeout=30)
        self._by_key(app.text_input, "participant_id").set_value(self.PID)
        self._by_key(app.radio, "screen_role").set_value(
            "General (no clinical training)"
        )
        self._by_key(app.radio, "screen_comfort").set_value("4")
        self._by_key(app.checkbox, "consent").set_value(True)
        app.run(timeout=30)

        self._button(app, "Start").click()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["phase"], "task")
        self.assertEqual(len(app.session_state["plan"]), 8)

        self._button(app, "Ask the assistant").click()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        idx = app.session_state["idx"]
        instance = app.session_state["plan"][idx]
        if instance.asks_question:
            self.assertTrue(
                any(
                    (b.key or "").startswith(f"opt_{idx}_") for b in app.button
                )
            )
        else:
            self.assertIsNotNone(self._by_key(app.radio, f"trust_{idx}"))


if __name__ == "__main__":
    unittest.main()
