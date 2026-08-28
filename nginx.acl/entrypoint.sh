#!/bin/sh
# v5 edge -nginx-acl entrypoint — assembles a single nginx.conf from
# edge.conf.template + acl-helpers.conf.template (decision 13: shell `if` +
# envsubst, NOT Jinja, NOT fragments).
#
# How it works
# ------------
# 1. The helper template (acl-helpers.conf.template) is assembled by the SAME
#    shell-if + envsubst pipeline as the main template — helpers are not static.
# 2. Conditional mode blocks in the templates are marked with comment lines:
#        # [if PORTAL_MODE=https]
#        # [else (ENABLE_ACL=false)] / # [else]
#        # [endif]
#        # [if ENABLE_ACL=true]
#    and self-contained inline conditionals:
#        # [if PORTAL_MODE=https] return 301 https://$host$request_uri;
#    The shell walks the lines, keeping a push-down stack of 2-char frames
#    (TI=if-true / TE=if-false / FI=else-true / FE=else-false). A block-open
#    `# [if X]` pushes a frame; `# [else]` flips the top frame; `# [endif]`
#    pops it. An INLINE `# [if X] rest` (text after the `]`) emits `rest` when
#    the condition is true and is otherwise dropped — it is self-contained and
#    pushes nothing. A line is emitted only when the top of the stack is
#    emitting. This yields valid nginx conf in every mode with no dead lines.
# 3. envsubst with a strict allow-list substitutes only the constants we own
#    (${PORTAL_SCHEME} ${PORTAL_HOSTNAME} ${PORTAL_REDIRECT_PORT}
#    ${HTTPS_REDIRECT_PORT}) so nginx $variables ($host, $scheme, $request_uri,
#    $ssl_server_name, ...) survive byte-for-byte.
# 4. `nginx -g 'daemon off;'` runs in the foreground as UID nginx (non-root,
#    decision 17; NET_BIND_SERVICE is a default docker capability that lets a
#    non-root process bind ports 80/443).

set -u

# ---- env-overridable paths ----
EDGE_TEMPLATE="${EDGE_TEMPLATE:-/etc/nginx/acl/edge.conf.template}"
HELPERS_TEMPLATE="${HELPERS_TEMPLATE:-/etc/nginx/acl/acl-helpers.conf.template}"
HELPERS_OUT="${HELPERS_OUT:-/etc/nginx/acl/acl-helpers.conf}"
NGINX_CONF_OUT="${NGINX_CONF_OUT:-/etc/nginx/nginx.conf}"
START_NGINX="${START_NGINX:-1}"

# ---- envsubst allow-list: ONLY these constants are substituted ----
ENVSUBST_VARS='$PORTAL_SCHEME $PORTAL_HOSTNAME $PORTAL_REDIRECT_PORT $HTTPS_REDIRECT_PORT'

# ---- condvar evaluation ----
# PORTAL_MODE is an enum (`http` | `https`), not a boolean; ENABLE_ACL is a
# boolean (`true` | `false`). Both are normalized to 'true'/'false'.
case "${PORTAL_MODE:-http}" in
    https|HTTPS|ssl|443) PORTAL_HTTPS=true ;;
    *) PORTAL_HTTPS=false ;;
esac
_cond() {
    case "${1:-}" in
        true|TRUE|True|1|yes) echo true ;;
        *) echo false ;;
    esac
}
ACL_ON="$(_cond "${ENABLE_ACL:-true}")"

