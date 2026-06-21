# DB Whisperer Benchmark

This standalone benchmark exercises DB Whisperer's ETL and Querier directly.
It does not start Streamlit, call `ApplicationService`, or use the ambiguity
module. All generated databases, prompt logs, and reports remain inside this
directory.

## Run

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
