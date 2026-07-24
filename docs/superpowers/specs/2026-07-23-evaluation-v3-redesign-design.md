# Evaluation V3 Redesign

## Purpose

Update DB Whisperer's current evaluation so it measures the ambiguity funnel
implemented in commit `12c7d44`, while preserving the strongest deterministic
and auditable parts of Evaluation V2.

Evaluation V3 remains independent under `benchmark_v3/`. Evaluation V2 is
historical and must not be made runnable or changed to represent the new
system.

## Research Configuration

The official campaign uses:

- Four arms: `baseline`, `candidate_only`, `semantic_only`, and `full`.
- One SQL candidate for the baseline.
- Three SQL candidates (`K=3`) for every ambiguity-enabled arm.
- Five repetitions.
- The existing `$3.75` campaign ceiling.
- The configured V3 OpenRouter model for SQL generation and DB Whisperer's
  internal ambiguity decisions.
- Deterministic evaluation and scoring. Model generation remains stochastic.

The arms isolate the implemented evidence sources:

- `baseline`: one QueryService result without ambiguity handling.
- `candidate_only`: executed candidate differences without semantic findings,
  schema context, or relationship context in the ambiguity decision.
- `semantic_only`: semantic findings and schema context without candidate
  diversity during initial ambiguity selection. Executed alternatives may be
  used after clarification only to assess compliance.
- `full`: candidate evidence plus semantic, schema, type, and direct
  relationship support.

Join-path multiplicity is not an arm, case type, clarification mechanism, or
scoring category. Direct relationships remain supporting context only.

## Suite Design

The frozen official suite contains 24 cases: 22 query cases and two ETL
fixtures. All old `jp_*` cases and their matched controls are removed rather
than renamed.

### Ambiguity families

Use three two-interpretation families:

1. **Patients from 2024**
   - Patient birth year.
   - Hospital admission year.
   - Exercises same-type temporal evidence, candidate plausibility, semantic
     priority when a candidate is an unsupported outlier, and the failure that
     motivated the new funnel.
2. **Patient stay duration**
   - Hospital admission duration.
   - ICU stay duration.
   - Exercises temporal/numeric semantic mappings and clarification
     compliance.
3. **Most common diagnoses**
   - Number of diagnosis occurrences.
   - Number of distinct affected patients.
   - Exercises a natural candidate-derived aggregation and denominator
     distinction.

Each family has two identical-wording cases whose simulated intentions differ
and two explicit controls, one for each interpretation. This yields six
ambiguous cases and six unambiguous controls.

Every ambiguity case declares two deterministic interpretation signatures,
option-matching evidence, the simulated intention, the expected clarification
source, and the result contract for the selected interpretation.

### General text-to-SQL cases

Six cases cover:

- Scalar counting.
- Grouping and deterministic ordering.
- Dictionary joins and human-readable labels.
- Multi-table filters.
- Date arithmetic and null handling.
- `DISTINCT`, `HAVING`, ranking, and top-N behavior.

The individual cases may cover more than one item, but the suite validator
requires every listed capability to be tagged at least once.

### Safety and graceful-failure cases

Four cases cover:

- Destructive write SQL.
- DDL or multi-statement requests.
- External file or network scans.
- A nonexistent table or clinical concept.

Safety passes only when no unsafe executable SQL is accepted and the database
remains unchanged. A missing-schema case passes only through a grounded refusal
or a clear, non-crashing failure; fabricated identifiers presented as a valid
answer fail.

### ETL fixtures

Retain one single-table fixture and one relational fixture. Run these fixtures
independently in every repetition. Their scores are shared across arms and are
never attributed to an ambiguity treatment.

## Case Contracts and Suite Validation

Reference SQL is an executable exemplar, not an exact-text oracle. It must use
the least-sufficient logical join count for its interpretation.

Each query contract records:

- Stable case, family, category, and capability identifiers.
- Question, ambiguity expectation, expected mechanism, and simulated intent.
- Deterministic option-matching signatures.
- Required and forbidden schema concepts.
- Required filters, aggregation grain, null behavior, ordering, and limit
  semantics when applicable.
- Result comparison mode: scalar, unordered multiset, ordered rows, top-N, or
  declared compatible subset/superset.
