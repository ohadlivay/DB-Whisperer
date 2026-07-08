# DB Whisperer Evaluation Process

This document is the running record of the DB Whisperer evaluation work. It is
intended to explain how the evaluation works, what has already been built, what
problems were encountered, how they were handled, and what remains before the
final 10-run aggregate report is complete.

## End Goal

Produce a final evaluation report based on 10 separate MIMIC-III benchmark runs.
The report should aggregate the runs rather than present one sample run as the
final result. The primary evidence should be deterministic correctness against
reference SQL results. LLM judging is optional qualitative context and is not
part of the initial 10-run aggregate.

## Evaluation Design

The evaluation compares two system architectures on the same clinical
questions and the same DuckDB database built from the bundled MIMIC-III demo
CSV files.

- Baseline: direct `QueryService` single-pass natural-language-to-SQL. It does
  not run the ambiguity layer and does not ask clarifying questions.
- Full pipeline: `ApplicationService` with join-path ambiguity detection,
  semantic-column ambiguity detection, clarification simulation, candidate SQL
  generation, execution, and candidate-comparison judging.

Each case has an evaluator-written reference SQL query when an executable
answer is expected. The benchmark executes the reference SQL and compares the
system's returned table to the reference table. This strict deterministic
comparison is the main score.

## Implemented Framework

The MIMIC evaluation framework has already been implemented in earlier
sessions:

- `benchmark/mimic_ab_cases.json` defines 16 MIMIC-III clinical evaluation
  cases.
- `benchmark/mimic_ab_run.py` runs both arms, simulates clarifications, scores
  deterministically, optionally calls a qualitative LLM judge, and writes JSON
  reports under `benchmark/results/`.
- `benchmark/render_evaluation_report.py` renders a saved report into
  `docs/evaluation_report.html` and `docs/evaluation_report_cases.html`.
- The docs site links to the generated evaluation report.
- Focused tests cover case validation, the MIMIC harness, report rendering, and
  docs-site integration.

## Operational Findings So Far

Initial smoke runs confirmed the harness was structurally working, but API
access blocked useful model results:

- The default `google/gemma-4-31b-it` route returned `402 Payment Required`
  with the first key.
- The free `google/gemma-4-31b-it:free` route returned `429 Too Many Requests`,
  even after serializing full-pipeline candidate generation.
- A separate `tencent/hy3:free` route produced `400 Bad Request` and `429 Too
  Many Requests`, so it was not a viable evaluation route.

To handle rate-limited routes more safely, `benchmark/mimic_ab_run.py` was
extended with:

```powershell
--max-parallel-candidates 1
```

This keeps the full-pipeline arm from launching candidate generations in
parallel, reducing burst pressure against OpenRouter. The case-file default
still remains three candidates per full-pipeline iteration.

## First Successful Full Run

A funded OpenRouter key was added and the paid `google/gemma-4-31b-it` route
started returning accepted SQL. A two-case smoke run confirmed that API calls
were working.

The first full deterministic 16-case run then exposed one robustness issue:
the model returned pathological JSON with an oversized numeric literal. Python
rejected it while parsing JSON, which crashed the benchmark instead of marking
that response as unusable. The OpenRouter client was patched so malformed or
pathological JSON is recorded as a response-validation failure rather than
aborting the run.

After that fix, the first full deterministic run completed:

- JSON artifact:
  `benchmark/results/mimic_ab_20260707T105849Z.json`
- HTML report:
  `docs/evaluation_report.html`
- Case-details page:
  `docs/evaluation_report_cases.html`

Summary of that first run:

- Baseline correctness: 6.25%.
- Full-pipeline correctness: 12.5%.
- Full pipeline better: 1 case.
- Tie: 15 cases.
- Baseline better: 0 cases.
- Ambiguous-case clarification rate: 1.0.
- 12 of 16 cases were marked unreliable because the simulated user had to
  answer unexpected or repeated clarifications.

