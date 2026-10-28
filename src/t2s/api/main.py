"""FastAPI application for guarded natural-language SQL queries."""

import json
import logging
import math
import re
import threading
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, Literal

import sqlglot
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlglot import exp

from t2s.auth.permissions import (
    ROLE_PERMISSIONS,
    can_access_dataset,
    has_permission,
    query_tables_for_role,
)
from t2s.auth.store import AuthStore
from t2s.config import settings
from t2s.generation.generator import SQLGenerator
from t2s.guardrails.exceptions import UnsafeQueryError
from t2s.guardrails.policy import SQLPolicyEngine
from t2s.sandbox.executor import ExecutionSandbox
from t2s.schema.introspector import SchemaIntrospector
from t2s.schema.models import DatabaseSchema
from t2s.verification.abstention import AbstentionThresholds, choose_decision
from t2s.verification.agreement import MultiCandidateAgreement
from t2s.verification.back_translation import HallucinationDetector
from t2s.verification.sanity_checks import ResultSanityChecker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize additive authorization tables before accepting requests."""
    auth_store.ensure_schema()
    yield


app = FastAPI(title="Trustworthy Text-to-SQL API", lifespan=lifespan)

# These components no longer create LLM or embedding clients during import.
generator = SQLGenerator()
detector = HallucinationDetector()
sandbox = ExecutionSandbox()
checker = ResultSanityChecker()
agreement = MultiCandidateAgreement(generator=generator, sandbox=sandbox)

SELF_CORRECT_MAX_RETRIES = 2
_UPLOADED_KEY_SCHEMA_LOCK = threading.Lock()
_UPLOADED_KEY_SCHEMAS_READY: set[str] = set()
MAX_DATASET_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_IMPORTED_TABLES = 30
MAX_IMPORTED_ROWS_PER_TABLE = 500_000
CALIBRATION_REPORT = (
    Path(__file__).resolve().parents[3] / "eval" / "results" / "calibration" / "report.json"
)
FALLBACK_THRESHOLDS = AbstentionThresholds(answer_threshold=0.85, warn_threshold=0.50)
auth_store = AuthStore()
SESSION_COOKIE = "safequery_session"


@lru_cache(maxsize=64)
def _extract_uploaded_schema(schema_name: str) -> DatabaseSchema:
    """Cache the immutable schema for imported datasets without retaining DB pools."""
    introspector = SchemaIntrospector()
    try:
        return introspector.extract_schema(schema_name)
    finally:
        introspector.engine.dispose()


def current_user(request: Request) -> dict[str, Any]:
    user = auth_store.user_for_session(request.cookies.get(SESSION_COOKIE))
    if user is None:
        auth_store.audit("unauthorized_request", detail={"path": request.url.path})
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_permission(permission: str):
    def dependency(
        user: Annotated[dict[str, Any], Depends(current_user)],
    ) -> dict[str, Any]:
        if not has_permission(user["role"], permission):
            auth_store.audit(
                "unauthorized_request",
                actor_user_id=user["id"],
                detail={"permission": permission},
            )
            raise HTTPException(
                status_code=403, detail="You do not have permission for this action."
            )
        return user

    return dependency


AuthenticatedUser = Annotated[dict[str, Any], Depends(current_user)]
ManageUsersUser = Annotated[dict[str, Any], Depends(require_permission("manage_users"))]
ManageRolesUser = Annotated[dict[str, Any], Depends(require_permission("manage_roles"))]
ManageDatasetsUser = Annotated[dict[str, Any], Depends(require_permission("manage_datasets"))]
DeleteDatasetUser = Annotated[dict[str, Any], Depends(require_permission("delete_dataset"))]
ViewAuditUser = Annotated[dict[str, Any], Depends(require_permission("view_audit_logs"))]
ViewAnalyticsUser = Annotated[dict[str, Any], Depends(require_permission("view_query_analytics"))]
QueryUser = Annotated[dict[str, Any], Depends(require_permission("query_data"))]
ViewSchemaUser = Annotated[dict[str, Any], Depends(require_permission("view_schema"))]
ViewHistoryUser = Annotated[dict[str, Any], Depends(require_permission("view_own_history"))]


def require_dataset(user: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    dataset = auth_store.get_dataset(dataset_id)
    grants = auth_store.list_user_grants(user["id"])
    if (
        not dataset
        or not dataset["is_active"]
        or (
            not dataset["schema_name"].startswith("upload_")
            and not can_access_dataset(user["role"], dataset_id, grants)
        )
    ):
        auth_store.audit(
            "unauthorized_dataset_access",
            actor_user_id=user["id"],
            dataset_id=dataset_id,
        )
        raise HTTPException(status_code=403, detail="You do not have access to this dataset.")
    return dataset


def authorize_sql_dataset(sql: str, user: dict[str, Any], dataset: dict[str, Any]) -> None:
    """Check final SQL tables against both dataset grants and role table access."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise HTTPException(
            status_code=400, detail="Generated SQL could not be validated."
        ) from exc
    allowed = (
        frozenset(name.lower() for name in dataset["tables"])
        if dataset["schema_name"].startswith("upload_")
        else query_tables_for_role(user["role"], dataset["tables"])
    )
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not table.db and not table.catalog and table.name.lower() in ctes:
            continue
        if table.name.lower() not in allowed or table.db.lower() not in {
            "",
            dataset["schema_name"],
        }:
            auth_store.audit(
                "unauthorized_sql_table",
                actor_user_id=user["id"],
                dataset_id=dataset["dataset_id"],
                detail={"table": table.name},
            )
            raise HTTPException(
                status_code=403,
                detail="This question needs a table that is not available to your role.",
            )


