"""ApiKey ORM model — API tokens for end-user service access."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, ForeignKey

from ..database import Base


class ApiKey(Base):
    """A long-lived JWT API key for an end-user.

    Multiple named keys per user with user-defined labels.
    Keys are revocable individually.
    Default expiry: 1 year from creation.
    """

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("end_users.id"), nullable=False, index=True)
    label = Column(String(255), nullable=False)
    token_hash = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
    is_revoked = Column(Boolean, default=False, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "label": self.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_revoked": self.is_revoked,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }
