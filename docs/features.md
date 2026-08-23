# Provision Gateway — Features Status

> **Version**: 2.2
> **Date**: 2026-08-22 (updated — v4 Service-ACL enforcement merged and live-verified: three-credential token model, hybrid X-Client-Type verify, byte-identical per-service nginx confs + env.d mode switch, unified nginx assembly + portal mode, `/__basic__/` Basic short-circuit, default API key; live-deployment QA fixes QA1–QA4)
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
| A2 | Admin login (email + password) | ✅ | v4 cookie-jar auth — `POST /api/auth/login` returns a `provision_token` cookie (token_type=cookie, Max-Age=604800); Bearer access_token/refresh_token removed |
| A3 | Token auto-refresh | 🔴 | Removed in v4 — `/api/auth/refresh` dropped (three-credential model); auth via provision_token cookie; `POST /api/auth/logout` clears it |
| A4 | Role-based access (admin/viewer) | ✅ | `require_admin_role()` dependency |
| A5 | Password change | ✅ | `PUT /api/auth/password` |
| A6 | End-user registration (portal users) | ✅ | `POST /api/auth/users/register` |
| A7 | End-user approval workflow | ✅ | `PUT /api/auth/users/{id}/approve` |
| A8 | End-user role management | ✅ | `PUT /api/auth/users/{id}` — roles: viewer, special, admin |
| A9 | Special users per-user assignment | ✅ | Per-user `allowed_special_users` via toggleable tags modal in Users page; v4 F8 B11 — special users blocked at dashboard (login 403 special; middleware blocks role=special) |
| A10 | Deployable users list | ✅ | `GET /api/auth/users/deployable` — DB-driven, returns approved+active users |
| A11 | End-user login (JWT with user_type) | ✅ | `POST /api/auth/login` supports both admin and end-user tokens; role=special → 403 at login, middleware blocks role=special (F8 B11) |
| A12 | Role-based sidebar filtering | ✅ | End-user viewers see only Services page |
| A13 | Auto-register deployed users | ✅ | `GET /api/users` syncs provision-api users into gateway `end_users` table on each list |
| A14 | Special users as DB records | ✅ | Special users registered via Users page (role="special"), not via Settings textarea; role=special blocked at dashboard (login 403, F8 B11) |

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
| S5 | File tree browser | ✅ | Directory structure, .git filtering, git status tags. Template classification uses the git-tracked/original criterion (GAP-4, iter-1): a file enters Templates only if git-tracked when git is available; untracked/LLM-generated deployment-critical files appear ONLY under Generated Files; `.generated` marker files excluded from all listings |
| S6 | Monaco code editor | ✅ | YAML/Nginx syntax highlighting, dark theme |
| S7 | Git diff view | ✅ | Monaco DiffEditor, line-by-line colored comparison |
| S8 | File save with git tracking | ✅ | `PUT /api/services/{name}/files/{file}` |
| S9 | Convert to Jinja2 templates | ✅ | `POST /api/services/{name}/convert` |
| S10 | Delete service project | ✅ | With active-users conflict detection |
| S11 | Check deploy readiness | ✅ | Auto-generate missing files via LLM |
| S12 | Repository scan for LLM context | ✅ | Language/framework/port detection |
| S13 | Service file versioning (git) | 🔮 | Stretch goal |
| S14 | Template marketplace | 🔮 | Stretch goal |
| S15 | Example service (REST API) | ✅ | `examples/service/` — hello-world with Dockerfile, no compose/nginx |
| S16 | Example MCP (streamable HTTP) | ✅ | `examples/mcp/` — interacts with example service API |
| S17 | Auto-detect manual source projects | ✅ | `list_services()` scans `SOURCE_PROJECTS_DIR` — all dirs auto-detected |
| S18 | Add service — from pre-built template (DB) | ✅ | Backend `POST /api/services` mode=template with template_id retained; `GET /api/services/templates` returns template list correctly (route ordering fixed in Iteration 2). **The "From Template" tab was removed from the Add Source Project modal and orphan `AddServiceModal.tsx` was deleted in iter-1 (GAP-1)** — the UI now offers Git + Upload Zip only; mode=template remains available at the API level. |
| S19 | Active file system monitoring for new projects | ✅ | Background `_project_monitor_loop` polls source_projects every 10s; events via `GET /api/services/notifications`. Route ordering fixed in Iteration 2: `/notifications` route now registered before `/{name}` catch-all. Both endpoints working correctly. |

---

## P. User Provisioning (Core Operations)

