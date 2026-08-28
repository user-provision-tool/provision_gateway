"""SQLAlchemy engine and session for SQLite gateway database."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    # Fail a pool checkout quickly when every connection is in use instead of
    # blocking 30s (SQLAlchemy default). A long block in an async request would
    # freeze the event loop and wedge the whole worker (Aug 2026 outage); 2s is
    # long enough to absorb normal churn while staying recoverable under load.
    pool_timeout=2,
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

    G2: also de-duplicates drifted ``is_default`` (keep the oldest default per
    user, unset the rest) and creates the partial unique index
    ``(user_id) WHERE is_default``. G7: backfills ``mask`` for pre-migration
    rows (the raw token is hashed at rest, so a stable display mask is derived
    from the stored ``token_hash``).
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine_)
    if "api_keys" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("api_keys")}
        with engine_.begin() as conn:
            if "mask" not in cols:
                conn.execute(text("ALTER TABLE api_keys ADD COLUMN mask VARCHAR(255)"))
            if "is_default" not in cols:
                conn.execute(
                    text("ALTER TABLE api_keys ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 0")
                )
            # G2: drift repair — keep exactly one default per user (the oldest
            # id) so the partial unique index below can be created. Idempotent.
            conn.execute(text(
                "UPDATE api_keys SET is_default = 0 "
                "WHERE is_default = 1 "
                "AND id NOT IN ("
                "  SELECT MIN(id) FROM api_keys WHERE is_default = 1 GROUP BY user_id"
                ")"
            ))
            # G2: create the partial unique index if the existing DB predates it
            # (fresh DBs get it from Base.metadata.create_all via __table_args__).
            idx_names = {i["name"] for i in inspect(engine_).get_indexes("api_keys")}
            if "uq_api_keys_one_default" not in idx_names:
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_api_keys_one_default "
                    "ON api_keys (user_id) WHERE is_default = 1"
                ))
            # G7: backfill masks for pre-migration rows (display-only).
            conn.execute(text(
                "UPDATE api_keys SET mask = substr(token_hash, -8) "
                "WHERE mask IS NULL OR mask = ''"
            ))


def init_db():
    """Create all tables if they don't exist, then migrate existing tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_schema(engine)
