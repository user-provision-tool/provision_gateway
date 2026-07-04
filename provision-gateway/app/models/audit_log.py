"""AuditLog ORM model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import relationship

from ..database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(Integer, nullable=True)
    action = Column(String(100), nullable=False)
    target_user = Column(String(255), nullable=True)
    target_service = Column(String(255), nullable=True)
    target_label = Column(String(50), nullable=True)
    detail_json = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="success")
    error_message = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self, admin_email: str | None = None) -> dict:
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "admin_email": admin_email,
            "action": self.action,
            "target_user": self.target_user,
            "target_service": self.target_service,
            "target_label": self.target_label,
            "detail_json": self.detail_json,
            "status": self.status,
            "error_message": self.error_message,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
