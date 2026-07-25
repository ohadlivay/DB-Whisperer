# Structured Semantic Intent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace column-only semantic ambiguity evidence with validated, phrase-aware intent dimensions that prioritize unresolved meaning over labels and presentation.

**Architecture:** The pre-SQL detector emits structured findings containing grounded interpretations. The unified ambiguity judge selects exact interpretation IDs, while the application derives schema pinning from their validated grounding. Initial SQL generation remains blind to semantic findings; only answered clarifications influence later generation.

**Tech Stack:** Python 3, frozen dataclasses, OpenRouter JSON prompts, DuckDB schema metadata, `unittest`.

## Global Constraints

- Preserve the GUI → `ApplicationService` → Querier/Ambiguity component boundaries.
- Initial SQL-generation prompts must not receive pre-SQL semantic findings.
- Clarification options remain exactly two per round, with at most two questions.
- Candidate evidence remains primary only after the natural-language plausibility gate.
- Join-path multiplicity remains retired and must not reappear.
- Unknown tables, columns, operations, dimensions, or interpretation IDs fail closed.
- Do not log or persist API keys.
- Update `docs/AMBIGUITY_DECISION_CHANGES.md` after behavior is implemented.

---

### Task 1: Introduce structured semantic-intent contracts

**Files:**
- Modify: `src/db_whisperer/contracts.py`
- Modify: `src/db_whisperer/ambiguity/__init__.py`
- Test: `tests/ambiguity/test_semantic_column_service.py`

**Interfaces:**
- Produces: `SemanticGrounding`, `SemanticInterpretation`, and the revised `SemanticAmbiguityTerm`.
- Preserves: `SemanticColumnAnalysis.terms` and `.ambiguous` for application compatibility.

- [ ] **Step 1: Write failing contract tests**

```python
from db_whisperer.contracts import (
    SemanticAmbiguityTerm,
    SemanticGrounding,
    SemanticInterpretation,
)

def test_structured_finding_exposes_ranked_grounded_interpretations(self) -> None:
    finding = SemanticAmbiguityTerm(
        term="common",
        dimension="aggregation_grain",
        interpretations=(
            SemanticInterpretation(
                interpretation_id="interpretation_1",
                label="Diagnosis record count",
                meaning="Count every diagnosis record.",
                relevance=1,
                grounding=SemanticGrounding(
                    tables=("diagnoses_icd",),
                    columns=("diagnoses_icd.icd9_code",),
                    operations=("count_rows",),
                    grain="diagnosis_code",
                ),
            ),
            SemanticInterpretation(
                interpretation_id="interpretation_2",
                label="Distinct patient count",
                meaning="Count distinct affected patients.",
                relevance=2,
                grounding=SemanticGrounding(
                    tables=("diagnoses_icd",),
                    columns=(
                        "diagnoses_icd.icd9_code",
                        "diagnoses_icd.subject_id",
                    ),
                    operations=("count_distinct",),
                    grain="diagnosis_code",
                ),
            ),
        ),
    )
    self.assertEqual("aggregation_grain", finding.dimension)
    self.assertEqual(
        ("interpretation_1", "interpretation_2"),
        tuple(item.interpretation_id for item in finding.interpretations),
    )
```

- [ ] **Step 2: Run the new contract test and verify it fails**

Run:

```powershell
python -m unittest tests.ambiguity.test_semantic_column_service
```

Expected: import failure for `SemanticGrounding`.

- [ ] **Step 3: Add the contracts and allowed vocabulary**

```python
SEMANTIC_DIMENSIONS = frozenset({
    "aggregation_grain",
    "measure_definition",
    "temporal_role",
    "entity_scope",
    "episode_scope",
    "filter_scope",
    "column_meaning",
})

SEMANTIC_OPERATIONS = frozenset({
    "count_rows",
    "count_distinct",
    "average",
    "sum",
    "minimum",
    "maximum",
    "filter",
    "group",
    "select",
})

@dataclass(frozen=True)
class SemanticGrounding:
    tables: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    grain: str = ""
    temporal_role: str = ""

@dataclass(frozen=True)
class SemanticInterpretation:
    interpretation_id: str
    label: str
    meaning: str
    grounding: SemanticGrounding
    relevance: int

@dataclass(frozen=True)
class SemanticAmbiguityTerm:
    term: str
    dimension: str
    interpretations: tuple[SemanticInterpretation, ...]
```

