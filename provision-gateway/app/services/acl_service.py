"""ACL grants — one ID-keyed matrix for the HTTP gate and stream verdict (T9).

Both columns reference ``users.id``. The shared side of a grant must be a live
``special`` or ``admin`` account (R5.6).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.acl_grant import STATE_ACTIVE, STATE_INACTIVE, AclGrant
from ..models.audit_log import AuditLog
from ..models.user import User
from ..models.user_login import UserLogin

logger = logging.getLogger(__name__)

PUBLIC_VERDICT_HEADER = "X-Public-Service"
PUBLIC_VERDICT_VALUE = "1"


def _now():
    return datetime.now(timezone.utc)


def username_of(db: Session, user_id) -> str | None:
    row = db.query(UserLogin).filter(
        UserLogin.user_id == user_id, UserLogin.kind == "username"
    ).first()
    return row.login if row else None


def user_by_username(db: Session, username: str) -> User | None:
    row = db.query(UserLogin).filter(UserLogin.login == username).first()
    return db.query(User).filter(User.id == row.user_id).first() if row else None


def account_live(user: User | None) -> bool:
    return bool(user is not None and user.account_live)


def is_public_owner(user: User | None) -> bool:
    return bool(
        user is not None
        and user.role == "special"
        and user.is_public_shared
        and user.account_live
    )


def was_public_record(db: Session, user: User | None) -> bool:
    if user is None:
        return False
    if user.is_public_shared:
        return True
    if user.deleted_at is None or user.role != "special":
        return False
    username = username_of(db, user.id)
    row = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "public_flag",
            AuditLog.target_user == username,
            AuditLog.status == "success",
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    if row is None:
        return False
    detail = row.detail_json if isinstance(row.detail_json, dict) else {}
    return bool(detail.get("is_public_shared"))


def owner_is_gone(user: User | None) -> bool:
    return user is None or user.deleted_at is not None


def allows(db: Session, grantee: User | None, shared_user_id) -> bool:
    """Own service, or an active grant whose both ends are live."""
    if not account_live(grantee) or shared_user_id is None:
        return False
    if str(grantee.id) == str(shared_user_id):
        return True
    shared = db.query(User).filter(User.id == shared_user_id).first()
    if not account_live(shared):
        return False
    return db.query(AclGrant).filter(
        AclGrant.grantee_user_id == grantee.id,
        AclGrant.shared_user_id == shared.id,
        AclGrant.state == STATE_ACTIVE,
    ).first() is not None


def allows_username(db: Session, grantee: User | None, target_username: str) -> bool:
    if not target_username:
        return False
    target = user_by_username(db, target_username)
    if target is None:
        return False
    return allows(db, grantee, target.id)


def allowed_user_ids(db: Session, grantee_id) -> set[str]:
    """Own id plus every shared account with an active, live grant."""
    grantee = db.query(User).filter(User.id == grantee_id).first()
    if grantee is None or not grantee.account_live:
        return set()
    ids = {str(grantee.id)}
    rows = (
        db.query(AclGrant, User)
        .join(User, AclGrant.shared_user_id == User.id)
        .filter(AclGrant.grantee_user_id == grantee.id, AclGrant.state == STATE_ACTIVE)
        .all()
    )
    for _grant, shared in rows:
        if shared.account_live:
            ids.add(str(shared.id))
    return ids


def allowed_usernames(db: Session, grantee_id) -> set[str]:
    ids = allowed_user_ids(db, grantee_id)
    out: set[str] = set()
    for uid in ids:
        name = username_of(db, uid)
        if name:
            out.add(name)
    return out


def list_for_grantee(db: Session, grantee_id) -> list[dict]:
    rows = (
        db.query(AclGrant, User)
        .join(User, AclGrant.shared_user_id == User.id)
        .filter(AclGrant.grantee_user_id == grantee_id)
        .all()
    )
    out = []
    for grant, shared in rows:
        out.append({
            "id": grant.id,
            "shared_user_id": str(shared.id),
            "shared_username": username_of(db, shared.id),
            "is_public_shared": bool(shared.is_public_shared),
            "role": shared.role,
            "state": grant.state,
        })
    out.sort(key=lambda r: r["shared_username"] or "")
    return out


def candidates(db: Session, exclude_user_id=None) -> list[dict]:
    q = db.query(User).filter(
        User.role.in_(("special", "admin")),
        User.is_approved.is_(True), User.deleted_at.is_(None),
    )
    if exclude_user_id is not None:
        q = q.filter(User.id != exclude_user_id)
    out = []
    for u in q.all():
        out.append({
            "id": str(u.id),
            "username": username_of(db, u.id),
            "role": u.role,
            "is_active": bool(u.is_active),
            "is_public_shared": bool(u.is_public_shared),
        })
    out.sort(key=lambda r: r["username"] or "")
    return out


def set_grant(db: Session, grantee_id, shared_id, active: bool) -> dict | None:
    if str(grantee_id) == str(shared_id):
        raise ValueError("An account cannot grant access to itself")
    grantee = db.query(User).filter(User.id == grantee_id).first()
    shared = db.query(User).filter(User.id == shared_id).first()
    if grantee is None or shared is None:
        raise ValueError("Account not found")
    if grantee.deleted_at is not None or shared.deleted_at is not None:
        raise ValueError("A deleted account cannot participate in a grant")
    if shared.role not in ("special", "admin"):
        raise ValueError("The shared side must be a live special or admin account")
    if active and not (account_live(grantee) and account_live(shared)):
        raise ValueError("Both accounts must be active and approved to activate a grant")
    row = db.query(AclGrant).filter(
        AclGrant.grantee_user_id == grantee_id,
        AclGrant.shared_user_id == shared_id,
    ).first()
    if row is None:
        if not active:
            return None
        row = AclGrant(grantee_user_id=grantee_id, shared_user_id=shared_id,
                       state=STATE_ACTIVE, created_at=_now(), updated_at=_now())
        db.add(row)
    else:
        row.state = STATE_ACTIVE if active else STATE_INACTIVE
        row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return {"id": row.id, "shared_user_id": str(shared.id),
            "shared_username": username_of(db, shared.id), "state": row.state}


def inactivate_for_user(db: Session, user_id) -> int:
    rows = db.query(AclGrant).filter(
        or_(AclGrant.grantee_user_id == user_id, AclGrant.shared_user_id == user_id),
        AclGrant.state == STATE_ACTIVE,
    ).all()
    for row in rows:
        row.state = STATE_INACTIVE
        row.updated_at = _now()
    return len(rows)


def remove_for_user(db: Session, user_id) -> int:
    rows = db.query(AclGrant).filter(
        or_(AclGrant.grantee_user_id == user_id, AclGrant.shared_user_id == user_id)
    ).all()
    for row in rows:
        db.delete(row)
    return len(rows)
