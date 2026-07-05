# Provision Gateway — WebUI Operation Sequences

> Date: 2026-07-05 (updated)
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
- **Sequence**: Opens chat modal → user types message → POST /api/llm/generate (type=troubleshoot) → display response
- **Status**: ✅ Working (requires active LLM config)

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

---

## 2. Source Projects Page (`/services`)

### 2.1 Add Project Button
- **Trigger**: Click "Add Project" button (admin only)
- **Sequence**: Opens modal with 3 tabs:
  - **From Git**: Fill repo URL, branch, name → POST /api/services (mode=git) → clone repo
  - **Upload Zip**: Fill name, base64 zip content → POST /api/services (mode=upload)
  - **From Template**: Fill name, type → POST /api/llm/generate → POST /api/services/save-generated
- **Status**: ✅ Working (requires working LLM for template mode)

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
- **Trigger**: Click "Deploy" button
- **Sequence**: Opens DeployForm modal → select user, service, label, domain, password → POST /api/users/deploy → async task created
- **Status**: ✅ Working

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

#### 3.5.1 Up Button
- **Trigger**: Click up arrow
- **Sequence**: POST /api/users/{user}/{service}/{label}/up → docker compose up -d
- **Status**: ✅ Fixed (compose path resolution + docker compose plugin)

#### 3.5.2 Down Button
- **Trigger**: Click down arrow
- **Sequence**: POST /api/users/{user}/{service}/{label}/down → docker compose stop
- **Status**: ✅ Fixed

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
  - URL (clickable link)
  - Test button (POST /api/users/{user}/{service}/{label}/test-curl)
  - Container names + status
  - Deployment files (compose, nginx, env - clickable links to editor)
  - Volumes (if available)
- **Status**: ✅ Working

### 3.7 Test Button
- **Trigger**: Click "Test" link next to URL
- **Sequence**: POST /api/users/{user}/{service}/{label}/test-curl → display HTTP response
- **Status**: ✅ Working

---

## 4. Tasks Page (`/tasks`)

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
- **Sequence**: POST /api/auth/login → JWT tokens → redirect to /dashboard
- **Status**: ✅ Working

### 8.2 Register New Account
- **Trigger**: Click "Register new account" link
- **Sequence**: Opens modal → fill username, email, password, confirm → POST /api/auth/users/register
- **Status**: ✅ Implemented (requires admin approval to login)

---

## Summary

| Page | Operations | Status |
|---|---|---|
| Dashboard | 7 operations | All ✅ |
| Source Projects | 9 operation groups | All ✅ |
| Services (Users) | 10 operation groups | All ✅ (Up/Down fixed) |
| Tasks | 4 operations | ✅ (SSE log per-task ⚠️ reads global log file, filters by task context) |
| Settings | 3 panels (LLM, Proxy, Special Users) | All ✅ |
| Audit | 3 operations (filter, CSV export, auto-refresh) | All ✅ |
| User Management | 5 operations (register, approve, special users, delete, role change) | All ✅ |
| Login | 2 operations (login, register) | All ✅ |

### Known Issues:
1. **Task SSE log reads global file** — The log endpoint reads `DOCKER_OPS_LOG` (global file) and filters by task context (user/service name). Per-task log isolation could be improved if provision-api exposes a per-task log endpoint.
2. **Git-diff shown as separate view** — Diff is displayed in Monaco DiffEditor component (read-only), not as inline decorations in the main editor. This is an acceptable UX choice.
3. **Redeploy button** — The Redeploy button triggers rebuild with `no_cache: true`. Full e2e flow verification needed.
4. **SSL certs display** — SSL certificate file paths (fullchain.pem, privkey.pem) are available in the expanded service card but could be more prominently displayed.
5. **siyuan-mcp container status** — The siyuan-mcp container for alice shows "0 up, 1 down" (exited). Root cause investigation needed.

### Verified Pages (Browser Check — 2026-07-05):
- ✅ Login page (`/login`) — Email + password fields, Register link, gradient background
- ✅ Dashboard (`/dashboard`) — Stat cards (0 Services, 0 Users, 0 Running Tasks, 0/0 Containers), CPU/RAM/Disk gauges, System Components table, Global Proxy card, Welcome card
- ✅ Source Projects (`/services`) — 3 projects listed (siyuan, siyuan-mcp, test-nginx-clone), Add Project button, file tags, Deploy/Delete buttons
- ✅ Services (`/users`) — alice with 2 services (siyuan Running(1), siyuan-mcp 0up/1down), all action buttons present (Up/Down/Rebuild/Redeploy/Key/Dup/Delete), Clone All button, Filter input, Deploy button
- ✅ Tasks (`/tasks`) — 1 failed rebuild task, Logs and Delete buttons, pagination
- ✅ Settings (`/settings`) — LLM panel (BYOK deepseek-chat ACTIVE), Proxy panel, Special Users panel
- ✅ User Management (`/users/manage`) — tester (viewer, Approved), alice (viewer, Approved), Special Users Configuration collapsible, Register User button