| # | Feature | Status | Notes |
|---|---|---|---|
| P1 | Deploy service to user | ✅ | Full form: user, service, domain, password, volumes, build args, proxy; label auto-computed; deploy blocked when missing essential files without LLM |
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
| P15 | Service header resource stats | ✅ | RAM (RSS), CPU shown on collapse panel header via docker stats; verified — /api/system/stats proxies to provision-api /docker/stats |

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
| L2 | Local agent configuration (Ollama) | 🔮 | Removed from Settings UI — future feature alongside provision-agent. Backend defers local agent at the API level (GAP-2, iter-1): `mode='local_agent'` is normalized to `byok`, `agent_url`/`agent_model` are never persisted, `_resolve_endpoint` no longer routes to `agent_url`, and the `LLMConfig.mode` column default is now `byok`. |
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
| L13 | Auto-deploy LLM file generation | ✅ | Checkbox on DeployForm; LLM generates missing compose/nginx/env/Dockerfile before deploy — auto-submits (G6). Non-autoDeploy mode saves generated files to disk before deploy (G12 fixed in Iteration 2). |
| L14 | Generated files review in deploy | ✅ | Inline preview of LLM-generated files with review before saving; clickable file tags open Monaco editor (G6 confirmed working) |
| L15 | Missing provision_service import fix | ✅ | Iteration 4: Added missing `from ..services.provision_service import provision_service` to services.py — fixes NameError at check-missing-files endpoint |
| L16 | DeployForm error handling | ✅ | Iteration 4: Replaced silent catch with checkError state, error Alert, and disabled deploy button on check failure |

---

## R. Real-Time Operations

| # | Feature | Status | Notes |
|---|---|---|---|
| R1 | Status polling (Dashboard: 10s) | ✅ | `usePolling` hook |
| R2 | Task polling (Tasks: 5s) | ✅ | Auto-refresh table |
| R3 | Build log streaming (SSE) | ✅ | Per-task filtered, terminal-style display |
| R4 | Task progress tracking | ✅ | Status badges, elapsed time |
| R5 | Toast notifications | ✅ | Browser Notification API + antd messages; time-filtered (2s window, no localStorage) |
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
| M7 | Subnet pool usage (Dashboard card) | ✅ | `GET /api/system/subnet-pool` (require_admin) — proxied to provision-api `/subnet-pool`; pool free/total/exhausted + allocations displayed on Dashboard |

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

## ACL. ACL & Access Control

| # | Feature | Status | Notes |
|---|---|---|---|
| ACL1 | ENABLE_ACL environment variable | ✅ | v4: env.d one-liner mode switch — `write_env_d` writes `set $auth_mode acl;|basic;`; per-service conf is byte-identical across modes (never regenerated on mode switch); defaults `false` (N8) |
| ACL2 | /api/auth/verify endpoint | ✅ | NGINX auth_request subrequest; v4 hybrid X-Client-Type rule (X-Provision-Token header ⇒ api / provision_token cookie ⇒ browser / Accept text/html ⇒ browser / else ⇒ api); X-Client-Type on every response; X-Auth-Action always incl. unauthorized; nginx Accept `is_browser` map removed; F3 ordering — revocation check runs before admin bypass (R1), provision_token validated for `expires_at` and active/approved |
| ACL3 | gateway_token cookie (admin session) | 🔴 | Removed in v4 — gateway mints only `provision_token` (+ 30s exchange code); middleware no longer validates `gateway_token` |
| ACL4 | provision_token cookie (service access) | ✅ | HTTP-only cookie; v4 1-week TTL (604800s via `create_provision_token`) bound to the default key's `api_key_id`; cookie Max-Age=604800 (compose default `PROVISION_COOKIE_TTL` fixed in QA3) |
| ACL11 | Login token model | ✅ | v4 three credentials: `provision_token` + 30s exchange code; Bearer `access_token`/`refresh_token` dropped; `POST /api/auth/logout` added (clears cookie) |
| ACL5 | HostnameIndex | ✅ | In-memory hostname-to-registry-entry lookup; maps service URLs to registry entries |
| ACL6 | Registry (user_registry.yml) | ✅ | Read-only registry wrapper; reads provision-api registry via shared filesystem |
| ACL7 | /go/{hostname} service redirect | ✅ | v4: issues 30s HMAC-signed exchange code + `Location` header; `/api/auth/exchange` swaps code→provision_token via 302+Set-Cookie (`; Secure` per registry flag); no JWT in URL (F7) |
| ACL8 | Viewer ACL: own services only | ✅ | Viewers access own services + allowed_special_users; v4 N1 trims each allowed_special_users element |
| ACL9 | Admin unrestricted access | ✅ | Admins have unrestricted access to all services |
| ACL10 | X-Service-Basic credential injection | ✅ | Base64 user:password header for nginx auth_basic on target service; v4 N2 — no "123456" fallback (passwd-less returns empty credential) |
| ACL12 | Byte-identical per-service conf + env.d mode switch | ✅ | `render_nginx_conf` always injects the v4 scaffolding (no ENABLE_ACL branch); per-service conf byte-identical across ENABLE_ACL/modes (test `test_render_nginx_conf_byte_identical_across_enable_acl`) |
| ACL13 | `/__basic__/` Basic short-circuit | ✅ | internal `location /__basic__/` holds the only `auth_basic`/`auth_basic_user_file`; empty env.d ⇒ `$auth_mode ""` ⇒ `/__basic__/` with 0 gateway subrequests (B5/B12) |
| ACL14 | Minimal deployment (empty dirs ⇒ Basic) | ✅ | unified `nginx.provision.conf` + `default_server 444` catch-all; docker `nginx -t` clean with empty env.d/portal.d/services.d and minimal+services (F5) |
| ACL15 | Portal mode + unified assembly | ✅ | portal.d + services.d + env.d includes; `PORTAL_MODE` http/https (443 ssl + 80 301); portal verify/exchange return 404; compose env plumbing (F6) |
| ACL16 | API-first 401/403 | ✅ | `@auth_401/@auth_403` return 401/403 for non-browser client_type; `WWW-Authenticate` always on 401; no `?redirect=` param (GAP-14) |

