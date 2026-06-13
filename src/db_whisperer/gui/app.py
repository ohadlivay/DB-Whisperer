"""Streamlit interface for DB Whisperer."""

from __future__ import annotations

from html import escape
import os
from typing import Any

import streamlit as st

from db_whisperer.application import ApplicationService
from db_whisperer.contracts import ComponentState, CsvUpload, SchemaMetadata


def _initialize_state() -> None:
    defaults: dict[str, Any] = {
        "active_query": "",
        "data_ready": False,
        "upload_signature": (),
        "ingestion_result": None,
        "workflow_result": None,
        "clarifications": (),
        "clarification_history": (),
        "active_candidate_count": 3,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _apply_styles() -> None:
    """Apply the visual tokens and layout from the Stitch design."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400&display=swap');

        :root {
            color-scheme: light;
            --surface: #f8f9ff;
            --surface-lowest: #ffffff;
            --surface-low: #eff4ff;
            --surface-variant: #d3e4fe;
            --outline: #c6c6cd;
            --text: #0b1c30;
            --text-muted: #45464d;
            --secondary: #006c49;
        }

        html, body, [class*="css"] {
            font-family: "Inter", sans-serif;
        }

        .stApp, [data-testid="stAppViewContainer"] {
            background: var(--surface-lowest);
            color: var(--text);
        }

        [data-testid="stHeader"] {
            background: var(--surface-lowest);
        }

        [data-testid="stSidebar"] {
            width: 320px !important;
            min-width: 320px !important;
            background: var(--surface);
            border-right: 1px solid var(--outline);
        }

        [data-testid="stSidebar"] > div:first-child {
            width: 320px !important;
        }

        [data-testid="stSidebar"] .block-container,
        [data-testid="stSidebarUserContent"] {
            padding: 0 !important;
        }

        .block-container {
            max-width: 960px;
            padding: 2rem 2rem 7rem;
        }

        .sidebar-brand {
            padding: 1.5rem;
            border-bottom: 1px solid var(--outline);
            font-size: 1.45rem;
            font-weight: 600;
            letter-spacing: -0.02em;
            color: var(--text);
        }

        .sidebar-section {
            margin: 1.5rem 1.5rem 0.5rem;
            color: var(--text-muted);
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploader"],
        [data-testid="stSidebar"] [data-testid="stTextInput"],
        [data-testid="stSidebar"] [data-testid="stNumberInput"],
        [data-testid="stSidebar"] [data-testid="stSelectbox"] {
            margin-left: 1.5rem;
            margin-right: 1.5rem;
            width: auto;
        }

        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
            background: var(--surface);
            border: 2px dashed var(--outline);
            border-radius: 0.5rem;
            padding: 1.2rem 0.6rem;
        }

        [data-testid="stSidebar"] hr {
            border-color: var(--outline);
            margin: 1.5rem 0 0;
        }

        .status-row {
            display: flex;
            align-items: center;
            gap: 0.55rem;
            padding: 0.3rem 1.5rem;
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        .status-dot {
            width: 0.5rem;
            height: 0.5rem;
            border-radius: 50%;
            background: #9ca3af;
        }

        .status-dot.ready {
            background: var(--secondary);
            box-shadow: 0 0 8px rgba(0, 108, 73, 0.5);
        }

        .chat-stream {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            padding-top: 0.5rem;
        }

        .message-row {
            display: flex;
            width: 100%;
        }

        .message-row.user {
            justify-content: flex-end;
        }

        .message-row.assistant {
            justify-content: flex-start;
        }

        .message {
            max-width: 80%;
            padding: 1rem;
            border: 1px solid var(--outline);
            box-shadow: 0 1px 3px rgba(11, 28, 48, 0.06);
            color: var(--text);
            font-size: 0.95rem;
            line-height: 1.5;
        }

        .message.user {
            background: var(--surface-low);
            border-color: var(--surface-variant);
            border-radius: 1rem 1rem 0 1rem;
        }

        .message.assistant {
            background: var(--surface);
            border-radius: 1rem 1rem 1rem 0;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--outline);
            border-radius: 0.75rem;
        }

        [data-testid="stExpander"] {
            border-color: var(--outline);
        }

        code, pre {
            font-family: "JetBrains Mono", monospace !important;
        }

        [data-testid="stChatInput"] {
            background: linear-gradient(
                to top,
                var(--surface-lowest) 72%,
                rgba(255, 255, 255, 0)
            );
            padding-top: 1.5rem;
        }

        [data-testid="stChatInput"] > div {
            border-color: var(--outline);
            border-radius: 9999px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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


@st.cache_resource
def _application_service() -> ApplicationService:
    """Create the application coordinator once per Streamlit process."""
    return ApplicationService()


def _ingest_upload(
    upload: Any | None,
    application: ApplicationService,
) -> None:
    signature = (upload.name, upload.size) if upload is not None else ()
    if signature == st.session_state.upload_signature:
        return

    st.session_state.upload_signature = signature
    st.session_state.active_query = ""
    st.session_state.workflow_result = None
    st.session_state.clarifications = ()
    st.session_state.clarification_history = ()
    if upload is None:
        st.session_state.ingestion_result = None
        st.session_state.data_ready = False
        return

    result = application.ingest_csvs(
        [
            CsvUpload(
                name=upload.name,
                content=upload.getvalue(),
                content_type=upload.type or "text/csv",
            )
        ]
    )
    st.session_state.ingestion_result = result
    st.session_state.data_ready = result.state == ComponentState.ACCEPTED


def _render_sidebar(
    application: ApplicationService,
) -> tuple[str, str, int]:
    with st.sidebar:
        st.markdown(
            '<div class="sidebar-brand">DB Whisperer</div>',
            unsafe_allow_html=True,
        )

        _section_label("Data source")
        upload = st.file_uploader(
            "Upload a CSV file",
            type=["csv"],
            accept_multiple_files=False,
            label_visibility="collapsed",
        )
        _ingest_upload(upload, application)

        _section_label("LLM configuration")
        entered_api_key = st.text_input(
            "OpenRouter API key",
            type="password",
            placeholder="sk-or-v1-...",
        )
        model = st.text_input(
            "Model",
            value=os.getenv("OPENROUTER_MODEL", ""),
            placeholder="provider/model",
        )
        candidate_count = int(st.number_input(
            "SQL candidates",
            min_value=2,
            max_value=10,
            value=3,
            step=1,
        ))
        api_key = entered_api_key or os.getenv("OPENROUTER_API_KEY", "")

        st.divider()
        _status(
            "DuckDB ready" if st.session_state.data_ready else "Waiting for data",
            st.session_state.data_ready,
        )
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

    return api_key, model, candidate_count


def _render_user_message() -> None:
    st.markdown(
        f"""
        <div class="message-row user">
            <div class="message user">{escape(st.session_state.active_query)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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


def _render_clarification_history() -> None:
    for question, answer in st.session_state.clarification_history:
        _render_message(question, "assistant")
        _render_message(answer, "user")


def _format_clarification(question: str, answer: str) -> str:
    """Format the selected ambiguity choice for the Querier prompt."""
    return (
        f"Question: {question.strip()}\n"
        f"Selected answer: {answer.strip()}"
    )


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
            st.rerun()
        return

    result = workflow.query_result
    if result is None:
        _render_message(workflow.message, "assistant")
        return

    _render_message(result.message, "assistant")
    records = [
        dict(zip(result.columns, row, strict=True))
        for row in result.rows
    ]
    st.dataframe(records, width="stretch", hide_index=True)
    if result.truncated:
        st.caption("Results were limited to the first 1,000 rows.")
    if result.sql:
        with st.expander("View generated SQL"):
            st.code(result.sql, language="sql")


def _render_chat(
    application: ApplicationService,
    api_key: str,
    model: str,
    candidate_count: int,
) -> None:
    if st.session_state.active_query:
        st.markdown('<div class="chat-stream">', unsafe_allow_html=True)
        _render_user_message()
        _render_clarification_history()
        _render_workflow_response(application, api_key, model)
        st.markdown("</div>", unsafe_allow_html=True)

    workflow = st.session_state.workflow_result
    awaiting_clarification = (
        workflow is not None
        and workflow.state == ComponentState.PENDING
        and not workflow.complete
    )
    prompt = st.chat_input(
        "Ask about your data...",
        disabled=(
            not st.session_state.data_ready
            or awaiting_clarification
        ),
    )
    if prompt:
        st.session_state.active_query = prompt.strip()
        st.session_state.clarifications = ()
        st.session_state.clarification_history = ()
        st.session_state.active_candidate_count = candidate_count
        with st.spinner("Comparing query interpretations..."):
            st.session_state.workflow_result = application.submit_query(
                prompt=prompt.strip(),
                schema=_current_schema(),
                api_key=api_key,
                model=model,
                iteration=1,
                candidate_count=candidate_count,
            )
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
    _render_chat(application, api_key, model, candidate_count)


if __name__ == "__main__":
    main()
