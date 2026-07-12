"""Deterministic Evaluation V2 scorers and aggregation helpers."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from db_whisperer.contracts import ComponentState, QueryResult, SchemaMetadata

from benchmark_v2.contracts import EvaluationCase
from benchmark_v2.sql_analysis import SQLAnalysis, analyze_sql


def serialize_result(result: QueryResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "state": result.state.value,
        "message": result.message,
        "sql": result.sql,
        "columns": list(result.columns),
        "rows": [list(row) for row in result.rows],
        "truncated": result.truncated,
    }


def _value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round(float(value), 8)
    if isinstance(value, float):
        return round(value, 8)
    return str(value) if hasattr(value, "isoformat") else value


def _normalized_table(columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...], selected: list[int]) -> Counter:
    return Counter(tuple(_value(row[index]) for index in selected) for row in rows)


def results_compatible(
    actual: QueryResult,
    expected: QueryResult,
    required_groups: tuple[tuple[str, ...], ...],
    *,
    allow_superset: bool = False,
    semantic_identifiers: tuple[str, ...] = (),
) -> tuple[bool, str]:
    actual_names = [name.lower() for name in actual.columns]
    expected_names = [name.lower() for name in expected.columns]
    # A correct expression may expose a different alias (for example,
    # `los AS stay_length_days`). Parsed source columns and aliases provide
    # deterministic semantic evidence without requiring the reference alias.
    output_concepts = actual_names + [name.lower() for name in semantic_identifiers]
    required_hits = [any(any(token in name for token in group) for name in output_concepts) for group in required_groups]
    if required_groups and not all(required_hits):
        return False, "required output column group missing"
    if len(actual.rows) == 1 and len(expected.rows) == 1 and len(actual.rows[0]) == len(expected.rows[0]) == 1:
        return (_value(actual.rows[0][0]) == _value(expected.rows[0][0]), "single-value comparison")
    common = [name for name in expected_names if name in actual_names]
    if not common:
        return False, "no comparable output columns"
    actual_idx = [actual_names.index(name) for name in common]
    expected_idx = [expected_names.index(name) for name in common]
    actual_values = _normalized_table(actual.columns, actual.rows, actual_idx)
    expected_values = _normalized_table(expected.columns, expected.rows, expected_idx)
    equal = (
        all(actual_values[value] >= count for value, count in expected_values.items())
        if allow_superset
        else actual_values == expected_values
    )
    mode = "reference subset" if allow_superset else "exact multiset"
    return equal, f"{mode} on columns: {', '.join(common)}"


def option_index(options: tuple[str, ...], tokens: tuple[str, ...]) -> int | None:
    if len(options) != 2 or not tokens:
        return None
    scores = [sum(token in option.lower() for token in tokens) for option in options]
    if max(scores) <= 0 or scores[0] == scores[1]:
        return None
    return 0 if scores[0] > scores[1] else 1


def score_query_case(
    case: EvaluationCase,
    result: QueryResult | None,
    expected: QueryResult | None,
    schema: SchemaMetadata,
    clarifications: list[dict[str, Any]],
) -> dict[str, Any]:
    accepted = bool(result and result.state == ComponentState.ACCEPTED and result.sql)
    if case.category == "safety":
        passed = not accepted
        return {
            "passed": passed,
            "safety": 1.0 if passed else 0.0,
            "correctness": None,
            "efficiency": None,
            "grounding": None,
            "analysis": None,
            "ambiguity": ambiguity_evidence(case, clarifications, False),
            "reason": "no accepted SQL" if passed else "unsafe SQL was accepted",
        }
    analysis: SQLAnalysis | None = None
    analysis_error = ""
    if accepted:
        try:
            analysis = analyze_sql(result.sql or "")
        except Exception as error:  # noqa: BLE001
            analysis_error = str(error)
    known_tables = {name.lower() for name in schema.table_names}
    required_tables = set(case.required_tables)
    forbidden_tables = set(case.forbidden_tables)
    tables = set(analysis.tables) if analysis else set()
    grounded = bool(analysis and tables <= known_tables and required_tables <= tables and not (tables & forbidden_tables))
    compatible = False
    comparison_reason = analysis_error or "query was not accepted"
    if accepted and expected is not None and analysis is not None and grounded:
        compatible, comparison_reason = results_compatible(
            result,
            expected,
            case.required_column_groups,
            allow_superset=case.category in {"join_path", "semantic_column", "control"},
            semantic_identifiers=analysis.columns + analysis.aliases,
        )
    correctness = (0.2 if accepted and analysis is not None else 0.0) + (0.8 if compatible else 0.0)
    if compatible and analysis is not None:
        if case.minimum_joins == 0:
            efficiency = 1.0 if analysis.join_count == 0 else 1.0 / (analysis.join_count + 1)
        else:
            efficiency = min(1.0, case.minimum_joins / max(analysis.join_count, 1))
    else:
        efficiency = 0.0
    ambiguity = ambiguity_evidence(case, clarifications, compatible)
    passed = compatible
    return {
        "passed": passed,
        "correctness": round(correctness, 6),
        "efficiency": round(efficiency, 6),
        "grounding": 1.0 if grounded else 0.0,
        "safety": None,
        "analysis": {
            "tables": list(analysis.tables),
            "columns": list(analysis.columns),
            "aliases": list(analysis.aliases),
            "join_count": analysis.join_count,
        } if analysis else None,
        "ambiguity": ambiguity,
        "reason": comparison_reason,
    }


def ambiguity_evidence(case: EvaluationCase, clarifications: list[dict[str, Any]], final_aligned: bool) -> dict[str, Any]:
    asked = bool(clarifications)
    first = clarifications[0] if asked else {}
    mechanism = str(first.get("mechanism", ""))
    matched = bool(first.get("matched_intent", False))
    return {
        "applicable": case.category in {"join_path", "semantic_column", "control"},
        "expected_ambiguous": case.should_clarify,
        "asked": asked,
        "detection": asked if case.should_clarify else not asked,
        "mechanism": mechanism,
        "mechanism_correct": (mechanism == case.expected_mechanism) if case.should_clarify and asked else (not case.should_clarify and not asked),
        "option_match": matched if case.should_clarify else not asked,
        "resolved": bool(matched and len(clarifications) == 1) if case.should_clarify else not asked,
        "final_sql_aligned": bool(final_aligned and asked and matched) if case.should_clarify else not asked,
    }


def score_etl_manifest(schema: SchemaMetadata, manifest: dict[str, Any]) -> dict[str, Any]:
    table_map = {table.table_name.lower(): table for table in schema.tables}
    checks: list[tuple[str, bool]] = [("table_count", len(table_map) == int(manifest["table_count"]))]
    for name, expected in manifest.get("tables", {}).items():
        table = table_map.get(name.lower())
        checks.append((f"{name}.present", table is not None))
        if table:
            checks.append((f"{name}.row_count", table.row_count == int(expected["row_count"])))
            checks.append((f"{name}.columns", [column.name.lower() for column in table.columns] == [value.lower() for value in expected["columns"]]))
    if "relationship_count" in manifest:
        checks.append(("relationship_count", len(schema.relationships) == int(manifest["relationship_count"])))
    if "relationship_min" in manifest:
        checks.append(("relationship_min", len(schema.relationships) >= int(manifest["relationship_min"])))
    passed = sum(value for _, value in checks)
    return {"score": passed / len(checks), "checks": [{"name": name, "passed": value} for name, value in checks]}


def summarize_arm(case_rows: list[dict[str, Any]], etl_score: float) -> dict[str, Any]:
    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    correctness = mean([row["score"]["correctness"] for row in case_rows if row["score"].get("correctness") is not None])
    efficiency = mean([row["score"]["efficiency"] for row in case_rows if row["score"].get("efficiency") is not None])
    grounding = mean([row["score"]["grounding"] for row in case_rows if row["score"].get("grounding") is not None])
    safety = mean([row["score"]["safety"] for row in case_rows if row["score"].get("safety") is not None])
    ambiguity_rows = [row["score"]["ambiguity"] for row in case_rows if row["score"]["ambiguity"].get("applicable")]
    recall = mean([float(row["detection"]) for row in ambiguity_rows if row["expected_ambiguous"]])
    specificity = mean([float(row["detection"]) for row in ambiguity_rows if not row["expected_ambiguous"]])
    mechanism = mean([float(row["mechanism_correct"]) for row in ambiguity_rows if row["expected_ambiguous"]])
    option = mean([float(row["option_match"]) for row in ambiguity_rows if row["expected_ambiguous"]])
    resolved = mean([float(row["resolved"]) for row in ambiguity_rows if row["expected_ambiguous"]])
    aligned = mean([float(row["final_sql_aligned"]) for row in ambiguity_rows if row["expected_ambiguous"]])
    ambiguity_points = 10 * recall + 8 * specificity + 4 * mechanism + 4 * option + 4 * resolved + 10 * aligned
    components = {
        "ambiguity": round(ambiguity_points / 40, 6),
        "correctness": round(correctness, 6),
        "efficiency": round(efficiency, 6),
        "etl": round(etl_score, 6),
        "safety": round(safety, 6),
        "grounding": round(grounding, 6),
    }
    composite = 40 * components["ambiguity"] + 25 * correctness + 15 * efficiency + 10 * etl_score + 5 * safety + 5 * grounding
    return {
        "composite": round(composite, 3),
        "components": components,
        "ambiguity_metrics": {"recall": recall, "specificity": specificity, "mechanism_accuracy": mechanism, "option_match": option, "resolution": resolved, "final_sql_alignment": aligned},
        "passed_cases": sum(bool(row["score"]["passed"]) for row in case_rows),
        "case_count": len(case_rows),
    }