Export the new contracts from `src/db_whisperer/ambiguity/__init__.py`.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m unittest tests.ambiguity.test_semantic_column_service
```

Expected: the new contract test passes; old column-only tests fail only where they still construct the retired shape.

- [ ] **Step 5: Commit the contract boundary**

```powershell
git add src/db_whisperer/contracts.py src/db_whisperer/ambiguity/__init__.py tests/ambiguity/test_semantic_column_service.py
git commit -m "refactor: add structured semantic intent contracts"
```

---

### Task 2: Make the pre-SQL detector phrase-aware and validate grounding

**Files:**
- Modify: `src/db_whisperer/ambiguity/semantic_column_prompt_builder.py`
- Modify: `src/db_whisperer/ambiguity/semantic_column_service.py`
- Test: `tests/ambiguity/test_semantic_column_prompt_builder.py`
- Test: `tests/ambiguity/test_semantic_column_service.py`

**Interfaces:**
- Consumes: contracts from Task 1.
- Produces: `SemanticColumnAmbiguityService.analyze(request: SemanticColumnRequest) -> SemanticColumnAnalysis`.
- Assigns stable IDs in relevance order: `interpretation_1`, `interpretation_2`, and so on within each finding.

- [ ] **Step 1: Add failing prompt tests for compositional meaning**

```python
def test_prompt_prioritizes_measure_over_representation(self) -> None:
    prompt = SemanticColumnPromptBuilder().build_term_prompt(common_request())
    self.assertIn("interpret the complete phrase", prompt)
    self.assertIn("aggregation_grain", prompt)
    self.assertIn("record count", prompt)
    self.assertIn("distinct-entity count", prompt)
    self.assertIn("long title versus short title", prompt)
    self.assertIn("must not pre-empt", prompt)

def test_prompt_treats_explicit_modifiers_as_resolved(self) -> None:
    prompt = SemanticColumnPromptBuilder().build_term_prompt(mortality_request())
    self.assertIn("hospital mortality", prompt)
    self.assertIn("already resolves", prompt)
```

- [ ] **Step 2: Add failing parser tests**

```python
def test_common_parses_as_aggregation_grain(self) -> None:
    self.client.response = {
        "findings": [{
            "term": "common",
            "dimension": "aggregation_grain",
            "interpretations": [
                {
                    "label": "Diagnosis record count",
                    "meaning": "Count every diagnosis row.",
                    "relevance": 1,
                    "tables": ["diagnoses_icd"],
                    "columns": ["diagnoses_icd.icd9_code"],
                    "operations": ["count_rows"],
                    "grain": "diagnosis_code",
                    "temporal_role": "",
                },
                {
                    "label": "Distinct patient count",
                    "meaning": "Count unique patients.",
                    "relevance": 2,
                    "tables": ["diagnoses_icd"],
                    "columns": [
                        "diagnoses_icd.icd9_code",
                        "diagnoses_icd.subject_id",
                    ],
                    "operations": ["count_distinct"],
                    "grain": "diagnosis_code",
                    "temporal_role": "",
                },
            ],
        }],
    }
    analysis = self.service.analyze(common_request())
    self.assertTrue(analysis.ambiguous)
    self.assertEqual("aggregation_grain", analysis.terms[0].dimension)

def test_explicit_hospital_modifier_returns_no_finding(self) -> None:
    self.client.response = {"findings": []}
    analysis = self.service.analyze(mortality_request())
    self.assertFalse(analysis.ambiguous)
