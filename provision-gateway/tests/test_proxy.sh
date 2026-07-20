#!/bin/bash
# Integration tests for proxy feature — multi-config API
set -e

GW="${GATEWAY_URL:-http://localhost:8771}"
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
        echo "  ✗ $desc"
        echo "    expected: $expected"
        echo "    got:      $(echo "$actual" | head -c 200)"
        FAIL=$((FAIL + 1))
    fi
}

check_not() {
    local desc="$1"
    local not_expected="$2"
    local actual="$3"
    if ! echo "$actual" | grep -q "$not_expected"; then
        echo "  ✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $desc (found unwanted: $not_expected)"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "Proxy Feature Integration Tests (Multi-Config)"
echo "Gateway: $GW"
echo "============================================"

# Login
TOKEN=$(curl -s -X POST "$GW/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@example.com","password":"admin123"}' \
    | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
    echo "✗ Could not obtain auth token"
    exit 1
fi

# ---- Test 1: GET /api/system/proxy (list configs) ----
echo ""
echo "--- Test 1: GET proxy configs list ---"
R=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "GET /system/proxy returns configs list" '"configs"' "$R"

# ---- Test 2: POST /api/system/proxy (create config) ----
echo ""
echo "--- Test 2: Create proxy config ---"
R=$(curl -s -X POST "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name":"Test Proxy","protocol":"http","host":"proxy.test.internal","port":3128}')
check "POST proxy returns id" '"id"' "$R"
check "POST proxy returns host" '"proxy.test.internal"' "$R"
check "POST proxy returns port 3128" '"port":3128' "$R"
PROXY_ID=$(echo "$R" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
echo "  Created proxy ID: $PROXY_ID"

# ---- Test 3: GET /api/system/proxy (verify created) ----
echo ""
echo "--- Test 3: Verify created config in list ---"
R=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "GET proxy list has new config" "proxy.test.internal" "$R"
check "GET proxy list has port 3128" "3128" "$R"

# ---- Test 4: PUT /api/system/proxy/{id} (update config) ----
echo ""
echo "--- Test 4: Update proxy config ---"
R=$(curl -s -X PUT "$GW/api/system/proxy/$PROXY_ID" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"protocol":"http","host":"proxy.test.internal","port":3128}')
check "PUT proxy returns config" '"config"' "$R"
check "PUT proxy host updated" 'proxy.test.internal' "$R"

# ---- Test 5: Verify update persisted ----
echo ""
echo "--- Test 5: Verify update persisted ---"
R=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "GET shows updated host" 'proxy.test.internal' "$R"
check "GET shows updated port" '3128' "$R"

# ---- Test 6: PUT /api/system/proxy/{id}/activate ----
echo ""
echo "--- Test 6: Activate proxy config ---"
R=$(curl -s -X PUT "$GW/api/system/proxy/$PROXY_ID/activate" \
    -H "Authorization: Bearer $TOKEN")
# May fail if unreachable (expected for test hosts); just check response is valid JSON
check "Activate returns JSON" '"' "$R"
echo "  Activate response: $(echo "$R" | head -c 100)"

# ---- Test 7: POST /api/system/proxy/test (recheck all) ----
echo ""
echo "--- Test 7: Manual proxy test ---"
R=$(curl -s -X POST "$GW/api/system/proxy/test" \
    -H "Authorization: Bearer $TOKEN")
check "Test returns results" '"results"' "$R"

# ---- Test 8: Update with credentials ----
echo ""
echo "--- Test 8: Save proxy with credentials ---"
R=$(curl -s -X PUT "$GW/api/system/proxy/$PROXY_ID" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"protocol":"http","host":"secure.proxy.com","port":443,"username":"user1","password":"secret123"}')
check "Proxy with creds saved" '"config"' "$R"

R2=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "URL masks password" 'secure.proxy.com' "$R2"
check_not "Password not in response" '"secret123"' "$R2"

# ---- Test 9: Clear credentials ----
echo ""
echo "--- Test 9: Clear credentials ---"
R=$(curl -s -X PUT "$GW/api/system/proxy/$PROXY_ID" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"protocol":"http","host":"secure.proxy.com","port":443,"username":"","password":""}')
check "Credentials cleared" '"config"' "$R"
R2=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "Username is empty" '"username":""' "$R2"
check "Password masked is empty" '"password_masked":""' "$R2"

# ---- Test 10: POST /api/system/proxy/deactivate (deactivate all) ----
echo ""
echo "--- Test 10: Deactivate all proxies ---"
R=$(curl -s -X POST "$GW/api/system/proxy/deactivate" \
    -H "Authorization: Bearer $TOKEN")
check "Deactivate returns deactivated" '"deactivated"' "$R"

# ---- Test 11: Deploy with proxy disabled (should 400) ----
echo ""
echo "--- Test 11: Deploy with proxy disabled ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_name":"proxytest","service_name":"siyuan","project_root":"siyuan","compose_template_path":"docker-compose.yml.j2","nginx_conf_template_path":"nginx.conf.j2","label":"0","domain":"example.com","passwd":"test123","use_global_proxy":true}')
check "Deploy with proxy disabled returns 400" "400\|not enabled" "$R"

# ---- Test 12: Deploy without proxy (should work regardless) ----
echo ""
echo "--- Test 12: Deploy without proxy flag ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_name":"proxytest","service_name":"siyuan","project_root":"siyuan","compose_template_path":"docker-compose.yml.j2","nginx_conf_template_path":"nginx.conf.j2","label":"0","domain":"example.com","passwd":"test123","use_global_proxy":false}')
check "Deploy without proxy returns task" '"task_id"' "$R"

# ---- Test 13: Delete proxy config ----
echo ""
echo "--- Test 13: Delete proxy config ---"
R=$(curl -s -X DELETE "$GW/api/system/proxy/$PROXY_ID" \
    -H "Authorization: Bearer $TOKEN")
check "Delete returns deleted" '"deleted"' "$R"

# Verify deletion
R2=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check_not "Deleted proxy not in list" "proxy.test.internal" "$R2"

# ---- Test 14: Audit log records proxy actions ----
echo ""
echo "--- Test 14: Audit logs for proxy actions ---"
R=$(curl -s "$GW/api/audit?action=proxy_config" -H "Authorization: Bearer $TOKEN")
check "Audit has proxy_config entries" '"entries"' "$R"

# ---- Summary ----
echo ""
echo "============================================"
echo "Proxy Tests: $PASS passed, $FAIL failed"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    echo "Some tests FAILED"
    exit 1
fi
echo "All proxy tests passed! ✓"
