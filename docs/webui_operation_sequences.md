# Provision Gateway — WebUI Operation Sequences

> Version: 3.1
> Date: 2026-08-22 (updated — v4 Service-ACL enforcement: cookie-only login (provision_token), two-token/JWT-body login model removed, /go/ issues a 30s exchange code with no JWT in URL; prior: API Keys page, Alert page, viewer role-gating (AdminRoute), /go/{hostname} service access, multi-recipe DeployForm, two-token login, Dashboard Subnet Pool card)
> Purpose: Document all operation sequences defined by each button on the webui, verified for correctness.
> Verified against: actual running dashboard at http://localhost:8771

---

## 1. Dashboard Page (`/dashboard`)

### 1.1 Refresh Button
- **Trigger**: Click "Refresh" button
- **Sequence**: GET /api/system/status → re-render stat cards, system components table, user cards
- **Status**: ✅ Working

### 1.2 Reconcile Button
- **Trigger**: Click "Reconcile" button
- **Sequence**: POST /api/system/reconcile → triggers nginx upstream reconciliation → updates component table
- **Status**: ✅ Working

### 1.3 Question-circle (Troubleshoot Chat)
- **Trigger**: Click "?" icon in header (admin only)
- **Sequence**: Opens chat modal → user types message → POST /api/llm/generate (generate_type=troubleshoot) → always returns `400 Invalid type` (backend reads `type`; troubleshoot not implemented)
- **Status**: 🔜 Future — the frontend modal exists, but the backend troubleshoot contract is not implemented (needs redesign)

### 1.4 User Menu Dropdown
- **Trigger**: Click admin email in header
- **Sequence**: Dropdown shows "Change Password" and "Logout"
  - **Change Password**: Opens modal → PUT /api/auth/password → success toast
  - **Logout**: Clears token → redirects to /login
- **Status**: ✅ Working

### 1.5 Sidebar Navigation
- **Trigger**: Click any sidebar menu item
- **Sequence**: Navigate to route → page loads with API calls
- **Sidebar highlight**: ✅ Fixed (Users now highlights correctly on /users/manage)

### 1.6 Sidebar Collapse
- **Trigger**: Click menu-fold/menu-unfold button
- **Sequence**: Toggles sidebar collapsed state
- **Status**: ✅ Working

### 1.7 User Card Click (alice)
- **Trigger**: Click alice card
- **Sequence**: Navigate to /users (Services page filtered to alice)
- **Status**: ✅ Working

### 1.8 Subnet Pool Card
- **Trigger**: Dashboard renders on load / after Refresh
- **Sequence**: GET /api/system/subnet-pool → renders a "Subnet Pool" card with one panel per pool (CIDR, used %, progress bar, used/total slots). Colors: green ≤70%, orange 71–90%, red >90%. If subnet management is disabled (SUBNET_POOLS unset), shows "Subnet management is disabled" hint.
- **Status**: ✅ Working

---

## 2. Source Projects Page (`/services`)

### 2.1 Add Project Button
- **Trigger**: Click "Add Project" button (admin only)
- **Sequence**: Opens modal with 2 tabs (iter-1, GAP-1 — "From Template" tab removed):
  - **From Git**: Fill repo URL, branch, name → POST /api/services (mode=git) → clone repo
  - **Upload Zip**: Fill name, select a local .zip file via the file picker, or paste individual file contents as JSON → POST /api/services (mode=upload)
- **Backend template mode**: `POST /api/services` (mode=template, template_id=N) and `GET /api/services/templates` remain at the API level but are **DEPRECATED/dormant** — the `service_templates` table has no writer/seed in the repo (empty unless seeded manually), and the "From Template" tab was removed from the modal (orphan `AddServiceModal.tsx` deleted, GAP-1).
- **Status**: ✅ Working for the Git/Upload flows; template mode 🧟 deprecated (API-only, no data source).

### 2.2 Project Name Click (folder-open icon)
- **Trigger**: Click project name
- **Sequence**: Navigate to /services/{name} → GET /api/services/{name} → show file list + editor
- **Status**: ✅ Working

### 2.3 Template File Click (green tags)
- **Trigger**: Click template filename (e.g., docker-compose.yml.j2)
- **Sequence**: Navigate to /services/{name}?file={filename} → load file in editor
- **Status**: ✅ Working