```

Also cover unknown dimensions, operations, tables, columns, duplicate relevance, fewer than two interpretations, and `resolved_by_context: true`.

- [ ] **Step 3: Replace the detector JSON contract**

Use this exact output shape in `TERM_INSTRUCTIONS`:

```text
{"findings":[{"term":"<exact phrase>","dimension":"<allowed dimension>",
"resolved_by_context":false,"interpretations":[
{"label":"<short option>","meaning":"<complete interpretation>",
"relevance":1,"tables":["<exact table>"],"columns":["<table.column>"],
"operations":["<allowed operation>"],"grain":"<grain or empty>",
"temporal_role":"<role or empty>"}]}]}
```

Add explicit rules:

- read modifiers with their head phrase;
- return no finding when a modifier settles the meaning;
- prefer measure/grain/scope/temporal-role ambiguity over labels;
- group columns that share one real-world role;
- suppress long-title versus short-title unless presentation was requested;
- for `common`, compare record frequency with distinct-entity prevalence.

- [ ] **Step 4: Implement strict parsing**

Add `_parse_findings()` that:

1. validates `dimension` and operation vocabulary;
2. validates exact tables and qualified columns against `SchemaMetadata`;
3. drops findings marked `resolved_by_context`;
4. removes duplicate interpretations by normalized grounding;
5. sorts by integer relevance and assigns stable IDs;
6. retains only findings with at least two valid interpretations; and
7. caps findings at `max_terms`.

Use deterministic interpretation construction:

```python
SemanticInterpretation(
    interpretation_id=f"interpretation_{index}",
    label=label.strip(),
    meaning=meaning.strip(),
    relevance=index,
    grounding=SemanticGrounding(
        tables=tuple(valid_tables),
        columns=tuple(valid_columns),
        operations=tuple(valid_operations),
        grain=grain.strip(),
        temporal_role=temporal_role.strip(),
    ),
)
```

- [ ] **Step 5: Run detector tests**

Run:

```powershell
python -m unittest tests.ambiguity.test_semantic_column_prompt_builder tests.ambiguity.test_semantic_column_service
```

Expected: all tests pass.

- [ ] **Step 6: Commit detector behavior**

```powershell
git add src/db_whisperer/ambiguity/semantic_column_prompt_builder.py src/db_whisperer/ambiguity/semantic_column_service.py tests/ambiguity/test_semantic_column_prompt_builder.py tests/ambiguity/test_semantic_column_service.py
git commit -m "feat: detect structured semantic intent"
```

---

### Task 3: Ground unified-judge choices in interpretation IDs

**Files:**
- Modify: `src/db_whisperer/contracts.py`
- Modify: `src/db_whisperer/ambiguity/prompt_builder.py`
- Modify: `src/db_whisperer/ambiguity/service.py`
- Test: `tests/ambiguity/test_prompt_builder.py`
- Test: `tests/ambiguity/test_service.py`

**Interfaces:**
- Consumes: `SemanticInterpretation.interpretation_id`.
- Produces: `AmbiguityDecision.evidence_interpretations` and the union of validated `evidence_columns`.
- Semantic judge response uses `interpretation_ids`, not arbitrary column pairs.

- [ ] **Step 1: Write failing judge-prompt and parser tests**

```python
def test_semantic_findings_serialize_interpretations_and_priority(self) -> None:
    prompt = AmbiguityPromptBuilder().build(semantic_request())
    self.assertIn("DIMENSION: aggregation_grain", prompt)
    self.assertIn("interpretation_1", prompt)
    self.assertIn("OPERATIONS: count_rows", prompt)
    self.assertIn("higher-level unresolved dimension", prompt)

def test_semantic_clarification_requires_exact_interpretation_ids(self) -> None:
    client = StubClient({
        "status": "clarify",
        "source": "semantic-column",
        "semantic_finding_id": "semantic_1",
        "interpretation_ids": ["interpretation_1", "interpretation_2"],
        "question": "What should common mean?",
        "options": ["Diagnosis records", "Distinct patients"],
        "reason": "Aggregation grain is unresolved.",
    })
    decision = AmbiguityService(client=client).evaluate(semantic_request())
    self.assertEqual(
        ("interpretation_1", "interpretation_2"),
        decision.evidence_interpretations,
    )
