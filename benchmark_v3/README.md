# DBWhisperer Evaluation V3.1

Evaluation V3 tests the hybrid ambiguity pipeline independently from legacy
V2. Candidate SQL/result differences are primary; semantic-column findings and
direct relationship metadata are supporting evidence. Relationship-path
multiplicity is not an ambiguity type.

Candidate-only and full arms retain exact-alternative support counts. Candidate
differences are actionable only when both sides are natural, coherent readings
of the request; support counts are soft confidence rather than majority voting.

The four arms are `baseline`, `candidate_only`, `semantic_only`, and `full`.
The semantic-only judge is not shown executed alternatives for ambiguity
selection. After a user chooses an option, executed alternatives are shown in
a separate compliance-only section so every arm follows the shared rule that
the final SQL must apply the selected clarification.
V3 records `applied_to_final_sql`; a clarification-aware run passes only when
the selected option matches the declared intent, compliance is verified, and
the executed result matches the reference.
Reference scoring also checks the declared table, output-column, and minimum-
join constraints. The two ETL fixture manifests run once per report and are
included in aggregation.
V3 uses its own suite hash and output directory, so V2 checkpoints cannot be
resumed as V3.

Validate without network access:

```powershell
python -m benchmark_v3.validate_suite
```

Run a complete live campaign only with an explicit key and budget approval:

```powershell
$env:OPENROUTER_API_KEY = "..."
python -m benchmark_v3.run_evaluation
```

That single command ingests the dataset, prepares every reference result once,
runs and scores all four arms, then writes:

- `benchmark_v3/results/evaluation_v3_1.json`
- `benchmark_v3/results/evaluation_v3_1.html`
- `benchmark_v3/results/evaluation_v3_1_cases.html`
- `benchmark_v3/results/evaluation_v3_1.progress.jsonl`

It uses two independent workers by default. Reduce concurrency if the provider
throttles, or increase it cautiously up to eight workers:

```powershell
python -m benchmark_v3.run_evaluation --workers 1
python -m benchmark_v3.run_evaluation --workers 4
```

Every completed arm is printed with elapsed time and ETA. From another CMD
window, follow the sanitized progress log with:

```cmd
powershell -Command "Get-Content benchmark_v3\results\evaluation_v3_1.progress.jsonl -Wait"
```

Full prompts and model responses remain in `logs/prompts.jsonl`, or the path
selected by `DB_WHISPERER_PROMPT_LOG`. That detailed log may contain sensitive
dataset samples; the progress log does not contain prompts, SQL rows, or keys.
An interrupted run keeps its completed progress events but is not resumable.

Outputs under `benchmark_v3/results/` are ignored by git. Do not commit API
keys, prompt logs, generated DuckDB files, or raw sensitive database values.

Historical V3 output remains immutable. Rescore that artifact offline when
needed; this executes only local reference SQL, never model calls:

```powershell
python -m benchmark_v3.rescore_results benchmark_v3/results/evaluation_v3.json
python -m benchmark_v3.render_report benchmark_v3/results/evaluation_v3_rescored.json
```

Corrected reports are explicitly retrospective. Their fixed composite is the
equal average of answer correctness, ambiguity resolution, control specificity,
and safety behavior. ETL remains a shared prerequisite rather than an arm score.
