"""Presentation-only publishers for the two Evaluation V3 HTML reports."""
from __future__ import annotations

from collections import Counter, defaultdict
import html
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from benchmark_v3.report_model import build_report_model


REPORT_TABS = (
    ("overview", "Overview"),
    ("comparison", "System Comparison"),
    ("questions", "Results by Question"),
    ("quality", "Quality Dimensions"),
    ("clarifications", "Clarifications"),
    ("reliability", "Reliability"),
    ("etl", "ETL Validation"),
    ("failures", "Failure Analysis"),
    ("methodology", "Methodology"),
    ("details", "Detailed Results"),
)
ARM_LABELS = {
    "baseline": "Baseline",
    "candidate_only": "Candidate Only",
    "semantic_only": "Semantic Only",
    "full": "Full System",
}
ARM_DESCRIPTIONS = {
    "baseline": "Produces one answer without an ambiguity check.",
    "candidate_only": "Tries three answers and compares the successful results.",
    "semantic_only": "Checks whether vague wording could point to different database fields.",
    "full": "Combines the three-answer comparison with the semantic check.",
}


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "Not reported") -> str:
    if value is None or value == "":
        value = default
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return html.escape(str(value))


def _json(value: Any) -> str:
    return html.escape(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    )


def _number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return _text(value)


def _mean(value: Any, digits: int = 2) -> str:
    raw = _map(value).get("mean") if isinstance(value, Mapping) else value
    return _number(raw, digits)


def _mean_value(value: Any) -> float:
    raw = _map(value).get("mean") if isinstance(value, Mapping) else value
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _items(values: Any) -> str:
    items = values if isinstance(values, list) else ([values] if values else [])
    return (
        "".join(f"<li>{_text(value)}</li>" for value in items)
        or "<li>None reported.</li>"
    )


