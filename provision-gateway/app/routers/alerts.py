"""Alerts Center API — admin-only list, count and read state (R2/R6/R7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware import require_admin
from ..services import alert_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class IdsBody(BaseModel):
    ids: list[str] = []


@router.get("")
async def list_alerts(
    q: str | None = Query(None),
    type: str | None = Query(None),
    kind: str | None = Query(None),
    status: str | None = Query(None),
    read: str | None = Query(None),
    limit: int = Query(50, ge=0, le=200),
    offset: int = Query(0, ge=0),
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Newest first; `unread` is the unread-active count, independent of filters."""
    await alert_service.refresh(db)
    return alert_service.list_alerts(
        db, q=q, type=type, kind=kind, status=status, read=read,
        limit=limit, offset=offset,
    )


@router.get("/count")
async def counts(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await alert_service.refresh(db)
    return alert_service.counts(db)


@router.post("/read")
async def mark_read(
    body: IdsBody,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return alert_service.mark(db, body.ids, read=True,
                              actor_id=current_admin.get("id"))


@router.post("/unread")
async def mark_unread(
    body: IdsBody,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return alert_service.mark(db, body.ids, read=False,
                              actor_id=current_admin.get("id"))


@router.post("/read-all")
async def mark_all_read(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return alert_service.read_all(db, current_admin.get("id"))
