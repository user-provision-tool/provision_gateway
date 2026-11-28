"""Services router — cards, deploy and lifecycle on IDs (T7/T8/T10)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, get_db
from ..middleware import require_admin, require_gateway_token
from ..models.pre_deploy_session import PreDeploySession
from ..models.user import User
from ..services import acl_service, audit_service, curl_service, service_grants, stream_service
from ..services.cross_orchestrator import cross_orchestrator
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/services", tags=["services"])

_log = logging.getLogger("gateway.services")


def _direct_url(served_name: str, tls: bool) -> str:
    scheme = "https" if tls else "http"
    port = settings.NGINX_HTTPS_PORT if tls else settings.NGINX_HTTP_PORT
    default = 443 if tls else 80
    return f"{scheme}://{served_name}{'' if port == default else f':{port}'}"


def _owner_record(db: Session, username: str):
    from ..models.user_login import UserLogin
    row = db.query(UserLogin).filter(UserLogin.login == username).first()
    return db.query(User).filter(User.id == row.user_id).first() if row else None


def _api_user_row(db: Session, username: str):
    return db.execute(
        text("SELECT id, username, state, deleted_at FROM registry.api_users WHERE username=:u"),
        {"u": username},
    ).mappings().first()


def _annotate_services(db: Session, username: str, services: list[dict]) -> dict:
    """Attach owner/public/grant context to card dicts, in place.

    One home for both the per-user service list and the unified stage
    projection (R9): the card must render identically from either.
    """
    owner = _owner_record(db, username)
    api_user = _api_user_row(db, username)
    gone = bool(
        (owner is not None and owner.deleted_at is not None)
        or (api_user is not None and api_user["deleted_at"] is not None)
    )
    public = acl_service.is_public_owner(owner)
    was_public = bool(
        (owner is not None and owner.is_public_shared)
        or (owner is not None and acl_service.was_public_record(db, owner))
    )
    owner_role = owner.role if owner else None
    context = {
        "owner_api_user_id": str(api_user["id"]) if api_user else None,
        "owner_username": username,
        "owner_role": owner_role,
        "owner_has_gateway_user": owner is not None,
        "owner_is_public_shared": bool(owner.is_public_shared) if owner else False,
        "owner_was_public": was_public,
        "owner_api_user_deleted_at": (
            api_user["deleted_at"].isoformat() if api_user and api_user["deleted_at"] else None
        ),
        "owner_gone": gone,
        "is_public": public,
        "was_public": was_public,
    }
    for svc in services:
        if not isinstance(svc, dict):
            continue
        svc.update(context)
        svc["has_auth"] = bool(svc.get("has_auth"))
        svc["public_urls"] = (
            {n: _direct_url(n, bool(svc.get("tls")))
             for n in (svc.get("http_endpoints") or [])}
            if public else {}
        )
    # G83 — the card's cross-service section: effective targets, explicit rows.
    service_grants.annotate_services(db, services)
    return context


def _annotate(result: dict, db: Session) -> dict:
    username = str(result.get("user_name", ""))
    annotated: list[dict] = []
    for bucket in ("healthy_services", "unhealthy_services", "missing_services",
                   "failed_services"):
        for svc in (result.get(bucket) or []):
            if isinstance(svc, dict):
                annotated.append(svc)
    result.update(_annotate_services(db, username, annotated))
    return result


def _audit(db: Session, action: str, actor: dict, **kw) -> None:
    audit_service.log_action(db, action=action, actor_user_id=actor["id"], **kw)


@router.get("")
async def list_users(request: Request = None,
                     current_user: dict = Depends(require_gateway_token)):
    try:
        result = await provision_service.list_users()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"provision-api error: {e}")
    users = result.get("user_status", [])
    db = SessionLocal()
    try:
        for group in users:
            if isinstance(group, dict):
                _annotate(group, db)
        if current_user["role"] != "admin":
            allowed = acl_service.allowed_usernames(db, current_user["id"])
            users = [g for g in users if g.get("user_name") in allowed]
        orphans = sorted({g.get("user_name") for g in users if g.get("owner_gone")}) \
            if current_user["role"] == "admin" else []
    finally:
        db.close()
    payload = {"users": users, "count": len(users)}
    if current_user["role"] == "admin":
        payload["orphans"] = orphans
    return payload


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str,
                       current_user: dict = Depends(require_gateway_token)):
    """One read-only workflow projection (R8)."""
    from ..services import deploy_workflow as _wf
    from ..services import pre_deploy_registry as pdr
    record = pdr.get_session(workflow_id)
    if (record is not None and current_user["role"] != "admin"
            and str(record.get("owner_api_user_id")) != str(current_user["api_user_id"])):
        raise HTTPException(403, "Access denied: not your workflow")
    try:
        return _wf.workflow_status(workflow_id)
    except KeyError:
        raise HTTPException(404, "Workflow not found")


@router.get("/stages")
async def list_stages(user_name: str | None = Query(None),
                      current_user: dict = Depends(require_gateway_token)):
    """The unified per-tuple stage projection (R9)."""
    from ..services import deploy_workflow as _wf
    if current_user["role"] != "admin":
        db = SessionLocal()
        try:
            allowed = acl_service.allowed_usernames(db, current_user["id"])
        finally:
            db.close()
        if user_name and user_name not in allowed:
            raise HTTPException(403, "Access denied: you can only view your own services")
        result = await _wf.stages()
        result["groups"] = [g for g in result["groups"] if g.get("user_name") in allowed]
        result["count"] = sum(len(g["entries"]) for g in result["groups"])
        result.pop("orphans", None)
        return result
    result = await _wf.stages(user_name)
    db = SessionLocal()
    try:
        for group in result["groups"]:
            instances = [e for e in group["entries"] if e.get("source") == "instance"]
            _annotate_services(db, group.get("user_name") or "", instances)
    finally:
        db.close()
    return result


@router.get("/{user_name}")
async def get_user(user_name: str, request: Request = None,
                   current_user: dict = Depends(require_gateway_token)):
    db = SessionLocal()
    try:
        if current_user["role"] != "admin":
            allowed = acl_service.allowed_usernames(db, current_user["id"])
            if user_name not in allowed:
                raise HTTPException(403, "Access denied: you can only view your own services")
    finally:
        db.close()
    try:
        result = await provision_service.get_user(user_name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"provision-api error: {e}")
    db = SessionLocal()
    try:
        for group in result.get("user_status", []):
            if isinstance(group, dict):
                _annotate(group, db)
    finally:
        db.close()
    return result


def _resolve_owner(db: Session, req: dict) -> dict:
    raw_id = req.get("owner_api_user_id")
    raw_name = (req.get("user_name") or req.get("owner_username") or "").strip()
    if raw_id:
        api_user = db.execute(
            text("SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=:id"),
            {"id": str(raw_id)},
        ).mappings().first()
        if not api_user or api_user["state"] != "live" or api_user["deleted_at"] is not None:
            raise HTTPException(409, "owner is not a live account")
        return {"owner_api_user_id": str(api_user["id"]), "username": api_user["username"]}
    if not raw_name:
        raise HTTPException(422, "an owner is required (owner_api_user_id or user_name)")
    owner = _owner_record(db, raw_name)
    api_user = _api_user_row(db, raw_name)
    if owner is not None and not owner.account_live:
        raise HTTPException(409, "owner account is not live")
    if api_user is None or api_user["state"] != "live" or api_user["deleted_at"] is not None:
        if owner is None:
            raise HTTPException(422, f"owner '{raw_name}' does not exist")
    return {"owner_api_user_id": str(api_user["id"]) if api_user else str(owner.api_user_id),
            "username": raw_name}


@router.post("/deploy", status_code=202)
async def deploy_user(req: dict[str, Any], request: Request,
                      current_admin: dict = Depends(require_admin)):
    """Deploy intake — the gateway owns the whole workflow (R1/R2).

    The client sends the intent; the gateway validates policy, reserves the
    label, records `operation=deploy`, runs generation if needed and, on
    completion, claims and registers without a browser.
    """
    from ..services import deploy_workflow as _wf
    db = SessionLocal()
    try:
        owner = _resolve_owner(db, req)
    finally:
        db.close()
    intent = dict(req)
    intent["owner_api_user_id"] = owner["owner_api_user_id"]
    intent["user_name"] = owner["username"]
    try:
        handle = await _wf.start_deploy(intent, actor_user_id=current_admin["id"])
    except _wf.WorkflowError as exc:
        _audit_short("register", current_admin, req,
                     failure=f"{exc.code}: {exc.detail}")
        raise HTTPException(exc.status, {"error": exc.code, "detail": exc.detail})
    _audit_short("register", current_admin, req)
    return handle


@router.post("/generate", status_code=202)
async def generate_workflow(req: dict[str, Any],
                            current_admin: dict = Depends(require_admin)):
    """Generate intake — same identity and selection, no deploy (R1)."""
    from ..services import deploy_workflow as _wf
    db = SessionLocal()
    try:
        owner = _resolve_owner(db, req)
    finally:
        db.close()
    intent = dict(req)
    intent["owner_api_user_id"] = owner["owner_api_user_id"]
    intent["user_name"] = owner["username"]
    try:
        handle = await _wf.start_generate(intent)
    except _wf.WorkflowError as exc:
        raise HTTPException(exc.status, {"error": exc.code, "detail": exc.detail})
    return handle


def _audit_short(action: str, actor: dict, req: dict, failure: str | None = None) -> None:
    db = SessionLocal()
    try:
        audit_service.log_action(
            db, action=action, actor_user_id=actor["id"],
            target_user=req.get("user_name") or req.get("owner_username"),
            target_service=req.get("service_name"), target_label=req.get("label"),
            detail=req, status="failure" if failure else "success", error_message=failure,
        )
    finally:
        db.close()


@router.get("/{user_name}/{service_name}/next-label")
async def get_next_label(user_name: str, service_name: str,
                         current_admin: dict = Depends(require_admin)):
    try:
        result = await provision_service.get_user(user_name)
    except Exception:
        result = {}
    labels: list[int] = []
    for group in result.get("user_status", []) if isinstance(result, dict) else []:
        for bucket in ("healthy_services", "unhealthy_services", "missing_services",
                       "failed_services"):
            for svc in group.get(bucket) or []:
                if svc.get("service_name") == service_name:
                    try:
                        labels.append(int(str(svc.get("label"))))
                    except (TypeError, ValueError):
                        pass
    db = SessionLocal()
    try:
        owner = _owner_record(db, user_name)
        if owner:
            # Active AND historical labels: a tombstoned label is never reused
            # (R2.3). The -api's status payload excludes tombstones, so the
            # registry must be read directly.
            historical = db.execute(
                text(
                    "SELECT s.label FROM registry.services s "
                    "JOIN registry.api_users a ON a.id=s.owner_api_user_id "
                    "WHERE a.username=:u AND s.service_name=:s"
                ),
                {"u": user_name, "s": service_name},
            ).all()
            for row in historical:
                try:
                    labels.append(int(str(row[0])))
                except (TypeError, ValueError):
                    pass
            for session in db.query(PreDeploySession).filter(
                PreDeploySession.owner_api_user_id == owner.api_user_id,
                PreDeploySession.service_name == service_name,
            ):
                try:
                    labels.append(int(session.service_label))
                except (TypeError, ValueError):
                    pass
            # Reserved labels are held even before a session row exists (R5).
            for row in db.execute(
                text(
                    "SELECT label FROM gateway.label_reservations "
                    "WHERE owner_api_user_id=:o AND service_name=:s"
                ),
                {"o": str(owner.api_user_id), "s": service_name},
            ).all():
                try:
                    labels.append(int(str(row[0])))
                except (TypeError, ValueError):
                    pass
    finally:
        db.close()
    return {"label": str(max(labels) + 1 if labels else 0), "source": "auto_increment"}


# ---------------------------------------------------------------------------
# Lifecycle — ID routes (canonical) and name aliases
# ---------------------------------------------------------------------------

def _resolve_service_id(user_name: str, service_name: str, label: str) -> str:
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT s.id FROM registry.services s JOIN registry.api_users a "
                "ON a.id=s.owner_api_user_id WHERE a.username=:u AND s.service_name=:s "
                "AND s.label=:l AND s.deleted_at IS NULL"
            ),
            {"u": user_name, "s": service_name, "l": str(label)},
        ).mappings().first()
        if not row:
            raise HTTPException(404, "service not found")
        return str(row["id"])
    finally:
        db.close()


async def _lifecycle(service_id: str, action: str, req: dict | None = None,
                     current_admin: dict | None = None) -> dict:
    try:
        if action == "remove":
            from ..services.registry import Registry
            entry = Registry().get_by_id(service_id)
            result = await provision_service.remove_instance(service_id, "service")
            if entry is not None:
                try:
                    from ..services.deploy_workflow import teardown_deploy_copies
                    teardown_deploy_copies(entry)
                except Exception:  # noqa: BLE001
                    _log.exception("deployment copies teardown failed for %s", service_id)
        elif action == "rebuild":
            req = dict(req or {})
            if req.pop("use_global_proxy", False):
                # The Redeploy modal's "use global proxy" tick is not part of
                # the -api's RebuildRequest; expand it here into the four proxy
                # build args a context build needs, or the build reaches no
                # package index.
                from ..services.proxy_service import inject_proxy_build_args
                db = SessionLocal()
                try:
                    req["build_args"] = inject_proxy_build_args(
                        db, req.get("build_args"), True)
                finally:
                    db.close()
            result = await provision_service.rebuild_instance(service_id, **req)
        elif action == "start":
            result = await provision_service.start_instance(service_id)
        elif action == "stop":
            result = await provision_service.stop_instance(service_id)
        elif action == "password_set":
            result = await provision_service.change_instance_password(
                service_id, (req or {}).get("passwd", ""))
        elif action == "password_remove":
            result = await provision_service.remove_instance_password(service_id)
        else:
            raise ValueError(action)
    except Exception as e:  # noqa: BLE001
        raise provision_service.as_http_error(e)
    if action == "remove":
        _revoke_exposures(service_id, current_admin)
    if current_admin:
        _audit_short(action, current_admin, {"service_id": service_id,
                                             "service_name": service_id})
    # G83 — a lifecycle change moves the live/target set. A rebuild recreates
    # containers, so wait for its task; remove/start/stop are already complete.
    if action == "rebuild" and isinstance(result, dict) and result.get("task_id"):
        asyncio.create_task(cross_orchestrator.sync_after_task(result["task_id"]))
    elif action in ("remove", "start", "stop"):
        asyncio.create_task(cross_orchestrator.sync_all())
    return result


def _revoke_exposures(service_id: str, actor: dict | None) -> None:
    db = SessionLocal()
    try:
        stream_service.revoke_instance(db, service_id)
    except Exception:
        pass
    finally:
        db.close()


@router.delete("/instances/{service_id}")
async def delete_instance(service_id: str, scope: str = Query("service", pattern="^(instance|service)$"),
                          current_admin: dict = Depends(require_admin)):
    from ..services.registry import Registry
    entry = Registry().get_by_id(service_id)
    if entry is None:
        raise HTTPException(404, f"No such service: {service_id}")
    if entry.get("service_deleted_at") is not None:
        raise HTTPException(404, "Service is already deleted")
    if entry.get("status") in ("building", "deleting"):
        raise HTTPException(409, f"Service is {entry['status']}; cancel it before delete")
    try:
        result = await provision_service.remove_instance(service_id, scope)
    except Exception as e:  # noqa: BLE001
        raise provision_service.as_http_error(e)
    # R11.6 — the deployment's per-instance copies and its record go with the
    # per-user files the -api removed. An explicit recorded deletion.
    try:
        from ..services.deploy_workflow import teardown_deploy_copies
        teardown_deploy_copies(entry)
    except Exception:  # noqa: BLE001 — teardown metadata never blocks the delete
        _log.exception("deployment copies teardown failed for %s", service_id)
    if scope == "service":
        _revoke_exposures(service_id, current_admin)
        from ..services import pre_deploy_registry as pdr
        pdr.remove_session(service_id)
    _audit_short("remove", current_admin, {"service_id": service_id})
    # G95 R7 — the service delete ends its relationships (grant rows in BOTH
    # directions) and a source that loses its last one must be cleared, even
    # while stopped. The -api's remove is queued, so wait for its task before
    # removing the rows: until the tombstone is written the service is live.
    task_id = result.get("task_id") if isinstance(result, dict) else None
    if task_id:
        asyncio.create_task(cross_orchestrator.sync_after_task(
            str(task_id),
            on_settled=_grant_cleanup_hook(service_id) if scope == "service" else None))
    else:
        # No task handle (an already-settled/refused remove): still converge.
        if scope == "service":
            _remove_service_grants(service_id)
        asyncio.create_task(cross_orchestrator.sync_all())
    return result


def _remove_service_grants(service_id: str) -> int:
    """Delete the service's grant rows in both directions — ONLY once tombstoned.

    R7's cleanup is for a COMPLETED delete. A remove whose stream-conf hold
    failed stops before the disconnect, leaves the service live and marks the
    task failed; its grant rows must survive (only the tombstone ends the
    relationship). A post-tombstone reload failure is the R3 canary and DOES
    end it, so the tombstone — not the task status — is the test.
    """
    from sqlalchemy import text
    from ..database import ReaderSessionLocal

    rd = ReaderSessionLocal()
    try:
        row = rd.execute(
            text("SELECT deleted_at FROM registry.services WHERE id=:id"),
            {"id": service_id},
        ).mappings().first()
    finally:
        rd.close()
    if not row or row["deleted_at"] is None:
        return 0
    db = SessionLocal()
    try:
        return service_grants.remove_grants_for_service(db, service_id)
    finally:
        db.close()


def _grant_cleanup_hook(service_id: str):
    def hook() -> None:
        _remove_service_grants(service_id)
    return hook


@router.post("/instances/{service_id}/rebuild")
async def rebuild_instance(service_id: str, req: dict[str, Any] = {},
                           current_admin: dict = Depends(require_admin)):
    from ..services.registry import Registry
    entry = Registry().get_by_id(service_id)
    if entry is None:
        raise HTTPException(404, f"No such service: {service_id}")
    if entry.get("status") in ("building", "deleting"):
        raise HTTPException(409, f"Service is {entry['status']}; wait for it to settle")
    return await _lifecycle(service_id, "rebuild", req, current_admin)


@router.post("/instances/{service_id}/start")
async def start_instance(service_id: str, current_admin: dict = Depends(require_admin)):
    return await _lifecycle(service_id, "start", None, current_admin)


@router.post("/instances/{service_id}/stop")
async def stop_instance(service_id: str, current_admin: dict = Depends(require_admin)):
    return await _lifecycle(service_id, "stop", None, current_admin)


@router.put("/instances/{service_id}/password")
async def set_instance_password(service_id: str, req: dict[str, Any],
                                current_admin: dict = Depends(require_admin)):
    return await _lifecycle(service_id, "password_set", req, current_admin)


@router.delete("/instances/{service_id}/password")
async def remove_instance_password(service_id: str,
                                   current_admin: dict = Depends(require_admin)):
    return await _lifecycle(service_id, "password_remove", None, current_admin)


# Name aliases (resolve exactly one active service_id, then mutate by ID).
@router.delete("/{user_name}/{service_name}/{label}")
async def remove_user_service(user_name: str, service_name: str, label: str,
                              current_admin: dict = Depends(require_admin)):
    sid = _resolve_service_id(user_name, service_name, label)
    return await delete_instance(sid, "service", current_admin)


@router.post("/{user_name}/{service_name}/{label}/rebuild")
async def rebuild_user_service(user_name: str, service_name: str, label: str,
                               req: dict[str, Any] = {},
                               current_admin: dict = Depends(require_admin)):
    return await _lifecycle(_resolve_service_id(user_name, service_name, label),
                            "rebuild", req, current_admin)


@router.post("/{user_name}/{service_name}/{label}/up")
async def start_user_service(user_name: str, service_name: str, label: str,
                             current_admin: dict = Depends(require_admin)):
    return await _lifecycle(_resolve_service_id(user_name, service_name, label),
                            "start", None, current_admin)


@router.post("/{user_name}/{service_name}/{label}/down")
async def stop_user_service(user_name: str, service_name: str, label: str,
                            current_admin: dict = Depends(require_admin)):
    return await _lifecycle(_resolve_service_id(user_name, service_name, label),
                            "stop", None, current_admin)


@router.put("/{user_name}/{service_name}/{label}/password")
async def change_user_password(user_name: str, service_name: str, label: str,
                               req: dict[str, Any],
                               current_admin: dict = Depends(require_admin)):
    return await _lifecycle(_resolve_service_id(user_name, service_name, label),
                            "password_set", req, current_admin)


@router.delete("/{user_name}/{service_name}/{label}/password")
async def remove_user_password(user_name: str, service_name: str, label: str,
                               current_admin: dict = Depends(require_admin)):
    return await _lifecycle(_resolve_service_id(user_name, service_name, label),
                            "password_remove", None, current_admin)


@router.get("/{user_name}/{service_name}/{label}/containers/{container}/logs")
async def get_container_logs(user_name: str, service_name: str, label: str, container: str,
                             tail: int = Query(100, ge=1, le=10000),
                             current_admin: dict = Depends(require_admin)):
    try:
        return await provision_service.get_container_logs(user_name, service_name, label,
                                                          container, tail)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"provision-api error: {e}")


@router.get("/{user_name}/{service_name}/{label}/url")
async def get_service_url(user_name: str, service_name: str, label: str,
                          current_admin: dict = Depends(require_admin)):
    from ..services.registry import Registry
    entry = Registry().get_entry(user_name, service_name, label)
    if not entry:
        raise HTTPException(404, "service not found")
    endpoints = entry.get("http_endpoints") or []
    served = endpoints[0] if endpoints else entry.get("hostname", "")
    https = bool(entry.get("tls"))
    return {
        "url": _direct_url(served, https) if served else "",
        "http_url": _direct_url(served, False) if served else "",
        "https_enabled": https,
        "auth_enabled": bool(entry.get("htpasswd_path")),
        "nginx_http_port": settings.NGINX_HTTP_PORT,
        "nginx_https_port": settings.NGINX_HTTPS_PORT,
        "_internal_host": "subnet-nginx",
    }


@router.post("/{user_name}/{service_name}/{label}/test-curl")
async def test_curl(user_name: str, service_name: str, label: str,
                    req: dict[str, Any] = {},
                    current_admin: dict = Depends(require_admin)):
    info = await get_service_url(user_name, service_name, label, current_admin)
    result = await curl_service.test_url(
        url=info["url"], include_auth=req.get("include_auth", True),
        username=user_name, password=req.get("passwd", ""),
        follow_redirect=req.get("follow_redirect", True),
    )
    return {"url": result.url, "http_code": result.http_code, "headers": result.headers,
            "body_preview": result.body_preview, "time_total_ms": result.time_total_ms,
            "error": result.error}


def _source_services(source: dict) -> dict[str, dict]:
    """Flatten the -api's user_status buckets into ``old key -> service``."""
    out: dict[str, dict] = {}
    for group in source.get("user_status", []) if isinstance(source, dict) else []:
        for bucket in ("healthy_services", "unhealthy_services", "missing_services",
                       "failed_services"):
            for svc in group.get(bucket) or []:
                key = f"{source.get('user_name')}/{svc.get('service_name')}/{svc.get('label')}"
                out[key] = svc
    return out


