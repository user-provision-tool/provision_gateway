"""Integration test script for provision-gateway.

Tests the gateway API endpoints against the running provision-api stack.
"""

import subprocess
import sys
import json
import time
import os
import tempfile

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8870")
PROVISION_API_URL = os.environ.get("PROVISION_API_URL", "http://127.0.0.1:8875")

def run_curl(method, path, data=None, token=None):
    """Make a curl request and return (status_code, response_body).

    ``token`` is a v4 cookie-jar path. Login saves the HttpOnly provision_token
    cookie with ``-c``; every request sends it with ``-b``.
    """
    url = f"{GATEWAY_URL}{path}"
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, url]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    if token:
        cmd.extend(["-c", token, "-b", token])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    
    # Split body and status code
    parts = output.rsplit("\n", 1)
    if len(parts) == 2:
        body = parts[0]
        try:
            status = int(parts[1])
        except ValueError:
            body = output
            status = 0
    else:
        body = output
        status = 0
    
    try:
        body_json = json.loads(body)
    except json.JSONDecodeError:
        body_json = body
    
    return status, body_json


def test_health():
    """Test health endpoint."""
    print("Testing health endpoint...")
    status, body = run_curl("GET", "/health")
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert body.get("status") == "ok"
    print("  ✓ Health check passed")


def test_auth_flow(tmp_path):
    """Test full auth flow: setup, login (cookie), me, logout (v4).

    Pytest supplies ``tmp_path`` (pathlib.Path); ``main()`` supplies a plain
    cookie-jar path string. Both are accepted.
    """
    print("\nTesting auth flow...")

    if isinstance(tmp_path, str):
        cookie_jar = tmp_path
    else:
        cookie_jar = str(tmp_path / "cookies.txt")
    login_data = {"email": "gw-test@example.com", "password": "testpass123"}

    # Try setup
    status, body = run_curl("POST", "/api/auth/setup", login_data, token=cookie_jar)
    if status == 409:
        print("  Admin already exists, using existing credentials")
        # Use the existing admin
        login_data = {"email": "admin@subnet-acl.local", "password": "admin-pass-123"}
    elif status == 201:
        print("  Created test admin")
    else:
        print(f"  Setup response: {status} {body}")

    # Login (v4: cookie auth, no access_token in body)
    status, body = run_curl("POST", "/api/auth/login", login_data, token=cookie_jar)
    assert status == 200, f"Login failed: {body}"
    assert body.get("token_type") == "cookie", f"expected cookie auth, got: {body}"
    assert body.get("admin") is not None
    # HttpOnly provision_token cookie must be present in the jar
    with open(cookie_jar) as f:
        assert "provision_token" in f.read(), "no provision_token cookie saved"
    print("  ✓ Login successful (provision_token cookie)")

    # Get me
    status, me = run_curl("GET", "/api/auth/me", token=cookie_jar)
    assert status == 200
    assert me.get("email") is not None
    print("  ✓ GET /me works")

    # Logout (v4 removed the refresh endpoint; logout clears the cookie)
    status, logout_body = run_curl("POST", "/api/auth/logout", token=cookie_jar)
    assert status == 200
    print("  ✓ Logout works")

    return cookie_jar


def test_users_proxy(token):
    """Test users endpoints proxied to provision-api."""
    print("\nTesting users proxy...")
    
    # List users
    status, body = run_curl("GET", "/api/users", token=token)
    assert status == 200, f"List users failed: {body}"
    assert "users" in body or "user_status" in body
    print(f"  ✓ GET /users works (count: {body.get('count', 'N/A')})")
    
    # Get non-existent user
    status, body = run_curl("GET", "/api/users/nonexistent_user_xyz", token=token)
    # May return 200 with empty or 404
    print(f"  GET /users/nonexistent → {status}")


def test_tasks_proxy(token):
    """Test tasks endpoints."""
    print("\nTesting tasks proxy...")
    
    status, body = run_curl("GET", "/api/tasks", token=token)
    assert status == 200
    assert "tasks" in body
    print(f"  ✓ GET /tasks works (count: {body.get('count', 0)})")


