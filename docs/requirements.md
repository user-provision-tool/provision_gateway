# Provision Gateway — Formalized Requirements

> **Status**: CONFIRMED — proceeding to design phase
> **Date**: 2026-07-04

---

## 0. Context: What Exists Today

The **User Provision Tool** (`_users_provision/`) is a working system:

```
┌─────────────────────────────────────────────────┐
│  provision-api (FastAPI :8765)                   │
│  ├─ POST /users            register + deploy     │
│  ├─ GET  /users            status all users      │
│  ├─ GET  /users/{name}     status one user       │
│  ├─ DELETE /users/.../...  remove service        │
│  ├─ POST  .../rebuild      rebuild containers    │
│  ├─ GET  /tasks            list async tasks      │
│  ├─ GET  /tasks/{id}       poll task status      │
│  └─ DELETE /tasks/{id}     cancel task           │
│                                                  │
│  provision-nginx (nginx:alpine :80/:443)         │
│  ├─ Virtual-host routing to user containers      │
│  ├─ Dynamic conf reload (nginx -s reload)        │
│  └─ Per-user network connect/disconnect          │
└─────────────────────────────────────────────────┘
```

**The gap**: All interaction is via `curl` or CLI. No UI, no auth, no LLM-assisted config generation, no file management, no multi-user cloning.

---

## 1. What the Provision Gateway IS

The Provision Gateway is a **management layer** that wraps the provision-api with:

- A **browser-based WebUI** (the Dashboard) for all operations
- **Admin authentication** so multiple operators can share one host
- **LLM integration** for intelligent config generation (two modes: local agent or BYOK)
- **File management** — upload, edit, git-clone service definitions
- **Real-time monitoring** — live status, build logs, health checks
- **Operational robustness** — network reconciliation, orphan cleanup, audit trail

---

## 2. Architectural Boundaries

### 2.1 What belongs in the Gateway (this project)

| Concern | Location |
|---|---|
| WebUI / Dashboard | `provision-dashboard` container |
| Backend API for WebUI | `provision-gateway` container |
| Admin auth (register/login/JWT) | `provision-gateway` |
| File upload / edit / git clone | `provision-gateway` |
| LLM integration (local agent + BYOK) | `provision-gateway` |
| Real-time status polling / log streaming | `provision-gateway` + `provision-dashboard` |
| Network/container recording & reconciliation | `provision-gateway` |
| Health monitoring of provision-api & provision-nginx | `provision-gateway` |
| Audit log | `provision-gateway` |
| Service URL display & test-curl | `provision-dashboard` |

### 2.2 What belongs in the user_provision tool (list only — IMPLEMENTED ✅)

These gaps were identified in the current provision-api and have been implemented:

| Feature | Status | Rationale |
|---|---|---|
| **Orphan network cleanup on remove** | ✅ Implemented | `docker_ops.orphan_network_cleanup()` called in `provisioner.remove_user()` |
| **Expose build log streaming endpoint** | ✅ Implemented | `GET /tasks/{id}/log?tail=N&follow=true` → SSE endpoint (per-task log files) |
| **Expose provision-nginx connection state** | ✅ Implemented | `GET /nginx/connections` → networks, conf files, upstreams |
| **Network reconnect on provision-nginx restart** | ✅ Implemented | `POST /nginx/reconnect-all` → reconnects nginx to all networks |
| **Password change endpoint** | ✅ Implemented | `PUT /users/{u}/services/{s}/{l}/password` |
| **Container logs endpoint** | ✅ Implemented | `GET /users/{u}/{s}/{l}/containers/{c}/logs` |
| **Docker/host stats endpoints** | ✅ Implemented | `GET /docker/ps`, `/docker/stats`, `/docker/info`, `/host/stats` |
| **Container existence/running check** | ✅ Implemented | `GET /docker/container/{name}/exists`, `/running` |
| **Service/container stats (registry-based)** | ✅ Implemented | `GET /container-stats`, `/service-stats` |
| **SSL certificate management** | ✅ Implemented | `GET/POST/DELETE /ssl-certs`, `POST /ssl-certs/{domain}/refresh` |
| **Reconciliation endpoint** | ✅ Implemented | `POST /reconcile`, `GET /reconcile/status`, `GET /nginx-state` |

