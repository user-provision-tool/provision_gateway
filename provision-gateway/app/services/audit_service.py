"""Audit log — users.id actor, ID targets, redacted detail (T6/R5.9)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models.audit_log import AuditLog
from ..models.user_login import UserLogin

_SECRET_KEYS = {"passwd", "passwd_plain", "password", "privkey", "api_key", "token",
                "resume_values"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def log_action(
    db: Session,
    action: str,
    actor_user_id=None,
    target_api_user_id=None,
    target_service_id=None,
    target_user: str | None = None,
    target_service: str | None = None,
    target_label: str | None = None,
    detail: dict[str, Any] | None = None,
    status: str = "success",
    error_message: str | None = None,
    ip_address: str | None = None,
    **legacy,
) -> AuditLog:
    # `admin_id` was the old parameter name; accept it so no caller silently
    # writes an un-attributed row.
    if actor_user_id is None and legacy.get("admin_id") is not None:
        actor_user_id = legacy.get("admin_id")
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_api_user_id=target_api_user_id,
        target_service_id=target_service_id,
        target_user=target_user,
        target_service=target_service,
        target_label=target_label,
        detail_json=_redact(detail) if detail else None,
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
    actor_user_id=None,
    action: str | None = None,
    target_user: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    q = db.query(AuditLog)
    if actor_user_id is not None:
        q = q.filter(AuditLog.actor_user_id == actor_user_id)
    if action:
        q = q.filter(AuditLog.action == action)
    if target_user:
        q = q.filter(AuditLog.target_user == target_user)
    if from_date:
        q = q.filter(AuditLog.created_at >= from_date)
    if to_date:
        q = q.filter(AuditLog.created_at <= to_date)
    total = q.count()
    entries = q.order_by(desc(AuditLog.created_at)).offset(offset).limit(limit).all()
    ids = {e.actor_user_id for e in entries if e.actor_user_id}
    names: dict[str, str] = {}
    if ids:
        for row in db.query(UserLogin).filter(
            UserLogin.user_id.in_(ids), UserLogin.kind == "username"
        ):
            names[str(row.user_id)] = row.login
    return [e.to_dict(actor_username=names.get(str(e.actor_user_id))) for e in entries], total
