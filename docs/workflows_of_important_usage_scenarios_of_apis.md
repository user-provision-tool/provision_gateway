# Provision Gateway — Workflows of Important Usage Scenarios (APIs)

> **Version**: 1.0
> **Date**: 2026-07-05
> **Purpose**: Step-by-step API workflows for the most important usage scenarios, directly usable with `curl` or any HTTP client.

---

## Table of Contents

1. [First-Time Setup](#1-first-time-setup)
2. [Admin Authentication Flow](#2-admin-authentication-flow)
3. [Add Service from Git Repository](#3-add-service-from-git-repository)
4. [Add Service from File Upload](#4-add-service-from-file-upload)
5. [Edit Service Files (with Git Diff)](#5-edit-service-files-with-git-diff)
6. [Convert to Jinja2 Templates](#6-convert-to-jinja2-templates)
7. [Deploy Service to User](#7-deploy-service-to-user)
8. [Monitor Deploy Task (with Log Streaming)](#8-monitor-deploy-task-with-log-streaming)
9. [Clone All Services Between Users](#9-clone-all-services-between-users)
10. [Manage Service Lifecycle (Up/Down/Rebuild/Delete)](#10-manage-service-lifecycle-updownrebuilddelete)
11. [Change Service Password](#11-change-service-password)
12. [Test Service Connectivity (curl)](#12-test-service-connectivity-curl)
13. [System Monitoring & Reconciliation](#13-system-monitoring--reconciliation)
14. [Configure Global Proxy](#14-configure-global-proxy)
15. [Configure LLM (BYOK)](#15-configure-llm-byok)
16. [Generate Config via LLM](#16-generate-config-via-llm)
17. [Query Audit Logs](#17-query-audit-logs)
18. [End-User Management](#18-end-user-management)

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

## 3. Add Service from Git Repository

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

## 5. Edit Service Files (with Git Diff)

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

## 6. Convert to Jinja2 Templates

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

## 7. Deploy Service to User

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
```

---

## 8. Monitor Deploy Task (with Log Streaming)

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

## 9. Clone All Services Between Users

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

## 10. Manage Service Lifecycle (Up/Down/Rebuild/Delete)

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

## 11. Change Service Password

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

## 12. Test Service Connectivity (curl)

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

## 13. System Monitoring & Reconciliation

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
```

---

## 14. Configure Global Proxy

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

## 15. Configure LLM (BYOK)

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
    "agent_url": "",
    "agent_model": "",
    "system_prompt": "You are a DevOps assistant specializing in Docker, Docker Compose, and Nginx configuration. Generate production-ready configurations."
  }'

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

## 16. Generate Config via LLM

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

## 17. Query Audit Logs

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

## 18. End-User Management

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