> The gateway no longer duplicates any of the above logic — all operations are delegated to provision-api via HTTP proxy.

---

## 3. Container Architecture

### 3.1 Two-Container Split

```
                         ┌──────────────────────────┐
                         │   provision_default       │  ← single Docker network
                         │                          │
              ┌──────────┼──────────────────────────┼──────────┐
              │          │                          │          │
              ▼          ▼                          ▼          ▼
      ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
      │ provision-   │ │ provision-   │ │ provision-   │ │ provision-   │
      │ api          │ │ nginx        │ │ gateway      │ │ dashboard    │
      │ :8765        │ │ :80 / :443   │ │ :8770        │ │ :8771        │
      └──────────────┘ └──────────────┘ └──────────────┘ └──────┬───────┘
                                                                 │
                                                    127.0.0.1:8771 (localhost only)
                                                                 │
                                                            ┌────┴────┐
                                                            │ Browser │
                                                            │ (admin) │
                                                            └─────────┘
```

- **All four containers** are on `provision_default` only.
- **provision-gateway** (backend): FastAPI, internal port 8770. Talks to provision-api via `http://provision-api:8765`. Serves REST API to the dashboard container (internal Docker DNS). Own **new SQLite database** (not reusing any existing DB) for admin users, audit logs, LLM config.
- **provision-dashboard** (frontend): React SPA served by nginx:alpine. The nginx config proxies `/api/*` requests to `http://provision-gateway:8770`. Exposed on host as `127.0.0.1:8771` — **localhost only, not exposed externally**. Operators access it directly via `http://127.0.0.1:8771` (or SSH tunnel).
- **provision-nginx** continues to be the sole external-facing ingress for end-user services.
- **Neither gateway nor dashboard** connect to per-user networks — only provision-nginx does that.

### 3.2 Network Diagram

```
                          Internet
                             │
                    ┌────────┴────────┐
                    │  :80 / :443     │
                    ▼                 │
          ┌─────────────────────┐     │
          │  provision-nginx    │     │
          │  (shared ingress)   │     │
          └──────┬──────────────┘     │
                 │ provision_default  │
    ┌────────────┼────────────────────┤
    │            │                    │
    ▼            ▼                    ▼
┌──────────┐ ┌──────────┐     ┌──────────┐     ┌─────────────┐
│provision-│ │provision-│     │provision-│     │ provision-  │
│api :8765 │ │gateway   │◄───►│dashboard │     │ nginx also  │
│          │ │:8770     │ API │:8771     │     │ connected   │
└──────────┘ └────┬─────┘     └────┬─────┘     │ to per-user │
                  │               │            │ networks... │
                  │ SQLite        │ localhost  └─────────────┘
                  │ (volume)      │ :8771 only
                  ▼               ▼
            ┌──────────┐   ┌──────────┐
            │ gateway  │   │ Admin    │
            │ data/    │   │ Browser  │
            └──────────┘   └──────────┘
```

### 3.3 Why Two Containers?

| Container | Responsibility | Why separate? |
|---|---|---|
| `provision-gateway` | API + business logic + DB + file ops + LLM | Stateful. Talks to provision-api, docker socket, file system. Own SQLite DB. Own lifecycle. |
| `provision-dashboard` | nginx serving React SPA + API proxy | Stateless. Can be rebuilt/redeployed independently. Nginx proxies `/api/*` → gateway so the browser only talks to one origin. |

---

## 4. Feature Map — Detailed

### 4.1 Admin Authentication

| # | Feature | Details |
|---|---|---|
| A1 | Admin registration | First-run setup wizard: create initial admin account (email + password). Subsequent admins created by existing admins. |
| A2 | Admin login | Email + password → JWT access token (short-lived) + refresh token (long-lived). |
| A3 | Session management | Token stored in browser localStorage/sessionStorage. Auto-refresh. Logout clears token. |
| A4 | Role-based access | Two roles initially: `admin` (full CRUD) and `viewer` (read-only dashboard). Extensible later. |
| A5 | Password change | Authenticated admin can change own password. |

### 4.2 Dashboard — Global Overview