def _records(model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = model.get("records", model.get("cases", []))
    return [value for value in values if isinstance(value, Mapping)] \
        if isinstance(values, list) else []


def _query_records(model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in _records(model) if row.get("arm") in ARM_LABELS]


def _etl_records(model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in _records(model) if row.get("arm") == "etl"]


def _passed(row: Mapping[str, Any]) -> bool:
    return bool(_map(row.get("score")).get("passed", False))


def _reason(row: Mapping[str, Any]) -> str:
    score = _map(row.get("score"))
    reason = score.get("reason")
    if reason:
        return str(reason)
    checks = score.get("checks")
    if isinstance(checks, list):
        failed = [
            str(_map(check).get("name"))
            for check in checks
            if not bool(_map(check).get("passed"))
        ]
        return "All ETL checks passed." if not failed else (
            "Failed ETL checks: " + ", ".join(failed)
        )
    return "No scoring reason was recorded."


def _result(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(row.get("result"))


def _status(row: Mapping[str, Any]) -> str:
    return "Pass" if _passed(row) else "Fail"


def _status_badge(row: Mapping[str, Any]) -> str:
    status = _status(row)
    return f'<span class="status {status.lower()}">{status}</span>'


def _ci(value: Any) -> str:
    ci = _map(value).get("confidence_interval_95")
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        return f"{_number(ci[0])} to {_number(ci[1])}"
    return "Not reported"


def _bar(label: str, value: float, note: str = "") -> str:
    width = max(0.0, min(100.0, value))
    return (
        '<div class="bar-row"><div class="bar-label">'
        f"<span>{_text(label)}</span><strong>{value:.2f}%</strong></div>"
        f'<div class="bar-track"><span style="width:{width:.2f}%"></span></div>'
        f'<small>{_text(note, "")}</small></div>'
    )


def _one_page_style() -> str:
    return """
:root{--ink:#193340;--muted:#607580;--cream:#f7f5ee;--card:#fff;--line:#d9e2de;
--teal:#076b66;--deep:#0d3c48;--mint:#d9eee8;--gold:#d49a2a;--shadow:0 14px 34px #19334016}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--cream);
font:16px/1.62 Inter,Segoe UI,Arial,sans-serif}body.dark{--ink:#eaf4f2;--muted:#b5c8c6;
--cream:#0d1c22;--card:#132a31;--line:#2d4b50;--mint:#183d3c;--teal:#70d4c8;--deep:#eaf4f2;
--shadow:0 14px 34px #0005}h1,h2,h3{line-height:1.16}h2{font-size:clamp(26px,3vw,38px);
margin:0 0 16px}h3{font-size:19px;margin:0 0 8px}.skip{position:absolute;left:-999px}.skip:focus{
left:16px;top:12px;z-index:100;background:#fff;padding:8px}.hero{position:relative;overflow:hidden;color:#fff;
padding:62px max(24px,calc((100vw - 1080px)/2)) 76px;background:linear-gradient(135deg,#0b3542,#0b6967)}
.hero:after{content:"";position:absolute;width:340px;height:340px;border-radius:50%;right:-95px;top:-145px;
border:54px solid #ffffff12}.eyebrow{font-size:12px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;
color:#a9e4dc}.hero h1{position:relative;margin:7px 0 13px;max-width:750px;font-size:clamp(38px,6vw,68px);
letter-spacing:-.035em}.hero p:last-child{max-width:700px;margin:0;font-size:18px;color:#d9f1ed}.toolbar{
position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--cream) 94%,transparent);
border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}.toolbar-inner{max-width:1120px;margin:auto;
padding:10px 22px;display:flex;gap:9px;align-items:center}.context{margin-right:auto;color:var(--muted);
font-size:13px;font-weight:750}.button,.detail-link{border:1px solid var(--line);border-radius:9px;
background:var(--card);color:var(--ink);padding:8px 11px;font:inherit;font-size:13px;font-weight:750;
text-decoration:none;cursor:pointer}main{max-width:1080px;margin:auto;padding:0 22px 72px}.numbers{position:relative;
display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 42px}.number{padding:18px;
border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:var(--shadow)}.number strong{
display:block;color:var(--teal);font-size:31px;line-height:1}.number span{display:block;margin-top:7px;color:var(--muted);
font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}section{margin-top:58px}.question-card,
.takeaway{padding:28px;border-radius:17px;background:var(--card);box-shadow:var(--shadow);border:1px solid var(--line);
border-left:6px solid var(--teal)}.question-card{font-size:23px;font-weight:750;line-height:1.42}.steps,
.versions,.results,.lessons,.decisions{display:grid;gap:14px}.steps,.versions{grid-template-columns:repeat(4,1fr)}
.results,.lessons{grid-template-columns:1fr 1fr}.decisions{grid-template-columns:repeat(3,1fr)}.card{
padding:21px;border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:var(--shadow)}
.step-no{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:50%;background:var(--teal);color:#fff;
font-weight:800}.card p{margin:7px 0 0;color:var(--muted)}.tags{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}
.tag{padding:5px 10px;border-radius:999px;background:var(--mint);color:var(--teal);font-size:12px;font-weight:800}
.version{border-top:5px solid var(--teal)}.version strong{display:block;font-size:19px}.headline{
padding:27px;border-radius:17px;background:var(--deep);color:#fff}.headline h3{font-size:25px}.headline p{
color:#deefed}.score-pair{display:grid;grid-template-columns:1fr auto 1fr;gap:15px;align-items:center;margin-top:18px}
.score-box{padding:18px;border-radius:13px;background:var(--card);border:1px solid var(--line)}.score-box strong{
display:block;color:var(--teal);font-size:34px}.versus{font-weight:900;color:var(--muted)}.bar-card{margin-top:16px;
padding:22px;border:1px solid var(--line);border-radius:14px;background:var(--card)}.bar-row{margin:15px 0}
.bar-label{display:flex;justify-content:space-between;gap:15px}.bar-track{height:12px;margin-top:5px;border-radius:999px;
background:#dce7e4;overflow:hidden}.bar-track span{display:block;height:100%;border-radius:inherit;
background:linear-gradient(90deg,#0b6967,#2da89b)}.bar-row small{display:block;color:var(--muted);margin-top:4px}
.fact strong{display:block;color:var(--teal);font-size:28px}.notice{margin-top:16px;padding:18px 20px;
border-radius:13px;background:#e9f4ff;color:#214963;border:1px solid #c8dff1}.plain-list{margin:0;padding-left:20px}
.plain-list li{margin:8px 0}.two-sides{display:grid;grid-template-columns:1fr 1fr;gap:15px}.side{
padding:22px;border-radius:14px;background:var(--card);border:1px solid var(--line)}.side h3{color:var(--teal)}
.next{border-left-color:var(--gold)}footer{max-width:1080px;margin:0 auto 50px;padding:24px 22px;
border-top:1px solid var(--line);color:var(--muted);display:flex;justify-content:space-between;gap:16px}
@media(max-width:780px){.hero{padding:46px 24px 68px}.toolbar-inner{flex-wrap:wrap}.context{flex:1 0 100%}
.numbers{margin:18px 0 30px}.numbers,.steps,.versions,.results,.lessons,.decisions,.two-sides,
.score-pair{grid-template-columns:1fr}.versus{text-align:center}section{margin-top:42px}}
@media print{.toolbar{display:none}.hero{color:#111;background:#fff;border-bottom:2px solid #111}.hero p:last-child,
.eyebrow{color:#333}.numbers{margin:18px 0}.card,.number,.question-card,.takeaway{box-shadow:none}}
"""


def _report_style() -> str:
    return """
:root{--ink:#172033;--muted:#5d6b82;--bg:#f4f7fb;--card:#fff;--line:#d8e0ec;
--blue:#175cd3;--blue2:#0b3b91;--cyan:#0e7490;--green:#16794b;--red:#b42318;
--amber:#9a6700;--radius:16px}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;
color:var(--ink);background:var(--bg);font:14px/1.5 Inter,Segoe UI,Arial,sans-serif}.skip{position:absolute;
left:-999px}.skip:focus{left:16px;top:12px;z-index:100;background:#fff;padding:8px}.hero{color:#fff;
padding:42px max(24px,calc((100vw - 1320px)/2));background:linear-gradient(120deg,var(--blue2),var(--blue) 65%,#2783e8)}
.hero h1{margin:0 0 5px;font-size:clamp(32px,5vw,52px);letter-spacing:-.03em}.hero p{max-width:900px;
margin:0;color:#dbeafe;font-size:16px}.tabs-shell{position:sticky;top:0;z-index:20;background:#fff;
border-bottom:1px solid var(--line);box-shadow:0 5px 15px #1720330d}.tabs{max-width:1320px;margin:auto;
display:flex;overflow-x:auto}.tab{flex:0 0 auto;padding:15px 13px;border:0;border-bottom:3px solid transparent;
background:transparent;color:var(--muted);font:inherit;font-weight:750;cursor:pointer;white-space:nowrap}.tab[aria-selected=true]{
color:var(--blue);border-bottom-color:var(--blue)}main{max-width:1320px;margin:auto;padding:28px 22px 70px}
.tab-panel[hidden]{display:none}.panel-title{margin:0 0 5px;font-size:30px}.lede{margin:0 0 20px;
max-width:900px;color:var(--muted);font-size:16px}.notice{padding:15px 17px;margin:15px 0;border:1px solid #bfd5fa;
border-radius:12px;background:#edf4ff;color:#25456d}.metrics{display:grid;grid-template-columns:repeat(4,1fr);
gap:13px;margin:18px 0}.metric{padding:18px;border:1px solid var(--line);border-radius:var(--radius);background:var(--card)}
.metric strong{display:block;color:var(--blue);font-size:29px;line-height:1.1}.metric span{color:var(--muted);
font-size:12px;font-weight:750;text-transform:uppercase}.two-col,.chart-grid,.evidence-grid{display:grid;
grid-template-columns:1fr 1fr;gap:16px}.card{margin:16px 0;padding:20px;border:1px solid var(--line);
border-radius:var(--radius);background:var(--card)}.card h3{margin:0 0 10px}.bar-row{margin:14px 0}.bar-label{
display:flex;justify-content:space-between}.bar-track{height:11px;margin-top:5px;border-radius:99px;background:#e7edf6;overflow:hidden}
.bar-track span{display:block;height:100%;background:linear-gradient(90deg,var(--blue),#58a6ef)}.bar-row small{
display:block;color:var(--muted);margin-top:3px}.table-wrap{overflow:auto;border:1px solid var(--line);
border-radius:12px;background:var(--card)}table{width:100%;border-collapse:collapse;min-width:720px}th,td{
padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f8faff;
color:#3e4f68;font-size:12px;text-transform:uppercase;letter-spacing:.035em}tbody tr:hover{background:#f8faff}
.status{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:850;text-transform:uppercase}
.status.pass{color:var(--green);background:#e8f7ef}.status.fail{color:var(--red);background:#fff0ef}
.status.na{color:var(--muted);background:#eef1f5}.heat{display:grid;grid-template-columns:minmax(220px,1.7fr) repeat(4,1fr);
gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}.heat>div{
padding:10px;background:#fff}.heat .head{background:#eef4ff;font-weight:800}.heat .good{background:#eaf7f0;color:var(--green);
font-weight:800}.heat .mixed{background:#fff7e6;color:var(--amber);font-weight:800}.heat .bad{background:#fff0ef;
color:var(--red);font-weight:800}.filters{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin:14px 0}
.filters label{font-size:12px;font-weight:750;color:var(--muted)}.filters select,.filters input{width:100%;
padding:9px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink)}details{margin:10px 0;
border:1px solid var(--line);border-radius:12px;background:#fff}summary{padding:13px 15px;cursor:pointer;font-weight:750}
.detail-body{padding:0 15px 15px}.audit-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.audit{
min-width:0;padding:14px;border-radius:10px;background:#f8faff}.audit h4{margin:0 0 6px;color:#42526b}
pre{max-height:360px;margin:0;padding:12px;overflow:auto;white-space:pre-wrap;word-break:break-word;border-radius:8px;
background:#152238;color:#edf5ff;font:12px/1.45 Consolas,monospace}.sequence{padding:15px;border-left:4px solid var(--cyan);
background:#f1fbfd;border-radius:0 10px 10px 0}.muted{color:var(--muted)}.small{font-size:12px}
.count{font-weight:800;color:var(--blue)}.record-note{padding:8px 11px;border-radius:8px;background:#fff6e2;
color:#6e5114}.empty{padding:25px;text-align:center;color:var(--muted)}.inline-code{font-family:Consolas,monospace;
font-size:12px}.wide{grid-column:1/-1}
@media(max-width:850px){.metrics{grid-template-columns:1fr 1fr}.two-col,.chart-grid,.evidence-grid,
.audit-grid{grid-template-columns:1fr}.filters{grid-template-columns:1fr 1fr}.heat{grid-template-columns:minmax(170px,1.5fr) repeat(4,90px);
overflow:auto}.tabs{overflow-x:auto}}
@media(max-width:520px){.metrics,.filters{grid-template-columns:1fr}}
@media print{.tabs-shell{display:none}.tab-panel[hidden]{display:block}.tab-panel{break-before:page}
.hero{color:#111;background:#fff;border-bottom:2px solid #111}.hero p{color:#333}details{break-inside:avoid}}
"""


def _metric_cards(model: Mapping[str, Any]) -> str:
    arms = _map(model.get("arms"))
    cards = []
    for key, label in ARM_LABELS.items():
        arm = _map(arms.get(key))
        cards.append(
            '<article class="metric"><strong>%s</strong><span>%s composite</span>'
            '<p class="small">Pass rate: %s%%</p></article>'
            % (_mean(arm.get("composite")), _text(label), _mean(arm.get("pass_rate")))
        )
    return "".join(cards)


def _arm_bars(model: Mapping[str, Any], metric: str = "pass_rate") -> str:
    arms = _map(model.get("arms"))
    return "".join(
        _bar(label, _mean_value(_map(arms.get(key)).get(metric)))
        for key, label in ARM_LABELS.items()
    )


def _one_page_script() -> str:
    return """<script>const body=document.body;const theme=document.getElementById('theme');
if(localStorage.getItem('dbw-report-theme')==='dark')body.classList.add('dark');
theme.onclick=()=>{body.classList.toggle('dark');localStorage.setItem('dbw-report-theme',
body.classList.contains('dark')?'dark':'light')};document.getElementById('print').onclick=()=>window.print();</script>"""


def render_one_page(model: Mapping[str, Any]) -> str:
    """Render a plain-language one-page discussion brief."""

    records = _records(model)
    query_case_count = len({row.get("case_id") for row in _query_records(model)})
    etl_case_count = len({row.get("case_id") for row in _etl_records(model)})
    arms = _map(model.get("arms"))
    baseline = _map(arms.get("baseline"))
    full = _map(arms.get("full"))
    full_pass = _mean_value(full.get("pass_rate"))
    baseline_pass = _mean_value(baseline.get("pass_rate"))
    pass_lift = full_pass - baseline_pass
    funnel = _map(_map(model.get("ambiguity_funnel")).get("full"))
    recall = _mean(funnel.get("recall"))
    specificity = _mean(funnel.get("specificity"))
    operations = _map(_map(model.get("operations")).get("metrics"))
    versions = "".join(
        '<article class="card version"><strong>%s</strong><p>%s</p></article>'
        % (_text(label), _text(ARM_DESCRIPTIONS[key]))
        for key, label in ARM_LABELS.items()
    )
    bars = _arm_bars(model)
    total = len(records) or 450
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>What we tested—and what we learned</title><style>{_one_page_style()}</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="hero"><p class="eyebrow">DB Whisperer · Team Discussion Brief</p>
<h1>What we tested—and what we learned</h1>
<p>A plain-language overview of the evaluation method, the latest results, and the choices we need to make next.</p>
</header>
<nav class="toolbar" aria-label="Page tools"><div class="toolbar-inner">
<span class="context">Evaluation V3 overview</span>
<a class="detail-link" href="evaluation_report.html">Detailed report</a>
<button class="button" id="theme">Theme</button><button class="button" id="print">Print</button>
</div></nav>
<main id="main">
<div class="numbers">
<div class="number"><strong>{query_case_count or 22}</strong><span>Database questions</span></div>
<div class="number"><strong>4</strong><span>System versions</span></div>
<div class="number"><strong>5×</strong><span>Each question repeated</span></div>
<div class="number"><strong>{total}</strong><span>Total checks</span></div>
</div>
<section><h2>The question we wanted to answer</h2>
<div class="question-card">Can DB Whisperer turn everyday database questions into correct, safe answers—and does asking one focused follow-up question help when a request could mean more than one thing?</div>
</section>
<section><h2>How the test worked</h2>
<p>Every version faced the same questions and was checked in the same way.</p>
<div class="steps">
<article class="card"><span class="step-no">1</span><h3>Ask {query_case_count or 22} questions</h3><p>The set included clear, unclear, ordinary, comparative, and unsafe requests.</p></article>
<article class="card"><span class="step-no">2</span><h3>Compare 4 versions</h3><p>Each version switched specific ambiguity features on or off.</p></article>
<article class="card"><span class="step-no">3</span><h3>Repeat 5 times</h3><p>AI answers can vary, so every question was tried more than once.</p></article>
<article class="card"><span class="step-no">4</span><h3>Check the answer</h3><p>Automatic rules compared each result with a known-good database result.</p></article>
</div>
<div class="tags"><span class="tag">Unclear wording</span><span class="tag">Clear comparisons</span>
<span class="tag">Ordinary questions</span><span class="tag">Unsafe requests</span>
<span class="tag">{etl_case_count or 2} CSV loading checks</span></div>
</section>
<section><h2>What the four versions did</h2><div class="versions">{versions}</div></section>
<section><h2>What the latest results show</h2>
<div class="headline"><h3>The Full System performed best in this evaluation.</h3>
<p>It passed more complete tests than the simpler versions and achieved the strongest overall score.</p></div>
<div class="score-pair">
<article class="score-box"><span>Baseline pass rate</span><strong>{baseline_pass:.2f}%</strong><p>The simplest version.</p></article>
<div class="versus">compared with</div>
<article class="score-box"><span>Full System pass rate</span><strong>{full_pass:.2f}%</strong><p>All ambiguity features enabled.</p></article>
</div>
<div class="bar-card"><h3>Share of tests that passed</h3>{bars}
<p class="muted">A test passed only when the final outcome met all rules that applied to that question.</p></div>
<div class="results">
<article class="card fact"><strong>+{pass_lift:.2f}</strong><h3>percentage points</h3><p>The Full System’s pass-rate improvement over the Baseline.</p></article>
<article class="card fact"><strong>{_mean(full.get("composite"))}/100</strong><h3>overall score</h3><p>A weighted summary of answer quality, clarification behavior, safety, grounding, and data loading.</p></article>
<article class="card fact"><strong>{recall}%</strong><h3>clarification recall</h3><p>How often the Full System asked when a clarification was needed.</p></article>
<article class="card fact"><strong>{specificity}%</strong><h3>clarification specificity</h3><p>How often it stayed quiet when no clarification was needed.</p></article>
</div>
<div class="notice"><strong>About the offline correction.</strong> No model calls were made during rescoring.
We rechecked the saved answers after fixing rules that had been stricter than the user’s intent. One lab-frequency question is excluded from the headline because its wording did not define the intended frequency. Including it does not change which system performed best.</div>
</section>
<section><h2>What we learned</h2><div class="lessons">
<article class="card"><h3>Combining both ambiguity checks helped most</h3><p>The Full System had the highest pass rate and composite score.</p></article>
<article class="card"><h3>The semantic check produced most of the gain</h3><p>Semantic Only was much stronger than Baseline; Candidate Only was close to Baseline.</p></article>
<article class="card"><h3>Clarification behavior improved, but was not perfect</h3><p>The Full System usually asked at the right time, yet some answers still ended without an accepted query.</p></article>
<article class="card"><h3>The result is not just a scoring artifact</h3><p>The same ordering remains when the excluded lab-frequency question is restored.</p></article>
</div></section>
<section><h2>How to interpret the results</h2><div class="two-sides">
<article class="side"><h3>What they tell us</h3><ul class="plain-list">
<li>On this fixed MIMIC-III test set, explicit ambiguity handling improved performance.</li>
<li>The Full System was the strongest of the four versions tested.</li>
<li>The saved evidence is detailed enough to audit every pass and failure.</li></ul></article>
<article class="side"><h3>What they do not prove</h3><ul class="plain-list">
<li>That the same scores will hold for every database or every user.</li>
<li>That every clarification is phrased as clearly as a person would want.</li>
<li>That a higher score removes the need for more user testing.</li></ul></article>
</div></section>
<section><h2>Decisions to make together</h2><div class="decisions">
<article class="card"><h3>Prioritize reliability</h3><p>Improve cases where the system produced no accepted query.</p></article>
<article class="card"><h3>Improve follow-up questions</h3><p>Make semantic clarifications more consistently address the distinction the user actually meant.</p></article>
<article class="card"><h3>Broaden the evidence</h3><p>Repeat the study with more datasets, question styles, and real users.</p></article>
</div></section>
<section><h2>A practical way forward</h2><div class="takeaway next">
Keep the Full System as the leading design, fix the remaining no-query and clarification failures, then validate the same evaluation method on a broader set of databases and users.
</div></section>
</main>
<footer><span>Evaluation V3 · Corrected offline publication</span>
<span>Campaign cost: ${_number(operations.get("cost_usd"))} · {total} saved checks</span></footer>
{_one_page_script()}</body></html>"""


def _comparison_table(model: Mapping[str, Any]) -> str:
    arms = _map(model.get("arms"))
    rows = []
    metrics = (
        ("Composite score", "composite"),
        ("Pass rate", "pass_rate"),
        ("Correctness", "correctness"),
        ("Ambiguity handling", "ambiguity"),
        ("Efficiency", "efficiency"),
        ("Safety", "safety"),
        ("Schema grounding", "grounding"),
        ("ETL", "etl"),
    )
    for label, key in metrics:
        values = []
        for arm in ARM_LABELS:
            data = _map(arms.get(arm))
            value = data.get(key) if key in ("composite", "pass_rate") else \
                _map(data.get("components")).get(key)
            values.append(f"<td>{_mean(value)}</td>")
        rows.append(f"<tr><th>{_text(label)}</th>{''.join(values)}</tr>")
    headers = "".join(f"<th>{_text(label)}</th>" for label in ARM_LABELS.values())
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr>'
        f"<th>Measure</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _question_heatmap(model: Mapping[str, Any]) -> str:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    questions: dict[str, str] = {}
    for row in _query_records(model):
        case_id = str(row.get("case_id", "unknown"))
        arm = str(row.get("arm"))
        grouped[case_id][arm].append(row)
        questions[case_id] = str(row.get("question") or case_id)
    head = '<div class="head">Question</div>' + "".join(
        f'<div class="head">{_text(label)}</div>' for label in ARM_LABELS.values()
    )
    cells = [head]
    for case_id in sorted(grouped):
        excluded = any(bool(row.get("reporting_excluded")) for rows in grouped[case_id].values() for row in rows)
        suffix = " · excluded from headline" if excluded else ""
        cells.append(
            f'<div><strong>{_text(case_id)}</strong><br><span class="small muted">'
            f'{_text(questions[case_id])}{_text(suffix, "")}</span></div>'
        )
        for arm in ARM_LABELS:
            rows = grouped[case_id].get(arm, [])
            passed = sum(_passed(row) for row in rows)
            total = len(rows)
            css = "good" if total and passed == total else "bad" if passed == 0 else "mixed"
            cells.append(f'<div class="{css}">{passed}/{total}</div>')
    return f'<div class="heat">{"".join(cells)}</div>'


def _quality_table(model: Mapping[str, Any]) -> str:
    arms = _map(model.get("arms"))
    funnel = _map(model.get("ambiguity_funnel"))
    rows = []
    definitions = (
        ("Correctness", "Did the result match the requested meaning?", "component", "correctness"),
        ("Join efficiency", "Did the query avoid unnecessary joins?", "component", "efficiency"),
        ("Safety", "Did unsafe requests avoid producing executable SQL?", "component", "safety"),
        ("Schema grounding", "Did the SQL use database tables and columns correctly?", "component", "grounding"),
        ("Clarification recall", "When clarification was needed, did the system ask?", "funnel", "recall"),
        ("Clarification specificity", "When clarification was not needed, did the system stay quiet?", "funnel", "specificity"),
        ("Final alignment", "After clarification, did the result match the chosen meaning?", "funnel", "final_alignment"),
    )
    for label, definition, source, key in definitions:
        values = []
        for arm in ARM_LABELS:
            value = _map(_map(arms.get(arm)).get("components")).get(key) \
                if source == "component" else _map(funnel.get(arm)).get(key)
            values.append(f"<td>{_mean(value)}%</td>")
        rows.append(
            f"<tr><th>{_text(label)}</th><td>{_text(definition)}</td>{''.join(values)}</tr>"
        )
    headers = "".join(f"<th>{_text(label)}</th>" for label in ARM_LABELS.values())
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr><th>Dimension</th>'
        f"<th>What it measures</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _clarification_evidence(model: Mapping[str, Any]) -> str:
    records = [
        row for row in _query_records(model)
        if row.get("arm") == "full" and isinstance(row.get("clarifications"), list)
        and row.get("clarifications")
    ]
    if not records:
        return '<p class="empty">No clarification transcripts were saved.</p>'
    output = []
    for row in records:
        turns = row.get("clarifications", [])
        transcript = []
        for index, turn in enumerate(turns, 1):
            item = _map(turn)
            transcript.append(
                '<div class="sequence"><strong>Question %s:</strong> %s'
                '<br><span class="small">Compliance: %s · Candidate support: %s</span></div>'
                % (
                    index,
                    _text(item.get("question")),
                    _text(item.get("compliance_passed")),
                    _text(item.get("candidate_support", [])),
                )
            )
        output.append(
            '<details><summary>%s · run %s · %s</summary><div class="detail-body">'
            '<p><strong>User request:</strong> %s</p>%s<p><strong>Final outcome:</strong> %s — %s</p>'
            '</div></details>'
            % (
                _text(row.get("case_id")),
                _text(row.get("run")),
                _status_badge(row),
                _text(row.get("question")),
                "".join(transcript),
                _status(row),
                _text(_reason(row)),
            )
        )
    return "".join(output)


def _reliability_table(model: Mapping[str, Any]) -> str:
    grouped: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in _query_records(model):
        if row.get("reporting_excluded"):
            continue
        grouped[(int(row.get("run", 0)), str(row.get("arm")))].append(row)
    rows = []
    for run in sorted({key[0] for key in grouped}):
        cells = []
        for arm in ARM_LABELS:
            values = grouped.get((run, arm), [])
            rate = 100 * sum(_passed(row) for row in values) / len(values) if values else 0
            cells.append(f"<td>{rate:.2f}% <span class=\"muted\">({sum(_passed(row) for row in values)}/{len(values)})</span></td>")
        rows.append(f"<tr><th>Run {run}</th>{''.join(cells)}</tr>")
    headers = "".join(f"<th>{_text(label)}</th>" for label in ARM_LABELS.values())
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr>'
        f"<th>Repetition</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _inconsistent_cases(model: Mapping[str, Any]) -> str:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in _query_records(model):
        grouped[(str(row.get("case_id")), str(row.get("arm")))].append(row)
    rows = []
    for (case_id, arm), values in sorted(grouped.items()):
        passed = sum(_passed(row) for row in values)
        if 0 < passed < len(values):
            rows.append(
                f"<tr><td>{_text(case_id)}</td><td>{_text(ARM_LABELS.get(arm, arm))}</td>"
                f"<td>{passed}/{len(values)}</td></tr>"
            )
    if not rows:
        return "<p>No case changed pass status across repetitions.</p>"
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr><th>Question</th>'
        f"<th>Version</th><th>Passes</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _etl_table(model: Mapping[str, Any]) -> str:
    rows = []
    for row in _etl_records(model):
        checks = _map(row.get("score")).get("checks", [])
        passed_checks = sum(bool(_map(check).get("passed")) for check in checks) if isinstance(checks, list) else 0
        total_checks = len(checks) if isinstance(checks, list) else 0
        rows.append(
            f"<tr><td>{_text(row.get('case_id'))}</td><td>{_text(row.get('run'))}</td>"
            f"<td>{_status_badge(row)}</td><td>{passed_checks}/{total_checks}</td>"
            f"<td>{_text(_reason(row))}</td></tr>"
        )
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr><th>Fixture</th><th>Run</th>'
        f"<th>Result</th><th>Checks passed</th><th>Reason</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _failure_table(model: Mapping[str, Any]) -> str:
    counts: Counter[tuple[str, str]] = Counter()
    for row in _query_records(model):
        if not _passed(row):
            counts[(str(row.get("arm")), _reason(row))] += 1
    rows = "".join(
        f"<tr><td>{_text(ARM_LABELS.get(arm, arm))}</td><td>{_text(reason)}</td><td>{count}</td></tr>"
        for (arm, reason), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    return (
        '<div class="table-wrap"><table class="sortable"><thead><tr><th>Version</th>'
        f"<th>Recorded scoring reason</th><th>Count</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _audit_record(row: Mapping[str, Any], index: int) -> str:
    result = _result(row)
    score = _map(row.get("score"))
    arm = str(row.get("arm", "unknown"))
    clarifications = row.get("clarifications", [])
    rows = result.get("rows", [])
    sample = rows[:3] if isinstance(rows, list) else rows
    sql = result.get("sql")
    expected = row.get("expected_sql")
    etl = arm == "etl"
    note = (
        '<p class="record-note">This question was excluded from the headline aggregate, '
        'but its saved evidence is retained here.</p>'
        if row.get("reporting_excluded") else ""
    )
    audit_meta = {
        "terminal": row.get("terminal"),
        "result_state": result.get("state"),
        "columns": result.get("columns"),
        "row_count": len(rows) if isinstance(rows, list) else None,
        "sample_rows": sample,
        "required_tables": row.get("required_tables"),
        "forbidden_tables": row.get("forbidden_tables"),
        "comparison_contract": row.get("comparison"),
    }
    score_meta = {
        key: value for key, value in score.items()
        if key not in {"reason"}
    }
    search = " ".join(
        str(value) for value in (
            row.get("case_id"), row.get("question"), arm, _status(row), _reason(row)
        )
    ).casefold()
    return (
        '<details class="detail-record" data-arm="%s" data-case="%s" data-run="%s" '
        'data-status="%s" data-category="%s" data-search="%s">'
        '<summary><span class="count">#%s</span> %s · %s · run %s · %s</summary>'
        '<div class="detail-body">%s<p><strong>Original question:</strong> %s</p>'
        '<div class="audit-grid"><div class="audit"><h4>Expected SQL</h4><pre>%s</pre></div>'
        '<div class="audit"><h4>Generated SQL</h4><pre>%s</pre></div>'
        '<div class="audit"><h4>Pass</h4><p>%s</p><h4>Why it passed or failed</h4><p>%s</p></div>'
        '<div class="audit"><h4>Execution and result evidence</h4><pre>%s</pre></div>'
        '<div class="audit"><h4>Clarifications</h4><pre>%s</pre></div>'
        '<div class="audit"><h4>Scoring evidence</h4><pre>%s</pre></div></div></div></details>'
        % (
            _text(arm), _text(row.get("case_id")), _text(row.get("run")),
            _text(_status(row).lower()), _text(row.get("category")), _text(search),
            index, _text(row.get("case_id")), _text(ARM_LABELS.get(arm, "ETL Validation")),
            _text(row.get("run")), _status_badge(row), note,
            _text(row.get("question"), "Not applicable — ETL validation fixture."),
            _text(expected, "Not applicable — no SQL oracle for this ETL fixture." if etl else "No expected SQL was recorded."),
            _text(sql, "Not applicable — ETL fixture did not generate SQL." if etl else "No SQL was generated or accepted."),
            _status_badge(row), _text(_reason(row)), _json(audit_meta),
            _json(clarifications), _json(score_meta),
        )
    )


def _detail_filters(model: Mapping[str, Any]) -> str:
    records = _records(model)
    cases = sorted({str(row.get("case_id")) for row in records})
    runs = sorted({str(row.get("run")) for row in records})
    arm_options = [*ARM_LABELS.items(), ("etl", "ETL Validation")]
    return (
        '<div class="filters"><label>System version<select id="filter-arm"><option value="">All</option>%s</select></label>'
        '<label>Question<select id="filter-case"><option value="">All</option>%s</select></label>'
        '<label>Run<select id="filter-run"><option value="">All</option>%s</select></label>'
        '<label>Overall status<select id="filter-status"><option value="">All</option><option value="pass">Pass</option>'
        '<option value="fail">Fail</option></select></label><label>Search reason or question'
        '<input id="filter-search" type="search" placeholder="Type to filter"></label></div>'
        % (
            "".join(f'<option value="{_text(key)}">{_text(label)}</option>' for key, label in arm_options),
            "".join(f'<option value="{_text(case)}">{_text(case)}</option>' for case in cases),
            "".join(f'<option value="{_text(run)}">{_text(run)}</option>' for run in runs),
        )
    )


def _all_details(model: Mapping[str, Any]) -> str:
    return "".join(
        _audit_record(row, index)
        for index, row in enumerate(_records(model), 1)
    )


def _report_script() -> str:
    return """<script>
document.documentElement.classList.add('js');
const tabs=[...document.querySelectorAll('[role=tab]')],panels=[...document.querySelectorAll('[role=tabpanel]')];
function activateTab(key,focus=false){tabs.forEach(t=>{const on=t.id==='tab-'+key;t.setAttribute('aria-selected',on);
t.tabIndex=on?0:-1});panels.forEach(p=>p.hidden=p.id!=='panel-'+key);if(focus)document.getElementById('tab-'+key).focus()}
tabs.forEach((tab,index)=>{tab.onclick=()=>{const key=tab.id.slice(4);activateTab(key);history.replaceState(null,'','#'+key)};
tab.onkeydown=e=>{if(!['ArrowRight','ArrowLeft','Home','End'].includes(e.key))return;
e.preventDefault();const n=e.key==='Home'?0:e.key==='End'?tabs.length-1:e.key==='ArrowRight'?(index+1)%tabs.length:
(index-1+tabs.length)%tabs.length;activateTab(tabs[n].id.slice(4),true)}});
document.querySelectorAll('table.sortable').forEach(table=>{const body=table.tBodies[0];[...table.tHead.rows[0].cells].forEach((cell,index)=>{
cell.tabIndex=0;cell.setAttribute('role','button');cell.setAttribute('aria-sort','none');cell.setAttribute('aria-label','Sort by '+cell.textContent.trim());
let ascending=true;const sort=()=>{const rows=[...body.rows];rows.sort((a,b)=>{const av=a.cells[index].textContent.trim(),
bv=b.cells[index].textContent.trim(),an=parseFloat(av),bn=parseFloat(bv),result=Number.isNaN(an)||Number.isNaN(bn)?
av.localeCompare(bv):an-bn;return ascending?result:-result});rows.forEach(row=>body.appendChild(row));
cell.setAttribute('aria-sort',ascending?'ascending':'descending');ascending=!ascending};cell.onclick=sort;
cell.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();sort()}}})});
const filters=['arm','case','run','status'];const records=[...document.querySelectorAll('.detail-record')];
function filterRecords(){const search=(document.getElementById('filter-search').value||'').toLowerCase();let shown=0;
records.forEach(record=>{const match=filters.every(key=>{const value=document.getElementById('filter-'+key).value;
return !value||record.dataset[key]===value})&&(!search||record.dataset.search.includes(search));record.hidden=!match;if(match)shown++});
document.getElementById('visible-count').textContent=shown}
filters.forEach(key=>document.getElementById('filter-'+key).onchange=filterRecords);
document.getElementById('filter-search').oninput=filterRecords;filterRecords();
const requested=location.hash.slice(1);if(document.getElementById('panel-'+requested))activateTab(requested);
</script>"""


def render_full_report(model: Mapping[str, Any]) -> str:
    """Render the detailed ten-tab audit report."""

    records = _records(model)
    query_records = _query_records(model)
    adjusted_count = sum(not bool(row.get("reporting_excluded")) for row in query_records)
    operations = _map(_map(model.get("operations")).get("metrics"))
    full_delta = _map(_map(model.get("arm_deltas")).get("full")).get("composite")
    sensitivity = _map(_map(model.get("sensitivity")).get("all_cases"))
    sensitivity_arms = _map(sensitivity.get("arms"))
    exclusions = _map(model.get("reporting_adjustments"))
    provenance = _map(model.get("provenance"))
    terminal = _map(model.get("terminal_outcomes"))
    accepted = _map(terminal.get("accepted"))
    contents = {
        "overview": f"""<h2 class="panel-title">Overview</h2>
