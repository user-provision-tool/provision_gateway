"""PostgreSQL engine, sessions and versioned migrations for the gateway schema.

T1 moved the gateway off SQLite. The database service creates the database,
roles and schemas; ``gateway_app`` owns and migrates the ``gateway`` schema and
never creates a registry object (R1.2). The registry schema must exist before
this module migrates, because two gateway FKs reference registry rows; startup
retries until it does (the compose order is retry-based).

Two connections:
  * ``engine``        — ``gateway_app``: account/auth/task reads and writes;
  * ``reader_engine`` — ``gateway_reader``: the per-request registry read path
                        (hostname/status/Basic) and ``gateway.task_view``. It has
                        no grant on the gateway auth tables.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_log = logging.getLogger("gateway.database")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"
MIGRATION_LOCK_KEY = 7_412_002  # stable, gateway-owned advisory-lock key


engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    # Fail a pool checkout quickly when every connection is in use instead of
    # blocking 30s (SQLAlchemy default). A long block in an async request would
    # freeze the event loop and wedge the whole worker (Aug 2026 outage); 2s is
    # long enough to absorb normal churn while staying recoverable under load.
    pool_timeout=2,
    echo=False,
)

reader_engine = create_engine(
    settings.GATEWAY_READER_DATABASE_URL,
    pool_pre_ping=True,
    pool_timeout=2,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
ReaderSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=reader_engine)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


def get_db():
    """FastAPI dependency: yields a DB session and closes it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_reader_db():
    """FastAPI dependency for the read-only registry path."""
    db = ReaderSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migration_files() -> list[tuple[str, Path]]:
    if not MIGRATIONS_DIR.is_dir():
        return []
    return [(p.name, p) for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def _apply_migrations(timeout_seconds: float = 60.0) -> list[str]:
    """Apply pending gateway migrations under an advisory lock.

    Retried while the database (or the registry schema) is not ready. The lock
    is transaction-scoped, so it is released by commit/rollback and cannot be
    left held by a crashed worker.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": MIGRATION_LOCK_KEY},
                )
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS gateway.schema_migrations ("
                    " version text PRIMARY KEY,"
                    " applied_at timestamptz NOT NULL DEFAULT now())"
                ))
                done = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT version FROM gateway.schema_migrations")
                    )
                }
                applied: list[str] = []
                for version, path in _migration_files():
                    if version in done:
                        continue
                    conn.exec_driver_sql(path.read_text(encoding="utf-8"))
                    conn.execute(
                        text(
                            "INSERT INTO gateway.schema_migrations (version) "
                            "VALUES (:v)"
                        ),
                        {"v": version},
                    )
                    applied.append(version)
                    _log.info("applied gateway migration %s", version)
                return applied
        except Exception as exc:  # noqa: BLE001 — readiness retry, then fail closed
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"gateway database not ready after {timeout_seconds:.0f}s: {exc}"
                ) from exc
            time.sleep(2.0)


def init_db() -> None:
    """Wait for PostgreSQL and apply the gateway schema migrations.

    No ``create_all``: the DDL is the frozen SQL migration, so a fresh database
    and an upgraded one take exactly the same path.
    """
    applied = _apply_migrations()
    if applied:
        print(f"[gateway] applied migrations: {', '.join(applied)}", flush=True)
