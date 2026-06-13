# Application

The application component coordinates the end-to-end query workflow. It is
separate from Streamlit so the same workflow can later be used from tests,
scripts, or another interface.

## Responsibilities

1. Receive the current user request and schema context.
2. Ask the Querier to generate and execute K SQL candidates in parallel.
3. Send successful SQL/result pairs to the Ambiguity Specifier.
4. Return a clarification question or the most recent query result.

## Boundaries

- Does not generate SQL itself.
- Does not contain Streamlit presentation code.

## Current Scope

`ApplicationService` provides the interface used by the GUI:

- `ingest_csvs` routes uploads to the ETLer.
- `submit_query` runs one ambiguity iteration.

The GUI may call `submit_query` up to three times. User answers are passed back
as clarifications and included in every Querier prompt in the next iteration.
The user selects K in the GUI, with a default of three.
Candidate processing uses a bounded worker pool and restores candidate order
before ambiguity evaluation.
