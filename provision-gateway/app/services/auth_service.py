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
        create_api_key(db, admin.id, "Default", is_default=True, target_type="admin")
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
    if not user or not user.is_active or user.deleted_at is not None:
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


# ---------------------------------------------------------------------------
# The ACL matrix — ONE definition, two callers
# ---------------------------------------------------------------------------


def parse_allowed_special_users(raw: str | None) -> list[str]:
    """Parse the RETIRED comma-separated list, trimming (N1).

    Kept only for the one-time migration and the historical parser test; no
    authorization path reads it. The ACL matrix is ``acl_service.allows``.
    """
    from .acl_service import parse_legacy
    return parse_legacy(raw)


def acl_allows(db: Session, end_user, target_user_name: str) -> bool:
    """Does ``end_user`` reach an instance owned by ``target_user_name``?

    This IS the ACL matrix: the rule the HTTP gate applies at
    ``routers/auth.py::verify_auth`` to an (end user, target instance) pair, and
    the rule the stream verdict re-applies per connection at
    ``services/stream_service.py::verify``. One function on purpose — GRILLING
    **G39** §3 requires the stream path to evaluate "the same ACL matrix the HTTP
    path uses" (task-doc R3.3), and two copies of a security rule drift.

    The matrix is ID-keyed (GRILLING G78): own instance by username, or an
    active ``acl_grants`` row whose grantee and shared accounts are both live.
    The legacy comma-separated string is never consulted.
    """
    from .acl_service import allows
    return allows(db, end_user, target_user_name)


TARGET_TYPES = ("end_user", "admin")


def format_target_ref(user_id: int, target_type: str = "end_user") -> str:
    """Render the canonical prefixed target reference, e.g. ``admin:1``."""
    return f"{target_type}:{user_id}"


def parse_target_ref(ref) -> tuple[int, str]:
    """Parse a key target reference into ``(user_id, target_type)``.

    The reference is PREFIXED: ``end_user:7`` or ``admin:1``. A bare integer (or
    bare digit string) means ``end_user`` — the pre-prefix syntax, kept so every
    existing caller, stored row and API client keeps working unchanged.

    The prefix is what makes the target unambiguous: ``admins`` and ``end_users``
    have separate id spaces, so ``1`` alone names two different people.
    """
    if isinstance(ref, bool):  # bool is an int subclass; never a valid id
        raise ValueError("Invalid target id")
    if isinstance(ref, int):
        return ref, "end_user"
    text_ref = str(ref or "").strip()
    if not text_ref:
        raise ValueError("Invalid target id: empty")
    if ":" in text_ref:
        prefix, _, number = text_ref.partition(":")
        prefix = prefix.strip().lower()
        if prefix not in TARGET_TYPES:
            raise ValueError(
                f"Unknown target type '{prefix}' (expected one of: {', '.join(TARGET_TYPES)})"
            )
        number = number.strip()
    else:
        prefix, number = "end_user", text_ref
    if not number.isdigit():
        raise ValueError(f"Invalid target id '{number}': expected an integer")
    return int(number), prefix


def _target_identity(db: Session, user_id: int,
                     target_type: str = "end_user") -> tuple[str, str, str]:
    """Return (user_type, email, role) for a key's target user.

    ``target_type`` names the id space EXPLICITLY. API keys may target end_users
    or admins (GAP-07: mint with the target's real user_type so admin keys are
    not dead on arrival) — but the two tables' ids collide (id 1 is a different
    person in each), so resolving by id alone is a guess. That guess used to
    prefer ``EndUser``, which silently minted an admin's "own" key for whichever
    end user held the same id. Unknown targets raise rather than falling back to
    a nameless identity.
    """
    if target_type == "admin":
        admin = db.query(AdminUser).filter(AdminUser.id == user_id).first()
        if admin is None:
            raise ValueError(f"No admin with id {user_id}")
        return ("admin", admin.email, admin.role or "admin")
    end_user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if end_user is None or end_user.deleted_at is not None:
        raise ValueError(f"No end user with id {user_id}")
    return ("end_user", end_user.username, end_user.role or "viewer")