- Executable reference SQL.

Before live calls, validation must:

- Execute every reference SQL through the read-only validator.
- Confirm its result contract against the frozen dataset.
- Parse and derive its logical join count.
- Verify identical wording and distinct intentions within each ambiguous pair.
- Verify both explicit controls for each family.
- Verify the suite capability matrix.
- Reject join-path arms, categories, mechanisms, request fields, or option
  contracts.
- Verify all fixture manifests.
- Freeze and report a content hash.

If a correct generated query uses fewer logical joins than the reference, it
receives full efficiency credit and emits an oracle-review flag. Publication
must disclose unresolved oracle-review flags.

## Scoring

Every arm receives a transparent 0–100 composite:

| Component | Weight |
| --- | ---: |
| Ambiguity funnel | 40 |
| Answer correctness | 30 |
| Join efficiency | 10 |
| Read-only safety | 10 |
| Schema grounding | 5 |
| Shared ETL/schema health | 5 |

Normalize components over only applicable cases. Macro-average ambiguity
metrics by family before applying weights so paired intentions and controls do
not inflate results.

### Ambiguity funnel

The 40-point component reports and weights:

- Ambiguous-case detection recall.
- Unambiguous-control specificity and false-positive rate.
- Correct evidence source (`candidate-comparison` or `semantic-column`).
- Deterministic option-to-intent match.
- Resolution within the two-question maximum.
- Final clarification compliance and final-result alignment.

An ambiguous case cannot pass solely because the chosen option matched. The
final workflow must prove compliance and return a result compatible with that
interpretation. Candidate support counts, candidate rejection reasons,
fallback state, compliance retries, and fail-closed outcomes remain audit
evidence.

Funnel behavior that a stochastic live campaign cannot force is covered by
deterministic unit and integration scenarios:

- Exact duplicate clustering and support counts.
- Unsupported singleton outlier rejection.
- Arbitrary `A` versus `A or B` union rejection.
- Candidate priority when both readings are natural and supported.
- Semantic fallback after an invalid judge response.
- Arm evidence isolation.
- At most two sequential questions.
- Compliance retry and fail-closed behavior.

### Correctness

Correctness requires accepted, read-only, executable DuckDB SQL plus semantic
result compatibility. It does not require exact SQL text, exact projection
order, or a particular harmless join order.

Comparison supports aliases, derived expressions, numeric normalization,
ordered and unordered outputs, deterministic top-N, required output concepts,
and declared projection differences. Joins that change cardinality, grain,
filtering, aggregation, or required output are correctness failures.

### Join efficiency

Join count is a separate correctness-gated efficiency factor, not part of
semantic correctness:

- Incorrect, unsafe, failed, or incompatible SQL receives zero efficiency.
- A generated query at or below the validated reference join count receives
  full credit.
- When the reference requires joins, extra joins score
  `expected_join_count / actual_join_count`.
- When the reference requires zero joins, extra joins score
  `1 / (actual_join_count + 1)`.
- A correct query below the reference count raises an oracle-review flag.

Count logical joins from parsed SQL, including CTEs and subqueries. Do not use
regular expressions or physical query plans.

### Safety, grounding, and ETL

Safety verifies both rejected SQL and database immutability. Grounding verifies
that identifiers exist and required schema concepts are used without forbidden
substitutions. ETL checks tables, columns, types, rows, relationships, and
incomplete-discovery reporting against fixture manifests.

## Execution and Performance

Retain reliability and sample size while reducing wall-clock time:

- Ingest and hash the primary MIMIC dataset once per campaign.
- Reuse its immutable DuckDB file and schema for all five repetitions.
- Execute and cache reference results once per suite/database hash.
- Never cache model-generated SQL, semantic analysis, ambiguity decisions, or
  compliance judgments.
- Run at most two independent arm/case cells concurrently by default.
- Preserve the existing within-cell parallel generation of three candidates.
- Give each worker independent application/query services and HTTP transport.
- Provide `--workers 1` for conservative serial execution.
- Retry only transient provider failures with bounded exponential backoff and
  jitter.
- Atomically checkpoint every valid system observation.

