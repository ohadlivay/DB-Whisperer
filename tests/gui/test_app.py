"""Streamlit rendering tests for ambiguity workflow states."""

from __future__ import annotations

from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
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
    DATASET_MIMIC,
    DATASET_STUDENT,
    HOURGLASS_ICON,
    MIMIC_DATASET_DIR,
    MONEY_ICON,
    SESSION_DATABASE_ROOT_ENV,
    ModelOption,
    _application_service,
    _chat_window_height,
    _configured_model_option,
    _default_openrouter_api_key,
    _display_clarification_question,
    _example_dataset_upload,
    _format_session_usage_delta,
    _format_clarification,
    _ingest_sources,
    _latest_release,
    _model_options_for_api_key,
    _load_changelog,
    _model_option_label,
    _model_rating_label,
    _queue_query,
    _session_database_path,
    _sync_usage_tracking,
)


class GuiWorkflowTest(unittest.TestCase):
    def test_application_service_is_not_stale_across_reruns(self) -> None:
        first = _application_service()
        second = _application_service()

        self.assertIsNot(first, second)
        self.assertTrue(callable(second.preview_table))

    def test_session_database_path_is_private_and_stable(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory, patch.dict(
            os.environ,
            {SESSION_DATABASE_ROOT_ENV: directory},
        ):
            first_state = {}
            second_state = {}

            first_path = _session_database_path(first_state)
            same_session_path = _session_database_path(first_state)
            second_path = _session_database_path(second_state)

            self.assertEqual(first_path, same_session_path)
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(Path(directory), first_path.parent.parent)
            self.assertEqual("db_whisperer.duckdb", first_path.name)
            self.assertTrue(first_state["database_session_id"])
            self.assertNotEqual(
                first_state["database_session_id"],
                second_state["database_session_id"],
            )

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

    def test_hides_semantic_column_bookkeeping_from_question(self) -> None:
        internal_question = (
            "Which date do you mean? (clarifying which column: "
            '"orders.order_date" or "orders.required_date")'
        )

        self.assertEqual(
            "Which date do you mean?",
            _display_clarification_question(internal_question),
        )

    def test_hides_structured_grounding_from_question(self) -> None:
        internal_question = (
            "Were patients born or admitted in 2112? "
            '[grounding: "patients.dob", "admissions.admittime"]'
        )

        self.assertEqual(
            "Were patients born or admitted in 2112?",
            _display_clarification_question(internal_question),
        )

    def test_pending_workflow_hides_semantic_column_bookkeeping(self) -> None:
        app = self._app()
        internal_question = (
            "Which date do you mean? (clarifying which column: "
            '"orders.order_date" or "orders.required_date")'
        )
        app.session_state["workflow_result"] = QueryWorkflowResult(
            state=ComponentState.PENDING,
            message=internal_question,
            iteration=1,
            complete=False,
            ambiguity=AmbiguityDecision(
                state=ComponentState.ACCEPTED,
                passed=False,
                question=internal_question,
                options=("Order date", "Required date"),
            ),
        )

        app.run(timeout=20)

        self.assertFalse(app.exception)
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("Which date do you mean?", markdown)
        self.assertNotIn("clarifying which column", markdown)

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

    def test_default_api_key_allows_only_gemma_model(self) -> None:
        self.assertEqual(
            (ModelOption.GEMMA,),
            _model_options_for_api_key(using_default_api_key=True),
        )
        self.assertEqual(
            tuple(ModelOption),
            _model_options_for_api_key(using_default_api_key=False),
        )

    def test_default_api_key_prefers_streamlit_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": " env-key "},
        ), patch(
            "db_whisperer.gui.app.st.secrets",
            {"OPENROUTER_API_KEY": " secret-key "},
        ):
            self.assertEqual(
                "secret-key",
                _default_openrouter_api_key(),
            )

    def test_default_api_key_falls_back_to_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": " env-key "},
        ), patch("db_whisperer.gui.app.st.secrets", {}):
            self.assertEqual("env-key", _default_openrouter_api_key())

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

        # Derive the expected version from the shipped changelog so a version
        # bump does not require editing this assertion.
        latest_version = _latest_release(_load_changelog())["version"]
        self.assertIn(
            f"v{latest_version}",
            {button.label for button in app.button},
        )
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("Session usage:", markdown)

    def test_sidebar_displays_github_link(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)

        rendered_markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn(
            'href="https://github.com/ohadlivay/DB-Whisperer"',
            rendered_markdown,
        )
        self.assertIn("ohadlivay/DB-Whisperer", rendered_markdown)

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

        rendered_markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("help you explore your data", rendered_markdown)
        self.assertEqual(0, len(app.dataframe))
        rendered_sql = "\n".join(item.value for item in app.code)
        self.assertNotIn("LIMIT 10", rendered_sql)

    def test_table_carousel_shows_three_row_preview(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        schema = app.session_state["ingestion_result"].schema
        selected_table = schema.table_names[0]

        table_button = next(
            button for button in app.button if button.label == selected_table
        )
        table_button.click().run(timeout=30)

        self.assertFalse(app.exception)
        self.assertEqual(
            selected_table,
            app.session_state["schema_browser_table"],
        )
        three_row_previews = [
            frame for frame in app.dataframe if len(frame.value) == 3
        ]
        self.assertTrue(three_row_previews)
        table_schema = next(
            table
            for table in schema.tables
            if table.table_name == selected_table
        )
        self.assertEqual(
            [column.name for column in table_schema.columns],
            list(three_row_previews[0].value.columns),
        )

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

    def test_mimic_dataset_is_offered_in_selector(self) -> None:
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)

        # selectbox[0] is the dataset picker (model selector comes later).
        self.assertIn(DATASET_MIMIC, list(app.selectbox[0].options))

    def test_mimic_dataset_directory_is_bundled(self) -> None:
        # Fail loudly if the bundled MIMIC demo data is missing or moved, so
        # the sidebar option never points at an absent folder.
        self.assertTrue(
            MIMIC_DATASET_DIR.is_dir(),
            f"MIMIC dataset directory is missing: {MIMIC_DATASET_DIR}",
        )
        csv_names = {path.name for path in MIMIC_DATASET_DIR.glob("*.csv")}
        for core_table in ("PATIENTS.csv", "ADMISSIONS.csv", "LABEVENTS.csv"):
            self.assertIn(core_table, csv_names)

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

    def test_reset_conversation_button(self) -> None:
        app = self._app()
        workflow = QueryWorkflowResult(
            state=ComponentState.ACCEPTED,
            message="Returned 1 row(s).",
            iteration=1,
            complete=True,
            query_result=self._query_result(),
        )
        
        # Initialize settings first
        app.session_state["openrouter_api_key"] = "test-key"
        app.session_state["openrouter_model"] = "test-model"
        app.session_state["active_candidate_count"] = 5

        # Run to let the app initialize its internal states (like upload_signature)
        app.run(timeout=20)
        self.assertFalse(app.exception)
        
        # Now set the chat state that we want to see cleared
        app.session_state["active_query"] = "Show all data"
        app.session_state["workflow_result"] = workflow
        app.session_state["clarifications"] = ("c1",)
        app.session_state["clarification_history"] = (("q", "a"),)
        app.session_state["chat_history"] = ({
            "query": "old",
            "clarification_history": (),
            "workflow_result": workflow,
        },)
        app.session_state["schema_browser_table"] = "orders"
        app.session_state["query_pending"] = True

        app.run(timeout=20)
        self.assertFalse(app.exception)

        # Record upload signature before reset
        expected_signature = app.session_state["upload_signature"]

        # Simulate clicking the button
        reset_button = next(
            button for button in app.button if button.label == "Confirm Reset"
        )
        reset_button.click().run(timeout=20)

        self.assertFalse(app.exception)
        
        # Assert chat-related fields are cleared
        self.assertEqual("", app.session_state["active_query"])
        self.assertIsNone(app.session_state["workflow_result"])
        self.assertEqual((), app.session_state["clarifications"])
        self.assertEqual((), app.session_state["clarification_history"])
        self.assertEqual((), app.session_state["chat_history"])
        self.assertEqual("", app.session_state["schema_browser_table"])
        self.assertFalse(app.session_state["query_pending"])
        
        # Assert settings remain intact
        self.assertEqual("test-key", app.session_state["openrouter_api_key"])
        self.assertEqual("test-model", app.session_state["openrouter_model"])
        self.assertEqual(5, app.session_state["active_candidate_count"])
        self.assertEqual(expected_signature, app.session_state["upload_signature"])



if __name__ == "__main__":
    unittest.main()
