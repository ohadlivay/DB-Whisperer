# Evaluation Framework Implementation Plan

This file is the persistent working plan for implementing the MIMIC-III
evaluation framework. Update it after each implementation session so the next
session can resume without reconstructing context from the chat.

## Goal

Build a fully simulated evaluation framework for DBWhisperer that compares:

- **Baseline:** direct `QueryService` single-pass NL-to-SQL with no ambiguity
  detection.
- **Full pipeline:** `ApplicationService` with join-path ambiguity detection,
  semantic-column ambiguity detection, clarification simulation, candidate
  generation/execution, and candidate-comparison judging.

The evaluation uses the bundled MIMIC-III clinical demo dataset. Deterministic
result comparison against gold SQL is the primary score. The initial
qualitative judge is the same Gemma model used by DBWhisperer and must be
reported as `self_judged: true`.

## Current Status

- `EVALUATION.md` has been rewritten to match the current architecture.
- The test suite design now contains 16 MIMIC-III cases.
- The HTML report requirement is documented: generate a nested report page
  matching `docs/db_whisperer_embedded_site.html`.
- Iteration 1 implementation has started:
  - Added `benchmark/mimic_ab_cases.json` with 16 MIMIC-III cases.
  - Added `tests/benchmark/test_mimic_cases.py` to validate the case-file
    contract.
  - Verified the JSON parses with PowerShell and resolves to the bundled MIMIC
    dataset.
- Iteration 1 verification completed:
  - Ran `tests.benchmark.test_mimic_cases` with
    `C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe`.
  - Result: 7 tests passed.
- Iteration 2 implementation completed:
  - Added `benchmark/mimic_ab_run.py`, a MIMIC-specific A/B harness skeleton.
  - Added `tests/benchmark/test_mimic_ab_run.py` for suite loading, raw
    baseline/full execution with fakes, and report-shape validation.
  - Ran `tests.benchmark.test_mimic_cases` and
    `tests.benchmark.test_mimic_ab_run` together.
  - Result: 12 tests passed.
- Iteration 3 implementation completed:
  - Extended `benchmark/mimic_ab_run.py` with a simulated clarification loop
    for the full-pipeline arm.
  - Added deterministic clarification option selection from each case's
    free-text `simulated_user_answer` using exact/substring/token-overlap
    matching.
  - Added reliability warnings when the full pipeline asks an unexpected
    clarification, asks additional clarifications, or no option matches the
    simulated answer.
  - Updated `tests/benchmark/test_mimic_ab_run.py` with clarification
    simulation coverage.
  - Ran `tests.benchmark.test_mimic_cases` and
    `tests.benchmark.test_mimic_ab_run` together.
  - Result: 14 tests passed.
- Iteration 4 implementation completed:
  - Added deterministic gold SQL execution and exact-result scoring to
    `benchmark/mimic_ab_run.py`.
  - Added no-SQL expected scoring for safety, missing-schema, and
    underspecified cases.
  - Added baseline/full score comparison, score deltas, summary metrics,
    clarification rates, spurious clarification rates, and unreliable-case
    reporting.
  - Updated `tests/benchmark/test_mimic_ab_run.py` with deterministic scoring
    and summary tests.
  - Ran `tests.benchmark.test_mimic_cases` and
    `tests.benchmark.test_mimic_ab_run` together.
  - Result: 20 tests passed.

## Iteration Plan

### Iteration 1 - Case File And Schema Validation

Status: implemented, pending Python test execution in an environment with a
Python interpreter.

Create `benchmark/mimic_ab_cases.json` from `EVALUATION.md`.

Expected work:

- Add all 16 MIMIC-III cases in machine-readable form.
- Include fields: `id`, `category`, `question`, `ambiguous`,
  `ambiguity_type`, `intent`, `schema_elements`, `expected_sql`,
  `should_clarify`, `simulated_user_answer`, and `tests`.
