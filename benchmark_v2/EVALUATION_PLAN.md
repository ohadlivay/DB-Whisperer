# DBWhisperer Evaluation V2 Implementation Plan

## Summary

Create a fully isolated evaluation system under `benchmark_v2/`. The existing
`benchmark/` implementation, cases, results, and historical reports must remain
unchanged.

Evaluation V2 runs a frozen 18-case stratified suite through five experimental
arms for five repetitions. All official scoring is deterministic: no human evaluator
or separate LLM judge is used during case generation, execution, scoring, or
aggregation. DBWhisperer's own LLM calls remain enabled because they are part
of the framework being evaluated, so model outputs may still vary between
runs.

After the five runs, V2 must aggregate the results and regenerate the public
HTML report with the new scoring model and five-arm layout.

### Suite revision before publication

The initial 2.0.0 campaign is retained as pilot evidence. It exposed reference
contracts that required optional or redundant joins and an output check that
treated equivalent derived aliases as different. These issues are corrected in
suite 2.1.0 and documented in `SCORING_REVISION_2_1.md`. Only a complete
five-run campaign produced with the 2.1.0 suite and scorer may be published as
the final aggregate. Reports must display both the suite version and content
hash so pilot and publication results cannot be confused.

The campaign must expose live progress through both the terminal and a local,
read-only Streamlit dashboard. The dashboard shows safe metadata by default;
full prompts, responses, generated SQL, and sampled values require an explicit
session-only sensitive-log toggle.

## Isolation and Artifacts

Use the following V2-owned structure:

```text
benchmark_v2/
  EVALUATION_PLAN.md
  README.md
  cases/
  fixtures/
  results/
    runs/
    aggregate/
    live/
  case_generator.py
  validate_suite.py
  run_evaluation.py
  aggregate_results.py
  render_report.py
  monitor.py
```

- Store all V2 cases, ETL fixtures, temporary databases, prompt logs,
  individual reports, checkpoints, and aggregate JSON under `benchmark_v2/`.
- Add V2 tests under `tests/benchmark_v2/`.
- Do not import evaluation logic from `benchmark/` or write new output into
  `benchmark/results/`.
- Reuse production components from `src/db_whisperer`, since those are the
  systems under evaluation.
- Preserve all historical benchmark reports during development and execution.
- At final publication, regenerate `docs/evaluation_report.html` and its
  case-details page from the V2 aggregate artifact.

## Experimental Arms and Run Matrix

Run identical cases and controlled settings through:

1. **Single-pass baseline:** one `QueryService` candidate, including its
   existing syntax-repair retry, without ambiguity handling.
2. **Candidate-only ablation:** candidate comparison with both pre-generation
   detectors disabled.
3. **Join-path-only ablation:** join-path detection enabled, semantic-column
   detection disabled, and candidate comparison retained.
4. **Semantic-column-only ablation:** semantic-column detection enabled,
   join-path detection disabled, and candidate comparison retained.
5. **Full DBWhisperer:** both pre-generation detectors and candidate
   comparison enabled.

Use five repetitions. The baseline uses one candidate; every multi-candidate
arm uses `K=2`. Use `google/gemma-4-31b-it` as the SQL-generation and internal
DBWhisperer ambiguity model. Hold the database, schema context,
temperature policy, validator, result limits, and application iteration limits
constant across applicable arms.

Record model calls, token usage, cost, latency, retries, and failures as
operational metrics. They do not contribute to join-efficiency scoring.

## Live Progress and Logging

Maintain a campaign-scoped `status.json`, append-only `events.jsonl`, full
`prompts.jsonl`, mirrored `console.log`, and per-case checkpoints. Update the
status snapshot atomically after every phase, run, case, arm, iteration,
candidate batch, score, failure, and budget decision.

The terminal shows overall completion, current run/case/arm, result counts,
elapsed time, estimated remaining time, model calls, retries, cost, remaining
budget, and the latest error.

The Streamlit monitor binds only to `127.0.0.1`, refreshes every two seconds,
and remains read-only. It provides an overview, five-arm matrix, case results,
cost and latency views, filterable events/errors, and request-correlated model
interactions. Raw sensitive records are not loaded into the browser until the
user enables the explicit warning toggle. API keys and authorization headers
must never be logged.

## Frozen Suite

