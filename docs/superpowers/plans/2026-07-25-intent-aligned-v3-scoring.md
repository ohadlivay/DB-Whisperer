# Intent-Aligned Evaluation V3 Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score user-intent result compatibility independently from projection fidelity, normalize legitimate duration/ranking representations, and expose precise workflow terminal outcomes.

**Architecture:** Extend frozen case contracts with explicit semantic comparison rules. The scorer maps required expected concepts into actual results, then applies duration and ordering policies. The runner records terminal and pre-clarification evidence separately so aggregation can distinguish SQL quality from end-to-end completion.

**Tech Stack:** Python 3, DuckDB result evidence, SQLGlot-based analysis already wrapped by `benchmark_v3.sql_analysis`, JSON suite contracts, `unittest`.

## Global Constraints

- The original published campaign and its aggregate remain immutable.
- A rescore is labelled counterfactual and written to a new artifact.
- Correctness does not require exact SQL text, exact aliases, or exact width.
- Extra columns still fail when they change grain, cardinality, meaning, safety, or privacy.
- Join efficiency remains correctness-gated and separate.
- Provider, transport, credential, and harness failures remain infrastructure failures.
- Four arms, K=3, five repetitions, and the $3.75 ceiling remain unchanged.

---

### Task 1: Extend case contracts for semantic projection, duration, and order

**Files:**
- Modify: `benchmark_v3/contracts.py`
- Modify: `benchmark_v3/cases/evaluation_cases.json`
- Test: `tests/benchmark_v3/test_contracts.py`

**Interfaces:**
- Produces: `DurationContract` and new fields on `ReferenceContract`.
- Consumed later by `results_compatible`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_duration_contract_loads_supported_representations(self) -> None:
    case = next(
        case
        for case in load_suite(DEFAULT_SUITE).cases
        if case.id == "admission_duration_null_safe"
    )
    self.assertEqual("day", case.reference.duration.unit)
    self.assertEqual(
        ("integer", "decimal", "interval"),
        case.reference.duration.representations,
    )
    self.assertFalse(case.reference.duration.subunit_precision_required)

def test_rank_contract_does_not_require_numeric_rank_column(self) -> None:
    case = next(
        case
        for case in load_suite(DEFAULT_SUITE).cases
        if case.id == "patients_with_multiple_admissions_ranked"
    )
    self.assertEqual("ranked", case.reference.order_semantics)
    self.assertFalse(case.reference.rank_column_required)
    self.assertTrue(case.reference.tie_aware)
```

- [ ] **Step 2: Run contract tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_contracts
```

Expected: failure because `ReferenceContract.duration` does not exist.

- [ ] **Step 3: Add explicit reference contracts**

```python
@dataclass(frozen=True)
class DurationContract:
    unit: str
    representations: tuple[str, ...]
    subunit_precision_required: bool = False

@dataclass(frozen=True)
class ReferenceContract:
    comparison_mode: str
    required_filters: tuple[str, ...] = ()
    required_grouping: tuple[str, ...] = ()
    ordered: bool = False
    limit: int | None = None
    projection_mode: str = "required_subset"
    order_semantics: str = "none"
    rank_column_required: bool = False
    tie_aware: bool = False
    duration: DurationContract | None = None
```

Validate allowed values:

```python
PROJECTION_MODES = {"exact", "required_subset"}
ORDER_SEMANTICS = {"none", "ranked", "chronological"}
DURATION_REPRESENTATIONS = {"integer", "decimal", "interval"}
```

- [ ] **Step 4: Update the suite**

Make the admission control wording exactly:

```json
"question": "Show patients admitted to the hospital in the year 2112."
```

Add duration contracts to `stay_hospital`, `ctl_stay_hospital`,
`admission_duration_null_safe`, `stay_icu`, and `ctl_stay_icu`.

Set ranking cases to `order_semantics: "ranked"`, `tie_aware: true`, and
`rank_column_required: false`. Keep explicit “highest count first” ordering
material, but mark reference-only tie-breakers non-material.

- [ ] **Step 5: Run suite validation**

Run:

```powershell
python -m benchmark_v3.validate_suite
python -m unittest tests.benchmark_v3.test_contracts
```

Expected: suite validation and contract tests pass with a new suite hash.

- [ ] **Step 6: Commit case contracts**

```powershell
git add benchmark_v3/contracts.py benchmark_v3/cases/evaluation_cases.json tests/benchmark_v3/test_contracts.py
git commit -m "feat: declare intent-aligned v3 result contracts"
```

---

### Task 2: Compare required result concepts without exact-width failure

**Files:**
- Modify: `benchmark_v3/scoring.py`
- Test: `tests/benchmark_v3/test_scoring.py`

