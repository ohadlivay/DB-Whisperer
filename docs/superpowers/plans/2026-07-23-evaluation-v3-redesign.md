# Evaluation V3 Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the minimal Evaluation V3 scaffold with a broad, deterministic, resumable four-arm campaign that matches DB Whisperer's hybrid ambiguity funnel and publishes two populated HTML reports.

**Architecture:** Keep `benchmark_v2` frozen and implement the mature campaign entirely under `benchmark_v3`. Separate suite contracts, SQL analysis/scoring, observation, execution, aggregation, and presentation so each boundary is independently testable. Reuse production DB Whisperer services as the system under evaluation, while benchmark-owned code handles deterministic oracles, checkpoints, progress, and reports.

**Tech Stack:** Python 3, `unittest`, DuckDB, sqlglot, Requests, Streamlit production components, standard-library concurrency and HTML generation.

## Global Constraints

- Official arms are exactly `baseline`, `candidate_only`, `semantic_only`, and `full`.
- Baseline uses one SQL candidate; ambiguity-enabled arms use `K=3`.
- Official campaigns contain five repetitions and stop before exceeding `$3.75`.
- Default outer concurrency is two cells; each cell owns its services and HTTP transport.
- Join-path multiplicity is forbidden as an ambiguity arm, category, mechanism, or case contract.
- Primary MIMIC ingestion and deterministic reference execution happen once per campaign.
- Generated model outputs and ambiguity decisions are never cached across repetitions or arms.
- Only five compatible complete repetitions may be aggregated or published.
- Public HTML outputs are exactly `docs/evaluation_method_one_page.html` and `docs/evaluation_report.html`.
- Preserve unrelated working-tree changes and do not modify historical V2 behavior.

---

### Task 1: Freeze the V3 coverage matrix and case contracts

**Files:**
- Modify: `benchmark_v3/contracts.py`
- Replace content: `benchmark_v3/cases/evaluation_cases.json`
- Modify: `benchmark_v3/validate_suite.py`
- Create: `tests/benchmark_v3/test_contracts.py`
- Modify: `tests/benchmark_v3/test_v3.py`

**Interfaces:**
- Produces: `EvaluationCase`, `ReferenceContract`, `EvaluationSuite`, `load_suite(path)`, `validate_suite_shape(suite)`, and `validate_reference_suite(suite, schema, query)`.
- `EvaluationCase` exposes `capabilities`, `required_tables`, `forbidden_tables`, `required_column_groups`, `comparison_mode`, `expected_sql`, and ambiguity signatures.
- Later tasks consume the frozen suite hash and parsed reference join count.

- [ ] **Step 1: Write failing contract and coverage tests**

```python
class EvaluationV3ContractTest(unittest.TestCase):
    def test_official_suite_has_broad_v3_coverage(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        self.assertEqual(24, len(suite.cases))
        self.assertEqual(22, len(suite.query_cases))
        self.assertEqual(2, len(suite.etl_cases))
        self.assertEqual(3, suite.candidate_count)
        self.assertEqual(5, suite.repetitions)
        self.assertEqual(3.75, suite.budget_usd)

        capabilities = {tag for case in suite.query_cases for tag in case.capabilities}
        self.assertTrue({
            "scalar", "grouping", "ordering", "dictionary_join",
            "multi_table_filter", "date_arithmetic", "null_handling",
            "distinct", "having", "ranking", "top_n",
            "write_safety", "multi_statement_safety",
            "external_scan_safety", "missing_schema",
        } <= capabilities)

    def test_suite_contains_three_paired_ambiguity_families_and_two_controls_each(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        ambiguous = [case for case in suite.query_cases if case.should_clarify]
        families = {case.family_id for case in ambiguous}
        self.assertEqual(3, len(families))
        for family in families:
            paired = [case for case in ambiguous if case.family_id == family]
            controls = [
                case for case in suite.query_cases
                if case.family_id == family and case.category == "control"
            ]
            self.assertEqual(2, len(paired))
            self.assertEqual(1, len({case.question for case in paired}))
            self.assertEqual(2, len({case.intent_id for case in paired}))
            self.assertEqual(2, len(controls))

    def test_retired_join_path_contracts_are_forbidden(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        serialized = DEFAULT_SUITE.read_text(encoding="utf-8").casefold()
        self.assertNotIn('"join_path"', serialized)
        self.assertNotIn('"join-path"', serialized)
        self.assertFalse(any(case.id.startswith("jp_") for case in suite.cases))
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```powershell
python -m unittest tests.benchmark_v3.test_contracts -v
```

Expected: failures because V3 still has 18 cases, `K=2`, and lacks the expanded contract fields.

- [ ] **Step 3: Implement the contracts and 24-case JSON suite**

Add the immutable reference contract:

```python
@dataclass(frozen=True)
class ReferenceContract:
    comparison_mode: str
    required_filters: tuple[str, ...] = ()
    required_grouping: tuple[str, ...] = ()
    ordered: bool = False
    limit: int | None = None


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    family_id: str
    kind: str
    category: str
    capabilities: tuple[str, ...] = ()
    question: str = ""
    ambiguous: bool = False
    should_clarify: bool = False
    expected_mechanism: str = "none"
    intent_id: str = ""
    option_token_groups: tuple[tuple[str, ...], ...] = ()
    required_tables: tuple[str, ...] = ()
    forbidden_tables: tuple[str, ...] = ()
    required_column_groups: tuple[tuple[str, ...], ...] = ()
    expected_sql: str | None = None
    reference: ReferenceContract | None = None
    fixture_files: tuple[Path, ...] = ()
    manifest: dict[str, Any] | None = None
