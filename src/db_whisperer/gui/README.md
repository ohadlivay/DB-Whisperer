# GUI

The GUI provides the Streamlit interface for DB Whisperer.

## Responsibilities

- Accept CSV uploads and natural-language questions.
- Display ingestion and schema information.
- Display query results and the SQL used to produce them.
- Preserve conversation history across queries and Streamlit reruns.

## Boundaries

The GUI delegates workflow decisions to the application component. It does not
generate SQL, judge ambiguity, or access OpenRouter directly.

## Run the Prototype

From the repository root:

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```

The bundled student-impact CSV is loaded whenever no user file is selected, so
the app is immediately queryable as a toy example. Uploading another CSV
replaces the example for the current session.

The page also provides preset models with relative cost and response-time
indicators, a custom model-ID option, and query input. Previous exchanges
remain visible in a scrollable chat window.
