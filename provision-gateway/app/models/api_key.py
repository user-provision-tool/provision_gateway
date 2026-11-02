"""API keys — one user id space, no target_type (T6/R5.5)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID

from ..database import Base


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index(
            "uq_api_keys_one_default_py",
            "user_id",
            unique=True,
            postgresql_where=text("is_default AND NOT is_revoked"),
        ),
        {"schema": "gateway"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=False, index=True)
    label = Column(Text, nullable=False)
    token_hash = Column(Text, nullable=False, unique=True)
    mask = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, nullable=False, default=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, owner_username: str | None = None, owner_role: str | None = None) -> dict:
        return {
            "id": self.id,
            "user_id": str(self.user_id),
            "owner": (
                {"username": owner_username, "role": owner_role}
                if owner_username is not None else None
            ),
            "label": self.label,
            "mask": self.mask,
            "is_default": bool(self.is_default),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_revoked": bool(self.is_revoked),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }
