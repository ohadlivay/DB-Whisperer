# Evaluation V3 Method Changes

## Why V2 no longer matches DB Whisperer

V2 described an earlier experiment and no longer reflects the deployed ambiguity workflow. V3 evaluates executed SQL alternatives and validated semantic-column evidence. No join path determines clarification, and alternate relationship routes are not a reason to interrupt the user.

## Experimental arms

The four fixed arms are Baseline, Candidate Only, Semantic Only, and Full System. The baseline is the single-pass LLM-to-SQL comparison. Candidate Only judges executed candidate SQL and results. Semantic Only uses validated semantic-column findings. Full System combines both signals using the production priority and plausibility gates.

## Test-suite redesign

The frozen V3 suite contains 22 query cases and 2 ETL fixtures (24 cases total) across direct answers, semantic ambiguity, candidate-derived ambiguity, clarification resolution, safety, and relational requests. Every arm sees the same query cases and database state; ETL fixtures are shared operational evidence.

## Ambiguity-funnel scoring

The funnel reports whether ambiguity was present, whether an interruption was justified, question quality, and resolution after the answer. Candidate support is confidence rather than a majority-vote rule. A single candidate outlier or an arbitrary union cannot pre-empt validated semantic evidence.

## Correctness and least-sufficient joins

Correctness is scored from the expected result and executable SQL evidence. Multi-table queries are assessed for least-sufficient joins: only the relationships needed to answer the request should be introduced. Direct relationship metadata can help SQL generation, but alternate graph routes never independently create ambiguity.

Required SQL filters are compared as parsed expressions after removing table
qualification and identifier-quoting differences. Thus `subject_id`,
`"subject_id"`, and `i."subject_id"` are equivalent evidence for a declared
filter. DuckDB's equivalent `YEAR(column)` and
`EXTRACT(YEAR FROM column)` forms are normalized as well. Publication rejects
any stored “required filter is missing” reason that contradicts the parsed
generated SQL.

## K=3, five repetitions, and budget control

Baseline is a single-pass K=1 comparison. Candidate Only, Semantic Only, and Full System each produce K=3 SQL candidates; the complete compatible campaign is repeated five times. The planned campaign ceiling is $3.75; usage and retries are retained as campaign-global operational evidence when they cannot be attributed safely to an individual repetition.

## Faster campaign execution

Campaigns use two workers while generating the three candidates for a request in parallel. The database is prepared once per campaign and read-only queries run against the shared prepared state. This improves throughput without changing case order, scoring rules, or model provenance.

## Progress, checkpoints, and resume

The runner records campaign-wide progress and writes a checkpoint only after a
valid system observation, with immutable suite, model, runtime, and scoring
fingerprints. A wrong result, rejected SQL, or unresolved clarification under
healthy evaluation conditions is a valid DB Whisperer failure and scores zero.
Provider error envelopes, transport, credential, dataset-preparation, and
harness failures are evaluation infrastructure failures: they are retained in
status and event evidence, stop new work, and never enter correctness scoring.
Their affected cells remain uncheckpointed so a compatible resumed campaign
still obtains five valid observations per test. Resume is permitted only when
fingerprints match; incomplete or incompatible runs are excluded from
aggregation.

Accordingly, `450/450` means that all 440 query cells and 10 shared ETL cells
have valid observations. It is distinct from the final publication state,
which the terminal reports explicitly along with any blocking reason.
If all observations are complete but report publication fails, the campaign
retains its complete processing state and reports that publication alone must
be retried.

Clarification-question metadata is recorded when the question is asked, while
compliance is recorded only after the selected answer is evaluated in the next
workflow iteration. An accepted matched clarification cannot publish with
unknown compliance evidence.

Before another official paid campaign, the validator is replayed against the
previous production records without model calls, followed by a one-repetition
live canary. The five-repetition run is admitted only after both gates pass.

## Aggregation and publication

Aggregation uses only complete compatible repetitions containing valid system
observations. Arm metrics and deltas report uncertainty with 2,000 paired,
stratified percentile-bootstrap replicates: repetitions and question families
are resampled, ambiguity/control, correctness, and safety strata retain their
designed sizes, and the complete campaign statistic is recomputed for every
replicate. The same resample is used across arms so arm deltas remain paired.
Shared ETL instead uses a 2,000-replicate repetition-only percentile bootstrap.
This avoids mixing a complete-run point estimate with independently averaged
family summaries. Case-level SQL, results, scores, transcripts, candidate
support, compliance, system failures, and provenance remain available.
Infrastructure failures remain separate operational evidence. Publication
produces exactly a one-page method summary and an eight-tab evidence report.

