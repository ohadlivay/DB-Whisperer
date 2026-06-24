# DB Whisperer Agent Notes

## Project Purpose

DB Whisperer is a Streamlit/Python app for querying CSV data with natural
language. The current app loads CSV data into DuckDB, builds a schema-aware
prompt, asks OpenRouter for DuckDB SQL, validates and executes the SQL, and
uses an ambiguity layer to decide whether the user needs one clarifying
question.

The project-status PDF frames the research goal as an SE comparison: whether
an explicit ambiguity-detection layer improves interpretive accuracy and user
trust versus a single-pass LLM-to-SQL baseline. The long-term artifact should
answer across a connected database graph and ask a targeted clarification when
the user's wording maps to multiple valid interpretations.

## Current Implementation Snapshot

- Entry point: `app.py` adds `src` to `sys.path` and calls
  `db_whisperer.gui.app.main`.
- Core package: `src/db_whisperer`.
- Shared data contracts are in `src/db_whisperer/contracts.py`; preserve these
  boundaries when wiring components together.
- `application/` owns workflow orchestration. It generates K SQL candidates in
  parallel, executes them, sends successful SQL/result pairs to ambiguity
  judging, and returns either a final result or a pending clarification.
- `etler/` currently supports exactly one CSV upload. It creates or replaces a
  DuckDB file at `data/generated/db_whisperer.duckdb` by default and returns
  basic table/column/row-count metadata.
- `querier/` builds prompts from static instructions, DDL, top five rows,
  table shape, column statistics, exact valid identifiers, the user request,
  and optional clarifications. It expects OpenRouter to return
  `{"sql": "<query>"}`.
- `querier/sql_validator.py` only allows one DuckDB SELECT statement and
  rejects write operations plus external file/network scan functions.
- `ambiguity/` compares already executed SQL/table alternatives. It does not
  generate SQL, execute SQL, call the Querier, or manage UI loops.
- `gui/` is the Streamlit interface. It loads the bundled
  `data/single csv/ai_student_impact_dataset.csv` when no user file is
  uploaded, exposes OpenRouter model/API-key controls, and shows clarification
  buttons, result tables, generated SQL, chat history, version notes, and
  session usage.

## Important Gap From The Status PDF

The PDF describes a schema-graph architecture for multiple related CSV files,
especially clinical/MIMIC-style data. It proposes join-path multiplicity as the
primary ambiguity mechanism and semantic-type column matching as a fallback.

Implemented so far:

- Multi-CSV ingestion and pairwise foreign-key discovery (`etler/`).
- The schema graph and join-path enumeration (`schema_graph/`): discovered
  foreign keys are assembled into an undirected multigraph and the distinct
  simple join paths between two tables are enumerated, with hop and path caps.
- The primary ambiguity mechanism (`ambiguity/join_path_service.py`): an LLM
  extracts the entities in a question and maps them to tables, the graph
  enumerates join paths between them, and a clarifying question is raised when
  more than one distinct path connects an entity pair. The application runs
  this before SQL generation on the first round of a multi-table question and
  degrades to the candidate-comparison judge if detection fails.

Still missing from the PDF direction:

- Mechanism 2, the semantic-type column matching fallback (e.g. "dates" ->
  admission vs discharge vs date of birth).
- The controlled baseline-vs-full-pipeline evaluation harness. The
  `enable_join_path_detection` flag on `ApplicationService` is the seam for the
  ablation but no measurement harness exists yet.
- Semantic pruning of graph paths: enumeration is faithful to the PDF
  (all simple paths), so it can surface join paths that route through a fact
  table, which is correct graph behaviour but not always a natural join.

## Research And Product Direction From The PDF

- Research question: how accurately can an LLM-based system translate natural
  language into executable schema-aware database queries while reducing
  misinterpretation through explicit ambiguity detection and clarification.
- Expected behavior: return an answer/table plus the underlying query result,
  or ask one targeted clarifying question when the schema graph supports more
  than one interpretation.
- Primary ambiguity mechanism: extract mentioned entities, map them to tables,
  enumerate valid join paths in the schema graph, and clarify if more than one
  path exists.
- Secondary ambiguity mechanism: detect natural-language terms that map to
  multiple semantically similar columns, such as admission date, discharge
  date, date of birth, or chart time.
- Evaluation idea: compare the full pipeline with ambiguity detection against
  a baseline that skips Component B and goes straight to SQL generation.
  Measure SQL correctness, whether clarifications improve results, and user
  trust or perceived usefulness.

## Operational Notes

- Install and run locally with:

  ```powershell
  python -m pip install -r requirements.txt
  streamlit run app.py
  ```

- Dependencies are intentionally small: DuckDB, Requests, and Streamlit.
- OpenRouter keys are supplied by the user in the UI or via
  `OPENROUTER_API_KEY`. Do not log or persist API keys.
- `OPENROUTER_MODEL` can configure the default model. The GUI also provides
  presets and a custom model field.
- Prompt and response logs are written to `logs/prompts.jsonl` by default or
  to `DB_WHISPERER_PROMPT_LOG` if set.
- Logs include prompts, raw model responses, database samples/statistics,
  generated SQL, and failure details. Treat logs as sensitive because CSV
  values can appear there.

## Testing Expectations

Run the full suite after code changes:

```powershell
python -m unittest discover
```

Focused areas:

- ETL behavior: `tests/etler/test_service.py`.
- Prompt construction and SQL execution: `tests/querier/`.
- Ambiguity request validation and prompt formatting: `tests/ambiguity/`.
- Workflow orchestration, retries, parallel candidate handling, and
  clarification passing: `tests/application/test_service.py`.
- Streamlit behavior and session-state helpers: `tests/gui/test_app.py`.
- Prompt logging: `tests/test_prompt_logging.py`.

Add or update tests when changing component contracts, prompt sections,
OpenRouter response parsing, SQL validation rules, candidate ordering,
clarification flow, logging semantics, or GUI session-state behavior.

## Coding Guidance

- Keep component boundaries strict. The GUI should delegate workflow decisions
  to `ApplicationService`; ambiguity should judge alternatives only; the
  Querier should generate/validate/execute SQL only; ETL should own database
  creation and schema metadata.
- Prefer evolving shared dataclasses in `contracts.py` deliberately, then
  updating all call sites and tests.
- Preserve exact identifier handling. Generated SQL should use quoted source
  table and column names exactly as DuckDB discovered them.
- Keep SQL execution read-only. Changes to validation should be conservative
  and covered by tests.
- Do not assume graph-based multi-table support exists. If a feature needs it,
  implement schema metadata, relationship discovery/configuration, prompt
  context, ambiguity logic, GUI upload flow, and tests together.
- Be careful with model prompts. Small prompt edits can affect tests and
  behavior; update expectations intentionally.
- Avoid adding secrets, generated DuckDB files, logs, or large datasets to git.
