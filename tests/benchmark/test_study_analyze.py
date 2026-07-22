"""Tests for the human-in-the-loop study results aggregator and report."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
STUDY_DIR = ROOT / "benchmark" / "study"
sys.path.insert(0, str(STUDY_DIR))

import analyze  # noqa: E402
from analyze import aggregate, load_records, render_html  # noqa: E402


def _task(
    *,
    version: str,
    ambiguous: bool,
    correct: bool,
    dataset: str = "D",
    trust: int | None = 4,
    clarity: int | None = None,
    naturalness: int | None = None,
    comprehension: bool | None = None,
    elapsed: float = 10.0,
    participant: str = "p1",
    task_id: str = "t",
) -> dict:
    """One task record shaped like study_app writes it."""
    asked = version == analyze.VERSION_ASKING and ambiguous
    return {
        "type": "task",
        "participant_id": participant,
        "task_id": task_id,
        "dataset": dataset,
        "version": version,
        "ambiguous": ambiguous,
        "asked": asked,
        "correct": correct,
        "comprehension": comprehension if asked else None,
        "trust": trust,
        "clarity": clarity if asked else None,
        "naturalness": naturalness if asked else None,
        "elapsed_seconds": elapsed,
    }


def _start(pid: str) -> dict:
    return {"type": "session_start", "participant_id": pid}


def _end(pid: str) -> dict:
    return {"type": "session_end", "participant_id": pid}


class AggregateAccuracyTest(unittest.TestCase):
    def test_ambiguous_accuracy_and_delta(self) -> None:
        records = (
            [_task(version="asking", ambiguous=True, correct=True, comprehension=True)] * 3
            + [_task(version="asking", ambiguous=True, correct=False, comprehension=False)]
            + [_task(version="direct", ambiguous=True, correct=True)]
            + [_task(version="direct", ambiguous=True, correct=False)] * 3
        )
        acc = aggregate(records)["accuracy"]["ambiguous"]
        self.assertEqual(acc["asking"], {"n": 4, "correct": 3, "rate": 0.75})
        self.assertEqual(acc["direct"], {"n": 4, "correct": 1, "rate": 0.25})
        self.assertAlmostEqual(acc["delta"], 0.5)

    def test_empty_groups_are_none_not_zero(self) -> None:
        acc = aggregate([])["accuracy"]["ambiguous"]
        self.assertIsNone(acc["asking"]["rate"])
        self.assertIsNone(acc["direct"]["rate"])
        self.assertIsNone(acc["delta"])  # unknown stays unknown, not 0.0


class AggregateComprehensionTest(unittest.TestCase):
    def test_comprehension_uses_its_own_field_not_correct(self) -> None:
        # A contrived record where comprehension and correct disagree proves the
        # aggregator reads the comprehension field, not correct.
        records = [
            _task(version="asking", ambiguous=True, correct=False, comprehension=True),
            _task(version="asking", ambiguous=True, correct=True, comprehension=False),
        ]
        comp = aggregate(records)["comprehension"]["overall"]
        self.assertEqual(comp, {"n": 2, "correct": 1, "rate": 0.5})

    def test_by_dataset_split(self) -> None:
        records = [
            _task(version="asking", ambiguous=True, correct=True, comprehension=True, dataset="BikeStores"),
            _task(version="asking", ambiguous=True, correct=False, comprehension=False, dataset="MIMIC"),
        ]
        by_ds = aggregate(records)["comprehension"]["by_dataset"]
        self.assertEqual(by_ds["BikeStores"]["rate"], 1.0)
        self.assertEqual(by_ds["MIMIC"]["rate"], 0.0)


class AggregateTrustAndRatingsTest(unittest.TestCase):
    def test_trust_delta_ambiguous(self) -> None:
        records = [
            _task(version="asking", ambiguous=True, correct=True, comprehension=True, trust=5),
            _task(version="asking", ambiguous=True, correct=True, comprehension=True, trust=3),
            _task(version="direct", ambiguous=True, correct=False, trust=2),
        ]
        trust = aggregate(records)["trust"]["ambiguous"]
        self.assertEqual(trust["asking"], {"n": 2, "mean": 4.0})
        self.assertEqual(trust["direct"], {"n": 1, "mean": 2.0})
        self.assertAlmostEqual(trust["delta"], 2.0)

    def test_unrated_trust_excluded_and_counted(self) -> None:
        records = [
            _task(version="direct", ambiguous=False, correct=True, trust=None),
            _task(version="direct", ambiguous=False, correct=True, trust=4),
        ]
        summary = aggregate(records)
        self.assertEqual(summary["tasks"]["unrated_trust"], 1)
        self.assertEqual(summary["trust"]["control"]["direct"], {"n": 1, "mean": 4.0})

    def test_clarity_and_naturalness_only_over_asked(self) -> None:
        records = [
            _task(version="asking", ambiguous=True, correct=True, comprehension=True, clarity=5, naturalness=4),
            _task(version="direct", ambiguous=True, correct=False, clarity=1, naturalness=1),
        ]
        summary = aggregate(records)
        # The direct task's clarity/naturalness are null (never asked), so only
        # the asked task contributes.
        self.assertEqual(summary["clarity"]["overall"], {"n": 1, "mean": 5.0})
        self.assertEqual(summary["naturalness"]["overall"], {"n": 1, "mean": 4.0})


class AggregateParticipantsTest(unittest.TestCase):
    def test_completed_vs_incomplete(self) -> None:
        records = [
            _start("done"), _task(participant="done", version="direct", ambiguous=False, correct=True), _end("done"),
            _start("quit"), _task(participant="quit", version="direct", ambiguous=False, correct=True),
        ]
        parts = aggregate(records)["participants"]
        self.assertEqual(parts["total"], 2)
        self.assertEqual(parts["completed"], 1)
        self.assertEqual(parts["incomplete"], ["quit"])

    def test_duplicate_participant_flagged(self) -> None:
        records = [_start("p1"), _start("p1"), _end("p1")]
        self.assertEqual(
            aggregate(records)["data_quality"]["duplicate_participant_ids"], ["p1"]
        )

    def test_small_n_caveat_present(self) -> None:
        summary = aggregate([_start("p1"), _end("p1")])
        self.assertTrue(any("participant" in c for c in summary["caveats"]))


class DataQualityTest(unittest.TestCase):
    def test_malformed_passthrough_and_unknown_records(self) -> None:
        records = [{"type": "mystery"}, _task(version="direct", ambiguous=False, correct=True)]
        summary = aggregate(records, malformed_lines=3)
        self.assertEqual(summary["data_quality"]["malformed_lines"], 3)
        self.assertEqual(summary["data_quality"]["unknown_records"], 1)

    def test_preference_marked_not_collected(self) -> None:
        summary = aggregate([_task(version="direct", ambiguous=False, correct=True)])
        self.assertTrue(
            any("preference" in note.lower() for note in summary["not_collected"])
        )


class LoadRecordsTest(unittest.TestCase):
    def test_parses_and_counts_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.jsonl"
            path.write_text(
                json.dumps(_start("p1")) + "\n"
                + "not json\n"
                + "\n"  # blank line ignored, not counted
                + json.dumps(_task(version="direct", ambiguous=False, correct=True)) + "\n",
                encoding="utf-8",
            )
            records, malformed = load_records([path])
        self.assertEqual(malformed, 1)
        self.assertEqual(len(records), 2)


class RenderHtmlTest(unittest.TestCase):
    def test_renders_key_numbers_and_caveats(self) -> None:
        records = [
            _task(version="asking", ambiguous=True, correct=True, comprehension=True, trust=5, clarity=5, naturalness=5),
            _task(version="direct", ambiguous=True, correct=False, trust=2),
        ]
        html_text = render_html(aggregate(records), "2026-01-01 00:00 UTC")
        self.assertIn("<!doctype html>", html_text)
        self.assertIn("Human-in-the-loop study report", html_text)
        self.assertIn("comprehension", html_text.lower())
        # The "not collected" preference note must appear so absence is visible.
        self.assertIn("preference", html_text.lower())

    def test_empty_summary_renders_without_crashing(self) -> None:
        html_text = render_html(aggregate([]), "2026-01-01 00:00 UTC")
        self.assertIn("n/a", html_text)


if __name__ == "__main__":
    unittest.main()
