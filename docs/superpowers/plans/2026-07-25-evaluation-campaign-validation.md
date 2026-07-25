# Evaluation Campaign Validation and Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide deterministic preflight, targeted live regression, external-CMD official execution, and a user-review checkpoint before report publication.

**Architecture:** A preflight command validates the suite, references, runtime, scorer, report model, and historical replay without model calls. A separate targeted runner exercises selected case/arm cells without being publishable. The official launcher runs the complete campaign externally and stops at a validated review package.

**Tech Stack:** Python 3, Windows CMD/PowerShell launchers, OpenRouter, DuckDB, JSON checkpoints, `unittest`.

## Global Constraints

- Never run the paid official campaign inside the Codex session.
- The official campaign runs in an external CMD window.
- Terminal progress is campaign-wide: tests complete/total, percentage, elapsed, ETA.
- Targeted output can never be published as an official aggregate.
- Official results require four arms, K=3, five repetitions, and the $3.75 ceiling.
- HTML remains blocked until explicit post-result approval.
- API keys are masked, never logged, and never persisted.

---

### Task 1: Add deterministic report-readiness preflight

**Files:**
- Create: `benchmark_v3/preflight.py`
- Modify: `benchmark_v3/README.md`
- Test: `tests/benchmark_v3/test_campaign.py`

**Interfaces:**
- CLI: `python -m benchmark_v3.preflight`.
- Produces: terminal checklist and optional `preflight.json`; makes no model calls.

- [ ] **Step 1: Write failing preflight test**

```python
def test_preflight_validates_suite_scorer_and_report_contract_without_network(self) -> None:
    with patch(
        "requests.Session.post",
        side_effect=AssertionError("preflight must not call the network"),
    ):
        result = run_preflight(DEFAULT_SUITE, historical_campaign=self.history)
    self.assertTrue(result.passed)
    self.assertTrue(result.checks["suite"])
    self.assertTrue(result.checks["references"])
    self.assertTrue(result.checks["scorer"])
    self.assertTrue(result.checks["report_contract"])
    self.assertTrue(result.checks["historical_rescore"])
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign
```

Expected: missing preflight module.

- [ ] **Step 3: Implement preflight**

Run these checks in order:

1. suite shape and retired-contract rejection;
2. dataset/reference execution and hashes;
3. scorer fixture matrix;
4. report-model completeness fixture;
5. renderer temporary-output fixture;
6. runner fingerprint calculation;
7. historical deterministic rescore; and
8. no unreviewed HTML mutation.

Return nonzero if any required check fails.

- [ ] **Step 4: Run preflight tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_reporting tests.benchmark_v3.test_scoring
```

Expected: all tests pass.

- [ ] **Step 5: Commit preflight**

```powershell
git add benchmark_v3/preflight.py benchmark_v3/README.md tests/benchmark_v3/test_campaign.py
git commit -m "feat: add deterministic v3 campaign preflight"
```

---

### Task 2: Add a non-publishable targeted live regression runner

**Files:**
- Create: `benchmark_v3/run_targeted_evaluation.py`
- Create: `benchmark_v3/run_targeted_evaluation.cmd`
- Modify: `benchmark_v3/progress.py`
- Test: `tests/benchmark_v3/test_runner.py`
- Test: `tests/benchmark_v3/test_progress.py`

**Interfaces:**
- CLI accepts repeated `--case-id` and `--arm`.
- Produces `targeted-campaign.json` with `publishable: false`.

- [ ] **Step 1: Write failing schedule tests**

```python
def test_targeted_schedule_contains_only_requested_cells(self) -> None:
    schedule = targeted_schedule(
        suite=self.suite,
        case_ids=("from_2024_admission", "diagnoses_occurrences"),
        arms=("semantic_only", "full"),
        repetitions=1,
    )
    self.assertEqual(4, len(schedule))
    self.assertEqual(
        {"from_2024_admission", "diagnoses_occurrences"},
        {item.case_id for item in schedule},
    )

def test_targeted_campaign_is_never_publishable(self) -> None:
    payload = targeted_payload(self.records)
    self.assertFalse(payload["publishable"])
    with self.assertRaisesRegex(ValueError, "targeted"):
        validate_aggregate(payload)
```

- [ ] **Step 2: Run runner tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_progress
```

Expected: missing targeted runner.

- [ ] **Step 3: Implement targeted scheduling**

Validate every case and arm against the suite and exact arm list. Reuse
dataset preparation, `build_services`, `run_cell`, observability, and
campaign-wide progress. Do not call official aggregation or publication.