def _artifact_path(svc: dict, user_name: str) -> Path | None:
    """The source artifact beside the service's recipe (G102)."""
    from ..services.file_sets import FileSetError, _recipe_dir
    try:
        base = Path(settings.SOURCE_PROJECTS_DIR) / str(svc.get("service_name"))
        recipe = _recipe_dir(base, svc.get("recipe_path") or "")
    except FileSetError:
        return None
    return recipe / f"dependencies.{user_name}.{svc.get('label')}.json"


async def _wait_task(task_id: str, *, timeout_s: float = 600.0) -> dict:
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        data = await provision_service.get_task(task_id)
        status = str(data.get("status") or "")
        if status in ("completed", "failed", "cancelled", "unknown"):
            return data
        await asyncio.sleep(2.0)
    raise TimeoutError(f"clone registration task {task_id} did not finish")


async def _new_label(target_user: str, service_id: str) -> str | None:
    target = await provision_service.get_user(target_user)
    for key, svc in _source_services({"user_name": target_user,
                                      **target}).items():
        if str(svc.get("service_id")) == str(service_id):
            return str(svc.get("label"))
    return None


async def _clone_user_impl(source_user: str, target_user: str, req: dict) -> dict:
    """G102 R5 — clone with dependency artifacts and mapped grants.

    Registration order is dependency-first; the new label is resolved from the
    target user's status by the returned service_id (the register response
    carries no label); the mapped grants are written only after every
    registration completed (both services must be running); the artifacts are
    remapped and rewritten for the target user.
    """
    import json as _json
    try:
        source = await provision_service.get_user(source_user)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"provision-api error: {e}")
    services = _source_services({"user_name": source_user, **source})
    artifacts: dict[str, dict] = {}
    deps_of: dict[str, set[str]] = {}
    for key, svc in services.items():
        path = _artifact_path(svc, source_user)
        if path and path.is_file():
            try:
                data = _json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            artifacts[key] = data
            deps_of[key] = {k for k in (data.get("services") or {}) if k != key}

    tasks: list[dict] = []
    key_map: dict[str, str] = {}  # old key -> new key
    sid_map: dict[str, str] = {}  # old key -> new service id
    pending = dict(services)
    while pending:
        ready = [k for k in pending if not (deps_of.get(k, set()) & set(pending))]
        if not ready:
            # A cycle (or a dependency outside the source set): register the
            # remainder in a stable order and report the broken edge.
            ready = sorted(pending)[:1]
        for old_key in ready:
            svc = pending.pop(old_key)
            payload = {
                "user_name": target_user,
                "service_name": svc.get("service_name"),
                "label": None,
                "compose_template_path": svc.get("compose_template_path"),
                "nginx_conf_template_path": svc.get("nginx_conf_template_path"),
                "recipe_path": svc.get("recipe_path", ""),
                "domain": req.get("domain", "localhost"),
                "passwd": req.get("passwd", ""),
            }
            if svc.get("profiles"):
                payload["profiles"] = svc["profiles"]
            try:
                result = await provision_service.register_user(**payload)
                task_id = result.get("task_id")
                service_id = result.get("service_id")
                tasks.append({"service": svc.get("service_name"), "service_id": service_id,
                              "task_id": task_id})
                if task_id:
                    task = await _wait_task(str(task_id))
                    if str(task.get("status")) != "completed":
                        tasks[-1]["error"] = (
                            f"registration {task.get('status')}: {task.get('error') or ''}")
                        continue
                label = await _new_label(target_user, str(service_id))
                if label is None:
                    tasks[-1]["error"] = "the target label could not be resolved"
                    continue
                new_key = f"{target_user}/{svc.get('service_name')}/{label}"
                key_map[old_key] = new_key
                sid_map[old_key] = str(service_id)
                tasks[-1]["label"] = label
            except Exception as e:  # noqa: BLE001
                tasks.append({"service": svc.get("service_name"), "error": str(e)})

    # Mapped grants only after every registration (both ends are running).
    db = SessionLocal()
    try:
        written: set[tuple[str, str]] = set()
        for old_key, data in artifacts.items():
            src = sid_map.get(old_key)
            if not src:
                continue
            for dep_old in (data.get("services") or {}):
                if dep_old == old_key:
                    continue
                tgt = sid_map.get(dep_old)
                if not tgt or (src, tgt) in written:
                    continue
                try:
                    service_grants.set_grant(db, src, tgt)
                    written.add((src, tgt))
                except ValueError as exc:
                    tasks.append({"service": f"{old_key} -> {dep_old}",
                                  "error": f"grant not written: {exc}"})
    finally:
        db.close()

    # Rewrite each artifact for the target user, remapping the keys.
    for old_key, data in artifacts.items():
        new_source = key_map.get(old_key)
        if not new_source:
            continue
        source_svc = services[old_key]
        new_key = new_source
        new_services: dict[str, dict] = {}
        source_record = dict((data.get("services") or {}).get(old_key) or {})
        env: dict[str, dict] = {}
        for var, ref in (source_record.get("env") or {}).items():
            dep_old = str((ref or {}).get("depends_on") or "")
            if dep_old in key_map:
                env[str(var)] = {"depends_on": key_map[dep_old]}
        for svc_key, record in (data.get("services") or {}).items():
            if svc_key == old_key:
                continue
            if svc_key not in key_map:
                tasks.append({"service": svc_key,
                              "error": "dependency has no clone counterpart; omitted"})
                continue
            new_svc_key = key_map[svc_key]
            new_services[new_svc_key] = {
                "user": target_user,
                "service": record.get("service"),
                "label": new_svc_key.rsplit("/", 1)[-1],
                "env": {},
            }
        new_services[new_key] = {
            "user": target_user,
            "service": source_svc.get("service_name"),
            "label": new_key.rsplit("/", 1)[-1],
            "env": env,
        }
        path = _artifact_path(source_svc, target_user)
        if path is None:
            continue
        content = _json.dumps({
            "version": 1,
            "generated_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": new_key,
            "services": new_services,
        }, indent=2) + "\n"
        from ..services.generated_marker import write_marker_pair
        write_marker_pair(path, content, cls="ins",
                          description=f"cloned per-user dependency artifact ({path.name})")
    return {"tasks": tasks, "total": len(tasks)}