| # | Feature | Details |
|---|---|---|
| D1 | System health bar | Top bar showing: provision-api status (🟢/🔴), provision-nginx status (🟢/🔴), host Docker stats (CPU/RAM/Disk %). Auto-refreshes every 10s. |
| D2 | User summary cards | Grid of cards: each card = one end-user. Shows: user name, # of services, healthy/unhealthy counts, last deployed time. Click → drill into user detail. |
| D3 | Service summary cards | Alternative view: grid of service instances. Each card: service name, user name, label, container status badge (Up/Exited/Down), URL link, quick actions (rebuild, remove, view logs). |
| D4 | Task queue widget | Sidebar or bottom panel: list of recent/pending/running tasks with progress spinner. Click → expand to see log output. |
| D5 | Quick search | Search bar: type user name or service name → filtered results. |

### 4.3 Service Management

| # | Feature | Details |
|---|---|---|
| S1 | Add new service — from repo URL | Paste a Git repo URL. Gateway clones it into `source_projects/`. If `docker-compose.yml` / `nginx.conf` / `.env` / `Dockerfile` are missing, the LLM (local agent or BYOK) auto-generates them based on repo analysis (README, language, framework detection). Preview generated files before confirming. |
| S2 | Add new service — from file upload | Drag & drop or file picker for: `docker-compose.yml`, `nginx.conf`, `.env`, `Dockerfile`. Any missing-but-required files can be LLM-generated. |
| S3 | Add new service — from template | Pick from a built-in template library (e.g., "WordPress + MySQL", "Dify", "Immich", "GitLab"). Templates include pre-made `.j2` compose + nginx conf. |
| S4 | Edit service files | In-browser code editor (Monaco Editor) for: `docker-compose.yml`, `.j2` templates, `nginx.conf`, `.env`. Syntax highlighting, autocomplete hints for template variables (`{{ user_name }}`, `{{ container_prefix }}`, etc.). |
| S5 | Delete service definition | Remove a project from `source_projects/` (only if no users are actively using it). |
| S6 | Service file versioning | Git-backed: every edit is a commit. Rollback to previous version. (Stretch goal.) |

### 4.4 User Provisioning (Core Operations)

| # | Feature | Details |
|---|---|---|
| P1 | Deploy service to user | Form UI mapping to `POST /users`: select service (project), fill user name, label (auto-increment suggested), domain, password, volumes map, build args. Click "Deploy" → async task created → real-time progress shown. |
| P2 | One-click clone: User A → User B | Select source user (A), target user (B). Gateway reads all services of A from registry, then issues `POST /users` for each service with B's name. Volume paths auto-remapped. Domain names auto-adjusted. Password can be set per-service or same for all. |
| P3 | Rebuild service | One-click `POST .../rebuild` with optional no-cache toggle and build args override. Shows build log in real-time. |
| P4 | Remove service | One-click `DELETE /users/.../...` with confirmation dialog showing what will be torn down (containers, volumes, networks). |
| P5 | Batch operations | Select multiple service instances (checkboxes) → batch rebuild or batch remove. |
| P6 | Service password management | View/change per-user service password (re-hash, re-write `.htpasswd`, nginx reload). |
| P7 | Volume management | View per-user volume paths and disk usage. (Stretch: download/backup volume data.) |

### 4.5 Service URL & Connectivity

| # | Feature | Details |
|---|---|---|
| U1 | Service URL display | After deployment, show the clickable URL: `https://{service}-{user}-{label}.{domain}` (or `http://` if no TLS). Uses the actual `NGINX_HTTP_PORT` / `NGINX_HTTPS_PORT` from provision-nginx config. |
| U2 | Test curl | Each service card has a "Test" button. Opens a panel that runs `curl -I {url}` (or full `curl`) from within the gateway container and displays the HTTP response status, headers, body preview. Useful for verifying connectivity without leaving the WebUI. |
| U3 | Auth test | When HTTP basic auth is enabled, the test curl includes the stored credentials to verify auth is working. |

### 4.6 LLM Integration

