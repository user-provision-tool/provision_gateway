#!/bin/bash
# ============================================================================
# test_gateway_api.sh — Functionality tests for provision-gateway
#
# Tests the gateway API at http://localhost:8771/api
# Requires valid admin credentials.
# ============================================================================

set -e
API="http://localhost:8771/api"
ADMIN_EMAIL="admin@example.com"
ADMIN_PASS="admin123"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  ✅ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $desc (expected '$expected' not found)"
        echo "     got: $(echo "$actual" | head -c 200)"
        FAIL=$((FAIL + 1))
    fi
}

check_not() {
    local desc="$1"
    local unexpected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$unexpected"; then
        echo "  ❌ $desc (found unexpected '$unexpected')"
        FAIL=$((FAIL + 1))
    else
        echo "  ✅ $desc"
        PASS=$((PASS + 1))
    fi
}

echo "============================================"
echo " Provision-Gateway Functionality Tests"
echo " Target: $API"
echo "============================================"
echo ""

# ─── 0. Get Auth Token ──────────────────────────────────────────────────────

echo "── 0. Authentication ──"

echo -n "  0.1 POST /auth/login (admin): "
RESP=$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASS\"}")
TOKEN=$(echo "$RESP" | docker exec -i provision-gateway python -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$TOKEN" ]; then
    echo "  ✅ got token"
    PASS=$((PASS + 1))
else
    echo "  ❌ failed to get token"
    FAIL=$((FAIL + 1))
    exit 1
fi

AUTH="Authorization: Bearer $TOKEN"

echo -n "  0.2 GET /auth/me: "
RESP=$(curl -s "$API/auth/me" -H "$AUTH")
check "shows admin email" "$ADMIN_EMAIL" "$RESP"

echo -n "  0.3 POST /auth/login (bad password should fail): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"wrong"}')
[ "$HTTP_CODE" = "401" ] && echo "  ✅ returns 401" && PASS=$((PASS + 1)) || { echo "  ❌ expected 401, got $HTTP_CODE"; FAIL=$((FAIL + 1)); }

echo -n "  0.4 POST /auth/refresh: "
REFRESH_TOKEN=$(echo "$RESP" | docker exec -i provision-gateway python -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))" 2>/dev/null)
if [ -z "$REFRESH_TOKEN" ]; then
    # Get refresh token from login response
    LOGIN_RESP=$(curl -s -X POST "$API/auth/login" -H "Content-Type: application/json" -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASS\"}")
    REFRESH_TOKEN=$(echo "$LOGIN_RESP" | docker exec -i provision-gateway python -c "import sys,json; print(json.load(sys.stdin).get('refresh_token',''))" 2>/dev/null)
fi
REFRESH_RESP=$(curl -s -X POST "$API/auth/refresh" -H "Content-Type: application/json" -d "{\"refresh_token\":\"$REFRESH_TOKEN\"}")
check "returns new access_token" '"access_token"' "$REFRESH_RESP"

echo -n "  0.5 PUT /auth/password (change own password): "
RESP=$(curl -s -X PUT "$API/auth/password" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"current_password\":\"$ADMIN_PASS\",\"new_password\":\"$ADMIN_PASS\"}")
check "password message" "message" "$RESP"

echo -n "  0.6 Unauthenticated access (should 401): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API/users")
[ "$HTTP_CODE" = "401" ] && echo "  ✅ returns 401" && PASS=$((PASS + 1)) || { echo "  ❌ expected 401, got $HTTP_CODE"; FAIL=$((FAIL + 1)); }


# ─── 1. System ──────────────────────────────────────────────────────────────

echo "── 1. System Status ──"

echo -n "  1.1 GET /system/status: "
RESP=$(curl -s "$API/system/status" -H "$AUTH")
check "provision_api status" '"provision_api"' "$RESP"
check "components" '"components"' "$RESP"
check "docker_host" '"docker_host"' "$RESP"

