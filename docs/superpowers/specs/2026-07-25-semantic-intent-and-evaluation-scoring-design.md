# Semantic Intent and Evaluation Scoring Follow-up

## Purpose

The first official Evaluation V3 campaign exposed two related problems:

1. DB Whisperer's semantic ambiguity evidence is limited to vague terms that
   map to multiple same-type columns. It can therefore prefer a column-label
   distinction over the measure, aggregation grain, temporal role, or entity
   scope the user actually left unresolved.
2. Evaluation V3 treats exact result width, aliases, representation, and
   reference ordering as correctness gates even when the generated result
   satisfies the user's intent.

This follow-up preserves the four-arm, K=3, five-repetition V3 research design.
It changes the production semantic evidence contract, the correctness scorer,
the clarification submetrics, one debatable control query, and failure
reporting. Join-path ambiguity remains retired.

## Campaign Evidence Behind the Design

The published campaign contained 360 correctness-applicable observations.
Strict scoring passed 20. A deterministic counterfactual over the saved rows
found 120 additional observations whose required values were recoverable after
allowing harmless projection, alias, and non-material ordering differences.

The remaining result-shape failures were also heterogeneous:

- 20 top-10 ranking results omitted an explicit rank column and differed only
  among tied boundary rows.
- 9 hospital durations used an interval instead of fractional days.
- 7 ICU durations returned the requested duration without all reference
  context columns.
- 21 admission durations used whole days instead of fractional days.

The generic `query was not accepted` reason covered 51 observations:

- 43 stopped at an unmatched clarification;
- 2 failed during post-clarification regeneration;
- 3 produced one usable candidate but failed the two-candidate workflow
  quorum; and
- 3 produced no valid initial SQL.

These findings do not invalidate the campaign evidence. They show that the
current report combines user-intent correctness, exact benchmark conformance,
ambiguity behavior, and workflow termination in ways that obscure the cause.

## Production Semantic-Intent Design

### Structured findings

Replace column-only semantic findings with structured intent findings. A
finding identifies:

- the exact unresolved phrase from the request;
- one semantic dimension;
- two or more internally grounded interpretations;
- the tables, columns, operations, or grain that support each interpretation;
- whether request modifiers already resolve the dimension; and
- a relevance ordering used when the UI must show exactly two options.

Supported dimensions initially include:

- `aggregation_grain`;
- `measure_definition`;
- `temporal_role`;
- `entity_scope`;
- `episode_scope`;
- `filter_scope`; and
- `column_meaning`.

The detector may retain several grounded interpretations internally. The
unified judge still asks one two-option question per round.

### Interpretation contract

Each interpretation has a stable round-local ID, a concise user-facing label,
and structured grounding. Grounding may include:

- exact qualified columns;
- exact tables;
- aggregation semantics such as record count or distinct-entity count;
- entity grain;
- temporal role; and
- scope constraints.

Unknown identifiers invalidate only the affected interpretation. A finding
with fewer than two valid unresolved interpretations is not actionable.

### Compositional resolution

The detector must interpret complete phrases rather than collect same-type
columns independently.

Explicit modifiers settle a dimension when they select one natural meaning:

- `hospital mortality` selects death during a hospital admission;
- `distinct patients` selects a distinct-patient denominator;
- `ICU stay` selects the ICU episode;
- `admitted to the hospital` selects hospital admission time.

The detector returns no actionable finding for a dimension already settled by
the wording.

### Priority and suppression

When several findings remain, the unified judge uses this priority:

1. measure definition, aggregation grain, temporal role, and entity or episode
   scope;
2. source-column meaning that materially changes cohort membership or values;
3. representation or label choice.

Representation-only choices such as diagnosis long title versus short title
are suppressed unless the request explicitly asks for names, descriptions,
codes, or another presentation form. A lower-priority distinction cannot
pre-empt an unresolved higher-priority dimension.

For `How common is each diagnosis?`, the actionable finding is:

- diagnosis-record occurrences using a row count; versus
- distinct affected patients using a distinct-patient count.

The question should ask what `common` means. It must not ask about long versus
short titles while the count grain remains unresolved.

### Temporal-role grouping

Temporal columns are grouped by real-world role before option selection.
Multiple death timestamps form one death-role interpretation rather than
several competing column options.

For `Show me patients from the year 2112`, the detector can ground:

- birth year with `patients.dob`;
- hospital admission year with `admissions.admittime`; and
- death year with the applicable death timestamps.

The unified judge must choose the most natural unresolved roles for the full
phrase and record why omitted roles are less relevant. The documented
birth-versus-admission behavior must be covered by an end-to-end detector test,
not only by tests that inject a preconstructed finding.

### Full-system and ablation behavior

The Full System retains candidate evidence as primary after the plausibility
gate. Structured semantic findings remain supporting evidence and fallback.

Semantic Only receives the same structured findings but no candidate
diversity during initial selection. This keeps the ablation meaningful without
allowing false same-type-column findings such as `hospital_expire_flag` versus
`expire_flag` to become automatic questions.

Candidate Only remains unchanged except for shared clarification-quality
reporting. Baseline remains a single direct generation.

## Evaluation Scoring Design

### Semantic answer correctness

Answer correctness measures whether the returned result satisfies the user's
resolved intent. It does not require:

- exact projection width;
- exact aliases;
- harmless extra context columns;
- exact reference tie-breaking when tied alternatives are equally valid; or
- one fixed duration representation.

Required result concepts and values must be recoverable through an unambiguous
mapping. Extra columns fail correctness only when they change row grain,
cardinality, grouping, filtering, meaning, or expose a material safety or
privacy concern.