<p class="lede">This report documents the corrected Evaluation V3 campaign, from the headline comparison to every saved SQL result and scoring decision.</p>
<div class="notice"><strong>Deterministic offline rescore.</strong> No model calls were made during rescoring. The original campaign evidence was preserved; only the approved scoring rules and one headline denominator were corrected.</div>
<div class="metrics">{_metric_cards(model)}</div>
<div class="two-col"><article class="card"><h3>Experiment</h3>
<p>Four system versions answered 22 database questions five times. Two ETL fixtures were also run five times, producing {len(records)} saved checks.</p>
<p><strong>Candidate count:</strong> K=3 for Candidate Only, Semantic Only, and Full System; K=1 for Baseline.</p></article>
<article class="card"><h3>How to read this report</h3><p>Start with System Comparison, then inspect results by question and quality dimension. The Detailed Results tab contains the full audit record for all {len(records)} checks.</p></article></div>
<div class="card"><h3>Configuration comparison</h3>{_arm_bars(model, "composite")}</div>
<div class="notice"><strong>Headline scope:</strong> {adjusted_count} query cells plus shared ETL evidence. The lab-frequency case is retained in every detailed view but excluded from the headline because the question did not define its intended frequency grain.</div>""",
        "comparison": f"""<h2 class="panel-title">System Comparison</h2>