- [ ] **Step 4: Implement targeted summary**

Write:

```json
{
  "report_type": "dbwhisperer_v3_targeted_regression",
  "publishable": false,
  "suite_hash": "hash",
  "model": "model",
  "case_ids": [],
  "arms": [],
  "repetitions": 1,
  "records": [],
  "usage": {},
  "terminal_summary": {}
}
```

- [ ] **Step 5: Add external CMD launcher**

Mirror secure masked-key behavior from `run_official_evaluation.cmd`. Display
only overall targeted progress, elapsed time, completion percentage, and ETA.

- [ ] **Step 6: Run targeted-runner tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_progress
```

Expected: all tests pass.

- [ ] **Step 7: Commit targeted runner**

```powershell
git add benchmark_v3/run_targeted_evaluation.py benchmark_v3/run_targeted_evaluation.cmd benchmark_v3/progress.py tests/benchmark_v3/test_runner.py tests/benchmark_v3/test_progress.py
git commit -m "feat: add targeted v3 live regression runner"
```

---

### Task 3: Update the official external launcher for review-ready completion

**Files:**
- Modify: `benchmark_v3/run_official_evaluation.cmd`
- Modify: `benchmark_v3/run_evaluation.py`
- Test: `tests/benchmark_v3/test_campaign.py`
- Test: `tests/benchmark_v3/test_progress.py`

**Interfaces:**
- Official CMD exits zero at validated review readiness.
- No automatic HTML publication.

- [ ] **Step 1: Write failing CLI completion tests**

```python
def test_main_reports_review_ready_instead_of_published(self) -> None:
    result = CampaignRunResult(
        complete=True,
        aggregate_ready=True,
        review_ready=True,
        stop_reason="",
    )
    with patch("benchmark_v3.run_evaluation.run_campaign", return_value=result):
        with self.assertLogs(level="INFO") as captured:
            main_for_test(["--campaign-id", "official-test"])
    self.assertIn("review package", "\n".join(captured.output).casefold())
```

- [ ] **Step 2: Run campaign tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_progress
```

Expected: CLI still requires `published`.

- [ ] **Step 3: Update completion messages and exit conditions**

On success print:

```text
Overall 100.0% | tests 450/450 | elapsed HH:MM:SS | ETA 00:00:00
Campaign complete and review-ready. No HTML reports were generated.
Review: <campaign-dir>\review-package.md
```

On aggregate/review failure, keep observations resumable and return nonzero.

- [ ] **Step 4: Verify masked key handling**

Test that the CMD script prompts only when `OPENROUTER_API_KEY` is absent,
passes it to the child process environment, and clears the process-local value
on exit without writing it to disk or echoing it.

- [ ] **Step 5: Run campaign/progress tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_progress
```

Expected: all tests pass.

- [ ] **Step 6: Commit launcher behavior**

```powershell
git add benchmark_v3/run_official_evaluation.cmd benchmark_v3/run_evaluation.py tests/benchmark_v3/test_campaign.py tests/benchmark_v3/test_progress.py
git commit -m "feat: finish official campaigns at review readiness"
```

---

### Task 4: Define the staged live validation matrix and runbook

**Files:**
- Create: `benchmark_v3/LIVE_VALIDATION_RUNBOOK.md`
- Modify: `benchmark_v3/README.md`
- Modify: `docs/EVALUATION_V3_METHOD_CHANGES.md`
- Test: `tests/benchmark_v3/test_campaign.py`

**Interfaces:**
- Documents exact targeted and official commands.

- [ ] **Step 1: Add failing runbook-content test**

```python
def test_live_runbook_orders_targeted_before_official(self) -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    self.assertLess(text.index("Targeted one-repetition"), text.index("Official five-repetition"))
    self.assertIn("semantic_only", text)
    self.assertIn("full", text)
    self.assertIn("review-package.md", text)
    self.assertIn("Do not publish HTML", text)
```

- [ ] **Step 2: Write the exact targeted matrix**

One repetition:

```text
Cases:
from_2024_birth
from_2024_admission
ctl_from_2024_birth
ctl_from_2024_admission
diagnoses_occurrences
diagnoses_distinct_patients
ctl_diagnoses_occurrences
ctl_diagnoses_distinct_patients
stay_hospital
stay_icu
ctl_stay_hospital
ctl_stay_icu
icu_mortality_by_first_careunit

