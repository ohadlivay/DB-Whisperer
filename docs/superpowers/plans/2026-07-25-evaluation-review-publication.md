# Evaluation Review and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee report-data completeness before paid runs, generate a review package before HTML, and publish the two reference-quality reports only from an explicitly approved aggregate.

**Architecture:** Aggregation produces immutable machine-readable evidence. A report contract validates the presentation model, and a review-package builder produces JSON/Markdown without HTML. Approval records the campaign identity and aggregate hash; a separate publication command validates that approval before rendering and atomically promoting exactly two reports.

**Tech Stack:** Python 3, JSON/Markdown, existing HTML renderers, SHA-256, atomic file replacement, `unittest`.

## Global Constraints

- Campaign execution must never overwrite either HTML report.
- Renderer tests use temporary paths only.
- Approval is tied to campaign ID and aggregate SHA-256.
- Publication makes no model calls.
- Exactly two final HTML reports are produced after approval.
- The reference documents define information hierarchy and content types, not only colors.
- All model-derived text and result values are HTML escaped.
- Original, counterfactual, and new live results are clearly distinguished.

---

### Task 1: Define and validate the complete report-data contract

**Files:**
- Create: `benchmark_v3/report_contract.py`
- Modify: `benchmark_v3/report_model.py`
- Test: `tests/benchmark_v3/test_reporting.py`

**Interfaces:**
- Produces: `validate_report_model(model: Mapping[str, Any]) -> None`.
- `build_report_model` returns a validated model.

- [ ] **Step 1: Write a failing completeness test**

```python
def test_report_model_contains_every_approved_information_type(self) -> None:
    model = build_report_model(complete_aggregate())
    validate_report_model(model)
    for key in (
        "research_question",
        "experimental_design",
        "methodology",
        "provenance",
        "headline_metrics",
        "arm_deltas",
        "ambiguity_funnel",
        "correctness_diagnostics",
        "projection_diagnostics",
        "terminal_outcomes",
        "case_findings",
        "findings",
        "interpretations",
        "recommendations",
        "limitations",
        "report_readiness",
    ):
        self.assertIn(key, model)
```

- [ ] **Step 2: Run reporting tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting
```

Expected: missing `report_contract` or required keys.

- [ ] **Step 3: Implement strict validation**

Define required top-level keys and validate:

- four exact arms;
- finite headline distributions and deltas;
- all ambiguity submetrics;
- terminal categories and denominators;
- non-empty research question, methodology, findings, and limitations;
- representative success and failure cases;
- usage/cost/elapsed provenance;
- explicit result provenance label; and
- readiness checklist with every item `true`.

Raise `ValueError` with the missing dotted path.

- [ ] **Step 4: Expand `build_report_model`**

Remove the three hard-coded findings. Build typed findings from aggregate
evidence:

```python
{
    "finding_id": "full_vs_baseline_composite",
    "kind": "comparative",
    "claim": claim,
    "evidence": {
        "baseline": baseline_distribution,
        "full": full_distribution,
        "delta": full_delta,
    },
    "caveat": caveat,
}
```

Keep findings, interpretations, and recommendations in separate arrays.

- [ ] **Step 5: Run reporting tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting
```

Expected: all report-model contract tests pass.

- [ ] **Step 6: Commit the report contract**

```powershell
git add benchmark_v3/report_contract.py benchmark_v3/report_model.py tests/benchmark_v3/test_reporting.py
git commit -m "feat: validate v3 report data completeness"
```

---

### Task 2: Generate JSON and Markdown review packages without HTML

**Files:**
- Create: `benchmark_v3/review_package.py`
- Test: `tests/benchmark_v3/test_reporting.py`

**Interfaces:**
- Produces: `write_review_package(aggregate_path, output_dir) -> tuple[Path, Path]`.
- Files: `review-package.json`, `review-package.md`.

- [ ] **Step 1: Write failing package tests**

```python
def test_review_package_contains_claims_and_case_evidence_without_html(self) -> None:
    outputs = write_review_package(self.aggregate_path, self.output_dir)
    self.assertEqual(
        ("review-package.json", "review-package.md"),
        tuple(path.name for path in outputs),
    )
    self.assertFalse(any(path.suffix == ".html" for path in self.output_dir.iterdir()))
    markdown = outputs[1].read_text(encoding="utf-8")
    self.assertIn("# Campaign Review", markdown)
    self.assertIn("## Arm comparison", markdown)
    self.assertIn("## Clarification findings", markdown)
    self.assertIn("## Terminal outcomes", markdown)
    self.assertIn("## Report-readiness checklist", markdown)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting
```

Expected: missing `write_review_package`.

- [ ] **Step 3: Implement review-package serialization**

