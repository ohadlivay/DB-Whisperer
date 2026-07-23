# DBWhisperer Evaluation V3

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
V3 uses its own suite hash and output directory, so V2 checkpoints cannot be
resumed as V3.

Validate without network access:

```powershell
python -m benchmark_v3.validate_suite
```

Run a live campaign only with an explicit key and budget approval:

```powershell
$env:OPENROUTER_API_KEY = "..."
python -m benchmark_v3.run_evaluation
```

The campaign runner uses the four arms `baseline`, `candidate_only`,
`semantic_only`, and `full`. Baseline generates one candidate; the ambiguity
arms generate three. Its deterministic five-repetition schedule rotates arm
order, runs at most two case/arm cells concurrently, and additionally executes
the two shared ETL fixtures once per repetition. It writes atomic checkpoints
and `campaign.json` before admitting work, then resumes only when campaign and
checkpoint fingerprints match the suite, dataset, model, actual prompt/runtime
sources, scorer, and arms. Dataset/reference artifacts are fingerprinted and
cached in the campaign directory; relationship-discovery warnings are carried
into each report. Terminal progress reports percent, elapsed time, and ETA.
Use `--workers 1` for a conservative serial live run.

Outputs under `benchmark_v3/results/` are ignored by git. Do not commit API
keys, prompt logs, generated DuckDB files, or raw sensitive database values.
