"""Read-only live Streamlit dashboard for an Evaluation V2 campaign."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import streamlit as st


def campaign_argument() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--campaign-dir", type=Path, default=None)
    args, _ = parser.parse_known_args()
    configured = args.campaign_dir or os.getenv("DBW_V2_CAMPAIGN_DIR")
    if not configured:
        raise ValueError("--campaign-dir or DBW_V2_CAMPAIGN_DIR is required")
    return Path(configured).expanduser().resolve()


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def filtered(rows: list[dict[str, Any]], field: str, selected: str) -> list[dict[str, Any]]:
    return rows if selected == "all" else [row for row in rows if str(row.get(field, "")) == selected]


st.set_page_config(page_title="DBWhisperer V2 Live", page_icon="📊", layout="wide")
CAMPAIGN_DIR = campaign_argument()


@st.fragment(run_every="2s")
def live_dashboard() -> None:
    status = read_json(CAMPAIGN_DIR / "status.json")
    events = read_jsonl(CAMPAIGN_DIR / "events.jsonl", 3000)
    st.title("DBWhisperer Evaluation V2")
    st.caption(f"Read-only local monitor · {CAMPAIGN_DIR}")
    cols = st.columns(6)
    cols[0].metric("State", status.get("state", "starting"))
    cols[1].metric("Progress", f"{100 * float(status.get('progress', 0)):.1f}%")
    cols[2].metric("Completed", f"{status.get('completed_units', 0)}/{status.get('total_units', 0)}")
    cols[3].metric("Passed", status.get("passed", 0))
    cols[4].metric("Model calls", status.get("model_calls", 0))
    cols[5].metric("Cost", f"${float(status.get('cost_usd', 0)):.4f} / ${float(status.get('budget_usd', 0)):.2f}")
    st.progress(float(status.get("progress", 0)))
    current = status.get("current", {})
    st.info(
        "Current: "
        + " · ".join(f"{key}={value}" for key, value in current.items())
        if current else "Waiting for runner updates."
    )
    if status.get("latest_error") and status.get("state") not in {"running", "resuming", "complete"}:
        st.error(status["latest_error"])

    overview, matrix, usage, event_tab, raw_tab = st.tabs(
        ["Overview", "Run matrix", "Usage", "Events & errors", "Sensitive raw logs"]
    )
    with overview:
        st.json({key: value for key, value in status.items() if key not in {"latest_error"}})
        recent = [row for row in events if row.get("event") == "case_arm_completed"][-20:]
        st.subheader("Recent case results")
        st.dataframe(recent, width="stretch")
    with matrix:
        complete = [row for row in events if row.get("event") == "case_arm_completed"]
        st.dataframe(complete, width="stretch")
    with usage:
        st.json({key: status.get(key) for key in ("model_calls", "retries", "prompt_tokens", "completion_tokens", "cost_usd", "budget_usd", "elapsed_seconds", "eta_seconds")})
        calls = [row for row in events if row.get("event") == "model_call_completed"]
        st.dataframe(calls[-250:], width="stretch")
    with event_tab:
        severities = ["all", *sorted({str(row.get("severity", "")) for row in events if row.get("severity")})]
        severity = st.selectbox("Severity", severities)
        selected = filtered(events, "severity", severity)
        event_types = ["all", *sorted({str(row.get("event", "")) for row in selected if row.get("event")})]
        event_type = st.selectbox("Event", event_types)
        selected = filtered(selected, "event", event_type)
        st.dataframe(selected[-1000:], width="stretch")
    with raw_tab:
        st.warning("Prompts and responses may contain sensitive database samples. Keep this dashboard on localhost.")
        reveal = st.toggle("Reveal sensitive raw logs", value=False)
        if reveal:
            raw = read_jsonl(CAMPAIGN_DIR / "prompts.jsonl", 500)
            components = ["all", *sorted({str(row.get("component", "")) for row in raw if row.get("component")})]
            component = st.selectbox("Component", components)
            st.dataframe(filtered(raw, "component", component), width="stretch")
        else:
            st.caption(f"Raw log path: {CAMPAIGN_DIR / 'prompts.jsonl'}")


live_dashboard()