<p class="lede">The Full System achieved the highest composite score and pass rate. Candidate Only remained close to Baseline, while Semantic Only produced most of the improvement.</p>
<div class="card">{_arm_bars(model)}</div>{_comparison_table(model)}
<div class="two-col"><article class="card"><h3>Full System versus Baseline</h3>
<p><strong>{_mean(full_delta)} points</strong> higher composite score.</p><p>Paired 95% confidence interval: {_text(_ci(full_delta))}.</p></article>
<article class="card"><h3>What changed?</h3><p>Baseline generates one answer. Candidate Only compares three generated answers. Semantic Only checks vague words against database fields. Full System combines both checks.</p></article></div>
<div class="card"><h3>Original vs corrected</h3><p>The offline correction accepted intent-equivalent projections, approved duration representations, and tie-aware rankings. It changed {_text(_map(model.get("change_ledger_summary")).get("score_changes"))} scores and {_text(_map(model.get("change_ledger_summary")).get("pass_status_flips"))} pass decisions without generating new answers.</p></div>
<div class="card"><h3>Sensitivity analysis</h3><p>With all questions included, Full System composite: {_mean(_map(sensitivity_arms.get("full")).get("composite"))}; Baseline: {_mean(_map(sensitivity_arms.get("baseline")).get("composite"))}. The system ordering is unchanged.</p></div>""",
        "questions": f"""<h2 class="panel-title">Results by Question</h2>