### 2.4 Deploy Button
- **Trigger**: Click "Deploy" button in Actions column
- **Sequence**: Navigate to /services/{name} → shows file view → user can review then use deploy flow
- **Status**: ✅ Working

### 2.5 Delete Button
- **Trigger**: Click delete icon in Actions column
- **Sequence**: Confirmation dialog → DELETE /api/services/{name} → remove project
- **Status**: ✅ Working

### 2.6 File Browser (in project detail)
- **Trigger**: Click file in file list
- **Sequence**: GET /api/services/{name}/files/{filename} → display content in Monaco editor
- **.git filtering**: ✅ Implemented (hides .git, node_modules, dist, .vite)
- **Generated files**: ✅ Highlighted green with "new" tag
- **Status**: ✅ Working

### 2.7 File Editor
- **Trigger**: Click "Edit" button after selecting a file
- **Sequence**: Monaco editor becomes editable → "Save" and "Cancel" buttons appear
  - **Save**: PUT /api/services/{name}/files/{filename} → save content
  - **Cancel**: Revert to original content
- **Status**: ✅ Working

### 2.8 Show Changes (Git-diff view)
- **Trigger**: Click "Show Changes" button when file has unsaved modifications
- **Sequence**: Computes line-by-line diff → displays in colored pre block
  - Added lines: green background
  - Removed lines: red background
  - Modified lines: yellow background
- **Status**: ✅ Working (separate diff view, not inline Monaco diff editor)

### 2.9 Convert Button
- **Trigger**: Click "Convert" when project has plain compose/nginx files
- **Sequence**: POST /api/services/{name}/convert → convert to .j2 templates
- **Status**: ✅ Working

---

## 3. Services Page (`/users`) — Deployed Services

### 3.1 Deploy Button
- **Trigger**: Click "Deploy" button (rocket icon, top-right)
- **Sequence**: Opens DeployForm modal → select user, service → label is auto-computed (GET /api/users/{user}/{service}/next-label) and shown as disabled input → fill domain, password → POST /api/users/deploy → async task created
- **Multi-recipe projects**: When a source project has more than one recipe, the Service dropdown lists each recipe as `name @ recipe_path` (option value `name@@recipe_path`). Selecting one runs `GET /api/services/{name}/check-missing-files?recipe_path=...` and deploys with `project_root = {base}/{recipe_path}` (template paths scoped to the recipe subdirectory).
- **Deploy validation**: When the selected service is missing essential files (compose, nginx conf) and no LLM-generated files exist, the Deploy button is disabled and a warning is shown. User must either configure LLM to generate missing files or provide them manually.
- **Auto-generated templates flow**: When service is missing essential files (docker-compose, nginx.conf, .env, Dockerfile), an alert shows with "Auto Templates Completion" checkbox.
  - **Auto mode (checked)**: LLM generates missing files → auto-submits deploy after 500ms delay
  - **Manual mode (unchecked)**: User clicks "Generate with LLM" → generated files shown as clickable tags for review → deploy saves files to disk first, then submits
  - If LLM not configured, user must upload files manually in the source project
- **Status**: ✅ Working (G12 fixed: generated files saved to disk regardless of autoDeploy state)

### 3.2 Refresh Button
- **Trigger**: Click "Refresh" button
- **Sequence**: GET /api/users → re-render service groups
- **Status**: ✅ Working

### 3.3 Search Filter
- **Trigger**: Type in "Filter..." input
- **Sequence**: Client-side filtering by user name, service name → highlight matches
- **Match highlighting**: ✅ Working (highlight() function)
- **Status**: ✅ Working

### 3.4 Clone All Button (per user)
- **Trigger**: Click "Clone All" under a user's name
- **Sequence**: Opens modal → enter target user → POST /api/users/clone → clone all services to target
- **Status**: ✅ Working

### 3.5 Per-Service Action Buttons
Each service card shows these buttons:

### 3.5.1 Play/Pause Button (Toggle)
- **Trigger**: Click Play/Pause toggle button
- **Sequence**: 
  - If running (▶): POST /api/users/{user}/{service}/{label}/down → docker compose stop
  - If stopped (⏸): POST /api/users/{user}/{service}/{label}/up → docker compose up -d
