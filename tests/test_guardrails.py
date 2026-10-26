"""Behavioral tests for the SQL AST policy engine."""

import json

import pytest

from t2s.guardrails.exceptions import UnsafeQueryError
from t2s.guardrails.policy import SQLPolicyEngine


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (id) VALUES (1)",
        "UPDATE orders SET status = 'cancelled'",
        "DELETE FROM orders WHERE id = 1",
        "MERGE INTO orders USING customers ON orders.id = customers.id "
        "WHEN MATCHED THEN DELETE",
        "CREATE TABLE copied_orders (id INT)",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN extra INT",
        "SELECT id INTO copied_orders FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "WITH u AS (UPDATE orders SET status = 'cancelled' RETURNING *) "
        "SELECT * FROM u",
        "COPY orders TO '/tmp/orders.csv'",
        "SET search_path TO public",
    ],
)
def test_rejects_forbidden_operations_anywhere(sql_policy, sql):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM pg_roles",
        "SELECT * FROM pg_user",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM public.orders",
        "SELECT * FROM analytics.events",
        "SELECT * FROM other_db.core.orders",
        'SELECT * FROM "pg_roles"',
        "SeLeCt * FrOm Pg_RoLeS",
        'SELECT * FROM "public"."orders"',
        "SELECT * FROM core.not_a_business_table",
        "SELECT * FROM generate_series(1, 100000000)",
    ],
)
def test_rejects_tables_outside_core_allowlist(sql_policy, sql):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(1)",
        "SELECT set_config('x', 'y', false)",
        "SELECT version()",
        "SELECT current_database()",
        "SELECT current_user",
        "SELECT session_user",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/tmp/file')",
        "SELECT dblink('connection', 'SELECT 1')",
        "SELECT unknown_custom_function(1)",
    ],
)
def test_rejects_functions_outside_allowlist(sql_policy, sql):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT id FROM orders; DROP TABLE orders",
        "SELECT id FROM orders /* comment */; DELETE FROM orders",
        "SELECT id FROM orders -- comment\n; UPDATE orders SET status = 'x'",
        "SELECT 1; /* second statement */ SELECT 2",
    ],
)
def test_rejects_stacked_statements(sql_policy, sql):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id FROM orders o CROSS JOIN customers c",
        "SELECT o.id FROM orders o CROSS JOIN customers c CROSS JOIN regions r",
        "SELECT o.id FROM orders o JOIN customers c",
        "SELECT o.id FROM orders o, customers c",
    ],
)
def test_rejects_cartesian_or_conditionless_joins(sql_policy, sql):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders",
        "SeLeCt ID FrOm CuStOmErS",
        "SELECT id FROM core.orders WHERE id > 0",
        'SELECT id FROM "core"."customers"',
        'SELECT name AS "客户" FROM customers',
        "SELECT 'semi;colon' FROM orders",
        "SELECT id FROM orders /* harmless comment */",
        "-- leading comment\nSELECT id FROM orders",
        "SELECT id FROM orders -- trailing comment\nWHERE id > 0",
        "SELECT id FROM orders /* pg_roles, pg_sleep, DELETE are comments */",
        "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
        "SELECT q.id FROM (SELECT id FROM orders) AS q",
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT o.id FROM orders o JOIN customers c USING (id)",
        "SELECT id FROM customers WHERE is_deleted = FALSE AND segment = 'enterprise'",
        (
            "SELECT id FROM orders AS o WHERE NOT EXISTS "
            "(SELECT 1 FROM payments AS p WHERE p.order_id = o.id)"
        ),
        "SELECT COALESCE(SUM(net_revenue), 0) FROM orders",
        "SELECT LOWER(name) FROM customers",
        "SELECT DATE_TRUNC('month', order_date) FROM orders",
        "SELECT id::integer FROM orders",
        "SELECT CURRENT_DATE",
    ],
)
def test_accepts_safe_queries(sql_policy, sql):
    safe_sql = sql_policy.validate(sql)

    assert safe_sql
    assert "LIMIT 1000" in safe_sql.upper()


def test_accepts_one_trailing_semicolon(sql_policy):
    safe_sql = sql_policy.validate("SELECT id FROM orders;")

    assert "SELECT" in safe_sql.upper()


def test_custom_table_and_function_allowlists_are_supported(tmp_path):
    policy = SQLPolicyEngine(
        allowed_tables={"customers"},
        allowed_functions={"lower"},
        audit_log_path=tmp_path / "audit.jsonl",
    )

    assert "LOWER" in policy.validate("SELECT LOWER(name) FROM customers").upper()
    with pytest.raises(UnsafeQueryError):
        policy.validate("SELECT id FROM orders")


def test_caps_large_limit_to_configured_maximum(sql_policy):
    policy = SQLPolicyEngine(max_rows=50, audit_log_path=sql_policy.audit_log_path)

    safe_sql = policy.validate("SELECT id FROM orders LIMIT 100000000")

    assert "LIMIT 50" in safe_sql.upper()
    assert "100000000" not in safe_sql


