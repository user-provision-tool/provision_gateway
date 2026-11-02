"""Audit log — users.id actor and ID targets (T6)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ..database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "gateway"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=True)
    action = Column(Text, nullable=False)
    target_api_user_id = Column(UUID(as_uuid=True), nullable=True)
    target_service_id = Column(UUID(as_uuid=True), nullable=True)
    target_user = Column(Text, nullable=True)
    target_service = Column(Text, nullable=True)
    target_label = Column(Text, nullable=True)
    detail_json = Column(JSONB, nullable=True)
    status = Column(Text, nullable=False, default="success")
    error_message = Column(Text, nullable=True)
    ip_address = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, actor_username: str | None = None) -> dict:
        return {
            "id": self.id,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "actor_username": actor_username,
            "action": self.action,
            "target_api_user_id": str(self.target_api_user_id) if self.target_api_user_id else None,
            "target_service_id": str(self.target_service_id) if self.target_service_id else None,
            "target_user": self.target_user,
            "target_service": self.target_service,
            "target_label": self.target_label,
            "detail_json": self.detail_json,
            "status": self.status,
            "error_message": self.error_message,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