def _safe_identifier(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    result = re.sub(r"[^a-zA-Z0-9_]+", "_", normalized).strip("_").lower()
    if not result:
        result = fallback
    if result[0].isdigit():
        result = "t_" + result
    return result[:55]


def _unique_names(names: list[str]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for index, original in enumerate(names, start=1):
        base = _safe_identifier(str(original), f"column_{index}")
        reserved = {
            "all",
            "by",
            "create",
            "delete",
            "drop",
            "from",
            "group",
            "index",
            "insert",
            "join",
            "key",
            "limit",
            "offset",
            "order",
            "primary",
            "select",
            "table",
            "update",
            "user",
            "values",
            "where",
        }
        if base in reserved:
            base = "data_" + base
        name, suffix = base, 2
        while name in used:
            tail = f"_{suffix}"
            name = base[: 63 - len(tail)] + tail
            suffix += 1
        used.add(name)
        result.append(name)
    return result


def _uploaded_frames(filename: str, content: bytes):
    import pandas as pd

    extension = Path(filename).suffix.lower()
    if extension == ".csv":
        return [
            (
                Path(filename).stem,
                pd.read_csv(
                    BytesIO(content), low_memory=False, nrows=MAX_IMPORTED_ROWS_PER_TABLE + 1
                ),
            )
        ]
    if extension == ".xlsx":
        workbook = pd.read_excel(
            BytesIO(content),
            sheet_name=None,
            engine="openpyxl",
            nrows=MAX_IMPORTED_ROWS_PER_TABLE + 1,
        )
        return list(workbook.items())
    raise HTTPException(status_code=415, detail="Upload CSV or XLSX files only.")


def _infer_uploaded_keys(conn: Any, schema_name: str, table_names: list[str]) -> None:
    """Infer common id-style primary/foreign keys when uploaded values prove valid."""
    inspector = inspect(conn)
    columns_by_table = {
        table: {
            column["name"]: column
            for column in inspector.get_columns(table, schema=schema_name)
        }
        for table in table_names
    }

    def singular(name: str) -> str:
        if name.endswith("ies") and len(name) > 3:
            return name[:-3] + "y"
        if name.endswith("s") and not name.endswith("ss"):
            return name[:-1]
        return name

    def valid_primary_key(table: str, column: str) -> bool:
        escaped_table = table.replace('"', '""')
        escaped_column = column.replace('"', '""')
        return bool(
            conn.execute(
                text(
                    f'SELECT COUNT(*)=COUNT("{escaped_column}") '
                    f'AND COUNT(*)=COUNT(DISTINCT "{escaped_column}") '
                    f'FROM "{schema_name}"."{escaped_table}"'
                )
            ).scalar_one()
        )

    def primary_key_name(table: str, columns: dict[str, Any]) -> str | None:
        id_columns = [name for name in columns if name.endswith("_id")]
        # Match the entity suffix, even when a dataset prefixes its table names
        # (for example ship_routes.route_id and game_games.game_id).
        entity_keys = [
            name
            for name in id_columns
            if singular(table) == name[:-3] or singular(table).endswith("_" + name[:-3])
        ]
        for candidate in entity_keys + (["id"] if "id" in columns else []):
            if valid_primary_key(table, candidate):
                return candidate
        # If names cannot identify the entity, use a unique *_id only when the
        # data proves there is exactly one such candidate.
        unique_candidates = [name for name in id_columns if valid_primary_key(table, name)]
        return unique_candidates[0] if len(unique_candidates) == 1 else None

    # Validate candidate primary keys first so foreign keys can target any upload order.
    primary_keys: dict[str, str] = {}
    for table, columns in columns_by_table.items():
        current_pk = inspector.get_pk_constraint(table, schema=schema_name).get(
            "constrained_columns", []
        )
        if len(current_pk) == 1:
            primary_keys[table] = current_pk[0]
            continue
        key_column = primary_key_name(table, columns)
        if not key_column:
            continue
        escaped_table = table.replace('"', '""')
        escaped_key = key_column.replace('"', '""')
        primary_keys[table] = key_column
        constraint = _safe_identifier(f"pk_{table}_{key_column}", "pk_imported")
        conn.execute(
            text(
                f'ALTER TABLE "{schema_name}"."{escaped_table}" '
                f'ADD CONSTRAINT "{constraint}" PRIMARY KEY ("{escaped_key}")'
            )
        )

    # Resolve matching *_id columns, including prefixed names such as
    # origin_port_id -> ports.port_id, then validate types and every non-null value.
    existing_fk_columns = {
        table: {
            column
            for fk in inspector.get_foreign_keys(table, schema=schema_name)
            for column in (fk.get("constrained_columns") or [])
        }
        for table in table_names
    }
    for table, columns in columns_by_table.items():
        for column, source_info in columns.items():
            if table in primary_keys and column == primary_keys[table]:
                continue
            if not column.endswith("_id") or column in existing_fk_columns[table]:
                continue
            candidates = []
            for target, target_key in primary_keys.items():
                if target == table:
                    continue
                if column == target_key or column.endswith("_" + target_key):
                    candidates.append((target, target_key))
            if len(candidates) > 1:
                # Prefer the entity named by the reference suffix, accounting for
                # dataset prefixes such as ship_ports.port_id and customers.id.
                reference = column[:-3]

                def target_match(
                    candidate: tuple[str, str], reference: str = reference
                ) -> tuple[bool, bool]:
                    target, target_key = candidate
                    entity = target_key[:-3] if target_key.endswith("_id") else singular(target)
                    token = entity.split("_")[-1]
                    matches = (
                        reference == entity
                        or reference.endswith("_" + entity)
                        or reference.split("_")[-1] == token
                    )
                    return not matches, target_key == "id"

                candidates.sort(key=target_match)
            for target, target_key in candidates:
                target_info = columns_by_table[target][target_key]
                try:
                    source_type = source_info["type"].python_type
                    target_type = target_info["type"].python_type
                    compatible = source_type is target_type or (
                        source_type is int and target_type is int
                    )
                except (AttributeError, NotImplementedError):
                    compatible = False
                if not compatible:
                    continue
                escaped_table = table.replace('"', '""')
                escaped_target = target.replace('"', '""')
                escaped_column = column.replace('"', '""')
                escaped_key = target_key.replace('"', '""')
                has_orphans = conn.execute(
                    text(
                        f'SELECT EXISTS (SELECT 1 FROM "{schema_name}"."{escaped_table}" s '
                        f'WHERE s."{escaped_column}" IS NOT NULL AND NOT EXISTS '
                        f'(SELECT 1 FROM "{schema_name}"."{escaped_target}" t '
                        f'WHERE t."{escaped_key}"=s."{escaped_column}"))'
                    )
                ).scalar_one()
                if has_orphans:
                    continue
                constraint = _safe_identifier(f"fk_{table}_{column}", "fk_imported")
                conn.execute(
                    text(
                        f'ALTER TABLE "{schema_name}"."{escaped_table}" '
                        f'ADD CONSTRAINT "{constraint}" FOREIGN KEY ("{escaped_column}") '
                        f'REFERENCES "{schema_name}"."{escaped_target}" ("{escaped_key}")'
                    )
                )
                break


def _ensure_uploaded_key_constraints(schema_name: str) -> None:
    """Backfill safe, validated id-style keys for older uploads once per process."""
    if schema_name in _UPLOADED_KEY_SCHEMAS_READY:
        return
    if not re.fullmatch(r"upload_[a-f0-9]{16}", schema_name):
        return
    with _UPLOADED_KEY_SCHEMA_LOCK:
        if schema_name in _UPLOADED_KEY_SCHEMAS_READY:
            return
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            with engine.begin() as conn:
                table_names = inspect(conn).get_table_names(schema=schema_name)
                _infer_uploaded_keys(conn, schema_name, table_names)
        except Exception:
            logger.exception("Could not backfill inferred keys for uploaded schema %s", schema_name)
        finally:
            engine.dispose()
        _extract_uploaded_schema.cache_clear()
        _UPLOADED_KEY_SCHEMAS_READY.add(schema_name)


@app.get("/datasets")
def available_datasets() -> list[dict[str, Any]]:
    """List active datasets so the login screen can select or import one."""
    return [
        {
            "dataset_id": dataset["dataset_id"],
            "display_name": dataset["display_name"],
            "tables": dataset["tables"],
            "is_default": dataset["dataset_id"] == "core",
        }
        for dataset in auth_store.list_datasets()
        if dataset["is_active"]
    ]


@app.post("/datasets/import", status_code=201)
async def import_dataset(
    display_name: str = Form(min_length=1, max_length=120),
    files: list[UploadFile] = File(min_length=1, max_length=10),  # noqa: B008
) -> dict[str, Any]:
    """Import CSV/XLSX tables into a new isolated schema without changing core."""
    frames: list[tuple[str, Any]] = []
    total_bytes = 0
    for upload in files:
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() not in {".csv", ".xlsx"}:
            raise HTTPException(status_code=415, detail="Upload CSV or XLSX files only.")
        content = await upload.read(MAX_DATASET_UPLOAD_BYTES - total_bytes + 1)
        total_bytes += len(content)
        if total_bytes > MAX_DATASET_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Combined upload limit is 200 MB.")
        try:
            frames.extend(_uploaded_frames(filename, content))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not read {filename}; check its format and headers."
            ) from exc
    if not frames or len(frames) > MAX_IMPORTED_TABLES:
        raise HTTPException(status_code=400, detail="Upload between 1 and 30 non-empty tables.")

    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Enter a dataset name.")
    table_names = _unique_names([name for name, _ in frames])
    for (original_name, frame), _table_name in zip(frames, table_names, strict=True):
        if frame.empty or len(frame) > MAX_IMPORTED_ROWS_PER_TABLE:
            raise HTTPException(
                status_code=400,
                detail=f"{original_name} must contain rows and stay under 500,000 rows.",
            )
        if len(frame.columns) == 0 or len(frame.columns) > 200:
            raise HTTPException(
                status_code=400, detail=f"{original_name} has an invalid column count."
            )
        frame.columns = _unique_names([str(column) for column in frame.columns])
        if any(len(column.encode("utf-8")) > 63 for column in frame.columns):
            raise HTTPException(
                status_code=400,
                detail=f"{original_name} has an invalid column name.",
            )

    dataset_id = "upload_" + uuid.uuid4().hex[:16]
    schema_name = dataset_id
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            for (_, frame), table_name in zip(frames, table_names, strict=True):
                frame.to_sql(
                    table_name,
                    conn,
                    schema=schema_name,
                    if_exists="fail",
                    index=False,
                    chunksize=1_000,
                )
            _infer_uploaded_keys(conn, schema_name, table_names)
            conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema_name}" TO t2s_readonly'))
            conn.execute(
                text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema_name}" TO t2s_readonly')
            )
        result = auth_store.register_imported_dataset(
            dataset_id, display_name.strip(), schema_name, table_names
        )
        _UPLOADED_KEY_SCHEMAS_READY.add(schema_name)
    except HTTPException:
        raise
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        raise HTTPException(
            status_code=400,
            detail="Dataset import failed; check the file data and try again.",
        ) from exc
    finally:
        engine.dispose()
    auth_store.audit(
        "dataset_imported",
        dataset_id=dataset_id,
        detail={"tables": len(table_names), "rows": sum(len(frame) for _, frame in frames)},
    )
    return {
        "dataset_id": result["dataset_id"],
        "display_name": result["display_name"],
        "tables": result["tables"],
    }


