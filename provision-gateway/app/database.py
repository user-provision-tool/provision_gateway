"""SQLAlchemy engine and session for SQLite gateway database."""

from __future__ import annotations

from sqlalchemy import create_engine, event
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


# --- SQLite pragmas for a FOUR-PROCESS deployment (GRILLING G31) ---------------
# The gateway runs `uvicorn --workers 4`, so four processes share this one file.
# Without these, concurrent writers do not queue — a writer that finds the write
# lock held fails immediately with "database is locked" rather than waiting, and
# readers block the writer outright.
#
#   journal_mode=WAL  readers never block the writer (and vice versa); the mode
#                     is stored in the DB header, so this is idempotent. It adds
#                     `-wal` / `-shm` files beside gateway.db.
#   busy_timeout      WAIT for the lock instead of failing — the single most
#                     important one here, and the only reason a burst of
#                     concurrent progress writes does not surface as errors.
#   synchronous=NORMAL  the WAL-appropriate durability/throughput balance: a
#                     commit can lose the last transaction only on an OS/power
#                     crash, never on a process crash.
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - exercised live
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


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
      - ``api_keys.target_type`` (prefixed key targets: ``end_user:<id>`` /
        ``admin:<id>``) and the ``(user_id, target_type)`` default-key index

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
    if "generation_jobs" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("generation_jobs")}
        if "pending_input" not in cols:
            # External-credential pause: the job parks in `awaiting_input` with
            # the pending request (and the resume payload) in this column.
            with engine_.begin() as conn:
                conn.execute(text("ALTER TABLE generation_jobs ADD COLUMN pending_input TEXT"))
    if "api_keys" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("api_keys")}
        with engine_.begin() as conn:
            if "mask" not in cols:
                conn.execute(text("ALTER TABLE api_keys ADD COLUMN mask VARCHAR(255)"))
            if "is_default" not in cols:
                conn.execute(
                    text("ALTER TABLE api_keys ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT 0")
                )
            # Prefixed key targets: which id space the row's ``user_id`` is in.
            # Every pre-existing row is an end-user key — before the prefix the
            # resolver could only ever return an EndUser — so that is the honest
            # backfill, and it is what keeps the old rows readable.
            if "target_type" not in cols:
                conn.execute(text(
                    "ALTER TABLE api_keys ADD COLUMN target_type VARCHAR(16) "
                    "NOT NULL DEFAULT 'end_user'"
                ))
            conn.execute(text(
                "UPDATE api_keys SET target_type = 'end_user' "
                "WHERE target_type IS NULL OR target_type = ''"
            ))
            # G2: drift repair — keep exactly one default per (user_id,
            # target_type) (the oldest id) so the partial unique index below can
            # be created. The id alone is NOT unique across the two id spaces.
            conn.execute(text(
                "UPDATE api_keys SET is_default = 0 "
                "WHERE is_default = 1 "
                "AND id NOT IN ("
                "  SELECT MIN(id) FROM api_keys WHERE is_default = 1 "
                "  GROUP BY user_id, target_type"
                ")"
            ))
            # G2: create the partial unique index if the existing DB predates it
            # (fresh DBs get it from Base.metadata.create_all via __table_args__).
            # An index that still keys on ``user_id`` alone would block the second
            # id space's default (end_users.id 1 vs admins.id 1 share the value),
            # so it is replaced, not kept.
            idx_by_name = {i["name"]: i for i in inspect(engine_).get_indexes("api_keys")}
            existing_idx = idx_by_name.get("uq_api_keys_one_default")
            if existing_idx is None:
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_api_keys_one_default "
                    "ON api_keys (user_id, target_type) WHERE is_default = 1"
                ))
            elif "target_type" not in (existing_idx.get("column_names") or []):
                conn.execute(text("DROP INDEX uq_api_keys_one_default"))
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_api_keys_one_default "
                    "ON api_keys (user_id, target_type) WHERE is_default = 1"
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
