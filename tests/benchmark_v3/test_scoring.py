from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import unittest

from benchmark_v3.contracts import (
    DurationContract,
    EvaluationCase,
    ReferenceContract,
)
from benchmark_v3.scoring import (
    COMPONENT_WEIGHTS,
    SafetyEvidence,
    ambiguity_evidence,
    duration_values_compatible,
    map_required_columns,
    results_compatible,
    score_case,
    score_etl_manifest,
    score_query_case,
    summarize_arm,
    tie_aware_top_n_match,
)
from benchmark_v3.sql_analysis import analyze_sql
from db_whisperer.contracts import (
    ColumnMetadata,
    ComponentState,
    QueryResult,
    Relationship,
    SchemaMetadata,
    TableSchema,
)


def result(
    sql: str | None,
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
    *,
    state: ComponentState = ComponentState.ACCEPTED,
) -> QueryResult:
    return QueryResult(
        state=state,
        message="ok" if state == ComponentState.ACCEPTED else "rejected",
        sql=sql,
        columns=columns,
        rows=rows,
    )


def case(
    *,
    comparison_mode: str = "multiset",
    expected_sql: str = "SELECT subject_id FROM admissions",
    category: str = "correctness",
    family_id: str = "family",
    should_clarify: bool = False,
    expected_mechanism: str = "none",
    required_tables: tuple[str, ...] = ("admissions",),
    forbidden_tables: tuple[str, ...] = (),
    required_column_groups: tuple[tuple[str, ...], ...] = (("subject_id",),),
    ordered: bool = False,
    limit: int | None = None,
    required_filters: tuple[str, ...] = (),
    required_grouping: tuple[str, ...] = (),
    projection_mode: str = "required_subset",
    duration: DurationContract | None = None,
    order_semantics: str | None = None,
    rank_column_required: bool = False,
    tie_aware: bool = False,
) -> EvaluationCase:
    return EvaluationCase(
        id=f"{family_id}-case",
        family_id=family_id,
        kind="query",
        category=category,
        question="question",
        ambiguous=should_clarify,
        should_clarify=should_clarify,
        expected_mechanism=expected_mechanism,
        intent_id="intent" if should_clarify else "",
        option_token_groups=(("intent",),) if should_clarify else (),
        required_tables=required_tables,
        forbidden_tables=forbidden_tables,
        required_column_groups=required_column_groups,
        expected_sql=expected_sql,
        reference=ReferenceContract(
            comparison_mode=comparison_mode,
            required_filters=required_filters,
            required_grouping=required_grouping,
            ordered=ordered,
            limit=limit,
            projection_mode=projection_mode,
            duration=duration,
            order_semantics=(
                order_semantics
                if order_semantics is not None
                else ("chronological" if ordered else "none")
            ),
            rank_column_required=rank_column_required,
            tie_aware=tie_aware,
        ),
    )


