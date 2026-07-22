# Provision Gateway — Features Status

> **Version**: 1.5
> **Date**: 2026-07-21 (updated — Iteration 1: UI fixes, parallel requests, LLM auto-deploy flow)
> **Purpose**: Quick reference and implementation status tracker for all features.

---

## Status Legend

| Icon | Status |
|---|---|
| ✅ | Implemented & Verified |
| 🟡 | Implemented — Needs Verification |
| 🔴 | Not Implemented |
| ⚠️ | Partially Implemented / Known Issues |
| 🔮 | Future / Stretch Goal |

---

## A. Admin Authentication

| # | Feature | Status | Notes |
|---|---|---|---|
| A1 | Admin registration (first-run setup) | ✅ | `/api/auth/setup`, SetupWizard page |
| A2 | Admin login (email + password → JWT) | ✅ | Access token (1h) + Refresh token (7d) |
| A3 | Token auto-refresh | ✅ | Axios interceptor in `client.ts` |
| A4 | Role-based access (admin/viewer) | ✅ | `require_admin_role()` dependency |
| A5 | Password change | ✅ | `PUT /api/auth/password` |
| A6 | End-user registration (portal users) | ✅ | `POST /api/auth/users/register` |
| A7 | End-user approval workflow | ✅ | `PUT /api/auth/users/{id}/approve` |
| A8 | End-user role management | ✅ | `PUT /api/auth/users/{id}` — roles: viewer, special, admin |
| A9 | Special users per-user assignment | ✅ | Per-user `allowed_special_users` via toggleable tags modal in Users page |
| A10 | Deployable users list | ✅ | `GET /api/auth/users/deployable` — DB-driven, returns approved+active users |
| A11 | End-user login (JWT with user_type) | ✅ | `POST /api/auth/login` supports both admin and end-user tokens |
| A12 | Role-based sidebar filtering | ✅ | End-user viewers see only Services page |
| A13 | Auto-register deployed users | ✅ | `GET /api/users` syncs provision-api users into gateway `end_users` table on each list |
| A14 | Special users as DB records | ✅ | Special users registered via Users page (role="special"), not via Settings textarea |

---

## D. Dashboard — Global Overview

| # | Feature | Status | Notes |
|---|---|---|---|
| D1 | System health stats | ✅ | Service/User/Task/Container counts (registry-based) |
| D2 | CPU/RAM/Disk gauges | ✅ | Circular progress with >80% warning |
| D3 | System components table | ✅ | provision-api, nginx, gateway, dashboard status |
| D4 | Global proxy status card | ✅ | Enabled/disabled + reachability |
| D5 | Container stats breakdown | ✅ | Registry-based: healthy/running, unhealthy, restarting, down, missing |
| D6 | Reconcile button | ✅ | Triggers nginx upstream reconciliation (proxied to provision-api) |
| D7 | Auto-polling (10s) | ✅ | Live indicator shown |
| D8 | Task notifications | ✅ | Browser notifications + toasts for completed/failed |

---

## S. Service Management (Source Projects)

| # | Feature | Status | Notes |
|---|---|---|---|
| S1 | Add service — from Git repo | ✅ | `POST /api/services` (mode=git), proxy support |
| S2 | Add service — from file upload | ✅ | `POST /api/services` (mode=upload) |
| S3 | Add service — from ZIP upload | ✅ | `POST /api/services` (mode=upload, zip_content) |
| S4 | Add service — from template (LLM) | ✅ | `POST /api/llm/generate` + `save-generated` |
| S5 | File tree browser | ✅ | Directory structure, .git filtering, git status tags |
| S6 | Monaco code editor | ✅ | YAML/Nginx syntax highlighting, dark theme |
| S7 | Git diff view | ✅ | Monaco DiffEditor, line-by-line colored comparison |
| S8 | File save with git tracking | ✅ | `PUT /api/services/{name}/files/{file}` |
| S9 | Convert to Jinja2 templates | ✅ | `POST /api/services/{name}/convert` |
| S10 | Delete service project | ✅ | With active-users conflict detection |
| S11 | Check deploy readiness | ✅ | Auto-generate missing files via LLM |
| S12 | Repository scan for LLM context | ✅ | Language/framework/port detection |
| S13 | Service file versioning (git) | 🔮 | Stretch goal |
| S14 | Template marketplace | 🔮 | Stretch goal |

---

## P. User Provisioning (Core Operations)

