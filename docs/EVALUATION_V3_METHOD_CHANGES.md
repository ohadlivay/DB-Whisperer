# Evaluation V3 Method Changes

## Why V2 no longer matches DB Whisperer

V2 described an earlier experiment and no longer reflects the deployed ambiguity workflow. V3 evaluates executed SQL alternatives and validated semantic-column evidence. No join path determines clarification, and alternate relationship routes are not a reason to interrupt the user.

## Experimental arms

The four fixed arms are Baseline, Candidate Only, Semantic Only, and Full System. The baseline is the single-pass LLM-to-SQL comparison. Candidate Only judges executed candidate SQL and results. Semantic Only uses validated semantic-column findings. Full System combines both signals using the production priority and plausibility gates.

## Test-suite redesign

The frozen V3 suite contains 22 query cases and 2 ETL fixtures (24 cases total) across direct answers, semantic ambiguity, candidate-derived ambiguity, clarification resolution, safety, and relational requests. Every arm sees the same query cases and database state; ETL fixtures are shared operational evidence.

## Ambiguity-funnel scoring

The funnel reports whether ambiguity was present, whether an interruption was justified, question quality, and resolution after the answer. Candidate support is confidence rather than a majority-vote rule. A single candidate outlier or an arbitrary union cannot pre-empt validated semantic evidence.

## Correctness and least-sufficient joins

Correctness is scored from the expected result and executable SQL evidence. Multi-table queries are assessed for least-sufficient joins: only the relationships needed to answer the request should be introduced. Direct relationship metadata can help SQL generation, but alternate graph routes never independently create ambiguity.

## K=3, five repetitions, and budget control

Baseline is a single-pass K=1 comparison. Candidate Only, Semantic Only, and Full System each produce K=3 SQL candidates; the complete compatible campaign is repeated five times. The planned campaign ceiling is $3.75; usage and retries are retained as campaign-global operational evidence when they cannot be attributed safely to an individual repetition.

## Faster campaign execution

Campaigns use two workers while generating the three candidates for a request in parallel. The database is prepared once per campaign and read-only queries run against the shared prepared state. This improves throughput without changing case order, scoring rules, or model provenance.

## Progress, checkpoints, and resume

The runner records progress and writes a checkpoint after every campaign cell, with immutable suite, model, and scoring fingerprints. Resume is permitted only when those fingerprints match; incomplete or incompatible runs are excluded from aggregation.

## Aggregation and publication

Aggregation uses only complete compatible repetitions, reports uncertainty from question-family bootstrap resampling, and keeps case-level SQL, results, scores, transcripts, candidate support, compliance, failures, and provenance. Publication produces exactly a one-page method summary and an eight-tab evidence report.

## Interpretation limits

The results estimate performance on the frozen suite, model, and database snapshot; they are not a general claim about every dataset or model. Year wording is explicitly disambiguated when it could mean a patient birth date versus an admission date. No conclusion relies on relationship-route multiplicity.
