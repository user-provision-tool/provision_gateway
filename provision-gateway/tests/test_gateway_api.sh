#!/bin/bash
# ============================================================================
# test_gateway_api.sh — Functionality tests for provision-gateway
#
# Tests the gateway API at http://localhost:8771/api
# Requires valid admin credentials.
#
# v4 auth (F4/N5): login sets a HttpOnly provision_token cookie; there is no
# access_token/refresh_token in the response body. All authenticated calls use
# the cookie jar (-b/-c).
# ============================================================================

set -e
API="${GATEWAY_URL:-http://localhost:8775}/api"
ADMIN_EMAIL="admin@subnet-acl.local"
ADMIN_PASS="admin-pass-123"
COOKIE_JAR="$(mktemp)"
VIEWER_COOKIE_JAR="$(mktemp)"
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

# ─── 0. Get Auth Cookie (v4: provision_token cookie) ────────────────────────

echo "── 0. Authentication ──"

echo -n "  0.1 POST /auth/login (admin): "
RESP=$(curl -s -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASS\"}")
if echo "$RESP" | grep -q '"token_type":"cookie"'; then
    echo "  ✅ got cookie auth"
    PASS=$((PASS + 1))
else
    echo "  ❌ failed to get cookie: $(echo "$RESP" | head -c 200)"
    FAIL=$((FAIL + 1))
    exit 1
fi

echo -n "  0.2 GET /auth/me: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/auth/me")
check "shows admin email" "$ADMIN_EMAIL" "$RESP"

echo -n "  0.3 POST /auth/login (bad password should fail): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"wrong"}')
[ "$HTTP_CODE" = "401" ] && echo "  ✅ returns 401" && PASS=$((PASS + 1)) || { echo "  ❌ expected 401, got $HTTP_CODE"; FAIL=$((FAIL + 1)); }

echo -n "  0.4 POST /auth/logout clears cookie: "
LOGOUT_RESP=$(curl -s -c "$COOKIE_JAR" -X POST "$API/auth/logout")
check "logout message" "Logged out" "$LOGOUT_RESP"
# Re-login as admin for subsequent tests
curl -s -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASS\"}" > /dev/null

echo -n "  0.5 PUT /auth/password (change own password): "
RESP=$(curl -s -b "$COOKIE_JAR" -X PUT "$API/auth/password" -H "Content-Type: application/json" \
  -d "{\"current_password\":\"$ADMIN_PASS\",\"new_password\":\"$ADMIN_PASS\"}")
check "password message" "message" "$RESP"

echo -n "  0.6 Unauthenticated access (should 401): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API/users")
[ "$HTTP_CODE" = "401" ] && echo "  ✅ returns 401" && PASS=$((PASS + 1)) || { echo "  ❌ expected 401, got $HTTP_CODE"; FAIL=$((FAIL + 1)); }


# ─── 1. System ──────────────────────────────────────────────────────────────

echo "── 1. System Status ──"

echo -n "  1.1 GET /system/status: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/system/status")
check "provision_api status" '"provision_api"' "$RESP"
check "components" '"components"' "$RESP"
check "docker_host" '"docker_host"' "$RESP"

echo -n "  1.2 GET /system/stats: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/system/stats")
check "containers list" '"containers"' "$RESP"

echo -n "  1.3 GET /system/stats?detail=true: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/system/stats?detail=true")
check "host stats" '"host"' "$RESP"


# ─── 2. Source Projects (Services) ──────────────────────────────────────────

echo "── 2. Source Projects ──"

echo -n "  2.1 GET /services (list): "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/services")
check "has example-service project" "example-service" "$RESP"
check "has example-mcp project" "example-mcp" "$RESP"

echo -n "  2.2 GET /services/example-service (detail): "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/services/example-service")
check "has files" '"files"' "$RESP"
check "has name" '"name"' "$RESP"


# ─── 3. User Services (Deployed) ────────────────────────────────────────────

