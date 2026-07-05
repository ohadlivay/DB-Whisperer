"""Tests for the A/B (full pipeline vs baseline) evaluation harness."""

from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

import ab_run  # noqa: E402
from _harness import DEFAULT_MAX_REFERENCE_ROWS, execute_reference  # noqa: E402
from db_whisperer.contracts import (  # noqa: E402
    AmbiguityDecision,
    ComponentState,
    CsvUpload,
    QueryResult,
    QueryWorkflowResult,
    SchemaMetadata,
)
from db_whisperer.etler import ETLService  # noqa: E402


BENCHMARK_DIR = ROOT / "benchmark"
MIMIC_CASES_PATH = BENCHMARK_DIR / "mimic_ab_cases.json"
MIMIC_DIR = (
    ROOT
    / "data"
    / "mimic-iii-clinical-database-demo-1.4-20260615T211207Z-3-001"
    / "mimic-iii-clinical-database-demo-1.4"
)


def _accepted(rows, columns=("n",), sql="SELECT 1;") -> QueryResult:
    return QueryResult(
        state=ComponentState.ACCEPTED,
        message="ok",
        sql=sql,
        columns=columns,
        rows=tuple(rows),
    )


def _pending(
    question="Which connection?",
    options=("direct", "through visit"),
    mechanism="join-path",
):
    return QueryWorkflowResult(
        state=ComponentState.PENDING,
        message=question,
        iteration=1,
        complete=False,
        query_result=None,
        ambiguity=AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question=question,
            options=options,
            mechanism=mechanism,
        ),
    )


def _complete(rows) -> QueryWorkflowResult:
    return QueryWorkflowResult(
        state=ComponentState.ACCEPTED,
        message="done",
        iteration=2,
        complete=True,
        query_result=_accepted(rows),
    )


