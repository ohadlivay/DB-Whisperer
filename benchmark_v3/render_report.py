"""Render corrected Evaluation V3 summary and case-level HTML reports."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from benchmark_v3.aggregate_results import aggregate


ARMS = {
    "baseline": ("Primary control", "Direct text-to-SQL without an ambiguity layer."),
    "candidate_only": ("Candidate ablation", "Executed alternatives without semantic evidence."),
    "semantic_only": ("Semantic ablation", "Semantic evidence without candidate diversity."),
    "full": ("DBWhisperer treatment", "Candidate, semantic, schema, and compliance evidence."),
}
METRICS = {
    "detection_recall": "Detection recall",
    "intended_option_match": "Intended-option match",
    "compliant_resolution": "Compliant resolution",
}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def label(value: str) -> str:
    return value.replace("_", " ").title()


def render(report: dict[str, Any], details_name: str) -> str:
    retrospective = bool(report.get("retrospective"))
    score_adjective = "corrected " if retrospective else ""
    cards = "".join(
        f'<article class="card result-card{" featured" if arm == "full" else ""}">'
        f'<span class="eyebrow">{esc(ARMS[arm][0])}</span><h4>{esc(label(arm))}</h4>'
        f'<strong>{data["composite"]} / 100</strong><p>Equal-weight {score_adjective}composite</p>'
        f'<p class="note">Strict success: {data["passed"]}/{data["total"]}'
        + (f' · original: {data["original_passed"]}/{data["total"]}' if retrospective else "")
        + '</p></article>'
        for arm, data in report["arms"].items()
    )
    arm_rows = "".join(
        f"<tr><th>{label(arm)}</th><td>{esc(role)}</td><td>{esc(purpose)}</td></tr>"
        for arm, (role, purpose) in ARMS.items()
    )
    components = ("answer_correctness", "ambiguity_resolution", "control_specificity", "safety_behavior")
    component_rows = "".join(
        f"<tr><th>{label(arm)}</th>" + "".join(
            f"<td>{data['components'][component]}%<small class='denom'>{esc(data['component_counts'][component])}</small></td>" for component in components
        ) + "</tr>" for arm, data in report["arms"].items()
    )
    metric_rows = "".join(
        f"<tr><th>{esc(title)}</th>" + "".join(
            f"<td>{report['arms'][arm]['ambiguity_metrics'][metric]}%<small class='denom'>{report['arms'][arm]['ambiguity_counts'][metric]}</small></td>" for arm in report["arms"]
        ) + "</tr>" for metric, title in METRICS.items()
    )
    safety_rows = "".join(
        f"<tr><th>{label(arm)}</th><td>{data['safety_metrics']['containment']}%</td>"
        f"<td>{data['safety_metrics']['refusal_fidelity']}%</td></tr>"
        for arm, data in report["arms"].items()
    )
    mechanism_rows = "".join(
        f"<tr><th>{label(arm)}</th><td>{esc(json.dumps(data['mechanisms'], sort_keys=True))}</td></tr>"
        for arm, data in report["arms"].items()
    )
    analysis_title = "Retrospective analysis — no new model calls" if retrospective else "Final V3.1 evaluation"
    analysis_text = (
        "The raw outputs and recorded choices are preserved. Because scoring was revised after observing the run, this report is exploratory rather than independent confirmation."
        if retrospective else
        "Contracts were frozen before execution and every record was scored during the live campaign. No retrospective correction step was applied."
    )
    page_title = "Evaluation V3: corrected analysis" if retrospective else "Evaluation V3.1"
    subtitle = "The completed model run, rescored with general declarative contracts." if retrospective else "A single-pass, contract-scored hybrid ambiguity evaluation."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DBWhisperer {esc(page_title)}</title><style>{CSS}</style></head><body>
<header class="site-header"><div class="nav"><div><h1>DB Whisperer</h1><small>{esc(page_title)}</small></div><nav><a href="#overview">Overview</a><a href="#results">Results</a><a href="#metrics">Metrics</a><a href="#method">Method</a></nav></div></header><main>
<section id="overview" class="hero"><div class="container"><h2>{esc(page_title)}</h2><p class="subtitle">{esc(subtitle)}</p><article class="card warning"><h3>{esc(analysis_title)}</h3><p>{esc(analysis_text)}</p><p>There are {report['case_count']} intent contracts over {report['unique_prompt_count']} unique prompts. ETL is a shared prerequisite and no longer inflates every arm.</p></article><div class="chips"><span>{report['run_count']} repetitions</span><span>{report['case_count']} intent contracts</span><span>{report['unique_prompt_count']} unique prompts</span><span>{report['evaluation_count']} evaluations</span><span>ETL {report['etl']['passed']}/{report['etl']['total']}</span><span>Scoring {esc(report['scoring_version'])}</span></div></div></section>
<section><div class="container"><div class="title"><h3>Experimental arms</h3><p>The direct baseline remains visible separately from ambiguity capability.</p></div><div class="card table"><table><thead><tr><th>Arm</th><th>Role</th><th>Purpose</th></tr></thead><tbody>{arm_rows}</tbody></table></div></div></section>
<section id="results" class="soft"><div class="container"><div class="title"><h3>{"Corrected composite" if retrospective else "Composite results"}</h3><p>Fixed equal average of the four displayed components; weights were not tuned to arm rankings.</p></div><div class="grid">{cards}</div></div></section>
<section><div class="container"><div class="title"><h3>Component scores</h3><p>Counts and case evidence are available in the detailed report.</p></div><div class="card table"><table><thead><tr><th>Arm</th><th>Answer correctness</th><th>Ambiguity resolution</th><th>Control specificity</th><th>Safety behavior</th></tr></thead><tbody>{component_rows}</tbody></table></div></div></section>
<section id="metrics" class="soft"><div class="container"><div class="title"><h3>Ambiguity funnel</h3><p>Mechanism identity is diagnostic; it never gates a pass.</p></div><div class="card table"><table><thead><tr><th>Metric</th>{''.join(f'<th>{label(arm)}</th>' for arm in report['arms'])}</tr></thead><tbody>{metric_rows}</tbody></table></div></div></section>
<section><div class="container"><div class="title"><h3>Safety is two outcomes</h3><p>Containment measures harmful execution; refusal fidelity measures whether the unsafe request was actually refused.</p></div><div class="card table"><table><thead><tr><th>Arm</th><th>Containment</th><th>Refusal fidelity</th></tr></thead><tbody>{safety_rows}</tbody></table></div></div></section>
<section><div class="container"><div class="title"><h3>Observed mechanisms</h3><p>Descriptive clarification counts only.</p></div><div class="card table"><table><thead><tr><th>Arm</th><th>Mechanism counts</th></tr></thead><tbody>{mechanism_rows}</tbody></table></div></div></section>
<section id="method"><div class="container"><div class="title"><h3>Rating system</h3><p>One generic scorer applies case-declared contracts without case-ID branches.</p></div><div class="grid two"><article class="card"><h4>Equal-weight composite</h4><p><strong>25%</strong> answer correctness</p><p><strong>25%</strong> ambiguity resolution</p><p><strong>25%</strong> control specificity</p><p><strong>25%</strong> safety behavior</p><p class="note">Baseline ambiguity is N/A operationally and zero only in the product-capability composite.</p></article><article class="card"><h4>Result policies</h4><p><strong>Relational:</strong> declared tables, projections, predicates, joins, and aggregates.</p><p><strong>Scalar:</strong> exact value, alias-independent.</p><p><strong>Keyed rows:</strong> declared semantic columns only.</p><p><strong>Safety:</strong> containment and refusal are separate.</p></article></div></div></section>
</main><footer><h3>Case-level evidence</h3><p>Questions, intent contracts, generated SQL, and check-level failure reasons.</p><a href="{esc(details_name)}">{"Open corrected evidence" if retrospective else "Open case evidence"}</a><p class="note">Suite {esc(report['suite_hash'])}</p></footer></body></html>"""


