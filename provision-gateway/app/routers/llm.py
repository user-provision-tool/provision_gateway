"""LLM router — /api/llm/* endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import require_admin
from ..services.audit_service import log_action
from ..services.generation_jobs import GenerationJobError, generation_jobs
from ..services.llm_service import llm_service

router = APIRouter(prefix="/api/llm", tags=["llm"])


# ---------------------------------------------------------------------------
# Multi-config management
# ---------------------------------------------------------------------------

@router.get("/configs")
async def list_llm_configs(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all LLM configs."""
    configs = llm_service.list_configs(db)
    active = llm_service.get_config(db)
    return {"configs": configs, "active": active}


@router.post("/configs")
async def create_llm_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new LLM config."""
    config = llm_service.create_config(db, req)
    log_action(db, action="llm_config_create", admin_id=current_admin["id"], status="success")
    return {"config": config.to_dict()}


@router.put("/configs/{config_id}/activate")
async def activate_llm_config(
    config_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Activate an LLM config."""
    config = llm_service.activate_config(db, config_id)
    if not config:
        raise HTTPException(404, "Config not found")
    log_action(db, action="llm_config_activate", admin_id=current_admin["id"], status="success")
    return {"activated": True, "config": config.to_dict()}


@router.delete("/configs/{config_id}")
async def delete_llm_config(
    config_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete an LLM config."""
    if not llm_service.delete_config(db, config_id):
        raise HTTPException(404, "Config not found")
    return {"deleted": True}


@router.get("/config")
async def get_llm_config(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get current LLM configuration."""
    return llm_service.get_config(db)


@router.put("/config")
async def update_llm_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update LLM configuration."""
    try:
        config = llm_service.save_config(db, req)
    except Exception as e:
        raise HTTPException(500, str(e))
    
    log_action(db, action="llm_config", admin_id=current_admin["id"], status="success")
    return {"updated": True, "config": config.to_dict()}


@router.post("/test")
async def test_llm_connection(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Test the LLM connection."""
    try:
        result = await llm_service.test_connection(db)
    except Exception as e:
        raise HTTPException(502, f"LLM test failed: {e}")
    return result


@router.post("/generate")
async def generate_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generate a config file using LLM (legacy single-shot path).

    New callers should use the async job endpoints (POST /api/llm/jobs) —
    the agent-with-tools generation with validation self-repair runs there.
    """
    config_type = req.get("type")
    if config_type not in ("docker_compose", "nginx_conf", "env_file", "dockerfile"):
        raise HTTPException(400, f"Invalid type: {config_type}. Must be one of: docker_compose, nginx_conf, env_file, dockerfile")

    context = req.get("context", {})

    try:
        result = await llm_service.generate_config(db, config_type, context)
    except Exception as e:
        raise HTTPException(502, f"LLM generation failed: {e}")

    log_action(db, action="llm_generate", admin_id=current_admin["id"],
               status="success", detail={"type": config_type})
    return result


# ---------------------------------------------------------------------------
# Async generation jobs (file-selection-and-generation design §Generation)
# ---------------------------------------------------------------------------

@router.post("/jobs", status_code=202)
async def create_generation_job(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create an async LLM generation job.

    Body::

        {
          "service_name": "dify",
          "recipe_path": "docker",           # "" = root recipe
          "job_type": "generate_missing",    # 'generate_missing' | 'per_user_env' | single category
          "category": "compose",             # for single-category jobs
          "prompt": "...",                   # mandatory for empty recipes
          "selection": {"compose": [...], "nginx": ..., "env": [...], "profiles": [...]},
          "deploy_metadata": {"user_name": ..., "label": ..., "domain": ...},
          "user_name": "...", "label": "...", "domain": "..."   # per_user_env target
        }

    ``generate_missing`` runs the ordered phases compose → nginx → env as ONE
    job (progress per phase); ``per_user_env`` writes the per-user env file
    only (stored default untouched).
    """
    service_name = req.get("service_name")
    if not service_name:
        raise HTTPException(400, "'service_name' is required")
    job_type = req.get("job_type") or "generate_missing"
    try:
        job = generation_jobs.create_job(
            db, service_name, req.get("recipe_path") or "", job_type, req
        )
    except GenerationJobError as e:
        raise HTTPException(400, str(e))

    log_action(db, action="llm_generation_job", admin_id=current_admin["id"],
               target_service=service_name, status="created",
               detail={"job_id": job.id, "job_type": job_type})
    return {"job_id": job.id, "status": job.status, "job_type": job_type}


@router.get("/jobs")
async def list_generation_jobs(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List generation jobs (oldest TTL-pruned; newest 200 returned)."""
    return {"jobs": generation_jobs.list_jobs(db)}


@router.get("/jobs/{job_id}")
async def get_generation_job(
    job_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Poll one generation job (progress + result when completed)."""
    job = generation_jobs.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict(include_result=True)


@router.delete("/jobs/{job_id}")
async def cancel_generation_job(
    job_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Cancel a running/queued generation job."""
    job = generation_jobs.cancel_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    log_action(db, action="llm_generation_job_cancel", admin_id=current_admin["id"],
               target_service=job.service_name, status="cancelled",
               detail={"job_id": job.id})
    return {"job_id": job.id, "status": job.status}
