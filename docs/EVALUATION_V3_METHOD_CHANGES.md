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

These are approved design changes pending implementation. The complete design,
tests, replay requirements, and reporting implications are documented in
`docs/superpowers/specs/2026-07-25-semantic-intent-and-evaluation-scoring-design.md`.
