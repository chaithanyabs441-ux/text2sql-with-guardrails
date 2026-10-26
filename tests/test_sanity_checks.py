"""Tests for result sanity warnings."""

import pandas as pd
import pytest

from t2s.verification.sanity_checks import ResultSanityChecker


@pytest.fixture
def checker():
    return ResultSanityChecker()


def test_execution_error_returns_one_warning(checker):
    warnings = checker.check(pd.DataFrame(), "permission denied")

    assert warnings == ["Database execution error: permission denied"]


def test_empty_result_warns(checker):
    warnings = checker.check(pd.DataFrame())

    assert warnings == ["Query returned zero rows. Verify filters or joins."]


def test_mostly_negative_numeric_column_warns(checker):
    warnings = checker.check(pd.DataFrame({"amount": [-4, -3, -2, 1]}))

    assert "Column 'amount' contains mostly negative values." in warnings


def test_exactly_half_negative_values_do_not_warn(checker):
    warnings = checker.check(pd.DataFrame({"amount": [-1, 1]}))

    assert not any("mostly negative" in warning for warning in warnings)


def test_large_result_warns(checker):
    warnings = checker.check(pd.DataFrame({"id": range(1001)}))

    assert "Result set exceeds 1000 rows. Check if a LIMIT should be applied." in warnings


def test_high_null_ratio_warns(checker):
    warnings = checker.check(pd.DataFrame({"value": [None, None, 3]}))

    assert any("Column 'value' is >50% NULL." in warning for warning in warnings)


def test_normal_result_has_no_warnings(checker):
    warnings = checker.check(pd.DataFrame({"id": [1, 2], "name": ["A", "B"]}))

    assert warnings == []
