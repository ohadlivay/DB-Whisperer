# Team TODO

## 1. Multi-CSV Support - DONE

- Allow users to upload and ingest multiple CSV files in one session.
- Discover candidate primary keys and foreign keys to support querying over relational databases
- Store relationship metadata in shared contracts so ETL, query generation, ambiguity detection, and the GUI can use the same schema graph.
- Update prompts to describe tables, columns, and valid join paths clearly.

## 2. Smarter RAG And Schema Linking

- Add retrieval when database context grows beyond a configurable threshold `N`.
- Add semantic schema linking that maps user terms and entities to likely tables, columns, and join paths.
- Rank the most relevant tables, columns, samples, statistics, and relationships for the current user request.
- Preserve enough connected schema context to avoid retrieving isolated tables that cannot form a valid query plan.
- Send only the selected context to the LLM while preserving exact identifier rules.
- Add tests that compare small-context full prompts with large-context retrieved prompts.

## 3. Evaluation And Benchmarking

- Create a versioned test set of natural-language queries paired with expected answers, SQL, relevant schema elements, and expected clarification behavior where applicable.
- Build a benchmarking runner that evaluates ETL metadata, schema linking, SQL generation, ambiguity detection, and end-to-end answer quality separately.
- Add deterministic execution-based metrics where possible and an LLM-as-a-judge rubric for semantic answer quality.
- Record aggregate scores, per-category failures, model configuration, latency, and token cost so experiments are reproducible and comparable.
- Compare the full ambiguity-aware pipeline against the single-pass baseline described in the research plan.

## 4. Chat UI/UX

- Improve the visual hierarchy and responsiveness of chat messages, schema browsing, result tables, SQL details, loading states, clarifications, and errors.
- Make long conversations and large result sets easier to navigate without hiding the active question or input.
- Add focused Streamlit tests for the main empty, loading, clarification, success, failure, and mobile-width interaction states.

## 5. Multi-Turn Chat Context - DONE

- Preserve completed user/assistant turns as compact conversation context.
- Include prior user query, assistant summary, generated SQL, and key result columns in later prompts.
- Support natural follow-up questions that refer to previous responses in the same session.
- Keep the latest user request authoritative so old context helps follow-ups without overriding new questions.
- Avoid sending full previous result tables unless a small preview is explicitly useful.

## 6. Reset Conversation Button - DONE

- Add a reset button that starts a fresh chat session without clearing settings.
- Keep API key, selected model, candidate count, and uploaded files.
- Clear chat history, active query, workflow result, clarifications, clarification history, and pending query state.
- Add Streamlit tests for reset behavior.

## 7. Optional External Persistence

- Evaluate an optional external database service such as Supabase/Postgres for persistent datasets, chat sessions, and benchmark results.
- Keep local DuckDB as the default and make remote persistence opt-in.
- Define tenant isolation, retention, deletion, migration, and access-control behavior before storing user data remotely.
- Keep API keys and other secrets out of persisted records and application logs.
