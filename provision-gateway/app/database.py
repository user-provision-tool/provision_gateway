"""SQLAlchemy engine and session for SQLite gateway database."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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


def _ensure_schema(engine_=engine):
    """Best-effort ALTER-based migration of pre-existing tables.

    ``Base.metadata.create_all`` only creates missing tables; it never alters
    existing ones, so a gateway.db created by an earlier version keeps its old
    columns. This idempotently adds the columns newer models introduced:

      - ``api_keys.mask`` / ``api_keys.is_default`` (v4 §6.1.1-6.1.3, GAP-7/8)

    Missing a migration means every end-user login that touches the default-key
    query (``SELECT ... api_keys.mask ...``) raises ``no such column`` and the
    startup default-key backfill fails silently. Runs after ``create_all`` so a
    fresh DB is a no-op and an existing DB is brought up to date.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine_)
    if "api_keys" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("api_keys")}
        stmts: list[str] = []
        if "mask" not in cols:
            stmts.append("ALTER TABLE api_keys ADD COLUMN mask VARCHAR(255)")
        if "is_default" not in cols:
            stmts.append(
                "ALTER TABLE api_keys ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 0"
            )
        with engine_.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))


def init_db():
    """Create all tables if they don't exist, then migrate existing tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_schema(engine)
