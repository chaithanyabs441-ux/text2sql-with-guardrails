"""Tests for embedding table selection and foreign-key bridge expansion."""

import numpy as np

from t2s.schema.models import (
    ColumnModel,
    DatabaseSchema,
    ForeignKeyModel,
    TableModel,
)
from t2s.schema.retriever import SchemaRetriever


class FixedEmbedder:
    """Return deterministic vectors without loading a sentence model."""

    def encode(self, texts, convert_to_numpy=True):
        vectors = []
        for text in texts:
            if text == "question":
                vectors.append([1.0, 0.0])
            elif text.startswith("orders"):
                vectors.append([0.9, 0.1])
            elif text.startswith("regions"):
                vectors.append([0.8, 0.2])
            elif text.startswith("customers"):
                vectors.append([0.1, 0.9])
            else:
                raise AssertionError(f"Unexpected embed text: {text}")
        return np.asarray(vectors)


def make_schema() -> DatabaseSchema:
    """Build a small FK graph where customers bridges orders and regions."""
    id_column = [ColumnModel(name="id", data_type="integer")]
    return DatabaseSchema(
        tables=[
            TableModel(
                name="orders",
                columns=id_column,
                foreign_keys=[
                    ForeignKeyModel(
                        constrained_columns=["customer_id"],
                        referred_table="customers",
                        referred_columns=["id"],
                    )
                ],
            ),
            TableModel(
                name="regions",
                columns=id_column,
            ),
            TableModel(
                name="customers",
                columns=id_column,
                foreign_keys=[
                    ForeignKeyModel(
                        constrained_columns=["region_id"],
                        referred_table="regions",
                        referred_columns=["id"],
                    )
                ],
            ),
        ]
    )


def test_embedding_retrieval_adds_foreign_key_bridge_table():
    retriever = SchemaRetriever(max_tables=2, embedder=FixedEmbedder())

    filtered = retriever.filter_schema("question", make_schema())

    assert {table.name for table in filtered.tables} == {
        "orders",
        "regions",
        "customers",
    }


def test_retrieval_returns_empty_schema_without_loading_embedder():
    retriever = SchemaRetriever()

    filtered = retriever.filter_schema("question", DatabaseSchema(tables=[]))

    assert filtered.tables == []
    assert retriever._embedder is None
