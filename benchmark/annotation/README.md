# Clarification-quality annotation (Protocol 2)

A runnable implementation of **Protocol 2** from
[`../HUMAN_IN_THE_LOOP.md`](../HUMAN_IN_THE_LOOP.md): the cheapest way to measure
clarification quality, needing **no live participants**. A few experts score the
clarifying questions the system already produced, and this tooling turns those
scores into means, inter-rater reliability, and a report — including the
naturalness gap that motivates semantic path pruning.

It is the natural first step before the live Protocol 1 study: it validates that
the questions read clearly *before* you spend participants on them.

## The three steps

```powershell
# 1. Extract clarifications into a blind rating sheet.
#    From real model output (preferred) — needs ab_*.json from an A/B run:
python benchmark/annotation/extract.py --from-ab-reports --raters alice,bob,carol
#    ...or from the authored Protocol 1 stimuli when no A/B run exists yet:
python benchmark/annotation/extract.py --from-scenarios --raters alice,bob,carol

# 2. Each rater fills the score columns of their benchmark/annotation/results/
#    rating_<name>.csv (open in Excel/Sheets, type 1-5, save).

# 3. Aggregate every filled sheet into metrics + an HTML report.
python benchmark/annotation/analyze_annotations.py
```

Outputs land in `benchmark/annotation/results/` (git-ignored): the
`annotation_set.json`, the blind `rating_*.csv` sheets, and the generated
`annotation_summary.json` + `annotation_report.html`.

## What the raters score (1–5 each)

Every clarification is one question with two options. Rate:

- **Clarity** — is it plain, non-technical language a non-expert follows?
- **Discriminativeness** — are the two options genuinely different and mutually
  exclusive, not two phrasings of the same thing?
- **Faithfulness** — do the options actually match the interpretations the
  system found (the real join paths or columns), not invented ones?
- **Naturalness** — is this an ambiguity a real user would plausibly have, or an
  artifact — e.g. a MIMIC "longest path" routed through an unrelated fact table?

The sheet is **blind**: it shows the question and its two options but *not* the
dataset or the mechanism that produced it, so a rater is not primed by "this is
the dense MIMIC one". The analyzer re-attaches that metadata afterwards (by the
stable item id) to split the results by dataset and mechanism.

## Reading the report

The headline is the **naturalness × dataset** row: high for BikeStores and the
clean lab-dictionary cases, low for the dense MIMIC pairs — the density finding.

`alpha` is **Krippendorff's alpha** (interval): 1.0 is perfect agreement, 0 is
chance, and it can go negative. It is defined only over items two or more raters
scored, and is unstable below ~3 raters or ~15 items — the report says so
explicitly rather than presenting a shaky number as settled. Empty cells are
`n/a`, never `0`. The reliability maths is pure and unit-tested in
`tests/benchmark/test_annotation.py`, with hand-computed anchors so it is
checkable without a stats library.

## Honest limitations

- **It rates authored options unless you feed it real runs.** `--from-scenarios`
  rates the frozen Protocol 1 stimuli; to judge what the *model* emits, run
  `benchmark/ab_run.py` first and use `--from-ab-reports`.
- **Small by nature.** A demo set is a handful of clarifications; α over that few
  items is indicative only. Accumulate clarifications across several A/B runs
  before drawing conclusions.
- **Blindness is partial.** The sheet hides dataset/mechanism, but a
  domain-expert rater may still infer "this is clinical" from the wording; that
  is inherent to rating real clinical clarifications.