@router.post("/clone", status_code=202)
async def clone_user(req: dict[str, Any],
                     current_admin: dict = Depends(require_admin)):
    source_user = req.get("source_user")
    target_user = req.get("target_user")
    if not source_user or not target_user:
        raise HTTPException(400, "source_user and target_user are required")
    return await _clone_user_impl(str(source_user), str(target_user), req)


# ---------------------------------------------------------------------------
# Per-user deployment files (recipe-scoped)
# ---------------------------------------------------------------------------

def _resolve_per_user_file(user_name: str, service_name: str, label: str,
                           recipe_path: str, filename: str) -> Path | None:
    from ..services.file_sets import FileSetError, _recipe_dir
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    try:
        recipe_dir = _recipe_dir(project_dir, recipe_path or "")
    except FileSetError:
        return None
    if filename.startswith("/") or "\\" in filename or ":" in filename or ".." in filename.split("/"):
        return None
    resolved = (recipe_dir / filename).resolve()
    if not str(resolved).startswith(str(recipe_dir.resolve()) + "/"):
        return None
    return resolved


@router.get("/{user_name}/{service_name}/{label}/per-user-file")
async def get_per_user_file(user_name: str, service_name: str, label: str,
                            recipe_path: str = Query(""), filename: str = Query(...),
                            current_admin: dict = Depends(require_admin)):
    fp = _resolve_per_user_file(user_name, service_name, label, recipe_path, filename)
    if fp is None:
        raise HTTPException(400, f"Invalid per-user file path: {filename!r}")
    content = fp.read_text() if fp.exists() else ""
    return {"user_name": user_name, "service_name": service_name, "label": label,
            "recipe_path": recipe_path, "filename": fp.name, "path": str(fp),
            "content": content, "size": len(content),
            "modified_at": fp.stat().st_mtime if fp.exists() else None,
            "exists": fp.exists()}


