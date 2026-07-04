#!/bin/bash
# Integration test script for provision-gateway
# Tests the gateway API against the running provision-api stack.
set -e

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8770}"
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
    -d '{"email":"admin@example.com","password":"admin123"}')
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

# ---- Auth: Login ----
echo ""
echo "3. Auth Login"
LOGIN_RESP=$(curl -s -X POST "$GATEWAY_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@example.com","password":"admin123"}')
check "Login returns access_token" '"access_token"' "$LOGIN_RESP"

# Extract token
TOKEN=$(echo "$LOGIN_RESP" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
if [ -z "$TOKEN" ]; then
    echo "  ✗ Could not extract token"
    exit 1
fi
echo "  Token obtained: ${TOKEN:0:20}..."

# ---- Auth: Me ----
echo ""
echo "4. Auth Me"
ME_RESP=$(curl -s "$GATEWAY_URL/api/auth/me" -H "Authorization: Bearer $TOKEN")
check "GET /me returns email" '"email"' "$ME_RESP"

# ---- Users ----
echo ""
echo "5. Users Proxy"
USERS_RESP=$(curl -s "$GATEWAY_URL/api/users" -H "Authorization: Bearer $TOKEN")
check "GET /users returns users" '"users"' "$USERS_RESP"

# ---- Tasks ----
echo ""
echo "6. Tasks Proxy"
TASKS_RESP=$(curl -s "$GATEWAY_URL/api/tasks" -H "Authorization: Bearer $TOKEN")
check "GET /tasks returns tasks" '"tasks"' "$TASKS_RESP"

# ---- Audit ----
echo ""
echo "7. Audit Logs"
AUDIT_RESP=$(curl -s "$GATEWAY_URL/api/audit" -H "Authorization: Bearer $TOKEN")
check "GET /audit returns entries" '"entries"' "$AUDIT_RESP"

# ---- System ----
echo ""
echo "8. System Status"
SYS_RESP=$(curl -s "$GATEWAY_URL/api/system/status" -H "Authorization: Bearer $TOKEN")
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

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
echo "All tests passed! ✓"
