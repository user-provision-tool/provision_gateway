# Provision Gateway — Changes (2026-07-08)

> **Date**: 2026-07-08
> **Range**: `d0e36b77369909ee92d056ba9c0e68c35549c945` → `0ce6a86` (HEAD)
> **Commits**: 13 commits across 42 files

---

## Summary of Changes

### Categories

1. **Architecture: Remove Gateway Duplicates** — Delegate all Docker, compose, nginx, and reconciliation operations to provision-api
2. **Feature: End-User Authentication** — Login supports both admin (gateway) and end-user (provision-api) accounts
3. **Feature: SSL Certificate Management** — Upload, list, refresh, delete SSL certs; select in deploy form
4. **Feature: Registry-Based Stats** — Dashboard shows registry-derived container/service stats, not raw docker ps
5. **Feature: Play/Pause Toggle** — Single toggle button replaces separate Up/Down buttons
6. **Feature: Dashboard Layout Improvements** — Consistent stat cards, end-user viewer role support, role-based sidebar
7. **Bug Fixes** — Task notification fix, file auto-location, template vs generated files separation
8. **Tests** — Comprehensive gateway and provision-api test suites added
9. **Infrastructure** — Python-multipart added, config cleanup

---

## 1. Architecture: Remove Gateway Duplicates

### Files Removed
| File | Reason |
|---|---|
| `provision-gateway/app/lib/compose_converter.py` (155 lines) | Duplicate of provision-api's compose converter |
| `provision-gateway/app/lib/nginx_converter.py` (129 lines) | Duplicate of provision-api's nginx converter |
| `provision-gateway/app/lib/nginx_parser.py` (55 lines) | Duplicate — parsing handled by provision-api |
| `provision-gateway/app/services/docker_service.py` (191 lines) | All Docker ops now proxied to provision-api |
| `provision-gateway/app/services/reconciliation.py` (145 lines) | Reconciliation now proxied to provision-api |

### Files Modified
| File | Change |
|---|---|
| `provision-gateway/app/lib/__init__.py` | Docstring updated: "does NOT duplicate converter logic" |
| `provision-gateway/app/config.py` | Removed `NGINX_STATE_FILE`; added comment about log proxying |
| `provision-gateway/app/services/provision_service.py` | Added 20+ new proxy methods: start_user, stop_user, change_user_password, docker_ps, docker_stats, docker_info, host_stats, container_exists, container_running, network_connect, nginx_reload, reconcile, reconciliation_status, nginx_state, stream_task_log, get_container_logs, get_container_stats, get_service_stats, list_ssl_certs, upload_ssl_cert, refresh_ssl_cert, delete_ssl_cert |
| `provision-gateway/app/services/service_manager.py` | `convert_compose()` and `convert_nginx()` simplified — just copy raw files with header; actual conversion delegated to provision-api at deploy time |
| `provision-gateway/app/routers/users.py` | Up/down/password endpoints now delegate to provision_service instead of direct Docker CLI |
| `provision-gateway/app/routers/system.py` | Status/stats/reconcile all proxied to provision-api; added SSL cert CRUD endpoints |
| `provision-gateway/app/routers/tasks.py` | SSE log streaming now proxies to provision-api's per-task SSE endpoint; supports `?token=` query param for EventSource auth |
| `provision-gateway/Dockerfile` | Added `python-multipart>=0.0.9` (for SSL cert form uploads) |

### Key Architectural Principle
> The gateway MUST NOT duplicate provision-api logic. All compose/nginx template conversion, Docker operations, nginx reconciliation, and container monitoring are delegated to provision-api via HTTP. The gateway is purely a **management proxy and WebUI backend**.

---

## 2. Feature: End-User Authentication

