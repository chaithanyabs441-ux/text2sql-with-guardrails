"""Tests for aggregate LLM usage and configurable cost estimates."""

import pytest

from t2s.llm.cost_tracker import CostTracker


def test_records_tokens_latency_and_unknown_cost_without_pricing():
    tracker = CostTracker()

    record = tracker.record("model-a", 10, 5, 0.25)
    totals = tracker.snapshot()

    assert record["total_tokens"] == 15
    assert record["estimated_cost_usd"] is None
    assert totals["calls"] == 1
    assert totals["prompt_tokens"] == 10
    assert totals["completion_tokens"] == 5
    assert totals["total_latency_sec"] == 0.25
    assert totals["estimated_cost_usd"] is None


def test_calculates_cost_from_configured_per_million_prices():
    tracker = CostTracker({"model-a": (2.0, 4.0)})

    record = tracker.record("model-a", 1000, 500, 0.5)

    assert record["estimated_cost_usd"] == 0.004
    assert tracker.snapshot()["estimated_cost_usd"] == 0.004


def test_can_configure_model_pricing_after_initialization():
    tracker = CostTracker()
    tracker.set_model_pricing("model-a", 1.0, 2.0)

    record = tracker.record("model-a", 1000, 1000, 1.0)

    assert record["estimated_cost_usd"] == 0.003


@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens", "latency_sec"),
    [(-1, 0, 0.0), (0, -1, 0.0), (0, 0, -0.1)],
)
def test_rejects_negative_usage(prompt_tokens, completion_tokens, latency_sec):
    tracker = CostTracker()

    with pytest.raises(ValueError):
        tracker.record("model-a", prompt_tokens, completion_tokens, latency_sec)
