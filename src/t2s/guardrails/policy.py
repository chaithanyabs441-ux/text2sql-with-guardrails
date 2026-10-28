"""AST-based SQL policy checks for generated queries."""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import NoReturn

import sqlglot
from sqlglot import exp

from t2s.guardrails.exceptions import UnsafeQueryError

logger = logging.getLogger(__name__)
_AUDIT_LOCK = Lock()


class SQLPolicyEngine:
    """Validate generated SQL and return a normalized, row-limited query."""

    # These expression nodes can mutate data, alter database state, or invoke
    # commands. Checking every node also catches them inside CTEs and subqueries.
    FORBIDDEN_NODE_NAMES = frozenset(
        {
            "insert",
            "update",
            "delete",
            "merge",
            "into",
            "lock",
            "copy",
            "command",
            "set",
            "create",
            "drop",
            "alter",
        }
    )

    # The sample database's business tables. Callers can supply a narrower or
    # broader set when they intentionally expose a different schema.
    DEFAULT_ALLOWED_TABLES = frozenset(
        {
            "regions",
            "categories",
            "products",
            "customers",
            "employees",
            "orders",
            "order_items",
            "payments",
            "returns",
            "support_tickets",
        }
    )

    # These names cover common read-only scalar, aggregate, string, date/time,
    # and mathematical operations. Unknown or user-defined functions are denied.
    DEFAULT_ALLOWED_FUNCTIONS = frozenset(
        {
            "abs",
            "acos",
            "array_agg",
            "array_to_string",
            "asin",
            "atan",
            "avg",
            "bool_and",
            "bool_or",
            "case",
            "ceil",
            "ceiling",
            "char_length",
            "cast",
            "coalesce",
            "concat",
            "concat_ws",
            "cos",
            "cot",
            "count",
            "date",
            "date_part",
            "date_trunc",
            "currentdate",
            "dateadd",
            "datediff",
            "current_date",
            "current_timestamp",
            "extract",
            "exp",
            "floor",
            "greatest",
            "json_agg",
            "least",
            "length",
            "ln",
            "log",
            "lower",
            "ltrim",
            "max",
            "min",
            "mod",
            "now",
            "nullif",
            "power",
            "regexp_like",
            "regexp_replace",
            "replace",
            "round",
            "rtrim",
            "sin",
            "split_part",
            "sqrt",
            "string_agg",
            "substr",
            "substring",
            "sum",
            "tan",
            "timestamptrunc",
            "to_char",
            "to_date",
            "to_timestamp",
            "trim",
            "try_cast",
            "trycast",
            "trunc",
            "upper",
        }
    )

    # Shipping addresses are sensitive synthetic data in the demo schema.
    # Callers can extend this map for the columns their application protects.
    DEFAULT_SENSITIVE_COLUMNS = {"orders": frozenset({"shipping_address"})}

    def __init__(
        self,
        enforce_single_statement: bool = True,
        block_forbidden_operations: bool = True,
        enforce_select_only: bool = True,
        enforce_table_allowlist: bool = True,
        block_dangerous_functions: bool = True,
        enforce_limit: bool = True,
        enforce_join_limit: bool = True,
        enforce_subquery_depth: bool = True,
        block_cartesian_joins: bool = True,
        max_rows: int = 1000,
        max_joins: int = 5,
        max_subquery_depth: int = 3,
        dialect: str = "postgres",
        allowed_tables: Iterable[str] | None = None,
        allowed_schema: str = "core",
        allowed_functions: Iterable[str] | None = None,
        sensitive_columns: Mapping[str, Iterable[str]] | None = None,
        enforce_sensitive_columns: bool = True,
        block_tautological_predicates: bool = True,
        audit_log_path: str | Path = "logs/guardrail_audit.jsonl",
    ) -> None:
        self.enforce_single_statement = enforce_single_statement
        self.block_forbidden_operations = block_forbidden_operations
        self.enforce_select_only = enforce_select_only
        self.enforce_table_allowlist = enforce_table_allowlist
        self.block_dangerous_functions = block_dangerous_functions
        self.enforce_limit = enforce_limit
        self.enforce_join_limit = enforce_join_limit
        self.enforce_subquery_depth = enforce_subquery_depth
        self.block_cartesian_joins = block_cartesian_joins
        self.enforce_sensitive_columns = enforce_sensitive_columns
        self.block_tautological_predicates = block_tautological_predicates
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")
        if max_joins < 0:
            raise ValueError("max_joins cannot be negative")
        if max_subquery_depth < 0:
            raise ValueError("max_subquery_depth cannot be negative")
        self.max_rows = max_rows
        self.max_joins = max_joins
        self.max_subquery_depth = max_subquery_depth
        self.dialect = dialect
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", allowed_schema.lower()):
            raise ValueError("Invalid allowed schema")
        self.allowed_schema = allowed_schema.lower()
        table_names = (
            self.DEFAULT_ALLOWED_TABLES if allowed_tables is None else allowed_tables
        )
        function_names = (
            self.DEFAULT_ALLOWED_FUNCTIONS
            if allowed_functions is None
            else allowed_functions
        )
        self.allowed_tables = frozenset(name.lower() for name in table_names)
        self.allowed_functions = frozenset(name.lower() for name in function_names)
        column_map = (
            self.DEFAULT_SENSITIVE_COLUMNS
            if sensitive_columns is None
            else sensitive_columns
        )
        self.sensitive_columns = {
            table.lower(): frozenset(column.lower() for column in columns)
            for table, columns in column_map.items()
        }
        self.audit_log_path = Path(audit_log_path)

    def validate(self, sql: str) -> str:
        """Reject unsafe SQL and return the validated query with a LIMIT.

        Exactly one SQL statement is accepted. Forbidden operations are checked
        throughout the parsed AST, including CTEs and nested subqueries.
        """
        try:
            statements = [
                statement
                for statement in sqlglot.parse(sql, read=self.dialect)
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
        except sqlglot.errors.ParseError as exc:
            self._reject("SQL_PARSE", f"SQL could not be parsed: {exc}")

        if not statements:
            self._reject("EMPTY_QUERY", "SQL must contain a query statement.")

        if self.enforce_single_statement and len(statements) != 1:
            self._reject(
                "SINGLE_STATEMENT",
                "Exactly one SQL statement is permitted.",
            )

        safe_statements = []
        for ast in statements:
            safe_statements.append(self._validate_ast(ast))
        return ";\n".join(safe_statements)

    def _validate_ast(self, ast: exp.Expression) -> str:
        """Apply enabled policy rules to one parsed statement."""

        if self.block_forbidden_operations:
            forbidden_nodes = [
                node
                for node in ast.walk()
                if type(node).__name__.lower() in self.FORBIDDEN_NODE_NAMES
            ]
            if forbidden_nodes:
                node_name = type(forbidden_nodes[0]).__name__.upper()
                self._reject(
                    "FORBIDDEN_OPERATION",
                    f"SQL operation '{node_name}' is not permitted.",
                )

        # Restrict the statement's root to query expressions. The AST-wide
        # forbidden-node scan above separately catches writes nested in a query.
        if self.enforce_select_only and not isinstance(
            ast, exp.Select | exp.Union | exp.Subquery
        ):
            self._reject(
                "SELECT_ONLY",
                f"Query type '{type(ast).__name__}' is not allowed. "
                "Only SELECT is permitted.",
            )

        # CTE names are query-local relations, not database tables. Exempt only
        # unqualified references to those names from the base-table allowlist.
        cte_names = {
            cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE) if cte.alias
        }
        if self.enforce_table_allowlist:
            for table in ast.find_all(exp.Table):
                table_name = table.name.lower()
                schema_name = table.db.lower()
                catalog_name = table.catalog.lower()
                if not schema_name and not catalog_name and table_name in cte_names:
                    continue
                if (
                    table_name not in self.allowed_tables
                    or schema_name not in {"", self.allowed_schema}
                    or catalog_name
                ):
                    table_sql = table.sql(dialect=self.dialect)
                    self._reject(
                        "TABLE_ALLOWLIST",
                        f"Access to table '{table_sql}' is not allowed.",
                    )

        if self.block_dangerous_functions:
            for func in ast.find_all(exp.Func):
                # sqlglot models boolean operators as function-like nodes too;
                # they are SQL syntax, not user-defined function invocations.
                if isinstance(func, exp.And | exp.Or | exp.Not | exp.Exists):
                    continue
                function_name = func.name.lower().strip()
                expression_name = type(func).__name__.lower()
                if (
                    function_name not in self.allowed_functions
                    and expression_name not in self.allowed_functions
                ):
                    self._reject(
                        "FUNCTION_ALLOWLIST",
                        f"Use of function '{function_name or expression_name}' "
                        "is not allowed.",
                    )

            # sqlglot represents these SQL keywords as dedicated AST nodes.
            if ast.find(exp.CurrentUser):
                self._reject(
                    "IDENTITY_FUNCTION",
                    "Access to CURRENT_USER is forbidden.",
                )
            if ast.find(exp.SessionUser):
                self._reject(
                    "IDENTITY_FUNCTION",
                    "Access to SESSION_USER is forbidden.",
                )
            if ast.find(exp.CurrentDatabase):
                self._reject(
                    "DATABASE_METADATA_FUNCTION",
                    "Access to CURRENT_DATABASE is forbidden.",
                )

        if self.enforce_sensitive_columns:
            self._check_sensitive_columns(ast)

        if self.block_tautological_predicates:
            self._check_tautological_predicates(ast)

        if self.enforce_limit:
            self._cap_query_limits(ast)

        self._check_query_complexity(ast)

        safe_sql = ast.sql(dialect=self.dialect, pretty=True)
        logger.info("Guardrails passed. Safe SQL: %s", safe_sql)
        return safe_sql

    def _check_sensitive_columns(self, ast: exp.Expression) -> None:
        """Reject references or wildcards that could return protected columns."""
        cte_names = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
        aliases: dict[str, str] = {}
        referenced_tables: set[str] = set()
        for table in ast.find_all(exp.Table):
            table_name = table.name.lower()
            if table_name in cte_names and not table.db and not table.catalog:
                continue
            referenced_tables.add(table_name)
            aliases[table_name] = table_name
            aliases[table.alias_or_name.lower()] = table_name

        for column in ast.find_all(exp.Column):
            if column.is_star:
                continue
            column_name = column.name.lower()
            if column.table:
                source_table = aliases.get(column.table.lower(), column.table.lower())
                protected = column_name in self.sensitive_columns.get(
                    source_table, frozenset()
                )
            else:
                protected = any(
                    column_name in self.sensitive_columns.get(table, frozenset())
                    for table in referenced_tables
                )
            if protected:
                self._reject(
                    "SENSITIVE_COLUMN",
                    f"Access to sensitive column '{column_name}' is not permitted.",
                )

        protected_tables = referenced_tables.intersection(self.sensitive_columns)
        if not protected_tables:
            return
        for select in ast.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Star):
                    source_table = ""
                elif isinstance(projection, exp.Column) and projection.is_star:
                    source_table = aliases.get(
                        projection.table.lower(), projection.table.lower()
                    )
                else:
                    continue
                if source_table in protected_tables or not source_table:
                    self._reject(
                        "SENSITIVE_COLUMN",
                        "Wildcard projection may expose sensitive columns.",
                    )

    def _check_tautological_predicates(self, ast: exp.Expression) -> None:
        """Reject WHERE/HAVING/JOIN predicates that are always true."""
        predicates = [
            clause.this
            for clause in ast.find_all(exp.Where, exp.Having)
            if clause.this is not None
        ]
        predicates.extend(
            join.args["on"]
            for join in ast.find_all(exp.Join)
            if join.args.get("on") is not None
        )
        if any(self._is_tautology(predicate) for predicate in predicates):
            self._reject(
                "TAUTOLOGICAL_PREDICATE",
                "A filter or join condition is always true and could bypass intended restrictions.",
            )

    @classmethod
    def _is_tautology(cls, expression: exp.Expression) -> bool:
        """Recognize common constant-true boolean expressions without evaluation."""
        if isinstance(expression, exp.Paren):
            return cls._is_tautology(expression.this)
        if isinstance(expression, exp.Boolean):
            return str(expression.this).lower() == "true"
        if isinstance(expression, exp.Or):
            return cls._is_tautology(expression.this) or cls._is_tautology(
                expression.expression
            )
        if isinstance(expression, exp.And):
            return cls._is_tautology(expression.this) and cls._is_tautology(
                expression.expression
            )
        if isinstance(expression, exp.EQ):
            left, right = expression.this, expression.expression
            if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
                if left.is_string or right.is_string:
                    return left.is_string and right.is_string and left.this == right.this
                try:
                    from decimal import Decimal

                    return Decimal(str(left.this)) == Decimal(str(right.this))
                except Exception:
                    return False
        return False

    def _reject(self, rule_id: str, reason: str) -> NoReturn:
        """Write one structured audit event and raise a policy error."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "rule_id": rule_id,
            "reason": reason,
        }
        json_line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOCK, self.audit_log_path.open(
                "a", encoding="utf-8", newline="\n"
            ) as audit_file:
                audit_file.write(json_line + "\n")
        except OSError:
            # Keep the SQL rejection in effect and retain a structured fallback
            # in the configured application logger if the file is unavailable.
            logger.exception("Could not write guardrail audit file")
            logger.error("Guardrail audit event: %s", json_line)

        self._raise_policy_error(rule_id, reason)

    @staticmethod
    def _raise_policy_error(rule_id: str, reason: str) -> NoReturn:
        raise UnsafeQueryError(reason, layer=rule_id)

    def _check_query_complexity(self, ast: exp.Expression) -> None:
        """Reject excessive joins, implicit cross joins, and deep subqueries."""
        joins = list(ast.find_all(exp.Join))
        if self.block_cartesian_joins:
            for join in joins:
                kind = str(join.args.get("kind") or "").upper()
                has_condition = bool(join.args.get("on") or join.args.get("using"))
                if kind == "CROSS" or not has_condition:
                    self._reject(
                        "CARTESIAN_JOIN",
                        "CROSS JOINs and joins without an explicit ON or USING "
                        "condition are not permitted.",
                    )

        if self.enforce_join_limit and len(joins) > self.max_joins:
            self._reject(
                "JOIN_LIMIT",
                f"Query has {len(joins)} joins; the maximum is {self.max_joins}.",
            )

        if self.enforce_subquery_depth:
            query_nodes = (exp.Select, exp.Union)
            nesting_nodes = (exp.Subquery, exp.CTE)
            depths = []
            for query in ast.find_all(*query_nodes):
                depth = 0
                parent = query.parent
                while parent is not None:
                    if parent is not ast and isinstance(parent, nesting_nodes):
                        depth += 1
                    parent = parent.parent
                depths.append(depth)

            maximum_depth = max(depths, default=0)
            if maximum_depth > self.max_subquery_depth:
                self._reject(
                    "SUBQUERY_DEPTH",
                    f"Query nesting depth is {maximum_depth}; the maximum is "
                    f"{self.max_subquery_depth}.",
                )

    def _cap_query_limits(self, ast: exp.Expression) -> None:
        """Add or clamp LIMIT and OFFSET on every query in the AST."""
        query_nodes = (exp.Select, exp.Union)
        for query in ast.find_all(*query_nodes):
            limit = query.args.get("limit")
            if limit is None:
                query.limit(self.max_rows, copy=False)
            else:
                limit_value = self._literal_clause_value(limit)
                if limit_value is None or limit_value < 0 or limit_value > self.max_rows:
                    query.limit(self.max_rows, copy=False)

            offset = query.args.get("offset")
            if offset is not None:
                offset_value = self._literal_clause_value(offset)
                if offset_value is None or offset_value > self.max_rows:
                    query.offset(self.max_rows, copy=False)
                elif offset_value < 0:
                    query.offset(0, copy=False)

    @staticmethod
    def _literal_clause_value(clause: exp.Expression) -> int | None:
        """Return a non-string integer literal from a LIMIT or OFFSET node."""
        expression = clause.args.get("expression")
        if isinstance(expression, exp.Neg):
            expression = expression.this
            sign = -1
        else:
            sign = 1
        if not isinstance(expression, exp.Literal) or expression.is_string:
            return None
        try:
            return sign * int(expression.this)
        except (TypeError, ValueError):
            return None
