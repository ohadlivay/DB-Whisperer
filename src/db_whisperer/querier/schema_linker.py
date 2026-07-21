"""Rules-based and LLM-based database schema linking for RAG."""

from __future__ import annotations

import re
from typing import Any

from db_whisperer.contracts import SchemaMetadata
from db_whisperer.querier.relationship_connectivity import (
    shortest_table_connection,
)

GENERIC_COLUMNS = {
    "id",
    "name",
    "date",
    "status",
    "type",
    "created_at",
    "updated_at",
    "value",
    "total",
    "count",
}

SCHEMA_LINKER_INSTRUCTIONS = """You identify which database tables are relevant to a user question.
Given a list of database tables and their columns, and the user's question, identify which tables are relevant to answer the question.
Return exactly one JSON object with this shape:
{"tables": ["<exact table name 1>", "<exact table name 2>"]}
Return an empty list when no listed table is clearly relevant.
Use table names exactly as they appear in the TABLES section.
Do not invent tables or columns.
Do not return SQL, Markdown, explanations, or any other keys."""


_CLARIFICATION_COLUMNS = re.compile(
    r'\(clarifying which column:\s*"([^"]+)"\s+or\s+"([^"]+)"\)',
    re.IGNORECASE,
)


def clarification_required_tables(
    clarifications: tuple[str, ...],
    schema: SchemaMetadata,
) -> set[str]:
    """Return schema tables grounded by semantic clarification evidence."""
    known_columns: dict[str, str] = {}
    for table in schema.tables:
        for column in table.columns:
            known_columns[f"{table.table_name}.{column.name}"] = (
                table.table_name
            )
    for column in schema.columns:
        if column.table_name:
            known_columns[f"{column.table_name}.{column.name}"] = (
                column.table_name
            )

    required: set[str] = set()
    for clarification in clarifications:
        for match in _CLARIFICATION_COLUMNS.finditer(clarification):
            for qualified_name in match.groups():
                table_name = known_columns.get(qualified_name)
                if table_name is not None:
                    required.add(table_name)
    return required


class SchemaLinker:
    """Identify which database tables are relevant to a user query."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client

    def match_tokens(self, user_prompt: str, schema: SchemaMetadata) -> set[str]:
        """Scan the query text for matches of table or non-generic column names."""
        tokens = set(re.findall(r"\b[a-zA-Z0-9_]+\b", user_prompt.lower()))

        # Simple plural-to-singular normalization for English nouns
        singulars = set()
        for token in tokens:
            if token.endswith("s") and len(token) > 1:
                singulars.add(token[:-1])
            if token.endswith("es") and len(token) > 2:
                singulars.add(token[:-2])
        tokens.update(singulars)

        matched_tables = set()

        for table in schema.tables:
            table_name = table.table_name.lower()
            normalized_table_words = set(re.findall(r"\b[a-zA-Z0-9_]+\b", table_name))

            # Match table name directly
            if table_name in tokens or (
                normalized_table_words and normalized_table_words.issubset(tokens)
            ):
                matched_tables.add(table.table_name)
                continue

            # Match column names (ignoring generic ones)
            for col in table.columns:
                col_name = col.name.lower()
                if col_name in GENERIC_COLUMNS:
                    continue
                normalized_col_words = set(re.findall(r"\b[a-zA-Z0-9_]+\b", col_name))
                if col_name in tokens or (
                    normalized_col_words and normalized_col_words.issubset(tokens)
                ):
                    matched_tables.add(table.table_name)
                    break

        return matched_tables

    def build_linker_prompt(self, user_prompt: str, schema: SchemaMetadata) -> str:
        """Construct the prompt showing all tables and columns in a compact list."""
        lines = []
        for table in schema.tables:
            cols = ", ".join(col.name for col in table.columns)
            lines.append(f"- Table: {table.table_name}\n  Columns: {cols}")
        tables_list = "\n".join(lines)
        return (
            f"{SCHEMA_LINKER_INSTRUCTIONS}\n\n"
            f"=== TABLES ===\n"
            f"{tables_list}\n"
            f"=== END TABLES ===\n\n"
            f"=== USER QUESTION ===\n"
            f"{user_prompt}\n"
            f"=== END USER QUESTION ==="
        )

    def link_schema(
        self,
        user_prompt: str,
        schema: SchemaMetadata,
        api_key: str,
        model: str,
        required_tables: set[str] | None = None,
    ) -> set[str]:
        """Determine the set of relevant and connected tables for the prompt."""
        known_tables = set(schema.table_names)
        core_tables = {
            table
            for table in (required_tables or set())
            if table in known_tables
        }

        # 1. Rule-based token matching (always run as an fast-match / fallback)
        try:
            core_tables.update(self.match_tokens(user_prompt, schema))
        except Exception:
            pass

        # 2. LLM-based schema ranking (if client is configured)
        if self.client is not None and api_key.strip() and model.strip():
            try:
                prompt = self.build_linker_prompt(user_prompt, schema)
                response = self.client.generate_json(
                    prompt=prompt,
                    api_key=api_key,
                    model=model,
                    metadata={"component": "schema_linker"},
                )
                llm_tables = response.get("tables", [])
                if isinstance(llm_tables, list):
                    for table in llm_tables:
                        if isinstance(table, str) and table in schema.table_names:
                            core_tables.add(table)
            except Exception:
                # Catch failures (e.g. connection/parsing issues) and rely on fallback matches
                pass

        # 3. If no core tables are identified, return all tables (fail-safe fallback)
        if not core_tables:
            return set(schema.table_names)

        # 4. Add bridge tables from one deterministic shortest connection.
        # This supports SQL generation only; it does not enumerate or compare
        # alternate routes and therefore cannot create ambiguity decisions.
        allowed_tables = set(core_tables)
        core_list = sorted(core_tables)
        for index, source in enumerate(core_list):
            for target in core_list[index + 1:]:
                allowed_tables.update(
                    shortest_table_connection(schema, source, target)
                )

        return allowed_tables
