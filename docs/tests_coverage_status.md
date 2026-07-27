# Provision Gateway — Tests Coverage Status

> **Version**: 1.3
> **Date**: 2026-07-22 (updated — tasks-220702026.md fixes: 29 gateway + 297 provision tests pass)
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
| `test_unit.py` | Python (pytest) | Unit | 29 | ✅ Passing |
| `test_proxy.py` | Python (pytest) | Unit | 8 | ✅ Passing |
| `test_integration.py` | Python (subprocess) | Integration | 9 | ✅ Passing |
| `test_integration.sh` | Bash | Integration | 9 | ✅ Passing |
| `test_deploy.sh` | Bash | Integration | 9 | ✅ Passing |
| `test_proxy.sh` | Bash | Integration | 12 | ✅ Passing |
| `test_gateway_api.sh` | Bash | Integration | 347 lines (comprehensive) | ✅ Passing |
| `test_provision_api.sh` | Bash | Integration | 252 lines (comprehensive) | ✅ Passing |
| **Total** | | | **65+** | |

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
| `auth_service.py` | 4 (hash, verify, JWT, end-user auth) | 3 (login, refresh, end-user login) | 🟢 Good |
| `proxy_service.py` | 3 (env injection, disabled proxy) | 12 (full CRUD, deploy integration) | 🟢 Good |
| `provision_service.py` | 14 (method existence checks) | 3 (list users, get user, error handling) | 🟢 Good |
| `service_manager.py` | 0 | 1 (list services) | 🔴 None |
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
| `auth.py` | 0 | 5 (setup, login, me, refresh, end-user login) | 🟡 Partial |
| `system.py` | 0 | 4 (status, proxy CRUD, SSL certs) | 🟡 Partial |
| `services.py` | 0 | 1 (list) | 🔴 Minimal |
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
| `/api/auth/password` | PUT | ❌ | — |
| `/api/auth/users` | GET | ❌ | — |
| `/api/auth/users/register` | POST | ❌ | — |
| `/api/auth/users/{id}/approve` | PUT | ❌ | — |
| `/api/auth/users/{id}` | PUT/DELETE | ❌ | — |
| `/api/auth/users/deployable` | GET | ❌ | — |
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
| `/api/services` | GET/POST | ✅ | deploy.sh (list only) |
| `/api/services/{name}` | GET/DELETE | ❌ | — |
| `/api/services/{name}/files/{file}` | GET/PUT | ❌ | — |
| `/api/services/{name}/convert` | POST | ❌ | — |
| `/api/services/scan` | POST | ❌ | — |
| `/api/services/save-generated` | POST | ❌ | — |
| `/api/services/check-deploy` | POST | ❌ | — |
| `/api/services/{name}/git/status` | GET | ❌ | — |
| `/api/services/{name}/git/diff` | GET | ❌ | — |
| `/api/services/{name}/git/head-file` | GET | ❌ | — |
| `/api/users` | GET | ✅ | integration.py, deploy.sh |
| `/api/users/{name}` | GET | ❌ | — |
| `/api/users/deploy` | POST | ✅ | deploy.sh (5 variations) |
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

**Summary:** 11 of 47 endpoints tested (23.4%)

---

## 4. Coverage by Feature

| Feature Category | Test Coverage | Status |
|---|---|---|
| **Authentication** | Login, setup, token refresh, me | 🟡 Partial (missing: register, password change, user management, deployable users) |
| **System Monitoring** | Status endpoint | 🟡 Partial (missing: stats, config) |
| **Proxy Management** | Full CRUD, enable/disable, credentials, reachability test, deploy integration, audit | 🟢 Good |
| **Service Projects** | List only | 🔴 Minimal (missing: CRUD, files, git, convert, scan, check-deploy) |
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

### 5.3 Test Quality Recommendations

1. **Add pytest fixtures** for common setup (DB session, admin auth token, mock HTTP responses)
2. **Add conftest.py** with shared fixtures (currently minimal)
3. **Separate unit from integration** — use pytest markers (`@pytest.mark.unit`, `@pytest.mark.integration`)
4. **Add test coverage reporting** — `pytest --cov=app --cov-report=html`
5. **Add CI pipeline** — GitHub Actions or similar to run tests on PR
6. **Add frontend tests** — Vitest + React Testing Library for components, Playwright for E2E
7. **Add API contract tests** — Schema validation for request/response payloads
8. **Add performance tests** — Response time assertions for critical endpoints

### 5.4 Recommended Test Priority

| Priority | Area | Reason |
|---|---|---|
| P0 | Frontend E2E (Playwright) | User-facing; regressions directly visible |
| P1 | LLM Service (unit) | Complex logic; prompt quality critical |
| P1 | Reconciliation (unit) | Recovery logic; bugs cause downtime |
| P2 | Service Manager (unit) | File operations; data loss risk |
| P2 | Docker Service (unit) | Container management; production impact |
| P3 | Remaining API endpoints | Completeness |
| P3 | MCP Server | New feature; external interface |
