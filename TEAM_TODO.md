# Team TODO

## 1. Multi-CSV Support - DONE

- Allow users to upload and ingest multiple CSV files in one session.
- Discover candidate primary keys and foreign keys to support querying over relational databases
- Store relationship metadata in shared contracts so ETL, query generation, ambiguity detection, and the GUI can use the same schema graph.
- Update prompts to describe tables, columns, and valid join paths clearly.

## 2. RAG For Large Context

- Add retrieval when database context grows beyond a configurable threshold `N`.
- Rank the most relevant tables, columns, samples, statistics, and relationships for the current user request.
- Send only the selected context to the LLM while preserving exact identifier rules.
- Add tests that compare small-context full prompts with large-context retrieved prompts.

## 3. Chat History In Prompts

- Preserve completed user/assistant turns as compact conversation context.
- Include prior user query, assistant summary, generated SQL, and key result columns in later prompts.
- Keep the latest user request authoritative so old context helps follow-ups without overriding new questions.
- Avoid sending full previous result tables unless a small preview is explicitly useful.

## 4. Reset Conversation Button

- Add a reset button that starts a fresh chat session without clearing settings.
- Keep API key, selected model, candidate count, and uploaded files.
- Clear chat history, active query, workflow result, clarifications, clarification history, and pending query state.
- Add Streamlit tests for reset behavior.
