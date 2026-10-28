"""API tests that stub database and provider-dependent components."""

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from t2s.api import main
from t2s.auth.store import AuthStore
from t2s.generation.generator import SQLGenerationResponse
from t2s.schema.models import DatabaseSchema
from t2s.verification.agreement import AgreementResult, CandidateOutcome

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def authenticated_api(monkeypatch):
    store = AuthStore(
        engine=create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    store.ensure_schema()
    admin = store.create_user("test-admin", "test-admin-password-123", "admin")
    store.grant_dataset(admin["id"], "core", admin["id"])
    monkeypatch.setattr(main, "auth_store", store)
    monkeypatch.setattr(
        main.agreement,
        "evaluate",
        lambda question, strategy="schema_glossary", sql_authorizer=None: AgreementResult(
            candidate_count=3,
            executed_count=3,
            dominant_cluster_size=3,
            agreement_ratio=1.0,
            candidates=(),
        ),
    )
    client.cookies.clear()
    response = client.post(
        "/auth/login",
        json={"username": "test-admin", "password": "test-admin-password-123"},
    )
    assert response.status_code == 200
    yield store
    client.cookies.clear()


def test_health_endpoint_reports_ready():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_calibrated_confidence_loads_model_and_validation_thresholds(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "features": [
                    "back_translation_similarity",
                    "agreement_ratio",
                    "sanity_warning_count",
                    "self_reported_confidence",
                    "schema_coverage",
                ],
                "model": {"intercept": [0.0], "coefficients": [[0, 0, 0, 0, 0]]},
                "abstention_thresholds": {
                    "answer_threshold": 0.8,
                    "warn_threshold": 0.4,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CALIBRATION_REPORT", report_path)
    signals = {
        "back_translation_similarity": 0.9,
        "agreement_ratio": 1.0,
        "sanity_warning_count": 0.0,
        "self_reported_confidence": 0.9,
        "schema_coverage": 1.0,
    }

    score, thresholds, ready = main._calibrated_confidence(signals)

    assert score == 0.5
    assert thresholds.answer_threshold == 0.8
    assert thresholds.warn_threshold == 0.4
    assert ready is True


def test_schema_endpoint_returns_generator_schema(monkeypatch):
    schema = DatabaseSchema(tables=[])
    monkeypatch.setattr(main.generator, "get_schema", lambda: schema)

    response = client.get("/v1/schema")

    assert response.status_code == 200
    assert response.json() == {"tables": []}


def test_history_endpoint_returns_empty_list_when_no_queries_are_recorded():
    response = client.get("/v1/history")

    assert response.status_code == 200
    assert response.json() == []


def test_query_response_is_recorded_in_history(monkeypatch):
    # This API behavior test covers the no-report fallback regardless of whether
    # a developer has generated a local calibration artifact.
    monkeypatch.setattr(
        main,
        "CALIBRATION_REPORT",
        main.CALIBRATION_REPORT.with_name("missing-report.json"),
    )
    generated = SQLGenerationResponse(
        sql="SELECT id FROM customers",
        explanation="Select customer identifiers.",
        tables_used=["customers"],
        self_reported_confidence=0.9,
        answerable=True,
        ambiguity_flag=False,
    )
    monkeypatch.setattr(
        main.generator,
        "generate",
        lambda question, strategy, database_error=None, **kwargs: (generated, generated.sql),
    )
    monkeypatch.setattr(
        main.sandbox,
        "execute",
        lambda sql: (pd.DataFrame({"id": [1]}), 1.0, None),
    )
    monkeypatch.setattr(main.detector, "back_translate", lambda sql: "List customer ids")
    monkeypatch.setattr(main.detector, "calculate_similarity", lambda question, translated: 0.9)

    response = client.post("/v1/query", json={"question": "List customer ids"})
    history = client.get("/v1/history")

    assert response.status_code == 200
    assert response.json()["data"] == [{"id": 1}]
    assert response.json()["decision"] == "answer"
    assert response.json()["confidence_signals"]["agreement_ratio"] == 1.0
    assert response.json()["calibration_ready"] is False
    assert history.json()[0]["question"] == "List customer ids"
    assert "id FROM customers" in history.json()[0]["sql"]


def test_low_confidence_abstains_and_returns_only_safe_alternatives(monkeypatch):
    generated = SQLGenerationResponse(
        sql="SELECT id FROM customers",
        explanation="Select customer identifiers.",
        tables_used=["customers"],
        self_reported_confidence=0.1,
        answerable=True,
        ambiguity_flag=False,
    )
    monkeypatch.setattr(
        main.generator,
        "generate",
        lambda question, strategy, database_error=None, **kwargs: (generated, generated.sql),
    )
    monkeypatch.setattr(
        main.sandbox,
        "execute",
        lambda sql: (pd.DataFrame({"id": [1]}), 1.0, None),
    )
    monkeypatch.setattr(main.detector, "back_translate", lambda sql: "List customers")
    monkeypatch.setattr(main.detector, "calculate_similarity", lambda question, translated: 0.2)
    monkeypatch.setattr(
        main.agreement,
        "evaluate",
        lambda question, strategy, sql_authorizer=None: AgreementResult(
            candidate_count=3,
            executed_count=1,
            dominant_cluster_size=1,
            agreement_ratio=1 / 3,
            candidates=(
                CandidateOutcome(
                    temperature=0.0,
                    status="executed",
                    safe_sql="SELECT id FROM orders",
                    cluster_id=1,
                ),
            ),
        ),
    )

    response = client.post("/v1/query", json={"question": "List customers"})

    assert response.status_code == 200
    assert response.json()["decision"] == "abstain_with_alternatives"
    assert response.json()["sql"] == ""
    assert response.json()["data"] == []
    assert response.json()["alternatives"] == ["SELECT id FROM orders"]


def test_query_preserves_unanswerable_client_error(monkeypatch):
    response_data = SQLGenerationResponse(
        sql="",
        explanation="The requested data is unavailable.",
        tables_used=[],
        self_reported_confidence=0.1,
        answerable=False,
        ambiguity_flag=False,
    )
    monkeypatch.setattr(
        main.generator,
        "generate",
        lambda question, strategy, database_error=None, **kwargs: (response_data, ""),
    )

    response = client.post("/v1/query", json={"question": "unknown data"})

    assert response.status_code == 400
    assert "unanswerable" in response.json()["detail"]


def test_query_returns_service_unavailable_for_missing_provider_key(monkeypatch):
    def missing_key(question, strategy, database_error=None, **kwargs):
        raise RuntimeError("GENERATOR_API_KEY is not configured.")

    monkeypatch.setattr(main.generator, "generate", missing_key)

    response = client.post("/v1/query", json={"question": "List customers"})

    assert response.status_code == 503
    assert "GENERATOR_API_KEY" in response.json()["detail"]


def test_query_hides_unexpected_internal_error(monkeypatch):
    def unexpected_error(question, strategy, database_error=None, **kwargs):
        raise ValueError("internal database detail")

    monkeypatch.setattr(main.generator, "generate", unexpected_error)

    response = client.post("/v1/query", json={"question": "List customers"})

    assert response.status_code == 500
    assert response.json()["detail"] == "The query could not be processed."
    assert "internal database detail" not in response.text


def test_selfcorrect_retries_database_error_at_most_twice(monkeypatch):
    generated = SQLGenerationResponse(
        sql="SELECT id FROM customers",
        explanation="Select customer identifiers.",
        tables_used=["customers"],
        self_reported_confidence=0.9,
        answerable=True,
        ambiguity_flag=False,
    )
    errors = []

    def generate(question, strategy, database_error=None, **kwargs):
        errors.append(database_error)
        return generated, generated.sql

    monkeypatch.setattr(main.generator, "generate", generate)
    monkeypatch.setattr(
        main.sandbox,
        "execute",
        lambda sql: (pd.DataFrame(), 1.0, "column does not exist"),
    )
    monkeypatch.setattr(main.detector, "back_translate", lambda sql: "List customer ids")
    monkeypatch.setattr(main.detector, "calculate_similarity", lambda question, translated: 0.9)

    def evaluate_candidates(question, strategy="schema_glossary", sql_authorizer=None):
        for temperature in (0.0, 0.3, 0.7):
            main.generator.generate(
                question,
                strategy=strategy,
                database_error=None,
                temperature=temperature,
            )
        return AgreementResult(
            candidate_count=3,
            executed_count=3,
            dominant_cluster_size=3,
            agreement_ratio=1.0,
            candidates=(),
        )

    monkeypatch.setattr(main.agreement, "evaluate", evaluate_candidates)

    response = client.post(
        "/v1/query",
        json={
            "question": "List customer ids",
            "strategy": "schema_glossary_selfcorrect",
        },
    )

    assert response.status_code == 200
    assert len(errors) == 6
    assert errors[:3] == [None, "column does not exist", "column does not exist"]
    assert errors[3:] == [None, None, None]
    assert any("Database execution error" in warning for warning in response.json()["warnings"])
