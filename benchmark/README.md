# DB Whisperer Benchmark

This directory holds two standalone benchmarks. Both keep every generated
database, prompt log, and report inside this directory.

- **`run.py` — baseline accuracy.** Exercises ETL and the Querier directly:
  one prompt straight to SQL, no `ApplicationService` and no ambiguity module.
  Measures raw single-pass NL-to-SQL accuracy. Documented below.
- **`ab_run.py` — controlled A/B comparison.** Runs the same questions through
  the **full pipeline** (`ApplicationService` with Component B) and through a
  **baseline** (single-pass Querier), then compares them. This is the harness
  for the project's research question. See
  [A/B benchmark](#ab-benchmark-full-pipeline-vs-baseline).

## Run (baseline `run.py`)

From the repository root:

1. Add your OpenRouter key to `benchmark/.env`.
2. Run:

```powershell
python benchmark/run.py
```

Values already set in the shell take precedence over `benchmark/.env`.

The tested model generates SQL once. The reference SQL in `cases.json` is
validated and executed against the same benchmark-local DuckDB database. An
exact columns-and-rows match receives `4/4` without a judge call. A mismatch is
sent to the separately configured judge model.

Optional arguments:

```powershell
python benchmark/run.py `
  --cases benchmark/cases.json `
  --model google/gemma-4-31b-it `
  --judge-model provider/judge-model `
  --output-dir benchmark/results
```

## Scoring

- `4`: Fully equivalent answer.
- `3`: Correct with a minor precision or presentation issue.
- `2`: Partially correct with a material omission or error.
- `1`: Relevant but mostly incorrect.
- `0`: Incorrect, unusable, or a query-generation failure.

Judge failures are reported as unscored instead of being treated as system
failures. Each run prints a concise summary and writes a timestamped JSON
report under `benchmark/results/`.

Prompt logs and result files may contain dataset values and generated SQL.
Treat the entire `benchmark/results/` directory as sensitive. API keys are
used only in request headers and are never written to the report or logs.

## A/B benchmark (full pipeline vs baseline)

`ab_run.py` answers the project's reframed research question: does an explicit
schema-graph ambiguity-detection layer, placed before SQL generation, improve
interpretive accuracy versus a single-pass baseline? It runs each question
through two architectures against one shared DuckDB database:

- **baseline** — `QueryService` directly: one prompt, straight to SQL, no
  Component B.
- **full** — `ApplicationService` with the complete Component B: the join-path
  primary mechanism, the semantic-column secondary mechanism, and the
  candidate-comparison judge. When the pipeline asks a clarifying question, a
  **simulated user** answers it by choosing the interpretation the case
  declares, exactly as a real user clicking one of the two buttons would.

The `enable_join_path_detection` and `enable_semantic_column_detection` flags on
`ApplicationService` are the seams for ablating each mechanism; the harness
leaves both on so the full arm exercises all of Component B.

Both arms are scored against an author-written gold query (exact table match,
else the same 0-4 judge rubric as `run.py`).

### Run

```powershell
python benchmark/ab_run.py `
  --cases benchmark/ab_cases.json `
  --model google/gemma-4-31b-it `
  --judge-model provider/judge-model
```

`OPENROUTER_API_KEY` and a judge model are required, as for `run.py`. The
default suite (`ab_cases.json`) uses the bundled multi-CSV **BikeStores**
dataset, the only bundled dataset whose schema graph contains genuine
join-path multiplicity.

### Case schema

Each case in `ab_cases.json`:

| field | required | meaning |
| --- | --- | --- |
| `id` | yes | unique case identifier |
| `question` | yes | the natural-language question (neutral wording for ambiguous cases) |
| `expected_sql` | yes | gold query; executed to produce the reference table (max 50 rows) |
| `ambiguous` | yes | `true` if the schema graph offers more than one join path |
| `clarification_path_index` | iff ambiguous | which option the simulated user clicks: `0` = shortest/most-direct path, `1` = longer path through an intermediate table |
| `intent` | optional | human-readable description of the wanted interpretation |
| `entity_pair` | optional | the two tables expected to be join-path ambiguous |

The two `store_products_*` cases share identical wording on purpose: the
baseline can satisfy at most one with a single blind guess, while the full
pipeline asks and reaches whichever interpretation the user confirmed.
**Control** cases (`ambiguous: false`) are unambiguous; the full pipeline
should match the baseline and should *not* ask a question.

### What the report measures

The timestamped `results/ab_*.json` report records, per case, both arms'
generated SQL, result table, score, the clarification questions asked (verbatim)
and which mechanism produced them. The summary reports accuracy per arm split
into **ambiguous** and **control** groups, plus:

- on ambiguous cases: how often the full pipeline beat / tied / lost to the
  baseline, and the `clarification_rate`;
- on control cases: the `spurious_clarification_rate` (the layer asking when it
  should not).

A case declares one answer (`clarification_path_index`) for one join-path
clarification. If the pipeline asks anything else -- a second clarification, a
clarification on a control case, or a non-join-path mechanism whose options are
not path-ordered -- the simulated user cannot answer it faithfully, so that case
is flagged **`unreliable`** in the report (with a recorded reason) and listed in
`summary.unreliable_cases`. Read an unreliable case's comparison with caution.

**User trust is intentionally not scored automatically.** It is assessed by a
human reading the recorded clarification questions, which the report preserves
verbatim.

Because SQL generation runs at the production temperature (1.3), individual runs
vary; treat one report as a sample, not a fixed number. The reference execution
and all pure harness logic are covered by `tests/benchmark/`.
