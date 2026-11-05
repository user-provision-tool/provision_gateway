"""G85 — the cross nginx stream door and the serving-port alignment (source shape).

Static assertions over the templates the cross container renders. The live
TLS trace is §12 C17/C18; these keep the contract from drifting.
"""

from __future__ import annotations

from pathlib import Path

NGINX_ACL = Path(__file__).resolve().parents[2] / "nginx.acl"


def _read(name: str) -> str:
    return (NGINX_ACL / name).read_text()


def test_cross_http_and_https_listen_on_the_edge_port_numbers():
    conf = _read("cross.conf.template")
    assert "listen ${NGINX_HTTP_PORT} default_server;" in conf
    assert "listen ${NGINX_HTTPS_PORT} ssl default_server;" in conf
    # The pre-G85 fixed listeners are retired.
    assert "listen 80 default_server;" not in conf
    assert "listen 443 ssl default_server;" not in conf


def test_cross_stream_listener_uses_the_edge_pattern_and_no_host_port():
    conf = _read("cross.conf.template")
    assert "listen      ${STREAM_PORT} ssl;" in conf
    assert "ssl_preread on;" in conf
    assert "js_preread  cross_stream_acl.preread;" in conf
    assert "cross-stream-acl.js" in conf
    assert "load_module /usr/lib/nginx/modules/ngx_stream_js_module.so;" in conf
    # The stream block proxies to the internal nginx under the canonical SNI.
    assert "proxy_ssl_name      $stream_sni;" in conf


def test_entrypoint_substitutes_the_serving_ports():
    entry = _read("entrypoint.sh")
    assert "$NGINX_HTTP_PORT" in entry and "$NGINX_HTTPS_PORT" in entry
    assert 'NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-80}"' in entry
    assert 'NGINX_HTTPS_PORT="${NGINX_HTTPS_PORT:-443}"' in entry


def test_cross_hook_calls_the_token_free_verify_with_the_source_ip_and_never_logs_the_handle():
    hook = _read("cross-stream-acl.js")
    assert "/api/auth/cross-stream-verify" in hook
    assert "'X-Source-IP': src" in hook
    assert "verdict.inner_port" in hook
    assert "verdict.canonical_name" in hook
    # The SNI is a credential: it is never interpolated into a log line.
    assert "s.log(ngx.INFO, 'cross stream exposure routed to ' + verdict.canonical_name)" in hook
    assert "s.log(" in hook and "' + sn" not in hook and "+ sn)" not in hook
