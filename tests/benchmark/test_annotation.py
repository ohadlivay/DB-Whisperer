"""Tests for the Protocol 2 clarification-annotation tooling."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_DIR = ROOT / "benchmark" / "annotation"
sys.path.insert(0, str(ANNOTATION_DIR))

import analyze_annotations as az  # noqa: E402
import extract  # noqa: E402
from reliability import krippendorff_alpha  # noqa: E402


class KrippendorffAlphaTest(unittest.TestCase):
    """Anchors are hand-computed from the coincidence-matrix formula."""

    def test_perfect_agreement_is_one(self) -> None:
        self.assertEqual(krippendorff_alpha([[1, 1], [2, 2], [3, 3], [4, 4]]), 1.0)

    def test_hand_computed_interval_case(self) -> None:
        # o[(1,1)]=2,(1,2)=1,(2,1)=1,(2,2)=2 -> D_o=1/3, D_e=0.6 -> alpha=1-5/9.
        self.assertAlmostEqual(
            krippendorff_alpha([[1, 1], [1, 2], [2, 2]]), 0.4444, places=4
        )

    def test_single_maximally_spread_unit_is_zero(self) -> None:
        self.assertAlmostEqual(krippendorff_alpha([[1, 2, 3]]), 0.0, places=9)

    def test_no_variance_is_undefined_not_perfect(self) -> None:
        self.assertIsNone(krippendorff_alpha([[2, 2], [2, 2]]))

    def test_no_pairable_units_is_none(self) -> None:
        self.assertIsNone(krippendorff_alpha([[1], [2], [3]]))

    def test_missing_metric_raises(self) -> None:
        with self.assertRaises(ValueError):
            krippendorff_alpha([[1, 2]], metric="ordinal")


class ExtractTest(unittest.TestCase):
    def test_item_id_is_stable_and_order_sensitive(self) -> None:
        a = extract.item_id("Which?", ["A", "B"])
        self.assertEqual(a, extract.item_id(" Which? ", [" A ", " B "]))  # trimmed
        self.assertNotEqual(a, extract.item_id("Which?", ["B", "A"]))  # order matters

    def test_item_rejects_non_two_option(self) -> None:
        self.assertIsNone(extract._item("q", ["only one"], "D", "m", "s"))
        self.assertIsNone(extract._item("", ["A", "B"], "D", "m", "s"))

    def test_from_scenarios_skips_controls(self) -> None:
        scenarios = {
            "scenarios": [
                {
                    "ambiguous": True,
                    "dataset": "BikeStores",
                    "clarification_question": "Which products?",
                    "interpretations": [
                        {"option_label": "Stocked"},
                        {"option_label": "Ordered"},
                    ],
                },
                {"ambiguous": False, "dataset": "BikeStores", "interpretations": []},
            ]
        }
        items = extract.clarifications_from_scenarios(scenarios)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["mechanism"], "authored")

    def test_from_ab_report_reads_mechanism(self) -> None:
        report = {
            "suite": "MIMIC",
            "cases": [
                {
                    "full": {
                        "clarifications": [
                            {
                                "question": "Labs via which path?",
                                "options": ["Direct", "Via admission"],
                                "mechanism": "join-path",
                            }
                        ]
                    }
                }
            ],
        }
        items = extract.clarifications_from_ab_report(report)
        self.assertEqual(items[0]["dataset"], "MIMIC")
        self.assertEqual(items[0]["mechanism"], "join-path")

    def test_dedupe_counts_occurrences(self) -> None:
        one = extract._item("q", ["A", "B"], "D", "m", "s")
        items = extract.dedupe([one, dict(one), one])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["occurrences"], 3)


def _meta() -> dict:
    return {
        "i1": {"item_id": "i1", "dataset": "BikeStores", "mechanism": "authored"},
        "i2": {"item_id": "i2", "dataset": "MIMIC", "mechanism": "join-path"},
        "i3": {"item_id": "i3", "dataset": "MIMIC", "mechanism": "join-path"},
    }


class AggregateTest(unittest.TestCase):
    def test_means_alpha_and_dataset_split(self) -> None:
        rater_scores = {
            "r1": {
                "i1": {"clarity": 5, "naturalness": 5},
                "i2": {"clarity": 4, "naturalness": 2},
                "i3": {"clarity": 4, "naturalness": 2},
            },
            "r2": {
                "i1": {"clarity": 5, "naturalness": 5},
                "i2": {"clarity": 4, "naturalness": 2},
                "i3": {"clarity": 4, "naturalness": 3},
            },
        }
        summary = az.aggregate(_meta(), rater_scores)
        nat = summary["dimensions"]["naturalness"]
        # Naturalness is high on BikeStores, low on MIMIC — the density finding.
        self.assertEqual(nat["by_dataset"]["BikeStores"]["mean"], 5.0)
        self.assertEqual(nat["by_dataset"]["MIMIC"]["mean"], 2.25)
        # Clarity: every rater gave the same score per item -> perfect agreement.
        self.assertEqual(summary["dimensions"]["clarity"]["overall"]["alpha"], 1.0)

    def test_unrated_items_and_small_rater_caveat(self) -> None:
        summary = az.aggregate(_meta(), {"r1": {"i1": {"clarity": 4}}})
        self.assertIn("i2", summary["data_quality"]["items_unrated"])
        self.assertTrue(any("rater" in c for c in summary["caveats"]))

    def test_unknown_item_id_flagged(self) -> None:
        summary = az.aggregate(_meta(), {"r1": {"ghost": {"clarity": 4}}})
        self.assertEqual(summary["data_quality"]["unknown_item_ids"], ["ghost"])

    def test_few_items_caveat_even_with_enough_raters(self) -> None:
        # 3 items rated by 3 raters: alpha is defined, but 3 items is too few
        # to be stable — the caveat must fire so the number is not over-read.
        rater_scores = {
            r: {i: {"clarity": 4} for i in ("i1", "i2", "i3")}
            for r in ("r1", "r2", "r3")
        }
        summary = az.aggregate(_meta(), rater_scores)
        self.assertTrue(any("indicative" in c for c in summary["caveats"]))

    def test_empty_renders_without_crashing(self) -> None:
        html_text = az.render_html(az.aggregate(_meta(), {}), "2026-01-01 00:00 UTC")
        self.assertIn("n/a", html_text)
        self.assertIn("Protocol 2", html_text)


class LoadRaterCsvTest(unittest.TestCase):
    def test_parses_scores_and_skips_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rating_alice.csv"
            path.write_text(
                "item_id,question,option_a,option_b,clarity,discriminativeness,faithfulness,naturalness\n"
                "i1,q,A,B,5,4,,3\n"       # blank faithfulness -> skipped, not invalid
                "i2,q,A,B,9,x,2,2\n",     # 9 out of range, x non-number -> invalid
                encoding="utf-8",
            )
            rater, scores, invalid = az.load_rater_csv(path)
        self.assertEqual(rater, "alice")  # 'rating_' prefix stripped
        self.assertEqual(scores["i1"], {"clarity": 5.0, "discriminativeness": 4.0, "naturalness": 3.0})
        self.assertEqual(invalid, 2)

    def test_end_to_end_extract_then_aggregate(self) -> None:
        # extract writes a set + blank sheet; a filled sheet aggregates back.
        scenarios = {
            "scenarios": [
                {
                    "ambiguous": True,
                    "dataset": "BikeStores",
                    "clarification_question": "Which products?",
                    "interpretations": [
                        {"option_label": "Stocked"},
                        {"option_label": "Ordered"},
                    ],
                }
            ]
        }
        items = extract.dedupe(extract.clarifications_from_scenarios(scenarios))
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "rating_bob.csv"
            extract.write_rating_sheet(items, sheet)
            text = sheet.read_text(encoding="utf-8")
            item_id = items[0]["item_id"]
            # Fill the four blank score columns for the one item.
            header, row = text.strip().split("\n")
            row = ",".join(row.split(",")[:4] + ["5", "5", "5", "4"])
            sheet.write_text(header + "\n" + row + "\n", encoding="utf-8")
            _, scores, _ = az.load_rater_csv(sheet)
        meta = {i["item_id"]: i for i in items}
        summary = az.aggregate(meta, {"bob": scores})
        self.assertEqual(summary["items"]["rated"], 1)
        self.assertEqual(
            summary["dimensions"]["naturalness"]["by_dataset"]["BikeStores"]["mean"],
            4.0,
        )
        self.assertIn(item_id, meta)


if __name__ == "__main__":
    unittest.main()
