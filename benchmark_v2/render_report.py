"""Render the Evaluation V2 aggregate as public HTML pages."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sys
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from benchmark_v2.run_evaluation import PROJECT_ROOT


DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "evaluation_report.html"

ARM_DESCRIPTIONS = {
    "baseline": (
        "Primary control",
        "One direct text-to-SQL generation with no explicit ambiguity detector. "
        "This is the main reference point for measuring DBWhisperer.",
    ),
    "candidate_only": (
        "Candidate-count ablation",
        "Generates K SQL candidates but disables both schema-aware detectors. "
        "It isolates gains caused by multiple attempts from gains caused by ambiguity detection.",
    ),
    "join_only": (
        "Join-path ablation",
        "Enables join-path ambiguity detection but disables semantic-column detection. "
        "It measures the primary graph-based mechanism independently.",
    ),
    "semantic_only": (
        "Semantic-column ablation",
        "Enables semantic-column ambiguity detection but disables join-path detection. "
        "It measures the fallback mechanism independently.",
    ),
    "full": (
        "DBWhisperer treatment",
        "Runs K candidates with both join-path and semantic-column ambiguity detection. "
        "This is the complete framework compared with the baseline.",
    ),
}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def arm_label(arm: str) -> str:
    return arm.replace("_", " ").title()


def pass_rate(data: dict[str, Any]) -> tuple[int, int, float]:
    runs = data.get("runs", [])
    passed = sum(int(run.get("passed_cases", 0)) for run in runs)
    total = sum(int(run.get("case_count", 0)) for run in runs)
    return passed, total, round(100 * passed / total, 2) if total else 0.0


def render(aggregate: dict[str, Any], details_name: str) -> str:
    cards = "".join(
        f'<article class="card result-card{" featured" if arm == "full" else ""}"><span class="eyebrow">{esc(ARM_DESCRIPTIONS.get(arm, ("Experimental arm", ""))[0])}</span>'
        f'<h4>{esc(arm_label(arm))}</h4><strong>{esc(data["composite"]["mean"])} / 100</strong>'
        f'<p>Weighted composite score</p>'
        f'<p class="pass">Strict case pass rate: {pass_rate(data)[0]}/{pass_rate(data)[1]} ({pass_rate(data)[2]}%)</p></article>'
        for arm, data in aggregate.get("arms", {}).items()
    )
    rows = "".join(
        "<tr><th>" + esc(arm_label(arm)) + "</th>" + "".join(
            f"<td>{esc(data['components'][component]['mean'])}</td>"
            for component in ("ambiguity", "correctness", "efficiency", "etl", "safety", "grounding")
        ) + "</tr>"
        for arm, data in aggregate.get("arms", {}).items()
    )
    arm_rows = "".join(
        f"<tr><th>{esc(arm_label(arm))}</th><td>{esc(ARM_DESCRIPTIONS.get(arm, ('Experimental arm', ''))[0])}</td>"
        f"<td>{esc(ARM_DESCRIPTIONS.get(arm, ('', 'No description available.'))[1])}</td></tr>"
        for arm in aggregate.get("arms", {})
    )
    model = aggregate.get("model", "unknown")
    runs = aggregate.get("run_count", 0)
    k = aggregate.get("candidate_count", 0)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DBWhisperer Evaluation V2</title><style>
:root{{--primary:#2563eb;--primary-dark:#1e40af;--accent:#06b6d4;--accent-dark:#0891b2;--bg:#f8fafc;--bg-soft:#eff6ff;--card:#fff;--text:#1f2937;--muted:#64748b;--border:#dbeafe;--shadow:0 18px 45px rgba(30,64,175,.12);--radius:22px}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;font-family:Inter,Arial,Helvetica,sans-serif;color:var(--text);background:linear-gradient(135deg,#eff6ff 0%,#ecfeff 45%,#fff 100%);line-height:1.6}}a{{color:inherit;text-decoration:none}}
.site-header{{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.82);backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}}.nav{{max-width:1180px;margin:auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:18px}}.brand-title{{margin:0;font-size:25px;line-height:1.1;font-weight:800;background:linear-gradient(90deg,var(--primary),var(--accent));-webkit-background-clip:text;color:transparent}}.brand-subtitle{{margin:3px 0 0;font-size:13px;color:var(--muted)}}nav{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:18px;font-size:14px;font-weight:650;color:#475569}}nav a:hover{{color:var(--primary)}}
section{{padding:72px 24px}}.container{{max-width:1180px;margin:auto}}.hero{{padding:86px 24px 68px;text-align:center}}.hero h2{{margin:0 auto 18px;max-width:980px;font-size:clamp(42px,7vw,72px);line-height:1.02;font-weight:900;letter-spacing:-.055em;background:linear-gradient(90deg,var(--primary),var(--accent),var(--primary-dark));-webkit-background-clip:text;color:transparent}}.subtitle{{margin:0 0 28px;font-size:clamp(18px,2.5vw,24px);color:#475569;font-weight:550}}.hero-card{{max-width:940px;margin:0 auto 26px;padding:30px;border-radius:var(--radius);border:1px solid var(--border);background:rgba(255,255,255,.82);box-shadow:var(--shadow);text-align:left}}.hero-card h3{{margin:0 0 12px;color:var(--primary-dark);font-size:25px}}.hero-card p{{margin:0 0 12px;color:#334155;font-size:17px}}.process{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}}.process div{{padding:14px;border-radius:14px;background:var(--bg-soft);color:#334155;font-size:14px}}.process strong{{display:block;color:var(--primary);font-size:16px}}
.chips{{display:flex;flex-wrap:wrap;justify-content:center;gap:12px}}.chip{{display:inline-flex;align-items:center;padding:8px 15px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-weight:750;font-size:13px;border:1px solid #bfdbfe}}.chip:nth-child(even){{background:#cffafe;color:#0e7490;border-color:#a5f3fc}}.section-soft{{background:rgba(255,255,255,.48)}}.section-blue{{background:linear-gradient(120deg,rgba(239,246,255,.92),rgba(236,254,255,.92))}}.section-title{{text-align:center;margin:0 0 40px}}.section-title h3{{margin:0 0 12px;font-size:clamp(31px,4vw,45px);line-height:1.1;letter-spacing:-.03em}}.section-title p{{margin:0 auto;max-width:800px;color:var(--muted);font-size:17px}}
.grid{{display:grid;gap:22px}}.grid-2{{grid-template-columns:repeat(2,minmax(0,1fr))}}.grid-results{{grid-template-columns:repeat(auto-fit,minmax(205px,1fr))}}.card{{background:rgba(255,255,255,.86);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 10px 28px rgba(30,64,175,.08);padding:26px;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}}.card:hover{{transform:translateY(-3px);border-color:#93c5fd;box-shadow:0 18px 45px rgba(30,64,175,.14)}}.card h4{{margin:0 0 12px;font-size:22px;line-height:1.18;color:var(--primary-dark)}}.card p{{margin:0 0 14px;color:#475569}}.definition-card{{min-height:205px}}.definition-card .term{{display:inline-grid;width:45px;height:45px;margin-bottom:16px;place-items:center;border-radius:50%;color:#fff;background:linear-gradient(135deg,var(--primary),var(--accent));font-weight:900}}.result-card strong{{display:block;font-size:31px;color:var(--primary);line-height:1.2}}.result-card.featured{{border:2px solid #60a5fa;background:linear-gradient(150deg,#fff,#eff6ff)}}.eyebrow{{font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--accent-dark);font-weight:800}}.pass,.note{{color:var(--muted)!important;font-size:14px}}
.table-card{{padding:8px 22px 18px;overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:720px}}th,td{{padding:14px 12px;border-bottom:1px solid var(--border);text-align:right;vertical-align:top}}thead th{{color:var(--primary-dark);font-size:13px;text-transform:uppercase;letter-spacing:.04em}}th:first-child,td:first-child{{text-align:left}}.arms td:nth-child(2),.arms td:nth-child(3){{text-align:left}}.method-card p{{margin:0 0 13px;color:#475569}}code{{overflow-wrap:anywhere;background:#eff6ff;color:#1e40af;padding:2px 5px;border-radius:5px}}.button{{display:inline-flex;align-items:center;justify-content:center;padding:12px 18px;border-radius:12px;font-weight:800;font-size:14px;background:var(--primary);color:#fff}}.footer{{padding:58px 24px;color:#fff;background:linear-gradient(90deg,var(--primary-dark),var(--primary),var(--accent-dark));text-align:center}}.footer h3{{margin:0 0 10px;font-size:31px}}.footer p{{margin:0 0 24px;color:#dbeafe}}
@media(max-width:920px){{.site-header{{position:static}}.nav{{flex-direction:column;align-items:flex-start}}nav{{justify-content:flex-start}}.grid-2,.process{{grid-template-columns:1fr 1fr}}section{{padding:56px 18px}}.hero{{padding-top:62px}}}}@media(max-width:560px){{.grid-2,.process{{grid-template-columns:1fr}}.hero-card,.card{{padding:22px}}nav{{gap:12px;font-size:13px}}}}
</style></head><body><div class="site"><header class="site-header"><div class="nav"><div><h1 class="brand-title">DB Whisperer</h1><p class="brand-subtitle">Evaluation V2 &middot; Deterministic benchmark</p></div><nav><a href="#overview">Overview</a><a href="#concepts">Concepts</a><a href="#arms">Arms</a><a href="#results">Results</a><a href="#method">Method</a></nav></div></header><main>
<section id="overview" class="hero"><div class="container"><h2>DBWhisperer Evaluation V2</h2><p class="subtitle">Measuring whether explicit ambiguity detection improves schema-aware text-to-SQL.</p><article class="hero-card"><h3>How the evaluation works</h3><p>We used 16 database questions and ran each one through five system configurations. We repeated the experiment five times, creating 400 arm-case evaluations. Two additional ETL fixtures checked whether CSV files were turned into the correct database schema.</p><p>Every result was scored automatically—without a human or LLM judge. The benchmark executed the generated SQL and checked whether it answered the question, used the least sufficient joins, stayed read-only, and remained grounded in the loaded schema. Ambiguous questions also checked whether the system asked and resolved the right clarification.</p><div class="process"><div><strong>1. Ask</strong>Run the same questions</div><div><strong>2. Execute</strong>Validate and run SQL</div><div><strong>3. Measure</strong>Score six dimensions</div><div><strong>4. Compare</strong>Baseline versus Full</div></div></article><div class="chips"><span class="chip">{esc(runs)} runs</span><span class="chip">18 cases</span><span class="chip">400 evaluations</span><span class="chip">5 arms</span><span class="chip">{esc(model)}</span><span class="chip">K={esc(k)}</span></div></div></section>
<section id="concepts" class="section-soft"><div class="container"><div class="section-title"><h3>Arms and ablations</h3><p>Two related experiment terms that play different roles in this report.</p></div><div class="grid grid-2"><article class="card definition-card"><span class="term">A</span><h4>What is an arm?</h4><p>An arm is any complete system configuration tested in the experiment. Baseline, Candidate Only, Join Only, Semantic Only, and Full are all arms.</p><p><strong>Every ablation is an arm, but not every arm is an ablation.</strong></p></article><article class="card definition-card"><span class="term">−</span><h4>What is an ablation?</h4><p>An ablation is an arm where one or more components are disabled. It helps explain which component caused an improvement or regression.</p><p>Candidate Only, Join Only, and Semantic Only are the three diagnostic ablations.</p></article></div></div></section>
<section id="arms"><div class="container"><div class="section-title"><h3>Experimental arms</h3><p>Baseline versus Full answers the main research question. The ablations explain where the difference comes from.</p></div><div class="card table-card"><table class="arms"><thead><tr><th>Arm</th><th>Role</th><th>Purpose</th></tr></thead><tbody>{arm_rows}</tbody></table></div></div></section>
<section id="results" class="section-blue"><div class="container"><div class="section-title"><h3>Composite results</h3><p>The strict pass rate requires the complete case contract to pass. The weighted score also preserves partial evidence across the measured dimensions.</p></div><div class="grid grid-results">{cards}</div></div></section>
<section><div class="container"><div class="section-title"><h3>Component scores</h3><p>Scores are percentages. Ambiguity receives the largest weight because it is the primary research focus.</p></div><div class="card table-card"><table><thead><tr><th>Arm</th><th>Ambiguity</th><th>Correctness</th><th>Efficiency</th><th>Shared ETL</th><th>Safety</th><th>Grounding</th></tr></thead><tbody>{rows}</tbody></table></div></div></section>
<section id="method" class="section-soft"><div class="container"><div class="section-title"><h3>Detailed measurement method</h3><p>The exact deterministic rules behind the scores.</p></div><article class="card method-card">
<p><strong>SQL correctness (25%):</strong> generated SQL had to parse, execute, use the required schema scope, contain the required output concepts, and return a result compatible with the reference result. The SQL text did not need to match the reference SQL one-to-one.</p>
<p><strong>SQL efficiency (15%):</strong> scored only after semantic correctness passed. The parsed SQL join count was compared with the case's minimum required join count, rewarding the least sufficient join path.</p>
<p><strong>ETL (10%):</strong> deterministic fixture manifests checked tables, columns, row counts, and discovered relationships after CSV ingestion. ETL is shared because all query arms use the same constructed schema.</p>
<p><strong>Read-only safety (5%):</strong> a non-SELECT request passed only when the system returned no accepted executable SQL. Database row-count snapshots also had to remain unchanged.</p>
<p><strong>Schema grounding (5%):</strong> SQLGlot-extracted tables had to stay inside the loaded schema and satisfy the case's required and forbidden table constraints.</p>
<p><strong>Ambiguity handling (40%):</strong> combines detection recall, control specificity, correct mechanism, intended-option match, one-question resolution, and alignment of the final SQL with the selected interpretation.</p>
<p>The composite is <code>40% ambiguity + 25% correctness + 15% efficiency + 10% ETL + 5% safety + 5% grounding</code>. Scores summarize five repetitions.</p></article></div></section>
<section><div class="container"><div class="section-title"><h3>Ambiguity metric definitions</h3><p>How the ambiguity portion of the score is interpreted.</p></div><div class="card table-card"><table class="arms"><tbody><tr><th>Recall</th><td>Fraction of declared ambiguous cases where a clarification was asked.</td></tr><tr><th>Specificity</th><td>Fraction of matched unambiguous controls where no clarification was asked.</td></tr><tr><th>Mechanism accuracy</th><td>Whether the clarification came from the declared join-path or semantic-column mechanism.</td></tr><tr><th>Option match</th><td>Whether exactly one offered option matched the test case's declared interpretation tokens.</td></tr><tr><th>Resolution</th><td>Whether the intended option was matched and the case required only one clarification.</td></tr><tr><th>Final SQL alignment</th><td>Whether the resolved query also produced a deterministically compatible result.</td></tr></tbody></table></div></div></section>
</main><footer class="footer"><div class="container"><h3>Evaluation evidence</h3><p>Scoring was fully automatic and deterministic. No human evaluator or LLM judge was used.</p><a class="button" href="{esc(details_name)}">Open case-level evidence</a><p style="margin-top:24px;font-size:13px">Suite {esc(aggregate.get('suite_version', 'unknown'))} &middot; Campaign {esc(aggregate.get('campaign_id'))} &middot; Cost ${esc(aggregate.get('usage', {}).get('campaign_cost_usd', 0))}<br><code style="background:transparent;color:#dbeafe">{esc(aggregate.get('suite_hash'))}</code></p></div></footer></div></body></html>"""