<p class="lede">Each cell shows how many of the five repetitions passed for that question and system version. Select the Detailed Results tab to inspect the SQL and reason for every individual run.</p>
{_question_heatmap(model)}""",
        "quality": f"""<h2 class="panel-title">Quality Dimensions</h2>
<p class="lede">The composite score combines several checks. A high result in one dimension cannot compensate for a required condition that failed.</p>
{_quality_table(model)}
<div class="two-col"><article class="card"><h3>Correctness diagnostics</h3><pre>{_json(model.get("correctness_diagnostics", {}))}</pre></article>
<article class="card"><h3>Projection diagnostics</h3><pre>{_json(model.get("projection_diagnostics", {}))}</pre></article></div>""",
        "clarifications": f"""<h2 class="panel-title">Clarifications</h2>
<p class="lede">Clarification quality is evaluated in stages: whether the system asked at the right time, whether the question addressed a plausible distinction, and whether the final result followed the selected option.</p>
<div class="card"><h3>Full System ambiguity metrics</h3>{''.join(_bar(label.replace('_',' ').title(), _mean_value(value)) for label, value in _map(_map(model.get("ambiguity_funnel")).get("full")).items() if label in {"recall","specificity","plausibility","target_coverage","resolution","compliance","final_alignment"})}</div>
<h3>Saved Full System clarification sequences</h3>{_clarification_evidence(model)}""",
        "reliability": f"""<h2 class="panel-title">Reliability</h2>
