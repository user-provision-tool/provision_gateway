#!/usr/bin/env bash
# Boundary guard (R7) — the parts that need the sibling worktree and git.
#
# 1. `_users_provision` has no uncommitted change (rule 4: the -api untouched).
# 2. The supported clients (dashboard source, thin CLI) carry no -api address.
# 3. The gateway migrations write no `registry.*` object.
#
# The pytest suite (`tests/test_boundary_guard.py`) checks the same static
# invariants; this script adds the cross-repo check the E2E's step 10 reports.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GW_ROOT="$(cd "${HERE}/.." && pwd)"
PROJECT_ROOT="$(cd "${GW_ROOT}/.." && pwd)"
# `_users_provision` is a SIBLING of the `_provision_gateway` checkout, at the
# workspace root (two levels above this repo root).
USERS_REPO="$(cd "${GW_ROOT}/../.." && pwd)/_users_provision"
DASHBOARD_SRC="${PROJECT_ROOT}/provision-dashboard/src"

fail() { printf 'BOUNDARY GUARD FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'BOUNDARY GUARD OK: %s\n' "$*"; }

# 1. The -api is untouched.
if [ -d "${USERS_REPO}/.git" ] || [ -f "${USERS_REPO}/.git" ]; then
  if ! git -C "${USERS_REPO}" diff --quiet HEAD --; then
    fail "_users_provision has uncommitted changes (rule 4)"
  fi
  ok "_users_provision is unchanged"
else
  printf 'BOUNDARY GUARD WARN: %s is not a git worktree; skipped\n' "${USERS_REPO}" >&2
fi

# 2. No -api address in the supported clients. Built at run time so the literal
# never appears in this source.
API_PORT="$(printf '%s' '88')$(printf '%s' '75')"
API_HOST="$(printf '%s' 'subnet-acl-provision-api')"
if [ -d "${DASHBOARD_SRC}" ]; then
  if grep -RInE ":${API_PORT}\b|${API_HOST}\b" "${DASHBOARD_SRC}" \
      --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' \
      --include='*.json' --include='*.html' >/dev/null 2>&1; then
    fail "the dashboard source names the -api"
  fi
  ok "the dashboard source names no -api address"
fi
if [ -f "${GW_ROOT}/scripts/thin_cli_check.sh" ]; then
  if grep -Fq -e ":${API_PORT}" -e "${API_HOST}" \
      "${GW_ROOT}/scripts/thin_cli_check.sh"; then
    fail "the thin CLI names the -api"
  fi
  ok "the thin CLI names no -api address"
fi

# 3. No registry writes in the gateway migrations.
if grep -REIn "(INSERT[[:space:]]+INTO|UPDATE|DELETE[[:space:]]+FROM|ALTER[[:space:]]+TABLE|CREATE[[:space:]]+TABLE|DROP[[:space:]]+TABLE|TRUNCATE)[[:space:]]+registry\." \
    "${GW_ROOT}/app/db/migrations" >/dev/null 2>&1; then
  fail "a gateway migration writes registry.*"
fi
ok "gateway migrations write gateway.* only"

ok "all boundary checks passed"
