"""Rules-based and LLM-based database schema linking for RAG."""

from __future__ import annotations

import re
from typing import Any

from db_whisperer.contracts import SchemaMetadata
from db_whisperer.schema_graph import SchemaGraph

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
    ) -> set[str]:
        """Determine the set of relevant and connected tables for the prompt."""
        core_tables = set()

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

        # 4. Find join paths connecting core tables in the schema graph
        allowed_tables = set(core_tables)
        try:
            graph = SchemaGraph.from_schema(schema)
            # Find the shortest join path between every pair of identified tables
            core_list = list(core_tables)
            for i in range(len(core_list)):
                for j in range(i + 1, len(core_list)):
                    t1, t2 = core_list[i], core_list[j]
                    if graph.has_table(t1) and graph.has_table(t2):
                        enum = graph.enumerate_join_paths(t1, t2)
                        if enum.paths:
                            # The paths are sorted by length, so index 0 is the shortest join path
                            shortest_path = enum.paths[0]
                            allowed_tables.update(shortest_path.tables)
        except Exception:
            # Fall back to returning the disconnected core tables if graph connection fails
            pass

        return allowed_tables
