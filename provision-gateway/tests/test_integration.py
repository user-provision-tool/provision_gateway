"""Live-gateway integration tests (current contracts, 2026-11-02).

These tests drive a RUNNING gateway over HTTP and therefore are not unit
tests. They run wherever a test gateway exists (the test environment); they do
NOT create users, services or tasks and do NOT modify any existing row.

Data rule (strict): the only writes are the side effects of the suite's own
authentication — the login sets its own cookie jar and audit rows record the
suite's own login/logout; everything else is a read, or a refusal on a
deliberately nonexistent id. The test identity is provisioned outside the
suite (bootstrap/operator) and passed in:

    GATEWAY_TEST_USER=<username-or-email>
    GATEWAY_TEST_PASSWORD=<password>
    GATEWAY_URL=http://127.0.0.1:8870        # the gateway under test
    PROVISION_API_URL=http://127.0.0.1:8875  # printed only

The suite skips only when credentials are absent or the gateway is not
answering /health; a live gateway plus credentials always runs it.
"""

import json
import os
import subprocess

import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8870")
PROVISION_API_URL = os.environ.get("PROVISION_API_URL", "http://127.0.0.1:8875")
TEST_USER = os.environ.get("GATEWAY_TEST_USER", "")
TEST_PASSWORD = os.environ.get("GATEWAY_TEST_PASSWORD", "")


def run_curl(method, path, data=None, cookie_jar=None, max_time=None):
    """Make a request and return (status_code, parsed_body).

    ``cookie_jar`` is a v4 cookie-jar path. Login saves the HttpOnly
    provision_token cookie with ``-c``; every request sends it with ``-b``.
    """
    url = f"{GATEWAY_URL}{path}"
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, url]
    if data is not None:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    if cookie_jar:
        cmd.extend(["-c", cookie_jar, "-b", cookie_jar])
    if max_time:
        cmd.extend(["--max-time", str(max_time)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    parts = output.rsplit("\n", 1)
    if len(parts) == 2:
        body, raw_status = parts
        try:
            status = int(raw_status)
        except ValueError:
            body, status = output, 0
    else:
        body, status = output, 0
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = body
    return status, parsed


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Log in once; the cookie jar lives in the test's own tmp dir."""
    jar = str(tmp_path_factory.mktemp("it") / "cookies.txt")
    status, body = run_curl(
        "POST", "/api/auth/login",
        {"email": TEST_USER, "password": TEST_PASSWORD}, cookie_jar=jar,
    )
    assert status == 200, f"login failed for {TEST_USER!r}: {status} {body}"
    with open(jar) as fh:
        assert "provision_token" in fh.read(), "no provision_token cookie saved"
    return jar


def test_health():
    """Unauthenticated liveness: the gateway reports its database healthy."""
    status, body = run_curl("GET", "/health")
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body.get("status") == "ok", body


def test_unauthenticated_denied():
    """An admin surface without a cookie is refused, never silently served."""
    status, body = run_curl("GET", "/api/tasks")
    assert status == 401, (status, body)


def test_auth_login_me_logout(client):
    """Login (cookie), me, logout — the current unified-user contract.

    The response carries ``user`` (not the retired ``admin``); ``me`` exposes
    id/username/role. No ``/setup`` or ``/register`` call is made: those routes
    are retired (410) and user creation belongs to the admin API.
    """
    status, me = run_curl("GET", "/api/auth/me", cookie_jar=client)
    assert status == 200, me
    assert me.get("id") and me.get("username"), me
    assert me.get("role") in ("viewer", "admin"), me

    status, out = run_curl("POST", "/api/auth/logout", cookie_jar=client)
    assert status == 200, out
    # the suite logs back in for the remaining tests (its own cookie jar)
    status, body = run_curl(
        "POST", "/api/auth/login",
        {"email": TEST_USER, "password": TEST_PASSWORD}, cookie_jar=client,
    )
    assert status == 200, body


def test_admin_users_list(client):
    """GET /api/auth/users returns the unified user list (admin only)."""
    status, body = run_curl("GET", "/api/auth/users", cookie_jar=client)
    assert status == 200, body
    assert isinstance(body.get("users"), list), body
    names = {u.get("username") for u in body["users"]}
    assert TEST_USER.split("@")[0] in names or TEST_USER in names, (
        "the test identity must appear in the user list", names,
    )


def test_services_cards_view(client):
    """GET /api/services is the per-user cards view (users + count [+ orphans])."""
    status, body = run_curl("GET", "/api/services", cookie_jar=client)
    assert status == 200, body
    assert isinstance(body.get("users"), list), body
    assert body.get("count") == len(body["users"]), body


def test_source_projects_list(client):
    """GET /api/projects is the Source Projects recipe list."""
    status, body = run_curl("GET", "/api/projects", cookie_jar=client)
    assert status == 200, body
    assert isinstance(body.get("services"), list), body


def test_tasks_list(client):
    """GET /api/tasks returns the unified gt-/api- read model."""
    status, body = run_curl("GET", "/api/tasks", cookie_jar=client)
    assert status == 200, body
    assert isinstance(body.get("tasks"), list), body
    assert body.get("count") == len(body["tasks"]), body


def test_audit_logs(client):
    """GET /api/audit returns the audit page shape."""
    status, body = run_curl("GET", "/api/audit?limit=5", cookie_jar=client)
    assert status == 200, body
    assert isinstance(body.get("entries"), list), body
    assert "total" in body, body


def test_system_status(client):
    """GET /api/system/status answers with the aggregate status."""
    status, body = run_curl("GET", "/api/system/status", cookie_jar=client)
    assert status == 200, body


def test_container_logs_unknown_service(client):
    """The container-logs route exists and proxies; an unknown target is an
    explicit error (404 from the -api or 502 from the proxy), never a 500."""
    status, body = run_curl(
        "GET",
        "/api/services/it_nonexistent_user/it_nonexistent_svc/0/containers/web/logs?tail=10",
        cookie_jar=client,
    )
    assert status in (404, 502), (status, body)


def test_task_log_unknown_task(client):
    """An unknown task's log is 404 (the task-route status passthrough)."""
    status, body = run_curl(
        "GET", "/api/tasks/it-nonexistent-task/log?follow=false", cookie_jar=client,
    )
    assert status == 404, (status, body)


def test_instance_actions_on_bogus_id_are_404(client):
    """Card actions on a nonexistent instance are 404, not 502.

    This is the only lifecycle surface the suite touches, and it targets an id
    that never exists — no row is created, read or modified.
    """
    bogus = "00000000-0000-0000-0000-000000000000"
    cases = [
        ("POST", f"/api/services/instances/{bogus}/stop"),
        ("POST", f"/api/services/instances/{bogus}/start"),
        ("POST", f"/api/services/instances/{bogus}/rebuild"),
        ("DELETE", f"/api/services/instances/{bogus}?scope=service"),
    ]
    for method, path in cases:
        status, body = run_curl(method, path, cookie_jar=client)
        assert status == 404, (method, path, status, body)
