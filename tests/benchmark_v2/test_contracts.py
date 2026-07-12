from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmark_v2.contracts import load_suite
from benchmark_v2.sql_analysis import analyze_sql


SUITE = Path(__file__).resolve().parents[2] / "benchmark_v2" / "cases" / "evaluation_cases.json"


class ContractsTest(unittest.TestCase):
    def test_canonical_suite_has_expected_shape(self) -> None:
        suite = load_suite(SUITE)
        self.assertEqual(18, len(suite.cases))
        self.assertEqual(16, len(suite.query_cases))
        self.assertEqual(2, len(suite.etl_cases))
        self.assertEqual(5, suite.repetitions)
        self.assertEqual(2, suite.candidate_count)
        for case in suite.query_cases:
            if case.expected_sql is None:
                continue
            analysis = analyze_sql(case.expected_sql)
            self.assertEqual(case.minimum_joins, analysis.join_count, case.id)
            self.assertEqual(set(case.required_tables), set(analysis.tables), case.id)

    def test_duplicate_case_id_is_rejected(self) -> None:
        payload = json.loads(SUITE.read_text(encoding="utf-8"))
        payload["cases"][1]["id"] = payload["cases"][0]["id"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "suite.json"
            # Fixture paths must remain resolvable, so fail on IDs before files.
            payload["cases"] = [case for case in payload["cases"] if case["kind"] != "etl"] + payload["cases"][-2:]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "case IDs"):
                load_suite(path)