Build the validated report model, write its review-safe subset as JSON, then
write Markdown in this order:

1. validity and provenance;
2. executive metrics;
3. arm comparison and confidence intervals;
4. ambiguity funnel;
5. correctness/projection diagnostics;
6. terminal outcomes;
7. family/case evidence;
8. proposed findings;
9. limitations; and
10. readiness checklist.

Use JSON fences for SQL/result excerpts and cap rows deterministically.

- [ ] **Step 4: Verify no HTML side effect**

Patch `benchmark_v3.render_report.write_reports` to raise if called; assert
review-package generation still succeeds.

- [ ] **Step 5: Commit review packaging**

```powershell
git add benchmark_v3/review_package.py tests/benchmark_v3/test_reporting.py
git commit -m "feat: create pre-publication review package"
```

---

### Task 3: Stop campaign completion before publication

**Files:**
- Modify: `benchmark_v3/run_evaluation.py`
- Modify: `tests/benchmark_v3/test_campaign.py`
- Modify: `tests/benchmark_v3/test_runner.py`

**Interfaces:**
- `run_campaign` finalizes aggregate and review package but does not render HTML.
- `CampaignRunResult` exposes `aggregate_ready` and `review_ready`; publication state is not a run requirement.

- [ ] **Step 1: Write failing no-auto-publication test**

```python
def test_complete_campaign_aggregates_and_reviews_without_publishing_html(self) -> None:
    with patch(
        "benchmark_v3.render_report.write_reports",
        side_effect=AssertionError("HTML publication must not run"),
    ):
        result = run_complete_campaign(self.config)
    self.assertTrue(result.aggregate_ready)
    self.assertTrue(result.review_ready)
    self.assertFalse((PROJECT_ROOT / "docs" / "evaluation_report.html").stat().st_mtime_ns > self.before)
```

- [ ] **Step 2: Run campaign tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_runner
```

Expected: existing runner calls `publish_campaign`.

- [ ] **Step 3: Replace auto-publication with finalization**

Implement:

```python
def finalize_campaign(campaign_dir: Path) -> tuple[Path, tuple[Path, Path]]:
    aggregate = aggregate_campaign(campaign_dir)
    validate_aggregate(aggregate)
    aggregate_path = campaign_dir / "aggregate.json"
    atomic_json(aggregate_path, aggregate)
    review_paths = write_review_package(aggregate_path, campaign_dir)
    return aggregate_path, review_paths
```

Update campaign state to `review_ready`, not `published`.

- [ ] **Step 4: Update CLI success conditions**

An official five-repetition command exits zero when processing, aggregate
validation, report-readiness validation, and review-package creation succeed.
Its final terminal message says that HTML awaits explicit approval.

- [ ] **Step 5: Run campaign tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_runner
```

Expected: all tests pass and no renderer is called.

- [ ] **Step 6: Commit two-phase completion**

```powershell
git add benchmark_v3/run_evaluation.py tests/benchmark_v3/test_campaign.py tests/benchmark_v3/test_runner.py
git commit -m "refactor: stop v3 campaigns at review"
```

---

### Task 4: Add hash-bound approval and separate publication CLI

**Files:**
- Create: `benchmark_v3/publication.py`
- Create: `benchmark_v3/approve_campaign.py`
- Create: `benchmark_v3/publish_reports.py`
- Test: `tests/benchmark_v3/test_campaign.py`

**Interfaces:**
- Approval CLI: `python -m benchmark_v3.approve_campaign <campaign-dir>`.
- Publish CLI: `python -m benchmark_v3.publish_reports <campaign-dir>`.
- Approval artifact: `report-approval.json`.

- [ ] **Step 1: Write failing approval tests**

```python
def test_approval_binds_campaign_and_aggregate_hash(self) -> None:
    approval = approve_campaign(self.directory, approved_by="user")
    payload = json.loads(approval.read_text(encoding="utf-8"))
    self.assertEqual(self.directory.name, payload["campaign_id"])
    self.assertEqual(sha256_file(self.aggregate), payload["aggregate_sha256"])
    self.assertEqual("user", payload["approved_by"])

def test_publish_rejects_changed_aggregate(self) -> None:
    approve_campaign(self.directory, approved_by="user")
    self.aggregate.write_text('{"changed":true}', encoding="utf-8")
    with self.assertRaisesRegex(ValueError, "approval hash"):
        publish_approved_campaign(self.directory)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign
```

Expected: missing approval module.

- [ ] **Step 3: Implement approval**

Require a complete validated aggregate and both review-package files. Write:

```json
{
  "report_type": "dbwhisperer_v3_report_approval",
  "campaign_id": "campaign-id",
  "aggregate_sha256": "hex-digest",
  "approved_by": "user",
  "approved_at": "UTC ISO-8601"
}
```

