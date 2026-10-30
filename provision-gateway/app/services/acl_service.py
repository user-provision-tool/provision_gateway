"""The ACL grant store — one home for the ID-keyed matrix (GRILLING G78).

The old matrix was ``allowed_special_users`` on ``EndUser``: a comma-separated
string compared against a target username. It had no per-row state, could not
be invalidated when an account was deactivated or deleted, and a reused
username inherited stale grants.

This module owns the replacement:

* :class:`AclGrant` rows keyed by ``end_users.id``;
* the **one** predicate the HTTP gate and the stream verdict share
  (``auth_service.acl_allows`` delegates here);
* the admin operations (set a tag active/inactive, bulk-inactivate on
  deactivation, remove both directions on deletion);
* the one-time migration from the retired string.

Nothing here reads the legacy string except :func:`migrate_legacy_grants`; the
migration clears it after conversion.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.acl_grant import (
    STATE_ACTIVE,
    STATE_INACTIVE,
    AclGrant,
)
from ..models.audit_log import AuditLog
from ..models.end_user import EndUser

logger = logging.getLogger(__name__)

# Frozen contract values (T0). The gateway/edge/public-card consumers all use
# these names; the edge template spells the header in nginx syntax.
PUBLIC_VERDICT_HEADER = "X-Public-Service"
PUBLIC_VERDICT_VALUE = "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def account_live(user: EndUser | None) -> bool:
    """An account that may participate in an ACL decision.

    Both ends of a grant must be active, approved and not soft-deleted. This is
    stricter than "the row exists" on purpose: a deactivated account's rows are
    inactive, and a row that somehow survives a deletion must still deny.
    """
    return bool(
        user is not None
        and user.is_active
        and user.is_approved
        and user.deleted_at is None
    )


def is_public_owner(user: EndUser | None) -> bool:
    """The target is a public functional user that may take the public branch.

    Publicness does not override lifecycle: a public flag on an inactive,
    unapproved or soft-deleted account is inert.
    """
    return bool(
        user is not None
        and user.role == "special"
        and user.is_public_shared
        and account_live(user)
    )


def was_public_record(db: Session, user: EndUser | None) -> bool:
    """Historical public marker for display, without restoring access.

    A soft-deleted special user keeps its flag under the current delete path.
    For tombstones written before that change (the retained `g79public` review
    group), the registration-time `public_flag` audit row is the independent
    record that the account was public. The active public verdict still goes
    through :func:`is_public_owner`, which requires the account to be live, so
    this can never restore access.
    """
    if user is None:
        return False
    if user.is_public_shared:
        return True
    if user.deleted_at is None or user.role != "special":
        return False
    row = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "public_flag",
            AuditLog.target_user == user.username,
            AuditLog.status == "success",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    if row is None:
        return False
    try:
        detail = json.loads(row.detail_json or "{}")
    except (TypeError, ValueError):
        return False
    return bool(detail.get("is_public_shared"))


def owner_is_gone(user: EndUser | None) -> bool:
    """The registry owner has no usable account row.

    A soft-deleted row is the G78 case; a registry entry with no ``EndUser`` at
    all is the legacy case §9.3 records. Both are unreachable to a non-admin and
    are shown as owner-gone so the operator can clean them up.
    """
    return user is None or user.deleted_at is not None


def parse_legacy(raw: str | None) -> list[str]:
    """Parse the retired comma-separated string, trimming entries (N1)."""
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def allows(db: Session, grantee: EndUser | None, target_user_name: str) -> bool:
    """Does ``grantee`` reach an instance owned by ``target_user_name``?

    The matrix is: own instance, or an **active** ``acl_grants`` row whose
    grantee and shared accounts are both live. IDs are the keys; the target
    username is resolved to its account row, so a deleted target cannot be
    reached even if a stale row somehow remains.
    """
    if not account_live(grantee) or not target_user_name:
        return False
    if target_user_name == grantee.username:
        return True

    target = (
        db.query(EndUser)
        .filter(EndUser.username == target_user_name)
        .first()
    )
    if not account_live(target):
        return False

    row = (
        db.query(AclGrant)
        .filter(
            AclGrant.grantee_user_id == grantee.id,
            AclGrant.shared_user_id == target.id,
            AclGrant.state == STATE_ACTIVE,
        )
        .first()
    )
    return row is not None


def allowed_usernames(db: Session, grantee_id: int) -> set[str]:
    """Own username plus every shared account with an active, live grant.

    This is the list the dashboard uses to decide which service groups a viewer
    may see. It is derived from the same rows ``allows`` reads, so the list and
    the gate cannot disagree.
    """
    grantee = db.query(EndUser).filter(EndUser.id == grantee_id).first()
    if grantee is None or not account_live(grantee):
        return set()
    names = {grantee.username}
    rows = (
        db.query(AclGrant, EndUser)
        .join(EndUser, AclGrant.shared_user_id == EndUser.id)
        .filter(
            AclGrant.grantee_user_id == grantee_id,
            AclGrant.state == STATE_ACTIVE,
        )
        .all()
    )
    for _grant, shared in rows:
        if account_live(shared):
            names.add(shared.username)
    return names


def list_for_grantee(db: Session, grantee_id: int) -> list[dict]:
    """Every row where ``grantee_id`` is the grantee, active or inactive."""
    rows = (
        db.query(AclGrant, EndUser)
        .join(EndUser, AclGrant.shared_user_id == EndUser.id)
        .filter(AclGrant.grantee_user_id == grantee_id)
        .order_by(EndUser.username.asc())
        .all()
    )
    return [
        {
            "id": grant.id,
            "shared_user_id": shared.id,
            "shared_username": shared.username,
            "is_public_shared": bool(shared.is_public_shared),
            "state": grant.state,
        }
        for grant, shared in rows
    ]


def candidates(db: Session, exclude_user_id: int | None = None) -> list[dict]:
    """Special accounts the admin can grant to.

    Includes inactive accounts so an existing greyed tag still has a name, but
    never a soft-deleted one (its rows are removed, and the name is reserved).
    """
    q = (
        db.query(EndUser)
        .filter(
            EndUser.role == "special",
            EndUser.is_approved.is_(True),
            EndUser.deleted_at.is_(None),
        )
        .order_by(EndUser.username.asc())
    )
    if exclude_user_id is not None:
        q = q.filter(EndUser.id != exclude_user_id)
    return [
        {
            "id": u.id,
            "username": u.username,
            "is_active": bool(u.is_active),
            "is_public_shared": bool(u.is_public_shared),
        }
        for u in q.all()
    ]


def set_grant(
    db: Session,
    grantee_id: int,
    shared_id: int,
    active: bool,
) -> dict | None:
    """Create or toggle one grant row.

    * ``active=True`` creates a row or activates an existing inactive one; both
      accounts must be live.
    * ``active=False`` deactivates an existing row and keeps it (the greyed-tag
      state); a pair that never had a row is a no-op.

    Raises ``ValueError`` for a self-grant, a missing/deleted account, or an
    activation while either account is inactive/unapproved.
    """
    if grantee_id == shared_id:
        raise ValueError("An account cannot grant access to itself")
    grantee = db.query(EndUser).filter(EndUser.id == grantee_id).first()
    shared = db.query(EndUser).filter(EndUser.id == shared_id).first()
    if grantee is None or shared is None:
        raise ValueError("Account not found")
    if grantee.deleted_at is not None or shared.deleted_at is not None:
        raise ValueError("A deleted account cannot participate in a grant")
    if active and not (account_live(grantee) and account_live(shared)):
        raise ValueError("Both accounts must be active and approved to activate a grant")

    row = (
        db.query(AclGrant)
        .filter(
            AclGrant.grantee_user_id == grantee_id,
            AclGrant.shared_user_id == shared_id,
        )
        .first()
    )
    if row is None:
        if not active:
            return None
        row = AclGrant(
            grantee_user_id=grantee_id,
            shared_user_id=shared_id,
            state=STATE_ACTIVE,
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(row)
    else:
        row.state = STATE_ACTIVE if active else STATE_INACTIVE
        row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "shared_user_id": shared.id,
        "shared_username": shared.username,
        "state": row.state,
    }


def inactivate_for_user(db: Session, user_id: int) -> int:
    """Inactivate every row where ``user_id`` is grantee OR shared.

    Activation of the account does not call the inverse: the operator must
    re-approve each grant by clicking its greyed tag (G78).
    """
    rows = (
        db.query(AclGrant)
        .filter(
            or_(
                AclGrant.grantee_user_id == user_id,
                AclGrant.shared_user_id == user_id,
            ),
            AclGrant.state == STATE_ACTIVE,
        )
        .all()
    )
    for row in rows:
        row.state = STATE_INACTIVE
        row.updated_at = _now()
    return len(rows)


def remove_for_user(db: Session, user_id: int) -> int:
    """Delete every grant row in both directions (account soft delete)."""
    rows = (
        db.query(AclGrant)
        .filter(
            or_(
                AclGrant.grantee_user_id == user_id,
                AclGrant.shared_user_id == user_id,
            )
        )
        .all()
    )
    count = len(rows)
    for row in rows:
        db.delete(row)
    return count


def migrate_legacy_grants(db: Session) -> dict[str, int]:
    """Convert ``allowed_special_users`` strings to ``acl_grants`` rows (R16).

    Runs once after the schema is created. Idempotent by construction: the
    unique pair prevents duplicates, and each converted source string is cleared
    so a retry has nothing left to do.

    * active row when both accounts are active/approved and not deleted;
    * inactive row when either account is inactive;
    * a target with no account row, or a soft-deleted target, is dropped with a
      log entry (decision 14);
    * a soft-deleted source account's rows are dropped with the source.
    """
    sources = (
        db.query(EndUser)
        .filter(
            EndUser.allowed_special_users.isnot(None),
            EndUser.allowed_special_users != "",
        )
        .all()
    )
    if not sources:
        return {"sources": 0, "created": 0, "inactive": 0, "dropped": 0}

    users = {u.username: u for u in db.query(EndUser).all()}
    created = inactive = dropped = 0
    for source in sources:
        names = parse_legacy(source.allowed_special_users)
        if source.deleted_at is not None:
            logger.warning(
                "acl migration: dropping %d grant(s) of soft-deleted account %r",
                len(names), source.username,
            )
            dropped += len(names)
            source.allowed_special_users = None
            continue
        for name in names:
            target = users.get(name)
            if target is None:
                logger.warning(
                    "acl migration: dropping dangling grant %r -> %r (no account)",
                    source.username, name,
                )
                dropped += 1
                continue
            if target.deleted_at is not None:
                logger.warning(
                    "acl migration: dropping grant %r -> %r (target soft-deleted)",
                    source.username, name,
                )
                dropped += 1
                continue
            if target.id == source.id:
                continue
            existing = (
                db.query(AclGrant)
                .filter(
                    AclGrant.grantee_user_id == source.id,
                    AclGrant.shared_user_id == target.id,
                )
                .first()
            )
            if existing is not None:
                continue
            state = (
                STATE_ACTIVE
                if account_live(source) and account_live(target)
                else STATE_INACTIVE
            )
            # INSERT OR IGNORE makes the four-worker startup race harmless: the
            # unique pair means the loser's row is already there, and its
            # source string is still cleared below.
            now = _now()
            result = db.execute(
                AclGrant.__table__.insert().prefix_with("OR IGNORE").values(
                    grantee_user_id=source.id,
                    shared_user_id=target.id,
                    state=state,
                    created_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount:
                created += 1
                if state == STATE_INACTIVE:
                    inactive += 1
        source.allowed_special_users = None
    db.commit()
    logger.info(
        "acl migration: %d source(s), %d row(s) created (%d inactive), %d dropped",
        len(sources), created, inactive, dropped,
    )
    return {
        "sources": len(sources),
        "created": created,
        "inactive": inactive,
        "dropped": dropped,
    }
