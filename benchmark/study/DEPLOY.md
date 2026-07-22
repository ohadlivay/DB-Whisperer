# Put the study online (public link anyone can answer)

The study app runs locally with `streamlit run`. To let anyone answer it from a
link, you host it on **Streamlit Community Cloud** (free) and point it at a
**results inbox** so answers survive — a hosted app's local files are wiped on
every restart, so without an inbox the data would vanish.

You need two free accounts and about 10 minutes. Claude wrote all the code; these
are the click-steps only you can do (they use *your* accounts).

---

## Step 1 — Make a results inbox (Formspree)

1. Sign up at <https://formspree.io> (free tier is fine for a pilot).
2. Create a new form. Copy its endpoint URL — it looks like
   `https://formspree.io/f/abcdwxyz`.
3. The first submission may need a one-time confirmation click from Formspree's
   email — do a test run (Step 5) and confirm it.

*(Any URL that accepts a JSON POST works — a Google Apps Script web app, a
serverless function, etc. Formspree is just the least setup.)*

## Step 2 — Put the code on GitHub

The app deploys from a GitHub repo, so push this branch:

```bash
git push -u origin claude/benchmark-mimic-hitl
```

## Step 3 — Deploy on Streamlit Community Cloud

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **New app** → pick this repo → branch **`claude/benchmark-mimic-hitl`**.
3. Set **Main file path** to `benchmark/study/study_app.py`.

## Step 4 — Add two settings (Secrets)

Before you click Deploy, open **Advanced settings → Secrets** and paste:

```toml
results_webhook = "https://formspree.io/f/abcdwxyz"   # your Step 1 URL
study_datasets = "BikeStores"                          # public link = shopping tasks only
```

- `results_webhook` — where each finished session is sent.
- `study_datasets` — leave it as `BikeStores` so the open link only serves the
  shopping tasks. **Remove this line** only if you want the medical (MIMIC) tasks
  public too — but those need clinically-trained raters to judge, so keep them
  for invited people instead.

## Step 5 — Deploy and share

Click **Deploy**. In a minute you get a public link like
`https://your-app.streamlit.app`. Open it yourself, click through once to confirm
a submission lands in Formspree (confirm the email if asked), then send the link
to anyone.

---

## Getting your data back for analysis

Each finished session arrives in Formspree as one submission. To score them:

1. In Formspree, export the form's submissions as **JSON** (e.g. `export.json`).
2. Rebuild the per-participant result files and run the analyzer:

   ```bash
   python benchmark/study/import_webhook.py export.json
   python benchmark/study/analyze.py
   ```

   The first command reconstructs `results/<id>.jsonl`; the second writes the
   `report.html` scorecard.

## Good to know

- **Only finished sessions are saved to the inbox** (one submission each). If
  someone quits halfway, their partial answers aren't uploaded.
- **Consent & ethics are still yours.** The app shows a consent checkbox, but a
  real public study may need ethics/IRB sign-off — especially before making any
  MIMIC (health-data) tasks public.
- **Don't ask participants to type real names** as their ID; anything works, and
  keeping it anonymous is the point.
- **Test locally with an inbox** by setting environment variables instead of
  Streamlit secrets:

  ```bash
  DB_WHISPERER_RESULTS_WEBHOOK="https://formspree.io/f/abcdwxyz" \
  DB_WHISPERER_STUDY_DATASETS="BikeStores" \
  streamlit run benchmark/study/study_app.py
  ```
