"""JWT authentication middleware / dependency."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.admin import AdminUser
from ..models.end_user import EndUser
from ..services.auth_service import decode_access_token, decode_gateway_token, get_admin_by_id, get_end_user_by_id

security_scheme = HTTPBearer(auto_error=False)


def _extract_gateway_token(request: Request) -> str | None:
    """Extract the auth token from the provision_token cookie or Authorization header.

    v4 §11.2 (N5): the provision_token cookie is the single credential the
    dashboard carries; the old gateway_token cookie is kept as a fallback.
    """
    cookie = request.cookies.get("provision_token") or request.cookies.get("gateway_token")
    if cookie:
        return cookie
    # Fall back to Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


async def require_gateway_token(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: validates the provision_token cookie or Bearer token.

    Returns a user dict with keys: id, email, role, user_type.
    Blocks special users entirely (403).
    Raises 401 if token is missing, invalid, or user not found.
    """
    token = _extract_gateway_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing provision token (cookie or Authorization header)")

    try:
        payload = decode_gateway_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired provision token")

    user_id_str: str | None = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user_type = payload.get("user_type", "admin")
    role = payload.get("role", "viewer")
    email = payload.get("email", "")

    # Block special users entirely
    if role == "special":
        raise HTTPException(status_code=403, detail="Special users are not permitted to access the dashboard")

    if user_type == "admin":
        admin = get_admin_by_id(db, user_id)
        if admin is None or not admin.is_active:
            raise HTTPException(status_code=401, detail="Admin not found or inactive")
        return {"id": admin.id, "email": admin.email, "role": admin.role, "user_type": "admin"}
    else:
        end_user = get_end_user_by_id(db, user_id)
        if end_user is None or not end_user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        if not end_user.is_approved:
            raise HTTPException(status_code=401, detail="User not yet approved")
        return {"id": end_user.id, "email": end_user.username,
                "role": end_user.role, "user_type": "end_user",
                "allowed_special_users": (end_user.allowed_special_users or "").split(",") if end_user.allowed_special_users else []}


async def require_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: requires gateway_token AND admin role.

    Returns the user dict from require_gateway_token.
    Raises 403 if user does not have admin role.
    """
    user = await require_gateway_token(request=request, db=db)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


async def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    """FastAPI dependency: extracts and validates JWT, returns the AdminUser.

    Raises 401 if the token is missing, invalid, or the admin doesn't exist.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    token = credentials.credentials

    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    admin_id_str: str | None = payload.get("sub")
    if admin_id_str is None:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    try:
        admin_id = int(admin_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    admin = get_admin_by_id(db, admin_id)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="Admin not found or inactive")

    return admin


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency: extracts JWT, returns user dict for both admin and end-user.

    Returns dict with keys: id, email, role, user_type
    Raises 401 if token is missing, invalid, or user not found.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    token = credentials.credentials

    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id_str: str | None = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    try:
        user_id = int(user_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user_type = payload.get("user_type", "admin")
    role = payload.get("role", "viewer")
    email = payload.get("email", "")

    if user_type == "admin":
        admin = get_admin_by_id(db, user_id)
        if admin is None or not admin.is_active:
            raise HTTPException(status_code=401, detail="Admin not found or inactive")
        return {"id": admin.id, "email": admin.email, "role": admin.role, "user_type": "admin"}
    else:
        end_user = get_end_user_by_id(db, user_id)
        if end_user is None or not end_user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        if not end_user.is_approved:
            raise HTTPException(status_code=401, detail="User not yet approved")
        return {"id": end_user.id, "email": end_user.username, "role": end_user.role, "user_type": "end_user"}


async def get_current_admin_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> AdminUser | None:
    """Like get_current_admin but returns None instead of raising 401."""
    if credentials is None:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
        admin_id = int(payload.get("sub", 0))
        return get_admin_by_id(db, admin_id)
    except (JWTError, ValueError):
        return None


def require_admin_role(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """Dependency: requires the authenticated admin to have 'admin' role."""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return admin
