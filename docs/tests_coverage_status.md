# Provision Gateway — Tests Coverage Status

> **Version**: 1.32
> **Date**: 2026-08-28 (refresh — full pytest suite **293 passed / 0 failed**, verified by live run on 2026-08-28 (cycle 20260828T190332Z, QA iter-3 r1: pytest 293/293, 11.63s). 32 new tests added iter-1 by the scan-rearchitecture cycle — TestProjectStateModule 7, TestScanRearchitecture 16 (incl. registry TTL cache), TestScanRearchitectureHandlers 8, concurrency scan-in-flight 1 — and 4 old-behavior tests rewritten (marker-only classification, root-only default, direct save_generated_files call, `not iscoroutinefunction`). The shell suites (`test_integration.sh`, `test_deploy.sh`, `test_proxy.sh`, `test_gateway_api.sh`, `test_provision_api.sh`) require the live Docker stack and are run in-container; QA iter-3 r1: shell 124/0 (test_integration 10, test_gateway_api 53, test_deploy 10, test_proxy 24, test_provision_api 27).)
> **Status**: Current state of test coverage

---

## Table of Contents

1. [Test Inventory](#1-test-inventory)
2. [Coverage by Module](#2-coverage-by-module)
3. [Coverage by API Endpoint](#3-coverage-by-api-endpoint)
4. [Coverage by Feature](#4-coverage-by-feature)
5. [Gaps & Recommendations](#5-gaps--recommendations)

---

## 1. Test Inventory

### 1.1 Test Files Summary

| File | Language | Type | Test Cases | Status |
|---|---|---|---|---|
| `test_unit.py` | Python (pytest) | Unit | 258 | ✅ All passing (prior 87 + coder's GAP-1/2/4 tests — reworked TestUploadModeJSONFormat, reworked TestTemplateMode, new TestLLMConfigDefersLocalAgent, new TestTemplateClassificationMarkerOnly (renamed from TestTemplateClassificationGitTracked, scan-rearchitecture F35) — plus QA's new TestLLMConfigDefersLocalAgent::test_model_column_default_is_byok, and subnet-acl + multi-recipe coverage: TestAuthVerifyHeaders, TestVerifyAuthStatusCodes, TestGoServiceRedirect, TestApiKeyModel, TestSubnetPoolSystemEndpoint, TestRecipePathMultiRecipe, TestRouteRoleGating; **2026-08-28**: middleware robustness regression — `TestNewMiddleware` now asserts auth deps are synchronous `def` (not coroutines), a guard for the DB-pool/event-loop deadlock fix; **2026-08-28 (scan-rearchitecture cycle 20260828T190332Z)**: +31 new tests — TestProjectStateModule 7, TestScanRearchitecture 16 (incl. registry TTL cache), TestScanRearchitectureHandlers 8 — and 4 old-behavior tests rewritten (marker-only classification, root-only default, direct save_generated_files call, `not iscoroutinefunction`) |
| `test_concurrency.py` | Python (pytest) | Unit (concurrency) | 4 | ✅ Passing (NEW iter-1, GAP-3 — 20 concurrent in-process requests via httpx ASGITransport + Dockerfile `--workers` check; runs in the default pytest suite, no live gateway needed; +1 scan-rearchitecture cycle — `test_health_responds_while_service_scan_in_flight` (F37)) |
| `test_proxy.py` | Python (pytest) | Unit | 12 | ✅ Passing |
| `test_integration.py` | Python (subprocess) | Integration | 9 | 🟡 9 skipped in the default pytest run — host-port probes (conftest curls `localhost:8770/health` from the host; port 8770 is internal-only by compose design). Equivalent live coverage via `test_integration.sh` run in-container → 9/0 passed (iter-6, re-confirmed iter-7 + iter-8 + iter-9 + iter-10 + iter-11 + iter-12 + iter-13 + iter-14 + iter-15 + iter-16 + iter-17 + iter-18 + iter-19 + iter-20 + iter-21) |
| `test_integration.sh` | Bash | Integration | 113 lines | ✅ Passing |
| `test_deploy.sh` | Bash | Integration | 199 lines | 🟡 1 pre-existing failure (Test 3 → 400 "Global proxy is not enabled." — the script's `PUT /system/proxy {"enabled":true}` creates a config but does NOT activate it; the API requires `PUT /proxy/{id}/activate`, which only activates reachable proxies; test host 172.18.0.1:7897 unreachable). Confirmed NOT a code regression (iter-6 + iter-7 + iter-8 + iter-9 + iter-10 + iter-11 + iter-12 + iter-13 + iter-14 + iter-15 + iter-16 + iter-17 + iter-18 + iter-19 + iter-20 + iter-21 + iter-22 + iter-23 + iter-24, git-verified; 25th consecutive occurrence — 20th identical shell run) |
| `test_proxy.sh` | Bash | Integration | 189 lines | ✅ Passing |
| `test_gateway_api.sh` | Bash | Integration | 347 lines | ✅ Passing |
| `test_provision_api.sh` | Bash | Integration | 252 lines | ✅ Passing |
| `test_load.py` | Python (httpx) | Load/Perf | 5 scenarios | 🧟 STALE — reads `access_token` + `Authorization: Bearer`, which the cookie-only v5 gateway rejects; not runnable against the current gateway. Not part of the passing suite |
| **Total** | | | **Full pytest suite: 293 passed / 0 failed** (verified 2026-08-28, cycle 20260828T190332Z — QA iter-3 r1: pytest 293/293, 11.63s; per-file cells above are documented snapshots — this cycle's scan-rearchitecture additions alone are +32: test_unit +31 (TestProjectStateModule 7, TestScanRearchitecture 16, TestScanRearchitectureHandlers 8), test_concurrency +1 (scan-in-flight); prior refresh figures predate those additions). Shell integration suites (test_integration.sh, test_deploy.sh, test_proxy.sh, test_gateway_api.sh, test_provision_api.sh) run in-container against the live stack; QA iter-3 r1: 124/0. | |

### 1.2 Test Execution

```bash
# Unit tests (Python)
cd provision-gateway
pip install pytest
python -m pytest tests/test_unit.py -v
python -m pytest tests/test_proxy.py -v

# Integration tests (Python)
python tests/test_integration.py

# Integration tests (Shell)
bash tests/test_integration.sh
bash tests/test_deploy.sh
bash tests/test_proxy.sh
bash tests/test_gateway_api.sh
bash tests/test_provision_api.sh
```

---

## 2. Coverage by Module

### 2.1 Backend Services

| Service Module | Unit Tests | Integration Tests | Coverage |
|---|---|---|---|
| `auth_service.py` | 4 (hash, verify, JWT, end-user auth) + gateway/provision token + API-key helpers (TestGatewayTokenDecode, TestApiKeyModel) | 2 (login, end-user login) | 🟢 Good |
| `proxy_service.py` | 3 (env injection, disabled proxy) | 12 (full CRUD, deploy integration) | 🟢 Good |
| `provision_service.py` | 14 (method existence checks) | 3 (list users, get user, error handling) | 🟢 Good |
| `service_manager.py` | 12 (create_from_template, scan_for_new_projects, get_new_project_events, project tracking, recipe-path multi-recipe — TestRecipePathMultiRecipe) | 1 (list services) | 🟡 Partial (TestProjectMonitoring, TestTemplateMode, TestRecipePathMultiRecipe in test_unit.py) |
| `llm_service.py` | 0 | 0 | 🔴 None |
| `curl_service.py` | 0 | 0 | 🔴 None |
| `audit_service.py` | 0 | 2 (list audit, filter by action) | 🟡 Partial |
| `crypto.py` | 4 (encrypt/decrypt, empty, invalid, uniqueness) | 0 | 🟢 Good |

> **Removed modules** (no longer exist in gateway — delegated to provision-api):
> - `docker_service.py`, `reconciliation.py`, `compose_converter.py`, `nginx_converter.py`, `nginx_parser.py`
> - Architecture validation tests (`test_unit.py`) verify these files are deleted and the gateway does not duplicate provision-api logic.

### 2.2 Backend Routers

| Router | Unit Tests | Integration Tests | Coverage |
|---|---|---|---|
| `auth.py` | 36 (verify headers/status codes, gateway-token decode, go redirect, API-key model) | 4 (setup, login, me, end-user login) | 🟡 Partial |
| `system.py` | 1 (subnet-pool route registered) | 4 (status, proxy CRUD, SSL certs) | 🟡 Partial |
| `services.py` | 7 (TestRecipePathMultiRecipe — check-missing-files recipe_path forwarding, save-generated recipe subdir) | 1 (list) | 🟡 Partial |
| `users.py` | 0 | 7 (deploy, up/down, password, container logs, error cases) | 🟡 Partial |
| `tasks.py` | 0 | 5 (list, SSE log streaming, cancel, invalid task handling) | 🟡 Partial |
| `llm.py` | 0 | 0 | 🔴 None |
| `audit.py` | 0 | 2 (list, filter) | 🟡 Partial |

### 2.3 Frontend (provision-dashboard)

| Component | Unit Tests | Browser Tests | Coverage |
|---|---|---|---|
| All pages | 0 | 0 | 🔴 None |
| All components | 0 | 0 | 🔴 None |
| API client (`client.ts`) | 0 | 0 (implicit via integration) | 🔴 None |
| Hooks (`usePolling`, `useSSE`) | 0 | 0 | 🔴 None |
| Auth store (`authStore.tsx`) | 0 | 0 (implicit via integration) | 🔴 None |

### 2.4 MCP Server (provision-mcp)

> **⚠ Non-functional against the v5 gateway** — `verify_admin_token` requires `type=='access'` + Bearer
> (both removed in v5); needs redesign. Zero test coverage.

| Component | Unit Tests | Integration Tests | Coverage |
|---|---|---|---|
| `server.py` | 0 | 0 | 🔴 None (non-functional) |

---

## 3. Coverage by API Endpoint

| Endpoint | Method | Tested? | Test File |
|---|---|---|---|
| `/health` | GET | ✅ | integration.py, integration.sh |
| `/api/auth/setup` | POST | ✅ | integration.py, integration.sh |
| `/api/auth/register` | POST | ❌ | — |
| `/api/auth/login` | POST | ✅ | integration.py, integration.sh |
| `/api/auth/refresh` | POST | — (removed in v4) | v4 dropped the three-credential token model — endpoint no longer exists; covered by `TestV4AuthEndpoints` |
| `/api/auth/me` | GET | ✅ | integration.sh |
| `/api/auth/verify` | GET | ✅ | test_unit.py (TestAuthVerifyHeaders, TestVerifyAuthStatusCodes) |
| `/api/auth/password` | PUT | ❌ | — |
| `/api/auth/keys` | POST | ✅ | test_unit.py (TestApiKeyModel, TestGatewayTokenDecode) |
| `/api/auth/keys` | GET | ✅ | test_unit.py (TestGatewayTokenDecode) |
| `/api/auth/keys/{id}` | DELETE | ✅ | test_unit.py (TestGatewayTokenDecode) |
| `/api/auth/keys/{id}/default` | PUT | ❌ | — |
| `/api/auth/exchange` | GET | ❌ | — (internal — reached via the edge `/_set_token`) |
| `/api/auth/users` | GET | ❌ | — |
| `/api/auth/users/register` | POST | ❌ | — |
| `/api/auth/users/{id}/approve` | PUT | ❌ | — |
| `/api/auth/users/{id}` | PUT/DELETE | ❌ | — |
| `/api/auth/users/deployable` | GET | ❌ | — |
| `/go/{hostname}` | GET | ✅ | test_unit.py (TestGoServiceRedirect) |
| `/api/system/status` | GET | ✅ | integration.py, integration.sh |
| `/api/system/stats` | GET | ❌ | — |
| `/api/system/reconcile` | POST | ❌ | — |
| `/api/system/reconcile/status` | GET | ❌ | — |
| `/api/system/nginx-state` | GET | ❌ | — |
| `/api/system/proxy` | GET/POST | ✅ | proxy.sh (12 tests) |
| `/api/system/proxy/{id}` | PUT/DELETE | ✅ | proxy.sh |
| `/api/system/proxy/{id}/activate` | PUT | ✅ | proxy.sh |
| `/api/system/proxy/test` | POST | ✅ | proxy.sh |
| `/api/system/proxy/deactivate` | POST | ❌ | — |
| `/api/system/config` | GET/PUT | ❌ | — |
| `/api/system/subnet-pool` | GET | ✅ | test_unit.py (TestSubnetPoolSystemEndpoint) |
| `/api/services` | GET/POST | ✅ | deploy.sh (list only) |
| `/api/services/{name}` | GET/DELETE | ❌ | — |
| `/api/services/{name}/files/{file}` | GET/PUT | ❌ | — |
| `/api/services/{name}/convert` | POST | ❌ | — |
| `/api/services/{name}/check-missing-files` | GET | ✅ | test_unit.py (TestRecipePathMultiRecipe — recipe_path forwarding) |
| `/api/services/templates` | GET | 🟡 | presence-only — `TestTemplateMode` checks the route/method exist, not behavior; endpoint is DEPRECATED (no data source) |
| `/api/services/notifications` | GET | ✅ | test_unit.py (TestProjectMonitoring) |
| `/api/services/scan` | POST | ❌ | — |
| `/api/services/save-generated` | POST | ✅ | test_unit.py (TestRecipePathMultiRecipe — creates recipe subdir when recipe_path given) |
| `/api/services/check-deploy` | POST | ❌ | — |
| `/api/services/{name}/git/status` | GET | ❌ | — |
| `/api/services/{name}/git/diff` | GET | ❌ | — |
| `/api/services/{name}/git/head-file` | GET | ❌ | — |
| `/api/users` | GET | ✅ | integration.py, deploy.sh |
| `/api/users/{name}` | GET | ❌ | — |
| `/api/users/deploy` | POST | ✅ | deploy.sh (5 variations), test_unit.py (TestDeployValidation) |
| `/api/users/{u}/{s}/next-label` | GET | ✅ | test_unit.py (TestServiceLabelAutoIncrement) |
| `/api/users/{u}/{s}/{l}` | DELETE | ❌ | — |
| `/api/users/{u}/{s}/{l}/rebuild` | POST | ❌ | — |
| `/api/users/{u}/{s}/{l}/up` | POST | ❌ | — |
| `/api/users/{u}/{s}/{l}/down` | POST | ❌ | — |
| `/api/users/{u}/{s}/{l}/password` | PUT | ❌ | — |
| `/api/users/{u}/{s}/{l}/url` | GET | ❌ | — |
| `/api/users/{u}/{s}/{l}/test-curl` | POST | ❌ | — |
| `/api/users/clone` | POST | ❌ | — |
| `/api/tasks` | GET | ✅ | integration.py |
| `/api/tasks/{id}` | GET/DELETE | ❌ | — |
| `/api/tasks/{id}/log` | GET (SSE) | ❌ | — |
| `/api/llm/configs` | GET/POST | ❌ | — |
| `/api/llm/configs/{id}` | PUT/DELETE | ❌ | — |
| `/api/llm/config` | GET/PUT | ❌ | — |
| `/api/llm/test` | POST | ❌ | — |
| `/api/llm/generate` | POST | ❌ | — |
| `/api/audit` | GET | ✅ | integration.py, deploy.sh, proxy.sh |

**Summary:** 25 of 65 endpoints tested (38.5%)

---

## 4. Coverage by Feature

| Feature Category | Test Coverage | Status |
|---|---|---|
| **Authentication** | Login, setup, token refresh, me, auth verify (`/api/auth/verify`), service-access redirect (`/go/{hostname}`), API-key CRUD (`/api/auth/keys*`) | 🟡 Partial (missing: register, password change, user management, deployable users) |
| **System Monitoring** | Status endpoint, subnet-pool (`/api/system/subnet-pool`) | 🟡 Partial (missing: stats, config) |
| **Proxy Management** | Full CRUD, enable/disable, credentials, reachability test, deploy integration, audit | 🟢 Good |
| **Service Projects** | List, check-missing-files + save-generated with `recipe_path` (multi-recipe) | 🟡 Partial (missing: CRUD, files, git, convert, scan, check-deploy) |
| **User Deployment** | Deploy with variations, error cases, proxy integration | 🟡 Partial (missing: delete, rebuild, up/down, password, url, test-curl, clone) |
| **Tasks** | List only | 🔴 Minimal (missing: detail, cancel, log streaming) |
| **LLM** | None | 🔴 None |
| **Audit** | List with filters, action-specific checks | 🟢 Good |
| **Reconciliation** | None | 🔴 None |
| **Frontend** | None | 🔴 None |
| **MCP Server** | None | 🔴 None (non-functional vs v5) |

---

## 5. Gaps & Recommendations

### 5.1 Critical Gaps (No Tests)

| Gap | Impact | Recommendation |
|---|---|---|
| Frontend (entire) | User-facing UI has zero automated tests | Add React Testing Library + Playwright tests for critical flows (login, deploy, service management) |
| MCP Server | External AI agent integration has no tests | **Non-functional vs v5** (cannot authenticate) — needs redesign before tests are meaningful; then add pytest tests for SSE, sessions, JWT |
| LLM Service | Config generation is untested | Add unit tests with mocked HTTP responses; test prompt building and code extraction |
| Reconciliation | Network recovery logic untested | Add unit tests with mocked docker CLI output; test parsing of nginx conf files |
| Service Manager | File operations, git clone, template conversion untested | Add unit tests with temp directories; mock git subprocess calls |
| Docker Service | Container management untested | Add unit tests with mocked subprocess output |
| Curl Service | URL testing untested | Add unit tests with mocked subprocess |

### 5.2 Partial Coverage Gaps

| Gap | Missing Tests |
|---|---|
| Auth | Register, password change, user management (CRUD, approve, special users) |
| Users | Delete, rebuild, up/down, password, URL, test-curl, clone |
| Services | CRUD, file operations, git operations, convert |
| Tasks | Detail, cancel, SSE log streaming |
| System | Stats, config, reconcile |

### 5.3 Known Test Issues

| Issue | File | Root Cause | Priority |
|---|---|---|---|
| `test_check_deploy_uses_service_name` fails | `test_unit.py` | Test references removed `checkDeploy` export (G14/G16 dead code cleanup). Should be removed or updated. | HIGH |
| `test_deploy.sh` Test 3 fails | `test_deploy.sh` | Script pre-step `PUT /system/proxy {"enabled":true}` creates/updates a config but does NOT activate it → deploy with `use_global_proxy:true` returns 400 "Global proxy is not enabled." (confirmed iter-6 first shell run; re-confirmed iter-7 second run + iter-8 third run + iter-9 fourth run + iter-10 fifth run + iter-11 sixth run + iter-12 seventh run + iter-13 eighth run + iter-14 ninth run + iter-15 tenth run + iter-16 eleventh run + iter-17 twelfth run + iter-18 thirteenth run + iter-19 fourteenth run + iter-20 fifteenth run + iter-21 sixteenth run + iter-22 seventeenth run + iter-23 eighteenth run + iter-24 nineteenth run + iter-25 twentieth run — 25th consecutive occurrence). Correct flow: create a config → `PUT /proxy/{id}/activate` (which only activates proxies with `reachable=="true"`; the script's test host 172.18.0.1:7897 is unreachable). Not a code regression (git-verified: test_deploy.sh unchanged since 698e208). Fix = call the activate endpoint after creating a reachable config, or assert the documented 400. | HIGH |
| ~~`/api/services/templates` returns 404~~ | `services.py` | **RESOLVED in Iteration 2** — `/templates` route moved before `/{name}` catch-all. 6 new route ordering tests verify. | ~~CRITICAL~~ ✅ |
| ~~`/api/services/notifications` returns 404~~ | `services.py` | **RESOLVED in Iteration 2** — `/notifications` route moved before `/{name}` catch-all. | ~~CRITICAL~~ ✅ |
| ~~Missing `Select` import in `ServicesPage.tsx`~~ | `ServicesPage.tsx:8` | **RESOLVED in Iteration 2** — `Select` added to antd import. TypeScript error TS2552 fixed. | ~~MEDIUM~~ ✅ |

### 5.4 Test Quality Recommendations

1. **Fix pre-existing test failures** (see 5.3 above) — these block clean test pipeline runs.
2. **Add pytest fixtures** for common setup (DB session, admin auth token, mock HTTP responses)
2. **Add conftest.py** with shared fixtures (currently minimal)
3. **Separate unit from integration** — use pytest markers (`@pytest.mark.unit`, `@pytest.mark.integration`)
4. **Add test coverage reporting** — `pytest --cov=app --cov-report=html`
5. **Add CI pipeline** — GitHub Actions or similar to run tests on PR
6. **Add frontend tests** — Vitest + React Testing Library for components, Playwright for E2E
7. **Add API contract tests** — Schema validation for request/response payloads
8. **Add performance tests** — Response time assertions for critical endpoints

### 5.5 Recommended Test Priority

| Priority | Area | Reason |
|---|---|---|
| P0 | Frontend E2E (Playwright) | User-facing; regressions directly visible |
| P1 | LLM Service (unit) | Complex logic; prompt quality critical |
| P1 | Reconciliation (unit) | Recovery logic; bugs cause downtime |
| P2 | Service Manager (unit) | File operations; data loss risk |
| P2 | Docker Service (unit) | Container management; production impact |
| P3 | Remaining API endpoints | Completeness |
| P3 | MCP Server | New feature; external interface |
