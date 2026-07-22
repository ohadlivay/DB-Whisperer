"""Aggregate Protocol 2 clarification ratings into metrics and an HTML report.

Reads the annotation set (``extract.py``'s output, for each item's hidden
dataset/mechanism) and every rater's filled ``rating_*.csv``, then reports per
dimension — clarity, discriminativeness, faithfulness, naturalness — the mean
score and Krippendorff's alpha, split by dataset and by the mechanism that
produced the clarification.

The headline the protocol predicts is **naturalness by dataset**: high for
BikeStores and the clean lab-dictionary cases, low for the dense MIMIC pairs
whose "longest path" routes through an unrelated table — the evidence that
motivates semantic path pruning.

Like the Protocol 1 analyzer, an empty group is ``n/a`` rather than a misleading
zero, alpha is ``None`` (not a number) when there is too little agreement data,
and the report states how many raters and items stand behind each figure.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
from typing import Any

ANNOTATION_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ANNOTATION_DIR))

from reliability import alpha_for_field  # noqa: E402


DIMENSIONS = ("clarity", "discriminativeness", "faithfulness", "naturalness")
MIN_SCORE, MAX_SCORE = 1, 5
MIN_RATERS = 3  # the protocol asks for at least three
MIN_STABLE_ITEMS = 15  # below this, alpha over so few units is indicative only


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_annotation_set(path: Path) -> dict[str, dict[str, Any]]:
    """Return ``{item_id: {dataset, mechanism, ...}}`` from an annotation set."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {item["item_id"]: item for item in payload.get("items", [])}


def load_rater_csv(path: Path) -> tuple[str, dict[str, dict[str, float]], int]:
    """Load one rater's sheet: (rater name, {item_id: {dim: score}}, invalid).

    Blank cells are 'not rated' and skipped silently; a value outside 1-5 or a
    non-number is counted as invalid and skipped, so a typo never silently
    becomes a real score.
    """
    rater = path.stem
    if rater.startswith("rating_"):
        rater = rater[len("rating_") :]
    scores: dict[str, dict[str, float]] = {}
    invalid = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            item = (row.get("item_id") or "").strip()
            if not item:
                continue
            for dim in DIMENSIONS:
                raw = (row.get(dim) or "").strip()
                if not raw:
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    invalid += 1
                    continue
                if not MIN_SCORE <= value <= MAX_SCORE:
                    invalid += 1
                    continue
                scores.setdefault(item, {})[dim] = value
    return rater, scores, invalid


# --------------------------------------------------------------------------
# Aggregation (pure)
# --------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _group_mean(
    ratings: dict[str, dict[str, float]],
    meta: dict[str, dict[str, Any]],
    key: str,
) -> dict[str, dict[str, Any]]:
    """Mean score grouped by an item metadata field (dataset or mechanism)."""
    groups: dict[str, list[float]] = {}
    for item_id, by_rater in ratings.items():
        label = str(meta.get(item_id, {}).get(key, "unknown"))
        groups.setdefault(label, []).extend(by_rater.values())
    return {
        label: {"n_ratings": len(values), "mean": _round(_mean(values))}
        for label, values in sorted(groups.items())
    }


