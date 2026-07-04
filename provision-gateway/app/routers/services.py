"""Services router — /api/services/* endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services.audit_service import log_action
from ..services.service_manager import service_manager
from ..services.llm_service import llm_service
from ..utils.file_scanner import scan_directory

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
async def list_services(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """List all service projects in source_projects."""
    services = service_manager.list_services()
    return {"services": services}


@router.get("/{name}")
async def get_service(
    name: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get details for a single service project."""
    svc = service_manager.get_service(name)
    if not svc:
        raise HTTPException(404, f"Service '{name}' not found")
    return svc


@router.post("", status_code=201)
async def create_service(
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Create a new service project.
    
    Modes:
    - git: clone from repo_url
    - upload: from file contents
    - template: from template_id (future)
    """
    mode = req.get("mode", "git")
    name = req.get("name")
    if not name:
        raise HTTPException(400, "'name' is required")
    
    try:
        if mode == "git":
            repo_url = req.get("repo_url")
            if not repo_url:
                raise HTTPException(400, "'repo_url' is required for git mode")
            branch = req.get("branch", "main")
            svc = service_manager.create_from_git(repo_url, branch, name)
        elif mode == "upload":
            files = req.get("files", {})
            svc = service_manager.create_from_upload(name, files)
        elif mode == "template":
            template_id = req.get("template_id")
            raise HTTPException(501, "Template mode not yet implemented")
        else:
            raise HTTPException(400, f"Unknown mode: {mode}")
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    
    log_action(db, action="service_create", admin_id=current_admin.id,
               target_service=name, status="success")
    return svc


@router.delete("/{name}")
async def delete_service(
    name: str,
    force: bool = Query(False),
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Delete a service project."""
    deleted = service_manager.delete_service(name)
    if not deleted:
        raise HTTPException(404, f"Service '{name}' not found")
    
    log_action(db, action="service_delete", admin_id=current_admin.id,
               target_service=name, status="success")
    return {"deleted": True}


@router.get("/{name}/files/{filename:path}")
async def get_service_file(
    name: str,
    filename: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Read a file from a service project."""
    content = service_manager.get_file(name, filename)
    if content is None:
        raise HTTPException(404, f"File '{filename}' not found in service '{name}'")
    return {"filename": filename, "content": content}


@router.put("/{name}/files/{filename:path}")
async def write_service_file(
    name: str,
    filename: str,
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Write or update a file in a service project."""
    content = req.get("content", "")
    ok = service_manager.write_file(name, filename, content)
    
    log_action(db, action="config_edit", admin_id=current_admin.id,
               target_service=name, status="success",
               detail={"filename": filename})
    return {"filename": filename, "written": ok}


@router.post("/{name}/convert")
async def convert_service_files(
    name: str,
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Convert plain compose/nginx files to Jinja2 templates."""
    result = {}
    
    compose_file = req.get("compose_file")
    if compose_file:
        try:
            converted = service_manager.convert_compose(name, compose_file)
            result.update(converted)
        except Exception as e:
            raise HTTPException(422, f"Compose conversion failed: {e}")
    
    nginx_file = req.get("nginx_file")
    if nginx_file:
        try:
            converted = service_manager.convert_nginx(name, nginx_file)
            result.update(converted)
        except Exception as e:
            raise HTTPException(422, f"Nginx conversion failed: {e}")
    
    log_action(db, action="config_edit", admin_id=current_admin.id,
               target_service=name, status="success",
               detail={"converted": list(result.keys())})
    return result


@router.post("/scan")
async def scan_repo(
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Scan a directory and return RepoContext for LLM generation."""
    directory = req.get("directory", "")
    if not directory:
        raise HTTPException(400, "'directory' is required")
    
    from pathlib import Path
    ctx = scan_directory(Path(directory))
    return {
        "repo_description": ctx.repo_description,
        "repo_files": ctx.repo_files,
        "port": ctx.port,
        "needs_db": ctx.needs_db,
        "needs_cache": ctx.needs_cache,
        "needs_volume": ctx.needs_volume,
        "language": ctx.language,
        "framework": ctx.framework,
        "has_dockerfile": ctx.has_dockerfile,
        "has_compose": ctx.has_compose,
        "has_nginx_conf": ctx.has_nginx_conf,
    }
