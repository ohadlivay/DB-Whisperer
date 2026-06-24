"""Tests for schema-graph assembly and join-path enumeration."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db_whisperer.contracts import (
    ColumnMetadata,
    JoinPath,
    Relationship,
    SchemaMetadata,
    TableSchema,
)
from db_whisperer.schema_graph import (
    SchemaGraph,
    describe_join_path,
    entity_table_pairs,
)


def _rel(child_table, child_column, parent_table, parent_column) -> Relationship:
    return Relationship(
        child_table=child_table,
        child_column=child_column,
        parent_table=parent_table,
        parent_column=parent_column,
    )


# The canonical PDF example, reduced to its three tables.
MIMIC_TABLES = ("patients", "admissions", "labevents")
MIMIC_RELATIONSHIPS = (
    _rel("admissions", "subject_id", "patients", "subject_id"),
    _rel("labevents", "subject_id", "patients", "subject_id"),
    _rel("labevents", "hadm_id", "admissions", "hadm_id"),
)


class SchemaGraphTest(unittest.TestCase):
    def test_assembles_undirected_edges_from_relationships(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        self.assertEqual(MIMIC_TABLES, graph.tables)
        self.assertEqual(3, len(graph.edges))
        # patients is reachable from both admissions and labevents.
        neighbours = {name for name, _ in graph.neighbors("patients")}
        self.assertEqual({"admissions", "labevents"}, neighbours)

    def test_skips_edges_to_unknown_tables(self) -> None:
        graph = SchemaGraph(
            ("orders",),
            (_rel("orders", "customer_id", "customers", "customer_id"),),
        )

        self.assertEqual((), graph.edges)
        self.assertEqual((), graph.neighbors("orders"))

    def test_enumerates_two_paths_for_the_labs_example(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        enumeration = graph.enumerate_join_paths("patients", "labevents")

        self.assertTrue(enumeration.is_ambiguous)
        self.assertEqual(2, len(enumeration.paths))
        direct, via_visit = enumeration.paths
        # Shortest first: the direct subject_id link.
        self.assertEqual(("patients", "labevents"), direct.tables)
        self.assertEqual((), direct.intermediate_tables)
        # Then the longer path through the hospital visit.
        self.assertEqual(
            ("patients", "admissions", "labevents"),
            via_visit.tables,
        )
        self.assertEqual(("admissions",), via_visit.intermediate_tables)

    def test_single_edge_is_unambiguous(self) -> None:
        graph = SchemaGraph(
            ("orders", "customers"),
            (_rel("orders", "customer_id", "customers", "customer_id"),),
        )

        enumeration = graph.enumerate_join_paths("orders", "customers")

        self.assertFalse(enumeration.is_ambiguous)
        self.assertEqual(1, len(enumeration.paths))

    def test_disconnected_tables_have_no_path(self) -> None:
        graph = SchemaGraph(
            ("a", "b", "c", "d"),
            (_rel("b", "a_id", "a", "id"), _rel("d", "c_id", "c", "id")),
        )

        enumeration = graph.enumerate_join_paths("a", "c")

        self.assertEqual((), enumeration.paths)
        self.assertFalse(enumeration.is_ambiguous)

    def test_parallel_edges_are_distinct_paths(self) -> None:
        graph = SchemaGraph(
            ("messages", "users"),
            (
                _rel("messages", "sender_id", "users", "user_id"),
                _rel("messages", "recipient_id", "users", "user_id"),
            ),
        )

        enumeration = graph.enumerate_join_paths("messages", "users")

        self.assertTrue(enumeration.is_ambiguous)
        self.assertEqual(2, len(enumeration.paths))
        used_columns = {
            path.relationships[0].child_column for path in enumeration.paths
        }
        self.assertEqual({"sender_id", "recipient_id"}, used_columns)

    def test_self_reference_does_not_break_enumeration(self) -> None:
        graph = SchemaGraph(
            ("staffs", "stores"),
            (
                _rel("staffs", "manager_id", "staffs", "staff_id"),
                _rel("staffs", "store_id", "stores", "store_id"),
            ),
        )

        enumeration = graph.enumerate_join_paths("staffs", "stores")

        self.assertEqual(1, len(enumeration.paths))
        self.assertEqual(("staffs", "stores"), enumeration.paths[0].tables)

    def test_max_hops_excludes_longer_paths(self) -> None:
        graph = SchemaGraph(
            ("a", "b", "c", "d"),
            (
                _rel("b", "a_id", "a", "id"),
                _rel("c", "b_id", "b", "id"),
                _rel("d", "c_id", "c", "id"),
            ),
        )

        self.assertEqual(
            (),
            graph.enumerate_join_paths("a", "d", max_hops=2).paths,
        )
        reachable = graph.enumerate_join_paths("a", "d", max_hops=3)
        self.assertEqual(1, len(reachable.paths))
        self.assertEqual(("a", "b", "c", "d"), reachable.paths[0].tables)

    def test_max_paths_marks_truncation(self) -> None:
        # Three parallel edges produce three paths; a cap of two keeps the
        # enumeration ambiguous (>=2 paths) while flagging the cut-off.
        graph = SchemaGraph(
            ("messages", "users"),
            (
                _rel("messages", "sender_id", "users", "user_id"),
                _rel("messages", "recipient_id", "users", "user_id"),
                _rel("messages", "editor_id", "users", "user_id"),
            ),
            max_paths=2,
        )

        enumeration = graph.enumerate_join_paths("messages", "users")

        self.assertEqual(2, len(enumeration.paths))
        self.assertTrue(enumeration.truncated)
        self.assertTrue(enumeration.is_ambiguous)

    def test_has_ambiguous_pair_detects_multi_path_schemas(self) -> None:
        self.assertTrue(
            SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS).has_ambiguous_pair()
        )
        tree = SchemaGraph(
            ("orders", "customers"),
            (_rel("orders", "customer_id", "customers", "customer_id"),),
        )
        self.assertFalse(tree.has_ambiguous_pair())

    def test_describe_join_path_handles_zero_relationship_path(self) -> None:
        # A single-table path carries no join conditions and renders no [...].
        self.assertEqual(
            "solo",
            describe_join_path(JoinPath(tables=("solo",), relationships=())),
        )

    def test_source_equals_target_is_empty(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        self.assertEqual(
            (),
            graph.enumerate_join_paths("patients", "patients").paths,
        )

    def test_unknown_endpoint_is_empty(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        self.assertEqual(
            (),
            graph.enumerate_join_paths("patients", "ghost").paths,
        )

    def test_enumeration_is_deterministic(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        first = graph.enumerate_join_paths("patients", "labevents")
        second = graph.enumerate_join_paths("patients", "labevents")

        self.assertEqual(
            [path.tables for path in first.paths],
            [path.tables for path in second.paths],
        )

    def test_from_schema_uses_metadata(self) -> None:
        schema = SchemaMetadata(
            table_names=MIMIC_TABLES,
            relationships=MIMIC_RELATIONSHIPS,
            tables=(
                TableSchema(
                    table_name="patients",
                    columns=(ColumnMetadata("subject_id", "INTEGER"),),
                    row_count=1,
                ),
            ),
        )

        graph = SchemaGraph.from_schema(schema)

        self.assertEqual(MIMIC_TABLES, graph.tables)
        self.assertEqual(3, len(graph.edges))

    def test_adjacency_summary_lists_neighbours(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)

        summary = dict(graph.adjacency_summary())

        self.assertEqual({"admissions", "labevents"}, set(summary["patients"]))
        self.assertEqual({"patients", "admissions"}, set(summary["labevents"]))

    def test_describe_join_path_renders_chain_and_keys(self) -> None:
        graph = SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS)
        via_visit = graph.enumerate_join_paths("patients", "labevents").paths[1]

        description = describe_join_path(via_visit)

        self.assertIn("patients -> admissions -> labevents", description)
        self.assertIn("admissions.subject_id = patients.subject_id", description)
        self.assertIn("labevents.hadm_id = admissions.hadm_id", description)

    def test_entity_table_pairs_are_unique_and_ordered(self) -> None:
        self.assertEqual(
            (("a", "b"), ("a", "c"), ("b", "c")),
            entity_table_pairs(("a", "b", "c", "a")),
        )

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS, max_hops=0)
        with self.assertRaises(ValueError):
            SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS, max_paths=0)
        # A cap below two cannot represent ambiguity, so it is rejected too.
        with self.assertRaises(ValueError):
            SchemaGraph(MIMIC_TABLES, MIMIC_RELATIONSHIPS, max_paths=1)

    def test_dedupe_ignores_advisory_relationship_metadata(self) -> None:
        # Two FK records with the same join keys but different advisory scores
        # describe the same join and must collapse to a single path.
        graph = SchemaGraph(
            ("orders", "customers"),
            (
                Relationship(
                    "orders", "customer_id", "customers", "customer_id",
                    score=1.0,
                ),
                Relationship(
                    "orders", "customer_id", "customers", "customer_id",
                    score=0.5,
                ),
            ),
        )

        enumeration = graph.enumerate_join_paths("orders", "customers")

        self.assertEqual(1, len(enumeration.paths))
        self.assertFalse(enumeration.is_ambiguous)

    def test_duplicate_relationships_do_not_consume_the_path_cap(self) -> None:
        # A duplicate advisory FK (same join keys, different score) must not
        # fill the cap and hide a genuinely distinct path behind a truncation.
        graph = SchemaGraph(
            ("a", "b", "c"),
            (
                Relationship("b", "a_id", "a", "id", score=1.0),
                Relationship("b", "a_id", "a", "id", score=0.5),
                Relationship("c", "a_id", "a", "id"),
                Relationship("b", "c_id", "c", "id"),
            ),
            max_paths=2,
        )

        enumeration = graph.enumerate_join_paths("a", "b")

        # The direct a-b path and the a-c-b path both survive; the duplicate
        # does not crowd one out, so the pair is correctly ambiguous.
        self.assertEqual(2, len(enumeration.paths))
        self.assertTrue(enumeration.is_ambiguous)
        self.assertFalse(enumeration.truncated)


if __name__ == "__main__":
    unittest.main()
