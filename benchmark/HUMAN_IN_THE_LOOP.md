# Human-in-the-loop benchmark

This defines how to measure the half of the project's research question that the
automated A/B harness (`ab_run.py`) deliberately leaves out. **Protocol 1 has a
runnable GUI** in [`study/`](study/) (`streamlit run benchmark/study/study_app.py`);
Protocol 2 and the live recruitment study remain proposals.

## Why a human study is needed

The A/B harness drives the full pipeline with a **simulated user** that always
answers a clarifying question with the interpretation the case declares. That is
the right instrument for one claim — *does asking the right question let the
system reach the correct SQL?* — but it is silent on the PDF's second axis,
**user trust and perceived usefulness**, and it quietly assumes three things a
real user might not satisfy:

1. **Oracle intent.** The simulated user knows the gold interpretation by
   construction, so a confusing or mislabeled clarifying question still gets
   answered correctly. A real user can misread the question and pick the wrong
   option even when the system did everything else right.
2. **Costless friction.** The simulated user is never annoyed by an unnecessary
   question. A real user may lose trust when the system asks on an unambiguous
   request (the `spurious_clarification_rate` cases).
3. **Natural ambiguity.** The simulated user answers whatever two options are
   presented. A real user would be confused by an *unnatural* option — exactly
   the failure the MIMIC density finding predicts, where the "longest path" is a
   chain through an unrelated fact table.

The human study measures (1) comprehension, (2) trust, and (3) naturalness
directly, against the same two arms and the same datasets.

## Research questions and hypotheses

- **RQ1 (accuracy under real users).** Does the full pipeline produce answers
  that match the user's *true* intent more often than the baseline, when a real
  human — not an oracle — answers the clarification?
  - H1: full ≥ baseline on intent-match for ambiguous tasks; full ≈ baseline on
    control tasks.
- **RQ2 (comprehension).** When asked, do users pick the option matching their
  true intent?
  - H2: clarification-comprehension rate is high (e.g. ≥ 0.8) for clean
    (BikeStores, MIMIC `d_labitems`) cases and **lower** for the dense MIMIC
    pairs whose options are unnatural — quantifying the density finding.
- **RQ3 (trust / preference).** Do users trust and prefer the asking arm, and is
  trust *hurt* by spurious questions on control tasks?
  - H3: higher trust for full on ambiguous tasks; no significant trust loss on
    control tasks (or, if there is loss, measure it).

## Protocol 1 — task-based blind A/B user study (primary)

**Design:** within-subject, two arms, blinded, order-counterbalanced.

- **Arms.** Baseline = `QueryService` (single pass, never asks). Full =
  `ApplicationService` with Component B (may ask one two-option question). These
  are the exact arms `ab_run.py` already wires.
- **Tasks.** Reuse the A/B case questions (BikeStores `ab_cases.json` + MIMIC
  `mimic_ab_cases.json`), plus a few extra natural tasks. Each participant gets,
  per task, a **plain-English goal that encodes a ground-truth intent without
  naming tables or columns** — e.g. *"You want the products physically on the
  shelves of this store right now"* (maps to the `store_products_stocked`
  interpretation). The goal text is the oracle the *experimenter* holds; the
  participant only sees the goal and the system, never the gold SQL.
- **Procedure per task.** The participant reads the goal, issues the fixed
  question, and for the full arm clicks one clarification option if asked, then
  rates the result. Arm assignment is hidden; order is counterbalanced (Latin
  square) so learning and fatigue do not confound the arm comparison.
- **Populations.** BikeStores tasks: general participants. MIMIC tasks:
  clinically literate participants (clinicians, nurses, med/health-informatics
  students), because lay users cannot judge whether a clinical clarification is
  natural.

**Per-task measures**

| measure | type | how |
| --- | --- | --- |
| intent-match correctness | objective 0–4 | score the final result table against the gold query for the *assigned* intent, reusing `score_result`/`judge` from the harness |
| clarification comprehension | objective bool (full arm, ambiguous) | did the option the user clicked correspond to the assigned intent's path? |
| trust | Likert 1–5 | "How confident are you this answer is what you wanted?" |
| clarity | Likert 1–5 (full arm, when asked) | "How clear was the question the system asked?" |
| naturalness | Likert 1–5 (full arm, when asked) | "Did the choice it offered make sense for your goal?" |
| effort | seconds, clicks | instrument the study UI |
| preference | forced choice | after both arms for a task: "Which did you trust more?" |