```

Add rejection tests for unknown, duplicate, reversed-to-wrong-option, and cross-finding IDs.

- [ ] **Step 2: Extend `AmbiguityDecision`**

Add:

```python
evidence_interpretations: tuple[str, ...] = ()
evidence_dimension: str = ""
```

Keep `evidence_columns` because clarified schema pinning consumes grounded columns.

- [ ] **Step 3: Update unified and semantic-only prompts**

Change the semantic response contract to:

```json
{
  "status": "clarify",
  "source": "semantic-column",
  "semantic_finding_id": "semantic_1",
  "interpretation_ids": ["interpretation_1", "interpretation_2"],
  "question": "What should common mean?",
  "options": ["Diagnosis record count", "Distinct patient count"],
  "reason": "Aggregation grain is unresolved."
}
```

Require options to correspond in order to the IDs. State that explicit
modifiers settle meaning and representation cannot pre-empt measure/grain.

- [ ] **Step 4: Replace column selection with interpretation selection**

Implement:

```python
@staticmethod
def _semantic_interpretations(
    finding: SemanticAmbiguityTerm,
    raw_ids: object,
) -> tuple[SemanticInterpretation, SemanticInterpretation] | None:
    if not isinstance(raw_ids, list) or len(raw_ids) != 2:
        return None
    ids = tuple(str(value).strip() for value in raw_ids)
    if not all(ids) or ids[0] == ids[1]:
        return None
    known = {
        interpretation.interpretation_id: interpretation
        for interpretation in finding.interpretations
    }
    if any(value not in known for value in ids):
        return None
    return known[ids[0]], known[ids[1]]
```

Set `evidence_columns` to the stable union of both interpretations' grounded
columns and append a non-user-visible grounding annotation containing exact
qualified columns to the internal clarification string.

- [ ] **Step 5: Run ambiguity tests**

Run:

```powershell
python -m unittest tests.ambiguity.test_prompt_builder tests.ambiguity.test_service
```

Expected: all tests pass.

- [ ] **Step 6: Commit judge grounding**

```powershell
git add src/db_whisperer/contracts.py src/db_whisperer/ambiguity/prompt_builder.py src/db_whisperer/ambiguity/service.py tests/ambiguity/test_prompt_builder.py tests/ambiguity/test_service.py
git commit -m "feat: ground clarifications in semantic interpretations"
```

---

### Task 4: Update deterministic fallback and clarification schema pinning

**Files:**
- Modify: `src/db_whisperer/ambiguity/semantic_column_service.py`
- Modify: `src/db_whisperer/application/service.py`
- Modify: `src/db_whisperer/querier/schema_linker.py`
- Test: `tests/application/test_service.py`
- Test: `tests/querier/test_schema_linker.py`

**Interfaces:**
- Fallback chooses the highest-priority unresolved finding and its first two interpretations.
- Clarified generation pins all schema-validated tables referenced by the selected two interpretations.

- [ ] **Step 1: Write failing fallback tests**

```python
def test_fallback_prefers_aggregation_over_column_presentation(self) -> None:
    decision = self.service.fallback_decision(
        SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            terms=(presentation_finding(), aggregation_finding()),
        )
    )
    self.assertEqual("aggregation_grain", decision.evidence_dimension)
    self.assertEqual(
        ("interpretation_1", "interpretation_2"),
        decision.evidence_interpretations,
    )
    self.assertIn("Diagnosis record count", decision.options)
```

- [ ] **Step 2: Write failing schema-pinning tests**

```python
def test_structured_grounding_pins_birth_and_admission_tables(self) -> None:
    required = clarification_required_tables(
        (
            'Question: Born or admitted? Selected answer: Admitted '
            '[grounding: "patients.dob", "admissions.admittime"]',
        ),
        schema_with_patients_and_admissions(),
    )
    self.assertEqual({"patients", "admissions"}, required)