```

Populate these exact case groups:

```text
Ambiguity + controls (12):
  from_2024_birth, from_2024_admission,
  ctl_from_2024_birth, ctl_from_2024_admission
  stay_hospital, stay_icu,
  ctl_stay_hospital, ctl_stay_icu
  diagnoses_occurrences, diagnoses_distinct_patients,
  ctl_diagnoses_occurrences, ctl_diagnoses_distinct_patients

General text-to-SQL (6):
  count_admissions
  admissions_by_type
  lab_frequency_with_labels
  icu_mortality_by_first_careunit
  admission_duration_null_safe
  patients_with_multiple_admissions_ranked

Safety/graceful failure (4):
  safe_delete
  safe_multi_statement_ddl
  safe_external_scan
  missing_clinical_concept

ETL (2):
  etl_single
  etl_relational
```

Use executable MIMIC reference SQL for every non-safety query. Give both
interpretations in each family identical initial wording and provide explicit
controls for both meanings.

- [ ] **Step 4: Implement shape and offline reference validation**

```python
ALLOWED_MECHANISMS = {"none", "candidate-comparison", "semantic-column"}
REQUIRED_CAPABILITIES = {
    "scalar", "grouping", "ordering", "dictionary_join",
    "multi_table_filter", "date_arithmetic", "null_handling",
    "distinct", "having", "ranking", "top_n",
    "write_safety", "multi_statement_safety",
    "external_scan_safety", "missing_schema",
}

def validate_reference_suite(
    suite: EvaluationSuite,
    schema: SchemaMetadata,
    query: QueryService,
) -> dict[str, dict[str, Any]]:
    """Execute reference SQL once and return serialized result/join evidence."""
```

Reject missing capability coverage, invalid mechanisms, unpaired intentions,
missing controls, non-executable references, unsafe references, missing
fixtures, and any `join_path`/`join-path`/`jp_` contract value.

- [ ] **Step 5: Run contract and existing V3 tests**

```powershell
python -m unittest tests.benchmark_v3.test_contracts tests.benchmark_v3.test_v3 -v
```

Expected: all contract tests pass.

- [ ] **Step 6: Commit the frozen suite**

```powershell
git add benchmark_v3/contracts.py benchmark_v3/cases/evaluation_cases.json benchmark_v3/validate_suite.py tests/benchmark_v3/test_contracts.py tests/benchmark_v3/test_v3.py
git commit -m "test: redesign evaluation v3 suite"
```

---

### Task 2: Implement deterministic SQL analysis and component scoring

**Files:**
- Create: `benchmark_v3/sql_analysis.py`
- Replace content: `benchmark_v3/scoring.py`
- Create: `tests/benchmark_v3/test_sql_analysis.py`
- Create: `tests/benchmark_v3/test_scoring.py`

**Interfaces:**
- Produces: `SQLAnalysis`, `analyze_sql`, `results_compatible`, `score_query_case`, `score_etl_manifest`, and `summarize_arm`.
- Consumes Task 1's `EvaluationCase.reference` and schema contracts.
- Later aggregation consumes normalized component scores and ambiguity evidence.

- [ ] **Step 1: Write failing parser and correctness tests**

```python
class SQLAnalysisTest(unittest.TestCase):
    def test_counts_joins_inside_ctes_and_subqueries(self) -> None:
        analysis = analyze_sql(
            "WITH x AS (SELECT * FROM a JOIN b ON a.id=b.id) "
            "SELECT * FROM x JOIN c ON x.id=c.id"
        )
        self.assertEqual(2, analysis.join_count)
        self.assertEqual(("a", "b", "c"), analysis.tables)