- **Status**: ✅ Fixed (single toggle button replaces separate Up/Down buttons)

### 3.5.2 Down Button
- ~~**Trigger**: Click down arrow~~
- ~~**Sequence**: POST /api/users/{user}/{service}/{label}/down → docker compose stop~~
- **Status**: ❌ Replaced by Play/Pause toggle (see 3.5.1)

### 3.5.3 Up Button
- ~~**Trigger**: Click up arrow~~
- ~~**Sequence**: POST /api/users/{user}/{service}/{label}/up → docker compose up -d~~
- **Status**: ❌ Replaced by Play/Pause toggle (see 3.5.1)

#### 3.5.3 Rebuild Button
- **Trigger**: Click "Rebuild"
- **Sequence**: POST /api/users/{user}/{service}/{label}/rebuild → async task created → link to Tasks page
- **Status**: ✅ Working

#### 3.5.4 Redeploy Button
- **Trigger**: Click "Redeploy"
- **Sequence**: Opens redeploy flow (re-deploys with same config)
- **Status**: ⚠️ Button present, needs full flow verification

#### 3.5.5 Key Button (Password Change)
- **Trigger**: Click key icon
- **Sequence**: Opens password change modal → PUT /api/users/{user}/{service}/{label}/password → nginx reload
- **Status**: ✅ Working

#### 3.5.6 Dup Button (Duplicate)
- **Trigger**: Click "Dup"
- **Sequence**: Prompt for target user → POST /api/users/deploy with same config → deploy to new user
- **Status**: ✅ Working

#### 3.5.7 Delete Button
- **Trigger**: Click delete icon
- **Sequence**: Popconfirm → DELETE /api/users/{user}/{service}/{label} → remove service
- **Status**: ✅ Working

### 3.6 Service Card Expansion
- **Trigger**: Click collapsed service card
- **Sequence**: Expands to show:
  - URL (clickable link → `/go/{service}-{user}-{label}.localhost`)
  - Test button (POST /api/users/{user}/{service}/{label}/test-curl)
  - Container names + status
  - Deployment files (compose, nginx, env - clickable links to editor)
  - Volumes (if available)
- **Status**: ✅ Working

**Service-access redirect (`/go/{hostname}`)** — clicking the URL link opens `/go/{service}-{user}-{label}.localhost` on the gateway. `GET /api/auth/go/{hostname}` validates the `provision_token` session, looks up the hostname in the HostnameIndex, checks the viewer's ACL (own service or `allowed_special_users`), then issues a **30s HMAC exchange code** + `Location` header — **no JWT in the URL** (v4 F7). The service-side `/_set_token` is a plain variable proxy to `/api/auth/exchange`, which swaps the code for the `provision_token` cookie via `302`+`Set-Cookie` and loads the service root. A cookie exchange is required because the login cookie is host-scoped and is NOT sent to the service hostname directly.

### 3.7 Test Button
- **Trigger**: Click "Test" link next to URL
- **Sequence**: POST /api/users/{user}/{service}/{label}/test-curl → display HTTP response
- **Status**: ✅ Working

---

## 4. Tasks Page (`/tasks`)

### 4.0 Task Notifications (Global)
- **Trigger**: Any task transitions from pending → completed/failed (detected globally, not per-page)
- **Sequence**: AppLayout.tsx polls GET /api/tasks every 2s → detects status transitions → shows antd notification toast + browser Notification API alert
- **Deduplication**: Each task notified only once via `notifiedRef`
- **Time filter**: 2-second window on `updated_at` timestamps prevents stale-task notifications on page load
- **Status**: ✅ Working (global notification, not tied to Tasks page)

### 4.1 Refresh Button
- **Trigger**: Click "Refresh"
- **Sequence**: GET /api/tasks → re-render task table
- **Auto-polling**: ✅ Every 5 seconds via usePolling
- **Status**: ✅ Working

### 4.2 Logs Button
- **Trigger**: Click "Logs" (eye icon) on a task row
- **Sequence**: Opens SSE log drawer → GET /api/tasks/{task_id}/log (SSE stream) → live log display
- **Status**: ⚠️ SSE reads global DOCKER_OPS_LOG file, not per-task filtered

