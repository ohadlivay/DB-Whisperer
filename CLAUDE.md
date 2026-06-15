# Claude Handoff Notes

Read `AGENTS.md` first. It contains the general project guidance, current
architecture, status-PDF summary, gaps, and testing expectations.

## Highest-Signal Context

- This repository is the current DB Whisperer prototype, not the full
  schema-graph system described in `C:/Users/yotam/Downloads/Project Status 1.pdf`.
- Current app: one CSV -> DuckDB -> schema-aware prompt -> OpenRouter SQL ->
  read-only validation/execution -> ambiguity judging over K executed
  candidate alternatives -> optional two-choice clarification -> Streamlit UI.
- Future target from the PDF: multiple related CSVs, schema graph, join-path
  ambiguity detection, semantic column ambiguity fallback, and a comparison
  against a single-pass LLM baseline.
- The most dangerous wrong assumption is that multi-table relationship or
  schema-graph support already exists. It does not.

## Where To Look First

- `ARCHITECTURE.md` for the intended component split.
- `src/db_whisperer/contracts.py` for all cross-component payloads.
- `src/db_whisperer/application/service.py` for the end-to-end workflow.
- `src/db_whisperer/etler/service.py` for the current single-CSV limitation.
- `src/db_whisperer/querier/prompt_builder.py` for the prompt context sent to
  SQL generation.
- `src/db_whisperer/querier/sql_validator.py` for safety constraints.
- `src/db_whisperer/ambiguity/prompt_builder.py` and
  `src/db_whisperer/ambiguity/service.py` for ambiguity judging.
- `src/db_whisperer/gui/app.py` for Streamlit state, controls, and rendering.
- `src/db_whisperer/gui/changelog.json` for shipped feature history.

## Current Behavior To Preserve

- ETL rejects zero, empty, or multiple files and replaces the previous DuckDB
  database when a new CSV is ingested.
- The bundled student-impact CSV is the default dataset when no user upload is
  selected.
- The Querier includes schema DDL, five sample rows, shape, column statistics,
  a valid identifier allowlist, user prompt, and optional clarifications.
- Generated SQL must be one read-only SELECT. Forbidden operations and external
  scan functions are rejected before execution.
- Invalid SQL syntax gets one validation-repair retry.
- Application candidate attempts run in parallel but are restored to candidate
  order before ambiguity evaluation.
- Ambiguity judging deduplicates exact SQL/result alternatives and skips the
  LLM call if fewer than two unique alternatives remain.
- Clarification answers are formatted as question/selected answer pairs and
  appended to later Querier and Ambiguity prompts.
- The third iteration returns the latest successful query result instead of
  asking another ambiguity question.
- Prompt logs correlate prompts and responses by request ID and must not
  include API keys, but they can include sensitive CSV values.

## Before Making Changes

- Check `git status --short` and avoid disturbing unrelated user changes.
- Read the nearby tests before editing behavior. The suite is small and
  intentionally documents many product decisions.
- If changing prompts, response contracts, or GUI state keys, expect tests to
  need deliberate updates.

## Verification

Use:

```powershell
python -m unittest discover
```

For Streamlit/manual verification:

```powershell
streamlit run app.py
```

If adding the PDF's schema-graph direction, add tests that prove multiple CSVs
can be ingested, relationships are represented, join paths can be enumerated,
clarifications correspond to real paths or semantic column choices, and the
single-pass baseline remains available for evaluation.