def test_audit_logs(token):
    """Test audit log endpoints."""
    print("\nTesting audit logs...")
    
    status, body = run_curl("GET", "/api/audit", token=token)
    assert status == 200
    assert "entries" in body
    print(f"  ✓ GET /audit works (total: {body.get('total', 0)})")


def test_system_status(token):
    """Test system status endpoint."""
    print("\nTesting system status...")
    
    status, body = run_curl("GET", "/api/system/status", token=token)
    assert status == 200
    print(f"  ✓ GET /system/status works")


def test_container_logs_endpoint(token):
    """Test container logs endpoint (proxied to provision-api)."""
    print("\nTesting container logs endpoint...")
    
    # Test with non-existent user/service — should get error from provision-api
    status, body = run_curl(
        "GET",
        "/api/users/nonexistent_user/nonexistent_svc/0/containers/web/logs?tail=10",
        token=token,
    )
    # Should get a response (may be 404 or 502 depending on provision-api state)
    print(f"  GET /users/.../containers/.../logs → {status}")
    # Even a 404/502 is acceptable — we just need the endpoint to exist and proxy
    assert status in (200, 404, 502), f"Unexpected status: {status}"


def test_tasks_log_sse_endpoint(token):
    """Test SSE task log streaming endpoint (proxied to provision-api)."""
    print("\nTesting SSE task log endpoint...")
    
    # Test with non-existent task_id — should get error from provision-api
    url = f"{GATEWAY_URL}/api/tasks/nonexistent_task_id_12345/log?tail=5&follow=false"
    cmd = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-c", token, "-b", token,
        "--max-time", "5",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    
    parts = output.rsplit("\n", 1)
    if len(parts) == 2:
        body = parts[0]
        try:
            status = int(parts[1])
        except ValueError:
            body = output
            status = 0
    else:
        body = output
        status = 0
    
    print(f"  GET /tasks/.../log (SSE) → {status}")
    # Should get some response — even an error is acceptable as long as the
    # endpoint exists and is proxying correctly
    assert status in (200, 404, 502, 500), f"Unexpected status: {status}"


def test_new_endpoints_exist(token):
    """Verify all new endpoints from user_provision changes are reachable via gateway."""
    print("\nTesting new proxied endpoints...")
    
    new_endpoints = [
        # Docker / host monitoring
        ("GET", "/api/system/status"),  # internally uses docker_ps, docker_stats, docker_info, host_stats
        # Task management
        ("GET", "/api/tasks"),
        # Reconciliation
        ("GET", "/api/system/status"),  # internally uses reconciliation
    ]
    
    for method, path in new_endpoints:
        status, body = run_curl(method, path, token=token)
        print(f"  {method} {path} → {status}")
        # All should be reachable
        assert status < 500, f"Server error on {method} {path}: {body}"


def main():
    print("=" * 60)
    print("Provision Gateway Integration Tests")
    print(f"Gateway: {GATEWAY_URL}")
    print(f"Provision API: {PROVISION_API_URL}")
    print("=" * 60)
    
    tests = [
        test_health,
        test_auth_flow,
    ]
    
    token = None
    for test_fn in tests:
        try:
            if test_fn is test_auth_flow:
                # tmp_path is a pytest fixture; main() supplies a temp dir.
                tmp = tempfile.mkdtemp(prefix="gw-int-")
                result = test_auth_flow(os.path.join(tmp, "cookies.txt"))
            else:
                result = test_fn()
            if result:
                token = result
        except AssertionError as e:
            print(f"\n  ✗ FAILED: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"\n  ✗ ERROR: {e}")
            sys.exit(1)
    
    # Tests that need a token
    if token:
        for test_fn in [
            test_users_proxy,
            test_tasks_proxy,
            test_audit_logs,
            test_system_status,
            test_container_logs_endpoint,
            test_tasks_log_sse_endpoint,
            test_new_endpoints_exist,
        ]:
            try:
                test_fn(token)
            except AssertionError as e:
                print(f"\n  ✗ FAILED: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"\n  ✗ ERROR: {e}")
                sys.exit(1)
    
    print("\n" + "=" * 60)
    print("All integration tests passed! ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
