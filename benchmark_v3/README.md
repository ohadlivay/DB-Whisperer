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

Saved campaign evidence can be rescored with the current deterministic scorer
without changing any original checkpoint, run report, campaign file, or
aggregate:

```powershell
python -m benchmark_v3.rescore_campaign benchmark_v3\results\runs\<campaign-id>
```

This writes `counterfactual-rescore.json`. It reuses saved SQL, results, and
clarification transcripts, so it can quantify scoring-policy changes but
cannot measure the new semantic detector or any changed model-facing prompt.
Only a new live campaign measures those system changes.

Run a live campaign only with an explicit key and budget approval:

```powershell
$env:OPENROUTER_API_KEY = "..."
python -m benchmark_v3.run_evaluation
```

For the official external campaign, first validate the suite locally. If
`OPENROUTER_API_KEY` is absent, the launcher requests it through a masked
PowerShell prompt and keeps it only in the external process environment for
the duration of the run. Do not place the key in the launcher, a command file,
source control, or a prompt log. From the repo root, use the no-secret Windows
launcher:

```powershell
benchmark_v3\run_official_evaluation.cmd official-20260723
```

It runs two workers, five repetitions, the frozen $3.75 suite budget, and the
four V3 arms. A new campaign ID is generated when the optional ID is omitted;
use the same safe lowercase ID to resume an interrupted campaign:

```powershell
python -m benchmark_v3.run_evaluation --campaign-id official-20260723 --workers 2 --repetitions 5
```

Campaigns live under `benchmark_v3/results/runs/<campaign-id>`. Checkpoints
and raw run reports are retained. Only a complete, valid five-repetition
official campaign (450 valid system observations: 440 query cells and 10 ETL
observations)
stages `aggregate.json` and both HTML files, then promotes all three with
backups and rollback. Incomplete, budget-stopped, or errored campaigns never
replace the public reports; a failed promotion restores the prior aggregate and
report bytes and records `latest_error`. Runs with fewer than five repetitions
are nonpublishing validation/smoke artifacts and the CLI exits nonzero.
Official publication additionally requires the loaded suite hash to match the
frozen `DEFAULT_SUITE`; a custom `--suite` can retain its own checkpoints and
raw evidence but cannot replace public reports.

The campaign runner uses the four arms `baseline`, `candidate_only`,
`semantic_only`, and `full`. Baseline generates one candidate; the ambiguity
arms generate three. Its deterministic five-repetition schedule rotates arm
order, runs at most two case/arm cells concurrently, and additionally executes
the two shared ETL fixtures once per repetition. It writes atomic checkpoints
only for valid system observations and writes `campaign.json` before admitting
work. Wrong answers and other genuine DB Whisperer failures are checkpointed
and scored; provider error envelopes, credential, transport, dataset
preparation, and harness failures halt new work, remain operational evidence,
and leave affected cells pending for resume. Failed ETL observations are valid
system failures but receive zero ETL credit. The runner resumes only when
campaign and checkpoint fingerprints match the suite, dataset, model, actual
prompt/runtime sources, scorer, and arms. Dataset/reference artifacts are fingerprinted and
cached in the campaign directory; relationship-discovery warnings are carried
into each report. The Windows launcher keeps one in-place `Overall`
line showing completed tests out of all 450 tests, overall percentage,
campaign elapsed time, and campaign ETA. The compact line stays within a
standard 80-column Command Prompt and does not print a separate progress block
for each test. The final terminal message separately states whether those
observations were published and gives the exact blocking reason when they were
not.

Before an official run, replay validation should reject known-contaminated
records and a one-repetition canary should be audited for valid observation
provenance, parsed-filter consistency, and final clarification compliance.
Identifier quoting and table qualification do not change required-filter
semantics, and DuckDB's `YEAR(column)` and
`EXTRACT(YEAR FROM column)` forms are treated as equivalent.

Arm-metric and arm-delta 95% confidence intervals use 2,000 paired,
stratified percentile-bootstrap replicates over both repetitions and question
families. Each replicate preserves the designed ambiguity/control,
correctness, and safety family counts, recomputes the complete score, and
applies the same resample to every arm. Shared-ETL intervals use 2,000
repetition-only percentile-bootstrap replicates because ETL is not an arm or
question-family observation.
Use `--workers 1` for a conservative serial live run.

Outputs under `benchmark_v3/results/` are ignored by git. Do not commit API
keys, prompt logs, generated DuckDB files, or raw sensitive database values.