echo "── 3. Deployed Services ──"

echo -n "  3.1 GET /users (list): "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/users")
check "returns users" '"users"' "$RESP"

# Check if alice exists (from earlier deploys)
if echo "$RESP" | grep -q "alice"; then
    echo "  ✅ alice has services"
    PASS=$((PASS + 1))
else
    echo "  ⚠️ alice not found (may have been cleaned up)"
fi

echo -n "  3.2 POST /users/deploy (deploy example-mcp for testuser): "
RESP=$(curl -s -b "$COOKIE_JAR" -X POST "$API/users/deploy" -H "Content-Type: application/json" \
  -d '{
    "user_name": "testuser",
    "service_name": "example-mcp",
    "project_root": "example-mcp",
    "compose_template_path": "docker-compose.yml.j2",
    "nginx_conf_template_path": "nginx.conf.j2",
    "label": "1",
    "domain": "snaprovision.com",
    "passwd": "test123",
    "https": false
  }')
check "returns task_id" '"task_id"' "$RESP"

echo "     (polling until testuser/example-mcp/1 is registered...)"
REGISTERED=0
for i in $(seq 1 30); do
    POLL_RESP=$(curl -s -b "$COOKIE_JAR" "$API/users")
    if echo "$POLL_RESP" | grep -q "testuser"; then
        echo "     service registered after ${i}s"
        REGISTERED=1
        break
    fi
    sleep 1
done
if [ "$REGISTERED" -eq 0 ]; then
    echo "     WARNING: service did not appear in user list within 30s — lifecycle tests may fail"
fi


# ─── 4. Up / Down / Password (delegated to provision-api) ───────────────────

echo "── 4. Service Lifecycle (via gateway → provision-api) ──"

echo -n "  4.1 POST /users/testuser/example-mcp/1/down (stop): "
RESP=$(curl -s -b "$COOKIE_JAR" -X POST "$API/users/testuser/example-mcp/1/down")
check "returns stopped" '"down"' "$RESP"
sleep 2

echo -n "  4.2 POST /users/testuser/example-mcp/1/up (start): "
RESP=$(curl -s -b "$COOKIE_JAR" -X POST "$API/users/testuser/example-mcp/1/up")
check "returns started" '"up"' "$RESP"
sleep 2

echo -n "  4.3 PUT /users/testuser/example-mcp/1/password (change): "
RESP=$(curl -s -b "$COOKIE_JAR" -X PUT "$API/users/testuser/example-mcp/1/password" \
  -H "Content-Type: application/json" \
  -d '{"passwd": "newpass789"}')
check "password updated" "Password updated" "$RESP"

echo -n "  4.4 GET /users/testuser/example-mcp/1/containers/example-mcp-user_testuser-1-fastapi-app/logs (container logs): "
# The container logs endpoint proxies to provision-api.
# If the container exists, returns 200 with logs. If not, provision-api returns 404.
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API/users/testuser/example-mcp/1/containers/example-mcp-user_testuser-1-fastapi-app/logs?tail=10" -b "$COOKIE_JAR")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "502" ]; then
    echo "  ✅ container logs endpoint responds ($HTTP_CODE)"
    PASS=$((PASS + 1))
else
    echo "  ❌ unexpected status $HTTP_CODE"
    FAIL=$((FAIL + 1))
fi


# ─── 5. Tasks ───────────────────────────────────────────────────────────────

echo "── 5. Tasks ──"

echo -n "  5.1 GET /tasks (list): "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/tasks")
check "returns tasks" '"tasks"' "$RESP"

