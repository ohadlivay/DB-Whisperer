"""Tests for schema-graph join-path ambiguity detection."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.ambiguity import JoinPathAmbiguityService
from db_whisperer.ambiguity.openrouter_client import AmbiguityJudgeError
from db_whisperer.contracts import (
    ComponentState,
    JoinPathRequest,
    Relationship,
    SchemaMetadata,
)


def _rel(child_table, child_column, parent_table, parent_column) -> Relationship:
    return Relationship(
        child_table=child_table,
        child_column=child_column,
        parent_table=parent_table,
        parent_column=parent_column,
    )


MIMIC_SCHEMA = SchemaMetadata(
    database_path="mimic.duckdb",
    table_names=("patients", "admissions", "labevents"),
    relationships=(
        _rel("admissions", "subject_id", "patients", "subject_id"),
        _rel("labevents", "subject_id", "patients", "subject_id"),
        _rel("labevents", "hadm_id", "admissions", "hadm_id"),
    ),
)

SINGLE_PATH_SCHEMA = SchemaMetadata(
    database_path="shop.duckdb",
    table_names=("orders", "customers"),
    relationships=(_rel("orders", "customer_id", "customers", "customer_id"),),
)

NO_GRAPH_SCHEMA = SchemaMetadata(
    database_path="flat.duckdb",
    table_names=("events",),
    relationships=(),
)

# Has an ambiguous pair (patients<->labevents) so the pre-check lets the LLM
# run, but a question about (patients, drugs) maps to a single-path pair.
MIXED_SCHEMA = SchemaMetadata(
    database_path="mixed.duckdb",
    table_names=("patients", "admissions", "labevents", "drugs"),
    relationships=(
        _rel("admissions", "subject_id", "patients", "subject_id"),
        _rel("labevents", "subject_id", "patients", "subject_id"),
        _rel("labevents", "hadm_id", "admissions", "hadm_id"),
        _rel("drugs", "patient_id", "patients", "subject_id"),
    ),
)

# Two parallel foreign keys directly connect the same pair of tables.
TWO_PARALLEL_SCHEMA = SchemaMetadata(
    database_path="msg.duckdb",
    table_names=("messages", "users"),
    relationships=(
        _rel("messages", "sender_id", "users", "user_id"),
        _rel("messages", "recipient_id", "users", "user_id"),
    ),
)

# Three parallel foreign keys -> three direct paths between messages and users.
THREE_PARALLEL_SCHEMA = SchemaMetadata(
    database_path="msg3.duckdb",
    table_names=("messages", "users"),
    relationships=(
        _rel("messages", "sender_id", "users", "user_id"),
        _rel("messages", "recipient_id", "users", "user_id"),
        _rel("messages", "editor_id", "users", "user_id"),
    ),
)

# Two disconnected clusters: (p,q) has two paths, (r,s) has three. Used to
# exercise most-ambiguous-pair selection and the "other pairs" report.
TWO_CLUSTER_SCHEMA = SchemaMetadata(
    database_path="clusters.duckdb",
    table_names=("p", "q", "m1", "r", "s", "n1", "n2"),
    relationships=(
        _rel("q", "p_id", "p", "id"),
        _rel("m1", "p_id", "p", "id"),
        _rel("q", "m1_id", "m1", "id"),
        _rel("s", "r_id", "r", "id"),
        _rel("n1", "r_id", "r", "id"),
        _rel("s", "n1_id", "n1", "id"),
        _rel("n2", "r_id", "r", "id"),
        _rel("s", "n2_id", "n2", "id"),
    ),
)


class FakeJoinPathClient:
    """Return queued responses (dicts) or raise queued exceptions."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def evaluate(self, prompt: str, api_key: str, model: str) -> dict:
        self.prompts.append(prompt)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def _request(schema: SchemaMetadata, query: str = "labs for patient 123") -> JoinPathRequest:
    return JoinPathRequest(
        user_query=query,
        schema=schema,
        api_key="key",
        model="provider/model",
    )


