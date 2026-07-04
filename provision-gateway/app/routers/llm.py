"""LLM router — /api/llm/* endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services.audit_service import log_action
from ..services.llm_service import llm_service

router = APIRouter(prefix="/api/llm", tags=["llm"])


@router.get("/config")
async def get_llm_config(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Get current LLM configuration."""
    return llm_service.get_config(db)


@router.put("/config")
async def update_llm_config(
    req: dict[str, Any],
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Update LLM configuration."""
    try:
        config = llm_service.save_config(db, req)
    except Exception as e:
        raise HTTPException(500, str(e))
    
    log_action(db, action="llm_config", admin_id=current_admin.id, status="success")
    return {"updated": True, "config": config.to_dict()}


@router.post("/test")
async def test_llm_connection(
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Generate a config file using LLM."""
    config_type = req.get("type")
    if config_type not in ("docker_compose", "nginx_conf", "env_file", "dockerfile"):
        raise HTTPException(400, f"Invalid type: {config_type}. Must be one of: docker_compose, nginx_conf, env_file, dockerfile")
    
    context = req.get("context", {})
    
    try:
        result = await llm_service.generate_config(db, config_type, context)
    except Exception as e:
        raise HTTPException(502, f"LLM generation failed: {e}")
    
    log_action(db, action="llm_generate", admin_id=current_admin.id,
               status="success", detail={"type": config_type})
    return result
