"""Streamlit rendering tests for ambiguity workflow states."""

from __future__ import annotations

from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
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
    DATASET_STUDENT,
    HOURGLASS_ICON,
    MONEY_ICON,
    ModelOption,
    _chat_window_height,
    _configured_model_option,
    _example_dataset_upload,
    _format_session_usage_delta,
    _format_clarification,
    _ingest_sources,
    _latest_release,
    _load_changelog,
    _model_option_label,
    _model_rating_label,
    _queue_query,
    _sync_usage_tracking,
)


class GuiWorkflowTest(unittest.TestCase):
    def _app(self) -> AppTest:
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state["data_ready"])
        app.session_state["active_query"] = "Analyze the data"
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
                "openrouter/free",
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

    def test_changelog_uses_most_recent_release_for_version(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "changelog.json"
            path.write_text(
                json.dumps(
                    {
                        "releases": [
                            {
                                "version": "1.0.0",
                                "released_at": "2026-06-13T12:00:00+03:00",
                                "changes": ["Initial release."],
                            },
                            {
                                "version": "1.2.0",
                                "released_at": "2026-06-15T12:00:00+03:00",
                                "changes": ["Newest release."],
                            },
                            {
                                "version": "1.1.0",
                                "released_at": "2026-06-14T12:00:00+03:00",
                                "changes": ["Middle release."],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            releases = _load_changelog(path)

            self.assertEqual("1.2.0", _latest_release(releases)["version"])
            self.assertEqual(
                ["1.2.0", "1.1.0", "1.0.0"],
                [release["version"] for release in releases],
            )

    def test_sidebar_displays_latest_version_button(self) -> None:
        app = self._app()

        self.assertIn("v1.4.0", {button.label for button in app.button})
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("Session usage:", markdown)

    def test_usage_tracking_reports_session_delta(self) -> None:
        state = {}
        usages = iter((12.5, 12.625))
        calls: list[str] = []

        def fetch_usage(api_key: str) -> float:
            calls.append(api_key)
            return next(usages)

        _sync_usage_tracking(
            " secret-key ",
            state,
            fetch_usage=fetch_usage,
        )

        self.assertEqual("$0.0000", _format_session_usage_delta(state)[-7:])
        self.assertEqual(12.5, state["usage_baseline"])
        self.assertEqual(12.5, state["usage_current"])
        self.assertNotIn("secret-key", state.values())

        _sync_usage_tracking(
            "secret-key",
            state,
            force_refresh=True,
            fetch_usage=fetch_usage,
        )

        self.assertEqual(
            "Session usage: $0.1250",
            _format_session_usage_delta(state),
        )
        self.assertEqual(["secret-key", "secret-key"], calls)

    def test_usage_tracking_resets_without_api_key(self) -> None:
        state = {
            "usage_key_fingerprint": "old",
            "usage_baseline": 10.0,
            "usage_current": 11.0,
            "usage_error": "old error",
        }

        _sync_usage_tracking("", state)

        self.assertEqual("", state["usage_key_fingerprint"])
        self.assertIsNone(state["usage_baseline"])
        self.assertIsNone(state["usage_current"])
        self.assertEqual("", state["usage_error"])
        self.assertEqual("Session usage: --", _format_session_usage_delta(state))

    def test_single_csv_example_is_available(self) -> None:
        upload = _example_dataset_upload()

        self.assertEqual("ai_student_impact_dataset.csv", upload.name)
        self.assertTrue(upload.content.startswith(b"Student_ID,"))

    def test_bundled_relational_dataset_is_the_default(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)

        self.assertFalse(app.exception)
        ingestion = app.session_state["ingestion_result"]
        source_names = set(ingestion.schema.source_names)
        self.assertIn("orders.csv", source_names)
        self.assertIn("customers.csv", source_names)
        self.assertGreater(len(ingestion.schema.table_names), 1)
        self.assertTrue(ingestion.schema.relationships)

    def test_student_dataset_can_be_selected(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)

        app.selectbox[0].set_value(DATASET_STUDENT).run(timeout=30)

        self.assertFalse(app.exception)
        ingestion = app.session_state["ingestion_result"]
        self.assertEqual(
            ("ai_student_impact_dataset.csv",),
            ingestion.schema.source_names,
        )

    def test_relationships_panel_lists_discovered_fks(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)

        rendered = json.dumps([table.value for table in app.table], default=str)
        self.assertIn("orders.customer_id", rendered)
        self.assertIn("customers.customer_id", rendered)

    def test_empty_sources_is_a_noop(self) -> None:
        # An empty source list must short-circuit before touching Streamlit
        # session state, so previously ingested data is preserved.
        self.assertIsNone(_ingest_sources([], ("upload", ()), object()))


if __name__ == "__main__":
    unittest.main()
