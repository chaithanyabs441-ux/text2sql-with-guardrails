import logging
import re
import time

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from t2s.config import settings

logger = logging.getLogger(__name__)

class ExecutionSandbox:
    """Run validated SQL using the dedicated read-only database account."""

    def __init__(self) -> None:
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        """Create the database engine only when the first query is executed."""
        if self._engine is None:
            self._engine = create_engine(settings.DATABASE_URL_READONLY)
        return self._engine

    def execute(
        self, safe_sql: str, timeout_sec: int = 5, schema_name: str = "core"
    ) -> tuple[pd.DataFrame, float, str | None]:
        """Execute a query and return its rows, elapsed milliseconds, and error."""
        start_time = time.time()

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema_name):
            return pd.DataFrame(), 0.0, "Invalid dataset schema."
        try:
            with self.engine.connect() as conn:
                try:
                    conn.execute(text("SET TRANSACTION READ ONLY;"))
                    conn.execute(text(f'SET search_path TO "{schema_name}", public;'))
                    conn.execute(
                        text(f"SET LOCAL statement_timeout = '{timeout_sec}s';")
                    )

                    df = pd.read_sql(text(safe_sql), conn)
                finally:
                    # End the transaction explicitly on both successful and
                    # failed reads so connection-local state cannot leak.
                    conn.rollback()

                latency_ms = (time.time() - start_time) * 1000
                logger.info(
                    "Executed successfully in %.2fms. Rows: %s",
                    latency_ms,
                    len(df),
                )
                return df, latency_ms, None
                
        except Exception as exc:
            latency_ms = (time.time() - start_time) * 1000
            logger.error("Execution failed: %s", exc)

            # Pandas may wrap a database exception as the direct cause.
            error_msg = (
                str(exc.__cause__) if exc.__cause__ is not None else str(exc)
            )

            return pd.DataFrame(), latency_ms, error_msg
