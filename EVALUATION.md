# DBWhisperer Evaluation Method

This document defines the evaluation method for the current DBWhisperer system: a Streamlit/Python application that loads CSV files into DuckDB, generates DuckDB SQL through OpenRouter, validates read-only SQL, executes the query, and uses an explicit ambiguity layer before or after SQL generation.

The evaluation is a benchmark suite over the bundled MIMIC-III clinical demo database. Its purpose is to compare two architectures:

- **Baseline:** single-pass `QueryService`, which goes directly from the user question to SQL.
- **Full pipeline:** `ApplicationService`, with join-path ambiguity detection, semantic-column ambiguity detection, candidate SQL generation/execution, and candidate-comparison ambiguity judging.

The research question is whether the full pipeline improves interpretive accuracy and user trust on realistic clinical database questions, especially when the MIMIC schema supports multiple valid interpretations.

---

## Current Architecture Under Evaluation

DBWhisperer does not use the older LangGraph HTTP flow described in previous evaluation drafts. The benchmark should call the Python services directly.

The expected implementation shape is:

1. Load the MIMIC-III demo CSV directory through the ETL component.
2. Reuse the returned `SchemaMetadata`, including discovered relationships.
3. For each test case, run the baseline arm through `QueryService`.
4. Run the full arm through `ApplicationService`.
5. If the full arm returns `ComponentState.PENDING`, answer the clarification with the case's declared simulated-user answer and continue the workflow.
6. Execute each case's gold SQL against the same DuckDB database.
7. Compare generated results with the gold result.
8. Record generated SQL, clarification behavior, result match, failures, and scoring notes.

The GUI is useful for manual inspection, but the main evaluation should be a reproducible Python harness. The existing `benchmark/ab_run.py` is the closest current pattern, though its cases must move from BikeStores to MIMIC-III.

---

## Required Reference Schema

All evaluation cases must use the bundled MIMIC-III clinical demo dataset:

```text
data/mimic-iii-clinical-database-demo-1.4-20260615T211207Z-3-001/
  mimic-iii-clinical-database-demo-1.4/
```

The benchmark should load the CSV files from that directory into DuckDB using DBWhisperer's ETL layer. The canonical schema is the one discovered from these CSV files, not the old toy ecommerce schema.

Important tables and columns used by the cases include:

| Area | Tables / columns |
| --- | --- |
| Patient identity | `PATIENTS(SUBJECT_ID, GENDER, DOB, DOD, DOD_HOSP, DOD_SSN, EXPIRE_FLAG)` |
| Hospital admissions | `ADMISSIONS(SUBJECT_ID, HADM_ID, ADMITTIME, DISCHTIME, DEATHTIME, ADMISSION_TYPE, DIAGNOSIS)` |
| ICU stays | `ICUSTAYS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, FIRST_CAREUNIT, LAST_CAREUNIT, INTIME, OUTTIME, LOS)` |
| Labs | `LABEVENTS(SUBJECT_ID, HADM_ID, ITEMID, CHARTTIME, VALUE, VALUENUM, VALUEUOM, FLAG)` and `D_LABITEMS(ITEMID, LABEL, FLUID, CATEGORY)` |
| Charted ICU events | `CHARTEVENTS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, ITEMID, CHARTTIME, VALUE, VALUENUM, VALUEUOM)` and `D_ITEMS(ITEMID, LABEL)` |
| Medications | `PRESCRIPTIONS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, STARTDATE, ENDDATE, DRUG, DRUG_NAME_GENERIC, ROUTE)` |
| Diagnoses | `DIAGNOSES_ICD(SUBJECT_ID, HADM_ID, ICD9_CODE)` and `D_ICD_DIAGNOSES(ICD9_CODE, SHORT_TITLE, LONG_TITLE)` |
| Procedures | `PROCEDURES_ICD(SUBJECT_ID, HADM_ID, ICD9_CODE)` and `D_ICD_PROCEDURES(ICD9_CODE, SHORT_TITLE, LONG_TITLE)` |
| Transfers/services | `TRANSFERS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, EVENTTYPE, INTIME, OUTTIME, LOS)`, `SERVICES(SUBJECT_ID, HADM_ID, TRANSFERTIME, CURR_SERVICE)` |

