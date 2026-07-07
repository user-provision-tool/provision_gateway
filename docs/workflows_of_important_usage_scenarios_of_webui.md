# Provision Gateway — Workflows of Important Usage Scenarios (WebUI)

> **Version**: 1.1
> **Date**: 2026-07-08 (updated)
> **Purpose**: Step-by-step WebUI workflows verified against the actual dashboard at `http://localhost:8771`.

---

## Table of Contents

1. [First-Time Setup Wizard](#1-first-time-setup-wizard)
2. [Login & Registration](#2-login--registration)
3. [Dashboard — System Overview](#3-dashboard--system-overview)
4. [Source Projects — Add Service from Git](#4-source-projects--add-service-from-git)
5. [Source Projects — Upload Service Files](#5-source-projects--upload-service-files)
6. [Source Projects — File Editor with Git Diff](#6-source-projects--file-editor-with-git-diff)
7. [Source Projects — Convert to Templates](#7-source-projects--convert-to-templates)
8. [Services — Deploy to User](#8-services--deploy-to-user)
9. [Services — Play/Pause/Rebuild/Redeploy/Delete](#9-services--playpauserebuildredeploydelete)
10. [Services — Clone All Between Users](#10-services--clone-all-between-users)
11. [Services — Change Password & Test Connectivity](#11-services--change-password--test-connectivity)
12. [Tasks — Monitor & View Logs](#12-tasks--monitor--view-logs)
13. [SSL Certificates — Upload & Manage](#13-ssl-certificates--upload--manage)
14. [Settings — LLM Configuration](#14-settings--llm-configuration)
15. [Settings — Global Proxy](#15-settings--global-proxy)
16. [Settings — Special Functional Users](#16-settings--special-functional-users)
17. [Audit — Query & Export](#17-audit--query--export)
18. [User Management — Register, Approve, Assign Roles](#18-user-management--register-approve-assign-roles)
19. [Troubleshoot Chat](#19-troubleshoot-chat)

---

## 1. First-Time Setup Wizard

**Goal:** Initialize the gateway with the first admin account.

**Steps:**
1. Open browser → `http://localhost:8771`
2. You are redirected to `/setup` (Setup Wizard appears because no admin exists)
3. **Step 1: Create Admin Account**
   - Enter email (e.g., `admin@example.com`)
   - Enter password (minimum 6 characters)
   - Confirm password
   - Click **"Create Admin Account"**
4. **Step 2: Done**
   - ✅ Success checkmark appears
   - Auto-redirects to `/dashboard` after 1.5 seconds

**API calls:**
- `POST /api/auth/setup` → Creates initial admin
- `POST /api/auth/login` → Auto-login after setup

**Verification:** After setup, visiting `/setup` again redirects to `/login` (admin already exists).

---

## 2. Login & Registration

**Goal:** Login as admin or register a new account.

### Login Flow
1. Open `http://localhost:8771/login`
2. Enter email and password
3. Click **"Log In"**
4. Redirected to `/dashboard`
5. JWT tokens stored in browser `localStorage`

### Register New Account Flow
1. On login page, click **"Register new account"**
2. Fill in: Username, Email, Password, Confirm Password
3. Click **"Register"**
4. Message: "Registration submitted! Please wait for admin approval."
5. Admin must approve before the new user can login

**API calls:**
- `POST /api/auth/login` → JWT tokens
- `POST /api/auth/users/register` → New user registration (pending approval)

**UI Elements:**
- Login card with gradient background (purple-blue)
- Email input with user icon
- Password input with lock icon and show/hide toggle
- "Register new account" link below the separator

---

## 3. Dashboard — System Overview

**Goal:** View system health, resource usage, and quick stats.

**Page:** `/dashboard`

**What you see:**
1. **Stat Cards Row:** Services count, Users count, Running Tasks, Containers (running/total)
2. **CPU / RAM / Disk Gauges:** Circular progress gauges with color warnings (>80% turns red)
3. **System Components Table:** Status of provision-api, provision-nginx, provision-gateway, provision-dashboard
4. **Global Proxy Status Card:** Shows proxy enabled/disabled and reachability
5. **User Summary Cards:** Per-user healthy/unhealthy service counts
6. **Welcome Card:** Greeting with admin email
7. **Live Indicator:** Spinning "Live" tag when 10s polling is active

**Actions:**
- Click **"Refresh"** → Manual refresh of all data
- Click **"Reconcile"** → Triggers nginx upstream reconciliation
- Click user card → Navigates to `/users` filtered to that user

**API calls (every 10s):**
- `GET /api/system/status` → System health + counts
- `GET /api/system/stats` → Container stats
- `GET /api/system/proxy` → Proxy status
- `GET /api/users` → User summary

---

## 4. Source Projects — Add Service from Git

**Goal:** Clone a Git repository as a new service source project.

**Page:** `/services`

**Steps:**
1. Click **"+ Add Project"** button (top-right)
2. Modal opens with 3 tabs
3. **"From Git"** tab:
   - **Repository URL:** Paste GitHub/GitLab URL (e.g., `https://github.com/user/repo.git`)
   - **Branch:** Enter branch name (default: `main`)
   - **Service Name:** Auto-filled from repo name, editable
   - **Use Global Proxy:** Checkbox (disabled/greyed out if no proxy is configured)
4. Click **"Clone & Create"**
5. Gateway clones the repo into `source_projects/{name}/`
6. Service appears in the projects table

**What you see after:**
- Project row with: folder icon + name, template file tags, generated file tags, Deploy/Delete buttons

**API calls:**
- `POST /api/services` (mode=git)

**Verification:**
- Click the project name → opens file viewer showing cloned files
- Green "new" tag on untracked files

---

## 5. Source Projects — Upload Service Files

**Goal:** Create a service by uploading individual files.

**Page:** `/services`

**Steps:**
1. Click **"+ Add Project"**
2. Select **"Upload Zip"** tab
3. Enter **Service Name**
4. **Option A:** Paste base64-encoded ZIP content
5. **Option B:** Provide files as JSON map (compose, nginx, env, dockerfile)
6. Click **"Create"**

**API calls:**
- `POST /api/services` (mode=upload)

---

## 6. Source Projects — File Editor with Git Diff

**Goal:** View, edit, and review changes to service project files with syntax highlighting.

**Page:** `/services/{name}`

**Steps:**
1. Click a project name in the services table → navigates to detail view
2. **File Tree Browser** (left panel):
   - Directory tree structure
   - `.git/`, `node_modules/`, `dist/`, `.vite/` are filtered out
   - Files show git status tags: **N** (new/untracked), **M** (modified)
   - Generated files highlighted in green with "new" tag
3. Click a file → loads content in Monaco Editor
4. **3 View Modes (toggle via buttons):**
   - **Edit Mode:** Monaco Editor editable, dark theme, YAML/nginx syntax highlighting
   - **Diff Mode:** Monaco DiffEditor (inline, read-only) — shows HEAD vs working tree changes with green/red/yellow highlights
   - **Read-Only Mode:** Monaco Editor read-only (for unmodified files)
5. Make changes → click **"Save"** → sends `PUT /api/services/{name}/files/{file}`
6. Click **"Cancel"** → reverts to original content
7. Click **"Show Changes"** → opens diff view with colored line-by-line comparison

**API calls:**
- `GET /api/services/{name}` → Project details + file list
- `GET /api/services/{name}/files/{file}` → File content
- `PUT /api/services/{name}/files/{file}` → Save changes
- `GET /api/services/{name}/git/status` → Git status
- `GET /api/services/{name}/git/head-file?file=...` → HEAD version
- `GET /api/services/{name}/git/diff?file=...` → Diff output

**Verification:**
- After save, git status shows **M** tag
- Diff view correctly highlights added/removed/modified lines
- **Back to Services button** (navigates back to `/services`)

---

## 7. Source Projects — Convert to Templates

**Goal:** Convert plain `docker-compose.yml` and `nginx.conf` to Jinja2 `.j2` templates.

**Page:** `/services/{name}`

**Steps:**
1. Ensure project has `docker-compose.yml` and/or `nginx.conf` (plain files)
2. Click **"Convert"** button
3. Gateway runs `compose_converter` and `nginx_converter`
4. New `.yml.j2` and `.conf.j2` files appear in the file tree
5. Template tags appear in the project row on `/services` page

**What conversion does:**
- Replaces `container_name` with `{{ container_prefix }}<name>`
- Replaces bind-mount paths with `{{ volumes['key'] }}`
- Replaces network names with `{{ network_name }}`
- Adds header comment documenting template variables

**API calls:**
- `POST /api/services/{name}/convert`

---

## 8. Services — Deploy to User

**Goal:** Deploy a service template for a specific user.

**Page:** `/users`

**Steps:**
1. Click **"Deploy"** button (rocket icon, top-right)
2. **DeployForm Modal** opens:
   - **User Name:** Dropdown of deployable users (from `GET /api/auth/users/deployable`)
   - **Service:** Dropdown of services with templates (from `GET /api/services`)
   - **Label:** 0, 1, or 2 (auto-increment suggestion)
   - **Domain:** Text input (e.g., `snaprovision.com`)
   - **Password:** Password input (for HTTP basic auth)
   - **HTTPS Toggle:** Enables fullchain/privkey path inputs
   - **Volume Mapping:** Form.List — key/value pairs with add/remove buttons
   - **Build Args:** Form.List — key/value pairs with add/remove buttons
   - **Use Global Proxy:** Checkbox (disabled if no proxy configured)
3. Click **"Deploy"** (rocket button)
4. Task ID is displayed → link to Tasks page for monitoring

**API calls:**
- `GET /api/auth/users/deployable` → Available users dropdown
- `GET /api/services` → Available services dropdown
- `POST /api/users/deploy` → Submit deployment

**Verification:**
- New service card appears on `/users` page under the user's name
- Task appears on `/tasks` page with status

---

## 9. Services — Play/Pause/Rebuild/Redeploy/Delete

**Goal:** Manage the lifecycle of deployed services.

**Page:** `/users`

**Per-Service Action Buttons (on each service card):**

| Button | Icon | Action | API Call |
|---|---|---|---|
| **Play/Pause** (toggle) | ▶ play / ⏸ pause | Toggle container state | `POST /users/{u}/{s}/{l}/up` or `POST /users/{u}/{s}/{l}/down` |
| **Rebuild** | Text button | Rebuild with async task | `POST /users/{u}/{s}/{l}/rebuild` |
| **Redeploy** | 🚀 rocket | Redeploy (same config, no_cache) | `POST /users/{u}/{s}/{l}/rebuild` (no_cache=true) |
| **Key** | 🔑 key icon | Change password modal | `PUT /users/{u}/{s}/{l}/password` |
| **Dup** | 📋 copy icon | Duplicate to another user | `POST /users/deploy` (same config, new user) |
| **Delete** | 🗑 trash icon | Remove service (with confirmation) | `DELETE /users/{u}/{s}/{l}` |

> **Note**: Up and Down operations are delegated to provision-api. The Play/Pause button auto-detects container state (running → shows pause icon, stopped → shows play icon).

**Service Card Expansion (click to expand):**
- Status badge (Running / N up, M down)
- Per-container status tags
- URL (clickable) with **Test** button
- Deployment file links (plain text with directory context)
- Volume info
- SSL file info (if HTTPS enabled)

**API calls for expansion:**
- `POST /users/{u}/{s}/{l}/test-curl` → Test URL connectivity
- `GET /users/{u}/{s}/{l}/containers/{c}/logs?tail=100` → Container logs (proxied to provision-api)

**Verification:**
- Play/Pause instantly changes container status
- Rebuild creates a task visible on Tasks page
- Delete removes the service card after confirmation

---

## 10. Services — Clone All Between Users

**Goal:** Copy all services from one user to another.

**Page:** `/users`

**Steps:**
1. Find the source user section (e.g., "alice — 2 services")
2. Click **"Clone All"** button (swap icon)
3. **Clone Modal** opens:
   - **Target User:** Text input (e.g., "bob")
   - **Domain:** Pre-filled
   - **Password:** Set for all cloned services
4. Click **"Clone All"**
5. Multiple async tasks created (one per service)
6. Track progress on Tasks page

**API calls:**
- `POST /api/users/clone` → Creates all clone tasks

**Verification:**
- Bob appears on `/users` page with the same services as Alice
- Volume paths auto-remapped (alice → bob)
- Domain names auto-adjusted

---

## 11. Services — Change Password & Test Connectivity

**Goal:** Update HTTP basic auth password and test service URL.

**Page:** `/users`

### Change Password
1. Expand a service card
2. Click **🔑 Key** button
3. Modal: Enter new password
4. Click **"Update"**
5. Gateway re-hashes password, rewrites `.htpasswd`, reloads nginx

### Test Connectivity
1. Expand a service card
2. Click **"Test"** link next to the URL
3. Panel shows:
   - HTTP status code (200 ✅ / error ❌)
   - Response headers
   - Body preview (first 500 chars)
   - Total time (ms)

**API calls:**
- `PUT /users/{u}/{s}/{l}/password` → Password update
- `POST /users/{u}/{s}/{l}/test-curl` → Connectivity test

---

## 12. Tasks — Monitor & View Logs

**Goal:** Track async tasks and view real-time build logs.

**Page:** `/tasks`

**What you see:**
1. **Task Table:** Columns — ID, Type, Target, Status (color-coded), Updated, Elapsed, Created, Actions
2. **Status colors:** pending=default, running=processing (blue spinner), completed=success (green), failed=error (red), cancelled=warning (orange)
3. **Auto-polling:** Table refreshes every 5 seconds
4. **Pagination:** Page size selector (default 20/page)

### View Logs
1. Click **"Logs"** button (eye icon) on a task row
2. **Log Drawer** opens from the right:
   - Terminal-style dark background
   - Live SSE stream of build output
   - "Live" tag shows when connected
   - Auto-scrolls to latest lines
3. Close drawer to stop streaming

### Cancel/Delete Task
1. Click **🗑 Delete** icon → confirmation popup
2. Task cancelled (if pending/running) or removed (if completed/failed)

**API calls:**
- `GET /api/tasks` → Task list (every 5s)
- `GET /api/tasks/{id}/log` (SSE) → Live log streaming
- `DELETE /api/tasks/{id}` → Cancel/delete

---

## 13. Settings — LLM Configuration

**Goal:** Configure AI provider for config generation and troubleshooting.

**Page:** `/settings` (admin only)

**What you see:**
1. **LLM Configuration Panel:**
   - Existing configs shown as cards with: mode icon, model name, base URL, ACTIVE badge, delete button
   - **Add Config Form:**
     - **Mode:** Dropdown — Bring Your Own Key / Local Agent
     - **API Base URL:** Pre-filled placeholder (`https://api.deepseek.com/v1`)
     - **Model Name:** Pre-filled placeholder (`deepseek-chat`)
     - **API Key:** Password input with show/hide toggle
     - **Agent URL:** For local mode (`http://localhost:11434/v1`)
     - **Agent Model:** For local mode (`llama3.1:8b`)
     - **System Prompt:** Textarea for custom system prompt
   - **Add Config** button (save icon)
   - **Test Active** button (robot icon) → Tests connection, shows success/failure alert

**API calls:**
- `GET /api/llm/configs` → List all configs
- `POST /api/llm/configs` → Add new config
- `PUT /api/llm/configs/{id}/activate` → Activate (deactivates others)
- `DELETE /api/llm/configs/{id}` → Delete
- `POST /api/llm/test` → Test connection

**Verification:**
- After adding config, card appears with model name and ACTIVE/INACTIVE badge
- Test Active shows latency and response preview

---

## 14. Settings — Global Proxy

**Goal:** Configure HTTP/HTTPS proxy for git clones and Docker builds.

**Page:** `/settings` (admin only)

**What you see:**
1. **Global Proxy Panel:**
   - Existing proxy configs shown as cards with: name, protocol, host:port, reachability status (🟢 reachable / 🔴 unreachable / ⚪ not checked), activate toggle, delete button
   - **Add Proxy Form:**
     - **Name:** Display name
     - **Protocol:** HTTP / HTTPS / SOCKS5 dropdown
     - **Host:** Proxy hostname/IP
     - **Port:** Proxy port (default 8080)
     - **Username:** Optional
     - **Password:** Optional (masked)
   - Auto-tests reachability after save
   - Activate toggle only works if proxy is reachable

**API calls:**
- `GET /api/system/proxy` → List proxies
- `POST /api/system/proxy` → Add proxy (auto-tests)
- `PUT /api/system/proxy/{id}/activate` → Activate (reachability-gated)
- `DELETE /api/system/proxy/{id}` → Delete
- `POST /api/system/proxy/test` → Manual recheck

**Verification:**
- After adding, reachability status updates (🟢 or 🔴)
- Activate toggle only enables when reachable
- Deploy form's "Use Global Proxy" checkbox enables when a proxy is active

---

## 15. Settings — Special Functional Users

**Goal:** Configure global special users list (shared, public, internal).

**Page:** `/settings` (admin only)

**What you see:**
1. **Special Functional Users Panel:**
   - Textarea with comma-separated usernames
   - Current value: `shared, public, internal`
   - **Save** button

**API calls:**
- `GET /api/system/config?key=special_users` → Load current value
- `PUT /api/system/config?key=special_users` → Save new value

**Effect:**
- These users appear in the deployable users dropdown
- End-users can be assigned access to specific special users

---

## 16. Audit — Query & Export

**Goal:** Search and export the audit trail.

**Page:** `/audit`

**What you see:**
1. **Filter Bar:**
   - **Action Dropdown:** Filter by action type (register, remove, rebuild, deploy, clone, config_edit, admin_create, password_change, llm_config, proxy_config, reconcile)
   - **Target User Input:** Text filter
   - **Date Range Picker:** Start and end date
   - **Clear Filters** button
2. **Audit Table:** Columns — Time, Admin, Action (color-coded tag), Target User, Target Service, Status (✓/✗), Detail (JSON expandable)
3. **CSV Export** button → Downloads filtered audit log as CSV file
4. **Auto-refresh:** 30-second polling

**API calls:**
- `GET /api/audit?action=...&target_user=...&from=...&to=...&limit=50&offset=0` → Query
- CSV export is client-side (generated from table data)

---

## 17. User Management — Register, Approve, Assign Roles

**Goal:** Manage end-user accounts with role-based access control.

**Page:** `/users/manage`

**What you see:**
1. **User Table:** Columns — Username, Role (viewer/special), Status (Approved/Pending), Allowed Special Users (purple tags), Actions
2. **Register User Button** (user-add icon)
3. **Special Functional Users Configuration** (collapsible, shows global special users list)

### Register User
1. Click **"Register User"**
2. Modal: Username, Password, Role (viewer/special)
3. Click **"Register"** → `POST /api/auth/users/register`
4. User appears in table with Status: **Pending**

### Approve User
1. Find pending user in table
2. Click **"Approve"** button
3. Status changes to **Approved**
4. User can now login

### Assign Special Users
1. Click **"Special"** button (setting icon) on a user row
2. Modal shows toggleable tags for each global special user (from Settings)
3. Toggle which special users this end-user can access
4. Click **"Save"** → `PUT /api/auth/users/{id}`

### Delete User
1. Click **🗑 Close** button on a user row
2. Confirmation dialog
3. User removed

**API calls:**
- `GET /api/auth/users` → List users
- `POST /api/auth/users/register` → Register
- `PUT /api/auth/users/{id}/approve` → Approve
- `PUT /api/auth/users/{id}` → Update (role, special users)
- `DELETE /api/auth/users/{id}` → Delete

**Role-Based Access Control:**
- **viewer:** Can see Dashboard, Services, Tasks, Audit only. Cannot perform mutating actions.
- **special:** Can deploy/manage only their assigned special users' services
- **admin:** Full access to all pages and actions

---

## 18. Troubleshoot Chat

**Goal:** Get AI-assisted troubleshooting for service issues.

**Access:** Click **"?"** (question-circle) icon in the header (admin only)

**Steps:**
1. Click the **"?"** icon in the top header bar
2. **Chat Modal** opens with message input
3. Type a question (e.g., "Why is siyuan-mcp for alice down?")
4. Press Enter or click Send
5. LLM responds with diagnostic advice
6. Chat history is maintained in the modal (cleared on close)

**API calls:**
- `POST /api/llm/generate` (type=troubleshoot)

**Precondition:** LLM must be configured and active (Settings page)

---

## Appendix: Page Summary

| Page | Route | Access | Key Features |
|---|---|---|---|
| Login | `/login` | Public | Email+password, register link |
| Setup | `/setup` | First-run only | Admin account creation |
| Dashboard | `/dashboard` | Authenticated | Stats, gauges, system components, user cards, reconcile |
| Source Projects | `/services` | Authenticated | Project table, add (git/upload/template), file editor, git diff, convert |
| Services | `/users` | Authenticated | Per-user service cards, deploy, up/down, rebuild, clone, password, test |
| Tasks | `/tasks` | Authenticated | Task table, SSE log viewer, cancel/delete |
| Settings | `/settings` | Admin | LLM config, proxy config, special users |
| Audit | `/audit` | Authenticated | Filterable audit table, CSV export |
| User Management | `/users/manage` | Admin | Register, approve, role assignment, special users |

## Appendix: Known UI Behaviors

1. **Sidebar highlight:** Active menu item is highlighted; "Users" correctly highlights on `/users/manage`
2. **Sidebar collapse:** Menu-fold/menu-unfold button toggles sidebar width (220px ↔ 80px)
3. **User dropdown:** Click admin email → Change Password / Logout options
4. **Non-admin restrictions:** Viewer role sees only Dashboard, Services, Tasks, Audit in sidebar
5. **Auto-polling:** Dashboard (10s), Tasks (5s), Audit (30s)
6. **Loading states:** Spin indicators shown while data loads; "Loading..." text for gauges
7. **Empty states:** Appropriate messages when no data (e.g., "No services yet")
8. **Error handling:** Toast notifications for API errors; 401 auto-redirects to login
9. **Task notifications:** Browser notifications + toasts for completed/failed tasks (15s polling)