class _ScriptedApp:
    """A fake ApplicationService returning a scripted workflow per call."""

    max_iterations = 3
    candidates_per_iteration = 3

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def submit_query(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


def _case(**overrides) -> ab_run.AbCase:
    base = {
        "id": "c1",
        "question": "How many products for store X?",
        "expected_sql": "SELECT 1;",
        "ambiguous": True,
        "clarification_path_index": 1,
        "intent": "",
        "entity_pair": (),
    }
    base.update(overrides)
    return ab_run.AbCase(**base)


SCHEMA = SchemaMetadata(database_path="unused.duckdb", table_names=("t",))


class RunFullTest(unittest.TestCase):
    """The full arm drives the GUI clarification loop automatically."""

    def test_picks_declared_interpretation_and_resubmits(self) -> None:
        app = _ScriptedApp([_pending(), _complete([(226,)])])
        outcome = ab_run.run_full(_case(), SCHEMA, app, "key", "model")

        self.assertEqual(outcome.termination, "complete")
        self.assertTrue(outcome.asked_question)
        self.assertEqual(outcome.result.rows, ((226,),))
        # The case wants path index 1, so option[1] is chosen and fed back.
        asked = outcome.clarifications_asked[0]
        self.assertEqual(asked["chosen_index"], 1)
        self.assertEqual(asked["chosen"], "through visit")
        self.assertEqual(asked["mechanism"], "join-path")
        # A declared first join-path clarification is reliably answered.
        self.assertTrue(asked["declared"])
        self.assertFalse(outcome.unreliable)
        # The second submit carries exactly the GUI-formatted clarification.
        self.assertEqual(
            app.calls[1]["clarifications"],
            ("Question: Which connection?\nSelected answer: through visit",),
        )
        self.assertEqual(app.calls[1]["iteration"], 2)

    def test_completes_without_a_question(self) -> None:
        app = _ScriptedApp([_complete([(1,)])])
        outcome = ab_run.run_full(_case(), SCHEMA, app, "key", "model")

        self.assertEqual(outcome.termination, "complete")
        self.assertFalse(outcome.asked_question)
        self.assertEqual(outcome.clarifications_asked, ())
        self.assertEqual(len(app.calls), 1)

    def test_control_case_asked_is_flagged_unreliable(self) -> None:
        control = _case(ambiguous=False, clarification_path_index=None)
        app = _ScriptedApp([_pending(), _complete([(1,)])])
        outcome = ab_run.run_full(control, SCHEMA, app, "key", "model")

        # A control case should not be asked; if it is, the run is flagged
        # unreliable (not silently treated as a clean answer) and defaults to 0.
        self.assertTrue(outcome.asked_question)
        self.assertTrue(outcome.unreliable)
        self.assertFalse(outcome.clarifications_asked[0]["declared"])
        self.assertEqual(outcome.clarifications_asked[0]["chosen_index"], 0)
        self.assertIn("not marked ambiguous", outcome.unreliable_reasons[0])

    def test_non_join_path_first_clarification_is_unreliable(self) -> None:
        # The declared index is path-ordered, so it does not apply to a
        # semantic-column (or candidate-comparison) question even on round one.
        app = _ScriptedApp(
            [_pending(mechanism="semantic-column"), _complete([(1,)])]
        )
        outcome = ab_run.run_full(_case(), SCHEMA, app, "key", "model")

        self.assertTrue(outcome.unreliable)
        self.assertFalse(outcome.clarifications_asked[0]["declared"])
        self.assertIn("not path-ordered", outcome.unreliable_reasons[0])

    def test_wrong_entity_pair_first_clarification_is_unreliable(self) -> None:
        # A dense schema can make entity extraction clarify a different pair than
        # the case targets; the path-ordered index would then mean the wrong
        # thing, so the run must be flagged rather than scored as the user's
        # intent.
        case = _case(entity_pair=("patients", "d_labitems"))
        app = _ScriptedApp(
            [_pending(question='Connect "patients" and "labevents" how?'),
             _complete([(1,)])]
        )
        outcome = ab_run.run_full(case, SCHEMA, app, "key", "model")

        self.assertTrue(outcome.unreliable)
        self.assertFalse(outcome.clarifications_asked[0]["declared"])
        self.assertIn("different table pair", outcome.unreliable_reasons[0])

    def test_declared_entity_pair_first_clarification_is_reliable(self) -> None:
        case = _case(entity_pair=("patients", "d_labitems"))
        app = _ScriptedApp(
            [_pending(question='For "patients" and "d_labitems", which labs?'),
             _complete([(1,)])]
        )
        outcome = ab_run.run_full(case, SCHEMA, app, "key", "model")

        self.assertFalse(outcome.unreliable)
        self.assertTrue(outcome.clarifications_asked[0]["declared"])

    def test_failed_workflow_terminates(self) -> None:
        failed = QueryWorkflowResult(
            state=ComponentState.FAILED,
            message="boom",
            iteration=1,
            query_result=QueryResult(state=ComponentState.FAILED, message="boom"),
        )
        app = _ScriptedApp([failed])
        outcome = ab_run.run_full(_case(), SCHEMA, app, "key", "model")

        self.assertEqual(outcome.termination, "failed")
        self.assertEqual(outcome.result.state, ComponentState.FAILED)

    def test_unbounded_questions_stop_at_iteration_cap(self) -> None:
        # An app that always asks must not loop forever; the iteration cap is a
        # backstop even though the real service returns a result on the last
        # round.
        app = _ScriptedApp([_pending()])
        outcome = ab_run.run_full(_case(), SCHEMA, app, "key", "model")

        self.assertEqual(outcome.termination, "max_iterations")
        self.assertEqual(len(outcome.clarifications_asked), app.max_iterations)
        # The first question is declared; the extra rounds are not, so the run
        # is flagged unreliable.
        self.assertTrue(outcome.unreliable)


class PickOptionIndexTest(unittest.TestCase):
    def _decision(self, options=("a", "b")) -> AmbiguityDecision:
        return AmbiguityDecision(
            state=ComponentState.ACCEPTED,
            passed=False,
            question="q",
            options=options,
            mechanism="join-path",
        )

    def test_uses_declared_index(self) -> None:
        self.assertEqual(
            ab_run.pick_option_index(_case(clarification_path_index=1), self._decision()),
            1,
        )

    def test_defaults_to_zero_without_index(self) -> None:
        control = _case(ambiguous=False, clarification_path_index=None)
        self.assertEqual(ab_run.pick_option_index(control, self._decision()), 0)

    def test_defaults_to_zero_when_options_malformed(self) -> None:
        self.assertEqual(
            ab_run.pick_option_index(_case(), self._decision(options=("only",))),
            0,
        )


class ScoreResultTest(unittest.TestCase):
    @staticmethod
    def _judge(value):
        def judge(_question, _expected, _actual):
            return value

        return judge

    def test_exact_match_scores_four_without_judge(self) -> None:
        def exploding_judge(*_args):
            raise AssertionError("judge must not be called on exact match")

        scored = ab_run.score_result(
            "q", ("n",), ((5,),), _accepted([(5,)], columns=("n",)), exploding_judge
        )
        self.assertEqual(scored["score"], 4)
        self.assertEqual(scored["comparison"], "exact")
        self.assertTrue(scored["exact_match"])

    def test_mismatch_uses_judge(self) -> None:
        scored = ab_run.score_result(
            "q", ("n",), ((5,),), _accepted([(9,)], columns=("n",)), self._judge((2, "off"))
        )
        self.assertEqual(scored["score"], 2)
        self.assertEqual(scored["comparison"], "judge")
        self.assertEqual(scored["reason"], "off")

    def test_failed_result_scores_zero(self) -> None:
        failed = QueryResult(state=ComponentState.FAILED, message="bad sql")
        scored = ab_run.score_result("q", ("n",), ((5,),), failed, self._judge((4, "x")))
        self.assertEqual(scored["score"], 0)
        self.assertEqual(scored["comparison"], "system_failure")

    def test_judge_error_is_unscored_not_zero(self) -> None:
        def broken_judge(*_args):
            raise ValueError("judge down")

        scored = ab_run.score_result(
            "q", ("n",), ((5,),), _accepted([(9,)], columns=("n",)), broken_judge
        )
        self.assertIsNone(scored["score"])
        self.assertEqual(scored["comparison"], "judge_failure")


class CompareScoresTest(unittest.TestCase):
    def test_classifications(self) -> None:
        self.assertEqual(ab_run.compare_scores(0, 4), "full_better")
        self.assertEqual(ab_run.compare_scores(4, 4), "tie")
        self.assertEqual(ab_run.compare_scores(4, 2), "baseline_better")
        self.assertEqual(ab_run.compare_scores(None, 4), "unscored")
        self.assertEqual(ab_run.compare_scores(4, None), "unscored")


class SummarizeTest(unittest.TestCase):
    @staticmethod
    def _result(
        ambiguous,
        comparison,
        baseline_score,
        full_score,
        asked,
        unreliable=False,
        case_id="c",
    ):
        return {
            "id": case_id,
            "ambiguous": ambiguous,
            "comparison": comparison,
            "baseline": {"score": baseline_score},
            "full": {
                "score": full_score,
                "clarification_asked": asked,
                "unreliable": unreliable,
            },
        }

    def test_splits_ambiguous_and_control(self) -> None:
        results = [
            self._result(True, "full_better", 0, 4, True),
            self._result(True, "tie", 4, 4, True),
            self._result(False, "tie", 4, 4, False),
            self._result(False, "baseline_better", 4, 0, True),
        ]
        summary = ab_run.summarize(results)

        self.assertEqual(summary["total_cases"], 4)
        self.assertEqual(summary["ambiguous"]["count"], 2)
        self.assertEqual(summary["ambiguous"]["comparison"]["full_better"], 1)
        self.assertEqual(summary["ambiguous"]["clarification_rate"], 1.0)
        self.assertEqual(summary["control"]["count"], 2)
        # One of the two control cases was (wrongly) asked a question.
        self.assertEqual(summary["control"]["spurious_clarification_rate"], 0.5)
        self.assertEqual(summary["full"]["mean_score"], 3.0)

    def test_unscored_arm_excluded_from_mean(self) -> None:
        results = [
            self._result(True, "unscored", None, 4, True),
            self._result(True, "tie", 4, 4, True),
        ]
        summary = ab_run.summarize(results)
        # Baseline mean ignores the None score and averages only the 4.
        self.assertEqual(summary["baseline"]["mean_score"], 4.0)
        self.assertEqual(summary["baseline"]["scored"], 1)

    def test_lists_unreliable_cases(self) -> None:
        results = [
            self._result(True, "tie", 4, 4, True, case_id="ok"),
            self._result(
                False, "baseline_better", 4, 0, True,
                unreliable=True, case_id="bad",
            ),
        ]
        summary = ab_run.summarize(results)
        self.assertEqual(summary["unreliable_cases"], ["bad"])


class LoadAbSuiteTest(unittest.TestCase):
    """Suite loading validates the simulated-user contract per case."""

    def _write_suite(self, tmp: Path, suite: dict) -> Path:
        (tmp / "data.csv").write_text("id\n1\n", encoding="utf-8")
        suite_path = tmp / "suite.json"
        suite_path.write_text(json.dumps(suite), encoding="utf-8")
        return suite_path

    def _valid_suite(self) -> dict:
        return {
            "name": "demo",
            "dataset": "data.csv",
            "cases": [
                {
                    "id": "amb",
                    "question": "q1",
                    "expected_sql": "SELECT 1;",
                    "ambiguous": True,
                    "clarification_path_index": 0,
                },
                {
                    "id": "ctl",
                    "question": "q2",
                    "expected_sql": "SELECT 2;",
                    "ambiguous": False,
                },
            ],
        }

    def test_loads_valid_suite(self) -> None:
        with TemporaryDirectory() as name:
            tmp = Path(name)
            suite = ab_run.load_ab_suite(self._write_suite(tmp, self._valid_suite()))
            self.assertEqual(suite.name, "demo")
            self.assertEqual(suite.candidate_count, ab_run.DEFAULT_CANDIDATE_COUNT)
            self.assertEqual(len(suite.cases), 2)
            self.assertEqual(suite.cases[0].clarification_path_index, 0)
            self.assertIsNone(suite.cases[1].clarification_path_index)

    def test_ambiguous_case_requires_path_index(self) -> None:
        suite = self._valid_suite()
        del suite["cases"][0]["clarification_path_index"]
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))

    def test_path_index_must_be_zero_or_one(self) -> None:
        suite = self._valid_suite()
        suite["cases"][0]["clarification_path_index"] = 2
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))

    def test_control_case_must_not_set_path_index(self) -> None:
        suite = self._valid_suite()
        suite["cases"][1]["clarification_path_index"] = 0
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))

    def test_duplicate_case_ids_rejected(self) -> None:
        suite = self._valid_suite()
        suite["cases"][1]["id"] = "amb"
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))

    def test_missing_dataset_rejected(self) -> None:
        suite = self._valid_suite()
        suite["dataset"] = "does_not_exist.csv"
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))

    def test_candidate_count_must_be_at_least_two(self) -> None:
        suite = self._valid_suite()
        suite["candidate_count"] = 1
        with TemporaryDirectory() as name:
            tmp = Path(name)
            with self.assertRaises(ValueError):
                ab_run.load_ab_suite(self._write_suite(tmp, suite))


