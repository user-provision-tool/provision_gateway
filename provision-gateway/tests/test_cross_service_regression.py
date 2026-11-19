"""Cross-service regression checks tied to the DNS-proxy change (G101 T4).

The cross resolver's supervisor must detect a config **content** change (not only
a whole-second mtime), so two writes in the same second are both restarted.  The
live behaviour is exercised in the DNS-proxy E2E; this is its static contract.
"""

from __future__ import annotations

from pathlib import Path


def _entrypoint() -> str:
    # tests -> provision-gateway -> _provision_gateway
    return (Path(__file__).resolve().parents[2]
            / "cross-resolver" / "entrypoint.sh").read_text()


def test_cross_resolver_supervisor_compares_config_content():
    text = _entrypoint()
    assert "md5sum" in text or "conf_hash" in text, (
        "the supervisor must hash the config, not only read the mtime")
    assert "config content changed" in text
    assert "stat -c %Y" not in text, (
        "a whole-second mtime comparison can miss a second write in the same second")


def test_cross_resolver_supervisor_has_no_static_resolver_address():
    text = _entrypoint()
    # The proxy resolves the resolver by NAME; no IP is pinned in the image.
    assert "CROSS_RESOLVER_NAME" not in text
    assert "127.0.0.11" not in text
