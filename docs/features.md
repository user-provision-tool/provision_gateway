# Provision Gateway — Features Status

> **Version**: 2.4
> **Date**: 2026-08-24 (updated — cycle 20260824T173309Z v5 ACL-enforcement design implemented + verified, F1–F15 all IMPLEMENTED: the edge `-nginx-acl` is now the sole-exposed entry — TLS termination + dynamic SNI→cert (F2), portal routing (F4), force-https (F5), ACL gate in `location /` (F3/F14), ACL-off pass-through to native Basic (F6), SSL_DIR `:ro` key-holder contract (F12); internal per-service confs simplified to the ACL-free byte-identical form — v4 scaffold / env.d / portal.d / `/__basic__/` removed (F8), `nginx.provision.conf` services-only (F9); `ENABLE_ACL` touches only gateway + edge (F7); `/docker/nginx/env` → 410 (F10); two separated compose files (F11); gateway contract unchanged — verify/`/go/`/exchange (F13); `migrate_v5.py` swept 33 confs, 0 scaffold remain (F15). Live-verified: 239 pytest / 0 (gateway suite), browser 2/0; GAP-16 (edge `WWW-Authenticate: Basic realm="subnet-acl"` `always` on 401) and GAP-17 (migrate_v5 repo-root invocation) fixed.
> Prior: cycle 20260823T204609Z iter-1 hardening G1–G8 verified: nginx `/_set_token` relays the exchange query string via `proxy_pass http://$gw/api/auth/exchange$is_args$args;` (G1); one-default-per-user partial unique index `uq_api_keys_one_default` + drift repair + `delete_end_user` cascade (G2); special-user 403 login path unit-verified `TestG3SpecialUserLogin403` (G3); admins get a default key at registration/login with login/exchange bound to the default `api_key_id` (G4); legacy gateway_token/Bearer fallbacks fully removed — middleware accepts only `provision_token` cookie / `X-Provision-Token` header (G5); API Keys UI adds Mask + Default columns + Set-as-Default (G6); `_ensure_schema` mask backfill — 0 NULL of 29 keys (G7); `POST /api/auth/keys` promotes to default when the user has none (G8). Prior: v4 Service-ACL enforcement merged and live-verified: three-credential token model, hybrid X-Client-Type verify, byte-identical per-service nginx confs + env.d mode switch, unified nginx assembly + portal mode, `/__basic__/` Basic short-circuit, default API key; live-deployment QA fixes QA1–QA4)
> **Purpose**: Quick reference and implementation status tracker for all features.
> **Updated**: 2026-08-27 — cycle 20260827T161836Z iter-3 FINAL verification (supervisor PASSED,
> openGapCount=0): the iter-3 gap list was EMPTY (analyzer PASSED gaps:[], gap-reviewer r1 PASSED
> failures:[], coder filesChanged=[] — no code/config/test changes). Golden F1–F22 all remain
> IMPLEMENTED and prior gaps G1–G4 remain RESOLVED in the working tree (edge `/_set_token`
> non-internal with `$is_args$args` relay at acl-helpers.conf.template:92; gateway NGINX_* = edge
> ports 8767/8768; ENABLE_ACL=true on gateway + edge; test_integration.sh trap EXIT TERM INT HUP).
> Final test evidence: gateway pytest 250/0 + users_provision 388/0 (pythonPassed 638), shell
> suites 124/0 (incl. 13.8 follow-the-`/go/`-edge-exchange), browser 5/0, migrate_v5 dry-run
> VERIFICATION PASSED. No feature row in this doc changed status.
> Prior: doc-accuracy pass (L9/L10 reclassified as future, MCP as broken, template
> mode deprecated, counts corrected).
> **Updated**: 2026-08-28 — gateway robustness fix (no feature row changed status; F1–F22 remain
> IMPLEMENTED). The gateway deadlocked on 2026-08-28 (DB pool exhausted by concurrent authenticated
> load while a leftover browser polled the dashboard; async auth deps blocking on a pool checkout froze
> every worker's event loop). Fix: all five auth dependencies are now synchronous and use short-lived
> DB sessions (closed before the endpoint's external awaits), DB-heavy endpoints release the connection
> before `await`ing provision-api/network calls, and the engine uses `pool_timeout=2` fail-fast. See
> `architecture.md` §"DB connection discipline" and the testing plan `MW7`/`DB5` (regression targets).
> **Updated**: 2026-08-28 — cycle 20260828T190332Z gateway source-project scan re-architecture FINAL
> verification (supervisor PASSED, openGapCount=0, iteration 3): scan-rearchitecture plan's F1–F37
> implemented + verified — marker-only template classification (git `ls-files` deleted; git used only
> for N/M badges), shallow per-recipe-dir scans (os.scandir, never rglob), `.provision-state.json`
> per-project state with fingerprints (project_state.py), cache-warm `_get_service_info`, root-only
> default with explicit recipe paths (`_discover_recipes` deleted), `POST /api/services/{name}/recipes`
> + `GET /api/services/{name}/tree` with 400/404 semantics (ServiceNotFoundError), 15 sync `def`
> handlers with threadpool wraps, gear-icon recipe editor modal + lazy per-directory tree. DB1
> large-repo freeze RESOLVED (synthetic 9600-file repo: cold 46ms / warm 6ms / 4.7KB, /health
> non-blocking). GAP-15 RESOLVED (non-dir → 400, ghost service → 404). Iteration-3 gap list EMPTY
> (analyzer gaps:[], gap-reviewer r1 failures:[], coder filesChanged=[]) — no further changes after
> iter-1. S1 holds (0 `_users_provision` edits). Final test evidence (QA iter-3 r1 + supervisor):
> pytest 293/0 (11.63s), shell 124/0 (10+53+10+24+27), browser 4/0, registry 15/15 RESOLVED 0 OPEN.
> Rows updated: S5 (marker-only classification + lazy tree), RC1 (root-only default + explicit recipe
> paths).
> **Updated**: 2026-09-01 — cycle 20260901T102115Z file-selection-and-generation design FINAL
> verification (supervisor PASSED, openGapCount=0, iteration 3): the iter-3 gap list was EMPTY
> (analyzer PASSED gaps:[], gap-reviewer r1 PASSED failures:[], coder filesChanged:[] — no
> code/config/test changes). GAP-23/GAP-24 (implemented iter-2) remain live, re-verified this
> iteration: race-window documentation at all 4 implementation sites (design.md §11 L1718,
> routers/services.py:393, generation_jobs.py:75, user_provision_tool/api.py:350); compose-preview
> chain end-to-end (gateway GET /{name}/compose-preview → provision-api GET
> /services/{name}/compose/preview → DeployForm volume rows from volume_keys; browser Deploy modal
> auto-populated 54 volume rows, 0 console errors). Final test evidence (QA iter-3 r1 + supervisor):
> gateway pytest 348/0 + provision-api pytest 438/0 (pythonPassed 786), shell 124/0 (53+10+10+24+27),
> browser 4/0, edge nginx 0 [error], registry 24/24 RESOLVED 0 OPEN. No feature row in this doc
> changed status.
> **Updated**: 2026-09-01 — cycle 20260901T164901Z file-selection-and-generation design FINAL
> verification (supervisor PASSED, openGapCount=0, iteration 3): GAP-1..GAP-4 (implemented iter-1)
> re-verified live — per-user-file GET/PUT and save-generated now reject recipe_path traversal at
> every join site (`..`/`../..` → 400, absolute recipe_path re-rooted into the project dir;
> users.py `_resolve_per_user_file`, services.py save-generated + `_compute_needs_env`/check-missing
> fallbacks, llm_service validation drafts reject invalid recipe_path, service_manager
> `_scan_recipe_dir` skips traversal-invalid paths); new traversal-safe `file_sets.derive_profiles`
> + admin-gated `POST /api/services/{name}/file-sets/derive`; both panels (GenerateMissingPanel,
> DeployForm) recompute the profiles section from the in-panel compose selection (23-profile union
> live, nothing persisted) — rows F3-F11/F17-F19/F47/F49 DEVIATES → IMPLEMENTED. Iter-2 env fix
> GAP-5 (`server_names_hash_bucket_size 128` in nginx.provision.conf) holds: subnet-acl-nginx Up
> RestartCount 0, 8766 401/444 signature; GAP-6 (test_gateway_api 4.1 down-op) resolved 53/0.
> Iter-3 GAP-7 (flaky test_merge_real_docker_golden) fixed in the test-script layer
> (`_retry_transient` bounded retry, user_provision_tool tests). Final test evidence (QA iter-3
> r2 + supervisor): gateway pytest 363/0 + user_provision_tool pytest 441/0 (pythonPassed 804),
> shell 245/0, browser 6/0 (iter-3 r1 full matrix; r2 0/0 — webui unchanged, 0 console errors),
> registry 7/7 RESOLVED 0 OPEN. All F1-F62 IMPLEMENTED — no feature row in this doc changed status.
> **Updated**: 2026-09-03 — cycle 20260903T154936Z file-selection-and-generation design FINAL
> verification (supervisor PASSED, openGapCount=0, iteration 3): the iter-1/iter-2/iter-3 gap
> lists were ALL EMPTY (analyzer PASSED gaps:[] each iteration, gap-reviewer r1 PASSED
> failures:[], coder filesChanged:[] — no code/config/test changes in this whole cycle; drift
> clean: no source file newer than 2026-09-01T19:31:40Z). F1-F62 all remain IMPLEMENTED;
> prior-cycle GAP-1..GAP-7 (cycle 20260901T164901Z) remain RESOLVED 7/7, re-verified in source
> this cycle (users.py:557-674 traversal rejection + derive 400s; nginx.provision.conf:27
> server_names_hash_bucket_size; _retry_transient tests/test_selection_generation.py:17/169);
> the four design-documented bugs still FIXED in source (no DEVIATES table rows). Final test
> evidence (QA iter-3 r1 + supervisor): python 804/0 (gateway 363/0 + user_provision_tool
> 441/0), shell 245/0 (27+10+10+24+53+121), browser 1/0 (dashboard renders, ACL Enabled,
> 5/5 System Components Running, console 0 errors), stack 5/5 Up RestartCount 0 (probes
> 200/200/200), registry 0 OPEN. No feature row in this doc changed status.
> **Coverage note**: this doc is the capability summary; the complete endpoint reference (including
> `GET /api/auth/me`, `POST /api/auth/logout`, `DELETE /api/tasks/{task_id}`, per-service container logs,
> and the DB `_ensure_schema` migration) is in `api_references.md`.

---

## Status Legend

| Icon | Status |
|---|---|
| ✅ | Implemented & Verified |
| 🟡 | Implemented — Needs Verification |
| 🔴 | Not Implemented |
| ⚠️ | Partially Implemented / Known Issues |
| 🔮 | Future / Stretch Goal |
| 🔜 | Future — not yet shipped (needs redesign + implementation) |
| 🧟 | Deprecated / dead — code or row exists but is not a live feature |
| 💥 | Broken — shipped but non-functional against the current system |

---

## F. v5 Feature cross-reference (requirement IDs → feature)

Traceability from the v5 requirement/design doc's F-IDs to the features below (design anchor
`_tasks/acl-enforcement-design-v5.md`):

| F-ID | Feature |
|---|---|
| F2 | TLS termination + dynamic SNI→cert (edge, ACL1/§Edge) |
| F3 / F14 | ACL gate `location /` `auth_request` → `/api/auth/verify` (edge; ACL2/ACL16) |
| F4 | Portal routing on the edge (ACL15) |
| F5 | Force-https for certed services (edge; ACL16) |
| F6 | ACL-off pass-through → native Basic (ACL13) |
| F7 | `ENABLE_ACL` touches only gateway + edge (ACL1) |
| F8 | Internal per-service confs SIMPLE ACL-free (ACL12 / users_provision T1) |
| F9 | `nginx.provision.conf` services-only (ACL14 / users_provision T6) |
| F10 | `/docker/nginx/env` → 410 (users_provision API-14) |
| F11 | Two separated compose files (gateway + provision) |
| F12 | `SSL_DIR` `:ro` key-holder contract (edge-security.md) |
| F13 | Gateway contract unchanged — verify `/go/`/exchange (ACL2/ACL7) |
| F15 | `migrate_v5.py` sweep (users_provision MV-1) |

---

## A. Admin Authentication

| # | Feature | Status | Notes |
|---|---|---|---|
| A1 | Admin registration (first-run setup) | ✅ | `/api/auth/setup`, SetupWizard page |
| A2 | Admin login (email + password) | ✅ | v4 cookie-jar auth — `POST /api/auth/login` returns a `provision_token` cookie (token_type=cookie, Max-Age=604800); Bearer access_token/refresh_token removed |
| A3 | Token auto-refresh | 🔴 | Removed in v4 — `/api/auth/refresh` dropped (three-credential model); auth via provision_token cookie; `POST /api/auth/logout` clears it |
| A4 | Role-based access (admin/viewer) | ✅ | `require_admin` → `require_gateway_token` (provision_token cookie / `X-Provision-Token` header), `app/middleware/__init__.py` |
| A5 | Password change | ✅ | `PUT /api/auth/password` |
| A6 | End-user registration (portal users) | ✅ | `POST /api/auth/users/register` |
| A7 | End-user approval workflow | ✅ | `PUT /api/auth/users/{id}/approve` |
| A8 | End-user role management | ✅ | `PUT /api/auth/users/{id}` — roles: viewer, special, admin |
| A9 | Special users per-user assignment | ✅ | Per-user `allowed_special_users` via toggleable tags modal in Users page; v4 F8 B11 — special users blocked at dashboard (login 403 special; middleware blocks role=special); the 403 path is unit-verified by `TestG3SpecialUserLogin403` (G3 — valid bcrypt password on a special user → 403, no token minted; placeholder-password accounts remain blocked as 401-as-blocked) |
| A10 | Deployable users list | ✅ | `GET /api/auth/users/deployable` — DB-driven, returns approved+active users |
| A11 | End-user login (JWT with user_type) | ✅ | `POST /api/auth/login` supports both admin and end-user tokens; role=special → 403 at login, middleware blocks role=special (F8 B11, G3 unit-verified) |
| A12 | Role-based sidebar filtering | ✅ | End-user viewers see only Services page |
| A13 | Auto-register deployed users | ✅ | `GET /api/users` syncs provision-api users into gateway `end_users` table on each list |
| A14 | Special users as DB records | ✅ | Special users registered via Users page (role="special"), not via Settings textarea; role=special blocked at dashboard (login 403, F8 B11; G3 unit-verified) |

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
| D9 | ACL status indicator | ✅ | `GET /api/system/status` returns `acl {gateway, edge, enabled, consistent}` (edge `ENABLE_ACL` read via provision-api `/docker/container/{name}/env`); dashboard header shows green **ACL: Enabled** / red Disabled / orange Mismatch / gray unknown; `subnet-acl-nginx-acl` added to the components table (2026-08-28) |

---

## S. Service Management (Source Projects)

| # | Feature | Status | Notes |
|---|---|---|---|
| S1 | Add service — from Git repo | ✅ | `POST /api/services` (mode=git), proxy support |
| S2 | Add service — from file upload | ✅ | `POST /api/services` (mode=upload) |
| S3 | Add service — from ZIP upload | ✅ | `POST /api/services` (mode=upload, zip_content) |
| S4 | Add service — from template | 🧟 | Misnamed: template mode is the **DB-template path** (see S18), not LLM; LLM file generation is a separate feature (L5–L8/L13). The "From Template" UI tab was removed (GAP-1); the API path is dormant (no seed data) |
| S5 | File tree browser | ✅ | Directory structure, .git filtering, git status tags. Template classification is marker-only (scan-rearchitecture cycle 20260828T190332Z, F1/F2): a file is Generated iff a sibling `{file}.generated` marker exists — git is never used for classification (git `ls-files` deleted; git only for N/M badges); `.generated` marker files excluded from all listings. Tree is lazy per-directory (F33): detail page loads children via `GET /api/services/{name}/tree?dir=` on expand, `?file=` deep link auto-expands ancestors |
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
| S18 | Add service — from pre-built template (DB) | 🧟 | **DEPRECATED/dormant** — `mode=template` + `GET /api/services/templates` remain at the API level, but the `service_templates` table has **no writer/seed in the repo** and **no behavioral test** (only route/method presence checks); it is reachable only against manually-seeded data. The "From Template" UI tab was removed (GAP-1) — the modal offers Git + Upload Zip only. |
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
| L9 | Troubleshooting chat | 🔜 | Future — the frontend chat modal exists (`AppLayout.tsx`, history maintained) but the backend troubleshoot contract was never implemented: `POST /api/llm/generate {generate_type:'troubleshoot'}` returns `400 Invalid type: None` (`llm.py:116-118` reads `type`) |
| L10 | Service template generation | 🔜 | Future — `llm.py:117` whitelists only `docker_compose\|nginx_conf\|env_file\|dockerfile`; `service_config` is rejected and no frontend sends it |
| L11 | Credential encryption | ✅ | API keys are stored as `token_hash` + `mask` (SHA-256, never recoverable); AES-256-GCM (`utils/crypto.py`) protects LLM BYOK + proxy credentials, which must be decrypted at call time |
| L12 | Missing files check API | ✅ | `GET /api/services/{name}/check-missing-files` → provision-api `GET /services/{name}/check-missing-files` |
| L13 | Auto-deploy LLM file generation | ✅ | Checkbox on DeployForm; LLM generates missing compose/nginx/env/Dockerfile before deploy — auto-submits (G6). Non-autoDeploy mode saves generated files to disk before deploy (G12 fixed in Iteration 2). |
| L14 | Generated files review in deploy | ✅ | Inline preview of LLM-generated files with review before saving; clickable file tags open Monaco editor (G6 confirmed working) |
| L15 | Missing provision_service import fix | 🧟 | Not a feature — a one-time "Iteration 4" changelog fix (adding an import). No runtime behavior to protect |
| L16 | DeployForm error handling | ✅ | Live behavior — `checkError` state, error Alert, disabled deploy button (`DeployForm.tsx`). Listed here as an Iteration-4 changelog row; the behavior belongs to the deploy flow (P1/P11), not the LLM group |

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

> **Non-functional against the v5 gateway.** `provision-mcp/server.py` `verify_admin_token` requires
> `type=='access'` (a credential type v5 removed) and `call_gateway` sends `Authorization: Bearer`
> (which v5 middleware rejects) — the MCP server **cannot authenticate**. Needs redesign.

| # | Feature | Status | Notes |
|---|---|---|---|
| MC1 | SSE streaming deploy workflow | 💥 | Event types: session, status, request_generation, deployed, task_update, done, error |
| MC2 | Session-based state management | 💥 | In-memory dict, not persisted |
| MC3 | JWT verification | 💥 | Uses GATEWAY_SECRET_KEY — but requires `type=='access'` (removed in v5) |
| MC4 | File generation request/response | 💥 | request_generation event + submit-generation endpoint |
| MC5 | Task polling loop | 💥 | 2s interval, 60 max iterations (2 min timeout) |
| MC6 | Session query endpoint | 💥 | `GET /session/{id}` |

---

## ACL. ACL & Access Control

| # | Feature | Status | Notes |
|---|---|---|---|
| ACL1 | ENABLE_ACL environment variable | ✅ | v5 (F7): `ENABLE_ACL` is read only by the gateway + the edge `-nginx-acl`; the `-api` no longer reads `ENABLE_ACL`/`PORTAL_MODE` (env.d mode switch removed). Toggle = recreate the edge + restart the gateway. Defaults `false` (compose `ENABLE_ACL=${ENABLE_ACL:-false}`). **The edge is the sole client-facing entry in fullset** (decision 10): published on `${ACL_HTTP_PORT:-8767}` / `${ACL_HTTPS_PORT:-8768}`, and `NGINX_HTTP_PORT`/`NGINX_HTTPS_PORT` are the **edge's** client-facing ports. |
| ACL2 | /api/auth/verify endpoint | ✅ | Gateway contract UNCHANGED in v5 (F13); now invoked by the EDGE `-nginx-acl` `/_auth_jwt` auth_request subrequest (F3), not by internal per-service confs. v4 hybrid X-Client-Type rule (X-Provision-Token header ⇒ api / provision_token cookie ⇒ browser / Accept text/html ⇒ browser / else ⇒ api); X-Client-Type on every response; X-Auth-Action always incl. unauthorized; `is_browser` map removed; F3 ordering — revocation check runs before admin bypass (R1), provision_token validated for `expires_at` and active/approved; G5 — middleware `_extract_gateway_token` accepts ONLY `provision_token` cookie / `X-Provision-Token` header (legacy `gateway_token` cookie and Bearer fallbacks removed; legacy/access-typed tokens return 401 live) |
| ACL3 | gateway_token cookie (admin session) | 🔴 | Removed in v4 (G5 hardening) — gateway mints only `provision_token` (+ 30s exchange code); `decode_gateway_token` accepts only `type=provision`; legacy `gateway_token` cookie, Bearer, and access-typed tokens all return 401 live; `tasks.py` drops `gateway_token`/Bearer credentials |
| ACL4 | provision_token cookie (service access) | ✅ | HTTP-only cookie; v4 1-week TTL (604800s via `create_provision_token`) bound to the default key's `api_key_id`; cookie Max-Age=604800 (compose default `PROVISION_COOKIE_TTL` fixed in QA3); also accepted via `X-Provision-Token` header for API clients (G5) |
| ACL11 | Login token model | ✅ | v4 three credentials: `provision_token` + 30s exchange code; Bearer `access_token`/`refresh_token` dropped; `POST /api/auth/logout` added (clears cookie) |
| ACL5 | HostnameIndex | ✅ | In-memory hostname-to-registry-entry lookup; maps service URLs to registry entries |
| ACL6 | Registry (user_registry.yml) | ✅ | Read-only registry wrapper; reads provision-api registry via shared filesystem |
| ACL7 | /go/{hostname} service redirect | ✅ | v4/v5 (F13): issues 30s HMAC-signed exchange code + `Location` header; the EDGE `/_set_token` relays `proxy_pass http://$gw/api/auth/exchange$is_args$args;` (G1 — `?code=&redirect=` query string preserved, no more 401 "Missing exchange code"); `/api/auth/exchange` swaps code→provision_token via 302+Set-Cookie (`; Secure` per registry flag); no JWT in URL (F13) |
| ACL8 | Viewer ACL: own services only | ✅ | Viewers access own services + allowed_special_users; v4 N1 trims each allowed_special_users element |
| ACL9 | Admin unrestricted access | ✅ | Admins have unrestricted access to all services |
| ACL10 | X-Service-Basic credential injection | ✅ | Base64 user:password header for nginx auth_basic on target service; v4 N2 — no "123456" fallback (passwd-less returns empty credential) |
| ACL12 | Byte-identical SIMPLE per-service conf (v5) | ✅ | v5 (F8): `render_nginx_conf` emits the SIMPLE ACL-free form (`server_name` + `auth_basic` + variable-based `proxy_pass`), byte-identical across ENABLE_ACL — no `ENABLE_ACL` branch, no v4 scaffold (`TestV5SimpleNginxSyntax`: no auth_request/WWW-Authenticate/`@auth_401`/`@auth_403`/env.d/`$client_type`; byte-identical render tests). `strip_v4_scaffold` removes stale v4 tokens from deployed confs (33 swept live, 0 scaffold remain) |
| ACL13 | ACL-off pass-through → native Basic (v5) | ✅ | v5 (F6/B5): the `/__basic__/` short-circuit and env.d are REMOVED — internal per-service confs are simple ACL-free (`auth_basic` + `auth_basic_user_file`). In ACL-off mode the edge passes traffic straight through to the internal native Basic: non-certed service over http → `401 WWW-Authenticate: Basic realm="example-service - alice"` (native dialog restored) |
| ACL14 | Minimal deployment (services-only nginx.provision.conf) | ✅ | v5 (F9): internal `nginx.provision.conf` is services-only — `include /etc/nginx/services.d/*.conf;` + `listen 80 default_server; return 444;` catch-all; no portal.d/env.d includes (`TestV5PortalDDeprecated.test_internal_nginx_provision_conf_services_only`). Internal nginx host ports live only in the base compose for the minimal deployment; fullset suppresses them via the separated gateway compose + edge entry (F11) |
| ACL15 | Portal routing on the edge (v5) | ✅ | v5 (F4): portal routing moved to the EDGE `-nginx-acl` portal server (`server_name ${PORTAL_HOSTNAME} 127.0.0.1 localhost`): portal host → gateway/dashboard, `location = /api/auth/verify|/api/auth/exchange { return 404; }` (GAP-31 internal-only), `/api/` → gateway (`proxy_read_timeout 3600s`, U3), `/go/` rewrite, `/login` → gateway, `/alert` + `/` → dashboard. Internal nginx no longer renders portal.d vhosts (F9). Live: portal host → 200 dashboard, verify/exchange → 404 |
| ACL16 | API-first 401/403 (edge) | ✅ | v5 (F3, GAP-16 fixed): the 401/403 challenge moved to the EDGE `@auth_401/@auth_403` — `add_header WWW-Authenticate 'Basic realm="subnet-acl"' always;` at `acl-helpers.conf.template:49,56` (`always` is required — nginx drops add_header on 401 otherwise). Live: API client → `401` WITH the challenge; browser → `302` portal /login; unknown client → `401` + header (fail-closed). Regression `test_auth_401_challenge`. No `?redirect=` param (GAP-14) |

---

## AK. API Key Management

| # | Feature | Status | Notes |
|---|---|---|---|
| AK1 | Create API key | ✅ | `POST /api/auth/keys`; admin for any user, viewer for self |
| AK2 | List API keys | ✅ | `GET /api/auth/keys`; admin sees all, viewer sees own |
| AK3 | Revoke/delete API key | ✅ | `DELETE /api/auth/keys/{id}`; admin revokes any, viewer own |
| AK4 | API key lifetime (provision token) | ✅ | API keys are **1-year** provision tokens carrying their own `api_key_id` (`auth_service.create_api_key_token`); the login/exchange **cookie** is 1-week default-bound (`PROVISION_COOKIE_TTL`); the `/go/` exchange **code** is 30s (`EXCHANGE_CODE_TTL_SEC`) |
| AK5 | ApiKeysPage frontend | ✅ | Admin page at /api-keys for managing end-user API keys; iter-1 (G6) adds a **Mask** column and a **Default** column (gold star tag) plus a **Set-as-Default** action (`PUT /api/auth/keys/{id}/default`) — browser-verified live (click moves the default, tag/button update, DB default moves) |
| AK6 | Default key (is_default) | ✅ | `ApiKey.is_default` + partial unique index `uq_api_keys_one_default` on (user_id) WHERE is_default=1 (G2) with `_ensure_schema` drift repair and `delete_end_user` cascading api_keys (G2); `PUT /keys/{id}/default`; `DELETE /keys` rejects revoking the default (400); default created at registration and for admins — login/exchange bind admin tokens to the default key's `api_key_id` (G4, live token `api_key_id: 9`); `POST /api/auth/keys` promotes the new key to default when the user has none (G8, v4 §6.1.6); 1000-key cap / lazy eviction; live DB migrated via `_ensure_schema` (QA1) and drift scan clean (every default-bearing user has exactly 1 default) |
| AK7 | API key = JWT + mask + real user_type | ✅ | Keys minted as 1-year JWT provision tokens with own `api_key_id` + `mask`; key-targeted tokens carry the target's real `user_type` (GAP-07); `_ensure_schema` backfills `mask` from `token_hash` for NULL/empty-mask rows (G7) — live 0 of 29 keys have NULL mask |

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
| RC1 | Recipe paths — root-only default + explicit set | ✅ | Scan-rearchitecture (cycle 20260828T190332Z, F4/F15): `_discover_recipes` DELETED — no auto-detection; default (auto origin) is root-only `["."]` (`_resolve_recipe_paths`). Recipe paths set explicitly via `POST /api/services/{name}/recipes` (`set_recipes`; rejects `..`/absolute/non-dir → 400, unknown service → 404 ServiceNotFoundError) |
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
| Dashboard | 9 | 9 | 9 | 0 |
| Service Management | 19 | 15 | 15 | 4 |
| User Provisioning | 15 | 15 | 15 | 0 |
| Service URL & Connectivity | 5 | 5 | 5 | 0 |
| LLM Integration | 16 | 12 | 12 | 4 |
| Real-Time Operations | 7 | 7 | 7 | 0 |
| Reconciliation | 7 | 7 | 7 | 0 |
| System Monitoring | 7 | 7 | 7 | 0 |
| Audit & Logging | 5 | 5 | 5 | 0 |
| Proxy Management | 9 | 9 | 9 | 0 |
| User Management | 7 | 7 | 7 | 0 |
| MCP Server | 6 | 0 | 0 | 6 |
| ACL & Access Control | 16 | 15 | 15 | 1 |
| API Key Management | 7 | 7 | 7 | 0 |
| Alerts Page | 2 | 2 | 2 | 0 |
| Multi-Recipe Services | 6 | 6 | 6 | 0 |
| **TOTAL** | **157** | **141** | **141** | **16** |

**Implementation Rate:** 140/156 = **89.7%**
**Verified Rate:** 140/156 = **89.7%**
