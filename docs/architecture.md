# Provision Gateway — Architecture Document

> **Version**: 1.5
> **Date**: 2026-08-28 (updated — cycle 20260828T190332Z gateway source-project scan re-architecture: service_manager row + provision-gateway bullets updated — marker-only classification (git `ls-files` deleted; git only for N/M badges), shallow `_scan_recipe_dir` scans, `.provision-state.json` state + fingerprints (`project_state.py`, new module), cache-warm `_get_service_info`, root-only default / explicit `set_recipes` (`_discover_recipes` deleted), `POST /api/services/{name}/recipes` + `GET /api/services/{name}/tree` endpoints; services.py comment extended; prior: 2026-08-24 — cycle 20260824T173309Z v5 ACL-enforcement: ACL gate moved to the edge `-nginx-acl` (edge `location /` `auth_request` → gateway verify); internal per-service confs simplified — v4 scaffold / env.d / `/__basic__/` / portal.d removed; `ENABLE_ACL` read by gateway + edge only; prior: v4 Service-ACL enforcement: three-credential token model dropped (access_token/refresh_token/gateway_token), `/api/auth/refresh` removed, `/go/` 30s exchange code with no JWT in URL, env.d mode-switch; prior: LLM client BYOK-only / llm_config mode default 'byok' (GAP-2), Add Project modal 2 tabs (GAP-1), test-suite counts)
> **Status**: Current (reflects implemented codebase, post-deduplication refactor)

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
| `provision-gateway` | `python:3.13-slim` + custom | 8770 (internal) | `users_provision_default` | Backend API + business logic |
| `provision-dashboard` | `nginx:alpine` + React build | 8771→80 (localhost only) | `users_provision_default` | Web UI serving + API proxy |
| `provision-mcp` | `python:3.13-slim` + custom | 8780 (internal) | `users_provision_default` | MCP server for external AI agents — **⚠ non-functional vs v5 (cannot authenticate)** |
| `provision-api` | External dependency | 8765→8000 | `users_provision_default` | User provisioning operations |
| `provision-nginx` | External dependency | 99→80, 1993→443 | `users_provision_default` + per-user networks | End-user service ingress |

> **Note on naming**: The container names shown above use the `provision-*` prefix as referenced throughout the documentation. In certain deployments, the actual Docker container names may use a `subnet-acl-*` prefix (e.g., `subnet-acl-gateway`, `subnet-acl-nginx`). Both naming conventions refer to the same set of containers — the difference is deployment-specific and does not affect functionality.

### 2.2 Container Responsibilities

#### provision-gateway (Backend)
- FastAPI application serving REST API
- SQLite database for admin users, end users, audit logs, LLM config, proxy config
- JWT authentication and authorization (admin + end-user)
- All Docker operations proxied to provision-api (no direct Docker socket access)
- All compose/nginx template conversion delegated to provision-api
- File operations on shared `PROVISION_DIR` volume
- LLM client for config generation (BYOK mode only; local agent deferred to future — GAP-2, iter-1)
- Proxy configuration management
- Git operations for service source management (the Dockerfile runs `git config --global --add safe.directory '*'` so git N/M badge computation works regardless of repo ownership — commit 9f12b57; git is never used for template classification — scan-rearchitecture cycle 20260828T190332Z, F1/F2)
- Source-project scanning (scan-rearchitecture cycle 20260828T190332Z): marker-only template classification (Generated iff sibling `{file}.generated` marker exists), shallow per-recipe-dir `os.scandir` scans (never rglob), per-project `.provision-state.json` state with fingerprints (`app/services/project_state.py`), cache-warm `_get_service_info`, root-only default with explicit recipe paths (`_discover_recipes` deleted; `set_recipes` via `POST /api/services/{name}/recipes`)
- Async HTTP proxy to provision-api for all user provisioning operations
- SSL certificate management (proxied to provision-api)

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
- **⚠ Non-functional against the v5 gateway** — `verify_admin_token` requires `type=='access'` (removed in v5) and `call_gateway` sends `Authorization: Bearer` (rejected by v5 middleware); needs redesign.
- Proxies deployment requests to gateway

---

