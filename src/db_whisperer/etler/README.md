# ETLer

The ETLer turns an uploaded CSV file into a queryable DuckDB database.

## Responsibilities

- Import one CSV file as one DuckDB table.
- Expose table and column metadata.
- Provide schema context to the Querier and Ambiguity Specifier.

## Input

- One CSV file.

## Output

- A DuckDB database.
- Basic table and column metadata.

The ETLer does not generate natural-language answers or SQL queries.

## Current Scope

`ETLService.ingest` persists the uploaded CSV in
`data/generated/db_whisperer.duckdb` by default and returns its absolute path,
table name, columns, and row count. Multiple files, relationships, and entity
resolution are intentionally not implemented yet.
