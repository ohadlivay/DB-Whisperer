"""Streamlit rendering tests for ambiguity workflow states."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import (
    AmbiguityDecision,
    ComponentState,
    QueryResult,
    QueryWorkflowResult,
)
from db_whisperer.gui.app import (
    HOURGLASS_ICON,
    MONEY_ICON,
    ModelOption,
    _chat_window_height,
    _configured_model_option,
    _format_clarification,
    _model_option_label,
    _model_rating_label,
    _queue_query,
)


class GuiWorkflowTest(unittest.TestCase):
    def _app(self) -> AppTest:
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.run(timeout=20)
        self.assertFalse(app.exception)
        app.session_state["active_query"] = "Analyze the data"
        app.session_state["data_ready"] = True
        return app

    @staticmethod
    def _query_result() -> QueryResult:
        return QueryResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            sql="SELECT 1 AS value",
            columns=("value",),
            rows=((1,),),
        )

    def test_pending_workflow_asks_question_and_hides_results(self) -> None:
        app = self._app()
        app.session_state["workflow_result"] = QueryWorkflowResult(
            state=ComponentState.PENDING,
            message="Which interpretation should be used?",
            iteration=1,
            complete=False,
            query_result=self._query_result(),
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question="Which interpretation should be used?",
                options=("Use all records", "Use filtered records"),
            ),
        )

        app.run(timeout=20)

        self.assertFalse(app.exception)
        button_labels = {item.label for item in app.button}
        self.assertIn("Use all records", button_labels)
        self.assertIn("Use filtered records", button_labels)
        self.assertFalse(any(
            item.label == "Your answer"
            for item in app.text_input
        ))
        self.assertEqual(0, len(app.dataframe))

    def test_formats_question_and_selected_answer_for_querier(self) -> None:
        clarification = _format_clarification(
            "Which records?",
            "Use all records",
        )

        self.assertEqual(
            "Question: Which records?\n"
            "Selected answer: Use all records",
            clarification,
        )

    def test_complete_workflow_displays_latest_result(self) -> None:
        app = self._app()
        app.session_state["workflow_result"] = QueryWorkflowResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            iteration=3,
            complete=True,
            query_result=self._query_result(),
        )

        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertEqual(1, len(app.dataframe))

    def test_query_is_queued_before_workflow_generation(self) -> None:
        state = {
            "active_query": "",
            "workflow_result": object(),
            "clarifications": ("old",),
            "clarification_history": (("old question", "old answer"),),
            "active_candidate_count": 2,
            "query_pending": False,
        }

        _queue_query("  Analyze the data  ", 5, state)

        self.assertEqual("Analyze the data", state["active_query"])
        self.assertIsNone(state["workflow_result"])
        self.assertEqual((), state["clarifications"])
        self.assertEqual((), state["clarification_history"])
        self.assertEqual(5, state["active_candidate_count"])
        self.assertTrue(state["query_pending"])

    def test_chat_scrolls_only_after_history_exists(self) -> None:
        state = {
            "active_query": "",
            "workflow_result": None,
            "chat_history": (),
        }
        self.assertEqual("content", _chat_window_height(state))

        state["active_query"] = "Analyze the data"
        self.assertEqual("content", _chat_window_height(state))

        state["chat_history"] = ({"query": "Earlier"},)
        self.assertEqual(620, _chat_window_height(state))

    def test_chat_history_displays_previous_and_active_results(self) -> None:
        app = self._app()
        previous_workflow = QueryWorkflowResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            iteration=1,
            complete=True,
            query_result=self._query_result(),
        )
        app.session_state["chat_history"] = (
            {
                "query": "Previous question",
                "clarification_history": (
                    ("Previous clarification?", "Previous answer"),
                ),
                "workflow_result": previous_workflow,
            },
        )
        app.session_state["workflow_result"] = QueryWorkflowResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            iteration=1,
            complete=True,
            query_result=self._query_result(),
        )

        app.run(timeout=20)

        self.assertFalse(app.exception)
        self.assertEqual(2, len(app.dataframe))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("Previous question", markdown)
        self.assertIn("Previous clarification?", markdown)
        self.assertIn("Previous answer", markdown)
        self.assertIn("Analyze the data", markdown)

    def test_candidate_count_defaults_to_three(self) -> None:
        app = self._app()

        candidate_inputs = [
            item
            for item in app.number_input
            if item.label == "SQL candidates"
        ]

        self.assertEqual(1, len(candidate_inputs))
        self.assertEqual(3, candidate_inputs[0].value)

    def test_model_options_include_presets_and_custom_choice(self) -> None:
        self.assertEqual(
            [
                "deepseek/deepseek-v4-flash",
                "moonshotai/kimi-k2.7-code",
                "google/gemma-4-31b-it",
                "Choose your own",
            ],
            [option.value for option in ModelOption],
        )
        self.assertEqual(
            ModelOption.GEMMA,
            _configured_model_option("google/gemma-4-31b-it"),
        )
        self.assertEqual(
            ModelOption.GEMMA,
            _configured_model_option(""),
        )
        self.assertEqual(
            ModelOption.CUSTOM,
            _configured_model_option("provider/custom-model"),
        )

    def test_model_labels_show_relative_cost_and_time(self) -> None:
        gemma = _model_rating_label(ModelOption.GEMMA)
        kimi = _model_rating_label(ModelOption.KIMI)

        self.assertEqual(
            ModelOption.GEMMA.value,
            _model_option_label(ModelOption.GEMMA),
        )
        self.assertNotIn(MONEY_ICON, _model_option_label(ModelOption.KIMI))
        self.assertNotIn(HOURGLASS_ICON, _model_option_label(ModelOption.KIMI))
        self.assertEqual(1, gemma.count(MONEY_ICON))
        self.assertEqual(1, gemma.count(HOURGLASS_ICON))
        self.assertEqual(3, kimi.count(MONEY_ICON))
        self.assertEqual(3, kimi.count(HOURGLASS_ICON))


if __name__ == "__main__":
    unittest.main()
