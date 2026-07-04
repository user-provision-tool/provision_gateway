"""Auth router — /api/auth/* endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin, require_admin_role
from ..models.admin import AdminUser
from ..schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    SetupRequest,
)
from ..services import auth_service
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# POST /api/auth/setup — first-run admin creation (no auth needed)
# ---------------------------------------------------------------------------

@router.post("/setup", status_code=201)
def setup_admin(req: SetupRequest, db: Session = Depends(get_db)):
    """Create the initial admin user. Only works when no admin exists."""
    if auth_service.has_any_admin(db):
        raise HTTPException(
            status_code=409,
            detail="Admin already exists. Use POST /api/auth/register instead.",
        )
    admin = auth_service.create_admin(db, req.email, req.password, role="admin")
    return {"message": "Initial admin created. Please login.", "id": admin.id}


# ---------------------------------------------------------------------------
# POST /api/auth/register — create additional admin users (admin-only)
# ---------------------------------------------------------------------------

@router.post("/register", status_code=201)
def register_admin(
    req: RegisterRequest,
    current_admin: AdminUser = Depends(require_admin_role),
    db: Session = Depends(get_db),
):
    """Create a new admin user. Only existing admins can create others."""
    existing = auth_service.get_admin_by_email(db, req.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Viewers cannot create admins
    if req.role == "admin" and current_admin.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create admin users")

    admin = auth_service.create_admin(db, req.email, req.password, role=req.role)
    return admin.to_dict()


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------

@router.post("/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate and return JWT tokens."""
    admin = auth_service.authenticate_admin(db, req.email, req.password)
    if admin is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = auth_service.create_access_token(admin.id, admin.email, admin.role)
    refresh_token = auth_service.create_refresh_token(admin.id, admin.email)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.JWT_EXPIRE_SEC,
        "admin": admin.to_dict(),
    }


# ---------------------------------------------------------------------------
# POST /api/auth/refresh
# ---------------------------------------------------------------------------

@router.post("/refresh")
def refresh_token(req: RefreshRequest, db: Session = Depends(get_db)):
    """Use a refresh token to get a new access token."""
    try:
        payload = auth_service.decode_token(req.refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    admin_id = int(payload.get("sub", 0))
    admin = auth_service.get_admin_by_id(db, admin_id)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="Admin not found or inactive")

    access_token = auth_service.create_access_token(admin.id, admin.email, admin.role)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.JWT_EXPIRE_SEC,
    }


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------

@router.get("/me")
def get_me(current_admin: AdminUser = Depends(get_current_admin)):
    """Return the currently authenticated admin's profile."""
    return current_admin.to_dict()


# ---------------------------------------------------------------------------
# PUT /api/auth/password
# ---------------------------------------------------------------------------

@router.put("/password")
def change_password(
    req: PasswordChangeRequest,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Change the current admin's password."""
    success = auth_service.change_password(
        db, current_admin, req.current_password, req.new_password
    )
    if not success:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    return {"message": "Password updated."}
