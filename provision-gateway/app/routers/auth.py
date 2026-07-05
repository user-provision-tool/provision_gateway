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


# ---------------------------------------------------------------------------
# End-User Management (admin-only)
# ---------------------------------------------------------------------------

from ..models.end_user import EndUser
import bcrypt as _bcrypt
from datetime import datetime, timezone


@router.get("/users")
def list_end_users(
    current_admin: AdminUser = Depends(require_admin_role),
    db: Session = Depends(get_db),
):
    """List all registered end-users."""
    users = db.query(EndUser).order_by(EndUser.created_at.desc()).all()
    return {"users": [u.to_dict() for u in users]}


@router.post("/users/register")
def register_end_user(
    req: dict,
    db: Session = Depends(get_db),
):
    """Register a new end-user. Requires admin approval before activation."""
    username = req.get("username", "").strip()
    password = req.get("password", "")
    if not username or not password:
        raise HTTPException(400, "username and password required")
    if len(password) < 4:
        raise HTTPException(400, "password too short (min 4 chars)")
    
    existing = db.query(EndUser).filter(EndUser.username == username).first()
    if existing:
        raise HTTPException(409, f"User '{username}' already exists")
    
    user = EndUser(
        username=username,
        password_hash=_bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode(),
        role=req.get("role", "viewer"),
        is_approved=False,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"created": True, "user": user.to_dict(), "message": "Registration submitted. Awaiting admin approval."}


@router.put("/users/{user_id}/approve")
def approve_end_user(
    user_id: int,
    current_admin: AdminUser = Depends(require_admin_role),
    db: Session = Depends(get_db),
):
    """Admin approves an end-user."""
    user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_approved = True
    user.approved_at = datetime.now(timezone.utc)
    db.commit()
    return {"approved": True, "user": user.to_dict()}


@router.put("/users/{user_id}")
def update_end_user(
    user_id: int,
    req: dict,
    current_admin: AdminUser = Depends(require_admin_role),
    db: Session = Depends(get_db),
):
    """Update end-user settings (role, special users, active status)."""
    user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if "role" in req:
        user.role = req["role"]
    if "is_active" in req:
        user.is_active = bool(req["is_active"])
    if "allowed_special_users" in req:
        user.allowed_special_users = ",".join(req["allowed_special_users"]) if isinstance(req["allowed_special_users"], list) else req["allowed_special_users"]
    db.commit()
    return {"updated": True, "user": user.to_dict()}


@router.delete("/users/{user_id}")
def delete_end_user(
    user_id: int,
    current_admin: AdminUser = Depends(require_admin_role),
    db: Session = Depends(get_db),
):
    """Delete an end-user."""
    user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    db.delete(user)
    db.commit()
    return {"deleted": True}


@router.get("/users/deployable")
def list_deployable_users(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List users available for deployment (approved + active + special users)."""
    users = db.query(EndUser).filter(
        EndUser.is_approved == True,
        EndUser.is_active == True,
    ).all()
    result = [u.to_dict() for u in users]
    # Add special functional users
    specials = ["shared", "public", "internal"]
    for s in specials:
        if not any(u["username"] == s for u in result):
            result.append({"username": s, "role": "special", "is_approved": True, "is_active": True, "allowed_special_users": []})
    return {"users": sorted(result, key=lambda x: x["username"])}
