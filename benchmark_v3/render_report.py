"""Presentation-only publishers for the two Evaluation V3 HTML reports."""
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
    ("questions", "Case Evidence"),
    ("quality", "Correctness & Projection"),
    ("ambiguity", "Ambiguity Funnel"),
    ("operations", "Safety, ETL & Operations"),
    ("methodology", "Methodology & Limitations"),
    ("evidence", "Terminal Outcomes"),
)
ARM_LABELS = {"baseline": "Baseline", "candidate_only": "Candidate Only",
              "semantic_only": "Semantic Only", "full": "Full System"}


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if value is None:
        return "Not reported"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return html.escape(str(value))


def _json(value: Any) -> str:
    return html.escape(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _mean(value: Any) -> str:
    return _text(_map(value).get("mean") if isinstance(value, Mapping) else value)


def _items(values: Any) -> str:
    values = values if isinstance(values, list) else ([values] if values else [])
    return "".join("<li>%s</li>" % _text(value) for value in values) or "<li>None reported.</li>"


def _style() -> str:
    return """:root{--ink:#182338;--muted:#5d697b;--paper:#f5f7f3;--card:#fff;--line:#dce2dc;--navy:#143149;--teal:#087a72;--amber:#c88716;--shadow:0 14px 38px #152a3914}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--paper);font:15px/1.58 Inter,Segoe UI,Arial,sans-serif}body.dark{--ink:#edf2f7;--muted:#aeb9c8;--paper:#0d1722;--card:#152333;--line:#2b3d4e;--navy:#e7f4f7;--teal:#67d7cc;--shadow:0 14px 38px #0004}h1,h2,h3{font-family:Georgia,serif}.skip{position:absolute;left:-999px}.skip:focus{left:16px;top:12px;z-index:100}.hero{padding:58px max(24px,calc((100vw - 1080px)/2)) 76px;color:#fff;background:linear-gradient(135deg,#102a41,#174d59 62%,#08776f)}.eyebrow{color:#a6e9e1;font-size:12px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.hero h1{margin:0;font-size:clamp(38px,6vw,68px)}.toolbar{position:sticky;top:0;z-index:20;padding:10px 18px;background:color-mix(in srgb,var(--paper) 92%,transparent);border-bottom:1px solid var(--line)}.toolbar-inner{max-width:1180px;min-width:0;margin:auto;display:flex;gap:9px;align-items:center}.context{margin-right:auto;color:var(--muted);font-weight:700;font-size:13px}.button,.tab-button{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);cursor:pointer}.button{padding:8px 11px;border-radius:10px;font-weight:750;flex:0 0 auto}.tablist{display:flex;min-width:0;flex:1 1 auto;overflow-x:auto;scrollbar-width:thin}.tab-button{padding:11px 8px;border-width:0 0 3px;white-space:nowrap;font-size:13px;font-weight:700}.tab-button[aria-selected=true]{color:var(--teal);border-color:var(--teal)}main{max-width:1180px;margin:auto;padding:0 22px 68px}.numbers{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:-36px 0 30px;position:relative}.number,.card,.panel,details{border:1px solid var(--line);background:var(--card);box-shadow:var(--shadow);border-radius:15px}.number{padding:17px 18px}.number strong{display:block;color:var(--teal);font:700 31px Georgia,serif}.number span{color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase}.card,.panel{padding:20px}.cards,.steps,.versions{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.arm strong{display:block;color:var(--teal);font:700 28px Georgia,serif}.question-card,.takeaway{padding:25px;border-left:6px solid var(--teal);border-radius:17px;background:var(--card);box-shadow:var(--shadow);font:700 23px/1.35 Georgia,serif}.takeaway{border-color:var(--amber);font:inherit}.two,.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}section{margin-top:30px}.tab-panel[hidden]{display:none}table{width:100%;border-collapse:collapse;background:var(--card)}th,td{padding:10px;border:1px solid var(--line);text-align:left;vertical-align:top}pre{padding:12px;overflow:auto;white-space:pre-wrap;color:#eaf4f3;background:#182338;border-radius:8px}details{margin:10px 0;padding:12px}@media(max-width:760px){.hero{padding:44px 24px 64px}.hero p{overflow-wrap:anywhere}.toolbar-inner{flex-wrap:wrap}.numbers{margin:18px 0 30px}.numbers,.cards,.steps,.versions,.two,.evidence-grid{grid-template-columns:1fr}.context{display:none}.tablist{order:2;flex-basis:100%}}@media print{.toolbar{display:none}.tab-panel[hidden]{display:block}.hero{color:#111;background:#fff;border-bottom:2px solid #111}}"""


def _cards(model: Mapping[str, Any]) -> str:
    cards = _map(model.get("arm_cards", model.get("arms")))
    return "".join("<article class=\"card arm\"><p>%s</p><strong>%s</strong><span>Composite score</span><small>Pass rate: %s</small></article>" % (label, _mean(_map(cards.get(key)).get("composite")), _mean(_map(cards.get(key)).get("pass_rate"))) for key, label in ARM_LABELS.items())


def _evidence(model: Mapping[str, Any]) -> str:
    cases = model.get("cases", model.get("records", []))
    if not isinstance(cases, list):
        return "<p>No case evidence was supplied.</p>"
    out = []
    for record in cases:
        row, result = _map(record), _map(_map(record).get("result"))
        turns = row.get("clarifications", [])
        support = [_map(turn).get("candidate_support", []) for turn in turns if isinstance(turn, Mapping)] if isinstance(turns, list) else []
        out.append("<details><summary>%s · %s · %s · run %s</summary><div class=\"evidence-grid\"><div><h4>Question</h4><pre>%s</pre><h4>Expected SQL</h4><pre>%s</pre><h4>Generated SQL</h4><pre>%s</pre><h4>Result</h4><pre>%s</pre></div><div><h4>Score</h4><pre>%s</pre><h4>Clarifications</h4><pre>%s</pre><h4>Candidate support</h4><pre>%s</pre><h4>Clarification compliance</h4><pre>%s</pre><h4>Comparison metadata</h4><pre>%s</pre></div></div></details>" % (_text(row.get("case_id")), _text(row.get("category")), _text(row.get("arm")), _text(row.get("run")), _json(row.get("question", "Not recorded")), _json(row.get("expected_sql", "Not recorded")), _json(result.get("sql")), _json(result), _json(row.get("score", {})), _json(turns), _json(support), _json([_map(turn).get("compliance_passed") for turn in turns] if isinstance(turns, list) else []), _json(row.get("comparison", {}))))
    return "".join(out) or "<p>No case evidence was supplied.</p>"


def _legacy_render_one_page(model: Mapping[str, Any]) -> str:
    provenance, methodology = _map(model.get("provenance")), _map(model.get("methodology"))
    return """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>%s</title><style>%s</style></head><body><a class=\"skip\" href=\"#main\">Skip to content</a><header class=\"hero\"><p class=\"eyebrow\">Campaign publication · Evaluation V3</p><h1>%s</h1><p>A plain-language overview of the V3 method and evidence.</p></header><nav class=\"toolbar\" aria-label=\"Page tools\"><div class=\"toolbar-inner\"><span class=\"context\">Evaluation V3 overview</span><button class=\"button\" id=\"theme\">Theme</button><button class=\"button\" id=\"print\">Print</button></div></nav><main id=\"main\"><div class=\"numbers\"><div class=\"number\"><strong>24</strong><span>Cases</span></div><div class=\"number\"><strong>4</strong><span>Arms</span></div><div class=\"number\"><strong>5×</strong><span>Repetitions</span></div><div class=\"number\"><strong>K=3</strong><span>Candidate arms</span></div></div><section><h2>The question we tested</h2><div class=\"question-card\">Can targeted clarification improve schema-aware text-to-SQL while preserving correct and safe answers?</div></section><section><h2>How the test worked</h2><div class=\"steps\"><article class=\"card\">22 query cases</article><article class=\"card\">2 ETL fixtures</article><article class=\"card\">Frozen deterministic scoring</article><article class=\"card\">Checkpoint each cell</article></div></section><section><h2>What the four arms did</h2><div class=\"cards\">%s</div></section><section><h2>What the aggregate shows</h2><div class=\"two\"><article class=\"panel\"><h3>Ambiguity funnel</h3><pre>%s</pre></article><article class=\"panel\"><h3>Campaign operations</h3><pre>%s</pre></article></div></section><section><h2>What we learned</h2><div class=\"takeaway\"><ul>%s</ul></div></section><section><h2>How to interpret the results</h2><ul>%s</ul><pre>%s</pre></section></main><script>const b=document.body;if(localStorage.getItem('dbw-v3-theme')==='dark')b.classList.add('dark');document.getElementById('theme').onclick=()=>{b.classList.toggle('dark');localStorage.setItem('dbw-v3-theme',b.classList.contains('dark')?'dark':'light')};document.getElementById('print').onclick=()=>window.print();</script></body></html>""" % (_text(model.get("title", "DB Whisperer Evaluation V3")), _style(), _text(model.get("title", "DB Whisperer Evaluation V3")), _cards(model), _json(model.get("ambiguity_funnel", {})), _json(model.get("operations", {})), _items(model.get("findings")), _items(model.get("limitations")), _json({"methodology": methodology, "provenance": provenance}))


def _legacy_render_full_report(model: Mapping[str, Any]) -> str:
    arms = _map(model.get("arms")); headers = "".join("<th>%s</th>" % label for label in ARM_LABELS.values())
    components = "".join("<tr><th>%s</th>%s</tr>" % (name.title(), "".join("<td>%s</td>" % _mean(_map(_map(arms.get(arm)).get("components")).get(name)) for arm in ARM_LABELS)) for name in ("ambiguity", "correctness", "efficiency", "safety", "grounding", "etl"))
    contents = {
        "overview": "<h2>Overview</h2><div class=\"cards\">%s</div><pre>%s</pre>" % (_cards(model), _json(model.get("provenance", {}))),
        "comparison": "<h2>System Comparison</h2><table class=\"sortable\"><thead><tr><th>Measure</th>%s</tr></thead><tbody><tr><th>Composite</th>%s</tr><tr><th>Pass rate</th>%s</tr></tbody></table><pre>%s</pre>" % (headers, "".join("<td>%s</td>" % _mean(_map(model.get("headline_metrics")).get(arm)) for arm in ARM_LABELS), "".join("<td>%s</td>" % _mean(_map(arms.get(arm)).get("pass_rate")) for arm in ARM_LABELS), _json(model.get("arm_deltas", {}))),
        "questions": "<h2>Results by Question</h2>" + _evidence(model),
        "quality": "<h2>Quality Components</h2><table class=\"sortable\"><thead><tr><th>Component</th>%s</tr></thead><tbody>%s</tbody></table>" % (headers, components),
        "ambiguity": "<h2>Ambiguity Funnel</h2><p>The Ambiguity funnel records clarification compliance and candidate support from transcript turns.</p><pre>%s</pre>" % _json(model.get("ambiguity_funnel", {})),
        "operations": "<h2>Safety, ETL &amp; Operations</h2><pre>%s</pre><ul>%s</ul>" % (_json({"shared_etl": model.get("shared_etl"), "operations": model.get("operations"), "usage": model.get("usage")}), _items(model.get("warnings"))),
        "methodology": "<h2>Methodology</h2><p>Baseline uses K=1. Candidate Only, Semantic Only, and Full System use K=3. The frozen suite has 22 query cases and 2 ETL fixtures (24 total), with five repetitions and a checkpoint after every cell. Relationship-route multiplicity is outside this evaluation.</p><pre>%s</pre>" % _json(model.get("methodology", {})),
        "evidence": "<h2>Case Evidence</h2><h3>Failures</h3><pre>%s</pre><h3>Oracle reviews</h3><pre>%s</pre>%s" % (_json(model.get("failures", [])), _json(model.get("oracle_reviews", [])), _evidence(model)),
    }
    buttons = "".join("<button class=\"tab-button\" role=\"tab\" id=\"tab-%s\" aria-controls=\"panel-%s\" aria-selected=\"%s\" tabindex=\"%s\">%s</button>" % (key, key, str(key == "overview").lower(), 0 if key == "overview" else -1, label) for key, label in REPORT_TABS)
    panels = "".join("<span id=\"%s\" hidden></span><section id=\"panel-%s\" class=\"tab-panel\" role=\"tabpanel\" aria-labelledby=\"tab-%s\"%s>%s</section>" % (key, key, key, "" if key == "overview" else " hidden", contents[key]) for key, _ in REPORT_TABS)
    script = """<script>document.documentElement.classList.add('js');const tabs=[...document.querySelectorAll('[role=tab]')],panels=[...document.querySelectorAll('[role=tabpanel]')];function activateTab(key,focus=false){tabs.forEach(t=>{const on=t.id==='tab-'+key;t.setAttribute('aria-selected',on);t.tabIndex=on?0:-1});panels.forEach(p=>p.hidden=p.id!=='panel-'+key);if(focus)document.getElementById('tab-'+key).focus()}tabs.forEach((tab,index)=>{tab.onclick=()=>activateTab(tab.id.slice(4));tab.onkeydown=e=>{if(!['ArrowRight','ArrowLeft','Home','End'].includes(e.key))return;e.preventDefault();const n=e.key==='Home'?0:e.key==='End'?tabs.length-1:e.key==='ArrowRight'?(index+1)%tabs.length:(index-1+tabs.length)%tabs.length;activateTab(tabs[n].id.slice(4),true)}});document.querySelectorAll('table.sortable').forEach(table=>{const body=table.tBodies[0];[...table.tHead.rows[0].cells].forEach((cell,index)=>{cell.tabIndex=0;cell.setAttribute('role','button');cell.setAttribute('aria-sort','none');cell.setAttribute('aria-label','Sort by '+cell.textContent.trim());let ascending=true;const sort=()=>{const rows=[...body.rows];rows.sort((a,b)=>{const av=a.cells[index].textContent.trim(),bv=b.cells[index].textContent.trim(),an=parseFloat(av),bn=parseFloat(bv),result=Number.isNaN(an)||Number.isNaN(bn)?av.localeCompare(bv):an-bn;return ascending?result:-result});rows.forEach(row=>body.appendChild(row));cell.setAttribute('aria-sort',ascending?'ascending':'descending');ascending=!ascending};cell.onclick=sort;cell.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();sort()}}})});const b=document.body;if(localStorage.getItem('dbw-v3-theme')==='dark')b.classList.add('dark');document.getElementById('theme').onclick=()=>{b.classList.toggle('dark');localStorage.setItem('dbw-v3-theme',b.classList.contains('dark')?'dark':'light')};document.getElementById('print').onclick=()=>window.print();</script>"""
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>%s evidence report</title><style>%s</style></head><body><header class=\"hero\"><p class=\"eyebrow\">Auditable campaign evidence</p><h1>%s</h1><p>Four V3 arms, 24 cases, and five repetitions.</p></header><nav class=\"toolbar\" aria-label=\"Report tools\"><div class=\"toolbar-inner\"><span class=\"context\">V3 evidence report</span><div class=\"tablist\" role=\"tablist\" aria-label=\"Report sections\">%s</div><button class=\"button\" id=\"theme\">Theme</button><button class=\"button\" id=\"print\">Print</button></div></nav><main>%s</main>%s</body></html>" % (_text(model.get("title", "DB Whisperer Evaluation V3")), _style(), _text(model.get("title", "DB Whisperer Evaluation V3")), buttons, panels, script)


def render_one_page(model: Mapping[str, Any]) -> str:
    """Render the approved one-page hierarchy without mutating evidence."""
    rendered = _legacy_render_one_page(model)
    rendered = rendered.replace("The question we tested", "Research question")
    rendered = rendered.replace(
        "Can targeted clarification improve schema-aware text-to-SQL while preserving correct and safe answers?",
        _text(model.get("research_question")),
    )
    rendered = rendered.replace("How the test worked", "Experimental design")
    rendered = rendered.replace("What the four arms did", "Headline results")
    rendered = rendered.replace("What we learned", "Principal findings")
    rendered = rendered.replace("How to interpret the results", "Limitations")
    supplement = (
        "<section><h2>Scoring framework</h2><pre>%s</pre></section>"
        "<section><h2>Correctness diagnostics</h2><pre>%s</pre></section>"
    ) % (
        _json(model.get("scoring_framework", {})),
        _json(model.get("correctness_diagnostics", {})),
    )
    return rendered.replace("</main>", supplement + "</main>")


def render_full_report(model: Mapping[str, Any]) -> str:
    """Render the evidence report with the approved analytical content types."""
    rendered = _legacy_render_full_report(model)
    rendered = rendered.replace("Results by Question", "Case Evidence")
    rendered = rendered.replace("Quality Components", "Correctness &amp; Projection")
    supplement = (
        "<section><h2>Terminal Outcomes</h2><pre>%s</pre></section>"
        "<section><h2>Principal Findings</h2><pre>%s</pre></section>"
        "<section><h2>Recommendations</h2><pre>%s</pre></section>"
        "<section><h2>Limitations</h2><pre>%s</pre></section>"
    ) % (
        _json(model.get("terminal_outcomes", {})),
        _json(model.get("findings", [])),
        _json(model.get("recommendations", [])),
        _json(model.get("limitations", [])),
    )
    return rendered.replace("</main>", supplement + "</main>")


def _atomic_write(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)
    return path


def write_reports(aggregate_path: Path, one_page_path: Path, full_report_path: Path) -> tuple[Path, Path]:
    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    model = aggregate.get("model") if isinstance(aggregate, Mapping) else None
    if not isinstance(model, Mapping):
        model = build_report_model(aggregate)
    return (_atomic_write(Path(one_page_path), render_one_page(model)), _atomic_write(Path(full_report_path), render_full_report(model)))
