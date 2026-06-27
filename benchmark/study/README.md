# Human-in-the-loop study GUI

A runnable implementation of **Protocol 1** from
[`../HUMAN_IN_THE_LOOP.md`](../HUMAN_IN_THE_LOOP.md): put a real person in front
of the assistant and measure whether asking a clarifying question helps them,
confuses them, or annoys them.

## Run it

```powershell
# (one time, or after editing the suites) build the fixed stimuli
python benchmark/study/build_scenarios.py

# launch the study for one participant
streamlit run benchmark/study/study_app.py
```

No OpenRouter key is needed — the app never calls a model (see *Deterministic
stimuli* below).

## What a participant does

`streamlit run` opens a short wizard:

1. **Consent + two screening questions** (background, comfort with data).
2. **8 tasks.** Each task shows a plain-English **goal**, a fixed **question**,
   and an **Ask** button. Then either:
   - the assistant answers directly (the *direct* version), or
   - it asks one **two-option** clarifying question; the participant clicks the
     option matching their goal, then sees the answer (the *asking* version).
   They rate **trust** (1–5), plus **clarity** and **naturalness** when asked.
3. **Two wrap-up questions** about the overall experience.

Each participant sees every task once; half the tasks use the asking version and
half the direct version, balanced and ordered deterministically from the
participant id (so a session is reproducible and the comparison is
counterbalanced). For an ambiguous task the participant gets one of the two
sibling goals, so the goal text never hints at the "expected" answer.

## Deterministic stimuli (why no model is called)

A controlled study needs every participant to see the *same* answers; live
generation at temperature 1.3 would make each session different and confound the
comparison. So `build_scenarios.py` pre-computes each task's real answer table by
running the benchmark **gold queries** (from `../ab_cases.json` and
`../mimic_ab_cases.json`) against the bundled datasets, and writes them to
`scenarios.json`. The gold SQL is the single source of truth, so the study can
never drift from the A/B benchmark it mirrors. The *direct* version shows the
interpretation a single-pass baseline is assumed to guess (`baseline_pick`, the
direct/shortest reading); the *asking* version shows whichever option the
participant clicks.

## Results

Each session appends JSON lines to `results/<participant_id>.jsonl`
(git-ignored). One `session_start`, one record per task, one `session_end`.
Per-task fields include `version` (`asking`/`direct`), `ambiguous`, `goal_id`,
`chosen_key`, `displayed_key`, `correct` (did the final answer match the goal),
`comprehension` (did the click match the goal — asking + ambiguous only),
`trust`, `clarity`, `naturalness`, and `elapsed_seconds`. These map directly onto
the aggregate metrics in `../HUMAN_IN_THE_LOOP.md`: intent-match accuracy per
version, clarification comprehension, and trust deltas.

## Honest limitations

- **Scripted, not live.** The GUI replays fixed stimuli; it does not exercise the
  real pipeline's variance. That is deliberate for a controlled study, but it
  means the clarifying questions/options shown are authored, not whatever a given
  model would emit on the day. (To study the model's *actual* phrasing, use
  Protocol 2 on real `results/ab_*.json` reports.)
- **Controls don't simulate spurious asks by default.** A control task answers
  directly in both versions, so it measures baseline trust, not the annoyance of
  an unnecessary question. Add a clarification to a control in
  `build_scenarios.py` if you want to test that.
- **Automated testing is partial.** The session logic is unit-tested
  (`tests/benchmark/test_study.py`) and the consent→task→ask path has a smoke
  test, but Streamlit's `AppTest` cannot script the full multi-page wizard, so
  click-through the flow once with `streamlit run` before fielding it.
- **Ethics.** Even the MIMIC demo is health data; recruit clinically literate
  participants for the MIMIC tasks and follow the consent/IRB notes in
  `../HUMAN_IN_THE_LOOP.md`.
