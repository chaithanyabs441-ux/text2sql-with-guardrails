"""Persistent authentication state, dataset grants, and audit events."""

import hashlib
import json
import secrets
import time
from contextlib import suppress
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import Engine

from t2s.auth.permissions import ROLES
from t2s.config import settings

SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_HASHER = PasswordHasher()
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("not-a-real-user-password")
metadata = MetaData()
users = Table(
    "t2s_auth_users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(120), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("role", String(24), nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "role IN ('employee', 'analyst', 'admin')",
        name="ck_t2s_auth_user_role",
    ),
)
sessions = Table(
    "t2s_auth_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("t2s_auth_users.id"), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked_at", Float),
)
datasets = Table(
    "t2s_auth_datasets",
    metadata,
    Column("dataset_id", String(80), primary_key=True),
    Column("display_name", String(160), nullable=False),
    Column("schema_name", String(80), nullable=False),
    Column("tables_json", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_at", Float, nullable=False),
)
dataset_grants = Table(
    "t2s_auth_dataset_grants",
    metadata,
    Column("user_id", Integer, ForeignKey("t2s_auth_users.id"), nullable=False),
    Column("dataset_id", String(80), ForeignKey("t2s_auth_datasets.dataset_id"), nullable=False),
    Column("granted_by", Integer, ForeignKey("t2s_auth_users.id"), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("user_id", "dataset_id", name="uq_t2s_auth_user_dataset"),
)
audit_events = Table(
    "t2s_auth_audit_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("actor_user_id", Integer, ForeignKey("t2s_auth_users.id")),
    Column("event_type", String(80), nullable=False),
    Column("target_user_id", Integer, ForeignKey("t2s_auth_users.id")),
    Column("dataset_id", String(80)),
    Column("detail_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
login_attempts = Table(
    "t2s_auth_login_attempts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(120), nullable=False),
    Column("remote_addr", String(80), nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("ix_t2s_auth_login_attempts_created", login_attempts.c.created_at)
Index(
    "ix_t2s_auth_audit_actor_type",
    audit_events.c.actor_user_id,
    audit_events.c.event_type,
    audit_events.c.created_at,
)


class AuthStore:
    """Persistence boundary for users, opaque sessions, grants, and audit data."""

    def __init__(self, engine: Engine | None = None, database_url: str | None = None):
        self._engine = engine
        self._database_url = database_url or settings.DATABASE_URL

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self._database_url, pool_pre_ping=True)
        return self._engine

    def ensure_schema(self) -> None:
        """Create missing auth tables additively and register the existing core dataset."""
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            statement = text(
                "INSERT INTO t2s_auth_datasets "
                "(dataset_id, display_name, schema_name, tables_json, is_active, created_at) "
                "VALUES (:id, :name, :schema, :tables, TRUE, :now) "
                "ON CONFLICT (dataset_id) DO NOTHING"
            )
            conn.execute(
                statement,
                {
                    "id": "core",
                    "name": "SafeQuery sample database",
                    "schema": "core",
                    "tables": json.dumps(
                        [
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
                        ]
                    ),
                    "now": time.time(),
                },
            )

    @staticmethod
    def normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_user(
        self, username: str, password: str, role: str, actor_id: int | None = None
    ) -> dict[str, Any]:
        username = self.normalize_username(username)
        if role not in ROLES:
            raise ValueError("Unknown role.")
        if not 3 <= len(username) <= 120:
            raise ValueError("Username must contain 3 to 120 characters.")
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters.")
        with self.engine.begin() as conn:
            result = conn.execute(
                users.insert().values(
                    username=username,
                    password_hash=PASSWORD_HASHER.hash(password),
                    role=role,
                    is_active=True,
                    created_at=time.time(),
                )
            )
            user_id = result.inserted_primary_key[0]
            self._audit(conn, "user_created", actor_user_id=actor_id, target_user_id=user_id)
        return self.get_user(user_id) or {}

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
            return dict(row) if row else None

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(users).where(users.c.username == self.normalize_username(username))
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(users).order_by(users.c.id)).mappings().all()
            result = []
            for row in rows:
                user = dict(row)
                user.pop("password_hash", None)
                user["datasets"] = self.list_user_grants(user["id"], conn)
                result.append(user)
            return result

    def active_admin_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(users)
                    .where(users.c.role == "admin", users.c.is_active.is_(True))
                ).scalar_one()
            )

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_name(username)
        if not user or not user["is_active"]:
            with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
                PASSWORD_HASHER.verify(DUMMY_PASSWORD_HASH, password)
            return None
        try:
            return user if PASSWORD_HASHER.verify(user["password_hash"], password) else None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None

    def create_session(self, user_id: int) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + SESSION_TTL_SECONDS
        with self.engine.begin() as conn:
            conn.execute(
                sessions.insert().values(
                    token_hash=self._token_hash(token),
                    user_id=user_id,
                    created_at=now,
                    expires_at=expires,
                    revoked_at=None,
                )
            )
        return token, int(expires)

    def user_for_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(users)
                    .join(sessions, users.c.id == sessions.c.user_id)
                    .where(
                        sessions.c.token_hash == self._token_hash(token),
                        sessions.c.revoked_at.is_(None),
                        sessions.c.expires_at > time.time(),
                        users.c.is_active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def revoke_session(self, token: str | None) -> None:
        if token:
            with self.engine.begin() as conn:
                conn.execute(
                    sessions.update()
                    .where(sessions.c.token_hash == self._token_hash(token))
                    .values(revoked_at=time.time())
                )

    def login_allowed(self, username: str, remote_addr: str) -> bool:
        cutoff = time.time() - 15 * 60
        with self.engine.connect() as conn:
            count = conn.execute(
                select(login_attempts.c.id).where(
                    login_attempts.c.created_at > cutoff,
                    login_attempts.c.username == self.normalize_username(username),
                    login_attempts.c.remote_addr == remote_addr,
                )
            ).all()
        return len(count) < 5

    def record_login_attempt(self, username: str, remote_addr: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                login_attempts.insert().values(
                    username=self.normalize_username(username),
                    remote_addr=remote_addr[:80],
                    created_at=time.time(),
                )
            )

    def clear_login_attempts(self, username: str, remote_addr: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                login_attempts.delete().where(
                    login_attempts.c.username == self.normalize_username(username),
                    login_attempts.c.remote_addr == remote_addr,
                )
            )

    @staticmethod
    def _dataset_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["tables"] = json.loads(item.pop("tables_json"))
        return item

    def list_datasets(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(datasets).order_by(datasets.c.dataset_id)).mappings()
            return [self._dataset_dict(row) for row in rows]

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(datasets).where(datasets.c.dataset_id == dataset_id))
                .mappings()
                .first()
            )
            return self._dataset_dict(row) if row else None

    def register_imported_dataset(
        self, dataset_id: str, display_name: str, schema_name: str, table_names: list[str]
    ) -> dict[str, Any]:
        """Register an isolated uploaded dataset as available to signed-in users."""
        with self.engine.begin() as conn:
            conn.execute(
                datasets.insert().values(
                    dataset_id=dataset_id,
                    display_name=display_name,
                    schema_name=schema_name,
                    tables_json=json.dumps(table_names),
                    is_active=True,
                    created_at=time.time(),
                )
            )
        return self.get_dataset(dataset_id) or {}

    def list_user_grants(self, user_id: int, conn: Any = None) -> list[str]:
        if conn is None:
            with self.engine.connect() as connection:
                return self.list_user_grants(user_id, connection)
        return list(
            conn.execute(
                select(dataset_grants.c.dataset_id).where(dataset_grants.c.user_id == user_id)
            ).scalars()
        )

    def grant_dataset(self, user_id: int, dataset_id: str, actor_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO t2s_auth_dataset_grants "
                    "(user_id, dataset_id, granted_by, created_at) "
                    "VALUES (:uid, :did, :actor, :now) "
                    "ON CONFLICT (user_id, dataset_id) DO NOTHING"
                ),
                {"uid": user_id, "did": dataset_id, "actor": actor_id, "now": time.time()},
            )
            self._audit(
                conn,
                "dataset_access_granted",
                actor_user_id=actor_id,
                target_user_id=user_id,
                dataset_id=dataset_id,
            )

    def revoke_dataset(self, user_id: int, dataset_id: str, actor_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                dataset_grants.delete().where(
                    dataset_grants.c.user_id == user_id, dataset_grants.c.dataset_id == dataset_id
                )
            )
            self._audit(
                conn,
                "dataset_access_revoked",
                actor_user_id=actor_id,
                target_user_id=user_id,
                dataset_id=dataset_id,
            )

    def update_role(self, user_id: int, role: str, actor_id: int) -> None:
        if role not in ROLES:
            raise ValueError("Unknown role.")
        with self.engine.begin() as conn:
            result = conn.execute(users.update().where(users.c.id == user_id).values(role=role))
            if not result.rowcount:
                raise LookupError("User not found.")
            self._audit(
                conn,
                "user_role_changed",
                actor_user_id=actor_id,
                target_user_id=user_id,
                detail={"role": role},
            )

    def set_active(self, user_id: int, active: bool, actor_id: int) -> None:
        now = time.time()
        with self.engine.begin() as conn:
            result = conn.execute(
                users.update().where(users.c.id == user_id).values(is_active=active)
            )
            if not result.rowcount:
                raise LookupError("User not found.")
            if not active:
                conn.execute(
                    sessions.update()
                    .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            self._audit(
                conn,
                "user_deactivated" if not active else "user_activated",
                actor_user_id=actor_id,
                target_user_id=user_id,
            )

    def set_password(self, user_id: int, password: str, actor_id: int) -> None:
        if len(password) < 12:
            raise ValueError("Password must contain at least 12 characters.")
        with self.engine.begin() as conn:
            result = conn.execute(
                users.update()
                .where(users.c.id == user_id)
                .values(password_hash=PASSWORD_HASHER.hash(password))
            )
            if not result.rowcount:
                raise LookupError("User not found.")
            conn.execute(
                sessions.update()
                .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=time.time())
            )
            self._audit(
                conn, "user_password_changed", actor_user_id=actor_id, target_user_id=user_id
            )

    def audit(
        self,
        event_type: str,
        actor_user_id: int | None = None,
        target_user_id: int | None = None,
        dataset_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            self._audit(conn, event_type, actor_user_id, target_user_id, dataset_id, detail)

    @staticmethod
    def _audit(
        conn: Any,
        event_type: str,
        actor_user_id: int | None = None,
        target_user_id: int | None = None,
        dataset_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            audit_events.insert().values(
                actor_user_id=actor_user_id,
                event_type=event_type,
                target_user_id=target_user_id,
                dataset_id=dataset_id,
                detail_json=json.dumps(detail or {}, ensure_ascii=False),
                created_at=time.time(),
            )
        )

    def add_query_history(
        self, user_id: int, question: str, strategy: str, safe_sql: str, outcome: str, score: float,
        dataset_id: str = "core",
    ) -> None:
        self.audit(
            "query_completed",
            actor_user_id=user_id,
            detail={
                "question": question[:2000],
                "strategy": strategy,
                "sql": safe_sql[:10000],
                "outcome": outcome,
                "confidence_score": score,
            },
            dataset_id=dataset_id,
        )

    def list_query_history(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(audit_events)
                .where(
                    audit_events.c.actor_user_id == user_id,
                    audit_events.c.event_type == "query_completed",
                )
                .order_by(audit_events.c.created_at.desc())
                .limit(limit)
            ).mappings()
            return [
                {
                    "question": (detail := json.loads(row["detail_json"]))["question"],
                    "strategy": detail["strategy"],
                    "sql": detail["sql"],
                    "created_at": row["created_at"],
                    "warnings": [],
                    "confidence_score": detail["confidence_score"],
                    "decision": detail["outcome"],
                }
                for row in rows
            ]

    def list_query_activity(
        self, user_id: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = select(audit_events).where(audit_events.c.event_type == "query_completed")
        if user_id is not None:
            query = query.where(audit_events.c.actor_user_id == user_id)
        with self.engine.connect() as conn:
            rows = conn.execute(
                query.order_by(audit_events.c.created_at.desc()).limit(limit)
            ).mappings()
            return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def list_audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(audit_events).order_by(audit_events.c.created_at.desc()).limit(limit)
            ).mappings()
            return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]
