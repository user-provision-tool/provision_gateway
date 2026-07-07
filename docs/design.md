# Provision Gateway — Product Design Document

> **Version**: 1.2
> **Date**: 2026-07-08 (updated — post-deduplication refactor)
> **Status**: Implemented — reflects current codebase
> **Depends on**: [requirements.md](./requirements.md) (CONFIRMED)
> **See also**: [architecture.md](./architecture.md) | [api_references.md](./api_references.md) | [tests_coverage_status.md](./tests_coverage_status.md) | [changes-provision_gateway-20260708.md](./changes-provision_gateway-20260708.md)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Container Specifications](#2-container-specifications)
3. [Directory Structure](#3-directory-structure)
4. [Database Schema](#4-database-schema)
5. [API Specification](#5-api-specification)
6. [Frontend Design](#6-frontend-design)
7. [Key Module Designs](#7-key-module-designs)
8. [provision-api Gap List](#8-provision-api-gap-list)
9. [Implementation Plan](#9-implementation-plan)

---

## 1. Architecture Overview

### 1.1 System Context

```
┌────────────────────────────────────────────────────────────────────┐
│                          Docker Host                                │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    provision_default                          │ │
│  │                                                               │ │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐        │ │
│  │  │provision-api│  │provision-    │  │provision-    │        │ │
│  │  │  FastAPI    │  │nginx         │  │gateway       │        │ │
│  │  │  :8765      │  │ :80 / :443   │  │ FastAPI:8770 │        │ │
│  │  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘        │ │
│  │         │                │                  │                 │ │
│  │         │    ┌───────────┴──────────┐       │                 │ │
│  │         │    │ per-user networks    │       │  /api/* proxy   │ │
│  │         │    │ (myapp-user_alice-0) │       │                 │ │
│  │         │    └──────────────────────┘       │                 │ │
│  │         │                                   │                 │ │
│  │  ┌──────┴───────────────────────────────────┴──────┐          │ │
│  │  │              provision-dashboard                │          │ │
│  │  │         nginx:alpine serving React SPA          │          │ │
│  │  │         :8771 → 127.0.0.1:8771 (localhost)     │          │ │
│  │  └────────────────────────────────────────────────┘          │ │
│  │                                                               │ │
│  │  ┌──────────────────┐                                        │ │
│  │  │  (optional)      │  ← user-deployed proxy container       │ │
│  │  │  proxy container  │     (e.g. squid, v2ray, clash)        │ │
│  │  │  e.g. squid:3128 │     on provision_default network       │ │
│  │  └──────────────────┘                                        │ │
│  │                                                               │ │
│  │  ┌──────────────────┐                                        │ │
│  │  │  provision-mcp   │  ← MCP server for external AI agents   │ │
│  │  │  FastAPI :8780   │     (SSE streaming, session-based)     │ │
│  │  └──────────────────┘                                        │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─────────────┐                                                   │
│  │ /var/run/   │  ← Docker socket (mounted into provision-gateway) │
│  │ docker.sock │                                                   │
│  └─────────────┘                                                   │
│                                                                    │
│  ┌─────────────┐                                                   │
│  │ PROVISION_  │  ← shared data (mounted into gateway + nginx +    │
│  │ DIR         │     api containers)                               │
│  └─────────────┘                                                   │
└────────────────────────────────────────────────────────────────────┘
```

### 1.2 Data Flow

```
 Browser (127.0.0.1:8771)
     │
     │  HTTP (localhost only)
     ▼
 provision-dashboard (nginx:alpine)
     │
     │  /api/* → http://provision-gateway:8770
     │  /*     → static React files
     ▼
 provision-gateway (FastAPI :8770)
     │
     ├─→ http://provision-api:8765          (REST calls to provision-api)
     ├─→ /var/run/docker.sock               (docker ps, docker stats, docker network)
     ├─→ $PROVISION_DIR/source_projects/    (file read/write for service files)
     ├─→ $PROVISION_DIR/generated/          (read registry, nginx confs, htpasswd)
     ├─→ $GATEWAY_DATA_DIR/gateway.db       (SQLite — admin, end_users, audit, LLM config, proxy config)
     ├─→ $PROVISION_DIR/provision_nginx_state.json  (network recording)
     └─→ LLM endpoint (local agent or BYOK) (OpenAI-compatible /v1/chat/completions)

 provision-mcp (FastAPI :8780)
     │
     ├─→ http://provision-gateway:8770/api/*  (deploy workflow via gateway)
     └─→ In-memory session store              (deployment sessions, not persisted)
```

### 1.3 Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| Gateway backend | Python 3.13 + FastAPI | Consistent with provision-api; shared mental model |
| Gateway DB ORM | SQLAlchemy + Alembic | Mature, well-supported |
| Gateway DB | SQLite | Zero-config, no extra container, sufficient for admin data |
| Auth | python-jose (JWT) + bcrypt | JWT with bcrypt directly (passlib incompatible with bcrypt 5.x) |
| LLM client | httpx (async) → OpenAI-compatible `/v1/chat/completions` | Generic protocol; works with Ollama, OpenAI, Anthropic via gateways |
| Encryption (BYOK key) | cryptography (AES-256-GCM) | Industry standard for at-rest key storage |
| Docker SDK | docker-py | Programmatic Docker API access (health checks, stats, events) |
| Git operations | GitPython | Clone repos, shallow clone support |
| Dashboard | React 18 + TypeScript | Confirmed by admin |
| Dashboard UI kit | Ant Design 5 | Rich component library, table/form/modal heavy use case |
| Dashboard code editor | Monaco Editor | For YAML/nginx/env file editing |
| Dashboard HTTP | Axios + React Query | API calls with caching and polling |
| Dashboard bundler | Vite | Fast dev server, optimized builds |
| Dashboard serve | nginx:alpine | Serves static build + proxies /api/* → gateway |
| Gateway serve | uvicorn | ASGI server, same as provision-api |

---

## 2. Container Specifications

### 2.1 provision-gateway

```yaml
# docker-compose.gateway.yml (excerpt)
services:
  provision-gateway:
    build:
      context: ./provision-gateway
      dockerfile: Dockerfile
    container_name: provision-gateway
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock          # Docker API access
      - ${PROVISION_DIR}:${PROVISION_DIR}                   # same-path bind mount
      - ${GATEWAY_DATA_DIR:-${PROVISION_DIR}/gateway_data}:/data  # SQLite + uploads
    environment:
      - PROVISION_DIR=${PROVISION_DIR}
      - GATEWAY_DATA_DIR=/data
      - PROVISION_API_URL=http://provision-api:8000
      - GATEWAY_SECRET_KEY=${GATEWAY_SECRET_KEY}             # for JWT + encryption
      - NGINX_HTTP_PORT=${NGINX_HTTP_PORT:-80}
      - NGINX_HTTPS_PORT=${NGINX_HTTPS_PORT:-443}
      - DOCKER_OPS_LOG=${PROVISION_DIR}/generated/docker_ops.log
    networks:
      - provision_default
    restart: unless-stopped
    # No host port mapping — only reachable via Docker DNS from dashboard
```

**Dockerfile key points:**
- Multi-stage: `python:3.13-slim` base, `uv` for deps
- Same pattern as provision-api: copy `docker` CLI binary from `docker:cli` if needed (for subprocess fallback), but prefer `docker-py` SDK
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8770"]`

### 2.2 provision-dashboard

```yaml
# docker-compose.gateway.yml (excerpt)
services:
  provision-dashboard:
    build:
      context: ./provision-dashboard
      dockerfile: Dockerfile
    container_name: provision-dashboard
    ports:
      - "127.0.0.1:${DASHBOARD_PORT:-8771}:80"    # localhost only!
    networks:
      - provision_default
    restart: unless-stopped
```

**Dockerfile key points:**
- Stage 1: Node 20 → `npm ci` → `npm run build` (Vite)
- Stage 2: `nginx:alpine` → copy `dist/` → copy `nginx.conf` with API proxy
- Nginx config proxies `/api/*` → `http://provision-gateway:8770`

```nginx
# provision-dashboard nginx.conf
server {
    listen 80;
    server_name localhost;

    # React SPA static files
    root /usr/share/nginx/html;
    index index.html;

    # API proxy to gateway
    location /api/ {
        proxy_pass http://provision-gateway:8770;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # SSE support for log streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

### 2.3 docker-compose.gateway.yml (full)

```yaml
version: "3.8"

services:
  provision-gateway:
    build:
      context: ./provision-gateway
      dockerfile: Dockerfile
    container_name: provision-gateway
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ${PROVISION_DIR}:${PROVISION_DIR}
      - gateway_data:/data
    environment:
      - PROVISION_DIR=${PROVISION_DIR}
      - GATEWAY_DATA_DIR=/data
      - PROVISION_API_URL=http://provision-api:8000
      - GATEWAY_SECRET_KEY=${GATEWAY_SECRET_KEY:?err}
      - NGINX_HTTP_PORT=${NGINX_HTTP_PORT:-80}
      - NGINX_HTTPS_PORT=${NGINX_HTTPS_PORT:-443}
      - DOCKER_OPS_LOG=${PROVISION_DIR}/generated/docker_ops.log
    networks:
      - users_provision_default
    restart: unless-stopped

  provision-dashboard:
    build:
      context: ./provision-dashboard
      dockerfile: Dockerfile
    container_name: provision-dashboard
    ports:
      - "127.0.0.1:${DASHBOARD_PORT:-8771}:80"
    networks:
      - users_provision_default
    restart: unless-stopped

volumes:
  gateway_data:
    name: provision_gateway_data

networks:
  users_provision_default:
    external: true
    name: users_provision_default
```

> **Note**: `users_provision_default` network is created by `docker-compose.provision.yml` (the existing provision-api stack). The gateway compose file uses `external: true` to join that network.

---

## 3. Directory Structure

### 3.1 Repository Layout

```
_provision_gateway/
├── README.md                           # This project overview
├── requirements.md                     # Formalized requirements (CONFIRMED)
├── design.md                           # This file — product design
│
├── docker-compose.gateway.yml          # Gateway + Dashboard deployment
│
├── provision-gateway/                  # Backend container source
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   │
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                     # FastAPI app, lifespan, middleware
│   │   ├── config.py                   # Settings from env vars
│   │   ├── database.py                 # SQLAlchemy engine + session
│   │   │
│   │   ├── models/                     # SQLAlchemy ORM models
│   │   │   ├── __init__.py
│   │   │   ├── admin.py                # AdminUser model
│   │   │   ├── llm_config.py           # LLMConfig model
│   │   │   ├── audit_log.py            # AuditLog model
│   │   │   └── service_template.py     # ServiceTemplate model
│   │   │
│   │   ├── schemas/                    # Pydantic request/response schemas
│   │   │   ├── __init__.py
│   │   │   ├── auth.py
│   │   │   ├── system.py
│   │   │   ├── services.py
│   │   │   ├── users.py
│   │   │   ├── tasks.py
│   │   │   ├── llm.py
│   │   │   └── audit.py
│   │   │
│   │   ├── routers/                    # FastAPI route modules
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                 # /api/auth/*
│   │   │   ├── system.py               # /api/system/*
│   │   │   ├── services.py             # /api/services/*
│   │   │   ├── users.py                # /api/users/*
│   │   │   ├── tasks.py                # /api/tasks/*
│   │   │   ├── llm.py                  # /api/llm/*
│   │   │   └── audit.py                # /api/audit/*
│   │   │
│   │   ├── services/                   # Business logic layer
│   │   │   ├── __init__.py
│   │   │   ├── auth_service.py         # Admin + End-user auth, JWT, password hashing
│   │   │   ├── provision_service.py    # All proxy calls to provision-api (Docker, reconciliation, SSL, users)
│   │   │   ├── service_manager.py      # File ops, git clone, template conversion (delegated)
│   │   │   ├── llm_service.py          # LLM client, config generation
│   │   │   ├── curl_service.py         # Test-curl from within container
│   │   │   ├── audit_service.py        # Audit log writer + querier
│   │   │   └── proxy_service.py        # Proxy config CRUD + env injection
│   │   │
│   │   ├── middleware/
│   │   │   └── __init__.py             # JWT verification (get_current_admin, get_current_user)
│   │   │
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── crypto.py               # AES-256-GCM encrypt/decrypt for BYOK key
│   │       └── file_scanner.py          # Scan repo for Dockerfile, compose, etc.
│   │
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_auth.py
│       ├── test_services.py
│       ├── test_provision.py
│       ├── test_llm.py
│       ├── test_docker.py
│       ├── test_reconciliation.py
│       └── test_curl.py
│
├── provision-dashboard/                # Frontend container source
│   ├── Dockerfile
│   ├── nginx.conf                      # SPA serve + /api/* proxy
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   │
│   ├── src/
│   │   ├── main.tsx                    # React entry point
│   │   ├── App.tsx                     # Root layout + routing
│   │   ├── api/                        # API client layer
│   │   │   ├── client.ts              # Axios instance, interceptors, JWT
│   │   │   ├── auth.ts
│   │   │   ├── system.ts
│   │   │   ├── services.ts
│   │   │   ├── users.ts
│   │   │   ├── tasks.ts
│   │   │   ├── llm.ts
│   │   │   └── audit.ts
│   │   │
│   │   ├── hooks/                     # Custom React hooks
│   │   │   ├── useAuth.ts
│   │   │   ├── usePolling.ts          # Generic polling hook
│   │   │   ├── useSSE.ts              # Server-Sent Events for log streaming
│   │   │   └── useSystemStatus.ts     # Health polling hook
│   │   │
│   │   ├── pages/                     # Page-level components
│   │   │   ├── LoginPage.tsx
│   │   │   ├── SetupWizard.tsx        # First-run setup
│   │   │   ├── DashboardPage.tsx      # Main overview
│   │   │   ├── ServicesPage.tsx       # Service project management
│   │   │   ├── ServiceDetailPage.tsx  # Single service: files, deploy
│   │   │   ├── UsersPage.tsx          # All end-users view
│   │   │   ├── UserDetailPage.tsx     # Single user: services, clone
│   │   │   ├── TasksPage.tsx          # Task queue + log viewer
│   │   │   ├── SettingsPage.tsx       # LLM config, domain, ports
│   │   │   └── AuditPage.tsx          # Audit log viewer
│   │   │
│   │   ├── components/                # Reusable UI components
│   │   │   ├── layout/
│   │   │   │   ├── AppLayout.tsx      # Shell: sidebar + header + content
│   │   │   │   ├── Sidebar.tsx
│   │   │   │   └── Header.tsx         # System health bar
│   │   │   │
│   │   │   ├── system/
│   │   │   │   ├── HealthBar.tsx      # provision-api / nginx status
│   │   │   │   ├── DockerStats.tsx    # CPU/RAM/Disk gauges
│   │   │   │   ├── ReconciliationPanel.tsx
│   │   │   │   └── ProxyPanel.tsx     # Global proxy config with enable/disable toggle  ← new
│   │   │   │
│   │   │   ├── services/
│   │   │   │   ├── ServiceCard.tsx
│   │   │   │   ├── ServiceList.tsx
│   │   │   │   ├── AddServiceModal.tsx    # Repo URL / upload / template
│   │   │   │   ├── FileEditor.tsx         # Monaco Editor wrapper
│   │   │   │   └── FileUploader.tsx       # Drag & drop upload
│   │   │   │
│   │   │   ├── users/
│   │   │   │   ├── UserCard.tsx
│   │   │   │   ├── DeployForm.tsx         # Deploy service to user form
│   │   │   │   ├── CloneUserModal.tsx     # Clone all from A → B
│   │   │   │   ├── ServiceInstanceCard.tsx # Per-instance: status, URL, actions
│   │   │   │   └── CurlTestPanel.tsx      # Test curl result display
│   │   │   │
│   │   │   ├── tasks/
│   │   │   │   ├── TaskList.tsx
│   │   │   │   ├── TaskDetail.tsx
│   │   │   │   └── LogViewer.tsx          # Terminal-like log display
│   │   │   │
│   │   │   ├── llm/
│   │   │   │   ├── LLMConfigPanel.tsx
│   │   │   │   ├── ChatPanel.tsx          # Troubleshooting assistant
│   │   │   │   └── ConfigGenerationPreview.tsx
│   │   │   │
│   │   │   └── common/
│   │   │       ├── StatusBadge.tsx
│   │   │       ├── ConfirmDialog.tsx
│   │   │       ├── EmptyState.tsx
│   │   │       └── ErrorBoundary.tsx
│   │   │
│   │   ├── store/                     # State management (React Context or Zustand)
│   │   │   ├── authStore.ts
│   │   │   └── systemStore.ts
│   │   │
│   │   └── styles/
│   │       └── global.css
│   │
│   └── public/
│       └── favicon.ico
│
└── docs/                               # Additional documentation
    └── api-examples.md                 # Curl examples for gateway API
```

---

## 4. Database Schema

### 4.1 SQLite Tables (Actual Implementation)

The actual implementation includes additional tables beyond the original design:

```sql
-- Gateway DB: $GATEWAY_DATA_DIR/gateway.db

--------------------------------------------------------------
-- Admin Users (gateway operators)
--------------------------------------------------------------
CREATE TABLE admins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,              -- bcrypt hash
    role            TEXT NOT NULL DEFAULT 'admin',  -- 'admin' | 'viewer'
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT
);

--------------------------------------------------------------
-- End Users (portal users for deployed services)
--------------------------------------------------------------
CREATE TABLE end_users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,              -- bcrypt hash
    role                 TEXT NOT NULL DEFAULT 'viewer',  -- 'viewer' | 'special' | 'admin'
    is_approved          INTEGER NOT NULL DEFAULT 0,  -- requires admin approval
    is_active            INTEGER NOT NULL DEFAULT 1,
    allowed_special_users TEXT,                      -- comma-separated list
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at          TEXT
);

--------------------------------------------------------------
-- LLM Configuration (multiple configs, one active)
--------------------------------------------------------------
CREATE TABLE llm_config (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mode             TEXT NOT NULL DEFAULT 'byok',  -- 'local_agent' | 'byok'
    agent_url        TEXT,                     -- e.g. http://localhost:11434/v1
    agent_model      TEXT,                     -- e.g. llama3.1:8b
    byok_api_key_enc TEXT,                     -- AES-256-GCM encrypted
    byok_base_url    TEXT,                     -- e.g. https://api.deepseek.com/v1
    byok_model       TEXT,                     -- e.g. deepseek-chat
    is_active        INTEGER NOT NULL DEFAULT 0,
    system_prompt    TEXT,                     -- Custom system prompt for config generation
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

--------------------------------------------------------------
-- Proxy Configuration (multiple proxies, one active)
--------------------------------------------------------------
CREATE TABLE proxy_configs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT,                     -- Optional display name
    protocol         TEXT NOT NULL DEFAULT 'http',  -- 'http' | 'https' | 'socks5'
    host             TEXT NOT NULL,
    port             INTEGER NOT NULL DEFAULT 8080,
    username_enc     TEXT,                     -- AES-256-GCM encrypted
    password_enc     TEXT,                     -- AES-256-GCM encrypted
    is_active        INTEGER NOT NULL DEFAULT 0,
    reachable        TEXT,                     -- 'true' | 'false' | null
    last_checked_at  TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

--------------------------------------------------------------
-- Audit Log
--------------------------------------------------------------
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        INTEGER REFERENCES admins(id),
    action          TEXT NOT NULL,             -- 'register'|'remove'|'rebuild'|'config_edit'
                                               -- |'admin_create'|'service_create'|'service_delete'
                                               -- |'clone'|'password_change'|'llm_config'|'reconcile'
    target_user     TEXT,                      -- End-user name (alice, bob)
    target_service  TEXT,                      -- Service name (myapp)
    target_label    TEXT,                      -- Label (0, 1)
    detail_json     TEXT,                      -- JSON blob with action-specific detail
    status          TEXT NOT NULL DEFAULT 'success',  -- 'success' | 'failure'
    error_message   TEXT,                      -- Error detail if status='failure'
    ip_address      TEXT,                      -- Admin's IP at time of action
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_audit_admin    ON audit_log(admin_id);
CREATE INDEX idx_audit_action   ON audit_log(action);
CREATE INDEX idx_audit_user     ON audit_log(target_user);
CREATE INDEX idx_audit_created  ON audit_log(created_at);

--------------------------------------------------------------
-- Service Templates (optional — local template library)
--------------------------------------------------------------
CREATE TABLE service_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    category        TEXT,                      -- 'cms'|'ai'|'devtools'|'media'|...
    compose_j2      TEXT NOT NULL,             -- Jinja2 compose template content
    nginx_j2        TEXT,                      -- Jinja2 nginx conf template content
    env_template    TEXT,                      -- Sample .env with ${PLACEHOLDERS}
    dockerfile      TEXT,                      -- Optional Dockerfile content
    icon            TEXT,                      -- Icon name from Ant Design icon set
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

--------------------------------------------------------------
-- Gateway Settings (key-value store for non-critical config)
--------------------------------------------------------------
CREATE TABLE gateway_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Keys: 'domain', 'nginx_http_port', 'nginx_https_port',
--        'polling_interval_sec', 'reconciliation_interval_min',
--        'proxy_enabled', 'proxy_url', 'proxy_protocol',
--        'proxy_host', 'proxy_port', 'proxy_username_enc',
--        'proxy_password_enc'
```

### 4.2 Filesystem State

```
$PROVISION_DIR/provision_nginx_state.json
```
```json
{
  "version": 1,
  "last_updated": "2026-07-04T12:00:00Z",
  "networks": {
    "myapp-user_alice-0": {
      "containers": [
        {"name": "myapp-user_alice-0-web", "status": "running"},
        {"name": "myapp-user_alice-0-db", "status": "running"}
      ],
      "nginx_connected": true
    },
    "gitlab-user_bob-0": {
      "containers": [
        {"name": "gitlab-user_bob-0-web", "status": "running"}
      ],
      "nginx_connected": true
    }
  },
  "upstreams": [
    {
      "conf_file": "myapp.user-alice.0.nginx.conf",
      "server_name": "myapp-alice-0.example.com",
      "proxy_pass": "http://myapp-user_alice-0-web:8000",
      "target_container": "myapp-user_alice-0-web",
      "target_network": "myapp-user_alice-0"
    },
    {
      "conf_file": "gitlab.user-bob.0.nginx.conf",
      "server_name": "gitlab-bob-0.example.com",
      "proxy_pass": "http://gitlab-user_bob-0-web:80",
      "target_container": "gitlab-user_bob-0-web",
      "target_network": "gitlab-user_bob-0"
    }
  ]
}
```

---

## 5. API Specification

### 5.1 Conventions

- Base path: `http://provision-gateway:8770`
- All endpoints except `/health` and `/api/auth/*` require `Authorization: Bearer <JWT>`
- Content-Type: `application/json` unless noted
- SSE endpoints use `text/event-stream`

### 5.2 Health

```
GET /health
→ 200 { "status": "ok", "db": "connected", "provision_api": "reachable", "uptime_sec": 1234 }
```

### 5.3 Auth

```
POST /api/auth/setup
  Request:  { "email": "admin@example.com", "password": "secret123" }
  Response: 201 { "message": "Initial admin created. Please login." }
  Errors:   409 (admin already exists — use /api/auth/register instead)

POST /api/auth/register
  Request:  { "email": "...", "password": "...", "role": "viewer" }
  Response: 201 { "id": 2, "email": "...", "role": "viewer", "created_at": "..." }
  Auth:     Admin only (cannot create admins unless yourself admin)

POST /api/auth/login
  Request:  { "email": "...", "password": "..." }
  Response: 200 { "access_token": "eyJ...", "refresh_token": "...", "token_type": "bearer",
                  "expires_in": 3600, "admin": { "id": 1, "email": "...", "role": "admin" } }

POST /api/auth/refresh
  Request:  { "refresh_token": "..." }
  Response: 200 { "access_token": "eyJ...", "expires_in": 3600 }

GET /api/auth/me
  Response: 200 { "id": 1, "email": "...", "role": "admin", "created_at": "...", "last_login_at": "..." }

PUT /api/auth/password
  Request:  { "current_password": "...", "new_password": "..." }
  Response: 200 { "message": "Password updated." }
```

### 5.4 System

```
GET /api/system/status
  Response: 200 {
    "provision_api":    { "status": "healthy", "latency_ms": 2, "version": "..." },
    "provision_nginx":  { "status": "healthy", "container_id": "abc123", "uptime": "3h" },
    "docker_host":      { "containers_total": 45, "containers_running": 42,
                          "cpu_percent": 23.5, "mem_percent": 67.2, "disk_percent": 54.0 },
    "gateway":          { "version": "1.0.0", "uptime_sec": 12345, "db_size_mb": 2.3 }
  }

GET /api/system/stats?detail=true
  Response: 200 {
    "containers": [
      { "name": "provision-api", "cpu_percent": 1.2, "mem_usage_mb": 89, "status": "running" },
      ...
    ],
    "disk": { "provision_dir_size_gb": 12.5, "provision_dir_free_gb": 87.5 }
  }

POST /api/system/reconcile
  Response: 202 { "task_id": "...", "message": "Reconciliation started." }
  → Gateway runs reconciliation in background, updates provision_nginx_state.json

GET /api/system/reconcile/status
  Response: 200 {
    "last_run": "2026-07-04T12:00:00Z",
    "result": {
      "total_upstreams": 12,
      "reachable": 10,
      "unreachable": 2,
      "unreachable_details": [
        { "upstream": "http://gitlab-user_bob-0-web:80", "reason": "container not found" }
      ],
      "networks_reconnected": 0,
      "nginx_reloaded": true
    }
  }

GET /api/system/nginx-state
  Response: 200 { ... }  — contents of provision_nginx_state.json

GET /api/system/proxy
  Response: 200 {
    "enabled": false,
    "protocol": "http",
    "host": "",
    "port": 8080,
    "username": "",
    "password_masked": "",
    "url": "",                     // computed: {protocol}://{host}:{port}
    "reachable": null,             // true | false | null (not yet checked)
    "last_checked_at": null        // ISO timestamp of last reachability check
  }

PUT /api/system/proxy
  Request:  {
    "enabled": true,
    "protocol": "http",           // 'http' | 'https' | 'socks5'
    "host": "proxy.internal",
    "port": 8080,
    "username": "",               // optional — leave empty to keep existing
    "password": ""                // optional — leave empty to keep existing
  }
  Response: 200 {
    "updated": true,
    "proxy": { ... },
    "reachability": {             // auto-triggered after save
      "reachable": true,          // true | false
      "latency_ms": 12,
      "error": null,              // error message if unreachable
      "checked_at": "2026-07-04T..."
    }
  }
  → Proxy credentials are AES-256-GCM encrypted at rest
  → The computed ``url`` field is returned for display
  → After saving, the gateway automatically tests connectivity to the proxy URL
    by attempting a TCP/TLS handshake (or HTTP CONNECT for http proxies)

POST /api/system/proxy/test
  Request:  (no body)
  Response: 200 {
    "reachable": true,
    "latency_ms": 8,
    "error": null,
    "checked_at": "2026-07-04T12:00:00Z"
  }
  → Standalone recheck — tests connectivity to the configured proxy URL
  → Returns 400 if no proxy is configured
  → The reachability result is cached and also returned by GET /api/system/proxy
```

### 5.5 Services (Project Management)

```
GET /api/services
  Response: 200 {
    "services": [
      {
        "name": "myapp",
        "path": "/srv/provision/source_projects/myapp",
        "files": ["docker-compose.myapp.yml.j2", "myapp.nginx.conf.j2", ".env", "Dockerfile"],
        "has_compose_template": true,
        "has_nginx_template": true,
        "active_users": 3,
        "active_instances": ["alice/0", "bob/0", "charlie/0"],
        "created_at": "2026-07-01T10:00:00Z"
      }
    ]
  }

POST /api/services
  Request: multipart/form-data or JSON — three creation modes:
  ┌──────────────────────────────────────────────────────────┐
  │ Mode 1: From Git Repo                                    │
  │   { "mode": "git", "repo_url": "https://...",            │
  │     "branch": "main", "name": "myapp",                   │
  │     "use_proxy": false }           ← new                 │
  │                                                          │
  │ Mode 2: From File Upload                                 │
  │   { "mode": "upload", "name": "myapp" }                  │
  │   + multipart files: compose, nginx, env, dockerfile      │
  │                                                          │
  │ Mode 3: From Template                                    │
  │   { "mode": "template", "template_id": 1, "name": "wp" } │
  └──────────────────────────────────────────────────────────┘
  Response: 201 { "name": "myapp", "path": "...", "files": [...], "llm_generated": ["nginx.conf"] }
  → If ``use_proxy`` is true but global proxy is disabled, returns 400
    ``{"detail": "Global proxy is not enabled. Configure it in Settings first."}``
  → Frontend: checkbox is disabled (greyed out) when global proxy is not enabled

GET /api/services/{name}
  Response: 200 { ...service detail with all file contents... }

PUT /api/services/{name}/files/{filename}
  Request:  { "content": "..." }
  Response: 200 { "filename": "...", "written": true }

DELETE /api/services/{name}?force=false
  Response: 200 { "deleted": true }
  Errors:   409 (active users exist; use ?force=true to override)

POST /api/services/{name}/convert
  Request:  { "compose_file": "docker-compose.yml", "nginx_file": "nginx.conf" }
  Response: 200 { "compose_template": "docker-compose.myapp.yml.j2",
                  "nginx_template": "myapp.nginx.conf.j2" }
  → Runs the same compose_converter + nginx_converter logic as provision-api
```

### 5.6 Users (End-User Provisioning)

```
GET /api/users
  Response: 200 { ... }  — proxied from GET /users on provision-api, enriched with URL info

GET /api/users/{user_name}
  Response: 200 { ... }  — proxied from GET /users/{name} on provision-api

POST /api/users/deploy
  Request:  {
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.myapp.yml.j2",
    "nginx_conf_file_path": "myapp.nginx.conf.j2",
    "env_file_path": ".env",
    "label": "0",
    "domain": "example.com",
    "passwd": "secret",
    "volumes": { "app_data": "/srv/provision/user-data/alice/app" },
    "build_args": { "HTTP_PROXY": "http://proxy:8080" },
    "use_global_proxy": false,          ← new
    "https": true,
    "fullchain": "/etc/letsencrypt/live/example.com/fullchain.pem",
    "privkey": "/etc/letsencrypt/live/example.com/privkey.pem"
  }
  Response: 202 { "task_id": "...", "status": "pending", "type": "register" }
  → Proxied to POST /users on provision-api
  → After completion: updates provision_nginx_state.json
  → Writes audit log entry
  → If ``use_global_proxy`` is true, the gateway injects the global proxy URL
    into ``build_args`` as ``HTTP_PROXY`` and ``HTTPS_PROXY`` before forwarding
    to provision-api. Any explicit ``build_args`` take precedence over auto-injected ones.
  → If ``use_global_proxy`` is true but global proxy is disabled, returns 400.
  → Frontend: checkbox is disabled when global proxy is not enabled.

POST /api/users/clone
  Request:  {
    "source_user": "alice",
    "target_user": "bob",
    "domain": "example.com",
    "passwd": "secret",
    "volume_base_override": "/srv/provision/user-data/bob"
  }
  Response: 202 {
    "tasks": [
      { "service": "myapp", "label": "0", "task_id": "..." },
      { "service": "gitlab", "label": "0", "task_id": "..." }
    ],
    "total": 2
  }
  → Reads source user's services from provision-api GET /users/{source_user}
  → For each service, issues POST /users with remapped user_name + volumes
  → All tasks run in parallel

DELETE /api/users/{user_name}/{service_name}/{label}
  Response: 202 { "task_id": "...", "status": "pending", "type": "remove" }
  → Proxied to DELETE /users/... on provision-api

POST /api/users/{user_name}/{service_name}/{label}/rebuild
  Request:  { "no_cache": true, "build_args": { "HTTP_PROXY": "..." } }
  Response: 202 { "task_id": "...", "status": "pending", "type": "rebuild" }

PUT /api/users/{user_name}/{service_name}/{label}/password
  Request:  { "passwd": "newsecret" }
  Response: 200 { "message": "Password updated. Nginx reloaded." }
  → Gateway re-hashes password, re-writes .htpasswd, calls nginx -s reload
  → This is done directly (not via provision-api) since provision-api doesn't have a password-change endpoint

GET /api/users/{user_name}/{service_name}/{label}/url
  Response: 200 {
    "url": "https://myapp-alice-0.example.com",
    "http_url": "http://myapp-alice-0.example.com",
    "https_enabled": true,
    "auth_enabled": true,
    "nginx_http_port": 80,
    "nginx_https_port": 443
  }

POST /api/users/{user_name}/{service_name}/{label}/test-curl
  Request:  { "include_auth": true, "follow_redirect": true }
  Response: 200 {
    "url": "https://myapp-alice-0.example.com",
    "http_code": 200,
    "headers": { "content-type": "text/html", ... },
    "body_preview": "<!DOCTYPE html>...",
    "time_total_ms": 45.2,
    "error": null
  }
  → Gateway runs `curl` from its own container
```

### 5.7 Tasks

```
GET /api/tasks
  Response: 200 { ... }  — proxied from GET /tasks on provision-api

GET /api/tasks/{task_id}
  Response: 200 { ... }  — proxied from GET /tasks/{id} on provision-api

GET /api/tasks/{task_id}/log?tail=200&follow=true
  Response: text/event-stream (SSE)
  → Streams DOCKER_OPS_LOG file or provision-api log output
  → Each SSE event: { "line": "...", "timestamp": "..." }

DELETE /api/tasks/{task_id}
  Response: 200 { ... }  — proxied from DELETE /tasks/{id} on provision-api
```

### 5.8 LLM

```
GET /api/llm/config
  Response: 200 {
    "mode": "local_agent",
    "agent_url": "http://localhost:11434/v1",
    "agent_model": "llama3.1:8b",
    "byok_configured": true,
    "byok_model": "gpt-4o",
    "byok_api_key_masked": "sk-...xxxx",
    "is_active": true,
    "system_prompt": "You are a DevOps assistant..."
  }

PUT /api/llm/config
  Request:  {
    "mode": "byok",
    "agent_url": "http://localhost:11434/v1",
    "agent_model": "llama3.1:8b",
    "byok_api_key": "sk-...",
    "byok_base_url": "https://api.openai.com/v1",
    "byok_model": "gpt-4o",
    "system_prompt": "..."
  }
  Response: 200 { "updated": true }
  → If byok_api_key is empty string, keep existing key

POST /api/llm/test
  Response: 200 {
    "success": true,
    "latency_ms": 450,
    "model": "llama3.1:8b",
    "response_preview": "Hello! I'm ready to help with DevOps tasks."
  }

POST /api/llm/generate
  Request:  {
    "type": "docker_compose" | "nginx_conf" | "env_file" | "dockerfile",
    "context": {
      "repo_description": "A Python FastAPI app with Redis caching",
      "repo_files": ["Dockerfile", "requirements.txt", "main.py"],
      "port": 8000,
      "needs_db": false,
      "needs_cache": true
    }
  }
  Response: 200 {
    "generated_content": "services:\n  web:\n    ...",
    "filename_suggestion": "docker-compose.yml",
    "warnings": ["No healthcheck defined — consider adding one."]
  }
```

### 5.9 Audit

```
GET /api/audit?admin_id=1&action=register&user=alice&from=2026-07-01&to=2026-07-04&limit=50&offset=0
  Response: 200 {
    "total": 142,
    "limit": 50,
    "offset": 0,
    "entries": [
      {
        "id": 142,
        "admin_email": "admin@example.com",
        "action": "register",
        "target_user": "alice",
        "target_service": "myapp",
        "target_label": "0",
        "detail_json": "{\"domain\":\"example.com\",\"https\":true}",
        "status": "success",
        "ip_address": "172.18.0.1",
        "created_at": "2026-07-04T11:30:00Z"
      }
    ]
  }
```

---

## 6. Frontend Design

### 6.1 Route Map

```
/login                   → LoginPage
/setup                   → SetupWizard (only when no admin exists)
/                        → DashboardPage (redirect if not authed)
/dashboard               → DashboardPage
/services                → ServicesPage (service project list)
/services/:name          → ServiceDetailPage (files, deploy button)
/users                   → UsersPage (end-user list with deployed services)
/users/:name             → UserDetailPage (user's service instances)
/users/manage            → UserManagementPage (register/approve/assign roles)
/tasks                   → TasksPage (task queue)
/settings                → SettingsPage (LLM config, global proxy, domain, ports)
/audit                   → AuditPage
```

### 6.2 Page Layout

```
┌──────────────────────────────────────────────────────────┐
│  [Logo]  Provision Gateway                    [🔔] [👤]  │  ← Header
├──────────┬───────────────────────────────────────────────┤
│          │                                               │
│ Dashboard│  ┌──────────────────────────────────────────┐ │
│ Services │  │  System Health                           │ │
│ Users    │  │  🟢 provision-api  🟢 provision-nginx   │ │
│ Tasks    │  │  CPU 23%  RAM 67%  Disk 54%             │ │
│ Settings │  └──────────────────────────────────────────┘ │
│ Audit    │                                               │
│          │  ┌─────────────┐ ┌─────────────┐ ┌─────────┐ │
│          │  │ User: alice │ │ User: bob   │ │ User:   │ │
│          │  │ 3 services  │ │ 2 services  │ │ charlie │ │
│          │  │ 🟢 all OK   │ │ 🟡 1 down   │ │ 1 svc   │ │
│          │  └─────────────┘ └─────────────┘ └─────────┘ │
│          │                                               │
│          │  ┌── Task Queue ───────────────────────────┐ │
│          │  │ ⏳ Register myapp for bob...        45s  │ │
│          │  │ ✅ Rebuild gitlab for alice         12s  │ │
│          │  │ ❌ Remove immich for charlie        2s   │ │
│          │  └────────────────────────────────────────┘ │
│          │                                               │
├──────────┴───────────────────────────────────────────────┤
│  Provision Gateway v1.0.0              © 2026            │
└──────────────────────────────────────────────────────────┘
```

### 6.3 Key UI Interactions

#### Add Service Flow (Modal Wizard)

```
Step 1: Choose source
  ┌─────────────────────────────────────┐
  │  Add New Service                    │
  │                                     │
  │  ○ From Git Repository             │
  │  ○ Upload Files                     │
  │  ○ From Template                    │
  │                                     │
  │              [Next →]               │
  └─────────────────────────────────────┘

Step 2a (Git): Paste URL + name
  ┌─────────────────────────────────────┐
  │  Git Repository                     │
  │  URL: [https://github.com/...    ]  │
  │  Branch: [main                 ▼]  │
  │  Name:  [myapp                  ]  │
  │                                     │
  │  ☑ Auto-generate missing files      │
  │    with LLM                         │
  │  ☐ Use global proxy for clone       │  ← new (only clickable if global proxy
  │    (proxy disabled — enable in       │     is enabled in Settings; otherwise
  │     Settings to use this option)      │     shown as disabled with hint)      │
  │  [← Back]  [Clone & Analyze →]      │
  └─────────────────────────────────────┘

Step 2b (Upload): Drag & drop files
  ┌─────────────────────────────────────┐
  │  Upload Files                       │
  │  Name: [myapp                  ]    │
  │                                     │
  │  ┌─────────────────────────────┐    │
  │  │  📁 Drop files here          │    │
  │  │  or click to browse          │    │
  │  │                             │    │
  │  │  docker-compose.yml  ✓      │    │
  │  │  nginx.conf           ✓      │    │
  │  │  .env                 ✓      │    │
  │  │  Dockerfile           ✓      │    │
  │  └─────────────────────────────┘    │
  │                                     │
  │  [← Back]  [Create Service →]      │
  └─────────────────────────────────────┘

Step 3 (Review LLM-generated files):
  ┌─────────────────────────────────────┐
  │  Review Generated Files             │
  │                                     │
  │  Tabs: [docker-compose.yml] [nginx.conf] [.env] │
  │  ┌─────────────────────────────┐    │
  │  │ services:                   │    │
  │  │   web:                      │    │
  │  │     build: .                │    │
  │  │     container_name: ...     │    │
  │  │     ...                     │    │
  │  └─────────────────────────────┘    │
  │                                     │
  │  ⚠ No healthcheck defined          │
  │                                     │
  │  [← Back]  [Save & Convert →]      │
  └─────────────────────────────────────┘
```

#### Deploy to User Form

```
┌──────────────────────────────────────────────┐
│  Deploy: myapp → alice                       │
│                                              │
│  User Name:    [alice____________________]   │
│  Label:        [0▼]  (auto-increment hint)   │
│  Domain:       [example.com______________]   │
│  Password:     [••••••••_________________]   │
│  ☑ Enable HTTPS                              │
│    Fullchain:  [/etc/letsencrypt/..._____]   │
│    Privkey:    [/etc/letsencrypt/..._____]   │
│                                              │
│  ── Volume Mapping ──────────────────────────│
│  app_data  → [/srv/provision/user-data/alice/app___] │
│  db_data   → [/srv/provision/user-data/alice/db____] │
│  + Add Volume                                 │
│                                              │
│  ── Build Args (optional) ───────────────────│
│  HTTP_PROXY → [___________________________]  │
│  + Add Build Arg                              │
│                                              │
│  ☐ Use global proxy for this deployment      │  ← new
│    (only available when global proxy is       │
│     enabled in Settings; otherwise disabled)   │
│    auto-injects HTTP_PROXY / HTTPS_PROXY       │
│                                              │
│                        [Cancel]  [🚀 Deploy] │
└──────────────────────────────────────────────┘
```

#### Service Instance Card

```
┌──────────────────────────────────────────────┐
│  myapp / alice / 0                    🟢 Up  │
│                                              │
│  🔗 https://myapp-alice-0.example.com        │
│     [🔗 Open] [🧪 Test curl]                 │
│                                              │
│  Containers:                                 │
│  ┌──────────┬─────────┬──────┬────────────┐ │
│  │ Name     │ Status  │ CPU  │ Memory     │ │
│  ├──────────┼─────────┼──────┼────────────┤ │
│  │ ...-web  │ Running │ 2.1% │ 89 MB     │ │
│  │ ...-db   │ Running │ 1.2% │ 156 MB    │ │
│  └──────────┴─────────┴──────┴────────────┘ │
│                                              │
│  [🔄 Rebuild] [🔑 Change Password] [🗑 Remove]│
└──────────────────────────────────────────────┘
```

#### Proxy Panel (in Settings)

```
┌──────────────────────────────────────────────┐
│  Global Proxy                                 │
│                                              │
│  ── Toggle ──────────────────────────────────│
│  [●●●] Enable Global Proxy     🟢 Reachable  │
│         (12ms, checked 2s ago)  [🔄 Recheck] │
│                                              │
│  ── Configuration ───────────────────────────│
│  Protocol:  [http ▼]                         │
│  Host:      [proxy.internal______________]   │
│  Port:      [3128________________________]   │
│  Username:  [___________________________]    │
│  Password:  [••••••••___________________]    │
│                                              │
│  Proxy URL: http://proxy.internal:3128       │
│                                              │
│                        [Save Configuration]  │
└──────────────────────────────────────────────┘

Status badge behavior:
  🟢 Reachable  — proxy responded to TCP handshake / HTTP CONNECT
  🔴 Unreachable — connection refused, timeout, or DNS failure
  ⚪ Not checked — proxy saved but not yet tested, or proxy disabled
  ⏳ Checking…   — test in progress

Recheck flow:
  1. Admin clicks [🔄 Recheck]
  2. Button shows spinner, status shows ⏳ Checking…
  3. Gateway POST /api/system/proxy/test
  4. Status updates to 🟢 or 🔴 with latency
```

#### Log Viewer (in Task Detail)

```
┌──────────────────────────────────────────────┐
│  Task: Register myapp for alice              │
│  Status: ⏳ Running | Elapsed: 2m 34s        │
│                                              │
│  ┌── Build Log ──────────────────────────┐  │
│  │ $ docker compose -f ... build          │  │
│  │ Step 1/5 : FROM python:3.13-slim      │  │
│  │  ---> abc123def456                     │  │
│  │ Step 2/5 : COPY requirements.txt .    │  │
│  │  ---> Using cache                      │  │
│  │ Step 3/5 : RUN pip install -r ...     │  │
│  │ Collecting fastapi==0.115.0            │  │
│  │   Downloading fastapi-0.115.0...       │  │
│  │   ...                                  │  │
│  │                                        │  │
│  │  ⬇ auto-scroll      [📋 Copy] [⬇ Download] │
│  └────────────────────────────────────────┘  │
│                                              │
│  [Cancel Task]                               │
└──────────────────────────────────────────────┘
```

### 6.4 State Management

- **Auth state**: JWT stored in `localStorage`. React Context provides `authState` + `login()`/`logout()` to entire app.
- **System status**: Polled every 10s via `react-query`'s `refetchInterval`. Stored in React Query cache.
- **User/service list**: Polled every 10s (same pattern).
- **Task log**: SSE connection via `EventSource` API, buffered into a line array, rendered with `react-window` for virtualized scrolling.
- **File editor**: Local React state (Monaco Editor's `onChange`). Only written to server on explicit "Save".

---

## 7. Key Module Designs

### 7.1 Reconciliation Service (`app/services/reconciliation.py`)

```
class ReconciliationService:
    """
    Responsible for:
    1. Reading all *.nginx.conf from $PROVISION_DIR/generated/
    2. Parsing proxy_pass upstreams
    3. Verifying target containers exist and are running (docker ps)
    4. Verifying provision-nginx is connected to each target network
    5. Reconnecting if needed (docker network connect)
    6. Reloading nginx (docker exec provision-nginx nginx -s reload)
    7. Writing provision_nginx_state.json
    """

    async def run_full_reconciliation(self) -> ReconciliationReport:
        ...
    
    async def on_nginx_restart(self) -> ReconciliationReport:
        # Called when Docker event detects provision-nginx restart
        ...
    
    async def record_current_state(self) -> None:
        # Called after every register/remove/rebuild
        ...
```

**Reconciliation algorithm:**
```
1. Read generated/*.nginx.conf files
2. For each conf:
   a. Parse server_name, proxy_pass directive
   b. Extract target_container name from proxy_pass URL
   c. Docker inspect target_container → exists? running?
   d. Extract network name from container's Networks
   e. Docker network inspect network → is provision-nginx connected?
3. For each unreachable:
   a. If container missing → mark as UNREACHABLE (can't fix)
   b. If network missing → mark as UNREACHABLE (can't fix)
   c. If nginx not connected to network → docker network connect + mark RECONNECTED
4. docker exec provision-nginx nginx -s reload
5. Write provision_nginx_state.json
6. Return ReconciliationReport
```

### 7.2 Docker Event Monitor (`app/services/docker_service.py`)

```
class DockerEventMonitor:
    """
    Listens to Docker events stream for provision-nginx container.
    On 'restart' or 'die' + 'start' events for provision-nginx:
      → triggers ReconciliationService.on_nginx_restart()
    """

    async def start(self):
        # docker-py events() with filters: container=provision-nginx
        ...

    async def stop(self):
        ...
```

### 7.3 LLM Service (`app/services/llm_service.py`)

```
class LLMService:
    """
    Two modes:
    - local_agent: POST {agent_url}/chat/completions
    - byok: POST {byok_base_url}/chat/completions with Authorization: Bearer {api_key}
    
    Both use OpenAI-compatible request/response format.
    """

    async def test_connection(self) -> TestResult:
        ...

    async def generate_docker_compose(self, context: RepoContext) -> GeneratedFile:
        prompt = self._build_compose_prompt(context)
        response = await self._call_llm(prompt)
        return self._extract_yaml_block(response)

    async def generate_nginx_conf(self, context: RepoContext) -> GeneratedFile:
        ...

    async def generate_env_file(self, context: RepoContext) -> GeneratedFile:
        ...

    async def generate_dockerfile(self, context: RepoContext) -> GeneratedFile:
        ...

    async def chat(self, messages: list[dict]) -> str:
        # For troubleshooting assistant
        ...
```

### 7.4 File Scanner (`app/utils/file_scanner.py`)

```
class FileScanner:
    """
    Scans a directory (cloned repo) to detect:
    - Dockerfile presence
    - docker-compose*.yml presence
    - nginx*.conf presence
    - .env presence
    - Language/framework (from requirements.txt, package.json, go.mod, etc.)
    - Exposed port (from Dockerfile EXPOSE, app code)
    
    Returns RepoContext used by LLM for config generation.
    """

    def scan(self, directory: Path) -> RepoContext:
        ...
```

### 7.5 Curl Service (`app/services/curl_service.py`)

```
class CurlService:
    """
    Runs curl from within the gateway container to test service URLs.
    Uses subprocess.run for safety (timeout, no shell injection).
    """

    async def test_url(
        self,
        url: str,
        include_auth: bool = False,
        username: str = None,
        password: str = None,
        follow_redirect: bool = True,
        timeout_sec: int = 10
    ) -> CurlResult:
        cmd = ["curl", "-s", "-o", "-", "-w", "%{http_code}|%{time_total}|%{size_download}"]
        if follow_redirect:
            cmd.append("-L")
        if include_auth and username:
            cmd.extend(["-u", f"{username}:{password}"])
        cmd.append(url)
        # subprocess.run with timeout, capture stdout/stderr
        ...
```

### 7.6 Proxy Service (`app/services/proxy_service.py`)

```
class ProxyService:
    """
    Manages global proxy configuration for the gateway.

    Stores proxy settings in gateway_settings table.
    Provides methods to:
    - Get/set proxy configuration
    - Build proxy environment variables for docker compose
    - Configure git to use the proxy for clone operations
    - Expose proxy URL to other containers on provision_default network
    """

    def get_proxy_config(self, db: Session) -> ProxyConfig:
        """Read proxy settings from gateway_settings.
        Returns a ProxyConfig with:
          - enabled: bool
          - protocol: 'http' | 'https' | 'socks5'
          - host: str
          - port: int
          - username: str (decrypted)
          - password: str (decrypted)
          - url: str  (computed full proxy URL)
        """

    def save_proxy_config(self, db: Session, config: ProxyConfigInput) -> ProxyConfig:
        """Save proxy settings. Encrypts credentials with AES-256-GCM.
        If username/password are empty strings, keeps existing values."""

    def get_proxy_env(self, db: Session) -> dict[str, str]:
        """Return proxy environment variables for docker compose builds.
        If proxy is enabled, returns:
          { 'HTTP_PROXY': '{url}', 'HTTPS_PROXY': '{url}' }
        If disabled or not configured, returns empty dict."""

    def configure_git_proxy(self, db: Session) -> None:
        """Configure git to use the global proxy for clone operations.
        Sets http.proxy and https.proxy in the global git config.
        If proxy is disabled, unsets these values."""

    def inject_build_args(self, db: Session, build_args: dict | None,
                          use_global_proxy: bool) -> dict:
        """Merge proxy env vars into build_args.
        User-specified build_args take precedence over auto-injected ones.
        Only injects if use_global_proxy=True and proxy is enabled."""


class ProxyConfig(BaseModel):
    enabled: bool = False
    protocol: str = "http"
    host: str = ""
    port: int = 8080
    username: str = ""
    password: str = ""
    url: str = ""  # computed

class ProxyConfigInput(BaseModel):
    enabled: bool
    protocol: str = "http"
    host: str = ""
    port: int = 8080
    username: str = ""
    password: str = ""
```

**How proxy is exposed to other containers:**

The global proxy address (e.g. `http://proxy.internal:8080`) is stored in the
gateway. Any container on the `provision_default` network can reach this address
if a proxy service is also deployed on that network (e.g. a Squid forward proxy
or a VPN gateway container). The gateway itself does NOT run a proxy server —
it only stores the configuration and injects the URL into build args and git
operations.

**Example flow — deploy with proxy:**

```
Admin configures proxy:  PUT /api/system/proxy
                         { "enabled":true, "host":"squid-proxy", "port":3128 }

Admin deploys service:   POST /api/users/deploy
                         { "use_global_proxy": true, ... }

Gateway injects:         build_args = { "HTTP_PROXY": "http://squid-proxy:3128",
                                        "HTTPS_PROXY": "http://squid-proxy:3128" }
                         → forwarded to provision-api → docker compose build --build-arg
```

**Example flow — git clone with proxy:**

```
Admin clones repo:       POST /api/services
                         { "mode":"git", "use_proxy": true, ... }

Gateway runs:            git config --global http.proxy http://squid-proxy:3128
                         git clone ...
                         git config --global --unset http.proxy  (cleanup)
```

```

---

## 8. provision-api Gap List

These features need to be implemented in `_users_provision/` to fully support the gateway.
**They are listed here for tracking; do NOT implement them as part of the gateway.**

| # | Feature | Priority | Endpoint / Change | Notes |
|---|---|---|---|---|
| P1 | **Orphan network cleanup on remove** | High | Inside `provisioner.remove_user()` or `docker_ops.compose_down()` | After `docker compose down`, check if the network still exists. If only `provision-nginx` is connected, run `docker network disconnect provision-nginx {network}` then `docker network rm {network}`. |
| P2 | **Build log streaming endpoint** | High | `GET /tasks/{task_id}/log?tail=N&follow=true` → SSE | Currently `DOCKER_OPS_LOG` is a file. Add an SSE endpoint that tails the file. The gateway streams this to the browser. |
| P3 | **Nginx connection state endpoint** | High | `GET /nginx/connections` → JSON | Returns: (a) list of networks provision-nginx is connected to, (b) list of `*.nginx.conf` files in GENERATED_DIR, (c) parsed upstreams. Needed by gateway reconciliation. |
| P4 | **Reconnect-all endpoint** | High | `POST /nginx/reconnect-all` → JSON | Iterates all entries in user_registry.yml, for each: `docker network connect {network_name} provision-nginx` (idempotent). Then `nginx -s reload`. Returns count of reconnected networks. |
| P5 | **Password change endpoint** | Medium | `PUT /users/{user_name}/{service_name}/{label}/password` | Re-hash new password, re-write `.htpasswd`, update registry. Currently password is set at registration only. |
| P6 | **Container logs endpoint** | Low | `GET /users/{user}/{service}/{label}/containers/{container}/logs?tail=N` | For the dashboard to show per-container logs without `docker logs` access. |

---

## 9. Implementation Plan

### Phase 1: Foundation (Week 1-2)

```
□ provision-gateway/
  □ Dockerfile (multi-stage, uv, uvicorn)
  □ pyproject.toml with dependencies
  □ app/main.py (FastAPI app skeleton, CORS, lifespan)
  □ app/config.py (env var loading)
  □ app/database.py (SQLAlchemy + SQLite)
  □ app/models/ (all 4 models)
  □ app/schemas/auth.py
  □ app/routers/auth.py (register, login, refresh, me, password)
  □ app/services/auth_service.py
  □ app/middleware/auth_middleware.py (JWT dependency)
  □ Alembic migrations

□ provision-dashboard/
  □ Vite + React + TypeScript scaffold
  □ Ant Design integration
  □ AppLayout (sidebar + header + content)
  □ LoginPage
  □ SetupWizard
  □ api/client.ts (Axios + JWT interceptor)
  □ api/auth.ts
```

### Phase 2: Core Operations (Week 3-4)

```
□ provision-gateway/
  □ app/services/provision_service.py (proxy to provision-api)
  □ app/routers/users.py (deploy, clone, remove, rebuild, password, url, test-curl)
  □ app/routers/tasks.py (list, get, log SSE, cancel)
  □ app/services/curl_service.py
  □ app/services/audit_service.py
  □ app/routers/audit.py

□ provision-dashboard/
  □ DashboardPage (system health bar, user cards, task queue)
  □ UsersPage
  □ UserDetailPage (service instances, deploy form, clone modal)
  □ ServiceInstanceCard
  □ DeployForm
  □ CloneUserModal
  □ CurlTestPanel
  □ TasksPage + LogViewer
  □ usePolling hook
  □ useSSE hook
```

### Phase 3: Service Management + LLM (Week 5-6)

```
□ provision-gateway/
  □ app/services/service_manager.py (file ops, git clone)
  □ app/services/llm_service.py (OpenAI-compatible client)
  □ app/utils/file_scanner.py (repo analysis)
  □ app/utils/crypto.py (AES-256-GCM)
  □ app/routers/services.py (CRUD)
  □ app/routers/llm.py (config, test, generate)

□ provision-dashboard/
  □ ServicesPage
  □ ServiceDetailPage
  □ AddServiceModal (Git / Upload / Template tabs)
  □ FileEditor (Monaco Editor)
  □ FileUploader
  □ LLMConfigPanel
  □ ConfigGenerationPreview
  □ SettingsPage
```

### Phase 4: Monitoring + Reconciliation (Week 7)

```
□ provision-gateway/
  □ app/services/docker_service.py (docker-py: ps, stats, events)
  □ app/services/monitoring_service.py (health checks)
  □ app/services/reconciliation.py
  □ app/utils/nginx_parser.py
  □ app/routers/system.py (status, stats, reconcile)

□ provision-dashboard/
  □ HealthBar
  □ DockerStats
  □ ReconciliationPanel
  □ AuditPage
```

### Phase 5: Integration Testing + Polish (Week 8)

```
□ docker-compose.gateway.yml (full integration with provision stack)
□ End-to-end test script
□ README.md (gateway documentation)
□ UI polish (loading states, error handling, empty states, responsive)
□ Dark mode toggle
```

---

## 10. Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `PROVISION_DIR` | ✓ | — | Same as provision-api; base directory for all provision data |
| `GATEWAY_DATA_DIR` | — | `$PROVISION_DIR/gateway_data` | Gateway SQLite DB + uploads storage |
| `GATEWAY_SECRET_KEY` | ✓ | — | 32+ char random string; used for JWT signing + API key encryption |
| `PROVISION_API_URL` | — | `http://provision-api:8000` | URL of the provision-api container (container port, not host port) |
| `NGINX_HTTP_PORT` | — | `80` | provision-nginx HTTP port (for URL display) |
| `NGINX_HTTPS_PORT` | — | `443` | provision-nginx HTTPS port (for URL display) |
| `DOCKER_OPS_LOG` | — | `$PROVISION_DIR/generated/docker_ops.log` | Build log file path for streaming |
| `DASHBOARD_PORT` | — | `8771` | Host port for dashboard (mapped to 127.0.0.1 only) |
| `GATEWAY_LOG_LEVEL` | — | `INFO` | Logging level |
| `JWT_EXPIRE_SEC` | — | `3600` | JWT access token TTL |
| `JWT_REFRESH_EXPIRE_SEC` | — | `604800` | JWT refresh token TTL (7 days) |
