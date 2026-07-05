# Provision Gateway — Architecture Document

> **Version**: 1.0
> **Date**: 2026-07-05
> **Status**: Current (reflects implemented codebase)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Container Architecture](#2-container-architecture)
3. [Network Topology](#3-network-topology)
4. [Backend Architecture (provision-gateway)](#4-backend-architecture-provision-gateway)
5. [Frontend Architecture (provision-dashboard)](#5-frontend-architecture-provision-dashboard)
6. [MCP Server Architecture (provision-mcp)](#6-mcp-server-architecture-provision-mcp)
7. [Data Flow Patterns](#7-data-flow-patterns)
8. [Directory Structure](#8-directory-structure)
9. [Technology Stack](#9-technology-stack)

---

## 1. System Overview

Provision Gateway is a **management layer** that wraps the existing `provision-api` (User Provision Tool) with:

- A **browser-based WebUI** for all operations
- **Admin authentication** with role-based access control
- **LLM integration** for intelligent config generation
- **File management** — upload, edit, git-clone service definitions
- **Real-time monitoring** — live status, build logs, health checks
- **Operational robustness** — network reconciliation, orphan cleanup, audit trail
- **External AI integration** — MCP server for agent-driven deployments

### System Context Diagram

```
                         ┌──────────────────────────────────────────┐
                         │              Docker Host                  │
                         │                                          │
                         │  ┌────────────────────────────────────┐  │
                         │  │        provision_default            │  │
                         │  │         (Docker Network)            │  │
                         │  │                                    │  │
  ┌──────────┐           │  │  ┌──────────┐   ┌───────────┐     │  │
  │ Browser  │──HTTP────►│  │  │Dashboard │   │ Gateway   │     │  │
  │ :8771    │           │  │  │nginx:80  │──►│FastAPI    │     │  │
  └──────────┘           │  │  │(React    │   │:8770      │     │  │
                         │  │  │ SPA)     │   │           │     │  │
                         │  │  └──────────┘   └─────┬─────┘     │  │
                         │  │                       │            │  │
  ┌──────────┐           │  │  ┌──────────┐   ┌─────┴─────┐     │  │
  │ External  │──SSE────►│  │  │ MCP      │   │Provision  │     │  │
  │ AI Agent  │           │  │  │Server    │──►│API        │     │  │
  │           │           │  │  │FastAPI   │   │FastAPI    │     │  │
  └──────────┘           │  │  │:8780     │   │:8765      │     │  │
                         │  │  └──────────┘   └─────┬─────┘     │  │
                         │  │                       │            │  │
  ┌──────────┐           │  │  ┌──────────┐   ┌─────┴─────┐     │  │
  │ End User │──HTTPS───►│  │  │Provision │   │Docker     │     │  │
  │ Services │           │  │  │Nginx     │   │Socket     │     │  │
  └──────────┘           │  │  │:80/:443  │   │(/var/run) │     │  │
                         │  │  └────┬─────┘   └───────────┘     │  │
                         │  │       │                            │  │
                         │  │  ┌────┴─────────────────────┐      │  │
                         │  │  │  Per-User Docker Networks │      │  │
                         │  │  │  (myapp-user_alice-0, etc)│      │  │
                         │  │  └──────────────────────────┘      │  │
                         │  └────────────────────────────────────┘  │
                         └──────────────────────────────────────────┘
```

---

## 2. Container Architecture

### 2.1 Container Inventory

| Container | Image | Ports | Network | Purpose |
|---|---|---|---|---|
| `provision-gateway` | `python:3.13-slim` + custom | 8770 (internal) | `provision_default` | Backend API + business logic |
| `provision-dashboard` | `nginx:alpine` + React build | 8771→80 (localhost only) | `provision_default` | Web UI serving + API proxy |
| `provision-mcp` | `python:3.13-slim` + custom | 8780 (internal) | `provision_default` | MCP server for external AI agents |
| `provision-api` | External dependency | 8765→8000 | `provision_default` | User provisioning operations |
| `provision-nginx` | External dependency | 99→80, 1993→443 | `provision_default` + per-user networks | End-user service ingress |

### 2.2 Container Responsibilities

#### provision-gateway (Backend)
- FastAPI application serving REST API
- SQLite database for admin users, audit logs, LLM config, proxy config
- JWT authentication and authorization
- Docker socket access for container management, stats, reconciliation
- File operations on shared `PROVISION_DIR` volume
- LLM client for config generation (BYOK + local agent modes)
- Proxy configuration management
- Git operations for service source management
- Async proxy to provision-api

#### provision-dashboard (Frontend)
- Nginx serving React SPA (built with Vite)
- Proxies `/api/*` requests to `provision-gateway:8770`
- SSE passthrough support (unbuffered proxy)
- SPA fallback routing

#### provision-mcp (MCP Server)
- FastAPI application for AI agent integration
- SSE streaming deployment workflow
- Session-based state management (in-memory)
- JWT verification against gateway secret
- Proxies deployment requests to gateway

---

## 3. Network Topology

### 3.1 Docker Networks

```
provision_default (shared management network)
├── provision-gateway       (8770)
├── provision-dashboard     (8771 → 80)
├── provision-mcp           (8780)
├── provision-api           (8765 → 8000)
└── provision-nginx         (80, 443)

Per-User Networks (isolated)
├── myapp-user_alice-0
│   ├── myapp-user_alice-0-web
│   ├── myapp-user_alice-0-db
│   └── provision-nginx (connected via docker network connect)
│
├── siyuan-user_alice-0
│   ├── siyuan-user_alice-0-main
│   └── provision-nginx (connected)
│
└── ... (one per user-service-label combination)
```

### 3.2 Access Control

| Access Path | Protocol | Authentication | Restriction |
|---|---|---|---|
| Browser → Dashboard | HTTP | None (network) | `127.0.0.1:8771` only |
| Dashboard → Gateway | HTTP | JWT Bearer | Internal Docker DNS |
| MCP → Gateway | HTTP | JWT Bearer | Internal Docker DNS |
| Gateway → provision-api | HTTP | None | Internal Docker DNS |
| Gateway → Docker Socket | Unix Socket | None | Read-only mount |
| End User → provision-nginx | HTTP/HTTPS | HTTP Basic Auth (htpasswd) | Public |
| End User → Service Container | HTTP | Service-specific | Via provision-nginx proxy |

---

## 4. Backend Architecture (provision-gateway)

### 4.1 Application Layer

```
app/
├── main.py              # FastAPI app, lifespan, CORS, global exception handler
├── config.py            # Pydantic Settings (env vars)
├── database.py          # SQLAlchemy engine, session, init_db()
│
├── models/              # SQLAlchemy ORM Models
│   ├── admin.py         # AdminUser
│   ├── audit_log.py     # AuditLog
│   ├── end_user.py      # EndUser (portal users)
│   ├── llm_config.py    # LLMConfig
│   ├── proxy_config.py  # ProxyConfig
│   ├── gateway_setting.py  # GatewaySetting (KV store)
│   ├── system_config.py    # SystemConfig (KV store)
│   └── service_template.py # ServiceTemplate
│
├── schemas/             # Pydantic Request/Response Schemas
│   └── auth.py          # SetupRequest, LoginRequest, TokenResponse, etc.
│
├── routers/             # FastAPI Route Handlers
│   ├── auth.py          # /api/auth/* (login, register, users, approve)
│   ├── system.py        # /api/system/* (status, stats, reconcile, proxy)
│   ├── services.py      # /api/services/* (CRUD, files, git, convert)
│   ├── users.py         # /api/users/* (deploy, up/down, rebuild, clone)
│   ├── tasks.py         # /api/tasks/* (list, status, log SSE, cancel)
│   ├── llm.py           # /api/llm/* (configs, test, generate)
│   └── audit.py         # /api/audit/* (query with filters)
│
├── services/            # Business Logic Services
│   ├── auth_service.py      # bcrypt hash/verify, JWT create/decode
│   ├── provision_service.py # Async HTTP proxy to provision-api
│   ├── service_manager.py   # File ops, git clone, template conversion
│   ├── llm_service.py       # LLM client, config generation
│   ├── docker_service.py    # Docker CLI wrapper (ps, stats, info)
│   ├── reconciliation.py    # Nginx upstream/network reconciliation
│   ├── curl_service.py      # URL testing via subprocess curl
│   ├── audit_service.py     # Audit log writer + querier
│   └── proxy_service.py     # Proxy config CRUD + env injection
│
├── middleware/           # Middleware
│   └── auth_middleware.py   # JWT verification dependency
│
├── lib/                 # Template Converters
│   ├── compose_converter.py # docker-compose.yml → .yml.j2
│   └── nginx_converter.py   # nginx.conf → .conf.j2
│
└── utils/               # Utilities
    ├── crypto.py            # AES-256-GCM encrypt/decrypt
    ├── nginx_parser.py      # Parse nginx conf → upstreams
    └── file_scanner.py      # Scan repo → RepoContext for LLM
```

### 4.2 Database Schema (SQLite)

```
gateway.db
├── admins                  # Admin user accounts
│   ├── id (PK), email (UNIQUE), password_hash, role, is_active,
│   │   created_at, last_login_at
│
├── end_users               # Portal end-user accounts
│   ├── id (PK), username (UNIQUE), password_hash, role,
│   │   is_approved, is_active, allowed_special_users, created_at, approved_at
│
├── audit_log               # Action audit trail
│   ├── id (PK), admin_id (FK), action, target_user, target_service,
│   │   target_label, detail_json, status, error_message, ip_address, created_at
│
├── llm_config              # LLM provider configurations
│   ├── id (PK), mode, agent_url, agent_model, byok_api_key_enc,
│   │   byok_base_url, byok_model, is_active, system_prompt, updated_at
│
├── proxy_configs           # Proxy configurations
│   ├── id (PK), name, protocol, host, port, username_enc, password_enc,
│   │   is_active, reachable, last_checked_at, last_error, created_at, updated_at
│
├── gateway_settings        # Key-value settings (deprecated, migrating to system_config)
│   ├── key (PK), value, updated_at
│
├── system_config           # Key-value system configuration
│   ├── id (PK), key (UNIQUE), value
│
└── service_templates       # Pre-built service templates
    ├── id (PK), name (UNIQUE), description, category, compose_j2,
    │   nginx_j2, env_template, dockerfile, icon, is_builtin, created_at, updated_at
```

### 4.3 Dependency Injection Chain

```
Request → FastAPI Router
    → get_db()           (DB session)
    → get_current_admin() (JWT verification → lookup admin)
    → require_admin_role()(role check: admin vs viewer)
    → Service Layer      (business logic)
    → Response
```

### 4.4 Singleton Services

| Service | Singleton | Purpose |
|---|---|---|
| `provision_service` | Yes | HTTP client for provision-api |
| `service_manager` | Yes | File operations on PROVISION_DIR |
| `reconciliation_service` | Yes | Nginx state management |
| `llm_service` | Yes | LLM client and config generation |

---

## 5. Frontend Architecture (provision-dashboard)

### 5.1 Component Tree

```
main.tsx (Entry Point)
└── Providers
    ├── BrowserRouter
    ├── QueryClientProvider (React Query)
    ├── ConfigProvider (Ant Design theme)
    ├── AntApp (Ant Design static methods)
    └── AuthProvider (React Context)
        └── App.tsx (Routes)
            ├── /login → LoginPage
            ├── /setup → SetupWizard
            └── / → ProtectedRoute → AppLayout
                ├── Sidebar (collapsible, role-based menu)
                ├── Header (health bar, user dropdown, chat)
                └── Outlet
                    ├── /dashboard → DashboardPage
                    │   └── StatCards, Gauges, SystemComponents, UserCards
                    ├── /services[/:name] → ServicesPage
                    │   ├── ServiceTable (list view)
                    │   ├── AddServiceModal (Git/Upload/Template tabs)
                    │   └── ServiceDetailPage (file tree, Monaco editor, git diff)
                    ├── /users[/:name] → UsersPage
                    │   ├── DeployForm (modal)
                    │   ├── ServiceInstanceCards (expandable)
                    │   └── CloneUserModal
                    ├── /tasks → TasksPage
                    │   ├── TaskTable (with auto-polling)
                    │   └── LogDrawer (SSE streaming)
                    ├── /settings → SettingsPage
                    │   ├── LlmPanel (multi-config CRUD)
                    │   ├── ProxyPanel (multi-proxy CRUD, reachability)
                    │   └── SpecialUsersPanel
                    ├── /audit → AuditPage
                    │   └── FilterableTable + CSV export
                    └── /users/manage → UserManagementPage
                        ├── UserTable (register, approve, delete)
                        └── SpecialUsersModal (per-user assignment)
```

### 5.2 State Management

| State | Mechanism | Persistence |
|---|---|---|
| Auth (admin, tokens) | React Context (`authStore.tsx`) | localStorage |
| API calls | Axios (`client.ts`) with JWT interceptor | — |
| Server state | React Query (`@tanstack/react-query`) | Cache + polling |
| Polling | `usePolling(callback, intervalMs)` hook | In-memory |
| Log streaming | `useSSE(url)` hook (EventSource) | In-memory buffer |
| Page-local state | React `useState` | In-memory |
| Form state | Ant Design `Form` | In-memory |

### 5.3 Custom Hooks

| Hook | File | Purpose |
|---|---|---|
| `useAuth()` | `hooks/useAuth.ts` | Auth context consumer (re-export) |
| `usePolling(callback, interval, enabled)` | `hooks/usePolling.ts` | Generic interval-based polling |
| `useSSE(url)` | `hooks/useSSE.ts` | Server-Sent Events with JWT auth |

### 5.4 API Client (`src/api/client.ts`)

- Axios instance with base URL `/api`, 30s timeout
- Request interceptor: attaches `Authorization: Bearer <token>`
- Response interceptor: on 401, attempts token refresh via `POST /api/auth/refresh`
- On refresh failure: clears tokens, redirects to `/login`

---

## 6. MCP Server Architecture (provision-mcp)

### 6.1 Purpose

The MCP (Model Context Protocol) Server enables **external AI agents** (e.g., Claude, Codex) to perform deployment operations through a streaming protocol. It bridges the gap between AI coding assistants and the provision infrastructure.

### 6.2 Design

```
External AI Agent
    │
    │  POST /deploy (SSE stream)
    ▼
provision-mcp (FastAPI :8780)
    │
    ├── Verify JWT (using GATEWAY_SECRET_KEY)
    ├── Check service readiness (GET /api/services/{name})
    ├── If files missing → emit request_generation event
    │   └── Agent calls POST /submit-generation → save files
    ├── POST /api/users/deploy (via gateway)
    └── Poll task status every 2s (up to 60 iterations)
        └── Emit SSE events: pending → running → completed/failed
```

### 6.3 SSE Event Types

| Event | Direction | Description |
|---|---|---|
| `session` | MCP → Agent | Returns `session_id` for tracking |
| `status` | MCP → Agent | Progress update |
| `request_generation` | MCP → Agent | Requests AI-generated files |
| `deployed` | MCP → Agent | Deployment submitted, returns `task_id` |
| `task_update` | MCP → Agent | Polling status update |
| `done` | MCP → Agent | Workflow complete |
| `error` | MCP → Agent | Error occurred |

### 6.4 Session Storage

- In-memory Python dict (`sessions: dict[str, dict]`)
- Session contains: `created_at`, `admin_id`, `admin_email`, `service_name`, `user_name`, `label`, `events: list`
- No persistence — sessions lost on restart

---

## 7. Data Flow Patterns

### 7.1 Dashboard → Gateway → provision-api (Read)

```
Browser → GET /api/users (JWT)
    → Dashboard nginx → /api/* proxy
        → provision-gateway:8770
            → get_current_admin() (verify JWT)
            → provision_service.list_users()
                → httpx GET http://provision-api:8765/users
                    → provision-api response
                ← JSON enriched with URL info
            ← JSON response
        ← 200 JSON
    ← Render user cards
```

### 7.2 Deploy Service (Write, Async)

```
Browser → POST /api/users/deploy (JWT + form data)
    → Gateway verifies JWT, admin role
    → proxy_service.inject_build_args() (if use_global_proxy)
    → provision_service.register_user(...)
        → httpx POST http://provision-api:8765/users
            → provision-api creates async task
        ← { task_id: "abc123", status: "pending" }
    → audit_service.log_action("register", ...)
    → reconciliation_service.record_current_state()
    ← 202 { task_id, status }
→ Browser displays task link
```

### 7.3 Log Streaming (SSE)

```
Browser → EventSource("/api/tasks/{id}/log?tail=200&follow=true")
    → Gateway SSE endpoint
        → Read DOCKER_OPS_LOG file
        → Filter lines by task context (user_name, service_name)
        → Send recent `tail` lines
        → Poll file every 1s for new matching lines
        → Send each new line as SSE event
    ← text/event-stream
→ Browser renders in LogDrawer (terminal-like)
```

### 7.4 Reconciliation (On-Demand)

```
Admin clicks "Reconcile"
    → POST /api/system/reconcile
        → reconciliation_service.run_reconciliation()
            → Scan generated/*.nginx.conf
            → Parse proxy_pass upstreams
            → docker inspect each target container
            → docker network inspect each network
            → docker network connect (if nginx disconnected)
            → docker exec nginx -s reload
            → Write provision_nginx_state.json
        ← ReconciliationReport
    ← 200 { result }
```

### 7.5 Git Service Management

```
Admin clicks "Add Project" → "From Git"
    → POST /api/services (mode=git, repo_url, branch)
        → service_manager.create_from_git()
            → git clone --depth 1 <repo_url> <source_projects/name>
            → (if use_proxy) git config http.proxy <proxy_url>
            → file_scanner.scan_directory()
            → Return RepoContext (language, framework, ports, etc.)
        ← 201 { name, files, ... }
```

---

## 8. Directory Structure

### 8.1 Repository Layout

```
_provision_gateway/
├── docker-compose.gateway.yml    # Docker Compose for gateway stack
├── requirements.md               # Product requirements (CONFIRMED)
├── LICENSE
│
├── docs/                         # Documentation
│   ├── design.md                 # Product design document
│   ├── architecture.md           # This file
│   ├── api_references.md         # API reference
│   ├── tests_coverage_status.md  # Test coverage report
│   ├── webui_operation_sequences.md  # WebUI flow documentation
│   ├── workflows_of_important_usage_scenarios_of_apis.md
│   └── workflows_of_important_usage_scenarios_of_webui.md
│
├── provision-gateway/            # Backend (FastAPI)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py               # FastAPI application entry point
│   │   ├── config.py             # Environment configuration
│   │   ├── database.py           # SQLAlchemy setup
│   │   ├── models/               # ORM models (7 models)
│   │   ├── schemas/              # Pydantic schemas
│   │   ├── routers/              # API route handlers (7 routers)
│   │   ├── services/             # Business logic (9 services)
│   │   ├── middleware/           # Auth middleware
│   │   ├── lib/                  # Template converters
│   │   └── utils/                # Utilities (crypto, parser, scanner)
│   └── tests/                    # Test suite (6 files, 49 test cases)
│
├── provision-dashboard/          # Frontend (React + TypeScript)
│   ├── Dockerfile
│   ├── nginx.conf                # Nginx config with API proxy
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx              # React entry point
│       ├── App.tsx               # Routes + layout
│       ├── api/                  # API client functions (8 modules)
│       ├── hooks/                # Custom hooks (useAuth, usePolling, useSSE)
│       ├── pages/                # Page components (8 pages)
│       ├── components/           # Reusable components (layout, services, users, tasks, llm, common)
│       ├── store/                # State management (authStore)
│       └── styles/               # Global CSS
│
├── provision-mcp/                # MCP Server (FastAPI)
│   ├── Dockerfile
│   └── server.py                 # MCP server with SSE endpoints
│
├── _tasks/                       # Dynamic task files
│   └── tasks-20260705-3.md
│
└── _ignore/                      # Reference/planning artifacts
    ├── IMPLEMENTATION.md
    ├── features.md
    ├── deploy-form.md
    ├── new.md
    └── updated_design.md
```

### 8.2 Runtime Filesystem

```
PROVISION_DIR (/srv/provision)
├── source_projects/              # Service source files (git clones, uploads)
│   ├── siyuan/
│   │   ├── Dockerfile
│   │   ├── docker-compose.yml.j2
│   │   └── nginx.conf.j2
│   └── siyuan-mcp/
│       ├── Dockerfile
│       ├── docker-compose.yml.j2
│       └── nginx.conf.j2
│
├── generated/                    # Generated configurations
│   ├── docker-compose.user-alice.0.yml
│   ├── siyuan.user-alice.0.nginx.conf
│   ├── docker_ops.log            # Build/deploy log file
│   └── registry.json             # User service registry
│
├── ssl/                          # SSL certificates
│   └── snaprovision.com/
│       ├── fullchain.pem
│       └── privkey.pem
│
├── user_data/                    # Per-user persistent data
│   └── alice/
│       ├── siyuan/
│       └── ...
│
└── provision_nginx_state.json    # Reconciliation state cache

GATEWAY_DATA_DIR (/data)
└── gateway.db                    # SQLite database
```

---

## 9. Technology Stack

### 9.1 Backend (provision-gateway)

| Component | Technology | Version |
|---|---|---|
| Language | Python | 3.13 |
| Web Framework | FastAPI | ≥0.115.0 |
| ASGI Server | Uvicorn | ≥0.30.0 |
| ORM | SQLAlchemy | ≥2.0.0 |
| Database | SQLite | — |
| Migrations | Alembic | ≥1.13.0 |
| Auth (JWT) | python-jose | ≥3.3.0 |
| Auth (Password) | bcrypt | ≥4.0.0 |
| HTTP Client | httpx | ≥0.27.0 |
| Docker SDK | docker-py | ≥7.0.0 |
| Git Operations | GitPython | ≥3.1.0 |
| Encryption | cryptography | ≥42.0.0 |
| YAML | PyYAML | ≥6.0 |
| Validation | Pydantic | ≥2.0.0 |
| Settings | pydantic-settings | ≥2.0.0 |
| Async I/O | aiofiles | ≥24.0.0 |

### 9.2 Frontend (provision-dashboard)

| Component | Technology | Version |
|---|---|---|
| Language | TypeScript | ~5.6.0 |
| UI Library | React | ^18.3.1 |
| Build Tool | Vite | ^6.0.0 |
| UI Kit | Ant Design | ^5.22.0 |
| Icons | @ant-design/icons | ^5.5.0 |
| Routing | react-router-dom | ^6.28.0 |
| HTTP Client | Axios | ^1.7.0 |
| Server State | @tanstack/react-query | ^5.60.0 |
| Code Editor | @monaco-editor/react | ^4.6.0 |
| Date Library | dayjs | ^1.11.13 |
| Serve | nginx:alpine | latest |

### 9.3 MCP Server (provision-mcp)

| Component | Technology | Version |
|---|---|---|
| Language | Python | 3.13 |
| Web Framework | FastAPI | latest |
| ASGI Server | Uvicorn | latest |
| HTTP Client | httpx | latest |
| Auth (JWT) | python-jose | latest |

---

## Appendix: Key Design Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | SQLite over PostgreSQL | Zero-config, no extra container, sufficient for admin data |
| 2 | Dashboard on localhost only | Security — no external exposure of management interface |
| 3 | Separate gateway + dashboard containers | Gateway is stateful (DB, socket, files); dashboard is stateless (nginx + static files) |
| 4 | OpenAI-compatible LLM protocol | Works with Ollama, OpenAI, DeepSeek, OpenRouter, and any compatible endpoint |
| 5 | AES-256-GCM for API key storage | Industry standard for at-rest encryption |
| 6 | docker CLI over docker-py SDK | Subprocess is more reliable for complex operations (docker compose, network connect) |
| 7 | Shallow git clone (--depth 1) | Speed and disk efficiency for service source management |
| 8 | In-memory MCP sessions | Simplicity; sessions are short-lived deployment workflows |
