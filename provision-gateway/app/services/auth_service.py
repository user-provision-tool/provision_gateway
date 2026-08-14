"""Auth service — admin CRUD, JWT creation/verification, password hashing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
import bcrypt as _bcrypt
from sqlalchemy.orm import Session

from ..config import settings
from ..models.admin import AdminUser


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
    """Decode a gateway/access token used for dashboard & gateway API access.

    Accepts both ``type='access'`` (Bearer header) and ``type='gateway'``
    (the ``gateway_token`` cookie issued by login).  This lets
    ``require_gateway_token`` honour either credential source.
    """
    payload = decode_token(token)
    if payload.get("type") not in ("gateway", "access"):
        raise JWTError("Token is not a gateway/access token")
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
    """Create a new admin user."""
    admin = AdminUser(
        email=email,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(admin)
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
# Provision token (long-lived JWT for service access via provision-nginx)
# ---------------------------------------------------------------------------

def create_provision_token(user_id: int, email: str, role: str, user_type: str = "end_user",
                           api_key_id: int | None = None) -> str:
    """Create a long-lived JWT provision token (1 year expiry).

    This is the token used by provision-nginx to authenticate service access.
    Set as the provision_token cookie.
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=31536000)  # 1 year
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


def verify_provision_token(token: str) -> dict[str, Any]:
    """Verify a provision token (used by /api/auth/verify nginx subrequest).

    Returns the decoded payload on success.
    Raises JWTError if expired or invalid.
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
        finally:
            db.close()
    return payload


# ---------------------------------------------------------------------------
# API Key helpers
# ---------------------------------------------------------------------------

from ..models.api_key import ApiKey
import hashlib
import secrets


def _hash_token(token: str) -> str:
    """Hash an API token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_key(db: Session, user_id: int, label: str) -> tuple[ApiKey, str]:
    """Create a new API key for a user. Returns (ApiKey, raw_token)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    key = ApiKey(
        user_id=user_id,
        label=label,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw


def list_api_keys(db: Session, user_id: int | None = None) -> list[ApiKey]:
    """List API keys. If user_id is None, return all (admin)."""
    q = db.query(ApiKey)
    if user_id is not None:
        q = q.filter(ApiKey.user_id == user_id)
    return q.order_by(ApiKey.created_at.desc()).all()


def revoke_api_key(db: Session, key_id: int) -> bool:
    """Revoke an API key by ID. Returns True if found and revoked."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        return False
    key.is_revoked = True
    db.commit()
    return True


def get_api_key_by_id(db: Session, key_id: int) -> ApiKey | None:
    """Get an API key by its ID."""
    return db.query(ApiKey).filter(ApiKey.id == key_id).first()


def create_default_api_key(db: Session, user_id: int) -> tuple[ApiKey, str]:
    """Create a default API key for a user (called at first login if none exists)."""
    return create_api_key(db, user_id, "Default")
