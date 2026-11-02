"""gateway.tasks — the gateway's generation/analysis task family (T7)."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import synonym

from ..database import Base

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AWAITING_INPUT = "awaiting_input"  # reserved/import-only
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

TERMINAL = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

ORIGIN_FIX = "fix"
ORIGIN_DEPLOY = "deploy"
ORIGIN_STANDALONE = "standalone"


class GatewayTask(Base):
    __tablename__ = "tasks"
    __table_args__ = {"schema": "gateway"}

    task_id = Column(Text, primary_key=True)
    # The generation engine still speaks of a job "id"; the task id IS that id.
    id = synonym("task_id")
    collision_token = Column(Text, nullable=False, unique=True)
    procedure_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    service_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    executor = Column(Text, nullable=False, default="gateway")
    kind = Column(Text, nullable=False)  # generate | analyze
    operation = Column(Text, nullable=False)
    # The old column name was `job_type`; same value as `operation`.
    job_type = synonym("operation")
    origin = Column(Text, nullable=False)  # deploy | fix | standalone
    status = Column(Text, nullable=False)
    phase = Column(Text, nullable=True)
    phase_index = Column(Integer, nullable=True)
    phase_total = Column(Integer, nullable=True)
    progress = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    log_ref = Column(Text, nullable=True)
    usage = Column(JSONB, nullable=True)
    request = Column(JSONB, nullable=True)
    result = Column(JSONB, nullable=True)
    target_snapshot = Column(JSONB, nullable=True)
    events = Column(JSONB, nullable=True)
    pending_input = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def service_name(self) -> str | None:
        return (self.target_snapshot or {}).get("service_name")

    @service_name.setter
    def service_name(self, value):
        snap = dict(self.target_snapshot or {})
        snap["service_name"] = value
        self.target_snapshot = snap

    @property
    def recipe_path(self) -> str:
        return (self.target_snapshot or {}).get("recipe_path") or ""

    @recipe_path.setter
    def recipe_path(self, value):
        snap = dict(self.target_snapshot or {})
        snap["recipe_path"] = value or ""
        self.target_snapshot = snap

    def to_dict(self, include_result: bool = False) -> dict:
        data = {
            "id": self.task_id,
            "task_id": self.task_id,
            "collision_token": self.collision_token,
            "procedure_id": str(self.procedure_id),
            "service_id": str(self.service_id) if self.service_id else None,
            "executor": self.executor,
            "kind": self.kind,
            "operation": self.operation,
            "job_type": self.operation,
            "origin": self.origin,
            "status": self.status,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "phase_total": self.phase_total,
            "progress": self.progress,
            "error": self.error,
            "usage": self.usage,
            "events": self.events,
            "pending_input": self.pending_input,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_result:
            data["result"] = self.result
        return data


def job_origin(request: dict | str | None, job_type: str | None) -> str:
    """The path that created a job — derived from what the job records."""
    import json
    if (job_type or "") == "fix":
        return ORIGIN_FIX
    if isinstance(request, str):
        try:
            request = json.loads(request or "{}")
        except (TypeError, ValueError):
            request = {}
    req = request if isinstance(request, dict) else {}
    return ORIGIN_DEPLOY if req.get("user_name") else ORIGIN_STANDALONE