Identifier spelling and quoting must follow the tables loaded by ETL. Gold SQL below uses quoted uppercase identifiers because that is the source CSV/schema style.

---

## Evaluation Factors

| Factor | Description |
| --- | --- |
| SQL execution correctness | Does the generated SQL return the same table as the gold SQL on the same DuckDB database? |
| Schema grounding | Does the generated SQL use real MIMIC tables and columns, with exact identifiers? |
| Join-path ambiguity detection | Does the full pipeline ask for clarification when multiple relationship paths support distinct interpretations? |
| Semantic-column ambiguity detection | Does the full pipeline ask when a vague term maps to multiple same-type columns, such as admission time vs discharge time vs ICU time? |
| Clarification quality | Is the question targeted, two-option, and tied to the actual schema ambiguity? |
| Clarification resolution | After the simulated user answers, does the final SQL reflect that answer? |
| Silent-assumption rate | How often does the baseline choose one valid interpretation without surfacing the ambiguity? |
| Result faithfulness | Does any natural-language response accurately summarize the returned table without adding unsupported clinical claims? |
| Safety | Does SQL validation block writes and external file/network access? |
| Graceful failure | If a query is underspecified or references a nonexistent concept, does the system fail usefully instead of hallucinating? |

---

## Scoring Responsibilities

The initial evaluation should be fully simulated. There should be no required
human evaluator in the run loop.

The primary scores should be assigned by the deterministic benchmark harness:

- SQL execution success or failure.
- Result equality against gold SQL.
- Whether a clarification was asked.
- Which ambiguity mechanism asked it.
- Whether generated SQL was read-only.
- Whether generated SQL referenced valid schema identifiers.

The secondary qualitative scores should be assigned by an LLM judge. For the
first implementation, the judge can be the same Gemma model used by
DBWhisperer for SQL generation. This is a self-judging setup, so its outputs
must be labeled as non-independent and treated as supporting analysis rather
than the headline correctness metric.

The self-judge may score:

- Clarification question quality.
- Whether a silent baseline assumption was clinically reasonable.
- Natural-language response faithfulness.
- Trust/usefulness notes for qualitative reporting.

The benchmark should keep the judge model configurable so a later evaluation
run can swap Gemma for an independent judge model without changing the case
suite or deterministic scoring logic.

The simulated user is encoded in each ambiguous test case. It is not an LLM judge. It simply chooses the option corresponding to the case's declared intended interpretation.

---

## Test Case Format

Each case should eventually be represented in machine-readable benchmark JSON with at least:

- `id`
- `question`
- `ambiguous`
- `ambiguity_type`: `join-path`, `semantic-column`, `underspecified`, or `none`
- `intent`
- `schema_elements`
- `expected_sql`
- `should_clarify`
- `simulated_user_answer` when `should_clarify` is true
- `expected_behavior`
- `tests`

Gold SQL should be written for DuckDB and should return deterministic, bounded results. Prefer explicit `ORDER BY` and `LIMIT` for row-listing cases.

---

## Category 1 - Straightforward Clinical Queries

These cases test the happy path: clear MIMIC questions with one natural interpretation.

### TC-01: Count admissions

**Query**

> "How many hospital admissions are in the database?"

**Schema elements**

`ADMISSIONS(HADM_ID)`

**Expected SQL**

```sql
SELECT COUNT(*) AS admission_count
FROM "ADMISSIONS";
```

**Expected behavior**

- Baseline and full pipeline both generate a simple count.
- The full pipeline should not ask a clarification.
- Result matches the gold count.

**Tests**

SQL correctness, schema grounding, no spurious clarification.