Create and freeze 18 official cases:

- Two join-path families with two identical-wording intention variants each:
  4 cases.
- Two semantic-column families with two identical-wording intention variants
  each: 4 cases.
- One explicitly disambiguated control for each ambiguity family: 4 cases.
- Two clear correctness and efficiency cases.
- Two read-only safety cases.
- Two shared ETL fixtures.

Every ambiguity family must contain exactly two canonical interpretations. V2
does not evaluate three-or-more-interpretation clarification trees because the
current production detectors expose two options and do not retain a multi-path
decision tree.

Every case declares:

- Stable case and family identifiers.
- The question, category, and applicable scoring dimensions.
- Whether clarification is required and the expected mechanism.
- Two schema-supported interpretations for ambiguous families.
- The simulated user's intended interpretation.
- Deterministic option-matching tokens or path/column signatures.
- Required and optional output fields.
- Required filters, aggregation, ordering, limits, and result grain.
- Permitted source tables and join variants.
- Minimum join count for every accepted output variant.
- Executable reference SQL and expected-result evidence.

Reference SQL is an exemplar, not the only accepted SQL text.

## Deterministic Suite Validation

Generate the suite once, validate it, store its content hash, and reuse it
unchanged for every arm and repetition. No judge models are called.

Admit an ambiguity family only when deterministic checks confirm:

- Both interpretations reference discovered schema elements.
- Both reference queries parse, validate as read-only, and execute.
- The interpretations differ in join route, schema scope, semantic column,
  result grain, or returned result.
- Equivalent-result alternatives have been merged.
- The paired variants have identical initial wording and differ only in their
  simulated intention and reference constraints.
- The matched control contains a declared disambiguating phrase and maps to
  exactly one interpretation signature.
- The simulated answer can be mapped deterministically to one returned option
  using its declared path, column, and identifier tokens.

The harness records clarification wording but does not judge its style,
helpfulness, fluency, or general linguistic plausibility. These qualities are
reported as `not_evaluated`.

## SQL Analysis

Add a dedicated SQL parser dependency for logical SQL analysis. Do not use
regular expressions or optimizer-dependent physical plans to score joins and
identifiers.

The analyzer must:

- Parse one validated DuckDB `SELECT` statement.
- Extract tables, aliases, qualified and unqualified columns, CTEs, subqueries,
  explicit joins, and implicit comma joins.
- Resolve identifiers against `SchemaMetadata` and the DuckDB binder.
- Count logical relationship edges, including joins inside CTEs and
  subqueries.
- Preserve evidence explaining every grounding and efficiency score.

## Scoring

Calculate one transparent 0-100 score for every arm:

| Component | Weight |
| --- | ---: |
| Ambiguity detection and resolution | 40% |
| Answer correctness | 25% |
| Join efficiency | 15% |
| ETL/schema correctness | 10% |
| Read-only safety | 5% |
| Schema grounding | 5% |

Normalize each component over only its applicable tagged cases before applying
its weight. Macro-average paired intention variants by ambiguity family.

### Ambiguity: 40 points

- Ambiguous-case detection recall: 10 points.
- Unambiguous-control specificity: 8 points.
- Correct detector mechanism: 4 points.
- Deterministic option-to-oracle match and simulated resolution: 8 points.
- Final SQL alignment with the selected interpretation: 10 points.

Do not assign points for subjective clarification-language quality. Record it
as `not_evaluated` and exclude it from the denominator rather than assigning a
zero.

### Correctness: 25 points

- Valid DuckDB syntax and successful read-only execution: 5 points.
- Satisfaction of declared semantic result constraints: 20 points.

Permit aliases, optional columns, projection differences, harmless join order
differences, and equivalent query structures. Compare unordered results as
multisets unless ordering or top-N behavior is part of the requested intent.

### Join efficiency: 15 points

- Score only syntactically valid, executable, semantically correct SQL.
- Incorrect or failed SQL receives zero efficiency credit.
- Award full credit for the minimum declared logical join count.
- Award proportional credit for redundant joins using
  `minimum_join_count / actual_join_count`.
- Give full credit to a correct zero-join query whose minimum is zero.
- Treat joins that change the required cardinality as correctness failures,
  not merely efficiency deductions.

### ETL/schema correctness: 10 points

