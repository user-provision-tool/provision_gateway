#!/bin/bash
# Integration test script for provision-gateway
# Tests the gateway API against the running provision-api stack.
#
# v4 auth (F4/N5): login sets a HttpOnly provision_token cookie; there is no
# access_token/refresh_token in the body. All authenticated calls use a cookie
# jar (-c to save on login, -b to send on each request).
set -e

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8870}"
COOKIE_JAR="$(mktemp)"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (expected: $expected, got: $actual)"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "Provision Gateway Integration Test (Shell)"
echo "Gateway: $GATEWAY_URL"
echo "============================================"

# ---- Health ----
echo ""
echo "1. Health Check"
HEALTH=$(curl -s "$GATEWAY_URL/health")
check "Health returns ok" '"status":"ok"' "$HEALTH"

# ---- Auth: Setup ----
echo ""
echo "2. Auth Setup"
SETUP_RESP=$(curl -s -X POST "$GATEWAY_URL/api/auth/setup" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@subnet-acl.local","password":"admin-pass-123"}')
# May return 409 if already exists - both are acceptable
if echo "$SETUP_RESP" | grep -q '"message"'; then
    echo "  ✓ Setup completed"
    PASS=$((PASS + 1))
elif echo "$SETUP_RESP" | grep -q '"detail"'; then
    echo "  - Setup skipped (admin exists)"
    PASS=$((PASS + 1))
else
    echo "  ✗ Setup failed: $SETUP_RESP"
    FAIL=$((FAIL + 1))
fi

# ---- Auth: Login (v4: provision_token cookie) ----
echo ""
echo "3. Auth Login"
LOGIN_RESP=$(curl -s -c "$COOKIE_JAR" -X POST "$GATEWAY_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@subnet-acl.local","password":"admin-pass-123"}')
check "Login returns cookie auth" '"token_type":"cookie"' "$LOGIN_RESP"

# Verify the cookie jar actually contains provision_token
if grep -q "provision_token" "$COOKIE_JAR"; then
    echo "  ✓ provision_token cookie saved"
    PASS=$((PASS + 1))
else
    echo "  ✗ no provision_token cookie in jar"
    FAIL=$((FAIL + 1))
fi

# ---- Auth: Me ----
echo ""
echo "4. Auth Me"
ME_RESP=$(curl -s -b "$COOKIE_JAR" "$GATEWAY_URL/api/auth/me")
check "GET /me returns email" '"email"' "$ME_RESP"

# ---- Users ----
echo ""
echo "5. Users Proxy"
USERS_RESP=$(curl -s -b "$COOKIE_JAR" "$GATEWAY_URL/api/users")
check "GET /users returns users" '"users"' "$USERS_RESP"

# ---- Tasks ----
echo ""
echo "6. Tasks Proxy"
TASKS_RESP=$(curl -s -b "$COOKIE_JAR" "$GATEWAY_URL/api/tasks")
check "GET /tasks returns tasks" '"tasks"' "$TASKS_RESP"

# ---- Audit ----
echo ""
echo "7. Audit Logs"
AUDIT_RESP=$(curl -s -b "$COOKIE_JAR" "$GATEWAY_URL/api/audit")
check "GET /audit returns entries" '"entries"' "$AUDIT_RESP"

# ---- System ----
echo ""
echo "8. System Status"
SYS_RESP=$(curl -s -b "$COOKIE_JAR" "$GATEWAY_URL/api/system/status")
check "GET /system/status returns gateway" '"gateway"' "$SYS_RESP"

# ---- Unauthorized ----
echo ""
echo "9. Unauthorized Access"
UNAUTH_RESP=$(curl -s -w "\n%{http_code}" "$GATEWAY_URL/api/users" 2>/dev/null)
check "Unauthorized returns 401" "401" "$UNAUTH_RESP"

# ---- Summary ----
echo ""
echo "============================================"
echo "Results: $PASS passed, $FAIL failed"
echo "============================================"

rm -f "$COOKIE_JAR"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
echo "All tests passed! ✓"