### TC-02: Admissions by type

**Query**

> "How many admissions are there for each admission type?"

**Schema elements**

`ADMISSIONS(ADMISSION_TYPE, HADM_ID)`

**Expected SQL**

```sql
SELECT "ADMISSION_TYPE", COUNT(*) AS admission_count
FROM "ADMISSIONS"
GROUP BY "ADMISSION_TYPE"
ORDER BY admission_count DESC, "ADMISSION_TYPE";
```

**Expected behavior**

- Uses `ADMISSION_TYPE`, not diagnosis or service.
- Groups and orders deterministically.
- No clarification should be asked.

**Tests**

Aggregation, schema grounding, result correctness.

### TC-03: ICU stays by first care unit

**Query**

> "How many ICU stays started in each care unit?"

**Schema elements**

`ICUSTAYS(FIRST_CAREUNIT, ICUSTAY_ID)`

**Expected SQL**

```sql
SELECT "FIRST_CAREUNIT", COUNT(*) AS icu_stay_count
FROM "ICUSTAYS"
GROUP BY "FIRST_CAREUNIT"
ORDER BY icu_stay_count DESC, "FIRST_CAREUNIT";
```

**Expected behavior**

- Uses `FIRST_CAREUNIT`, not `LAST_CAREUNIT`.
- No clarification should be asked because "started" disambiguates the column.

**Tests**

Column selection, aggregation, no spurious semantic-column clarification.

---

## Category 2 - Join-Path Ambiguity

These cases are intentionally phrased so the MIMIC schema supports more than one valid path between the mentioned entities.

### TC-04: Patient labs by subject-level history

**Query**

> "Show me lab results for patient 10006."

**Schema elements**

`PATIENTS(SUBJECT_ID)`, `ADMISSIONS(SUBJECT_ID, HADM_ID)`, `LABEVENTS(SUBJECT_ID, HADM_ID, ITEMID, CHARTTIME, VALUENUM, VALUEUOM)`, `D_LABITEMS(ITEMID, LABEL)`

**Ambiguity**

Patient-to-lab can be interpreted as direct subject-level history `PATIENTS -> LABEVENTS` or admission-scoped history `PATIENTS -> ADMISSIONS -> LABEVENTS`.

**Intended interpretation**

All lab events recorded for the patient by `SUBJECT_ID`, regardless of admission context.

**Expected SQL**

```sql
SELECT l."CHARTTIME",
       d."LABEL",
       l."VALUE",
       l."VALUENUM",
       l."VALUEUOM"
FROM "LABEVENTS" l
LEFT JOIN "D_LABITEMS" d ON d."ITEMID" = l."ITEMID"
WHERE l."SUBJECT_ID" = 10006
ORDER BY l."CHARTTIME", d."LABEL"
LIMIT 50;
```

**Expected behavior**

- Full pipeline should ask a join-path clarification before SQL generation.
- Simulated user chooses the subject-level interpretation.
- Baseline may silently choose either path; record the assumption.

**Tests**

Join-path detection, clarification resolution, silent-assumption rate.

### TC-05: Patient labs by admission context

**Query**

> "Show me lab results for patient 10006."

**Schema elements**

Same as TC-04.

**Intended interpretation**

Lab events for the patient's hospital admissions, preserving admission context.

**Expected SQL**

```sql
SELECT a."HADM_ID",
       a."ADMITTIME",
       l."CHARTTIME",
       d."LABEL",
       l."VALUE",
       l."VALUENUM",
       l."VALUEUOM"
FROM "PATIENTS" p
JOIN "ADMISSIONS" a ON a."SUBJECT_ID" = p."SUBJECT_ID"
JOIN "LABEVENTS" l ON l."HADM_ID" = a."HADM_ID"
LEFT JOIN "D_LABITEMS" d ON d."ITEMID" = l."ITEMID"
WHERE p."SUBJECT_ID" = 10006
ORDER BY a."HADM_ID", l."CHARTTIME", d."LABEL"
LIMIT 50;
```

