"""Auth dependencies — unified users, current-role authority (T9).

Every authorization loads the current `users` row; a token claim alone never
grants access. The liveness rule is `User.account_live` (active + approved +
not deleted + deletion saga complete).
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer
from jose import JWTError

from .. import database
from ..services import auth_service

security_scheme = HTTPBearer(auto_error=False)


def _extract_gateway_token(request: Request) -> str | None:
    cookie = request.cookies.get("provision_token")
    if cookie:
        return cookie
    return request.headers.get("X-Provision-Token") or None


def require_gateway_token(request: Request) -> dict:
    """Validate a provision token and return the current user.

    Returns ``{id, username, role, api_user_id}``; special users are blocked
    from the dashboard (403). No connection is held past this dependency.
    """
    token = _extract_gateway_token(request)
    if token is None:
        raise HTTPException(401, "Missing provision token (cookie or header)")

    db = database.SessionLocal()
    try:
        try:
            payload = auth_service.verify_provision_token(db, token)
        except JWTError:
            raise HTTPException(401, "Invalid or expired provision token")
        except Exception:  # noqa: BLE001 — a store failure denies, never 500
            raise HTTPException(401, "Authentication temporarily unavailable")
        user = auth_service.get_user_by_id(db, payload.get("sub"))
        if user is None:
            raise HTTPException(401, "User not found")
        if user.role == "special":
            raise HTTPException(403, "Special users are not permitted to access the dashboard")
        if not user.account_live:
            raise HTTPException(401, "User is inactive, unapproved or deleted")
        username = auth_service.username_for(db, user.id)
        return {
            "id": str(user.id),
            "username": username,
            "role": user.role,
            "api_user_id": str(user.api_user_id),
        }
    finally:
        db.close()


def require_admin(request: Request) -> dict:
    user = require_gateway_token(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user


def require_owner_or_admin(request: Request) -> dict:
    """Owner of the target service or an admin; the target is checked by the route."""
    user = require_gateway_token(request)
    if user["role"] not in ("viewer", "admin"):
        raise HTTPException(403, "Owner or admin role required")
    return user


# Compatibility names for any older import; authority is unchanged.
def get_current_admin(request: Request):
    user = require_admin(request)
    return user


def get_current_user(request: Request) -> dict:
    return require_gateway_token(request)