def aggregate(
    meta: dict[str, dict[str, Any]],
    rater_scores: dict[str, dict[str, dict[str, float]]],
    invalid_scores: int = 0,
) -> dict[str, Any]:
    """Compute per-dimension means, per-group means, and alpha.

    Pure over its inputs so it is unit-testable without files. ``rater_scores``
    is ``{rater: {item_id: {dimension: score}}}``.
    """
    raters = sorted(rater_scores)
    rated_items: set[str] = set()
    unknown_items: set[str] = set()

    dimensions: dict[str, Any] = {}
    for dim in DIMENSIONS:
        # {item_id: {rater: score}} for this dimension only.
        item_ratings: dict[str, dict[str, float]] = {}
        for rater, by_item in rater_scores.items():
            for item_id, by_dim in by_item.items():
                if dim in by_dim:
                    item_ratings.setdefault(item_id, {})[rater] = by_dim[dim]
                    rated_items.add(item_id)
                    if item_id not in meta:
                        unknown_items.add(item_id)
        all_scores = [s for raters_ in item_ratings.values() for s in raters_.values()]
        pairable = sum(1 for r in item_ratings.values() if len(r) >= 2)
        dimensions[dim] = {
            "overall": {
                "n_ratings": len(all_scores),
                "n_items": len(item_ratings),
                "mean": _round(_mean(all_scores)),
                "alpha": _round(alpha_for_field(item_ratings), 3),
            },
            "items_pairable": pairable,
            "by_dataset": _group_mean(item_ratings, meta, "dataset"),
            "by_mechanism": _group_mean(item_ratings, meta, "mechanism"),
        }

    unrated = sorted(set(meta) - rated_items)
    caveats: list[str] = []
    if len(raters) < MIN_RATERS:
        caveats.append(
            f"Only {len(raters)} rater(s); the protocol asks for at least "
            f"{MIN_RATERS}. Krippendorff's alpha is unstable below that and "
            "should be read as indicative only."
        )
    # Max, not min: a dimension nobody rated should not make the whole study
    # look like it has no reliability data. Alpha is undefined everywhere only
    # when no dimension has a single item two raters both scored.
    any_pairable = max(
        (d["items_pairable"] for d in dimensions.values()), default=0
    )
    if any_pairable == 0 and raters:
        caveats.append(
            "No item was rated by two or more raters, so alpha is undefined "
            "(shown as n/a). Reliability needs overlapping ratings."
        )
    elif 0 < len(rated_items) < MIN_STABLE_ITEMS:
        caveats.append(
            f"Only {len(rated_items)} clarification(s) were rated; alpha and the "
            "means over so few items are indicative, not stable. Collect more "
            "(run more A/B cases through extract.py) before drawing conclusions."
        )
    if unrated:
        caveats.append(
            f"{len(unrated)} item(s) in the set received no rating and are "
            "excluded from every mean."
        )
    if unknown_items:
        caveats.append(
            f"{len(unknown_items)} rated item id(s) are not in the annotation "
            "set (a stale or mismatched sheet); their dataset/mechanism is "
            "'unknown'."
        )
    if invalid_scores:
        caveats.append(
            f"{invalid_scores} cell(s) held a non-number or out-of-range value "
            "and were skipped."
        )

    return {
        "raters": {"count": len(raters), "names": raters},
        "items": {"total": len(meta), "rated": len(rated_items)},
        "dimensions": dimensions,
        "data_quality": {
            "invalid_scores": invalid_scores,
            "items_unrated": unrated,
            "unknown_item_ids": sorted(unknown_items),
        },
        "caveats": caveats,
    }


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------


