"""Stream exposure handles — mint, list, revoke, and the per-connection verdict.

A handle is an **opaque** random token: lowercase base32 over 20 random bytes →
32 characters, ONE DNS label (≤ 63, and the alphabet survives DNS
case-insensitivity), ~160 bits. It is never an encoding of the target: a label
cannot carry a target *and* a credential, and any encoding expands (GRILLING
**G39** §2), so the target is resolved here, gateway-side.

The handle is a **credential carried in the clear** inside the client's TLS SNI.
Three consequences are structural, not conventions:

* only ``sha256(handle)`` is ever stored (``StreamExposure.handle_hash``);
* the raw handle is returned **exactly once**, by :func:`mint` — no reader exists;
* the platform logs the canonical name, never the handle (task-doc R7.3).

The verdict (:func:`verify`) is **fail-closed**: unknown, revoked, row gone,
instance deleted, owner no longer entitled, or any error at all is a *deny*.
There is no default-allow path and no cache — the check runs per connection, so a
grant revoked a second ago bites at the next connect (R3.3).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models.end_user import EndUser
from ..models.stream_exposure import StreamExposure
from .auth_service import acl_allows

logger = logging.getLogger(__name__)

# 20 random bytes → 32 base32 chars: one label, ~160 bits, DNS-safe alphabet.
HANDLE_BYTES = 20

# Reserved as the instance/sub-service separator (task-doc R1.5): its presence
# inside a name would make the canonical name ambiguous, so the builder refuses
# it. Area A's validation (T16) rejects it for the user, before it ever gets here.
CANONICAL_SEPARATOR = "--"


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------


def new_handle() -> str:
    """A fresh opaque handle: lowercase base32, 32 chars, one DNS label."""
    return (
        base64.b32encode(secrets.token_bytes(HANDLE_BYTES))
        .decode("ascii")
        .rstrip("=")
        .lower()
    )


def hash_handle(raw: str) -> str:
    """The store's only representation of a handle (sha256 hex — the api_keys idiom)."""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def domain_of(entry: dict[str, Any]) -> str:
    """The deployment domain, taken from the instance's registry hostname.

    ``<svc>-<user>-<label>.<domain>`` → ``<domain>``. Everything this feature
    addresses is exactly ONE label under it (a handle, an instance host, a
    canonical name), which is what makes the single domain wildcard sufficient
    (GRILLING G39 §6).
    """
    hostname = str((entry or {}).get("hostname", "") or "")
    _, _, domain = hostname.partition(".")
    return domain


def canonical_name(entry: dict[str, Any], sub_service: str) -> str:
    """``<instance host>--<sub_service>.<domain>`` — the inner hop's name.

    This is the name the edge dials as the inner TLS SNI and the name the
    generated ``*.stream.conf`` block selects on its ``server_name`` (task-doc
    R5.1/R9.2) — one name, used verbatim on both sides, so no map is needed on
    that listener.
    """
    host, _, domain = str((entry or {}).get("hostname", "") or "").partition(".")
    if CANONICAL_SEPARATOR in sub_service or CANONICAL_SEPARATOR in host:
        raise ValueError(
            f"'{CANONICAL_SEPARATOR}' is reserved and must not appear in an "
            f"instance host or a sub-service name (got {host!r} / {sub_service!r})"
        )
    return f"{host}{CANONICAL_SEPARATOR}{sub_service}.{domain}"


def exposure_host(handle: str, domain: str) -> str:
    """The client-facing address: ``<handle>.<domain>`` — one label, no port."""
    return f"{handle}.{domain}"


# ---------------------------------------------------------------------------
# Mint / list / revoke
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _revoke(query) -> int:
    """Mark every live row the query selects as revoked; return how many."""
    revoked = 0
    for row in query.filter(StreamExposure.revoked_at.is_(None)).all():
        row.revoked_at = _now()
        revoked += 1
    return revoked


