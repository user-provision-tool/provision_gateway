"""EndUser ORM model — registered end-users for service deployment."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from ..database import Base


class EndUser(Base):
    """A registered end-user who can have services deployed for them."""

    __tablename__ = "end_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="viewer")  # 'viewer' | 'special'
    is_approved = Column(Boolean, nullable=False, default=False)  # admin must approve
    is_active = Column(Boolean, nullable=False, default=True)
    allowed_special_users = Column(String(1000), nullable=True)  # comma-separated list
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    approved_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_approved": self.is_approved,
            "is_active": self.is_active,
            "allowed_special_users": (self.allowed_special_users or "").split(",") if self.allowed_special_users else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
        }
