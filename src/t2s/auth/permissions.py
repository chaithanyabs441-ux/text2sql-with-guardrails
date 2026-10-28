"""Central role-to-permission mapping and dataset authorization helpers."""

from collections.abc import Iterable

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "employee": frozenset({"query_data", "view_dataset", "view_schema", "view_own_history"}),
    "analyst": frozenset(
        {
            "query_data",
            "view_dataset",
            "view_schema",
            "view_own_history",
            "view_query_analytics",
        }
    ),
    "admin": frozenset(
        {
            "query_data",
            "view_dataset",
            "view_schema",
            "view_own_history",
            "view_query_analytics",
            "manage_datasets",
            "delete_dataset",
            "manage_users",
            "manage_roles",
            "view_audit_logs",
        }
    ),
}

# Query access is enforced against the physical tables in generated SQL. Keep
# employee queries focused on operational data; analysts can use finance,
# returns, and workforce tables; admins can use every registered core table.
ROLE_QUERY_TABLES: dict[str, frozenset[str] | None] = {
    "employee": frozenset(
        {
            "regions",
            "categories",
            "products",
            "customers",
            "orders",
            "order_items",
            "support_tickets",
        }
    ),
    "analyst": None,
    "admin": None,
}

ROLES = frozenset(ROLE_PERMISSIONS)


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def can_access_dataset(role: str, dataset_id: str, grants: Iterable[str]) -> bool:
    return role == "admin" or dataset_id in grants


def query_tables_for_role(role: str, dataset_tables: Iterable[str]) -> frozenset[str]:
    """Return the physical dataset tables queryable by this role."""
    dataset = frozenset(table.lower() for table in dataset_tables)
    if role not in ROLE_QUERY_TABLES:
        return frozenset()
    role_tables = ROLE_QUERY_TABLES.get(role)
    return dataset if role_tables is None else dataset & role_tables
