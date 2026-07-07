# Provision Gateway — API Reference

> **Version**: 1.1
> **Date**: 2026-07-08 (updated)
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

- All endpoints except `/health`, `/api/auth/setup`, `/api/auth/login`, `/api/auth/refresh`, and `/api/auth/users/register` require `Authorization: Bearer <JWT>` header
- JWT access tokens expire after 1 hour (configurable via `JWT_EXPIRE_SEC`)
- Refresh tokens expire after 7 days (`JWT_REFRESH_EXPIRE_SEC`)
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

Authenticate and receive tokens. Supports both gateway admin accounts (by email) and end-user portal accounts (by username).

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

**Errors:**
- `401` — "Invalid email/username or password"

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
  "files": {
    "docker-compose.yml": "services:\n  web:\n    ...",
    "nginx.conf": "server { ... }"
  }
}
```

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
      "agent_url": null,
      "agent_model": null,
      "is_active": true,
      "system_prompt": "You are a DevOps assistant...",
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
  "agent_url": "",
  "agent_model": "",
  "system_prompt": "You are a DevOps assistant specializing in Docker and Nginx."
}
```

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
