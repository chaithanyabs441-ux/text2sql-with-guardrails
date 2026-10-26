"""Tests for selectable SQL prompt strategies."""

import pytest

from t2s.generation.prompt_builder import PromptBuilder
from t2s.schema.models import ColumnModel, DatabaseSchema, TableModel


@pytest.fixture
def schema():
    return DatabaseSchema(
        tables=[
            TableModel(
                name="orders",
                columns=[ColumnModel(name="net_revenue", data_type="numeric")],
            )
        ]
    )


def test_zero_shot_uses_schema_without_glossary_or_examples(schema, tmp_path):
    builder = PromptBuilder(glossary_path=str(tmp_path / "missing.yaml"))

    prompt = builder.build_generation_prompt("Revenue?", schema, strategy="zero_shot")

    assert "TABLE orders" in prompt
    assert "BUSINESS GLOSSARY" not in prompt
    assert "FEW-SHOT EXAMPLES" not in prompt


def test_schema_glossary_includes_definitions_and_few_shot_examples(schema):
    builder = PromptBuilder()

    prompt = builder.build_generation_prompt("Net revenue?", schema)

    assert "BUSINESS GLOSSARY" in prompt
    assert "net_revenue" in prompt
    assert "FEW-SHOT EXAMPLES" in prompt


def test_region_breakdown_example_preserves_regions_with_zero_matches(schema):
    builder = PromptBuilder()

    prompt = builder.build_generation_prompt(
        "How many active enterprise customers are in each region?", schema
    )

    assert "FROM regions AS r" in prompt
    assert "LEFT JOIN customers AS c" in prompt
    assert "c.is_deleted = FALSE" in prompt
    assert "put filters on the counted table in the JOIN condition" in prompt


def test_selfcorrect_prompt_includes_database_error_as_untrusted_data(schema):
    builder = PromptBuilder()

    prompt = builder.build_generation_prompt(
        "Net revenue?",
        schema,
        strategy="schema_glossary_selfcorrect",
        database_error="column typo does not exist",
    )

    assert "PREVIOUS DATABASE ERROR" in prompt
    assert "untrusted diagnostic" in prompt
    assert "column typo does not exist" in prompt


def test_unknown_prompt_strategy_is_rejected(schema):
    builder = PromptBuilder()

    with pytest.raises(ValueError, match="Unknown prompt strategy"):
        builder.build_generation_prompt("Question", schema, strategy="unknown")
