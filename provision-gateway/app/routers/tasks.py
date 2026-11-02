"""Unified task read model — one list, both families (T7/R4.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..middleware import require_admin
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

TERMINAL = ("completed", "failed", "cancelled")


def _row(db: Session, task_id: str) -> dict | None:
    row = db.execute(
        text("SELECT * FROM gateway.task_view WHERE task_id=:id"), {"id": task_id}
    ).mappings().first()
    return dict(row) if row else None


def _clean(row: dict) -> dict:
    row = dict(row)
    for key in ("procedure_id", "service_id"):
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row


@router.get("")
async def list_tasks(current_admin: dict = Depends(require_admin)):
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT * FROM gateway.task_view WHERE deleted_at IS NULL "
                "ORDER BY created_at DESC, task_id DESC"
            )
        ).mappings().all()
    finally:
        db.close()
    tasks = [_clean(dict(r)) for r in rows]
    for t in tasks:
        t["is_record"] = False
    return {"count": len(tasks), "tasks": tasks}


@router.get("/{task_id}")
async def get_task(task_id: str, current_admin: dict = Depends(require_admin)):
    db = SessionLocal()
    try:
        row = _row(db, task_id)
    finally:
        db.close()
    if row is None:
        raise HTTPException(404, "Task not found")
    return _clean(row)


@router.delete("/{task_id}")
async def cancel_task(task_id: str, current_admin: dict = Depends(require_admin)):
    """Prefix dispatch: `gt-` gateway cancel, `api-` proxied to the -api."""
    if task_id.startswith("api-"):
        try:
            return await provision_service.cancel_task(task_id)
        except Exception as e:  # noqa: BLE001
            raise provision_service.as_task_error(e)
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT status, operation FROM gateway.tasks WHERE task_id=:id"),
            {"id": task_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(404, "Task not found")
        if row["operation"] == "fix_remove":
            raise HTTPException(409, "fix_remove is not cancellable")
        if row["status"] in TERMINAL or row["status"] == "unknown":
            raise HTTPException(409, f"Task is already terminal: {row['status']}")
        if row["status"] not in ("queued", "running", "paused", "awaiting_input"):
            raise HTTPException(409, f"Task cannot be cancelled: {row['status']}")
        db.execute(
            text("UPDATE gateway.tasks SET status='cancelled', updated_at=now() "
                 "WHERE task_id=:id AND status IN ('queued','running','paused','awaiting_input')"),
            {"id": task_id},
        )
        db.commit()
    finally:
        db.close()
    # Session rules: a live generation cancel removes its session; an
    # operator-held paused/refused session is kept.
    from ..services import generation_jobs as _gj
    _gj.generation_jobs.cancel_session_after_task_cancel(task_id)
    return {"task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/answer")
async def answer_task(task_id: str, body: dict,
                      current_admin: dict = Depends(require_admin)):
    from ..services.generation_jobs import generation_jobs
    try:
        return generation_jobs.answer_pause(task_id, body or {})
    except KeyError:
        raise HTTPException(404, "Task not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/{task_id}/log")
async def stream_task_log(task_id: str, request: Request,
                          tail: int = Query(200), follow: bool = Query(True),
                          token: str = Query("")):
    require_admin(request)
    if task_id.startswith("api-"):
        try:
            task = await provision_service.get_task(task_id)
        except Exception as e:  # noqa: BLE001
            raise provision_service.as_task_error(e)
        if task.get("deleted_at"):
            raise HTTPException(410, "Task was pruned; its log is gone")
        async def proxy():
            try:
                async for line in provision_service.stream_task_log(task_id, tail, follow):
                    yield line
            except Exception as exc:  # noqa: BLE001
                import json as _json
                yield f"event: error\ndata: {_json.dumps({'error': str(exc)})}\n\n"
        return StreamingResponse(proxy(), media_type="text/event-stream")

    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT log_ref, deleted_at FROM gateway.tasks WHERE task_id=:id"),
            {"id": task_id},
        ).mappings().first()
    finally:
        db.close()
    if row is None:
        raise HTTPException(404, "Task not found")
    if row["deleted_at"] is not None:
        raise HTTPException(410, "Task was pruned; its log is gone")
    if not row["log_ref"] or not Path(row["log_ref"]).exists():
        raise HTTPException(404, "Task log is gone")
    log_file = Path(row["log_ref"])

    async def file_sse():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-tail:]:
            yield f"data: {line}\n\n"
        if not follow:
            yield "event: done\ndata: {}\n\n"
            return
        pos = log_file.stat().st_size
        while True:
            await asyncio.sleep(1)
            if log_file.exists() and log_file.stat().st_size > pos:
                with log_file.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    for line in fh.read().splitlines():
                        yield f"data: {line}\n\n"
                    pos = fh.tell()
    return StreamingResponse(file_sse(), media_type="text/event-stream")
