"""Render a DBWhisperer MIMIC evaluation JSON report to HTML.

The renderer is intentionally static: it does not run the benchmark, call
OpenRouter, or read the database. It turns one saved `mimic_ab_*.json` artifact
into a nested docs page that follows the visual language of
`docs/db_whisperer_embedded_site.html`.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "evaluation_report.html"


def load_report(path: Path) -> dict[str, Any]:
    """Load one JSON report file."""
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Report must contain one JSON object.")
    return payload


def render_report(
    report: dict[str, Any],
    *,
    case_details_href: str = "evaluation_report_cases.html",
) -> str:
    """Render the public summary HTML report page."""
    title = "DB Whisperer Evaluation Summary"
    summary = report.get("summary", {})
    judge = report.get("judge", {})
    schema = report.get("schema", {})

    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en" dir="ltr">',
            "<head>",
            '  <meta charset="UTF-8" />',
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />',
            f"  <title>{title}</title>",
            "  <style>",
            stylesheet(),
            "  </style>",
            "</head>",
            "<body>",
            '  <div class="site">',
            render_header(),
            "    <main>",
            render_summary_header(report, summary),
            render_framework(report, schema, judge),
            render_metrics(summary),
            render_factor_overview(),
            render_case_details_link(report, case_details_href),
            render_discussion(report, summary),
            "    </main>",
            render_footer(),
            "  </div>",
            "</body>",
            "</html>",
            "",
        ]
    )


def render_case_details_report(report: dict[str, Any]) -> str:
    """Render a separate detailed case-results page."""
    title = "DB Whisperer Detailed Case Results"
    cases = report.get("cases", [])
    aggregate = is_aggregate_report(report)
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en" dir="ltr">',
            "<head>",
            '  <meta charset="UTF-8" />',
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />',
            f"  <title>{title}</title>",
            "  <style>",
            stylesheet(),
            "  </style>",
            "</head>",
            "<body>",
            '  <div class="site">',
            render_header(),
            "    <main>",
            """
      <section class="report-header">
        <div class="container">
          <p class="eyebrow">Detailed Results</p>
          <h2>Case-by-Case Evaluation Details</h2>
          <p class="subtitle">This page contains the full case table. It is separated from the summary because repeated evaluation runs can produce more than 100 case-level rows.</p>
        </div>
      </section>
            """,
            render_aggregate_case_table(cases)
            if aggregate
            else render_case_table(cases),
            render_source_runs(report) if aggregate else "",
            "    </main>",
            render_footer(),
            "  </div>",
            "</body>",
            "</html>",
            "",
        ]
    )


def stylesheet() -> str:
    """CSS aligned with the embedded project site."""
    return """
    :root {
      --primary: #2563eb;
      --primary-dark: #1e40af;
      --accent: #06b6d4;
      --bg: #f8fafc;
      --bg-soft: #eff6ff;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #64748b;
      --border: #dbeafe;
      --shadow: 0 18px 45px rgba(30, 64, 175, 0.12);
      --radius: 18px;
      --ok: #16a34a;
      --warn: #d97706;
      --bad: #dc2626;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      font-family: Inter, Arial, Helvetica, sans-serif;
      color: var(--text);
      background: linear-gradient(135deg, #eff6ff 0%, #ecfeff 45%, #ffffff 100%);
      line-height: 1.6;
      direction: ltr;
    }
    a { color: inherit; text-decoration: none; }
    .site { min-height: 100vh; }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(255, 255, 255, 0.86);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
    }
    .nav {
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
    }
    .brand-title {
      margin: 0;
      font-size: 25px;
      line-height: 1.1;
      font-weight: 800;
      background: linear-gradient(90deg, var(--primary), var(--accent));
      -webkit-background-clip: text;
      color: transparent;
    }
    .brand-subtitle { margin: 3px 0 0; font-size: 13px; color: var(--muted); }
    nav { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 18px; font-size: 14px; font-weight: 650; color: #475569; }
    nav a:hover { color: var(--primary); }
    section { padding: 72px 24px; }
    .container { max-width: 1180px; margin: 0 auto; }
    .report-header { padding: 68px 24px 48px; text-align: left; }
    .eyebrow { margin: 0 0 10px; color: var(--primary-dark); font-weight: 800; letter-spacing: .08em; text-transform: uppercase; font-size: 13px; }
    .report-header h2 {
      margin: 0 0 18px;
      max-width: 980px;
      font-size: clamp(34px, 5vw, 58px);
      line-height: 1.04;
      font-weight: 900;
      letter-spacing: -0.025em;
      background: linear-gradient(90deg, var(--primary), var(--accent), var(--primary-dark));
      -webkit-background-clip: text;
      color: transparent;
    }
    .subtitle { margin: 0 0 28px; max-width: 860px; font-size: clamp(17px, 2.1vw, 22px); color: #475569; font-weight: 550; }
    .section-title { text-align: center; margin: 0 0 40px; }
    .section-title h3 { margin: 0 0 12px; font-size: clamp(30px, 4vw, 44px); line-height: 1.1; letter-spacing: -0.03em; }
    .section-title p { margin: 0 auto; max-width: 820px; color: var(--muted); font-size: 17px; }
    .grid { display: grid; gap: 22px; }
    .grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .card {
      background: rgba(255, 255, 255, .9);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: 0 10px 28px rgba(30, 64, 175, .08);
      padding: 24px;
    }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 10px 18px;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--primary), var(--accent));
      color: white;
      font-weight: 800;
      box-shadow: 0 12px 26px rgba(37, 99, 235, .18);
    }
    .button.secondary {
      color: var(--primary-dark);
      background: #ffffff;
      border: 1px solid var(--border);
      box-shadow: none;
    }
    .metric { min-height: 150px; }
    .metric-label { margin: 0 0 8px; color: var(--muted); font-size: 13px; font-weight: 800; text-transform: uppercase; letter-spacing: .07em; }
    .metric-value { margin: 0; font-size: 40px; line-height: 1; font-weight: 900; color: var(--primary-dark); }
    .metric-note { margin: 12px 0 0; color: #475569; font-size: 14px; }
    .bar-row { margin: 18px 0; }
    .bar-label { display: flex; justify-content: space-between; gap: 12px; font-weight: 750; color: #334155; margin-bottom: 8px; }
    .bar-track { height: 14px; border-radius: 99px; background: #e2e8f0; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 99px; background: linear-gradient(90deg, var(--primary), var(--accent)); }
    .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); background: rgba(255,255,255,.9); box-shadow: 0 10px 28px rgba(30, 64, 175, .07); }
    table { width: 100%; border-collapse: collapse; min-width: 980px; }
    th, td { padding: 13px 14px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; font-size: 14px; }
    th { color: #334155; background: #eff6ff; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
    tr:last-child td { border-bottom: 0; }
    code, .code {
      font-family: Consolas, Monaco, monospace;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
      color: #0f172a;
    }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 800; background: #e0f2fe; color: #075985; }
    .pill.ok { background: #dcfce7; color: #166534; }
    .pill.warn { background: #fef3c7; color: #92400e; }
    .pill.bad { background: #fee2e2; color: #991b1b; }
    .muted { color: var(--muted); }
    .footer { padding: 56px 24px; text-align: center; background: #0f172a; color: #e2e8f0; }
    .footer h3 { margin: 0 0 8px; font-size: 28px; }
    .footer p { margin: 0; color: #94a3b8; }
    @media (max-width: 900px) {
      .nav { flex-direction: column; align-items: flex-start; }
      nav { justify-content: flex-start; }
      .grid-4, .grid-3, .grid-2 { grid-template-columns: 1fr; }
      section { padding: 56px 18px; }
      .report-header { padding-top: 54px; }
    }
    """


def render_header() -> str:
    return """
    <header>
      <div class="nav">
        <div>
          <h1 class="brand-title">DB Whisperer</h1>
          <p class="brand-subtitle">Evaluation Results</p>
        </div>
        <nav>
          <a href="db_whisperer_embedded_site.html">Project Site</a>
          <a href="#framework">Framework</a>
          <a href="#results">Results</a>
          <a href="#cases">Cases</a>
          <a href="#discussion">Discussion</a>
        </nav>
      </div>
    </header>
    """


def render_summary_header(report: dict[str, Any], summary: dict[str, Any]) -> str:
    total = value(summary, "total_cases", 0)
    model = text(report.get("tested_model", "unknown model"))
    aggregate = is_aggregate_report(report)
    run_count = value(report, "run_count", 1)
    eyebrow = "Aggregate Evaluation Summary" if aggregate else "Evaluation Summary"
    subtitle = (
        "This page summarizes repeated simulated evaluation runs for DB Whisperer. "
        "The headline metrics are aggregated across all included runs."
        if aggregate
        else "This page summarizes simulated evaluation results for DB Whisperer. "
        "The system is compared with a simpler baseline that answers questions "
        "without asking for clarification."
    )
    total_note = (
        f"{run_count} run(s) across the evaluation cases"
        if aggregate
        else "Across the evaluation runs included in this report"
    )
    return f"""
      <section class="report-header" id="top">
        <div class="container">
          <p class="eyebrow">{eyebrow}</p>
          <h2>How Well DB Whisperer Handles Ambiguous Database Questions</h2>
          <p class="subtitle">{text(subtitle)}</p>
          <div class="grid grid-2">
            <article class="card metric">
              <p class="metric-label">Model</p>
              <p class="metric-note code">{model}</p>
            </article>
            <article class="card metric">
              <p class="metric-label">Total Case Results</p>
              <p class="metric-value">{total}</p>
              <p class="metric-note">{text(total_note)}</p>
            </article>
          </div>
        </div>
      </section>
    """


def render_framework(
    report: dict[str, Any],
    schema: dict[str, Any],
    judge: dict[str, Any],
) -> str:
    aggregate = is_aggregate_report(report)
    if aggregate:
        enabled_count = value(judge, "enabled_run_count", 0)
        disabled_count = value(judge, "disabled_run_count", 0)
        judge_text = f"{enabled_count} enabled / {disabled_count} disabled run(s)"
        self_judged = "yes" if judge.get("all_self_judged") else "no"
        judge_model = ", ".join(judge.get("models", [])) or "not used"
        relationship_text = (
            f"{value(schema, 'relationship_count_min', 'n/a')}-"
            f"{value(schema, 'relationship_count_max', 'n/a')}"
        )
        discovery_text = (
            f"{value(schema, 'discovery_complete_run_count', 0)} complete run(s)"
        )
    else:
        judge_text = "enabled" if judge.get("enabled") else "disabled"
        self_judged = "yes" if judge.get("self_judged") else "no"
        judge_model = text(judge.get("model", "not set"))
        relationship_text = text(value(schema, "relationship_count", 0))
        discovery_text = text(schema.get("discovery_complete", "unknown"))
    return f"""
      <section id="framework">
        <div class="container">
          <div class="section-title">
            <h3>Evaluation Framework</h3>
            <p>The same clinical database questions are answered in two ways: once by a simple no-clarification baseline, and once by the full DB Whisperer system. This shows whether asking clarification questions improves the final answer.</p>
          </div>
          <div class="grid grid-3">
            <article class="card">
              <h4>Baseline</h4>
              <p>The baseline answers immediately. It converts the user's question into a database query without first checking whether the question could mean more than one thing.</p>
            </article>
            <article class="card">
              <h4>DB Whisperer</h4>
              <p>The full system can detect ambiguous wording, ask a targeted clarification, and then generate a database query based on the clarified meaning.</p>
            </article>
            <article class="card">
              <h4>Scoring</h4>
              <p>Each test case has a reference answer query written by the evaluator. The generated answer is compared with that reference result. This automatic comparison is the main score.</p>
            </article>
          </div>
          <div class="grid grid-3" style="margin-top:22px">
            <article class="card">
              <h4>Schema</h4>
              <p><strong>{value(schema, "table_count", 0)}</strong> tables, <strong>{relationship_text}</strong> discovered relationships.</p>
              <p class="muted">Discovery complete: {discovery_text}</p>
            </article>
            <article class="card">
              <h4>Judge</h4>
              <p>Qualitative notes: <strong>{judge_text}</strong></p>
              <p>Same model judged itself: <strong>{self_judged}</strong></p>
              <p class="code">{judge_model}</p>
            </article>
            <article class="card">
              <h4>Dataset</h4>
              <p class="code">{text(report.get("dataset", ""))}</p>
            </article>
          </div>
        </div>
      </section>
    """


def render_metrics(summary: dict[str, Any]) -> str:
    baseline = summary.get("baseline", {})
    full = summary.get("full", {})
    ambiguous = summary.get("ambiguous", {})
    control = summary.get("control", {})
    comparison = summary.get("overall_comparison", {})
    baseline_pct = number(baseline.get("normalized_percentage"))
    full_pct = number(full.get("normalized_percentage"))
    clarification_rate = percent(ambiguous.get("expected_clarification_rate"))
    spurious_rate = percent(control.get("spurious_clarification_rate"))
    return f"""
      <section id="results">
        <div class="container">
          <div class="section-title">
            <h3>Results Overview</h3>
            <p>These headline metrics show answer correctness and whether the system asked clarification questions at the right time.</p>
          </div>
          <div class="grid grid-4">
            {metric_card("Baseline Correctness", baseline_pct + "%", "How often the immediate-answer approach matched the reference result.")}
            {metric_card("DB Whisperer Correctness", full_pct + "%", "How often the clarification-aware system matched the reference result.")}
            {metric_card("Useful Clarifications", clarification_rate, "How often DB Whisperer asked when a case was intentionally ambiguous.")}
            {metric_card("Unneeded Clarifications", spurious_rate, "How often DB Whisperer asked when the question was already clear.")}
          </div>
          <div class="grid grid-2" style="margin-top:24px">
            <article class="card">
              <h4>Score Comparison</h4>
              {bar("Baseline", baseline.get("normalized_percentage"))}
              {bar("Full Pipeline", full.get("normalized_percentage"))}
            </article>
            <article class="card">
              <h4>Win / Tie / Loss</h4>
              <p><span class="pill ok">DB Whisperer better</span> {value(comparison, "full_better", 0)}</p>
              <p><span class="pill">Tie</span> {value(comparison, "tie", 0)}</p>
              <p><span class="pill bad">Baseline better</span> {value(comparison, "baseline_better", 0)}</p>
              <p><span class="pill warn">Unscored</span> {value(comparison, "unscored", 0)}</p>
            </article>
          </div>
          <div class="grid grid-2" style="margin-top:24px">
            <article class="card">
              <h4>Score Stability</h4>
              <p>Baseline score spread: <strong>{score_spread(baseline)}</strong></p>
              <p>Full pipeline score spread: <strong>{score_spread(full)}</strong></p>
              <p class="muted">For aggregate reports, spread is the population standard deviation across all case results. For single-run reports it may be unavailable.</p>
            </article>
            <article class="card">
              <h4>Reliability</h4>
              <p>Cases with unreliable clarification simulation: <strong>{len(summary.get("unreliable_cases", []))}</strong></p>
              <p class="muted">Unreliable means the simulated user had to answer an unexpected clarification, repeated clarification, or unmatched option.</p>
            </article>
          </div>
        </div>
      </section>
    """


def render_factor_overview() -> str:
    factors = [
        (
            "Correctness",
            "Did the system return the same data as the reference answer?",
        ),
        (
            "Ambiguity Detection",
            "Did DB Whisperer notice when a question could have multiple valid meanings?",
        ),
        (
            "Clarification Quality",
            "Was the clarification question specific enough for a user to choose the intended meaning?",
        ),
        (
            "Unnecessary Interruptions",
            "Did the system avoid asking follow-up questions when the original question was already clear?",
        ),
        (
            "Safety",
            "Did the system avoid destructive database actions such as deleting or changing records?",
        ),
        (
            "Trust and Faithfulness",
            "Did the answer stay grounded in the returned data, without adding unsupported claims?",
        ),
    ]
    cards = "\n".join(
        f"""
            <article class="card">
              <h4>{text(title)}</h4>
              <p>{text(description)}</p>
            </article>
        """
        for title, description in factors
    )
    return f"""
      <section id="factors">
        <div class="container">
          <div class="section-title">
            <h3>What Was Evaluated</h3>
            <p>The evaluation looks beyond raw accuracy. It also checks whether the system asks useful questions, avoids unnecessary interruptions, and behaves safely.</p>
          </div>
          <div class="grid grid-3">
            {cards}
          </div>
        </div>
      </section>
    """


def render_case_details_link(report: dict[str, Any], href: str) -> str:
    total = len(report.get("cases", []))
    if is_aggregate_report(report):
        run_count = value(report, "run_count", 0)
        note = (
            f"This aggregate report contains {total} case summaries across "
            f"{run_count} run(s). The detail page shows per-case averages, "
            "clarification rates, reliability rates, and per-run scores."
        )
    else:
        note = (
            f"This report currently contains {total} case-level rows. A full "
            "10-run evaluation can contain more than 100 rows."
        )
    return f"""
      <section id="case-details">
        <div class="container">
          <div class="card">
            <h3 style="margin-top:0">Detailed Case Results</h3>
            <p>The main report keeps the overview readable. Detailed case-level rows are available separately, including generated queries, clarification choices, scores, and judge notes.</p>
            <p class="muted">{text(note)}</p>
            <a class="button" href="{text(href)}">View Detailed Case Results</a>
          </div>
        </div>
      </section>
    """


def render_case_table(cases: list[dict[str, Any]]) -> str:
    rows = "\n".join(render_case_row(case) for case in cases)
    return f"""
      <section id="cases">
        <div class="container">
          <div class="section-title">
            <h3>Per-Case Results</h3>
            <p>Each row keeps the deterministic score separate from clarification behavior and qualitative notes.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Type</th>
                  <th>Baseline</th>
                  <th>Full Pipeline</th>
                  <th>Comparison</th>
                  <th>Clarification</th>
                  <th>Qualitative Notes</th>
                </tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    """


def render_aggregate_case_table(cases: list[dict[str, Any]]) -> str:
    """Render per-case aggregate rows."""
    rows = "\n".join(render_aggregate_case_row(case) for case in cases)
    return f"""
      <section id="cases">
        <div class="container">
          <div class="section-title">
            <h3>Per-Case Aggregate Results</h3>
            <p>Each row summarizes the same case across all included evaluation runs.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Type</th>
                  <th>Baseline Avg</th>
                  <th>Full Avg</th>
                  <th>Win / Tie / Loss</th>
                  <th>Clarification</th>
                  <th>Reliability</th>
                </tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    """


def render_aggregate_case_row(case: dict[str, Any]) -> str:
    comparison = case.get("comparison", {})
    baseline = case.get("baseline", {})
    full = case.get("full", {})
    return f"""
                <tr>
                  <td><strong>{text(case.get("id", ""))}</strong><br><span class="muted">{text(case.get("question", ""))}</span></td>
                  <td>{pill(text(case.get("ambiguity_type", "none")))}</td>
                  <td><strong>{aggregate_score_text(baseline)}</strong><br><span class="muted">exact: {value(baseline, "exact_score_count", 0)}, zero: {value(baseline, "zero_score_count", 0)}</span></td>
                  <td><strong>{aggregate_score_text(full)}</strong><br><span class="muted">exact: {value(full, "exact_score_count", 0)}, zero: {value(full, "zero_score_count", 0)}</span></td>
                  <td>{comparison_counts_text(comparison)}</td>
                  <td><strong>{percent(case.get("clarification_rate"))}</strong><br><span class="muted">{value(case, "clarification_asked_count", 0)} of {value(case, "run_count", 0)} run(s)</span></td>
                  <td><strong>{percent(case.get("unreliable_rate"))}</strong><br><span class="muted">{value(case, "unreliable_count", 0)} unreliable run(s)</span></td>
                </tr>
    """


def render_case_row(case: dict[str, Any]) -> str:
    baseline_score = nested(case, "baseline", "deterministic_score", "score")
    full_score = nested(case, "full", "deterministic_score", "score")
    comparison = text(case.get("comparison", "unscored"))
    full = case.get("full", {})
    clarifications = full.get("clarifications", []) if isinstance(full, dict) else []
    judgment = case.get("qualitative_judgment", {})
    question = text(case.get("question", ""))
    baseline_sql = sql_snippet(nested(case, "baseline", "result", "sql"))
    full_sql = sql_snippet(nested(case, "full", "workflow", "query_result", "sql"))
    clarification_text = render_clarification_summary(clarifications)
    qualitative = render_qualitative(judgment)
    return f"""
                <tr>
                  <td><strong>{text(case.get("id", ""))}</strong><br><span class="muted">{question}</span></td>
                  <td>{pill(text(case.get("ambiguity_type", "none")))}</td>
                  <td><strong>{score_text(baseline_score)}</strong><br><span class="code">{baseline_sql}</span></td>
                  <td><strong>{score_text(full_score)}</strong><br><span class="code">{full_sql}</span></td>
                  <td>{pill(comparison, comparison_class(comparison))}</td>
                  <td>{clarification_text}</td>
                  <td>{qualitative}</td>
                </tr>
    """


def render_clarification_summary(clarifications: list[dict[str, Any]]) -> str:
    if not clarifications:
        return '<span class="muted">None</span>'
    first = clarifications[0]
    chosen = text(first.get("chosen", ""))
    mechanism = text(first.get("mechanism", ""))
    return (
        f"{pill(mechanism)}<br>"
        f"<strong>Chosen:</strong> {chosen}<br>"
        f"<span class=\"muted\">{len(clarifications)} clarification(s)</span>"
    )


def render_qualitative(judgment: dict[str, Any]) -> str:
    if not isinstance(judgment, dict) or not judgment:
        return '<span class="muted">Not available</span>'
    if judgment.get("status") == "judge_failure":
        return f"{pill('judge failure', 'bad')}<br>{text(judgment.get('error', ''))}"
    return (
        f"{pill(text(judgment.get('clarification_quality', 'n/a')))}<br>"
        f"{text(judgment.get('trust_note', ''))}<br>"
        f"<span class=\"muted\">{text(judgment.get('reason', ''))}</span>"
    )


def render_discussion(report: dict[str, Any], summary: dict[str, Any]) -> str:
    judge = report.get("judge", {})
    aggregate = is_aggregate_report(report)
    self_judged = bool(
        judge.get("all_self_judged") if aggregate else judge.get("self_judged")
    )
    limitation = (
        "The qualitative judge used the same model as the system, so those "
        "notes are non-independent and should be treated as supporting context."
        if self_judged
        else "The qualitative judge was configured separately from the tested model."
    )
    unreliable = summary.get("unreliable_cases", [])
    unreliable_text = (
        ", ".join(text(item) for item in unreliable)
        if unreliable
        else "None"
    )
    aggregate_note = (
        f"This report aggregates {value(report, 'run_count', 1)} separate "
        "evaluation run(s), so the headline metrics should be read as repeated-run "
        "averages rather than a single sample."
        if aggregate
        else "This report represents one evaluation run."
    )
    return f"""
      <section id="discussion">
        <div class="container">
          <div class="section-title">
            <h3>Discussion and Conclusions</h3>
            <p>The report is designed to separate executable correctness from interpretive and trust-oriented observations.</p>
          </div>
          <div class="grid grid-2">
            <article class="card">
              <h4>Interpretation</h4>
              <p>The central comparison is whether explicit ambiguity detection improves results on ambiguous MIMIC questions without adding unnecessary clarifications to control cases.</p>
              <p>{text(aggregate_note)}</p>
              <p>A reference answer query is a query written in advance by the evaluator. The benchmark runs that query and compares DB Whisperer's output to the reference result.</p>
              <p>Unreliable cases: <span class="code">{unreliable_text}</span></p>
            </article>
            <article class="card">
              <h4>Limitations</h4>
              <p>{text(limitation)}</p>
              <p>The automatic comparison is strict. If two answers are logically similar but formatted differently, the page may show that as a mismatch until a more flexible comparison layer is added.</p>
            </article>
          </div>
        </div>
      </section>
    """


def render_footer() -> str:
    return ""


def metric_card(label: str, value_text: str, note: str) -> str:
    return f"""
            <article class="card metric">
              <p class="metric-label">{text(label)}</p>
              <p class="metric-value">{text(value_text)}</p>
              <p class="metric-note">{text(note)}</p>
            </article>
    """


def bar(label: str, percentage: Any) -> str:
    pct = safe_percentage(percentage)
    return f"""
              <div class="bar-row">
                <div class="bar-label"><span>{text(label)}</span><span>{number(percentage)}%</span></div>
                <div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>
              </div>
    """


def score_spread(summary: dict[str, Any]) -> str:
    """Format population standard deviation when available."""
    spread = summary.get("population_stdev")
    if spread is None:
        return "n/a"
    return f"{number(spread)} points"


def aggregate_score_text(summary: dict[str, Any]) -> str:
    """Format aggregate score distribution."""
    mean = summary.get("mean")
    if mean is None:
        return "unscored"
    return f"{number(mean)}/4 avg"


def comparison_counts_text(comparison: dict[str, Any]) -> str:
    """Format aggregate win/tie/loss counts."""
    return (
        f"{pill('full better', 'ok')} {value(comparison, 'full_better', 0)}<br>"
        f"{pill('tie')} {value(comparison, 'tie', 0)}<br>"
        f"{pill('baseline better', 'bad')} {value(comparison, 'baseline_better', 0)}"
    )


def pill(label: str, class_name: str = "") -> str:
    cls = f"pill {class_name}".strip()
    return f'<span class="{cls}">{text(label)}</span>'


def comparison_class(value_text: str) -> str:
    if value_text == "full_better":
        return "ok"
    if value_text == "baseline_better":
        return "bad"
    if value_text == "unscored":
        return "warn"
    return ""


def score_text(score: Any) -> str:
    return "unscored" if score is None else f"{text(score)}/4"


def sql_snippet(sql: Any) -> str:
    if not sql:
        return "No accepted SQL"
    sql_text = str(sql)
    return text(sql_text if len(sql_text) <= 180 else sql_text[:177] + "...")


def value(payload: dict[str, Any], key: str, default: Any = "") -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


def nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def number(value_item: Any) -> str:
    if value_item is None:
        return "n/a"
    if isinstance(value_item, float):
        return f"{value_item:.2f}".rstrip("0").rstrip(".")
    return text(value_item)


def percent(value_item: Any) -> str:
    if value_item is None:
        return "n/a"
    try:
        return f"{float(value_item) * 100:.1f}%"
    except (TypeError, ValueError):
        return text(value_item)


def safe_percentage(value_item: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value_item)))
    except (TypeError, ValueError):
        return 0.0


def text(value_item: Any) -> str:
    return html.escape(str(value_item), quote=True)


def is_aggregate_report(report: dict[str, Any]) -> bool:
    """True when a report is a repeated-run aggregate artifact."""
    return report.get("report_type") == "mimic_ab_aggregate"


def render_source_runs(report: dict[str, Any]) -> str:
    """Render the list of source reports included in an aggregate."""
    runs = report.get("source_reports", [])
    if not isinstance(runs, list) or not runs:
        return ""
    rows = "\n".join(
        f"""
                <tr>
                  <td>{text(run.get("run_id", ""))}</td>
                  <td class="code">{text(run.get("path", ""))}</td>
                  <td>{text(run.get("started_at", ""))}</td>
                  <td>{text(run.get("completed_at", ""))}</td>
                </tr>
        """
        for run in runs
    )
    return f"""
      <section id="runs">
        <div class="container">
          <div class="section-title">
            <h3>Source Runs</h3>
            <p>These are the individual benchmark artifacts included in the aggregate report.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run ID</th>
                  <th>Report Path</th>
                  <th>Started</th>
                  <th>Completed</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>
      </section>
    """


def write_report(report_path: Path, output_path: Path) -> Path:
    """Render `report_path` to `output_path` and a sibling case-detail page."""
    report = load_report(report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    details_path = output_path.with_name(f"{output_path.stem}_cases.html")
    output_path.write_text(
        render_report(report, case_details_href=details_path.name),
        encoding="utf-8",
    )
    details_path.write_text(render_case_details_report(report), encoding="utf-8")
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a MIMIC evaluation JSON report to HTML.",
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = write_report(args.report, args.output)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Could not render report: {error}")
        return 2
    print(f"HTML report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