**Interfaces:**
- Produces: `map_required_columns(actual, expected, case, analysis)`.
- Returns an unambiguous expected-to-actual index tuple or a reason.

- [ ] **Step 1: Write failing subset and ambiguity tests**

```python
def test_harmless_extra_columns_preserve_correctness(self) -> None:
    actual = accepted(
        columns=("subject_id", "hadm_id", "admittime", "dischtime", "los"),
        rows=((10006, 142345, START, END, 9),),
    )
    expected = accepted(
        columns=("hadm_id", "admittime", "dischtime", "hospital_los_days"),
        rows=((142345, START, END, 9),),
    )
    compatible, reason = results_compatible(
        actual, expected, hospital_case(), analyze_sql(actual.sql)
    )
    self.assertTrue(compatible, reason)

def test_extra_grouping_column_that_duplicates_rows_fails(self) -> None:
    actual = accepted(
        columns=("admission_type", "insurance", "admission_count"),
        rows=(("EMERGENCY", "Medicare", 10), ("EMERGENCY", "Private", 7)),
    )
    expected = accepted(
        columns=("admission_type", "admission_count"),
        rows=(("EMERGENCY", 17),),
    )
    compatible, _ = results_compatible(
        actual, expected, admissions_by_type_case(), analyze_sql(actual.sql)
    )
    self.assertFalse(compatible)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring
```

Expected: extra-width case fails with `exact comparison requires matching column widths`.

- [ ] **Step 3: Implement unambiguous required-column mapping**

Mapping order:

1. unique normalized exact column name;
2. unique normalized value vector;
3. unique derived-expression concept from `SQLAnalysis`;
4. fail closed when zero or multiple mappings remain.

Return:

```python
@dataclass(frozen=True)
class ProjectionMatch:
    actual_indexes: tuple[int, ...]
    extra_indexes: tuple[int, ...]
    aliases_used: tuple[tuple[str, str], ...]
```

Project actual rows through `actual_indexes` before comparison. Compare the
projected multiset to the expected rows so extra columns cannot hide
cardinality changes.

- [ ] **Step 4: Record projection diagnostics**

Return a `comparison` mapping from `score_query_case`:

```python
{
    "semantic_compatible": compatible,
    "projection_precision": required_count / actual_count,
    "extra_columns": list(extra_columns),
    "aliases_used": [list(pair) for pair in aliases_used],
    "ordering_material": ordering_material,
    "duration_representation": duration_representation,
}
```

- [ ] **Step 5: Run scoring tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring
```

Expected: subset cases pass; grain-changing extras fail.

- [ ] **Step 6: Commit semantic projection**

```powershell
git add benchmark_v3/scoring.py tests/benchmark_v3/test_scoring.py
git commit -m "feat: score required result concepts"
```

---

### Task 3: Normalize duration representations

**Files:**
- Modify: `benchmark_v3/scoring.py`
- Test: `tests/benchmark_v3/test_scoring.py`

**Interfaces:**
- Produces: `normalize_duration(value, contract) -> NormalizedDuration | None`.

- [ ] **Step 1: Write failing duration tests**

```python
def test_fractional_integer_and_interval_days_are_compatible(self) -> None:
    contract = DurationContract(
        unit="day",
        representations=("integer", "decimal", "interval"),
        subunit_precision_required=False,
    )
    self.assertTrue(duration_values_compatible(8.8375, 9, contract))
    self.assertTrue(
        duration_values_compatible(
            8.8375,
            "8 days, 20:06:00",
            contract,
        )
    )

