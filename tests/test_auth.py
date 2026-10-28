"""Authentication, authorization, and dataset-boundary regression tests."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from t2s.api import main
from t2s.auth.store import AuthStore, datasets, sessions
from t2s.generation.generator import SQLGenerationResponse


@pytest.fixture
def auth_client(monkeypatch):
    store = AuthStore(
        engine=create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    store.ensure_schema()
    admin = store.create_user("root-admin", "test-admin-password-123", "admin")
    store.grant_dataset(admin["id"], "core", admin["id"])
    monkeypatch.setattr(main, "auth_store", store)
    with TestClient(main.app) as client:
        yield client, store, admin


def login(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_unauthenticated_requests_cannot_read_or_query(auth_client):
    client, _, _ = auth_client
    assert client.post("/v1/query", json={"question": "count customers"}).status_code == 401
    assert client.get("/v1/schema").status_code == 401
    assert client.get("/v1/history").status_code == 401
    assert client.get("/admin/users").status_code == 401


def test_login_rate_limit_and_audit_events(auth_client):
    client, store, _ = auth_client
    for _ in range(5):
        assert login(client, "missing-user", "wrong-password").status_code == 401
    assert login(client, "missing-user", "wrong-password").status_code == 429
    event_types = {event["event_type"] for event in store.list_audit_events()}
    assert {"login_failed", "login_rate_limited"} <= event_types


def test_login_cookie_is_http_only_and_database_stores_only_token_hash(auth_client):
    client, store, admin = auth_client
    response = login(client, "root-admin", "test-admin-password-123")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    raw_token = client.cookies.get(main.SESSION_COOKIE)
    with store.engine.connect() as connection:
        token_hashes = connection.execute(select(sessions.c.token_hash)).scalars().all()
    assert raw_token not in token_hashes
    assert store.user_for_session(raw_token)["id"] == admin["id"]


def test_employee_can_query_granted_core_dataset_but_not_other_datasets(auth_client, monkeypatch):
    client, store, admin = auth_client
    employee = store.create_user("employee-one", "employee-password-123", "employee")
    store.grant_dataset(employee["id"], "core", admin["id"])
    sql = "SELECT id FROM customers LIMIT 5"
    generated = SQLGenerationResponse(
        sql=sql,
        explanation="Count customers.",
        tables_used=["customers"],
        self_reported_confidence=0.99,
        answerable=True,
        ambiguity_flag=False,
    )
    monkeypatch.setattr(main.generator, "generate", lambda *args, **kwargs: (generated, sql))
    monkeypatch.setattr(
        main.sandbox,
        "execute",
        lambda safe_sql: (pd.DataFrame({"id": [1]}), 1.0, None),
    )
    monkeypatch.setattr(main.detector, "back_translate", lambda safe_sql: "List customer IDs")
    monkeypatch.setattr(main.detector, "calculate_similarity", lambda *args: 1.0)
    monkeypatch.setattr(
        main.agreement,
        "evaluate",
        lambda question, strategy="schema_glossary", sql_authorizer=None: type(
            "AgreementStub", (), {"agreement_ratio": 1.0, "candidates": ()}
        )(),
    )

    assert login(client, "employee-one", "employee-password-123").status_code == 200
    allowed = client.post("/v1/query", json={"question": "List customers"})
    denied = client.post("/v1/query", json={"question": "List customers", "dataset_id": "private"})
    assert allowed.status_code == 200
    assert denied.status_code == 403


def test_employee_cannot_access_admin_even_with_forged_role_field(auth_client):
    client, store, admin = auth_client
    employee = store.create_user("employee-two", "employee-password-123", "employee")
    store.grant_dataset(employee["id"], "core", admin["id"])
    assert login(client, "employee-two", "employee-password-123").status_code == 200
    assert client.get("/auth/me").json()["role"] == "employee"
    response = client.post(
        "/admin/users",
        json={"username": "forged", "password": "forged-password-123", "role": "admin"},
    )
    assert response.status_code == 403
    assert store.get_user_by_name("forged") is None


def test_analyst_cannot_grant_self_dataset_or_change_own_role(auth_client):
    client, store, admin = auth_client
    analyst = store.create_user("analyst-one", "analyst-password-123", "analyst")
    assert login(client, "analyst-one", "analyst-password-123").status_code == 200
    assert client.post("/admin/users/1/datasets/core/grant", json={}).status_code == 403
    assert client.post("/admin/users/1/role", json={"role": "admin"}).status_code == 403
    assert store.list_user_grants(analyst["id"]) == []


def test_admin_can_manage_users_and_grants_with_audit(auth_client):
    client, store, admin = auth_client
    assert login(client, "root-admin", "test-admin-password-123").status_code == 200
    created = client.post(
        "/admin/users",
        json={"username": "new-employee", "password": "employee-password-123", "role": "employee"},
    )
    assert created.status_code == 201
    target_id = created.json()["id"]
    assert "password_hash" not in created.json()
    assert client.post(f"/admin/users/{target_id}/datasets/core/grant", json={}).status_code == 200
    assert (
        client.post(f"/admin/users/{target_id}/role", json={"role": "analyst"}).status_code == 200
    )
    assert client.post(f"/admin/users/{target_id}/deactivate", json={}).status_code == 200
    assert store.get_user(target_id)["role"] == "analyst"
    assert store.list_user_grants(target_id) == ["core"]
    event_types = {event["event_type"] for event in store.list_audit_events()}
    assert {
        "user_created",
        "dataset_access_granted",
        "user_role_changed",
        "user_deactivated",
    } <= event_types
    assert admin["id"] == 1


def test_deactivated_user_session_is_revoked(auth_client):
    client, store, admin = auth_client
    employee = store.create_user("employee-three", "employee-password-123", "employee")
    store.grant_dataset(employee["id"], "core", admin["id"])
    assert login(client, "employee-three", "employee-password-123").status_code == 200
    assert client.get("/auth/me").status_code == 200
    store.set_active(employee["id"], False, admin["id"])
    assert client.get("/auth/me").status_code == 401


def test_passwords_are_hashed_and_password_reset_invalidates_sessions(auth_client):
    client, store, admin = auth_client
    employee = store.create_user("employee-four", "employee-password-123", "employee")
    store.grant_dataset(employee["id"], "core", admin["id"])
    assert employee["password_hash"] != "employee-password-123"
    assert login(client, "employee-four", "employee-password-123").status_code == 200
    with TestClient(main.app) as admin_client:
        assert login(admin_client, "root-admin", "test-admin-password-123").status_code == 200
        assert (
            admin_client.post(
                f"/admin/users/{employee['id']}/password",
                json={"password": "replacement-password-456"},
            ).status_code
            == 200
        )
    assert client.get("/auth/me").status_code == 401
    assert store.authenticate("employee-four", "replacement-password-456") is not None


def test_final_sql_table_scope_includes_joins_ctes_and_nested_references(auth_client, monkeypatch):
    client, store, admin = auth_client
    employee = store.create_user("restricted-user", "employee-password-123", "employee")
    with store.engine.begin() as conn:
        conn.execute(
            insert(datasets).values(
                dataset_id="customers_only",
                display_name="Customers only",
                schema_name="core",
                tables_json='["customers"]',
                is_active=True,
                created_at=1.0,
            )
        )
    store.grant_dataset(employee["id"], "customers_only", admin["id"])
    sql = "SELECT id FROM customers"
    generated = SQLGenerationResponse(
        sql=sql,
        explanation="List customers.",
        tables_used=["customers"],
        self_reported_confidence=0.9,
        answerable=True,
        ambiguity_flag=False,
    )
    monkeypatch.setattr(main.generator, "generate", lambda *args, **kwargs: (generated, sql))
    monkeypatch.setattr(
        main.sandbox, "execute", lambda safe_sql: (pd.DataFrame({"id": [1]}), 1.0, None)
    )
    monkeypatch.setattr(main.detector, "back_translate", lambda safe_sql: "List customers")
    monkeypatch.setattr(main.detector, "calculate_similarity", lambda *args: 1.0)
    candidate_executions = []

    def agreement_with_forbidden_candidate(
        question, strategy="schema_glossary", sql_authorizer=None
    ):
        sql_authorizer(
            "WITH c AS (SELECT id FROM customers) "
            "SELECT c.id FROM c JOIN (SELECT id FROM orders) o ON o.id = c.id"
        )
        candidate_executions.append("should never execute")

    monkeypatch.setattr(main.agreement, "evaluate", agreement_with_forbidden_candidate)
    assert login(client, "restricted-user", "employee-password-123").status_code == 200
    response = client.post(
        "/v1/query",
        json={"question": "List customers", "dataset_id": "customers_only"},
    )
    assert response.status_code == 403
    assert candidate_executions == []


def test_core_dataset_cannot_be_unregistered(auth_client):
    client, _, _ = auth_client
    assert login(client, "root-admin", "test-admin-password-123").status_code == 200
    assert client.post("/admin/datasets/core/unregister", json={}).status_code == 409