Arms:
semantic_only
full
```

Run the rewritten admission control across all four arms in a second targeted
command because its user wording changed.

- [ ] **Step 3: Document pass gates**

Before official execution require:

- full deterministic test suite passing;
- preflight passing;
- historical rescore generated;
- no long/short-title clarification for `common`;
- no overall-death clarification for `hospital mortality`;
- birth/admission target coverage in the Full arm;
- explicit hospital-admission control not clarified;
- no infrastructure failures; and
- report-readiness checklist fully true.

- [ ] **Step 4: Document official and review commands**

The official campaign is launched by double-clicking or invoking:

```powershell
benchmark_v3\run_official_evaluation.cmd
```

After completion, inspect:

```text
benchmark_v3/results/runs/<campaign-id>/review-package.md
benchmark_v3/results/runs/<campaign-id>/review-package.json
```

State: do not approve or publish HTML until findings are discussed with the
user.

- [ ] **Step 5: Run documentation tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign
```

Expected: all tests pass.

- [ ] **Step 6: Commit runbook**

```powershell
git add benchmark_v3/LIVE_VALIDATION_RUNBOOK.md benchmark_v3/README.md docs/EVALUATION_V3_METHOD_CHANGES.md tests/benchmark_v3/test_campaign.py
git commit -m "docs: define staged v3 campaign validation"
```

---

### Task 5: Execute deterministic verification before any paid run

**Files:**
- Generated only: `benchmark_v3/results/runs/<historical-id>/counterfactual-rescore.json`
- No source modifications expected.

**Interfaces:**
- Produces test and preflight evidence for user review.

- [ ] **Step 1: Run the full test suite**

Run:

```powershell
python -m unittest discover
```

Expected: all tests pass, with only the existing intentional skip.

- [ ] **Step 2: Run whitespace and suite validation**

Run:

```powershell
git diff --check
python -m benchmark_v3.validate_suite
```

Expected: no whitespace errors and suite validation passes.

- [ ] **Step 3: Run deterministic preflight**

Run:

```powershell
python -m benchmark_v3.preflight
```

Expected: every required check reports `PASS`; no network calls occur.

- [ ] **Step 4: Rescore the historical official campaign**

Run:

```powershell
python -m benchmark_v3.rescore_campaign benchmark_v3/results/runs/v3-official-evidence-final-20260724
```

Expected: a new `counterfactual-rescore.json`; original campaign files remain
byte-for-byte unchanged.

- [ ] **Step 5: Report readiness to the user**

Provide:

- test count and failures/skips;
- suite and runtime hashes;
- historical original versus counterfactual score;
- report-readiness checklist;
- exact targeted CMD command; and
- confirmation that no HTML or paid campaign has run.

Do not start live execution until this evidence is reviewed.

---

### Task 6: Run live campaigns and stop for result approval

**Files:**
- Runtime artifacts only under `benchmark_v3/results/runs/`.
- Do not modify `docs/evaluation_method_one_page.html`.
- Do not modify `docs/evaluation_report.html`.

**Interfaces:**
- Produces targeted evidence, then one official review package.

- [ ] **Step 1: Launch the targeted one-repetition campaign externally**

Run in an external CMD window:

```powershell
benchmark_v3\run_targeted_evaluation.cmd
```

Expected: overall progress only; targeted artifact has `publishable: false`.

- [ ] **Step 2: Analyze targeted behavior**

Verify every pass gate from Task 4. If a gate fails, stop, diagnose the system
or harness, add a failing deterministic test, fix, and repeat the targeted run.

- [ ] **Step 3: Launch the official campaign externally**

Run:

```powershell
benchmark_v3\run_official_evaluation.cmd
```

Expected: 450/450 valid observations, aggregate and review package present,
no HTML generated.

- [ ] **Step 4: Analyze the official review package**

Check completeness, infrastructure state, arm metrics, confidence intervals,
semantic correctness, projection diagnostics, ambiguity behavior, terminal
outcomes, cost, duration, and representative evidence.

- [ ] **Step 5: Send results to the user and stop**

Provide the findings and supporting numbers in chat. Explicitly state that:

- the aggregate is complete or incomplete;
- no HTML reports have been generated;
- publication awaits approval; and
- any unresolved methodological concern blocks approval.

- [ ] **Step 6: Publish only after explicit approval**

After the user approves the results:

```powershell
python -m benchmark_v3.approve_campaign benchmark_v3/results/runs/<campaign-id>
python -m benchmark_v3.publish_reports benchmark_v3/results/runs/<campaign-id>
```

Expected: exactly two approved HTML reports replace the documents under
`docs/`, with the approval hash recorded in campaign evidence.