### 4.3 Cancel Button
- **Trigger**: Click cancel icon on pending/running task
- **Sequence**: Popconfirm → DELETE /api/tasks/{task_id}
- **Status**: ✅ Working

### 4.4 Delete Button
- **Trigger**: Click delete icon on any task
- **Sequence**: Popconfirm → DELETE /api/tasks/{task_id}
- **Status**: ✅ Working

---

## 5. Settings Page (`/settings`)

### 5.1 LLM Configuration Panel
- **Add Config**: Fill mode, API URL, model, key → POST /api/llm/configs
- **Activate**: PUT /api/llm/configs/{id}/activate
- **Delete**: DELETE /api/llm/configs/{id}
- **Test Active**: POST /api/llm/test → connection test
- **Status**: ✅ Working (BYOK deepseek-chat configured)

### 5.2 Global Proxy Panel
- **Add Proxy**: Fill name, protocol, host, port → POST /api/system/proxy (auto-tests reachability)
- **Activate**: PUT /api/system/proxy/{id}/activate (reachability-gated)
- **Delete**: DELETE /api/system/proxy/{id}
- **Status**: ✅ Working (Host Proxy active, reachable)

### 5.3 Special Users Panel
- **Configure**: Set global special users list
- **Status**: ✅ Working (configured in system config)

---

## 6. Audit Page (`/audit`)

### 6.1 Filters
- **Action dropdown**: Filter by action type
- **Target User input**: Filter by user name
- **Date range**: Start/end date pickers
- **Clear**: Reset all filters
- **Status**: ✅ Working

### 6.2 CSV Export
- **Trigger**: Click "CSV" button
- **Sequence**: Download filtered audit log as CSV
- **Status**: ✅ Working

### 6.3 Auto-refresh
- **Sequence**: 30s polling via usePolling
- **Status**: ✅ Working

---

## 7. User Management Page (`/users/manage`)

### 7.1 Register User Button
- **Trigger**: Click "Register User"
- **Sequence**: Modal → fill username, password, role → POST /api/auth/users/register
- **Status**: ✅ Working

### 7.2 Special Functional Users Card (Collapsible)
- **Trigger**: Click to expand
- **Sequence**: Shows global special users list + configuration info
- **Status**: ✅ Implemented

### 7.3 Per-User Special Users Assignment
- **Trigger**: Click "Special" button on user row
- **Sequence**: Opens modal → shows toggleable special users → PUT /api/auth/users/{id} (allowed_special_users)
- **Status**: ✅ Implemented

### 7.4 Approve Button
- **Trigger**: Click "Approve" on pending user
- **Sequence**: PUT /api/auth/users/{id}/approve → user activated
- **Status**: ✅ Working

### 7.5 Delete Button
- **Trigger**: Click close icon on user row
- **Sequence**: DELETE /api/auth/users/{id} → remove user
- **Status**: ✅ Working

---

## 8. Login Page (`/login`)

### 8.1 Login
- **Trigger**: Fill email + password → click "Log In"
- **Sequence**: POST /api/auth/login → sets a single `provision_token` httponly cookie (1-week, token_type=cookie; the Bearer `access_token`/`refresh_token` body pair and the legacy `gateway_token` cookie were removed in v4) → redirect to /dashboard
- **Status**: ✅ Working

### 8.2 Register New Account
- **Trigger**: Click "Register new account" link
- **Sequence**: Opens modal → fill username, email, password, confirm → POST /api/auth/users/register
- **Status**: ✅ Implemented (requires admin approval to login)

---

## 9. API Keys Page (`/api-keys`)

> Accessible by admins AND viewers (NOT admin-only). Admins see all users' keys; viewers see only their own.

### 9.1 Create Key Button
- **Trigger**: Click "Create Key" button
- **Sequence**: Opens modal → enter Label (required) → admins also see an optional "User ID" input (blank = own key, or pick a target user id) → POST /api/auth/keys → returns the one-time raw token
- **Status**: ✅ Working

### 9.2 One-Time Token Display + Copy
- **Trigger**: After creating a key
- **Sequence**: Modal switches to success state → shows the raw token in a read-only textarea with warning "Copy this token now — it will not be shown again" → "Copy Token" button copies to clipboard (navigator.clipboard.writeText)
- **Status**: ✅ Working

