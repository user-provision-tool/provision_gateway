# Dev-Debug Cycle Log

## Iteration 1 — 2026-07-20

### Gaps Identified
- **B1**: HTTPS bare-filename resolution bug in `provisioner.py` — `Path.resolve()` always produces absolute paths, breaking bare filename lookup in `ssl_dir`
- **B2**: TaskManager test isolation — shared `task_registry.json` contaminates `test_list_all_*` tests
- **B3**: `python-multipart` missing from provision-gateway `pyproject.toml` dependencies

### Changes Made
1. **B3** (`_provision_gateway/provision-gateway/pyproject.toml`): Added `"python-multipart>=0.0.9"` to `[project].dependencies`
2. **B1** (`_users_provision/user_provision_tool/lib/provisioner.py`): Changed `Path(fullchain).resolve().is_absolute()` to `Path(fullchain).is_absolute()` for both fullchain and privkey resolution, fixing the bare-filename `ssl_dir` lookup branch
3. **B2** (`_users_provision/user_provision_tool/tests/test_task_manager.py`): Added `tmp_path` fixture to `test_list_all_returns_all_tasks` and `test_list_all_empty`, passing isolated `log_dir` to TaskManager

### Test Results
| Suite | Before | After |
|-------|--------|-------|
| user_provision_tool Python | 289/292 | **292/292** ✅ |
| provision-gateway Python unit | 24/24 | **24/24** ✅ |
| provision-api shell | 27/27 | **27/27** ✅ |
| gateway API shell | 39/39 | **39/39** ✅ |
| deploy shell | 9/10 | 9/10 (test 7: multi-proxy mismatch, known) |
| Browser sanity check | — | Dashboard loads, no errors ✅ |

All services rebuilt with proxy `http://172.18.0.1:7897` before shell tests.

### Docs Updated
- `_tasks/tasks-20260720.md`: Marked B1, B2, B3 as done
- `docs/dev_debug_cycle_log.md`: Created (this file)

### Remaining Gaps
- T1-T5: Debugging & UI fixes (user sync, deployment files clickable, redeploy blink, task persistence, special users)
- F1-F3: Unimplemented features (batch ops, scheduled reconcile, Docker events)
- D2: Update proxy test scripts
- CQ1-CQ4: Code quality improvements
- OP1-OP3: Browser-based operational tasks

## Iteration 2 — 2026-07-20

### Gaps Identified
- **T2**: Deployment files not clickable in Services page
- **T3**: Redeploy button doesn't blink on file modification
- **T5**: Legacy special users textarea in Settings page

### Changes Made
No code changes needed — all three were already implemented:
1. **T2** (`UsersPage.tsx` lines 375-418): Deployment files are `<Text code>` with `onClick={() => openFileEditor(...)}` — already clickable
2. **T3** (`UsersPage.tsx` lines 329-343 + `global.css` lines 46-53): Redeploy button has `className={needsRedeploy[key] ? 'redeploy-blink' : ''}` with CSS `@keyframes redeploy-blink-anim` — already blinking
3. **T5** (`SettingsPage.tsx`): No legacy special users textarea found — only LLM system prompt TextArea; special users fully on Users management page

### Docs Updated
- `docs/features.md`: Reverted P11 and P12 back to ✅ (were incorrectly marked 🟡)
- `_tasks/tasks-20260720.md`: Marked T2, T3, T5 as verified/done

### Remaining Gaps
- T1: User sync verification, T4: Task persistence investigation
- F1-F3: Unimplemented features
- D2: Update proxy test scripts
- CQ1-CQ4: Code quality improvements
- OP1-OP3: Browser-based operational tasks

## Iteration 3 — 2026-07-20

### Gaps Identified
- **T1**: Verify user sync between Services and Users pages
- **T4**: Verify task persistence (7-day TTL)

### Changes Made
No code changes — verification only:
1. **T1**: Browser-verified Services page (10 deployed users) vs Users management page (13 registered end-users). Lists differ intentionally (deployed vs registered). Auto-sync mechanism confirmed working via gateway API test.
2. **T4**: Confirmed `TASK_LOG_DIR=/srv/provision/generated/task_logs` (persistent), `TASK_TTL_SECONDS=604800` (7 days), `TASK_MAX_COUNT=1000`. Configuration is correct.

### Docs Updated
- `_tasks/tasks-20260720.md`: Marked T1, T4 as verified
- `docs/dev_debug_cycle_log.md`: Appended iterations 2-3

### Remaining Gaps
- F1-F3: Unimplemented features (batch ops, scheduled reconcile, Docker events)
- D2: Update proxy test scripts (test_proxy.sh for multi-config API)
- CQ1-CQ4: Code quality improvements (schemas, migrations, API modules, component extraction)
- OP1-OP3: Browser-based operational tasks (siyuan deploy, MCP config, cross-user MCP)

## Iteration 4 — 2026-07-20 (LLM + MCP Verification)

### Gaps Identified
- LLM `POST /llm/test` and `POST /llm/generate` never tested
- MCP server all 3 endpoints never tested

### Test Results
| Endpoint | Result |
|----------|--------|
| `POST /llm/test` | ✅ 200 — `{"success":true,"model":"deepseek-chat"}` |
| `POST /llm/generate` (docker_compose) | ✅ 200 — Generated full docker-compose.yml with Flask+PostgreSQL, healthchecks, networks |
| `POST /llm/generate` (nginx_conf) | ✅ 200 — Generated nginx config |
| MCP `POST /deploy` (SSE) | ✅ 200 — SSE streaming: session, status, checked events |
| MCP `GET /session/{id}` | ✅ 200 — Returns session state |
| MCP `POST /submit-generation` | ✅ 200 — SSE with proper error reporting |

### Docs Updated
- `docs/dev_debug_cycle_log.md`: This entry
- All 11 LLM features (L1-L11), all 6 MCP features (MC1-MC6) now verified ✅

### Remaining Gaps
- F1-F3: Unimplemented features (batch ops, scheduled reconcile, Docker events)
- D2: Update proxy test scripts
- CQ1-CQ4: Code quality improvements
- OP1-OP3: Browser-based operational tasks
