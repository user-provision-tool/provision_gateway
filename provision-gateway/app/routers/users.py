"""Users router — /api/users/* endpoints (proxied to provision-api, enriched)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..config import settings
from ..services import audit_service, curl_service
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def list_users(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List all end-users from provision-api, syncing missing users to gateway DB."""
    try:
        result = await provision_service.list_users()
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    users = result.get("user_status", [])

    # Sync: ensure all users from provision-api exist in gateway end_users DB
    from ..models.end_user import EndUser
    import bcrypt as _bcrypt
    import secrets

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

    return {"users": users, "count": len(users)}


@router.get("/{user_name}")
async def get_user(
    user_name: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get a single end-user's services from provision-api."""
    try:
        result = await provision_service.get_user(user_name)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.post("/deploy", status_code=202)
async def deploy_user(
    req: dict[str, Any],
    request: Request,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Deploy a service to a user (proxied to provision-api POST /users)."""
    # Inject global proxy into build_args if requested
    use_global_proxy = req.pop("use_global_proxy", False)
    if use_global_proxy:
        from ..services.proxy_service import inject_proxy_build_args, has_active_proxy
        if not has_active_proxy(db):
            raise HTTPException(400, "Global proxy is not enabled. Configure it in Settings first.")
        build_args = req.get("build_args") or {}
        req["build_args"] = inject_proxy_build_args(db, build_args, True)

    # Auto-register the user in gateway end_users if not already present
    user_name = req.get("user_name", "").strip()
    if user_name:
        from ..models.end_user import EndUser
        import bcrypt as _bcrypt
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

    try:
        result = await provision_service.register_user(**req)
    except Exception as e:
        audit_service.log_action(
            db,
            action="register",
            admin_id=current_admin.id,
            target_user=req.get("user_name"),
            target_service=req.get("service_name"),
            target_label=req.get("label", "0"),
            detail=req,
            status="failure",
            error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db,
        action="register",
        admin_id=current_admin.id,
        target_user=req.get("user_name"),
        target_service=req.get("service_name"),
        target_label=req.get("label", "0"),
        detail=req,
        status="success",
    )
    return result


@router.delete("/{user_name}/{service_name}/{label}")
async def remove_user_service(
    user_name: str,
    service_name: str,
    label: str,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Remove a user's service instance."""
    try:
        result = await provision_service.remove_user(user_name, service_name, label)
    except Exception as e:
        audit_service.log_action(
            db, action="remove", admin_id=current_admin.id,
            target_user=user_name, target_service=service_name,
            target_label=label, status="failure", error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db, action="remove", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.post("/{user_name}/{service_name}/{label}/rebuild")
async def rebuild_user_service(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any] = {},
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Rebuild a user's service instance."""
    try:
        result = await provision_service.rebuild_user(user_name, service_name, label, **req)
    except Exception as e:
        audit_service.log_action(
            db, action="rebuild", admin_id=current_admin.id,
            target_user=user_name, target_service=service_name,
            target_label=label, status="failure", error_message=str(e),
        )
        raise HTTPException(502, f"provision-api error: {e}")

    audit_service.log_action(
        db, action="rebuild", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.post("/{user_name}/{service_name}/{label}/up")
async def start_user_service(
    user_name: str, service_name: str, label: str,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Start a user's service (delegated to provision-api)."""
    try:
        result = await provision_service.start_user(user_name, service_name, label)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    audit_service.log_action(db, action="start", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name, target_label=label, status="success")
    return result


@router.post("/{user_name}/{service_name}/{label}/down")
async def stop_user_service(
    user_name: str, service_name: str, label: str,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Stop a user's service (delegated to provision-api)."""
    try:
        result = await provision_service.stop_user(user_name, service_name, label)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    audit_service.log_action(db, action="stop", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name, target_label=label, status="success")
    return result


@router.put("/{user_name}/{service_name}/{label}/password")
async def change_user_password(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
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
        db, action="password_change", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return result


@router.get("/{user_name}/{service_name}/{label}/containers/{container}/logs")
async def get_container_logs(
    user_name: str, service_name: str, label: str, container: str,
    tail: int = Query(100, ge=1, le=10000, description="Number of log lines to return"),
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the URL for a user's service instance."""
    from ..config import settings
    from pathlib import Path
    import re

    generated_dir = settings.PROVISION_DIR / "generated"
    nginx_conf = generated_dir / f"{user_name}.{service_name}.{label}.nginx.conf"

    server_name = f"{service_name}-{user_name}-{label}.example.com"
    https_enabled = False

    if nginx_conf.exists():
        content = nginx_conf.read_text()
        m = re.search(r"server_name\s+([^;]+);", content)
        if m:
            server_name = m.group(1).strip().split()[0]
        https_enabled = "listen 443 ssl" in content or "ssl_certificate" in content

    http_port = settings.NGINX_HTTP_PORT
    https_port = settings.NGINX_HTTPS_PORT

    return {
        "url": f"https://{server_name}" if https_enabled else f"http://{server_name}",
        "http_url": f"http://{server_name}",
        "https_enabled": https_enabled,
        "auth_enabled": True,
        "nginx_http_port": http_port,
        "nginx_https_port": https_port,
    }


@router.post("/{user_name}/{service_name}/{label}/test-curl")
async def test_curl(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any] = {},
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
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
            "compose_template_path": service_entry.get("compose_template_path"),
            "nginx_conf_template_path": service_entry.get("nginx_conf_template_path"),
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
        db, action="clone", admin_id=current_admin.id,
        target_user=target_user,
        detail={"source_user": source_user, "tasks": tasks},
        status="success",
    )
    return {"tasks": tasks, "total": len(tasks)}


# ---------------------------------------------------------------------------
# Deployment File Operations (read/write generated deployment files)
# ---------------------------------------------------------------------------

from pathlib import Path as _Path
import os as _os


def _resolve_deployment_file(
    user_name: str, service_name: str, label: str, file_type: str
) -> _Path | None:
    """Resolve a deployment file path from file_type identifier.
    
    file_type can be:
    - 'env' → .env.{user_name}.{label}
    - 'compose' → docker-compose.user-{user_name}.{label}.yml
    - 'nginx' → {service_name}.user-{user_name}.{label}.nginx.conf
    """
    source_dir = settings.SOURCE_PROJECTS_DIR / service_name
    generated_dir = settings.GENERATED_DIR

    if file_type == "env":
        return source_dir / f".env.{user_name}.{label}"
    elif file_type == "compose":
        return source_dir / f"docker-compose.user-{user_name}.{label}.yml"
    elif file_type == "nginx":
        # Try common naming patterns for nginx conf
        candidates = [
            generated_dir / f"{service_name}.user-{user_name}.{label}.nginx.conf",
            generated_dir / f"{user_name}.{service_name}.{label}.nginx.conf",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]  # Return primary candidate even if not exists (for write)
    return None


@router.get("/{user_name}/{service_name}/{label}/deployment-files")
async def list_deployment_files(
    user_name: str, service_name: str, label: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """List deployment files for a service instance (paths, sizes, modification times)."""
    files = []
    for ft in ["env", "compose", "nginx"]:
        fp = _resolve_deployment_file(user_name, service_name, label, ft)
        if fp:
            info = {
                "file_type": ft,
                "path": str(fp),
                "filename": fp.name,
                "exists": fp.exists(),
            }
            if fp.exists():
                stat = fp.stat()
                info["size"] = stat.st_size
                info["modified_at"] = stat.st_mtime
            files.append(info)
    return {"files": files}


@router.get("/{user_name}/{service_name}/{label}/deployment-files/{file_type}")
async def get_deployment_file(
    user_name: str, service_name: str, label: str, file_type: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the content of a deployment file."""
    fp = _resolve_deployment_file(user_name, service_name, label, file_type)
    if not fp:
        raise HTTPException(400, f"Unknown file type: {file_type}")
    if not fp.exists():
        raise HTTPException(404, f"File not found: {fp}")
    try:
        content = fp.read_text()
    except Exception as e:
        raise HTTPException(500, f"Failed to read file: {e}")
    return {
        "file_type": file_type,
        "path": str(fp),
        "filename": fp.name,
        "content": content,
        "size": len(content),
        "modified_at": fp.stat().st_mtime,
    }


@router.put("/{user_name}/{service_name}/{label}/deployment-files/{file_type}")
async def save_deployment_file(
    user_name: str, service_name: str, label: str, file_type: str,
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Save/update a deployment file's content."""
    content = req.get("content", "")
    fp = _resolve_deployment_file(user_name, service_name, label, file_type)
    if not fp:
        raise HTTPException(400, f"Unknown file type: {file_type}")

    # Ensure parent directory exists
    fp.parent.mkdir(parents=True, exist_ok=True)

    try:
        fp.write_text(content)
    except Exception as e:
        raise HTTPException(500, f"Failed to write file: {e}")

    audit_service.log_action(
        db, action="deployment_file_edit", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name,
        target_label=label,
        detail={"file_type": file_type, "path": str(fp)},
        status="success",
    )
    return {
        "saved": True,
        "file_type": file_type,
        "path": str(fp),
        "size": len(content),
        "modified_at": fp.stat().st_mtime,
    }


@router.get("/{user_name}/{service_name}/{label}/registration-time")
async def get_registration_time(
    user_name: str, service_name: str, label: str,
    current_admin: AdminUser = Depends(get_current_admin),
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