### Changes
| File | Change |
|---|---|
| `provision-gateway/app/services/auth_service.py` | Added `authenticate_user()` — checks both admins and end_users tables; added `authenticate_end_user()`, `get_end_user_by_username()`, `get_end_user_by_id()`. `create_access_token()` and `create_refresh_token()` now accept `user_type` parameter. |
| `provision-gateway/app/middleware/__init__.py` | Added `get_current_user()` dependency — returns unified dict for both admin and end-user tokens with keys: `id`, `email`, `role`, `user_type`. Handles JWT `user_type` claim. |
| `provision-gateway/app/routers/auth.py` | `POST /login` now supports both admin (email) and end-user (username) authentication. Response includes `user_type` field. `POST /refresh` handles both user types. `GET /me` uses `get_current_user` (returns unified dict). |
| `provision-dashboard/src/api/auth.ts` | `TokenResponse` now has optional `admin`/`user` fields and `user_type` field. `AdminUser` interface has optional fields for end-user compatibility. |
| `provision-dashboard/src/store/authStore.tsx` | Login handler merges both `admin` and `user` response formats. Sets `user_type` on the stored user object. |
| `provision-dashboard/src/components/layout/AppLayout.tsx` | Added end-user role detection. Sidebar filters: end-user viewers see only Services page; end-user admins see all pages. Task notification polling handles undefined task IDs. |

### Auth Flow
```
POST /api/auth/login {email: "alice", password: "..."}
  → auth_service.authenticate_user()
  → checks AdminUser table first, then EndUser table
  → returns {user_type: "end_user", user: {id, username, role}}
  → JWT carries user_type claim
  
GET /api/auth/me (Bearer token)
  → get_current_user middleware
  → decodes user_type from JWT
  → returns unified dict: {id, email, role, user_type}
```

---

## 3. Feature: SSL Certificate Management

### Backend (provision-gateway)
| Endpoint | Method | Description |
|---|---|---|
| `/api/system/ssl-certs` | GET | List SSL certificate domains (proxied to provision-api) |
| `/api/system/ssl-certs` | POST | Upload SSL certs (path mode or paste mode) |
| `/api/system/ssl-certs/{domain}` | DELETE | Delete a domain's SSL certs |
| `/api/system/ssl-certs/{domain}/refresh` | POST | Re-import certs from original source path |

### Frontend (provision-dashboard)
- New **SSLPage** (`src/pages/SSLPage.tsx`, 150 lines):
  - Table with columns: Domain, Certificate, Private Key, Expiry (days_left), Actions
  - Color-coded expiry tags: red (≤7d), orange (≤30d), green (>30d)
  - Add Certificate modal: Domain + SSL Directory Path (reads fullchain.pem + privkey.pem)
  - Refresh button: re-imports from source path
  - Delete with confirmation
  - 30s auto-polling + 60s expiry check
- **DeployForm** updated:
  - When HTTPS enabled, shows SSL Certificate dropdown (searchable Select)
  - Selecting a cert auto-fills domain, fullchain_path, privkey_path
  - Domain field disabled when SSL cert selected

---

## 4. Feature: Registry-Based Stats

### Dashboard Changes
The Dashboard (`DashboardPage.tsx`) now uses **registry-based stats** from provision-api instead of raw `docker ps`:

- **Container stats** (`container_stats`): total_expected, healthy_running, unhealthy_running, restarting, down, missing — only counts containers registered in user_registry.yml
- **Service stats** (`service_stats`): healthy, unhealthy, expected — from registry
- **Services count**: Now shows `svcExpected` with breakdown (healthy/unhealthy tags) instead of simple sum
- **Containers count**: Shows registry-based breakdown with color-coded tags (green=up, orange=unhealthy, purple=restarting, red=down, gray=missing)
- **Removed**: User summary cards (now redundant with registry-based stats)
- **Removed**: Direct `/system/stats` call and per-user service aggregation

---

## 5. Feature: Play/Pause Toggle

### UsersPage Changes
- **Replaced** separate Up (▶) and Down (⏸) buttons with a **single toggle button**
- Button shows `PauseOutlined` icon when running, `CaretRightOutlined` icon when stopped
- Auto-detects state: checks `healthy_containers` count to determine play/pause
- `type="primary"` when stopped (prominent play action), `type="default"` when running

### API Changes
- Up/down endpoints now delegated to provision-api via `provision_service.start_user()` / `provision_service.stop_user()`

---

## 6. Dashboard Layout Improvements

### AppLayout Changes
- **Role-based sidebar filtering**: End-user viewers see only "Services" page; end-user admins see all pages; gateway admins see all pages
- **SSL Certs menu item**: New "SSL Certs" entry with `SafetyCertificateOutlined` icon
- **Task notification fix**: Guard against undefined task IDs (`t.id || t.task_id`)

