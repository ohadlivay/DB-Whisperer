from pathlib import Path
import tempfile
import unittest

from benchmark_v3.aggregate_results import aggregate
from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import ARMS, DEFAULT_SUITE, build_services, choose_option
from benchmark_v3.scoring import score_case
from db_whisperer.contracts import ComponentState, QueryResult


class EvaluationV3Test(unittest.TestCase):
    def test_suite_has_four_active_arms_and_no_join_path_mechanism(self) -> None:
        suite = load_suite(DEFAULT_SUITE)
        self.assertEqual(
            ("baseline", "candidate_only", "semantic_only", "full"),
            ARMS,
        )
        self.assertEqual("3", suite.version.split(".")[0])
        self.assertNotIn("join_path", {case.category for case in suite.cases})
        self.assertNotIn("join-path", {case.expected_mechanism for case in suite.cases})

    def test_arm_configuration_is_independent_and_meaningful(self) -> None:
        _, applications = build_services(2)
        candidate = applications["candidate_only"]
        semantic = applications["semantic_only"]
        full = applications["full"]
        self.assertFalse(candidate.enable_semantic_column_detection)
        self.assertFalse(candidate.ambiguity.prompt_builder.include_schema_context)
        self.assertTrue(semantic.enable_semantic_column_detection)
        self.assertFalse(semantic.ambiguity.prompt_builder.include_relationships)
        self.assertFalse(semantic.ambiguity.prompt_builder.include_candidate_evidence)
        self.assertTrue(full.ambiguity.prompt_builder.include_relationships)
        self.assertTrue(full.ambiguity.prompt_builder.include_candidate_evidence)

    def test_option_selection_is_deterministic(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        option, matched = choose_option(case, ("irrelevant", " ".join(case.option_tokens)))
        self.assertTrue(matched)
        self.assertEqual(" ".join(case.option_tokens), option)

    def test_scoring_requires_expected_clarification_behavior(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        result = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            columns=("x",),
            rows=((1,),),
        )
        score = score_case(case, result, result, [])
        self.assertFalse(score["passed"])

    def test_scoring_requires_clarification_compliance(self) -> None:
        case = load_suite(DEFAULT_SUITE).query_cases[0]
        result = QueryResult(
            state=ComponentState.ACCEPTED,
            message="ok",
            columns=("x",),
            rows=((1,),),
        )
        score = score_case(case, result, result, [{
            "matched_intent": True,
            "mechanism": case.expected_mechanism,
            "compliance_passed": False,
        }])

        self.assertFalse(score["passed"])
        self.assertFalse(
            score["clarification"]["applied_to_final_sql"]
        )


if __name__ == "__main__":
    unittest.main()
