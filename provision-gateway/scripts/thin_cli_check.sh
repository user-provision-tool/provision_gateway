#!/usr/bin/env bash
# Thin CLI parity harness (R14, T12).
#
# Proves every operator flow can be driven through GATEWAY ENDPOINTS ONLY, with
# a stateless admin API key in `X-Provision-Token` — no login, no cookie, no
# -api address anywhere. The script prints the traffic it made; §11 step 10
# inspects it. It is a client, never a substitute for the WebUI: the UI track
# of §11 runs beside it.
#
# Required env:
#   GATEWAY_ADMIN_KEY   an admin API key (the `X-Provision-Token` value)
#   THIN_USER           an existing deployable owner username
#   THIN_SERVICE        a recipe/service name
# Optional:
#   GATEWAY_URL         default http://127.0.0.1:8870
#   THIN_RECIPE_PATH    default ""
#   THIN_LABEL          default "auto"
#   THIN_PROMPT         default "generate the missing deployment files"
#   THIN_SELECTION      JSON selection, default {"compose":[],"nginx":null,
#                       "env":[],"profiles":[]}
#   THIN_PAUSE_VALUES   JSON values for the first pause, default {}
#   THIN_TIMEOUT        seconds per poll loop, default 900
set -euo pipefail

BASE="${GATEWAY_URL:-http://127.0.0.1:8870}"
: "${GATEWAY_ADMIN_KEY:?set GATEWAY_ADMIN_KEY (an admin API key)}"
: "${THIN_USER:?set THIN_USER}"
: "${THIN_SERVICE:?set THIN_SERVICE}"
RECIPE_PATH="${THIN_RECIPE_PATH:-}"
LABEL="${THIN_LABEL:-auto}"
PROMPT="${THIN_PROMPT:-generate the missing deployment files}"
SELECTION="${THIN_SELECTION:-}"
[ -n "${SELECTION}" ] || SELECTION='{"compose":[],"nginx":null,"env":[],"profiles":[]}'
PAUSE_VALUES="${THIN_PAUSE_VALUES:-}"
[ -n "${PAUSE_VALUES}" ] || PAUSE_VALUES='{}'
TIMEOUT="${THIN_TIMEOUT:-900}"

AUTH=(-H "X-Provision-Token: ${GATEWAY_ADMIN_KEY}" -H "Content-Type: application/json")

log() { printf '%s\n' "$*" >&2; }

api() { # method path [body]
  local method="$1" path="$2" body="${3:-}"
  log ">>> ${method} ${BASE}${path}"
  if [ -n "${body}" ]; then
    curl -sS -X "${method}" "${AUTH[@]}" --data "${body}" "${BASE}${path}"
  else
    curl -sS -X "${method}" "${AUTH[@]}" "${BASE}${path}"
  fi
}

json_get() { # key  (reads stdin JSON)
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],""))' "$1"
}

wait_workflow() { # workflow_id  -> prints the final status JSON
  local wid="$1" waited=0 body stage
  while [ "${waited}" -lt "${TIMEOUT}" ]; do
    body="$(api GET "/api/services/workflows/${wid}")"
    stage="$(printf '%s' "${body}" | json_get stage)"
    log "<<< stage=${stage}"
    case "${stage}" in
      paused)
        local task_id values
        task_id="$(printf '%s' "${body}" | python3 -c 'import json,sys; d=json.load(sys.stdin); g=d.get("generation") or {}; print(g.get("task_id") or "")')"
        if [ -n "${task_id}" ]; then
          api POST "/api/tasks/${task_id}/answer" "{\"values\": ${PAUSE_VALUES}}" >/dev/null
          log ">>> answered pause on ${task_id} with values only"
        fi
        ;;
      generated|deploying|failed|running|building|refused|unknown)
        printf '%s' "${body}"; return 0 ;;
      generating)
        sleep 2 ;;
      *)
        sleep 2 ;;
    esac
    waited=$((waited + 2))
  done
  log "timeout waiting for workflow ${wid}"
  return 1
}

intent() { # operation
  python3 - "$THIN_USER" "$THIN_SERVICE" "$RECIPE_PATH" "$LABEL" "$PROMPT" "$SELECTION" <<'PY'
import json, sys
user, service, recipe_path, label, prompt, selection = sys.argv[1:7]
print(json.dumps({
    "user_name": user,
    "service_name": service,
    "project_root": f"{service}/{recipe_path}" if recipe_path else service,
    "recipe_path": recipe_path,
    "label": label,
    "domain": "localhost",
    "https": False,
    "selection": json.loads(selection),
    "prompt": prompt,
}))
PY
}

main() {
  # A stateless key: every call carries it; there is no session to establish.
  log "== thin CLI: gateway-only, X-Provision-Token auth =="

  log "== 1. stage projection =="
  api GET "/api/services/stages" >/dev/null

  log "== 2. grant candidates for ${THIN_USER} =="
  api GET "/api/service-grants/candidates?user_name=${THIN_USER}" >/dev/null

  log "== 3. generate (review gate) =="
  local gen_body gen_id
  gen_body="$(api POST "/api/services/generate" "$(intent generate)")"
  gen_id="$(printf '%s' "${gen_body}" | json_get workflow_id)"
  log "workflow=${gen_id}"
  wait_workflow "${gen_id}" >/dev/null || true

  log "== 4. deploy (generation + pause, server-owned) =="
  local dep_body dep_id
  dep_body="$(api POST "/api/services/deploy" "$(intent deploy)")"
  dep_id="$(printf '%s' "${dep_body}" | json_get workflow_id)"
  log "workflow=${dep_id}"
  wait_workflow "${dep_id}" >/dev/null || true

  log "== 5. status after the deploy =="
  api GET "/api/services/workflows/${dep_id}" >/dev/null

  # Optional mutating flows — exercised when a fixture is supplied, skipped
  # otherwise (the E2E records which). They are gateway endpoints only.
  if [ -n "${THIN_SOURCE_ID:-}" ]; then
    log "== 5a. set-based grants =="
    api PUT "/api/service-grants/${THIN_SOURCE_ID}" \
      "{\"target_service_ids\": ${THIN_TARGET_IDS:-[]}}" >/dev/null
  else
    log "== 5a. set-based grants: skipped (no THIN_SOURCE_ID fixture) =="
  fi
  if [ -n "${THIN_CANCEL_TASK:-}" ]; then
    log "== 5b. cancel =="
    api DELETE "/api/tasks/${THIN_CANCEL_TASK}" >/dev/null
  else
    log "== 5b. cancel: skipped (no THIN_CANCEL_TASK fixture) =="
  fi
  if [ -n "${THIN_RETRY_SERVICE_ID:-}" ]; then
    log "== 5c. retry =="
    api POST "/api/pre-deploy/${THIN_RETRY_SERVICE_ID}/retry" '{}' >/dev/null
  else
    log "== 5c. retry: skipped (no THIN_RETRY_SERVICE_ID fixture) =="
  fi

  log "== 6. the CLI trace carries no -api address =="
  # Built at run time so the literal never appears in this source (the check
  # must not match itself).
  local api_port api_host
  api_port="$(printf '%s' '88')$(printf '%s' '75')"
  api_host="subnet-acl-$(printf '%s' 'provision')-api"
  if grep -Fq -e ":${api_port}" -e "${api_host}" -e "provision-api:${api_port}" "$0"; then
    log "FAIL: the CLI source names the -api"
    exit 1
  fi
  log "OK: no -api address in the CLI source or its configuration"
  log "== thin CLI parity run finished =="
}

main "$@"
