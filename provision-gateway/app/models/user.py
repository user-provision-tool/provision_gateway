"""Unified gateway user (T6)."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from ..database import Base
from . import registry_ref  # noqa: F401 — registers the FK target tables


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "gateway"}

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("registry.api_users.id"),
        nullable=False, unique=True, index=True,
    )
    role = Column(String(16), nullable=False)  # viewer | special | admin
    password_hash = Column(Text, nullable=False)
    is_approved = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    is_public_shared = Column(Boolean, nullable=False, default=False)
    deletion_state = Column(String(16), nullable=False, default="none")
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def account_live(self) -> bool:
        """The ONE liveness rule (R5.7)."""
        return bool(
            self.is_active and self.is_approved
            and self.deleted_at is None and self.deletion_state == "none"
        )

    def to_dict(self, username: str | None = None) -> dict:
        return {
            "id": str(self.id),
            "api_user_id": str(self.api_user_id),
            "username": username,
            "role": self.role,
            "is_approved": bool(self.is_approved),
            "is_active": bool(self.is_active),
            "is_public_shared": bool(self.is_public_shared),
            "deletion_state": self.deletion_state,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
