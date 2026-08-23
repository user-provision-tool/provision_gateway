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