def _lazy_evict_api_keys(db: Session, user_id: int, target_type: str = "end_user") -> int:
    """Lazy eviction: drop the user's oldest revoked/expired keys to make room.

    Scoped to the target's id space, like every other key query: the same
    numeric id exists for an end user and an admin.

    Returns the number of keys evicted (v4 §6.1.6).
    """
    now = _db_utcnow()
    removable = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.target_type == target_type)
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
                   is_default: bool = False,
                   target_type: str = "end_user") -> tuple[ApiKey, str]:
    """Create a new API key — a 1-year JWT provision token carrying its own
    api_key_id. Only the hash + a short mask are stored (v4 §6.1.1-6.1.3).

    ``target_type`` names the id space the key targets (``end_user`` | ``admin``);
    it is stored on the row and the token so the key can say which space its
    ``user_id`` is in. Every filter here carries both halves — the id alone is
    not unique across the two id spaces.

    Enforces a 1000-key cap with lazy eviction (v4 §6.1.6). Returns (ApiKey, raw_token).
    """
    # --- Cap check with lazy eviction ---
    count = db.query(ApiKey).filter(
        ApiKey.user_id == user_id, ApiKey.target_type == target_type
    ).count()
    if count >= MAX_API_KEYS_PER_USER:
        _lazy_evict_api_keys(db, user_id, target_type)
        count = db.query(ApiKey).filter(
            ApiKey.user_id == user_id, ApiKey.target_type == target_type
        ).count()
        if count >= MAX_API_KEYS_PER_USER:
            raise ValueError("API key limit reached (1000 keys per user)")

    # G2: when creating a DEFAULT key, clear any (stale) existing default for
    # the target first — the partial unique index on (user_id, target_type)
    # WHERE is_default would otherwise reject the insert (e.g. a re-registered
    # user inheriting a default orphaned by SQLite id-reuse).
    if is_default:
        db.query(ApiKey).filter(
            ApiKey.user_id == user_id,
            ApiKey.target_type == target_type,
            ApiKey.is_default.is_(True),
        ).update({"is_default": False})

    # Resolve the target's real identity for the token (GAP-07 + the prefix).
    user_type, email, role = _target_identity(db, user_id, target_type)

    # Insert row first so we can bind its id into the token.
    key = ApiKey(
        user_id=user_id,
        target_type=target_type,
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
    """Mark *key_id* as its target's default (un-defaulting the target's others).

    Scoped to ``(user_id, target_type)``: promoting an admin's key must not
    un-default the end user who happens to share the numeric id. Returns True on
    success.
    """
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    # Clear any existing default for this target (both halves of the reference).
    db.query(ApiKey).filter(
        ApiKey.user_id == key.user_id,
        ApiKey.target_type == key.target_type,
    ).update({"is_default": False})
    key.is_default = True
    db.commit()
    return True


def get_or_create_default_key(db: Session, user_id: int,
                              target_type: str = "end_user") -> ApiKey:
    """Return the target's default API key, creating/auto-promoting as needed.

    ``target_type`` is the caller's OWN space (login/exchange pass their token's
    ``user_type``): without it an admin's login would bind to the end user who
    shares their id.

    - If a valid default exists, return it.
    - If the default is revoked/expired, auto-promote the oldest valid key or
      auto-create a new default (D7: login is never locked out).
    """
    now = _db_utcnow()

    default = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.target_type == target_type,
                ApiKey.is_default.is_(True))
        .first()
    )
    if default is not None and not default.is_revoked and _db_naive(default.expires_at) > now:
        return default

    # Promote oldest valid key to default if one exists.
    valid = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.target_type == target_type,
                ApiKey.is_revoked.is_(False))
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
    key, _ = create_api_key(db, user_id, "Default", is_default=True,
                            target_type=target_type)
    return key


def list_api_keys(db: Session, user_id: int | None = None,
                  target_type: str | None = None) -> list[ApiKey]:
    """List API keys.

    ``user_id is None`` returns all keys (the admin view). Otherwise the filter
    carries BOTH halves of the reference — an id-only filter would show an
    admin's keys to the end user who shares their id (and vice versa).
    """
    q = db.query(ApiKey)
    if user_id is not None:
        q = q.filter(ApiKey.user_id == user_id,
                     ApiKey.target_type == (target_type or "end_user"))
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


def user_has_default_key(db: Session, user_id: int,
                         target_type: str = "end_user") -> bool:
    """Return True if the target currently owns a default key (v4 §6.1.6)."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.target_type == target_type,
                ApiKey.is_default.is_(True))
        .first()
    ) is not None


def delete_api_keys_for_user(db: Session, user_id: int,
                             target_type: str = "end_user") -> int:
    """Delete every API key owned by the target (used when deleting the user).

    Scoped to the id space: deleting end user 1 must not take the admin 1's keys
    with it. v4 §6.1.3 / G2: user deletion must cascade to api_keys, otherwise
    SQLite id-reuse orphans stale ``is_default=1`` rows onto the next user.
    Returns the number of keys deleted.
    """
    keys = db.query(ApiKey).filter(ApiKey.user_id == user_id,
                                   ApiKey.target_type == target_type).all()
    count = len(keys)
    for key in keys:
        db.delete(key)
    if count:
        db.commit()
    return count


def create_default_api_key(db: Session, user_id: int,
                           target_type: str = "end_user") -> tuple[ApiKey, str]:
    """Create a default API key for a target (called at registration / first login)."""
    return create_api_key(db, user_id, "Default", is_default=True,
                          target_type=target_type)


def backfill_default_keys(db: Session) -> int:
    """Backfill an is_default flag for existing targets that lack a default key.

    Picks the target's oldest valid key as default; creates one if none exist
    (v4 §6.1.3/6.1.5, review GAP-25). Iterates TARGETS (``user_id`` +
    ``target_type``), not bare ids, so the two id spaces keep separate defaults.
    Returns the number of targets backfilled.
    """
    now = _db_utcnow()
    targets = {
        (row[0], row[1] or "end_user")
        for row in db.query(ApiKey.user_id, ApiKey.target_type).all()
    }
    backfilled = 0
    for uid, target_type in targets:
        has_default = (
            db.query(ApiKey)
            .filter(ApiKey.user_id == uid, ApiKey.target_type == target_type,
                    ApiKey.is_default.is_(True))
            .first()
        )
        if has_default is not None:
            continue
        valid = (
            db.query(ApiKey)
            .filter(ApiKey.user_id == uid, ApiKey.target_type == target_type,
                    ApiKey.is_revoked.is_(False))
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
                create_api_key(db, uid, "Default", is_default=True,
                               target_type=target_type)
                backfilled += 1
            except Exception:
                pass
    return backfilled