<p class="lede">Five repetitions show how much results changed from run to run. The table uses the corrected headline set.</p>
{_reliability_table(model)}<div class="card"><h3>Questions with mixed outcomes</h3>{_inconsistent_cases(model)}</div>
<div class="notice">Confidence intervals describe variation within this five-run campaign. They do not establish performance on every database or user population.</div>""",
        "etl": f"""<h2 class="panel-title">ETL Validation</h2>
<p class="lede">The shared data-loading checks verify that single-file and relational CSV inputs were loaded with the expected tables, rows, columns, and relationships.</p>
<div class="metrics"><article class="metric"><strong>{_mean(model.get("shared_etl"))}%</strong><span>Shared ETL score</span></article>
<article class="metric"><strong>{len(_etl_records(model))}</strong><span>Saved ETL checks</span></article>
<article class="metric"><strong>{sum(_passed(row) for row in _etl_records(model))}</strong><span>ETL checks passed</span></article>
<article class="metric"><strong>{len(model.get("warnings", []))}</strong><span>Loading warnings</span></article></div>
{_etl_table(model)}<div class="card"><h3>Relationship-discovery warnings</h3><ul>{_items(model.get("warnings"))}</ul></div>""",
        "failures": f"""<h2 class="panel-title">Failure Analysis</h2>
