"""Embedding-based table selection with foreign-key bridge expansion."""

from collections import deque
from typing import Any

import numpy as np

from t2s.schema.models import DatabaseSchema, TableModel


class SchemaRetriever:
    """Select relevant tables and include paths connecting selected tables."""

    def __init__(self, max_tables: int = 5, embedder: Any | None = None) -> None:
        if max_tables < 1:
            raise ValueError("max_tables must be at least 1")
        self.max_tables = max_tables
        self._embedder = embedder

    def filter_schema(
        self, question: str, schema: DatabaseSchema
    ) -> DatabaseSchema:
        """Return top-scoring tables plus FK tables needed as join bridges."""
        if not schema.tables:
            return DatabaseSchema(tables=[])

        documents = [self._describe_table(table) for table in schema.tables]
        embeddings = self._encode([question, *documents])
        question_embedding = embeddings[0]
        table_embeddings = embeddings[1:]
        norms = np.linalg.norm(table_embeddings, axis=1) * np.linalg.norm(
            question_embedding
        )
        scores = np.divide(
            table_embeddings @ question_embedding,
            norms,
            out=np.zeros(len(table_embeddings), dtype=float),
            where=norms != 0,
        )

        ranked_indices = np.argsort(-scores, kind="stable")
        selected_indices = list(ranked_indices[: self.max_tables])
        included_names = {schema.tables[index].name.lower() for index in selected_indices}

        graph = self._foreign_key_graph(schema.tables)
        selected_names = [schema.tables[index].name.lower() for index in selected_indices]
        bridge_names: set[str] = set()
        for left_index, left in enumerate(selected_names):
            for right in selected_names[left_index + 1 :]:
                path = self._shortest_path(graph, left, right)
                if path:
                    bridge_names.update(path[1:-1])

        included_names.update(bridge_names)
        selected_tables = [
            table for table in schema.tables if table.name.lower() in included_names
        ]
        return DatabaseSchema(tables=selected_tables)

    def _encode(self, texts: list[str]) -> np.ndarray:
        """Encode text using a lazily loaded sentence-transformer model."""
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer

            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        return np.asarray(
            self._embedder.encode(texts, convert_to_numpy=True), dtype=float
        )

    @staticmethod
    def _describe_table(table: TableModel) -> str:
        """Build the text embedded for one table."""
        columns = " ".join(
            f"{column.name} {column.data_type} {column.description or ''}"
            for column in table.columns
        )
        foreign_keys = " ".join(
            f"references {foreign_key.referred_table}"
            for foreign_key in table.foreign_keys
        )
        return f"{table.name} {table.description or ''} {columns} {foreign_keys}"

    @staticmethod
    def _foreign_key_graph(tables: list[TableModel]) -> dict[str, set[str]]:
        """Build an undirected graph from declared foreign-key relations."""
        known_names = {table.name.lower() for table in tables}
        graph = {name: set() for name in known_names}
        for table in tables:
            source = table.name.lower()
            for foreign_key in table.foreign_keys:
                target = foreign_key.referred_table.lower()
                if target in known_names:
                    graph[source].add(target)
                    graph[target].add(source)
        return graph

    @staticmethod
    def _shortest_path(
        graph: dict[str, set[str]], start: str, target: str
    ) -> list[str] | None:
        """Return a shortest FK path, if the selected tables are connected."""
        pending = deque([(start, [start])])
        visited = {start}
        while pending:
            current, path = pending.popleft()
            if current == target:
                return path
            for neighbor in sorted(graph.get(current, set())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append((neighbor, [*path, neighbor]))
        return None
