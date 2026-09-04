"""Users router — /api/users/* endpoints (proxied to provision-api, enriched)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import require_admin, require_gateway_token
from ..models.end_user import EndUser
from ..config import settings
from ..services import audit_service, curl_service
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/users", tags=["users"])


def _log_action_short(action: str, admin_id: int | None = None, **kw) -> None:
    """Record an audit log entry with a fresh short-lived session.

    Used for audit logging that happens after a long ``await`` so no DB
    connection is held across the external call.
    """
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        audit_service.log_action(db, action=action, admin_id=admin_id, **kw)
    finally:
        db.close()


@router.get("")
async def list_users(
    request: Request = None,
    current_user: dict = Depends(require_gateway_token),
):
    """List all end-users from provision-api, syncing missing users to gateway DB.

    Viewers: results are filtered to own services + allowed_special_users.
    Admins: see all services.

    The EndUser sync/filter uses a short-lived session opened AFTER the
    provision-api await, so no DB connection is held during the call.
    """
    try:
        result = await provision_service.list_users()
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    users = result.get("user_status", [])

    # Sync: ensure all users from provision-api exist in gateway end_users DB
    from ..database import SessionLocal
    from ..models.end_user import EndUser
    import bcrypt as _bcrypt
    import secrets

    db = SessionLocal()
    try:
        gateway_users = {u.username for u in db.query(EndUser).all()}
        for u in users:
            user_name = u.get("user_name", "").strip()
            if user_name and user_name not in gateway_users:
                random_pw = secrets.token_hex(16)
                new_user = EndUser(
                    username=user_name,
                    password_hash=_bcrypt.hashpw(random_pw.encode(), _bcrypt.gensalt()).decode(),
                    role="viewer",
                    is_approved=True,
                    is_active=True,
                )
                db.add(new_user)
        db.commit()

        # Filter for viewers: own services + allowed_special_users only
        if current_user["role"] != "admin":
            viewer_name = current_user.get("email", "")
            allowed_users = set()
            if viewer_name:
                allowed_users.add(viewer_name)
            # Get allowed_special_users from the current user
            if current_user.get("user_type") == "end_user":
                eu = db.query(EndUser).filter(EndUser.id == current_user["id"]).first()
                if eu and eu.allowed_special_users:
                    for name in eu.allowed_special_users.split(","):
                        name = name.strip()
                        if name:
                            allowed_users.add(name)
            users = [u for u in users if u.get("user_name") in allowed_users]
    finally:
        db.close()

    return {"users": users, "count": len(users)}


@router.get("/{user_name}")
async def get_user(
    user_name: str,
    request: Request = None,
    current_user: dict = Depends(require_gateway_token),
):
    """Get a single end-user's services from provision-api.

    Viewers may only see their own services; admins can see anyone's.
    The ACL check uses a short-lived session, closed before the provision-api
    call so no DB connection is held during the await.
    """
    # ACL check for viewers
    if current_user["role"] != "admin":
        viewer_name = current_user.get("email", "")
        allowed = {viewer_name}
        if current_user.get("user_type") == "end_user":
            from ..database import SessionLocal
            db = SessionLocal()
            try:
                eu = db.query(EndUser).filter(EndUser.id == current_user["id"]).first()
                if eu and eu.allowed_special_users:
                    for name in eu.allowed_special_users.split(","):
                        name = name.strip()
                        if name:
                            allowed.add(name)
            finally:
                db.close()
        if user_name not in allowed:
            raise HTTPException(403, "Access denied: you can only view your own services")

    try:
        result = await provision_service.get_user(user_name)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.post("/deploy", status_code=202)
async def deploy_user(
    req: dict[str, Any],
    request: Request,
    current_admin: dict = Depends(require_admin),
):
    """Deploy a service to a user (proxied to provision-api POST /users).

    Compose is the root of the dependency graph (design §Dependency graph):
    deploy hard-gates on compose presence only — nginx is optional. Returns
    400 if the deploy would fail due to a missing compose source.
    A successful submission persists the file-set selection as the new
    default (design §Selection & UI L43-46: persist on 202 accept, not task
    completion). All DB work uses short-lived sessions so no connection is
    held during the (potentially long) provision-api deploy call.
    """
    # Validate that a compose source is provided (GAP-002 + design gate).
    service_name = req.get("service_name", "")
    compose_path = (
        req.get("compose_file_path") or req.get("compose_template_path")
        or (req.get("compose_file_paths") or [None])[0]
    )

    # Check if service project has a compose source
    from starlette.concurrency import run_in_threadpool
    from ..services.service_manager import service_manager
    info = await run_in_threadpool(service_manager.get_service, service_name)
    if info:
        files = info.get("files", [])
        has_compose = any(f.endswith((".yml", ".yaml")) for f in files if not f.endswith(".j2"))
        has_compose |= any(f.endswith(".yml.j2") for f in files)

        if not has_compose and not compose_path:
            raise HTTPException(
                400,
                f"Cannot deploy '{service_name}': missing docker-compose.yml (or .yml.j2 template). "
                "Use the generate-missing panel (LLM configured) to generate it, or add it manually "
                "to the source project before deploying.",
            )

    # Persist the selection as the new default on submission (202 accept).
    selection = req.get("selection")
    recipe_path = req.get("recipe_path") or ""
    if isinstance(selection, dict):
        from ..services.file_sets import FileSetError, put_file_set
        try:
            put_file_set(service_name, recipe_path, selection)
        except FileSetError:
            pass  # selection persistence is best-effort — deploy still proceeds

    # Inject global proxy into build_args if requested
    use_global_proxy = req.pop("use_global_proxy", False)
    if use_global_proxy:
        from ..services.proxy_service import inject_proxy_build_args, has_active_proxy
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            if not has_active_proxy(db):
                raise HTTPException(400, "Global proxy is not enabled. Configure it in Settings first.")
            build_args = req.get("build_args") or {}
            req["build_args"] = inject_proxy_build_args(db, build_args, True)
        finally:
            db.close()

    # Auto-register the user in gateway end_users if not already present
    user_name = req.get("user_name", "").strip()
    if user_name:
        from ..models.end_user import EndUser
        from ..database import SessionLocal
        import bcrypt as _bcrypt
        db = SessionLocal()
        try:
            existing = db.query(EndUser).filter(EndUser.username == user_name).first()
            if not existing:
                # Auto-register with a random password (not used for login by default)
                import secrets
                random_pw = secrets.token_hex(16)
                new_user = EndUser(
                    username=user_name,
                    password_hash=_bcrypt.hashpw(random_pw.encode(), _bcrypt.gensalt()).decode(),
                    role="viewer",
                    is_approved=True,
                    is_active=True,
                )
                db.add(new_user)
                db.commit()
        finally:
            db.close()

    try:
        result = await provision_service.register_user(**req)
    except Exception as e:
        _log_action_short(
            action="register",
            admin_id=current_admin["id"],
            target_user=req.get("user_name"),
            target_service=req.get("service_name"),
            target_label=req.get("label", "0"),
            detail=req,
            status="failure",
            error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    _log_action_short(
        action="register",
        admin_id=current_admin["id"],
        target_user=req.get("user_name"),
        target_service=req.get("service_name"),
        target_label=req.get("label", "0"),
        detail=req,
        status="success",
    )
    return result


# ---------------------------------------------------------------------------
# Service label auto-increment (GAP-003)
# ---------------------------------------------------------------------------

@router.get("/{user_name}/{service_name}/next-label")
async def get_next_label(
    user_name: str,
    service_name: str,
    current_admin: dict = Depends(require_admin),
):
    """Compute the next available service label for a user+service combo.

    Queries provision-api for existing instances and returns the next
    auto-incremented integer label. Redeploy does NOT change the label.
    Label only increments when deploying multiple instances of the same
    source project for the same user.
    """
    try:
        result = await provision_service.get_user(user_name)
    except Exception:
        # If user not found or provision-api unreachable, start at 0
        return {"label": "0", "source": "default"}

    # Collect all services from provision-api response
    services_raw = result.get("user_status", result if isinstance(result, list) else [])
    if isinstance(services_raw, dict):
        # Handle nested structure
        services_raw = services_raw.get("healthy_services", []) + \
                       services_raw.get("unhealthy_services", []) + \
                       services_raw.get("missing_services", [])

    existing_labels: list[int] = []
    for entry in services_raw:
        if isinstance(entry, dict) and entry.get("service_name") == service_name:
            try:
                existing_labels.append(int(entry.get("label", "0")))
            except (ValueError, TypeError):
                pass

    next_label = max(existing_labels) + 1 if existing_labels else 0
    return {"label": str(next_label), "source": "auto_increment"}


@router.delete("/{user_name}/{service_name}/{label}")
async def remove_user_service(
    user_name: str,
    service_name: str,
    label: str,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove a user's service instance."""
    try:
        result = await provision_service.remove_user(user_name, service_name, label)
    except Exception as e:
        audit_service.log_action(
            db, action="remove", admin_id=current_admin["id"],
            target_user=user_name, target_service=service_name,
            target_label=label, status="failure", error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db, action="remove", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.post("/{user_name}/{service_name}/{label}/rebuild")
async def rebuild_user_service(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any] = {},
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Rebuild a user's service instance."""
    # Inject global proxy into build_args if requested
    use_global_proxy = req.pop("use_global_proxy", False)
    if use_global_proxy:
        from ..services.proxy_service import inject_proxy_build_args, has_active_proxy
        if not has_active_proxy(db):
            raise HTTPException(400, "Global proxy is not enabled. Configure it in Settings first.")
        build_args = req.get("build_args") or {}
        req["build_args"] = inject_proxy_build_args(db, build_args, True)

    try:
        result = await provision_service.rebuild_user(user_name, service_name, label, **req)
    except Exception as e:
        audit_service.log_action(
            db, action="rebuild", admin_id=current_admin["id"],
            target_user=user_name, target_service=service_name,
            target_label=label, status="failure", error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db, action="rebuild", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.post("/{user_name}/{service_name}/{label}/up")
async def start_user_service(
    user_name: str, service_name: str, label: str,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Start a user's service (delegated to provision-api)."""
    try:
        result = await provision_service.start_user(user_name, service_name, label)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    audit_service.log_action(db, action="start", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name, target_label=label, status="success")
    return result


@router.post("/{user_name}/{service_name}/{label}/down")
async def stop_user_service(
    user_name: str, service_name: str, label: str,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Stop a user's service (delegated to provision-api)."""
    try:
        result = await provision_service.stop_user(user_name, service_name, label)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    audit_service.log_action(db, action="stop", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name, target_label=label, status="success")
    return result


@router.put("/{user_name}/{service_name}/{label}/password")
async def change_user_password(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Change a user's htpasswd password (delegated to provision-api)."""
    passwd = req.get("passwd", "")
    if not passwd:
        raise HTTPException(400, "passwd is required")

    try:
        result = await provision_service.change_user_password(user_name, service_name, label, passwd)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db, action="password_change", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.get("/{user_name}/{service_name}/{label}/containers/{container}/logs")
async def get_container_logs(
    user_name: str, service_name: str, label: str, container: str,
    tail: int = Query(100, ge=1, le=10000, description="Number of log lines to return"),
    current_admin: dict = Depends(require_admin),
):
    """Get container logs for a specific compose service (delegated to provision-api)."""
    try:
        result = await provision_service.get_container_logs(
            user_name, service_name, label, container, tail,
        )
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.get("/{user_name}/{service_name}/{label}/url")
async def get_service_url(
    user_name: str, service_name: str, label: str,
    current_admin: dict = Depends(require_admin),
):
    """Get the URL for a user's service instance."""
    from ..config import settings
    from pathlib import Path
    import re

    generated_dir = settings.PROVISION_DIR / "generated"
    nginx_conf = generated_dir / f"{service_name}.user-{user_name}.{label}.nginx.conf"

    server_name = f"{service_name}-{user_name}-{label}.localhost"
    https_enabled = False

    if nginx_conf.exists():
        content = nginx_conf.read_text()
        m = re.search(r"server_name\s+([^;]+);", content)
        if m:
            server_name = m.group(1).strip().split()[0]
        # Only detect HTTPS from actual directives, not comments
        https_enabled = bool(re.search(r"^\s*listen\s+443\s+ssl", content, re.MULTILINE))

    http_port = settings.NGINX_HTTP_PORT
    https_port = settings.NGINX_HTTPS_PORT

    # Append port to URL when non-standard
    http_url = f"http://{server_name}"
    if http_port != 80:
        http_url += f":{http_port}"
    https_url = f"https://{server_name}"
    if https_port != 443:
        https_url += f":{https_port}"

    return {
        "url": https_url if https_enabled else http_url,
        "http_url": http_url,
        "https_enabled": https_enabled,
        "auth_enabled": True,
        "nginx_http_port": http_port,
        "nginx_https_port": https_port,
        # Internal URL reachable from Docker network (test-curl uses this)
        "_internal_host": "subnet-acl-nginx",
    }


@router.post("/{user_name}/{service_name}/{label}/test-curl")
async def test_curl(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any] = {},
    current_admin: dict = Depends(require_admin),
):
    """Test a user's service URL with curl from inside the gateway container."""
    url_info = await get_service_url(user_name, service_name, label, current_admin)
    url = url_info["url"]
    include_auth = req.get("include_auth", True)
    follow_redirect = req.get("follow_redirect", True)
    passwd = req.get("passwd", "")

    result = await curl_service.test_url(
        url=url, include_auth=include_auth,
        username=user_name, password=passwd,
        follow_redirect=follow_redirect,
    )

    return {
        "url": result.url, "http_code": result.http_code,
        "headers": result.headers, "body_preview": result.body_preview,
        "time_total_ms": result.time_total_ms, "error": result.error,
    }


@router.post("/clone", status_code=202)
async def clone_user(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Clone all services from source_user to target_user."""
    source_user = req.get("source_user")
    target_user = req.get("target_user")
    if not source_user or not target_user:
        raise HTTPException(400, "source_user and target_user are required")

    try:
        source_data = await provision_service.get_user(source_user)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")

    tasks = []
    entries = source_data if isinstance(source_data, list) else source_data.get("services", source_data.get("user_status", []))
    if isinstance(entries, dict):
        entries = [entries]

    for service_entry in entries:
        deploy_req = {
            "user_name": target_user,
            "service_name": service_entry.get("service_name"),
            "label": service_entry.get("label", "0"),
            "compose_file_path": service_entry.get("compose_template_path"),
            "nginx_conf_file_path": service_entry.get("nginx_conf_template_path"),
            "domain": req.get("domain", "localhost"),
            "passwd": req.get("passwd", "123456"),
        }
        try:
            result = await provision_service.register_user(**deploy_req)
            tasks.append({"service": service_entry.get("service_name"),
                          "label": service_entry.get("label", "0"),
                          "task_id": result.get("task_id")})
        except Exception as e:
            tasks.append({"service": service_entry.get("service_name"),
                          "label": service_entry.get("label", "0"),
                          "error": str(e)})

    audit_service.log_action(
        db, action="clone", admin_id=current_admin["id"],
        target_user=target_user,
        detail={"source_user": source_user, "tasks": tasks},
        status="success",
    )
    return {"tasks": tasks, "total": len(tasks)}


# ---------------------------------------------------------------------------
# Per-user deployment files (per-user-per-recipe scoping — design §Per-recipe
# scoping L221-238). The convention-based deployment-file editor
# (_resolve_deployment_file + fixed project-root paths + source-fallback) is
# RETIRED. A plain per-user-file GET/PUT keyed by (user, service, label,
# recipe_path) replaces it — no path-guessing; the deploy panel computes the
# per-user-per-recipe path itself and writes through its review/edit gate.
# ---------------------------------------------------------------------------

from pathlib import Path as _Path
import os as _os


def _resolve_per_user_file(
    user_name: str, service_name: str, label: str, recipe_path: str, filename: str
) -> _Path | None:
    """Resolve a per-user deployment file inside the recipe dir.

    Per-user files live in the RECIPE dir (not the project root)::
        {recipe}/.env.{user}.{label}
        {recipe}/docker-compose.user-{user}.{label}.yml

    The recipe dir is the project dir itself when ``recipe_path`` is empty.
    Path traversal is rejected (the file must stay inside the recipe dir).
    """
    from ..services.file_sets import FileSetError, _recipe_dir

    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    try:
        recipe_dir = _recipe_dir(project_dir, recipe_path or "")
    except FileSetError:
        return None  # traversal-invalid recipe_path ('' = project root)
    if filename.startswith("/") or "\\" in filename or ":" in filename or ".." in filename.split("/"):
        return None
    resolved = (recipe_dir / filename).resolve()
    recipe_resolved = recipe_dir.resolve()
    if not str(resolved).startswith(str(recipe_resolved) + "/") and resolved != recipe_resolved:
        return None
    return resolved


@router.get("/{user_name}/{service_name}/{label}/per-user-file")
async def get_per_user_file(
    user_name: str, service_name: str, label: str,
    recipe_path: str = Query("", description="Recipe subdirectory ('' = project root)"),
    filename: str = Query(..., description="Per-user filename, e.g. .env.{user}.{label}"),
    current_admin: dict = Depends(require_admin),
):
    """Get a per-user deployment file's content.

    Falls back to the recipe's base file when the per-user file does not
    exist yet (prefill from base/generated/last-saved → edit → save), so the
    review/edit gate always starts from a meaningful document.
    """
    fp = _resolve_per_user_file(user_name, service_name, label, recipe_path, filename)
    if fp is None:
        raise HTTPException(400, f"Invalid per-user file path: {filename!r}")

    content = ""
    exists = fp.exists()
    if exists:
        try:
            content = fp.read_text()
        except Exception as e:
            raise HTTPException(500, f"Failed to read file: {e}")
    else:
        # Prefill fallback: the recipe-level base file with the same stem
        # (e.g. .env for .env.{user}.{label}) — the base selection is the
        # sensible starting point for a per-user file.
        base_candidates = [fp.name, fp.name.split(".")[0] if "." in fp.name else ""]
        for base_name in base_candidates:
            base = fp.parent / base_name
            if base.is_file() and base != fp:
                try:
                    content = base.read_text()
                except Exception:
                    content = ""
                break

    return {
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
        "recipe_path": recipe_path,
        "filename": fp.name,
        "path": str(fp),
        "content": content,
        "size": len(content),
        "modified_at": fp.stat().st_mtime if exists else None,
        "exists": exists,
        "source_fallback": not exists and bool(content),
    }


@router.put("/{user_name}/{service_name}/{label}/per-user-file")
async def save_per_user_file(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save a per-user deployment file's content (review/edit gate write-through).

    Body: ``{"recipe_path": "...", "filename": ".env.alice.0", "content": "..."}``
    """
    recipe_path = req.get("recipe_path") or ""
    filename = req.get("filename") or ""
    content = req.get("content") or ""
    if not filename:
        raise HTTPException(400, "'filename' is required")
    fp = _resolve_per_user_file(user_name, service_name, label, recipe_path, filename)
    if fp is None:
        raise HTTPException(400, f"Invalid per-user file path: {filename!r}")

    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fp.write_text(content)
    except Exception as e:
        raise HTTPException(500, f"Failed to write file: {e}")

    audit_service.log_action(
        db, action="per_user_file_edit", admin_id=current_admin["id"],
        target_user=user_name, target_service=service_name,
        target_label=label,
        detail={"recipe_path": recipe_path, "filename": fp.name, "path": str(fp)},
        status="success",
    )
    return {
        "saved": True,
        "filename": fp.name,
        "path": str(fp),
        "size": len(content),
        "modified_at": fp.stat().st_mtime,
    }


@router.get("/{user_name}/{service_name}/{label}/registration-time")
async def get_registration_time(
    user_name: str, service_name: str, label: str,
    current_admin: dict = Depends(require_admin),
):
    """Get the service registration completion timestamp by finding the most
    recent successful 'register' task for this service instance."""
    try:
        # Query provision-api for all tasks, find the matching successful registration
        tasks_result = await provision_service.list_tasks()
        tasks = tasks_result.get("tasks", [])
        best_time = None
        for t in tasks:
            if t.get("type") != "register":
                continue
            if t.get("status") not in ("completed", "succeeded"):
                continue
            result = t.get("result") or {}
            if isinstance(result, str):
                import json
                try:
                    result = json.loads(result)
                except Exception:
                    pass
            t_user = result.get("user_name", "") if isinstance(result, dict) else ""
            t_svc = result.get("service_name", "") if isinstance(result, dict) else ""
            t_label = str(result.get("label", "0")) if isinstance(result, dict) else "0"
            if t_user == user_name and t_svc == service_name and t_label == str(label):
                updated = t.get("updated_at") or t.get("created_at")
                if updated and (best_time is None or updated > best_time):
                    best_time = updated
        return {"registration_time": best_time}
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")


@router.get("/{user_name}/{service_name}/{label}/volume-usage")
async def get_volume_usage(
    user_name: str,
    service_name: str,
    label: str,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(require_admin),
):
    """Get disk usage for a service instance's volume directories."""
    import os as _os
    import shutil as _shutil

    user_data_dir = settings.USER_DATA_DIR / user_name / service_name
    volumes: dict[str, dict[str, Any]] = {}

    if user_data_dir.exists():
        for vol_dir in user_data_dir.iterdir():
            if vol_dir.is_dir():
                try:
                    du = _shutil.disk_usage(str(vol_dir))
                    dir_size = 0
                    for dirpath, _dirnames, filenames in _os.walk(str(vol_dir)):
                        for f in filenames:
                            fp = _os.path.join(dirpath, f)
                            try:
                                dir_size += _os.path.getsize(fp)
                            except OSError:
                                pass
                    volumes[vol_dir.name] = {
                        "path": str(vol_dir),
                        "size_bytes": dir_size,
                        "disk_total_bytes": du.total,
                        "disk_used_bytes": du.used,
                        "disk_free_bytes": du.free,
                    }
                except OSError:
                    volumes[vol_dir.name] = {"path": str(vol_dir), "error": "Cannot read usage"}

    return {
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
        "user_data_dir": str(user_data_dir),
        "volumes": volumes,
    }