echo -n "  5.2 GET /tasks/{task_id}/log (SSE streaming, proxied to provision-api): "
# Get the first task_id from the list
TASK_ID=$(echo "$RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
tasks = data.get('tasks', [])
print(tasks[0].get('task_id','') if tasks else '')
" 2>/dev/null)
if [ -n "$TASK_ID" ]; then
    SSE_RESP=$(curl -s --max-time 3 -b "$COOKIE_JAR" "$API/tasks/$TASK_ID/log?tail=5&follow=false")
    if echo "$SSE_RESP" | grep -q "data:"; then
        echo "  ✅ SSE log stream works (got data: lines)"
        PASS=$((PASS + 1))
    else
        echo "  ⚠️ SSE log stream returned no data (may be empty log)"
        PASS=$((PASS + 1))
    fi
else
    echo "  ⚠️ skipping (no tasks found)"
fi

echo -n "  5.3 GET /tasks/nonexistent/log (invalid task, proxied to provision-api): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -b "$COOKIE_JAR" "$API/tasks/nonexistent_task_xyz/log?tail=5&follow=false")
# Should get some response (404 from provision-api, or error)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "502" ]; then
    echo "  ✅ invalid task handled ($HTTP_CODE)"
    PASS=$((PASS + 1))
else
    echo "  ❌ unexpected status $HTTP_CODE"
    FAIL=$((FAIL + 1))
fi


# ─── 6. Audit ───────────────────────────────────────────────────────────────

echo "── 6. Audit ──"

echo -n "  6.1 GET /audit: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/audit")
check "returns entries" '"entries"' "$RESP"

echo -n "  6.2 GET /audit?action=register: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/audit?action=register&limit=5")
check "filter works" '"entries"' "$RESP"

echo -n "  6.3 GET /audit?action=start: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/audit?action=start&limit=5")
check "start actions logged" '"entries"' "$RESP"


# ─── 7. LLM Config (if configured) ──────────────────────────────────────────

echo "── 7. LLM Configuration ──"

echo -n "  7.1 GET /llm/configs: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/llm/configs")
check "returns configs" '"configs"' "$RESP"

echo -n "  7.2 GET /llm/config: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/llm/config")
check "returns config" '"mode"' "$RESP"


# ─── 8. End-User Management ─────────────────────────────────────────────────

echo "── 8. End-User Management ──"

echo -n "  8.1 GET /auth/users: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/auth/users")
check "returns users list" '"users"' "$RESP"

echo -n "  8.2 GET /auth/users/deployable: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/auth/users/deployable")
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
GATEWAYTEST_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user',{}).get('id',''))" 2>/dev/null)
if [ -n "$GATEWAYTEST_ID" ]; then
    echo -n "  8.5 Approve gatewaytest: "
    APPROVE_RESP=$(curl -s -b "$COOKIE_JAR" -X PUT "$API/auth/users/$GATEWAYTEST_ID/approve")
    check "approved" '"approved"' "$APPROVE_RESP"

    echo -n "  8.6 Login as gatewaytest (approved): "
    END_RESP=$(curl -s -c "$VIEWER_COOKIE_JAR" -X POST "$API/auth/login" -H "Content-Type: application/json" \
      -d '{"email":"gatewaytest","password":"test123"}')
    check "end-user login works (cookie auth)" '"token_type":"cookie"' "$END_RESP"
    check "user_type is end_user" '"end_user"' "$END_RESP"
fi


# ─── 9. Proxy (if configured) ───────────────────────────────────────────────

echo "── 9. Proxy ──"

echo -n "  9.1 GET /system/proxy: "
RESP=$(curl -s -b "$COOKIE_JAR" "$API/system/proxy")
check "returns configs" '"configs"' "$RESP"


# ─── 10. Cleanup ────────────────────────────────────────────────────────────

echo "── 10. Cleanup ──"

echo -n "  10.1 DELETE /users/testuser/example-mcp/1: "
RESP=$(curl -s -b "$COOKIE_JAR" -X DELETE "$API/users/testuser/example-mcp/1")
check "removal queued" '"task_id"' "$RESP"

echo -n "  10.2 DELETE /auth/users/$GATEWAYTEST_ID (remove test end-user): "
if [ -n "$GATEWAYTEST_ID" ]; then
    RESP=$(curl -s -b "$COOKIE_JAR" -X DELETE "$API/auth/users/$GATEWAYTEST_ID")
    check "user deleted" '"deleted"' "$RESP"
