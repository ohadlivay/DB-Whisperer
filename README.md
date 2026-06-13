# DB Whisperer

Explore your database with ease! simply upload a CSV and start chatting. The app is designed to handle relational databases and allows you to specify ambiguous queries

## Live Demo

[Open DB Whisperer](https://db-whisperer.streamlit.app/)

## How to Use

1. Get an API key at [OpenRouter](https://openrouter.ai/keys).
2. Open the live demo and upload your CSV files.
3. Start chatting.

The API key is sent to OpenRouter for the current session and is not written to
the application logs.

## Run Locally

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL shown by Streamlit.

## Technology

Python, Streamlit, DuckDB, and OpenRouter.

For component responsibilities and data flow, see
[ARCHITECTURE.md](ARCHITECTURE.md).
