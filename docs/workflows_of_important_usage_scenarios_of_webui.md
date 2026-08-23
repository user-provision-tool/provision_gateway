# Provision Gateway — Workflows of Important Usage Scenarios (WebUI)

> **Version**: 3.1
> **Date**: 2026-08-22 (updated — v4 Service-ACL enforcement: cookie-only login (provision_token, token_type=cookie), two-token/JWT-body login model removed, /go/ issues a 30s exchange code with no JWT in URL; prior: API Keys page, Alert page, viewer role-gating (AdminRoute), /go/{hostname} service access, multi-recipe DeployForm, two-token login, Dashboard Subnet Pool card)
> **Purpose**: Step-by-step WebUI workflows verified against the actual dashboard at `http://localhost:8771`.

---

## Table of Contents

1. [First-Time Setup Wizard](#1-first-time-setup-wizard)
2. [Login & Registration](#2-login--registration)
3. [Dashboard — System Overview](#3-dashboard--system-overview)
4. [Source Projects — Add Service from Git](#4-source-projects--add-service-from-git)
5. [Source Projects — Upload Service Files](#5-source-projects--upload-service-files)
6. [Source Projects — Add Service from Template](#6-source-projects--add-service-from-template)
7. [Source Projects — File Editor with Git Diff](#7-source-projects--file-editor-with-git-diff)
8. [Source Projects — Convert to Templates](#8-source-projects--convert-to-templates)
9. [Services — Deploy to User](#9-services--deploy-to-user)
10. [Services — Play/Pause/Rebuild/Redeploy/Delete](#10-services--playpauserebuildredeploydelete)
11. [Services — Clone All Between Users](#11-services--clone-all-between-users)
12. [Services — Change Password & Test Connectivity](#12-services--change-password--test-connectivity)
13. [Tasks — Monitor & View Logs](#13-tasks--monitor--view-logs)
14. [Settings — LLM Configuration](#14-settings--llm-configuration)
15. [Settings — Global Proxy](#15-settings--global-proxy)
16. [Settings — Special Functional Users](#16-settings--special-functional-users)
17. [Audit — Query & Export](#17-audit--query--export)
18. [User Management — Register, Approve, Assign Roles](#18-user-management--register-approve-assign-roles)
19. [Troubleshoot Chat](#19-troubleshoot-chat)
20. [API Keys — Create, Copy, List, Revoke](#20-api-keys--create-copy-list-revoke)
21. [Alert Page — Access Denied & Token Expired](#21-alert-page--access-denied--token-expired)
22. [Services — Open a Service via /go Redirect](#22-services--open-a-service-via-go-redirect)

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
5. v4: the server sets a single `provision_token` httponly cookie (1-week, token_type=cookie). The Bearer `access_token`/`refresh_token` localStorage model and the legacy `gateway_token` cookie were **removed in v4**

### Register New Account Flow
1. On login page, click **"Register new account"**
2. Fill in: Username, Email, Password, Confirm Password
3. Click **"Register"**
4. Message: "Registration submitted! Please wait for admin approval."
5. Admin must approve before the new user can login

**API calls:**
- `POST /api/auth/login` → provision_token cookie (v4; no Bearer body tokens)
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
6. **Subnet Pool Card:** Per-pool usage panels (CIDR, used %, progress bar, used/total slots) from `GET /api/system/subnet-pool`
7. **Welcome Card:** Greeting with admin email
8. **Live Indicator:** Spinning "Live" tag when 10s polling is active

**Actions:**
- Click **"Refresh"** → Manual refresh of all data
- Click **"Reconcile"** → Triggers nginx upstream reconciliation
- Click user card → Navigates to `/users` filtered to that user

**API calls (every 10s):**
- `GET /api/system/status` → System health + counts
- `GET /api/system/stats` → Container stats
- `GET /api/system/proxy` → Proxy status
- `GET /api/users` → User summary
- `GET /api/system/subnet-pool` → Subnet pool usage (Dashboard Subnet Pool card)

---

## 4. Source Projects — Add Service from Git

**Goal:** Clone a Git repository as a new service source project.

**Page:** `/services`

**Steps:**
1. Click **"+ Add Project"** button (top-right)
2. Modal opens with 2 tabs: From Git, Upload Zip (iter-1, GAP-1 — "From Template" tab removed)
3. **"From Git"** tab:
   - **Repository URL:** Paste GitHub/GitLab URL (e.g., `https://github.com/user/repo.git`)
   - **Branch:** Enter branch name (default: `main`)
   - **Service Name:** Auto-filled from repo name, editable
   - **Use Global Proxy:** Checkbox (disabled/greyed out if no proxy is configured)
4. Click **"Clone & Create"**
5. Gateway clones the repo into `source_projects/{name}/`
6. Service appears in the projects table

**What you see after:**
- Project row with: folder icon + name, template file tags (only Dockerfile, docker-compose*, *.nginx.conf, *.conf, .env, .env.example — G2 template classification), generated file tags, Deploy/Delete buttons
- `template_files` and `generated_files` fields distinguish between Jinja2 templates and LLM-generated files

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
4. **Option A:** Select a local `.zip` file via the file picker (drag-and-drop / browse — `Upload.Dragger`)
5. **Option B:** Provide files as JSON map (compose, nginx, env, dockerfile)
6. Click **"Create"**

**API calls:**
- `POST /api/services` (mode=upload)

---

## 6. Source Projects — Add Service from Template

**Goal:** Create a service project from a pre-built template in the service_templates database table.

> **IMPORTANT (iter-1, GAP-1):** The "From Template" tab has been REMOVED from the Add Source Project modal (UI now offers From Git + Upload Zip only; orphan `AddServiceModal.tsx` deleted). This flow is now **API-only** — the backend `mode: template` path and `GET /api/services/templates` are retained.

**Page:** `/services`

**Steps (API-only):**
1. Load available templates: `GET /api/services/templates`
2. Create the project: `POST /api/services` with `{"mode": "template", "template_id": N, "name": "..."}`
3. The system creates the project using the template's compose_j2, nginx_j2, env_template, and dockerfile content
4. New project appears in the services table

**API calls:**
- `GET /api/services/templates` → Load available templates
- `POST /api/services` (mode=template, template_id=N) → Create project

---

## 7. Source Projects — File Editor with Git Diff

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

## 8. Source Projects — Convert to Templates

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

## 9. Services — Deploy to User

**Goal:** Deploy a service template for a specific user.

**Page:** `/users`

**Steps:**
1. Click **"Deploy"** button (rocket icon, top-right)
2. **DeployForm Modal** opens:
   - **User Name:** Dropdown of deployable users (from `GET /api/auth/users/deployable`)
   - **Service:** Dropdown of source projects (from `GET /api/services`). For multi-recipe projects, each recipe appears as `name @ recipe_path` (option value `name@@recipe_path`); selecting one triggers `GET /api/services/{name}/check-missing-files?recipe_path=...` and deploys with `project_root = {base}/{recipe_path}` (template paths scoped to the recipe subdirectory)
   - **Label:** Auto-computed from existing instances (display-only disabled input, fetched via `GET /api/users/{user}/{service}/next-label`)
   - **Domain:** Text input (e.g., `snaprovision.com`)
   - **Password:** Password input (for HTTP basic auth)
   - **HTTPS Toggle:** Enables fullchain/privkey path inputs
   - **Volume Mapping:** Form.List — key/value pairs with add/remove buttons
   - **Build Args:** Form.List — key/value pairs with add/remove buttons
   - **Use Global Proxy:** Checkbox (disabled if no proxy configured)
3. **Template completion flow**: If service is missing essential files (docker-compose, nginx.conf, .env, Dockerfile), a warning alert shows with "Auto Templates Completion" checkbox. The **"Deploy"** button is disabled when missing files exist and no LLM-generated files are available:
   - **Auto mode (checked, default)**: Click "Generate Missing Files via LLM" → LLM generates files → auto-submits deploy after 500ms delay
   - **Manual mode (unchecked)**: Click "Generate with LLM" → generated files appear as clickable tags for review in Monaco editor → click "Deploy" saves files to disk first then submits
   - Generated file tags are color-coded (blue) and clickable to open full Monaco editor for review
   - If LLM is not configured, missing files must be uploaded manually in the source project before deploy is allowed
4. Click **"Deploy"** (rocket button)
5. If successful, task ID is displayed → link to Tasks page for monitoring

**API calls:**
- `GET /api/auth/users/deployable` → Available users dropdown
- `GET /api/services` → Available services dropdown
- `GET /api/users/{user}/{service}/next-label` → Auto-compute label on user/service selection
- `GET /api/services/{name}/check-missing-files` → Check for missing essential files (auto on mount)
- `POST /api/llm/generate` → Generate missing file content via LLM
- `POST /api/services/save-generated` → Save generated files to disk (always before deploy — G12)
- `POST /api/users/deploy` → Submit deployment

**Verification:**
- New service card appears on `/users` page under the user's name
- Task appears on `/tasks` page with status
- Task notification appears (global 2s polling + browser Notification API)

---

## 10. Services — Play/Pause/Rebuild/Redeploy/Delete

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
- URL (clickable → `/go/{service}-{user}-{label}.localhost`) with **Test** button
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

## 11. Services — Clone All Between Users

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

## 12. Services — Change Password & Test Connectivity

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

## 13. Tasks — Monitor & View Logs

**Goal:** Track async tasks and view real-time build logs.

**Page:** `/tasks`

**Global Task Notification** (active on all pages):
- The AppLayout header polls `GET /api/tasks` every 2 seconds (G3)
- Detects status transitions: pending → completed or pending → failed
- Shows antd notification toast + browser Notification API popup
- Deduplication via `notifiedRef` — each task transition notified only once
- 2-second time filter on `updated_at` prevents stale-task notifications on page load
- Clicking the notification does NOT auto-navigate (avoids disrupting current page)

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

## 14. Settings — LLM Configuration

**Goal:** Configure AI provider for config generation and troubleshooting.

**Page:** `/settings` (admin only)

**What you see:**
1. **LLM Configuration Panel:**
   - Existing configs shown as cards with: mode icon, model name, base URL, ACTIVE badge, delete button
   - **Add Config Form (BYOK-only, GAP-2 iter-1):**
     - **Mode:** Fixed to "Bring Your Own Key (OpenAI-compatible)" — the dropdown is disabled; Local Agent and Provision Agent are FUTURE features and are no longer selectable in the UI
     - **API Base URL:** Pre-filled placeholder (`https://api.deepseek.com/v1`)
     - **Model Name:** Pre-filled placeholder (`deepseek-chat`)
     - **API Key:** Password input with show/hide toggle
     - Note: The **Agent URL / Agent Model / System Prompt** fields have been REMOVED from the BYOK panel (Req R13 — Settings is BYOK-only). Local-agent fields are deferred at the API level too (GAP-2, iter-1): `mode='local_agent'` is normalized to `byok`, and `agent_url`/`agent_model` are never persisted. The SettingsPage.tsx form exposes only Mode / API Base URL / Model Name / API Key.
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

## 15. Settings — Global Proxy

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

## 16. Settings — Special Functional Users

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

## 17. Audit — Query & Export

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

## 18. User Management — Register, Approve, Assign Roles

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
- **viewer:** Sees only **Services** and **API Keys** in the sidebar. Dashboard, Source Projects, Tasks, Settings, Audit, Users, and SSL are wrapped in `AdminRoute` — a viewer hitting one of those URLs is redirected to `/users`. Viewers can start/stop/test their own (and granted special-user) services but cannot perform admin mutating actions (deploy, clone, rebuild, delete, password, etc.).
- **special:** Blocked at login ("Special users cannot access the dashboard") — they authenticate for service access via API key / provision token only.
- **admin:** Full access to all pages and actions.

---

## 19. Troubleshoot Chat

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

## 20. API Keys — Create, Copy, List, Revoke

**Goal:** Manage long-lived API keys (provision tokens) for programmatic / service access.

**Page:** `/api-keys` (accessible to admins AND viewers)

**Steps:**
1. Click **"Create Key"** (top-right)
2. **Create Key Modal:**
   - **Label:** Required (e.g. "Production", "CI/CD")
   - **User ID (admins only):** Optional numeric user id; blank creates the key for yourself
3. Click **"Create"** → `POST /api/auth/keys`
4. **One-time token display:** the modal switches to a success state showing the raw token in a read-only textarea with warning *"Copy this token now — it will not be shown again."* Click **"Copy Token"** to copy it to the clipboard
5. **Key table:**
   - **Admins:** see all users' keys, including a **User ID** column
   - **Viewers:** see only their own keys (no User ID column)
   - Columns: ID, Label, Created, Expires, Status (Active/Revoked), Actions
6. **Revoke:** click **"Revoke"** on an active key → Popconfirm → `DELETE /api/auth/keys/{id}` → tag turns red "Revoked"

**API calls:**
- `POST /api/auth/keys` → Create (returns `token` exactly once)
- `GET /api/auth/keys` → List (admin: all; viewer: own)
- `DELETE /api/auth/keys/{id}` → Revoke

**Effect:**
- Each key is a provision token embedded with `api_key_id`, used by provision-nginx for service access; revoking a key invalidates its token.

---

## 21. Alert Page — Access Denied & Token Expired

**Goal:** Display clear error screens when the subnet-acl nginx ACL layer blocks or expires a user.

**Page:** `/alert` (public route)

### 21.1 `?reason=acl_denied` — Access Denied
1. subnet-acl nginx `@auth_403` (browser request to a service the user has no access to) redirects to `http://{dashboard}/alert?reason=acl_denied&service={host}`
2. Alert page reads `reason` and `service` from the URL
3. Shows **"Access Denied"** — *"You do not have access to {service}. Contact your administrator if you believe this is an error."*
4. **"Back to Dashboard"** button

### 21.2 `?reason=token_expired` — API Token Expired
1. Reached by direct URL `/alert?reason=token_expired`
2. Shows **"API Token Expired"** — *"Your API token has expired. Please log in again to get a new token."*
3. **"Go to Login"** button

> **Note:** The nginx `@auth_401` redirect currently sends expired-token browsers to `/login` (Option B, acl-enforcement-design-v2.md §12); the `token_expired` alert is only reachable directly.

**API calls:** none (reads query params only)

---

## 22. Services — Open a Service via /go Redirect

**Goal:** Open a deployed service from the dashboard in a new tab, using the gateway as a token-exchange hop.

**Page:** `/users` → expand a service card

**Steps:**
1. Expand a service card on the Services page
2. Click the URL link — it points to `/go/{service}-{user}-{label}.localhost` (hostname resolved from the registry)
3. Browser navigates to the gateway `GET /api/auth/go/{hostname}`:
   - Validates the `provision_token` session cookie (v4; the legacy `gateway_token` cookie / Bearer model was removed)
   - Looks up the service by hostname in the HostnameIndex (404 if unknown)
   - For viewers: ACL check — target user must equal the viewer's own user or be in `allowed_special_users` (403 otherwise)
4. Gateway issues a **30s HMAC exchange code** + `Location` header — **no JWT in any URL** (v4 F7)
5. The service-domain `_set_token` is a plain variable proxy to `/api/auth/exchange`, which swaps the code for the `provision_token` cookie via `302`+`Set-Cookie` and redirects to `/` → service loads silently
6. The `Auth` tag on the card indicates the service has HTTP basic auth; the cookie exchange bypasses the need to type credentials

**Why:** the login cookie is host-scoped (`localhost:8771`) and is NOT sent to the service hostname, so a cookie exchange through `/go` is required.

**API calls:**
- `GET /api/auth/go/{hostname}` → 302 to `{host}/_set_token?code={30s HMAC code}&redirect=/` (never a live bearer JWT in the URL)

---

## Appendix: Page Summary

| Page | Route | Access | Key Features |
|---|---|---|---|
| Login | `/login` | Public | Email+password, register link |
| Setup | `/setup` | First-run only | Admin account creation |
| Dashboard | `/dashboard` | Admin (AdminRoute) | Stats, gauges, system components, subnet pool, user cards, reconcile |
| Source Projects | `/services` | Admin (AdminRoute) | Project table, add (git/upload — template creation is API-only, GAP-1), file editor, git diff, convert |
| Services | `/users` | Authenticated (admins + viewers) | Per-user service cards, `/go/{hostname}` access links, deploy, up/down, rebuild, clone, password, test |
| Tasks | `/tasks` | Admin (AdminRoute) | Task table, SSE log viewer, cancel/delete |
| Settings | `/settings` | Admin (AdminRoute) | LLM config, proxy config, special users |
| Audit | `/audit` | Admin (AdminRoute) | Filterable audit table, CSV export |
| User Management | `/users/manage` | Admin (AdminRoute) | Register, approve, role assignment, special users |
| SSL Certs | `/ssl` | Admin (AdminRoute) | Upload & manage certificates |
| API Keys | `/api-keys` | Authenticated (admins + viewers) | Create key (label; admin can set User ID), one-time token + Copy, list (admin sees all + User ID column; viewer sees own), Revoke |
| Alert | `/alert` | Public | `acl_denied` / `token_expired` variants |

## Appendix: Known UI Behaviors

1. **Sidebar highlight:** Active menu item is highlighted; "Users" correctly highlights on `/users/manage`
2. **Sidebar collapse:** Menu-fold/menu-unfold button toggles sidebar width (220px ↔ 80px)
3. **User dropdown:** Click admin email → Change Password / Logout options
4. **Non-admin restrictions:** Viewer sees only Services + API Keys in the sidebar; admin-only routes (Dashboard, Source Projects, Tasks, Settings, Audit, Users, SSL) are guarded by `AdminRoute` and redirect a viewer to `/users`
5. **Auto-polling:** Dashboard (10s), Tasks (5s), Audit (30s)
6. **Loading states:** Spin indicators shown while data loads; "Loading..." text for gauges
7. **Empty states:** Appropriate messages when no data (e.g., "No services yet")
8. **Error handling:** Toast notifications for API errors; 401 auto-redirects to login
9. **Task notifications:** Browser notifications + toasts for completed/failed tasks (2s polling)