### Projection and presentation diagnostics

Projection precision and presentation fidelity are reported separately from
semantic correctness. They record:

- missing requested columns;
- harmless extra columns;
- ambiguous aliases;
- material versus non-material ordering differences; and
- unnecessary sensitive fields.

These diagnostics do not turn an otherwise correct answer into zero semantic
correctness.

### Duration normalization

Duration answers may use:

- integer numeric units;
- fractional numeric units; or
- DuckDB interval/timedelta values.

The case contract declares the requested unit. The scorer normalizes supported
representations and recognizes documented whole-day and elapsed-time
calculations. Equivalent intervals and fractional values pass. Whole-day
answers pass when the request does not require sub-day precision.

Raw start and end timestamps are useful context but do not by themselves
replace a requested duration value.

### Ranking and ordering

Ordering is correctness-critical only when the request explicitly requires
ranking, chronology, top-N, or another ordered interpretation.

An ordered result can satisfy a request to rank items without returning a
separate numeric rank column unless the user explicitly requests rank values.
Top-N comparison is tie-aware: equally ranked boundary members do not fail
solely because the reference selected a different deterministic tie member.
Reference-only tie-breakers remain reproducibility diagnostics.

### Clarification quality

Clarification evaluation separates:

- whether interruption was justified;
- whether the question describes a plausible unresolved dimension;
- whether its options cover the simulated target interpretation;
- whether the options are mutually exclusive and user-facing;
- whether the selected answer was applied; and
- whether the final result aligns with the answer.

A plausible but incomplete question can receive plausibility credit while
failing target coverage and resolution. An unnecessary clarification still
reduces ambiguity specificity.

### Workflow outcomes

Replace `query was not accepted` in reports with a stable terminal taxonomy:

- `unresolved_clarification`;
- `unnecessary_clarification`;
- `target_option_missing`;
- `initial_generation_format_failure`;
- `candidate_quorum_failure`;
- `sql_validation_failure`;
- `sql_execution_failure`;
- `post_clarification_generation_failure`;
- `clarification_compliance_failure`; and
- `no_final_result`.

End-to-end completion still fails when no final answer is returned. The report
also scores the best pre-clarification executed result diagnostically so an
unnecessary interruption is distinguishable from an inability to generate a
correct query.

## Suite Change

Change the debatable control:

`Show patients admitted in the year 2112.`

to:

`Show patients admitted to the hospital in the year 2112.`

The revised wording explicitly resolves hospital versus ICU admission while
retaining the admission-year and year-literal behavior under test.

Existing explicit controls for hospital mortality, diagnosis-record count,
distinct-patient count, and ICU duration remain non-ambiguous. They receive
regression coverage against the false clarifications observed in the campaign.

## Data Flow

1. The pre-SQL detector reads the full request, prior answers, and schema.
2. It emits validated structured findings with grounded interpretations.
3. SQL candidates are generated and executed as before.
4. The unified judge receives candidate alternatives and, where enabled,
   structured findings.
5. The judge selects the most important unresolved semantic dimension or
   passes.
6. After an answer, existing schema pinning and compliance validation use the
   selected interpretation grounding.
7. Evaluation records the terminal workflow outcome, semantic result score,
   projection diagnostics, ambiguity submetrics, grounding, efficiency, and
   safety separately.

## Failure Handling

- Invalid structured findings are dropped with observable reasons.
- If all findings are invalid, the existing no-semantic-evidence path applies.
- A judge cannot select columns, tables, operations, or interpretation IDs
  outside the validated finding.
- Explicitly resolved findings cannot trigger deterministic fallback.
- A fallback question uses the highest-priority validated unresolved finding,
  not the first same-type column pair.
- Provider, transport, and harness failures remain infrastructure failures and
  never enter system correctness scoring.

## Testing

### Production tests

Add detector, prompt, parser, application, and end-to-end scenarios for:

- birth year versus hospital admission year with death columns present;
- `common` as occurrences versus distinct patients;
- suppression of long-title versus short-title presentation ambiguity;
- suppression of overall-death ambiguity for `hospital mortality`;
- suppression of hospital-versus-ICU ambiguity after `admitted to the
  hospital`;
- ICU duration using both endpoints or `los`;
- structured-finding validation and stable IDs;
- priority among multiple unresolved dimensions;
- fallback using the highest-priority finding; and
- clarification compliance using operation and grain grounding.

### Evaluation tests

Add scorer and report tests for:

- required-column subset comparison with harmless extras;
- aliases and derived-expression mappings;
- integer, fractional, and interval duration equivalence;
- whole-day duration acceptance without a precision requirement;
- rejection when extra projection changes row grain;
- order-as-rank and tie-aware top-N;
- explicit-order requests versus reference-only tie-breakers;
- clarification plausibility separate from target coverage;
- each terminal workflow outcome; and
- best pre-clarification result diagnostics.

Replay the new deterministic scorer over the saved official campaign before
any new paid run. The replay is labelled a counterfactual rescore and does not
overwrite the immutable original campaign. Run a one-repetition canary only
after all deterministic tests pass.

## Documentation and Reporting

Update:

- `docs/AMBIGUITY_DECISION_CHANGES.md` for production semantic-intent changes;
- `docs/EVALUATION_V3_METHOD_CHANGES.md` for scoring, suite, and reporting
  changes;
- both HTML report renderers and their methodology text; and
- report evidence tables to show terminal outcome and projection diagnostics.

The final report must distinguish the immutable original score, any
counterfactual deterministic rescore, and results from a later live campaign.