def render_details(aggregate: dict[str, Any]) -> str:
    case_sections: list[str] = []
    for case in aggregate.get("cases", []):
        rows = "".join(
            f"<tr><th>{esc(arm_label(arm))}</th><td>{esc(values['pass_rate'])}%</td>"
            f"<td>{esc(values['correctness']['mean'])}</td><td>{esc(values['efficiency']['mean'])}</td>"
            f"<td>{esc(values['grounding']['mean'])}</td></tr>"
            for arm, values in case.get("arms", {}).items()
        )
        evidence_payload: dict[str, Any] = {}
        for arm, values in case.get("arms", {}).items():
            evidence_payload[arm] = {
                "pass_rate": values.get("pass_rate"),
                "runs": [
                    {
                        "repetition": index,
                        "state": (run.get("result") or {}).get("state"),
                        "sql": (run.get("result") or {}).get("sql"),
                        "columns": (run.get("result") or {}).get("columns", []),
                        "clarifications": run.get("clarifications", []),
                        "score": run.get("score", {}),
                        "duration_seconds": run.get("duration_seconds"),
                    }
                    for index, run in enumerate(values.get("runs", []), start=1)
                ],
            }
        evidence = esc(json.dumps(evidence_payload, indent=2, ensure_ascii=False))
        case_sections.append(
            f"<section><h2>{esc(case.get('case_id'))}</h2><p>{esc(case.get('category'))} &middot; family {esc(case.get('family_id'))}</p>"
            f"<table><thead><tr><th>Arm</th><th>Pass rate</th><th>Correctness</th><th>Efficiency</th><th>Grounding</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<details><summary>Run evidence, SQL, and clarifications</summary><pre>{evidence}</pre></details></section>"
        )
    return """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Evaluation V2 details</title>
<style>:root{--primary:#2563eb;--primary-dark:#1e40af;--accent:#06b6d4;--text:#1f2937;--muted:#64748b;--border:#dbeafe;--radius:22px}*{box-sizing:border-box}body{margin:0;font-family:Inter,Arial,Helvetica,sans-serif;color:var(--text);background:linear-gradient(135deg,#eff6ff 0%,#ecfeff 45%,#fff 100%);line-height:1.6}header{padding:58px 24px;text-align:center;background:rgba(255,255,255,.55);border-bottom:1px solid var(--border)}h1{margin:0 0 10px;font-size:clamp(38px,6vw,64px);letter-spacing:-.045em;background:linear-gradient(90deg,var(--primary),var(--accent),var(--primary-dark));-webkit-background-clip:text;color:transparent}header p{margin:0;color:var(--muted);font-size:17px}main{max-width:1180px;margin:auto;padding:36px 24px 72px}section{background:rgba(255,255,255,.88);border:1px solid var(--border);border-radius:var(--radius);padding:26px;margin:20px 0;box-shadow:0 10px 28px rgba(30,64,175,.08)}h2{margin:0 0 4px;color:var(--primary-dark)}section>p{margin-top:0;color:var(--muted)}table{width:100%;border-collapse:collapse;min-width:650px}section{overflow-x:auto}th,td{padding:12px;border-bottom:1px solid var(--border);text-align:right}th:first-child{text-align:left}details{margin-top:18px}summary{cursor:pointer;color:var(--primary);font-weight:800}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0f172a;color:#e2e8f0;padding:18px;border-radius:16px;font-size:13px}</style></head><body><header><h1>Evaluation V2 case details</h1><p>Generated SQL, clarification evidence, and deterministic scores for every case.</p></header><main>""" + "".join(case_sections) + "</main></body></html>"


def write_report(aggregate_path: Path, output: Path) -> tuple[Path, Path]:
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if aggregate.get("report_type") != "dbwhisperer_v2_aggregate":
        raise ValueError("Expected a V2 aggregate report")
    output.parent.mkdir(parents=True, exist_ok=True)
    details = output.with_name(f"{output.stem}_cases.html")
    output.write_text(render(aggregate, details.name), encoding="utf-8")
    details.write_text(render_details(aggregate), encoding="utf-8")
    return output, details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aggregate", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary, details = write_report(args.aggregate.resolve(), args.output.resolve())
    print(f"HTML report: {summary}\nCase details: {details}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
