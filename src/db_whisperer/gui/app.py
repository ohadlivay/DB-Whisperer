"""Streamlit interface for DB Whisperer."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
import hashlib
from html import escape
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

import requests
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from db_whisperer.application import ApplicationService
from db_whisperer.contracts import (
    ComponentState,
    CsvUpload,
    QueryResult,
    SchemaMetadata,
)
from db_whisperer.etler import ETLService
from db_whisperer.schema_graph import SchemaGraph


class ModelOption(StrEnum):
    """Supported model presets plus a user-supplied model ID."""

    DEEPSEEK = "deepseek/deepseek-v4-flash"
    KIMI = "moonshotai/kimi-k2.7-code"
    GEMMA = "google/gemma-4-31b-it"
    FREE = "openrouter/free"
    CUSTOM = "Choose your own"


MODEL_RATINGS: dict[ModelOption, tuple[int, int]] = {
    ModelOption.DEEPSEEK: (1, 2),
    ModelOption.KIMI: (3, 3),
    ModelOption.GEMMA: (1, 1),
    ModelOption.FREE: (1, 2),
}
MONEY_ICON = "\U0001F4B0"
HOURGLASS_ICON = "\u23F3"
CHANGELOG_PATH = Path(__file__).with_name("changelog.json")
OPENROUTER_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
SESSION_DATABASE_ROOT_ENV = "DB_WHISPERER_SESSION_DATABASE_ROOT"
SESSION_DATABASE_FILENAME = "db_whisperer.duckdb"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = PROJECT_ROOT / "data"
RELATIONAL_DATASET_DIR = DATA_ROOT / "relational csv"
MIMIC_DATASET_DIR = (
    DATA_ROOT
    / "mimic-iii-clinical-database-demo-1.4-20260615T211207Z-3-001"
    / "mimic-iii-clinical-database-demo-1.4"
)
SINGLE_DATASET_PATH = DATA_ROOT / "single csv" / "ai_student_impact_dataset.csv"
EXAMPLE_DATASET_PATH = SINGLE_DATASET_PATH

DATASET_BIKESTORES = "BikeStores (relational)"
DATASET_MIMIC = "MIMIC-III clinical demo (relational)"
DATASET_STUDENT = "Student impact (single CSV)"
DATASET_UPLOAD = "Upload your own"
WELCOME_MESSAGE = (
    "I'm here to help you explore your data! "
    "Ask away, and I'll do my best to understand your question and provide an answer. "
)


def _model_option_label(option: ModelOption) -> str:
    """Show the original model ID without ratings in the selector."""
    return option.value


def _model_rating_label(option: ModelOption) -> str:
    """Render the selected model's relative cost and latency tray."""
    rating = MODEL_RATINGS.get(option)
    if rating is None:
        return "Cost and response time are not rated."
    money, time = rating
    return (
        f"Cost {MONEY_ICON * money}  "
        f"Time {HOURGLASS_ICON * time}"
    )


def _configured_model_option(model: str) -> ModelOption:
    """Select a preset when the configured model matches one exactly."""
    normalized = model.strip()
    if not normalized:
        return ModelOption.GEMMA
    for option in ModelOption:
        if option != ModelOption.CUSTOM and option.value == normalized:
            return option
    return ModelOption.CUSTOM


def _model_options_for_api_key(
    using_default_api_key: bool,
) -> tuple[ModelOption, ...]:
    """Restrict the shared default API key to the approved model."""
    if using_default_api_key:
        return (ModelOption.GEMMA,)
    return tuple(ModelOption)


def _release_timestamp(release: dict[str, Any]) -> datetime:
    """Parse one changelog release timestamp."""
    released_at = release.get("released_at")
    if not isinstance(released_at, str):
        raise ValueError("Every changelog release requires released_at.")
    return datetime.fromisoformat(released_at.replace("Z", "+00:00"))


