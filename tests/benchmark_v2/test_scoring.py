from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db_whisperer.contracts import ColumnMetadata, ComponentState, QueryResult, SchemaMetadata, TableSchema

from benchmark_v2.contracts import EvaluationCase
from benchmark_v2.scoring import option_index, score_query_case
from benchmark_v2.sql_analysis import analyze_sql


class SQLAnalysisTest(unittest.TestCase):
    def test_counts_cte_and_outer_joins(self) -> None:
        analysis = analyze_sql("WITH x AS (SELECT * FROM admissions a JOIN patients p ON a.subject_id=p.subject_id) SELECT * FROM x JOIN labevents l ON x.hadm_id=l.hadm_id")
        self.assertEqual(2, analysis.join_count)
        self.assertEqual({"admissions", "patients", "labevents"}, set(analysis.tables))

    def test_deterministic_option_match_requires_unique_winner(self) -> None:
        self.assertEqual(1, option_index(("Direct subject link", "Through admissions by HADM_ID"), ("admissions", "hadm_id")))
        self.assertIsNone(option_index(("Patient", "Patient"), ("patient",)))


class ScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        columns = (ColumnMetadata("hadm_id", "BIGINT", "admissions"),)
        self.schema = SchemaMetadata(
            table_names=("admissions",),
            columns=columns,
            tables=(TableSchema("admissions", columns, 1),),
        )

    def test_correct_extra_projection_can_match_reference(self) -> None:
        case = EvaluationCase(
            id="x", family_id="x", kind="query", category="correctness", question="q",
            required_tables=("admissions",), required_column_groups=(("hadm_id",),), minimum_joins=0,
            expected_sql="SELECT hadm_id FROM admissions",
        )
        expected = QueryResult(ComponentState.ACCEPTED, "ok", "SELECT hadm_id FROM admissions", ("hadm_id",), ((1,),))
        actual = QueryResult(ComponentState.ACCEPTED, "ok", "SELECT hadm_id, 1 AS extra FROM admissions", ("hadm_id", "extra"), ((1, 1),))
        score = score_query_case(case, actual, expected, self.schema, [])
        self.assertTrue(score["passed"])
        self.assertEqual(1.0, score["efficiency"])

    def test_incorrect_sql_gates_efficiency(self) -> None:
        case = EvaluationCase(id="x", family_id="x", kind="query", category="correctness", question="q", required_tables=("admissions",), expected_sql="SELECT 1")
        failed = QueryResult(ComponentState.FAILED, "bad")
        score = score_query_case(case, failed, None, self.schema, [])
        self.assertEqual(0.0, score["efficiency"])

    def test_ambiguity_case_accepts_reference_rows_as_subset(self) -> None:
        case = EvaluationCase(
            id="x", family_id="x", kind="query", category="join_path", question="q",
            required_tables=("admissions",), required_column_groups=(("hadm_id",),), expected_sql="SELECT hadm_id FROM admissions",
        )
        expected = QueryResult(ComponentState.ACCEPTED, "ok", "SELECT hadm_id FROM admissions LIMIT 1", ("hadm_id",), ((1,),))
        actual = QueryResult(ComponentState.ACCEPTED, "ok", "SELECT hadm_id FROM admissions", ("hadm_id",), ((1,), (2,)))
        score = score_query_case(case, actual, expected, self.schema, [])
        self.assertTrue(score["passed"])

    def test_equivalent_derived_alias_uses_source_column_as_semantic_evidence(self) -> None:
        los = ColumnMetadata("los", "DOUBLE", "icustays")
        schema = SchemaMetadata(
            table_names=("icustays",), columns=(los,),
            tables=(TableSchema("icustays", (los,), 1),),
        )
        case = EvaluationCase(
            id="x", family_id="x", kind="query", category="control", question="q",
            required_tables=("icustays",), required_column_groups=(("los", "icu_los_days"),),
            expected_sql="SELECT icustay_id, los AS icu_los_days FROM icustays",
        )
        expected = QueryResult(
            ComponentState.ACCEPTED, "ok", "SELECT icustay_id, los AS icu_los_days FROM icustays",
            ("icustay_id", "icu_los_days"), ((7, 2.5),),
        )
        actual = QueryResult(
            ComponentState.ACCEPTED, "ok", "SELECT icustay_id, los AS stay_length_days FROM icustays",
            ("icustay_id", "stay_length_days"), ((7, 2.5),),
        )
        score = score_query_case(case, actual, expected, schema, [])
        self.assertTrue(score["passed"])