## 3. Network Topology

### 3.1 Docker Networks

```
users_provision_default (shared management network)
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
| Dashboard → Gateway | HTTP | `provision_token` cookie (v4) | Internal Docker DNS |
| MCP → Gateway | HTTP | JWT Bearer — **⚠ non-functional vs v5** (Bearer removed in v4/v5; MCP cannot authenticate) | Internal Docker DNS |
| Gateway → provision-api | HTTP | None | Internal Docker DNS |
| End User → edge `-nginx-acl` (fullset) | HTTP/HTTPS | v5 edge ACL gate (F3): `location /` `auth_request` → gateway `/api/auth/verify` (provision_token cookie / X-Provision-Token); ACL-off → pass-through to internal native Basic (`auth_basic`) | Public |
| End User → Service Container | HTTP | Service-specific | Via edge → internal nginx proxy |

### 3.3 ACL (Access Control List)

The gateway is the auth authority; the edge `-nginx-acl` (fullset) is the gate. **v5 model (cycle 20260824T173309Z, F3/F14):** ACL enforcement moved to the edge — the edge `location /` runs `auth_request /_auth_jwt` → gateway `/api/auth/verify`. Internal per-service confs are SIMPLE and ACL-free (`server_name` + `auth_basic` + variable `proxy_pass`), byte-identical across `ENABLE_ACL` — the v4 env.d mode switch, `/__basic__/` short-circuit and portal.d vhosts are removed (F8/F9). `ENABLE_ACL` is read only by the gateway + edge (F7); toggle = recreate the edge + restart the gateway.

**How it works:**

1. **Cookie-based authentication**: a single HTTP-only cookie carries the session:
   - `provision_token` — **1-week (604800s, `PROVISION_COOKIE_TTL`)** JWT set at login (token_type=cookie). Gates every `/api/*` dashboard request via the `require_gateway_token` / `require_admin` dependencies in `app/middleware/__init__.py`, and is consumed by nginx `auth_request` to authorize end-user service access. The legacy `gateway_token` cookie and the Bearer `access_token`/`refresh_token` pair were **removed in v4** (three-credential model dropped; `POST /api/auth/refresh` removed, `POST /api/auth/logout` added).

2. **`/api/auth/verify` endpoint**: Called by the EDGE `-nginx-acl` as an `auth_request` subrequest (F3); internal per-service confs no longer call it (simple ACL-free form, F8). The endpoint:
   - Extracts JWT from `provision_token` cookie or `X-Provision-Token` header.
   - Applies the v4 **hybrid `X-Client-Type` rule** (X-Provision-Token header ⇒ api / provision_token cookie ⇒ browser / Accept text/html ⇒ browser / else ⇒ api); `X-Client-Type` on every response, `X-Auth-Action` always.
   - In ACL-off mode the edge passes traffic through to the internal native Basic (`auth_basic`), so verify is not called (F6/B5).
   - In ACL mode: validates the JWT, looks up the target service by hostname, and checks whether the authenticated user is authorized (revocation check runs before admin bypass; `expires_at` and active/approved validated — F3 ordering).

3. **Authorization rules**:
   - **Admins**: Have unrestricted access to all services.
   - **Viewers**: Can only access their own services (where `target_user == viewer's username`) plus services belonging to users in their `allowed_special_users` list.
   - **Special users** (role=special): blocked at dashboard login (403) and cannot receive provision tokens (F8 B11).
   - **Denied**: Returns 403 with `X-Auth-Action: acl_denied` header.
   - **Token missing**: Returns 401 with `X-Auth-Action: login_required`.
   - **Token expired**: Returns 401 with `X-Auth-Action: token_expired`.

4. **Credential injection**: On successful verification, the endpoint returns an `X-Service-Basic` header containing the base64-encoded `username:password` for the target service's auth_basic. Nginx uses this to authenticate against the service's htpasswd (v4 N2 — no "123456" fallback).

5. **Hostname-to-service resolution**: Two in-memory services read `user_registry.yml` from the shared filesystem:
   - **HostnameIndex** (`app/services/hostname_index.py`): Maps hostnames (e.g., `myapp-alice-0.localhost`) to registry entries for O(1) lookup.
   - **Registry** (`app/services/registry.py`): Read-only registry wrapper for listing all entries.

6. **Service access redirect (`/go/{hostname}`)**: Dashboard endpoint that validates the `provision_token` session, checks ACL, and issues a **30s HMAC-signed exchange code** + `Location` header — **no JWT in any URL** (F13). The edge-side `/_set_token` is a plain variable proxy to `/api/auth/exchange`, which swaps the code for the `provision_token` cookie via `302`+`Set-Cookie` (F3/F13).

**ACL-related environment variables:**
| Variable | Default | Description |
|---|---|---|
| `ENABLE_ACL` | `false` | v5 (F7): read only by the gateway + the edge `-nginx-acl`; the `-api` no longer reads it; toggle = recreate the edge + restart the gateway |
| `REGISTRY_FILE` | `generated/user_registry.yml` | Path to the registry YAML file |
| `PROVISION_COOKIE_TTL` | `604800` | Provision token cookie TTL in seconds (compose default; QA3) |

### 3.4 API Key Authentication

End-users can create API keys as an alternative to JWT-based authentication for programmatic service access. API keys are generated via `POST /api/auth/keys` and managed through the `/api-keys` dashboard page.

- Each API key is associated with a specific end-user.
- Key creation returns a raw token (shown once) and a `provision_token` for service access.
- API keys can be listed and revoked (`GET /api/auth/keys`, `DELETE /api/auth/keys/{id}`).
- Admins can manage keys for any user; viewers can only manage their own keys.

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
│   ├── auth.py          # /api/auth/* (login, register, users, approve, verify, keys, /go)
│   ├── system.py        # /api/system/* (status, stats, reconcile, proxy, subnet-pool)
│   ├── services.py      # /api/services/* (CRUD, files, git, convert, check-missing-files, save-generated, scan, recipes, tree)
│   ├── users.py         # /api/users/* (deploy, up/down, rebuild, clone)
│   ├── tasks.py         # /api/tasks/* (list, status, log SSE, cancel)
│   ├── llm.py           # /api/llm/* (configs, test, generate)
│   └── audit.py         # /api/audit/* (query with filters)
│
├── services/            # Business Logic Services
│   ├── auth_service.py      # bcrypt hash/verify, JWT create/decode, end-user auth
│   ├── provision_service.py # Async HTTP proxy to provision-api (all ops)
│   ├── service_manager.py   # File ops, git clone, template conversion (delegated),
│   │                        #   template-based service creation (create_from_template),
│   │                        #   project change tracking (scan_for_new_projects, get_new_project_events),
│   │                        #   marker-only classification, shallow scans, set_recipes, list_tree_children
│   ├── project_state.py     # NEW (scan-rearchitecture): .provision-state.json state + dir fingerprints
│   ├── llm_service.py       # LLM client, config generation
│   ├── curl_service.py      # URL testing via subprocess curl
│   ├── audit_service.py     # Audit log writer + querier
│   └── proxy_service.py     # Proxy config CRUD + env injection
│
├── middleware/           # Middleware
│   ├── __init__.py          # JWT verification — require_gateway_token, require_admin (provision_token cookie, 1-week TTL) gate every /api/* route;
│   │                        #   v4: the legacy gateway_token cookie / Bearer access_token model was dropped (three-credential removal)
│
├── lib/                 # Shared utilities (no converters — delegated to provision-api)
│
└── utils/               # Utilities
    ├── crypto.py            # AES-256-GCM encrypt/decrypt
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
├── api_keys                # 1-year provision tokens; one default per user
│   ├── id (PK), user_id (FK→end_users), label, token_hash (UNIQUE), mask,
│   │   is_default, created_at, expires_at, is_revoked, last_used_at
│   │   (partial unique index uq_api_keys_one_default on user_id WHERE is_default=1)
│
├── audit_log               # Action audit trail
│   ├── id (PK), admin_id (FK), action, target_user, target_service,
│   │   target_label, detail_json, status, error_message, ip_address, created_at
│
├── llm_config              # LLM provider configurations
│   ├── id (PK), mode, agent_url, agent_model, byok_api_key_enc,
│   │   byok_base_url, byok_model, is_active, system_prompt, updated_at
│   │   (mode default is now 'byok'; local-agent fields are deferred —
│   │   normalized to byok and never persisted — GAP-2, iter-1)
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
└── service_templates       # Pre-built service templates (DEPRECATED — unpopulated: no writer/seed in the repo; mode=template dormant)
    ├── id (PK), name (UNIQUE), description, category, compose_j2,
    │   nginx_j2, env_template, dockerfile, icon, is_builtin, created_at, updated_at
```

### 4.2.1 DB Connection Discipline (no event-loop blocking)

The gateway is an async (FastAPI) app over a synchronous SQLAlchemy/SQLite engine. To prevent
DB-pool pressure from wedging the service, the following invariants are enforced (hardened
2026-08-28 after a production-style deadlock: concurrent authenticated load exhausted the
`QueuePool` (5 + 10 overflow per worker × 4 workers) and an async auth dependency blocking on a
checkout froze every worker's event loop):

1. **Auth dependencies are synchronous.** `require_gateway_token` / `require_admin` /
   `get_current_admin` / `get_current_user` / `get_current_admin_optional` are `def` (not
   `async def`), so FastAPI executes them in a worker thread. A pool checkout that blocks
   occupies a thread — never the event loop — so in-flight requests awaiting external calls
   can still complete and release their connections.
2. **Short-lived auth sessions.** Those dependencies open a `SessionLocal()` internally and close
   it in `finally`; they no longer depend on the request-lifetime `get_db` session. The auth
   connection returns to the pool before the endpoint's external awaits.
3. **Release before external awaits.** Endpoints that `await` a slow external call
   (provision-api Docker ops, network probes, deploys) close the DB session first and re-open a
   short-lived one only for post-await DB work (e.g. audit logging) — e.g. `get_user`,
   `deploy_user`, `list_users`, `add/update_proxy_config`. `proxy_service.test_config_reachability`
   reads host/port with a short-lived session, releases it before the connect probe, and writes
   the result back with a fresh session.
4. **`pool_timeout=2`.** The engine (`database.py`) fails an empty-pool checkout after 2s instead
   of the 30s SQLAlchemy default, so a sync DB call can never stall the loop for a long time;
   under pool exhaustion the gateway degrades to fast 500s and recovers without a restart.

### 4.3 Dependency Injection Chain

```
Request → FastAPI Router
    → get_db()                     (DB session)
    → require_gateway_token()      (JWT verification → unified user lookup; provision_token cookie, 1-week TTL)
    → require_admin()              (role check: admin vs viewer — replaces legacy require_admin_role)
    → Service Layer                (business logic)
    → Response
```

`require_gateway_token()` (the replacement for the legacy `get_current_admin()`/`get_current_user()` on `/api/*` routes) supports both admin (gateway admins) and end-user (portal users) sessions via the v4 `provision_token` cookie (the legacy `gateway_token` cookie and Bearer tokens were removed in v4). It returns a unified dict with keys: `id`, `email`, `role`, `user_type`. The JWT `user_type` claim (`admin` or `end_user`) determines which table is queried; special users (role=special) are blocked at login with 403 and blocked in middleware with 403. `require_admin()` layers the admin-only role check on top.

### 4.4 Singleton Services

| Service | Singleton | Purpose |
|---|---|---|
| `provision_service` | Yes | HTTP client for provision-api (all Docker, reconciliation, SSL, user ops) |
| `service_manager` | Yes | File operations on PROVISION_DIR; scan-rearchitecture (cycle 20260828T190332Z): marker-only classification (git `ls-files` deleted; git only for N/M badges), shallow `_scan_recipe_dir` scans, `.provision-state.json` state + fingerprints (project_state.py), cache-warm `_get_service_info`, root-only default / explicit `set_recipes` (`_discover_recipes` deleted), `list_tree_children` for the lazy `GET /api/services/{name}/tree` endpoint |
| `llm_service` | Yes | LLM client and config generation |
| `_project_monitor_loop` | Task | Background asyncio task in main.py lifespan; polls source_projects every 10s for new directories, accessible via GET /api/services/notifications |

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
                ├── Sidebar (collapsible, role-based menu — admins see all 9 items;
                │   viewers see only "Services" (/users) and "API Keys" (/api-keys))
                ├── Header (health bar, user dropdown, chat)
                └── Outlet
                    ├── /dashboard → AdminRoute → DashboardPage
                    │   └── StatCards, Gauges, SystemComponents, UserCards
                    ├── /services[/:name] → AdminRoute → ServicesPage
                    │   ├── ServiceTable (list view)
                    │   ├── Add Project modal (Git / Upload Zip tabs — "From Template"
                    │   │   tab removed and orphan AddServiceModal.tsx deleted, GAP-1 iter-1)
                    │   └── ServiceDetailPage (file tree, Monaco editor, git diff)
                    ├── /users[/:name] → UsersPage (viewer-accessible)
                    │   ├── DeployForm (modal)
                    │   ├── ServiceInstanceCards (expandable)
                    │   └── CloneUserModal
                    ├── /tasks → AdminRoute → TasksPage
                    │   ├── TaskTable (with auto-polling)
                    │   └── LogDrawer (SSE streaming)
                    ├── /settings → AdminRoute → SettingsPage
                    │   ├── LlmPanel (multi-config CRUD)
                    │   ├── ProxyPanel (multi-proxy CRUD, reachability)
                    │   └── SpecialUsersPanel
                    ├── /audit → AdminRoute → AuditPage
                    │   └── FilterableTable + CSV export
                    ├── /users/manage → AdminRoute → UserManagementPage
                    │   ├── UserTable (register, approve, delete)
                    │   └── SpecialUsersModal (per-user assignment)
                    ├── /ssl → AdminRoute → SSLPage
                    │   ├── CertTable (domain, cert, key, expiry, actions)
                    │   └── AddCertModal (domain + ssl path upload)
                    ├── /api-keys → ApiKeysPage (viewer-accessible)
                    │   └── ApiKeyTable (create/list/revoke, shows raw token once)
                    └── /alert → AlertPage (viewer-accessible)
                        └── Reason card (token_expired / acl_denied → login / back)

                AdminRoute (`src/App.tsx`) redirects non-admin viewers to `/users`,
                so admin-only pages are not reachable by direct URL (Gap 10 / G3).
                Viewers reach only Services, API Keys, and alerts.
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
- v4: auth is cookie-based — the client relies on the `provision_token` cookie set at login (no Bearer token in storage; the `access_token`/`refresh_token` localStorage model was removed in v4)
- On 401 from the dashboard, the client redirects to `/login` (there is no token-refresh endpoint in v4 — `POST /api/auth/refresh` was removed; `POST /api/auth/logout` clears the cookie)

---

## 6. MCP Server Architecture (provision-mcp)

> **⚠ Non-functional against the v5 gateway.** `verify_admin_token` (`provision-mcp/server.py`)
> requires `type=='access'` (a credential type v5 removed) and `call_gateway` sends
> `Authorization: Bearer` (which v5 middleware rejects) — the MCP server **cannot authenticate**.
> Needs redesign. The following describes the intended design.

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
Browser → GET /api/users (provision_token cookie)
    → Dashboard nginx → /api/* proxy
        → provision-gateway:8770
            → require_gateway_token() (verify provision_token cookie)
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
│   └── tests/                    # Test suite (9 test files + conftest.py; full pytest 112 passed / 9 skipped / 0 failed — iter-1, GAP-3 added test_concurrency.py)
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

> **⚠ Non-functional against the v5 gateway** — see §6.

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
| 5 | Hash what you verify; encrypt what you recover | API keys are stored as SHA-256 `token_hash` + `mask` (never recoverable); AES-256-GCM protects LLM BYOK + proxy credentials, which must be decrypted at call time |
| 6 | docker CLI over docker-py SDK | Subprocess is more reliable for complex operations (docker compose, network connect) |
| 7 | Shallow git clone (--depth 1) | Speed and disk efficiency for service source management |
| 8 | In-memory MCP sessions | Simplicity; sessions are short-lived deployment workflows |
