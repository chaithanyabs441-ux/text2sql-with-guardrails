"""Thread-safe in-memory tracking for LLM tokens, latency, and estimated cost."""

from threading import Lock
from typing import Any


class CostTracker:
    """Aggregate LLM usage and estimate cost when model prices are configured.

    Prices are specified in USD per one million prompt and completion tokens.
    No default prices are assumed because provider pricing changes over time.
    """

    def __init__(
        self,
        pricing_per_million: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._pricing_per_million = dict(pricing_per_million or {})
        self._lock = Lock()
        self._totals = self._new_aggregate()
        self._by_model: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _new_aggregate() -> dict[str, Any]:
        """Return an empty aggregate counter."""
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_latency_sec": 0.0,
            "estimated_cost_usd": 0.0,
            "priced_calls": 0,
        }

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_sec: float,
    ) -> dict[str, Any]:
        """Record one provider response and return its usage details."""
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("Token counts cannot be negative")
        if latency_sec < 0:
            raise ValueError("Latency cannot be negative")

        rates = self._pricing_per_million.get(model)
        estimated_cost = None
        if rates is not None:
            prompt_rate, completion_rate = rates
            if prompt_rate < 0 or completion_rate < 0:
                raise ValueError("Token prices cannot be negative")
            estimated_cost = (
                prompt_tokens * prompt_rate + completion_tokens * completion_rate
            ) / 1_000_000

        record = {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "latency_sec": round(latency_sec, 4),
            "estimated_cost_usd": (
                round(estimated_cost, 10) if estimated_cost is not None else None
            ),
        }
        with self._lock:
            self._add(self._totals, record)
            model_totals = self._by_model.setdefault(model, self._new_aggregate())
            self._add(model_totals, record)
        return record

    def set_model_pricing(
        self, model: str, prompt_per_million: float, completion_per_million: float
    ) -> None:
        """Configure token prices in USD per million tokens for one model."""
        if prompt_per_million < 0 or completion_per_million < 0:
            raise ValueError("Token prices cannot be negative")
        with self._lock:
            self._pricing_per_million[model] = (
                prompt_per_million,
                completion_per_million,
            )

    @staticmethod
    def _add(aggregate: dict[str, Any], record: dict[str, Any]) -> None:
        """Add one usage record to aggregate counters."""
        aggregate["calls"] += 1
        aggregate["prompt_tokens"] += record["prompt_tokens"]
        aggregate["completion_tokens"] += record["completion_tokens"]
        aggregate["total_latency_sec"] += record["latency_sec"]
        if record["estimated_cost_usd"] is not None:
            aggregate["estimated_cost_usd"] += record["estimated_cost_usd"]
            aggregate["priced_calls"] += 1

    @staticmethod
    def _format_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
        """Return a public aggregate without internal bookkeeping fields."""
        calls = aggregate["calls"]
        priced_calls = aggregate["priced_calls"]
        return {
            "calls": calls,
            "prompt_tokens": aggregate["prompt_tokens"],
            "completion_tokens": aggregate["completion_tokens"],
            "total_tokens": (
                aggregate["prompt_tokens"] + aggregate["completion_tokens"]
            ),
            "total_latency_sec": round(aggregate["total_latency_sec"], 4),
            "average_latency_sec": (
                round(aggregate["total_latency_sec"] / calls, 4) if calls else 0.0
            ),
            "estimated_cost_usd": (
                round(aggregate["estimated_cost_usd"], 10)
                if priced_calls == calls and calls
                else None
            ),
            "priced_calls": priced_calls,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return thread-safe aggregate totals, including per-model usage."""
        with self._lock:
            return {
                **self._format_aggregate(self._totals),
                "by_model": {
                    model: self._format_aggregate(aggregate)
                    for model, aggregate in self._by_model.items()
                },
            }


cost_tracker = CostTracker()