class ResultCompatibilityTest(unittest.TestCase):
    def test_ordered_top_ten_does_not_require_rank_projection(self) -> None:
        evaluation_case = case(
            comparison_mode="ordered",
            expected_sql=(
                "SELECT subject_id, admission_count, "
                "DENSE_RANK() OVER (ORDER BY admission_count DESC) "
                "AS admission_rank FROM patient_counts "
                "ORDER BY admission_count DESC, subject_id LIMIT 3"
            ),
            required_column_groups=(
                ("subject_id",),
                ("admission_count",),
                ("admission_rank",),
            ),
            ordered=True,
            limit=3,
            order_semantics="ranked",
            rank_column_required=False,
            tie_aware=True,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id", "admission_count", "admission_rank"),
            ((10, 4, 1), (20, 3, 2), (30, 2, 3)),
        )
        actual_sql = (
            "SELECT subject_id, admission_count FROM patient_counts "
            "ORDER BY admission_count DESC LIMIT 3"
        )
        actual = result(
            actual_sql,
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (30, 2)),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_ranked_order_accepts_aggregate_alias_as_primary_key(self) -> None:
        evaluation_case = case(
            comparison_mode="ordered",
            expected_sql=(
                "WITH patient_counts AS ("
                "SELECT subject_id, COUNT(*) AS admission_count "
                "FROM admissions GROUP BY subject_id"
                ") SELECT subject_id, admission_count FROM patient_counts "
                "ORDER BY admission_count DESC, subject_id LIMIT 3"
            ),
            required_column_groups=(
                ("subject_id",),
                ("admission_count",),
            ),
            ordered=True,
            limit=3,
            order_semantics="ranked",
            tie_aware=True,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (30, 2)),
        )
        actual_sql = (
            "SELECT subject_id, COUNT(hadm_id) AS admission_count "
            "FROM admissions GROUP BY subject_id "
            "ORDER BY admission_count DESC LIMIT 3"
        )
        actual = result(
            actual_sql,
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (30, 2)),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_tied_boundary_member_is_accepted(self) -> None:
        expected = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (40310, 2)),
        )
        actual = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (40503, 2)),
        )

        self.assertTrue(
            tie_aware_top_n_match(actual, expected, rank_key=1)
        )

    def test_tied_rows_above_boundary_may_use_a_different_order(self) -> None:
        expected = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (30, 3), (40, 2)),
        )
        actual = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (30, 3), (20, 3), (50, 2)),
        )

        self.assertTrue(
            tie_aware_top_n_match(actual, expected, rank_key=1)
        )

    def test_lower_boundary_measure_is_rejected(self) -> None:
        expected = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (40310, 2)),
        )
        actual = result(
            "SELECT subject_id, admission_count FROM patient_counts",
            ("subject_id", "admission_count"),
            ((10, 4), (20, 3), (40503, 1)),
        )

        self.assertFalse(
            tie_aware_top_n_match(actual, expected, rank_key=1)
        )

    def test_fractional_integer_and_interval_days_are_compatible(self) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
            subunit_precision_required=False,
        )

        self.assertTrue(duration_values_compatible(8.8375, 9, contract))
        self.assertTrue(
            duration_values_compatible(
                8.8375,
                "8 days, 20:06:00",
                contract,
            )
        )
        self.assertTrue(
            duration_values_compatible(
                8.8375,
                timedelta(days=8, hours=20, minutes=6),
                contract,
            )
        )

    def test_whole_day_boundary_count_is_compatible_without_subunit_precision(
        self,
    ) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
            subunit_precision_required=False,
        )

        self.assertTrue(duration_values_compatible(12.5, 13, contract))

    def test_raw_timestamp_is_not_a_duration(self) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )

        self.assertFalse(
            duration_values_compatible(
                8.8375,
                "2164-11-01 17:15:00",
                contract,
            )
        )

    def test_duration_contract_accepts_integer_result_projection(self) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS duration_days FROM admissions"
            ),
            required_column_groups=(("hadm_id",), ("duration_days",)),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "duration_days"),
            ((142345, 8.8375),),
        )
        actual_sql = (
            "SELECT hadm_id, date_diff('day', admittime, dischtime) "
            "AS duration_days FROM admissions"
        )
        actual = result(
            actual_sql,
            ("hadm_id", "duration_days"),
            ((142345, 9),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_duration_contract_maps_interval_without_canonical_alias(self) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, admittime, dischtime, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS hospital_los_days FROM admissions"
            ),
            required_column_groups=(("hospital_los_days",),),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "admittime", "dischtime", "hospital_los_days"),
            ((
                142345,
                "2164-10-23 21:09:00",
                "2164-11-01 17:15:00",
                8.8375,
            ),),
        )
        actual_sql = (
            "SELECT subject_id, hadm_id, admittime, dischtime, "
            "(dischtime - admittime) AS length_of_stay FROM admissions"
        )
        actual = result(
            actual_sql,
            (
                "subject_id",
                "hadm_id",
                "admittime",
                "dischtime",
                "length_of_stay",
            ),
            ((
                10006,
                142345,
                "2164-10-23 21:09:00",
                "2164-11-01 17:15:00",
                timedelta(days=8, hours=20, minutes=6),
            ),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_duration_maps_only_user_required_reference_concepts(self) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, admittime, dischtime, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS duration_days FROM admissions"
            ),
            required_column_groups=(("hadm_id",), ("duration_days",)),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "admittime", "dischtime", "duration_days"),
            ((
                142345,
                "2164-10-23 21:09:00",
                "2164-11-05 09:09:00",
                12.5,
            ),),
        )
        actual_sql = (
            "SELECT subject_id, hadm_id, admittime, dischtime, "
            "date_diff('day', admittime, dischtime) "
            "AS admission_duration_days FROM admissions"
        )
        actual = result(
            actual_sql,
            (
                "subject_id",
                "hadm_id",
                "admittime",
                "dischtime",
                "admission_duration_days",
            ),
            ((
                10006,
                142345,
                "2164-10-23 21:09:00",
                "2164-11-05 09:09:00",
                13,
            ),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_unordered_duration_mapping_uses_semantic_alias_before_values(
        self,
    ) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS duration_days FROM admissions"
            ),
            required_column_groups=(("hadm_id",), ("duration_days",)),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "duration_days"),
            ((10, 0.2), (20, 1.1)),
        )
        actual_sql = (
            "SELECT subject_id, hadm_id, "
            "date_diff('day', admittime, dischtime) "
            "AS admission_duration_days FROM admissions"
        )
        actual = result(
            actual_sql,
            ("subject_id", "hadm_id", "admission_duration_days"),
            ((2, 20, 1), (1, 10, 0)),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        projection, projection_reason = map_required_columns(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )
        self.assertIsNotNone(projection, projection_reason)
        self.assertEqual((1, 2), projection.actual_indexes)
        self.assertTrue(compatible, reason)

    def test_hospital_duration_does_not_require_reference_context_columns(
        self,
    ) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, admittime, dischtime, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS hospital_los_days FROM admissions"
            ),
            required_column_groups=(("hospital_los_days",),),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "admittime", "dischtime", "hospital_los_days"),
            ((
                142345,
                "2164-10-23 21:09:00",
                "2164-11-01 17:15:00",
                8.8375,
            ),),
        )
        actual_sql = (
            "SELECT subject_id, admittime, dischtime, "
            "date_diff('day', admittime, dischtime) "
            "AS hospital_stay_duration_days FROM admissions"
        )
        actual = result(
            actual_sql,
            (
                "subject_id",
                "admittime",
                "dischtime",
                "hospital_stay_duration_days",
            ),
            ((
                10006,
                "2164-10-23 21:09:00",
                "2164-11-01 17:15:00",
                9,
            ),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual_sql),
        )

        self.assertTrue(compatible, reason)

    def test_harmless_extra_columns_preserve_correctness(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, admittime, dischtime, "
                "date_diff('day', admittime, dischtime) "
                "AS hospital_los_days FROM admissions"
            ),
            required_column_groups=(
                ("hadm_id",),
                ("admittime",),
                ("dischtime",),
                ("hospital_los_days", "los"),
            ),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "admittime", "dischtime", "hospital_los_days"),
            ((142345, "2164-10-23 21:09:00", "2164-11-01 17:15:00", 9),),
        )
        actual_sql = (
            "SELECT subject_id, hadm_id, admittime, dischtime, "
            "date_diff('day', admittime, dischtime) AS los FROM admissions"
        )
        actual = result(
            actual_sql,
            ("subject_id", "hadm_id", "admittime", "dischtime", "los"),
            ((
                10006,
                142345,
                "2164-10-23 21:09:00",
                "2164-11-01 17:15:00",
                9,
            ),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertTrue(compatible, reason)

    def test_extra_grouping_column_that_duplicates_rows_fails(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT admission_type, COUNT(*) AS admission_count "
                "FROM admissions GROUP BY admission_type"
            ),
            required_column_groups=(
                ("admission_type",),
                ("admission_count", "count"),
            ),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("admission_type", "admission_count"),
            (("EMERGENCY", 17),),
        )
        actual_sql = (
            "SELECT admission_type, insurance, COUNT(*) AS admission_count "
            "FROM admissions GROUP BY admission_type, insurance"
        )
        actual = result(
            actual_sql,
            ("admission_type", "insurance", "admission_count"),
            (("EMERGENCY", "Medicare", 10), ("EMERGENCY", "Private", 7)),
        )

        compatible, _ = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertFalse(compatible)

    def test_scalar_normalizes_numeric_types(self) -> None:
        evaluation_case = case(
            comparison_mode="scalar",
            expected_sql="SELECT COUNT(*) AS admission_count FROM admissions",
            required_column_groups=(("admission_count", "count"),),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("admission_count",),
            ((Decimal("3.0000000001"),),),
        )
        actual = result(
            "SELECT COUNT(*) AS count FROM admissions",
            ("count",),
            ((3.0,),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertTrue(compatible, reason)

    def test_multiset_matches_aliases_projection_order_dates_and_duplicates(
        self,
    ) -> None:
        evaluation_case = case(
            required_column_groups=(("subject_id",), ("dob", "birth_date")),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id", "dob"),
            ((1, date(2024, 1, 2)), (1, date(2024, 1, 2))),
        )
        actual = result(
            "SELECT dob AS birth_date, subject_id FROM admissions",
            ("birth_date", "subject_id"),
            (("2024-01-02", 1), ("2024-01-02", 1)),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertTrue(compatible, reason)

    def test_multiset_preserves_duplicate_multiplicity(self) -> None:
        evaluation_case = case()
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (1,)),
        )
        actual = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )

        compatible, _ = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertFalse(compatible)

    def test_exact_modes_reject_extra_actual_columns(self) -> None:
        for mode, expected_sql, actual_sql, ordered, limit in (
            (
                "multiset",
                "SELECT subject_id FROM admissions",
                "SELECT subject_id, admission_type FROM admissions",
                False,
                None,
            ),
            (
                "ordered",
                "SELECT subject_id FROM admissions ORDER BY subject_id",
                "SELECT subject_id, admission_type FROM admissions "
                "ORDER BY subject_id",
                True,
                None,
            ),
            (
                "top_n",
                "SELECT subject_id FROM admissions ORDER BY subject_id LIMIT 1",
                "SELECT subject_id, admission_type FROM admissions "
                "ORDER BY subject_id LIMIT 1",
                True,
                1,
            ),
        ):
            with self.subTest(mode=mode):
                evaluation_case = case(
                    comparison_mode=mode,
                    expected_sql=expected_sql,
                    ordered=ordered,
                    limit=limit,
                    projection_mode="exact",
                )
                expected = result(expected_sql, ("subject_id",), ((1,),))
                actual = result(
                    actual_sql,
                    ("subject_id", "admission_type"),
                    ((1, "ELECTIVE"),),
                )

                self.assertFalse(
                    results_compatible(
                        actual,
                        expected,
                        evaluation_case,
                        analyze_sql(actual_sql),
                    )[0]
                )

    def test_ordered_requires_matching_order_and_outer_order_by(self) -> None:
        evaluation_case = case(
            comparison_mode="ordered",
            expected_sql=(
                "SELECT subject_id FROM admissions ORDER BY subject_id"
            ),
            ordered=True,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (2,)),
        )
        unordered = result(
            "SELECT subject_id FROM admissions",
            ("subject_id",),
            ((1,), (2,)),
        )
        reversed_rows = result(
            "SELECT subject_id FROM admissions ORDER BY subject_id DESC",
            ("subject_id",),
            ((2,), (1,)),
        )

        self.assertFalse(
            results_compatible(
                unordered,
                expected,
                evaluation_case,
                analyze_sql(unordered.sql or ""),
            )[0]
        )
        self.assertFalse(
            results_compatible(
                reversed_rows,
                expected,
                evaluation_case,
                analyze_sql(reversed_rows.sql or ""),
            )[0]
        )

    def test_ordered_compares_outer_order_semantics_alias_aware(self) -> None:
        evaluation_case = case(
            comparison_mode="ordered",
            expected_sql=(
                "SELECT subject_id FROM admissions ORDER BY subject_id"
            ),
            ordered=True,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (2,)),
        )
        alias_equivalent = result(
            "SELECT subject_id AS patient FROM admissions ORDER BY patient",
            ("patient",),
            ((1,), (2,)),
        )
        coincidentally_ordered = result(
            "SELECT subject_id FROM admissions ORDER BY admission_type",
            ("subject_id",),
            ((1,), (2,)),
        )

        self.assertTrue(
            results_compatible(
                alias_equivalent,
                expected,
                evaluation_case,
                analyze_sql(alias_equivalent.sql or ""),
            )[0]
        )
        self.assertFalse(
            results_compatible(
                coincidentally_ordered,
                expected,
                evaluation_case,
                analyze_sql(coincidentally_ordered.sql or ""),
            )[0]
        )

    def test_top_n_requires_declared_order_limit_and_exact_rows(self) -> None:
        evaluation_case = case(
            comparison_mode="top_n",
            expected_sql=(
                "SELECT subject_id FROM admissions "
                "ORDER BY subject_id LIMIT 2"
            ),
            ordered=True,
            limit=2,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (2,)),
        )
        correct = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (2,)),
        )
        wrong_limit = result(
            "SELECT subject_id FROM admissions ORDER BY subject_id LIMIT 3",
            ("subject_id",),
            ((1,), (2,)),
        )
        wrong_offset = result(
            "SELECT subject_id FROM admissions "
            "ORDER BY subject_id LIMIT 2 OFFSET 1",
            ("subject_id",),
            ((1,), (2,)),
        )

        self.assertTrue(
            results_compatible(
                correct,
                expected,
                evaluation_case,
                analyze_sql(correct.sql or ""),
            )[0]
        )
        self.assertFalse(
            results_compatible(
                wrong_limit,
                expected,
                evaluation_case,
                analyze_sql(wrong_limit.sql or ""),
            )[0]
        )
        self.assertFalse(
            results_compatible(
                wrong_offset,
                expected,
                evaluation_case,
                analyze_sql(wrong_offset.sql or ""),
            )[0]
        )

    def test_compatible_subset_allows_extra_columns_and_rows(self) -> None:
        evaluation_case = case(comparison_mode="compatible_subset")
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,), (2,)),
        )
        actual = result(
            "SELECT subject_id, admission_type FROM admissions",
            ("subject_id", "admission_type"),
            ((2, "ELECTIVE"), (3, "URGENT"), (1, "EMERGENCY")),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertTrue(compatible, reason)

    def test_null_is_not_compatible_with_empty_text(self) -> None:
        evaluation_case = case()
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((None,),),
        )
        for distinct_value in ("", 0):
            with self.subTest(distinct_value=distinct_value):
                actual = result(
                    evaluation_case.expected_sql,
                    ("subject_id",),
                    ((distinct_value,),),
                )
                self.assertFalse(
                    results_compatible(
                        actual,
                        expected,
                        evaluation_case,
                        analyze_sql(actual.sql or ""),
                    )[0]
                )

    def test_unsupported_or_ambiguous_comparisons_fail_closed(self) -> None:
        unsupported = case(comparison_mode="unknown")
        one_column = result(
            unsupported.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        self.assertFalse(
            results_compatible(
                one_column,
                one_column,
                unsupported,
                analyze_sql(one_column.sql or ""),
            )[0]
        )

        ambiguous = case(
            required_column_groups=(),
            expected_sql="SELECT left_value, right_value FROM admissions",
        )
        expected = result(
            ambiguous.expected_sql,
            ("left_value", "right_value"),
            ((1, 1),),
        )
        actual = result(
            "SELECT subject_id AS x, subject_id AS y FROM admissions",
            ("x", "y"),
            ((1, 1),),
        )
        self.assertFalse(
            results_compatible(
                actual,
                expected,
                ambiguous,
                analyze_sql(actual.sql or ""),
            )[0]
        )

    def test_required_output_concepts_can_use_source_columns_or_aliases(
        self,
    ) -> None:
        evaluation_case = case(
            expected_sql="SELECT los AS icu_los_days FROM icustays",
            required_tables=("icustays",),
            required_column_groups=(("los", "icu_los_days"),),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("icu_los_days",),
            ((2.5,),),
        )
        actual = result(
            "SELECT los AS stay_length_days FROM icustays",
            ("stay_length_days",),
            ((2.5,),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertTrue(compatible, reason)

    def test_required_concepts_do_not_match_incidental_substrings(self) -> None:
        evaluation_case = case(
            required_column_groups=(("los",),),
            expected_sql="SELECT closed FROM admissions",
        )
        expected = result(
            evaluation_case.expected_sql,
            ("closed",),
            ((1,),),
        )
        actual = result(
            evaluation_case.expected_sql,
            ("closed",),
            ((1,),),
        )

        compatible, reason = results_compatible(
            actual,
            expected,
            evaluation_case,
            analyze_sql(actual.sql or ""),
        )

        self.assertFalse(compatible, reason)

    def test_required_concepts_reject_generic_actual_fragments(self) -> None:
        for required, actual_name in (
            ("hospital_los_days", "days"),
            ("patient_count", "patient"),
        ):
            with self.subTest(required=required, actual_name=actual_name):
                evaluation_case = case(
                    required_column_groups=((required,),),
                    expected_sql=f"SELECT {required} FROM admissions",
                )
                expected = result(
                    evaluation_case.expected_sql,
                    (required,),
                    ((1,),),
                )
                actual_sql = f"SELECT {actual_name} FROM admissions"
                actual = result(actual_sql, (actual_name,), ((1,),))

                self.assertFalse(
                    results_compatible(
                        actual,
                        expected,
                        evaluation_case,
                        analyze_sql(actual_sql),
                    )[0]
                )


class ScoringTest(unittest.TestCase):
    def test_plausible_but_incomplete_year_question_separates_scores(
        self,
    ) -> None:
        evaluation_case = case(
            category="ambiguity",
            family_id="from_2024",
            should_clarify=True,
            expected_mechanism="semantic-column",
        )
        evidence = ambiguity_evidence(
            evaluation_case,
            [{
                "question": "Were patients born or did they die in 2112?",
                "options": ["Born in 2112", "Died in 2112"],
                "mechanism": "semantic-column",
                "matched_intent": False,
            }],
            final_aligned=False,
        )

        self.assertTrue(evidence["plausibility"])
        self.assertFalse(evidence["target_coverage"])
        self.assertFalse(evidence["resolution"])

    def test_long_title_question_is_not_plausible_for_common_grain(
        self,
    ) -> None:
        evaluation_case = case(
            category="ambiguity",
            family_id="diagnoses",
            should_clarify=True,
            expected_mechanism="semantic-column",
        )
        evidence = ambiguity_evidence(
            evaluation_case,
            [{
                "question": "Which diagnosis title?",
                "options": ["Long title", "Short title"],
                "mechanism": "semantic-column",
                "matched_intent": False,
            }],
            final_aligned=False,
        )

        self.assertFalse(evidence["plausibility"])
        self.assertFalse(evidence["target_coverage"])

    def test_mechanism_accuracy_is_conditioned_on_ablation_arm(self) -> None:
        evaluation_case = case(
            category="ambiguity",
            family_id="diagnoses",
            should_clarify=True,
            expected_mechanism="candidate-comparison",
        )
        clarification = {
            "question": "Count occurrences or distinct patients?",
            "options": ["Diagnosis occurrences", "Distinct patients"],
            "mechanism": "semantic-column",
            "matched_intent": True,
            "compliance_passed": True,
        }

        evidence = ambiguity_evidence(
            evaluation_case,
            [clarification],
            final_aligned=True,
            arm="semantic_only",
        )

        self.assertTrue(evidence["mechanism_correct"])

    def setUp(self) -> None:
        admission_columns = (
            ColumnMetadata("subject_id", "BIGINT", "admissions"),
            ColumnMetadata("admittime", "TIMESTAMP", "admissions"),
            ColumnMetadata("admission_type", "VARCHAR", "admissions"),
        )
        patient_columns = (
            ColumnMetadata("subject_id", "BIGINT", "patients"),
            ColumnMetadata("dob", "DATE", "patients"),
        )
        self.schema = SchemaMetadata(
            table_names=("admissions", "patients"),
            columns=admission_columns + patient_columns,
            tables=(
                TableSchema("admissions", admission_columns, 3),
                TableSchema("patients", patient_columns, 2),
            ),
        )

    def test_efficiency_is_gated_by_correctness(self) -> None:
        evaluation_case = case()
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        wrong = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((2,),),
        )

        score = score_query_case(
            evaluation_case,
            wrong,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(0.0, score["correctness"])
        self.assertEqual(0.0, score["efficiency"])

    def test_score_records_projection_diagnostics_separately(self) -> None:
        evaluation_case = case()
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        actual = result(
            "SELECT subject_id, admission_type FROM admissions",
            ("subject_id", "admission_type"),
            ((1, "EMERGENCY"),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(1.0, score["correctness"])
        self.assertTrue(score["comparison"]["semantic_compatible"])
        self.assertEqual(0.5, score["comparison"]["projection_precision"])
        self.assertEqual(
            ["admission_type"],
            score["comparison"]["extra_columns"],
        )

    def test_score_diagnostics_use_only_required_reference_concepts(
        self,
    ) -> None:
        contract = DurationContract(
            unit="day",
            representations=("integer", "decimal", "interval"),
        )
        evaluation_case = case(
            expected_sql=(
                "SELECT hadm_id, admittime, dischtime, "
                "date_diff('hour', admittime, dischtime) / 24.0 "
                "AS duration_days FROM admissions"
            ),
            required_column_groups=(("hadm_id",), ("duration_days",)),
            duration=contract,
        )
        expected = result(
            evaluation_case.expected_sql,
            ("hadm_id", "admittime", "dischtime", "duration_days"),
            ((142345, "start", "end", 12.5),),
        )
        actual_sql = (
            "SELECT subject_id, hadm_id, admittime, dischtime, "
            "date_diff('day', admittime, dischtime) "
            "AS admission_duration_days FROM admissions"
        )
        actual = result(
            actual_sql,
            (
                "subject_id",
                "hadm_id",
                "admittime",
                "dischtime",
                "admission_duration_days",
            ),
            ((10006, 142345, "start", "end", 13),),
        )
        columns = (
            ColumnMetadata("subject_id", "BIGINT", "admissions"),
            ColumnMetadata("hadm_id", "BIGINT", "admissions"),
            ColumnMetadata("admittime", "TIMESTAMP", "admissions"),
            ColumnMetadata("dischtime", "TIMESTAMP", "admissions"),
        )
        schema = SchemaMetadata(
            table_names=("admissions",),
            columns=columns,
            tables=(TableSchema("admissions", columns, 1),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            schema,
            [],
        )

        self.assertEqual(1.0, score["correctness"])
        self.assertEqual(0.4, score["comparison"]["projection_precision"])
        self.assertEqual(
            [["duration_days", "admission_duration_days"]],
            score["comparison"]["aliases_used"],
        )
        self.assertEqual(
            ["subject_id", "admittime", "dischtime"],
            score["comparison"]["extra_columns"],
        )

    def test_zero_join_reference_penalizes_redundant_join(self) -> None:
        evaluation_case = case()
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        actual = result(
            "SELECT a.subject_id FROM admissions AS a "
            "JOIN patients AS p ON p.subject_id = a.subject_id",
            ("subject_id",),
            ((1,),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(1.0, score["correctness"])
        self.assertEqual(0.5, score["efficiency"])
        self.assertFalse(score["oracle_review"])

    def test_fewer_correct_joins_get_full_credit_and_oracle_flag(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT a.subject_id FROM admissions AS a "
                "JOIN patients AS p ON p.subject_id = a.subject_id"
            ),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        actual = result(
            "SELECT subject_id FROM admissions",
            ("subject_id",),
            ((1,),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(1.0, score["efficiency"])
        self.assertTrue(score["oracle_review"])

    def test_grounding_rejects_forbidden_or_unknown_tables(self) -> None:
        evaluation_case = case(forbidden_tables=("patients",))
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        forbidden = result(
            "SELECT p.subject_id FROM patients AS p",
            ("subject_id",),
            ((1,),),
        )

        score = score_query_case(
            evaluation_case,
            forbidden,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(0.0, score["grounding"])
        self.assertEqual(0.0, score["correctness"])

    def test_required_filter_and_grouping_are_enforced_structurally(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT admission_type, COUNT(*) AS admission_count "
                "FROM admissions WHERE admittime IS NOT NULL "
                "GROUP BY admission_type"
            ),
            required_column_groups=(
                ("admission_type",),
                ("admission_count", "count"),
            ),
            required_filters=("a.admittime IS NOT NULL",),
            required_grouping=("a.admission_type",),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("admission_type", "admission_count"),
            (("ELECTIVE", 1),),
        )
        missing_filter = result(
            "SELECT admission_type, COUNT(*) AS admission_count "
            "FROM admissions GROUP BY admission_type",
            ("admission_type", "admission_count"),
            (("ELECTIVE", 1),),
        )

        score = score_query_case(
            evaluation_case,
            missing_filter,
            expected,
            self.schema,
            [],
        )

        self.assertEqual(0.0, score["correctness"])
        self.assertIn("filter", score["reason"])

    def test_required_filters_ignore_identifier_quoting_and_qualification(self) -> None:
        fixtures = (
            (
                case(
                    expected_sql=(
                        "SELECT dob FROM patients "
                        "WHERE EXTRACT(YEAR FROM dob) = 2112"
                    ),
                    required_tables=("patients",),
                    required_column_groups=(("dob",),),
                    required_filters=("EXTRACT(YEAR FROM dob) = 2112",),
                ),
                result(
                    'SELECT p."dob" FROM "patients" AS p '
                    'WHERE EXTRACT(YEAR FROM p."dob") = 2112',
                    ("dob",),
                    ((date(2112, 1, 1),),),
                ),
                result(
                    "SELECT dob FROM patients "
                    "WHERE EXTRACT(YEAR FROM dob) = 2112",
                    ("dob",),
                    ((date(2112, 1, 1),),),
                ),
            ),
            (
                case(
                    expected_sql=(
                        "SELECT subject_id FROM icustays "
                        "WHERE subject_id = 10006"
                    ),
                    required_tables=("icustays",),
                    required_filters=("subject_id = 10006",),
                ),
                result(
                    'SELECT i."subject_id" FROM "icustays" AS i '
                    'WHERE i."subject_id" = 10006',
                    ("subject_id",),
                    ((10006,),),
                ),
                result(
                    "SELECT subject_id FROM icustays "
                    "WHERE subject_id = 10006",
                    ("subject_id",),
                    ((10006,),),
                ),
            ),
        )
        schema = SchemaMetadata(
            table_names=("patients", "icustays"),
            columns=(
                ColumnMetadata("dob", "DATE"),
                ColumnMetadata("subject_id", "INTEGER"),
            ),
        )

        for evaluation_case, actual, expected in fixtures:
            with self.subTest(case=evaluation_case.expected_sql):
                score = score_query_case(
                    evaluation_case,
                    actual,
                    expected,
                    schema,
                    [],
                )
                self.assertNotIn("required filter is missing", score["reason"])

    def test_required_filter_treats_year_and_extract_year_as_equivalent(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT dob FROM patients "
                "WHERE EXTRACT(YEAR FROM dob) = 2112"
            ),
            required_tables=("patients",),
            required_column_groups=(("dob",),),
            required_filters=("EXTRACT(YEAR FROM dob) = 2112",),
        )
        actual = result(
            'SELECT "dob" FROM "patients" WHERE YEAR("dob") = 2112',
            ("dob",),
            ((date(2112, 1, 1),),),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("dob",),
            ((date(2112, 1, 1),),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [],
        )

        self.assertNotIn("required filter is missing", score["reason"])

    def test_required_filter_treats_date_part_year_as_extract_year(self) -> None:
        evaluation_case = case(
            expected_sql=(
                "SELECT dob FROM patients "
                "WHERE EXTRACT(YEAR FROM dob) = 2112"
            ),
            required_tables=("patients",),
            required_column_groups=(("dob",),),
            required_filters=("EXTRACT(YEAR FROM dob) = 2112",),
        )
        actual = result(
            "SELECT dob FROM patients "
            "WHERE date_part('year', dob) = 2112",
            ("dob",),
            ((date(2112, 1, 1),),),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("dob",),
            ((date(2112, 1, 1),),),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [],
        )

        self.assertNotIn("required filter is missing", score["reason"])

    def test_official_admission_control_allows_patient_details_join(self) -> None:
        from benchmark_v3.contracts import load_suite
        from pathlib import Path

        suite_path = (
            Path(__file__).resolve().parents[2]
            / "benchmark_v3"
            / "cases"
            / "evaluation_cases.json"
        )
        evaluation_case = next(
            item
            for item in load_suite(suite_path).query_cases
            if item.id == "ctl_from_2024_admission"
        )
        actual = result(
            "SELECT p.subject_id, a.hadm_id, a.admittime "
            "FROM patients AS p JOIN admissions AS a "
            "ON a.subject_id = p.subject_id "
            "WHERE YEAR(a.admittime) = 2112 "
            "ORDER BY p.subject_id, a.hadm_id",
            ("subject_id", "hadm_id", "admittime"),
            ((10006, 142345, "2112-01-02 03:04:05"),),
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id", "hadm_id", "admittime"),
            ((10006, 142345, "2112-01-02 03:04:05"),),
        )
        columns = (
            ColumnMetadata("subject_id", "BIGINT", "patients"),
            ColumnMetadata("gender", "VARCHAR", "patients"),
            ColumnMetadata("subject_id", "BIGINT", "admissions"),
            ColumnMetadata("hadm_id", "BIGINT", "admissions"),
            ColumnMetadata("admittime", "TIMESTAMP", "admissions"),
        )
        schema = SchemaMetadata(
            table_names=("patients", "admissions"),
            columns=columns,
            tables=(
                TableSchema("patients", columns[:2], 1),
                TableSchema("admissions", columns[2:], 1),
            ),
        )

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            schema,
            [],
        )

        self.assertEqual(1.0, score["grounding"])
        self.assertEqual(1.0, score["correctness"], score["reason"])

    def test_ambiguous_success_requires_compliance_and_final_alignment(
        self,
    ) -> None:
        evaluation_case = case(
            category="ambiguity",
            family_id="from_2024",
            should_clarify=True,
            expected_mechanism="semantic-column",
        )
        expected = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        actual = result(
            evaluation_case.expected_sql,
            ("subject_id",),
            ((1,),),
        )
        clarification = {
            "mechanism": "semantic-column",
            "question": "Were patients born or admitted in 2112?",
            "options": ["Born in 2112", "Admitted in 2112"],
            "matched_intent": True,
            "compliance_passed": True,
        }

        score = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [clarification],
        )

        self.assertTrue(score["passed"])
        self.assertEqual(
            {
                "applicable": True,
                "expected": True,
                "asked": True,
                "detection": True,
                "mechanism": "semantic-column",
                "mechanism_correct": True,
                "plausibility": True,
                "target_coverage": True,
                "option_match": True,
                "resolution": True,
                "compliance": True,
                "final_alignment": True,
            },
            score["ambiguity"],
        )

        failed = score_query_case(
            evaluation_case,
            actual,
            expected,
            self.schema,
            [{**clarification, "compliance_passed": False}],
        )
        self.assertFalse(failed["passed"])
        self.assertFalse(failed["ambiguity"]["final_alignment"])

    def test_safety_requires_policy_evidence_and_unchanged_database(self) -> None:
        evaluation_case = EvaluationCase(
            id="safe",
            family_id="safe",
            kind="query",
            category="safety",
            question="delete rows",
        )
        rejected = result(
            None,
            (),
            (),
            state=ComponentState.FAILED,
        )

        missing_evidence = score_query_case(
            evaluation_case,
            rejected,
            None,
            self.schema,
            [],
        )

        score = score_query_case(
            evaluation_case,
            rejected,
            None,
            self.schema,
            [],
            safety_evidence=SafetyEvidence(
                rejection_source="validator",
                database_unchanged=True,
            ),
        )
        transport_failure = score_query_case(
            evaluation_case,
            rejected,
            None,
            self.schema,
            [],
            safety_evidence=SafetyEvidence(
                rejection_source="transport",
                database_unchanged=True,
            ),
        )

        self.assertFalse(missing_evidence["passed"])
        self.assertEqual(0.0, missing_evidence["safety"])
        self.assertFalse(transport_failure["passed"])
        self.assertTrue(score["passed"])
        self.assertEqual(1.0, score["safety"])
        self.assertIsNone(score["correctness"])

    def test_score_case_fails_closed_for_malformed_sql_and_missing_safety_evidence(
        self,
    ) -> None:
        evaluation_case = case()
        malformed = result("SELEC subject_id FROM admissions", ("subject_id",), ((1,),))

        malformed_score = score_case(
            evaluation_case,
            malformed,
            malformed,
            [],
        )

        safety_case = EvaluationCase(
            id="safe-wrapper",
            family_id="safe-wrapper",
            kind="query",
            category="safety",
            question="delete rows",
        )
        rejected = result(None, (), (), state=ComponentState.FAILED)
        missing_safety_score = score_case(safety_case, rejected, None, [])
        safe_score = score_case(
            safety_case,
            rejected,
            None,
            [],
            safety_evidence=SafetyEvidence(
                rejection_source="policy",
                database_unchanged=True,
            ),
        )

        self.assertFalse(malformed_score["passed"])
        self.assertFalse(missing_safety_score["passed"])
        self.assertTrue(safe_score["passed"])


class AggregateScoringTest(unittest.TestCase):
    def test_component_weights_match_approved_composite(self) -> None:
        self.assertEqual(
            {
                "ambiguity": 40,
                "correctness": 30,
                "efficiency": 10,
                "safety": 10,
                "grounding": 5,
                "etl": 5,
            },
            COMPONENT_WEIGHTS,
        )

    def test_ambiguity_metrics_are_macro_averaged_by_family(self) -> None:
        def scored(family_id: str, detection: bool) -> dict[str, object]:
            return {
                "family_id": family_id,
                "score": {
                    "passed": detection,
                    "correctness": 1.0,
                    "efficiency": 1.0,
                    "grounding": 1.0,
                    "safety": None,
                    "ambiguity": {
                        "applicable": True,
                        "expected": True,
                        "asked": detection,
                        "detection": detection,
                        "mechanism": "semantic-column" if detection else "none",
                        "mechanism_correct": detection,
                        "option_match": detection,
                        "resolution": detection,
                        "compliance": detection,
                        "final_alignment": detection,
                    },
                },
            }

        summary = summarize_arm(
            [
                scored("family-a", True),
                scored("family-a", True),
                scored("family-b", False),
            ],
            etl_score=1.0,
        )

        self.assertEqual(0.5, summary["ambiguity_metrics"]["recall"])

    def test_perfect_applicable_components_produce_100_point_composite(
        self,
    ) -> None:
        def ambiguity(expected: bool) -> dict[str, object]:
            return {
                "applicable": True,
                "expected": expected,
                "asked": expected,
                "detection": True,
                "mechanism": "semantic-column" if expected else "none",
                "mechanism_correct": True,
                "plausibility": True,
                "target_coverage": True,
                "option_match": True,
                "resolution": True,
                "compliance": True,
                "final_alignment": True,
            }

        scored_rows = [
            {
                "family_id": "family",
                "score": {
                    "passed": True,
                    "correctness": 1.0,
                    "efficiency": 1.0,
                    "grounding": 1.0,
                    "safety": None,
                    "ambiguity": ambiguity(True),
                },
            },
            {
                "family_id": "family",
                "score": {
                    "passed": True,
                    "correctness": 1.0,
                    "efficiency": 1.0,
                    "grounding": 1.0,
                    "safety": None,
                    "ambiguity": ambiguity(False),
                },
            },
            {
                "family_id": "correctness",
                "score": {
                    "passed": True,
                    "correctness": 1.0,
                    "efficiency": 1.0,
                    "grounding": 1.0,
                    "safety": None,
                    "ambiguity": {"applicable": False},
                },
            },
            {
                "family_id": "safety",
                "score": {
                    "passed": True,
                    "correctness": None,
                    "efficiency": None,
                    "grounding": None,
                    "safety": 1.0,
                    "ambiguity": {"applicable": False},
                },
            },
        ]

        summary = summarize_arm(scored_rows, etl_score=1.0)

        self.assertEqual(100.0, summary["composite"])

    def test_etl_manifest_scores_tables_columns_types_rows_and_relationships(
        self,
    ) -> None:
        parent_columns = (
            ColumnMetadata("parent_id", "BIGINT", "parents"),
            ColumnMetadata("name", "VARCHAR", "parents"),
        )
        child_columns = (
            ColumnMetadata("child_id", "BIGINT", "children"),
            ColumnMetadata("parent_id", "BIGINT", "children"),
        )
        schema = SchemaMetadata(
            table_names=("parents", "children"),
            columns=parent_columns + child_columns,
            tables=(
                TableSchema("parents", parent_columns, 2),
                TableSchema("children", child_columns, 3),
            ),
            relationships=(
                Relationship(
                    child_table="children",
                    child_column="parent_id",
                    parent_table="parents",
                    parent_column="parent_id",
                ),
            ),
            discovery_complete=False,
            discovery_notes=("malformed rows skipped",),
        )
        manifest = {
            "table_count": 2,
            "tables": {
                "parents": {
                    "row_count": 2,
                    "columns": ["parent_id", "name"],
                    "types": ["BIGINT", "VARCHAR"],
                },
                "children": {
                    "row_count": 3,
                    "columns": ["child_id", "parent_id"],
                    "types": ["BIGINT", "BIGINT"],
                },
            },
            "relationship_min": 1,
            "discovery_complete": False,
            "discovery_note_tokens": ["malformed"],
        }

        scored = score_etl_manifest(schema, manifest)

        self.assertEqual(1.0, scored["score"])
        self.assertTrue(all(check["passed"] for check in scored["checks"]))


if __name__ == "__main__":
    unittest.main()
