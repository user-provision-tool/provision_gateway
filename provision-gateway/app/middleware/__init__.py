"""JWT authentication middleware / dependency."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from ..database import SessionLocal
from ..models.admin import AdminUser
from ..models.end_user import EndUser
from ..services.auth_service import decode_access_token, decode_gateway_token, get_admin_by_id, get_end_user_by_id

security_scheme = HTTPBearer(auto_error=False)


def _extract_gateway_token(request: Request) -> str | None:
    """Extract the auth token from the provision_token cookie or API-key header.

    v4 §6.1 (three-credential model): the provision_token cookie (browser) and
    the ``X-Provision-Token`` header (API keys are long-lived provision tokens)
    are the ONLY credentials. The legacy ``gateway_token`` cookie and
    ``Authorization: Bearer`` fallbacks are REMOVED (G5) so an old client or an
    attacker holding a legacy access/gateway token cannot authenticate against
    the management API.
    """
    cookie = request.cookies.get("provision_token")
    if cookie:
        return cookie
    return request.headers.get("X-Provision-Token") or None


def require_gateway_token(
    request: Request,
) -> dict:
    """FastAPI dependency: validates the provision_token cookie or Bearer token.

    Synchronous dependency: FastAPI runs it in a worker thread, so a DB pool
    checkout that blocks (e.g. the pool is momentarily exhausted) can never
    freeze the event loop — which would otherwise wedge every in-flight request
    awaiting an external call. The auth query uses a short-lived session that is
    closed before the request proceeds, so its connection is NOT held across the
    endpoint's slow external awaits.

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

    db = SessionLocal()
    try:
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
    finally:
        db.close()


def require_admin(
    request: Request,
) -> dict:
    """FastAPI dependency: requires gateway_token AND admin role.

    Returns the user dict from require_gateway_token.
    Raises 403 if user does not have admin role.
    """
    user = require_gateway_token(request=request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> AdminUser:
    """FastAPI dependency: extracts and validates JWT, returns the AdminUser.

    Synchronous dependency (see require_gateway_token docstring for why).
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

    db = SessionLocal()
    try:
        admin = get_admin_by_id(db, admin_id)
        if admin is None or not admin.is_active:
            raise HTTPException(status_code=401, detail="Admin not found or inactive")
        return admin
    finally:
        db.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> dict:
    """FastAPI dependency: extracts JWT, returns user dict for both admin and end-user.

    Synchronous dependency (see require_gateway_token docstring for why).
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

    db = SessionLocal()
    try:
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
    finally:
        db.close()


def get_current_admin_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> AdminUser | None:
    """Like get_current_admin but returns None instead of raising 401."""
    if credentials is None:
        return None
    db = SessionLocal()
    try:
        try:
            payload = decode_access_token(credentials.credentials)
            admin_id = int(payload.get("sub", 0))
            return get_admin_by_id(db, admin_id)
        except (JWTError, ValueError):
            return None
    finally:
        db.close()


def require_admin_role(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """Dependency: requires the authenticated admin to have 'admin' role."""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return admin