class ScoringTest(unittest.TestCase):
    def test_efficiency_is_gated_by_correctness(self) -> None:
        score = score_query_case(case, wrong_result, expected, schema, [])
        self.assertEqual(0.0, score["efficiency"])

    def test_zero_join_reference_penalizes_redundant_join(self) -> None:
        score = score_query_case(case, correct_one_join_result, expected, schema, [])
        self.assertEqual(0.5, score["efficiency"])

    def test_fewer_correct_joins_get_full_credit_and_oracle_flag(self) -> None:
        score = score_query_case(case, correct_zero_join_result, expected, schema, [])
        self.assertEqual(1.0, score["efficiency"])
        self.assertTrue(score["oracle_review"])
```

- [ ] **Step 2: Run parser/scoring tests and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_sql_analysis tests.benchmark_v3.test_scoring -v
```

Expected: import failures because `sql_analysis.py` and the mature scoring API do not exist.

- [ ] **Step 3: Implement sqlglot-based logical analysis**

```python
@dataclass(frozen=True)
class SQLAnalysis:
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    aliases: tuple[str, ...]
    join_count: int
    has_order: bool
    limit: int | None


def analyze_sql(sql: str) -> SQLAnalysis:
    tree = sqlglot.parse_one(sql, read="duckdb")
    # Exclude CTE aliases from physical table names, include all nested joins,
    # and preserve first-seen identifier order.
```

- [ ] **Step 4: Implement result-contract comparison**

```python
def results_compatible(
    actual: QueryResult,
    expected: QueryResult,
    case: EvaluationCase,
    analysis: SQLAnalysis,
) -> tuple[bool, str]:
    """Compare scalar, multiset, ordered, top-N, or compatible-subset outputs."""
```

Normalize decimals, floats, dates, aliases, row ordering, and required semantic
concepts. Enforce declared ordering and top-N only when the case requires them.

- [ ] **Step 5: Implement component and composite scoring**

```python
COMPONENT_WEIGHTS = {
    "ambiguity": 40,
    "correctness": 30,
    "efficiency": 10,
    "safety": 10,
    "grounding": 5,
    "etl": 5,
}

def join_efficiency(expected_joins: int, actual_joins: int) -> tuple[float, bool]:
    if actual_joins <= expected_joins:
        return 1.0, actual_joins < expected_joins
    if expected_joins == 0:
        return 1.0 / (actual_joins + 1), False
    return expected_joins / actual_joins, False
```

Store explicit ambiguity funnel fields: expected, asked, detection,
mechanism/source correctness, option match, resolution, compliance, and final
alignment. Macro-average by family in `summarize_arm`.

- [ ] **Step 6: Run scoring tests**

```powershell
python -m unittest tests.benchmark_v3.test_sql_analysis tests.benchmark_v3.test_scoring -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit deterministic scoring**

```powershell
git add benchmark_v3/sql_analysis.py benchmark_v3/scoring.py tests/benchmark_v3/test_sql_analysis.py tests/benchmark_v3/test_scoring.py
git commit -m "feat: add deterministic v3 scoring"
```

---

### Task 3: Add campaign observation, progress, retries, and checkpoints

**Files:**
- Create: `benchmark_v3/observability.py`
- Create: `benchmark_v3/progress.py`
- Create: `tests/benchmark_v3/test_observability.py`
- Create: `tests/benchmark_v3/test_progress.py`

**Interfaces:**
- Produces: `CampaignObserver`, `InstrumentedSession`, `TerminalProgress`, `atomic_json`, and `retry_transient`.
- `CampaignObserver.complete_cell(duration, arm, category, passed)` updates rolling estimates.
- `TerminalProgress.snapshot(status)` formats percentage, elapsed, ETA, active cells, usage, and errors.

- [ ] **Step 1: Write failing observer and ETA tests**

```python
class ProgressTest(unittest.TestCase):
    def test_snapshot_contains_required_progress_fields(self) -> None:
        rendered = TerminalProgress.snapshot({
            "completed_units": 5, "total_units": 20,
            "elapsed_seconds": 125, "eta_seconds": 375,
            "passed": 4, "failed": 1, "model_calls": 18,
            "retries": 2, "cost_usd": 0.42, "budget_usd": 3.75,
            "active": [{"run": 1, "case": "stay_icu", "arm": "full"}],
            "latest_error": "",
        })
        self.assertIn("25.0%", rendered)
        self.assertIn("elapsed 00:02:05", rendered)
        self.assertIn("ETA 00:06:15", rendered)
        self.assertIn("$0.4200/$3.75", rendered)

    def test_eta_uses_arm_and_category_rolling_durations(self) -> None:
        observer.complete_cell(duration=10, arm="baseline", category="control", passed=True)
        observer.complete_cell(duration=40, arm="full", category="ambiguity", passed=True)
        self.assertGreater(observer.status["eta_seconds"], 0)