@router.put("/{user_name}/{service_name}/{label}/per-user-file")
async def save_per_user_file(user_name: str, service_name: str, label: str,
                             req: dict[str, Any],
                             current_admin: dict = Depends(require_admin),
                             db: Session = Depends(get_db)):
    recipe_path = req.get("recipe_path") or ""
    filename = req.get("filename") or ""
    content = req.get("content") or ""
    if not filename:
        raise HTTPException(400, "'filename' is required")
    fp = _resolve_per_user_file(user_name, service_name, label, recipe_path, filename)
    if fp is None:
        raise HTTPException(400, f"Invalid per-user file path: {filename!r}")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    _audit(db, "per_user_file_edit", current_admin, target_user=user_name,
           target_service=service_name, target_label=label,
           detail={"recipe_path": recipe_path, "filename": fp.name}, status="success")
    return {"saved": True, "filename": fp.name, "path": str(fp), "size": len(content)}


@router.get("/{user_name}/{service_name}/{label}/registration-time")
async def get_registration_time(user_name: str, service_name: str, label: str,
                                current_admin: dict = Depends(require_admin)):
    sid = _resolve_service_id(user_name, service_name, label)
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT max(updated_at) AS t FROM registry.tasks "
                "WHERE service_id=:id AND kind='deploy' AND status='completed'"
            ),
            {"id": sid},
        ).mappings().first()
        return {"registration_time": row["t"].isoformat() if row and row["t"] else None}
    finally:
        db.close()


@router.get("/{user_name}/{service_name}/{label}/volume-usage")
async def get_volume_usage(user_name: str, service_name: str, label: str,
                           current_admin: dict = Depends(require_admin)):
    import os as _os
    import shutil as _shutil
    user_data_dir = settings.USER_DATA_DIR / user_name / service_name
    volumes: dict[str, dict[str, Any]] = {}
    if user_data_dir.exists():
        for vol_dir in user_data_dir.iterdir():
            if not vol_dir.is_dir():
                continue
            du = _shutil.disk_usage(str(vol_dir))
            size = 0
            for dirpath, _dirnames, filenames in _os.walk(str(vol_dir)):
                for f in filenames:
                    try:
                        size += _os.path.getsize(_os.path.join(dirpath, f))
                    except OSError:
                        pass
            volumes[vol_dir.name] = {"path": str(vol_dir), "size_bytes": size,
                                     "disk_total_bytes": du.total, "disk_used_bytes": du.used,
                                     "disk_free_bytes": du.free}
    return {"user_name": user_name, "service_name": service_name, "label": label,
            "user_data_dir": str(user_data_dir), "volumes": volumes}
