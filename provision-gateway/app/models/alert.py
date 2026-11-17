"""gateway.alerts and gateway.grant_sync_state — the Alerts Center store."""

from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ..database import Base

KIND_EVENT = "event"
KIND_CONDITION = "condition"

STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = {"schema": "gateway"}

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Event dedupe identity (`hold:<task_id>`); NULL on a condition row.
    source_key = Column(Text, nullable=True, unique=True)
    kind = Column(Text, nullable=False)
    # Condition identity; NULL on an event row.
    condition_key = Column(Text, nullable=True, index=True)
    status = Column(Text, nullable=False, default=STATUS_ACTIVE)
    type = Column(Text, nullable=False)
    severity = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    task_id = Column(Text, nullable=True)
    service_id = Column(UUID(as_uuid=True), nullable=True)
    user_name = Column(Text, nullable=True)
    service_name = Column(Text, nullable=True)
    label = Column(Text, nullable=True)
    context = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    read_by = Column(UUID(as_uuid=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "source_key": self.source_key,
            "kind": self.kind,
            "condition_key": self.condition_key,
            "status": self.status,
            "type": self.type,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "task_id": self.task_id,
            "service_id": str(self.service_id) if self.service_id else None,
            "user_name": self.user_name,
            "service_name": self.service_name,
            "label": self.label,
            "context": self.context or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "read_by": str(self.read_by) if self.read_by else None,
            "read": self.read_at is not None,
        }


class GrantSyncState(Base):
    __tablename__ = "grant_sync_state"
    __table_args__ = {"schema": "gateway"}

    source_service_id = Column(UUID(as_uuid=True), primary_key=True)
    last_outcome = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    task_id = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