## Interpretation limits

The results estimate performance on the frozen suite, model, and database snapshot; they are not a general claim about every dataset or model. Year wording is explicitly disambiguated when it could mean a patient birth date versus an admission date. No conclusion relies on relationship-route multiplicity.

## Approved post-campaign scoring follow-up

The official campaign remains immutable. Its strict correctness score measures
the scorer that was frozen for that run. Post-campaign review found that exact
projection width, aliases, reference-only ordering, and duration
representation were incorrectly acting as semantic correctness gates.

The approved follow-up makes user-intent result compatibility the primary
correctness decision. Harmless extra columns, unambiguous aliases, and
non-material reference tie-breakers do not erase correctness. Projection
precision and presentation fidelity are reported separately. Extra columns
still fail correctness when they change grain, cardinality, grouping,
filtering, meaning, or create a material safety or privacy concern.

Duration contracts accept integer units, fractional units, and
interval/timedelta representations. Whole-day calculations are acceptable
when the request does not require sub-day precision. Raw endpoint timestamps
alone remain context rather than a duration answer.

Ranking requests can be satisfied by correct ordered output without a separate
rank-number column unless rank values were explicitly requested. Top-N
comparison is tie-aware, while reference-only deterministic tie-breakers are
reported as reproducibility diagnostics.

Clarification scoring separates plausibility from target-option coverage.
Reports replace the generic `query was not accepted` label with terminal
outcomes that distinguish unresolved or unnecessary clarification, missing
target options, malformed generation, candidate quorum, validation, execution,
post-clarification generation, and compliance failures. The best executed
pre-clarification result is retained as a diagnostic even when the end-to-end
workflow returns no final answer.

The admission-year control is revised from `Show patients admitted in the year
2112.` to `Show patients admitted to the hospital in the year 2112.` so the
control explicitly resolves hospital versus ICU admission.

These scoring and evidence changes are implemented. The complete design,
tests, replay requirements, and reporting implications are documented in
`docs/superpowers/specs/2026-07-25-semantic-intent-and-evaluation-scoring-design.md`.

### Frozen result versus counterfactual rescore

The originally published campaign score remains frozen and is never rewritten.
`python -m benchmark_v3.rescore_campaign <campaign-dir>` creates a separate
`counterfactual-rescore.json` from the saved SQL, results, clarification
transcripts, and frozen reference artifact. It records both the source
campaign hash and current scorer hash.

The counterfactual artifact answers how the saved outputs score under the
revised deterministic rules. It does not claim that the updated semantic
detector would have asked the same questions or produced the same SQL. A new
live four-arm campaign is required to measure those production changes.

### Review before HTML publication

Campaign execution and HTML publication are now separate approved phases. A
complete campaign first produces validated aggregate evidence plus a
Markdown/JSON review package. The package contains the metrics, uncertainty,
arm deltas, case evidence, clarification findings, terminal outcomes,
projection diagnostics, provenance, cost, limitations, and proposed findings
needed for review without generating final HTML.

Before a paid campaign, a deterministic report-readiness gate verifies that
the evidence model can populate the information types, explanations, findings,
caveats, and drill-down required by both reference reports. Renderer tests use
fixtures or temporary output and do not overwrite the documents under `docs/`.

The paid campaign continues to run in an external CMD window with overall
progress, elapsed time, completion percentage, and whole-campaign ETA. It
stops after validation, aggregation, and review-package creation. HTML
publication makes no model calls and requires explicit user approval tied to
the campaign identity and aggregate hash.

Only after approval are the two final reports generated. The concise report
uses `docs/evaluation_method_one_page.html` as its information-hierarchy and
explanation baseline. The detailed report uses
`docs/evaluation_report.html` as its navigation, evidence-depth, methodology,
finding, and case-drill-down baseline. Visual similarity alone is
insufficient; the generated reports must contain the same kinds of analytical
content populated from the approved new campaign.

### Staged campaign validation

