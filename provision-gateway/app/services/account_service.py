"""Account creation and the api_user saga (T6/R5.1-R5.4)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.gateway_setting import GatewaySetting
from ..models.user import User
from ..models.user_login import UserLogin
from . import auth_service
from .api_users_client import ApiUsersConflict, ApiUsersUnavailable, api_users

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utcnow():
    return datetime.now(timezone.utc)


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username or "--" in username or not NAME_RE.match(username):
        raise ValueError(
            "username must match ^[a-zA-Z0-9_-]+$ and must not contain '--'"
        )
    return username


def normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    email = email.strip().lower()
    if not email:
        return None
    if not EMAIL_RE.match(email):
        raise ValueError("email is not a valid address")
    return email


def _login_taken(db: Session, login: str) -> bool:
    return db.query(UserLogin).filter(UserLogin.login == login).first() is not None


def _live_api_user_name(db: Session, username: str) -> bool:
    return auth_service.api_user_username_taken(db, username)


def _add_login(db: Session, user: User, login: str, kind: str) -> None:
    db.add(UserLogin(user_id=user.id, login=login, kind=kind))


def create_account(
    db: Session,
    *,
    username: str,
    password: str,
    role: str,
    is_approved: bool,
    is_active: bool = True,
    is_public_shared: bool = False,
    email: str | None = None,
) -> User:
    """Create one unified account; the -api owns the api_users row.

    A live service-only api_user with this name is 409: self-registration can
    never adopt an owner's services (an admin uses ``adopt_owner``).
    """
    username = validate_username(username)
    email = normalize_email(email)
    if role not in auth_service.ROLES:
        raise ValueError("invalid role")
    if role == "special" and not is_approved:
        raise ValueError("a special account is created approved and active")
    if role != "special" and is_public_shared:
        raise ValueError("only a special account can be public")
    if len(password or "") < 4:
        raise ValueError("password too short (min 4 chars)")
    if _login_taken(db, username):
        raise ValueError(f"login '{username}' is already in use")
    if _live_api_user_name(db, username):
        raise ValueError(
            f"username '{username}' belongs to a live service-only owner; "
            "it needs an explicit admin adoption"
        )
    if email and _login_taken(db, email):
        raise ValueError(f"login '{email}' is already in use")

    ensured = api_users.ensure(username, mode="account")
    api_user_id = ensured["id"]
    if not ensured.get("created", True) and ensured.get("state") == "live":
        raise ValueError(
            f"username '{username}' belongs to a live service-only owner; "
            "it needs an explicit admin adoption"
        )

    user = User(
        id=uuid.uuid4(),
        api_user_id=uuid.UUID(str(api_user_id)),
        role=role,
        password_hash=auth_service.hash_password(password),
        is_approved=bool(is_approved),
        is_active=bool(is_active),
        is_public_shared=bool(is_public_shared),
        deletion_state="none",
        created_at=utcnow(),
        approved_at=utcnow() if is_approved else None,
    )
    db.add(user)
    _add_login(db, user, username, "username")
    if email:
        _add_login(db, user, email, "email")
    try:
        db.commit()
    except Exception:
        db.rollback()
        # The local insert failed: release the pending row we just ensured.
        try:
            api_users.release_pending(api_user_id)
        except Exception:
            pass
        raise
    db.refresh(user)
    # Claim after the local commit. A transient failure leaves a pending row
    # with a local users row, which the repair scan claims.
    try:
        api_users.claim(str(user.api_user_id))
    except Exception:
        pass
    return user


def create_public_viewer(db: Session, *, username: str, password: str,
                         email: str | None = None) -> User:
    return create_account(
        db, username=username, password=password, role="viewer",
        is_approved=False, is_active=True, email=email,
    )


def create_special(db: Session, *, username: str, password: str,
                   is_public_shared: bool, email: str | None = None) -> User:
    return create_account(
        db, username=username, password=password, role="special",
        is_approved=True, is_active=True, is_public_shared=is_public_shared,
        email=email,
    )


def bootstrap_admin(db: Session, *, username: str, password: str,
                    email: str | None = None) -> User:
    """Create exactly one admin when no live admin exists (local command only)."""
    if auth_service.has_any_admin(db):
        raise ValueError("a live admin already exists; bootstrap refuses")
    admin = create_account(
        db, username=username, password=password, role="admin",
        is_approved=True, is_active=True, email=email,
    )
    row = db.query(GatewaySetting).filter(GatewaySetting.key == "registration_open").first()
    if row is None:
        db.add(GatewaySetting(key="registration_open", value="true", updated_at=utcnow()))
    else:
        row.value = "true"
        row.updated_at = utcnow()
    db.commit()
    return admin


def repair_accounts(db: Session) -> dict:
    """Claim pending api_users rows and finalize pending deletions (R5.1/R5.8)."""
    from sqlalchemy import text
    from ..models.pre_deploy_session import PreDeploySession
    from ..models.stream_exposure import StreamExposure
    from .acl_service import remove_for_user

    claimed = finalized = 0
    pending = db.execute(
        text(
            "SELECT u.id, u.api_user_id FROM gateway.users u "
            "JOIN registry.api_users a ON a.id = u.api_user_id "
            "WHERE a.state = 'pending' AND a.deleted_at IS NULL"
        )
    ).mappings().all()
    for row in pending:
        try:
            api_users.claim(str(row["api_user_id"]))
            claimed += 1
        except Exception:
            continue
    pending_del = db.query(User).filter(User.deletion_state == "pending").all()
    for user in pending_del:
        try:
            api_users.soft_delete(str(user.api_user_id))
        except Exception:
            continue
        now = utcnow()
        remove_for_user(db, user.id)
        auth_service.delete_api_keys_for_user(db, user.id)
        db.query(PreDeploySession).filter(
            PreDeploySession.owner_api_user_id == user.api_user_id
        ).delete(synchronize_session=False)
        db.query(StreamExposure).filter(
            StreamExposure.owner_user_id == user.id,
            StreamExposure.revoked_at.is_(None),
        ).update({StreamExposure.revoked_at: now}, synchronize_session=False)
        user.is_active = False
        user.is_approved = False
        user.deletion_state = "final"
        user.deleted_at = now
        db.commit()
        finalized += 1
    return {"claimed": claimed, "finalized": finalized}


def adopt_owner(db: Session, api_user_id: str) -> User:
    """Give a live service-only owner a viewer account (admin action).

    The services stay attached; the password is unusable until the admin resets
    it. A taken username login is 409.
    """
    owner = api_users.get(api_user_id)
    if owner.get("deleted_at"):
        raise ValueError("owner is tombstoned")
    username = owner["username"]
    if _login_taken(db, username):
        raise ValueError(f"login '{username}' is already in use")
    if db.query(User).filter(User.api_user_id == uuid.UUID(str(api_user_id))).first():
        raise ValueError("this owner already has an account")
    user = User(
        id=uuid.uuid4(), api_user_id=uuid.UUID(str(api_user_id)), role="viewer",
        password_hash=auth_service.unusable_password_hash(),
        is_approved=True, is_active=True, is_public_shared=False,
        deletion_state="none", created_at=utcnow(), approved_at=utcnow(),
    )
    db.add(user)
    _add_login(db, user, username, "username")
    db.commit()
    db.refresh(user)
    return user