@unittest.skipUnless(MIMIC_DIR.is_dir(), "MIMIC demo data not present.")
class MimicAbSuiteTest(unittest.TestCase):
    """The bundled MIMIC A/B suite is well-formed and its gold is material."""

    def test_suite_loads_with_expected_shape(self) -> None:
        suite = ab_run.load_ab_suite(MIMIC_CASES_PATH)
        self.assertEqual(suite.dataset_path, MIMIC_DIR.resolve())

        ambiguous = [c for c in suite.cases if c.ambiguous]
        control = [c for c in suite.cases if not c.ambiguous]
        self.assertEqual(len(ambiguous), 4)
        self.assertEqual(len(control), 3)

        # Every ambiguous case is anchored on the d_labitems dictionary pair --
        # the only MIMIC pairs with exactly two join paths within the hop cap,
        # so the mechanism's shortest/longest options are both natural.
        for case in ambiguous:
            self.assertIn("d_labitems", case.entity_pair)
            self.assertIn(case.clarification_path_index, (0, 1))

        # Each ambiguous question is a 0/1 sibling pair: identical wording,
        # opposite declared interpretation, so a single blind baseline guess can
        # satisfy at most one of the two.
        siblings: dict[str, list[int]] = defaultdict(list)
        for case in ambiguous:
            siblings[case.question].append(case.clarification_path_index)
        self.assertEqual(len(siblings), 2)
        for indices in siblings.values():
            self.assertEqual(sorted(indices), [0, 1])

    @unittest.skipUnless(
        os.environ.get("DB_WHISPERER_RUN_MIMIC_TEST"),
        "Set DB_WHISPERER_RUN_MIMIC_TEST=1 to run the slow MIMIC gold check.",
    )
    def test_gold_sql_executes_and_siblings_differ(self) -> None:
        # Exercises the same reference executor the harness uses, against the
        # real ETL-loaded MIMIC database, so a broken gold query or an
        # accidentally-equal sibling pair fails loudly instead of at run time.
        suite = ab_run.load_ab_suite(MIMIC_CASES_PATH)
        with TemporaryDirectory(dir=ROOT) as directory:
            database_path = Path(directory) / "mimic_ab.duckdb"
            uploads = [
                CsvUpload(name=path.name, content=path.read_bytes())
                for path in sorted(MIMIC_DIR.glob("*.csv"))
            ]
            ingestion = ETLService(database_path=database_path).ingest(uploads)
            self.assertEqual(
                ComponentState.ACCEPTED, ingestion.state, ingestion.message
            )
            answers: dict[str, tuple] = {}
            for case in suite.cases:
                _columns, rows = execute_reference(
                    str(database_path), case.expected_sql
                )
                self.assertLessEqual(len(rows), DEFAULT_MAX_REFERENCE_ROWS)
                answers[case.id] = rows

        # Sibling interpretations must return materially different tables, or the
        # ambiguous case proves nothing about clarification.
        self.assertNotEqual(
            answers["patient_lab_types_anywhere"],
            answers["patient_lab_types_during_admissions"],
        )
        self.assertNotEqual(
            answers["icustay_lab_types_same_admission"],
            answers["icustay_lab_types_same_patient"],
        )


if __name__ == "__main__":
    unittest.main()