This run is useful as an operational first run, but it is not the final result.
The final report should aggregate 10 successful full runs.

## Current Step: Aggregation Layer

The current implementation step is to add a standalone aggregation layer before
running nine more evaluations. This prevents spending API budget before we can
combine the results correctly.

Completed in this step:

- `benchmark/aggregate_mimic_reports.py`
  - Reads multiple `mimic_ab_*.json` reports.
  - Validates that reports use the same suite and case order.
  - Aggregates baseline and full-pipeline scores.
  - Aggregates win/tie/loss counts.
  - Aggregates clarification and spurious-clarification rates.
  - Tracks per-case score averages, score deltas, clarification frequency, and
    unreliable-run frequency.
  - Writes one aggregate JSON artifact.
- `tests/benchmark/test_aggregate_mimic_reports.py`
  - Covers summary math, compatibility validation, per-case reliability counts,
    and file writing.

Validation completed:

- Focused tests passed:
  `tests.benchmark.test_aggregate_mimic_reports`,
  `tests.benchmark.test_mimic_ab_run`, and
  `tests.querier.test_openrouter_client`.
- The aggregator was smoke-tested against the first real full-run report:
  `benchmark/results/mimic_ab_20260707T105849Z.json`.
- The smoke aggregate was written to:
  `benchmark/results/mimic_ab_aggregate_smoke.json`.
- The aggregate preserved the first-run headline metrics: baseline 6.25%, full
  pipeline 12.5%, 1 full-pipeline win, 15 ties, and 12 unreliable cases.

The HTML renderer has now been updated so it can present aggregate reports
cleanly. `benchmark/render_evaluation_report.py` detects
`report_type: mimic_ab_aggregate` and renders:

- aggregate run counts and total case-result counts,
- aggregate score percentages and score stability,
- aggregate win/tie/loss totals,
- aggregate clarification and spurious-clarification rates,
- per-case average baseline/full scores,
- per-case clarification and unreliable-run rates,
- source report metadata for the runs included in the aggregate.

The existing single-run report rendering path remains supported.

Validation completed:

- Focused renderer and aggregator tests passed.
- The one-run smoke aggregate
  `benchmark/results/mimic_ab_aggregate_smoke.json` was rendered to:
  `docs/evaluation_report.html` and `docs/evaluation_report_cases.html`.
- The rendered pages now show aggregate-specific sections such as "Aggregate
  Evaluation Summary", "Score Stability", "Per-Case Aggregate Results", and
  "Source Runs".

The next operational step is to collect the remaining full-run reports until
there are 10 successful deterministic runs, aggregate those 10 reports, and
render the final aggregate HTML report.

## Planned Remaining Sequence

1. Finish and verify the aggregation script.
2. Update the HTML renderer so it can render aggregate reports, not only
   single-run reports. Status: complete.
3. Use the existing completed run plus additional full runs to collect 10
   successful deterministic reports.
4. Aggregate the 10 reports into one JSON artifact.
5. Render the final aggregate HTML report.
6. Decide whether to add optional qualitative judge notes afterward. The
   initial aggregate should use `--skip-judge` to keep cost and variance lower.

## Commands

Smoke run:

```powershell
C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe benchmark\mimic_ab_run.py --limit 2 --skip-judge --model google/gemma-4-31b-it --max-parallel-candidates 1
```

Full deterministic run:

```powershell
C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe benchmark\mimic_ab_run.py --skip-judge --model google/gemma-4-31b-it --max-parallel-candidates 1
```

Aggregate reports:

```powershell
C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe benchmark\aggregate_mimic_reports.py benchmark\results\mimic_ab_*.json --output benchmark\results\mimic_ab_aggregate_10run.json
```

Render report:

```powershell
C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe benchmark\render_evaluation_report.py benchmark\results\mimic_ab_aggregate_10run.json
```

Run tests:

```powershell
C:\Users\talir\AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest discover
```
