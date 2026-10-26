"""Shared test configuration that avoids requiring provider credentials."""

import os

import pytest

os.environ.setdefault("GENERATOR_API_KEY", "test-generator-key")
os.environ.setdefault("JUDGE_API_KEY", "test-judge-key")


@pytest.fixture
def sql_policy(tmp_path):
    """Create a policy engine whose audit log stays inside pytest's temp dir."""
    from t2s.guardrails.policy import SQLPolicyEngine

    return SQLPolicyEngine(audit_log_path=tmp_path / "guardrails.jsonl")