```

- [ ] **Step 2: Run progress tests and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_observability tests.benchmark_v3.test_progress -v
```

Expected: imports fail because observation and progress modules do not exist.

- [ ] **Step 3: Implement atomic status, events, usage, and checkpoints**

```python
class CampaignObserver:
    def __init__(self, campaign_dir: Path, work_items: tuple[WorkItem, ...], budget_usd: float) -> None:
        self.campaign_dir = campaign_dir
        self.work_items = work_items
        self.budget_usd = budget_usd
        self.status = initial_status(campaign_dir, work_items, budget_usd)
        self._lock = Lock()
        self._durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        self.publish()

    def activate(self, item: WorkItem, phase: str) -> None:
        with self._lock:
            active = dict(self.status["active_by_key"])
            active[item.key] = {
                "run": item.repetition,
                "case": item.case_id,
                "arm": item.arm,
                "phase": phase,
            }
        self.publish(active_by_key=active, active=list(active.values()))

    def complete_cell(
        self, *, duration: float, arm: str, category: str, passed: bool
    ) -> None:
        with self._lock:
            self._durations[(arm, category)].append(duration)
        self.publish(
            completed_units=int(self.status["completed_units"]) + 1,
            passed=int(self.status["passed"]) + int(passed),
            failed=int(self.status["failed"]) + int(not passed),
            eta_seconds=self.estimate_remaining_seconds(),
        )

    def checkpoint(self, key: str, payload: dict[str, Any]) -> Path:
        path = self.campaign_dir / "checkpoints" / f"{key}.json"
        atomic_json(path, payload)
        self.event("checkpoint_written", key=key, checkpoint=str(path))
        return path
```

Keep `status.json`, `events.jsonl`, `console.log`, `prompts.jsonl`, and
`checkpoints/`. Make prompt and event writes thread-safe and never log API keys
or authorization headers.

- [ ] **Step 4: Implement transient retries and budget preflight**

```python
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

def retry_transient(
    operation: Callable[[], Response],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    random_source: random.Random,
) -> Response:
    """Retry connection errors and transient status codes with capped jitter."""
```

Before each paid request, raise `BudgetStop` when recorded cost is at or above
the ceiling. Record retries independently from model-call counts.

- [ ] **Step 5: Implement the background terminal renderer**

```python
class TerminalProgress:
    def __init__(self, observer: CampaignObserver, *, stream: TextIO, interval: float = 1.0) -> None:
        self.observer = observer
        self.stream = stream
        self.interval = interval
        self._stop = Event()
        self._thread = Thread(target=self._render_loop, daemon=True)

    @staticmethod
    def snapshot(status: Mapping[str, Any]) -> str:
        complete = int(status.get("completed_units", 0))
        total = int(status.get("total_units", 0))
        percent = 100.0 * complete / total if total else 0.0
        active = ", ".join(
            f"r{item['run']}:{item['case']}/{item['arm']}"
            for item in status.get("active", [])
        ) or "waiting"
        return (
            f"{percent:5.1f}% {complete}/{total} | "
            f"elapsed {format_duration(status.get('elapsed_seconds'))} | "
            f"ETA {format_duration(status.get('eta_seconds'))} | "
            f"pass {status.get('passed', 0)} fail {status.get('failed', 0)} | "
            f"calls {status.get('model_calls', 0)} retries {status.get('retries', 0)} | "
            f"${float(status.get('cost_usd', 0)):.4f}/"
            f"${float(status.get('budget_usd', 0)):.2f} | {active}"
        )
```

Use carriage-return refresh for interactive streams and newline snapshots for
redirected output. Never block campaign execution.

- [ ] **Step 6: Run observation/progress tests**

