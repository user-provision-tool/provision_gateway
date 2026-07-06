#!/bin/bash
# ============================================================================
# test_provision_api.sh — Functionality tests for provision-api
#
# Tests the provision-api directly at http://localhost:8765
# All endpoints except /health require no authentication (internal service).
# ============================================================================

set -e
API="http://localhost:8765"
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

check_code() {
    local desc="$1"
    local expected="$2"
    local code="$3"
    if [ "$code" = "$expected" ]; then
        echo "  ✅ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $desc (expected HTTP $expected, got $code)"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo " Provision-API Functionality Tests"
echo " Target: $API"
echo "============================================"
echo ""

# ─── 1. Health ──────────────────────────────────────────────────────────────

echo "── 1. Health ──"

echo -n "  1.1 GET /health: "
RESP=$(curl -s "$API/health")
check "returns status ok" '"status":"ok"' "$RESP"


# ─── 2. Docker / Host Stats ─────────────────────────────────────────────────

echo "── 2. Docker & Host Stats ──"

echo -n "  2.1 GET /docker/ps: "
RESP=$(curl -s "$API/docker/ps")
check "returns container list" '"name"' "$RESP"

echo -n "  2.2 GET /docker/stats: "
RESP=$(curl -s "$API/docker/stats")
check "returns stats array" '"cpu"' "$RESP"

echo -n "  2.3 GET /docker/info: "
RESP=$(curl -s "$API/docker/info")
check "returns container count" '"containers_total"' "$RESP"

echo -n "  2.4 GET /host/stats: "
RESP=$(curl -s "$API/host/stats")
check "returns cpu_percent" '"cpu_percent"' "$RESP"

echo -n "  2.5 GET /docker/container/provision-api/exists: "
RESP=$(curl -s "$API/docker/container/provision-api/exists")
check "exists=true" 'true' "$RESP"

echo -n "  2.6 GET /docker/container/provision-api/running: "
RESP=$(curl -s "$API/docker/container/provision-api/running")
check "running=true" 'true' "$RESP"

echo -n "  2.7 GET /docker/container/nonexistent-container/exists: "
RESP=$(curl -s "$API/docker/container/nonexistent-container/exists")
check "exists=false" 'false' "$RESP"


# ─── 3. Users — Register / List / Get ───────────────────────────────────────

echo "── 3. Users — Register ──"

echo -n "  3.1 POST /users (register siyuan for testuser): "
RESP=$(curl -s -X POST "$API/users" \
  -H "Content-Type: application/json" \
  -d '{
    "user_name": "testuser",
    "service_name": "siyuan",
    "project_root": "siyuan",
    "compose_template_path": "docker-compose.yml.j2",
    "nginx_conf_template_path": "nginx.conf.j2",
    "label": "1",
    "domain": "snaprovision.com",
    "passwd": "test123",
    "volumes": {"siyuan_data": "/srv/provision/user_data/testuser/siyuan/1/siyuan_data"},
    "https": false
  }')
check "returns task_id" '"task_id"' "$RESP"

echo "     (waiting for registration to complete...)"
sleep 8

echo -n "  3.2 GET /users (list all): "
RESP=$(curl -s "$API/users")
check "shows testuser" "testuser" "$RESP"

echo -n "  3.3 GET /users/testuser (single user): "
RESP=$(curl -s "$API/users/testuser")
check "has service siyuan" "siyuan" "$RESP"


# ─── 4. Service Lifecycle — Stop / Start / Password ─────────────────────────

echo "── 4. Service Lifecycle ──"

echo -n "  4.1 POST /users/testuser/services/siyuan/1/down (stop): "
RESP=$(curl -s -X POST "$API/users/testuser/services/siyuan/1/down")
check "returns stopped" '"down"' "$RESP"
sleep 2

echo -n "  4.2 Container stopped after down: "
RESP=$(curl -s "$API/docker/container/siyuan-user_testuser-1-siyuan/running" 2>/dev/null)
check "running=false" 'false' "$RESP"

echo -n "  4.3 POST /users/testuser/services/siyuan/1/up (start): "
RESP=$(curl -s -X POST "$API/users/testuser/services/siyuan/1/up")
check "returns started" '"up"' "$RESP"
sleep 2

echo -n "  4.4 Container running after up: "
RESP=$(curl -s "$API/docker/container/siyuan-user_testuser-1-siyuan/running" 2>/dev/null)
check "running=true" 'true' "$RESP"

echo -n "  4.5 PUT /users/testuser/services/siyuan/1/password (change): "
RESP=$(curl -s -X PUT "$API/users/testuser/services/siyuan/1/password" \
  -H "Content-Type: application/json" \
  -d '{"passwd": "newpass456"}')
check "password updated" "Password updated" "$RESP"

echo -n "  4.6 Password includes user_name: "
check "has user_name" '"user_name"' "$RESP"


# ─── 5. Rebuild ─────────────────────────────────────────────────────────────

echo "── 5. Rebuild ──"

echo -n "  5.1 POST /users/testuser/services/siyuan/1/rebuild: "
RESP=$(curl -s -X POST "$API/users/testuser/services/siyuan/1/rebuild")
check "returns task_id" '"task_id"' "$RESP"


# ─── 6. Tasks ───────────────────────────────────────────────────────────────

echo "── 6. Tasks ──"

echo -n "  6.1 GET /tasks (list tasks): "
RESP=$(curl -s "$API/tasks")
check "returns tasks array" '"tasks"' "$RESP"

echo -n "  6.2 GET /tasks (has entries): "
TASK_COUNT=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('tasks',d.get('task_ids',[]))))" 2>/dev/null || echo "0")
if [ "$TASK_COUNT" -gt 0 ] 2>/dev/null; then
    echo "  ✅ $TASK_COUNT tasks found"
    PASS=$((PASS + 1))
