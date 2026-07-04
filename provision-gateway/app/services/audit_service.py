"""Audit service — write and query audit log entries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models.admin import AdminUser
from ..models.audit_log import AuditLog


def log_action(
    db: Session,
    action: str,
    admin_id: int | None = None,
    target_user: str | None = None,
    target_service: str | None = None,
    target_label: str | None = None,
    detail: dict[str, Any] | None = None,
    status: str = "success",
    error_message: str | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Record an audit log entry."""
    entry = AuditLog(
        admin_id=admin_id,
        action=action,
        target_user=target_user,
        target_service=target_service,
        target_label=target_label,
        detail_json=json.dumps(detail) if detail else None,
        status=status,
        error_message=error_message,
        ip_address=ip_address,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def query_logs(
    db: Session,
    admin_id: int | None = None,
    action: str | None = None,
    target_user: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Query audit logs with optional filters. Returns (entries, total)."""
    q = db.query(AuditLog)

    if admin_id is not None:
        q = q.filter(AuditLog.admin_id == admin_id)
    if action:
        q = q.filter(AuditLog.action == action)
    if target_user:
        q = q.filter(AuditLog.target_user == target_user)
    if from_date:
        q = q.filter(AuditLog.created_at >= from_date)
    if to_date:
        q = q.filter(AuditLog.created_at <= to_date)

    total = q.count()
    entries = (
        q.order_by(desc(AuditLog.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Resolve admin emails
    admin_ids = {e.admin_id for e in entries if e.admin_id}
    admin_map: dict[int, str] = {}
    if admin_ids:
        admins = db.query(AdminUser).filter(AdminUser.id.in_(admin_ids)).all()
        admin_map = {a.id: a.email for a in admins}

    return [
        e.to_dict(admin_email=admin_map.get(e.admin_id))
        for e in entries
    ], total
