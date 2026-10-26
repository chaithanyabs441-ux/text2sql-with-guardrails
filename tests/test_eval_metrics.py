"""Exact binomial confidence interval tests for evaluation reports."""

import pytest
from eval.metrics import clopper_pearson_interval


@pytest.mark.parametrize(
    ("successes", "trials", "expected_lower", "expected_upper"),
    [
        (5, 10, 0.1870860284, 0.8129139716),
        (0, 10, 0.0, 0.3084971078),
        (10, 10, 0.6915028922, 1.0),
    ],
)
def test_clopper_pearson_matches_exact_reference_values(
    successes, trials, expected_lower, expected_upper
):
    lower, upper = clopper_pearson_interval(successes, trials)

    assert lower == pytest.approx(expected_lower, abs=1e-9)
    assert upper == pytest.approx(expected_upper, abs=1e-9)


def test_clopper_pearson_returns_no_interval_without_trials():
    assert clopper_pearson_interval(0, 0) == (None, None)


@pytest.mark.parametrize(
    ("successes", "trials", "confidence"),
    [(-1, 5, 0.95), (6, 5, 0.95), (0, -1, 0.95), (0, 5, 1.0)],
)
def test_clopper_pearson_rejects_invalid_inputs(successes, trials, confidence):
    with pytest.raises(ValueError):
        clopper_pearson_interval(successes, trials, confidence)
