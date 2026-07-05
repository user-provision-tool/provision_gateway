#!/bin/bash
# Integration tests for Deploy Form feature
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

echo "============================================"
echo "Deploy Form Integration Tests"
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

# ---- Test 1: List source projects (for deploy form dropdown) ----
echo ""
echo "--- Test 1: Source projects available for deploy ---"
R=$(curl -s "$GW/api/services" -H "Authorization: Bearer $TOKEN")
check "Source projects list returns services" '"services"' "$R"

# ---- Test 2: Deploy with all fields ----
echo ""
echo "--- Test 2: Deploy with user_name, service_name, label, domain, passwd ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "user_name":"testdeploy",
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "nginx_conf_template_path":"nginx.conf.j2",
      "label":"0",
      "domain":"example.com",
      "passwd":"test123",
      "use_global_proxy":false
    }')
check "Deploy returns task_id" '"task_id"' "$R"
check "Deploy status is pending" '"pending"' "$R"

# ---- Test 3: Deploy with use_global_proxy=true (proxy is enabled) ----
echo ""
echo "--- Test 3: Deploy with use_global_proxy=true ---"
# Ensure proxy is enabled first
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"http","host":"172.18.0.1","port":7897}' > /dev/null

R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "user_name":"testproxy",
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "nginx_conf_template_path":"nginx.conf.j2",
      "label":"1",
      "domain":"example.com",
      "passwd":"test123",
      "use_global_proxy":true
    }')
check "Deploy with proxy returns task_id" '"task_id"' "$R"

# ---- Test 4: Deploy with build_args ----
echo ""
echo "--- Test 4: Deploy with build_args ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "user_name":"testbuild",
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "nginx_conf_template_path":"nginx.conf.j2",
      "label":"2",
      "domain":"example.com",
      "passwd":"test123",
      "build_args":{"MY_ARG":"value","ANOTHER":"thing"},
      "use_global_proxy":false
    }')
check "Deploy with build_args returns task_id" '"task_id"' "$R"

# ---- Test 5: Deploy with volumes ----
echo ""
echo "--- Test 5: Deploy with volumes ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "user_name":"testvol",
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "nginx_conf_template_path":"nginx.conf.j2",
      "label":"3",
      "domain":"example.com",
      "passwd":"test123",
      "volumes":{"app_data":"/srv/provision/user-data/testvol/app","logs":"/srv/provision/user-data/testvol/logs"},
      "use_global_proxy":false
    }')
check "Deploy with volumes returns task_id" '"task_id"' "$R"

# ---- Test 6: Deploy missing required field user_name ----
echo ""
echo "--- Test 6: Deploy without user_name ---"
R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "label":"0",
      "domain":"example.com",
      "passwd":"test123",
      "use_global_proxy":false
    }')
check "Deploy without user_name returns error" '"detail"' "$R"

# ---- Test 7: Deploy use_global_proxy with proxy disabled ----
echo ""
echo "--- Test 7: Disable proxy, try deploy with use_global_proxy=true ---"
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":false}' > /dev/null

R=$(curl -s -X POST "$GW/api/users/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "user_name":"testdisabled",
      "service_name":"siyuan-mcp",
      "project_root":"siyuan-mcp",
      "compose_template_path":"docker-compose.yml.j2",
      "label":"0",
      "domain":"example.com",
      "passwd":"test123",
      "use_global_proxy":true
    }')
check "Deploy with disabled proxy returns 400" "400\|not enabled" "$R"

# Re-enable proxy
curl -s -X PUT "$GW/api/system/proxy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"enabled":true,"protocol":"http","host":"172.18.0.1","port":7897}' > /dev/null

# ---- Test 8: List users after deploys ----
echo ""
echo "--- Test 8: Users list shows deploys ---"
R=$(curl -s "$GW/api/users" -H "Authorization: Bearer $TOKEN")
check "Users list has entries" '"users"' "$R"

# ---- Test 9: Audit log has deploy entries ----
echo ""
echo "--- Test 9: Audit log records deploy actions ---"
R=$(curl -s "$GW/api/audit?action=register" -H "Authorization: Bearer $TOKEN")
check "Audit has register entries" '"entries"' "$R"

# ---- Summary ----
echo ""
echo "============================================"
echo "Deploy Form Tests: $PASS passed, $FAIL failed"
echo "============================================"

if [ "$FAIL" -gt 0 ]; then
    echo "Some tests FAILED"
    exit 1
fi
echo "All deploy form tests passed! ✓"