echo -n "  1.2 GET /system/stats: "
RESP=$(curl -s "$API/system/stats" -H "$AUTH")
check "containers list" '"containers"' "$RESP"

echo -n "  1.3 GET /system/stats?detail=true: "
RESP=$(curl -s "$API/system/stats?detail=true" -H "$AUTH")
check "host stats" '"host"' "$RESP"


# ─── 2. Source Projects (Services) ──────────────────────────────────────────

echo "── 2. Source Projects ──"

echo -n "  2.1 GET /services (list): "
RESP=$(curl -s "$API/services" -H "$AUTH")
check "has siyuan project" "siyuan" "$RESP"
check "has siyuan-mcp project" "siyuan-mcp" "$RESP"

echo -n "  2.2 GET /services/siyuan (detail): "
RESP=$(curl -s "$API/services/siyuan" -H "$AUTH")
check "has files" '"files"' "$RESP"
check "has name" '"name"' "$RESP"


# ─── 3. User Services (Deployed) ────────────────────────────────────────────

echo "── 3. Deployed Services ──"

echo -n "  3.1 GET /users (list): "
RESP=$(curl -s "$API/users" -H "$AUTH")
check "returns users" '"users"' "$RESP"

# Check if alice exists (from earlier deploys)
if echo "$RESP" | grep -q "alice"; then
    echo "  ✅ alice has services"
    PASS=$((PASS + 1))
else
    echo "  ⚠️ alice not found (may have been cleaned up)"
fi

echo -n "  3.2 POST /users/deploy (deploy siyuan-mcp for testuser): "
RESP=$(curl -s -X POST "$API/users/deploy" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "user_name": "testuser",
    "service_name": "siyuan-mcp",
    "project_root": "siyuan-mcp",
    "compose_template_path": "docker-compose.yml.j2",
    "nginx_conf_template_path": "nginx.conf.j2",
    "label": "1",
    "domain": "snaprovision.com",
    "passwd": "test123",
    "https": false
  }')
check "returns task_id" '"task_id"' "$RESP"

echo "     (waiting for deploy...)"
sleep 12


# ─── 4. Up / Down / Password (delegated to provision-api) ───────────────────

echo "── 4. Service Lifecycle (via gateway → provision-api) ──"

echo -n "  4.1 POST /users/testuser/siyuan-mcp/1/down (stop): "
RESP=$(curl -s -X POST "$API/users/testuser/siyuan-mcp/1/down" -H "$AUTH")
check "returns stopped" '"down"' "$RESP"
sleep 2

echo -n "  4.2 POST /users/testuser/siyuan-mcp/1/up (start): "
RESP=$(curl -s -X POST "$API/users/testuser/siyuan-mcp/1/up" -H "$AUTH")
check "returns started" '"up"' "$RESP"
sleep 2

echo -n "  4.3 PUT /users/testuser/siyuan-mcp/1/password (change): "
RESP=$(curl -s -X PUT "$API/users/testuser/siyuan-mcp/1/password" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"passwd": "newpass789"}')
check "password updated" "Password updated" "$RESP"


# ─── 5. Tasks ───────────────────────────────────────────────────────────────

echo "── 5. Tasks ──"

echo -n "  5.1 GET /tasks (list): "
RESP=$(curl -s "$API/tasks" -H "$AUTH")
check "returns tasks" '"tasks"' "$RESP"


# ─── 6. Audit ───────────────────────────────────────────────────────────────

echo "── 6. Audit ──"

echo -n "  6.1 GET /audit: "
RESP=$(curl -s "$API/audit" -H "$AUTH")
check "returns entries" '"entries"' "$RESP"

echo -n "  6.2 GET /audit?action=register: "
RESP=$(curl -s "$API/audit?action=register&limit=5" -H "$AUTH")
check "filter works" '"entries"' "$RESP"

echo -n "  6.3 GET /audit?action=start: "
RESP=$(curl -s "$API/audit?action=start&limit=5" -H "$AUTH")
check "start actions logged" '"entries"' "$RESP"


