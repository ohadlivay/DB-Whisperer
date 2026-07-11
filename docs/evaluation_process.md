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

## Current Aggregate Progress

The deterministic-only repeated-run phase has started. The LLM judge remains
disabled with `--skip-judge`, so these results are based only on exact
execution-result comparison against the reference SQL.

Completed full deterministic runs:

1. `benchmark/results/mimic_ab_20260707T105849Z.json`
2. `benchmark/results/mimic_ab_20260708T170911Z.json`
3. `benchmark/results/mimic_ab_20260708T174012Z.json`

Current aggregate artifact:

- `benchmark/results/mimic_ab_aggregate_current.json`

Current aggregate HTML pages:

- `docs/evaluation_report.html`
- `docs/evaluation_report_cases.html`

Current aggregate summary after 3 of 10 planned runs:

- Total case results: 48.
- Baseline correctness: 12.5%.
- Full-pipeline correctness: 8.33%.
- Full pipeline better: 1 case result.
- Tie: 44 case results.
- Baseline better: 3 case results.
- Ambiguous expected clarification rate: 1.0.
- Control-case spurious clarification rate: 0.5833.
- Cases with at least one unreliable run: 12.

The report now shows two aggregate calculations:

- Primary end-to-end aggregate: includes all case results, including cases
  where DB Whisperer asked unexpected, repeated, or unmatched clarifications.
  This measures the system as it actually behaved.
- Reliable-only aggregate: excludes case results where the simulated
  clarification could not faithfully follow the predefined intent. This answers
  the narrower question of how the systems compare when the clarification loop
  follows the evaluation contract.

Current reliable-only aggregate after 3 runs:

- Included reliable case results: 15.
- Excluded unreliable case results: 33.
- Baseline reliable-only correctness: 26.67%.
- Full-pipeline reliable-only correctness: 26.67%.
- Reliable-only full pipeline better: 1 case result.
- Reliable-only tie: 13 case results.
- Reliable-only baseline better: 1 case result.

Remaining before the final report: 7 additional successful full deterministic
runs.

Comparison checkpoint to revisit after 10 runs: at 3 runs, the baseline is
ahead on deterministic correctness, while DB Whisperer is consistently asking
clarifications for ambiguous cases but is also over-asking on control cases.
The final 10-run report should explicitly compare whether these interim trends
hold, improve, or reverse.

Conclusion checkpoint to revisit after 10 runs: if the unreliable-case rate
remains high, this should be treated as a central evaluation finding, not a
minor limitation. The conclusion should state that the current ambiguity layer
often leaves the expected clarification path, which limits the system's ability
to demonstrate reliable improvement over the baseline.

Requested evaluation factors:

- Correctness: did the system return the same data as the reference answer?
- Ambiguity Detection: did DB Whisperer notice when a question could have
  multiple valid meanings?
- Clarification Quality: was the clarification question specific enough for a
  user to choose the intended meaning?
- Unnecessary Interruptions: did the system avoid asking follow-up questions
  when the original question was already clear?
- Safety: did the system avoid destructive database actions such as deleting or
  changing records?
- Trust and Faithfulness: did the answer stay grounded in the returned data,
  without adding unsupported claims?

Deterministic factor scoring status:

- Correctness is scored deterministically for both baseline and full pipeline
  using exact result comparison against the reference SQL result.
- Ambiguity Detection is scored deterministically for the full pipeline as the
  percentage of expected-ambiguous cases where DB Whisperer asked a
  clarification.
- Clarification Quality is scored with a partial deterministic proxy: the first
  clarification must match the simulated user answer and the run must not be
  marked unreliable. Human or independent-LLM review may still be useful for
  wording quality.
- Unnecessary Interruption avoidance is scored deterministically as the
  percentage of control cases where DB Whisperer did not ask a clarification.
- Safety is scored deterministically on cases explicitly tagged as SQL
  safety/destructive-operation tests, by checking whether the arm avoided
  accepted SQL.
- Trust and Faithfulness are not fully deterministic and should be treated as
  qualitative-only unless a human or independent LLM judge is added later.

Current deterministic factor scores after 3 runs:

- Correctness: baseline 12.5%, full pipeline 8.33%.
- Ambiguity Detection: full pipeline 100.0%.
- Clarification Quality deterministic proxy: full pipeline 20.83%.
- Unnecessary Interruption avoidance: full pipeline 41.67%.
- Safety: baseline 0.0%, full pipeline 0.0%.
- Trust and Faithfulness: qualitative-only, not scored in deterministic runs.

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