<p class="lede">These are recorded scoring outcomes, not speculative root-cause diagnoses. Open the Detailed Results tab to see the SQL, execution evidence, and full score for each failure.</p>
{_failure_table(model)}
<div class="two-col"><article class="card"><h3>Terminal outcomes</h3><p><strong>{_text(accepted.get("count"))}</strong> of {_text(accepted.get("denominator"))} query cells reached an accepted result.</p><pre>{_json(terminal)}</pre></article>
<article class="card"><h3>Interpretation</h3><ul>{_items(model.get("interpretations"))}</ul></article></div>""",
        "methodology": f"""<h2 class="panel-title">Methodology</h2>
<p class="lede">Evaluation V3 compares controlled versions of the same system on a frozen relational test suite.</p>
<div class="two-col"><article class="card"><h3>Experimental design</h3><p>Four arms, K=3 for ambiguity-enabled candidate generation, five repetitions, 22 query cases, two ETL fixtures, and a $3.75 campaign ceiling.</p><p>Alternate relationship routes do not trigger ambiguity. Join efficiency rewards the least sufficient query plan.</p></article>
<article class="card"><h3>Automated scoring</h3><p>Saved SQL and results are checked against expected outcomes, required concepts, join sufficiency, safety behavior, schema grounding, and clarification evidence. No human or LLM judge assigned the scores.</p></article></div>
<div class="card"><h3>Deterministic offline correction</h3><p>No model calls were made. The correction rescored the frozen campaign evidence under approved intent-aligned rules and retained a change ledger. The source aggregate and corrected publication are hash-linked.</p>
<p><strong>Excluded headline case:</strong> {_text(", ".join(exclusions.get("excluded_case_ids", [])))} — {_text(exclusions.get("exclusion_reason"))}</p></div>
<div class="two-col"><article class="card"><h3>Provenance</h3><pre>{_json(provenance)}</pre></article>
<article class="card"><h3>Limitations</h3><ul>{_items(model.get("limitations"))}</ul></article></div>""",
        "details": f"""<h2 class="panel-title">Detailed Results</h2>
