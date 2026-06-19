# ETLer

The ETLer turns an uploaded CSV file into a queryable DuckDB database.

## Responsibilities

- Import one or more CSV files as one DuckDB table each.
- Expose per-table and column metadata.
- Discover foreign-key relationships between tables.
- Provide schema context to the Querier and Ambiguity Specifier.

## Input

- One or more CSV files.

## Output

- A DuckDB database.
- Per-table schema metadata (columns, row counts, detected keys).
- Discovered foreign-key relationships.

The ETLer does not generate natural-language answers or SQL queries.

## Current Scope

`ETLService.ingest` loads every uploaded CSV into
`data/generated/db_whisperer.duckdb` (one table per file) and returns the
database path plus per-table schema in `SchemaMetadata.tables`.

Relationships are discovered from naming hints confirmed by value overlap
(an FK column's values must be a near-subset of a parent key's values). Each
`Relationship` reports the child/parent columns, the raw `overlap`, a ranking
`score`, and whether it is `ambiguous` (several plausible parents) or `sampled`
(computed on a capped sample of a very large column). Relationships are
advisory metadata only and are never enforced as DuckDB constraints.

Robustness: columns that break type inference are reloaded as VARCHAR; a final
fallback skips malformed rows and flags `discovery_complete=False`. The literal
text `NULL` is treated as a null token so id columns that use it still infer
clean numeric types.

Entity resolution across tables is intentionally not implemented.
