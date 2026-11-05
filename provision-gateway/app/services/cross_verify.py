"""Token-free cross-path authorization (GRILLING G83/G84/G85).

The cross nginx is the only caller. It sends ``X-Source-IP`` (its own
``$remote_addr``) and ``Host``; this module resolves the source service from the
Docker DNS PTR plus ``registry.service_instances.container_names``, the target
service from the gateway's hostname index, and applies the grant matrix:
same-owner, or an explicit service grant (G84: every cross-owner target —
public or private — needs the row; the account prerequisite applies to a private
target and is waived for a live public one). Both services must be live; there
is no transitivity. On allow it returns the target Basic; every failure is a
plain 401/403 — never an allow.

The same module carries the stream verdict (G85): ``cross-stream-verify`` on
``<source IP, exposure handle>`` resolves the live ``stream_exposures`` row to
its canonical name and target port and applies the same matrix. The edge's
handle/user ACL is not consulted there.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

from sqlalchemy import text
from sqlalchemy.orm import Session

from pathlib import Path

from ..config import settings
from ..database import reader_engine
from ..models.service_grant import ServiceGrant
from ..models.stream_exposure import StreamExposure
from ..models.user import User
from . import acl_service
from .basic_credentials import authorization_header
from .stream_service import hash_handle

_log = logging.getLogger("gateway.cross_verify")

_CROSS_NGINX_IP: str | None = None
_STATS = {"allowed": 0, "denied": 0}


def cross_nginx_ip() -> str | None:
    """The cross nginx's actual IP, from config or Docker DNS (cached)."""
    global _CROSS_NGINX_IP
    if settings.CROSS_NGINX_IP:
        return settings.CROSS_NGINX_IP
    if _CROSS_NGINX_IP is None:
        try:
            _CROSS_NGINX_IP = socket.gethostbyname("subnet-acl-cross-nginx")
        except OSError:
            return None
    return _CROSS_NGINX_IP


def _normalize_ip(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("::ffff:"):
        raw = raw[len("::ffff:"):]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


CROSS_NGINX_NAME = "subnet-acl-cross-nginx"


def is_cross_nginx_caller(client_ip: str | None) -> bool:
    """The pinned cross-network address, or the cross nginx's container identity.

    The HTTP subrequest arrives from the pinned cross address, but the stream
    hook's njs ``ngx.fetch`` resolves ``subnet-acl-gateway`` and may connect
    over the SHARED network, where the cross nginx has a different (unpinned)
    address. Trust that too, by the Docker DNS PTR — a source container cannot
    spoof its own container name.
    """
    if not client_ip:
        return False
    trusted = cross_nginx_ip()
    if trusted and _normalize_ip(client_ip) == _normalize_ip(trusted):
        return True
    return _ptr_name(client_ip) == CROSS_NGINX_NAME


def _ptr_name(source_ip: str) -> str | None:
    """Container name from the Docker DNS PTR (``<name>.<network>``)."""
    if not source_ip:
        return None
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(source_ip)
    except OSError:
        return None
    name = host.split(".", 1)[0].strip()
    return name or None


def resolve_source(source_ip: str) -> dict | None:
    """The single live service whose ``container_names`` contains the PTR name."""
    name = _ptr_name(source_ip)
    if not name:
        return None
    with reader_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.id AS service_id, s.owner_api_user_id, a.username AS user_name, "
                "       s.service_name, s.label, s.deleted_at AS service_deleted_at, "
                "       i.status, i.hostname, i.container_names "
                "  FROM registry.services s "
                "  JOIN registry.api_users a ON a.id = s.owner_api_user_id "
                "  JOIN registry.service_instances i ON i.service_id = s.id "
                " WHERE s.deleted_at IS NULL "
                "   AND i.status = 'running' "
                "   AND i.container_names @> ARRAY[:name]::text[]"
            ),
            {"name": name},
        ).mappings().all()
    if len(rows) != 1:
        return None
    row = dict(rows[0])
    row["service_id"] = str(row["service_id"])
    row["owner_api_user_id"] = str(row["owner_api_user_id"])
    row["label"] = str(row["label"])
    return row


def _gateway_user(db: Session, api_user_id) -> User | None:
    return db.query(User).filter(User.api_user_id == api_user_id).first()


