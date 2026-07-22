"""Pytest fixtures for provision-gateway tests."""

import os
import pytest
import subprocess

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8770")

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
def token():
    """Fixture that returns a JWT token for authenticated tests.

    Skips tests if the gateway is not running.
    """
    if not _is_gateway_running():
        pytest.skip("Gateway is not running — skipping integration test")

    # Try to get a token by logging in as admin
    admin_email = os.environ.get("GATEWAY_ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "admin123")

    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         f"{GATEWAY_URL}/api/auth/login",
         "-H", "Content-Type: application/json",
         "-d", f'{{"email":"{admin_email}","password":"{admin_password}"}}'],
        capture_output=True, text=True, timeout=10,
    )

    import json
    try:
        data = json.loads(result.stdout)
        token_val = data.get("access_token", "")
        if token_val:
            return token_val
    except json.JSONDecodeError:
        pass

    pytest.skip("Could not obtain auth token — gateway may not be set up yet")
    return ""  # unreachable but satisfies type checker