## Protocol 2 — expert clarification-quality annotation (cheap, no live users)

The A/B report already preserves every clarification question and its two
options verbatim (`cases[].full.clarifications`). Have 3+ blind raters score
each recorded clarification on:

- **Clarity** — is it non-technical / ELI5?
- **Discriminativeness** — are the two options genuinely different and mutually
  exclusive?
- **Faithfulness** — do the options actually match the two join paths the
  mechanism found?
- **Naturalness** — is this an ambiguity a real user would plausibly have, or an
  artifact (e.g. a MIMIC longest-path chain through an unrelated table)?

Report inter-rater reliability (Krippendorff's α). This protocol is the
fastest way to quantify the MIMIC density finding: it should show high
naturalness for BikeStores and the `d_labitems` cases and low naturalness for
the dense-pair options, motivating semantic path pruning.

## Aggregate metrics

- Intent-match accuracy: full vs baseline, split ambiguous / control (the human
  analogue of the existing `summary` split).
- Clarification-comprehension rate, per dataset and per pair-cleanliness.
- Mean trust and trust delta (full − baseline); control-task trust delta to test
  the spurious-question cost.
- Net preference for full.
- Protocol 2: mean clarity / discriminativeness / faithfulness / naturalness +
  α, per dataset.

## Implementation

This is built in [`study/`](study/) — a standalone Streamlit app, not a change to
the product pipeline. `study/README.md` has the details; in short:

- A study driver loads a task list (goal text + the existing case `id` so gold
  and `clarification_path_index` are reused for scoring).
- To keep stimuli identical across participants, it does **not** call the model
  live. `build_scenarios.py` pre-computes each task's real answer table by running
  the A/B gold queries against the bundled datasets, so every participant sees the
  same answers and the study can't drift from the benchmark.
- Per task it assigns a version — *asking* (may ask one clarifying question) or
  *direct* (answers straight away) — balanced and seeded by the participant id;
  for the asking version a **real** click selects the interpretation.
- It appends one JSON line per task to `results/<participant_id>.jsonl`:
  version, goal, chosen option, `correct`, `comprehension`, the Likert ratings,
  and timing. Only de-identified demo data and ratings; no API keys.
- Correctness is scored against the goal's gold interpretation, the same gold the
  automated A/B uses, so both studies share one rubric.

A live variant (calling `QueryService` / `ApplicationService` as `run_full` does)
is possible but trades the deterministic, reproducible stimulus for the model's
run-to-run variance; the scripted build is the better controlled instrument.

## Analysis and rigor

- Pre-register H1–H3 and the analysis before collecting data.
- Within-subject comparison: paired tests (Wilcoxon signed-rank for Likert,
  McNemar for the paired correctness/comprehension bools). Mixed-effects models
  with random effects for participant and task if N allows.
- Power: a within-subject preference/trust effect is detectable at modest N
  (≈ 12–20); pilot first to estimate variance.

## Threats to validity and ethics

- **Demand characteristics / blinding.** Participants must not infer which arm
  "should" be better; hence hidden arm labels and counterbalancing.
- **Goal priming.** The plain-English goal must not leak the schema path
  (no table/column names), or it trivializes option selection.
- **MIMIC access.** Full MIMIC-III requires PhysioNet credentialing; this study
  uses the **demo** (de-identified, ODbL), which is lower-risk, but recruit
  clinically literate raters and keep the row-level data inside the study tool.
  Obtain consent and, if the institution requires it, IRB/ethics review.
- **Generalization.** Two datasets and fixed tasks; report as a bounded study,
  not a universal claim, mirroring the "treat one report as a sample" caveat of
  the automated harness.

## Minimal first step

Run **Protocol 2** on the clarifications already captured in existing
`results/ab_*.json` reports — it needs no live participants and immediately
quantifies clarification clarity and the MIMIC naturalness gap. Use that to
refine the questions and option phrasing before investing in the live Protocol 1
study.