def _load_changelog(
    path: Path = CHANGELOG_PATH,
) -> tuple[dict[str, Any], ...]:
    """Load and validate changelog releases from JSON."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    releases = payload.get("releases")
    if not isinstance(releases, list) or not releases:
        raise ValueError("Changelog must contain at least one release.")

    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("Every changelog release must be an object.")
        if not isinstance(release.get("version"), str):
            raise ValueError("Every changelog release requires a version.")
        changes = release.get("changes")
        if (
            not isinstance(changes, list)
            or not changes
            or not all(isinstance(change, str) for change in changes)
        ):
            raise ValueError(
                "Every changelog release requires a non-empty changes list."
            )
        _release_timestamp(release)

    return tuple(
        sorted(releases, key=_release_timestamp, reverse=True)
    )


def _latest_release(
    releases: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Return the most recently released changelog entry."""
    if not releases:
        raise ValueError("At least one changelog release is required.")
    return max(releases, key=_release_timestamp)


def _api_key_fingerprint(api_key: str) -> str:
    """Return a stable non-secret fingerprint for session tracking."""
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()


def _fetch_openrouter_key_usage(
    api_key: str,
    timeout_seconds: float = 10,
) -> float:
    """Fetch cumulative usage for the current OpenRouter API key."""
    response = requests.get(
        OPENROUTER_KEY_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("OpenRouter key response did not contain data.")
    usage = data.get("usage")
    if isinstance(usage, bool):
        raise ValueError("OpenRouter key usage was not numeric.")
    try:
        return float(usage)
    except (TypeError, ValueError) as error:
        raise ValueError("OpenRouter key usage was not numeric.") from error


def _default_openrouter_api_key() -> str:
    """Return the hidden default API key from Streamlit secrets or env."""
    try:
        secret_key = st.secrets.get("OPENROUTER_API_KEY", "")
    except StreamlitSecretNotFoundError:
        secret_key = ""

    if isinstance(secret_key, str) and secret_key.strip():
        return secret_key.strip()
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def _sync_usage_tracking(
    api_key: str,
    state: MutableMapping[str, Any],
    *,
    force_refresh: bool = False,
    fetch_usage: Callable[[str], float] = _fetch_openrouter_key_usage,
) -> None:
    """Track usage delta from the moment the current API key appears."""
    normalized_key = api_key.strip()
    if not normalized_key:
        state["usage_key_fingerprint"] = ""
        state["usage_baseline"] = None
        state["usage_current"] = None
        state["usage_error"] = ""
        return

    fingerprint = _api_key_fingerprint(normalized_key)
    is_new_key = state.get("usage_key_fingerprint") != fingerprint
    should_fetch = (
        is_new_key
        or force_refresh
        or state.get("usage_baseline") is None
    )
    if not should_fetch:
        return

    if is_new_key:
        state["usage_key_fingerprint"] = fingerprint
        state["usage_baseline"] = None
        state["usage_current"] = None

    try:
        usage = fetch_usage(normalized_key)
    except Exception as error:  # pragma: no cover - exact HTTP errors vary.
        state["usage_error"] = f"Usage unavailable: {error}"
        return

    if state.get("usage_baseline") is None:
        state["usage_baseline"] = usage
    state["usage_current"] = usage
    state["usage_error"] = ""


def _format_session_usage_delta(
    state: MutableMapping[str, Any],
) -> str:
    """Render the current OpenRouter session usage delta."""
    if state.get("usage_error"):
        return "Session usage: unavailable"
    baseline = state.get("usage_baseline")
    current = state.get("usage_current")
    if baseline is None or current is None:
        return "Session usage: --"
    delta = max(float(current) - float(baseline), 0.0)
    return f"Session usage: ${delta:.4f}"


def _session_database_root() -> Path:
    """Return the root directory for per-session DuckDB files."""
    configured_root = os.getenv(SESSION_DATABASE_ROOT_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser()
    return Path(tempfile.gettempdir()) / "db_whisperer" / "sessions"


def _session_database_path(state: MutableMapping[str, Any]) -> Path:
    """Return a stable, private DuckDB path for one Streamlit session."""
    existing_path = state.get("database_path")
    if isinstance(existing_path, str) and existing_path.strip():
        return Path(existing_path)

    session_id = state.get("database_session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = uuid4().hex
        state["database_session_id"] = session_id

    database_path = (
        _session_database_root()
        / session_id
        / SESSION_DATABASE_FILENAME
    )
    state["database_path"] = str(database_path)
    return database_path


@st.dialog("Changelog", width="large")
def _show_changelog(releases: tuple[dict[str, Any], ...]) -> None:
    """Display release notes in a modal dialog."""
    for release in sorted(
        releases,
        key=_release_timestamp,
        reverse=True,
    ):
        released_on = _release_timestamp(release).date().isoformat()
        st.subheader(f"Version {release['version']}")
        st.caption(released_on)
        for change in release["changes"]:
            st.markdown(f"- {change}")


def _initialize_state() -> None:
    defaults: dict[str, Any] = {
        "active_query": "",
        "data_ready": False,
        "upload_signature": (),
        "ingestion_result": None,
        "workflow_result": None,
        "clarifications": (),
        "clarification_history": (),
        "chat_history": (),
        "schema_browser_table": "",
        "active_candidate_count": 3,
        "query_pending": False,
        "database_session_id": "",
        "database_path": "",
        "usage_key_fingerprint": "",
        "usage_baseline": None,
        "usage_current": None,
        "usage_error": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _apply_styles() -> None:
    """Apply the visual tokens and layout from the Stitch design."""
    css_path = Path(__file__).parent / "style.css"
    try:
        css_content = css_path.read_text(encoding="utf-8")
        st.markdown(
            f"<style>{css_content}</style>",
            unsafe_allow_html=True,
        )
    except FileNotFoundError:
        # Fallback or error logging
        st.error("Stylesheet 'style.css' not found.")


def _section_label(text: str) -> None:
    st.markdown(
        f'<div class="sidebar-section">{escape(text)}</div>',
        unsafe_allow_html=True,
    )


def _status(label: str, ready: bool) -> None:
    state = "ready" if ready else ""
    st.markdown(
        f"""
        <div class="status-row">
            <span class="status-dot {state}"></span>
            <span>{escape(label)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _application_service() -> ApplicationService:
    """Create a coordinator using the currently loaded service classes."""
    return ApplicationService(
        etler=ETLService(_session_database_path(st.session_state)),
    )


@lru_cache(maxsize=1)
def _example_dataset_upload() -> CsvUpload:
    """Read the bundled single-CSV example once per application process."""
    return CsvUpload(
        name=EXAMPLE_DATASET_PATH.name,
        content=EXAMPLE_DATASET_PATH.read_bytes(),
        content_type="text/csv",
    )


@lru_cache(maxsize=8)
def _builtin_dataset_uploads(path_str: str) -> tuple[CsvUpload, ...]:
    """Read a bundled dataset: one CSV file, or every CSV in a folder."""
    path = Path(path_str)
    if path.is_dir():
        return tuple(
            CsvUpload(
                name=csv_path.name,
                content=csv_path.read_bytes(),
                content_type="text/csv",
            )
            for csv_path in sorted(path.glob("*.csv"))
        )
    return (
        CsvUpload(
            name=path.name,
            content=path.read_bytes(),
            content_type="text/csv",
        ),
    )


def _ingest_sources(
    sources: list[CsvUpload],
    signature: tuple[Any, ...],
    application: ApplicationService,
) -> None:
    """Ingest a set of CSV sources, skipping when nothing changed.

    An empty source list is a no-op so a freshly selected upload mode keeps
    the previously ingested data until real files arrive.
    """
    if not sources:
        return
    if signature == st.session_state.upload_signature:
        return

    st.session_state.upload_signature = signature
    st.session_state.active_query = ""
    st.session_state.workflow_result = None
    st.session_state.clarifications = ()
    st.session_state.clarification_history = ()
    st.session_state.chat_history = ()
    st.session_state.schema_browser_table = ""
    st.session_state.query_pending = False

    result = application.ingest_csvs(sources)
    st.session_state.ingestion_result = result
    st.session_state.data_ready = result.state == ComponentState.ACCEPTED


def _ingest_builtin(
    path: Path,
    key: str,
    application: ApplicationService,
) -> None:
    sources = list(_builtin_dataset_uploads(str(path)))
    _ingest_sources(sources, ("builtin", key), application)


def _ingest_uploads(
    uploads: list[Any] | None,
    application: ApplicationService,
) -> None:
    uploads = uploads or []
    sources = [
        CsvUpload(
            name=upload.name,
            content=upload.getvalue(),
            content_type=upload.type or "text/csv",
        )
        for upload in uploads
    ]
    signature = ("upload", tuple(sorted((u.name, u.size) for u in uploads)))
    _ingest_sources(sources, signature, application)


def _render_sidebar(
    application: ApplicationService,
) -> tuple[str, str, int]:
    with st.sidebar:
        existing_api_key = st.session_state.get("openrouter_api_key", "")
        default_api_key = _default_openrouter_api_key()
        _sync_usage_tracking(
            existing_api_key or default_api_key,
            st.session_state,
        )

        releases = _load_changelog()
        latest_release = _latest_release(releases)
        st.markdown(
            (
                '<div class="sidebar-brand">'
                '<div class="sidebar-brand-title">DB Whisperer</div>'
                '<a class="sidebar-github-link" '
                'href="https://github.com/ohadlivay/DB-Whisperer" '
                'target="_blank" rel="noopener noreferrer">'
                'ohadlivay/DB-Whisperer</a>'
                '</div>'
            ),
            unsafe_allow_html=True,
        )
        with st.container(key="version_usage_row"):
            if st.button(
                f"v{latest_release['version']}",
                key="version_button",
                help="View changelog",
            ):
                _show_changelog(releases)
            with st.container(key="usage_label"):
                st.markdown(
                    (
                        '<div class="usage-pill">'
                        f"{escape(_format_session_usage_delta(st.session_state))}"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

        _section_label("Data source")
        dataset_choice = st.selectbox(
            "Dataset",
            options=[
                DATASET_BIKESTORES,
                DATASET_MIMIC,
                DATASET_STUDENT,
                DATASET_UPLOAD,
            ],
            index=0,
            label_visibility="collapsed",
        )
        if dataset_choice == DATASET_UPLOAD:
            uploads = st.file_uploader(
                "Upload CSV files",
                type=["csv"],
                accept_multiple_files=True,
                label_visibility="collapsed",
            )
            _ingest_uploads(uploads, application)
            if not uploads:
                st.caption("Upload one or more related CSV files.")
        elif dataset_choice == DATASET_STUDENT:
            _ingest_builtin(SINGLE_DATASET_PATH, "student", application)
            st.caption(f"Using bundled example: {SINGLE_DATASET_PATH.name}")
        elif dataset_choice == DATASET_MIMIC:
            _ingest_builtin(MIMIC_DATASET_DIR, "mimic", application)
            st.caption(
                "Using bundled example: MIMIC-III clinical demo "
                "(26 related tables, ODbL). First load takes a few seconds."
            )
        else:
            _ingest_builtin(RELATIONAL_DATASET_DIR, "bikestores", application)
            st.caption("Using bundled example: BikeStores (9 related tables)")

        _section_label("LLM configuration")
        entered_api_key = st.text_input(
            "OpenRouter API key",
            type="password",
            placeholder="sk-or-v1-...",
            key="openrouter_api_key",
            help="Get your key on [OpenRouter](https://openrouter.ai/keys)"
        )
        typed_api_key = entered_api_key.strip()
        using_default_api_key = not typed_api_key and bool(default_api_key)
        configured_model = os.getenv("OPENROUTER_MODEL", "")
        configured_option = (
            ModelOption.GEMMA
            if using_default_api_key
            else _configured_model_option(configured_model)
        )
        model_options = list(_model_options_for_api_key(using_default_api_key))
        selected_model = st.selectbox(
            "Model",
            options=model_options,
            index=model_options.index(configured_option),
            format_func=_model_option_label,
            disabled=using_default_api_key,
        )
        if selected_model == ModelOption.CUSTOM:
            model = st.text_input(
                "Custom model ID",
                value=(
                    configured_model
                    if configured_option == ModelOption.CUSTOM
                    else ""
                ),
                placeholder="provider/model",
            )
        else:
            model = selected_model.value
        st.markdown(
            (
                '<div class="model-rating">'
                f"{escape(_model_rating_label(selected_model))}"
                "</div>"
            ),
            unsafe_allow_html=True,
        )
        candidate_count = int(st.number_input(
            "SQL candidates",
            min_value=2,
            max_value=10,
            value=3,
            step=1,
            label_visibility="visible",
            help="Number of SQL candidates to generate"
        ))

        api_key = typed_api_key or default_api_key
        _sync_usage_tracking(api_key, st.session_state)

        st.divider()
        schema = _current_schema()
        ready_label = (
            f"DuckDB ready — {len(schema.table_names)} table(s)"
            if st.session_state.data_ready
            else "Waiting for data"
        )
        _status(ready_label, st.session_state.data_ready)
        _status(
            (
                "OpenRouter configured"
                if api_key and model
                else "OpenRouter not configured"
            ),
            bool(api_key and model),
        )

        ingestion_result = st.session_state.ingestion_result
        if ingestion_result is not None:
            st.caption(ingestion_result.message)

        if schema.relationships:
            _render_relationships_panel(schema)
            _render_schema_graph_panel(schema)
        if st.session_state.data_ready and not schema.discovery_complete:
            st.warning(
                "Relationship discovery was incomplete:\n"
                + "\n".join(f"- {note}" for note in schema.discovery_notes)
            )

    return api_key, model, candidate_count


def _render_relationships_panel(schema: SchemaMetadata) -> None:
    """List discovered foreign-key relationships in a collapsible panel."""
    with st.expander(f"Relationships ({len(schema.relationships)})"):
        st.table(
            [
                {
                    "child": f"{r.child_table}.{r.child_column}",
                    "parent": f"{r.parent_table}.{r.parent_column}",
                    "overlap": round(r.overlap, 2),
                    "ambiguous": "yes" if r.ambiguous else "",
                    "sampled": "yes" if r.sampled else "",
                }
                for r in schema.relationships
            ]
        )


def _render_schema_graph_panel(schema: SchemaMetadata) -> None:
    """Show the assembled schema graph as a per-table adjacency list.

    This is the same graph the join-path ambiguity detector traverses, so the
    user can see which tables are connected and where multi-hop joins exist.
    """
    graph = SchemaGraph.from_schema(schema)
    if not graph.edges:
        return
    with st.expander("Schema graph"):
        st.caption(
            "Tables connected by discovered joins. More than one path between "
            "two tables is where the system asks a clarifying question."
        )
        for table, neighbours in graph.adjacency_summary():
            connected = ", ".join(neighbours) if neighbours else "(no joins)"
            st.markdown(f"- **{escape(table)}** &rarr; {escape(connected)}")


def _render_message(text: str, role: str) -> None:
    st.markdown(
        f"""
        <div class="message-row {role}">
            <div class="message {role}">{escape(text)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _current_schema() -> SchemaMetadata:
    ingestion_result = st.session_state.ingestion_result
    return (
        ingestion_result.schema
        if ingestion_result is not None
        else SchemaMetadata()
    )


def _render_clarification_history(
    clarification_history: tuple[tuple[str, str], ...],
) -> None:
    for question, answer in clarification_history:
        _render_message(question, "assistant")
        _render_message(answer, "user")


def _format_clarification(question: str, answer: str) -> str:
    """Format the selected ambiguity choice for the Querier prompt."""
    return (
        f"Question: {question.strip()}\n"
        f"Selected answer: {answer.strip()}"
    )


def _queue_query(
    prompt: str,
    candidate_count: int,
    state: MutableMapping[str, Any],
) -> None:
    """Store a query for the next rerun before starting blocking work."""
    state["active_query"] = prompt.strip()
    state["workflow_result"] = None
    state["clarifications"] = ()
    state["clarification_history"] = ()
    state["active_candidate_count"] = candidate_count
    state["query_pending"] = True


def _chat_window_height(
    state: MutableMapping[str, Any],
) -> int | str:
    """Enable a bounded scroll area only after history exists."""
    history_count = len(state.get("chat_history", ()))

    if history_count:
        return 620
    return "content"


def _render_query_result(
    result: QueryResult,
    message: str | None = None,
) -> None:
    """Render a successful query result table and its SQL."""
    _render_message(message or result.message, "assistant")
    records = [
        dict(zip(result.columns, row, strict=True))
        for row in result.rows
    ]
    table_height = min(38 + max(len(records), 1) * 35, 320)
    st.dataframe(
        records,
        width="stretch",
        height=table_height,
        hide_index=True,
    )
    if result.truncated:
        st.caption("Results were limited to the first 1,000 rows.")
    if result.sql:
        with st.expander("View generated SQL"):
            st.code(result.sql, language="sql")


def _render_completed_workflow(workflow: Any) -> None:
    """Render a completed or failed workflow without interactive controls."""
    if workflow.state == ComponentState.FAILED:
        _render_message(workflow.message, "assistant")
        return

    result = workflow.query_result
    if result is None:
        _render_message(workflow.message, "assistant")
        return

    _render_query_result(result)


def _select_schema_table(table_name: str) -> None:
    """Store the table selected in the schema browser."""
    st.session_state.schema_browser_table = table_name


def _render_schema_browser(application: ApplicationService) -> None:
    """Render table navigation, columns, and a three-row data preview."""
    schema = _current_schema()
    if not st.session_state.data_ready or not schema.table_names:
        return

    selected_table = st.session_state.schema_browser_table
    if selected_table not in schema.table_names:
        selected_table = ""
        st.session_state.schema_browser_table = ""

    with st.container(key="schema_browser"):
        st.subheader("Database tables")
        st.caption(
            "Select a table to inspect its columns and first three rows."
        )
        with st.container(
            horizontal=True,
            gap="small",
            key="schema_table_carousel",
        ):
            for index, table_name in enumerate(schema.table_names):
                st.button(
                    table_name,
                    key=f"schema-table-{index}",
                    type=(
                        "primary"
                        if table_name == selected_table
                        else "secondary"
                    ),
                    on_click=_select_schema_table,
                    args=(table_name,),
                )

        if not selected_table:
            return

        result = application.preview_table(selected_table, schema, limit=3)
        if result.state != ComponentState.ACCEPTED:
            st.error(result.message)
            return

        records = [
            dict(zip(result.columns, row, strict=True))
            for row in result.rows
        ]
        st.dataframe(
            records,
            width="stretch",
            height=min(38 + max(len(records), 1) * 35, 180),
            hide_index=True,
        )


def _render_welcome() -> None:
    """Render the empty-chat introduction."""
    if st.session_state.active_query or st.session_state.chat_history:
        return

    _render_message(WELCOME_MESSAGE, "assistant")


def _archive_active_conversation() -> None:
    """Move the current completed exchange into session chat history."""
    query = st.session_state.active_query.strip()
    workflow = st.session_state.workflow_result
    if not query or workflow is None:
        return

    st.session_state.chat_history = (
        *st.session_state.chat_history,
        {
            "query": query,
            "clarification_history": tuple(
                st.session_state.clarification_history
            ),
            "workflow_result": workflow,
        },
    )


def _render_archived_conversations() -> None:
    for conversation in st.session_state.chat_history:
        _render_message(conversation["query"], "user")
        _render_clarification_history(
            conversation["clarification_history"]
        )
        _render_completed_workflow(conversation["workflow_result"])


def _render_workflow_response(
    application: ApplicationService,
    api_key: str,
    model: str,
) -> None:
    workflow = st.session_state.workflow_result
    if workflow is None:
        return

    if workflow.state == ComponentState.FAILED:
        _render_message(workflow.message, "assistant")
        return

    if not workflow.complete:
        question = (
            workflow.ambiguity.question
            if workflow.ambiguity is not None
            else workflow.message
        )
        _render_message(question or "Please clarify your request.", "assistant")
        options = (
            workflow.ambiguity.options
            if workflow.ambiguity is not None
            else ()
        )
        if len(options) != 2:
            st.error("The ambiguity response did not provide two options.")
            return

        selected_answer = None
        option_columns = st.columns(2)
        for option_index, option in enumerate(options):
            with option_columns[option_index]:
                if st.button(
                    option,
                    key=(
                        f"clarification-{workflow.iteration}-"
                        f"option-{option_index}"
                    ),
                    use_container_width=True,
                ):
                    selected_answer = option

        if selected_answer is not None:
            clarification = _format_clarification(
                question or "",
                selected_answer,
            )
            st.session_state.clarifications = (
                *st.session_state.clarifications,
                clarification,
            )
            st.session_state.clarification_history = (
                *st.session_state.clarification_history,
                (question, selected_answer),
            )
            with st.spinner("Refining query..."):
                st.session_state.workflow_result = application.submit_query(
                    prompt=st.session_state.active_query,
                    schema=_current_schema(),
                    api_key=api_key,
                    model=model,
                    clarifications=st.session_state.clarifications,
                    iteration=workflow.iteration + 1,
                    candidate_count=st.session_state.active_candidate_count,
                )
                _sync_usage_tracking(
                    api_key,
                    st.session_state,
                    force_refresh=True,
                )
            st.rerun()
        return

    _render_completed_workflow(workflow)


def _render_chat(
    application: ApplicationService,
    api_key: str,
    model: str,
    candidate_count: int,
) -> None:
    with st.container(
        height=_chat_window_height(st.session_state),
        border=False,
        key="chat_window",
    ):
        _render_welcome()
        _render_archived_conversations()
        if st.session_state.active_query:
            _render_message(st.session_state.active_query, "user")
            _render_clarification_history(
                st.session_state.clarification_history
            )
            _render_workflow_response(application, api_key, model)
            if st.session_state.query_pending:
                _render_message("Working on your query...", "assistant")

    workflow = st.session_state.workflow_result
    awaiting_clarification = (
        workflow is not None
        and workflow.state == ComponentState.PENDING
        and not workflow.complete
    )
    query_pending = st.session_state.query_pending
    prompt = st.chat_input(
        "Ask about your data...",
        disabled=(
            not st.session_state.data_ready
            or awaiting_clarification
            or query_pending
        ),
    )
    if prompt:
        _archive_active_conversation()
        _queue_query(prompt, candidate_count, st.session_state)
        st.rerun()

    if st.session_state.query_pending:
        with st.spinner("Comparing query interpretations..."):
            st.session_state.workflow_result = application.submit_query(
                prompt=st.session_state.active_query,
                schema=_current_schema(),
                api_key=api_key,
                model=model,
                iteration=1,
                candidate_count=st.session_state.active_candidate_count,
            )
            _sync_usage_tracking(
                api_key,
                st.session_state,
                force_refresh=True,
            )
        st.session_state.query_pending = False
        st.rerun()


def main() -> None:
    """Render the DB Whisperer Streamlit application."""
    st.set_page_config(
        page_title="DB Whisperer",
        page_icon=":material/database:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _initialize_state()
    _apply_styles()
    application = _application_service()
    api_key, model, candidate_count = _render_sidebar(application)
    _render_schema_browser(application)
    _render_chat(application, api_key, model, candidate_count)


if __name__ == "__main__":
    main()
