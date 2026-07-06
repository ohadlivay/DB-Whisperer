"""Validation tests for the MIMIC-III A/B evaluation case file."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = ROOT / "benchmark" / "mimic_ab_cases.json"


class MimicCaseFileTest(unittest.TestCase):
    """The MIMIC benchmark cases define the contract for the next harness."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(CASE_PATH.read_text(encoding="utf-8"))
        cls.cases = cls.payload["cases"]

    def test_suite_metadata_points_to_bundled_mimic_dataset(self) -> None:
        self.assertEqual(self.payload["name"], "mimic_iii_clinical_ambiguity")
        dataset_path = (CASE_PATH.parent / self.payload["dataset"]).resolve()
        self.assertTrue(dataset_path.exists(), dataset_path)
        self.assertTrue((dataset_path / "PATIENTS.csv").exists())
        self.assertTrue((dataset_path / "ADMISSIONS.csv").exists())
        self.assertTrue((dataset_path / "LABEVENTS.csv").exists())

    def test_declares_initial_self_judge_as_secondary(self) -> None:
        judge = self.payload["judge"]
        self.assertTrue(judge["enabled"])
        self.assertTrue(judge["self_judged"])
        self.assertIn("deterministic", judge["notes"].lower())

    def test_all_cases_have_required_shape(self) -> None:
        required = {
            "id",
            "category",
            "question",
            "ambiguous",
            "ambiguity_type",
            "intent",
            "schema_elements",
            "expected_sql",
            "should_clarify",
            "simulated_user_answer",
            "expected_behavior",
            "tests",
        }

        for case in self.cases:
            with self.subTest(case=case.get("id")):
                self.assertEqual(required, set(case))
                self.assertIsInstance(case["id"], str)
                self.assertTrue(case["id"].startswith("tc_"))
                self.assertIsInstance(case["category"], str)
                self.assertIsInstance(case["question"], str)
                self.assertIsInstance(case["ambiguous"], bool)
                self.assertIsInstance(case["intent"], str)
                self.assertIsInstance(case["schema_elements"], list)
                self.assertIsInstance(case["should_clarify"], bool)
                self.assertIsInstance(case["expected_behavior"], list)
                self.assertIsInstance(case["tests"], list)
                self.assertTrue(case["question"].strip())
                self.assertTrue(case["intent"].strip())
                self.assertTrue(case["expected_behavior"])
                self.assertTrue(case["tests"])

    def test_case_ids_are_unique_and_complete(self) -> None:
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), 16)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "tc_01_count_admissions")
        self.assertEqual(ids[-1], "tc_16_underspecified_aggregate")

    def test_ambiguity_contract_is_explicit(self) -> None:
        allowed_types = {"none", "join-path", "semantic-column", "underspecified"}
        ambiguous_cases = []

        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["ambiguity_type"], allowed_types)
                if case["should_clarify"]:
                    ambiguous_cases.append(case)
                    self.assertTrue(case["ambiguous"])
                    self.assertNotEqual(case["ambiguity_type"], "none")
                    self.assertIsInstance(case["simulated_user_answer"], str)
                    self.assertTrue(case["simulated_user_answer"].strip())
                else:
                    self.assertIsNone(case["simulated_user_answer"])

        self.assertEqual(len(ambiguous_cases), 8)
        self.assertEqual(
            {case["ambiguity_type"] for case in ambiguous_cases},
            {"join-path", "semantic-column", "underspecified"},
        )

    def test_gold_sql_cases_are_select_only_or_declared_no_sql(self) -> None:
        no_sql_cases = []
        for case in self.cases:
            sql = case["expected_sql"]
            with self.subTest(case=case["id"]):
                if sql is None:
                    no_sql_cases.append(case["id"])
                    self.assertIn(case["category"], {"safety_and_graceful_failure"})
                    continue
                self.assertIsInstance(sql, str)
                self.assertTrue(sql.strip().lower().startswith("select "))
                lowered = sql.lower()
                for forbidden in (
                    " delete ",
                    " update ",
                    " insert ",
                    " drop ",
                    " create ",
                    " copy ",
                    " read_csv",
                    " httpfs",
                ):
                    self.assertNotIn(forbidden, f" {lowered} ")

        self.assertEqual(
            no_sql_cases,
            [
                "tc_14_write_operation_request",
                "tc_15_nonexistent_clinical_concept",
                "tc_16_underspecified_aggregate",
            ],
        )

    def test_duplicate_ambiguous_question_pair_is_intentional(self) -> None:
        duplicated = [
            case for case in self.cases
            if case["question"] == "Show me lab results for patient 10006."
        ]
        self.assertEqual(
            [case["id"] for case in duplicated],
            [
                "tc_04_patient_labs_subject_history",
                "tc_05_patient_labs_admission_context",
            ],
        )
        self.assertNotEqual(duplicated[0]["expected_sql"], duplicated[1]["expected_sql"])
        self.assertNotEqual(
            duplicated[0]["simulated_user_answer"],
            duplicated[1]["simulated_user_answer"],
        )


if __name__ == "__main__":
    unittest.main()

