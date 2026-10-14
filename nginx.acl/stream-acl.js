/**
 * The edge's STREAM gate (task-doc R4.2/R4.4, GRILLING G39 §4).
 *
 * One job: for every TLS connection on the shared stream port, read the SNI (the
 * operator's exposure handle), ask the gateway, and either route the session to
 * the internal nginx under the CANONICAL NAME the gateway returns, or refuse it.
 * **Fail-closed, without exception**: no SNI, an unknown or revoked handle, a
 * scope mismatch, a refusal from the gateway — and every ERROR, including a
 * gateway that cannot be reached — ends in `s.deny()`.
 *
 * Three facts this file is built on, each MEASURED on this stack (2026-10-10),
 * because the obvious forms do not work:
 *
 *  1. **`js_access` cannot see the SNI.** In a stream server it runs at session
 *     init, BEFORE the TLS handshake: `s.ssl` is undefined and
 *     `$ssl_server_name` is empty. The handler that sees the SNI is
 *     **`js_preread`**.
 *  2. **`js_preread` needs `ssl_preread on;` in the server block.** Without it
 *     the preread phase never runs for a terminating listener — the handler is
 *     never called AND the client's handshake never completes. With it, nginx
 *     prereads the ClientHello, the handshake completes, and the handler runs
 *     with `$ssl_server_name` populated.
 *  3. **The handshake cannot be refused, only the routing.** The listener has a
 *     certificate of its own (the domain wildcard from the same SNI map the http
 *     vhosts use), so a refused client still completes a handshake and is then
 *     dropped with nothing forwarded. `s.deny()` is the refusal, and it exists in
 *     the preread phase.
 *
 * The session WAITS in the preread phase while the gateway is asked, and is
 * released with `s.allow()` — so the answer to "may this connection be routed"
 * is always the gateway's, never a local default.
 *
 * LOGGING (task-doc R7.3): the SNI **is** the handle — a credential in the clear
 * — so it is never logged. A refusal logs its reason, an allow logs the
 * canonical name, and neither logs the handle.
 */

var GATEWAY_VERIFY = 'http://subnet-acl-gateway:8770/api/stream/verify';

// The internal nginx's stream listener, as the shared network names it. The
// container port is an implementation detail no client ever sees (R5.1); when
// the internal nginx terminates this hop it selects the server block by
// `server_name`, so the NAME is the routing, not the port.
var INNER_NGINX = 'subnet-acl-nginx:7687';

function deny(s, why) {
    s.log(ngx.ERR, 'stream exposure denied: ' + why);
    s.deny();
}

function preread(s) {
    // The SNI comes from the nginx VARIABLE, not from `s.ssl.serverName`:
    // measured 2026-10-10, `s.ssl.serverName` is empty in this configuration
    // (a `js_preread` handler on a terminating listener with `ssl_preread on`),
    // while `$ssl_server_name` is populated — and it is the same value the cert
    // maps above resolve on. `s.ssl` is kept as a fallback for shapes where the
    // variable is not yet set.
    var sn = s.variables.ssl_server_name || (s.ssl && s.ssl.serverName) || '';

    // No SNI at all: nothing to resolve, nothing to leak, no route (R4.2).
    if (!sn) {
        deny(s, 'no SNI');
        return;
    }

    ngx.fetch(GATEWAY_VERIFY, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handle: sn })
    })
        .then(function (resp) {
            if (resp.status !== 200) {
                throw new Error('gateway status ' + resp.status);
            }
            return resp.json();
        })
        .then(function (verdict) {
            if (!verdict || verdict.allow !== true || !verdict.canonical_name) {
                deny(s, 'gateway refused (' + ((verdict && verdict.reason) || 'no verdict') + ')');
                return;
            }
            // Allowed: hand the session to the content phase, which opens the
            // platform-internal TLS hop with the canonical name as its SNI.
            s.variables.stream_upstream = INNER_NGINX;
            s.variables.stream_sni = verdict.canonical_name;
            s.log(ngx.INFO, 'stream exposure routed to ' + verdict.canonical_name);
            s.allow();
        })
        .catch(function (err) {
            // Fail-closed: a gateway we cannot reach is a refusal, never a route.
            deny(s, 'gateway unreachable (' + err + ')');
        });
}

export default { preread };