---

## AK. API Key Management

| # | Feature | Status | Notes |
|---|---|---|---|
| AK1 | Create API key | ✅ | `POST /api/auth/keys`; admin for any user, viewer for self |
| AK2 | List API keys | ✅ | `GET /api/auth/keys`; admin sees all, viewer sees own |
| AK3 | Revoke/delete API key | ✅ | `DELETE /api/auth/keys/{id}`; admin revokes any, viewer own |
| AK4 | API key with provision token | ✅ | Key creation returns raw token; v4 provision_token for service access is 1-week (604800s) bound to the default key |
| AK5 | ApiKeysPage frontend | ✅ | Admin page at /api-keys for managing end-user API keys |
| AK6 | Default key (is_default) | ✅ | `ApiKey.is_default` + partial unique index; `PUT /keys/{id}/default`; `DELETE /keys` rejects revoking the default (400); default created at registration and for admins; 1000-key cap / lazy eviction; live DB migrated via `_ensure_schema` (QA1) |
| AK7 | API key = JWT + mask + real user_type | ✅ | Keys minted as 1-year JWT provision tokens with own `api_key_id` + `mask`; key-targeted tokens carry the target's real `user_type` (GAP-07) |

---

## AL. Alerts Page

| # | Feature | Status | Notes |
|---|---|---|---|
| AL1 | AlertPage frontend | ✅ | Page at /alerts for system notifications and alerts |
| AL2 | Project detection alerts | ✅ | Notifications from background project monitor loop (new projects in source_projects/) |

---

## RC. Multi-Recipe Services (recipe_path)

| # | Feature | Status | Notes |
|---|---|---|---|
| RC1 | Recipe auto-discovery | ✅ | `service_manager._discover_recipes` finds subdirs with both a `Dockerfile` and a plain `docker-compose*.yml`; git-tracked list when git available, filesystem fallback when not |
| RC2 | DeployForm `name@@recipe_path` selection | ✅ | Service dropdown lists each recipe as `name @ recipe_path`; value = `name@@recipe_path`; `project_root` = `{base}/{recipe_path}`, template paths are bare filenames inside the recipe |
| RC3 | `recipe_path` in check-missing-files | ✅ | `GET /api/services/{name}/check-missing-files?recipe_path=...` (gateway forwards to provision-api); 404 message includes the recipe |
| RC4 | `recipe_path` in save-generated | ✅ | `POST /api/services/save-generated` accepts `recipe_path` and creates the target recipe subdir |
| RC5 | ServicesPage per-recipe readiness | ✅ | Readiness (robot) buttons track per-recipe missing-files + checking state |
| RC6 | git safe.directory in gateway image | ✅ | `provision-gateway/Dockerfile` runs `git config --global --add safe.directory '*'` so recipe discovery works on untrusted repos |

---

## Summary Statistics

| Category | Total | Implemented | Verified | Gaps |
|---|---|---|---|---|
| Authentication | 14 | 13 | 13 | 1 |
| Dashboard | 8 | 8 | 8 | 0 |
| Service Management | 19 | 17 | 17 | 2 |
| User Provisioning | 15 | 15 | 15 | 0 |
| Service URL & Connectivity | 5 | 5 | 5 | 0 |
| LLM Integration | 16 | 15 | 15 | 1 |
| Real-Time Operations | 7 | 7 | 7 | 0 |
| Reconciliation | 7 | 7 | 7 | 0 |
| System Monitoring | 7 | 7 | 7 | 0 |
| Audit & Logging | 5 | 5 | 5 | 0 |
| Proxy Management | 9 | 9 | 9 | 0 |
| User Management | 7 | 7 | 7 | 0 |
| MCP Server | 6 | 6 | 6 | 0 |
| ACL & Access Control | 16 | 15 | 15 | 1 |
| API Key Management | 7 | 7 | 7 | 0 |
| Alerts Page | 2 | 2 | 2 | 0 |
| Multi-Recipe Services | 6 | 6 | 6 | 0 |
| **TOTAL** | **156** | **151** | **151** | **5** |

**Implementation Rate:** 151/156 = **96.8%**
**Verified Rate:** 151/156 = **96.8%**
