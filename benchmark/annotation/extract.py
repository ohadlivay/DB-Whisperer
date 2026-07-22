"""Extract clarifications to be rated in the Protocol 2 annotation study.

Protocol 2 (``../HUMAN_IN_THE_LOOP.md``) needs no live participants: it has a
few experts score the clarifying questions the system already produced. This
script gathers those questions into a blind rating sheet.

Two sources:

* ``--from-ab-reports`` — the model-generated clarifications recorded verbatim in
  ``benchmark/results/ab_*.json`` (``cases[].full.clarifications``). This is the
  intended source: it rates what the model actually emits on the day.
* ``--from-scenarios`` — the authored clarifications frozen in the Protocol 1
  ``scenarios.json``. Useful as a warm-up, or when no A/B run exists yet.

Identical clarifications are de-duplicated so a rater never scores the same
question twice, and each keeps a stable content id so multiple raters' sheets
align. The rating sheet is deliberately **blind**: it carries the question and
its two options but not the dataset or the mechanism that produced it, so a
rater cannot be primed by "this is the dense MIMIC one".
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ANNOTATION_DIR = Path(__file__).resolve().parent
BENCHMARK_DIR = ANNOTATION_DIR.parent
DEFAULT_AB_REPORTS = BENCHMARK_DIR / "results"
DEFAULT_SCENARIOS = BENCHMARK_DIR / "study" / "scenarios.json"
# Outputs land in results/ so they inherit benchmark/.gitignore: the annotation
# set, blank sheets, filled sheets, and reports are all generated or study data.
DEFAULT_OUT_DIR = ANNOTATION_DIR / "results"

DIMENSIONS = ("clarity", "discriminativeness", "faithfulness", "naturalness")


def item_id(question: str, options: list[str]) -> str:
    """Stable content id for one clarification (question + ordered options).

    Option order is preserved, not sorted: for a join-path clarification the two
    options are path-ordered and swapping them would be a different question.
    """
    payload = "\x1f".join([question.strip(), *[o.strip() for o in options]])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def _item(
    question: str,
    options: list[str],
    dataset: str,
    mechanism: str,
    source: str,
) -> dict[str, Any] | None:
    """One annotation item, or None if it is not a well-formed two-option ask."""
    question = (question or "").strip()
    cleaned = [str(o).strip() for o in options if str(o).strip()]
    if not question or len(cleaned) != 2:
        return None
    return {
        "item_id": item_id(question, cleaned),
        "question": question,
        "options": cleaned,
        "dataset": dataset,
        "mechanism": mechanism or "unknown",
        "source": source,
    }


def clarifications_from_ab_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull every recorded clarification from one loaded ``ab_*.json`` report."""
    dataset = str(report.get("suite") or report.get("dataset") or "unknown")
    items: list[dict[str, Any]] = []
    for case in report.get("cases", []):
        full = case.get("full", {}) if isinstance(case, dict) else {}
        for clar in full.get("clarifications", []) or []:
            item = _item(
                clar.get("question", ""),
                clar.get("options", []),
                dataset,
                clar.get("mechanism", "unknown"),
                "ab_report",
            )
            if item is not None:
                items.append(item)
    return items


def clarifications_from_scenarios(scenarios: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull authored clarifications from a Protocol 1 ``scenarios.json``."""
    items: list[dict[str, Any]] = []
    for scenario in scenarios.get("scenarios", []):
        if not scenario.get("ambiguous"):
            continue
        options = [
            interp.get("option_label", "")
            for interp in scenario.get("interpretations", [])
        ]
        item = _item(
            scenario.get("clarification_question", ""),
            options,
            str(scenario.get("dataset", "unknown")),
            "authored",
            "scenarios",
        )
        if item is not None:
            items.append(item)
    return items


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse identical clarifications, counting how often each was seen.

    Sorted by id so the output is deterministic regardless of input order.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        existing = by_id.get(item["item_id"])
        if existing is None:
            by_id[item["item_id"]] = {**item, "occurrences": 1}
        else:
            existing["occurrences"] += 1
    return [by_id[key] for key in sorted(by_id)]


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _ordered_for(items: list[dict[str, Any]], rater: str | None) -> list[dict[str, Any]]:
    """Blind row order for a rater: shuffled and seeded by rater name.

    A per-rater order keeps one rater's sheet from priming the next; with no
    rater given, the id-sorted order is used unchanged.
    """
    if not rater:
        return items
    from random import Random

    ordered = list(items)
    Random(_seed(rater)).shuffle(ordered)
    return ordered


def write_rating_sheet(
    items: list[dict[str, Any]], path: Path, rater: str | None = None
) -> None:
    """Write a blind CSV sheet: question + options + empty score columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item_id", "question", "option_a", "option_b", *DIMENSIONS])
        for item in _ordered_for(items, rater):
            writer.writerow(
                [item["item_id"], item["question"], *item["options"], "", "", "", ""]
            )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract clarifications into a blind Protocol 2 rating sheet.",
    )
    parser.add_argument(
        "--from-ab-reports",
        nargs="?",
        const=DEFAULT_AB_REPORTS,
        type=Path,
        help="Directory of ab_*.json reports (default: benchmark/results).",
    )
    parser.add_argument(
        "--from-scenarios",
        nargs="?",
        const=DEFAULT_SCENARIOS,
        type=Path,
        help="A scenarios.json to read authored clarifications from.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--raters",
        default="",
        help="Comma-separated rater names; emits one blind sheet each.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.from_ab_reports and not args.from_scenarios:
        print(
            "Choose a source: --from-ab-reports and/or --from-scenarios.",
            file=sys.stderr,
        )
        return 2

    collected: list[dict[str, Any]] = []
    if args.from_ab_reports:
        reports = sorted(args.from_ab_reports.glob("ab_*.json"))
        if not reports:
            print(
                f"No ab_*.json reports in {args.from_ab_reports}. Run "
                "benchmark/ab_run.py first, or use --from-scenarios.",
                file=sys.stderr,
            )
            return 1
        for report_path in reports:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            collected.extend(clarifications_from_ab_report(report))
    if args.from_scenarios:
        scenarios = json.loads(args.from_scenarios.read_text(encoding="utf-8"))
        collected.extend(clarifications_from_scenarios(scenarios))

    items = dedupe(collected)
    if not items:
        print("No two-option clarifications found to rate.", file=sys.stderr)
        return 1

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    set_path = out_dir / "annotation_set.json"
    set_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    raters = [r.strip() for r in args.raters.split(",") if r.strip()]
    if raters:
        for rater in raters:
            write_rating_sheet(items, out_dir / f"rating_{rater}.csv", rater)
    else:
        write_rating_sheet(items, out_dir / "rating_sheet.csv")

    sheets = len(raters) if raters else 1
    print(
        f"Extracted {len(items)} unique clarification(s) to {set_path}\n"
        f"Wrote {sheets} blind rating sheet(s) to {out_dir}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