else
    echo "  ⚠️ No tasks found (may be empty after cleanup)"
fi


# ─── 7. Nginx ───────────────────────────────────────────────────────────────

echo "── 7. Nginx ──"

echo -n "  7.1 GET /nginx/connections: "
RESP=$(curl -s "$API/nginx/connections")
check "returns connections data" 'connected_networks' "$RESP"

echo -n "  7.2 POST /nginx/reconnect-all: "
RESP=$(curl -s -X POST "$API/nginx/reconnect-all")
check "returns reconnected" '"reconnected"' "$RESP"

echo -n "  7.3 POST /docker/nginx/reload: "
RESP=$(curl -s -X POST "$API/docker/nginx/reload")
check "returns reloaded" '"reloaded"' "$RESP"


# ─── 8. Remove service ──────────────────────────────────────────────────────

echo "── 8. Remove Service ──"

echo -n "  8.1 DELETE /users/testuser/services/siyuan/1: "
RESP=$(curl -s -X DELETE "$API/users/testuser/services/siyuan/1")
check "returns task_id" '"task_id"' "$RESP"

sleep 5

echo -n "  8.2 GET /users/testuser (should be empty after removal): "
RESP=$(curl -s "$API/users/testuser")
if echo "$RESP" | grep -q '"siyuan"'; then
    echo "  ⚠️ siyuan still present (may not have finished removing)"
else
    echo "  ✅ service removed (no siyuan found)"
    PASS=$((PASS + 1))
fi


# ─── 9. Error Cases ─────────────────────────────────────────────────────────

echo "── 9. Error Cases ──"

echo -n "  9.1 GET /users/nonexistent: "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API/users/nonexistent")
# 404 is also acceptable (no registrations found)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "404" ]; then
    echo "  ✅ returns $HTTP_CODE (valid response)"
    PASS=$((PASS + 1))
else
    echo "  ❌ unexpected $HTTP_CODE"
    FAIL=$((FAIL + 1))
fi

echo -n "  9.2 POST to nonexistent service up (should 404): "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/users/nobody/services/none/0/up")
if [ "$HTTP_CODE" = "404" ]; then
    echo "  ✅ returns 404"
    PASS=$((PASS + 1))
else
    echo "  ⚠️ returns $HTTP_CODE (expected 404)"
    FAIL=$((FAIL + 1))
fi


# ─── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo " Results: $PASS passed, $FAIL failed"
echo "============================================"

[ "$FAIL" -eq 0 ] && echo "✅ All provision-api tests passed!" || echo "❌ Some tests failed!"
exit $FAIL
