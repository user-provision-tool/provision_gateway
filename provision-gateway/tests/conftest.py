"""Pytest fixtures for provision-gateway tests."""

import os
import pytest
import subprocess

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8870")

_gateway_checked = False
_gateway_running = False


def _is_gateway_running() -> bool:
    """Check if the provision-gateway is reachable (cached)."""
    global _gateway_checked, _gateway_running
    if _gateway_checked:
        return _gateway_running
    _gateway_checked = True
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{GATEWAY_URL}/health"],
            capture_output=True, text=True, timeout=5,
        )
        _gateway_running = result.stdout.strip() == "200"
    except Exception:
        _gateway_running = False
    return _gateway_running


@pytest.fixture(autouse=True)
def _skip_if_no_gateway(request):
    """Auto-skip integration tests if the gateway is not running.

    Only applies to tests in test_integration.py — unit tests are unaffected.
    """
    if "test_integration" in request.node.fspath.strpath:
        if not _is_gateway_running():
            pytest.skip("Gateway is not running — skipping integration test")


@pytest.fixture(scope="session")
def token(tmp_path_factory):
    """Fixture that returns a v4 cookie-jar path for authenticated tests.

    v4 auth (F4/N5) returns no access_token/refresh_token: login sets a
    HttpOnly ``provision_token`` cookie. The fixture logs in and saves the
    cookie to a session-scoped jar; callers pass the jar path to ``run_curl``,
    which sends it via ``-b``.

    Skips tests if the gateway is not running.
    """
    if not _is_gateway_running():
        pytest.skip("Gateway is not running — skipping integration test")

    admin_email = os.environ.get("GATEWAY_ADMIN_EMAIL", "admin@subnet-acl.local")
    admin_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "admin-pass-123")
    cookie_jar = str(tmp_path_factory.mktemp("cookies") / "cookies.txt")

    result = subprocess.run(
        ["curl", "-s", "-c", cookie_jar, "-X", "POST",
         f"{GATEWAY_URL}/api/auth/login",
         "-H", "Content-Type: application/json",
         "-d", f'{{"email":"{admin_email}","password":"{admin_password}"}}'],
        capture_output=True, text=True, timeout=10,
    )

    try:
        with open(cookie_jar) as f:
            content = f.read()
        if "provision_token" in content:
            return cookie_jar
    except OSError:
        pass

    pytest.skip("Could not obtain auth cookie — gateway may not be set up yet")
    return ""  # unreachable but satisfies type checker


@pytest.fixture(autouse=True)
def _isolated_gateway_db(tmp_path_factory, monkeypatch):
    """Point every test at its OWN throwaway SQLite database (GRILLING G31).

    Two reasons this is autouse rather than opt-in:

    1. **Safety.** The gateway's real store is `gateway_data/gateway.db`, shared
       with the live container. Any test that reaches `SessionLocal` without an
       override writes into the running system's data.
    2. **The store is a table now.** `pre_deploy_registry` used to be a JSON file
       under `GATEWAY_DATA_DIR`, so setting that env var to `tmp_path` was enough
       isolation. The sessions moved into SQLite, so a temp *directory* no longer
       isolates anything — a temp *engine* does.

    `database.SessionLocal` is resolved at call time by every service (the
    codebase's convention), so patching it here reaches all of them.
    """
    import app.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — registers every model on Base.metadata
    from app.database import Base

    # A SEPARATE directory, not the test's own `tmp_path`: a test that
    # fingerprints `tmp_path` would otherwise see this file (`gateway-test.db`)
    # as one of its own entries.
    data_dir = tmp_path_factory.mktemp("gateway-data")
    engine = create_engine(f"sqlite:///{data_dir}/gateway.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        database, "SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False),
    )
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(data_dir))
    # ...and the RESOLVED setting, not only the environment variable. `settings`
    # is built at import time, so a later `setenv` cannot reach it — which left
    # every per-task LOG path pointing at the live `/srv/provision/gateway_data`
    # for the whole suite. Anything that unlinks a log (`prune_finished` does)
    # would then have deleted a real deployment's log file.
    from app.config import settings
    monkeypatch.setattr(settings, "GATEWAY_DATA_DIR", data_dir, raising=False)

    # The manager is a module-level SINGLETON and every test gets a FRESH
    # database whose job ids restart at 1. Its id-keyed bookkeeping would
    # therefore outlive the rows it describes: a cancel booked in one test
    # (or a `_cancel_polled` throttle timestamp) would silently apply to a
    # later test's job that reused the id. Same stale-id hazard `_set_status`
    # guards against with its `discard`. Reset it with the database.
    from app.services.generation_jobs import generation_jobs
    generation_jobs._tasks.clear()
    generation_jobs._cancelled.clear()
    generation_jobs._cancel_polled.clear()

    return engine