The normal maximum candidate-generation burst is six requests: two outer cells
times three candidates. Increasing the default beyond two workers is out of
scope because it would raise throttling and reliability risk.

Each repetition uses a recorded deterministic case shuffle and a
counterbalanced arm order. This prevents one arm from always running first or
last during a long provider session.

## Progress, Resume, and Failure Handling

The terminal continuously displays:

- Overall percentage and completed/total cells.
- Active run, case, arm, and phase.
- Elapsed time and rolling ETA.
- Passed and failed counts.
- Model calls, retries, and cost against `$3.75`.
- The latest error.

ETA uses rolling duration estimates by arm and case class rather than a single
campaign-wide average. A background terminal renderer keeps elapsed time
visible during long model calls. Plain non-interactive output remains available
for redirected logs and CI.

Resume requires matching suite, dataset, model, prompt/configuration, arm,
scorer, and runtime fingerprints. Genuine DB Whisperer failures under healthy
evaluation conditions are valid observations, are recorded, and score zero.
Provider error envelopes, credential, exhausted transport-retry,
dataset-preparation, and harness failures are infrastructure failures: they
stop new work, remain operational evidence, and leave affected cells
uncheckpointed so they cannot affect system scores. Dataset, suite,
fingerprint, or infrastructure failures stop processing. A completed campaign
retains its processing-complete state if aggregation or report publication
fails, allowing publication to be retried without rerunning cells.

A budget stop happens before launching a new paid operation, creates an
explicitly incomplete resumable campaign, and cannot publish or enter an
official aggregate.

## Aggregation and Reliability

Aggregate exactly five compatible completed repetitions containing one valid
system observation per scheduled cell. Reject infrastructure observations and mismatched
suite, dataset, scorer, prompt, model, candidate-count, arm, and runtime
fingerprints.

Report:

- Component and composite distributions.
- Pass rates and arm deltas.
- Fixed-seed bootstrap confidence intervals over question families.
- Recall, specificity, false-positive and false-negative rates.
- Clarification source accuracy, option match, resolution, compliance, and
  final alignment.
- Correctness-gated join-efficiency distributions.
- Safety, grounding, shared ETL, failures, and oracle-review flags.
- Model calls, tokens, cost, latency, retries, and provider failures.

Operational cost and latency are outcomes, not scoring inputs.

## Reports

After a complete compatible campaign, generate exactly two public HTML report
types from the aggregate artifact:

1. `docs/evaluation_method_one_page.html`
2. `docs/evaluation_report.html`

The one-page report preserves the existing visual design and section structure
while replacing obsolete V2, five-arm, and join-path content with V3 method and
results.

The full report preserves the current visual language and tabbed structure,
updated for four arms and the new ambiguity funnel. It embeds all case evidence
instead of generating a third case-details HTML report.

Both reports must:

- Populate values only from the validated aggregate.
- Escape model- and data-derived content.
- Display suite, dataset, model, prompt/configuration, and scorer provenance.
- Disclose stochastic generation and deterministic scoring.
- Show incomplete relationship-discovery warnings.
- Include failures rather than silently excluding them.
- Contain no obsolete join-path ambiguity claims.

Implementation also creates `docs/EVALUATION_V3_METHOD_CHANGES.md`, a durable
comparison with V2 covering the retired mechanism and arm, suite redesign,
`K=3`, scoring, compliance, join-efficiency treatment, performance, progress,
aggregation, and publication workflow. This Markdown file is maintained as
evaluation documentation and is not generated from campaign results.

## Verification

Implementation follows test-driven development. Required checks are:

1. Focused failing and passing unit tests for each changed behavior.
2. Offline suite validation.
3. A mocked end-to-end campaign.
4. `python -m unittest discover`.
5. A complete five-repetition live campaign.
6. Aggregate compatibility validation.
7. Browser-based visual and responsive inspection of both HTML reports.
8. A final comparison proving every published value came from the aggregate.

The live campaign may use a local environment file or process variable for the
OpenRouter key. API keys and authorization headers must never be logged or
persisted.

## Working-Tree Constraint

The workspace contains pre-existing uncommitted evaluation and report changes.
Implementation must preserve unrelated user-owned edits, inspect overlaps
carefully, and avoid modifying historical V2 behavior.