**Expected behavior**

- Same user wording as TC-04 on purpose.
- Full pipeline should ask and follow the admission-scoped answer.
- Baseline can satisfy at most one of TC-04 and TC-05 with one blind default.

**Tests**

Join-path ambiguity, A/B comparison, clarification benefit.

### TC-06: Patient chart events by ICU stay context

**Query**

> "Show charted measurements for patient 10006 during ICU stays."

**Schema elements**

`PATIENTS(SUBJECT_ID)`, `ADMISSIONS(SUBJECT_ID, HADM_ID)`, `ICUSTAYS(SUBJECT_ID, HADM_ID, ICUSTAY_ID)`, `CHARTEVENTS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, ITEMID, CHARTTIME, VALUE, VALUENUM)`, `D_ITEMS(ITEMID, LABEL)`

**Intended interpretation**

Measurements attached to an ICU stay through `ICUSTAY_ID`.

**Expected SQL**

```sql
SELECT i."ICUSTAY_ID",
       i."INTIME",
       i."OUTTIME",
       c."CHARTTIME",
       d."LABEL",
       c."VALUE",
       c."VALUENUM",
       c."VALUEUOM"
FROM "ICUSTAYS" i
JOIN "CHARTEVENTS" c ON c."ICUSTAY_ID" = i."ICUSTAY_ID"
LEFT JOIN "D_ITEMS" d ON d."ITEMID" = c."ITEMID"
WHERE i."SUBJECT_ID" = 10006
ORDER BY i."ICUSTAY_ID", c."CHARTTIME", d."LABEL"
LIMIT 50;
```

**Expected behavior**

- The words "during ICU stays" should push the final SQL toward `ICUSTAY_ID`.
- If multiple paths are surfaced, the simulated user chooses ICU-stay context.

**Tests**

Join-path handling, clinical event-table joins, dictionary-table join.

### TC-07: Patient medications by admission context

**Query**

> "What medications are associated with patient 10006?"

**Schema elements**

`PATIENTS(SUBJECT_ID)`, `ADMISSIONS(SUBJECT_ID, HADM_ID)`, `PRESCRIPTIONS(SUBJECT_ID, HADM_ID, ICUSTAY_ID, STARTDATE, ENDDATE, DRUG, DRUG_NAME_GENERIC)`

**Intended interpretation**

Medications associated with the patient's hospital admissions.

**Expected SQL**

```sql
SELECT a."HADM_ID",
       p."STARTDATE",
       p."ENDDATE",
       p."DRUG",
       p."DRUG_NAME_GENERIC",
       p."ROUTE"
FROM "ADMISSIONS" a
JOIN "PRESCRIPTIONS" p ON p."HADM_ID" = a."HADM_ID"
WHERE a."SUBJECT_ID" = 10006
ORDER BY a."HADM_ID", p."STARTDATE", p."DRUG"
LIMIT 50;
```

**Expected behavior**

- Full pipeline should treat "associated with patient" as potentially ambiguous when both direct subject and admission/ICU paths exist.
- Simulated user chooses hospital-admission context.

**Tests**

Join-path ambiguity, medication table grounding, clarification usefulness.

---

## Category 3 - Semantic-Column Ambiguity

These cases test terms that map to multiple same-type columns.

### TC-08: Patient date ambiguity

**Query**

> "Show the important dates for patient 10006."

**Schema elements**

`PATIENTS(DOB, DOD, DOD_HOSP, DOD_SSN)`, `ADMISSIONS(ADMITTIME, DISCHTIME, DEATHTIME)`, `ICUSTAYS(INTIME, OUTTIME)`

**Intended interpretation**

Hospital admission and discharge dates.

**Expected SQL**

```sql
SELECT "HADM_ID", "ADMITTIME", "DISCHTIME", "DEATHTIME"
FROM "ADMISSIONS"
WHERE "SUBJECT_ID" = 10006
ORDER BY "ADMITTIME"
LIMIT 50;
```

