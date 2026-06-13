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

The page provides a domain-neutral data sidebar, LLM configuration, status
indicators, and query input. It displays each ambiguity question with two
clickable answer options, then shows the final rows and SQL after a pass or
three iterations. Previous exchanges remain visible in a scrollable chat
window until the data source changes or the Streamlit session ends. A submitted
question is displayed before candidate generation begins.
