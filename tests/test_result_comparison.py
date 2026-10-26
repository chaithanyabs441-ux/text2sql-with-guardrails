"""Value-based result comparison behavior, including strict compatibility mode."""

from decimal import Decimal

import pandas as pd
import pytest

from t2s.verification.result_comparison import result_sets_equal


def test_allows_an_extra_generated_id_column():
    gold = pd.DataFrame({"name": ["Ada", "Lin"]})
    generated = pd.DataFrame({"id": [4, 8], "customer": ["Lin", "Ada"]})
    assert result_sets_equal(gold, generated)


def test_matches_reordered_columns_by_values():
    gold = pd.DataFrame({"name": ["Ada", "Lin"], "age": [31, 28]})
    generated = pd.DataFrame({"age_out": [28, 31], "name_out": ["Lin", "Ada"]})
    assert result_sets_equal(gold, generated)


def test_accepts_numeric_values_within_relative_tolerance():
    assert result_sets_equal(
        pd.DataFrame({"amount": [1_000_000.0]}),
        pd.DataFrame({"amount_out": [1_000_000.5]}),
    )


def test_rejects_numeric_values_outside_relative_tolerance():
    assert not result_sets_equal(
        pd.DataFrame({"amount": [1_000_000.0]}),
        pd.DataFrame({"amount_out": [1_000_002.0]}),
    )


def test_treats_null_values_as_equal():
    assert result_sets_equal(
        pd.DataFrame({"value": [None, float("nan")]}),
        pd.DataFrame({"result": [pd.NA, None]}),
    )


def test_rejects_different_values():
    assert not result_sets_equal(
        pd.DataFrame({"value": ["approved"]}),
        pd.DataFrame({"result": ["pending"]}),
    )


def test_rejects_a_missing_gold_column():
    gold = pd.DataFrame({"name": ["Ada"], "region": ["EU"]})
    generated = pd.DataFrame({"value": ["Ada"]})
    assert not result_sets_equal(gold, generated)


def test_rejects_a_row_count_mismatch():
    assert not result_sets_equal(
        pd.DataFrame({"value": [1, 2]}), pd.DataFrame({"result": [1]})
    )


def test_preserves_duplicate_rows_as_a_multiset():
    gold = pd.DataFrame({"value": [1, 1, 2]})
    generated = pd.DataFrame({"result": [2, 1, 1]})
    assert result_sets_equal(gold, generated)
    assert not result_sets_equal(gold, pd.DataFrame({"result": [2, 2, 1]}))


def test_normalizes_decimal_integer_and_float_values():
    gold = pd.DataFrame({"amount": [Decimal("12.50"), 7]})
    generated = pd.DataFrame({"amount_out": [7.0, 12.5]})
    assert result_sets_equal(gold, generated)


def test_strict_mode_preserves_the_old_equal_column_count_rule():
    gold = pd.DataFrame({"value": [1]})
    generated = pd.DataFrame({"id": [8], "value_out": [1]})
    assert not result_sets_equal(gold, generated, strict=True)


def test_strict_mode_still_ignores_labels_and_row_order():
    gold = pd.DataFrame({"left": [1, 2]})
    generated = pd.DataFrame({"right": [2, 1]})
    assert result_sets_equal(gold, generated, strict=True)


def test_rejects_boolean_as_a_numeric_match():
    assert not result_sets_equal(
        pd.DataFrame({"value": [True]}), pd.DataFrame({"result": [1]})
    )


def test_matches_duplicate_gold_columns_to_distinct_generated_columns():
    gold = pd.DataFrame({"first": ["x", "y"], "second": ["a", "b"]})
    generated = pd.DataFrame(
        {"decoy": ["y", "x"], "second_out": ["a", "b"], "first_out": ["x", "y"]}
    )
    assert result_sets_equal(gold, generated)


def test_does_not_reuse_one_generated_column_for_two_gold_columns():
    gold = pd.DataFrame({"first": ["x"], "second": ["x"]})
    generated = pd.DataFrame({"only": ["x"]})
    assert not result_sets_equal(gold, generated)


def test_row_relationships_must_match_after_column_mapping():
    gold = pd.DataFrame({"left": [1, 2], "right": [10, 20]})
    generated = pd.DataFrame({"a": [1, 2], "b": [20, 10]})
    assert not result_sets_equal(gold, generated)


def test_order_by_result_order_is_ignored_for_limit_ties():
    gold = pd.DataFrame({"score": [100, 100], "name": ["Ada", "Lin"]})
    generated = pd.DataFrame({"name_out": ["Lin", "Ada"], "score_out": [100, 100]})
    assert result_sets_equal(gold, generated)


def test_different_rows_at_a_limit_tie_boundary_are_not_assumed_equal():
    gold = pd.DataFrame({"score": [100, 100], "name": ["Ada", "Lin"]})
    generated = pd.DataFrame({"score_out": [100, 100], "name_out": ["Ada", "Mia"]})
    assert not result_sets_equal(gold, generated)


@pytest.mark.parametrize("left,right", [(1, 1.0), (Decimal("3.25"), 3.25), (0, -0.0)])
def test_exact_numeric_representations_match(left, right):
    assert result_sets_equal(pd.DataFrame({"x": [left]}), pd.DataFrame({"y": [right]}))