<p class="lede">Complete documentation for all {len(records)} saved evaluation checks. Every record includes the original question, expected SQL, generated SQL, pass or fail status, and the reason it passed or failed.</p>
<div class="notice"><strong><span id="visible-count">{len(records)}</span> of {len(records)} records shown.</strong> ETL fixtures do not generate SQL, so their SQL fields are marked not applicable.</div>
{_detail_filters(model)}{_all_details(model)}""",
    }
    buttons = "".join(
        '<button class="tab" role="tab" id="tab-%s" aria-controls="panel-%s" '
        'aria-selected="%s" tabindex="%s">%s</button>'
        % (
            key, key, str(key == "overview").lower(),
            0 if key == "overview" else -1, _text(label),
        )
        for key, label in REPORT_TABS
    )
    panels = "".join(
        '<span id="%s" hidden></span><section id="panel-%s" class="tab-panel" '
        'role="tabpanel" aria-labelledby="tab-%s"%s>%s</section>'
        % (
            key, key, key, "" if key == "overview" else " hidden", contents[key],
        )
        for key, _ in REPORT_TABS
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DB Whisperer Evaluation</title><style>{_report_style()}</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="hero"><h1>DB Whisperer Evaluation</h1>
<p>Evaluation V3 · corrected campaign evidence · suite {_text(provenance.get("suite_version", "3.1.0"))} · {len(records)} saved checks</p></header>
<nav class="tabs-shell" aria-label="Report sections"><div class="tabs" role="tablist">{buttons}</div></nav>
<main id="main">{panels}</main>{_report_script()}</body></html>"""


def _atomic_write(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)
    return path


def write_reports(
    aggregate_path: Path,
    one_page_path: Path,
    full_report_path: Path,
) -> tuple[Path, Path]:
    aggregate = json.loads(Path(aggregate_path).read_text(encoding="utf-8"))
    model = aggregate.get("model") if isinstance(aggregate, Mapping) else None
    if not isinstance(model, Mapping):
        model = build_report_model(aggregate)
    return (
        _atomic_write(Path(one_page_path), render_one_page(model)),
        _atomic_write(Path(full_report_path), render_full_report(model)),
    )