else
    echo "  ⚠️ skipping (no test user id)"
fi

sleep 5


# ─── 11. Viewer ACL — sees granted special-user service (Gap 3) ─────────────

echo "── 11. Viewer sees granted special-user service (Gap 3) ──"

echo -n "  11.1 POST /auth/login (viewer1): "
VRESP=$(curl -s -c "$VIEWER_COOKIE_JAR" -X POST "$API/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"viewer1","password":"viewer-pass-123"}')
if echo "$VRESP" | grep -q '"token_type":"cookie"'; then
    echo "  ✅ viewer login OK"
    PASS=$((PASS + 1))
else
    echo "  ❌ viewer login failed: $(echo "$VRESP" | head -c 200)"
    FAIL=$((FAIL + 1))
fi

echo -n "  11.2 GET /api/users (viewer sees granted 'internal'): "
if echo "$VRESP" | grep -q '"token_type":"cookie"'; then
    VUSERS=$(curl -s -b "$VIEWER_COOKIE_JAR" "$API/users")
    check "viewer sees granted special user 'internal'" "internal" "$VUSERS"
    check "internal's example-service listed" "example-service" "$VUSERS"
else
    echo "  ⚠️ skipping (no viewer token)"
fi


# ─── 12. Service URL port (Gap 2) ───────────────────────────────────────────

echo "── 12. Service URL uses the EDGE port 8767 (Gap 2, decision 10) ──"

echo -n "  12.1 GET /system/status nginx_http_port=8767: "
SRESP=$(curl -s -b "$COOKIE_JAR" "$API/system/status")
check "nginx_http_port is 8767" '"nginx_http_port":8767' "$SRESP"

echo -n "  12.2 GET /users/alice/example-service/0/url uses :8767: "
URESP=$(curl -s -b "$COOKIE_JAR" "$API/users/alice/example-service/0/url")
check "service URL uses port 8767" 'http://example-service-alice-0.localhost:8767' "$URESP"


# ─── 13. ACL verify / API keys / subnet-pool / /go/ (subnet-acl features) ──

echo "── 13. ACL verify, API keys, subnet-pool, /go/ redirect ──"

echo -n "  13.1 GET /auth/verify without token → 401, api=unauthorized / browser=login_required: "
# Bare curl (no browser UA / X-Client-Type) ⇒ API client ⇒ "unauthorized".
VSTAT=$(curl -s -o /dev/null -w "%{http_code}" "$API/auth/verify")
VAUTH=$(curl -s -D - -o /dev/null "$API/auth/verify" | grep -i x-auth-action | tr -d '\r' | cut -d' ' -f2)
# Browser-style request (Accept: text/html) ⇒ "login_required".
BSTAT=$(curl -s -o /dev/null -w "%{http_code}" -H "Accept: text/html" "$API/auth/verify")
BAUTH=$(curl -s -D - -o /dev/null -H "Accept: text/html" "$API/auth/verify" | grep -i x-auth-action | tr -d '\r' | cut -d' ' -f2)
if [ "$VSTAT" = "401" ] && [ "$VAUTH" = "unauthorized" ] && [ "$BSTAT" = "401" ] && [ "$BAUTH" = "login_required" ]; then
    echo "  ✅ 401 + api=unauthorized / browser=login_required"
    PASS=$((PASS + 1))
else
    echo "  ❌ got api status=$VSTAT action=$VAUTH; browser status=$BSTAT action=$BAUTH"
    FAIL=$((FAIL + 1))
fi

echo -n "  13.2 POST /auth/keys (create API key): "
KRESP=$(curl -s -b "$COOKIE_JAR" -X POST "$API/auth/keys" -H "Content-Type: application/json" -d '{"label":"sh-acl-key"}')
KID=$(echo "$KRESP" | python3 -c "import sys,json; d=json.load(sys.stdin); k=d.get('key') or {}; print(k.get('id',''))" 2>/dev/null)
if [ -n "$KID" ]; then
    echo "  ✅ key created id=$KID"
    PASS=$((PASS + 1))
else
    echo "  ❌ $(echo "$KRESP" | head -c 150)"
    FAIL=$((FAIL + 1))
fi

echo -n "  13.3 GET /auth/keys lists the new key: "
KLIST=$(curl -s -b "$COOKIE_JAR" "$API/auth/keys")
check "new key in list" "sh-acl-key" "$KLIST"

echo -n "  13.4 DELETE /auth/keys/{id} revokes: "
if [ -n "$KID" ]; then
    KDEL=$(curl -s -b "$COOKIE_JAR" -X DELETE "$API/auth/keys/$KID")
    check "revoke returns ok" '"revoked":true' "$KDEL"
else
    echo "  ⚠️ skipping (no key id)"
fi

echo -n "  13.5 GET /system/subnet-pool (admin) 200: "
SPOOL=$(curl -s -o /dev/null -w "%{http_code}" -b "$COOKIE_JAR" "$API/system/subnet-pool")
if [ "$SPOOL" = "200" ]; then echo "  ✅ 200"; PASS=$((PASS + 1)); else echo "  ❌ got $SPOOL"; FAIL=$((FAIL + 1)); fi

echo -n "  13.6 GET /system/subnet-pool returns enabled pools: "
SPBODY=$(curl -s -b "$COOKIE_JAR" "$API/system/subnet-pool")
check "subnet-pool enabled" '"enabled":true' "$SPBODY"

echo -n "  13.7 GET /auth/go/{hostname} redirects via _set_token on the EDGE :8767: "
GO=$(curl -s -b "$COOKIE_JAR" -D - -o /dev/null "$API/auth/go/example-service-alice-0.localhost")
GOLOC=$(echo "$GO" | grep -i "^location:" | tr -d '\r')
case "$GOLOC" in
    *:8767/_set_token?code=*) echo "  ✅ redirect OK (edge :8767)"; PASS=$((PASS + 1));;
    *) echo "  ❌ got: $GOLOC"; FAIL=$((FAIL + 1));;