def decide(db: Session, source: dict | None, target: dict | None) -> dict:
    """The grant matrix. Returns a verdict dict; never raises on a refusal."""
    if source is None:
        return {"allowed": False, "status": 401, "reason": "unknown source"}
    if target is None:
        return {"allowed": False, "status": 403, "reason": "unknown or dead target"}
    if str(target.get("status") or "") != "running":
        return {"allowed": False, "status": 403, "reason": "target not live"}
    source_owner = str(source["owner_api_user_id"])
    target_owner = str(target["owner_api_user_id"])
    # G83 — account deactivation fails closed for the whole matrix, including
    # the implicit same-owner and public branches: a source whose owner account
    # is not live is not a source at all. (Measured 2026-11-04: without this
    # check a deactivated owner's same-owner services still called each other.)
    source_user = _gateway_user(db, source_owner)
    if source_user is None or not acl_service.account_live(source_user):
        return {"allowed": False, "status": 403,
                "reason": "source owner account is not active"}
    if source_owner == target_owner:
        return {"allowed": True, "reason": "same_owner"}
    target_user = _gateway_user(db, target_owner)
    if target_user is None:
        return {"allowed": False, "status": 403, "reason": "owner account missing"}
    # G84 — every cross-owner target, public or private, needs the explicit
    # row. Public is never an implicit allow.
    row = db.query(ServiceGrant).filter(
        ServiceGrant.source_service_id == source["service_id"],
        ServiceGrant.target_service_id == target["service_id"],
    ).first()
    if row is None:
        return {"allowed": False, "status": 403, "reason": "no service grant"}
    # G84 — the account prerequisite is waived for a public target (a public
    # functional user is reachable by every user at the user level). The waiver
    # is evaluated LIVE: an owner that stops being public/live falls through to
    # the account check and fails closed.
    if acl_service.is_public_owner(target_user):
        return {"allowed": True, "reason": "grant"}
    if not acl_service.allows(db, source_user, target_user.id):
        return {"allowed": False, "status": 403,
                "reason": "no active account grant between the owners"}
    return {"allowed": True, "reason": "grant"}


def verdict(source_ip: str, host: str, db: Session, hostname_index) -> dict:
    """Resolve both sides, decide, and attach the target Basic on allow."""
    source = resolve_source(source_ip)
    target = hostname_index.get_by_hostname(host) if host else None
    result = decide(db, source, target)
    if result["allowed"]:
        _STATS["allowed"] += 1
        result["target_service_id"] = str(target["service_id"])
        result["service_basic"] = authorization_header(str(target["service_id"]))
        return result
    _STATS["denied"] += 1
    _log.warning(
        "cross-verify denied: source_ip=%s source=%s host=%s reason=%s",
        source_ip,
        str(source["service_id"]) if source else "-",
        host or "-",
        result["reason"],
    )
    return result


def _stream_target(db: Session, registry, handle_host: str):
    """The live exposure row and its registry entry, or a denial dict.

    The edge's handle/user ACL is deliberately NOT applied here (G85): the cross
    path's only authorization is the service-grant matrix. The handle itself is
    a credential — it is never logged.
    """
    if not handle_host:
        return None, None, {"allowed": False, "status": 401, "reason": "no SNI"}
    row = db.query(StreamExposure).filter(
        StreamExposure.handle_hash == hash_handle(handle_host)
    ).first()
    if row is None:
        return None, None, {"allowed": False, "status": 403, "reason": "unknown handle"}
    if row.revoked_at is not None:
        return None, None, {"allowed": False, "status": 403, "reason": "revoked handle"}
    entry = registry.get_by_id(str(row.service_id)) if registry else None
    if entry is None or entry.get("service_deleted_at") is not None:
        return None, None, {"allowed": False, "status": 403, "reason": "instance gone"}
    # A stopped instance has no stream route (its rendered conf is paused); fail
    # closed at the gate instead of accepting the handshake and black-holing it.
    by_cat = entry.get("nginx_conf_paths_by_category") or {}
    stream_conf = by_cat.get("stream") if isinstance(by_cat, dict) else None
    if stream_conf and Path(str(stream_conf) + ".paused").exists() \
            and not Path(str(stream_conf)).exists():
        return None, None, {"allowed": False, "status": 403, "reason": "upstream stopped"}
    if str(entry.get("status") or "") != "running":
        return None, None, {"allowed": False, "status": 403, "reason": "instance not running"}
    return row, entry, None


def stream_verdict(source_ip: str, handle_host: str, db: Session, registry) -> dict:
    """G85 — token-free ``<source IP, handle>`` authorization for the stream door.

    Resolves the source service exactly as the HTTP path does, the handle to its
    live exposure, and applies the same service-grant matrix (G84). On allow it
    returns the canonical name the inner TLS hop must carry, the exposure's
    target port, and the internal nginx's stream-listener port to dial.
    """
    source = resolve_source(source_ip)
    if source is None:
        _STATS["denied"] += 1
        return {"allowed": False, "status": 401, "reason": "unknown source"}
    row, entry, denial = _stream_target(db, registry, handle_host)
    if denial is not None:
        _STATS["denied"] += 1
        _log.warning(
            "cross-stream-verify denied: source=%s reason=%s",
            str(source["service_id"]), denial["reason"],
        )
        return denial
    result = decide(db, source, entry)
    if not result["allowed"]:
        _STATS["denied"] += 1
        _log.warning(
            "cross-stream-verify denied: source=%s target=%s reason=%s",
            str(source["service_id"]), str(entry["service_id"]), result["reason"],
        )
        return result
    _STATS["allowed"] += 1
    _log.info(
        "cross-stream-verify allowed: source=%s canonical=%s reason=%s",
        str(source["service_id"]), row.canonical_name, result["reason"],
    )
    return {
        "allowed": True,
        "reason": result["reason"],
        "canonical_name": row.canonical_name,
        "port": int(row.port),
        "inner_port": int(settings.INTERNAL_STREAM_PORT),
        "target_service_id": str(entry["service_id"]),
    }


def stats() -> dict:
    return dict(_STATS)