def test_preserves_limit_below_configured_maximum(sql_policy):
    policy = SQLPolicyEngine(max_rows=50, audit_log_path=sql_policy.audit_log_path)

    safe_sql = policy.validate("SELECT id FROM orders LIMIT 20")

    assert "LIMIT 20" in safe_sql.upper()
    assert "LIMIT 50" not in safe_sql.upper()


def test_caps_nested_query_limits(sql_policy):
    safe_sql = sql_policy.validate(
        "SELECT q.id FROM (SELECT id FROM orders LIMIT 2000) AS q LIMIT 3000"
    )

    assert safe_sql.upper().count("LIMIT 1000") == 2


def test_enforces_join_count(sql_policy):
    policy = SQLPolicyEngine(
        max_joins=1,
        audit_log_path=sql_policy.audit_log_path,
    )
    sql = (
        "SELECT o.id FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "JOIN regions r ON r.id = c.region_id"
    )

    with pytest.raises(UnsafeQueryError, match="maximum is 1"):
        policy.validate(sql)


def test_enforces_subquery_depth(sql_policy):
    policy = SQLPolicyEngine(
        max_subquery_depth=2,
        audit_log_path=sql_policy.audit_log_path,
    )
    sql = (
        "SELECT q3.id FROM (SELECT q2.id FROM (SELECT q1.id FROM "
        "(SELECT id FROM orders) q1) q2) q3"
    )

    with pytest.raises(UnsafeQueryError, match="nesting depth"):
        policy.validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.shipping_address FROM orders AS o",
        "SELECT shipping_address FROM orders",
        "SELECT * FROM orders",
        "SELECT o.* FROM orders AS o",
    ],
)
def test_blocks_sensitive_shipping_address_references(sql_policy, sql):
    with pytest.raises(UnsafeQueryError) as exc_info:
        sql_policy.validate(sql)

    assert exc_info.value.layer == "SENSITIVE_COLUMN"


def test_sensitive_columns_can_be_configured_for_an_application(sql_policy):
    policy = SQLPolicyEngine(
        sensitive_columns={"customers": {"name"}},
        audit_log_path=sql_policy.audit_log_path,
    )

    with pytest.raises(UnsafeQueryError, match="sensitive column 'name'"):
        policy.validate("SELECT c.name FROM customers AS c")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM customers WHERE id = 1 OR 1 = 1",
        "SELECT id FROM customers WHERE name = 'x' OR 'a' = 'a'",
        "SELECT id FROM customers WHERE TRUE",
    ],
)
def test_blocks_tautological_filter_bypasses(sql_policy, sql):
    with pytest.raises(UnsafeQueryError) as exc_info:
        sql_policy.validate(sql)

    assert exc_info.value.layer == "TAUTOLOGICAL_PREDICATE"


def test_allows_non_tautological_filter_with_redundant_true_condition(sql_policy):
    safe_sql = sql_policy.validate("SELECT id FROM customers WHERE id = 1 AND 1 = 1")

    assert "WHERE" in safe_sql.upper()


def test_rule_flags_can_be_disabled_individually(sql_policy):
    policy = SQLPolicyEngine(
        enforce_single_statement=False,
        block_cartesian_joins=False,
        audit_log_path=sql_policy.audit_log_path,
    )

    safe_sql = policy.validate(
        "SELECT o.id FROM orders o CROSS JOIN customers c; SELECT 1"
    )

    assert safe_sql.upper().count("SELECT") == 2


def test_rejections_are_written_as_json_lines(sql_policy):
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate("SELECT * FROM pg_roles")

    lines = sql_policy.audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["rule_id"] == "TABLE_ALLOWLIST"
    assert "pg_roles" in event["reason"]
    assert event["timestamp"]


@pytest.mark.parametrize(
    ("sql", "expected_rule"),
    [
        ("SELECT FROM", "SQL_PARSE"),
        ("", "EMPTY_QUERY"),
        ("SELECT 1; SELECT 2", "SINGLE_STATEMENT"),
        ("DELETE FROM orders", "FORBIDDEN_OPERATION"),
        ("SELECT * FROM pg_roles", "TABLE_ALLOWLIST"),
        ("SELECT set_config('x', 'y', false)", "FUNCTION_ALLOWLIST"),
        (
            "SELECT o.id FROM orders o CROSS JOIN customers c",
            "CARTESIAN_JOIN",
        ),
    ],
)
def test_each_blocked_rule_writes_audit_event(tmp_path, sql, expected_rule):
    policy = SQLPolicyEngine(audit_log_path=tmp_path / "audit.jsonl")

    with pytest.raises(UnsafeQueryError):
        policy.validate(sql)

    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["rule_id"] == expected_rule


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE id = 1",
        "SELECT id FROM orders; -- trailing comment",
        "SELECT name AS \"κλάδος\" FROM customers",
        "SELECT id FROM orders WHERE name = 'DROP TABLE orders'",
    ],
)
def test_comments_and_unicode_do_not_change_safe_query_parsing(sql_policy, sql):
    assert sql_policy.validate(sql)


def test_rejects_unicode_homoglyph_function_name(sql_policy):
    # The 'е' in this name is Cyrillic, not Latin.
    with pytest.raises(UnsafeQueryError):
        sql_policy.validate("SELECT pg_slеep(1)")