| # | Feature | Details |
|---|---|---|
| L1 | Local agent configuration | Settings panel to configure: agent endpoint URL (e.g., `http://localhost:11434/v1` for Ollama, or a custom agent endpoint), model name, system prompt template. "Test Connection" button. |
| L2 | BYOK configuration | Settings panel to enter: API key for OpenAI / Anthropic / OpenRouter / etc., base URL override, model name. Key is stored encrypted (AES-256-GCM) in the gateway DB. "Test Connection" button. |
| L3 | LLM mode toggle | Switch between "Local Agent" and "BYOK" mode. When local agent is configured, it's preferred. When not, BYOK is used. Both can be configured simultaneously; the toggle chooses the active one. |
| L4 | Config generation | When adding a new service from a repo URL, the LLM analyzes the repo and generates missing `docker-compose.yml`, `nginx.conf`, `.env`, `Dockerfile`. User reviews & edits before saving. |
| L5 | Troubleshooting assistant | Chat panel: "My service for alice is down, what should I check?" → LLM queries provision-api status, docker ps, logs → gives diagnostic advice. |
| L6 | Template generation | "Create a docker-compose.yml for a Python FastAPI app with Redis" → LLM generates, user saves as new service template. |

### 4.7 Real-Time Operations

| # | Feature | Details |
|---|---|---|
| R1 | Status polling | Dashboard polls `GET /users` every N seconds (configurable, default 10s). Health badges update live. Green pulse animation on healthy, red on unhealthy. |
| R2 | Build log streaming | When a task is running, the dashboard opens a log panel. The gateway reads `DOCKER_OPS_LOG` (or a future SSE endpoint) and streams output to the browser via WebSocket or SSE. Terminal-like display with ANSI color support. |
| R3 | Task progress | Each task shows: type icon (register/rebuild/remove), status badge (pending→running→completed/failed), elapsed time, progress bar (indeterminate for Docker ops). Completed tasks auto-archive after 1 hour. |
| R4 | Toast notifications | Browser notification when a task completes or fails. Click toast → jump to task detail. |

### 4.8 Network & Container Reconciliation

| # | Feature | Details |
|---|---|---|
| N1 | Connection recording file | The gateway maintains a JSON file (`provision_nginx_state.json`) that records: (a) all Docker networks that provision-nginx is connected to, (b) the containers on each network, (c) all nginx upstream definitions parsed from `GENERATED_DIR/*.nginx.conf`. Updated on every register/remove/rebuild operation. |
| N2 | Reconciliation on nginx restart | The gateway monitors provision-nginx container state (via Docker events or polling). When provision-nginx restarts or is rebuilt: (1) read all `*.nginx.conf` files from `GENERATED_DIR`, (2) for each `proxy_pass` upstream, verify the target container exists and is running, (3) for each network, verify provision-nginx is connected (re-connect if not), (4) reload nginx, (5) report any unreachable upstreams. |
| N3 | Reconciliation on demand | "Reconcile" button in the WebUI → runs the same check manually and displays a report: "✓ 12 upstreams reachable, ✗ 2 unreachable (container not found: gitlab-user_bob-0-web)". |
| N4 | Scheduled reconciliation | Optional cron-like periodic reconciliation (e.g., every hour) to catch drift. |

### 4.9 System Monitoring

| # | Feature | Details |
|---|---|---|
| M1 | provision-api health | Poll `GET /health` every 10s. Show status badge + response time. Alert if down. |
| M2 | provision-nginx health | Check if provision-nginx container is running (`docker ps`). Optionally curl `http://provision-nginx/health` if a health endpoint exists. |
| M3 | Docker host stats | Via Docker socket: `docker info` (containers total, running, stopped), `docker stats --no-stream` (CPU/RAM per container), disk usage on `PROVISION_DIR`. Displayed as gauges on the dashboard. |
| M4 | Gateway self-health | `GET /health` on gateway itself. Uptime, DB status, LLM connection status. |

### 4.10 Audit & Logging

| # | Feature | Details |
|---|---|---|
| AU1 | Audit log | Every mutating action (register, remove, rebuild, config edit, admin create) is logged: timestamp, admin user, action type, target user/service, result (success/fail), detail. Stored in gateway DB. |
| AU2 | Audit log viewer | Filterable table in WebUI: by admin, by action type, by target user, by date range. Export as CSV. |
| AU3 | Gateway own logs | Structured JSON logging to stdout/stderr. Docker captures via `docker logs`. |

---

## 5. User Journeys (Key Flows)

### 5.1 First-Time Setup

