"""Tasks router — /api/tasks/* endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..config import settings
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """List all tasks from provision-api."""
    try:
        result = await provision_service.list_tasks()
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get a single task's status from provision-api."""
    try:
        result = await provision_service.get_task(task_id)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.delete("/{task_id}")
async def cancel_task(
    task_id: str,
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Cancel a pending/running task."""
    try:
        result = await provision_service.cancel_task(task_id)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.get("/{task_id}/log")
async def stream_task_log(
    task_id: str,
    tail: int = Query(200, description="Number of recent lines to send first"),
    follow: bool = Query(True, description="Whether to keep streaming new lines"),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Stream task build log via Server-Sent Events, filtered by task context."""
    log_file = settings.DOCKER_OPS_LOG
    
    # Try to get task context for filtering
    task_context: str | None = None
    try:
        task = await provision_service.get_task(task_id)
        result = task.get("result") or {}
        if isinstance(result, dict):
            user = result.get("user_name", "")
            svc = result.get("service_name", "")
            if user or svc:
                task_context = f"{user}/{svc}" if user and svc else (user or svc)
        else:
            ttype = task.get("type", "")
            task_context = ttype
    except Exception:
        pass

    def _line_matches(line: str) -> bool:
        """Check if a log line is relevant to this task."""
        if not task_context:
            return True  # No filter — show all
        # Check if line contains task context (user/service patterns)
        return task_context.lower() in line.lower()

    async def log_generator():
        if log_file.exists():
            try:
                lines = log_file.read_text().splitlines()
                # Filter lines relevant to this task, then take tail
                matched = [l for l in lines if _line_matches(l)]
                recent = matched[-tail:] if len(matched) > tail else matched
                for line in recent:
                    yield f"data: {line}\n\n"
            except Exception:
                pass

        if not follow:
            yield "event: done\ndata: {}\n\n"
            return

        last_size = log_file.stat().st_size if log_file.exists() else 0
        while True:
            await asyncio.sleep(1)
            try:
                if log_file.exists():
                    current_size = log_file.stat().st_size
                    if current_size > last_size:
                        with open(log_file, "r") as f:
                            f.seek(last_size)
                            new_data = f.read()
                            for line in new_data.splitlines():
                                if line.strip() and _line_matches(line):
                                    yield f"data: {line}\n\n"
                        last_size = current_size
            except Exception:
                break

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