### ServicesPage Changes
- **Templates vs Generated Files separation**: `.env` files classified as templates; `docker-compose.user-*.yml` and `*.user-*.nginx.conf` classified as generated files
- **File auto-location**: `?file=` query parameter auto-opens file and expands directory tree

### Deployment Files Display (UsersPage)
- **Plain text headers** instead of colored Tag labels
- Correct file path structure with directory context
- File paths dynamically derived from service name, user name, and label

---

## 7. Bug Fixes

| Bug | Description | Fix |
|---|---|---|
| Task notification error | "❌ Task undefined... failed" | Handle undefined/missing task IDs (`t.id \|\| t.task_id`) |
| File auto-location | Clicking file link didn't open it | Handle `?file=` query param, auto-expand directory tree |
| Templates vs Generated | Files mixed up in wrong columns | Proper regex-based classification |
| Deployment files markup | Colored Tags instead of plain text | Plain text headers with directory context |
| SSE log endpoint URL | Missing `/api` prefix | Changed to `/api/tasks/{taskId}/log` |
| Sidebar highlight | Wrong menu item highlighted | Fixed `selectedKeys` logic for Users page |

---

## 8. Tests

### New Test Files
| File | Lines | Type | Description |
|---|---|---|---|
| `tests/test_gateway_api.sh` | 347 | Integration (bash) | Full gateway API test: auth, system, services, up/down/password, tasks, audit, LLM, end-user management, proxy, cleanup |
| `tests/test_provision_api.sh` | 252 | Integration (bash) | Full provision-api test: health, docker stats, user CRUD, service lifecycle, rebuild, tasks, nginx, error cases |

### Expanded Test Files
| File | Changes |
|---|---|
| `tests/test_integration.py` | Added `test_container_logs_endpoint`, `test_tasks_log_sse_endpoint`, `test_new_endpoints_exist` |
| `tests/test_unit.py` | Added `TestProvisionService` (14 method existence checks), `TestNoDuplicateConverter` (verifies compose_converter.py is deleted), `TestTasksRouterSSE` (verifies SSE proxying), `TestUsersRouterContainerLogs` (verifies container logs endpoint) |

---

## 9. Infrastructure

| File | Change |
|---|---|
| `provision-gateway/Dockerfile` | Added `python-multipart>=0.0.9` dependency (needed for SSL cert Form uploads) |
| `provision-gateway/app/config.py` | Removed `NGINX_STATE_FILE` configuration (state now managed by provision-api) |
| `provision-dashboard/tsconfig.tsbuildinfo` | Added (TypeScript incremental build info) |

---

## Commit History (chronological)

```
cc72970 fix: ITERATION 1 - multiple UI and infrastructure fixes
b299070 docs: update webui_operation_sequences after ITERATION 1 fixes
e91785d feat: ITERATION 2 - end-user login and role-based access control
8536ae7 fix: compose_converter container_name truncation for multi-hyphen service names
035b8a8 fix: remove gateway duplicate converters — delegate to provision-api
6f07a77 fix: delegate up/down/password to provision-api — remove direct Docker ops
e9084c0 fix: remove remaining duplicates — docker_service, nginx_parser, compose_converter
87928db test: add architecture validation tests — 18 new tests
5cf260e test: add comprehensive functionality tests for provision-api and gateway
afbdf2c refactor: move nginx state reconciliation to provision-api
e1ae7f4 feat: adapt to user_provision changes, fix SSE log streaming
aba9777 feat: registry-based container and service stats on dashboard
0ce6a86 feat: SSL cert management page, dashboard layout improvements
```

---

## Files Not Affected

These files were unchanged between the two commits:

- `provision-mcp/` — no changes
- `docker-compose.gateway.yml` — no changes (only referenced in diff context)
- `provision-gateway/app/models/` — no changes
- `provision-gateway/app/schemas/` — no changes
- `provision-gateway/app/services/llm_service.py` — no changes
- `provision-gateway/app/services/audit_service.py` — no changes
- `provision-gateway/app/services/curl_service.py` — no changes
- `provision-gateway/app/services/proxy_service.py` — no changes
- Most `.gitignore` and config files — no changes
