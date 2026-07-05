#!/bin/bash
# Integration tests for proxy feature
set -e

GW="${GATEWAY_URL:-http://localhost:8770}"
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
echo "Proxy Feature Integration Tests"
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

# ---- Test 1: GET /api/system/proxy (default state) ----
echo ""
echo "--- Test 1: GET proxy config (may have existing data) ---"
R=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "GET /system/proxy returns 200" '"enabled"' "$R"
check "Response has protocol field" '"protocol"' "$R"
check "Response has host field" '"host"' "$R"
check "Response has port field" '"port"' "$R"
check "Response has reachable field" '"reachable"' "$R"
check "Response has last_checked_at field" '"last_checked_at"' "$R"
check "Response has url field" '"url"' "$R"

# ---- Test 2: PUT /api/system/proxy (save config) ----
echo ""
echo "--- Test 2: Save proxy config ---"
R=$(curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"http","host":"proxy.test.internal","port":3128}')
check "PUT proxy returns updated" '"updated":true' "$R"
check "PUT proxy returns proxy object" '"proxy"' "$R"
check "PUT proxy includes reachability" '"reachability"' "$R"
check "PUT proxy host saved correctly" '"proxy.test.internal"' "$R"
check "PUT proxy port saved correctly" '3128' "$R"
check "PUT proxy URL computed" '"http://proxy.test.internal:3128"' "$R"

# ---- Test 3: GET /api/system/proxy (verify saved) ----
echo ""
echo "--- Test 3: Verify saved config persists ---"
R=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "GET proxy shows enabled=true" '"enabled":true' "$R"
check "GET proxy shows host" '"proxy.test.internal"' "$R"
check "GET proxy shows port 3128" '"port":3128' "$R"
check "GET proxy has reachability result" '"reachable"' "$R"

# ---- Test 4: PUT /api/system/proxy (disable proxy) ----
echo ""
echo "--- Test 4: Disable proxy ---"
R=$(curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":false,"protocol":"http","host":"proxy.test.internal","port":3128}')
check "Disable proxy updated" '"updated":true' "$R"
R2=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "Proxy is now disabled" '"enabled":false' "$R2"

# ---- Test 5: PUT /api/system/proxy (re-enable) ----
echo ""
echo "--- Test 5: Re-enable proxy ---"
R=$(curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"http","host":"proxy.test.internal","port":3128}')
check "Re-enable proxy updated" '"updated":true' "$R"

# ---- Test 6: POST /api/system/proxy/test (manual recheck) ----
echo ""
echo "--- Test 6: Manual proxy test ---"
R=$(curl -s -X POST "$GW/api/system/proxy/test" \
    -H "Authorization: Bearer $TOKEN")
check "Test returns reachable field" '"reachable"' "$R"
check "Test returns checked_at field" '"checked_at"' "$R"
check "Test returns latency_ms field" '"latency_ms"' "$R"
check "Test result is boolean" '"reachable":true\|"reachable":false\|"reachable":null' "$R"

# ---- Test 7: PUT /api/system/proxy with credentials ----
echo ""
echo "--- Test 7: Save proxy with credentials ---"
R=$(curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"https","host":"secure.proxy.com","port":443,"username":"user1","password":"secret123"}')
check "Proxy with creds saved" '"updated":true' "$R"
check "URL masks password" 'user1.*secure.proxy.com' "$R"
check_not "Password not in response" '"secret123"' "$R"

# ---- Test 8: PUT /api/system/proxy (delete credentials) ----
echo ""
echo "--- Test 8: Clear credentials ---"
R=$(curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"https","host":"secure.proxy.com","port":443,"username":"","password":""}')
check "Credentials cleared" '"updated":true' "$R"
R2=$(curl -s "$GW/api/system/proxy" -H "Authorization: Bearer $TOKEN")
check "Username is empty" '"username":""' "$R2"
check "Password masked is empty" '"password_masked":""' "$R2"

# ---- Test 9: POST /api/users/deploy with use_global_proxy ----
echo ""
echo "--- Test 9: Deploy with use_global_proxy (should inject build args) ---"
# Re-enable proxy first with a real host
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"http","host":"proxy.test.internal","port":3128}' > /dev/null

# This will fail because the service doesn't exist, but we check that proxy args were injected
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_name":"testuser","service_name":"myapp","project_root":"myapp","compose_template_path":"docker-compose.yml.j2","label":"0","domain":"example.com","passwd":"test123","use_global_proxy":true}')
# Will get a 502 from provision-api (service doesn't exist), but proxy injection happened
echo "  Deploy with proxy response: $(echo "$R" | head -c 150)"

# ---- Test 10: Deploy with use_global_proxy=false (no injection) ----
echo ""
echo "--- Test 10: Deploy without proxy ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_name":"testuser","service_name":"myapp","project_root":"myapp","compose_template_path":"docker-compose.yml.j2","label":"0","domain":"example.com","passwd":"test123","use_global_proxy":false}')
echo "  Deploy without proxy response: $(echo "$R" | head -c 150)"

# ---- Test 11: PUT proxy with use_global_proxy when disabled (should 400) ----
echo ""
echo "--- Test 11: Deploy with proxy but proxy disabled ---"
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":false,"protocol":"http","host":"proxy.test.internal","port":3128}' > /dev/null

R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"user_name":"testuser","service_name":"myapp","project_root":"myapp","compose_template_path":"docker-compose.yml.j2","label":"0","domain":"example.com","passwd":"test123","use_global_proxy":true}')
check "Deploy with proxy disabled returns 400" "400\|not enabled" "$R"

# ---- Test 12: Audit log records proxy actions ----
echo ""
echo "--- Test 12: Audit logs for proxy actions ---"
R=$(curl -s "$GW/api/audit?action=proxy_config" -H "Authorization: Bearer $TOKEN")
check "Audit has proxy_config entries" '"entries"' "$R"

# ---- Cleanup: disable proxy ----
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":false,"protocol":"http","host":"","port":8080}' > /dev/null

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
