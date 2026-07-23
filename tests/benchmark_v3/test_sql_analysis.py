from __future__ import annotations

import unittest
from unittest.mock import patch

from benchmark_v3 import contracts
from benchmark_v3.sql_analysis import SQLAnalysis
from benchmark_v3.sql_analysis import analyze_sql


class SQLAnalysisTest(unittest.TestCase):
    def test_counts_joins_inside_ctes_and_subqueries(self) -> None:
        analysis = analyze_sql(
            "WITH x AS ("
            "SELECT a.id FROM a JOIN b ON a.id = b.id"
            ") "
            "SELECT x.id AS entity_id "
            "FROM x JOIN (SELECT id FROM c) AS nested "
            "ON x.id = nested.id "
            "ORDER BY entity_id LIMIT 5"
        )

        self.assertEqual(2, analysis.join_count)
        self.assertEqual(("a", "b", "c"), analysis.tables)
        self.assertEqual(("id",), analysis.columns)
        self.assertEqual(("entity_id",), analysis.aliases)
        self.assertTrue(analysis.has_order)
        self.assertEqual(5, analysis.limit)

    def test_excludes_cte_aliases_and_preserves_first_seen_identifier_order(
        self,
    ) -> None:
        analysis = analyze_sql(
            "WITH admissions_subset AS ("
            "SELECT subject_id, hadm_id FROM admissions"
            ") "
            "SELECT subject_id, hadm_id FROM admissions_subset"
        )

        self.assertEqual(("admissions",), analysis.tables)
        self.assertEqual(("subject_id", "hadm_id"), analysis.columns)

    def test_only_outer_order_and_limit_describe_final_result_contract(
        self,
    ) -> None:
        analysis = analyze_sql(
            "WITH ranked AS ("
            "SELECT subject_id FROM admissions ORDER BY subject_id LIMIT 2"
            ") SELECT subject_id FROM ranked"
        )

        self.assertFalse(analysis.has_order)
        self.assertIsNone(analysis.limit)

    def test_reference_join_evidence_uses_shared_sql_analysis(self) -> None:
        shared = SQLAnalysis((), (), (), 7, False, None)

        with patch.object(contracts, "analyze_sql", return_value=shared) as parser:
            count = contracts._parsed_join_count("SELECT 1")

        self.assertEqual(7, count)
        parser.assert_called_once_with("SELECT 1")


if __name__ == "__main__":
    unittest.main()
