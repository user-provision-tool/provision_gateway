"""Auth router — unified users, one login space, one key space (T9)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from jose import JWTError
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, get_db
from ..middleware import require_admin, require_gateway_token
from ..models.api_key import ApiKey
from ..models.gateway_setting import GatewaySetting
from ..models.pre_deploy_session import PreDeploySession
from ..models.stream_exposure import StreamExposure
from ..models.user import User
from ..models.user_login import UserLogin
from ..schemas.auth import LoginRequest, PasswordChangeRequest
from ..services import acl_service, account_service, auth_service, audit_service
from ..services import cross_verify as cross_verify_service
from ..services.cross_orchestrator import schedule_sync, schedule_sync_threadsafe
from ..services.api_users_client import ApiUsersClient, ApiUsersUnavailable, api_users
from ..services.basic_credentials import authorization_header
from ..services.registry_entry import entry_tls

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _now():
    return datetime.now(timezone.utc)


def _user_payload(db: Session, user: User) -> dict:
    row = user.to_dict(username=auth_service.username_for(db, user.id))
    row["email"] = auth_service.email_for(db, user.id)
    row["was_public"] = acl_service.was_public_record(db, user)
    return row


def _get_user(db: Session, user_id) -> User:
    user = auth_service.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


def _check_mutable(user: User) -> None:
    if user.deletion_state != "none":
        raise HTTPException(409, "Account is in the deletion saga")


# ---------------------------------------------------------------------------
# Retired surfaces
# ---------------------------------------------------------------------------

@router.post("/setup", status_code=410)
def setup_retired():
    raise HTTPException(410, "/setup is retired; use the local bootstrap command")


@router.post("/register", status_code=410)
def register_admin_retired():
    raise HTTPException(410, "Admin registration is retired; no HTTP route mints an admin")


# ---------------------------------------------------------------------------
# Login / me / logout
# ---------------------------------------------------------------------------

@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = auth_service.authenticate_user(db, req.email, req.password)
    if user is None:
        raise HTTPException(401, "Invalid login or password")
    if user.role == "special":
        raise HTTPException(403, "Special users cannot access the dashboard")
    default_key = auth_service.get_or_create_default_key(db, user.id)
    token = auth_service.create_provision_token(db, user.id, default_key.id)
    body = {
        "token_type": "cookie",
        "expires_in": settings.PROVISION_COOKIE_TTL,
        "user": {
            "id": str(user.id),
            "username": auth_service.username_for(db, user.id),
            "role": user.role,
            "api_user_id": str(user.api_user_id),
        },
    }
    resp = JSONResponse(content=body)
    resp.set_cookie(
        key="provision_token", value=token, httponly=True, secure=False,
        samesite="lax", max_age=settings.PROVISION_COOKIE_TTL, path="/",
    )
    return resp


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("provision_token", path="/")
    return {"message": "Logged out."}


@router.get("/me")
def get_me(current_user: dict = Depends(require_gateway_token)):
    return current_user


# ---------------------------------------------------------------------------
# verify — the nginx auth_request subrequest
# ---------------------------------------------------------------------------

def _resolve_client_type(request: Request) -> str:
    if request.headers.get("X-Provision-Token"):
        return "api"
    if request.cookies.get("provision_token"):
        return "browser"
    if "text/html" in (request.headers.get("Accept") or ""):
        return "browser"
    return "api"


def _lookup_by_hostname(host: str, request: Request) -> dict | None:
    try:
        return request.app.state.hostname_index.get_by_hostname(host)
    except Exception:
        return None


def _owner_user(db: Session, entry: dict | None) -> User | None:
    if not entry:
        return None
    return db.query(User).filter(User.api_user_id == entry["owner_api_user_id"]).first()


@router.get("/verify")
def verify_auth(request: Request, db: Session = Depends(get_db)):
    client_type = _resolve_client_type(request)

    def _respond(status: int, headers: dict, content: dict | None = None) -> Response:
        headers = dict(headers)
        headers["X-Client-Type"] = client_type
        return JSONResponse(status_code=status, headers=headers, content=content)

    def _deny(action: str, status: int, detail: str) -> Response:
        return _respond(status, {"X-Auth-Action": action}, {"detail": detail})

    if not settings.ENABLE_ACL:
        return _deny("login_required", 401, "ACL disabled")

    host = request.headers.get("Host", "")
    entry = _lookup_by_hostname(host, request)
    if entry is None:
        return _deny("login_required", 401, "Unknown service host")

    owner = _owner_user(db, entry)
    if acl_service.is_public_owner(owner):
        return _respond(200, {acl_service.PUBLIC_VERDICT_HEADER: acl_service.PUBLIC_VERDICT_VALUE})

    token = request.cookies.get("provision_token") or request.headers.get("X-Provision-Token", "")
    if not token:
        return _deny("unauthorized" if client_type == "api" else "login_required", 401,
                     "No provision token")
    try:
        payload = auth_service.verify_provision_token(db, token)
    except JWTError:
        return _deny("token_expired", 401, "Token invalid or expired")
    except Exception:  # noqa: BLE001 — a store failure on the hot path MUST deny
        # Fail closed: a database outage can never turn into an allow, and it
        # must not become a 500 storm on every proxied request (T9).
        return _deny("token_expired", 401, "Token could not be verified")
    user = auth_service.get_user_by_id(db, payload.get("sub"))
    if user is None or not user.account_live:
        return _deny("login_required", 401, "User not found, inactive or unapproved")

    # Admin bypass is requester-side only: an admin can reach any service, but
    # the check is the CURRENT role, never a token claim.
    if user.role == "admin":
        return _respond(200, {"X-Service-Basic": authorization_header(entry["service_id"])})
    # The ACL matrix is keyed by `users.id`; the registry entry carries the
    # owner's `api_users.id`. Passing the latter made an owner fail the
    # self-access check and every grant compare against the wrong id.
    if owner is not None and acl_service.allows(db, user, owner.id):
        return _respond(200, {"X-Service-Basic": authorization_header(entry["service_id"])})
    return _deny("acl_denied", 403, "ACL denied")


# ---------------------------------------------------------------------------
# cross-verify — the cross nginx's token-free subrequest (G83)
# ---------------------------------------------------------------------------

@router.get("/cross-verify")
def cross_verify(request: Request, db: Session = Depends(get_db)):
    """Token-free authorization on ``<source-ip, target-url>``.

    The ONLY caller is the cross nginx; it is trusted by its actual IP. The
    cross path carries no token and no key at all — this verdict is the only
    authorization. Every other gateway endpoint keeps its token auth.
    """
    client_ip = request.client.host if request.client else ""
    if not cross_verify_service.is_cross_nginx_caller(client_ip):
        return JSONResponse(
            status_code=403,
            content={"detail": "caller is not the cross nginx"},
            headers={"X-Client-Type": "api"},
        )
    source_ip = request.headers.get("X-Source-IP", "")
    host = (request.headers.get("Host", "") or "").partition(":")[0]
    result = cross_verify_service.verdict(
        source_ip, host, db, request.app.state.hostname_index
    )
    headers = {"X-Client-Type": "api"}
    if result["allowed"]:
        headers["X-Service-Basic"] = result.get("service_basic") or ""
        return JSONResponse(
            status_code=200,
            content={"allowed": True, "reason": result["reason"]},
            headers=headers,
        )
    return JSONResponse(
        status_code=int(result.get("status") or 403),
        content={"detail": result["reason"]},
        headers=headers,
    )


@router.post("/cross-stream-verify")
async def cross_stream_verify(request: Request, db: Session = Depends(get_db)):
    """G85 — token-free authorization on ``<source-ip, exposure handle>``.

    The ONLY caller is the cross nginx's stream preread hook. The handle is the
    SNI (the edge's own exposure URL); it is a credential and is never logged.
    """
    client_ip = request.client.host if request.client else ""
    if not cross_verify_service.is_cross_nginx_caller(client_ip):
        return JSONResponse(
            status_code=403,
            content={"detail": "caller is not the cross nginx"},
            headers={"X-Client-Type": "api"},
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — a malformed hook call is a refusal
        payload = {}
    handle = str((payload or {}).get("handle") or "")
    source_ip = request.headers.get("X-Source-IP", "")
    result = cross_verify_service.stream_verdict(
        source_ip, handle, db, getattr(request.app.state, "registry", None)
    )
    headers = {"X-Client-Type": "api"}
    if result["allowed"]:
        return JSONResponse(
            status_code=200,
            content={
                "allow": True,
                "reason": result["reason"],
                "canonical_name": result["canonical_name"],
                "port": result["port"],
                "inner_port": result["inner_port"],
            },
            headers=headers,
        )
    return JSONResponse(
        status_code=int(result.get("status") or 403),
        content={"allow": False, "detail": result["reason"]},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def _key_owner(db: Session, key: ApiKey) -> dict:
    user = db.query(User).filter(User.id == key.user_id).first()
    return {
        "owner_username": auth_service.username_for(db, key.user_id) if user else None,
        "owner_role": user.role if user else None,
    }


@router.post("/keys", status_code=201)
def create_key(req: dict, current_user: dict = Depends(require_gateway_token),
               db: Session = Depends(get_db)):
    label = (req.get("label") or "Default").strip()
    if not label:
        raise HTTPException(400, "label is required")
    raw_target = req.get("user_id")
    target_id = current_user["id"] if raw_target in (None, "") else str(raw_target)
    try:
        target_id = str(uuid.UUID(str(target_id)))
    except (ValueError, TypeError):
        raise HTTPException(400, "user_id must be a UUID")
    if current_user["role"] != "admin" and target_id != current_user["id"]:
        raise HTTPException(403, "Viewers can only create keys for themselves")
    try:
        key, raw = auth_service.create_api_key(db, target_id, label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not auth_service.user_has_default_key(db, target_id):
        auth_service.set_default_api_key(db, key.id)
    return {"key": key.to_dict(**_key_owner(db, key)), "token": raw,
            "message": "Save this token — it will not be shown again."}


@router.get("/keys")
def list_keys(current_user: dict = Depends(require_gateway_token),
              db: Session = Depends(get_db)):
    if current_user["role"] == "admin":
        keys = auth_service.list_api_keys(db)
    else:
        keys = auth_service.list_api_keys(db, current_user["id"])
    return {"keys": [k.to_dict(**_key_owner(db, k)) for k in keys]}


@router.delete("/keys/{key_id}")
def delete_key(key_id: int, current_user: dict = Depends(require_gateway_token),
               db: Session = Depends(get_db)):
    key = auth_service.get_api_key_by_id(db, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")
    if current_user["role"] != "admin" and str(key.user_id) != current_user["id"]:
        raise HTTPException(403, "You can only revoke your own keys")
    try:
        auth_service.revoke_api_key(db, key_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"revoked": True, "key_id": key_id}


@router.put("/keys/{key_id}/default")
def set_default_key(key_id: int, current_user: dict = Depends(require_gateway_token),
                    db: Session = Depends(get_db)):
    key = auth_service.get_api_key_by_id(db, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")
    if current_user["role"] != "admin" and str(key.user_id) != current_user["id"]:
        raise HTTPException(403, "You can only manage your own keys")
    if not auth_service.set_default_api_key(db, key_id):
        raise HTTPException(404, "Key not found")
    return {"default": True, "key_id": key_id}


# ---------------------------------------------------------------------------
# /go and /exchange
# ---------------------------------------------------------------------------

@router.get("/go/{hostname}")
def go_to_service(hostname: str, request: Request, db: Session = Depends(get_db)):
    current = require_gateway_token(request)
    entry = _lookup_by_hostname(hostname, request)
    if entry is None:
        raise HTTPException(404, f"Service not found: {hostname}")
    user = auth_service.get_user_by_id(db, current["id"])
    owner = _owner_user(db, entry)
    if user.role != "admin" and not (
        owner is not None and acl_service.allows(db, user, owner.id)
    ):
        raise HTTPException(403, "Access denied: service not in your allowed list")
    https = entry_tls(entry)
    port = settings.NGINX_HTTPS_PORT if https else settings.NGINX_HTTP_PORT
    scheme = "https" if https else "http"
    service_url = f"{scheme}://{hostname}"
    if not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        service_url += f":{port}"
    code = auth_service.create_exchange_code(db, user.id, hostname, redirect="/")
    return RedirectResponse(url=f"{service_url}/_set_token?code={code}", status_code=303)


@router.get("/exchange")
def exchange(request: Request, db: Session = Depends(get_db)):
    code = request.query_params.get("code", "")
    if not code:
        raise HTTPException(401, "Missing exchange code")
    try:
        payload = auth_service.verify_exchange_code(code)
    except JWTError:
        raise HTTPException(401, "Invalid or expired exchange code")
    user = auth_service.get_user_by_id(db, payload.get("sub"))
    if user is None or not user.account_live or user.role == "special":
        raise HTTPException(401, "User cannot be authorized")
    https = False
    svc_host = payload.get("svc_host", "")
    if svc_host:
        entry = _lookup_by_hostname(svc_host, request)
        https = entry_tls(entry)
    default_key = auth_service.get_or_create_default_key(db, user.id)
    token = auth_service.create_provision_token(db, user.id, default_key.id)
    resp = RedirectResponse(url=payload.get("redirect", "/"), status_code=302)
    resp.set_cookie(key="provision_token", value=token, httponly=True, secure=https,
                    samesite="lax", max_age=settings.PROVISION_COOKIE_TTL, path="/")
    return resp


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

@router.put("/password")
def change_password(req: PasswordChangeRequest,
                    current: dict = Depends(require_gateway_token),
                    db: Session = Depends(get_db)):
    user = _get_user(db, current["id"])
    if not auth_service.change_password(db, user, req.current_password, req.new_password):
        raise HTTPException(400, "Current password is incorrect")
    return {"message": "Password updated."}


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def _registration_open(db: Session) -> bool:
    row = db.query(GatewaySetting).filter(GatewaySetting.key == "registration_open").first()
    return bool(row and str(row.value).lower() in ("1", "true", "yes"))


@router.get("/users")
def list_users(current_admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    out = []
    for user in users:
        row = _user_payload(db, user)
        row["grants"] = acl_service.list_for_grantee(db, user.id)
        out.append(row)
    return {"users": out, "candidates": acl_service.candidates(db)}


@router.get("/users/deployable")
def list_deployable_users(current_admin: dict = Depends(require_admin),
                          db: Session = Depends(get_db)):
    users = db.query(User).filter(
        User.is_approved.is_(True), User.is_active.is_(True),
        User.deleted_at.is_(None), User.deletion_state == "none",
    ).all()
    out = [{"id": str(u.id), "username": auth_service.username_for(db, u.id),
            "role": u.role, "api_user_id": str(u.api_user_id)} for u in users]
    out.sort(key=lambda r: r["username"] or "")
    return {"users": out}


@router.get("/users/{user_id}/grants")
def list_user_grants(user_id: str, current_admin: dict = Depends(require_admin),
                     db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    return {"user_id": str(user.id), "username": auth_service.username_for(db, user.id),
            "grants": acl_service.list_for_grantee(db, user.id),
            "candidates": acl_service.candidates(db, exclude_user_id=user.id)}


@router.put("/users/{user_id}/grants")
def set_user_grant(user_id: str, req: dict, current_admin: dict = Depends(require_admin),
                   db: Session = Depends(get_db)):
    try:
        shared_id = str(uuid.UUID(str(req.get("shared_user_id"))))
    except (ValueError, TypeError):
        raise HTTPException(400, "shared_user_id is required (UUID)")
    if "active" in req:
        active = bool(req["active"])
    else:
        state = str(req.get("state", "")).lower()
        if state not in ("active", "inactive"):
            raise HTTPException(400, "active (or state=active|inactive) is required")
        active = state == "active"
    try:
        row = acl_service.set_grant(db, user_id, shared_id, active)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit_service.log_action(
        db, action="grant_update", actor_user_id=current_admin["id"],
        target_user=auth_service.username_for(db, user_id),
        detail={"shared_user_id": shared_id, "active": active,
                "state": row["state"] if row else "none"}, status="success",
    )
    # An account grant can turn an explicit cross-owner service grant on/off.
    schedule_sync_threadsafe()
    return {"updated": True, "grant": row}


@router.post("/users/register", status_code=201)
def register_end_user(req: dict, db: Session = Depends(get_db)):
    """Public self-registration: viewer only, closed until the bootstrap."""
    if not _registration_open(db):
        raise HTTPException(503, "Registration is closed")
    if str(req.get("role", "viewer")) != "viewer" or req.get("is_public_shared"):
        raise HTTPException(400, "Public signup is viewer-only")
    try:
        user = account_service.create_public_viewer(
            db, username=req.get("username", ""), password=req.get("password", ""),
            email=req.get("email"),
        )
    except (ValueError, ApiUsersUnavailable) as exc:
        raise HTTPException(409 if isinstance(exc, ValueError) else 502, str(exc))
    return {"created": True, "user": _user_payload(db, user),
            "message": "Registration submitted. Awaiting admin approval."}


@router.post("/users/register-special", status_code=201)
def register_special(req: dict, current_admin: dict = Depends(require_admin),
                     db: Session = Depends(get_db)):
    try:
        user = account_service.create_special(
            db, username=req.get("username", ""), password=req.get("password", ""),
            is_public_shared=bool(req.get("is_public_shared", False)),
            email=req.get("email"),
        )
    except (ValueError, ApiUsersUnavailable) as exc:
        raise HTTPException(409 if isinstance(exc, ValueError) else 502, str(exc))
    if user.is_public_shared:
        audit_service.log_action(
            db, action="public_flag", actor_user_id=current_admin["id"],
            target_user=auth_service.username_for(db, user.id),
            detail={"is_public_shared": True, "at": "registration"}, status="success",
        )
    return {"created": True, "user": _user_payload(db, user)}


@router.put("/users/{user_id}/approve")
def approve_end_user(user_id: str, current_admin: dict = Depends(require_admin),
                     db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    _check_mutable(user)
    if user.deleted_at is not None:
        raise HTTPException(409, "Cannot approve a deleted account")
    user.is_approved = True
    user.approved_at = _now()
    db.commit()
    return {"approved": True, "user": _user_payload(db, user)}


@router.put("/users/{user_id}")
def update_user(user_id: str, req: dict, current_admin: dict = Depends(require_admin),
                db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    _check_mutable(user)
    if user.deleted_at is not None:
        raise HTTPException(409, "Cannot update a deleted account")
    if "is_public_shared" in req or "public" in req:
        raise HTTPException(400, "The public flag is chosen only at special registration")
    if "role" in req and str(user.id) == str(current_admin["id"]):
        # Self-service role changes are refused: an admin must never widen or
        # narrow its own authority (requirement of 2026-11-02). The dashboard
        # hides the button on the operator's own row as well.
        raise HTTPException(403, "You cannot change your own role")
    detail: dict = {}
    if "role" in req:
        new_role = req["role"]
        if new_role == "special" or user.role == "special":
            raise HTTPException(409, "special is never converted to or from viewer/admin")
        if new_role not in ("viewer", "admin") or new_role == user.role:
            raise HTTPException(400, "role must be viewer or admin")
        if user.role == "admin" and new_role == "viewer":
            # Serialize the last-admin decision over live admin rows.
            db.execute(text(
                "SELECT id FROM gateway.users WHERE role='admin' AND is_active "
                "AND deleted_at IS NULL FOR UPDATE"
            ))
            if auth_service.live_admin_count(db) <= 1:
                raise HTTPException(409, "The last active admin cannot be demoted")
        user.role = new_role
        detail["role"] = new_role
    if "is_active" in req:
        new_active = bool(req["is_active"])
        if not new_active and user.role == "admin":
            db.execute(text(
                "SELECT id FROM gateway.users WHERE role='admin' AND is_active "
                "AND deleted_at IS NULL FOR UPDATE"
            ))
            if auth_service.live_admin_count(db) <= 1:
                raise HTTPException(409, "The last active admin cannot be deactivated")
        if not new_active:
            detail["grants_inactivated"] = acl_service.inactivate_for_user(db, user.id)
        user.is_active = new_active
        detail["is_active"] = new_active
    db.commit()
    if detail:
        audit_service.log_action(
            db, action="user_update", actor_user_id=current_admin["id"],
            target_user=auth_service.username_for(db, user.id), detail=detail,
            status="success",
        )
    # Deactivation inactivates the account's grants; the cross-path set follows.
    if "is_active" in detail:
        schedule_sync_threadsafe()
    row = _user_payload(db, user)
    row["grants"] = acl_service.list_for_grantee(db, user.id)
    return {"updated": True, "user": row}


@router.put("/users/{user_id}/password")
def reset_user_password(user_id: str, req: dict,
                        current_admin: dict = Depends(require_admin),
                        db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    _check_mutable(user)
    if user.deleted_at is not None:
        raise HTTPException(409, "Cannot reset the password of a deleted account")
    password = (req or {}).get("password", "")
    if len(password) < 4:
        raise HTTPException(400, "password too short (min 4 chars)")
    user.password_hash = auth_service.hash_password(password)
    db.commit()
    return {"updated": True, "user": _user_payload(db, user)}


@router.post("/users/{api_user_id}/adopt")
def adopt_owner_route(api_user_id: str, current_admin: dict = Depends(require_admin),
                      db: Session = Depends(get_db)):
    try:
        user = account_service.adopt_owner(db, api_user_id)
    except (ValueError, ApiUsersUnavailable) as exc:
        raise HTTPException(409 if isinstance(exc, ValueError) else 502, str(exc))
    return {"adopted": True, "user": _user_payload(db, user),
            "message": "Reset the password before this viewer can log in."}


@router.post("/users/{api_user_id}/stop-services")
def stop_owner_services(api_user_id: str, current_admin: dict = Depends(require_admin)):
    try:
        return api_users.stop_services(api_user_id)
    except (ApiUsersUnavailable, ValueError) as exc:
        raise HTTPException(502, str(exc))


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, current_admin: dict = Depends(require_admin)):
    """Account deletion saga: stop services, then soft-delete (R5.8)."""
    db = SessionLocal()
    try:
        user = _get_user(db, user_id)
        if user.deleted_at is not None:
            raise HTTPException(409, "Account is already deleted")
        if user.deletion_state != "none":
            raise HTTPException(409, "Account deletion is already in progress")
        if user.role == "admin":
            db.execute(text(
                "SELECT id FROM gateway.users WHERE role='admin' AND is_active "
                "AND deleted_at IS NULL FOR UPDATE"
            ))
            if auth_service.live_admin_count(db) <= 1:
                raise HTTPException(409, "The last active admin cannot be deleted")
        # The last-admin rule above stays the more specific refusal (409); when
        # another admin exists, deleting the account you are signed in as is a
        # mistake, so it is refused outright (requirement of 2026-11-02).
        if str(user.id) == str(current_admin["id"]):
            raise HTTPException(403, "You cannot delete your own account")
        api_user_id = str(user.api_user_id)
        username = auth_service.username_for(db, user.id)
    finally:
        db.close()

    try:
        stopped = await asyncio.to_thread(api_users.stop_services, api_user_id)
    except Exception as exc:  # noqa: BLE001 — abort, account stays
        db = SessionLocal()
        try:
            audit_service.log_action(db, action="delete", actor_user_id=current_admin["id"],
                                     target_user=username, status="failure",
                                     error_message=f"service stop failed: {exc}")
        finally:
            db.close()
        raise HTTPException(502, {"error": "stop_failed",
                                  "message": "Service stop failed; account was not deleted"})
    if stopped.get("ok") is not True:
        db = SessionLocal()
        try:
            audit_service.log_action(db, action="delete", actor_user_id=current_admin["id"],
                                     target_user=username, status="failure",
                                     error_message="service stop failed",
                                     detail={"failed_services": stopped.get("results")})
        finally:
            db.close()
        raise HTTPException(502, {"error": "stop_failed",
                                  "failed_services": stopped.get("results")})

    db = SessionLocal()
    try:
        user = _get_user(db, user_id)
        user.deletion_state = "pending"
        user.is_active = False
        db.commit()
    finally:
        db.close()

    try:
        await asyncio.to_thread(api_users.soft_delete, api_user_id)
    except Exception as exc:  # noqa: BLE001 — stays pending; repair retries
        db = SessionLocal()
        try:
            audit_service.log_action(db, action="delete", actor_user_id=current_admin["id"],
                                     target_user=username, status="pending",
                                     error_message=f"api_user delete failed: {exc}")
        finally:
            db.close()
        raise HTTPException(502, {"error": "api_user_delete_pending",
                                  "message": "Deletion is pending and will be retried"})

    db = SessionLocal()
    try:
        user = _get_user(db, user_id)
        now = _now()
        grants = acl_service.remove_for_user(db, user.id)
        keys_deleted = auth_service.delete_api_keys_for_user(db, user.id)
        sessions = db.query(PreDeploySession).filter(
            PreDeploySession.owner_api_user_id == user.api_user_id
        ).delete(synchronize_session=False)
        exposures = db.query(StreamExposure).filter(
            StreamExposure.owner_user_id == user.id,
            StreamExposure.revoked_at.is_(None),
        ).update({StreamExposure.revoked_at: now}, synchronize_session=False)
        user.is_active = False
        user.is_approved = False
        user.deletion_state = "final"
        user.deleted_at = now
        db.commit()
        audit_service.log_action(
            db, action="delete", actor_user_id=current_admin["id"], target_user=username,
            detail={"soft_delete": True, "grants_removed": grants,
                    "api_keys_removed": keys_deleted,
                    "pre_deploy_sessions_removed": sessions,
                    "stream_exposures_revoked": exposures},
            status="success",
        )
        # The account's services are gone: refresh the cross-path derived state.
        schedule_sync()
        return {"deleted": True, "soft_deleted": True, "username": username}
    finally:
        db.close()