The command is run only after explicit user approval in this conversation.

- [ ] **Step 4: Implement publication**

Validate the aggregate, report model, approval identity, and hash. Render to a
staging directory, verify exactly two non-empty HTML files, then atomically
promote:

- `docs/evaluation_method_one_page.html`
- `docs/evaluation_report.html`

On any error, restore both previous files and leave aggregate/review evidence
unchanged.

- [ ] **Step 5: Run campaign publication tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign
```

Expected: approval, mismatch rejection, atomic promotion, and rollback pass.

- [ ] **Step 6: Commit approval-gated publication**

```powershell
git add benchmark_v3/publication.py benchmark_v3/approve_campaign.py benchmark_v3/publish_reports.py tests/benchmark_v3/test_campaign.py
git commit -m "feat: require approval before v3 report publication"
```

---

### Task 5: Rebuild both renderers against the reference content hierarchy

**Files:**
- Modify: `benchmark_v3/render_report.py`
- Test: `tests/benchmark_v3/test_reporting.py`
- Reference only: `docs/evaluation_method_one_page.html`
- Reference only: `docs/evaluation_report.html`

**Interfaces:**
- Preserves: `render_one_page`, `render_full_report`, and `write_reports`.
- Consumes only a validated report model.

- [ ] **Step 1: Add failing content-acceptance tests**

For the concise report, assert sections for:

```python
ONE_PAGE_SECTIONS = (
    "Research question",
    "Experimental design",
    "Scoring framework",
    "Headline results",
    "Ambiguity funnel",
    "Correctness diagnostics",
    "Principal findings",
    "Limitations",
)
```

For the detailed report, assert tabs/sections for methodology, comparison,
ambiguity, correctness/projection, terminal outcomes, case evidence,
operations, and limitations.

- [ ] **Step 2: Run reporting tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting
```

Expected: missing approved content sections.

- [ ] **Step 3: Rebuild the one-page renderer**

Reuse the reference document's information hierarchy and concise explanatory
style. Populate all values from the model. Keep the page printable and avoid
hard-coded campaign claims.

- [ ] **Step 4: Rebuild the detailed renderer**

Retain accessible keyboard tabs, sorting, responsive layout, print control,
theme control, escaped evidence, case SQL/result transcripts, and the approved
analytical sections. Show findings separately from interpretation and
recommendations.

- [ ] **Step 5: Add fixture-based snapshot contracts**

Render only into `TemporaryDirectory`. Assert:

- two outputs;
- every required content section;
- no placeholder text;
- no V2 or join-path language;
- escaped hostile strings;
- campaign values appear in both reports; and
- renderer does not mutate its model.

- [ ] **Step 6: Run reporting tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting
```

Expected: all tests pass.

- [ ] **Step 7: Commit report parity**

```powershell
git add benchmark_v3/render_report.py tests/benchmark_v3/test_reporting.py
git commit -m "feat: match v3 reports to reference content"
```

---

### Task 6: Document and verify publication workflow

**Files:**
- Modify: `benchmark_v3/README.md`
- Modify: `docs/EVALUATION_V3_METHOD_CHANGES.md`
- Test: `tests/benchmark_v3/test_reporting.py`

**Interfaces:**
- Documents exact run → review → approve → publish commands.

- [ ] **Step 1: Add documentation assertions**

```python
def test_documentation_requires_review_before_publish(self) -> None:
    text = README.read_text(encoding="utf-8")
    self.assertIn("review-package.md", text)
    self.assertIn("approve_campaign", text)
    self.assertIn("publish_reports", text)
    self.assertLess(text.index("review-package.md"), text.index("approve_campaign"))
    self.assertLess(text.index("approve_campaign"), text.index("publish_reports"))
```

- [ ] **Step 2: Update documentation**

Include these exact commands:

```powershell
python -m benchmark_v3.approve_campaign benchmark_v3/results/runs/<campaign-id>
python -m benchmark_v3.publish_reports benchmark_v3/results/runs/<campaign-id>
```

State that neither command makes model calls and approval must follow user
review of the JSON/Markdown package.

- [ ] **Step 3: Run reporting and campaign tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_reporting tests.benchmark_v3.test_campaign
```

Expected: all tests pass.

- [ ] **Step 4: Commit documentation**

```powershell
git add benchmark_v3/README.md docs/EVALUATION_V3_METHOD_CHANGES.md tests/benchmark_v3/test_reporting.py
git commit -m "docs: describe approval-gated evaluation reports"
```

- [ ] **Step 5: Final verification**

Run:

```powershell
python -m unittest discover
git diff --check
```

Expected: full suite passes and no whitespace errors are reported.
