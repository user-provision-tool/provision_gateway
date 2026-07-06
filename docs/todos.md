# Provision Gateway — TODO List

> **Date**: 2026-07-06
> **Status**: Active development tracking

---

## ITERATION 2 — In Progress

### Task 9: New user registration & role-based access ✅ (backend done, browser test pending)
- [x] Modify auth_service for end-user authentication
- [x] Modify login endpoint to check both admins and end_users tables
- [x] Add user_type field to JWT tokens
- [x] Add get_current_user middleware
- [x] Update /api/auth/me for end-user tokens
- [x] Update refresh token endpoint
- [x] Frontend: handle end-user login response
- [x] Frontend: sidebar shows correct menu by role
- [x] Frontend: services page filters by end-user username
- [ ] Browser test full registration flow (register → approve → login → verify)

### Task 10: Per-task logs + configurable persistence
- [ ] Modify provision-api to persist per-task logs in separate files
- [ ] Add configurable task persistence duration (default 1 week)
- [ ] Add settings page UI for task retention config
- [ ] Update gateway SSE log endpoint to read per-task log files
- [ ] Remove per-task log files when task expires

### Fix: Compose converter bug (container_name truncation)
- [ ] Replace gateway's buggy local compose_converter with HTTP delegation to provision-api
- [ ] Same for nginx_converter
- [ ] Fix `_extract_svc` rsplit bug as immediate mitigation

---

## ITERATION 1 — Completed ✅

### Task 6: siyuan-mcp always down ✅
- [x] Root cause: container_name template had `{{ container_prefix }}server` instead of `{{ container_prefix }}siyuan-mcp-server`
- [x] Fix .j2 template
- [x] Regenerate compose file
- [x] Recreate container with correct name
- [x] Remove old orphan container
- [x] Reconnect provision-nginx

### Task 7: Play/pause single button ✅
- [x] Replace separate Up/Down buttons with Play/Pause toggle
- [x] Auto-detect state (running → pause icon, stopped → play icon)

### Task 3: Templates vs Generated Files ✅
- [x] .env files now in Templates column
- [x] Per-user compose files in Generated Files column
- [x] Deduplication between columns

### Task 4: Deployment files plain text ✅
- [x] Replace colored Tag labels with plain text headers
- [x] Show correct file paths with directory context

### Task 2: File auto-location ✅
- [x] Handle ?file= query parameter in URL
- [x] Auto-load and auto-expand directory tree

### Bug fix: Task notification error ✅
- [x] Handle undefined task ID in notification polling

---

## Backlog

### Task 5: Deploy with snaprovision.com domain
- [ ] Configure local hosts for snaprovision.com → provision-nginx
- [ ] Deploy services with snaprovision.com domain
- [ ] Verify service-to-service communication

### Task 8: Full browser lifecycle test
- [ ] Deploy a service end-to-end via WebUI
- [ ] Test start/stop via play/pause toggle
- [ ] Test rebuild
- [ ] Test password change
- [ ] Test test-curl connectivity
- [ ] Test delete
- [ ] Verify no regressions

### Documentation
- [ ] Update features.md with latest implementation status
- [ ] Update architecture.md if needed
- [ ] Update api_references.md with end-user auth endpoints
