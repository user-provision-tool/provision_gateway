# Provision Gateway — Features Status

> **Version**: 2.8
> **Date**: 2026-09-29 (updated — AK1/AK6/AK7: API-key targets are **prefixed references** (`end_user:<id>` / `admin:<id>`; bare id = end_user; omitted = the caller's own, in their own id space), so an admin can finally mint a key **of their own** — before, `_target_identity` resolved `EndUser` before `AdminUser` and `admins.id 1` collided with `end_users.id 1`, silently minting an admin's "own" key for alice. The default-key partial unique index widened to `(user_id, target_type)` and every key query carries both halves of the reference. Prior: 2.7 — ACL4: the edge's `/_auth_jwt` forwards the CLIENT's `X-Provision-Token` (a cookie-less API client with a valid key is admitted; the browser flow rides on the relayed `Cookie` header; precedence `cookie or header`, cookie first), the defect that made a valid key behave like no credential is closed, and the realm tells which layer refused a 401). Prior: 2026-09-28 — L33 extended: the Source Projects row acts only on its OWN path's jobs, and no UI surface carries a busy guard, since G33's token already makes parallel generations safe; every job records its `origin`). Prior: 2026-09-26 — the service-card / task-lifecycle round: **L31** the collision token in generated artifact names (GRILLING G33), **L32** the pause lifecycle closing behind itself (a paused object is cancellable; answering consumes its leg — G27/G34), **L33** one work surface per path (sessions group with instances, one-line card notes, silent Tasks polling with no `Answer`, the Source Projects row's state-aware slot, the card's `Task:` link + `nginx:` row + recipe-scoped `LLM fix:` line). Verified live by T20/T23–T30 on the frozen build — `_tasks/service-cards-round-20260926.md`. Prior: 2.4 — cycle 20260824T173309Z v5 ACL-enforcement design implemented + verified, F1–F15 all IMPLEMENTED: the edge `-nginx-acl` is now the sole-exposed entry — TLS termination + dynamic SNI→cert (F2), portal routing (F4), force-https (F5), ACL gate in `location /` (F3/F14), ACL-off pass-through to native Basic (F6), SSL_DIR `:ro` key-holder contract (F12); internal per-service confs simplified to the ACL-free byte-identical form — v4 scaffold / env.d / portal.d / `/__basic__/` removed (F8), `nginx.provision.conf` services-only (F9); `ENABLE_ACL` touches only gateway + edge (F7); `/docker/nginx/env` → 410 (F10); two separated compose files (F11); gateway contract unchanged — verify/`/go/`/exchange (F13); `migrate_v5.py` swept 33 confs, 0 scaffold remain (F15). Live-verified: 239 pytest / 0 (gateway suite), browser 2/0; GAP-16 (edge `WWW-Authenticate: Basic realm="subnet-acl"` `always` on 401) and GAP-17 (migrate_v5 repo-root invocation) fixed.
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
> **Updated**: 2026-09-25 — GRILLING G27–G31 / D1–D5 implemented and verified (rows **L26–L30**
> added; **L24 marked OBSOLETE**; L18's `job_live` clarified). What landed: a pause is a
> **terminal leg** and answering it starts a **second job** (`POST /llm/jobs/{id}/input` and
> the parked-state resume branch deleted; the card follows the current job); one record shape
> with `gt-`/`api-` ids, per-task logs on both sides and the `record` mirror removed, ONE Tasks
> list with a source column; the gateway's **own** generation pool with its own
> `generation_at_capacity` refusal beside the -api's `at_capacity`, both on the card header;
> one cleanup contract (terminal set, TTL 604800 s, cap 1000, oldest-first, log disposed with
> the record, a paused record never pruned, TTL-on-reads / cap-on-submit in both engines); and
> DB-carried locking (registry in SQLite, WAL + `busy_timeout`, capacity counted in-transaction,
> atomic claim, cancellation on the row, a lease row per loop, coalesced progress). Also fixed
> on the way: the deploy auto-run polled only the job id captured at entry, so answering a
> pause left the deploy unissued with **no error** — it now follows `jobIdRef`; `cancelJob`
> adopts the status the server returns instead of asserting `cancelled`; the Tasks list's
> cancel set was aligned with the server's `job_live`; `ComposeFixPrompt` awaits its submit;
> a configured `0` in the new cleanup knobs is honoured rather than swallowed by `or default`;
> and the -api's cap sweep no longer evicts a LIVE task. Final test evidence: gateway pytest
> **482/0**, provision-api pytest **506/0**; traces on one build — **T6, T9, T13 (rows 1–7),
> T14, T16, T18, T19 (fresh gateway refusal incl. a real `503`), T20, T21, T22** all PASS, plus
> the runtime checks (`wal`, four `loop_leases` rows, 0 non-terminal jobs across a cancel).
> The previous traces' 44 `probe-*` subjects were swept through the UI (0 entries / 0
> containers left). Round record: `_tasks/G27-G31-round-20260925.md`.
> **Updated again**: 2026-09-25 (GRILLING-wide verification round). The audit found the
> images then running **predated two of that round's own fixes** (`_int_setting` and the
> -api's finished-only cap sweep), so all three images were rebuilt from the frozen sources
> and their digests recorded before the traces were re-run. Both fixes were then proven live
> with temporary overrides (`GATEWAY_MAX_GENERATIONS=0` → every generation refused at "limit
> 0"; `GATEWAY_MAX_JOBS=0` → 136 finished gateway records swept to zero with the 5 paused ones
> untouched; `TASK_MAX_COUNT=0` → 491 -api records → 1, the just-submitted LIVE task surviving
> with its log). **One GRILLING agreement was found unimplemented and closed**: a gateway
> refusal's `Retry` was a permanently disabled dead control (its record carried no options),
> so the refusal now records the request it was made with and its Retry re-runs the deploy
> including generation — verified end-to-end (job 333 → deploy registered → session removed).
> Evidence: `_tasks/GRILLING-verify-round-20260925.md`.
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
| P7 | Service password management | ✅ | Re-hash, rewrite .htpasswd, **re-record the registry's `passwd_plain`** (2026-09-29 — without it the gate injects an empty credential and a protected service can never be opened), nginx reload. Empty `passwd` is rejected (400); `DELETE .../password` removes protection: the conf is re-rendered without `auth_basic`, the entry's `passwd`/`passwd_plain`/`htpasswd_path` are cleared, the htpasswd is deleted |
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
| L17 | Pre-deploy session card IS the service card | ✅ | GRILLING **G24**: sessions render through the SAME `ServiceCard` widget as instances, differing only by status (`UsersPage.tsx` session block); the alert lives on the panel **header** so it shows folded (`ServiceCard.tsx:86-92`); the instance-only actions (`Rebuild`, `pause/play`, `Redeploy`, `key`, `Dup`) are suppressed by omission from `actions`. Verified live by **T14**. |
| L18 | `cancel` follows the JOB, not the session's mirrored state | ✅ | G24 rule 4. `/api/pre-deploy` returns a server-computed **`job_live`** per session (`pre_deploy.py:57` = `status in ("queued","running","awaiting_input")` — `paused` is **deliberately absent**: under G27 a pause is TERMINAL, so a paused leg has stopped and must not offer `cancel`; the Tasks list uses the same set) and the card gates on it, so the UI and the delete endpoint's `job_live` guard can no longer disagree. `cancel` additionally reads the status the server reports rather than trusting a 200, so it cannot claim success with no job. Verified live by **T14** and by T13's `failed + analyzing` window (`Cancel` present, gone once the job stopped). |
| L19 | `LLM based fix` is the ANALYSIS step of `check` | ✅ | GRILLING **G26**. The job is `job_type: fix` with phases exactly `["analyze"]`: it reads the failed task's log whole plus context and returns a **fixing guide** — it writes no files (`llm_service.generate_fix_guide`). On success the gateway performs the failed→paused handoff itself: the pre-deploy session is written **first** (carrying the guide, the task id and the slot), then the -api entry is **fully deleted** — add-then-delete, **the task is not deleted**. The analysis runs first: the -api entry stays `failed` and no session exists until it succeeds. `failed + analyzing` is computed gateway-side. No `-api → gateway` call. Verified live by **T13** (rows 1–7; add-then-delete proven by timestamps). |
| L20 | A guided restart emits `.fix`, not `.base` | ✅ | GRILLING **G25**: the naming follows the **trigger**, not the slot. `_fix_mode = bool(request.get("fix"))` selects `FIX_FILENAME`; the conflict resume sets `resumed["fix"] = True`, and `check`'s confirm carries `fix: true`. Verified live by **T11** (deploy path, job 214) and **T12** (generate path, job 217) — both produced `docker-compose.fix.yml` and the -api rendered that file; both previously certified `docker-compose.base.yml`. |
| L21 | The analysis step's `check` handoff pre-fills the guide | ✅ | G26 row 5: `check` opens the deploy panel with the guide pre-filled verbatim (verified byte-identical to the registry's guide by md5 in **T13**), and confirming restarts the path with `.fix` generation. **Label-pin defect RESOLVED** (2026-09-22): the panel pins the session's label on open, but for ~600 ms the un-hydrated form still reads `label: "0"` — the submit path now **re-asserts the pin** at submit time (`DeployForm.tsx` `handleDeploy`: `snap.label = pinnedAtSubmit`, read from the same `preselectedLabel` source as the open-time pin, shared via one `pinnedSessionLabel()` predicate), so an unpinned `values.label` can never reach the request regardless of click speed. Verified live by 3 confirm-as-fast-as-possible `check` rounds (sessions `browserverify/probe-slow/4` ×2 and `/5`): every run's `generation_jobs.request.label` equalled the session's own label and produced `.env.raw.browserverify.<label>` with the right label. See `_tasks/traces-r2/REMAINING.md`. |
| L22 | A `check` restart carries the session's global-proxy setting | ✅ | G20 applied to the `check` restart (G26 row 5): a resumed path **replays the options it was created with**, so the "Use global proxy" tick travels with the session. The handoff stores it — `_llm_fix_handoff` derives it from the FAILED instance's registry `build_args` (the proxy's four env keys; read before the add-then-delete removes the entry that carries it) and puts it on the paused session via `pre_deploy_registry.add_session(use_global_proxy=...)`. The dashboard seeds the panel from the session (`UsersPage.tsx` `check` → `preselectedUseGlobalProxy`) and `DeployForm.tsx` applies it synchronously on open (before the first await, avoiding the label's hydration window) and re-asserts it at submit unless the operator toggled the box. **Proxy-carry-over defect RESOLVED** (2026-09-22): before, a `check` restart registered `build_args: {}` (`browserverify/probe-slow/4`, `/5`) while the deploy that created the session had recorded the proxy args. Verified live on `proxycarry/probe-slow/0`: panel opened with the tick checked, restart entry `build_args` = the four proxy args, identical to the original deploy's, `request.label` = the session's label. |
| L23 | The **generate** entry point records the proxy too (L22 parity) | ✅ | G20's second entry point. L22 fixed the *deploy* path; the **generate** entry point was missed: `generateMissingFiles` built its `startJob` payload without `use_global_proxy`, so the pre-deploy session that job opens (`llm.py` `_session_scoped` → `pre_deploy_registry.add_session`) defaulted to `use_global_proxy: false` even with the panel's box ticked, and a later `check` on that parked session replayed `false`. Both entry points now read ONE predicate, `DeployForm.tsx` `effectiveUseGlobalProxy()` (the session seed + `proxyTouchedRef`, via the same `seedUseGlobalProxy` the deploy path used); the deploy-scoped generation reads it from the submit-time `snap` (the panel is destroyed by then). The router threads the request value for the session it creates — the request is the only source there (no instance exists yet to derive `build_args` from, unlike the fix handoff). **Generate-path proxy defect RESOLVED** (2026-09-22). Verified live: `genproxy/probe-conflict/0` via *Generate Missing Files via LLM* on the conflicting `docker-compose.yml` with the box ticked → conflict popup → "Fix the compose" → review gate; `generation_jobs.request.use_global_proxy = true` (job 257; job 256, the pre-fix T12 run, had the key **absent**) and the parked session `genproxy\|probe-conflict\|\|0` records `use_global_proxy: true` (pre-fix `finalgrill\|probe-conflict\|\|1` recorded `false`). `.fix` sanity held: `result.files` = `docker-compose.fix.yml`, `result.selection.compose` = `['docker-compose.fix.yml']`. |
| L25 | The compose-selection alert agrees with the `Deploy` button on a guided restart | ✅ | **Contradictory-alert defect RESOLVED** (2026-09-23). `DeployForm.tsx` waives BOTH "no compose selected" clauses on a guided restart (`fixingGuide`) — deliberately, since the analysis empties the faulty slot so the pipeline regenerates it and the restart is exactly the action the analysis exists to enable. The **alert** beside the button computed its condition from a different set of terms and did **not** carry the waiver, so a live `check` (G26 row 5, observed on `r5t13/probe-slow/0`, 2026-09-23) rendered *"No compose file is selected for this deployment … Pick a compose above to deploy"* next to an **enabled** `Deploy` — the message asserted the opposite of what the button did. Fix: added `&& !fixingGuide` to the alert's condition (`DeployForm.tsx`), with a comment recording why. The `Deploy`-enabled behaviour was NOT changed — it is the deliberate earlier decision (G25/G26). Verified live on the rebuilt dashboard `sha256:b02547a5…`: `check` on the paused session opened the panel with the compose slot at `generate new`, **no alert**, `Deploy` enabled; confirming generated `.fix` and deployed. |
| L24 | ~~A paused run's `-api` record task reaches a terminal state on resume~~ | 🔮 | **OBSOLETE under GRILLING G28** (2026-09-25). The machinery this row fixed no longer exists: a generation holds **no `-api` record task at all** — the `record` mirror (`_session_create` / `_session_append` / `_session_finish`, `session_task_id`) was removed, so there is nothing to "finish" and the leak class went with it. Kept, struck through, because its tests were retired with the premise (`TestAPausedGenerationSessionReachesATerminalState`) and a reader would otherwise wonder where they went. The original fix, for the record: `_run_job` closed its `-api` session task on every exit path **except** the two PAUSE returns, so the id that named the task became unrecoverable and five `generate-missing-files` records sat permanently `running`. |
| L26 | **A pause is a LEG; answering it starts a SECOND job** | ✅ | GRILLING **G27** (amends G21). A pause is a **TERMINAL** status carrying a payload in `result.pause` (`{kind, ask, request}`); `POST /llm/jobs/{id}/input` and the parked-state resume branch are **deleted**, and the answer posts to the ordinary create entry point with that payload under `resume` — the carried `request` IS the next leg's request, so the path continues from where the leg stopped and the card follows the **current** job (`pre_deploy_sessions.job_id` is rewritten by the next `add_session`). The carried request arrives with the faulty compose **unselected** (`selection.compose: []`) and `fix: true`, so the next leg regenerates it as `.fix` (G25). Client: `useGenerationJob` normalises `result.pause` into the flat shape the prompts already read (so a paused leg is answerable on every surface), follows the new job after a submit, and the deploy auto-run polls `jobIdRef.current` rather than the id it captured at entry — without that the deploy was never issued and no error appeared. Verified live by **T13** rows 1–7 and **T18** (job 316 `paused` unchanged → answering created job 317, which completed and deployed the `.fix` render; session re-pointed at 317). |
| L27 | **One record shape, two owners: `gt-` / `api-` ids, per-task logs, ONE Tasks list** | ✅ | GRILLING **G28**. The gateway's ids are `gt-<n>`, the -api's `api-<uuid12>` (`user_provision_tool/lib/task_manager.py` `new_task_id`); each owner keeps its own store (gateway SQLite, -api JSON + `task-<id>.log`), each writes a **per-task log** (`gt-<id>.log` under the gateway's `task_logs`), and the **`record` mirror is removed** — so a generation holds no -api task (L24's premise). The Tasks page is **ONE list** with a `Source` column and a `Type` column (reverting the two-table split), and `?job=` / `?task=` deep links open the right record's own log. Verified live by **T21** (one table, `gateway/gt-*` and `-api/api-*` rows with source tags) and by the deployment that followed T13 row 7. |
| L28 | **The gateway has its OWN generation pool and its own refusal** | ✅ | GRILLING **G29**. `settings.GATEWAY_MAX_GENERATIONS` (default 4) guards the LLM endpoint and cost, deliberately a **separate number** from the -api's `TASK_MAX_CONCURRENCY` (which guards the host against docker work). Capacity is counted **in the DB inside the insert transaction** (G31) so four workers cannot each admit a full pool. Two refusals, two stable codes, both on the **card header** with their own remedy: `at_capacity` ("cancel a hanging task") and `generation_at_capacity` ("wait for a running generation, or cancel one"); a gateway refusal also writes a `refused` session **with its own Retry**. That Retry needed two things the first implementation lacked (fixed 2026-09-25): the record must **carry the request** the refusal was made with (selection, proxy, `deploy_metadata`) or the button renders disabled, and — because a generation refusal means nothing was produced — the retry must **run the generation again** rather than replay paths that do not exist, which also required letting the operator's own retry through the session's reservation. Verified live on the rebuilt images: the card's Retry was a greyed-out dead control before, and after the fix Retry → seeded panel → generation job 333 completed → the deploy registered → the refused session was removed. A refusal record carries **everything the retry replays, the URL and the credential included** — user's ruling of 2026-09-26, in one place: `pre_deploy_registry.refusal_options`, which both refusal paths call. (A first pass blanked the URL and the password; the user corrected it: *"for the generate path which is part of a deploy path, it indeed sees the password, and for supporting retry, the password / url should indeed be stored!"* — a blanked field is not a safer record, it is a retry that deploys something else.) For that the **generation request** must carry them: `DeployForm` now sends `domain` + `passwd` in `deploy_metadata` on both generation payloads, since a refusal of that generation is where the record is written. Verified live: a fresh refusal recorded `domain: "localhost"`, `passwd: "verify-pass-926"` and its own selection, and its Retry opened with all three. Verified by **T19** — deploy A took the slot, deploy B got a real `503`, session `browserverify\|example-service\|recipes/slowbuild\|3` = `refused / generation_at_capacity`, and the page showed the panel alert, the card, and the header alert together. |
| L29 | **One cleanup CONTRACT, two engine-adapted implementations** | ✅ | GRILLING **G30**. Shared: only `completed \| failed \| cancelled` are eligible (the -api adds its own `unknown`), a **paused record is never pruned**, one TTL with one default (**604800 s**, the -api's `TASK_TTL_SECONDS`), a count cap (**1000**, the -api's `TASK_MAX_COUNT`), oldest-first eviction, the **log disposed with the record**, owner-side cleanup/recovery, and a paused record surviving recovery. Engine-adapted: the -api scans its dict, the gateway issues SQL. The **trigger rule** is part of the contract (D3's retraction): the TTL sweep runs on **reads**, the cap on **submit** — both engines. Asserted on both sides (see the round doc's bullet-by-bullet table); the gateway reads its two numbers via `prune_finished`/`prune_excess`/`_int_setting`, and `_int_setting` **honours an explicit 0** (a `0` is a real configuration: no records kept, or a maintenance-mode pool that refuses everything — the earlier `int(x or default)` silently discarded it). Verified live by **T22** (submit-time cap sweep: 123 → 122 finished, oldest evicted, both paused records untouched) and, on the rebuilt images, by a temporary `GATEWAY_MAX_GENERATIONS=0` + `GATEWAY_MAX_JOBS=0` run: every generation refused with "limit 0", and the submit swept **136 finished records to zero, leaving exactly the 5 paused ones**; the -api's own cap was proven the same way (`TASK_MAX_COUNT=0`: 491 records → 1, and the just-submitted live task survived with its log). |
| L30 | **Four workers, DB-carried locking** | ✅ | GRILLING **G31**. `pre-deploy-registry.json` moved into SQLite (`pre_deploy_sessions`); **WAL + `busy_timeout`** (`app/database.py`); capacity counted in-transaction; an **atomic claim** for queued jobs (`UPDATE … WHERE status='queued'` + rowcount); **cancellation on the ROW** (so a DELETE landing on another worker stops the run — `_check_cancelled` reads the row, with a 1 s per-job poll) and a sticky-cancel guard in `_set_status`; a **lease row** per periodic loop (`loop_leases`, one home for each of the four); progress writes coalesced (0.5 s). Verified live by **T20** (`DELETE /llm/jobs/334` → `cancelled`, and it stayed cancelled: the runner wrote `cancelled`, never `completed`), by `journal_mode: wal`, and by `loop_leases` holding exactly four rows. |
| L31 | **Generated artifacts carry a job-unique COLLISION TOKEN — and are never auto-deleted** | ✅ | GRILLING **G33**. The compose and nginx artifacts are RECIPE-LEVEL and user-agnostic, and the reservation key is `user\|service\|recipe_path\|label` — so two generations whose keys differ only by user or label used to write the SAME path: last writer wins, and one run validated a compose another had just replaced. The generated names now carry the **job id** as a token (`docker-compose.base.<job>.yml`, `nginx.base.<job>.conf`, and the `.fix` family likewise), resolved in ONE place (`_phase_names`) so the writer, the progress lines and the "did I produce this?" test cannot disagree. The token is **not a link to the job**: no ownership, no lifecycle, no cleanup keyed on the job's existence — the artifacts belong to the recipe and **outlive the job**, and nothing auto-deletes them (the gateway's only unlink on the job path is the job's own log). `env` is untouched (per-user `.env.raw.<user>.<label>`, already collision-free). Everything that identifies these files does so by PATTERN, not by exact name — the readiness scan, the pickers, the Generated Files column, the selection, the durable default, the -api's render, and (fixed with this round) the dashboard's per-file validation mapping and its two "which file did this run produce?" lookups, which read a **missing** validation as "valid" and would have gone silently green. Verified live by **T30** (two concurrent generations for one recipe, `alice\|17` and `bob\|0`: distinct names, both artifacts intact, none deleted, each job's selection naming its own file) and by **T13**'s guided leg (`docker-compose.fix.339.yml`). |
| L32 | **The pause lifecycle closes behind itself: a paused object is cancellable, and answering CONSUMES its leg** | ✅ | GRILLING **G27 + G34**. A pause ends the LEG but the job still exists — so (a) a `paused` object **is cancellable** (`CANCELLABLE_STATUSES`, one home, read by both the card's Delete and the row's `Cancel`): before, the DELETE no-opped and still answered 200 with `status: "paused"`, so a paused card could not end its object and the record could never become sweep-eligible; and (b) **answering a pause cancels the leg it answered** (the client names the parent, the server cancels it once the new leg exists) — otherwise every answered pause left a `paused` row nothing could reach and nothing could ever sweep (a paused record is never pruned, G30). A paused leg also stays out of the in-process discard set: it has no runner to discard. Verified live: the 5 orphaned paused objects cancelled from the row (`paused → cancelled`, progress `cancelled`), a card's `Delete` on a paused object the same, and `gt-340` → `cancelled` while its answer `gt-341` ran. |
| L33 | **ONE work surface per path: the card groups, the row's slot, the card's own link** | ✅ | The 2026-09-26 service-card round. A pre-deploy session is **not a category** — it renders inside its own user's group beside that user's instances (the "Pre-deploy sessions" section and its second `<Collapse>` are gone); the header's status note is **ONE line inline after the service name** with the remedy in its tooltip (refused headers were 150 px against 48 px for a plain card, and a failed card could stack two alerts — its two conditions fold into one line, since a deploy refused for `at_capacity` holds no task and so its log is gone by construction); the **Tasks page** polls silently (the spinner is first-load-only) and has **no `Answer` action** — a pause is answered where it was created, which for the session-less Generate path is the **Source Projects row's state-aware slot** (robot → running → `Check` + `Cancel`); the instance card gained the **`Task: <id>`** link, clickable while the record resolves and greyed after (from the -api's `task_available`, so the browser makes no per-card probe), a clickable **`nginx:`** row, and ONE recipe-scoped clickable `LLM fix:` line, hidden when the recipe has none. The `-api` → **`core`** rename is display only (ids stay `api-…`). Verified live by **T23–T29** on the frozen build — `_tasks/service-cards-round-20260926.md`. **Extended 2026-09-28** (`_tasks/service-cards-round-20260926.md` §11): the row acts **only on the jobs ITS OWN PATH created** — every job now records `origin` (`standalone \| deploy \| fix`, from `job_origin()`, the same helper the create path uses for the session it opens), because until then the row keyed on the newest job of any path, so a deploy's pause appeared on the row as well as on its card *and answering it there ran the generation while silently skipping the deploy*; and there is **no busy guard anywhere** — with G33's token two generations for one recipe cannot overwrite each other, so another path's work never greys or replaces this row's robot. |

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
| ACL4 | provision_token cookie (service access) | ✅ | HTTP-only cookie; v4 1-week TTL (604800s via `create_provision_token`) bound to the default key's `api_key_id`; cookie Max-Age=604800 (compose default `PROVISION_COOKIE_TTL` fixed in QA3); also accepted via `X-Provision-Token` header for API clients (G5) — **and the header now genuinely reaches the gate** (2026-09-29): the edge's `/_auth_jwt` forwards the CALLER's header (`$http_x_provision_token`, not the cookie's value) while relaying `Cookie` separately, so a cookie-less API client with a valid key is admitted and the instance's own Basic is injected upstream as before. Precedence is `cookie or header`, **cookie first** — a bad cookie is not rescued by a good header. Before the fix a VALID key behaved exactly like no credential on the service path (401) and the only route was the browser handshake; the -api's `TestAclHelpersTemplate` now guards the forwarded header (its previous version pinned the bug). Reading a 401: `realm="subnet-acl"` = the GATE refused it; the instance's own realm (`"<service> - <user>"`) = the gate PASSED and the instance's `auth_basic` refused the injected credential. |
| ACL11 | Login token model | ✅ | v4 three credentials: `provision_token` + 30s exchange code; Bearer `access_token`/`refresh_token` dropped; `POST /api/auth/logout` added (clears cookie) |
| ACL5 | HostnameIndex | ✅ | In-memory hostname-to-registry-entry lookup; maps service URLs to registry entries |
| ACL6 | Registry (user_registry.yml) | ✅ | Read-only registry wrapper; reads provision-api registry via shared filesystem |
| ACL7 | /go/{hostname} service redirect | ✅ | v4/v5 (F13): issues 30s HMAC-signed exchange code + `Location` header; the EDGE `/_set_token` relays `proxy_pass http://$gw/api/auth/exchange$is_args$args;` (G1 — `?code=&redirect=` query string preserved, no more 401 "Missing exchange code"); `/api/auth/exchange` swaps code→provision_token via 302+Set-Cookie (`; Secure` per registry flag); no JWT in URL (F13) |
| ACL8 | Viewer ACL: own services only | ✅ | Viewers access own services + allowed_special_users; v4 N1 trims each allowed_special_users element |
| ACL9 | Admin unrestricted access | ✅ | Admins have unrestricted access to all services |
| ACL10 | X-Service-Basic credential injection | ✅ | Base64 `user_name:passwd_plain` header for nginx auth_basic on the target service, rebuilt from the registry per request; v4 N2 — no "123456" fallback. **Two states produce an EMPTY credential and must not be confused** (2026-09-29): a deliberately passwd-less service (`htpasswd_path` unset ⇒ the render strips `auth_basic` ⇒ the instance accepts anything), and a *protected* service whose `passwd_plain` is missing/stale (the instance's `auth_basic` then refuses every request — 401 with the INSTANCE's realm while the gate reports success). `POST`/`PUT` password re-record the plaintext; `DELETE .../password` is the exit for the second state |
| ACL12 | Byte-identical SIMPLE per-service conf (v5) | ✅ | v5 (F8): `render_nginx_conf` emits the SIMPLE ACL-free form (`server_name` + `auth_basic` + variable-based `proxy_pass`), byte-identical across ENABLE_ACL — no `ENABLE_ACL` branch, no v4 scaffold (`TestV5SimpleNginxSyntax`: no auth_request/WWW-Authenticate/`@auth_401`/`@auth_403`/env.d/`$client_type`; byte-identical render tests). `strip_v4_scaffold` removes stale v4 tokens from deployed confs (33 swept live, 0 scaffold remain) |
| ACL13 | ACL-off pass-through → native Basic (v5) | ✅ | v5 (F6/B5): the `/__basic__/` short-circuit and env.d are REMOVED — internal per-service confs are simple ACL-free (`auth_basic` + `auth_basic_user_file`). In ACL-off mode the edge passes traffic straight through to the internal native Basic: non-certed service over http → `401 WWW-Authenticate: Basic realm="example-service - alice"` (native dialog restored) |
| ACL14 | Minimal deployment (services-only nginx.provision.conf) | ✅ | v5 (F9): internal `nginx.provision.conf` is services-only — `include /etc/nginx/services.d/*.conf;` + `listen 80 default_server; return 444;` catch-all; no portal.d/env.d includes (`TestV5PortalDDeprecated.test_internal_nginx_provision_conf_services_only`). Internal nginx host ports live only in the base compose for the minimal deployment; fullset suppresses them via the separated gateway compose + edge entry (F11) |
| ACL15 | Portal routing on the edge (v5) | ✅ | v5 (F4): portal routing moved to the EDGE `-nginx-acl` portal server (`server_name ${PORTAL_HOSTNAME} 127.0.0.1 localhost`): portal host → gateway/dashboard, `location = /api/auth/verify|/api/auth/exchange { return 404; }` (GAP-31 internal-only), `/api/` → gateway (`proxy_read_timeout 3600s`, U3), `/go/` rewrite, `/login` → gateway, `/alert` + `/` → dashboard. Internal nginx no longer renders portal.d vhosts (F9). Live: portal host → 200 dashboard, verify/exchange → 404 |
| ACL16 | API-first 401/403 (edge) | ✅ | v5 (F3, GAP-16 fixed): the 401/403 challenge moved to the EDGE `@auth_401/@auth_403` — `add_header WWW-Authenticate 'Basic realm="subnet-acl"' always;` at `acl-helpers.conf.template:49,56` (`always` is required — nginx drops add_header on 401 otherwise). Live: API client → `401` WITH the challenge; browser → `302` portal /login; unknown client → `401` + header (fail-closed). Regression `test_auth_401_challenge`. No `?redirect=` param (GAP-14) |

