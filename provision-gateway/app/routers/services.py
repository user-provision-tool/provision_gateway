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
from ..config import settings
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
            use_proxy = req.get("use_proxy", False)
            svc = service_manager.create_from_git(
                repo_url, branch, name, use_proxy=use_proxy, db_session=db,
            )
        elif mode == "upload":
            zip_b64 = req.get("zip_content", "")
            if zip_b64:
                import base64
                svc = service_manager.create_from_zip(name, base64.b64decode(zip_b64))
            else:
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


@router.post("/check-deploy")
async def check_deploy_readiness(
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Check if a service project has all files needed for deployment.
    
    Returns missing files and auto-generates them via LLM if configured.
    User must confirm before generated files are saved.
    """
    service_name = req.get("service_name")
    if not service_name:
        raise HTTPException(400, "'service_name' is required")
    
    info = service_manager._get_service_info(service_manager._source_dir / service_name)
    files = info.get("files", [])
    
    needed = {
        "compose_template": any(f.endswith(".yml.j2") or f.endswith(".yaml.j2") for f in files),
        "compose_file": any(f.endswith(".yml") or f.endswith(".yaml") for f in files if not f.endswith(".j2")),
        "nginx_template": any(f.endswith(".conf.j2") for f in files),
        "nginx_file": any(f.endswith(".conf") for f in files if not f.endswith(".j2")),
    }
    
    # A compose source is required (either template or plain file)
    has_compose = needed["compose_template"] or needed["compose_file"]
    has_nginx = needed["nginx_template"] or needed["nginx_file"]
    
    missing = []
    if not has_compose:
        missing.append("docker-compose.yml (or .yml.j2 template)")
    if not has_nginx:
        missing.append("nginx.conf (or .conf.j2 template)")
    
    generated = {}
    warnings = []
    
    # Auto-generate missing files via LLM if active config exists
    if missing and has_nginx is False:
        try:
            # Try to generate nginx conf
            scan_info = {
                "repo_description": f"Service: {service_name}",
                "repo_files": files[:10],
                "port": 80,
                "needs_db": False,
            }
            gen_result = await llm_service.generate_config(db, "nginx_conf", scan_info)
            if gen_result.get("generated_content"):
                generated["nginx.conf"] = gen_result["generated_content"]
                if gen_result.get("warnings"):
                    warnings.extend(gen_result["warnings"])
        except Exception:
            pass
    
    if missing and has_compose is False:
        try:
            scan_info = {
                "repo_description": f"Service: {service_name}",
                "repo_files": files[:10],
                "port": 80,
                "needs_db": False,
            }
            gen_result = await llm_service.generate_config(db, "docker_compose", scan_info)
            if gen_result.get("generated_content"):
                generated["docker-compose.yml"] = gen_result["generated_content"]
                if gen_result.get("warnings"):
                    warnings.extend(gen_result["warnings"])
        except Exception:
            pass
    
    return {
        "service": service_name,
        "ready": len(missing) == 0,
        "has_compose": has_compose,
        "has_nginx": has_nginx,
        "missing": missing,
        "generated": generated,
        "warnings": warnings,
        "needs_confirmation": len(generated) > 0,
    }


@router.post("/save-generated")
async def save_generated_files(
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Save LLM-generated files to the service project.
    
    Body: { "service_name": "myapp", "files": { "nginx.conf": "...", "docker-compose.yml": "..." } }
    """
    service_name = req.get("service_name")
    files = req.get("files", {})
    if not service_name or not files:
        raise HTTPException(400, "service_name and files required")
    
    target = settings.SOURCE_PROJECTS_DIR / service_name
    if not target.exists():
        raise HTTPException(404, f"Service '{service_name}' not found")
    
    saved = []
    for filename, content in files.items():
        filepath = target / filename
        filepath.write_text(content)
        saved.append(filename)
    
    # Mark as generated (add .generated suffix for tracking)
    for filename in saved:
        marker = target / f"{filename}.generated"
        marker.write_text("")
    
    log_action(db, action="llm_generated_files", admin_id=current_admin.id,
               target_service=service_name, status="success",
               detail={"files": saved})
    
    return {"saved": saved, "service": service_name}


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


# ------------------------------------------------------------------
# Git integration — real git status / git diff for change tracking
# ------------------------------------------------------------------

import subprocess
from pathlib import Path


def _git_command(service_name: str, *args: str) -> str:
    """Run a git command in the service project directory and return stdout."""
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        raise HTTPException(404, f"Service '{service_name}' not found")
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Git command timed out")
    except FileNotFoundError:
        raise HTTPException(500, "git CLI not available")


@router.get("/{name}/git/status")
async def git_status(
    name: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get git status for a service project (git status --porcelain)."""
    try:
        output = _git_command(name, "status", "--porcelain")
    except HTTPException:
        raise
    lines = [l for l in output.split("\n") if l.strip()]
    # Parse: " M file" or "?? file" format
    modified = []
    untracked = []
    for line in lines:
        if len(line) < 3:
            continue
        status = line[:2].strip()
        filename = line[3:].strip()
        if status in ("M", "A", "D", "R"):
            modified.append({"status": status, "file": filename})
        elif status == "??":
            untracked.append({"status": "?", "file": filename})
    return {"modified": modified, "untracked": untracked, "raw": lines}


@router.get("/{name}/git/diff")
async def git_diff(
    name: str,
    file: str = Query(None, description="Specific file to diff (relative path)"),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get git diff for a service project (working tree vs HEAD)."""
    try:
        args = ["diff"]
        if file:
            args.extend(["--", file])
        output = _git_command(name, *args)
    except HTTPException:
        raise
    return {"diff": output}


@router.get("/{name}/git/head-file")
async def git_head_file(
    name: str,
    file: str = Query(..., description="File path relative to project root"),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get file content from HEAD revision (git show HEAD:file)."""
    try:
        content = _git_command(name, "show", f"HEAD:{file}")
    except HTTPException:
        raise
    return {"content": content, "file": file}
