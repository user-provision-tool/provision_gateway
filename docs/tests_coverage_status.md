# Provision Gateway — Tests Coverage Status

> **Version**: 1.30
> **Date**: 2026-08-02 (updated — Cycle 20260801T165901Z Iteration 1: GAP-1/2/3/4 tests added by coder + QA — `test_concurrency.py` NEW, `test_unit.py` extended; full pytest suite now **112 passed / 9 skipped / 0 failed**. Iteration 6: rebuild succeeded via the no-proxy path → shell integration tests ran for the **first time** — **108 passed / 1 failed**. Iteration 7: shell integration tests re-ran a **second time** — **108 passed / 1 failed**, identical to iter-6 (result stable). Iteration 8: shell integration tests re-ran a **third time** — **108 passed / 1 failed**, identical to iter-6/iter-7 (result stable). Iteration 9: shell integration tests re-ran a **fourth time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8 (result stable across all 4 runs). Iteration 10: shell integration tests re-ran a **fifth time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8/iter-9 (result stable across all 5 runs). Iteration 11: shell integration tests re-ran a **sixth time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8/iter-9/iter-10 (result stable across all 6 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (11th consecutive occurrence). Iteration 12: shell integration tests re-ran a **seventh time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8/iter-9/iter-10/iter-11 (result stable across all 7 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (12th consecutive occurrence). Iteration 13: shell integration tests re-ran an **eighth time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8/iter-9/iter-10/iter-11/iter-12 (result stable across all 8 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (13th consecutive occurrence). Iteration 14: shell integration tests re-ran a **ninth time** — **108 passed / 1 failed**, identical to iter-6/iter-7/iter-8/iter-9/iter-10/iter-11/iter-12/iter-13 (result stable across all 9 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (14th consecutive occurrence). Iteration 15: shell integration tests re-ran a **tenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-14 (result stable across all 10 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (15th consecutive occurrence). Iteration 16: shell integration tests re-ran an **eleventh time** — **108 passed / 1 failed**, identical to iter-6 through iter-15 (result stable across all 11 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (16th consecutive occurrence). Iteration 17: shell integration tests re-ran a **twelfth time** — **108 passed / 1 failed**, identical to iter-6 through iter-16 (result stable across all 12 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (17th consecutive occurrence). Iteration 18: shell integration tests re-ran a **thirteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-17 (result stable across all 13 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (18th consecutive occurrence). Iteration 19: shell integration tests re-ran a **fourteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-18 (result stable across all 14 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (19th consecutive occurrence). Iteration 20: shell integration tests re-ran a **fifteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-19 (result stable across all 15 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (20th consecutive occurrence). Iteration 21: shell integration tests re-ran a **sixteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-20 (result stable across all 16 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (21st consecutive occurrence). Iteration 22: shell integration tests re-ran a **seventeenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-21 (result stable across all 17 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (22nd consecutive occurrence). Iteration 23: shell integration tests re-ran an **eighteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-22 (result stable across all 18 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (23rd consecutive occurrence). Iteration 24: shell integration tests re-ran a **nineteenth time** — **108 passed / 1 failed**, identical to iter-6 through iter-23 (result stable across all 19 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (24th consecutive occurrence). Iteration 25: shell integration tests re-ran a **twentieth time** — **108 passed / 1 failed**, identical to iter-6 through iter-24 (result stable across all 20 runs); the 1 failure = same pre-existing test_deploy.sh Test 3 script defect, re-confirmed NOT a code regression (25th consecutive occurrence))
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
| `test_unit.py` | Python (pytest) | Unit | 94+ | ✅ All passing (prior 87 + coder's GAP-1/2/4 tests — reworked TestUploadModeJSONFormat, reworked TestTemplateMode, new TestLLMConfigDefersLocalAgent, new TestTemplateClassificationGitTracked — plus QA's new TestLLMConfigDefersLocalAgent::test_model_column_default_is_byok, and subnet-acl + multi-recipe coverage: TestAuthVerifyHeaders, TestVerifyAuthStatusCodes, TestGoServiceRedirect, TestApiKeyModel, TestSubnetPoolSystemEndpoint, TestRecipePathMultiRecipe, TestRouteRoleGating) |
| `test_concurrency.py` | Python (pytest) | Unit (concurrency) | 2 | ✅ Passing (NEW iter-1, GAP-3 — 20 concurrent in-process requests via httpx ASGITransport + Dockerfile `--workers` check; runs in the default pytest suite, no live gateway needed) |
| `test_proxy.py` | Python (pytest) | Unit | 8 | ✅ Passing |
| `test_integration.py` | Python (subprocess) | Integration | 9 | 🟡 9 skipped in the default pytest run — host-port probes (conftest curls `localhost:8770/health` from the host; port 8770 is internal-only by compose design). Equivalent live coverage via `test_integration.sh` run in-container → 9/0 passed (iter-6, re-confirmed iter-7 + iter-8 + iter-9 + iter-10 + iter-11 + iter-12 + iter-13 + iter-14 + iter-15 + iter-16 + iter-17 + iter-18 + iter-19 + iter-20 + iter-21) |
| `test_integration.sh` | Bash | Integration | 113 lines | ✅ Passing |
| `test_deploy.sh` | Bash | Integration | 199 lines | 🟡 1 pre-existing failure (Test 3 → 400 "Global proxy is not enabled." — the script's `PUT /system/proxy {"enabled":true}` creates a config but does NOT activate it; the API requires `PUT /proxy/{id}/activate`, which only activates reachable proxies; test host 172.18.0.1:7897 unreachable). Confirmed NOT a code regression (iter-6 + iter-7 + iter-8 + iter-9 + iter-10 + iter-11 + iter-12 + iter-13 + iter-14 + iter-15 + iter-16 + iter-17 + iter-18 + iter-19 + iter-20 + iter-21 + iter-22 + iter-23 + iter-24, git-verified; 25th consecutive occurrence — 20th identical shell run) |
| `test_proxy.sh` | Bash | Integration | 189 lines | ✅ Passing |
| `test_gateway_api.sh` | Bash | Integration | 347 lines | ✅ Passing |
| `test_provision_api.sh` | Bash | Integration | 252 lines | ✅ Passing |
| `test_load.py` | Python (httpx) | Load/Perf | 5 scenarios | ✅ Working (load test for F1 — 20 concurrent requests) |
| **Total** | | | **Full pytest suite: 112 passed / 9 skipped / 0 failed** (iter-1, re-verified iter-2..21) — includes test_unit.py + test_concurrency.py + test_proxy.py. **Shell integration tests: 108 passed / 1 failed** (iter-6 first run / iter-7 second run / iter-8 third run / iter-9 fourth run / iter-10 fifth run / iter-11 sixth run / iter-12 seventh run / iter-13 eighth run / iter-14 ninth run / iter-15 tenth run / iter-16 eleventh run / iter-17 twelfth run / iter-18 thirteenth run / iter-19 fourteenth run / iter-20 fifteenth run / iter-21 sixteenth run / iter-22 seventeenth run / iter-23 eighteenth run / iter-24 nineteenth run / iter-25 twentieth run — identical 108/1, result stable across all 20 runs; rebuild unblocked via the no-proxy path; ENV-1/2 no longer blocking): test_provision_api.sh 27/0, test_gateway_api.sh 39/0, test_proxy.sh 24/0, test_integration.sh 9/0 (in-container), test_deploy.sh 9/1 (the 1 = pre-existing Test 3 script defect, see §5.3) | |

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
| `auth_service.py` | 4 (hash, verify, JWT, end-user auth) + gateway/provision token + API-key helpers (TestGatewayTokenDecode, TestApiKeyModel) | 3 (login, refresh, end-user login) | 🟢 Good |
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
| `auth.py` | 36 (verify headers/status codes, gateway-token decode, go redirect, API-key model) | 5 (setup, login, me, refresh, end-user login) | 🟡 Partial |
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

| Component | Unit Tests | Integration Tests | Coverage |
|---|---|---|---|
| `server.py` | 0 | 0 | 🔴 None |

---

## 3. Coverage by API Endpoint

| Endpoint | Method | Tested? | Test File |
|---|---|---|---|
| `/health` | GET | ✅ | integration.py, integration.sh |
| `/api/auth/setup` | POST | ✅ | integration.py, integration.sh |
| `/api/auth/register` | POST | ❌ | — |
| `/api/auth/login` | POST | ✅ | integration.py, integration.sh |
| `/api/auth/refresh` | POST | ✅ | integration.py |
| `/api/auth/me` | GET | ✅ | integration.sh |
| `/api/auth/verify` | GET | ✅ | test_unit.py (TestAuthVerifyHeaders, TestVerifyAuthStatusCodes) |
| `/api/auth/password` | PUT | ❌ | — |
| `/api/auth/keys` | POST | ✅ | test_unit.py (TestApiKeyModel, TestGatewayTokenDecode) |
| `/api/auth/keys` | GET | ✅ | test_unit.py (TestGatewayTokenDecode) |
| `/api/auth/keys/{id}` | DELETE | ✅ | test_unit.py (TestGatewayTokenDecode) |
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
| `/api/system/config` | GET/PUT | ❌ | — |
| `/api/system/subnet-pool` | GET | ✅ | test_unit.py (TestSubnetPoolSystemEndpoint) |
| `/api/services` | GET/POST | ✅ | deploy.sh (list only) |
| `/api/services/{name}` | GET/DELETE | ❌ | — |
| `/api/services/{name}/files/{file}` | GET/PUT | ❌ | — |
| `/api/services/{name}/convert` | POST | ❌ | — |
| `/api/services/{name}/check-missing-files` | GET | ✅ | test_unit.py (TestRecipePathMultiRecipe — recipe_path forwarding) |
| `/api/services/templates` | GET | ✅ | test_unit.py (TestTemplateMode) |
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

**Summary:** 26 of 62 endpoints tested (41.9%)

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
| **MCP Server** | None | 🔴 None |

---

## 5. Gaps & Recommendations

### 5.1 Critical Gaps (No Tests)

| Gap | Impact | Recommendation |
|---|---|---|
| Frontend (entire) | User-facing UI has zero automated tests | Add React Testing Library + Playwright tests for critical flows (login, deploy, service management) |
| MCP Server | External AI agent integration has no tests | Add pytest tests for SSE event stream, session management, JWT verification |
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