---

## AK. API Key Management

| # | Feature | Status | Notes |
|---|---|---|---|
| AK1 | Create API key | ✅ | `POST /api/auth/keys`; the target is a **PREFIXED reference** (`end_user:<id>` / `admin:<id>`; a bare id means end_user; omitted = the caller's own target, in the caller's own id space) — **2026-09-29**: before the prefix a bare id was resolved as an end user first, so an admin's blank "my own key" mint produced a key belonging to whichever end user held the same id (live: `admins.id 1` = `end_users.id 1` = alice, key 91 carried `sub 1 / alice / end_user`), and an admin could not obtain a key of their own at all. Admin may target any reference; viewer only self. Proven live through the UI on the rebuilt build: blank → `admin:1` (key 97), `admin:1` → key 98, `end_user:1` → alice (key 99), `service:1` → 400 `Unknown target type 'service'` |
| AK2 | List API keys | ✅ | `GET /api/auth/keys`; admin sees all, viewer sees own |
| AK3 | Revoke/delete API key | ✅ | `DELETE /api/auth/keys/{id}`; admin revokes any, viewer own |
| AK4 | API key lifetime (provision token) | ✅ | API keys are **1-year** provision tokens carrying their own `api_key_id` (`auth_service.create_api_key_token`); the login/exchange **cookie** is 1-week default-bound (`PROVISION_COOKIE_TTL`); the `/go/` exchange **code** is 30s (`EXCHANGE_CODE_TTL_SEC`) |
| AK5 | ApiKeysPage frontend | ✅ | Admin page at /api-keys for managing end-user API keys; iter-1 (G6) adds a **Mask** column and a **Default** column (gold star tag) plus a **Set-as-Default** action (`PUT /api/auth/keys/{id}/default`) — browser-verified live (click moves the default, tag/button update, DB default moves) |
| AK6 | Default key (is_default) | ✅ | `ApiKey.is_default` + partial unique index `uq_api_keys_one_default` on **(user_id, target_type)** WHERE is_default=1 (G2; widened 2026-09-29 — the id alone is not unique across the two id spaces, so an id-only index made the admin's default collide with alice's; `_ensure_schema` replaces an id-only index in place, and every filter now carries both halves) with `_ensure_schema` drift repair and `delete_end_user` cascading api_keys (G2, scoped to `end_user`); `PUT /keys/{id}/default` (scoped promotion); `DELETE /keys` rejects revoking the default (400); default created at registration and for admins — login/exchange bind admin tokens to the default key's `api_key_id`, each in its own id space (G4); `POST /api/auth/keys` promotes the new key to default when the target has none (G8, v4 §6.1.6); 1000-key cap / lazy eviction; live DB migrated via `_ensure_schema` (QA1) and drift scan clean (every default-bearing target has exactly 1 default — live: `end_user:1` key 33 and `admin:1` key 100 coexist) |
| AK7 | API key = JWT + mask + real user_type | ✅ | Keys minted as 1-year JWT provision tokens with own `api_key_id` + `mask`; key-targeted tokens carry the target's real `user_type` (GAP-07), and the row stores the matching `target_type` (2026-09-29 — the prefix's storage half, so listing/revoking/promoting can name the id space); `_ensure_schema` backfills `mask` from `token_hash` for NULL/empty-mask rows (G7) — live 0 of 29 keys have NULL mask |

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
