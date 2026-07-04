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

def create_access_token(admin_id: int, email: str, role: str) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.JWT_EXPIRE_SEC)
    payload = {
        "sub": str(admin_id),
        "email": email,
        "role": role,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(admin_id: int, email: str) -> str:
    """Create a JWT refresh token (longer-lived)."""
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.JWT_REFRESH_EXPIRE_SEC)
    payload = {
        "sub": str(admin_id),
        "email": email,
        "type": "refresh",
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