`python -m benchmark_v3.preflight` now performs deterministic suite,
reference, scorer, report-contract, temporary-renderer, fingerprint,
historical-rescore, and public-HTML immutability checks without model calls.
The live workflow then uses a non-publishable targeted one-repetition matrix
before the official five-repetition campaign. Exact commands and behavioral
pass gates are recorded in `benchmark_v3/LIVE_VALIDATION_RUNBOOK.md`.

The official external launcher retains four-arm coverage, K=3 for candidate
arms, five repetitions, two workers, and the $3.75 ceiling. Its terminal view
is campaign-wide: completed tests out of 450, percentage, elapsed time, and
whole-campaign ETA. Successful execution stops with review evidence; it does
not render either HTML report.

### Final validation hardening

The pre-publication review found and corrected additional edge cases:

- official finalization, approval, and publication require the frozen default
  suite hash and an explicitly publishable campaign;
- approval hashes the aggregate and both regenerated review-package files, so
  stale or edited review evidence cannot authorize publication;
- offline preflight works in a clean checkout and runs historical replay only
  when `--historical-campaign` is supplied;
- all-pass campaigns can produce a review package without manufacturing a
  failure example;
- targeted campaign IDs use the official safe-slug validation;
- semantic findings are discarded unless the reported vague phrase occurs in
  the actual user query; and
- each model request reserves the maximum cost of the frozen model's complete
  262K-token context plus enforced provider-price caps until usage is recorded,
  covering provider framing and preventing concurrent requests from sharing
  the same $3.75 allowance.

### Targeted semantic-regression audit

The first post-change targeted run completed without infrastructure failures,
but its behavioral gate failed: 10 of 26 primary cells passed and none of the
four admission-control cells passed. The run was correctly non-publishable and
did not authorize an official campaign or HTML generation.

Raw prompt evidence showed that the semantic model usually found the intended
distinctions, but gave both equally plausible interpretations the same
relevance rank. The deterministic validator rejected tied ranks even though it
later replaced raw ranks with stable local interpretation IDs. Equal ranks are
now accepted in returned order; duplicate semantic grounding still fails
closed.

The audit also found that the runner created the semantic detector with a
separate, uninstrumented OpenRouter client. Semantic calls therefore bypassed
campaign call counts, prompt evidence, budget admission, and recorded cost.
The targeted artifacts reported $0.07429228 across both stages, while the
central prompt log contained another $0.01000654 of semantic calls. All
query-generation, ambiguity-judge, and semantic-detector calls now share the
campaign's instrumented session and prompt logger.

Three deterministic scoring false negatives were corrected:

- `date_part('year', column)` is canonicalized with `YEAR(column)` and
  `EXTRACT(YEAR FROM column)`;
- duration columns are mapped by declared value compatibility across integer,
  decimal, and interval/timedelta representations instead of requiring the
  reference alias; and
- admission-year cases allow an optional `patients` join. Their shortest
  reference remains admission-table-only, so an unnecessary join affects
  efficiency rather than semantic correctness.

The admission-year references return `subject_id` and `admittime`; `hadm_id`
is no longer treated as a user-required output when the request asks for
patients. The ambiguous diagnosis wording is now `How common is each diagnosis
code?` in both target branches. This preserves the intended occurrence-count
versus distinct-patient ambiguity while removing long-title versus code as an
unrequested presentation variable from that regression family.

Mechanism accuracy is conditioned on the evaluated arm. Candidate Only can
correctly use candidate comparison, Semantic Only can correctly use semantic
column evidence, and Full can correctly use either candidate evidence or the
validated semantic fallback. Without this conditioning, a correct ablation
could be penalized for not using evidence that the arm intentionally removes.

Targeted-run completion no longer implies behavioral success. The targeted
artifact records `behavioral_passed` and the exact failing cells. Its process
exits nonzero when any selected cell has `score.passed != true`, even when
every cell completed, so the external launcher cannot green-light the official
campaign on execution progress alone.

### Malformed provider-response recovery

The first full campaign after the targeted audit stopped after 98 of 450 cells
when OpenRouter returned HTTP 200 with a body that was not valid JSON. The
provider call lasted 118 seconds; the transport initially counted it as a
completed call, and the downstream SQL client raised a JSON decode error. This
was an automation transport failure, not a DB Whisperer result.

