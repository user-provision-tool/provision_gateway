"""Unified auth service (T6).

One `users` table and one `api_users` row per viewer/special/admin; one
`user_logins` row per unique login string (username mandatory, email optional).
Tokens carry only `sub=users.id` (UUID), `api_key_id`, `type`, `exp` and
`token_epoch` — authority is the current row, never a claim.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..config import settings
from ..models.api_key import ApiKey
from ..models.gateway_setting import GatewaySetting
from ..models.user import User
from ..models.user_login import UserLogin

PROVISION_TOKEN_TTL_SEC = 604800
API_KEY_TTL_SEC = 31536000
MAX_API_KEYS_PER_USER = 1000
ROLES = ("viewer", "special", "admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return _bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def unusable_password_hash() -> str:
    """A random hash no password can satisfy (adopted owners before reset)."""
    return hash_password(uuid.uuid4().hex + uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# Token epoch
# ---------------------------------------------------------------------------

def token_epoch(db: Session) -> int:
    row = db.query(GatewaySetting).filter(GatewaySetting.key == "token_epoch").first()
    try:
        return int(row.value) if row else 0
    except (TypeError, ValueError):
        return 0


def bump_token_epoch(db: Session) -> int:
    current = token_epoch(db)
    row = db.query(GatewaySetting).filter(GatewaySetting.key == "token_epoch").first()
    if row is None:
        row = GatewaySetting(key="token_epoch", value=str(current + 1), updated_at=utcnow())
        db.add(row)
    else:
        row.value = str(current + 1)
        row.updated_at = utcnow()
    db.commit()
    return current + 1


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def _claims(user_id, api_key_id: int | None, kind: str, ttl: int, db: Session) -> dict:
    return {
        "sub": str(user_id),
        "api_key_id": api_key_id,
        "type": kind,
        "token_epoch": token_epoch(db),
        "exp": utcnow() + timedelta(seconds=ttl),
        "iat": utcnow(),
    }


def create_provision_token(db: Session, user_id, api_key_id: int | None = None) -> str:
    payload = _claims(user_id, api_key_id, "provision", PROVISION_TOKEN_TTL_SEC, db)
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_api_key_token(db: Session, user_id, api_key_id: int) -> str:
    payload = _claims(user_id, api_key_id, "provision", API_KEY_TTL_SEC, db)
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_exchange_code(db: Session, user_id, svc_host: str, redirect: str = "/") -> str:
    payload = {
        "sub": str(user_id),
        "svc_host": svc_host,
        "redirect": redirect,
        "type": "code",
        "exp": utcnow() + timedelta(seconds=settings.EXCHANGE_CODE_TTL_SEC),
        "iat": utcnow(),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.GATEWAY_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def decode_gateway_token(token: str) -> dict[str, Any]:
    payload = decode_token(token)
    if payload.get("type") != "provision":
        raise JWTError("Token is not a provision token")
    return payload


def verify_exchange_code(token: str) -> dict[str, Any]:
    payload = decode_token(token)
    if payload.get("type") != "code":
        raise JWTError("Not an exchange code")
    return payload


def verify_provision_token(db: Session, token: str) -> dict[str, Any]:
    """Decode + validate revocation, expiry, key binding and token epoch."""
    payload = decode_token(token)
    if payload.get("type") != "provision":
        raise JWTError("Token is not a provision token")
    if int(payload.get("token_epoch", -1)) != token_epoch(db):
        raise JWTError("Token epoch is stale")
    api_key_id = payload.get("api_key_id")
    if api_key_id is None:
        raise JWTError("Token has no API key binding")
    key = db.query(ApiKey).filter(ApiKey.id == int(api_key_id)).first()
    if key is None or key.is_revoked:
        raise JWTError("API key has been revoked")
    if str(key.user_id) != str(payload.get("sub")):
        raise JWTError("API key is not bound to this subject")
    if key.expires_at is not None and key.expires_at <= utcnow():
        raise JWTError("API key has expired")
    return payload


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------

def username_for(db: Session, user_id) -> str | None:
    row = (
        db.query(UserLogin)
        .filter(UserLogin.user_id == user_id, UserLogin.kind == "username")
        .first()
    )
    return row.login if row else None


def email_for(db: Session, user_id) -> str | None:
    row = (
        db.query(UserLogin)
        .filter(UserLogin.user_id == user_id, UserLogin.kind == "email")
        .first()
    )
    return row.login if row else None


def get_user_by_id(db: Session, user_id) -> User | None:
    try:
        return db.query(User).filter(User.id == uuid.UUID(str(user_id))).first()
    except (ValueError, TypeError):
        return None


def get_user_by_username(db: Session, username: str) -> User | None:
    login = db.query(UserLogin).filter(UserLogin.login == username).first()
    return get_user_by_id(db, login.user_id) if login else None


def get_user_by_login(db: Session, login: str) -> User | None:
    row = db.query(UserLogin).filter(UserLogin.login == login).first()
    return get_user_by_id(db, row.user_id) if row else None


def has_any_admin(db: Session) -> bool:
    return db.query(User).filter(User.role == "admin", User.is_active.is_(True),
                                 User.deleted_at.is_(None)).first() is not None


def live_admin_count(db: Session) -> int:
    return db.query(func.count(User.id)).filter(
        User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None)
    ).scalar() or 0


def authenticate_user(db: Session, login: str, password: str) -> User | None:
    user = get_user_by_login(db, login)
    if user is None or not user.account_live:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    db.commit()
    return user


def change_password(db: Session, user: User, current_password: str, new_password: str) -> bool:
    if not verify_password(current_password, user.password_hash):
        return False
    user.password_hash = hash_password(new_password)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Login-string uniqueness (cross-store)
# ---------------------------------------------------------------------------

def login_owner(db: Session, login: str) -> str | None:
    """The user id that owns *login*, if any (soft-deleted accounts keep it)."""
    row = db.query(UserLogin).filter(UserLogin.login == login).first()
    return str(row.user_id) if row else None


def api_user_username_taken(db: Session, username: str) -> bool:
    """A live service-only api_user owns this username (read-only registry)."""
    row = db.execute(
        text("SELECT 1 FROM registry.api_users WHERE username = :u AND deleted_at IS NULL"),
        {"u": username},
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# API keys (one id space)
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _evict(db: Session, user_id) -> int:
    now = utcnow()
    removable = db.query(ApiKey).filter(
        ApiKey.user_id == user_id,
        (ApiKey.is_revoked.is_(True)) | (ApiKey.expires_at <= now),
    ).order_by(ApiKey.created_at.asc()).all()
    for key in removable:
        db.delete(key)
    if removable:
        db.commit()
    return len(removable)


def create_api_key(db: Session, user_id, label: str, is_default: bool = False) -> tuple[ApiKey, str]:
    user = get_user_by_id(db, user_id)
    if user is None or user.role == "special" or not user.account_live:
        raise ValueError("this account is not a valid key owner")
    count = db.query(ApiKey).filter(ApiKey.user_id == user.id).count()
    if count >= MAX_API_KEYS_PER_USER:
        _evict(db, user.id)
        count = db.query(ApiKey).filter(ApiKey.user_id == user.id).count()
        if count >= MAX_API_KEYS_PER_USER:
            raise ValueError("API key limit reached (1000 keys per user)")
    if is_default:
        db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_default.is_(True)).update(
            {"is_default": False}
        )
    key = ApiKey(
        user_id=user.id, label=label, is_default=is_default, token_hash="", mask="",
        created_at=utcnow(), expires_at=utcnow() + timedelta(seconds=API_KEY_TTL_SEC),
        is_revoked=False,
    )
    db.add(key)
    db.flush()
    raw = create_api_key_token(db, user.id, key.id)
    key.token_hash = _hash_token(raw)
    key.mask = raw[-8:]
    db.commit()
    db.refresh(key)
    return key, raw


def get_or_create_default_key(db: Session, user_id) -> ApiKey:
    now = utcnow()
    default = db.query(ApiKey).filter(
        ApiKey.user_id == user_id, ApiKey.is_default.is_(True)
    ).first()
    if default is not None and not default.is_revoked and default.expires_at > now:
        return default
    valid = db.query(ApiKey).filter(
        ApiKey.user_id == user_id, ApiKey.is_revoked.is_(False), ApiKey.expires_at > now
    ).order_by(ApiKey.created_at.asc()).all()
    if valid:
        for k in valid:
            k.is_default = False
        valid[0].is_default = True
        db.commit()
        db.refresh(valid[0])
        return valid[0]
    key, _ = create_api_key(db, user_id, "Default", is_default=True)
    return key


def list_api_keys(db: Session, user_id=None) -> list[ApiKey]:
    q = db.query(ApiKey)
    if user_id is not None:
        q = q.filter(ApiKey.user_id == user_id)
    return q.order_by(ApiKey.is_default.desc(), ApiKey.created_at.desc()).all()


def set_default_api_key(db: Session, key_id: int) -> bool:
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    db.query(ApiKey).filter(ApiKey.user_id == key.user_id).update({"is_default": False})
    key.is_default = True
    db.commit()
    return True


def revoke_api_key(db: Session, key_id: int, allow_default: bool = False) -> bool:
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    if key.is_default and not allow_default:
        raise ValueError("Cannot revoke the default API key. Set another key as default first.")
    key.is_revoked = True
    db.commit()
    return True


def get_api_key_by_id(db: Session, key_id: int) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.id == key_id).first()


def user_has_default_key(db: Session, user_id) -> bool:
    return db.query(ApiKey).filter(
        ApiKey.user_id == user_id, ApiKey.is_default.is_(True)
    ).first() is not None


def delete_api_keys_for_user(db: Session, user_id) -> int:
    keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).all()
    for key in keys:
        db.delete(key)
    if keys:
        db.commit()
    return len(keys)


def acl_allows(db: Session, user, target_username: str) -> bool:
    """Compatibility façade over the ONE ACL matrix."""
    from .acl_service import allows_username
    return allows_username(db, user, target_username)


def backfill_default_keys(db: Session) -> int:
    """Give every live viewer/admin a default key; skip special/inactive."""
    backfilled = 0
    users = db.query(User).filter(
        User.role.in_(("viewer", "admin")), User.is_active.is_(True),
        User.is_approved.is_(True), User.deleted_at.is_(None),
        User.deletion_state == "none",
    ).all()
    for user in users:
        if user_has_default_key(db, user.id):
            continue
        # Never MINT at startup: imported keys are revoked and a usable key is
        # issued only after a fresh login (R5.5). Promote an existing valid key
        # if one exists; otherwise leave the account keyless until login.
        now = utcnow()
        valid = db.query(ApiKey).filter(
            ApiKey.user_id == user.id, ApiKey.is_revoked.is_(False),
            ApiKey.expires_at > now,
        ).order_by(ApiKey.created_at.asc()).first()
        if valid is not None:
            valid.is_default = True
            db.commit()
            backfilled += 1
    return backfilled