def mint(
    db: Session,
    *,
    owner_user_id: int,
    entry: dict[str, Any],
    sub_service: str,
    port: int,
) -> tuple[str, StreamExposure]:
    """Mint a handle for one stream row; return ``(host, row)``.

    ``host`` is the **client-facing name** — ``<handle>.<domain>`` — and it is
    what the store hashes. The edge sends the client's SNI verbatim
    (``stream-acl.js`` → ``POST /api/stream/verify {handle: <SNI>}``, the shape
    the task doc's §3.2 and contract C1 fix), so the lookup key must be the whole
    dialled name, not its first label: hashing the bare label would make every
    real connection a miss. A consequence worth keeping: a handle is bound to its
    domain, so one minted under domain A cannot be replayed under domain B.


    "Mint" is **revoke + insert** (R7.2): a row has at most one live handle, so
    generating a new URL invalidates the old one — at its *next* connection,
    because the verdict is evaluated per connection and nothing is cached.

    The raw handle leaves this function once and is never stored; the caller is
    the only place it can be shown (R2.2's "shown once"). The registry is not
    touched and the -api is never involved.
    """
    service_name = str(entry.get("service_name", ""))
    user_name = str(entry.get("user_name", ""))
    label = str(entry.get("label", ""))

    _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_name == service_name,
        StreamExposure.user_name == user_name,
        StreamExposure.label == label,
        StreamExposure.sub_service == sub_service,
        StreamExposure.port == int(port),
    ))

    host = exposure_host(new_handle(), domain_of(entry))
    row = StreamExposure(
        handle_hash=hash_handle(host),
        owner_user_id=int(owner_user_id),
        service_name=service_name,
        user_name=user_name,
        label=label,
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


def list_for_instance(
    db: Session, service_name: str, user_name: str, label: str
) -> list[StreamExposure]:
    """Every exposure row of one instance, newest first — **never a handle**.

    This is what the card reads (R6.1/R6.2): the recorded state decides what the
    card offers, and the recorded state cannot leak a credential.
    """
    return (
        db.query(StreamExposure)
        .filter(
            StreamExposure.service_name == service_name,
            StreamExposure.user_name == user_name,
            StreamExposure.label == str(label),
        )
        .order_by(StreamExposure.created_at.desc())
        .all()
    )


def revoke_row(
    db: Session, service_name: str, user_name: str, label: str,
    sub_service: str, port: int,
) -> int:
    """Revoke the live handle of one stream row (idempotent). Returns the count."""
    count = _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_name == service_name,
        StreamExposure.user_name == user_name,
        StreamExposure.label == str(label),
        StreamExposure.sub_service == sub_service,
        StreamExposure.port == int(port),
    ))
    db.commit()
    return count


def revoke_instance(
    db: Session, service_name: str, user_name: str, label: str
) -> int:
    """Revoke every live handle of one instance — the delete path's half of R7.1.

    Deleting an instance removes its generated ``*.stream.conf`` *and* revokes
    its handles; a live handle whose instance is gone would already be denied by
    :func:`verify`, but leaving the rows live would keep a credential that is
    merely unrouteable rather than dead.
    """
    count = _revoke(db.query(StreamExposure).filter(
        StreamExposure.service_name == service_name,
        StreamExposure.user_name == user_name,
        StreamExposure.label == str(label),
    ))
    db.commit()
    return count


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _deny(reason: str) -> dict[str, Any]:
    return {"allow": False, "canonical_name": None, "reason": reason}


def verify(
    db: Session,
    registry,
    raw_handle: str,
    *,
    requested_sub_service: str | None = None,
    requested_port: int | None = None,
) -> dict[str, Any]:
    """The verdict for ONE connection: ``{allow, canonical_name, reason}``.

    Fail-closed, no exceptions: every failure below — and any error at all —
    returns ``allow: False``. The callers are the edge's njs hook (which denies
    on a non-200 *or* on ``allow: false``) and the tests.

    ``requested_*`` carry the row the caller asked for. The edge sends neither —
    a client's TLS ClientHello names only the handle, which is the point of an
    opaque handle — so the bound row is what it opens; the parameters exist so
    the scope check (R3.2) is a real comparison wherever a caller *does* name a
    row, rather than a property nobody checks.
    """
    try:
        if not raw_handle:
            return _deny("no handle")

        row = (
            db.query(StreamExposure)
            .filter(StreamExposure.handle_hash == hash_handle(raw_handle))
            .first()
        )
        if row is None:
            return _deny("unknown handle")
        if row.revoked_at is not None:
            return _deny("revoked handle")

        # R3.2 — the scope check: a handle minted for one stream row can never
        # open another.
        if requested_sub_service is not None and requested_sub_service != row.sub_service:
            return _deny("scope: sub_service mismatch")
        if requested_port is not None and int(requested_port) != int(row.port):
            return _deny("scope: port mismatch")

        # The instance (and so the row) must still exist: the registry is the
        # -api's record, read here through the same shared mount.
        entry = registry.get_entry(row.user_name, row.service_name, row.label)
        if entry is None:
            return _deny("instance gone")

        # R3.3 — the ACL matrix, re-evaluated per connection and uncached, for
        # the handle's OWNER against the bound instance. Same function the HTTP
        # gate uses (auth_service.acl_allows).
        owner = db.query(EndUser).filter(EndUser.id == row.owner_user_id).first()
        if owner is None:
            return _deny("owner gone")
        if not owner.is_active or not owner.is_approved:
            return _deny("owner not entitled")
        if not acl_allows(owner, str(entry.get("user_name", ""))):
            return _deny("acl")

        return {"allow": True, "canonical_name": row.canonical_name, "reason": "allowed"}
    except Exception as exc:  # noqa: BLE001 — fail-closed is the contract
        logger.warning("stream verify failed, denying: %s", exc)
        return _deny("error")
