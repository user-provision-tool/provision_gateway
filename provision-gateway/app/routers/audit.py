"""Audit router — /api/audit/* endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services.audit_service import query_logs

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit_logs(
    admin_id: int | None = Query(None),
    action: str | None = Query(None),
    user: str | None = Query(None, alias="target_user"),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Query audit logs with optional filters."""
    entries, total = query_logs(
        db,
        admin_id=admin_id,
        action=action,
        target_user=user,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "limit": limit, "offset": offset, "entries": entries}
