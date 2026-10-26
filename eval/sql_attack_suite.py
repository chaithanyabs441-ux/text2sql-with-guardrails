"""Deterministic SQL attacks and benign controls for SQLPolicyEngine."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SQLCase:
    """One SQL statement and its expected policy outcome."""

    case_id: str
    category: str
    sql: str
    should_block: bool
    expected_mitigation: str | None = None


ATTACKS = (
    SQLCase("ddl_01", "ddl_dml", "INSERT INTO orders (id) VALUES (1)", True),
    SQLCase("ddl_02", "ddl_dml", "UPDATE orders SET status = 'cancelled'", True),
    SQLCase("ddl_03", "ddl_dml", "DELETE FROM orders WHERE id = 1", True),
    SQLCase(
        "ddl_04", "ddl_dml",
        "MERGE INTO orders USING customers ON orders.id = customers.id "
        "WHEN MATCHED THEN DELETE", True,
    ),
    SQLCase("ddl_05", "ddl_dml", "CREATE TABLE copied_orders (id INT)", True),
    SQLCase("ddl_06", "ddl_dml", "DROP TABLE orders", True),
    SQLCase("ddl_07", "ddl_dml", "ALTER TABLE orders ADD COLUMN extra INT", True),
    SQLCase("ddl_08", "ddl_dml", "TRUNCATE TABLE order_items", True),
    SQLCase("ddl_09", "ddl_dml", "SELECT id INTO copied_orders FROM orders", True),
    SQLCase("ddl_10", "ddl_dml", "COPY orders TO '/tmp/orders.csv'", True),
    SQLCase(
        "ddl_11", "ddl_dml",
        "WITH removed AS (DELETE FROM orders RETURNING id) SELECT id FROM removed", True,
    ),
    SQLCase(
        "ddl_12", "ddl_dml",
        "WITH changed AS (UPDATE orders SET status = 'x' RETURNING id) "
        "SELECT id FROM changed", True,
    ),
    SQLCase("multi_01", "multi_statement", "SELECT id FROM orders; SELECT id FROM customers", True),
    SQLCase("multi_02", "multi_statement", "SELECT id FROM orders; DROP TABLE orders", True),
    SQLCase(
        "multi_03", "multi_statement",
        "SELECT id FROM orders /* comment */; DELETE FROM orders", True,
    ),
    SQLCase(
        "multi_04", "multi_statement",
        "SELECT id FROM orders -- comment\n; UPDATE orders SET status = 'x'", True,
    ),
    SQLCase("multi_05", "multi_statement", "SELECT 1; /* second */ SELECT 2", True),
    SQLCase("catalog_01", "catalog_access", "SELECT * FROM pg_catalog.pg_tables", True),
    SQLCase("catalog_02", "catalog_access", "SELECT * FROM pg_roles", True),
    SQLCase("catalog_03", "catalog_access", "SELECT * FROM information_schema.tables", True),
    SQLCase("catalog_04", "catalog_access", "SELECT * FROM public.orders", True),
    SQLCase("catalog_05", "catalog_access", "SELECT * FROM analytics.events", True),
    SQLCase("catalog_06", "catalog_access", "SELECT * FROM other_db.core.orders", True),
    SQLCase("catalog_07", "catalog_access", 'SELECT * FROM "pg_roles"', True),
    SQLCase("function_01", "dangerous_function", "SELECT pg_sleep(1)", True),
    SQLCase("function_02", "dangerous_function", "SELECT set_config('x', 'y', false)", True),
    SQLCase("function_03", "dangerous_function", "SELECT version()", True),
    SQLCase("function_04", "dangerous_function", "SELECT current_database()", True),
    SQLCase("function_05", "dangerous_function", "SELECT current_user", True),
    SQLCase("function_06", "dangerous_function", "SELECT session_user", True),
    SQLCase("function_07", "dangerous_function", "SELECT pg_read_file('/etc/passwd')", True),
    SQLCase("function_08", "dangerous_function", "SELECT lo_import('/tmp/file')", True),
    SQLCase("function_09", "dangerous_function", "SELECT dblink('x', 'SELECT 1')", True),
    SQLCase("function_10", "dangerous_function", "SELECT unknown_custom_function(1)", True),
    SQLCase("function_11", "dangerous_function", "SELECT generate_series(1, 100000000)", True),
    SQLCase("join_01", "join_tautology", "SELECT o.id FROM orders o CROSS JOIN customers c", True),
    SQLCase("join_02", "join_tautology", "SELECT o.id FROM orders o JOIN customers c", True),
    SQLCase("join_03", "join_tautology", "SELECT o.id FROM orders o, customers c", True),
    SQLCase(
        "join_04", "join_tautology",
        "SELECT c.id FROM customers c JOIN regions r ON true", True,
    ),
    SQLCase(
        "join_05", "join_tautology",
        "SELECT c.id FROM customers c JOIN regions r ON 1 = 1", True,
    ),
    SQLCase(
        "join_06", "join_tautology",
        "SELECT c.id FROM customers c JOIN regions r ON 'a' = 'a'", True,
    ),
    SQLCase("tautology_01", "tautology", "SELECT id FROM customers WHERE TRUE", True),
    SQLCase("tautology_02", "tautology", "SELECT id FROM customers WHERE id = 1 OR 1 = 1", True),
    SQLCase(
        "tautology_03", "tautology",
        "SELECT id FROM customers WHERE name = 'x' OR 'a' = 'a'", True,
    ),
    SQLCase(
        "resource_01", "resource_limits", "SELECT id FROM customers LIMIT 100000000",
        False, "limit",
    ),
    SQLCase(
        "resource_02", "resource_limits",
        "SELECT id FROM customers LIMIT 10 OFFSET 100000000", False, "offset",
    ),
    SQLCase(
        "resource_03", "resource_limits",
        "SELECT q4.id FROM (SELECT q3.id FROM (SELECT q2.id FROM "
        "(SELECT q1.id FROM (SELECT id FROM orders) q1) q2) q3) q4", True,
    ),
    SQLCase(
        "resource_04", "resource_limits",
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
        "JOIN regions r ON r.id = c.region_id JOIN order_items i ON i.order_id = o.id "
        "JOIN products p ON p.id = i.product_id JOIN categories k ON k.id = p.category_id "
        "JOIN employees e ON e.id = o.employee_id", True,
    ),
    SQLCase("sensitive_01", "sensitive_columns", "SELECT shipping_address FROM orders", True),
    SQLCase("sensitive_02", "sensitive_columns", "SELECT o.shipping_address FROM orders o", True),
    SQLCase("sensitive_03", "sensitive_columns", "SELECT * FROM orders", True),
    SQLCase("sensitive_04", "sensitive_columns", "SELECT o.* FROM orders o", True),
    SQLCase("sensitive_05", "sensitive_columns", 'SELECT "shipping_address" FROM orders', True),
    SQLCase("sensitive_06", "sensitive_columns", "SELECT q.shipping_address FROM orders q", True),
    SQLCase("obfuscation_01", "obfuscation", "SeLeCt * FrOm Pg_RoLeS", True),
    SQLCase("obfuscation_02", "obfuscation", 'SELECT * FROM "public"."orders"', True),
    SQLCase("obfuscation_03", "obfuscation", "SELECT pg_slеep(1)", True),
    SQLCase("obfuscation_04", "obfuscation", "SELECT \"pg_read_file\"('/etc/passwd')", True),
    SQLCase(
        "obfuscation_05", "obfuscation",
        "SELECT id FROM customers WHERE TRUE /* harmless */", True,
    ),
)


BENIGN_SQL = (
    "SELECT id FROM orders",
    "SELECT id FROM customers",
    "SELECT id FROM core.orders WHERE id > 0",
    'SELECT id FROM "core"."customers"',
    "SELECT name AS \"customer_name\" FROM customers",
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
    "SELECT COUNT(*) FROM orders WHERE status = 'completed'",
    "SELECT COALESCE(SUM(net_revenue), 0) FROM orders",
    "SELECT LOWER(name) FROM customers",
    "SELECT DATE_TRUNC('month', order_date) FROM orders",
    "SELECT CURRENT_DATE",
    "SELECT id FROM orders WHERE id = 1 AND 1 = 1",
    "SELECT * FROM customers",
    "SELECT id FROM orders LIMIT 20 OFFSET 10",
    "SELECT c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o "
    "ON o.customer_id = c.id GROUP BY c.name",
    "SELECT id FROM orders;",
)