### 9.3 Refresh Button
- **Trigger**: Click "Refresh"
- **Sequence**: GET /api/auth/keys → re-render key table
- **Status**: ✅ Working

### 9.4 Revoke Button
- **Trigger**: Click "Revoke" on an active key row
- **Sequence**: Popconfirm → DELETE /api/auth/keys/{id} → key marked Revoked (red tag), no further actions shown
- **Status**: ✅ Working

### 9.5 Key Table
- **Columns**: ID, Label, (User ID — admins only), Created, Expires, Status (Active/Revoked tag), Actions (Revoke)
- **Viewer scope**: A viewer's table omits the User ID column and lists only the viewer's own keys (server-side filtered in GET /api/auth/keys)
- **Status**: ✅ Working

---

## 10. Alert Page (`/alert`)

> Reached from the subnet-acl-nginx ACL redirect, or by direct URL. Public route (no auth guard).

### 10.1 `?reason=acl_denied` (Access Denied)
- **Trigger**: subnet-acl nginx `@auth_403` redirect — browser hits a service the user has no access to → `302 http://{dashboard}/alert?reason=acl_denied&service={host}`
- **Sequence**: Reads `reason` and `service` from the URL → shows "Access Denied" with "You do not have access to {service}. Contact your administrator..." → "Back to Dashboard" button
- **Status**: ✅ Working

### 10.2 `?reason=token_expired` (API Token Expired)
- **Trigger**: Direct URL `/alert?reason=token_expired`
- **Sequence**: Shows "API Token Expired" — "Your API token has expired. Please log in again to get a new token." → "Go to Login" button
- **Note**: The subnet-acl nginx `@auth_401` redirect currently sends expired-token browsers to `/login` (Option B, acl-enforcement-design-v2.md §12), so this variant is only reached by direct URL.
- **Status**: ✅ Working

---

## Summary

| Page | Operations | Status |
|---|---|---|
| Dashboard | 8 operations (incl. Subnet Pool card) | All ✅ |
| Source Projects | 10 operation groups (2 tabs in Add Project — From Git / Upload Zip; "From Template" removed, GAP-1) | All ✅ |
| Services (Users) | 10 operation groups (incl. /go/{hostname} access, multi-recipe DeployForm) | All ✅ (Up/Down fixed) |
| Tasks | 4 operations + 1 global notification | ✅ (SSE log per-task ⚠️ reads global log file, filters by task context); global 2s task notification with toast + browser Notification API |
| Settings | 3 panels (LLM, Proxy, Special Users) | All ✅ |
| Audit | 3 operations (filter, CSV export, auto-refresh) | All ✅ |
| User Management | 5 operations (register, approve, special users, delete, role change) | All ✅ |
| Login | 2 operations (login, register) | All ✅ |
| API Keys | 5 operations (create, token copy, refresh, revoke, table) | All ✅ |
| Alert | 2 variants (acl_denied, token_expired) | All ✅ |

### Known Issues:
1. **Task SSE log reads global file** — The log endpoint reads `DOCKER_OPS_LOG` and filters by task context. Per-task log files would improve isolation (see Task 10 in tasks-20260705-3.md).
2. **Expired-token alert variant** — The Alert page supports `?reason=token_expired`, but the subnet-acl nginx `@auth_401` redirect currently sends expired-token browsers to `/login` (Option B, acl-enforcement-design-v2.md §12), so this variant is reached only by direct URL.
3. **Task log persistence** — Task logs should be configurable and per-task (see Task 10.2 in tasks-20260705-3.md).
4. **Redeploy button flow** — Full e2e verification needed.

### Fixed Issues:
**ITERATION 2 (2026-07-28):**
1. ✅ **G12 — Non-autoDeploy files saved to disk** — `handleDeploy` now saves generated files before deployment regardless of `autoDeploy` state.
2. ✅ **G15 — Removed unused hidden form field** — Removed `auto_templates_completion` field from DeployForm; auto-deploy uses React state only.
3. ✅ **G13-G16 — Dead code cleanup** — Removed 12 unused API exports from `services.ts`; kept only `createServiceGit`.
4. ✅ **GAP-005 — Route ordering fix (CRITICAL)** — Moved `/templates` and `/notifications` route definitions before the `/{name}` catch-all in services.py. GET /api/services/templates and GET /api/services/notifications no longer return 404. Verified by 6 new route ordering tests.
5. ✅ **GAP-006 — Missing Select import** — Added `Select` to antd import in ServicesPage.tsx TemplateForm component. Resolves TypeScript error TS2552.