```
1. Admin deploys docker-compose.provision.yml (existing flow)
   → provision-api + provision-nginx start

2. Admin deploys docker-compose.gateway.yml (new)
   → provision-gateway + provision-dashboard start
   → Gateway initializes SQLite DB, detects no admin exists → setup mode

3. Admin opens browser → http://127.0.0.1:8771
   → Setup wizard: create initial admin account (email, password)
   → Configure: domain name, provision-nginx HTTP/HTTPS ports
   → Configure LLM: local agent URL OR BYOK API key

4. Admin lands on empty dashboard → ready to add first service
```

### 5.2 Adding a Service from Git Repo

```
1. Admin clicks "Add Service" → selects "From Git Repository"
2. Pastes: https://github.com/user/some-app.git
3. Gateway clones repo into source_projects/some-app/
4. Gateway scans repo: finds Dockerfile but no docker-compose.yml or nginx.conf
5. Gateway calls LLM (local agent or BYOK):
   - "Analyze this repo and generate docker-compose.yml, nginx.conf, .env"
6. LLM returns generated files → displayed in review panel
7. Admin reviews, edits if needed, clicks "Save & Convert to Templates"
8. Gateway runs compose_converter + nginx_converter → .j2 templates created
9. Service "some-app" appears in service list, ready to deploy
```

### 5.3 Deploying to a User

```
1. Admin clicks "Deploy" on a service card
2. Form: user_name="alice", label="0", domain="example.com", password="••••"
   Volumes auto-suggested from template. Build args optional.
3. Click "Deploy" → async task created
4. Real-time panel shows:
   [====================] Building...
   $ docker compose -f ... build
   Step 1/5 : FROM python:3.13-slim
   ...
   [====================] Starting containers...
   $ docker compose -f ... up -d
   Container myapp-user_alice-0-web  Started
   [====================] Connecting nginx...
   $ docker network connect myapp-user_alice-0 provision-nginx
   $ docker exec provision-nginx nginx -s reload
   ✓ Deployed!
5. Service card now shows: URL https://myapp-alice-0.example.com
   Status: 🟢 Healthy
6. Admin clicks "Test" → curl shows 200 OK
```

### 5.4 Clone All Services from Alice to Bob

```
1. Admin navigates to user Alice → sees 3 healthy services
2. Clicks "Clone All to Another User"
3. Enters: target_user="bob", domain="example.com"
4. Gateway reads Alice's services from registry
5. Preview panel shows:
   - myapp (label 0) → will be myapp-bob-0.example.com
   - gitlab (label 0) → will be gitlab-bob-0.example.com
   - immich (label 0) → will be immich-bob-0.example.com
   Volume paths auto-remapped: /data/alice/... → /data/bob/...
6. Admin sets password (same for all or per-service)
7. Click "Clone All" → 3 async tasks created → progress tracked in parallel
```

### 5.5 Provision-nginx Restart Recovery

```
1. provision-nginx container restarts (crash, update, etc.)
2. Gateway detects provision-nginx state change (Docker event / health check fail→ok)
3. Gateway loads provision_nginx_state.json
4. For each recorded network:
   - docker network connect {network} provision-nginx (idempotent)
5. For each nginx upstream:
   - docker ps check: container exists and is running?
   - If missing → flag as unreachable
6. nginx -s reload
7. Gateway updates dashboard: "nginx restarted — 12/12 upstreams reconnected, 0 orphaned"
   (or "nginx restarted — 10/12 upstreams reconnected, 2 unreachable: [...]")
```

---

## 6. Data Model (Gateway DB — SQLite)

**New, dedicated SQLite database** stored at `$GATEWAY_DATA_DIR/gateway.db`. Not reusing any existing database.

```
admins
  id, email, password_hash, role (admin|viewer), created_at, last_login_at

llm_config
  id, mode (local_agent|byok), agent_url, model_name,
  api_key_encrypted, api_base_url, is_active, updated_at
  -- api_key_encrypted: AES-256-GCM encrypted, key derived from gateway secret

audit_log
  id, admin_id, action (register|remove|rebuild|config_edit|admin_create|...),
  target_user, target_service, target_label, detail_json, status, created_at

service_templates (optional — for template marketplace)
  id, name, description, category, compose_j2, nginx_j2, env_template, created_at

nginx_state (recording file — stored on filesystem, not DB)
  JSON file at $PROVISION_DIR/provision_nginx_state.json
  { networks: {...}, upstreams: [...], last_updated: "..." }
```