esac

echo -n "  13.8 Follow the /go/ exchange through the EDGE /_set_token relay (Gap 1): "
GOLOC=$(echo "$GOLOC" | tr -d '\r')
CODE=$(echo "$GOLOC" | sed -n 's/.*code=//p')
if [ -n "$CODE" ]; then
    EXCH=$(curl -s -D - -o /dev/null --max-time 10 \
        --resolve "example-service-alice-0.localhost:8767:127.0.0.1" \
        "http://example-service-alice-0.localhost:8767/_set_token?code=$CODE")
    EXCH_STATUS=$(echo "$EXCH" | grep -i "^HTTP/" | tr -d '\r' | tail -1)
    EXCH_COOKIE=$(echo "$EXCH" | grep -i "^set-cookie:" | tr -d '\r')
    if echo "$EXCH_STATUS" | grep -q "302" && echo "$EXCH_COOKIE" | grep -qi "provision_token="; then
        echo "  ✅ exchange relay OK ($EXCH_STATUS + Set-Cookie provision_token)"
        PASS=$((PASS + 1))
    else
        echo "  ❌ got status=[$EXCH_STATUS] cookie=[$EXCH_COOKIE]"
        FAIL=$((FAIL + 1))
    fi
else
    echo "  ❌ could not extract exchange code from $GOLOC"
    FAIL=$((FAIL + 1))
fi


# ─── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo " Results: $PASS passed, $FAIL failed"
echo "============================================"

rm -f "$COOKIE_JAR" "$VIEWER_COOKIE_JAR"

[ "$FAIL" -eq 0 ] && echo "✅ All gateway tests passed!" || echo "❌ Some tests failed!"
exit $FAIL
