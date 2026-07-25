from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from benchmark_v3.contracts import (
    EvaluationCase,
    ReferenceContract,
    load_suite,
    validate_reference_suite,
    validate_suite_shape,
)
from benchmark_v3.run_evaluation import DEFAULT_SUITE
from db_whisperer.contracts import (
    ComponentState,
    QueryCandidate,
    QueryResult,
    SchemaMetadata,
)


EXPECTED_CASE_IDS = {
    "from_2024_birth",
    "from_2024_admission",
    "ctl_from_2024_birth",
    "ctl_from_2024_admission",
    "stay_hospital",
    "stay_icu",
    "ctl_stay_hospital",
    "ctl_stay_icu",
    "diagnoses_occurrences",
    "diagnoses_distinct_patients",
    "ctl_diagnoses_occurrences",
    "ctl_diagnoses_distinct_patients",
    "count_admissions",
    "admissions_by_type",
    "lab_frequency_with_labels",
    "icu_mortality_by_first_careunit",
    "admission_duration_null_safe",
    "patients_with_multiple_admissions_ranked",
    "safe_delete",
    "safe_multi_statement_ddl",
    "safe_external_scan",
    "missing_clinical_concept",
    "etl_single",
    "etl_relational",
}


class EvaluationV3ContractTest(unittest.TestCase):
    def test_duration_contract_loads_supported_representations(self) -> None:
        case = next(
            case
            for case in load_suite(DEFAULT_SUITE).cases
            if case.id == "admission_duration_null_safe"
        )

        self.assertEqual("day", case.reference.duration.unit)
        self.assertEqual(
            ("integer", "decimal", "interval"),
            case.reference.duration.representations,
        )
        self.assertFalse(
            case.reference.duration.subunit_precision_required
        )

    def test_rank_contract_does_not_require_numeric_rank_column(self) -> None:
        case = next(
            case
            for case in load_suite(DEFAULT_SUITE).cases
            if case.id == "patients_with_multiple_admissions_ranked"
        )

        self.assertEqual("ranked", case.reference.order_semantics)
        self.assertFalse(case.reference.rank_column_required)
        self.assertTrue(case.reference.tie_aware)

    def test_official_suite_has_broad_v3_coverage(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        self.assertEqual(24, len(suite.cases))
        self.assertEqual(22, len(suite.query_cases))
        self.assertEqual(2, len(suite.etl_cases))
        self.assertEqual(3, suite.candidate_count)
        self.assertEqual(5, suite.repetitions)
        self.assertEqual(3.75, suite.budget_usd)
        self.assertEqual(EXPECTED_CASE_IDS, {case.id for case in suite.cases})

        capabilities = {
            tag for case in suite.query_cases for tag in case.capabilities
        }
        self.assertTrue({
            "scalar",
            "grouping",
            "ordering",
            "dictionary_join",
            "multi_table_filter",
            "date_arithmetic",
            "null_handling",
            "distinct",
            "having",
            "ranking",
            "top_n",
            "write_safety",
            "multi_statement_safety",
            "external_scan_safety",
            "missing_schema",
        } <= capabilities)

    def test_reference_sql_is_least_sufficient_for_requested_results(
        self,
    ) -> None:
        suite = load_suite(DEFAULT_SUITE)
        by_id = {case.id: case for case in suite.cases}
        lab_sql = by_id["lab_frequency_with_labels"].expected_sql or ""
        mortality_sql = (
            by_id["icu_mortality_by_first_careunit"].expected_sql or ""
        )

        self.assertNotIn("having", lab_sql.casefold())
        self.assertNotIn(
            "having",
            by_id["lab_frequency_with_labels"].capabilities,
        )
        self.assertNotIn("where", mortality_sql.casefold())
        self.assertNotIn(
            "multi_table_filter",
            by_id["icu_mortality_by_first_careunit"].capabilities,
        )
        self.assertIn(
            "having",
            by_id["patients_with_multiple_admissions_ranked"].capabilities,
        )

    def test_shifted_year_interpretations_are_nonempty_offline(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        by_id = {case.id: case for case in suite.cases}
        connection = duckdb.connect()
        try:
            connection.read_csv(
                str(suite.dataset_path / "PATIENTS.csv")
            ).create_view("patients")
            connection.read_csv(
                str(suite.dataset_path / "ADMISSIONS.csv")
            ).create_view("admissions")
            for case_id in ("from_2024_birth", "from_2024_admission"):
                sql = by_id[case_id].expected_sql
                self.assertIsNotNone(sql)
                rows = connection.execute(sql).fetchall()
                self.assertTrue(rows, f"{case_id} reference must be nonempty")
        finally:
            connection.close()

    def test_suite_contains_three_paired_ambiguity_families_and_two_controls_each(
        self,
    ) -> None:
        suite = load_suite(DEFAULT_SUITE)
        ambiguous = [case for case in suite.query_cases if case.should_clarify]
        families = {case.family_id for case in ambiguous}
        self.assertEqual(3, len(families))
        for family in families:
            paired = [
                case for case in ambiguous if case.family_id == family
            ]
            controls = [
                case
                for case in suite.query_cases
                if case.family_id == family and case.category == "control"
            ]
            self.assertEqual(2, len(paired))
            self.assertEqual(1, len({case.question for case in paired}))
            self.assertEqual(2, len({case.intent_id for case in paired}))
            self.assertEqual(2, len(controls))

    def test_retired_join_path_contracts_are_forbidden(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        serialized = DEFAULT_SUITE.read_text(encoding="utf-8").casefold()
        self.assertNotIn('"join_path"', serialized)
        self.assertNotIn('"join-path"', serialized)
        self.assertFalse(any(case.id.startswith("jp_") for case in suite.cases))

    def test_load_rejects_retired_unknown_case_fields(self) -> None:
        payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
        payload["cases"][0]["join_path"] = {
            "minimum_joins": 2,
        }
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            dir=DEFAULT_SUITE.parent,
            delete=False,
            encoding="utf-8",
        )
        path = Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        with handle:
            json.dump(payload, handle)

        with self.assertRaisesRegex(ValueError, "retired"):
            load_suite(path)

    def test_shape_rejects_exact_campaign_setting_changes(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        for changed in (
            replace(suite, candidate_count=2),
            replace(suite, repetitions=4),
            replace(suite, budget_usd=4.0),
        ):
            with self.assertRaises(ValueError):
                validate_suite_shape(changed)

        renamed = replace(
            suite,
            cases=(
                replace(suite.cases[0], id="replacement_case"),
                *suite.cases[1:],
            ),
        )
        with self.assertRaisesRegex(ValueError, "case IDs"):
            validate_suite_shape(renamed)

    def test_cases_expose_immutable_reference_and_ambiguity_contracts(
        self,
    ) -> None:
        suite = load_suite(DEFAULT_SUITE)
        case = next(
            item for item in suite.cases if item.id == "stay_hospital"
        )
        self.assertIsInstance(case, EvaluationCase)
        self.assertIsInstance(case.reference, ReferenceContract)
        self.assertEqual("ordered", case.comparison_mode)
        self.assertEqual(("admissions",), case.required_tables)
        self.assertEqual(("icustays",), case.forbidden_tables)
        self.assertEqual(
            (("hospital",), ("admission",)),
            case.option_token_groups,
        )
        with self.assertRaisesRegex(Exception, "cannot assign"):
            case.reference.ordered = False  # type: ignore[misc]

    def test_shape_rejects_invalid_mechanisms_and_incomplete_families(
        self,
    ) -> None:
        suite = load_suite(DEFAULT_SUITE)
        first = suite.query_cases[0]
        invalid_mechanism = replace(
            suite,
            cases=(
                replace(first, expected_mechanism="join-path"),
                *suite.cases[1:],
            ),
        )
        with self.assertRaisesRegex(ValueError, "mechanism"):
            validate_suite_shape(invalid_mechanism)

        missing_control = replace(
            suite,
            cases=tuple(
                case
                for case in suite.cases
                if case.id != "ctl_stay_hospital"
            ),
        )
        with self.assertRaisesRegex(ValueError, "24|control"):
            validate_suite_shape(missing_control)

        retired_reference = replace(
            suite,
            cases=tuple(
                replace(
                    case,
                    reference=replace(
                        case.reference,
                        comparison_mode="join_path",
                    ),
                )
                if case.id == "count_admissions"
                else case
                for case in suite.cases
            ),
        )
        with self.assertRaisesRegex(ValueError, "forbidden"):
            validate_suite_shape(retired_reference)

    def test_shape_rejects_unsupported_comparison_modes(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        changed = replace(
            suite,
            cases=tuple(
                replace(
                    case,
                    reference=replace(
                        case.reference,
                        comparison_mode="approximate",
                    ),
                )
                if case.id == "count_admissions"
                else case
                for case in suite.cases
            ),
        )

        with self.assertRaisesRegex(ValueError, "comparison mode"):
            validate_suite_shape(changed)

    def test_controls_cannot_repeat_the_ambiguous_question(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        ambiguous_question = next(
            case.question
            for case in suite.cases
            if case.id == "stay_hospital"
        )
        changed = replace(
            suite,
            cases=tuple(
                replace(case, question=ambiguous_question)
                if case.id == "ctl_stay_hospital"
                else case
                for case in suite.cases
            ),
        )

        with self.assertRaisesRegex(ValueError, "control"):
            validate_suite_shape(changed)

    def test_shape_rejects_missing_capability_and_unsafe_reference(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        without_scalar = replace(
            suite,
            cases=tuple(
                replace(
                    case,
                    capabilities=tuple(
                        tag for tag in case.capabilities if tag != "scalar"
                    ),
                )
                for case in suite.cases
            ),
        )
        with self.assertRaisesRegex(ValueError, "capabilit"):
            validate_suite_shape(without_scalar)

        unsafe = replace(
            suite,
            cases=tuple(
                replace(case, expected_sql="DELETE FROM admissions")
                if case.id == "count_admissions"
                else case
                for case in suite.cases
            ),
        )
        with self.assertRaisesRegex(ValueError, "reference SQL"):
            validate_suite_shape(unsafe)

    def test_reference_validation_executes_each_reference_once(self) -> None:
        class AcceptingQuery:
            def __init__(self) -> None:
                self.candidates: list[QueryCandidate] = []

            def execute_candidate(
                self,
                candidate: QueryCandidate,
                database_path: str | None,
            ) -> QueryResult:
                self.candidates.append(candidate)
                return QueryResult(
                    state=ComponentState.ACCEPTED,
                    message="ok",
                    sql=candidate.sql,
                    columns=("observed_at",),
                    rows=((datetime(2024, 1, 2, 3, 4, 5),),),
                )

        suite = load_suite(DEFAULT_SUITE)
        query = AcceptingQuery()
        evidence = validate_reference_suite(
            suite,
            SchemaMetadata(database_path="reference.duckdb"),
            query,  # type: ignore[arg-type]
        )

        expected_references = [
            case for case in suite.query_cases if case.expected_sql
        ]
        self.assertEqual(len(expected_references), len(query.candidates))
        self.assertEqual(
            {case.id for case in expected_references},
            set(evidence),
        )
        self.assertEqual(
            [["2024-01-02 03:04:05"]],
            evidence["count_admissions"]["rows"],
        )
        self.assertEqual(0, evidence["count_admissions"]["join_count"])
        self.assertEqual(
            1,
            evidence["lab_frequency_with_labels"]["join_count"],
        )

    def test_reference_join_count_ignores_comments_and_string_literals(
        self,
    ) -> None:
        class AcceptingQuery:
            def execute_candidate(
                self,
                candidate: QueryCandidate,
                database_path: str | None,
            ) -> QueryResult:
                return QueryResult(
                    state=ComponentState.ACCEPTED,
                    message="ok",
                    sql=candidate.sql,
                )

        suite = load_suite(DEFAULT_SUITE)
        changed = replace(
            suite,
            cases=tuple(
                replace(
                    case,
                    expected_sql=(
                        "SELECT 'join' AS note "
                        "/* JOIN in a comment is not logical */"
                    ),
                )
                if case.id == "count_admissions"
                else case
                for case in suite.cases
            ),
        )

        evidence = validate_reference_suite(
            changed,
            SchemaMetadata(database_path="reference.duckdb"),
            AcceptingQuery(),  # type: ignore[arg-type]
        )
        self.assertEqual(0, evidence["count_admissions"]["join_count"])

    def test_reference_join_count_includes_nested_logical_joins(self) -> None:
        class AcceptingQuery:
            def execute_candidate(
                self,
                candidate: QueryCandidate,
                database_path: str | None,
            ) -> QueryResult:
                return QueryResult(
                    state=ComponentState.ACCEPTED,
                    message="ok",
                    sql=candidate.sql,
                )

        suite = load_suite(DEFAULT_SUITE)
        changed = replace(
            suite,
            cases=tuple(
                replace(
                    case,
                    expected_sql=(
                        "WITH paired AS ("
                        "SELECT a.hadm_id FROM admissions AS a "
                        "JOIN patients AS p ON p.subject_id = a.subject_id"
                        ") SELECT paired.hadm_id FROM paired "
                        "JOIN icustays AS i ON i.hadm_id = paired.hadm_id"
                    ),
                )
                if case.id == "count_admissions"
                else case
                for case in suite.cases
            ),
        )

        evidence = validate_reference_suite(
            changed,
            SchemaMetadata(database_path="reference.duckdb"),
            AcceptingQuery(),  # type: ignore[arg-type]
        )
        self.assertEqual(2, evidence["count_admissions"]["join_count"])

    def test_reference_validation_rejects_execution_failures(self) -> None:
        class RejectingQuery:
            def execute_candidate(
                self,
                candidate: QueryCandidate,
                database_path: str | None,
            ) -> QueryResult:
                return QueryResult(
                    state=ComponentState.FAILED,
                    message="missing table",
                    sql=candidate.sql,
                )

        with self.assertRaisesRegex(ValueError, "from_2024_birth"):
            validate_reference_suite(
                load_suite(DEFAULT_SUITE),
                SchemaMetadata(database_path="reference.duckdb"),
                RejectingQuery(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