---

## 7. Gateway API Endpoints (for Dashboard Consumption)

```
GET    /health                              → gateway self-health
POST   /api/auth/register                   → create admin account
POST   /api/auth/login                      → login → JWT
POST   /api/auth/refresh                    → refresh JWT
GET    /api/auth/me                         → current admin info

GET    /api/system/status                   → provision-api + nginx + docker host health
GET    /api/system/stats                    → docker host CPU/RAM/disk stats
POST   /api/system/reconcile                → trigger network reconciliation
GET    /api/system/reconcile/status         → last reconciliation report

GET    /api/services                        → list all service projects (source_projects/)
POST   /api/services                        → create service (upload files or git clone)
GET    /api/services/{name}                 → get service details + file list
PUT    /api/services/{name}/files/{file}    → edit a service file
DELETE /api/services/{name}                 → delete service project (if no active users)
POST   /api/services/{name}/convert         → convert plain files → .j2 templates

GET    /api/users                           → all users status (proxy to provision-api)
GET    /api/users/{name}                    → single user status
POST   /api/users/deploy                    → deploy service to user (proxy + pre-processing)
POST   /api/users/clone                     → clone all services from user A → B
DELETE /api/users/{user}/{service}/{label}  → remove service
POST   /api/users/{user}/{service}/{label}/rebuild  → rebuild
PUT    /api/users/{user}/{service}/{label}/password  → change password

GET    /api/tasks                           → list tasks (proxy)
GET    /api/tasks/{id}                      → task status
GET    /api/tasks/{id}/log                  → stream build log (SSE)
DELETE /api/tasks/{id}                      → cancel task

GET    /api/url/{user}/{service}/{label}    → get service URL + test curl result

GET    /api/llm/config                      → get LLM config (masked API key)
PUT    /api/llm/config                      → update LLM config
POST   /api/llm/test                        → test LLM connection
POST   /api/llm/generate                    → generate config files from prompt

GET    /api/audit                           → query audit log (with filters)
```

---

## 8. What is OUT of Scope (for Now)

| Item | Reason |
|---|---|
| Modifying existing `_users_provision/` code | Explicit requirement — gateway is a wrapper; new provision-api features are listed in §2.2 for later implementation |
| Multi-host / swarm / k8s | provision tool is single-host by design |
| End-user self-service portal | Only admin operators use the gateway. End users (alice, bob) just access their services via URLs |
| Payment / billing integration | Not needed for v1 |
| SSO / OAuth for admins | Email+password sufficient for v1 |
| Service template marketplace backend | Can be local templates; online marketplace is v2 |
| Backup automation | Triggered manually from UI; scheduled backups are v2 |

---

## 9. Confirmed Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Scope of gateway | WebUI wrapper around existing provision-api + auth + LLM + file management. New provision-api needs listed in §2.2. |
| 2 | Reference other repos? | No. Focus only on `_users_provision` and `_provision_gateway`. |
| 3 | Database | New dedicated SQLite database. Do NOT reuse any existing DB. |
| 4 | Frontend framework | React |
| 5 | Dashboard access | Own local port (`127.0.0.1:8771`), NOT exposed externally, NOT through provision-nginx |
| 6 | Containers | 2 containers (gateway + dashboard) |
| 7 | SQLite acceptable? | Yes |
| 8 | LLM agent protocol | Generic OpenAI-compatible `/v1/chat/completions` |
| 9 | `provision_nginx_state.json` location | `$PROVISION_DIR/provision_nginx_state.json` |
| 10 | Repo clone depth | Shallow clone (`--depth 1`) for speed and disk efficiency |

---

## 10. Confirmation Checkpoints

- [x] The split of responsibilities between gateway and user_provision is correct
- [x] The two-container architecture (gateway + dashboard) is acceptable
- [x] All 10 feature categories (A1–AU3) cover your needs
- [x] The user journeys match your expectations
- [x] All open questions resolved (Section 9)
- [x] Dashboard on localhost only, not external
- [x] New SQLite DB, not reusing existing
- [x] Generic OpenAI-compatible LLM protocol