def _num(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _alpha(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "n/a"


def _card(label: str, value: str, sub: str) -> str:
    return (
        '<div class="card">'
        f'<div class="card-value">{html.escape(value)}</div>'
        f'<div class="card-label">{html.escape(label)}</div>'
        f'<div class="card-sub">{html.escape(sub)}</div>'
        "</div>"
    )


def render_html(summary: dict[str, Any], generated_at: str) -> str:
    """Render the annotation summary as a standalone HTML page."""
    dims = summary["dimensions"]
    datasets = sorted(
        {
            ds
            for dim in dims.values()
            for ds in dim["by_dataset"]
        }
    )

    cards = "".join(
        _card(
            dim.replace("_", " ").title(),
            _num(dims[dim]["overall"]["mean"]),
            f"α={_alpha(dims[dim]['overall']['alpha'])} · "
            f"{dims[dim]['overall']['n_ratings']} ratings",
        )
        for dim in DIMENSIONS
    )

    def matrix_rows() -> str:
        rows = []
        for dim in DIMENSIONS:
            block = dims[dim]
            cells = "".join(
                f"<td>{_num(block['by_dataset'].get(ds, {}).get('mean'))}</td>"
                for ds in datasets
            )
            rows.append(
                "<tr>"
                f"<td>{html.escape(dim.replace('_', ' ').title())}</td>"
                f"<td>{_num(block['overall']['mean'])}</td>"
                f"<td class='delta'>{_alpha(block['overall']['alpha'])}</td>"
                f"{cells}"
                "</tr>"
            )
        return "".join(rows)

    def mechanism_rows() -> str:
        mechs = sorted(
            {m for dim in dims.values() for m in dim["by_mechanism"]}
        )
        if not mechs:
            return "<tr><td colspan='5' class='muted'>No ratings yet.</td></tr>"
        rows = []
        for mech in mechs:
            cells = "".join(
                f"<td>{_num(dims[dim]['by_mechanism'].get(mech, {}).get('mean'))}</td>"
                for dim in DIMENSIONS
            )
            rows.append(f"<tr><td>{html.escape(mech)}</td>{cells}</tr>")
        return "".join(rows)

    dataset_headers = "".join(f"<th>{html.escape(ds)}</th>" for ds in datasets)
    caveats = "".join(f"<li>{html.escape(c)}</li>" for c in summary["caveats"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB Whisperer — Clarification quality (Protocol 2)</title>
<style>
  :root {{ --bg:#f6f7f9; --panel:#fff; --ink:#1b2130; --muted:#6b7280;
    --line:#e5e7eb; --accent:#2f6f4f; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  .wrap {{ max-width: 940px; margin: 0 auto; padding: 32px 20px 64px; }}
  header h1 {{ margin: 0 0 4px; font-size: 24px; }}
  header p {{ margin: 0; color: var(--muted); }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
    gap:12px; margin:24px 0; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:16px; }}
  .card-value {{ font-size:26px; font-weight:650; color:var(--accent); }}
  .card-label {{ font-size:13px; font-weight:600; margin-top:2px; }}
  .card-sub {{ font-size:12px; color:var(--muted); margin-top:4px; }}
  section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:18px 20px; margin:16px 0; }}
  section h2 {{ margin:0 0 12px; font-size:16px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
    font-size:14px; }}
  th {{ color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
    letter-spacing:.03em; }}
  td.delta {{ font-weight:650; }}
  .muted {{ color: var(--muted); }}
  .caveats li {{ margin:6px 0; }}
  footer {{ color:var(--muted); font-size:12px; margin-top:24px; text-align:center; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#12151b; --panel:#1a1f27; --ink:#e6e9ef; --muted:#9aa4b2;
      --line:#2a313c; --accent:#6fbf98; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Clarification quality — expert annotation (Protocol 2)</h1>
    <p>DB Whisperer · {html.escape(str(summary['raters']['count']))} rater(s) ·
       {html.escape(str(summary['items']['rated']))}/{html.escape(str(summary['items']['total']))}
       items rated · generated {html.escape(generated_at)}</p>
  </header>

  <div class="cards">{cards}</div>

  <section>
    <h2>Mean score (1–5) and agreement (α) by dimension × dataset</h2>
    <table>
      <thead><tr><th>Dimension</th><th>Overall</th><th>α</th>{dataset_headers}</tr></thead>
      <tbody>{matrix_rows()}</tbody>
    </table>
    <p class="muted" style="margin:10px 0 0;font-size:12px">
      Watch the <strong>naturalness</strong> row: a low value on a dense dataset
      is the density finding — the option pair is an artifact a real user would
      not plausibly face, motivating semantic path pruning.</p>
  </section>

  <section>
    <h2>Mean score by mechanism</h2>
    <table>
      <thead><tr><th>Mechanism</th><th>Clarity</th><th>Discriminativeness</th>
        <th>Faithfulness</th><th>Naturalness</th></tr></thead>
      <tbody>{mechanism_rows()}</tbody>
    </table>
  </section>

  <section>
    <h2>How to read this</h2>
    <ul class="caveats">{caveats or '<li class=muted>No caveats flagged.</li>'}</ul>
    <p class="muted" style="font-size:12px">α is Krippendorff's alpha (interval):
      1.0 perfect agreement, 0 chance, and it can go negative. It is defined only
      over items two or more raters scored.</p>
  </section>

  <footer>Protocol 2 · standalone report · can be nested into the docs site.</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_summary(summary: dict[str, Any]) -> None:
    print(
        f"\nRaters: {summary['raters']['count']}   Items rated: "
        f"{summary['items']['rated']}/{summary['items']['total']}"
    )
    for dim in DIMENSIONS:
        block = summary["dimensions"][dim]["overall"]
        # Plain "alpha" in console text: the glyph breaks Windows cp1252 stdout.
        print(
            f"  {dim:<18} mean {_num(block['mean'])}  "
            f"alpha {_alpha(block['alpha'])}  ({block['n_ratings']} ratings)"
        )
    for caveat in summary["caveats"]:
        print(f"  ! {caveat}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate Protocol 2 clarification ratings into a report.",
    )
    parser.add_argument(
        "--annotation-set",
        type=Path,
        default=ANNOTATION_DIR / "results" / "annotation_set.json",
    )
    parser.add_argument(
        "--annotations-dir",
        type=Path,
        default=ANNOTATION_DIR / "results",
        help="Directory of filled rating_*.csv sheets.",
    )
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-html", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    set_path = args.annotation_set.expanduser().resolve()
    if not set_path.is_file():
        print(
            f"Annotation set not found: {set_path}. Run extract.py first.",
            file=sys.stderr,
        )
        return 1
    meta = load_annotation_set(set_path)

    sheets = sorted(args.annotations_dir.expanduser().resolve().glob("rating_*.csv"))
    rater_scores: dict[str, dict[str, dict[str, float]]] = {}
    invalid_total = 0
    for sheet in sheets:
        rater, scores, invalid = load_rater_csv(sheet)
        # A blank template counts as no ratings; skip it so it is not a "rater".
        if scores:
            rater_scores[rater] = scores
        invalid_total += invalid
    if not rater_scores:
        print(
            f"No filled rating_*.csv sheets in {args.annotations_dir}. Fill a "
            "sheet's score columns first.",
            file=sys.stderr,
        )
        return 1

    summary = aggregate(meta, rater_scores, invalid_total)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out_dir = args.annotations_dir.expanduser().resolve()
    out_json = (args.out_json or out_dir / "annotation_summary.json").expanduser().resolve()
    out_html = (args.out_html or out_dir / "annotation_report.html").expanduser().resolve()
    out_json.write_text(
        json.dumps({"generated_at": generated_at, "summary": summary}, indent=2)
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
