"""The gateway's current cross-path values (GRILLING G91, 2026-11-09).

The no-pin design removed ``CROSS_NGINX_IP`` / ``CROSS_RESOLVER_IP``: the
addresses are dynamic, so the gateway resolves them itself instead of reading
a startup constant.

Two names, deliberately different in kind:

* the **cross nginx**: ``subnet-acl-cross-nginx-cross`` — a **cross-only
  alias**. The cross nginx is dual-homed (shared + cross); resolving the plain
  name can return the shared address, which is not the identity the gateway
  trusts and not the address the dnsmasq wildcard may carry.
* the **cross resolver**: ``subnet-acl-cross-resolver`` — its plain container
  name. It is cross-only, so the name is unambiguous.

One owner, one value: ``cross_orchestrator`` writes both the dnsmasq records
and the resolver pair from the values held here, and the cross-verify trust
compares against the same value. The ~5 s monitor refreshes the cache; a
missing cache entry falls back to a direct resolve so a request in the first
seconds after startup still works.
"""

from __future__ import annotations

import logging
import socket

from ..config import settings

_log = logging.getLogger("gateway.cross_values")

_cross_nginx_ip: str | None = None
_resolver_ip: str | None = None


def _resolve(name: str) -> str | None:
    """Docker embedded DNS lookup; None on failure (never a stale constant)."""
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def current_cross_nginx_ip() -> str | None:
    """The cross nginx's current cross-network address (cached, loop-refreshed)."""
    global _cross_nginx_ip
    if _cross_nginx_ip is None:
        _cross_nginx_ip = _resolve(settings.CROSS_NGINX_NAME)
    return _cross_nginx_ip


def resolve_cross_nginx_ip() -> str | None:
    """The cross nginx's address, resolved NOW (no cache).

    The cross-verify trust check uses this: it must never compare a caller
    against a value that another process's watch loop refreshed at a different
    time. Docker's embedded DNS answers locally, so the per-request lookup is
    cheap.
    """
    return _resolve(settings.CROSS_NGINX_NAME)


def current_resolver_ip() -> str | None:
    """The cross resolver's current address."""
    global _resolver_ip
    if _resolver_ip is None:
        _resolver_ip = _resolve(settings.CROSS_RESOLVER_NAME)
    return _resolver_ip


def refresh() -> dict:
    """Re-resolve both names; report which values changed.

    The first call (no previous value) initialises without reporting a change:
    the startup pass already writes the dnsmasq config and applies the pair.
    A failed lookup keeps the previous value and logs; the next tick retries.
    """
    global _cross_nginx_ip, _resolver_ip
    previous_nginx, previous_resolver = _cross_nginx_ip, _resolver_ip
    nginx_ip = _resolve(settings.CROSS_NGINX_NAME)
    resolver_ip = _resolve(settings.CROSS_RESOLVER_NAME)
    if nginx_ip is None:
        _log.warning("cross nginx alias %s did not resolve; keeping the last value",
                     settings.CROSS_NGINX_NAME)
        nginx_ip = previous_nginx
    if resolver_ip is None:
        _log.warning("cross resolver name %s did not resolve; keeping the last value",
                     settings.CROSS_RESOLVER_NAME)
        resolver_ip = previous_resolver
    _cross_nginx_ip, _resolver_ip = nginx_ip, resolver_ip
    return {
        "cross_nginx_ip": nginx_ip,
        "resolver_ip": resolver_ip,
        "cross_nginx_changed": previous_nginx is not None and nginx_ip != previous_nginx,
        "resolver_changed": previous_resolver is not None and resolver_ip != previous_resolver,
    }


def set_current(*, cross_nginx_ip: str | None = None,
                resolver_ip: str | None = None) -> None:
    """Prime / clear the cache (tests and the first resolve)."""
    global _cross_nginx_ip, _resolver_ip
    if cross_nginx_ip is not None:
        _cross_nginx_ip = cross_nginx_ip
    if resolver_ip is not None:
        _resolver_ip = resolver_ip


def reset() -> None:
    global _cross_nginx_ip, _resolver_ip
    _cross_nginx_ip = None
    _resolver_ip = None
