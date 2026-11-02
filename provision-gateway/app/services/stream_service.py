"""Stream exposure handles — mint/list/revoke and the per-connection verdict (T9).

The raw handle is never stored (sha256 only) and returned exactly once. A mint
for a row that already has a live handle is a ROTATION: revoke + insert, which
is what the card's single Generate button does on re-click (R5.6).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models.stream_exposure import StreamExposure
from ..models.user import User
from .acl_service import allows

logger = logging.getLogger(__name__)
HANDLE_BYTES = 20
CANONICAL_SEPARATOR = "--"


def new_handle() -> str:
    return base64.b32encode(secrets.token_bytes(HANDLE_BYTES)).decode("ascii").rstrip("=").lower()


def hash_handle(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def domain_of(entry: dict[str, Any]) -> str:
    hostname = str((entry or {}).get("hostname", "") or "")
    _, _, domain = hostname.partition(".")
    return domain


def canonical_name(entry: dict[str, Any], sub_service: str) -> str:
    host, _, domain = str((entry or {}).get("hostname", "") or "").partition(".")
    if CANONICAL_SEPARATOR in sub_service or CANONICAL_SEPARATOR in host:
        raise ValueError(f"'{CANONICAL_SEPARATOR}' is reserved in a host/sub-service")
    return f"{host}{CANONICAL_SEPARATOR}{sub_service}.{domain}"


def exposure_host(handle: str, domain: str) -> str:
    return f"{handle}.{domain}"


def _now():
    return datetime.now(timezone.utc)


def _revoke(query) -> int:
    count = 0
    for row in query.filter(StreamExposure.revoked_at.is_(None)).all():
        row.revoked_at = _now()
        count += 1
    return count


def mint(db: Session, *, owner_user_id, entry: dict[str, Any],
         sub_service: str, port: int) -> tuple[str, StreamExposure]:
    service_id = entry["service_id"]
    _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_id == service_id,
        StreamExposure.sub_service == sub_service,
        StreamExposure.port == int(port),
    ))
    host = exposure_host(new_handle(), domain_of(entry))
    row = StreamExposure(
        handle_hash=hash_handle(host),
        owner_user_id=owner_user_id,
        service_id=service_id,
        sub_service=sub_service,
        port=int(port),
        canonical_name=canonical_name(entry, sub_service),
        created_at=_now(),
        revoked_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return host, row


def list_for_instance(db: Session, service_id: str) -> list[StreamExposure]:
    return (
        db.query(StreamExposure)
        .filter(StreamExposure.service_id == service_id)
        .order_by(StreamExposure.created_at.desc())
        .all()
    )


def revoke_row(db: Session, service_id: str, sub_service: str, port: int) -> int:
    count = _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_id == service_id,
        StreamExposure.sub_service == sub_service,
        StreamExposure.port == int(port),
    ))
    db.commit()
    return count


def revoke_instance(db: Session, service_id: str) -> int:
    count = _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_id == service_id
    ))
    db.commit()
    return count


def _deny(reason: str) -> dict[str, Any]:
    return {"allow": False, "canonical_name": None, "reason": reason}


def verify(db: Session, registry, raw_handle: str, *,
           requested_sub_service: str | None = None,
           requested_port: int | None = None) -> dict[str, Any]:
    try:
        if not raw_handle:
            return _deny("no handle")
        row = db.query(StreamExposure).filter(
            StreamExposure.handle_hash == hash_handle(raw_handle)
        ).first()
        if row is None:
            return _deny("unknown handle")
        if row.revoked_at is not None:
            return _deny("revoked handle")
        if requested_sub_service is not None and requested_sub_service != row.sub_service:
            return _deny("scope: sub_service mismatch")
        if requested_port is not None and int(requested_port) != int(row.port):
            return _deny("scope: port mismatch")
        entry = registry.get_by_id(str(row.service_id))
        if entry is None or entry.get("service_deleted_at") is not None:
            return _deny("instance gone")
        # A stopped instance has no stream route: the -api pauses its rendered
        # stream conf (`<conf>.paused`) before stopping the containers. Fail
        # closed at the gate rather than accepting the handshake and
        # black-holing the session (E2E step 3).
        from pathlib import Path as _Path
        by_cat = entry.get("nginx_conf_paths_by_category") or {}
        stream_conf = by_cat.get("stream") if isinstance(by_cat, dict) else None
        if stream_conf and _Path(str(stream_conf) + ".paused").exists() \
                and not _Path(str(stream_conf)).exists():
            return _deny("upstream stopped")
        if str(entry.get("status") or "") != "running":
            return _deny("instance not running")
        owner = db.query(User).filter(User.id == row.owner_user_id).first()
        if owner is None:
            return _deny("owner gone")
        if not owner.account_live:
            return _deny("owner not entitled")
        # `allows` keys users.id, while the registry row names api_users.id.
        # Resolve the instance's gateway user first; comparing the two id spaces
        # made every stream verdict deny (found in the E2E door probe).
        shared_user = db.query(User).filter(
            User.api_user_id == entry.get("owner_api_user_id")
        ).first()
        if shared_user is None or not allows(db, owner, shared_user.id):
            return _deny("acl")
        return {"allow": True, "canonical_name": row.canonical_name, "reason": "allowed"}
    except Exception as exc:  # noqa: BLE001 — fail-closed is the contract
        logger.warning("stream verify failed, denying: %s", exc)
        return _deny("error")