def test_raw_timestamp_is_not_a_duration(self) -> None:
    contract = day_duration_contract()
    self.assertFalse(
        duration_values_compatible(
            8.8375,
            "2164-11-01 17:15:00",
            contract,
        )
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring
```

Expected: missing `duration_values_compatible`.

- [ ] **Step 3: Implement normalization**

```python
@dataclass(frozen=True)
class NormalizedDuration:
    seconds: float
    representation: str

_INTERVAL = re.compile(
    r"^(?P<days>-?\d+) days?, "
    r"(?P<hours>\d{1,2}):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d+)?)$"
)
```

Numeric decimal values use the declared unit. Integer values compare against
the nearest whole declared unit when subunit precision is not required.
Intervals compare in seconds with
`math.isclose(expected.seconds, actual.seconds, rel_tol=0.0, abs_tol=1.0)`.

- [ ] **Step 4: Integrate duration-aware column comparison**

Apply duration normalization only to columns named by a duration contract and
the mapped required duration concept. Do not globally treat arbitrary numeric
columns as durations.

- [ ] **Step 5: Run scoring tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring
```

Expected: integer, fractional, and interval cases pass; raw timestamps fail.

- [ ] **Step 6: Commit duration scoring**

```powershell
git add benchmark_v3/scoring.py tests/benchmark_v3/test_scoring.py
git commit -m "feat: normalize v3 duration answers"
```

---

### Task 4: Make ranking and top-N tie-aware

**Files:**
- Modify: `benchmark_v3/scoring.py`
- Modify: `benchmark_v3/sql_analysis.py`
- Test: `tests/benchmark_v3/test_scoring.py`
- Test: `tests/benchmark_v3/test_sql_analysis.py`

**Interfaces:**
- Produces:
  `ordering_satisfies_intent(case: EvaluationCase, actual: SQLAnalysis, expected: SQLAnalysis) -> bool`
  and
  `tie_aware_top_n_match(actual: QueryResult, expected: QueryResult, rank_key: int) -> bool`.

- [ ] **Step 1: Write failing ranking tests**

```python
def test_ordered_top_ten_does_not_require_rank_projection(self) -> None:
    actual = ranked_result(include_rank=False)
    expected = ranked_reference(include_rank=True)
    compatible, reason = results_compatible(
        actual, expected, ranked_case(), analyze_sql(actual.sql)
    )
    self.assertTrue(compatible, reason)

def test_tied_boundary_member_is_accepted(self) -> None:
    expected = top_ten_with_last_subject(40310, count=2)
    actual = top_ten_with_last_subject(40503, count=2)
    self.assertTrue(tie_aware_top_n_match(actual, expected, rank_key=1))
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring tests.benchmark_v3.test_sql_analysis
```

Expected: rank-column and tied-boundary cases fail.

- [ ] **Step 3: Implement material ordering**

For `order_semantics == "ranked"`, require the primary requested sort
direction but do not require reference-only tie-breakers. For
`order_semantics == "none"`, compare as a multiset even when the reference SQL
contains deterministic ordering.

- [ ] **Step 4: Implement tie-aware top-N**

Require all rows strictly above the boundary to match. At the boundary, accept
any entities with the same rank measure. Reject a result that substitutes a
lower measure.

- [ ] **Step 5: Run scoring and SQL-analysis tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring tests.benchmark_v3.test_sql_analysis
```

Expected: all tests pass.

- [ ] **Step 6: Commit ordering semantics**

```powershell
git add benchmark_v3/scoring.py benchmark_v3/sql_analysis.py tests/benchmark_v3/test_scoring.py tests/benchmark_v3/test_sql_analysis.py
git commit -m "feat: score material ordering and tied rankings"
```

---

### Task 5: Record precise terminal and pre-clarification evidence

**Files:**
- Modify: `benchmark_v3/run_evaluation.py`
- Modify: `benchmark_v3/validate_results.py`
- Test: `tests/benchmark_v3/test_runner.py`
- Test: `tests/benchmark_v3/test_results_validation.py`

**Interfaces:**
- Produces each query record's `terminal` and `best_preclarification_result`.

- [ ] **Step 1: Write failing terminal-classification tests**

```python
def test_unnecessary_clarification_is_not_generic_query_failure(self) -> None:
    record = run_control_with_unmatched_clarification()
    self.assertEqual(
        "unnecessary_clarification",
        record["terminal"]["category"],
    )
    self.assertEqual("accepted", record["best_preclarification_result"]["state"])

def test_one_of_three_successes_is_candidate_quorum_failure(self) -> None:
    record = run_with_one_successful_candidate()
    self.assertEqual("candidate_quorum_failure", record["terminal"]["category"])
    self.assertEqual(1, record["terminal"]["successful_candidates"])
```

Cover every approved terminal category.

- [ ] **Step 2: Run runner tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_results_validation
```

Expected: missing `terminal` evidence.

- [ ] **Step 3: Add terminal classification**

Implement a pure function:

```python
def classify_terminal_outcome(
    case: EvaluationCase,
    result: QueryResult | None,
    turns: Sequence[ClarificationTurn],
    candidates: Sequence[QueryCandidate],
    candidate_results: Sequence[QueryResult],
) -> dict[str, Any]:
```

Use the approved categories and include counts for generated, executed, and
successful candidates. Store messages only after sanitizing them for report
use.

- [ ] **Step 4: Preserve best executed pre-clarification result**

Select the last accepted result from iteration 1 before any clarification and
serialize it with the same state/sql/columns/rows contract as `result`.
This is diagnostic only and never replaces the end-to-end final result.

- [ ] **Step 5: Validate evidence shape**

Require a known terminal category for every query record. Require
`best_preclarification_result` to be either `null` or a complete serialized
accepted result. Reject `query was not accepted` as a published terminal
category.

- [ ] **Step 6: Run runner and validator tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_results_validation
```

Expected: all tests pass.

- [ ] **Step 7: Commit terminal evidence**

```powershell
git add benchmark_v3/run_evaluation.py benchmark_v3/validate_results.py tests/benchmark_v3/test_runner.py tests/benchmark_v3/test_results_validation.py
git commit -m "feat: classify v3 workflow outcomes"
```

---

### Task 6: Separate clarification plausibility from target coverage

**Files:**
- Modify: `benchmark_v3/scoring.py`
- Modify: `benchmark_v3/aggregate_results.py`
- Modify: `benchmark_v3/validate_results.py`
- Test: `tests/benchmark_v3/test_scoring.py`
- Test: `tests/benchmark_v3/test_aggregation.py`

**Interfaces:**
- Adds ambiguity fields `plausibility`, `target_coverage`, and keeps resolution/compliance/final alignment separate.

- [ ] **Step 1: Write failing clarification tests**

```python
def test_plausible_but_incomplete_year_question_separates_scores(self) -> None:
    evidence = ambiguity_evidence(
        admission_year_case(),
        [born_or_died_turn()],
        compatible=False,
    )
    self.assertTrue(evidence["plausibility"])
    self.assertFalse(evidence["target_coverage"])
    self.assertFalse(evidence["resolution"])

def test_long_title_question_is_not_plausible_for_common_grain(self) -> None:
    evidence = ambiguity_evidence(
        diagnosis_occurrence_case(),
        [long_title_or_short_title_turn()],
        compatible=False,
    )
    self.assertFalse(evidence["plausibility"])
    self.assertFalse(evidence["target_coverage"])
```

- [ ] **Step 2: Run scoring tests and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring
```

Expected: missing ambiguity fields.

- [ ] **Step 3: Implement and aggregate the two submetrics**

Add both to `AMBIGUITY_FUNNEL`. Preserve the approved 40-point ambiguity
component by documenting and testing its revised submetric weights in
`summarize_arm`.

- [ ] **Step 4: Update aggregate validation**

Require distributions for both fields in every arm. Continue paired,
stratified bootstrap recomputation.

- [ ] **Step 5: Run aggregation and validation tests**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_scoring tests.benchmark_v3.test_aggregation tests.benchmark_v3.test_results_validation
```

Expected: all tests pass.

- [ ] **Step 6: Commit clarification scoring**

```powershell
git add benchmark_v3/scoring.py benchmark_v3/aggregate_results.py benchmark_v3/validate_results.py tests/benchmark_v3/test_scoring.py tests/benchmark_v3/test_aggregation.py tests/benchmark_v3/test_results_validation.py
git commit -m "feat: separate clarification plausibility and coverage"
```

---

### Task 7: Add immutable counterfactual rescoring

**Files:**
- Create: `benchmark_v3/rescore_campaign.py`
- Modify: `benchmark_v3/README.md`
- Modify: `docs/EVALUATION_V3_METHOD_CHANGES.md`
- Test: `tests/benchmark_v3/test_campaign.py`

**Interfaces:**
- CLI: `python -m benchmark_v3.rescore_campaign <campaign-dir>`.
- Produces: `counterfactual-rescore.json`; never modifies original checkpoints, run reports, campaign, or aggregate.

- [ ] **Step 1: Write failing immutability test**

```python
def test_rescore_writes_new_artifact_without_mutating_campaign(self) -> None:
    before = snapshot_campaign_files(self.directory)
    output = rescore_campaign(self.directory)
    self.assertEqual("counterfactual-rescore.json", output.name)
    self.assertEqual(before, snapshot_campaign_files(self.directory))
    payload = json.loads(output.read_text(encoding="utf-8"))
    self.assertEqual("dbwhisperer_v3_counterfactual_rescore", payload["report_type"])
    self.assertIn("source_campaign_hash", payload)
    self.assertIn("scorer_version", payload)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign
```

Expected: import failure for `rescore_campaign`.

- [ ] **Step 3: Implement the rescore**

Load saved final results and clarifications, reload current frozen references,
run the current deterministic scorer, aggregate the rescored copies in memory,
and write a new artifact containing source and scorer hashes. Do not claim to
measure new model-facing system behavior.

- [ ] **Step 4: Run the rescore against a test fixture**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_campaign tests.benchmark_v3.test_scoring
```

Expected: all tests pass.

- [ ] **Step 5: Document the distinction**

Document original frozen score versus deterministic counterfactual rescore and
state that only a new live campaign measures the changed semantic detector.

- [ ] **Step 6: Commit and verify**

```powershell
git add benchmark_v3/rescore_campaign.py benchmark_v3/README.md docs/EVALUATION_V3_METHOD_CHANGES.md tests/benchmark_v3/test_campaign.py
git commit -m "feat: add immutable v3 counterfactual rescore"
python -m unittest discover
git diff --check
```

Expected: full suite passes and no whitespace errors are reported.
