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
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import streamlit as st

from study_logic import (
    TaskInstance,
    build_plan,
    load_scenarios,
    make_task_record,
)


STUDY_DIR = Path(__file__).resolve().parent
SCENARIOS_PATH = STUDY_DIR / "scenarios.json"
RESULTS_DIR = STUDY_DIR / "results"
RATING_HELP = "1 = not at all, 5 = completely"
# A leading sentinel keeps every choice explicit (nothing is pre-selected) while
# still giving each widget a real default value, which the rest of the app and
# the tests rely on.
SENTINEL = "— select —"
RATING_CHOICES = [SENTINEL, "1", "2", "3", "4", "5"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _results_path(participant_id: str) -> Path:
    safe = "".join(c for c in participant_id if c.isalnum() or c in "-_") or "anon"
    return RESULTS_DIR / f"{safe}.jsonl"


def _append_record(participant_id: str, record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with _results_path(participant_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _rating(label: str, key: str, help_text: str = RATING_HELP) -> int | None:
    """A 1–5 rating that returns ``None`` until the participant picks."""
    choice = st.radio(
        label, RATING_CHOICES, index=0, horizontal=True, key=key, help=help_text
    )
    return None if choice == SENTINEL else int(choice)


def _single_choice(label: str, options: list[str], key: str) -> str | None:
    """A required single choice that returns ``None`` until picked."""
    choice = st.radio(label, [SENTINEL, *options], index=0, key=key)
    return None if choice == SENTINEL else choice


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
        "Thanks for helping! You'll work through a series of short tasks. For "
        "each one you'll read a goal, ask the assistant a question, and rate how "
        "much you trust the answer. There are no right or wrong responses about "
        "*you* — we're testing the assistant, not you. It takes about 15 minutes."
    )
    # The input uses its own widget key; the chosen value is copied to the
    # stable participant_id at Start (see _init_state for why they differ).
    st.text_input("Participant ID", key="pid_input")
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
        st.session_state.plan = build_plan(
            load_scenarios(SCENARIOS_PATH),
            participant_id,
        )
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

    st.markdown("##### Your goal")
    st.info(instance.goal_text)

    st.markdown("##### The question being asked")
    st.markdown(f"> {instance.question}")

    if not st.session_state.asked:
        if st.button("Ask the assistant", type="primary"):
            st.session_state.asked = True
            st.rerun()
        return

    # The assistant has been "asked". The asking version of an ambiguous task
    # first shows a clarifying question; everything else answers directly.
    if instance.asks_question and st.session_state.chosen is None:
        st.markdown("##### The assistant needs to check something")
        st.warning(instance.clarification_question)
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
    st.markdown("##### Answer")
    _render_answer(instance.answer_for(displayed_key))

    _render_ratings_and_next(instance, idx)


def _render_ratings_and_next(instance: TaskInstance, idx: int) -> None:
    st.divider()
    trust = _rating(
        "How confident are you that this answer gives you what you wanted?",
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
        st.session_state.phase = "done"
        st.rerun()


def _render_done() -> None:
    st.title("Thank you! 🎉")
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
