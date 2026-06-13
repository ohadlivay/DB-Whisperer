# Querier

The Querier converts a natural-language request into DuckDB SQL using schema
context and an LLM.

## Responsibilities

- Build an LLM prompt from the user request and database schema.
- Include the database schema, top five rows, shape, and per-column statistics.
- Render the discovered schema as quoted DuckDB DDL and repeat an exact
  identifier allowlist near the user request.
- Generate one SQL query through OpenRouter.
- Validate that generated SQL is read-only and compatible with the schema.
- Execute the query against DuckDB.

## Input

- The current user request.
- DuckDB database path and schema metadata.
- OpenRouter API key and model.

## Output

- Generated SQL and query results.

`QueryService.build_prompt` exposes the final LLM prompt for testing. The prompt
is the concatenation of static SQL instructions, database schema, five sample
rows, table shape, column statistics, valid identifiers, and the user's request.
The identifier guidance is generated from the uploaded CSV schema and does not
depend on a particular dataset. When the ambiguity workflow has collected
user-confirmed clarifications, they are appended in a final `CLARIFICATIONS`
section. No section is added when there are no clarifications.