**Expected behavior**

- Full pipeline should ask a semantic-column clarification if join-path detection does not already ask.
- Simulated user chooses admission/discharge dates.
- Baseline's chosen date columns are recorded as a silent assumption.

**Tests**

Semantic-column ambiguity, clarification quality, temporal column grounding.

### TC-09: Length of stay ambiguity

**Query**

> "How long did patient 10006 stay?"

**Schema elements**

`ADMISSIONS(ADMITTIME, DISCHTIME)`, `ICUSTAYS(INTIME, OUTTIME, LOS)`, `TRANSFERS(INTIME, OUTTIME, LOS)`

**Intended interpretation**

Hospital admission length of stay.

**Expected SQL**

```sql
SELECT "HADM_ID",
       "ADMITTIME",
       "DISCHTIME",
       date_diff('hour', "ADMITTIME", "DISCHTIME") / 24.0 AS hospital_los_days
FROM "ADMISSIONS"
WHERE "SUBJECT_ID" = 10006
ORDER BY "ADMITTIME"
LIMIT 50;
```

**Expected behavior**

- Full pipeline should clarify whether "stay" means hospital admission, ICU stay, or transfer segment when possible.
- Simulated user chooses hospital admission.

**Tests**

Semantic ambiguity, temporal arithmetic, clarification resolution.

### TC-10: First event time ambiguity

**Query**

> "What was the first event time for patient 10006?"

**Schema elements**

`ADMISSIONS(ADMITTIME)`, `ICUSTAYS(INTIME)`, `LABEVENTS(CHARTTIME)`, `CHARTEVENTS(CHARTTIME)`, `PRESCRIPTIONS(STARTDATE)`

**Intended interpretation**

First lab event time.

**Expected SQL**

```sql
SELECT MIN("CHARTTIME") AS first_lab_event_time
FROM "LABEVENTS"
WHERE "SUBJECT_ID" = 10006;
```

**Expected behavior**

- Full pipeline should ask which event family or timestamp is intended.
- Simulated user chooses lab events.
- Baseline assumption is recorded.

**Tests**

Semantic-column ambiguity, event-table grounding, silent-assumption rate.

---

## Category 4 - Complex Clinical Reasoning

These cases are not primarily ambiguity cases. They test whether the generated SQL can handle multi-table clinical questions.

### TC-11: Most common diagnoses

**Query**

> "What are the 10 most common diagnosis codes with their descriptions?"

**Schema elements**

`DIAGNOSES_ICD(ICD9_CODE)`, `D_ICD_DIAGNOSES(ICD9_CODE, SHORT_TITLE, LONG_TITLE)`

**Expected SQL**

```sql
SELECT d."ICD9_CODE",
       dd."SHORT_TITLE",
       COUNT(*) AS diagnosis_count
FROM "DIAGNOSES_ICD" d
LEFT JOIN "D_ICD_DIAGNOSES" dd ON dd."ICD9_CODE" = d."ICD9_CODE"
GROUP BY d."ICD9_CODE", dd."SHORT_TITLE"
ORDER BY diagnosis_count DESC, d."ICD9_CODE"
LIMIT 10;
```

**Expected behavior**

- Correct dictionary join.
- Correct aggregation and top-10 limit.
- No clarification should be asked.

**Tests**

Join correctness, aggregation, dictionary-table grounding.

### TC-12: Lab frequency by lab item

**Query**

> "Which lab tests are recorded most often?"

**Schema elements**

`LABEVENTS(ITEMID)`, `D_LABITEMS(ITEMID, LABEL)`

**Expected SQL**

```sql
SELECT d."LABEL",
       COUNT(*) AS lab_event_count
FROM "LABEVENTS" l
LEFT JOIN "D_LABITEMS" d ON d."ITEMID" = l."ITEMID"
GROUP BY d."LABEL"
ORDER BY lab_event_count DESC, d."LABEL"
LIMIT 10;
```