def render_details(report: dict[str, Any], summary_name: str) -> str:
    sections = []
    for case in report["cases"]:
        contract = case.get("contract", {})
        rows = "".join(
            f"<tr><th>{label(arm)}</th><td>{data['passed']}/{data['total']}</td>"
            f"<td>{data['correctness']}%</td><td>{data['clarification_rate']}%</td>"
            f"<td>{data['resolution']}%</td><td>{esc(json.dumps(data['failure_reasons'], sort_keys=True))}</td></tr>"
            for arm, data in case["arms"].items()
        )
        evidence = {
            arm: [({
                "repetition": row.get("run"), "state": row.get("result", {}).get("state"),
                "sql": row.get("result", {}).get("sql"), "clarifications": row.get("clarifications", []),
                "score": row.get("score", {}),
            } | ({"original_score": row.get("original_score", {})} if report.get("retrospective") else {}))
            for row in data["runs"]] for arm, data in case["arms"].items()
        }
        sections.append(
            f'<section class="case"><span class="badge">{esc(case["category"])}</span><h2>{esc(case["case_id"])}</h2>'
            f'<p class="question">{esc(contract.get("question", ""))}</p><p>Intended interpretation: <strong>{esc(contract.get("intent_id", ""))}</strong> · policy: <strong>{esc(contract.get("result_policy", "relational"))}</strong></p>'
            f'<details><summary>Declarative case contract</summary><pre>{esc(json.dumps(contract, indent=2, ensure_ascii=False, default=str))}</pre></details>'
            f'<div class="table"><table><thead><tr><th>Arm</th><th>Strict</th><th>Correctness</th><th>Asked</th><th>Resolved</th><th>Failure reasons</th></tr></thead><tbody>{rows}</tbody></table></div>'
            f'<details><summary>Generated SQL and {"corrected " if report.get("retrospective") else ""}checks</summary><pre>{esc(json.dumps(evidence, indent=2, ensure_ascii=False))}</pre></details></section>'
        )
    heading = "Corrected V3 case evidence" if report.get("retrospective") else "V3.1 case evidence"
    description = "Retrospective contracts, original outputs, and check-level diagnostics." if report.get("retrospective") else "Frozen contracts, live outputs, and check-level diagnostics."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(heading)}</title><style>{CSS}</style></head><body><header class="detail-head"><a href="{esc(summary_name)}">← Summary report</a><h1>{esc(heading)}</h1><p>{esc(description)}</p></header><main class="container details">{''.join(sections)}</main></body></html>"""


def write_report(input_path: Path, output_path: Path) -> tuple[Path, Path]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if payload.get("report_type") in {"dbwhisperer_v3_evaluation", "dbwhisperer_v3_rescored"}:
        payload = aggregate([input_path])
    elif payload.get("report_type") != "dbwhisperer_v3_aggregate":
        raise ValueError("Expected a final V3.1 evaluation or legacy rescored V3 report.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    details_path = output_path.with_name(f"{output_path.stem}_cases.html")
    output_path.write_text(render(payload, details_path.name), encoding="utf-8")
    details_path.write_text(render_details(payload, output_path.name), encoding="utf-8")
    return output_path, details_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.report.with_name("evaluation_v3.html")
    summary, details = write_report(args.report.resolve(), output.resolve())
    print(f"HTML report: {summary}\nCase details: {details}")
    return 0


CSS = """
:root{--p:#2563eb;--d:#172554;--a:#06b6d4;--text:#1e293b;--muted:#64748b;--line:#dbeafe;--card:#ffffffed}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;font:16px/1.6 Inter,ui-sans-serif,system-ui,Arial;color:var(--text);background:linear-gradient(135deg,#eff6ff,#ecfeff 48%,#fff)}a{color:inherit;text-decoration:none}.site-header{position:sticky;top:0;z-index:10;background:#ffffffdc;backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}.nav{max-width:1180px;margin:auto;padding:15px 24px;display:flex;align-items:center;justify-content:space-between}.nav h1{margin:0;color:var(--p)}.nav small{color:var(--muted)}nav{display:flex;gap:18px;font-weight:750;color:#475569}.container{max-width:1180px;margin:auto}.hero,section{padding:68px 24px}.hero{text-align:center}.hero h2{margin:0;font-size:clamp(42px,7vw,70px);line-height:1.04;letter-spacing:-.05em;background:linear-gradient(90deg,var(--p),var(--a),var(--d));-webkit-background-clip:text;color:transparent}.subtitle{font-size:21px;color:#475569}.card,.case{background:var(--card);border:1px solid var(--line);border-radius:22px;box-shadow:0 14px 36px #1e40af14;padding:26px}.warning{max-width:940px;margin:28px auto;text-align:left;border-left:6px solid var(--a)}.warning h3{margin-top:0}.chips{display:flex;flex-wrap:wrap;justify-content:center;gap:10px}.chips span,.badge{padding:7px 13px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-weight:800;font-size:13px}.soft{background:#ffffff78}.title{text-align:center;margin-bottom:35px}.title h3{font-size:38px;margin:0}.title p,.note{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:20px}.two{grid-template-columns:repeat(2,minmax(0,1fr))}.result-card strong{font-size:31px;color:var(--p)}.result-card h4{font-size:22px;margin:5px 0}.featured{border:2px solid #60a5fa}.eyebrow{font-size:12px;text-transform:uppercase;color:#0891b2;font-weight:850}.denom{display:block;color:var(--muted);font-weight:500}.table{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:760px}th,td{padding:13px 11px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}th:first-child,td:first-child{text-align:left}thead th{color:var(--d);text-transform:uppercase;font-size:12px}footer,.detail-head{text-align:center;padding:55px 24px;color:#fff;background:linear-gradient(90deg,var(--d),var(--p),#0891b2)}footer a{display:inline-block;background:#fff;color:var(--d);padding:11px 17px;border-radius:12px;font-weight:850}.details{padding:20px 24px 70px}.case{margin:22px 0}.case h2{color:var(--d);margin:8px 0}.question{font-size:19px}details{margin:17px 0}summary{cursor:pointer;color:var(--p);font-weight:850}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:620px;overflow:auto;background:#0f172a;color:#e2e8f0;padding:17px;border-radius:13px;font-size:12px}@media(max-width:700px){.nav{display:block}.nav nav{margin-top:10px;flex-wrap:wrap}.two{grid-template-columns:1fr}.hero,section{padding:48px 17px}}
"""


if __name__ == "__main__":
    raise SystemExit(main())
