"""Users router — /api/users/* endpoints (proxied to provision-api, enriched)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services import audit_service, curl_service
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def list_users(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """List all end-users from provision-api."""
    try:
        result = await provision_service.list_users()
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    users = result.get("user_status", [])
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


@router.put("/{user_name}/{service_name}/{label}/password")
async def change_user_password(
    user_name: str, service_name: str, label: str,
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Change a user's htpasswd password directly."""
    from pathlib import Path
    import subprocess
    import bcrypt as _bcrypt

    passwd = req.get("passwd", "")
    if not passwd:
        raise HTTPException(400, "passwd is required")

    generated_dir = Path("/srv/provision/generated")
    htpasswd_file = generated_dir / f"{user_name}.{service_name}.{label}.htpasswd"

    if not htpasswd_file.exists():
        raise HTTPException(404, f"htpasswd file not found: {htpasswd_file}")

    # Generate bcrypt hash in htpasswd-compatible format ($2b$ rounds)
    passwd_hash = _bcrypt.hashpw(passwd.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    htpasswd_file.write_text(f"{user_name}:{passwd_hash}\n")

    subprocess.run(
        ["docker", "exec", "provision-nginx", "nginx", "-s", "reload"],
        capture_output=True,
    )

    audit_service.log_action(
        db, action="password_change", admin_id=current_admin.id,
        target_user=user_name, target_service=service_name,
        target_label=label, status="success",
    )
    return {"message": "Password updated. Nginx reloaded."}


@router.get("/{user_name}/{service_name}/{label}/url")
async def get_service_url(
    user_name: str, service_name: str, label: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the URL for a user's service instance."""
    from ..config import settings
    from pathlib import Path
    import re

    generated_dir = Path("/srv/provision/generated")
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
