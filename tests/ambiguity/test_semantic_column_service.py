"""Tests for pre-SQL semantic-column analysis."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity.semantic_column_service import (
    SemanticColumnAmbiguityService,
    semantic_bucket,
)
from db_whisperer.contracts import (
    ColumnMetadata,
    ComponentState,
    ExecutedQueryPair,
    SchemaMetadata,
    SemanticAmbiguityTerm,
    SemanticColumnAnalysis,
    SemanticColumnCandidate,
    SemanticGrounding,
    SemanticInterpretation,
    SemanticColumnRequest,
)


class FakeClient:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.prompts = []

    def evaluate(self, prompt, api_key, model):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.response


def schema() -> SchemaMetadata:
    return SchemaMetadata(
        table_names=("orders", "customers"),
        columns=(
            ColumnMetadata("order_date", "DATE", "orders"),
            ColumnMetadata("required_date", "DATE", "orders"),
            ColumnMetadata("name", "VARCHAR", "customers"),
        ),
    )


def request(clarifications=()) -> SemanticColumnRequest:
    return SemanticColumnRequest(
        user_query="show the important dates",
        schema=schema(),
        api_key="key",
        model="provider/model",
        clarifications=clarifications,
    )


def common_schema() -> SchemaMetadata:
    return SchemaMetadata(
        table_names=("diagnoses_icd", "d_icd_diagnoses"),
        columns=(
            ColumnMetadata("icd9_code", "VARCHAR", "diagnoses_icd"),
            ColumnMetadata("subject_id", "INTEGER", "diagnoses_icd"),
            ColumnMetadata("icd9_code", "VARCHAR", "d_icd_diagnoses"),
            ColumnMetadata("long_title", "VARCHAR", "d_icd_diagnoses"),
            ColumnMetadata("short_title", "VARCHAR", "d_icd_diagnoses"),
        ),
    )


def common_request() -> SemanticColumnRequest:
    return SemanticColumnRequest(
        user_query="Show the most common diagnoses.",
        schema=common_schema(),
        api_key="key",
        model="provider/model",
    )


def common_finding() -> dict[str, object]:
    return {
        "term": "most common diagnoses",
        "dimension": "aggregation_grain",
        "resolved_by_context": False,
        "interpretations": [
            {
                "label": "Diagnosis record count",
                "meaning": "Count every diagnosis row.",
                "relevance": 1,
                "tables": ["diagnoses_icd"],
                "columns": ["diagnoses_icd.icd9_code"],
                "operations": ["count_rows"],
                "grain": "diagnosis_code",
                "temporal_role": "",
            },
            {
                "label": "Distinct patient count",
                "meaning": "Count unique patients.",
                "relevance": 2,
                "tables": ["diagnoses_icd"],
                "columns": [
                    "diagnoses_icd.icd9_code",
                    "diagnoses_icd.subject_id",
                ],
                "operations": ["count_distinct"],
                "grain": "diagnosis_code",
                "temporal_role": "",
            },
        ],
    }


class SemanticColumnAnalysisTest(unittest.TestCase):
    def test_model_cannot_invent_a_vague_term_absent_from_user_query(self) -> None:
        client = FakeClient(response={"findings": [common_finding()]})
        service = SemanticColumnAmbiguityService(client=client)

        result = service.analyze(request())

        self.assertEqual(ComponentState.ACCEPTED, result.state)
        self.assertEqual((), result.terms)

    def test_structured_finding_exposes_ranked_grounded_interpretations(self) -> None:
        finding = SemanticAmbiguityTerm(
            term="common",
            dimension="aggregation_grain",
            interpretations=(
                SemanticInterpretation(
                    interpretation_id="interpretation_1",
                    label="Diagnosis record count",
                    meaning="Count every diagnosis record.",
                    relevance=1,
                    grounding=SemanticGrounding(
                        tables=("diagnoses_icd",),
                        columns=("diagnoses_icd.icd9_code",),
                        operations=("count_rows",),
                        grain="diagnosis_code",
                    ),
                ),
                SemanticInterpretation(
                    interpretation_id="interpretation_2",
                    label="Distinct patient count",
                    meaning="Count distinct affected patients.",
                    relevance=2,
                    grounding=SemanticGrounding(
                        tables=("diagnoses_icd",),
                        columns=(
                            "diagnoses_icd.icd9_code",
                            "diagnoses_icd.subject_id",
                        ),
                        operations=("count_distinct",),
                        grain="diagnosis_code",
                    ),
                ),
            ),
        )

        self.assertEqual("aggregation_grain", finding.dimension)
        self.assertEqual(
            ("interpretation_1", "interpretation_2"),
            tuple(item.interpretation_id for item in finding.interpretations),
        )

    def test_bucket_mapping(self) -> None:
        self.assertEqual("temporal", semantic_bucket("TIMESTAMP WITH TIME ZONE"))
        self.assertEqual("numeric", semantic_bucket("DECIMAL(10,2)"))
        self.assertEqual("boolean", semantic_bucket("BOOLEAN"))
        self.assertEqual("textual", semantic_bucket("VARCHAR"))

    def test_common_parses_as_aggregation_grain(self) -> None:
        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": [common_finding()]})
        ).analyze(common_request())

        self.assertTrue(analysis.ambiguous)
        finding = analysis.terms[0]
        self.assertEqual("aggregation_grain", finding.dimension)
        self.assertEqual(
            ("interpretation_1", "interpretation_2"),
            tuple(item.interpretation_id for item in finding.interpretations),
        )
        self.assertEqual(
            ("count_rows",),
            finding.interpretations[0].grounding.operations,
        )

    def test_explicit_hospital_modifier_returns_no_finding(self) -> None:
        hospital_request = SemanticColumnRequest(
            user_query="Show hospital mortality by first ICU care unit.",
            schema=SchemaMetadata(columns=(
                ColumnMetadata("hospital_expire_flag", "INTEGER", "admissions"),
                ColumnMetadata("dod", "TIMESTAMP", "patients"),
            )),
            api_key="key",
            model="provider/model",
        )

        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": []})
        ).analyze(hospital_request)

        self.assertFalse(analysis.ambiguous)

    def test_year_finding_retains_birth_and_admission_with_death_columns(self) -> None:
        year_request = SemanticColumnRequest(
            user_query="Show me patients from the year 2112.",
            schema=SchemaMetadata(
                table_names=("patients", "admissions"),
                columns=(
                    ColumnMetadata("dob", "TIMESTAMP", "patients"),
                    ColumnMetadata("dod", "TIMESTAMP", "patients"),
                    ColumnMetadata("dod_hosp", "TIMESTAMP", "patients"),
                    ColumnMetadata("admittime", "TIMESTAMP", "admissions"),
                ),
            ),
            api_key="key",
            model="provider/model",
        )
        finding = {
            "term": "from the year 2112",
            "dimension": "temporal_role",
            "resolved_by_context": False,
            "interpretations": [
                {
                    "label": "Born in 2112",
                    "meaning": "The patient's birth year is 2112.",
                    "relevance": 1,
                    "tables": ["patients"],
                    "columns": ["patients.dob"],
                    "operations": ["filter"],
                    "grain": "patient",
                    "temporal_role": "birth",
                },
                {
                    "label": "Admitted in 2112",
                    "meaning": "The hospital admission year is 2112.",
                    "relevance": 2,
                    "tables": ["admissions"],
                    "columns": ["admissions.admittime"],
                    "operations": ["filter"],
                    "grain": "patient",
                    "temporal_role": "hospital_admission",
                },
            ],
        }

        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": [finding]})
        ).analyze(year_request)

        self.assertTrue(analysis.ambiguous)
        self.assertEqual("temporal_role", analysis.terms[0].dimension)
        self.assertEqual(
            ("Born in 2112", "Admitted in 2112"),
            tuple(item.label for item in analysis.terms[0].interpretations),
        )
        self.assertNotIn(
            "patients.dod",
            tuple(
                column
                for item in analysis.terms[0].interpretations
                for column in item.grounding.columns
            ),
        )

    def test_explicit_scope_requests_have_no_actionable_finding(self) -> None:
        queries = (
            "Show hospital mortality rate by first ICU care unit for ICU "
            "stays with an admission.",
            "Count every diagnosis record for each diagnosis code.",
            "Count distinct patients for each diagnosis code.",
            "How long was each ICU stay for patient 10006?",
            "Show patients admitted to the hospital in the year 2112.",
        )
        for query in queries:
            with self.subTest(query=query):
                client = FakeClient({"findings": []})
                analysis = SemanticColumnAmbiguityService(
                    client=client
                ).analyze(
                    SemanticColumnRequest(
                        user_query=query,
                        schema=common_schema(),
                        api_key="key",
                        model="provider/model",
                    )
                )
                self.assertFalse(analysis.ambiguous)
                self.assertIn(query, client.prompts[0])

    def test_invalid_grounding_and_vocabulary_fail_closed(self) -> None:
        mutations = {
            "unknown dimension": ("dimension", "presentation_style"),
            "unknown operation": (
                "interpretations.0.operations",
                ["medianish"],
            ),
            "unknown table": (
                "interpretations.0.tables",
                ["missing_table"],
            ),
            "unknown column": (
                "interpretations.0.columns",
                ["diagnoses_icd.missing_column"],
            ),
        }
        for label, (path, value) in mutations.items():
            with self.subTest(label=label):
                finding = common_finding()
                if path == "dimension":
                    finding[path] = value
                else:
                    _, index, field = path.split(".")
                    finding["interpretations"][int(index)][field] = value
                analysis = SemanticColumnAmbiguityService(
                    client=FakeClient({"findings": [finding]})
                ).analyze(common_request())
                self.assertFalse(analysis.ambiguous)

    def test_duplicate_relevance_and_duplicate_grounding_fail_closed(self) -> None:
        for duplicate in ("relevance", "grounding"):
            with self.subTest(duplicate=duplicate):
                finding = common_finding()
                if duplicate == "relevance":
                    finding["interpretations"][1]["relevance"] = 1
                else:
                    finding["interpretations"][1].update(
                        finding["interpretations"][0]
                    )
                    finding["interpretations"][1]["label"] = "Same grounding"
                    finding["interpretations"][1]["meaning"] = "Same meaning"
                    finding["interpretations"][1]["relevance"] = 2
                analysis = SemanticColumnAmbiguityService(
                    client=FakeClient({"findings": [finding]})
                ).analyze(common_request())
                self.assertFalse(analysis.ambiguous)

    def test_fewer_than_two_interpretations_is_not_ambiguous(self) -> None:
        finding = common_finding()
        finding["interpretations"] = finding["interpretations"][:1]

        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": [finding]})
        ).analyze(common_request())

        self.assertFalse(analysis.ambiguous)

    def test_resolved_by_context_finding_is_dropped(self) -> None:
        finding = common_finding()
        finding["resolved_by_context"] = True

        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": [finding]})
        ).analyze(common_request())

        self.assertFalse(analysis.ambiguous)

    def test_malformed_response_is_failure(self) -> None:
        analysis = SemanticColumnAmbiguityService(
            client=FakeClient({"wrong": []})
        ).analyze(request())
        self.assertEqual(ComponentState.FAILED, analysis.state)

    def test_deterministic_fallback_uses_strongest_term(self) -> None:
        client = FakeClient({"findings": [common_finding()]})
        service = SemanticColumnAmbiguityService(client=client)
        decision = service.fallback_decision(service.analyze(common_request()))
        self.assertEqual("semantic-column", decision.mechanism)
        self.assertEqual(2, len(decision.options))
        self.assertEqual("Diagnosis record count", decision.options[0])
        self.assertEqual("aggregation_grain", decision.evidence_dimension)
        self.assertEqual(
            ("interpretation_1", "interpretation_2"),
            decision.evidence_interpretations,
        )

    def test_fallback_prefers_aggregation_over_column_presentation(self) -> None:
        presentation = SemanticAmbiguityTerm(
            term="diagnosis title",
            dimension="column_meaning",
            interpretations=(
                SemanticInterpretation(
                    "interpretation_1",
                    "Long title",
                    "Use the long diagnosis title.",
                    SemanticGrounding(
                        tables=("d_icd_diagnoses",),
                        columns=("d_icd_diagnoses.long_title",),
                        operations=("select",),
                    ),
                    1,
                ),
                SemanticInterpretation(
                    "interpretation_2",
                    "Short title",
                    "Use the short diagnosis title.",
                    SemanticGrounding(
                        tables=("d_icd_diagnoses",),
                        columns=("d_icd_diagnoses.short_title",),
                        operations=("select",),
                    ),
                    2,
                ),
            ),
        )
        aggregation = SemanticColumnAmbiguityService(
            client=FakeClient({"findings": [common_finding()]})
        ).analyze(common_request()).terms[0]

        decision = SemanticColumnAmbiguityService.fallback_decision(
            SemanticColumnAnalysis(
                state=ComponentState.ACCEPTED,
                terms=(presentation, aggregation),
            )
        )

        self.assertEqual("aggregation_grain", decision.evidence_dimension)
        self.assertIn("Diagnosis record count", decision.options)

    def test_fallback_exposes_grounding_columns(self) -> None:
        first = SemanticInterpretation(
            interpretation_id="interpretation_1",
            label="Birth year",
            meaning="Filter by the patient's birth year.",
            relevance=1,
            grounding=SemanticGrounding(
                tables=("patients",),
                columns=("patients.dob",),
                operations=("filter",),
                temporal_role="birth",
            ),
        )
        second = SemanticInterpretation(
            interpretation_id="interpretation_2",
            label="Admission year",
            meaning="Filter by hospital admission year.",
            relevance=2,
            grounding=SemanticGrounding(
                tables=("admissions",),
                columns=("admissions.admittime",),
                operations=("filter",),
                temporal_role="admission",
            ),
        )
        analysis = SemanticColumnAnalysis(
            state=ComponentState.ACCEPTED,
            terms=(
                SemanticAmbiguityTerm(
                    term="from 2024",
                    dimension="temporal_role",
                    interpretations=(first, second),
                ),
            ),
        )
        pairs = (
            ExecutedQueryPair(
                candidate_id="candidate_1",
                sql='SELECT * FROM "patients" WHERE YEAR("dob") = 2024',
                columns=("dob",),
                rows=(),
            ),
            ExecutedQueryPair(
                candidate_id="candidate_2",
                sql='SELECT "dob" FROM "patients"',
                columns=("dob",),
                rows=(),
            ),
        )

        decision = SemanticColumnAmbiguityService.fallback_decision(
            analysis,
            pairs=pairs,
        )

        self.assertEqual(
            ("patients.dob", "admissions.admittime"),
            decision.evidence_columns,
        )


if __name__ == "__main__":
    unittest.main()