| # | Feature | Status | Notes |
|---|---|---|---|
| P1 | Deploy service to user | ✅ | Full form: user, service, label, domain, password, volumes, build args, proxy |
| P2 | Clone all: User A → User B | ✅ | Auto-remaps volumes and domains |
| P3 | Rebuild service | ✅ | Async task with no-cache option |
| P4 | Remove service | ✅ | With confirmation dialog |
| P5 | Service Up (docker compose up) | ✅ | Delegated to provision-api; triggered by Play/Pause toggle |
| P6 | Service Down (docker compose stop) | ✅ | Delegated to provision-api; triggered by Play/Pause toggle |
| P7 | Service password management | ✅ | Re-hash, rewrite .htpasswd, nginx reload |
| P8 | Duplicate service to another user | ✅ | Same config, new user |
| P9 | Batch operations | ✅ | Checkbox multi-select + batch toolbar (stop/start/rebuild/remove) on Services page |
| P10 | Volume management UI | ✅ | Volume paths + disk usage (size, total/used/free) in expanded panel |
| P11 | Deployment file editor | ✅ | Clickable deployment files (env/compose/nginx) open in Monaco editor drawer |
| P12 | Redeploy blink on file change | ✅ | Redeploy button blinks when deployment files modified after registration; CSS animation `redeploy-blink` |
| P13 | Service registration time tracking | ✅ | `GET /api/.../registration-time` finds most recent successful register task |
| P14 | Deployment file CRUD API | ✅ | `GET/PUT /api/users/{u}/{s}/{l}/deployment-files/{type}` for env/compose/nginx |
| P15 | Service header resource stats | 🟡 | RAM (RSS), Disk, CPU shown on collapse panel header via docker stats + volume usage |

---

## U. Service URL & Connectivity

| # | Feature | Status | Notes |
|---|---|---|---|
| U1 | Service URL display | ✅ | HTTPS/HTTP URLs with clickable links |
| U2 | Test curl from gateway | ✅ | Shows HTTP status, headers, body preview, time |
| U3 | Auth test (include credentials) | ✅ | Optional basic auth in test curl |
| U4 | SSL cert file display | ✅ | SSL Certs page: list, upload, refresh, delete |
| U5 | SSL cert selection in deploy form | ✅ | Searchable Select dropdown, auto-fills domain + paths |

---

## L. LLM Integration

| # | Feature | Status | Notes |
|---|---|---|---|
| L1 | BYOK configuration (OpenAI-compatible) | ✅ | DeepSeek, OpenAI, OpenRouter, etc. |
| L2 | Local agent configuration (Ollama) | 🔮 | Removed from Settings UI — future feature alongside provision-agent |
| L3 | Multi-config management | ✅ | Multiple configs, one active at a time |
| L4 | Test connection | ✅ | Sends "Hello!", shows latency + response |
| L5 | Config generation (docker-compose) | ✅ | Context-aware prompt building |
| L6 | Config generation (nginx.conf) | ✅ | Template variable aware |
| L7 | Config generation (.env) | ✅ | Port, DB, cache detection |
| L8 | Config generation (Dockerfile) | ✅ | Language/framework based |
| L9 | Troubleshooting chat | ✅ | Chat modal in header, history maintained |
| L10 | Service template generation | ✅ | `generate_type: service_config` |
| L11 | API key encryption at rest | ✅ | AES-256-GCM |
| L12 | Missing files check API | ✅ | `GET /api/services/{name}/check-missing-files` → provision-api `GET /services/{name}/check-missing-files` |
| L13 | Auto-deploy LLM file generation | 🟡 | Checkbox on DeployForm; LLM generates missing compose/nginx/env/Dockerfile before deploy |
| L14 | Generated files review in deploy | 🟡 | Inline preview of LLM-generated files with review before saving+saving |

---

## R. Real-Time Operations

| # | Feature | Status | Notes |
|---|---|---|---|
| R1 | Status polling (Dashboard: 10s) | ✅ | `usePolling` hook |
| R2 | Task polling (Tasks: 5s) | ✅ | Auto-refresh table |
| R3 | Build log streaming (SSE) | ✅ | Per-task filtered, terminal-style display |
| R4 | Task progress tracking | ✅ | Status badges, elapsed time |
| R5 | Toast notifications | ✅ | Browser Notification API + antd messages |
| R6 | Audit log auto-refresh (30s) | ✅ | `usePolling` hook |
| R7 | Task persistence to disk | ✅ | `task_registry.json` in TASK_LOG_DIR; tasks survive provision-api restarts up to TTL |

---

## N. Network & Container Reconciliation

| # | Feature | Status | Notes |
|---|---|---|---|
| N1 | Nginx state recording (JSON) | ✅ | `provision_nginx_state.json` |
| N2 | Reconciliation on demand | ✅ | "Reconcile" button on Dashboard |
| N3 | Upstream verification | ✅ | Parses nginx conf, checks containers |
| N4 | Network reconnect | ✅ | `docker network connect` if nginx disconnected |
| N5 | Nginx reload after reconcile | ✅ | `docker exec nginx -s reload` |
| N6 | Scheduled reconciliation | ✅ | Background asyncio task, configurable interval via gateway_settings |
| N7 | Docker event monitoring | ✅ | docker-py event stream in thread executor, auto-triggers reconcile on nginx restart |

