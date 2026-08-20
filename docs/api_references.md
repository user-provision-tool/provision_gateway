# Provision Gateway — API Reference

> **Version**: 2.0
> **Date**: 2026-08-01 (updated — Cycle 20260801T165901Z Iteration 1: From Template tab removed from UI / mode=template API-only (GAP-1), local-agent fields deferred at API level (GAP-2); prior: /templates, /notifications, /next-label endpoints)
> **Base URL**: `http://provision-gateway:8770` (internal) / `http://localhost:8771/api` (via dashboard proxy)

---

## Table of Contents

1. [Conventions](#1-conventions)
2. [Health](#2-health)
3. [Authentication](#3-authentication)
4. [System](#4-system)
5. [Services (Source Projects)](#5-services-source-projects)
6. [Users (End-User Provisioning)](#6-users-end-user-provisioning)
7. [Tasks](#7-tasks)
8. [LLM Configuration](#8-llm-configuration)
9. [Audit](#9-audit)

---

## 1. Conventions

### 1.1 Authentication

- All endpoints except `/health`, `/api/auth/setup`, `/api/auth/login`, `/api/auth/refresh`, and `/api/auth/users/register` require a valid gateway token, sent either as the `gateway_token` cookie or as `Authorization: Bearer <JWT>`
- **Two-token login:** `POST /api/auth/login` sets two `HttpOnly` cookies:
  - `gateway_token` — 24h TTL (configurable via `JWT_EXPIRE_SEC`), used for dashboard/gateway API auth
  - `provision_token` — **1 year** TTL (configurable via `PROVISION_COOKIE_TTL`), used for service access via provision-nginx
- **Bearer tokens for API clients:** `POST /api/auth/login` also returns an `access_token` (1 hour, `JWT_EXPIRE_SEC`) and a `refresh_token` (7 days, `JWT_REFRESH_EXPIRE_SEC`) in the response body
- `/api/*` routes are gated by the `require_gateway_token` dependency, which accepts either the `gateway_token` cookie or `Authorization: Bearer <JWT>`; admin-only routes additionally use `require_admin` (`require_gateway_token` + `role == admin`). These replace the legacy `get_current_admin` / `require_admin_role` dependencies
- JWT tokens carry a `user_type` claim: `admin` (gateway admin) or `end_user` (portal user)
- Non-admin users (role=`viewer`) have read-only access; mutating endpoints require `admin` role
- The SSE log endpoint (`GET /api/tasks/{id}/log`) also supports `?token=` query parameter for EventSource (which cannot set headers)

### 1.2 Request/Response

- Content-Type: `application/json` (except SSE endpoints which use `text/event-stream`)
- HTTP status codes:
  - `200` — Success
  - `201` — Created
  - `202` — Accepted (async operation started)
  - `400` — Bad request
  - `401` — Unauthorized (missing/invalid token)
  - `403` — Forbidden (insufficient role)
  - `404` — Not found
  - `409` — Conflict
  - `500` — Internal server error

### 1.3 SSE Endpoints

- Content-Type: `text/event-stream`
- Events are JSON-encoded with `data:` prefix
- Clients should use `EventSource` API or equivalent

---

## 2. Health

### `GET /health`

No authentication required. Liveness/readiness probe.

**Response 200:**
```json
{
  "status": "ok",
  "db": "connected",
  "provision_api": "reachable",
  "uptime_sec": 12345
}
```

---

## 3. Authentication

### `POST /api/auth/setup`

First-run admin account creation. Only works when no admin exists.

**Request:**
```json
{
  "email": "admin@example.com",
  "password": "secret123"
}
```

**Response 201:**
```json
{
  "message": "Initial admin created. Please login."
}
```

**Errors:**
- `409` — Admin already exists (use `/api/auth/register` instead)

---

### `POST /api/auth/register`

Create additional admin/portal user. Requires `admin` role to create another admin.

**Request:**
```json
{
  "email": "newadmin@example.com",
  "password": "secret123",
  "role": "viewer"
}
```

**Response 201:**
```json
{
  "id": 2,
  "email": "newadmin@example.com",
  "role": "viewer",
  "created_at": "2026-07-05T00:00:00Z"
}
```

---

### `POST /api/auth/login`

Authenticate and receive tokens. Supports both gateway admin accounts (by email) and end-user portal accounts (by username). Sets two `HttpOnly` cookies (`gateway_token`, `provision_token`) for browser/dashboard auth and returns Bearer `access_token`/`refresh_token` in the body for API clients.

**Request:**
```json
{
  "email": "admin@example.com",
  "password": "secret123"
}
```

**Response 200 (admin):**
```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_type": "admin",
  "admin": {
    "id": 1,
    "email": "admin@example.com",
    "role": "admin"
  }
}
```

**Response 200 (end-user):**
```json
{
  "access_token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_type": "end_user",
  "user": {
    "id": 5,
    "username": "alice",
    "role": "viewer"
  }
}
```

**Cookies set (admin and end-user logins):**

| Cookie | Value | TTL | Purpose |
|---|---|---|---|
| `gateway_token` | JWT (`type=gateway`) | 24 hours (`JWT_EXPIRE_SEC`) | Dashboard/gateway API auth — accepted by `require_gateway_token` |
| `provision_token` | JWT (`type=provision`) | **1 year** (`PROVISION_COOKIE_TTL`) | Service access via provision-nginx — consumed by `GET /api/auth/verify` |

Both cookies are `HttpOnly`, `SameSite=Lax`, `Path=/`. The response body additionally carries Bearer `access_token` (1 hour, `JWT_EXPIRE_SEC`) and `refresh_token` (7 days, `JWT_REFRESH_EXPIRE_SEC`) for API clients that cannot use cookies.

**Errors:**
- `401` — "Invalid email/username or password"
- `403` — Special users (role=`special`) cannot access the dashboard

---

### `POST /api/auth/refresh`

Exchange refresh token for a new access token.

**Request:**
```json
{
  "refresh_token": "eyJhbGciOi..."
}
```

**Response 200:**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_type": "admin"
}
```

---

### `GET /api/auth/me`

Get current authenticated user profile. Returns a unified dict for both admin and end-user tokens.

**Response 200 (admin):**
```json
{
  "id": 1,
  "email": "admin@example.com",
  "role": "admin",
  "user_type": "admin"
}
```

**Response 200 (end-user):**
```json
{
  "id": 5,
  "email": "alice",
  "role": "viewer",
  "user_type": "end_user"
}
```

---

### `PUT /api/auth/password`

Change own password.

**Request:**
```json
{
  "current_password": "oldpass",
  "new_password": "newpass123"
}
```

**Response 200:**
```json
{
  "message": "Password updated."
}
```

---

### `GET /api/auth/users`

List all registered end-users (portal users). **Admin only.**

**Response 200:**
```json
{
  "users": [
    {
      "id": 1,
      "username": "alice",
      "role": "viewer",
      "is_approved": true,
      "is_active": true,
      "allowed_special_users": ["shared", "public"],
      "created_at": "2026-07-04T00:00:00Z",
      "approved_at": "2026-07-04T01:00:00Z"
    }
  ]
}
```

---

### `POST /api/auth/users/register`

Register a new end-user account (no auth required). Requires admin approval before login.

**Request:**
```json
{
  "username": "bob",
  "email": "bob@example.com",
  "password": "secret123",
  "role": "viewer"
}
```

**Response 201:**
```json
{
  "id": 2,
  "username": "bob",
  "role": "viewer",
  "is_approved": false,
  "message": "Registration submitted. Waiting for admin approval."
}
```

---

### `PUT /api/auth/users/{user_id}/approve`

Approve a pending end-user. **Admin only.**

**Response 200:**
```json
{
  "message": "User approved."
}
```

---

### `PUT /api/auth/users/{user_id}`

Update end-user properties (role, active status, allowed special users). **Admin only.**

**Request:**
```json
{
  "role": "admin",
  "is_active": true,
  "allowed_special_users": ["shared", "public", "internal"]
}
```

**Response 200:**
```json
{
  "id": 1,
  "username": "alice",
  "role": "admin",
  "is_active": true,
  "allowed_special_users": ["shared", "public", "internal"]
}
```

---

### `DELETE /api/auth/users/{user_id}`

Delete an end-user. **Admin only.**

**Response 200:**
```json
{
  "message": "User deleted."
}
```

---

### `GET /api/auth/users/deployable`

List users available for deployment (approved + active end-users, plus special users: `shared`, `public`, `internal`).

**Response 200:**
```json
{
  "users": [
    {"username": "alice", "type": "user"},
    {"username": "shared", "type": "special"},
    {"username": "public", "type": "special"},
    {"username": "internal", "type": "special"}
  ]
}
```

---

### `GET /api/auth/verify`

NGINX `auth_request` subrequest endpoint for ACL-based service access control. No authentication required (called by nginx, not browser).

**Request Headers:**
| Header | Description |
|---|---|
| `Cookie: provision_token=<JWT>` | Provision token from `/go/{hostname}` redirect |
| `X-Provision-Token: <JWT>` | Alternative to cookie (for non-browser clients) |

**Response 200 (ACL disabled or passed):**
- When `ENABLE_ACL=false`: returns empty 200 (pass-through to auth_basic).
- When `ENABLE_ACL=true` and JWT valid + ACL passed:
  ```
  Headers: X-Service-Basic: <base64-encoded user:pass>
  ```
  Nginx uses this header for `auth_basic` on the target service.

**Response 401 (no token):**
```json
{
  "detail": "No provision token"
}
```
Headers: `X-Auth-Action: redirect_login`

**Response 401 (token expired):**
```json
{
  "detail": "Token expired"
}
```
Headers: `X-Auth-Action: redirect_token_expired`

**Response 403 (ACL denied):**
```json
{
  "detail": "ACL denied"
}
```
Headers: `X-Auth-Action: redirect_acl_denied`

> **ACL Authorization Rules:**
> - Admins: unrestricted access to all services.
> - Viewers: access to own services + services belonging to users in `allowed_special_users` list.
> - Special users (role=special): blocked from dashboard login; cannot receive provision tokens.

---

### `GET /go/{hostname}`

Dashboard service access redirect. Validates `gateway_token`, checks ACL permissions, creates a `provision_token`, and redirects the browser to the service URL.

**Path Parameters:**
| Param | Description |
|---|---|
| `hostname` | Service hostname (e.g., `myapp-alice-0.localhost`) |

**Response 302:**
Redirects to `http://{hostname}/_set_token?token={provision_token}&redirect=/`

**Errors:**
- `401` — No valid gateway_token
- `403` — ACL denied (viewer cannot access that service)
- `404` — Service not found for given hostname

---

### `POST /api/auth/keys`

Create a new API key for end-user programmatic service access. Returns raw token (shown once only).

**Request:**
```json
{
  "label": "my-ci-key",
  "user_id": 1
}
```
| Field | Type | Description |
|---|---|---|
| `label` | string | Human-readable name for the key (required) |
| `user_id` | int | Target end-user ID (admin only; viewers auto-use their own) |

**Response 201:**
```json
{
  "key": {
    "id": 1,
    "label": "my-ci-key",
    "key_prefix": "pg_...",
    "user_id": 1,
    "is_active": true,
    "created_at": "2026-07-08T00:00:00Z"
  },
  "token": "pg_abc123...",
  "provision_token": "eyJhbGciOi...",
  "message": "Save this token — it will not be shown again."
}
```

**Errors:**
- `400` — label is required
- `403` — Viewer attempting to create a key for another user

---

### `GET /api/auth/keys`

List API keys. Admin sees all keys; viewers see only their own.

**Response 200:**
```json
{
  "keys": [
    {
      "id": 1,
      "label": "my-ci-key",
      "key_prefix": "pg_abc...",
      "user_id": 1,
      "is_active": true,
      "created_at": "2026-07-08T00:00:00Z"
    }
  ]
}
```

---

### `DELETE /api/auth/keys/{key_id}`

Revoke an API key (soft delete — sets `is_active=false`).

**Response 200:**
```json
{
  "revoked": true,
  "key_id": 1
}
```

**Errors:**
- `401` — No valid gateway_token
- `403` — Viewer attempting to revoke another user's key
- `404` — Key not found

---

## 4. System

### `GET /api/system/status`

Comprehensive system health overview. All Docker/container data proxied from provision-api.

**Response 200:**
```json
{
  "provision_api": {
    "status": "healthy",
    "latency_ms": 2.3
  },
  "components": {
    "provision-api": {"running": true, "exists": true, "status": "running"},
    "provision-nginx": {"running": true, "exists": true, "status": "running"},
    "provision-gateway": {"running": true, "exists": true, "status": "running"},
    "provision-dashboard": {"running": true, "exists": true, "status": "running"}
  },
  "docker_host": {
    "containers_total": 45,
    "containers_running": 42,
    "cpu_percent": 23.5,
    "mem_percent": 67.2,
    "disk_percent": 54.0
  },
  "proxy": {
    "enabled": true,
    "reachable": true
  },
  "services_count": 2,
  "users_count": 1,
  "tasks_running": 0,
  "service_stats": {
    "healthy": 2,
    "unhealthy": 0,
    "expected": 2
  },
  "container_stats": {
    "total_expected": 7,
    "healthy_running": 7,
    "unhealthy_running": 0,
    "restarting": 0,
    "down": 0,
    "missing": 0
  }
}
```

> **Note**: `services_count`, `service_stats`, and `container_stats` are derived from provision-api's user registry (not raw `docker ps`). They reflect only provisioned user services, not all Docker containers on the host.

---

### `GET /api/system/stats?detail=true`

Detailed Docker container statistics (proxied to provision-api).

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `detail` | bool | `false` | Include host-level stats |

**Response 200:**
```json
{
  "containers": [
    {
      "name": "provision-api",
      "cpu_percent": 1.2,
      "mem_usage": "89MiB / 1.5GiB",
      "status": "running"
    }
  ],
  "host": {
    "cpu_percent": 23.5,
    "mem_percent": 67.2
  }
}
```

---

### `POST /api/system/reconcile`

Trigger nginx upstream/network reconciliation (proxied to provision-api).

**Response 200:**
```json
{
  "message": "Reconciliation completed.",
  "report": {
    "total_upstreams": 12,
    "reachable": 10,
    "unreachable": 2,
    "networks_reconnected": 1,
    "nginx_reloaded": true
  }
}
```

---

### `GET /api/system/reconcile/status`

Get last reconciliation result (proxied to provision-api).

**Response 200:** Same schema as reconcile response, plus `last_run` timestamp.

---

### `GET /api/system/nginx-state`

Get nginx state from provision-api (proxied).

**Response 200:**
```json
{
  "version": 1,
  "last_updated": "2026-07-08T12:00:00Z",
  "networks": { "..." : "..." },
  "upstreams": [ "..." ]
}
```

---

### `GET /api/system/proxy`

List all proxy configurations.

**Response 200:**
```json
{
  "configs": [
    {
      "id": 1,
      "name": "Host Proxy",
      "protocol": "http",
      "host": "host.docker.internal",
      "port": 7890,
      "is_active": true,
      "reachable": true,
      "last_checked_at": "2026-07-05T12:00:00Z",
      "last_error": null,
      "url": "http://host.docker.internal:7890",
      "created_at": "2026-07-04T00:00:00Z",
      "updated_at": "2026-07-05T12:00:00Z"
    }
  ],
  "active": {
    "id": 1,
    "name": "Host Proxy",
    "protocol": "http",
    "host": "host.docker.internal",
    "port": 7890,
    "is_active": true,
    "reachable": true,
    "url": "http://host.docker.internal:7890"
  }
}
```

---

### `POST /api/system/proxy`

Add a new proxy configuration. Auto-tests reachability after save.

**Request:**
```json
{
  "name": "Office Proxy",
  "protocol": "http",
  "host": "proxy.office.internal",
  "port": 3128,
  "username": "user",
  "password": "pass"
}
```

**Response 201:**
```json
{
  "id": 2,
  "name": "Office Proxy",
  "protocol": "http",
  "host": "proxy.office.internal",
  "port": 3128,
  "is_active": false,
  "reachable": true,
  "last_checked_at": "2026-07-05T12:00:01Z",
  "url": "http://proxy.office.internal:3128"
}
```

---

### `PUT /api/system/proxy/{id}`

Update a proxy configuration.

**Request:** Same as POST. Omit fields to keep existing values.

**Response 200:** Updated proxy config object.

---

### `PUT /api/system/proxy/{id}/activate`

Activate a proxy (deactivates others). Only succeeds if proxy is reachable.

**Response 200:**
```json
{
  "message": "Proxy activated.",
  "config": { "...": "..." }
}
```

**Errors:**
- `400` — Proxy is not reachable

---

### `DELETE /api/system/proxy/{id}`

Delete a proxy configuration.

**Response 200:**
```json
{
  "message": "Proxy deleted."
}
```

---

### `POST /api/system/proxy/test`

Test connectivity to the active proxy.

**Response 200:**
```json
{
  "reachable": true,
  "latency_ms": 12,
  "error": null,
  "checked_at": "2026-07-05T12:00:00Z"
}
```

---

### `GET /api/system/config?key={key}`

Get a system configuration value.

**Response 200:**
```json
{
  "key": "special_users",
  "value": "shared,public,internal"
}
```

---

### `PUT /api/system/config?key={key}`

Set a system configuration value.

**Request:**
```json
{
  "value": "shared,public,internal"
}
```

**Response 200:**
```json
{
  "key": "special_users",
  "value": "shared,public,internal"
}
```

---

### SSL Certificates

All SSL certificate operations are proxied to provision-api.

#### `GET /api/system/ssl-certs`

List available SSL certificate domains.

**Response 200:**
```json
{
  "domains": [
    {
      "domain": "snaprovision.com",
      "fullchain_path": "/etc/letsencrypt/live/snaprovision.com/fullchain.pem",
      "privkey_path": "/etc/letsencrypt/live/snaprovision.com/privkey.pem",
      "created_at": "2026-07-08T00:00:00Z",
      "expiry_date": "2026-10-06T00:00:00Z",
      "days_left": 90
    }
  ]
}
```

#### `POST /api/system/ssl-certs`

Upload SSL certificates for a domain. Supports two modes:
- **Path mode**: Provide `ssl_path` to a directory containing `fullchain.pem` and `privkey.pem`.
- **Paste mode**: Provide `fullchain` and `privkey` PEM content directly.

**Request (Form Data):**
| Field | Type | Description |
|---|---|---|
| `domain` | string | Domain name (required) |
| `ssl_path` | string | Path to Let's Encrypt live directory (path mode) |
| `fullchain` | string | PEM content of fullchain (paste mode) |
| `privkey` | string | PEM content of private key (paste mode) |

**Response 201:**
```json
{
  "message": "SSL cert for snaprovision.com saved",
  "domain": "snaprovision.com",
  "expiry_date": "2026-10-06T00:00:00Z",
  "days_left": 90
}
```

#### `POST /api/system/ssl-certs/{domain}/refresh`

Re-import SSL certificates from the original source path.

**Response 200:**
```json
{
  "message": "SSL cert refreshed",
  "expiry_date": "2026-10-06T00:00:00Z"
}
```

#### `DELETE /api/system/ssl-certs/{domain}`

Delete SSL certificates for a domain.

**Response 200:**
```json
{
  "message": "SSL cert deleted"
}
```

---

### `GET /api/system/subnet-pool`

Get subnet pool usage statistics for the dashboard. **Admin only (requires `require_admin`)** — subnet CIDRs and usage are an admin-dashboard concern (Gap G5). Proxied to provision-api `/subnet-pool`.

**Response 200 (enabled):**
```json
{
  "enabled": true,
  "pools": [
    {
      "cidr": "10.90.0.0/16",
      "total_slots": 65534,
      "used_slots": 42,
      "free_slots": 65492,
      "used_pct": 0.1,
      "exhausted": false
    }
  ],
  "overall": {
    "total_slots": 65534,
    "used_slots": 42,
    "free_slots": 65492
  },
  "allocations": [
    {"user": "alice", "service": "siyuan", "label": "0", "subnet": "10.90.0.2"}
  ],
  "headroom": 256
}
```

**Response 200 (disabled — `SUBNET_POOLS` not set):**
```json
{
  "enabled": false,
  "pools": [],
  "headroom": 256,
  "message": "Subnet management disabled"
}
```

---

## 5. Services (Source Projects)

### `GET /api/services`

List all service source projects.

**Response 200:**
```json
{
  "services": [
    {
      "name": "siyuan",
      "path": "/srv/provision/source_projects/siyuan",
      "files": ["Dockerfile", "docker-compose.yml.j2", "nginx.conf.j2"],
      "generated_files": [],
      "template_files": [],
      "has_compose_template": true,
      "has_nginx_template": true,
      "active_users": 1,
      "active_instances": ["alice/0"],
      "created_at": "2026-07-04T10:00:00Z"
    }
  ]
}
```

---

### `GET /api/services/templates`

List all available service templates from the database (service_templates table). Backend `mode: template` + this endpoint are RETAINED (iter-1); note the "From Template" tab was removed from the Add Project modal (GAP-1), so the endpoint is now consumed via the API only, not the UI.

**Response 200:**
```json
{
  "templates": [
    {
      "id": 1,
      "name": "wordpress",
      "description": "WordPress with MySQL",
      "category": "cms",
      "icon": "FileTextOutlined",
      "is_builtin": true,
      "created_at": "2026-07-29T00:00:00Z"
    }
  ]
}
```

---

### `GET /api/services/notifications`

Get project detection events from the background project monitor loop (polls source_projects every 10s for new directories).

**Response 200:**
```json
{
  "notifications": [
    {
      "project_name": "new-project",
      "detected_at": "2026-07-29T12:00:05Z",
      "acknowledged": false
    }
  ],
  "count": 1
}
```

---

### `POST /api/services`

Create a new service project. Three modes:

**Mode 1 — From Git:**
```json
{
  "mode": "git",
  "repo_url": "https://github.com/user/repo.git",
  "branch": "main",
  "name": "myapp",
  "use_proxy": false
}
```

**Mode 2 — From Upload:**
```json
{
  "mode": "upload",
  "name": "myapp",
  "files": {
    "docker-compose.yml": "services:\n  web:\n    ...",
    "nginx.conf": "server { ... }",
    ".env": "PORT=8000",
    "Dockerfile": "FROM python:3.13-slim\n..."
  }
}
```

**Mode 3 — From ZIP:**
```json
{
  "mode": "upload",
  "name": "myapp",
  "zip_content": "base64_encoded_zip..."
}
```

**Mode 4 — From Template (DB):**
```json
{
  "mode": "template",
  "name": "myapp",
  "template_id": 1
}
```
> Note: supported at the API level and retained by iter-1 (GAP-1). The UI no longer exposes this mode — the "From Template" tab was removed from the Add Project modal; the modal now offers From Git + Upload Zip only.

**Response 201:**
```json
{
  "name": "myapp",
  "path": "/srv/provision/source_projects/myapp",
  "files": ["docker-compose.yml", "nginx.conf", ".env", "Dockerfile"],
  "llm_generated": ["nginx.conf"]
}
```

---

### `GET /api/services/{name}`

Get service project details with file list.

**Response 200:**
```json
{
  "name": "siyuan",
  "path": "/srv/provision/source_projects/siyuan",
  "files": [
    {"name": "Dockerfile", "is_generated": false},
    {"name": "docker-compose.yml.j2", "is_generated": false},
    {"name": "nginx.conf.j2", "is_generated": false}
  ],
  "has_compose_template": true,
  "has_nginx_template": true,
  "active_instances": ["alice/0"]
}
```

---

### `GET /api/services/{name}/check-missing-files`

Check which essential deployment files are missing for a service project. **Admin only.** Proxied to provision-api (`/services/{name}/check-missing-files`), then enriched with repo scan context for LLM-based file generation.

**Path Parameters:**
| Param | Description |
|---|---|
| `name` | Service project name (e.g., `myapp`) |

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `recipe_path` | string | `""` | Optional subdirectory for multi-recipe projects. When provided, files are checked inside `source_projects/{name}/{recipe_path}/`. The gateway forwards `recipe_path` to provision-api. |

**Response 200:**
```json
{
  "service_name": "myapp",
  "project_root": "/srv/provision/source_projects/myapp",
  "ready": false,
  "missing": ["docker-compose", "nginx.conf"],
  "existing": ["Dockerfile", ".env"],
  "scan_context": {
    "repo_description": "A Python FastAPI application with Redis caching",
    "repo_files": ["Dockerfile", "requirements.txt", "main.py"],
    "port": 8000,
    "needs_db": false,
    "needs_cache": true,
    "needs_volume": false,
    "language": "Python",
    "framework": "FastAPI",
    "has_dockerfile": true,
    "has_compose": false,
    "has_nginx_conf": false,
    "compose_services": []
  }
}
```

`missing`/`existing` values: `docker-compose`, `nginx.conf`, `Dockerfile`, `.env`. `scan_context` is present only when the (recipe) directory exists and a repo scan succeeds; it provides context for the LLM to auto-generate the missing files.

**Errors:**
- `404` — Service `{name}` not found (or recipe `{recipe_path}` not found when provided)
- `502` — provision-api error

---

### `DELETE /api/services/{name}?force=false`

Delete a service project.

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `force` | bool | `false` | Force delete even if active instances exist |

**Response 200:**
```json
{
  "deleted": true
}
```

**Errors:**
- `409` — Active users exist; use `?force=true` to override

---

### `GET /api/services/{name}/files/{filename}`

Read a file from a service project.

**Path Parameters:**
| Param | Description |
|---|---|
| `filename` | File path relative to project root (e.g., `docker-compose.yml.j2`) |

**Response 200:**
```json
{
  "filename": "docker-compose.yml.j2",
  "content": "services:\n  siyuan:\n    ...",
  "size_bytes": 1234
}
```

---

### `PUT /api/services/{name}/files/{filename}`

Write/update a file in a service project.

**Request:**
```json
{
  "content": "services:\n  siyuan:\n    container_name: {{ container_prefix }}siyuan\n    ..."
}
```

**Response 200:**
```json
{
  "filename": "docker-compose.yml.j2",
  "written": true
}
```

---

### `POST /api/services/{name}/files/{filename}`

Create a new file in a service project. Annotates the file with a `.generated` marker to indicate LLM-generated origin.

**Request:**
```json
{
  "content": "FROM python:3.13-slim\n..."
}
```

**Response 201:**
```json
{
  "filename": "Dockerfile",
  "created": true
}
```

---

### `DELETE /api/services/{name}/files/{filename}`

Delete a file from a service project.

**Response 200:**
```json
{
  "filename": "docker-compose.yml",
  "deleted": true
}
```

**Errors:**
- `404` — File not found in the service project

---

### `POST /api/services/{name}/convert`

Convert plain docker-compose.yml and nginx.conf to Jinja2 templates.

**Request:**
```json
{
  "compose_file": "docker-compose.yml",
  "nginx_file": "nginx.conf"
}
```

**Response 200:**
```json
{
  "compose_template": "docker-compose.siyuan.yml.j2",
  "nginx_template": "siyuan.nginx.conf.j2",
  "message": "Templates created successfully."
}
```

---

### `POST /api/services/scan`

Scan a directory for repository context (used for LLM config generation).

**Request:**
```json
{
  "directory": "/srv/provision/source_projects/myapp"
}
```

**Response 200:**
```json
{
  "directory": "/srv/provision/source_projects/myapp",
  "repo_description": "A Python FastAPI application with Redis caching",
  "repo_files": ["Dockerfile", "requirements.txt", "main.py"],
  "port": 8000,
  "needs_db": false,
  "needs_cache": true,
  "language": "Python",
  "framework": "FastAPI",
  "has_dockerfile": true,
  "has_compose": false,
  "has_nginx_conf": false,
  "has_env_file": false
}
```

---

### `POST /api/services/save-generated`

Save LLM-generated files to a service project.

**Request:**
```json
{
  "service_name": "myapp",
  "recipe_path": "recipes/ollama",
  "files": {
    "docker-compose.yml": "services:\n  web:\n    ...",
    "nginx.conf": "server { ... }"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `service_name` | string | yes | Service project name |
| `recipe_path` | string | no | Optional subdirectory for multi-recipe projects. When present, files are written into `source_projects/{service_name}/{recipe_path}/` (the directory is created if it does not exist). |
| `files` | object | yes | Map of `{filename: content}` to save |

**Response 200:**
```json
{
  "message": "Generated files saved.",
  "files": ["docker-compose.yml", "nginx.conf"]
}
```

---

### `POST /api/services/check-deploy`

Check if a service is ready for deployment. Auto-generates missing required files via LLM if configured.

**Request:**
```json
{
  "service_name": "myapp"
}
```

**Response 200:**
```json
{
  "ready": true,
  "missing_files": [],
  "generated_files": {}
}
```

---

### `GET /api/services/{name}/git/status`

Get git status for a service project.

**Response 200:**
```json
{
  "status": " M docker-compose.yml.j2\n?? new-file.txt",
  "files": {
    "docker-compose.yml.j2": "M",
    "new-file.txt": "??"
  }
}
```

---

### `GET /api/services/{name}/git/diff?file={filename}`

Get git diff for a service project or specific file.

**Response 200:**
```json
{
  "diff": "diff --git a/docker-compose.yml.j2 b/docker-compose.yml.j2\n..."
}
```

---

### `GET /api/services/{name}/git/head-file?file={filename}`

Get the HEAD (committed) version of a file.

**Response 200:**
```json
{
  "filename": "docker-compose.yml.j2",
  "content": "services:\n  siyuan:\n    ..."
}
```

---

## 6. Users (End-User Provisioning)

### `GET /api/users`

List all end-users and their deployed services (proxied from provision-api, enriched).

**Response 200:**
```json
{
  "users": {
    "alice": {
      "healthy_services": [
        {
          "service_name": "siyuan",
          "label": "0",
          "containers": [
            {"name": "siyuan-user_alice-0-main", "status": "running", "image": "siyuan:latest"}
          ],
          "compose_file": "docker-compose.user-alice.0.yml",
          "nginx_conf": "siyuan.user-alice.0.nginx.conf",
          "env_file": ".env.alice.0",
          "url": "https://siyuan-alice-0.snaprovision.com",
          "http_url": "http://siyuan-alice-0.snaprovision.com",
          "https_enabled": true,
          "ssl": {
            "fullchain": "/srv/provision/ssl/snaprovision.com/fullchain.pem",
            "privkey": "/srv/provision/ssl/snaprovision.com/privkey.pem"
          }
        }
      ],
      "unhealthy_services": [
        {
          "service_name": "siyuan-mcp",
          "label": "0",
          "containers": [
            {"name": "siyuan-mcp-user_alice-0-server", "status": "exited"}
          ]
        }
      ],
      "missing_services": []
    }
  }
}
```

---

### `GET /api/users/{user_name}`

Get a single user's deployed services.

**Response 200:** Same structure as a single user entry from `GET /api/users`.

---

### `POST /api/users/deploy`

Deploy a service to a user. Creates an async task.

**Request:**
```json
{
  "user_name": "alice",
  "service_name": "siyuan",
  "project_root": "siyuan",
  "compose_file_path": "docker-compose.siyuan.yml.j2",
  "nginx_conf_file_path": "siyuan.nginx.conf.j2",
  "env_file_path": ".env",
  "label": "0",
  "domain": "snaprovision.com",
  "passwd": "secret",
  "volumes": {
    "workspace": "/srv/provision/user-data/alice/siyuan"
  },
  "build_args": {},
  "use_global_proxy": false,
  "https": true,
  "fullchain": "/srv/provision/ssl/snaprovision.com/fullchain.pem",
  "privkey": "/srv/provision/ssl/snaprovision.com/privkey.pem"
}
```

**Response 202:**
```json
{
  "task_id": "abc123def456",
  "status": "pending",
  "type": "register",
  "message": "Deployment started. Track progress in Tasks."
}
```

---

### `GET /api/users/{user_name}/{service_name}/next-label`

Get the next auto-incremented label for deploying a service to a user. Queries existing instances for the same user+service and returns max+1.

**Response 200:**
```json
{
  "label": "1",
  "source": "auto_increment"
}
```

---

### `DELETE /api/users/{user_name}/{service_name}/{label}`

Remove a user's deployed service instance.

**Response 202:**
```json
{
  "task_id": "abc123def456",
  "status": "pending",
  "type": "remove"
}
```

---

### `POST /api/users/{user_name}/{service_name}/{label}/rebuild`

Rebuild a user's service containers.

**Request:**
```json
{
  "no_cache": true,
  "build_args": {
    "HTTP_PROXY": "http://proxy:8080"
  }
}
```

**Response 202:**
```json
{
  "task_id": "abc123def456",
  "status": "pending",
  "type": "rebuild"
}
```

---

### `POST /api/users/{user_name}/{service_name}/{label}/up`

Start a user's service containers (docker compose up -d).

**Response 200:**
```json
{
  "message": "Service started successfully.",
  "service": "siyuan",
  "user": "alice",
  "label": "0"
}
```

---

### `POST /api/users/{user_name}/{service_name}/{label}/down`

Stop a user's service containers (docker compose stop).

**Response 200:**
```json
{
  "message": "Service stopped successfully.",
  "service": "siyuan",
  "user": "alice",
  "label": "0"
}
```

---

### `PUT /api/users/{user_name}/{service_name}/{label}/password`

Change the HTTP basic auth password for a user's service.

**Request:**
```json
{
  "passwd": "newsecret"
}
```

**Response 200:**
```json
{
  "message": "Password updated. Nginx reloaded."
}
```

---

### `GET /api/users/{user_name}/{service_name}/{label}/containers/{container}/logs?tail=100`

Get container logs for a specific compose service (proxied to provision-api).

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `tail` | int | 100 | Number of log lines to return (1–10000) |

**Response 200:**
```json
{
  "container": "siyuan-user_alice-0-siyuan",
  "logs": "2026-07-08T12:00:00Z Starting siyuan...\n2026-07-08T12:00:01Z Boot complete.\n"
}
```

---

### `GET /api/users/{user_name}/{service_name}/{label}/url`

Get the accessible URL(s) for a user's service.

**Response 200:**
```json
{
  "url": "https://siyuan-alice-0.snaprovision.com",
  "http_url": "http://siyuan-alice-0.snaprovision.com",
  "https_enabled": true,
  "auth_enabled": true,
  "nginx_http_port": 80,
  "nginx_https_port": 443
}
```

---

### `POST /api/users/{user_name}/{service_name}/{label}/test-curl`

Test connectivity to a user's service URL from within the gateway container.

**Request:**
```json
{
  "include_auth": true,
  "follow_redirect": true
}
```

**Response 200:**
```json
{
  "url": "https://siyuan-alice-0.snaprovision.com",
  "http_code": 200,
  "headers": {
    "content-type": "text/html; charset=utf-8",
    "server": "nginx"
  },
  "body_preview": "<!DOCTYPE html><html>...",
  "time_total_ms": 45.2,
  "error": null
}
```

---

### `POST /api/users/clone`

Clone all services from one user to another.

**Request:**
```json
{
  "source_user": "alice",
  "target_user": "bob",
  "domain": "snaprovision.com",
  "passwd": "secret",
  "volume_base_override": "/srv/provision/user-data/bob"
}
```

**Response 202:**
```json
{
  "tasks": [
    {"service": "siyuan", "label": "0", "task_id": "abc123"},
    {"service": "siyuan-mcp", "label": "0", "task_id": "def456"}
  ],
  "total": 2
}
```

---

### `GET /api/users/{user_name}/{service_name}/{label}/volume-usage`

Get disk usage for a service instance's volume directories. Computes directory size by walking files and reports filesystem-level disk usage.

**Response 200:**
```json
{
  "user_name": "alice",
  "service_name": "siyuan",
  "label": "0",
  "user_data_dir": "/srv/provision/user_data/alice/siyuan",
  "volumes": {
    "workspace": {
      "path": "/srv/provision/user_data/alice/siyuan/workspace",
      "size_bytes": 1048576,
      "disk_total_bytes": 107374182400,
      "disk_used_bytes": 21474836480,
      "disk_free_bytes": 85899345920
    }
  }
}
```

---

### `GET /api/users/{user_name}/{service_name}/{label}/deployment-files`

List deployment files (compose, nginx, env) for a service instance.

**Response 200:**
```json
{
  "files": {
    "compose": "docker-compose.user-alice.0.yml",
    "nginx": "siyuan.user-alice.0.nginx.conf",
    "env": ".env.alice.0"
  }
}
```

#### `GET /api/users/{user_name}/{service_name}/{label}/deployment-files/{type}`

Get the content of a specific deployment file. Valid types: `compose`, `nginx`, `env`.

#### `PUT /api/users/{user_name}/{service_name}/{label}/deployment-files/{type}`

Update a deployment file for a service instance.

**Request:**
```json
{
  "content": "..."
}
```

---

### `GET /api/users/{user_name}/{service_name}/{label}/registration-time`

Get the registration timestamp for a service instance (most recent successful register task).

**Response 200:**
```json
{
  "registration_time": "2026-07-08T12:00:00Z"
}
```

---

## 7. Tasks

### `GET /api/tasks`

List all async tasks (proxied from provision-api).

**Response 200:**
```json
{
  "tasks": [
    {
      "id": "abc123def456",
      "type": "rebuild",
      "target": "alice/siyuan/0",
      "status": "failed",
      "created_at": "2026-07-05T12:00:00Z",
      "updated_at": "2026-07-05T12:13:27Z"
    }
  ]
}
```

---

### `GET /api/tasks/{task_id}`

Get a single task's status.

**Response 200:** Single task object (same schema as list item).

---

### `DELETE /api/tasks/{task_id}`

Cancel a pending or running task.

**Response 200:**
```json
{
  "message": "Task cancelled.",
  "task_id": "abc123def456"
}
```

---

### `GET /api/tasks/{task_id}/log?tail=200&follow=true`

Stream task build log via Server-Sent Events.

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `tail` | int | `200` | Number of recent lines to send first |
| `follow` | bool | `true` | Continue streaming new lines |

**Response:** `text/event-stream`

**SSE Event format:**
```
data: {"line": "Step 1/5 : FROM python:3.13-slim", "timestamp": "2026-07-05T12:00:01Z"}

data: {"line": " ---> abc123def456", "timestamp": "2026-07-05T12:00:02Z"}
```

The endpoint filters log lines by the task's context (user_name/service_name) and polls the global `DOCKER_OPS_LOG` file every 1 second for new matching lines.

---

## 8. LLM Configuration

### `GET /api/llm/configs`

List all LLM configurations and the active one.

**Response 200:**
```json
{
  "configs": [
    {
      "id": 1,
      "mode": "byok",
      "byok_base_url": "https://api.deepseek.com/v1",
      "byok_model": "deepseek-chat",
      "byok_api_key_masked": "sk-...xxxx",
      "is_active": true,
      "updated_at": "2026-07-05T12:00:00Z"
    }
  ],
  "active": { "...": "..." }
}
```

---

### `POST /api/llm/configs`

Create a new LLM configuration.

**Request:**
```json
{
  "mode": "byok",
  "byok_base_url": "https://api.openai.com/v1",
  "byok_model": "gpt-4o",
  "byok_api_key": "sk-abc123...",
  "system_prompt": "You are a DevOps assistant specializing in Docker and Nginx."
}
```
> Local-agent fields (`mode='local_agent'`, `agent_url`, `agent_model`) are deferred at the API level (GAP-2, iter-1): `create_config`/`save_config` normalize any `mode='local_agent'` to `byok` and never persist `agent_url`/`agent_model`.

**Response 201:**
```json
{
  "id": 2,
  "mode": "byok",
  "byok_base_url": "https://api.openai.com/v1",
  "byok_model": "gpt-4o",
  "byok_api_key_masked": "sk-...c123",
  "is_active": false
}
```

---

### `PUT /api/llm/configs/{id}/activate`

Activate an LLM configuration (deactivates all others).

**Response 200:**
```json
{
  "message": "LLM config activated.",
  "config": { "...": "..." }
}
```

---

### `DELETE /api/llm/configs/{id}`

Delete an LLM configuration.

**Response 200:**
```json
{
  "message": "LLM config deleted."
}
```

---

### `GET /api/llm/config`

Get the current active LLM configuration (backward-compat alias).

**Response 200:** Same as `configs[active]` from `/api/llm/configs`.

---

### `PUT /api/llm/config`

Create or update the active LLM configuration (backward-compat).

**Request:** Same as `POST /api/llm/configs`.

**Response 200:**
```json
{
  "updated": true,
  "config": { "...": "..." }
}
```

---

### `POST /api/llm/test`

Test the active LLM connection.

**Response 200:**
```json
{
  "success": true,
  "latency_ms": 450,
  "model": "deepseek-chat",
  "response_preview": "Hello! I'm ready to help with DevOps tasks."
}
```

---

### `POST /api/llm/generate`

Generate configuration files via LLM.

**Request:**
```json
{
  "type": "docker_compose",
  "generate_type": "docker_compose",
  "context": {
    "repo_description": "A Python FastAPI app with Redis caching",
    "repo_files": ["Dockerfile", "requirements.txt", "main.py"],
    "port": 8000,
    "needs_db": false,
    "needs_cache": true
  }
}
```

**Valid types / generate_types:**
- `docker_compose` — Generate docker-compose.yml
- `nginx_conf` — Generate nginx.conf
- `env_file` — Generate .env file
- `dockerfile` — Generate Dockerfile
- `troubleshoot` — Chat-style troubleshooting assistance
- `service_config` — Generate full service configuration

**Response 200:**
```json
{
  "generated_content": "services:\n  web:\n    build: .\n    ports:\n      - \"8000:8000\"\n    ...",
  "filename_suggestion": "docker-compose.yml",
  "warnings": []
}
```

---

## 9. Audit

### `GET /api/audit`

Query audit logs with filters.

**Query Parameters:**
| Param | Type | Description |
|---|---|---|
| `admin_id` | int | Filter by admin user ID |
| `action` | string | Filter by action type |
| `target_user` | string | Filter by target user name |
| `from` | ISO date | Start date (inclusive) |
| `to` | ISO date | End date (inclusive) |
| `limit` | int | Max results (default: 50) |
| `offset` | int | Pagination offset (default: 0) |

**Supported action types:**
`register`, `remove`, `rebuild`, `deploy`, `clone`, `config_edit`, `admin_create`, `password_change`, `llm_config`, `service_create`, `service_delete`, `proxy_config`, `reconcile`

**Response 200:**
```json
{
  "total": 142,
  "limit": 50,
  "offset": 0,
  "entries": [
    {
      "id": 142,
      "admin_email": "admin@example.com",
      "action": "register",
      "target_user": "alice",
      "target_service": "siyuan",
      "target_label": "0",
      "detail_json": "{\"domain\":\"snaprovision.com\",\"https\":true}",
      "status": "success",
      "error_message": null,
      "ip_address": "172.18.0.1",
      "created_at": "2026-07-05T12:00:00Z"
    }
  ]
}
```
