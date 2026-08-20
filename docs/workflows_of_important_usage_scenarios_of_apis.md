# Provision Gateway — Workflows of Important Usage Scenarios (APIs)

> **Version**: 3.0
> **Date**: 2026-08-19 (updated — new features: API key management (create/list/revoke), `/api/auth/go/{hostname}` service-access redirect + ACL verify, two-token login cookies (gateway_token 24h / provision_token 1y), multi-recipe deploy `project_root`, `/api/system/subnet-pool`)
> **Purpose**: Step-by-step API workflows for the most important usage scenarios, directly usable with `curl` or any HTTP client.

---

## Table of Contents

1. [First-Time Setup](#1-first-time-setup)
2. [Admin Authentication Flow](#2-admin-authentication-flow)
3. [End-User Authentication Flow](#3-end-user-authentication-flow)
4. [Add Service from Git Repository](#4-add-service-from-git-repository)
5. [Add Service from File Upload](#5-add-service-from-file-upload)
6. [Add Service from Template (DB)](#6-add-service-from-template-db)
7. [Edit Service Files (with Git Diff)](#7-edit-service-files-with-git-diff)
8. [Convert to Jinja2 Templates](#8-convert-to-jinja2-templates)
9. [Deploy Service to User](#9-deploy-service-to-user)
10. [Monitor Deploy Task (with Log Streaming)](#10-monitor-deploy-task-with-log-streaming)
11. [Clone All Services Between Users](#11-clone-all-services-between-users)
12. [Manage Service Lifecycle (Up/Down/Rebuild/Delete)](#12-manage-service-lifecycle-updownrebuilddelete)
13. [Change Service Password](#13-change-service-password)
14. [Get Container Logs](#14-get-container-logs)
15. [Test Service Connectivity (curl)](#15-test-service-connectivity-curl)
16. [System Monitoring & Reconciliation](#16-system-monitoring--reconciliation)
17. [SSL Certificate Management](#17-ssl-certificate-management)
18. [Configure Global Proxy](#18-configure-global-proxy)
19. [Configure LLM (BYOK)](#19-configure-llm-byok)
20. [Generate Config via LLM](#20-generate-config-via-llm)
21. [Query Audit Logs](#21-query-audit-logs)
22. [End-User Management](#22-end-user-management)
23. [API Key Management](#23-api-key-management)
24. [Service Access Redirect & ACL Verify](#24-service-access-redirect--acl-verify)

---

## 1. First-Time Setup

**Goal:** Initialize the gateway with the first admin account.

```bash
# Step 1: Check if setup is needed (optional)
curl -s http://localhost:8771/api/auth/me

# Step 2: Create initial admin
curl -s -X POST http://localhost:8771/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@example.com",
    "password": "securePassword123"
  }'

# Expected: 201 {"message": "Initial admin created. Please login."}

# Step 3: Login with the new admin
TOKEN=$(curl -s -X POST http://localhost:8771/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@example.com",
    "password": "securePassword123"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Token: $TOKEN"
```

---

## 2. Admin Authentication Flow

**Goal:** Login, use token, refresh when expired.

```bash
# --- Login ---
RESP=$(curl -s -X POST http://localhost:8771/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "securePassword123"}')

ACCESS_TOKEN=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
REFRESH_TOKEN=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")

# NOTE: login ALSO sets two httponly cookies (relevant for browser flows):
#   - gateway_token   (24h) — dashboard/gateway API access
#   - provision_token (1y)  — service access via provision-nginx
# All /api/* gateway routes accept the gateway_token cookie OR a Bearer header.

# --- Use token for authenticated requests ---
curl -s http://localhost:8771/api/auth/me \
  -H "Authorization: Bearer $ACCESS_TOKEN"

# --- Refresh token when expired ---
NEW_RESP=$(curl -s -X POST http://localhost:8771/api/auth/refresh \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\": \"$REFRESH_TOKEN\"}")

ACCESS_TOKEN=$(echo $NEW_RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# --- Change password ---
curl -s -X PUT http://localhost:8771/api/auth/password \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "securePassword123",
    "new_password": "evenMoreSecure456"
  }'

# --- Logout (client-side: discard tokens) ---
# No server endpoint needed — just remove tokens from storage
```

---

## 3. End-User Authentication Flow

**Goal:** Login as an end-user (portal user) and verify role-based access.

```bash
# --- Register a new end-user (no auth required) ---
curl -s -X POST http://localhost:8771/api/auth/users/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "password": "userpass123",
    "role": "viewer"
  }'

# Expected: 201 {"id": 1, "username": "alice", "is_approved": false, ...}

# --- Login as unapproved user (should fail) ---
curl -s -X POST http://localhost:8771/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "alice", "password": "userpass123"}'

# Expected: 401 "User not yet approved"

# --- Admin approves the user ---
ADMIN_TOKEN="..."
curl -s -X PUT http://localhost:8771/api/auth/users/1/approve \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# --- Login as approved end-user ---
END_RESP=$(curl -s -X POST http://localhost:8771/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "alice", "password": "userpass123"}')

END_TOKEN=$(echo $END_RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Response includes: "user_type": "end_user", "user": {"id": 1, "username": "alice", "role": "viewer"}
# Login also sets gateway_token (24h) + provision_token (1y) httponly cookies, and
# auto-creates a default API key for end-users who have none.
# Special-role users are rejected at login (403 "Special users cannot access the dashboard").

# --- Access with end-user token ---
curl -s http://localhost:8771/api/auth/me \
  -H "Authorization: Bearer $END_TOKEN"

# Expected: {"id": 1, "email": "alice", "role": "viewer", "user_type": "end_user"}

# --- Refresh end-user token ---
curl -s -X POST http://localhost:8771/api/auth/refresh \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\": \"$REFRESH_TOKEN\"}"

# Response includes: "user_type": "end_user"
```

---

## 4. Add Service from Git Repository

**Goal:** Clone a GitHub repo as a service source project, with optional proxy and LLM auto-generation.

```bash
TOKEN="your-jwt-token"

# Step 1: Clone repo as new service
curl -s -X POST http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "git",
    "repo_url": "https://github.com/user/my-fastapi-app.git",
    "branch": "main",
    "name": "my-fastapi-app",
    "use_proxy": false
  }'

# Expected: 201 with project details

# Step 2: Check what files were created
curl -s http://localhost:8771/api/services/my-fastapi-app \
  -H "Authorization: Bearer $TOKEN"

# Step 3 (optional): If missing docker-compose.yml or nginx.conf,
# scan the repo for LLM context
curl -s -X POST http://localhost:8771/api/services/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "directory": "/srv/provision/source_projects/my-fastapi-app"
  }'

# Step 4 (optional): Check deploy readiness, auto-generate missing files
curl -s -X POST http://localhost:8771/api/services/check-deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service_name": "my-fastapi-app"}'

# Step 5: Verify the service appears in the list
curl -s http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN"
```

---

## 4. Add Service from File Upload

**Goal:** Create a service project by uploading individual files.

```bash
TOKEN="your-jwt-token"

curl -s -X POST http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "upload",
    "name": "my-custom-app",
    "files": {
      "docker-compose.yml": "services:\n  web:\n    build: .\n    ports:\n      - \"3000:3000\"\n",
      "nginx.conf": "server {\n    listen 80;\n    server_name {{ server_name }};\n    location / {\n        proxy_pass http://{{ upstream }}:3000;\n    }\n}\n",
      ".env": "NODE_ENV=production\nPORT=3000\n",
      "Dockerfile": "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nRUN npm ci\nCMD [\"node\", \"index.js\"]\n"
    }
  }'

# Expected: 201
```

---

## 5. Add Service from Template (DB)

**Goal:** Create a service project from a pre-built template in the service_templates table.

```bash
TOKEN="your-jwt-token"

# Step 1: List available templates
curl -s http://localhost:8771/api/services/templates \
  -H "Authorization: Bearer $TOKEN"

# Expected:
# {
#   "templates": [
#     {"id": 1, "name": "wordpress", "description": "WordPress with MySQL", ...}
#   ]
# }

# Step 2: Create service from template
curl -s -X POST http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "template",
    "name": "my-wordpress-site",
    "template_id": 1
  }'

# Expected: 201 with project details

# Step 3: Verify the service appears in the list
curl -s http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN"
```

---

## 6. Edit Service Files (with Git Diff)

**Goal:** Read, edit, and review changes to service project files.

```bash
TOKEN="your-jwt-token"
SERVICE="siyuan"

# Step 1: Read a file
curl -s "http://localhost:8771/api/services/$SERVICE/files/docker-compose.yml.j2" \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Get the HEAD (committed) version for comparison
curl -s "http://localhost:8771/api/services/$SERVICE/git/head-file?file=docker-compose.yml.j2" \
  -H "Authorization: Bearer $TOKEN"

# Step 3: Edit the file
curl -s -X PUT "http://localhost:8771/api/services/$SERVICE/files/docker-compose.yml.j2" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "services:\n  siyuan:\n    container_name: {{ container_prefix }}siyuan\n    image: siyuan:latest\n    ports:\n      - \"6806:6806\"\n    volumes:\n      - {{ volumes[\"workspace\"] }}:/siyuan/workspace\n    networks:\n      - {{ network_name }}\n"
  }'

# Step 4: Check git status to see what changed
curl -s "http://localhost:8771/api/services/$SERVICE/git/status" \
  -H "Authorization: Bearer $TOKEN"

# Step 5: View the diff
curl -s "http://localhost:8771/api/services/$SERVICE/git/diff?file=docker-compose.yml.j2" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 7. Convert to Jinja2 Templates

**Goal:** Convert plain `docker-compose.yml` and `nginx.conf` to `.j2` templates with provision variables.

```bash
TOKEN="your-jwt-token"
SERVICE="my-custom-app"

# Step 1: Convert compose and nginx files to templates
curl -s -X POST "http://localhost:8771/api/services/$SERVICE/convert" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "compose_file": "docker-compose.yml",
    "nginx_file": "nginx.conf"
  }'

# Expected:
# {
#   "compose_template": "docker-compose.my-custom-app.yml.j2",
#   "nginx_template": "my-custom-app.nginx.conf.j2",
#   "message": "Templates created successfully."
# }

# Step 2: Verify the generated templates
curl -s "http://localhost:8771/api/services/$SERVICE/files/docker-compose.my-custom-app.yml.j2" \
  -H "Authorization: Bearer $TOKEN"

# Step 3: Verify service now shows has_compose_template and has_nginx_template
curl -s "http://localhost:8771/api/services/$SERVICE" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 8. Deploy Service to User

**Goal:** Deploy a service template for a user with custom configuration.

```bash
TOKEN="your-jwt-token"

# Step 1: Check available deployable users
curl -s http://localhost:8771/api/auth/users/deployable \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Check services available for deployment
curl -s http://localhost:8771/api/services \
  -H "Authorization: Bearer $TOKEN"
# Look for services where has_compose_template is true

# Step 3: Deploy
curl -s -X POST http://localhost:8771/api/users/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_name": "alice",
    "service_name": "siyuan",
    "project_root": "siyuan",
    "compose_file_path": "docker-compose.siyuan.yml.j2",
    "nginx_conf_file_path": "siyuan.nginx.conf.j2",
    "env_file_path": ".env",
    "label": "0",
    "domain": "snaprovision.com",
    "passwd": "securePassword123",
    "volumes": {
      "workspace": "/srv/provision/user-data/alice/siyuan"
    },
    "build_args": {},
    "use_global_proxy": false,
    "https": true,
    "fullchain": "/srv/provision/ssl/snaprovision.com/fullchain.pem",
    "privkey": "/srv/provision/ssl/snaprovision.com/privkey.pem"
  }'

# Expected: 202 with task_id
# {
#   "task_id": "abc123def456",
#   "status": "pending",
#   "type": "register"
# }

# Save the task_id for monitoring
TASK_ID="abc123def456"

# Multi-recipe projects (DeployForm): the Service dropdown value is `name@@recipe_path`.
#   - check-missing-files: GET /api/services/{name}/check-missing-files?recipe_path=...
#   - deploy payload: project_root = "{base}/{recipe_path}" (e.g. "siyuan/recipe-a"),
#     with compose/nginx template paths scoped to that recipe subdirectory.
```

---

## 9. Monitor Deploy Task (with Log Streaming)

**Goal:** Track deployment progress and view build logs in real-time.

```bash
TOKEN="your-jwt-token"
TASK_ID="abc123def456"

# Step 1: Poll task status
curl -s "http://localhost:8771/api/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Stream build logs via SSE (run in separate terminal)
curl -s -N "http://localhost:8771/api/tasks/$TASK_ID/log?tail=50&follow=true" \
  -H "Authorization: Bearer $TOKEN"

# Expected SSE output:
# data: {"line": "Step 1/5 : FROM siyuan:latest", "timestamp": "..."}
# data: {"line": " ---> Using cache", "timestamp": "..."}
# data: {"line": "Step 2/5 : COPY . .", "timestamp": "..."}
# ...

# Step 3: Cancel if needed
curl -s -X DELETE "http://localhost:8771/api/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TOKEN"

# Step 4: Check all tasks
curl -s http://localhost:8771/api/tasks \
  -H "Authorization: Bearer $TOKEN"
```

---

## 10. Clone All Services Between Users

**Goal:** Clone all services from user Alice to user Bob.

```bash
TOKEN="your-jwt-token"

# Step 1: Verify source user's services
curl -s http://localhost:8771/api/users/alice \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Clone all
curl -s -X POST http://localhost:8771/api/users/clone \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_user": "alice",
    "target_user": "bob",
    "domain": "snaprovision.com",
    "passwd": "bobSecret456",
    "volume_base_override": "/srv/provision/user-data/bob"
  }'

# Expected: 202 with multiple task_ids
# {
#   "tasks": [
#     {"service": "siyuan", "label": "0", "task_id": "task-001"},
#     {"service": "siyuan-mcp", "label": "0", "task_id": "task-002"}
#   ],
#   "total": 2
# }

# Step 3: Monitor all clone tasks
for TASK in task-001 task-002; do
  curl -s "http://localhost:8771/api/tasks/$TASK" \
    -H "Authorization: Bearer $TOKEN"
done

# Step 4: Verify bob now has the services
curl -s http://localhost:8771/api/users/bob \
  -H "Authorization: Bearer $TOKEN"
```

---

## 11. Manage Service Lifecycle (Up/Down/Rebuild/Delete)

**Goal:** Start, stop, rebuild, and remove deployed services.

```bash
TOKEN="your-jwt-token"
USER="alice"
SERVICE="siyuan"
LABEL="0"

# --- Start (docker compose up -d) ---
curl -s -X POST "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/up" \
  -H "Authorization: Bearer $TOKEN"

# --- Stop (docker compose stop) ---
curl -s -X POST "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/down" \
  -H "Authorization: Bearer $TOKEN"

# --- Rebuild (with no-cache) ---
curl -s -X POST "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/rebuild" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "no_cache": true,
    "build_args": {}
  }'

# Expected: 202 with task_id — monitor task for completion

# --- Delete (remove service entirely) ---
curl -s -X DELETE "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL" \
  -H "Authorization: Bearer $TOKEN"

# Expected: 202 with task_id
```

---

## 12. Change Service Password

**Goal:** Update HTTP basic auth password for a user's service.

```bash
TOKEN="your-jwt-token"
USER="alice"
SERVICE="siyuan"
LABEL="0"

curl -s -X PUT "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/password" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "passwd": "newSecurePassword789"
  }'

# Expected: 200 {"message": "Password updated. Nginx reloaded."}

# The gateway:
# 1. Hashes the new password with bcrypt
# 2. Rewrites the .htpasswd file for this service
# 3. Reloads nginx to apply the change
```

---

## 13. Test Service Connectivity (curl)

**Goal:** Test if a deployed service is reachable from within the gateway.

```bash
TOKEN="your-jwt-token"
USER="alice"
SERVICE="siyuan"
LABEL="0"

# Step 1: Get the service URL
curl -s "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/url" \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Test connectivity
curl -s -X POST "http://localhost:8771/api/users/$USER/$SERVICE/$LABEL/test-curl" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "include_auth": true,
    "follow_redirect": true
  }'

# Expected:
# {
#   "url": "https://siyuan-alice-0.snaprovision.com",
#   "http_code": 200,
#   "headers": {"content-type": "text/html; charset=utf-8"},
#   "body_preview": "<!DOCTYPE html>...",
#   "time_total_ms": 45.2,
#   "error": null
# }
```

---

## 14. System Monitoring & Reconciliation

**Goal:** Monitor system health and reconcile nginx state.

```bash
TOKEN="your-jwt-token"

# Step 1: Check overall system status
curl -s http://localhost:8771/api/system/status \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Get detailed container stats
curl -s "http://localhost:8771/api/system/stats?detail=true" \
  -H "Authorization: Bearer $TOKEN"

# Step 3: View current nginx state
curl -s http://localhost:8771/api/system/nginx-state \
  -H "Authorization: Bearer $TOKEN"

# Step 4: Run reconciliation (fixes nginx network connections)
curl -s -X POST http://localhost:8771/api/system/reconcile \
  -H "Authorization: Bearer $TOKEN"

# Step 5: Check reconciliation result
curl -s http://localhost:8771/api/system/reconcile/status \
  -H "Authorization: Bearer $TOKEN"

# Step 6: Subnet pool usage (Dashboard "Subnet Pool" card)
curl -s http://localhost:8771/api/system/subnet-pool \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"enabled": true, "pools": [{"cidr": "172.30.0.0/16", "used_slots": 3,
#             "total_slots": 8, "used_pct": 38}, ...]}
```

---

## 15. Configure Global Proxy

**Goal:** Set up a global HTTP proxy for git clones and Docker builds.

```bash
TOKEN="your-jwt-token"

# Step 1: Check current proxy configuration
curl -s http://localhost:8771/api/system/proxy \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Add a new proxy (auto-tests reachability)
curl -s -X POST http://localhost:8771/api/system/proxy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Office Proxy",
    "protocol": "http",
    "host": "proxy.office.internal",
    "port": 3128,
    "username": "proxyuser",
    "password": "proxypass"
  }'

# Expected: 201 with proxy config + reachability result

# Step 3: Activate the proxy (only if reachable)
curl -s -X PUT http://localhost:8771/api/system/proxy/1/activate \
  -H "Authorization: Bearer $TOKEN"

# Step 4: Test connectivity
curl -s -X POST http://localhost:8771/api/system/proxy/test \
  -H "Authorization: Bearer $TOKEN"

# Step 5: Deploy with proxy enabled
curl -s -X POST http://localhost:8771/api/users/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_name": "alice",
    "service_name": "siyuan",
    "compose_file_path": "docker-compose.siyuan.yml.j2",
    "nginx_conf_file_path": "siyuan.nginx.conf.j2",
    "env_file_path": ".env",
    "label": "0",
    "domain": "snaprovision.com",
    "passwd": "secret",
    "use_global_proxy": true
  }'

# Step 6: Delete proxy when no longer needed
curl -s -X DELETE http://localhost:8771/api/system/proxy/1 \
  -H "Authorization: Bearer $TOKEN"
```

---

## 16. Configure LLM (BYOK)

**Goal:** Set up Bring-Your-Own-Key LLM for AI-assisted config generation.

```bash
TOKEN="your-jwt-token"

# Step 1: Check existing LLM configs
curl -s http://localhost:8771/api/llm/configs \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Add a new LLM config
curl -s -X POST http://localhost:8771/api/llm/configs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "byok",
    "byok_base_url": "https://api.deepseek.com/v1",
    "byok_model": "deepseek-chat",
    "byok_api_key": "sk-your-api-key-here",
    "system_prompt": "You are a DevOps assistant specializing in Docker, Docker Compose, and Nginx configuration. Generate production-ready configurations."
  }'
  # Note: local-agent fields (agent_url/agent_model, mode='local_agent') are
  # deferred at the API level (GAP-2, iter-1) — normalized to byok, never persisted.

# Step 3: Activate the config
curl -s -X PUT http://localhost:8771/api/llm/configs/1/activate \
  -H "Authorization: Bearer $TOKEN"

# Step 4: Test the connection
curl -s -X POST http://localhost:8771/api/llm/test \
  -H "Authorization: Bearer $TOKEN"

# Expected:
# {
#   "success": true,
#   "latency_ms": 450,
#   "model": "deepseek-chat",
#   "response_preview": "Hello! I'm ready to help with DevOps tasks."
# }
```

---

## 17. Generate Config via LLM

**Goal:** Use AI to generate docker-compose.yml, nginx.conf, or .env files.

```bash
TOKEN="your-jwt-token"

# Step 1: Generate a docker-compose.yml
curl -s -X POST http://localhost:8771/api/llm/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "docker_compose",
    "generate_type": "docker_compose",
    "context": {
      "repo_description": "A Python FastAPI application with Redis caching and PostgreSQL database",
      "repo_files": ["Dockerfile", "requirements.txt", "main.py", "alembic.ini"],
      "port": 8000,
      "needs_db": true,
      "needs_cache": true,
      "needs_volume": true,
      "language": "Python",
      "framework": "FastAPI"
    }
  }'

# Step 2: Generate an nginx.conf
curl -s -X POST http://localhost:8771/api/llm/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "nginx_conf",
    "generate_type": "nginx_conf",
    "context": {
      "port": 8000,
      "has_https": true,
      "auth_basic": true
    }
  }'

# Step 3: Save generated files to a service project
curl -s -X POST http://localhost:8771/api/services/save-generated \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "my-fastapi-app",
    "files": {
      "docker-compose.yml": "services:\n  web:\n    build: .\n    ...",
      "nginx.conf": "server {\n    listen 80;\n    ...\n}"
    }
  }'

# Step 4: Use LLM for troubleshooting
curl -s -X POST http://localhost:8771/api/llm/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "troubleshoot",
    "generate_type": "troubleshoot",
    "context": {
      "message": "My siyuan service for alice keeps crashing. The logs show 'out of memory'. What should I check?"
    }
  }'
```

---

## 18. Query Audit Logs

**Goal:** Search and filter the audit trail for compliance and debugging.

```bash
TOKEN="your-jwt-token"

# Step 1: Get all audit entries (latest 50)
curl -s http://localhost:8771/api/audit \
  -H "Authorization: Bearer $TOKEN"

# Step 2: Filter by action type
curl -s "http://localhost:8771/api/audit?action=register&limit=20" \
  -H "Authorization: Bearer $TOKEN"

# Step 3: Filter by target user
curl -s "http://localhost:8771/api/audit?target_user=alice" \
  -H "Authorization: Bearer $TOKEN"

# Step 4: Filter by date range
curl -s "http://localhost:8771/api/audit?from=2026-07-01&to=2026-07-05" \
  -H "Authorization: Bearer $TOKEN"

# Step 5: Combine filters
curl -s "http://localhost:8771/api/audit?action=deploy&target_user=alice&from=2026-07-01&limit=100&offset=0" \
  -H "Authorization: Bearer $TOKEN"

# Step 6: Paginate
curl -s "http://localhost:8771/api/audit?limit=10&offset=10" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 19. End-User Management

**Goal:** Register, approve, and manage end-user accounts for the portal.

```bash
TOKEN="your-jwt-token"

# Step 1: Register a new end-user (no auth required)
curl -s -X POST http://localhost:8771/api/auth/users/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "bob",
    "email": "bob@example.com",
    "password": "bobPassword123",
    "role": "viewer"
  }'

# Expected: 201 with is_approved: false

# Step 2: Login as admin
ADMIN_TOKEN=$(curl -s -X POST http://localhost:8771/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "securePassword123"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Step 3: List all end-users
curl -s http://localhost:8771/api/auth/users \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Step 4: Approve the new user
curl -s -X PUT http://localhost:8771/api/auth/users/2/approve \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Step 5: Assign special users access
curl -s -X PUT http://localhost:8771/api/auth/users/2 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "role": "viewer",
    "allowed_special_users": ["shared", "public"]
  }'

# Step 6: Promote to admin
curl -s -X PUT http://localhost:8771/api/auth/users/2 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "role": "admin"
  }'

# Step 7: Delete a user
curl -s -X DELETE http://localhost:8771/api/auth/users/2 \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## 23. API Key Management

**Goal:** Create, list, and revoke long-lived API keys (provision tokens). Viewers manage their own keys; admins manage any user's.

```bash
TOKEN="your-jwt-token"

# --- Create a key (viewer: for self; admin: optional user_id for another user) ---
curl -s -X POST http://localhost:8771/api/auth/keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "CI/CD",
    "user_id": 1        # admin only; omit to create for yourself
  }'

# Expected 201: {"key": {...}, "token": "eyJ...", "provision_token": "...",
#                "message": "Save this token — it will not be shown again."}
# The raw `token` is shown ONLY once — save it immediately.

# --- List keys (admin: all users; viewer: own only) ---
curl -s http://localhost:8771/api/auth/keys \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"keys": [{"id": 1, "user_id": 1, "label": "CI/CD",
#                      "created_at": "...", "expires_at": "...", "is_revoked": false}]}

# --- Revoke a key ---
curl -s -X DELETE http://localhost:8771/api/auth/keys/1 \
  -H "Authorization: Bearer $TOKEN"
# Expected: {"revoked": true, "key_id": 1}

# --- Use the raw token as a provision token for service access ---
curl -s http://<service-host> \
  -H "X-Provision-Token: <token>"
```

**Notes:**
- The API-key token embeds `api_key_id` in a 1-year provision token; revoking the key invalidates it (enforced by `GET /api/auth/verify`).
- A viewer creating/revoking a key for another user id → `403`.
- End-users automatically get a `Default` key on first login if they have none.

---

## 24. Service Access Redirect & ACL Verify

**Goal:** Open a deployed service through the gateway and understand the nginx ACL deny/redirect responses.

### 24.1 Gateway service-access redirect

```bash
# The Services page URL links point here: /go/{service}-{user}-{label}.localhost
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" \
  http://localhost:8771/api/auth/go/siyuan-alice-0.localhost \
  -H "Authorization: Bearer $TOKEN"

# Expected: 302 → http://siyuan-alice-0.localhost:8766/_set_token?token={provision_token}&redirect=/
# The browser follows this; _set_token sets the provision_token cookie for the
# service domain, then redirects to / to load the service.
```

### 24.2 nginx ACL verify (auth_request subrequest)

`GET /api/auth/verify` is called by provision-nginx (`auth_request`) with no auth. It returns the status + `X-Auth-Action` header that nginx's `error_page` + `map $http_accept` turns into browser redirects or API status codes:

| Gateway response | X-Auth-Action | Browser redirect | API result |
|---|---|---|---|
| 200 + X-Service-Basic | — | allowed (credential injected) | 200 |
| 401, no/invalid token | `login_required` | `302 /login` | 401 |
| 401, expired token | `token_expired` | `302 /login` (Option B) | 401 |
| 401, user inactive/not approved | `login_required` | `302 /login` | 401 |
| 403, ACL denied | `acl_denied` | `302 /alert?reason=acl_denied&service={host}` | 403 |

**Alert page targets:** `?reason=acl_denied` → "Access Denied — You do not have access to {service}"; `?reason=token_expired` → "API Token Expired". Both are served by the dashboard SPA at `/alert`.
