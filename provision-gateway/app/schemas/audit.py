"""Pydantic schemas for audit log endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AuditEntry(BaseModel):
    """Single audit log entry."""
    id: int
    admin_email: str | None = None
    action: str = ""
    target_user: str | None = None
    target_service: str | None = None
    target_label: str | None = None
    detail_json: str | None = None
    status: str = "success"
    ip_address: str | None = None
    created_at: str | None = None


class AuditListResponse(BaseModel):
    """Paginated audit log entries."""
    total: int = 0
    limit: int = 50
    offset: int = 0
    entries: list[dict[str, Any]] = Field(default_factory=list)
