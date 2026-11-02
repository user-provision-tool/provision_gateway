"""Pytest fixtures for provision-gateway tests."""

import os
import sys
from pathlib import Path

import pytest
import subprocess

# tests/ helper for the real PostgreSQL test database (T12/R8.5).
sys.path.insert(0, str(Path(__file__).parent))
import pg_test_db  # noqa: E402 — path set above

# Point the application at the dedicated TEST database BEFORE any app module is
# imported: app.config reads these at import time. The fixture refuses any
# database whose name lacks "test", so a bad override cannot reach the live store.
os.environ["DATABASE_URL"] = pg_test_db.gateway_sqlalchemy_url()
os.environ["GATEWAY_READER_DATABASE_URL"] = pg_test_db.reader_sqlalchemy_url()

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


@pytest.fixture(autouse=True, scope="module")
def _integration_tests_need_gateway_and_credentials(request):
    """Skip the live-gateway file only when it cannot meaningfully run.

    Only applies to tests in test_integration.py — unit tests are unaffected.

    The file drives a running gateway with a pre-provisioned test identity, so
    it needs (a) credentials in ``GATEWAY_TEST_USER``/``GATEWAY_TEST_PASSWORD``
    and (b) a gateway answering ``/health`` at ``GATEWAY_URL``. With both it
    always runs; the suite itself creates no users, services or tasks (see the
    module docstring — it only reads and targets nonexistent ids).
    """
    if "test_integration" in request.node.fspath.strpath:
        if not (os.environ.get("GATEWAY_TEST_USER")
                and os.environ.get("GATEWAY_TEST_PASSWORD")):
            pytest.skip(
                "integration tests need GATEWAY_TEST_USER/GATEWAY_TEST_PASSWORD "
                "(a pre-provisioned test identity)"
            )
        if not _is_gateway_running():
            pytest.skip("integration tests need a gateway answering /health at GATEWAY_URL")


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


@pytest.fixture(scope="session", autouse=True)
def _pg_test_database():
    """Provision the real PostgreSQL test database once per session (T12).

    The -api's registry migrations run first (the gateway has cross-schema FKs
    into that schema); then the gateway's own migrations.
    """
    pg_test_db.provision()
    import app.database as database

    database.init_db()
    yield
    database.engine.dispose()
    database.reader_engine.dispose()


@pytest.fixture(autouse=True)
def _isolated_gateway_db(tmp_path_factory, monkeypatch):
    """Per-test truncation on the PostgreSQL test database + a temp data dir.

    Safety: the application's engines point at ``subnet_acl_test_gw`` only (set
    at conftest import). The live ``gateway_data`` directory is never touched
    because both the env var and the resolved setting are redirected here.
    """
    pg_test_db.truncate()

    from app.config import settings

    data_dir = tmp_path_factory.mktemp("gateway-data")
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(data_dir))

    # A per-test PROVISION_DIR with a fresh gateway-read keyring: refusal
    # options encrypt the deploy credential (R3.6) and must never touch the
    # live key file.
    import base64
    import json
    import secrets

    provision = tmp_path_factory.mktemp("provision")
    keys_dir = provision / "keys"
    keys_dir.mkdir(mode=0o700)
    (keys_dir / "service_basic.key").write_text(json.dumps({
        "current": "v1",
        "keys": {"v1": base64.b64encode(secrets.token_bytes(32)).decode()},
    }))
    monkeypatch.setenv("PROVISION_DIR", str(provision))
    monkeypatch.setattr(settings, "PROVISION_DIR", provision, raising=False)
    # ...and the RESOLVED setting, not only the environment variable. `settings`
    # is built at import time, so a later `setenv` cannot reach it — which would
    # leave every per-task LOG path pointing at the live gateway_data directory.
    monkeypatch.setattr(settings, "GATEWAY_DATA_DIR", data_dir, raising=False)

    # The manager is a module-level SINGLETON whose id-keyed bookkeeping
    # (in-flight asyncio tasks, cancels, throttle stamps) would otherwise
    # outlive the rows it describes between tests.
    from app.services.generation_jobs import generation_jobs
    generation_jobs._tasks.clear()
    generation_jobs._cancelled.clear()
    generation_jobs._cancel_polled.clear()

    return data_dir
