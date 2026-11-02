"""Real PostgreSQL test database for the gateway suite (task doc §7 T12, R8.5).

One dedicated test database (default ``subnet_acl_test_gw``) is provisioned
once per session: database, ``citext``, the ``registry`` schema owned by
``registry_app`` (with the -api's migrations applied first, because the gateway
has cross-schema FKs into it) and the ``gateway`` schema owned by
``gateway_app`` (the gateway's own migrations). Every test starts from truncated
tables: per-test truncation, not a temp schema.

Safety: the database name must contain ``test``; the fixture refuses anything
else, so a mis-typed override can never reach the live ``subnet_acl`` store.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

PG_HOST = os.environ.get("SUBNET_ACL_TEST_PG_HOST", "172.17.0.8")
PG_PORT = int(os.environ.get("SUBNET_ACL_TEST_PG_PORT", "5432"))
PG_ADMIN_PASSWORD = os.environ.get(
    "SUBNET_ACL_TEST_PG_ADMIN_PASSWORD", "subnet_acl_pg_admin"
)
TEST_DB = os.environ.get("GATEWAY_TEST_DB", "subnet_acl_test_gw")

REGISTRY_PW = os.environ.get("REGISTRY_DB_PASSWORD", "registry_app_pw")
GATEWAY_PW = os.environ.get("GATEWAY_DB_PASSWORD", "gateway_app_pw")
READER_PW = os.environ.get("GATEWAY_READER_DB_PASSWORD", "gateway_reader_pw")

REPO_ROOT = Path(__file__).resolve().parents[3]          # _subnet_acl
REGISTRY_MIGRATIONS_DIR = (
    REPO_ROOT / "_users_provision" / "user_provision_tool" / "db" / "migrations"
)

GATEWAY_TABLES = [
    "gateway.tasks",
    "gateway.pre_deploy_sessions",
    "gateway.stream_exposures",
    "gateway.acl_grants",
    "gateway.api_keys",
    "gateway.audit_log",
    "gateway.user_logins",
    "gateway.users",
    "gateway.gateway_settings",
    "gateway.system_config",
    "gateway.llm_config",
    "gateway.proxy_configs",
    "gateway.service_templates",
    "gateway.loop_leases",
    "gateway.import_state",
]

REGISTRY_TABLES = [
    "registry.tasks",
    "registry.service_basic",
    "registry.service_endpoints",
    "registry.service_instances",
    "registry.services",
    "registry.api_users",
    "registry.import_state",
]


def _assert_test_db(name: str) -> None:
    if "test" not in name.lower():
        raise RuntimeError(
            f"refusing to run the test fixture against database {name!r}: "
            "the test database name must contain 'test'"
        )


def admin_url(dbname: str = "postgres") -> str:
    return (
        f"postgresql://postgres:{PG_ADMIN_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"
    )


def registry_url(dbname: str = TEST_DB) -> str:
    return f"postgresql://registry_app:{REGISTRY_PW}@{PG_HOST}:{PG_PORT}/{dbname}"


def gateway_url(dbname: str = TEST_DB) -> str:
    return f"postgresql://gateway_app:{GATEWAY_PW}@{PG_HOST}:{PG_PORT}/{dbname}"


def gateway_sqlalchemy_url(dbname: str = TEST_DB) -> str:
    return (
        f"postgresql+psycopg://gateway_app:{GATEWAY_PW}"
        f"@{PG_HOST}:{PG_PORT}/{dbname}"
    )


def reader_sqlalchemy_url(dbname: str = TEST_DB) -> str:
    return (
        f"postgresql+psycopg://gateway_reader:{READER_PW}"
        f"@{PG_HOST}:{PG_PORT}/{dbname}"
    )


def ensure_database() -> None:
    _assert_test_db(TEST_DB)
    with psycopg.connect(admin_url(), autocommit=True, connect_timeout=10) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(admin_url(TEST_DB), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS citext")
        conn.execute(
            "CREATE SCHEMA IF NOT EXISTS registry AUTHORIZATION registry_app"
        )
        conn.execute(
            "CREATE SCHEMA IF NOT EXISTS gateway AUTHORIZATION gateway_app"
        )
        conn.execute(
            f'GRANT CONNECT ON DATABASE "{TEST_DB}" TO registry_app, '
            "gateway_app, gateway_reader"
        )
        conn.execute("GRANT USAGE ON SCHEMA gateway TO gateway_reader")


def apply_registry_migrations() -> list[str]:
    """Apply the -api's migrations as the registry schema owner (it runs first)."""
    applied: list[str] = []
    with psycopg.connect(
        registry_url(), row_factory=dict_row, autocommit=True
    ) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS registry.schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM registry.schema_migrations"
            ).fetchall()
        }
        for path in sorted(REGISTRY_MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO registry.schema_migrations (version) VALUES (%s)",
                (path.name,),
            )
            applied.append(path.name)
    return applied


def truncate() -> None:
    """Empty both schemas and restart identity; per-test isolation.

    Run as the cluster admin: ``registry`` rows are referenced by gateway FKs, so
    PostgreSQL requires ``CASCADE`` on the registry truncate, and the cascade
    must be able to clear the gateway tables too. The schema owners cannot do
    that (one owner per schema, by design).
    """
    _assert_test_db(TEST_DB)
    with psycopg.connect(admin_url(TEST_DB), autocommit=True) as conn:
        # A test that runs the app lifespan (or a background loop) can leave an
        # open transaction; TRUNCATE takes ACCESS EXCLUSIVE and would block
        # forever behind it. The database is this suite's alone, so end every
        # other backend before truncating.
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (TEST_DB,),
        )
        conn.execute(
            "TRUNCATE " + ", ".join(GATEWAY_TABLES + REGISTRY_TABLES)
            + " RESTART IDENTITY CASCADE"
        )


def provision() -> None:
    ensure_database()
    apply_registry_migrations()