```powershell
python -m unittest tests.benchmark_v3.test_observability tests.benchmark_v3.test_progress -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit campaign observation**

```powershell
git add benchmark_v3/observability.py benchmark_v3/progress.py tests/benchmark_v3/test_observability.py tests/benchmark_v3/test_progress.py
git commit -m "feat: add v3 campaign progress and checkpoints"
```

---

### Task 4: Rebuild the four-arm runner with bounded parallel execution

**Files:**
- Replace content: `benchmark_v3/run_evaluation.py`
- Modify: `benchmark_v3/README.md`
- Create: `tests/benchmark_v3/test_runner.py`
- Create: `tests/benchmark_v3/test_campaign.py`

**Interfaces:**
- Produces: `ARMS`, `WorkItem`, `CampaignFingerprint`, `build_services`, `build_schedule`, `run_cell`, `run_campaign`, and `main`.
- Consumes Tasks 1–3 contracts, scoring, and observation.
- Produces five run reports plus `campaign.json` for aggregation.

- [ ] **Step 1: Write failing arm, scheduling, resume, and concurrency tests**

```python
class RunnerTest(unittest.TestCase):
    def test_arm_configuration_matches_v3_funnel_and_k_three(self) -> None:
        _, applications = build_services(observer, candidate_count=3)
        self.assertEqual(("baseline", "candidate_only", "semantic_only", "full"), ARMS)
        self.assertEqual(3, applications["full"].candidates_per_iteration)
        self.assertFalse(applications["candidate_only"].enable_semantic_column_detection)
        self.assertFalse(
            applications["semantic_only"].ambiguity.prompt_builder.include_candidate_evidence
        )
        self.assertTrue(
            applications["full"].ambiguity.prompt_builder.include_relationships
        )

    def test_schedule_is_deterministic_and_counterbalanced(self) -> None:
        first = build_schedule(suite, repetitions=5)
        second = build_schedule(suite, repetitions=5)
        self.assertEqual(first, second)
        self.assertEqual(set(ARMS), {item.arm for item in first})
        self.assertNotEqual(
            [item.arm for item in first if item.repetition == 1][:4],
            [item.arm for item in first if item.repetition == 2][:4],
        )

    def test_completed_compatible_checkpoint_is_not_repeated(self) -> None:
        result = run_campaign(config, fake_services)
        self.assertEqual(0, fake_services.calls_for(checkpointed_item))
        self.assertIn(checkpointed_item.key, result.completed_keys)
```

- [ ] **Step 2: Run runner tests and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_campaign -v
```

Expected: failures because the current runner is sequential, non-resumable, and uses `K=2`.

- [ ] **Step 3: Implement immutable work items and compatibility fingerprints**

```python
@dataclass(frozen=True)
class WorkItem:
    repetition: int
    case_id: str
    family_id: str
    category: str
    arm: str

    @property
    def key(self) -> str:
        return f"run-{self.repetition:02d}-{self.case_id}-{self.arm}"


@dataclass(frozen=True)
class CampaignFingerprint:
    suite_hash: str
    dataset_hash: str
    model: str
    prompt_hash: str
    scorer_version: str
    candidate_count: int
    arms: tuple[str, ...]
    runtime_hash: str
```

- [ ] **Step 4: Implement per-worker service construction**

```python
def build_services(
    observer: CampaignObserver,
    candidate_count: int,
) -> tuple[QueryService, dict[str, ApplicationService]]:
    """Build isolated clients for candidate-only, semantic-only, and full."""
```

Use `InstrumentedSession`, benchmark-owned prompt logging, and exact prompt
builder flags from the approved design. Do not share mutable QueryService or
ApplicationService instances between outer workers.

- [ ] **Step 5: Implement one-time ingestion and reference cache**

```python
@dataclass(frozen=True)
class CampaignDataset:
    schema: SchemaMetadata
    dataset_hash: str
    references: Mapping[str, QueryResult]
    reference_joins: Mapping[str, int]
```

Ingest the primary dataset once, preserve relationship-discovery warnings,
validate the suite, execute each reference once, and write a fingerprinted
reference artifact under the campaign directory.

- [ ] **Step 6: Implement clarification transcripts and compliance evidence**

```python
@dataclass(frozen=True)
class ClarificationTurn:
    iteration: int
    mechanism: str
    question: str
    options: tuple[str, str]
    chosen_index: int
    chosen: str
    matched_intent: bool
    candidate_support: tuple[tuple[str, int], ...]
    candidate_rejection_reason: str
    fallback_used: bool
    compliance_passed: bool | None
    compliant_alternatives: tuple[str, ...]
```

Use deterministic token-group matching. Record at most two questions, the
compliance retry outcome, and fail-closed termination without inventing a
result.

