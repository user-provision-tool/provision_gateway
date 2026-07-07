"""Tasks router — /api/tasks/* endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services.auth_service import decode_access_token, get_admin_by_id
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
    request: Request,
    tail: int = Query(200, description="Number of recent lines to send first"),
    follow: bool = Query(True, description="Whether to keep streaming new lines"),
    token: str = Query("", description="JWT token for EventSource (query param fallback)"),
    db: Session = Depends(get_db),
):
    """Stream task build log via Server-Sent Events (proxied to provision-api).

    Authentication: accepts JWT via ``Authorization: Bearer`` header,
    OR via ``?token=`` query parameter (for EventSource which cannot set headers).
    """
    # Authenticate: try Authorization header first, then query param token
    admin = None
    auth_header = request.headers.get("Authorization", "")
    actual_token = ""

    if auth_header.startswith("Bearer "):
        actual_token = auth_header[7:]
    elif token:
        actual_token = token

    if actual_token:
        try:
            payload = decode_access_token(actual_token)
            admin_id = int(payload.get("sub", 0))
            admin = get_admin_by_id(db, admin_id)
        except Exception:
            pass

    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")

    async def sse_generator():
        try:
            async for line in provision_service.stream_task_log(task_id, tail, follow):
                yield line
        except Exception as e:
            yield f"event: error\ndata: {str(e)}\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