Compare tables, columns and types, row counts, keys, relationships, false
relationships, and incomplete-discovery reporting against fixture manifests.
Run ETL fixtures once per repetition and reuse the shared score across arms.
Keep ETL in each arm's composite, label it as shared, and exclude it from claims
about arm-to-arm improvement.

### Read-only safety: 5 points

Require destructive SQL, DDL, multi-statements, and external file/network
access requests to produce no accepted executable SQL. Verify database schema
and contents remain unchanged after every safety case.

### Schema grounding: 5 points

Require every generated table and column to exist in discovered metadata and
require use of the case's declared schema concepts. Fabricated identifiers or
substitution of an unrelated schema concept receive no grounding credit.

## Execution Efficiency and Budget Control

- Ingest the primary dataset once per repetition and share immutable schema
  metadata across arms.
- Execute and cache reference queries once per suite and database hash.
- Run ETL fixtures once per repetition, not once per arm.
- Process cases sequentially while retaining existing within-case candidate
  parallelism.
- Checkpoint after every arm/case and support safe resume without repeating
  completed calls.
- Capture OpenRouter usage through benchmark-owned instrumented HTTP sessions
  without changing production clients.
- Support a configurable campaign spending ceiling and stop before launching
  new work when the remaining allowance is insufficient. A budget stop creates
  an explicitly incomplete report that cannot be included in the final
  five-run aggregate.
- Default the campaign ceiling to `$3.75`, leaving a `$0.25` reserve on the
  user's `$4.00` OpenRouter balance. Check the key's remaining allowance before
  each case and checkpoint before stopping.

## Aggregation

Aggregate only five complete reports with matching suite, dataset, scorer,
prompt, SQL-generation model, arm configuration, and runtime-configuration
hashes.

Report:

- Component and composite means, standard deviations, pass rates, and arm
  deltas.
- Fixed-seed bootstrap confidence intervals over question families.
- Ambiguity recall, specificity, false-positive and false-negative rates,
  mechanism accuracy, resolution success, and final-SQL alignment.
- Join-path and semantic-column results separately.
- Correctness-gated efficiency distributions.
- ETL, safety, and grounding outcomes.
- Model calls, tokens, cost, latency, retries, and failures.
- All failed and incomplete case outcomes without silent exclusion.

## V2 HTML Report

Redesign the report around the five-arm experiment:

- Headline composite score comparison.
- Component-score breakdown by arm.
- Correctness-gated join-efficiency section.
- Ambiguity funnel from detection through final-SQL alignment.
- Join-path versus semantic-column performance.
- Shared ETL health, safety, grounding, cost, latency, and failures.
- Per-case drill-down with SQL, joins, semantic constraints, clarification
  transcript, deterministic option match, scores, and failure reasons.
- Run provenance, hashes, model settings, and source aggregate artifact.
- A visible `deterministic_scoring_only` label and clarification-language
  quality marked `not_evaluated`.

The final workflow must aggregate five compatible completed runs before
generating the summary and case-detail HTML pages. HTML publication is a
required completion criterion.

## Tests and Completion Criteria

- Add unit tests for case contracts, generation quotas, suite freezing,
  interpretation signatures, option matching, SQL parsing, semantic result
  matching, efficiency gating, ETL manifests, safety checks, checkpoints,
  budget stops, aggregation compatibility, score formulas, and HTML escaping
  and rendering.
- Run `python -m unittest discover`.
- Complete five 18-case runs across all five arms.
- Produce a validated aggregate under `benchmark_v2/results/aggregate/`.
- Render and visually verify the redesigned HTML summary and case-details
  pages.
- Confirm `benchmark/` and its historical results were not modified.
- Confirm the public HTML report displays the V2 aggregate, all five arms, the
  deterministic component scores, and the redesigned layout.

## Assumptions

- Deterministic-only describes evaluation and scoring; model generation remains
  stochastic and is measured across five repetitions.
- There are no human evaluators or evaluation-judge LLMs in the initial V2
  campaign.
- Evaluation covers ambiguity grounded in the loaded schema, not every
  imaginable linguistic interpretation.
- Every ambiguous family contains exactly two interpretations.
- The initial V2 campaign uses `google/gemma-4-31b-it`, and every report records
  the resolved model ID.
- Join efficiency means minimum necessary logical joins, not execution latency.
