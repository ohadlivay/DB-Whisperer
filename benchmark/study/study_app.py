"""Human-in-the-loop study GUI (Protocol 1).

A standalone Streamlit app that runs one participant through the study described
in ``benchmark/HUMAN_IN_THE_LOOP.md``: read a plain-English goal, ask a fixed
question, react to either a direct answer or a clarifying question, and rate the
result. Stimuli are pre-generated and deterministic (``scenarios.json``), so it
needs no OpenRouter key and every participant sees identical answers.

Run from the repository root:

    streamlit run benchmark/study/study_app.py

Each completed task and the final session summary are appended as JSON lines to
``benchmark/study/results/<participant_id>.jsonl`` (git-ignored). The file holds
only de-identified demo data and the participant's ratings; no API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import streamlit as st

import sink
from study_logic import (
    TaskInstance,
    build_plan,
    filter_scenarios_by_dataset,
    load_scenarios,
    make_task_record,
)


STUDY_DIR = Path(__file__).resolve().parent
SCENARIOS_PATH = STUDY_DIR / "scenarios.json"
RESULTS_DIR = STUDY_DIR / "results"
RATING_HELP = "1 = not at all, 5 = completely"


def _config(secret_key: str, env_key: str) -> str | None:
    """A deployment setting from st.secrets (hosted) or the environment (local)."""
    try:
        value = st.secrets.get(secret_key)  # raises if no secrets file at all
    except Exception:  # noqa: BLE001 - no secrets configured locally is fine
        value = None
    if value:
        return str(value)
    return os.environ.get(env_key) or None


def _webhook_url() -> str | None:
    """URL each completed session is posted to, if configured for deployment."""
    return _config("results_webhook", "DB_WHISPERER_RESULTS_WEBHOOK")


def _allowed_datasets() -> list[str] | None:
    """Datasets the study is limited to (e.g. 'BikeStores' for a public link)."""
    raw = _config("study_datasets", "DB_WHISPERER_STUDY_DATASETS")
    return [name.strip() for name in raw.split(",") if name.strip()] if raw else None


def _study_scenarios() -> tuple[Any, ...]:
    """The scenarios this deployment serves, after any dataset restriction."""
    return filter_scenarios_by_dataset(
        load_scenarios(SCENARIOS_PATH), _allowed_datasets()
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _results_path(participant_id: str) -> Path:
    safe = "".join(c for c in participant_id if c.isalnum() or c in "-_") or "anon"
    return RESULTS_DIR / f"{safe}.jsonl"


def _append_record(participant_id: str, record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with _results_path(participant_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    # Also accumulate in memory so the whole session can be posted to the webhook
    # at the end — the local file above is wiped on a hosted app's restart.
    st.session_state._session_records.append(record)


def _flush_webhook() -> None:
    """Post the whole completed session to the results webhook, if configured."""
    url = _webhook_url()
    if not url:
        st.session_state._submit_status = None  # local-only (development) mode
        return
    payload = sink.build_session_payload(
        list(st.session_state._session_records),
        st.session_state.participant_id,
    )
    ok, message = sink.post_session(url, payload)
    st.session_state._submit_status = {"ok": ok, "message": message}


def _rating(label: str, key: str, help_text: str = RATING_HELP) -> int | None:
    """A 1–5 rating, genuinely unselected until the participant picks."""
    return st.radio(
        label, [1, 2, 3, 4, 5], index=None, horizontal=True, key=key, help=help_text
    )


def _single_choice(label: str, options: list[str], key: str) -> str | None:
    """A required single choice, genuinely unselected until picked."""
    return st.radio(label, options, index=None, key=key)


def _init_state() -> None:
    defaults = {
        "phase": "welcome",
        # Stable participant id, assigned once at Start. It must NOT be the
        # text_input's widget key: Streamlit garbage-collects widget keys when
        # the widget stops rendering (after the welcome screen), which would make
        # this setdefault regenerate a fresh id every rerun and scatter each
        # task's record into a different file.
        "participant_id": "",
        "plan": (),
        "idx": 0,
        "cur_idx": -1,
        "asked": False,
        "chosen": None,
        "started_at": 0.0,
        # Accumulated records for this session, posted to the webhook at the end.
        "_session_records": [],
        "_submit_status": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("pid_input", uuid4().hex[:8])


def _reset_task_state(idx: int) -> None:
    """Start the per-task sub-state the first time a task index is shown."""
    if st.session_state.cur_idx != idx:
        st.session_state.cur_idx = idx
        st.session_state.asked = False
        st.session_state.chosen = None
        st.session_state.started_at = time.time()


def _render_answer(answer: dict[str, Any]) -> None:
    columns = answer["columns"]
    rows = answer["rows"]
    if len(rows) == 1 and len(columns) == 1:
        st.metric(label=str(columns[0]), value=str(rows[0][0]))
        return
    st.dataframe(
        [dict(zip(columns, row, strict=True)) for row in rows],
        hide_index=True,
        width="stretch",
    )


def _render_welcome() -> None:
    st.title("Data assistant study")
    st.write(
        "Thanks for helping test a data assistant! You'll do a series of short "
        "tasks — about 15 minutes total. You're rating the **assistant**, so "
        "there are no right or wrong answers about you."
    )
    st.markdown(
        "**How each task works:**\n\n"
        "1. You read a **goal** — the thing you want to find out.\n"
        "2. You press **Ask the assistant**. It answers your question — and "
        "sometimes it first asks *you* a quick question back to make sure it "
        "understood. If it does, pick the option that fits your goal.\n"
        "3. You rate how confident you are that the answer is what your goal "
        "asked for."
    )
    st.divider()
    # The input uses its own widget key; the chosen value is copied to the
    # stable participant_id at Start (see _init_state for why they differ).
    st.text_input(
        "Your nickname",
        key="pid_input",
        help="Any nickname is fine — please don't use your real name. "
        "It only labels your answers anonymously.",
    )
    role = _single_choice(
        "Your background",
        ["General (no clinical training)", "Clinically / health-informatics trained"],
        "screen_role",
    )
    comfort = _rating(
        "How comfortable are you reading data tables or spreadsheets?",
        "screen_comfort",
        help_text="1 = not at all, 5 = very comfortable",
    )
    consent = st.checkbox(
        "I agree to take part and understand my anonymous responses will be "
        "recorded for research.",
        key="consent",
    )
    participant_id = st.session_state.pid_input.strip()
    ready = (
        consent
        and role is not None
        and comfort is not None
        and bool(participant_id)
    )
    if st.button("Start", type="primary", disabled=not ready):
        st.session_state.participant_id = participant_id
        st.session_state.plan = build_plan(_study_scenarios(), participant_id)
        _append_record(
            participant_id,
            {
                "type": "session_start",
                "participant_id": participant_id,
                "role": role,
                "data_comfort": comfort,
                "n_tasks": len(st.session_state.plan),
                "timestamp": _now(),
            },
        )
        st.session_state.phase = "task"
        st.rerun()


def _render_task() -> None:
    plan: tuple[TaskInstance, ...] = st.session_state.plan
    idx = st.session_state.idx
    if idx >= len(plan):
        st.session_state.phase = "wrapup"
        st.rerun()
        return

    instance = plan[idx]
    _reset_task_state(idx)

    st.progress(idx / len(plan), text=f"Task {idx + 1} of {len(plan)}")
    st.caption(f"Dataset: {instance.dataset}")

    st.markdown("##### 🎯 Your goal")
    st.info(instance.goal_text)

    st.markdown("##### 💬 You'll ask the assistant this")
    st.markdown(f"> {instance.question}")
    st.caption(
        "This exact question gets sent to the assistant. Your job is to judge "
        "whether its answer gives you your goal above."
    )

    if not st.session_state.asked:
        if st.button("Ask the assistant", type="primary"):
            st.session_state.asked = True
            st.rerun()
        return

    # The assistant has been "asked". The asking version of an ambiguous task
    # first shows a clarifying question; everything else answers directly.
    if instance.asks_question and st.session_state.chosen is None:
        st.markdown("##### 🤔 The assistant needs to check what you meant")
        st.warning(instance.clarification_question)
        st.caption("Choose the option that matches **your goal** at the top.")
        columns = st.columns(len(instance.interpretations))
        for column, interpretation in zip(
            columns, instance.interpretations, strict=True
        ):
            with column:
                if st.button(
                    interpretation["option_label"],
                    key=f"opt_{idx}_{interpretation['key']}",
                    width="stretch",
                ):
                    st.session_state.chosen = interpretation["key"]
                    st.rerun()
        return

    displayed_key = instance.displayed_key(st.session_state.chosen)
    st.markdown("##### ✅ The assistant's answer")
    _render_answer(instance.answer_for(displayed_key))

    _render_ratings_and_next(instance, idx)


def _render_ratings_and_next(instance: TaskInstance, idx: int) -> None:
    st.divider()
    trust = _rating(
        "How confident are you this answer gives you what your goal asked for?",
        f"trust_{idx}",
    )
    clarity = naturalness = None
    if instance.asks_question:
        clarity = _rating(
            "How clear was the follow-up question the assistant asked?",
            f"clarity_{idx}",
        )
        naturalness = _rating(
            "Did the two choices it offered make sense for your goal?",
            f"natural_{idx}",
        )

    needs = trust is None or (
        instance.asks_question and (clarity is None or naturalness is None)
    )
    if st.button("Next", type="primary", disabled=needs):
        record = make_task_record(
            participant_id=st.session_state.participant_id,
            instance=instance,
            position=idx,
            chosen_key=st.session_state.chosen,
            trust=trust,
            clarity=clarity,
            naturalness=naturalness,
            elapsed_seconds=round(time.time() - st.session_state.started_at, 2),
            timestamp=_now(),
        )
        record["type"] = "task"
        _append_record(st.session_state.participant_id, record)
        st.session_state.idx += 1
        st.rerun()


def _render_wrapup() -> None:
    st.title("Almost done")
    st.write("Two last questions about the overall experience.")
    overall = _single_choice(
        "When the assistant asked a follow-up question, did it feel...",
        ["Helpful", "Neutral", "Annoying"],
        "wrap_overall",
    )
    want = _single_choice(
        "Would you want a data assistant that sometimes asks one clarifying "
        "question before answering?",
        ["Yes", "It depends", "No"],
        "wrap_want",
    )
    comment = st.text_area(
        "Anything else? Any moment it misunderstood you? (optional)",
        key="wrap_comment",
    )
    if st.button("Finish", type="primary", disabled=overall is None or want is None):
        _append_record(
            st.session_state.participant_id,
            {
                "type": "session_end",
                "participant_id": st.session_state.participant_id,
                "overall_clarification_feel": overall,
                "would_want_clarification": want,
                "comment": comment.strip(),
                "timestamp": _now(),
            },
        )
        _flush_webhook()
        st.session_state.phase = "done"
        st.rerun()


def _render_done() -> None:
    st.title("Thank you! 🎉")
    status = st.session_state.get("_submit_status")
    if status and not status["ok"]:
        # Honest, not silent: the answers are saved locally but the upload
        # failed, so tell the participant rather than pretend it worked.
        st.warning(
            "Your answers were saved on this device, but we couldn't upload them "
            f"({status['message']}). Please let the researcher know."
        )
    else:
        st.success("Your responses have been recorded. You can close this tab.")
    st.caption(f"Participant ID: {st.session_state.participant_id}")


def main() -> None:
    st.set_page_config(page_title="Data assistant study", page_icon="🧪")
    _init_state()
    if not SCENARIOS_PATH.is_file():
        st.error(
            "scenarios.json is missing. Generate it first:\n\n"
            "`python benchmark/study/build_scenarios.py`"
        )
        return
    if not _study_scenarios():
        st.error(
            "No study tasks match the configured datasets. Check the "
            "`study_datasets` setting."
        )
        return
    phase = st.session_state.phase
    if phase == "welcome":
        _render_welcome()
    elif phase == "task":
        _render_task()
    elif phase == "wrapup":
        _render_wrapup()
    else:
        _render_done()


if __name__ == "__main__":
    main()
