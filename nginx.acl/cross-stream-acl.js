/**
 * The CROSS path's STREAM gate (G85).
 *
 * The same exposure URL the edge serves (`{handle}.{domain}:{STREAM_PORT}`) is
 * dialled by a source container. For every TLS connection on that listener
 * this hook reads the SNI (the handle), sends `<source IP, handle>` to the
 * gateway's token-free `cross-stream-verify`, and either routes the session to
 * the internal nginx under the CANONICAL NAME the gateway returns, or refuses
 * it. **Fail-closed, without exception**: no SNI, an unknown or revoked
 * handle, a dead or paused instance, an unknown source, a refused grant, an
 * unreachable gateway — every one ends in `s.deny()`.
 *
 * Differences from the edge hook (`stream-acl.js`), and nothing else:
 *  - the endpoint is `/api/auth/cross-stream-verify` and the request carries
 *    the source container's address (`X-Source-IP`), so the gateway applies the
 *    service-grant matrix — the edge's handle/user ACL is NOT used here;
 *  - the inner upstream's PORT comes from the verdict (the internal nginx's
 *    stream listener), not a hardcoded constant.
 *
 * The SNI is the handle — a credential in the clear — so it is never logged.
 * An allow logs the canonical name; a refusal logs only its reason.
 */

var GATEWAY_VERIFY = 'http://subnet-acl-gateway:8770/api/auth/cross-stream-verify';
// The internal nginx's stream listener; the verdict carries the authoritative
// value, this is only the fallback when an older gateway omits it.
var INNER_FALLBACK_PORT = 7687;

function deny(s, why) {
    s.log(ngx.ERR, 'cross stream exposure denied: ' + why);
    s.deny();
}

function preread(s) {
    // The SNI comes from the nginx VARIABLE (measured on this stack: the
    // `s.ssl.serverName` form is empty in this shape) — the same value the
    // certificate map resolves on.
    var sn = s.variables.ssl_server_name || (s.ssl && s.ssl.serverName) || '';
    if (!sn) {
        deny(s, 'no SNI');
        return;
    }

    // The source container's cross-network address — the true client of this
    // listener. The gateway maps it through the Docker DNS PTR.
    var src = s.remoteAddress || s.variables.remote_addr || '';

    ngx.fetch(GATEWAY_VERIFY, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Source-IP': src
        },
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
            var port = verdict.inner_port || INNER_FALLBACK_PORT;
            s.variables.stream_upstream = 'subnet-acl-nginx:' + port;
            s.variables.stream_sni = verdict.canonical_name;
            s.log(ngx.INFO, 'cross stream exposure routed to ' + verdict.canonical_name);
            s.allow();
        })
        .catch(function (err) {
            deny(s, 'gateway unreachable (' + err + ')');
        });
}

export default { preread };
