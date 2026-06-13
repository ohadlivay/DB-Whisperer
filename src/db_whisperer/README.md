# DB Whisperer Package

This package contains the core components of DB Whisperer.

## Components

- `application`: Orchestrates ingestion, query candidates, and ambiguity rounds.
- `etler`: Loads CSV files into DuckDB and describes the resulting schema.
- `ambiguity`: Judges whether generated query candidates express different
  interpretations.
- `querier`: Generates, validates, and executes DuckDB SQL.
- `gui`: Presents the workflow through Streamlit.
- `contracts.py`: Defines the shared data passed between components.

Components communicate through shared contracts.

All outbound LLM prompts and raw responses are stored as JSON Lines in
`logs/prompts.jsonl`. Each pair shares a request ID. Set
`DB_WHISPERER_PROMPT_LOG` to use another path. Logs include database samples
and should be treated as sensitive data; API keys are never included. The same
file records candidate generation and execution outcomes, including rejected
SQL and DuckDB errors.