- [ ] **Step 7: Implement deterministic scheduling and two-worker execution**

```python
def build_schedule(suite: EvaluationSuite, repetitions: int) -> tuple[WorkItem, ...]:
    """Fixed-seed case shuffle plus Latin-square arm rotation."""

def run_campaign(config: CampaignConfig) -> CampaignResult:
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        # Submit at most `workers` cells, checkpoint each completion, and stop
        # scheduling new paid work after BudgetStop.
```

Default `--workers 2`; permit `--workers 1`. Continue after ordinary cell
failures. Stop on dataset, contract, fingerprint, budget, or aggregation
failures. Resume only matching checkpoints.

- [ ] **Step 8: Run runner and mocked campaign tests**

```powershell
python -m unittest tests.benchmark_v3.test_runner tests.benchmark_v3.test_campaign -v
```

Expected: all tests pass and the mocked campaign reaches 100% without network calls.

- [ ] **Step 9: Commit the V3 runner**

```powershell
git add benchmark_v3/run_evaluation.py benchmark_v3/README.md tests/benchmark_v3/test_runner.py tests/benchmark_v3/test_campaign.py
git commit -m "feat: rebuild evaluation v3 campaign runner"
```

---

### Task 5: Implement five-run aggregation and report data modeling

**Files:**
- Replace content: `benchmark_v3/aggregate_results.py`
- Create: `benchmark_v3/report_model.py`
- Create: `benchmark_v3/validate_results.py`
- Create: `tests/benchmark_v3/test_aggregation.py`
- Create: `tests/benchmark_v3/test_results_validation.py`

**Interfaces:**
- Produces: `aggregate_campaign(campaign_dir)`, `build_report_model(aggregate)`, and `validate_aggregate(payload)`.
- Report model contains all values required by both HTML renderers; renderers perform no score calculations.

- [ ] **Step 1: Write failing compatibility and family-macro tests**

```python
class AggregationTest(unittest.TestCase):
    def test_requires_five_complete_compatible_repetitions(self) -> None:
        with self.assertRaisesRegex(ValueError, "five complete"):
            aggregate_campaign(campaign_with_four_reports)

    def test_macro_averages_ambiguity_by_family(self) -> None:
        aggregate = aggregate_campaign(campaign_with_unequal_family_rows)
        self.assertEqual(
            50.0,
            aggregate["arms"]["full"]["ambiguity_metrics"]["recall"]["mean"],
        )

    def test_includes_failures_oracle_flags_and_operational_metrics(self) -> None:
        aggregate = aggregate_campaign(complete_campaign)
        self.assertIn("failures", aggregate)
        self.assertIn("oracle_reviews", aggregate)
        self.assertIn("usage", aggregate)
```

- [ ] **Step 2: Run aggregation tests and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_aggregation tests.benchmark_v3.test_results_validation -v
```

Expected: failures because the current aggregator accepts arbitrary report lists and only counts passes.

- [ ] **Step 3: Implement compatibility validation**

```python
COMPATIBILITY_FIELDS = (
    "suite_version", "suite_hash", "dataset_hash", "model", "prompt_hash",
    "scorer_version", "candidate_count", "arms", "runtime_hash",
)

def validate_aggregate(payload: Mapping[str, Any]) -> None:
    """Reject incomplete, mismatched, missing, or non-finite report data."""
```

Require five repetitions, every expected work item, four exact arms, finite
metrics, raw failure retention, and no unresolved incomplete state.

- [ ] **Step 4: Implement aggregation distributions and bootstrap intervals**

```python
def bootstrap_ci(values: Sequence[float], *, samples: int = 2000) -> tuple[float, float]:
    rng = random.Random(20260723)
    estimates = sorted(
        mean(rng.choices(tuple(values), k=len(values)))
        for _ in range(samples)
    )
    low = estimates[int(samples * 0.025)]
    high = estimates[min(samples - 1, int(samples * 0.975))]
    return round(low, 4), round(high, 4)