---

## M. System Monitoring

| # | Feature | Status | Notes |
|---|---|---|---|
| M1 | provision-api health | ✅ | `GET /health` via proxy |
| M2 | provision-nginx health | ✅ | `docker ps` status check |
| M3 | Docker host stats | ✅ | CPU/RAM/Disk from /proc + docker stats |
| M4 | Per-container stats | ✅ | `GET /api/system/stats?detail=true` |
| M5 | Gateway self-health | ✅ | `GET /health` with DB status |
| M6 | Disk usage on PROVISION_DIR | ✅ | `shutil.disk_usage` |

---

## AU. Audit & Logging

| # | Feature | Status | Notes |
|---|---|---|---|
| AU1 | Audit log (all mutating actions) | ✅ | Timestamp, admin, action, target, status |
| AU2 | Audit log viewer with filters | ✅ | Action, target user, date range |
| AU3 | CSV export | ✅ | Client-side Blob download |
| AU4 | Structured gateway logs | ✅ | stdout logging |
| AU5 | Audit auto-refresh | ✅ | 30s polling |

---

## PR. Proxy Management

| # | Feature | Status | Notes |
|---|---|---|---|
| PR1 | Multi-proxy configuration | ✅ | Add/update/delete multiple proxies |
| PR2 | Proxy protocol support (HTTP/HTTPS/SOCKS5) | ✅ | Dropdown selector |
| PR3 | Credential encryption | ✅ | AES-256-GCM for username/password |
| PR4 | Reachability auto-test | ✅ | TCP handshake after save |
| PR5 | Activate/deactivate toggle | ✅ | Only if reachable |
| PR6 | Proxy injection in deploy | ✅ | `use_global_proxy` flag in deploy form |
| PR7 | Proxy injection in git clone | ✅ | `use_proxy` flag in git service creation |
| PR8 | Manual recheck button | ✅ | `POST /api/system/proxy/test` |
| PR9 | Proxy disabled UI guard | ✅ | Checkbox disabled when no active proxy |

---

## UM. User Management (Portal)

| # | Feature | Status | Notes |
|---|---|---|---|
| UM1 | Register end-user | ✅ | Username, password, role |
| UM2 | Admin approval workflow | ✅ | Pending → Approved status |
| UM3 | Role assignment (viewer/special/admin) | ✅ | Via user update endpoint |
| UM4 | Special users per-user assignment | ✅ | Toggleable tags modal |
| UM5 | Global special users config | ✅ | Collapsible "Special Functional Users Configuration" panel on Users management page (not Settings) |
| UM6 | Delete end-user | ✅ | With confirmation |
| UM7 | Role-based sidebar filtering | ✅ | Viewer sees fewer menu items |

---

## MC. MCP Server (External AI Agent Integration)

| # | Feature | Status | Notes |
|---|---|---|---|
| MC1 | SSE streaming deploy workflow | ✅ | Event types: session, status, request_generation, deployed, task_update, done, error |
| MC2 | Session-based state management | ✅ | In-memory dict, not persisted |
| MC3 | JWT verification | ✅ | Uses GATEWAY_SECRET_KEY |
| MC4 | File generation request/response | ✅ | request_generation event + submit-generation endpoint |
| MC5 | Task polling loop | ✅ | 2s interval, 60 max iterations (2 min timeout) |
| MC6 | Session query endpoint | ✅ | `GET /session/{id}` |

---

## Summary Statistics

| Category | Total | Implemented | Verified | Gaps |
|---|---|---|---|---|
| Authentication | 14 | 14 | 14 | 0 |
| Dashboard | 8 | 8 | 8 | 0 |
| Service Management | 14 | 12 | 12 | 0 |
| User Provisioning | 15 | 15 | 14 | 1 |
| Service URL & Connectivity | 5 | 5 | 5 | 0 |
| LLM Integration | 14 | 13 | 11 | 2 |
| Real-Time Operations | 7 | 7 | 7 | 0 |
| Reconciliation | 7 | 7 | 7 | 0 |
| System Monitoring | 6 | 6 | 6 | 0 |
| Audit & Logging | 5 | 5 | 5 | 0 |
| Proxy Management | 9 | 9 | 9 | 0 |
| User Management | 7 | 7 | 7 | 0 |
| MCP Server | 6 | 6 | 6 | 0 |
| **TOTAL** | **117** | **114** | **111** | **3** |

**Implementation Rate:** 114/117 = **97.4%**
**Verified Rate:** 111/117 = **94.9%**