- Add or update tests that validate the case file shape.
- Keep this iteration independent of OpenRouter calls.

Exit criteria:

- Case JSON exists.
- Tests can load and validate every case.
- Every case has deterministic gold SQL or an explicit no-SQL safety/failure
  expectation.

### Iteration 2 - MIMIC A/B Harness Skeleton

Status: implemented and focused tests passing.

Create or extend a harness that can run MIMIC cases through both arms.

Expected work:

- Prefer a new `benchmark/mimic_ab_run.py` unless reusing `ab_run.py` is cleaner.
- Load the bundled MIMIC-III demo CSV directory through ETL.
- Produce one shared benchmark DuckDB database.
- Run baseline arm through `QueryService`.
- Run full arm through `ApplicationService`.
- Save raw per-case outputs to `benchmark/results/`.

Exit criteria:

- Harness can run a small subset of non-ambiguous cases.
- Results JSON contains baseline and full-pipeline sections per case.

### Iteration 3 - Clarification Simulation

Status: implemented and focused tests passing.

Add simulated user handling for pending clarification states.

Expected work:

- Detect `ComponentState.PENDING` from the full pipeline.
- Record question, options, mechanism, and reason.
- Select the case-declared simulated answer.
- Continue the workflow with that clarification.
- Mark cases unreliable if the clarification cannot be matched.

Exit criteria:

- Join-path and semantic-column cases can complete without human input.
- Result JSON records full clarification history.

### Iteration 4 - Deterministic Scoring

Status: implemented and focused tests passing.

Implement primary automatic scoring.

Expected work:

- Execute gold SQL against the same DuckDB database.
- Compare generated result tables to gold result tables.
- Record SQL validation errors, execution errors, and read-only safety failures.
- Compute summary metrics: baseline score, full score, ambiguous/control splits,
  clarification rate, spurious clarification rate, and win/tie/loss.

Exit criteria:

- Deterministic scores are present for each arm and each case.
- Summary metrics are stable and derived only from structured results.

### Iteration 5 - Gemma Self-Judge

Add optional qualitative judging.

Expected work:

- Use the configured model as the initial judge.
- Mark reports with `self_judged: true` when judge model equals system model.
- Judge only qualitative dimensions: clarification quality, faithfulness,
  clinical reasonableness, and trust/usefulness notes.
- Keep deterministic scores authoritative.

Exit criteria:

- Harness can run with or without judge calls.
- Judge output is clearly separated from deterministic scoring.

### Iteration 6 - HTML Report Generator

Generate a static report page from a JSON result artifact.

Expected work:

- Add a renderer, for example `benchmark/render_evaluation_report.py`.
- Read a benchmark JSON report.
- Write `docs/evaluation_report.html`.
- Match the visual style of `docs/db_whisperer_embedded_site.html`.
- Include overview, metric cards, visual comparisons, per-case table,
  discussion, limitations, and conclusions.

Exit criteria:

- A sample JSON report can render into a useful standalone HTML page.
- The page is suitable to link from the existing project site.

### Iteration 7 - Site Integration And Full Verification

Wire the generated page into the docs site and run verification.

Expected work:

- Add or document a link from `docs/db_whisperer_embedded_site.html`.
- Add tests for report rendering from fixture data.
- Run focused benchmark tests.
- Run `python -m unittest discover`.
- Optionally run a small MIMIC subset before the full evaluation.

Exit criteria:

- Implementation is documented, tested, and usable from a clean checkout.
- A full evaluation run can produce JSON plus HTML artifacts.

## Notes And Constraints

- Do not log or persist OpenRouter API keys.
- Treat prompt logs, generated SQL, and result artifacts as sensitive.
- Do not commit generated DuckDB files, prompt logs, or large benchmark results.
- Keep the deterministic harness independent from the self-judge so an
  independent judge model can be swapped in later.
- Prefer additive benchmark files over disrupting the existing BikeStores
  benchmark until the MIMIC harness is stable.
