"""Auth router — /api/auth/* endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from jose import JWTError
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import require_gateway_token, require_admin
from ..models.end_user import EndUser
from ..models.api_key import ApiKey
from ..schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    RegisterRequest,
    SetupRequest,
)
from ..services import auth_service
from ..config import settings
import bcrypt as _bcrypt

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
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new admin user. Only existing admins can create others.

    Uses the shared ``require_admin`` dependency (``gateway_token`` cookie or
    Bearer, 24h TTL) per gateway-acl-architecture.md §5, instead of the old
    Bearer-``access_token`` admin-only middleware.
    """
    existing = auth_service.get_admin_by_email(db, req.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Viewers cannot create admins (require_admin already enforces admin role)
    if req.role == "admin" and current_admin["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create admin users")

    admin = auth_service.create_admin(db, req.email, req.password, role=req.role)
    return admin.to_dict()


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------

@router.post("/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate and set the provision_token cookie.

    v4 §6.1/F4 (three-credential model): the ONLY credential set here is the
    1-week provision_token, bound to the user's default API key's api_key_id
    (so it dies with that key). access_token / refresh_token / gateway_token
    are no longer minted. Cookie Max-Age = PROVISION_COOKIE_TTL (604800).
    Special users (role=special) are blocked with 403.
    """
    result = auth_service.authenticate_user(db, req.email, req.password)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid email/username or password")

    user_type, user_info = result
    role = user_info.get("role", "viewer")
    user_id = user_info["id"]
    email = user_info.get("email", user_info.get("username", ""))

    # Block special users at login
    if role == "special":
        raise HTTPException(status_code=403, detail="Special users cannot access the dashboard")

    # Bind the provision token to the user's default key (D7: auto-create/promote).
    if user_type == "end_user":
        default_key = auth_service.get_or_create_default_key(db, user_id)
        api_key_id = default_key.id
    else:
        api_key_id = None

    provision_token = auth_service.create_provision_token(
        user_id, email, role, user_type, api_key_id=api_key_id
    )

    # Build JSON response body (no access/refresh tokens).
    body = {
        "token_type": "cookie",
        "expires_in": settings.PROVISION_COOKIE_TTL,
        "user_type": user_type,
    }

    if user_type == "admin":
        body["admin"] = user_info
    else:
        body["user"] = {
            "id": user_info["id"],
            "username": user_info.get("username", ""),
            "role": user_info.get("role", "viewer"),
        }

    # Set the provision_token cookie (the dashboard reads this, not localStorage).
    resp = JSONResponse(content=body)
    resp.set_cookie(
        key="provision_token",
        value=provision_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=settings.PROVISION_COOKIE_TTL,
        path="/",
    )
    return resp


# ---------------------------------------------------------------------------
# POST /api/auth/logout — unauthenticated, clears provision_token cookie
# (v4 §6.1.6 / review G14)
# ---------------------------------------------------------------------------

@router.post("/logout")
def logout(response: Response):
    """Clear the provision_token cookie, even if it is expired/invalid."""
    response.delete_cookie("provision_token", path="/")
    return {"message": "Logged out."}


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------

@router.get("/me")
def get_me(current_user: dict = Depends(require_gateway_token)):
    """Return the currently authenticated user's profile (admin or end-user).

    Uses the shared ``require_gateway_token`` dependency (``gateway_token``
    cookie or Bearer, 24h TTL) instead of ``get_current_user`` (Bearer
    ``access_token``, 1h TTL). This matches gateway-acl-architecture.md §5
    (every ``/api/*`` route gated by ``gateway_token``) and removes the
    transient 401 the browser emitted once the 1h access token expired — the
    long-lived ``gateway_token`` cookie is now consulted directly.
    """
    return current_user


# ---------------------------------------------------------------------------
# GET /api/auth/verify — nginx auth_request subrequest (no auth required)
# ---------------------------------------------------------------------------

@router.get("/verify")
def verify_auth(
    request: Request,
    db: Session = Depends(get_db),
):
    """Verify provision token for the nginx auth_request subrequest (v4 §6/F3).

    Returns the FINAL HTTP status nginx consumes:
      - 200 + ``X-Service-Basic``  → allowed (nginx injects the credential)
      - 401 + ``X-Auth-Action``    → login_required / unauthorized / token_expired
      - 403 + ``X-Auth-Action``    → acl_denied

    EVERY response carries ``X-Client-Type`` (hybrid rule, review GAP-11/A3):
    header ⇒ api, cookie ⇒ browser, Accept text/html ⇒ browser, else ⇒ api.
    Client type is decided HERE — nginx no longer uses an Accept map.

    Ordering (v4 §6.1.4, review R1): revocation check (key exists / not
    revoked / expires_at not passed) runs first via verify_provision_token —
    it applies to admins too — then the admin bypass, then user active/approved,
    then ACL.
    """
    client_type = _resolve_client_type(request)

    def _respond(status: int, headers: dict, content: dict | None = None) -> Response:
        headers = dict(headers)
        headers["X-Client-Type"] = client_type
        return JSONResponse(status_code=status, headers=headers, content=content)

    def _deny(action: str, status: int, detail: str) -> Response:
        return _respond(status, {"X-Auth-Action": action}, {"detail": detail})

    if not settings.ENABLE_ACL:
        # ACL disabled → 401 + X-Auth-Action + X-Client-Type (GAP-08 fix), so
        # nginx's @auth_401 can still act on the client type.
        return _deny("login_required", 401, "ACL disabled")

    cookie_token = request.cookies.get("provision_token")
    header_token = request.headers.get("X-Provision-Token", "")
    token = cookie_token or header_token

    if not token:
        # header ⇒ API gets "unauthorized"; cookie/browser gets "login_required"
        action = "unauthorized" if client_type == "api" else "login_required"
        return _deny(action, 401, "No provision token")

    try:
        payload = auth_service.verify_provision_token(token)
    except JWTError:
        # Check if expired vs invalid
        try:
            from jose import jwt as _jwt
            _jwt.decode(token, settings.GATEWAY_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
                        options={"verify_exp": False})
            # Token is structurally valid but expired
            return _deny("token_expired", 401, "Token expired")
        except Exception:
            return _deny("login_required", 401, "Invalid token")

    # Extract user info
    user_id = int(payload.get("sub", 0))
    user_type = payload.get("user_type", "end_user")

    if user_type == "admin":
        # Admins have access to everything (revocation already checked above)
        svc_basic = _get_service_basic_credential(request, db)
        return _respond(200, {"X-Service-Basic": svc_basic}, None)

    # End-user: must be active + approved
    end_user = db.query(EndUser).filter(EndUser.id == user_id).first()
    if not end_user or not end_user.is_active or not end_user.is_approved:
        return _deny("login_required", 401, "User not found, inactive, or not approved")

    # Extract target service from request hostname
    host = request.headers.get("Host", "")
    registry_entry = _lookup_by_hostname(host, request)

    if registry_entry:
        target_user = registry_entry.get("user_name", "")
        # Check if viewer is accessing their own service
        if target_user == end_user.username:
            svc_basic = _get_service_basic_credential(request, db)
            return _respond(200, {"X-Service-Basic": svc_basic}, None)

        # Check allowed_special_users (trimmed on parse — N1)
        allowed = _parse_allowed_special_users(end_user.allowed_special_users)
        if target_user in allowed:
            svc_basic = _get_service_basic_credential(request, db)
            return _respond(200, {"X-Service-Basic": svc_basic}, None)

    # ACL denied
    return _deny("acl_denied", 403, "ACL denied")


def _resolve_client_type(request: Request) -> str:
    """Hybrid client-type rule (v4 §6, review GAP-11/A3).

    X-Provision-Token header present ⇒ api; else provision_token cookie ⇒
    browser; else Accept contains text/html ⇒ browser; else ⇒ api.
    """
    if request.headers.get("X-Provision-Token"):
        return "api"
    if request.cookies.get("provision_token"):
        return "browser"
    accept = request.headers.get("Accept", "") or ""
    if "text/html" in accept:
        return "browser"
    return "api"


def _parse_allowed_special_users(raw: str | None) -> list[str]:
    """Parse a comma-separated allowed_special_users list, trimming each entry (N1)."""
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def _lookup_by_hostname(host: str, request: Request) -> dict | None:
    """Look up a registry entry by hostname from the HostnameIndex."""
    try:
        hostname_index = request.app.state.hostname_index
        return hostname_index.get_by_hostname(host)
    except Exception:
        return None


def _get_service_basic_credential(request: Request, db: Session) -> str:
    """Get the X-Service-Basic credential for a service.

    Returns the base64-encoded username:password for auth_basic on the target
    service. When ``passwd_plain`` is missing/empty (a supported passwd-less
    state), returns empty — never a guessable default (review N2).
    """
    host = request.headers.get("Host", "")
    entry = _lookup_by_hostname(host, request)
    if entry:
        user_name = entry.get("user_name", "")
        passwd = entry.get("passwd_plain") or ""
        if passwd == "":
            return ""
        import base64
        return base64.b64encode(f"{user_name}:{passwd}".encode()).decode()
    return ""


# ---------------------------------------------------------------------------
# API Key CRUD — POST/GET/DELETE /api/auth/keys
# ---------------------------------------------------------------------------

@router.post("/keys", status_code=201)
def create_key(
    req: dict,
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Generate a new API key. Admin can create for any user; viewer for self.

    Request body: {"label": "my-key", "user_id": 1}  (user_id is optional for viewers)

    The API key IS the provision token — a 1-year JWT carrying its own
    api_key_id and the target's real user_type (GAP-07) — so the returned
    ``token`` is directly usable as a Bearer/X-Provision-Token credential.
    """
    label = req.get("label", "Default").strip()
    if not label:
        raise HTTPException(400, "label is required")

    # Determine target user_id
    is_admin = current_user["role"] == "admin"
    target_user_id = req.get("user_id", current_user["id"]) if is_admin else current_user["id"]

    if not is_admin and target_user_id != current_user["id"]:
        raise HTTPException(403, "Viewers can only create keys for themselves")

    key, raw_token = auth_service.create_api_key(db, target_user_id, label)

    return {
        "key": key.to_dict(),
        "token": raw_token,
        "message": "Save this token — it will not be shown again.",
    }


@router.get("/keys")
def list_keys(
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """List API keys. Admin sees all; viewer sees own.

    Uses the shared ``require_gateway_token`` dependency (cookie or Bearer) so
    the extraction is consistent with ``POST /keys`` and other gateway routes —
    the previous hand-rolled ``_get_gateway_user_safe`` 401'd a valid admin
    ``gateway_token`` cookie.
    """
    is_admin = current_user["role"] == "admin"
    user_id = None if is_admin else current_user["id"]
    keys = auth_service.list_api_keys(db, user_id)
    return {"keys": [k.to_dict() for k in keys]}


def _get_gateway_user_safe(request: Request, db: Session) -> dict | None:
    """Extract the authenticated user from the provision_token cookie/header.

    v4 §11.2 (N5): the provision_token cookie is the single credential. We
    keep the gateway_token cookie + Bearer as backward-compatible fallbacks.
    """
    token = request.cookies.get("provision_token") or request.cookies.get("gateway_token") or ""
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None
    try:
        payload = auth_service.decode_gateway_token(token)
    except Exception:
        return None
    return {
        "id": int(payload.get("sub", 0)),
        "email": payload.get("email", ""),
        "role": payload.get("role", "viewer"),
        "user_type": payload.get("user_type", "end_user"),
    }


@router.delete("/keys/{key_id}")
def delete_key(
    key_id: int,
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Revoke an API key. Admin can revoke any; viewer own.

    Rejects revoking the user's default key with 400 (v4 §6.1.5).
    """
    key = auth_service.get_api_key_by_id(db, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")

    is_admin = current_user["role"] == "admin"
    if not is_admin and key.user_id != current_user["id"]:
        raise HTTPException(403, "You can only revoke your own keys")

    try:
        auth_service.revoke_api_key(db, key_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"revoked": True, "key_id": key_id}


@router.put("/keys/{key_id}/default")
def set_default_key(
    key_id: int,
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Mark an API key as the user's default (v4 §6.1.5)."""
    key = auth_service.get_api_key_by_id(db, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")

    is_admin = current_user["role"] == "admin"
    if not is_admin and key.user_id != current_user["id"]:
        raise HTTPException(403, "You can only manage your own keys")

    if not auth_service.set_default_api_key(db, key_id):
        raise HTTPException(404, "Key not found")
    return {"default": True, "key_id": key_id}


# ---------------------------------------------------------------------------
# GET /go/{hostname} — service access redirect from dashboard
# ---------------------------------------------------------------------------

@router.get("/go/{hostname}")
def go_to_service(
    hostname: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Redirect to a service URL using a 30s exchange code (v4 §6.2 / F7).

    Validates the provision_token cookie, checks ACL, then 303-redirects to
    ``{scheme}://{svc-host}:{port}/_set_token?code=...``. No provision_token
    JWT ever appears in a URL (GAP-10); the code is exchanged for the cookie
    by ``/api/auth/exchange`` via the service's ``_set_token`` plain proxy.
    """
    current_user = _get_gateway_user_safe(request, db)
    if current_user is None:
        raise HTTPException(401, "Authentication required")

    # Look up service by hostname
    try:
        hostname_index = request.app.state.hostname_index
        entry = hostname_index.get_by_hostname(hostname)
    except Exception:
        entry = None

    if entry is None:
        raise HTTPException(404, f"Service not found: {hostname}")

    # ACL check for viewers
    if current_user["role"] != "admin":
        target_user = entry.get("user_name", "")
        if target_user != current_user["email"]:
            # Check allowed_special_users (trimmed — N1)
            eu = db.query(EndUser).filter(EndUser.id == current_user["id"]).first()
            allowed = _parse_allowed_special_users(eu.allowed_special_users) if eu else []
            if target_user not in allowed:
                raise HTTPException(403, "Access denied: service not in your allowed list")

    # Scheme/port from the service's https flag (GAP-19): https services get
    # https + NGINX_HTTPS_PORT; http services get http + NGINX_HTTP_PORT.
    domain = entry.get("hostname", hostname)
    https = bool(entry.get("https", False))
    port = settings.NGINX_HTTPS_PORT if https else settings.NGINX_HTTP_PORT
    scheme = "https" if https else "http"

    service_url = f"{scheme}://{domain}"
    if not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        service_url += f":{port}"

    # Mint a 30s code and 303 the browser to the service's _set_token
    code = auth_service.create_exchange_code(
        current_user["id"], current_user["email"], current_user["role"],
        current_user["user_type"], domain, redirect="/",
    )
    _set_token_url = f"{service_url}/_set_token?code={code}"
    return RedirectResponse(url=_set_token_url, status_code=303)


# ---------------------------------------------------------------------------
# GET /api/auth/exchange — internal 30s-code → provision_token cookie relay
# (reached via the per-service `location = /_set_token` plain proxy; v4 §6.2)
# ---------------------------------------------------------------------------

@router.get("/exchange")
def exchange(request: Request, db: Session = Depends(get_db)):
    """Verify a 30s code and set the provision_token cookie via a 302 relay.

    Returns ``302 Location: /`` + ``Set-Cookie: provision_token=...``.
    ``; Secure`` is added only for https services (from the registry flag).
    nginx relays the 302 + Set-Cookie verbatim (A1).
    """
    code = request.query_params.get("code", "")
    if not code:
        raise HTTPException(401, "Missing exchange code")

    try:
        payload = auth_service.verify_exchange_code(code)
    except JWTError:
        raise HTTPException(401, "Invalid or expired exchange code")

    user_id = int(payload["sub"])
    role = payload.get("role", "viewer")
    user_type = payload.get("user_type", "end_user")
    email = payload.get("email", "")
    svc_host = payload.get("svc_host", "")

    # `; Secure` only for https services — resolved from the registry flag.
    https = False
    if svc_host:
        try:
            hostname_index = request.app.state.hostname_index
            entry = hostname_index.get_by_hostname(svc_host)
            https = bool(entry and entry.get("https", False))
        except Exception:
            https = False

    # Bind the fresh 1-week provision token to the user's default key (D7).
    if user_type == "end_user":
        default_key = auth_service.get_or_create_default_key(db, user_id)
        api_key_id = default_key.id
    else:
        api_key_id = None

    provision_token = auth_service.create_provision_token(
        user_id, email, role, user_type, api_key_id=api_key_id
    )

    resp = RedirectResponse(url=payload.get("redirect", "/"), status_code=302)
    resp.set_cookie(
        key="provision_token",
        value=provision_token,
        httponly=True,
        secure=https,
        samesite="lax",
        max_age=settings.PROVISION_COOKIE_TTL,
        path="/",
    )
    return resp


# ---------------------------------------------------------------------------
# PUT /api/auth/password
# ---------------------------------------------------------------------------

@router.put("/password")
def change_password(
    req: PasswordChangeRequest,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Change the current admin's password.

    Uses the shared ``require_admin`` dependency (``gateway_token`` cookie or
    Bearer, 24h TTL) per gateway-acl-architecture.md §5, instead of the old
    Bearer-``access_token`` admin-only middleware.
    """
    admin = auth_service.get_admin_by_id(db, current_admin["id"])
    if admin is None:
        raise HTTPException(status_code=401, detail="Admin not found")
    success = auth_service.change_password(
        db, admin, req.current_password, req.new_password
    )
    if not success:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    return {"message": "Password updated."}


# ---------------------------------------------------------------------------
# End-User Management (admin-only)
# ---------------------------------------------------------------------------


@router.get("/users")
def list_end_users(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all registered end-users (admin-only)."""
    users = db.query(EndUser).order_by(EndUser.created_at.desc()).all()
    return {"users": [u.to_dict() for u in users]}


@router.post("/users/register")
def register_end_user(
    req: dict,
    db: Session = Depends(get_db),
):
    """Register a new end-user. Requires admin approval before activation.

    Intentionally left unauthenticated: this is the public pre-auth signup
    flow from the login page (creates an unapproved end-user awaiting admin
    approval), so it cannot be gated by ``gateway_token``.
    """
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
    db.flush()
    # v4 §6.1.3: default key created at registration (not first login).
    try:
        auth_service.create_api_key(db, user.id, "Default", is_default=True)
    except Exception:
        pass
    db.commit()
    db.refresh(user)
    return {"created": True, "user": user.to_dict(), "message": "Registration submitted. Awaiting admin approval."}


@router.put("/users/{user_id}/approve")
def approve_end_user(
    user_id: int,
    current_admin: dict = Depends(require_admin),
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
    current_admin: dict = Depends(require_admin),
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
    current_admin: dict = Depends(require_admin),
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
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List users available for deployment (approved + active end-users, plus special users)."""
    # Get all approved, active end-users
    users = db.query(EndUser).filter(
        EndUser.is_approved == True,
        EndUser.is_active == True,
    ).all()
    result = [u.to_dict() for u in users]
    # Special users are those registered with role="special" in end_users
    # They're already included in the query above. 
    # No hardcoded special users — all special users must be registered via the Users page.
    return {"users": sorted(result, key=lambda x: x["username"])}