# PORTAL_MODE is the only https-mode signal: https → PORTAL_SCHEME=https.
# These are EXPORTED so envsubst (an external program reading the environment)
# substitutes the real values.
PORTAL_SCHEME="${PORTAL_SCHEME:-https}"
[ "$PORTAL_HTTPS" = "true" ] || PORTAL_SCHEME="http"
# Normalize the redirect-port vars to a leading-colon form when non-empty.
# Bare `$host` followed by a bare port concatenates into a bogus nginx variable
# (`$host8768`); `$host:8768` parses correctly (the colon ends `$host`).
# Similarly `${PORTAL_HOSTNAME}${PORTAL_REDIRECT_PORT}` stays a valid host:port.
HTTPS_REDIRECT_PORT="${HTTPS_REDIRECT_PORT:-}"
case "$HTTPS_REDIRECT_PORT" in ""|:*) ;; *) HTTPS_REDIRECT_PORT=":$HTTPS_REDIRECT_PORT" ;; esac
PORTAL_REDIRECT_PORT="${PORTAL_REDIRECT_PORT:-}"
case "$PORTAL_REDIRECT_PORT" in ""|:*) ;; *) PORTAL_REDIRECT_PORT=":$PORTAL_REDIRECT_PORT" ;; esac
export PORTAL_SCHEME PORTAL_HOSTNAME PORTAL_REDIRECT_PORT HTTPS_REDIRECT_PORT

# ---- frame stack (2-char frames, ${_stack%??} pop) ----
_push() { _stack="${_stack}$1"; }
_pop()  { _stack="${_stack%??}"; }
_emitting() {
    case "${_stack}" in
        *E) return 1 ;;   # top frame false/false → not emitting
        *)  return 0 ;;
    esac
}

# _cond_eval <expr> : push current evaluation of `if <expr>`
# <expr> is e.g. `PORTAL_MODE=https` (the marker's condition up to `]`).
_cond_eval() {
    case "$1" in
        PORTAL_MODE=*) v="$PORTAL_HTTPS" ;;
        ENABLE_ACL=*)  v="$ACL_ON" ;;
        *)             v=false ;;
    esac
    case "$v" in
        true)  _push TI ;;
        *)     _push TE ;;
    esac
}

# _flip : flip the top frame's polarity (for [else]). Frames are 2-char
# (first = condition result T/F, last = polarity I/E); flip the LAST char.
_flip() {
    case "$_stack" in
        *I) _stack="${_stack%I}E" ;;
        *E) _stack="${_stack%E}I" ;;
    esac
}

# ---- assembly ----
_emit_template() {
    src="$1"; out="$2"
    _stack=""
    : > "$out"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *'# [if '*)
                # strip leading whitespace to get indent + marker
                rest="${line#"${line%%[! ]*}"}"
                indent="${line%%"$rest"}"
                cond="${rest#\# [if }"     # `PORTAL_MODE=https] ...` or `PORTAL_MODE=https]`
                expr="${cond%%]*}"         # `PORTAL_MODE=https` (up to first `]`)
                tail="${cond#*]}"          # inline directive (empty for block-open)
                _cond_eval "$expr"
                case "$tail" in
                    '')
                        # block-open: frame stays open until `# [endif]`
                        ;;
                    *)
                        # inline: emit `rest` if condition is true, drop otherwise
                        if _emitting; then
                            inline="${tail#"${tail%%[! ]*}"}"
                            printf '%s%s\n' "$indent" "$inline" >> "$out"
                        fi
                        _pop   # inline is self-contained — no frame left
                        ;;
                esac
                ;;
            *'# [else]'*|*'# [else'*) _flip ;;
            *'# [endif]'*) _pop ;;
            *) if _emitting; then printf '%s\n' "$line" >> "$out"; fi ;;
        esac
    done < "$src"
}

# Helpers first (they are `include`d by the main conf).
_emit_template "$HELPERS_TEMPLATE" "$HELPERS_OUT"
_emit_template "$EDGE_TEMPLATE"    "$NGINX_CONF_OUT.tmp"

# envsubst the constants into both assembled files (allow-list preserves nginx $vars).
envsubst "$ENVSUBST_VARS" < "$NGINX_CONF_OUT.tmp" > "$NGINX_CONF_OUT"
envsubst "$ENVSUBST_VARS" < "$HELPERS_OUT" > "$HELPERS_OUT.tmp"
mv "$HELPERS_OUT.tmp" "$HELPERS_OUT"
rm -f "$NGINX_CONF_OUT.tmp"

# ---- start ----
if [ "$START_NGINX" != "0" ]; then
    exec nginx -g 'daemon off;'
fi
exit 0
