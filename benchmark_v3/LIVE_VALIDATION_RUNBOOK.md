# Evaluation V3 Live Validation Runbook

Run every command from the repository root. API keys are entered only in the
masked external-CMD prompt and remain process-local.

## Deterministic gates

Before using the model:

```powershell
python -m unittest discover
python -m benchmark_v3.validate_suite
python -m benchmark_v3.preflight
```

All tests and every preflight check must pass. The historical counterfactual
rescore must be produced, and neither public HTML file may change.

## Targeted one-repetition validation

Launch this in an external CMD window:

```powershell
benchmark_v3\run_targeted_evaluation.cmd targeted-semantic-regression
```

The first run covers the birth/admission-year, occurrences/distinct-patients,
hospital/ICU-stay, and ICU hospital-mortality cases in `semantic_only` and
`full`. The launcher then checks `ctl_from_2024_admission` in all four arms:
`baseline`, `candidate_only`, `semantic_only`, and `full`.

Required gates:

- no long-title/short-title clarification for “common”;
- no overall-death clarification for “hospital mortality”;
- correct birth/admission target coverage in the Full arm;
- no clarification for the explicit hospital-admission-year control;
- no infrastructure failures;
- every artifact states `publishable: false`;
- every selected cell has `score.passed: true`; and
- `targeted-campaign.json` states `behavioral_passed: true`.

The launcher exits nonzero if cells finish but any behavioral gate fails.
`Overall 100%` means execution completed; only exit code 0 plus
`behavioral_passed: true` means the targeted regression passed.

## Official five-repetition campaign

Only after the targeted evidence passes, launch:

```powershell
benchmark_v3\run_official_evaluation.cmd
```

The terminal shows one campaign-wide line with completed tests out of 450,
percentage, elapsed time, and whole-campaign ETA. Completion creates
`aggregate.json`, `review-package.json`, and `review-package.md`.

Inspect:

```text
benchmark_v3/results/runs/<campaign-id>/review-package.md
benchmark_v3/results/runs/<campaign-id>/review-package.json
```

Do not publish HTML. Discuss the findings with the user first.

## Approved publication

Only after explicit approval of the reviewed aggregate:

```powershell
python -m benchmark_v3.approve_campaign benchmark_v3/results/runs/<campaign-id>
python -m benchmark_v3.publish_reports benchmark_v3/results/runs/<campaign-id>
```

Approval is bound to the campaign ID and aggregate hash. Publication creates
exactly the two approved reports under `docs/`.