def distribution(values: Sequence[float]) -> dict[str, Any]:
    return {
        "mean": round(mean(values), 4),
        "stddev": round(pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "confidence_interval_95": list(bootstrap_ci(values)),
    }
```

Aggregate composite/components, arm deltas, ambiguity funnel, correctness-gated
efficiency, safety, grounding, shared ETL, failures, cost, latency, retries, and
oracle-review flags.

- [ ] **Step 5: Implement a presentation-only report model**

```python
def build_report_model(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    """Return escaped-later labels, charts, tables, findings, cases, and provenance."""
```

Derive all display values in this module. The renderer must only format model
values, ensuring both report types agree.

- [ ] **Step 6: Run aggregation and validation tests**

```powershell
python -m unittest tests.benchmark_v3.test_aggregation tests.benchmark_v3.test_results_validation -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit aggregation**

```powershell
git add benchmark_v3/aggregate_results.py benchmark_v3/report_model.py benchmark_v3/validate_results.py tests/benchmark_v3/test_aggregation.py tests/benchmark_v3/test_results_validation.py
git commit -m "feat: aggregate evaluation v3 campaigns"
```

---

### Task 6: Generate the two populated V3 HTML reports and change log

**Files:**
- Replace content: `benchmark_v3/render_report.py`
- Create: `tests/benchmark_v3/test_reporting.py`
- Create: `docs/EVALUATION_V3_METHOD_CHANGES.md`
- Generated by a complete campaign: `docs/evaluation_method_one_page.html`
- Generated by a complete campaign: `docs/evaluation_report.html`

**Interfaces:**
- Produces: `render_one_page(model)`, `render_full_report(model)`, and `write_reports(aggregate_path, one_page_path, full_report_path)`.
- Consumes Task 5's validated report model.
- Returns exactly the two HTML output paths.

- [ ] **Step 1: Write failing two-report and stale-language tests**

```python
class ReportingTest(unittest.TestCase):
    def test_writes_exactly_two_populated_reports(self) -> None:
        outputs = write_reports(
            aggregate_path,
            temporary / "evaluation_method_one_page.html",
            temporary / "evaluation_report.html",
        )
        self.assertEqual(2, len(outputs))
        one_page, full = (path.read_text(encoding="utf-8") for path in outputs)
        self.assertIn("Candidate Only", one_page)
        self.assertIn("Semantic Only", one_page)
        self.assertIn("Full System", one_page)
        self.assertIn("Ambiguity funnel", full)
        self.assertIn("Clarification compliance", full)

    def test_reports_have_no_obsolete_v2_arm_claims(self) -> None:
        combined = render_one_page(model) + render_full_report(model)
        self.assertNotIn("Join Only", combined)
        self.assertNotIn("five configurations", combined)
        self.assertNotIn("join-path ambiguity", combined.casefold())

    def test_model_derived_html_is_escaped(self) -> None:
        model["cases"][0]["question"] = "<script>alert(1)</script>"
        rendered = render_full_report(model)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
```

- [ ] **Step 2: Run reporting tests and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_reporting -v
```

Expected: failures because the V3 renderer currently emits one compact table.

- [ ] **Step 3: Implement the one-page report**

Preserve the visual structure of `docs/evaluation_method_one_page.html`:

```python
def render_one_page(model: Mapping[str, Any]) -> str:
    """Render method, four arms, headline results, findings, and limitations."""
```

Replace all hard-coded V2 values with report-model fields. Describe the four
arms, `K=3`, five repetitions, 24 cases, deterministic scoring, and current
aggregate results.

- [ ] **Step 4: Implement the full report with embedded case evidence**

Preserve the visual language and tab structure of `docs/evaluation_report.html`:

```python
REPORT_TABS = (
    ("overview", "Overview"),
    ("comparison", "System Comparison"),
    ("questions", "Results by Question"),
    ("quality", "Quality Components"),
    ("ambiguity", "Ambiguity Funnel"),
    ("operations", "Safety, ETL & Operations"),
    ("methodology", "Methodology"),
    ("evidence", "Case Evidence"),
)
```

Embed generated SQL, expected SQL, results, scores, clarification transcripts,
candidate support, compliance, failures, and provenance in the full report.
Do not generate `evaluation_report_cases.html`.

- [ ] **Step 5: Write the V2-to-V3 method change documentation**

Create `docs/EVALUATION_V3_METHOD_CHANGES.md` with these sections:

```markdown
# Evaluation V3 Method Changes
## Why V2 no longer matches DB Whisperer
## Experimental arms
## Test-suite redesign
## Ambiguity-funnel scoring
## Correctness and least-sufficient joins
## K=3, five repetitions, and budget control
## Faster campaign execution
## Progress, checkpoints, and resume
## Aggregation and publication
## Interpretation limits
```

Document the decisions and rationale from
`docs/superpowers/specs/2026-07-23-evaluation-v3-redesign-design.md`.

- [ ] **Step 6: Run report tests**

```powershell
python -m unittest tests.benchmark_v3.test_reporting -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit report generation and method documentation**

```powershell
git add benchmark_v3/render_report.py tests/benchmark_v3/test_reporting.py docs/EVALUATION_V3_METHOD_CHANGES.md
git commit -m "feat: publish two evaluation v3 reports"
```

---

### Task 7: Wire automatic publication, verify offline, and run the official campaign

**Files:**
- Modify: `benchmark_v3/run_evaluation.py`
- Modify: `benchmark_v3/README.md`
- Modify: `tests/benchmark_v3/test_campaign.py`
- Generated after successful live run: `docs/evaluation_method_one_page.html`
- Generated after successful live run: `docs/evaluation_report.html`

**Interfaces:**
- A successful `python -m benchmark_v3.run_evaluation` writes the five run reports, aggregate JSON, and both public HTML files.
- Incomplete or budget-stopped campaigns write no public HTML.

- [ ] **Step 1: Write the failing publication-gate test**

```python
def test_only_complete_five_run_campaign_publishes_reports(self) -> None:
    incomplete = run_campaign(incomplete_config, fake_services)
    self.assertFalse(incomplete.one_page_report.exists())
    self.assertFalse(incomplete.full_report.exists())

    complete = run_campaign(complete_config, fake_services)
    self.assertTrue(complete.one_page_report.exists())
    self.assertTrue(complete.full_report.exists())
```

- [ ] **Step 2: Run the publication-gate test and confirm RED**

```powershell
python -m unittest tests.benchmark_v3.test_campaign -v
```

Expected: the new publication-gate test fails because runner publication is not wired.

- [ ] **Step 3: Wire post-campaign aggregation, validation, and two-report publication**

```python
if campaign_complete:
    aggregate = aggregate_campaign(campaign_dir)
    validate_aggregate(aggregate)
    atomic_json(campaign_dir / "aggregate.json", aggregate)
    write_reports(
        campaign_dir / "aggregate.json",
        PROJECT_ROOT / "docs" / "evaluation_method_one_page.html",
        PROJECT_ROOT / "docs" / "evaluation_report.html",
    )
```

Any aggregation or rendering error marks the campaign incomplete and preserves
raw/checkpoint evidence without publishing partial HTML.

- [ ] **Step 4: Run all V3 tests and offline validation**

```powershell
python -m unittest discover -s tests/benchmark_v3 -v
python -m benchmark_v3.validate_suite
```

Expected: all V3 tests pass and suite validation reports 24 valid cases, four
arms, `K=3`, and no join-path contracts.

- [ ] **Step 5: Run the entire repository test suite**

```powershell
python -m unittest discover
```

Expected: zero failures and zero errors.

- [ ] **Step 6: Run a one-repetition non-publishing smoke campaign**

```powershell
python -m benchmark_v3.run_evaluation --repetitions 1 --workers 1 --campaign-id v3-smoke
```

Expected: progress displays percentage, elapsed time, ETA, and cost; the
campaign creates resumable artifacts but does not overwrite public reports.

- [ ] **Step 7: Run the official five-repetition campaign**

```powershell
python -m benchmark_v3.run_evaluation --workers 2 --campaign-id v3-official-20260723
```

Expected: campaign completes below `$3.75`, writes a validated aggregate, and
updates exactly the two public HTML report types. If the budget stops the run,
resume the same campaign ID after allowance is available; do not publish an
incomplete aggregate.

- [ ] **Step 8: Validate generated results against the aggregate**

```powershell
python -m benchmark_v3.validate_results benchmark_v3/results/runs/v3-official-20260723/aggregate.json
python -m unittest tests.benchmark_v3.test_reporting -v
```

Expected: aggregate validation passes and every headline/report value is
traceable to aggregate fields.

- [ ] **Step 9: Visually inspect both reports**

Open both generated HTML files in the in-app browser. Verify desktop and narrow
viewport layouts, tabs, charts, tables, embedded evidence, focus behavior,
overflow, contrast, and absence of stale V2/join-path copy.

- [ ] **Step 10: Run final verification immediately before completion**

```powershell
python -m unittest discover
git diff --check
git status --short
```

Expected: all tests pass, no whitespace errors, and only intentional V3,
documentation, and generated report changes remain in addition to the user's
pre-existing working-tree changes.

- [ ] **Step 11: Commit the verified implementation and populated reports**

```powershell
git add benchmark_v3 tests/benchmark_v3 docs/EVALUATION_V3_METHOD_CHANGES.md docs/evaluation_method_one_page.html docs/evaluation_report.html
git commit -m "feat: update evaluation for hybrid ambiguity funnel"
```
