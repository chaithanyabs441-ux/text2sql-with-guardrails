"""Unit tests for JSON parsing, repair, and cache behavior in the LLM adapter."""

import json
from types import SimpleNamespace

import pytest
from openai import BadRequestError
from pydantic import BaseModel

import t2s.llm.openai_adapter as adapter_module
from t2s.llm.cost_tracker import CostTracker
from t2s.llm.exceptions import ProviderRefusalError
from t2s.llm.openai_adapter import OpenAICompatibleAdapter


class SQLResponse(BaseModel):
    """Small response schema used to exercise structured output validation."""

    sql: str
    explanation: str


class StubCompletions:
    """Return prepared response content while recording provider request data."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = iter(contents)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        content = next(self.contents)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
        )


def make_stub_client(contents: list[str]) -> SimpleNamespace:
    """Build the subset of the OpenAI client used by the adapter."""
    completions = StubCompletions(contents)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        completions=completions,
    )
    return client


def test_generate_json_parses_and_caches_provider_response(monkeypatch):
    payload = {"sql": "SELECT id FROM customers", "explanation": "Select ids."}
    client = make_stub_client([json.dumps(payload)])
    writes = []
    tracker = CostTracker({"mock": (1.0, 2.0)})
    monkeypatch.setattr(adapter_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(adapter_module, "read_cache", lambda key: None)
    monkeypatch.setattr(adapter_module, "cost_tracker", tracker)
    monkeypatch.setattr(
        adapter_module,
        "write_cache",
        lambda key, value: writes.append((key, value)),
    )

    adapter = OpenAICompatibleAdapter("test-key", "https://example.test/v1", "mock")
    result = adapter.generate_json("List customers", SQLResponse)

    assert result == SQLResponse(**payload)
    assert len(writes) == 1
    assert writes[0][1] == payload
    assert adapter.get_usage()["prompt_tokens"] == 12
    assert adapter.get_usage()["estimated_cost_usd"] == 0.000028
    assert tracker.snapshot()["total_tokens"] == 20
    assert client.completions.requests[0]["response_format"] == {"type": "json_object"}


def test_generate_json_repairs_invalid_response(monkeypatch):
    payload = {"sql": "SELECT id FROM customers", "explanation": "Select ids."}
    client = make_stub_client(["not json", json.dumps(payload)])
    monkeypatch.setattr(adapter_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(adapter_module, "read_cache", lambda key: None)
    monkeypatch.setattr(adapter_module, "write_cache", lambda key, value: None)

    adapter = OpenAICompatibleAdapter("test-key", "https://example.test/v1", "mock")
    result = adapter.generate_json("List customers", SQLResponse, max_retries=2)

    assert result.sql == payload["sql"]
    assert len(client.completions.requests) == 2
    assert "Original Request:" in client.completions.requests[1]["messages"][0]["content"]


def test_generate_json_uses_validated_cache_without_provider_call(monkeypatch):
    payload = {"sql": "SELECT id FROM customers", "explanation": "Select ids."}
    client = make_stub_client([])
    monkeypatch.setattr(adapter_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(adapter_module, "read_cache", lambda key: payload)
    monkeypatch.setattr(
        adapter_module,
        "write_cache",
        lambda key, value: (_ for _ in ()).throw(AssertionError("unexpected cache write")),
    )

    adapter = OpenAICompatibleAdapter("test-key", "https://example.test/v1", "mock")
    result = adapter.generate_json("List customers", SQLResponse)

    assert result == SQLResponse(**payload)
    assert client.completions.requests == []


def test_generate_json_maps_provider_safety_refusal(monkeypatch):
    refusal = BadRequestError.__new__(BadRequestError)
    Exception.__init__(refusal, "Failed to generate JSON.")
    refusal.body = {
            "error": {
                "message": "Failed to generate JSON.",
                "failed_generation": "I'm sorry, but I can't comply with that.",
            }
    }

    class RefusingCompletions:
        def create(self, **kwargs):
            raise refusal

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=RefusingCompletions())
    )
    monkeypatch.setattr(adapter_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(adapter_module, "read_cache", lambda key: None)

    adapter = OpenAICompatibleAdapter("test-key", "https://example.test/v1", "mock")

    with pytest.raises(ProviderRefusalError, match="refused this request"):
        adapter.generate_json("Do something destructive", SQLResponse)


def test_generate_json_maps_json_wrapped_provider_refusal_without_retry(monkeypatch):
    refusal = {"error": "I’m sorry, but I can’t help with that."}
    client = make_stub_client([json.dumps(refusal)])
    monkeypatch.setattr(adapter_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(adapter_module, "read_cache", lambda key: None)

    adapter = OpenAICompatibleAdapter("test-key", "https://example.test/v1", "mock")

    with pytest.raises(ProviderRefusalError, match="refused this request"):
        adapter.generate_json("Do something destructive", SQLResponse)

    assert len(client.completions.requests) == 1
