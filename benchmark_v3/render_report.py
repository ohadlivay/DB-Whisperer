"""Presentation-only HTML publishers for the two Evaluation V3 reports.

The renderers deliberately format the validated report model; they never score,
aggregate, or infer campaign results.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any, Mapping

from benchmark_v3.report_model import build_report_model


REPORT_TABS = (
    ("overview", "Overview"),
    ("comparison", "System Comparison"),
    ("questions", "Results by Question"),
    ("quality", "Quality Components"),
    ("ambiguity", "Ambiguity Funnel"),
    ("operations", "Safety, ETL & Operations"),
    ("methodology", "Methodology"),
    ("evidence", "Case Evidence"),
)

ARM_LABELS = {
    "baseline": "Baseline",
    "candidate_only": "Candidate Only",
    "semantic_only": "Semantic Only",
    "full": "Full System",
}


def _text(value: Any) -> str:
    """Return text safe for a HTML text node, including model-provided data."""
    if value is None:
        return "Not reported"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return html.escape(str(value))


def _json(value: Any) -> str:
    return html.escape(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _mean(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("mean"))
    return _text(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _arm_cards(model: Mapping[str, Any]) -> str:
    cards = _mapping(model.get("arm_cards", model.get("arms")))
    return "".join(
        "<article class=\"card arm\"><p class=\"eyebrow\">{label}</p>"
        "<strong>{score}</strong><span>Composite score</span><small>Pass rate: {pass_rate}</small></article>".format(
            label=_text(label), score=_mean(_mapping(cards.get(key)).get("composite")),
            pass_rate=_mean(_mapping(cards.get(key)).get("pass_rate")),
        )
        for key, label in ARM_LABELS.items()
    )


def _list_items(values: Any) -> str:
    if not isinstance(values, list):
        values = [values] if values else []
    return "".join(f"<li>{_text(value)}</li>" for value in values) or "<li>None reported.</li>"


def _case_evidence(model: Mapping[str, Any]) -> str:
    cases = model.get("cases", model.get("records", []))
    if not isinstance(cases, list):
        return "<p>No case-level evidence was supplied.</p>"
    blocks: list[str] = []
    for case in cases:
        record = _mapping(case)
        result = _mapping(record.get("result"))
        score = _mapping(record.get("score"))
        ambiguity = _mapping(score.get("ambiguity"))
        case_id = record.get("case_id", record.get("id", "unnamed case"))
        blocks.append(
            "<details><summary>{case_id} · {category} · {arm} · run {run}</summary>"
            "<div class=\"evidence-grid\"><section><h4>Question</h4><pre>{question}</pre><h4>Expected SQL</h4><pre>{expected}</pre>"
            "<h4>Generated SQL</h4><pre>{sql}</pre><h4>Result</h4><pre>{result}</pre></section>"
            "<section><h4>Score</h4><pre>{score}</pre><h4>Clarifications</h4><pre>{clarifications}</pre>"
            "<h4>Candidate support</h4><pre>{support}</pre><h4>Clarification compliance</h4><pre>{compliance}</pre></section>"
            "</div></details>".format(
                case_id=_text(case_id), category=_text(record.get("category")),
                arm=_text(record.get("arm")), run=_text(record.get("run")),
                question=_json(record.get("question", "Not recorded")),
                expected=_json(record.get("expected_sql", record.get("reference_sql", "Not recorded"))),
                sql=_json(result.get("sql", "Not recorded")), result=_json(result), score=_json(score),
                clarifications=_json(record.get("clarifications", [])),
                support=_json(ambiguity.get("candidate_support", record.get("candidate_support", []))),
                compliance=_json(ambiguity.get("compliance", record.get("compliance"))),
            )
        )
    return "".join(blocks) or "<p>No case-level evidence was supplied.</p>"


def _component_rows(model: Mapping[str, Any], section: str) -> str:
    arms = _mapping(model.get("arms"))
    names = ("ambiguity", "correctness", "efficiency", "safety", "grounding", "etl")
    rows = []
    for name in names:
        cells = "".join(
            f"<td>{_mean(_mapping(_mapping(arms.get(key)).get('components')).get(name))}</td>"
            for key in ARM_LABELS
        )
        rows.append(f"<tr><th>{_text(name.title())}</th>{cells}</tr>")
    return "".join(rows)


def _table_header() -> str:
    return "".join(f"<th>{_text(label)}</th>" for label in ARM_LABELS.values())


def _style() -> str:
    return """
    :root{--ink:#182338;--paper:#f5f7f3;--surface:#fff;--teal:#087f7b;--cyan:#14b8c4;--muted:#607087;--line:#dce4e7;--shadow:0 12px 35px #18233818}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,Arial,sans-serif}h1,h2,h3,h4{font-family:Georgia,serif;line-height:1.15}main{max-width:1180px;margin:auto;padding:28px 24px 72px}.hero{margin:0;background:linear-gradient(125deg,#182338,#087f7b);color:white;padding:58px max(24px,calc((100vw - 1132px)/2));}.hero h1{font-size:clamp(2.3rem,6vw,4.7rem);margin:0}.hero p{max-width:780px;font-size:1.1rem}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.74rem;font-weight:700;color:var(--teal)}.hero .eyebrow{color:#9ce8e2}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:24px 0}.card,details,.panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:20px}.arm strong{display:block;font:2.4rem Georgia,serif;color:var(--teal)}.arm span,.arm small{display:block;color:var(--muted)}.toolbar{position:sticky;top:0;z-index:3;background:#fffffff0;border-bottom:1px solid var(--line);overflow:auto;white-space:nowrap}.toolbar a{display:inline-block;padding:13px 10px;color:var(--ink);text-decoration:none;font-weight:700}.toolbar a:hover{color:var(--teal)}.tab{scroll-margin-top:52px;margin-top:28px}.tab h2{font-size:2rem}table{border-collapse:collapse;width:100%;background:var(--surface)}th,td{padding:10px;border:1px solid var(--line);text-align:left;vertical-align:top}th{background:#e8f3f2}pre{background:#182338;color:#eaf4f3;padding:12px;overflow:auto;white-space:pre-wrap;border-radius:8px}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}summary{cursor:pointer;font-weight:700}details{margin:10px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.muted{color:var(--muted)}@media(max-width:760px){.cards{grid-template-columns:1fr 1fr}.evidence-grid,.two{grid-template-columns:1fr}main{padding:20px 14px 48px}.hero{padding:42px 20px}}
    """


def render_one_page(model: Mapping[str, Any]) -> str:
    """Render the compact, campaign-backed V3 method and outcome summary."""
    provenance = _mapping(model.get("provenance"))
    methodology = _mapping(model.get("methodology"))
    return """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title><style>{style}</style></head><body>
    <header class=\"hero\"><p class=\"eyebrow\">Campaign publication · Evaluation V3</p><h1>{title}</h1><p>A four-arm comparison of a single-pass baseline, candidate-only, semantic-only, and hybrid clarification workflow.</p></header>
    <main><section class=\"panel\"><h2>What changed</h2><p>V3 evaluates 24 cases across K=3 SQL candidates and five repetitions with frozen deterministic scoring. Candidate SQL and executed results are primary ambiguity evidence; semantic-column findings support one targeted two-option clarification. Relationship paths never trigger clarification.</p><p class=\"muted\">Suite {suite} · model {model_name} · {design}</p></section><section class=\"cards\">{cards}</section><section class=\"two\"><article class=\"panel\"><h2>Ambiguity funnel</h2><pre>{funnel}</pre></article><article class=\"panel\"><h2>Campaign operations</h2><pre>{operations}</pre></article></section><section class=\"two\"><article class=\"panel\"><h2>Findings</h2><ul>{findings}</ul></article><article class=\"panel\"><h2>Interpretation limits</h2><ul>{limitations}</ul></article></section></main></body></html>""".format(
        title=_text(model.get("title", "DB Whisperer Evaluation V3")), style=_style(),
        suite=_text(provenance.get("suite_version")), model_name=_text(provenance.get("model")),
        design=_text(methodology.get("design")), cards=_arm_cards(model),
        funnel=_json(model.get("ambiguity_funnel", {})), operations=_json(model.get("operations", model.get("operational", {}))),
        findings=_list_items(model.get("findings")), limitations=_list_items(model.get("limitations")),
    )


def render_full_report(model: Mapping[str, Any]) -> str:
    """Render the eight-tab evidence report from a validated presentation model."""
    provenance = _mapping(model.get("provenance")); methodology = _mapping(model.get("methodology"))
    deltas = _mapping(model.get("arm_deltas")); failures = model.get("failures", [])
    nav = "".join(f"<a href=\"#{key}\">{label}</a>" for key, label in REPORT_TABS)
    return """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title} evidence report</title><style>{style}</style></head><body>
    <header class=\"hero\"><p class=\"eyebrow\">Auditable campaign evidence</p><h1>{title}</h1><p>Four V3 arms, 24 cases, K=3 candidates, and five repetitions.</p></header><nav class=\"toolbar\">{nav}</nav><main>
    <section class=\"tab\" id=\"overview\"><h2>Overview</h2><p>{design}</p><p class=\"muted\">Suite {suite} ({suite_hash}) · model {model_name} · fingerprint {fingerprint}</p><div class=\"cards\">{cards}</div></section>
    <section class=\"tab\" id=\"comparison\"><h2>System Comparison</h2><table><thead><tr><th>Measure</th>{headers}</tr></thead><tbody><tr><th>Composite score</th>{composites}</tr><tr><th>Pass rate</th>{pass_rates}</tr></tbody></table><h3>Published deltas</h3><pre>{deltas}</pre></section>
    <section class=\"tab\" id=\"questions\"><h2>Results by Question</h2><p>Per-case SQL, results, scores, transcripts, and support are retained in Case Evidence.</p>{case_evidence}</section>
    <section class=\"tab\" id=\"quality\"><h2>Quality Components</h2><table><thead><tr><th>Component</th>{headers}</tr></thead><tbody>{components}</tbody></table></section>
    <section class=\"tab\" id=\"ambiguity\"><h2>Ambiguity Funnel</h2><p>The Ambiguity funnel reports clarification compliance and candidate support from scored records, not inferred by this renderer.</p><pre>{funnel}</pre></section>
    <section class=\"tab\" id=\"operations\"><h2>Safety, ETL &amp; Operations</h2><div class=\"two\"><article class=\"panel\"><h3>Shared ETL</h3><pre>{etl}</pre><h3>Warnings</h3><ul>{warnings}</ul></article><article class=\"panel\"><h3>Campaign operations</h3><pre>{operations}</pre><h3>Usage</h3><pre>{usage}</pre></article></div></section>
    <section class=\"tab\" id=\"methodology\"><h2>Methodology</h2><p>V3 uses exactly four arms: Baseline, Candidate Only, Semantic Only, and Full System. It runs 24 cases with K=3 candidates for every request and five complete repetitions. Relationship-route multiplicity is outside this evaluation.</p><pre>{methodology}</pre></section>
    <section class=\"tab\" id=\"evidence\"><h2>Case Evidence</h2><h3>Failures</h3><pre>{failures}</pre><h3>Oracle reviews</h3><pre>{oracles}</pre><h3>Case transcripts and SQL evidence</h3>{case_evidence}</section>
    </main></body></html>""".format(
        title=_text(model.get("title", "DB Whisperer Evaluation V3")), style=_style(), nav=nav,
        design=_text(methodology.get("design")), suite=_text(provenance.get("suite_version")),
        suite_hash=_text(provenance.get("suite_hash")), model_name=_text(provenance.get("model")),
        fingerprint=_text(provenance.get("fingerprint")), cards=_arm_cards(model), headers=_table_header(),
        composites="".join(f"<td>{_mean(_mapping(model.get('headline_metrics')).get(key))}</td>" for key in ARM_LABELS),
        pass_rates="".join(f"<td>{_mean(_mapping(_mapping(model.get('arms')).get(key)).get('pass_rate'))}</td>" for key in ARM_LABELS),
        deltas=_json(deltas), components=_component_rows(model, "components"), funnel=_json(model.get("ambiguity_funnel", {})),
        etl=_json(model.get("shared_etl", {})), warnings=_list_items(model.get("warnings")),
        operations=_json(model.get("operations", model.get("operational", {}))), usage=_json(model.get("usage", {})),
        methodology=_json(methodology), failures=_json(failures), oracles=_json(model.get("oracle_reviews", [])),
        case_evidence=_case_evidence(model),
    )


def _atomic_write(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)
    return path


def write_reports(aggregate_path: Path, one_page_path: Path, full_report_path: Path) -> tuple[Path, Path]:
    """Publish exactly the one-page and full V3 reports with atomic replacements."""
    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    model = aggregate.get("model") if isinstance(aggregate, Mapping) else None
    if not isinstance(model, Mapping):
        model = build_report_model(aggregate)
    return (
        _atomic_write(Path(one_page_path), render_one_page(model)),
        _atomic_write(Path(full_report_path), render_full_report(model)),
    )