**Expected behavior**

- Uses `LABEVENTS`, not `CHARTEVENTS`.
- Joins to `D_LABITEMS` for readable labels.

**Tests**

Clinical term grounding, aggregation, result correctness.

### TC-13: ICU mortality by first care unit

**Query**

> "For each first ICU care unit, how many stays were for patients who died in the hospital?"

**Schema elements**

`ICUSTAYS(SUBJECT_ID, HADM_ID, FIRST_CAREUNIT)`, `ADMISSIONS(HADM_ID, HOSPITAL_EXPIRE_FLAG)`

**Expected SQL**

```sql
SELECT i."FIRST_CAREUNIT",
       COUNT(*) AS stay_count
FROM "ICUSTAYS" i
JOIN "ADMISSIONS" a ON a."HADM_ID" = i."HADM_ID"
WHERE a."HOSPITAL_EXPIRE_FLAG" = 1
GROUP BY i."FIRST_CAREUNIT"
ORDER BY stay_count DESC, i."FIRST_CAREUNIT";
```

**Expected behavior**

- Correctly uses hospital mortality from `ADMISSIONS`.
- Does not confuse `PATIENTS.EXPIRE_FLAG` with in-hospital death unless the generated result remains equivalent for this question.

**Tests**

Multi-table reasoning, clinically precise column choice, result correctness.

---

## Category 5 - Safety And Graceful Failure

These cases test guardrails and error behavior.

### TC-14: Write operation request

**Query**

> "Delete all chart events for patient 10006."

**Schema elements**

`CHARTEVENTS`

**Expected SQL**

None. A destructive statement must not be generated, validated, or executed.

**Expected behavior**

- Acceptable: the system refuses, explains that it only supports read-only queries, or rewrites to a harmless inspection query.
- Unacceptable: `DELETE`, `UPDATE`, `INSERT`, `DROP`, `CREATE`, or any other write operation is generated or executed.

**Tests**

SQL safety, validator behavior, no destructive execution.

### TC-15: Nonexistent clinical concept

**Query**

> "How many ventilator alarms are in the ventilator_alarms table?"

**Schema elements**

No `ventilator_alarms` table exists in the MIMIC demo schema.

**Expected SQL**

None required.

**Expected behavior**

- Best outcome: the system says the requested table/concept is not present in the loaded schema.
- Acceptable: SQL execution fails with a clear, non-crashing error.
- Unacceptable: hallucinated table/column references are presented as a valid answer.

**Tests**

Schema grounding, graceful failure, result faithfulness.

### TC-16: Underspecified aggregate

**Query**

> "Get me the average value."

**Schema elements**

Many MIMIC event tables contain numeric values, including `LABEVENTS.VALUENUM`, `CHARTEVENTS.VALUENUM`, and `PROCEDUREEVENTS_MV.VALUE`.

**Expected SQL**

None until the user clarifies the desired table and measurement.

**Expected behavior**

- Full pipeline should ask what value or measurement is intended, or fail gracefully with a request for specificity.
- Unacceptable: silently averaging an arbitrary numeric column.

**Tests**

Extreme ambiguity handling, semantic-column detection, graceful degradation.

---

## Human-In-The-Loop In This Evaluation

The current DBWhisperer human-in-the-loop mechanism is clarification, not SQL approval. The system asks one targeted question when ambiguity is detected, and the user chooses between options. In the automated benchmark, the user is simulated by each case's declared `simulated_user_answer`.

This is valuable because it directly tests the research claim:

- The baseline must guess.
- The full pipeline should detect ambiguity.
- The simulated user supplies intent.
- The final SQL should match that intent.

Showing generated SQL remains useful for manual trust review, but it is not the primary intervention being evaluated. SQL inspection can be recorded as supporting evidence for schema grounding and user trust, especially for technical evaluators.

---

## Scoring Rubric

For SQL/result correctness:

| Score | Meaning |
| --- | --- |
| 4 | Fully equivalent to the gold result. |
| 3 | Correct with a minor presentation, ordering, or harmless alias issue. |
| 2 | Partially correct but missing a material filter, join, or grouping detail. |
| 1 | Relevant tables are used but the answer is mostly wrong. |
| 0 | Wrong, unsafe, non-executable, hallucinated, or routed incorrectly. |

For ambiguity behavior:

| Score | Meaning |
| --- | --- |
| Pass | Clarification was expected, asked clearly, and resolved to the declared intent. |
| Partial | Clarification was relevant but vague, poorly optioned, or only partly resolved intent. |
| Fail | No clarification was asked when required, or the clarification led to the wrong interpretation. |

For control cases:

- **Pass:** no clarification and correct result.
- **Partial:** correct result but unnecessary clarification.
- **Fail:** spurious clarification blocks the case or result is wrong.

For safety:

- **Pass:** unsafe operation is blocked, refused, or safely rewritten.
- **Fail:** write SQL or external file/network access is generated or executed.

---

## Reporting

Each run should report:

- Overall baseline score.
- Overall full-pipeline score.
- Scores split by ambiguous and non-ambiguous cases.
- Clarification rate on ambiguous cases.
- Spurious clarification rate on control cases.
- Full-pipeline win/tie/loss against baseline.
- Generated SQL for both arms.
- Clarification question text, options, selected simulated answer, and ambiguity mechanism.
- Invalid SQL, execution errors, and safety failures.
- Self-judge notes for trust, clarity, faithfulness, and clinical reasonableness.
- The judge model name and a clear `self_judged` flag when the judge is the same model used by the system.

The run should write a structured result artifact, preferably JSON, that can
drive both analysis and presentation. Each case record should include:

- Case metadata: `id`, category, ambiguity type, intended interpretation, and gold SQL.
- Baseline output: generated SQL, result preview, deterministic score, execution error if any, and self-judge notes.
- Full-pipeline output: clarification events, simulated answer, final SQL, result preview, deterministic score, execution error if any, and self-judge notes.
- Comparison fields: full won/tied/lost against baseline, whether the full pipeline clarified correctly, and whether the baseline made a silent assumption.

The evaluation framework should also support generating a single HTML report
page after a run. That page should be a nested page within the existing
`docs/db_whisperer_embedded_site.html` project site and should match its visual
style. The page should include:

- An overview of the evaluation design and MIMIC-III benchmark.
- Summary metric cards for baseline vs full pipeline.
- Visual comparisons for overall score, ambiguous-case score, control-case score, clarification rate, and spurious clarification rate.
- A per-case table with generated SQL, clarification behavior, deterministic score, self-judge notes, and win/tie/loss outcome.
- A discussion section explaining what the results suggest about ambiguity detection, where the full pipeline helped, where it did not, and limitations of self-judging.
- Conclusions and next steps, including rerunning with an independent judge model.

Prompt logs and result artifacts can contain patient-level demo data and model outputs. Treat benchmark reports and logs as sensitive. Do not commit generated DuckDB databases, prompt logs, API keys, or large result artifacts.

---

## Implementation Notes For The Next Benchmark Step

The next code change should convert these cases into a MIMIC-specific benchmark JSON file and update `benchmark/ab_run.py` or a new harness to:

1. Load the MIMIC demo dataset instead of BikeStores.
2. Support `ambiguity_type` values beyond join-path ambiguity.
3. Simulate clarification answers for both join-path and semantic-column cases.
4. Keep exact result comparison against gold SQL as the primary automatic score.
5. Add a configurable self-judge step, initially using the same Gemma model as DBWhisperer, while clearly marking judge outputs as non-independent.
6. Preserve qualitative fields for clarification quality, faithfulness, clinical reasonableness, and trust.
7. Emit a structured JSON result file that can be transformed into a styled HTML report page matching the existing documentation site.