**ITERATION 3 (2026-08-19):**
1. ✅ **Viewer role-gating (AdminRoute)** — App.tsx now wraps Dashboard/Source Projects/Tasks/Settings/Audit/Users/SSL in `AdminRoute` (non-admins → redirect `/users`); `/users`, `/api-keys`, `/alert` remain viewer-accessible. Sidebar hides admin items for viewers (Services + API Keys only).
2. ✅ **API Keys page (`/api-keys`)** — Create Key (label; admin may set User ID), one-time token display + Copy, list (admin sees all + User ID column, viewer sees own), Revoke. Endpoints: POST/GET `/api/auth/keys`, DELETE `/api/auth/keys/{id}`.
3. ✅ **Alert page (`/alert`)** — `?reason=acl_denied` (Access Denied — no access to {service}) and `?reason=token_expired` (API Token Expired) variants; reached from subnet-acl nginx `@auth_403` → `/alert?reason=acl_denied&service={host}` (expired token → `/login`).
4. ✅ **`/go/{hostname}` service access** — Services page URL links use `/go/{service}-{user}-{label}.localhost`; gateway `GET /api/auth/go/{hostname}` issues a 30s HMAC exchange code and 302s to `http://{host}:{port}/_set_token?code={code}&redirect=/`; `/_set_token` proxies to `/api/auth/exchange`, which sets the service-domain `provision_token` cookie (v4 — no JWT in any URL).
5. ✅ **Multi-recipe DeployForm** — Service dropdown lists `name @ recipe_path` (value `name@@recipe_path`); `check-missing-files?recipe_path=...` scopes readiness checks; deploy `project_root = {base}/{recipe_path}`.
6. ✅ **Cookie-only login (v4)** — login sets a single `provision_token` (1-week) httponly cookie; the legacy two-token (`gateway_token` + `provision_token`) and the JWT JSON body (`access_token`/`refresh_token`) model was removed in v4.
7. ✅ **Dashboard Subnet Pool card** — renders `GET /api/system/subnet-pool` pools (CIDR, used %, slots used/total).

**ITERATION 1 (2026-07-05):**
1. ✅ **siyuan-mcp always down** — Root cause: container_name template had `{{ container_prefix }}server` but should be `{{ container_prefix }}siyuan-mcp-server`. Fixed template, regenerated compose, restarted container.
2. ✅ **Separate Up/Down buttons** — Replaced with single Play/Pause toggle button.
3. ✅ **Task notification error** — "❌ Task undefined... failed" fixed by handling missing task IDs.
4. ✅ **Templates vs Generated Files** — Properly separated: .env files in Templates, generated compose files in Generated Files.
5. ✅ **File auto-location** — Files from URL query params now auto-open and auto-expand directory tree.

### Verified Pages (Browser Check — 2026-07-05):
- ✅ Login page (`/login`) — Email + password fields, Register link, gradient background
- ✅ Dashboard (`/dashboard`) — Stat cards (0 Services, 0 Users, 0 Running Tasks, 0/0 Containers), CPU/RAM/Disk gauges, System Components table, Global Proxy card, Welcome card
- ✅ Source Projects (`/services`) — 3 projects listed (siyuan, siyuan-mcp, test-nginx-clone), Add Project button, file tags, Deploy/Delete buttons
- ✅ Services (`/users`) — alice with 2 services (siyuan Running(1), siyuan-mcp 0up/1down), all action buttons present (Up/Down/Rebuild/Redeploy/Key/Dup/Delete), Clone All button, Filter input, Deploy button
- ✅ Tasks (`/tasks`) — 1 failed rebuild task, Logs and Delete buttons, pagination
- ✅ Settings (`/settings`) — LLM panel (BYOK deepseek-chat ACTIVE), Proxy panel, Special Users panel
- ✅ User Management (`/users/manage`) — tester (viewer, Approved), alice (viewer, Approved), Special Users Configuration collapsible, Register User button
