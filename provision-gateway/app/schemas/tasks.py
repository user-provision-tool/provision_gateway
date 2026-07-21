"""Pydantic schemas for task endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    """Individual task as returned by the API."""
    task_id: str = ""
    type: str = ""
    status: str = "pending"
    target_user: str | None = None
    target_service: str | None = None
    target_label: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None


class TaskListResponse(BaseModel):
    """List of tasks."""
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0


class TaskDetailResponse(BaseModel):
    """Single task detail."""
    task_id: str = ""
    type: str = ""
    status: str = "pending"
    target_user: str | None = None
    target_service: str | None = None
    target_label: str | None = None
    result: Any = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    elapsed_sec: float | None = None


class TaskCancelResponse(BaseModel):
    """Response after cancelling a task."""
    cancelled: bool = True
    task_id: str = ""
