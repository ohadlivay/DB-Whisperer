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

## Output

`AmbiguityService.evaluate` returns an `AmbiguityDecision` containing either:

- `passed=True`; or
- `passed=False`, one user-facing `question`, and exactly two `options`.