class JoinPathAmbiguityServiceTest(unittest.TestCase):
    def test_flags_join_path_ambiguity_with_two_options(self) -> None:
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "labs", "table": "labevents"},
                ]
            },
            {
                "question": "Do you want labs from a specific visit, "
                "or all visits?",
                "options": ["A specific visit", "All visits"],
                "reason": "Two join paths.",
            },
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.passed)
        # The model's question is preserved; both tables are named so the next
        # round can recognise this pair as settled.
        self.assertTrue(
            decision.question.startswith(
                "Do you want labs from a specific visit, or all visits?"
            )
        )
        self.assertIn("patients", decision.question)
        self.assertIn("labevents", decision.question)
        self.assertEqual(
            ("A specific visit", "All visits"),
            decision.options,
        )
        self.assertEqual("join-path", decision.mechanism)
        self.assertEqual(2, len(client.prompts))

    def test_precheck_skips_llm_for_tree_shaped_schema(self) -> None:
        # No table pair is multi-path, so the pre-check must skip both LLM
        # calls entirely (the fake client would raise if called).
        client = FakeJoinPathClient()

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(SINGLE_PATH_SCHEMA, "orders for each customer")
        )

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertTrue(decision.passed)
        self.assertEqual([], client.prompts)
        self.assertIn("more than one join path", decision.reason)

    def test_passes_when_extracted_entities_share_one_path(self) -> None:
        # The graph has ambiguity elsewhere, so the LLM runs, but the mentioned
        # entities (patients, drugs) are connected by a single path.
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "drugs", "table": "drugs"},
                ]
            }
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIXED_SCHEMA, "drugs for patient 123")
        )

        self.assertTrue(decision.passed)
        self.assertEqual(1, len(client.prompts))
        self.assertIn("At most one join path", decision.reason)

    def test_passes_with_fewer_than_two_entity_tables(self) -> None:
        client = FakeJoinPathClient(
            {"entities": [{"mention": "patient", "table": "patients"}]}
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertTrue(decision.passed)
        self.assertEqual(1, len(client.prompts))

    def test_drops_unknown_tables_before_counting_entities(self) -> None:
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "ghost", "table": "nonexistent_table"},
                ]
            }
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        # Only one known table remains, so there is no join-path ambiguity,
        # and the dropped hallucinated table is reported, not silently ignored.
        self.assertTrue(decision.passed)
        self.assertEqual(1, len(client.prompts))
        self.assertIn("nonexistent_table", decision.reason)

    def test_skips_llm_without_a_graph(self) -> None:
        client = FakeJoinPathClient()

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(NO_GRAPH_SCHEMA)
        )

        self.assertTrue(decision.passed)
        self.assertEqual([], client.prompts)

    def test_entity_extraction_failure_is_reported(self) -> None:
        client = FakeJoinPathClient(AmbiguityJudgeError("boom"))

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("Entity extraction failed", decision.reason)

    def test_malformed_entity_response_is_reported(self) -> None:
        client = FakeJoinPathClient({"not_entities": []})

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("no usable entities", decision.reason)

    def test_falls_back_to_deterministic_question_when_clarify_fails(
        self,
    ) -> None:
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "labs", "table": "labevents"},
                ]
            },
            AmbiguityJudgeError("clarify model down"),
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertEqual(ComponentState.ACCEPTED, decision.state)
        self.assertFalse(decision.passed)
        self.assertEqual(
            ("Linked directly on labevents.subject_id",
             "Linked through admissions"),
            decision.options,
        )
        self.assertIn("deterministic", decision.reason)

    def test_falls_back_when_clarify_returns_one_option(self) -> None:
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "labs", "table": "labevents"},
                ]
            },
            {"question": "Which one?", "options": ["only one"]},
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA)
        )

        self.assertFalse(decision.passed)
        self.assertEqual(2, len(decision.options))
        self.assertNotEqual(decision.options[0], decision.options[1])

    def test_truncated_enumeration_is_surfaced_in_reason(self) -> None:
        # Three parallel paths capped at two: ambiguous AND truncated.
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "messages", "table": "messages"},
                    {"mention": "users", "table": "users"},
                ]
            },
            {
                "question": "Which link?",
                "options": ["By sender", "By recipient"],
                "reason": "Multiple links.",
            },
        )

        decision = JoinPathAmbiguityService(client=client, max_paths=2).detect(
            _request(THREE_PARALLEL_SCHEMA, "messages and their users")
        )

        self.assertFalse(decision.passed)
        self.assertIn("hit its limit", decision.reason)
        self.assertEqual(2, len(client.prompts))

    def test_chooses_most_ambiguous_pair_and_reports_others(self) -> None:
        # (p,q) has two paths, (r,s) has three; the detector must clarify the
        # most ambiguous pair (r,s), report the other, and present 2 of 3.
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "p", "table": "p"},
                    {"mention": "q", "table": "q"},
                    {"mention": "r", "table": "r"},
                    {"mention": "s", "table": "s"},
                ]
            },
            AmbiguityJudgeError("force deterministic fallback"),
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(TWO_CLUSTER_SCHEMA, "connect p q r s")
        )

        self.assertFalse(decision.passed)
        self.assertIn("between 'r' and 's'", decision.reason)
        self.assertIn("Presented the two most distinct of 3 paths", decision.reason)
        self.assertIn(
            "1 other entity pair(s) are also ambiguous and will be clarified",
            decision.reason,
        )
        # Shortest (direct) and longest (through an intermediate) presented.
        self.assertEqual("Linked directly on s.r_id", decision.options[0])
        self.assertTrue(decision.options[1].startswith("Linked through"))

    def test_parallel_edge_fallback_options_are_meaningful(self) -> None:
        # Two parallel direct edges both have empty intermediates; the labels
        # must stay distinct and informative (join key), not "(option 1/2)".
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "messages", "table": "messages"},
                    {"mention": "users", "table": "users"},
                ]
            },
            AmbiguityJudgeError("clarify down"),
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(TWO_PARALLEL_SCHEMA, "messages and users")
        )

        self.assertFalse(decision.passed)
        self.assertEqual(2, len(decision.options))
        self.assertNotEqual(decision.options[0], decision.options[1])
        self.assertTrue(
            all(
                option.startswith("Linked directly on")
                for option in decision.options
            )
        )
        self.assertNotIn("(option", decision.options[0])

    def test_excludes_already_clarified_pair_on_later_round(self) -> None:
        entities = {
            "entities": [
                {"mention": "p", "table": "p"},
                {"mention": "q", "table": "q"},
                {"mention": "r", "table": "r"},
                {"mention": "s", "table": "s"},
            ]
        }
        # Round 1: clarify the most ambiguous pair (r, s).
        first = JoinPathAmbiguityService(
            client=FakeJoinPathClient(entities, AmbiguityJudgeError("fallback"))
        ).detect(_request(TWO_CLUSTER_SCHEMA, "connect p q r s"))
        self.assertIn("between 'r' and 's'", first.reason)
        answered = f"Question: {first.question}\nSelected answer: {first.options[0]}"

        # Round 2: (r, s) is settled by that answer, so the next pair (p, q) is
        # clarified instead of proceeding with unresolved ambiguity.
        second = JoinPathAmbiguityService(
            client=FakeJoinPathClient(entities, AmbiguityJudgeError("fallback"))
        ).detect(
            JoinPathRequest(
                user_query="connect p q r s",
                schema=TWO_CLUSTER_SCHEMA,
                api_key="key",
                model="provider/model",
                clarifications=(answered,),
            )
        )

        self.assertFalse(second.passed)
        self.assertIn("between 'p' and 'q'", second.reason)

    def test_passes_when_all_pairs_already_clarified(self) -> None:
        client = FakeJoinPathClient(
            {
                "entities": [
                    {"mention": "patient", "table": "patients"},
                    {"mention": "labs", "table": "labevents"},
                ]
            }
        )
        answered = (
            "Question: How should patients and labevents connect?\n"
            "Selected answer: directly"
        )

        decision = JoinPathAmbiguityService(client=client).detect(
            JoinPathRequest(
                user_query="labs for patient",
                schema=MIMIC_SCHEMA,
                api_key="key",
                model="provider/model",
                clarifications=(answered,),
            )
        )

        self.assertTrue(decision.passed)
        self.assertIn("already been clarified", decision.reason)
        # Entity extraction ran, but no clarification call (nothing unsettled).
        self.assertEqual(1, len(client.prompts))

    def test_validation_rejects_empty_query_without_llm(self) -> None:
        client = FakeJoinPathClient()

        decision = JoinPathAmbiguityService(client=client).detect(
            _request(MIMIC_SCHEMA, "   ")
        )

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertIn("User query is required", decision.reason)
        self.assertEqual([], client.prompts)

    def test_validation_rejects_missing_api_key(self) -> None:
        client = FakeJoinPathClient()
        request = JoinPathRequest(
            user_query="labs for patient",
            schema=MIMIC_SCHEMA,
            api_key="  ",
            model="provider/model",
        )

        decision = JoinPathAmbiguityService(client=client).detect(request)

        self.assertEqual(ComponentState.FAILED, decision.state)
        self.assertEqual([], client.prompts)


if __name__ == "__main__":
    unittest.main()