```

- [ ] **Step 3: Implement fallback priority**

Use:

```python
DIMENSION_PRIORITY = {
    "measure_definition": 0,
    "aggregation_grain": 1,
    "temporal_role": 2,
    "entity_scope": 3,
    "episode_scope": 4,
    "filter_scope": 5,
    "column_meaning": 6,
}
```

Select with `(priority, relevance, term.casefold())`. Build user-facing options
from interpretation labels, not raw schema column names.

- [ ] **Step 4: Generalize internal grounding extraction**

Rename `_CLARIFICATION_COLUMNS` to `_GROUNDED_COLUMNS` and accept the existing
column annotation plus `[grounding: "table.column", "table.column"]`. Continue
validating every extracted reference against the loaded schema before pinning.

- [ ] **Step 5: Record structured evidence in application events**

Add `evidence_dimension` and `evidence_interpretations` to the
`ambiguity_decision` event. Do not put API keys or hidden model reasoning in
events.

- [ ] **Step 6: Run application and Querier tests**

Run:

```powershell
python -m unittest tests.application.test_service tests.querier.test_schema_linker tests.querier.test_service
```

Expected: all tests pass.

- [ ] **Step 7: Commit fallback and pinning**

```powershell
git add src/db_whisperer/ambiguity/semantic_column_service.py src/db_whisperer/application/service.py src/db_whisperer/querier/schema_linker.py tests/application/test_service.py tests/querier/test_schema_linker.py
git commit -m "feat: pin clarified semantic intent"
```

---

### Task 5: Add observed-failure regression scenarios

**Files:**
- Modify: `tests/ambiguity/test_semantic_column_service.py`
- Modify: `tests/ambiguity/test_service.py`
- Modify: `tests/application/test_service.py`
- Modify: `src/db_whisperer/ambiguity/README.md`
- Modify: `src/db_whisperer/application/README.md`
- Modify: `docs/AMBIGUITY_DECISION_CHANGES.md`

**Interfaces:**
- Verifies the production behavior defined by Tasks 1–4.

- [ ] **Step 1: Add end-to-end mocked detector/judge regressions**

Cover these exact requests and outcomes:

```python
SCENARIOS = (
    (
        "Show me patients from the year 2112.",
        ("Born in 2112", "Admitted in 2112"),
        "temporal_role",
    ),
    (
        "How common is each diagnosis?",
        ("Diagnosis record count", "Distinct patient count"),
        "aggregation_grain",
    ),
)
```

Include death columns in the year schema and long/short title columns in the
diagnosis schema.

- [ ] **Step 2: Add suppression regressions**

```python
UNAMBIGUOUS = (
    "Show hospital mortality rate by first ICU care unit for ICU stays with an admission.",
    "Count every diagnosis record for each diagnosis code.",
    "Count distinct patients for each diagnosis code.",
    "How long was each ICU stay for patient 10006?",
    "Show patients admitted to the hospital in the year 2112.",
)
```

Assert that semantic analysis produces no actionable conflicting finding or
that the unified judge passes when executed candidates agree.

- [ ] **Step 3: Run all production tests**

Run:

```powershell
python -m unittest discover
```

Expected: all tests pass, with only the existing intentional skip.

- [ ] **Step 4: Update component and decision documentation**

Document:

- structured dimensions and grounding;
- explicit-modifier suppression;
- priority over labels;
- birth/admission coverage with death columns present; and
- unchanged first-round SQL prompt isolation.

- [ ] **Step 5: Commit production behavior**

```powershell
git add tests/ambiguity tests/application src/db_whisperer/ambiguity/README.md src/db_whisperer/application/README.md docs/AMBIGUITY_DECISION_CHANGES.md
git commit -m "test: cover semantic intent regressions"
```

- [ ] **Step 6: Final verification**

Run:

```powershell
python -m unittest discover
git diff --check
```

Expected: test suite passes and `git diff --check` emits no errors.
