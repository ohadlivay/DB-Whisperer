"""Aggregate human-in-the-loop study results into metrics and an HTML report.

This is the analysis half of Protocol 1 (``HUMAN_IN_THE_LOOP.md``): the study GUI
(``study_app.py``) writes one JSON line per task to
``results/<participant_id>.jsonl``; this module reads those files back and
computes the aggregate metrics the protocol defines, then renders a standalone,
shareable HTML report -- the human-study analogue of what ``ab_run.py`` produces
for the automated arm.

What it reports, split by ambiguous vs control tasks (because the asking arm is
expected to *help* on ambiguous questions and to *not hurt* on unambiguous ones):

* intent-match accuracy, asking vs direct, and the delta;
* clarification comprehension rate (did the clicked option match the goal),
  overall and per dataset;
* mean trust, and the trust delta (asking - direct);
* mean clarity and naturalness of the clarifying question, per dataset;
* median time on task per version.

It deliberately does not invent measures the GUI does not record. The protocol's
forced-choice *preference* metric is not collected by the current app, and
control tasks do not ask a spurious question by default, so the cost of an
unnecessary question is not measured -- both are surfaced in the report rather
than silently omitted or faked.

Result files hold de-identified demo data and participant ratings, no API keys,
but treat the whole results directory and any generated report as sensitive.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
from typing import Any, Iterable


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = STUDY_DIR / "results"

TASK = "task"
SESSION_START = "session_start"
SESSION_END = "session_end"
VERSION_ASKING = "asking"
VERSION_DIRECT = "direct"

# Below this many completed participants, per-cell deltas are descriptive noise,
# not evidence; the report says so rather than implying a finding.
SMALL_N_THRESHOLD = 12


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _classify(record: dict[str, Any]) -> str:
    """Best-effort record type, tolerant of a missing ``type`` field."""
    kind = record.get("type")
    if kind in (TASK, SESSION_START, SESSION_END):
        return kind
    if "task_id" in record and "version" in record:
        return TASK
    return "unknown"


def load_records(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], int]:
    """Read JSONL result files into a flat record list.

    Returns the parsed records and a count of malformed lines. A single
    unparseable line is counted and skipped rather than aborting the whole
    analysis, so one truncated session file never hides every other result; the
    count is reported so the loss is visible.
    """
    records: list[dict[str, Any]] = []
    malformed = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                malformed += 1
    return records, malformed


# --------------------------------------------------------------------------
# Aggregation (pure: derives everything from the record list)
# --------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _rate_cell(items: list[dict[str, Any]], field: str = "correct") -> dict[str, Any]:
    """{n, correct, rate} over a boolean field; rate is None when empty.

    Items whose field is null are excluded from both numerator and denominator,
    so an unscored task never dilutes the rate. ``field`` lets the same helper
    serve intent-match accuracy (``correct``) and clarification comprehension
    (``comprehension`` — did the clicked option match the goal), which are
    distinct measures even where they happen to coincide.
    """
    considered = [item for item in items if item.get(field) is not None]
    n = len(considered)
    correct = sum(1 for item in considered if item.get(field))
    return {"n": n, "correct": correct, "rate": _round(correct / n) if n else None}


def _mean_cell(items: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """{n, mean} over a numeric rating field, ignoring null/unrated entries."""
    values = [
        float(item[field])
        for item in items
        if isinstance(item.get(field), (int, float))
    ]
    return {"n": len(values), "mean": _round(_mean(values), 2)}


def _delta(higher: float | None, lower: float | None) -> float | None:
    """A - B, only when both sides exist; None keeps an unknown honestly unknown."""
    if higher is None or lower is None:
        return None
    return _round(higher - lower)


def _by_dataset_rate(
    items: list[dict[str, Any]], field: str = "correct"
) -> dict[str, dict[str, Any]]:
    datasets = sorted({item.get("dataset", "?") for item in items})
    return {
        ds: _rate_cell(
            [item for item in items if item.get("dataset") == ds], field
        )
        for ds in datasets
    }


def _by_dataset_mean(
    items: list[dict[str, Any]], field: str
) -> dict[str, dict[str, Any]]:
    datasets = sorted({item.get("dataset", "?") for item in items})
    return {
        ds: _mean_cell([item for item in items if item.get("dataset") == ds], field)
        for ds in datasets
    }


def aggregate(
    records: list[dict[str, Any]], malformed_lines: int = 0
) -> dict[str, Any]:
    """Compute every study metric from a flat record list.

    Pure and side-effect free so it can be unit tested directly on synthetic
    records. Empty groups yield ``None`` rates/means and ``None`` deltas rather
    than a misleading zero, and every group reports its own ``n`` so a headline
    number is never read without the sample behind it.
    """
    tasks = [r for r in records if _classify(r) == TASK]
    starts = [r for r in records if _classify(r) == SESSION_START]
    ends = [r for r in records if _classify(r) == SESSION_END]
    unknown = sum(1 for r in records if _classify(r) == "unknown")

    started_ids = [r.get("participant_id") for r in starts]
    completed_ids = {r.get("participant_id") for r in ends}
    task_ids = {r.get("participant_id") for r in tasks}
    all_ids = {i for i in started_ids if i is not None} | task_ids
    incomplete = sorted(str(i) for i in all_ids - completed_ids)
    duplicates = sorted(
        str(pid)
        for pid in {i for i in started_ids if i is not None}
        if started_ids.count(pid) > 1
    )

    def cell(version: str, ambiguous: bool) -> list[dict[str, Any]]:
        return [
            t
            for t in tasks
            if t.get("version") == version and bool(t.get("ambiguous")) == ambiguous
        ]

    amb_ask, amb_dir = cell(VERSION_ASKING, True), cell(VERSION_DIRECT, True)
    ctl_ask, ctl_dir = cell(VERSION_ASKING, False), cell(VERSION_DIRECT, False)

    acc_amb_ask, acc_amb_dir = _rate_cell(amb_ask), _rate_cell(amb_dir)
    acc_ctl_ask, acc_ctl_dir = _rate_cell(ctl_ask), _rate_cell(ctl_dir)

    trust_amb_ask = _mean_cell(amb_ask, "trust")
    trust_amb_dir = _mean_cell(amb_dir, "trust")
    trust_ctl_ask = _mean_cell(ctl_ask, "trust")
    trust_ctl_dir = _mean_cell(ctl_dir, "trust")

    # Comprehension is only defined where a question was actually asked.
    asked = [t for t in tasks if t.get("asked")]
    unrated_trust = sum(
        1 for t in tasks if not isinstance(t.get("trust"), (int, float))
    )

    caveats: list[str] = []
    if len(completed_ids - {None}) < SMALL_N_THRESHOLD:
        caveats.append(
            f"Only {len(completed_ids - {None})} participant(s) completed the "
            f"study (< {SMALL_N_THRESHOLD}); treat all deltas as descriptive, "
            "not statistically supported. Pre-register and use paired tests "
            "once N grows (see HUMAN_IN_THE_LOOP.md)."
        )
    caveats.append(
        "Control tasks answer directly in both versions, so the control trust "
        "delta reflects noise, not the annoyance of an unnecessary question: "
        "the spurious-clarification cost is not measured by default."
    )
    if incomplete:
        caveats.append(
            f"{len(incomplete)} participant(s) have no session_end record "
            "(abandoned or interrupted); their completed tasks are still counted."
        )
    if unrated_trust:
        caveats.append(
            f"{unrated_trust} task record(s) have no trust rating and are "
            "excluded from trust means."
        )
    if malformed_lines:
        caveats.append(
            f"{malformed_lines} result line(s) could not be parsed and were "
            "skipped."
        )

    return {
        "participants": {
            "total": len(all_ids),
            "completed": len(completed_ids - {None}),
            "incomplete": incomplete,
        },
        "tasks": {
            "total": len(tasks),
            "unrated_trust": unrated_trust,
            "by_cell": {
                "ambiguous": {"asking": len(amb_ask), "direct": len(amb_dir)},
                "control": {"asking": len(ctl_ask), "direct": len(ctl_dir)},
            },
        },
        "accuracy": {
            "ambiguous": {
                "asking": acc_amb_ask,
                "direct": acc_amb_dir,
                "delta": _delta(acc_amb_ask["rate"], acc_amb_dir["rate"]),
            },
            "control": {
                "asking": acc_ctl_ask,
                "direct": acc_ctl_dir,
                "delta": _delta(acc_ctl_ask["rate"], acc_ctl_dir["rate"]),
            },
        },
        "comprehension": {
            "overall": _rate_cell(asked, "comprehension"),
            "by_dataset": _by_dataset_rate(asked, "comprehension"),
        },
        "trust": {
            "ambiguous": {
                "asking": trust_amb_ask,
                "direct": trust_amb_dir,
                "delta": _delta(trust_amb_ask["mean"], trust_amb_dir["mean"]),
            },
            "control": {
                "asking": trust_ctl_ask,
                "direct": trust_ctl_dir,
                "delta": _delta(trust_ctl_ask["mean"], trust_ctl_dir["mean"]),
            },
        },
        "clarity": {
            "overall": _mean_cell(asked, "clarity"),
            "by_dataset": _by_dataset_mean(asked, "clarity"),
        },
        "naturalness": {
            "overall": _mean_cell(asked, "naturalness"),
            "by_dataset": _by_dataset_mean(asked, "naturalness"),
        },
        "timing_seconds": {
            "asking": {
                "n": len(amb_ask + ctl_ask),
                "median": _round(
                    _median(
                        [
                            float(t["elapsed_seconds"])
                            for t in amb_ask + ctl_ask
                            if isinstance(t.get("elapsed_seconds"), (int, float))
                        ]
                    ),
                    2,
                ),
            },
            "direct": {
                "n": len(amb_dir + ctl_dir),
                "median": _round(
                    _median(
                        [
                            float(t["elapsed_seconds"])
                            for t in amb_dir + ctl_dir
                            if isinstance(t.get("elapsed_seconds"), (int, float))
                        ]
                    ),
                    2,
                ),
            },
        },
        "data_quality": {
            "malformed_lines": malformed_lines,
            "unknown_records": unknown,
            "duplicate_participant_ids": duplicates,
        },
        "not_collected": [
            "Forced-choice preference (“which did you trust more?”) is "
            "not recorded by the study app, so net preference cannot be reported."
        ],
        "caveats": caveats,
    }


# --------------------------------------------------------------------------
# HTML report (standalone; can later be nested into the docs site)
# --------------------------------------------------------------------------


def _pct(rate: float | None) -> str:
    return f"{rate * 100:.1f}%" if rate is not None else "n/a"


def _num(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _signed_pct(delta: float | None) -> str:
    if delta is None:
        return "n/a"
    return f"{delta * 100:+.1f} pts"


def _signed_num(delta: float | None) -> str:
    return f"{delta:+.2f}" if delta is not None else "n/a"


def _bar(rate: float | None) -> str:
    width = 0 if rate is None else max(0.0, min(1.0, rate)) * 100
    return f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'


def _card(label: str, value: str, sub: str) -> str:
    return (
        '<div class="card">'
        f'<div class="card-value">{html.escape(value)}</div>'
        f'<div class="card-label">{html.escape(label)}</div>'
        f'<div class="card-sub">{html.escape(sub)}</div>'
        "</div>"
    )


def render_html(summary: dict[str, Any], generated_at: str) -> str:
    """Render the aggregate summary as a self-contained HTML page.

    Every headline number is shown next to its ``n``, and the caveats the
    aggregation surfaced are rendered as a first-class section, so a reader can
    never take a delta from a two-person pilot as a settled result.
    """
    acc = summary["accuracy"]
    trust = summary["trust"]
    comp = summary["comprehension"]["overall"]
    parts = summary["participants"]

    def cell_line(cell: dict[str, Any], kind: str) -> str:
        if kind == "rate":
            return f"{_pct(cell['rate'])} ({cell['correct']}/{cell['n']})"
        return f"{_num(cell['mean'])} (n={cell['n']})"

    cards = "".join(
        [
            _card(
                "Accuracy lift on ambiguous tasks",
                _signed_pct(acc["ambiguous"]["delta"]),
                f"asking {_pct(acc['ambiguous']['asking']['rate'])} vs "
                f"direct {_pct(acc['ambiguous']['direct']['rate'])}",
            ),
            _card(
                "Clarification comprehension",
                _pct(comp["rate"]),
                f"{comp['correct']}/{comp['n']} clicked the option matching "
                "their goal",
            ),
            _card(
                "Trust lift on ambiguous tasks",
                _signed_num(trust["ambiguous"]["delta"]),
                f"asking {_num(trust['ambiguous']['asking']['mean'])} vs "
                f"direct {_num(trust['ambiguous']['direct']['mean'])} (1-5)",
            ),
            _card(
                "Participants",
                str(parts["completed"]),
                f"completed of {parts['total']} started",
            ),
        ]
    )

    def accuracy_rows() -> str:
        rows = []
        for label, key in (("Ambiguous", "ambiguous"), ("Control", "control")):
            block = acc[key]
            rows.append(
                "<tr>"
                f"<td>{label}</td>"
                f"<td>{cell_line(block['asking'], 'rate')}{_bar(block['asking']['rate'])}</td>"
                f"<td>{cell_line(block['direct'], 'rate')}{_bar(block['direct']['rate'])}</td>"
                f"<td class='delta'>{_signed_pct(block['delta'])}</td>"
                "</tr>"
            )
        return "".join(rows)

    def trust_rows() -> str:
        rows = []
        for label, key in (("Ambiguous", "ambiguous"), ("Control", "control")):
            block = trust[key]
            rows.append(
                "<tr>"
                f"<td>{label}</td>"
                f"<td>{cell_line(block['asking'], 'mean')}</td>"
                f"<td>{cell_line(block['direct'], 'mean')}</td>"
                f"<td class='delta'>{_signed_num(block['delta'])}</td>"
                "</tr>"
            )
        return "".join(rows)

    def dataset_rows() -> str:
        clarity = summary["clarity"]["by_dataset"]
        natural = summary["naturalness"]["by_dataset"]
        comp_ds = summary["comprehension"]["by_dataset"]
        datasets = sorted(set(clarity) | set(natural) | set(comp_ds))
        if not datasets:
            return "<tr><td colspan='4' class='muted'>No asked tasks yet.</td></tr>"
        rows = []
        for ds in datasets:
            c = comp_ds.get(ds, {"rate": None, "correct": 0, "n": 0})
            rows.append(
                "<tr>"
                f"<td>{html.escape(ds)}</td>"
                f"<td>{_pct(c['rate'])} ({c['correct']}/{c['n']})</td>"
                f"<td>{_num(clarity.get(ds, {}).get('mean'))}</td>"
                f"<td>{_num(natural.get(ds, {}).get('mean'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    caveats = "".join(
        f"<li>{html.escape(text)}</li>" for text in summary["caveats"]
    )
    not_collected = "".join(
        f"<li>{html.escape(text)}</li>" for text in summary["not_collected"]
    )
    timing = summary["timing_seconds"]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB Whisperer — Human-in-the-loop study report</title>
<style>
  :root {{
    --bg: #f6f7f9; --panel: #ffffff; --ink: #1b2130; --muted: #6b7280;
    --line: #e5e7eb; --accent: #2f6f4f; --accent-weak: #e7f1ec; --bar: #cbd5e1;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }}
  header h1 {{ margin: 0 0 4px; font-size: 24px; }}
  header p {{ margin: 0; color: var(--muted); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px; margin: 24px 0; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px; }}
  .card-value {{ font-size: 26px; font-weight: 650; color: var(--accent); }}
  .card-label {{ font-size: 13px; font-weight: 600; margin-top: 2px; }}
  .card-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px 20px; margin: 16px 0; }}
  section h2 {{ margin: 0 0 12px; font-size: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
    vertical-align: middle; font-size: 14px; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
    letter-spacing: .03em; }}
  td.delta {{ font-weight: 650; }}
  .bar {{ height: 6px; background: var(--accent-weak); border-radius: 4px; margin-top: 5px;
    overflow: hidden; }}
  .bar span {{ display: block; height: 100%; background: var(--accent); }}
  .muted {{ color: var(--muted); }}
  .caveats li {{ margin: 6px 0; }}
  footer {{ color: var(--muted); font-size: 12px; margin-top: 24px; text-align: center; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#12151b; --panel:#1a1f27; --ink:#e6e9ef; --muted:#9aa4b2;
      --line:#2a313c; --accent:#6fbf98; --accent-weak:#22302a; --bar:#3a424e; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Human-in-the-loop study report</h1>
    <p>DB Whisperer · does asking a clarifying question beat guessing? ·
       generated {html.escape(generated_at)}</p>
  </header>

  <div class="cards">{cards}</div>

  <section>
    <h2>Intent-match accuracy — did the final answer match the goal?</h2>
    <table>
      <thead><tr><th>Task type</th><th>Asking (full)</th><th>Direct (baseline)</th>
        <th>Delta</th></tr></thead>
      <tbody>{accuracy_rows()}</tbody>
    </table>
    <p class="muted" style="margin:10px 0 0;font-size:12px">
      The asking arm is expected to help on ambiguous tasks and roughly tie on
      control tasks.</p>
  </section>

  <section>
    <h2>Trust (1–5) — confidence the answer was what they wanted</h2>
    <table>
      <thead><tr><th>Task type</th><th>Asking (full)</th><th>Direct (baseline)</th>
        <th>Delta</th></tr></thead>
      <tbody>{trust_rows()}</tbody>
    </table>
  </section>

  <section>
    <h2>Clarification quality by dataset (asked tasks only)</h2>
    <table>
      <thead><tr><th>Dataset</th><th>Comprehension</th><th>Clarity (1–5)</th>
        <th>Naturalness (1–5)</th></tr></thead>
      <tbody>{dataset_rows()}</tbody>
    </table>
    <p class="muted" style="margin:10px 0 0;font-size:12px">
      Low naturalness flags an unnatural option (e.g. a MIMIC join path through
      an unrelated table) — the density finding the protocol predicts.</p>
  </section>

  <section>
    <h2>Time on task</h2>
    <p class="muted">Median seconds — asking:
      <strong>{_num(timing['asking']['median'])}</strong> (n={timing['asking']['n']}),
      direct: <strong>{_num(timing['direct']['median'])}</strong>
      (n={timing['direct']['n']}).</p>
  </section>

  <section>
    <h2>How to read this &amp; what is not measured</h2>
    <ul class="caveats">{caveats}</ul>
    <ul class="caveats">{not_collected}</ul>
  </section>

  <footer>Standalone report · can be nested into
    docs/db_whisperer_embedded_site.html when that work merges.</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _resolve_inputs(args: argparse.Namespace) -> list[Path]:
    if args.files:
        return [Path(f).expanduser().resolve() for f in args.files]
    results_dir = args.results_dir.expanduser().resolve()
    return sorted(results_dir.glob("*.jsonl"))


def _print_summary(summary: dict[str, Any]) -> None:
    parts = summary["participants"]
    acc = summary["accuracy"]["ambiguous"]
    trust = summary["trust"]["ambiguous"]
    comp = summary["comprehension"]["overall"]
    print(
        f"\nParticipants: {parts['completed']} completed / {parts['total']} started"
        f"   Tasks: {summary['tasks']['total']}"
    )
    print(
        "  Ambiguous accuracy: "
        f"asking {_pct(acc['asking']['rate'])} ({acc['asking']['correct']}/{acc['asking']['n']})"
        f"  vs direct {_pct(acc['direct']['rate'])} ({acc['direct']['correct']}/{acc['direct']['n']})"
        f"  delta {_signed_pct(acc['delta'])}"
    )
    print(
        f"  Comprehension: {_pct(comp['rate'])} ({comp['correct']}/{comp['n']})"
        f"   Trust delta (ambiguous): {_signed_num(trust['delta'])}"
    )
    for caveat in summary["caveats"]:
        print(f"  ! {caveat}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate human-in-the-loop study results into a report.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific result .jsonl files (default: every file in --results-dir).",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-html", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    inputs = _resolve_inputs(args)
    if not inputs:
        print(
            f"No result files found in {args.results_dir}. Run the study first "
            "(streamlit run benchmark/study/study_app.py).",
            file=sys.stderr,
        )
        return 1

    try:
        records, malformed = load_records(inputs)
    except OSError as error:
        print(f"Could not read results: {error}", file=sys.stderr)
        return 1

    summary = aggregate(records, malformed_lines=malformed)
    if summary["tasks"]["total"] == 0:
        print(
            "No task records found in the result files (only session markers?).",
            file=sys.stderr,
        )
        return 1

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    results_dir = args.results_dir.expanduser().resolve()
    out_json = (args.out_json or results_dir / "summary.json").expanduser().resolve()
    out_html = (args.out_html or results_dir / "report.html").expanduser().resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    out_json.write_text(
        json.dumps(
            {"generated_at": generated_at, "summary": summary},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    out_html.write_text(render_html(summary, generated_at), encoding="utf-8")

    _print_summary(summary)
    print(f"\nSummary JSON: {out_json}")
    print(f"HTML report: {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
