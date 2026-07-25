# Ambiguity Specifier

The Ambiguity Specifier determines whether K generated SQL queries and their
executed result tables represent meaningfully different interpretations of the
user's request.

## Responsibilities

- Receive the user query and K pairs of SQL plus executed table data.
- Send a bounded representation of every pair to an LLM judge.
- Return `pass` when no material ambiguity is found.
- Otherwise, return one concise question and exactly two answer options.

## Boundaries

- Does not generate SQL.
- Does not execute SQL.
- Does not call the Querier.
- Does not manage clarification rounds.

The application layer is responsible for collecting the K executed pairs and
handling the returned clarification question and selected option.

## Prompt Context

For each executed table, the judge receives its columns, returned shape,
truncation status, null and distinct counts, and up to five sampled rows. This
keeps the full service input available while preventing large result tables
from producing unbounded prompts.

Before SQL generation, semantic analysis reads complete phrases and emits
validated intent findings. Each finding names one unresolved dimension and at
least two ranked interpretations. Interpretation grounding contains only
schema-validated tables, qualified columns, and allowed operations. Supported
dimensions include aggregation grain, measure definition, temporal role,
entity and episode scope, filter scope, and column meaning.

Explicit modifiers settle their dimension. For example, `hospital mortality`
means death during the hospital admission, `distinct patients` settles the
counting grain, and `admitted to the hospital in the year 2112` settles the
temporal role. Presentation choices such as long versus short diagnosis title
do not pre-empt an unresolved meaning such as whether `common` means diagnosis
record frequency or distinct-patient prevalence.

## Output

`AmbiguityService.evaluate` returns an `AmbiguityDecision` containing either:

- `passed=True`; or
- `passed=False`, one user-facing `question`, and exactly two `options`.

Semantic decisions additionally retain the selected dimension, exact
interpretation IDs, and the stable union of grounded columns. The external
mechanism name remains `semantic-column` for compatibility.
