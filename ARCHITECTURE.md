# DB Whisperer Architecture

## Overview

DB Whisperer loads one or more CSV files into DuckDB and answers natural-language
questions with validated read-only SQL. Streamlit owns presentation,
ApplicationService owns orchestration, and OpenRouter supplies model calls.

## Components

### Component A: ETL

ETL imports one table per CSV, exposes exact table/column metadata, and discovers
advisory foreign-key relationships from naming and value overlap. Relationships
are never enforced as DuckDB constraints.

### Component B: Hybrid Ambiguity

Ambiguity is analyzed before and after SQL generation:

1. Before SQL, semantic analysis finds vague terms that map to multiple exact
   columns in the same coarse type bucket. It retains all validated unresolved
   findings but does not interrupt.
2. The application generates and executes K candidate SQL statements.
3. Exact SQL/result duplicates are clustered so the judge sees stable
   alternative IDs and candidate support counts.
4. A unified LLM judge compares executed SQL/results as primary evidence and
   rejects alternatives that are not coherent, natural readings of the user
   request. Semantic findings, schema columns/types, and direct relationships
   are supporting evidence.
5. Eligible candidate ambiguity has priority. A singleton candidate remains
   eligible only with wording or schema/semantic corroboration. If no candidate
   distinction passes the plausibility gate, a semantic finding may justify
   clarification.

The judge returns pass or one short question with exactly two options. Direct
relationships never trigger ambiguity by themselves, and alternate relationship
routes are never counted or enumerated.

### Component C: Querier

The Querier builds schema-aware prompts, generates DuckDB SQL, validates a
single read-only SELECT, and executes it. Prompt context includes DDL, top rows,
shape, statistics, exact identifiers, direct relationships, the user request,
and prior clarifications.

Large schemas use schema linking. Relevant endpoints may be connected with one
deterministic shortest relationship chain so bridge tables remain available.
This is SQL-generation support only, not ambiguity detection.
After a semantic clarification is answered, its schema-validated qualified
columns pin their tables in the linked context. This happens only after the
user answers, so pre-SQL findings do not steer initial candidate generation.

### Component D: Application and GUI

ApplicationService runs semantic analysis, parallel candidate generation and
execution, unified ambiguity judging, fallbacks, and the clarification loop.
Only one two-option question is shown per round. The default three-iteration
limit permits at most two sequential questions.

On clarified rounds, the post-SQL judge classifies every executed alternative
against all settled answers. The application selects the highest-support
compliant alternative. If none comply, it regenerates one candidate batch with
binding clarification instructions; a second noncompliant or unverifiable
batch fails without returning SQL or rows. The final iteration therefore
returns a verified compliant result or an explicit failure, never an
unverified last-successful result.

## High-Level Flow

    User question
        -> pre-SQL semantic analysis
        -> generate and execute K candidates
        -> unified post-SQL ambiguity decision
        -> result, or one question with two options
        -> after an answer, compliance classification
        -> compliant result, one retry, or fail closed

Schema columns, types, semantic findings, and direct relationships feed the
unified decision as supporting evidence.

## Project Structure

    src/db_whisperer/
    |-- application/    workflow orchestration
    |-- etler/          CSV ingestion and relationship discovery
    |-- ambiguity/      semantic analysis and unified post-SQL judgment
    |-- querier/        SQL generation, linking, validation, and execution
    |-- gui/            Streamlit interface
    -- contracts.py     shared component data models

Evaluation V3 is isolated in benchmark_v3. Evaluation V2 and earlier join-path
experiments are preserved as historical artifacts and are not the current
executable evaluation.

## Key Constraints

- SQL execution remains read-only.
- API keys are never logged or persisted.
- Prompts and results may contain sensitive CSV values.
- Relationship metadata is advisory support, never standalone ambiguity proof.
- No production component enumerates relationship paths for ambiguity.
