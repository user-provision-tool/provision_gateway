"""Pre-deploy session endpoints, keyed by service_id (T8)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from ..database import SessionLocal
from ..middleware import require_admin
from ..services import pre_deploy_registry as pdr

router = APIRouter(prefix="/api/pre-deploy", tags=["pre-deploy"])

LIVE_TASK = ("queued", "running", "unknown")


def _task_status(task_id: str | None) -> str | None:
    if not task_id:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT status FROM gateway.tasks WHERE task_id=:id"), {"id": task_id}
        ).first()
        return row[0] if row else None
    finally:
        db.close()


def _services_row(service_id: str):
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT id, deleted_at FROM registry.services WHERE id=:id"),
            {"id": service_id},
        ).mappings().first()
    finally:
        db.close()


@router.get("")
async def list_pre_deploy(current_admin: dict = Depends(require_admin)):
    from ..services.generation_jobs import generation_jobs
    sessions = pdr.list_sessions()
    db = SessionLocal()
    try:
        analyses = generation_jobs.list_fix_analyses(db)
        for row in sessions:
            st = _task_status(row.get("generation_task_id"))
            row["job_live"] = st in LIVE_TASK
    except Exception:
        analyses = []
    finally:
        db.close()
    return {"sessions": sessions, "analyses": analyses, "count": len(sessions)}


@router.get("/{service_id}")
async def get_pre_deploy(service_id: str,
                         current_admin: dict = Depends(require_admin)):
    return pdr.get_session(service_id) or {}


@router.get("/{user}/{service}/{label}")
async def get_pre_deploy_by_names(user: str, service: str, label: str,
                                  current_admin: dict = Depends(require_admin)):
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT id FROM registry.api_users WHERE username=:u"), {"u": user}
        ).first()
        owner = str(row[0]) if row else None
    finally:
        db.close()
    if owner is None:
        return {}
    return pdr.find_by_tuple(owner, service, str(label)) or {}


@router.delete("/{service_id}")
async def delete_pre_deploy(service_id: str,
                            current_admin: dict = Depends(require_admin)):
    """Record-only delete; 409 while a live services row exists.

    A paused fix card is a full service delete (`DELETE
    /api/services/instances/{id}?scope=service`) followed by session removal.
    """
    row = pdr.get_session(service_id)
    if row is None:
        raise HTTPException(404, "no pre-deploy session for that service")
    svc = _services_row(service_id)
    if svc is not None and svc["deleted_at"] is None:
        raise HTTPException(409, "a live service exists; delete the service, not the record")
    cancelled = False
    task_id = row.get("generation_task_id")
    if task_id and _task_status(task_id) in ("queued", "running", "paused", "awaiting_input"):
        from ..services.generation_jobs import generation_jobs
        db = SessionLocal()
        try:
            generation_jobs.cancel_job(db, task_id)
            cancelled = True
        finally:
            db.close()
    pdr.remove_session(service_id)
    return {"deleted": True, "cancelled_job": cancelled}


@router.post("/{service_id}/retry")
async def retry_pre_deploy(service_id: str,
                           current_admin: dict = Depends(require_admin)):
    """Retry a refusal: generation-capacity re-runs generation, deploy-capacity
    replays the stored deployment (R2/R5)."""
    from ..services import deploy_workflow as _wf
    try:
        return await _wf.retry(service_id)
    except _wf.WorkflowError as exc:
        raise HTTPException(exc.status, {"error": exc.code, "detail": exc.detail})
