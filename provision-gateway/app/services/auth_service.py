"""Auth service — admin CRUD, JWT creation/verification, password hashing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
import bcrypt as _bcrypt
from sqlalchemy.orm import Session

from ..config import settings
from ..models.admin import AdminUser


def _db_utcnow() -> datetime:
    """Naive UTC now, for comparing against SQLAlchemy DATETIME columns.

    SQLite's DATETIME storage does not preserve tzinfo — a value written as
    ``datetime.now(timezone.utc)`` is read back as a *naive* datetime. Any
    comparison against an aware ``now`` raises ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` (surfaced by the v4 default-key
    query on end-user login, QA1). JWT payloads keep using the aware
    ``datetime.now(timezone.utc)`` — only DB comparisons go through here.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db_naive(dt: datetime | None) -> datetime | None:
    """Normalize a datetime column value to naive UTC for Python-side comparison.

    SQLite round-trips ``DateTime`` columns without tzinfo, but a freshly
    created (uncommitted / mocked) ORM instance may still carry an aware
    ``datetime``. Normalizing both sides makes comparisons robust either way.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    return _bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, email: str, role: str, user_type: str = "admin") -> str:
    """Create a JWT access token. user_type is 'admin' or 'end_user'."""
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.JWT_EXPIRE_SEC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "access",
        "user_type": user_type,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: int, email: str, user_type: str = "admin") -> str:
    """Create a JWT refresh token (longer-lived)."""
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.JWT_REFRESH_EXPIRE_SEC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": "refresh",
        "user_type": user_type,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT token. Raises JWTError on failure."""
    return jwt.decode(
        token,
        settings.GATEWAY_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode an access token, ensuring it has type='access'."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise JWTError("Token is not an access token")
    return payload


def decode_gateway_token(token: str) -> dict[str, Any]:
    """Decode a token used for dashboard & gateway API access.

    v4 §6.1 (three-credential model): only ``type='provision'`` is accepted —
    the login//go/ provision_token cookie and the API-key header are both
    ``type='provision'``. Legacy ``type='access'`` (Bearer) / ``type='gateway'``
    (old gateway_token cookie) tokens are REJECTED (G5) — they no longer exist.
    """
    payload = decode_token(token)
    if payload.get("type") != "provision":
        raise JWTError("Token is not a provision token")
    return payload


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------

def get_admin_by_email(db: Session, email: str) -> AdminUser | None:
    """Find an admin user by email."""
    return db.query(AdminUser).filter(AdminUser.email == email).first()


def get_admin_by_id(db: Session, admin_id: int) -> AdminUser | None:
    """Find an admin user by ID."""
    return db.query(AdminUser).filter(AdminUser.id == admin_id).first()


def has_any_admin(db: Session) -> bool:
    """Check if at least one admin user exists."""
    return db.query(AdminUser).first() is not None


def create_admin(
    db: Session,
    email: str,
    password: str,
    role: str = "admin",
) -> AdminUser:
    """Create a new admin user.

    G4 (v4 §6.1.5): admins get a default API key at registration too, so login
    can bind their provision_token to a revocable key. The default key is
    created best-effort; if it fails the admin is still created and the key is
    auto-created on first login (get_or_create_default_key).
    """
    admin = AdminUser(
        email=email,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(admin)
    db.flush()
    try:
        create_api_key(db, admin.id, "Default", is_default=True)
    except Exception:
        pass
    db.commit()
    db.refresh(admin)
    return admin


def authenticate_admin(db: Session, email: str, password: str) -> AdminUser | None:
    """Authenticate an admin by email and password. Returns the admin or None."""
    admin = get_admin_by_email(db, email)
    if not admin or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    # Update last_login_at
    admin.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return admin


def change_password(
    db: Session,
    admin: AdminUser,
    current_password: str,
    new_password: str,
) -> bool:
    """Change an admin's password. Returns True on success."""
    if not verify_password(current_password, admin.password_hash):
        return False
    admin.password_hash = hash_password(new_password)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# End-User authentication
# ---------------------------------------------------------------------------

from ..models.end_user import EndUser


def get_end_user_by_username(db: Session, username: str) -> EndUser | None:
    """Find an end-user by username."""
    return db.query(EndUser).filter(EndUser.username == username).first()


def get_end_user_by_id(db: Session, user_id: int) -> EndUser | None:
    """Find an end-user by ID."""
    return db.query(EndUser).filter(EndUser.id == user_id).first()


def authenticate_end_user(db: Session, username: str, password: str) -> EndUser | None:
    """Authenticate an end-user by username and password. Returns the user or None."""
    user = get_end_user_by_username(db, username)
    if not user or not user.is_active:
        return None
    if not user.is_approved:
        return None  # Not yet approved by admin
    if not verify_password(password, user.password_hash):
        return None
    return user


def authenticate_user(db: Session, email_or_username: str, password: str) -> tuple[str, dict] | None:
    """
    Authenticate a user by checking both admins and end_users tables.
    Returns (user_type, user_dict) or None.
    user_type is 'admin' or 'end_user'.
    """
    # Try admin first
    admin = authenticate_admin(db, email_or_username, password)
    if admin:
        return ("admin", {"id": admin.id, "email": admin.email, "role": admin.role})

    # Try end-user
    end_user = authenticate_end_user(db, email_or_username, password)
    if end_user:
        return ("end_user", {"id": end_user.id, "username": end_user.username, "role": end_user.role, "is_approved": end_user.is_approved, "is_active": end_user.is_active})

    return None


# ---------------------------------------------------------------------------
# Provision token (v4 §6.1.0/§6.1.7 — 1-week JWT for service access, bound to
# the user's default API key's api_key_id)
# ---------------------------------------------------------------------------

PROVISION_TOKEN_TTL_SEC = 604800  # 1 week
API_KEY_TTL_SEC = 31536000        # 1 year (API keys are long-lived provision tokens)
MAX_API_KEYS_PER_USER = 1000      # v4 §6.1.6 cap

def create_provision_token(user_id: int, email: str, role: str, user_type: str = "end_user",
                           api_key_id: int | None = None) -> str:
    """Create a 1-week JWT provision token.

    This is the token used by provision-nginx to authenticate service access.
    Set as the provision_token cookie. Bound to the user's default key's
    ``api_key_id`` so it dies when that key is revoked/expires.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=PROVISION_TOKEN_TTL_SEC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "provision",
        "user_type": user_type,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if api_key_id is not None:
        payload["api_key_id"] = api_key_id
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_api_key_token(user_id: int, email: str, role: str, user_type: str,
                         api_key_id: int) -> str:
    """Create a 1-year API-key JWT provision token carrying its own api_key_id.

    The API key itself IS a long-lived provision token (v4 §6.1.1-6.1.3).
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=API_KEY_TTL_SEC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "provision",
        "user_type": user_type,
        "api_key_id": api_key_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_gateway_token(user_id: int, email: str, role: str, user_type: str = "end_user") -> str:
    """Create a short-lived JWT gateway token (24h expiry).

    This is the token used for dashboard/gateway API access.
    Set as the gateway_token cookie.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=86400)  # 24 hours
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "gateway",
        "user_type": user_type,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_exchange_code(user_id: int, email: str, role: str, user_type: str,
                         svc_host: str, redirect: str = "/") -> str:
    """Create a 30s HMAC-signed exchange code (v4 §6.2).

    Signed as a JWT with ``type='code'`` and a 30s exp. No provision_token
    JWT is ever placed in a URL (GAP-10).
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.EXCHANGE_CODE_TTL_SEC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "user_type": user_type,
        "svc_host": svc_host,
        "redirect": redirect,
        "type": "code",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_exchange_code(token: str) -> dict[str, Any]:
    """Verify a 30s exchange code. Raises JWTError on invalid/expired."""
    payload = decode_token(token)
    if payload.get("type") != "code":
        raise JWTError("Not an exchange code")
    return payload


def verify_provision_token(token: str) -> dict[str, Any]:
    """Verify a provision token (used by /api/auth/verify nginx subrequest).

    Returns the decoded payload on success.
    Raises JWTError if expired or invalid.
    Revocation check covers key existence, ``is_revoked`` AND ``expires_at``
    (v4 §6.1.4, review R1/GAP-06). This runs for every token type — including
    admin tokens — because login//go/ provision tokens are bound to a key's
    api_key_id.
    """
    payload = decode_token(token)
    # Check for revocation via api_key_id
    api_key_id = payload.get("api_key_id")
    if api_key_id is not None:
        from ..models.api_key import ApiKey
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            key = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
            if key is None or key.is_revoked:
                raise JWTError("API key has been revoked")
            if key.expires_at is not None and _db_naive(key.expires_at) <= _db_utcnow():
                raise JWTError("API key has expired")
        finally:
            db.close()
    return payload


# ---------------------------------------------------------------------------
# API Key helpers
# ---------------------------------------------------------------------------

from ..models.api_key import ApiKey
from ..models.admin import AdminUser
import hashlib


def _hash_token(token: str) -> str:
    """Hash an API token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def _target_identity(db: Session, user_id: int) -> tuple[str, str, str]:
    """Return (user_type, email, role) for a key's target user.

    API keys may target end_users or admins (GAP-07: mint with the target's
    real user_type so admin keys are not dead on arrival).
    """
    end_user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if end_user is not None:
        return ("end_user", end_user.username, end_user.role or "viewer")
    admin = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if admin is not None:
        return ("admin", admin.email, admin.role or "admin")
    return ("end_user", "", "viewer")


def _lazy_evict_api_keys(db: Session, user_id: int) -> int:
    """Lazy eviction: drop the user's oldest revoked/expired keys to make room.

    Returns the number of keys evicted (v4 §6.1.6).
    """
    now = _db_utcnow()
    removable = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id)
        .filter((ApiKey.is_revoked.is_(True)) | (ApiKey.expires_at <= now))
        .order_by(ApiKey.created_at.asc())
        .all()
    )
    evicted = 0
    for key in removable:
        db.delete(key)
        evicted += 1
    if evicted:
        db.commit()
    return evicted


def create_api_key(db: Session, user_id: int, label: str,
                   is_default: bool = False) -> tuple[ApiKey, str]:
    """Create a new API key — a 1-year JWT provision token carrying its own
    api_key_id. Only the hash + a short mask are stored (v4 §6.1.1-6.1.3).

    Enforces a 1000-key cap with lazy eviction (v4 §6.1.6). Returns (ApiKey, raw_token).
    """
    # --- Cap check with lazy eviction ---
    count = db.query(ApiKey).filter(ApiKey.user_id == user_id).count()
    if count >= MAX_API_KEYS_PER_USER:
        _lazy_evict_api_keys(db, user_id)
        count = db.query(ApiKey).filter(ApiKey.user_id == user_id).count()
        if count >= MAX_API_KEYS_PER_USER:
            raise ValueError("API key limit reached (1000 keys per user)")

    # G2: when creating a DEFAULT key, clear any (stale) existing default for
    # the user first — the partial unique index on (user_id) WHERE is_default
    # would otherwise reject the insert (e.g. a re-registered user inheriting a
    # default orphaned by SQLite id-reuse).
    if is_default:
        db.query(ApiKey).filter(
            ApiKey.user_id == user_id, ApiKey.is_default.is_(True)
        ).update({"is_default": False})

    # Resolve the target's real identity for the token (GAP-07).
    user_type, email, role = _target_identity(db, user_id)

    # Insert row first so we can bind its id into the token.
    key = ApiKey(
        user_id=user_id,
        label=label,
        is_default=is_default,
        token_hash="",
        mask="",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=API_KEY_TTL_SEC),
    )
    db.add(key)
    db.flush()

    raw = create_api_key_token(user_id, email, role, user_type, api_key_id=key.id)
    key.token_hash = _hash_token(raw)
    key.mask = raw[-8:]  # short display mask
    db.commit()
    db.refresh(key)
    return key, raw


def set_default_api_key(db: Session, key_id: int) -> bool:
    """Mark *key_id* as the user's default (and un-default others). Returns True on success."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    # Clear any existing default for this user
    db.query(ApiKey).filter(ApiKey.user_id == key.user_id).update({"is_default": False})
    key.is_default = True
    db.commit()
    return True


def get_or_create_default_key(db: Session, user_id: int) -> ApiKey:
    """Return the user's default API key, creating/auto-promoting as needed.

    - If a valid default exists, return it.
    - If the default is revoked/expired, auto-promote the oldest valid key or
      auto-create a new default (D7: login is never locked out).
    """
    now = _db_utcnow()

    default = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.is_default.is_(True))
        .first()
    )
    if default is not None and not default.is_revoked and _db_naive(default.expires_at) > now:
        return default

    # Promote oldest valid key to default if one exists.
    valid = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.is_revoked.is_(False))
        .filter(ApiKey.expires_at > now)
        .order_by(ApiKey.created_at.asc())
        .all()
    )
    if valid:
        for k in valid:
            k.is_default = False
        valid[0].is_default = True
        db.commit()
        db.refresh(valid[0])
        return valid[0]

    # No valid key: create a new default.
    key, _ = create_api_key(db, user_id, "Default", is_default=True)
    return key


def list_api_keys(db: Session, user_id: int | None = None) -> list[ApiKey]:
    """List API keys. If user_id is None, return all (admin)."""
    q = db.query(ApiKey)
    if user_id is not None:
        q = q.filter(ApiKey.user_id == user_id)
    return q.order_by(ApiKey.is_default.desc(), ApiKey.created_at.desc()).all()


def revoke_api_key(db: Session, key_id: int, allow_default: bool = False) -> bool:
    """Revoke an API key by ID. Returns True if found and revoked.

    Reject revoking a default key with ``allow_default=False`` (v4 §6.1.5).
    Raises ``ValueError`` if the key is the user's default and not allowed.
    """
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    if key.is_default and not allow_default:
        raise ValueError("Cannot revoke the default API key. Set another key as default first.")
    key.is_revoked = True
    db.commit()
    return True


def get_api_key_by_id(db: Session, key_id: int) -> ApiKey | None:
    """Get an API key by its ID."""
    return db.query(ApiKey).filter(ApiKey.id == key_id).first()


def user_has_default_key(db: Session, user_id: int) -> bool:
    """Return True if *user_id* currently owns a default key (v4 §6.1.6)."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.is_default.is_(True))
        .first()
    ) is not None


def delete_api_keys_for_user(db: Session, user_id: int) -> int:
    """Delete every API key owned by *user_id* (used when deleting the user).

    v4 §6.1.3 / G2: user deletion must cascade to api_keys, otherwise SQLite
    id-reuse orphans stale ``is_default=1`` rows onto the next user. Returns the
    number of keys deleted.
    """
    keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).all()
    count = len(keys)
    for key in keys:
        db.delete(key)
    if count:
        db.commit()
    return count


def create_default_api_key(db: Session, user_id: int) -> tuple[ApiKey, str]:
    """Create a default API key for a user (called at registration / first login)."""
    return create_api_key(db, user_id, "Default", is_default=True)


def backfill_default_keys(db: Session) -> int:
    """Backfill an is_default flag for existing users that lack a default key.

    Picks the user's oldest valid key as default; creates one if none exist
    (v4 §6.1.3/6.1.5, review GAP-25). Returns the number of users backfilled.
    """
    now = _db_utcnow()
    user_ids = {k.user_id for k in db.query(ApiKey.user_id).all()}
    backfilled = 0
    for uid in user_ids:
        has_default = (
            db.query(ApiKey)
            .filter(ApiKey.user_id == uid, ApiKey.is_default.is_(True))
            .first()
        )
        if has_default is not None:
            continue
        valid = (
            db.query(ApiKey)
            .filter(ApiKey.user_id == uid, ApiKey.is_revoked.is_(False))
            .filter(ApiKey.expires_at > now)
            .order_by(ApiKey.created_at.asc())
            .all()
        )
        if valid:
            valid[0].is_default = True
            db.commit()
            backfilled += 1
        else:
            try:
                create_api_key(db, uid, "Default", is_default=True)
                backfilled += 1
            except Exception:
                pass
    return backfilled