@app.get("/health")
def health_check() -> dict[str, str]:
    """Report that the API process is ready to accept requests."""
    return {"status": "ok"}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=12, max_length=1024)
    role: Literal["employee", "analyst", "admin"]


class RoleRequest(BaseModel):
    role: Literal["employee", "analyst", "admin"]


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


@app.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    remote_addr = request.client.host if request.client else "unknown"
    if not auth_store.login_allowed(body.username, remote_addr):
        auth_store.audit("login_rate_limited", detail={"remote_addr": remote_addr[:80]})
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    user = auth_store.authenticate(body.username, body.password)
    if user is None:
        auth_store.record_login_attempt(body.username, remote_addr)
        auth_store.audit("login_failed", detail={"username": body.username[:120]})
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    auth_store.clear_login_attempts(body.username, remote_addr)
    token, expires = auth_store.create_session(user["id"])
    auth_store.audit("login_succeeded", actor_user_id=user["id"])
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="strict",
        max_age=int(expires - datetime.now(UTC).timestamp()),
        path="/",
    )
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    user = auth_store.user_for_session(token)
    auth_store.revoke_session(token)
    if user:
        auth_store.audit("logout", actor_user_id=user["id"])
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return {"status": "ok"}


@app.get("/auth/me")
def who_am_i(user: AuthenticatedUser) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "permissions": sorted(ROLE_PERMISSIONS[user["role"]]),
        "datasets": auth_store.list_user_grants(user["id"]),
    }