The instrumented transport now validates successful response bodies before
returning them to SQL or ambiguity clients. A malformed HTTP-200 response may
still have been billed, but its usage is unreadable, so the campaign charges
the pre-admitted maximum request cost instead of silently treating it as free.
It then uses the same bounded transient retry policy as connection failures
and retryable HTTP statuses. Only retry exhaustion blocks the campaign, and
durable logs contain a generic, secret-safe provider-response error rather
than the response body.

This transport change is fingerprinted. The 98 completed checkpoints remain
diagnostic evidence, but they are not mixed with post-fix cells in an official
aggregate; the reliable campaign starts under a new campaign ID and runtime
fingerprint.

### 2026-07-26 offline correction and report publication

The completed 450-cell campaign
`v3-official-provider-retry-20260725` was not rerun. Its saved SQL, result
tables, clarification transcripts, schema, and reference results were
deterministically rescored. No OpenRouter or other model calls occur during
this operation. Original campaign files and `aggregate.json` remain unchanged;
the correction writes separate `counterfactual-rescore.json`,
`corrected-aggregate.json`, and `rescore-change-ledger.json` artifacts.

The correction implements the decisions reviewed in this session:

- correctness evaluates the concepts and values required by user intent;
  optional context columns and harmless aliases are diagnostics, not failure
  gates;
- integer, decimal, and interval/timedelta duration outputs are accepted when
  the case contract allows them and sub-unit precision is not required;
- calendar-boundary whole-day durations may differ from elapsed decimal days
  by less than one declared day;
- duration columns are mapped by semantic duration aliases before value-only
  matching, preventing patient identifiers from accidentally matching small
  duration values;
- row ordering in the hospital-stay, ICU-stay, their explicit controls, and
  completed-admission-duration cases is treated as reference reproducibility
  rather than user intent;
- ranked outputs must use the requested primary measure and direction, but an
  omitted identity tie-break and a different order among equal-count patients
  do not fail; and
- join count remains an efficiency measure. A semantically correct result does
  not fail correctness merely because it used an unnecessary valid join.

The frozen `lab_frequency_with_labels` question leaves the meaning and grain
of “frequency” unresolved, but the case was classified as non-ambiguous and
has no simulated clarification answer. It is therefore excluded from corrected
headline denominators rather than retroactively assigned an answer. The report
also presents an all-cases sensitivity analysis so readers can see the effect
of including it. All 20 saved cells remain in the evidence and are marked with
the exclusion reason.

The corrected headline results are:

| Arm | Composite | Pass rate | Correctness |
| --- | ---: | ---: | ---: |
| Baseline | 44.53 | 47.62% | 67.06% |
| Candidate Only | 45.62 | 47.62% | 70.59% |
| Semantic Only | 67.43 | 60.95% | 81.18% |
| Full System | 76.54 | 69.52% | 83.53% |

Full System exceeds Baseline by 32.01 composite points; the paired 95%
bootstrap interval is +15.79 to +44.82. The ledger contains 120 score-record
changes and 53 overall pass-status flips. These counts describe deterministic
scoring changes, not newly generated system outputs.

The duration correction passes all 18 completed-admission cells that returned
accepted SQL. The two Semantic Only cells that returned no accepted query
remain failures. Ranking passes 19 previously mis-scored accepted outputs; the
remaining Candidate Only ranking failure is a genuine schema-grounding
failure. Likewise, offline scoring does not repair missing final queries,
wrong tables, wrong ambiguity decisions, or incompatible values.

The two approved HTML outputs are regenerated from
`corrected-aggregate.json`:

- `docs/evaluation_method_one_page.html` is the concise method, headline,
  correction, sensitivity, findings, and limitations view.
- `docs/evaluation_report.html` is the detailed four-arm evidence report with
  family results, failure reasons, funnel stages, operations, provenance, and
  representative SQL/clarification drill-down.

The detailed report samples result rows and representative cases rather than
embedding every raw result twice. Complete evidence remains in the campaign
artifacts and corrected review package. This reduces the report from roughly
40 MB to a reader-usable artifact without changing the scored evidence.

Historical preflight replay now copies `campaign.json`, `status.json`,
`aggregate.json`, all five run reports, and the matching reference artifact
into its isolated workspace. This keeps the independent offline replay aligned
with the corrected aggregator's complete evidence contract.
