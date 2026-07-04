"""Integration test script for provision-gateway.

Tests the gateway API endpoints against the running provision-api stack.
"""

import subprocess
import sys
import json
import time
import os

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8770")
PROVISION_API_URL = os.environ.get("PROVISION_API_URL", "http://localhost:8765")

def run_curl(method, path, data=None, token=None):
    """Make a curl request and return (status_code, response_body)."""
    url = f"{GATEWAY_URL}{path}"
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, url]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    if token:
        cmd.extend(["-H", f"Authorization: Bearer {token}"])
    
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


def test_auth_flow():
    """Test full auth flow: setup, login, refresh, me."""
    print("\nTesting auth flow...")
    
    # Clean up: login to check if admin exists
    login_data = {"email": "gw-test@example.com", "password": "testpass123"}
    
    # Try setup
    status, body = run_curl("POST", "/api/auth/setup", login_data)
    if status == 409:
        print("  Admin already exists, using existing credentials")
        # Use the existing admin
        login_data = {"email": "admin@example.com", "password": "admin123"}
    elif status == 201:
        print("  Created test admin")
    else:
        print(f"  Setup response: {status} {body}")
    
    # Login
    status, body = run_curl("POST", "/api/auth/login", login_data)
    assert status == 200, f"Login failed: {body}"
    token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    assert token is not None
    assert body.get("admin") is not None
    print("  ✓ Login successful")
    
    # Get me
    status, me = run_curl("GET", "/api/auth/me", token=token)
    assert status == 200
    assert me.get("email") is not None
    print("  ✓ GET /me works")
    
    # Refresh token
    status, refresh_body = run_curl("POST", "/api/auth/refresh", {"refresh_token": refresh_token})
    assert status == 200
    assert refresh_body.get("access_token") is not None
    print("  ✓ Token refresh works")
    
    return token


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
        for test_fn in [test_users_proxy, test_tasks_proxy, test_audit_logs, test_system_status]:
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
