# DBWhisperer Evaluation V2

> **Retired:** Evaluation V2 is a preserved historical experiment and is no
> longer runnable after join-path ambiguity removal. Use `benchmark_v3`
> instead.

Evaluation V2 is isolated from the historical `benchmark/` directory. It runs
five deterministic-scoring repetitions of an 18-case MIMIC suite across the
baseline, candidate-only, join-only, semantic-only, and full DBWhisperer arms.

## Validate

```powershell
python benchmark_v2/validate_suite.py
```

## Run with live monitoring

Supply the OpenRouter key through the process environment. Never place it in a
suite, command argument, log, or committed file. A local ignored
`benchmark_v2/.env` may use either `OPENROUTER_API_KEY=<key>` or
`API_KEY=<key>`.

```powershell
$env:OPENROUTER_API_KEY = "<key>"
python benchmark_v2/run_evaluation.py --monitor
```

The runner prints the localhost dashboard URL and writes live state under
`benchmark_v2/results/runs/<campaign-id>/`. Full prompt/response data is
sensitive and is hidden in the dashboard until explicitly revealed.

The campaign uses Gemma 4 31B, `K=2`, and a $3.75 local ceiling. For a hard
server-side guarantee, use an OpenRouter API key whose own credit limit is
$3.75 or lower.

## Aggregate and publish

```powershell
python benchmark_v2/aggregate_results.py benchmark_v2/results/runs/<campaign-id>
python benchmark_v2/render_report.py benchmark_v2/results/aggregate/evaluation_v2_aggregate.json
```

Only five complete compatible run reports can be aggregated. Incomplete
budget-stopped campaigns remain inspectable and resumable but cannot be
published as the final V2 result.