# ─── 7. LLM Config (if configured) ──────────────────────────────────────────

echo "── 7. LLM Configuration ──"

echo -n "  7.1 GET /llm/configs: "
RESP=$(curl -s "$API/llm/configs" -H "$AUTH")
check "returns configs" '"configs"' "$RESP"

echo -n "  7.2 GET /llm/config: "
RESP=$(curl -s "$API/llm/config" -H "$AUTH")
check "returns config" '"mode"' "$RESP"


# ─── 8. End-User Management ─────────────────────────────────────────────────

echo "── 8. End-User Management ──"

echo -n "  8.1 GET /auth/users: "
RESP=$(curl -s "$API/auth/users" -H "$AUTH")
check "returns users list" '"users"' "$RESP"

echo -n "  8.2 GET /auth/users/deployable: "
RESP=$(curl -s "$API/auth/users/deployable" -H "$AUTH")
check "returns deployable users" '"users"' "$RESP"

echo -n "  8.3 POST /auth/users/register (new end-user): "
RESP=$(curl -s -X POST "$API/auth/users/register" -H "Content-Type: application/json" \
  -d '{"username":"gatewaytest","password":"test123","role":"viewer"}')
check "user created" '"created"' "$RESP"

# Login as end-user to verify end-user auth works
echo -n "  8.4 Login as gatewaytest (unapproved should fail): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"gatewaytest","password":"test123"}')
[ "$HTTP_CODE" = "401" ] && echo "  ✅ unapproved user rejected (401)" && PASS=$((PASS + 1)) || { echo "  ❌ expected 401, got $HTTP_CODE"; FAIL=$((FAIL + 1)); }

# Approve and test login
GATEWAYTEST_ID=$(echo "$RESP" | docker exec -i provision-gateway python -c "import sys,json; print(json.load(sys.stdin).get('user',{}).get('id',''))" 2>/dev/null)
if [ -n "$GATEWAYTEST_ID" ]; then
    echo -n "  8.5 Approve gatewaytest: "
    APPROVE_RESP=$(curl -s -X PUT "$API/auth/users/$GATEWAYTEST_ID/approve" -H "$AUTH")
    check "approved" '"approved"' "$APPROVE_RESP"

    echo -n "  8.6 Login as gatewaytest (approved): "
    END_RESP=$(curl -s -X POST "$API/auth/login" -H "Content-Type: application/json" \
      -d '{"email":"gatewaytest","password":"test123"}')
    check "end-user login works" '"access_token"' "$END_RESP"
    check "user_type is end_user" '"end_user"' "$END_RESP"
fi


# ─── 9. Proxy (if configured) ───────────────────────────────────────────────

echo "── 9. Proxy ──"

echo -n "  9.1 GET /system/proxy: "
RESP=$(curl -s "$API/system/proxy" -H "$AUTH")
check "returns configs" '"configs"' "$RESP"


# ─── 10. Cleanup ────────────────────────────────────────────────────────────

echo "── 10. Cleanup ──"

echo -n "  10.1 DELETE /users/testuser/siyuan-mcp/1: "
RESP=$(curl -s -X DELETE "$API/users/testuser/siyuan-mcp/1" -H "$AUTH")
check "removal queued" '"task_id"' "$RESP"

echo -n "  10.2 DELETE /auth/users/$GATEWAYTEST_ID (remove test end-user): "
if [ -n "$GATEWAYTEST_ID" ]; then
    RESP=$(curl -s -X DELETE "$API/auth/users/$GATEWAYTEST_ID" -H "$AUTH")
    check "user deleted" '"deleted"' "$RESP"
else
    echo "  ⚠️ skipping (no test user id)"
fi

sleep 5


# ─── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo " Results: $PASS passed, $FAIL failed"
echo "============================================"

[ "$FAIL" -eq 0 ] && echo "✅ All gateway tests passed!" || echo "❌ Some tests failed!"
exit $FAIL