@app.get("/admin/users")
def admin_list_users(user: ManageUsersUser) -> list[dict[str, Any]]:
    return auth_store.list_users()


@app.post("/admin/users", status_code=201)
def admin_create_user(
    body: CreateUserRequest,
    user: ManageUsersUser,
) -> dict[str, Any]:
    try:
        created = auth_store.create_user(body.username, body.password, body.role, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="That username is already registered.") from exc
    created.pop("password_hash", None)
    return created


@app.post("/admin/users/{target_id}/role")
def admin_change_role(
    target_id: int,
    body: RoleRequest,
    user: ManageRolesUser,
) -> dict[str, str]:
    if target_id == user["id"] and body.role != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote your own account.")
    target = auth_store.get_user(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if (
        target["role"] == "admin"
        and target["is_active"]
        and body.role != "admin"
        and auth_store.active_admin_count() <= 1
    ):
        raise HTTPException(
            status_code=409, detail="The last active administrator cannot be demoted."
        )
    try:
        auth_store.update_role(target_id, body.role, user["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    return {"status": "updated"}


@app.post("/admin/users/{target_id}/deactivate")
def admin_deactivate_user(
    target_id: int,
    user: ManageUsersUser,
) -> dict[str, str]:
    if target_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    target = auth_store.get_user(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target["role"] == "admin" and target["is_active"] and auth_store.active_admin_count() <= 1:
        raise HTTPException(
            status_code=409,
            detail="The last active administrator cannot be deactivated.",
        )
    try:
        auth_store.set_active(target_id, False, user["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    return {"status": "deactivated"}


@app.post("/admin/users/{target_id}/password")
def admin_reset_password(
    target_id: int,
    body: PasswordResetRequest,
    user: ManageUsersUser,
) -> dict[str, str]:
    try:
        auth_store.set_password(target_id, body.password, user["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "password_updated"}


@app.post("/admin/users/{target_id}/activate")
def admin_activate_user(
    target_id: int,
    user: ManageUsersUser,
) -> dict[str, str]:
    try:
        auth_store.set_active(target_id, True, user["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    return {"status": "activated"}


@app.post("/admin/users/{target_id}/datasets/{dataset_id}/grant")
def admin_grant_dataset(
    target_id: int,
    dataset_id: str,
    user: ManageDatasetsUser,
) -> dict[str, str]:
    if not auth_store.get_user(target_id):
        raise HTTPException(status_code=404, detail="User not found.")
    dataset = auth_store.get_dataset(dataset_id)
    if not dataset or not dataset["is_active"]:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    auth_store.grant_dataset(target_id, dataset_id, user["id"])
    return {"status": "granted"}


@app.post("/admin/users/{target_id}/datasets/{dataset_id}/revoke")
def admin_revoke_dataset(
    target_id: int,
    dataset_id: str,
    user: ManageDatasetsUser,
) -> dict[str, str]:
    auth_store.revoke_dataset(target_id, dataset_id, user["id"])
    return {"status": "revoked"}


@app.get("/admin/datasets")
def admin_list_datasets(
    user: ManageDatasetsUser,
) -> list[dict[str, Any]]:
    return auth_store.list_datasets()


@app.post("/admin/datasets")
def admin_register_dataset(
    body: dict[str, Any],
    user: ManageDatasetsUser,
) -> dict[str, Any]:
    # Dynamic connection registration would require a separate trusted connector
    # workflow. Only the bundled core dataset is supported by this architecture.
    if body.get("dataset_id") != "core":
        raise HTTPException(status_code=400, detail="Only the bundled core dataset is supported.")
    dataset = auth_store.get_dataset("core")
    if not dataset:
        raise HTTPException(status_code=404, detail="The core dataset is unavailable.")
    auth_store.audit("dataset_registration_requested", actor_user_id=user["id"], dataset_id="core")
    return dataset


@app.post("/admin/datasets/{dataset_id}/unregister")
def admin_unregister_dataset(
    dataset_id: str,
    user: DeleteDatasetUser,
) -> dict[str, str]:
    if dataset_id == "core":
        auth_store.audit("dataset_unregister_blocked", actor_user_id=user["id"], dataset_id="core")
        raise HTTPException(status_code=409, detail="The core database cannot be unregistered.")
    raise HTTPException(status_code=404, detail="Dataset not found.")


@app.get("/admin/audit-logs")
def admin_audit_logs(
    user: ViewAuditUser,
) -> list[dict[str, Any]]:
    return auth_store.list_audit_events()


@app.get("/admin/query-activity")
def query_activity(
    user: ViewAnalyticsUser,
) -> list[dict[str, Any]]:
    return auth_store.list_query_activity(user_id=None if user["role"] == "admin" else user["id"])


class QueryRequest(BaseModel):
    """A natural-language question to translate and execute."""

    question: str
    dataset_id: str = Field(default="core", min_length=1, max_length=80)
    strategy: Literal["zero_shot", "schema_glossary", "schema_glossary_selfcorrect"] = (
        "schema_glossary"
    )


class QueryResponse(BaseModel):
    """Generated SQL, execution results, and verification signals."""

    sql: str
    explanation: str
    data: list[Any]
    warnings: list[str]
    confidence_score: float
    back_translated_question: str
    confidence_signals: dict[str, float]
    decision: Literal["answer", "warn", "abstain_with_alternatives"]
    alternatives: list[str]
    calibration_ready: bool


class QueryHistoryEntry(BaseModel):
    """Metadata for a recent successful request; results are not retained."""

    question: str
    strategy: str
    sql: str
    created_at: datetime
    warnings: list[str]
    confidence_score: float
    decision: str = "answer"


@app.post("/v1/query", response_model=QueryResponse)
def process_query(
    req: QueryRequest,
    user: QueryUser,
) -> QueryResponse:
    """Generate, validate, execute, and verify SQL for one question."""
    try:
        dataset = require_dataset(user, req.dataset_id)
        generic_dataset = dataset["schema_name"].startswith("upload_")
        if generic_dataset:
            _ensure_uploaded_key_constraints(dataset["schema_name"])
        dataset_schema = (
            _extract_uploaded_schema(dataset["schema_name"])
            if generic_dataset
            else None
        )
        dataset_policy = (
            SQLPolicyEngine(
                allowed_tables=dataset["tables"],
                allowed_schema=dataset["schema_name"],
                sensitive_columns={},
            )
            if generic_dataset
            else None
        )
        generation_options = {
            "schema_override": dataset_schema,
            "policy_engine": dataset_policy,
            "generic_dataset": generic_dataset,
        }
        response, safe_sql = generator.generate(
            req.question, strategy=req.strategy, **generation_options
        )

        if not response.answerable:
            raise HTTPException(
                status_code=400,
                detail="Question is unanswerable with the current schema.",
            )

        authorize_sql_dataset(safe_sql, user, dataset)
        df, _latency_ms, error = sandbox.execute(safe_sql, schema_name=dataset["schema_name"])
        retry_count = 0
        while (
            error
            and req.strategy == "schema_glossary_selfcorrect"
            and retry_count < SELF_CORRECT_MAX_RETRIES
        ):
            retry_count += 1
            response, safe_sql = generator.generate(
                req.question,
                strategy=req.strategy,
                database_error=error[:2000],
                **generation_options,
            )
            if not response.answerable:
                raise HTTPException(
                    status_code=400,
                    detail="Question is unanswerable with the current schema.",
                )
            authorize_sql_dataset(safe_sql, user, dataset)
            df, _latency_ms, error = sandbox.execute(safe_sql, schema_name=dataset["schema_name"])

        warnings = checker.check(df, error)
        back_translated_question = detector.back_translate(safe_sql)
        back_translation_similarity = detector.calculate_similarity(
            req.question, back_translated_question
        )
        agreement_result = agreement.evaluate(
            req.question,
            strategy=req.strategy,
            sql_authorizer=lambda candidate_sql: authorize_sql_dataset(
                candidate_sql, user, dataset
            ),
            schema_override=dataset_schema,
            policy=dataset_policy,
            schema_name=dataset["schema_name"],
            generic_dataset=generic_dataset,
        )
        agreement_ratio = agreement_result.agreement_ratio
        schema_coverage = _schema_coverage(safe_sql, response.tables_used)
        confidence_signals = {
            "back_translation_similarity": back_translation_similarity,
            "agreement_ratio": agreement_ratio,
            "sanity_warning_count": float(len(warnings)),
            "self_reported_confidence": response.self_reported_confidence,
            "schema_coverage": schema_coverage,
        }
        confidence_score, thresholds, calibration_ready = _calibrated_confidence(confidence_signals)
        decision = choose_decision(
            confidence_score,
            thresholds,
            warning_count=len(warnings),
            agreement_ratio=agreement_ratio,
        )
        alternatives = list(
            dict.fromkeys(
                candidate.safe_sql
                for candidate in agreement_result.candidates
                if candidate.status == "executed"
                and candidate.safe_sql
                and candidate.safe_sql != safe_sql
            )
        )
        data_records = df.to_dict(orient="records") if not df.empty else []
        if decision == "abstain_with_alternatives":
            safe_sql = ""
            data_records = []

        result = QueryResponse(
            sql=safe_sql,
            explanation=response.explanation,
            data=data_records,
            warnings=warnings,
            confidence_score=confidence_score,
            back_translated_question=back_translated_question,
            confidence_signals=confidence_signals,
            decision=decision,
            alternatives=alternatives,
            calibration_ready=calibration_ready,
        )
        auth_store.add_query_history(
            user["id"],
            req.question,
            req.strategy,
            safe_sql,
            decision,
            confidence_score,
            dataset_id=req.dataset_id,
        )
        return result
    except HTTPException:
        auth_store.audit("query_failed", actor_user_id=user["id"], dataset_id=req.dataset_id)
        raise
    except UnsafeQueryError as exc:
        auth_store.audit("query_blocked", actor_user_id=user["id"], dataset_id=req.dataset_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        auth_store.audit(
            "query_failed",
            actor_user_id=user["id"],
            dataset_id=req.dataset_id,
            detail={"category": "service_unavailable"},
        )
        # Lazy LLM clients report missing provider credentials when first used.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        auth_store.audit(
            "query_failed",
            actor_user_id=user["id"],
            dataset_id=req.dataset_id,
            detail={"category": "internal_error"},
        )
        logger.exception("Query processing failed")
        raise HTTPException(
            status_code=500,
            detail="The query could not be processed.",
        ) from exc


@app.get("/v1/schema", response_model=DatabaseSchema)
def get_schema(
    user: ViewSchemaUser,
    dataset_id: str = "core",
) -> DatabaseSchema:
    """Return the full granted dataset schema; query authorization stays role-scoped."""
    dataset = require_dataset(user, dataset_id)
    if dataset["schema_name"] == "core":
        return generator.get_schema()
    _ensure_uploaded_key_constraints(dataset["schema_name"])
    return _extract_uploaded_schema(dataset["schema_name"])


@app.get("/v1/history", response_model=list[QueryHistoryEntry])
def get_history(
    user: ViewHistoryUser,
) -> list[QueryHistoryEntry]:
    """Return recent query metadata owned by the signed-in user, newest first."""
    return [
        QueryHistoryEntry(
            question=item["question"],
            strategy=item["strategy"],
            sql=item["sql"],
            created_at=datetime.fromtimestamp(item["created_at"], UTC),
            warnings=item["warnings"],
            confidence_score=item["confidence_score"],
            decision=item.get("decision", "answer"),
        )
        for item in auth_store.list_query_history(user["id"])
    ]


def _schema_coverage(sql: str, declared_tables: list[str]) -> float:
    """Return the fraction of SQL table references reported by the generator."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return 0.0
    references = {table.name.lower() for table in tree.find_all(exp.Table) if table.name}
    if not references:
        return 1.0
    declared = {name.split(".")[-1].lower() for name in declared_tables}
    return len(references & declared) / len(references)


def _calibrated_confidence(
    signals: dict[str, float],
) -> tuple[float, AbstentionThresholds, bool]:
    """Score signals from the saved logistic model or use a labeled fallback."""
    fallback = (signals["self_reported_confidence"], FALLBACK_THRESHOLDS, False)
    try:
        report = json.loads(CALIBRATION_REPORT.read_text(encoding="utf-8"))
        features = report["features"]
        coefficients = report["model"]["coefficients"][0]
        intercept = report["model"]["intercept"][0]
        logit = intercept + sum(
            coefficient * signals[name]
            for name, coefficient in zip(features, coefficients, strict=True)
        )
        score = 1 / (1 + math.exp(-max(-30.0, min(30.0, logit))))
        threshold_data = report.get("abstention_thresholds")
        if threshold_data is None:
            return score, FALLBACK_THRESHOLDS, False
        thresholds = AbstentionThresholds(**threshold_data)
        return score, thresholds, True
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback
